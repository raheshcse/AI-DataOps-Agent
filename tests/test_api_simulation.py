"""New /sim endpoints plus Phase 1 behaviour that must not change."""

import pytest
from sqlalchemy.exc import OperationalError

from app.api import simulation as sim_api
from app.api.simulation import DbStatus, get_probe, get_repository
from app.main import app


@pytest.fixture(autouse=True)
def _clean():
    sim_api._engine.cache_clear()
    yield
    app.dependency_overrides.clear()
    sim_api._engine.cache_clear()


@pytest.fixture
def seeded_client(client, service, repo):
    app.dependency_overrides[get_repository] = lambda: repo
    return client


def test_pipelines_endpoint(seeded_client):
    r = seeded_client.get("/sim/pipelines")
    assert r.status_code == 200
    assert [p["pipeline_id"] for p in r.json()] == ["order_ingestion", "order_validation", "reporting_prep"]


def test_runs_endpoint_newest_first_filter_and_limit(seeded_client):
    runs = seeded_client.get("/sim/runs?limit=5").json()
    assert len(runs) == 5
    assert [r["started_at"] for r in runs] == sorted((r["started_at"] for r in runs), reverse=True)
    only = seeded_client.get("/sim/runs?pipeline_id=order_validation&limit=100").json()
    assert len(only) == 7 and {r["pipeline_id"] for r in only} == {"order_validation"}


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=abc", "pipeline_id=Bad;DROP", "pipeline_id=../x"])
def test_runs_rejects_invalid_parameters(seeded_client, query):
    assert seeded_client.get(f"/sim/runs?{query}").status_code == 422


def test_run_detail_with_validations_and_404(seeded_client, repo):
    val_run = next(r for r in repo.runs if r["pipeline_id"] == "order_validation")
    body = seeded_client.get(f"/sim/runs/{val_run['run_id']}").json()
    assert body["run"]["run_id"] == val_run["run_id"] and len(body["validations"]) == 6
    assert seeded_client.get("/sim/runs/RUN-DOES-NOT-EXIST").status_code == 404
    assert seeded_client.get("/sim/runs/bad%20id").status_code == 422


def test_scenarios_endpoint_lists_six_inactive(seeded_client):
    body = seeded_client.get("/sim/scenarios").json()
    assert len(body) == 6 and {s["status"] for s in body} == {"inactive"}
    assert body[0]["scenario_id"] == "SCN-01-PIPELINE-FAILURE"


def test_endpoints_are_read_only(client):
    for path in ("/sim/pipelines", "/sim/runs", "/sim/scenarios", "/sim/db-status"):
        assert client.post(path).status_code == 405
        assert client.delete(path).status_code == 405
    methods = {m for path, ops in app.openapi()["paths"].items() if path.startswith("/sim") for m in ops}
    assert methods == {"get"}


def test_database_errors_become_a_generic_503(client):
    class Broken:
        def list_pipelines(self):
            raise OperationalError("SELECT secret_table", {}, Exception("Login failed for user 'sa' password=hunter2"))

    app.dependency_overrides[get_repository] = lambda: Broken()
    r = client.get("/sim/pipelines")
    assert r.status_code == 503 and r.json() == {"detail": "Simulation database unavailable"}
    assert "hunter2" not in r.text and "secret_table" not in r.text


def test_unseeded_database_returns_503(client, repo):
    app.dependency_overrides[get_repository] = lambda: repo
    r = client.get("/sim/runs")  # repo has no data and no state: empty list, not an error
    assert r.status_code == 200 and r.json() == []
    assert client.get("/sim/scenarios").status_code == 200


def test_unreachable_sql_server_is_reported_safely_without_overrides(client, monkeypatch):
    monkeypatch.setenv("DB_SERVER", "localhost,1")  # nothing listens here
    monkeypatch.setenv("DB_LOGIN_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("DB_AUTH_MODE", "sql")
    monkeypatch.setenv("DB_USER", "dataops_app")
    monkeypatch.setenv("DB_PASSWORD", "super-secret-password-123")
    for path in ("/sim/pipelines", "/sim/runs", "/sim/scenarios", "/sim/db-status"):
        r = client.get(path)
        assert r.status_code == 503, path
        for leak in ("super-secret-password-123", "Traceback", "pyodbc", "dataops_app", "ODBC"):
            assert leak not in r.text, (path, leak)
    assert client.get("/sim/db-status").json()["status"] == "unreachable"


def test_db_status_connected_and_not_assumed_healthy(client):
    app.dependency_overrides[get_probe] = lambda: (
        lambda: DbStatus(status="connected", server="localhost", database="dataops_sim",
                         auth="windows (integrated)", baseline_seeded=True))
    r = client.get("/sim/db-status")
    assert r.status_code == 200 and r.json()["baseline_seeded"] is True


def test_db_status_misconfigured(client, monkeypatch):
    monkeypatch.setenv("DB_NAME", "bad name;")
    r = client.get("/sim/db-status")
    assert r.status_code == 503 and r.json()["status"] == "misconfigured"


def test_ready_still_reports_only_application_configuration(client, with_key):
    """Phase 1 contract: /ready makes no claim about SQL Server."""
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["checks"] == {"configuration": "valid", "openai_api_key": "configured"}
    assert "database" not in r.text.lower() and "sql" not in r.text.lower()


def test_ready_is_unaffected_by_unreachable_database(client, with_key, monkeypatch):
    monkeypatch.setenv("DB_SERVER", "localhost,1")
    assert client.get("/ready").status_code == 200
    assert client.get("/health").status_code == 200
