"""SQL Server integration tests.

They run only when RUN_SQLSERVER_TESTS=1 and a LOCAL SQL Server instance is reachable
with Windows authentication. They use a dedicated database whose name ends in
``_test`` (default ``dataops_sim_test``, created on demand) and never touch
``dataops_sim``. Skipped otherwise, and a skip is NOT a pass.
"""

import os

import pytest
from sqlalchemy import text

from app.config import Settings
from app.db.connection import (
    DatabaseUnavailable,
    check_connection,
    is_local_server,
    make_engine,
)
from app.db.repository import SqlRepository
from app.db.schema import metadata
from app.simulation.runlog import RunLogStore
from app.simulation.service import SimulationService

TEST_DB = os.environ.get("TEST_DB_NAME", "dataops_sim_test")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "tests/integration" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def sql_settings():
    if os.environ.get("RUN_SQLSERVER_TESTS") != "1":
        pytest.skip("Set RUN_SQLSERVER_TESTS=1 (and DB_SERVER) to run SQL Server integration tests.")
    assert TEST_DB.lower().endswith("_test"), "TEST_DB_NAME must end with _test"
    settings = Settings(db_name=TEST_DB, db_auth_mode="windows",
                        db_server=os.environ.get("DB_SERVER", r"localhost"),
                        db_trust_server_certificate=os.environ.get("DB_TRUST_SERVER_CERTIFICATE", "true").lower() == "true")
    assert is_local_server(settings.db_server), "Integration tests only run against a local server."
    return settings


@pytest.fixture(scope="session")
def engine(sql_settings):
    admin_master = make_engine(sql_settings, database="master", admin=True)
    try:
        check_connection(admin_master)
    except DatabaseUnavailable as exc:
        pytest.skip(f"SQL Server not reachable: {exc}")
    with admin_master.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"IF DB_ID(N'{TEST_DB}') IS NULL CREATE DATABASE [{TEST_DB}]"))
    eng = make_engine(sql_settings, admin=True)
    metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def svc(engine, sql_settings, tmp_path):
    """A freshly reset baseline in the *_test database, logging under tmp_path."""
    from app.db.safety import assert_safe_reset_target

    assert_safe_reset_target(sql_settings, TEST_DB, confirmed=True)
    service = SimulationService(SqlRepository(engine), RunLogStore(tmp_path / "logs"),
                                seed=sql_settings.sim_seed,
                                freshness_threshold_minutes=sql_settings.freshness_threshold_minutes)
    service.repo.wipe()
    service.seed()
    return service


@pytest.fixture
def dx(svc):
    """Diagnostics over the same freshly seeded *_test database (overrides the in-memory fixture)."""
    from app.diagnostics import DiagnosticsService

    return DiagnosticsService(svc.repo, svc.logs, svc.threshold)
