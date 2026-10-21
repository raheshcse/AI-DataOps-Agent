"""Simulator incident discovery and idempotent import (Phase 11 A/B)."""
from __future__ import annotations

from tests.api_support import api  # noqa: F401


def _sim_incidents(e):
    return e.get("/simulator/incidents", "reader").json()


def test_simulator_incidents_discoverable_and_read_only(api):
    e = api()
    before = e.env.sim.repo.list_incidents(1000)
    r = e.get("/simulator/incidents", "reader")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "simulator" and body["total"] == len(before) and body["total"] >= 1
    first = body["items"][0]
    assert first["imported"] is False and first["synthetic"] is True
    d = e.get(f"/simulator/incidents/{first['incident_id']}", "reader")
    assert d.status_code == 200 and "evidence_note" in d.json()
    assert e.env.sim.repo.list_incidents(1000) == before  # nothing written to the simulator store


def test_filters_search_and_validation(api):
    e = api()
    total = _sim_incidents(e)["total"]
    assert e.get("/simulator/incidents?severity=critical&status=resolved", "reader").json()["total"] <= total
    assert e.get("/simulator/incidents?q=zzzz-no-such", "reader").json()["total"] == 0
    assert e.get("/simulator/incidents?limit=0", "reader").status_code == 422
    assert e.get("/simulator/incidents/NOPE", "reader").status_code == 422
    assert e.get("/simulator/incidents/INC-9999", "reader").status_code == 404


def test_requires_authentication_and_import_permission(api):
    e = api()
    assert e.get("/simulator/incidents").status_code == 401
    iid = _sim_incidents(e)["items"][0]["incident_id"]
    assert e.post(f"/simulator/incidents/{iid}/import", "reader").status_code == 403


def test_import_is_idempotent_and_creates_no_governance_events(api):
    e = api()
    iid = _sim_incidents(e)["items"][0]["incident_id"]
    ledger_before = len(list(e.env.recorder.ledger.store.all_entries()))
    r1 = e.post(f"/simulator/incidents/{iid}/import", "operator")
    r2 = e.post(f"/simulator/incidents/{iid}/import", "operator")
    assert r1.status_code == 201 and r1.json()["created"] is True
    assert r2.status_code == 200 and r2.json()["created"] is False
    assert r1.json()["incident"]["incident_id"] == r2.json()["incident"]["incident_id"]
    assert r1.json()["incident"]["status"] == "new"
    assert len(list(e.env.recorder.ledger.store.all_entries())) == ledger_before
    listed = e.get("/incidents?limit=100", "reader").json()
    assert sum(1 for i in listed["items"] if i.get("external_ref") == f"sim:{iid}") == 1
    now = _sim_incidents(e)["items"][0]
    assert now["imported"] is True and now["workflow_incident_id"] == r1.json()["incident"]["incident_id"]
    assert e.get("/simulator/incidents?imported=false", "reader").json()["total"] == _sim_incidents(e)["total"] - 1


def test_unavailable_simulator_is_a_clean_503(api):
    e = api()
    e.client.app.routes  # noqa: B018
    for route in e.client.app.routes:
        if getattr(route, "path", "") == "/api/v1":
            route.app.state.sim_repository_factory = None
    r = e.get("/simulator/incidents", "reader")
    assert r.status_code == 503 and r.json()["error"]["code"] == "simulator_unavailable"


def test_incident_search_and_origin_filter_and_global_recovery_list(api):
    e = api()
    iid = _sim_incidents(e)["items"][0]["incident_id"]
    e.post(f"/simulator/incidents/{iid}/import", "operator")
    e.new_incident(title="Unrelated duplicate order report")
    assert e.get("/incidents?origin=simulator", "reader").json()["total"] == 1
    assert e.get("/incidents?q=Unrelated", "reader").json()["total"] == 1
    assert e.get("/incidents?q=%25", "reader").json()["total"] == 0  # a literal % is not a wildcard
    assert e.get("/incidents?q=x'%3B--", "reader").status_code == 200
    assert e.get("/recoveries", "reader").json()["total"] == 0
    assert e.get("/recoveries?status=BAD;", "reader").status_code == 422


def test_pipelines_and_runs_require_auth_and_are_read_only(api):
    e = api()
    assert e.get("/simulator/pipelines").status_code == 401
    p = e.get("/simulator/pipelines", "reader").json()
    assert p["total"] >= 1 and p["source"] == "simulator"
    runs = e.get("/simulator/runs?limit=5", "reader").json()
    assert len(runs["items"]) <= 5
    assert e.get("/simulator/runs?pipeline_id=Bad;Id", "reader").status_code == 422
    assert e.post("/simulator/runs", "admin", json={}).status_code == 405
