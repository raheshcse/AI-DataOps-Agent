"""Test isolation: ordinary tests never read the real environment or a real .env."""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

CONFIG_VARS = [
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "APP_ENV",
    "LOG_LEVEL",
    "API_HOST",
    "API_PORT",
    "DB_SERVER",
    "DB_NAME",
    "DB_AUTH_MODE",
    "DB_USER",
    "DB_PASSWORD",
    "DB_DRIVER",
    "DB_ENCRYPT",
    "DB_TRUST_SERVER_CERTIFICATE",
    "DB_LOGIN_TIMEOUT_SECONDS",
    "SIM_SEED",
    "SIM_LOG_DIR",
    "FRESHNESS_THRESHOLD_MINUTES",
    "RUN_SQLSERVER_TESTS",
]

FAKE_KEY = "sk-test-FAKEFAKEFAKE1234567890"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for name in CONFIG_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # so a developer's real .env is never picked up
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    # raise_server_exceptions=False so the 500 handler is exercised, not re-raised.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    return FAKE_KEY


@pytest.fixture
def repo():
    from tests.fakes import InMemoryRepo

    return InMemoryRepo()


@pytest.fixture
def service(repo, tmp_path):
    """A seeded SimulationService on the in-memory repository, logging under tmp_path."""
    from app.simulation.runlog import RunLogStore
    from app.simulation.service import SimulationService

    svc = SimulationService(repo, RunLogStore(tmp_path / "logs"))
    svc.seed()
    return svc
