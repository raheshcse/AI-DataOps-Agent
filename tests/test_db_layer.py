"""Database-independent tests of the DB layer: schema, SQL compilation, connection
strings, secret handling and failure behaviour. No SQL Server is contacted."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import mssql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.config import Settings
from app.db import schema as s
from app.db.connection import (
    DatabaseConfigError,
    DatabaseUnavailable,
    build_odbc_connection_string,
    check_connection,
    describe_target,
    is_local_server,
    make_engine,
    safe_error_summary,
)
from app.db.repository import READABLE_SCHEMA_TABLES, SqlRepository, type_family
from app.db.safety import UnsafeTarget, assert_safe_reset_target
from app.simulation.schema_contract import ORDER_FEED_CONTRACT
from tests.fakes import DRIFTED_OBSERVED

PASSWORD = "p@ss;word}x=1"


def sql_settings(**kw):
    base = dict(db_auth_mode="sql", db_user="dataops_app", db_password=PASSWORD,
                db_server=r"localhost\SQLEXPRESS03")
    base.update(kw)
    return Settings(**base)


# ---------------------------------------------------------------- schema / DDL
def test_every_table_compiles_for_sql_server():
    dialect = mssql.dialect()
    dialect.server_version_info = (15, 0, 2000)
    for table in s.TABLE_ORDER:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert ddl.startswith(f"\nCREATE TABLE {table.name}")
        for index in table.indexes:
            str(CreateIndex(index).compile(dialect=dialect))
    assert "DATETIME2(3)" in str(CreateTable(s.orders).compile(dialect=dialect))
    assert "batch_date DATE NOT NULL" in str(CreateTable(s.orders).compile(dialect=dialect))


def test_all_twelve_documented_tables_exist_with_foreign_keys():
    names = {t.name for t in s.TABLE_ORDER}
    assert {"customers", "products", "orders", "order_items", "pipeline_definitions",
            "pipeline_runs", "incident_records"} <= names
    assert len(names) == 12
    fk = {(t.name, fk.column.table.name) for t in s.TABLE_ORDER for c in t.c for fk in c.foreign_keys}
    assert {("orders", "customers"), ("order_items", "orders"), ("order_items", "products"),
            ("pipeline_runs", "pipeline_definitions"), ("incident_records", "pipeline_runs")} <= fk


def test_table_order_places_parents_before_children():
    seen = set()
    for t in s.TABLE_ORDER:
        for c in t.c:
            for fk in c.foreign_keys:
                assert fk.column.table.name in seen, f"{t.name} before {fk.column.table.name}"
        seen.add(t.name)


def test_orders_source_event_id_is_not_unique_so_duplicates_are_detectable():
    assert not any(getattr(c, "columns", None) and [x.name for x in c.columns] == ["source_event_id"]
                   for c in s.orders.constraints if c.__class__.__name__ == "UniqueConstraint")


def test_feed_contract_matches_table_definitions():
    assert {c.name: type_family(c.type) for c in s.stg_order_feed.columns} == ORDER_FEED_CONTRACT
    assert {c.name: type_family(c.type) for c in s.fx_order_feed_drifted.columns} == DRIFTED_OBSERVED


def test_type_family_handles_reflected_sql_server_types():
    from sqlalchemy import types as t

    assert type_family(mssql.INTEGER()) == "int"
    assert type_family(mssql.DATETIME2()) == "datetime"
    assert type_family(t.DATE()) == "date"
    assert type_family(t.NUMERIC(12, 2)) == "decimal"
    assert type_family(mssql.NVARCHAR()) == "string"
    assert type_family(mssql.VARCHAR(20)) == "string"
    assert type_family(mssql.BIT()) == "bool"
    assert type_family(mssql.UNIQUEIDENTIFIER()) == "uniqueidentifier"


def test_utc_datetime_roundtrip_and_rejects_naive():
    col = s.UTCDateTime()
    aware = datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)
    stored = col.process_bind_param(aware, mssql.dialect())
    assert stored.tzinfo is None
    assert col.process_result_value(stored, mssql.dialect()) == aware
    with pytest.raises(ValueError):
        col.process_bind_param(datetime(2026, 1, 15), mssql.dialect())


def test_schema_inspection_is_restricted_to_feed_tables():
    repo = SqlRepository(engine=None)  # never reaches the engine for a disallowed name
    assert READABLE_SCHEMA_TABLES == {"stg_order_feed", "fx_order_feed_drifted"}
    with pytest.raises(ValueError):
        repo.observed_schema("customers; DROP TABLE orders")


# ----------------------------------------------------------- connection strings
def test_windows_auth_connection_string():
    cs = build_odbc_connection_string(Settings(db_server=r".\SQLEXPRESS03"))
    assert "SERVER=.\\SQLEXPRESS03;" in cs and "Trusted_Connection=yes" in cs
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in cs and "UID=" not in cs
    assert "Encrypt=yes" in cs and "TrustServerCertificate=no" in cs


def test_sql_auth_string_escapes_special_characters():
    cs = build_odbc_connection_string(sql_settings())
    assert "UID={dataops_app}" in cs
    assert "PWD={p@ss;word}}x=1}" in cs  # ';' contained, '}' doubled
    assert "Trusted_Connection" not in cs


def test_masked_string_and_target_description_hide_the_password():
    masked = build_odbc_connection_string(sql_settings(), mask=True)
    assert PASSWORD not in masked and "PWD={***}" in masked
    assert PASSWORD not in str(describe_target(sql_settings()))
    assert PASSWORD not in repr(sql_settings()) and PASSWORD not in sql_settings().model_dump_json()


def test_admin_connections_always_use_windows_auth_and_may_target_master():
    cs = build_odbc_connection_string(sql_settings(), database="master", admin=True)
    assert "DATABASE=master" in cs and "Trusted_Connection=yes" in cs and "PWD" not in cs


def test_sql_auth_without_credentials_is_a_config_error():
    with pytest.raises(DatabaseConfigError):
        build_odbc_connection_string(Settings(db_auth_mode="sql"))


@pytest.mark.parametrize("name,value", [
    ("DB_SERVER", "host;Trusted_Connection=no"), ("DB_NAME", "x;DROP"), ("DB_NAME", "1bad"),
    ("DB_DRIVER", "{evil}"), ("DB_AUTH_MODE", "kerberos"), ("DB_USER", "a;b"),
    ("DB_LOGIN_TIMEOUT_SECONDS", "0"), ("FRESHNESS_THRESHOLD_MINUTES", "0"),
])
def test_invalid_database_settings_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_database_defaults_and_blank_credentials(monkeypatch):
    monkeypatch.setenv("DB_USER", "  ")
    monkeypatch.setenv("DB_PASSWORD", "")
    st = Settings()
    assert (st.db_server, st.db_name, st.db_auth_mode) == ("localhost", "dataops_sim", "windows")
    assert st.db_user is None and st.db_password is None
    assert st.freshness_threshold_minutes == 1500 and st.sim_seed == 20260115


@pytest.mark.parametrize("server,expected", [
    ("localhost", True), (r"localhost\SQLEXPRESS03", True), (r".\SQLEXPRESS03", True),
    ("(local)", True), ("tcp:127.0.0.1,1433", True), ("prod-sql-01", False),
    (r"prod-sql-01\SQLEXPRESS", False), ("10.0.0.5", False),
])
def test_is_local_server(server, expected):
    assert is_local_server(server) is expected


# ------------------------------------------------------------- failure handling
def test_unavailable_driver_or_server_gives_safe_error():
    """In environments without the ODBC runtime make_engine raises DatabaseUnavailable;
    with it, check_connection does. Either way the message is safe and secret-free."""
    st = sql_settings(db_server="localhost,1", db_login_timeout_seconds=1)  # nothing listens here
    with pytest.raises(DatabaseUnavailable) as info:
        check_connection(make_engine(st))
    assert PASSWORD not in str(info.value) and "dataops_app" not in str(info.value)


def test_safe_error_summary_redacts_secrets():
    err = RuntimeError("Login failed; password=hunter2 sk-abcdefgh12345678")
    summary = safe_error_summary(err)
    assert "hunter2" not in summary and "sk-abcdefgh12345678" not in summary


# --------------------------------------------------------------- reset safeguards
def guard(**kw):
    args = dict(settings=Settings(db_name="dataops_sim"), database_arg="dataops_sim", confirmed=True)
    args.update(kw)
    assert_safe_reset_target(**args)


def test_reset_guard_allows_the_intended_local_sim_database():
    guard()
    guard(settings=Settings(db_name="dataops_sim_test"), database_arg="dataops_sim_test")


@pytest.mark.parametrize("kw", [
    dict(confirmed=False),
    dict(database_arg=None),
    dict(database_arg="other_sim"),
    dict(settings=Settings(db_name="sales_prod"), database_arg="sales_prod"),
    dict(settings=Settings(db_server="prod-sql-01", db_name="dataops_sim")),
    dict(settings=Settings(app_env="production", db_name="dataops_sim")),
])
def test_reset_guard_refuses_unsafe_targets(kw):
    with pytest.raises(UnsafeTarget):
        guard(**kw)
