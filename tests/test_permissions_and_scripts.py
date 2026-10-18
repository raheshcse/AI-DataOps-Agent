import importlib

import pytest

from app.db import permissions as p
from app.db.schema import TABLE_NAMES


def test_identifiers_are_validated_and_quoted():
    assert p.quote_ident("dataops_app") == "[dataops_app]"
    for bad in ("x]; DROP LOGIN sa;--", "a b", "1abc", "", "a" * 65, "a-b"):
        with pytest.raises(ValueError):
            p.quote_ident(bad)


def test_password_literal_escapes_quotes_and_is_isolated_to_create_login():
    assert p.quote_literal("a'b") == "N'a''b'"
    sql = p.create_login_sql("dataops_app", "x'; DROP LOGIN sa;--")
    assert sql.startswith("CREATE LOGIN [dataops_app] WITH PASSWORD = N'x''; DROP LOGIN sa;--'")
    assert "CHECK_POLICY = ON" in sql
    assert not any("PASSWORD" in s for s in p.grant_statements("dataops_app"))


def test_grants_are_least_privilege():
    statements = p.grant_statements("dataops_app")
    text = "\n".join(statements)
    for forbidden in ("db_owner", "db_ddladmin", "ALTER", "CREATE TABLE", "DROP", "CONTROL", "sysadmin",
                      "TRUNCATE"):
        assert forbidden not in text
    selects = [s for s in statements if s.startswith("GRANT SELECT")]
    assert len(selects) == len(TABLE_NAMES)
    deletes = [s for s in statements if s.startswith("GRANT DELETE")]
    assert {s.split("dbo.[")[1].split("]")[0] for s in deletes} == {"orders", "order_items"}
    for protected in ("pipeline_runs", "validation_results", "incident_records", "customers"):
        assert not any(f"[{protected}]" in s and s.startswith(("GRANT UPDATE", "GRANT DELETE")) for s in statements)


@pytest.mark.parametrize("name", ["check_prereqs", "db_init", "db_create_app_login", "db_seed",
                                  "db_verify", "run_pipeline", "scenario", "show", "db_reset"])
def test_every_script_imports_and_exposes_main(name):
    module = importlib.import_module(f"scripts.{name}")
    assert callable(module.main)


def test_reset_script_refuses_without_confirmation(monkeypatch, capsys):
    import sys

    from scripts import db_reset

    monkeypatch.setattr(sys, "argv", ["db_reset", "--database", "dataops_sim"])
    with pytest.raises(SystemExit) as exit_info:
        db_reset.run_cli(db_reset.main)
    assert exit_info.value.code == 3
    assert "--yes-delete-all-data" in capsys.readouterr().out


def test_reset_script_refuses_wrong_database_before_connecting(monkeypatch, capsys):
    import sys

    from scripts import db_reset

    monkeypatch.setattr(sys, "argv", ["db_reset", "--database", "master", "--yes-delete-all-data"])
    with pytest.raises(SystemExit) as exit_info:
        db_reset.run_cli(db_reset.main)
    assert exit_info.value.code == 3 and "must exactly match DB_NAME" in capsys.readouterr().out
