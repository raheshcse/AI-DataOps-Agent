"""Human Review API, demo dataset API, dashboard summary (Phase 11 A/C/D)."""
from __future__ import annotations

from datetime import timedelta

from tests.api_support import api  # noqa: F401


def decide(e, req, role="admin", decision="approve"):
    return e.post(f"/approvals/{req}/decision", role, json={"decision": decision, "reason": "reviewed"})


def pending_recovery(e):
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    return iid, e.request_recovery(iid).json()


def test_queue_is_empty_without_real_pending_state(api):
    e = api()
    e.new_incident()
    body = e.get("/reviews", "reader").json()
    assert body["total"] == 0 and e.get("/reviews/summary", "reader").json()["pending"] == 0


def test_pending_item_comes_from_persisted_state_and_separates_statements(api):
    e = api()
    iid, rec = pending_recovery(e)
    q = e.get("/reviews", "reader").json()
    assert q["total"] == 1 and q["items"][0]["incident_id"] == iid and q["items"][0]["awaiting_decision"] is True
    d = e.get(f"/reviews/{rec['recovery_id']}", "reader").json()
    for k in ("confirmed_evidence", "agent_hypothesis", "recommendation", "human_decision"):
        assert k in d
    assert d["human_decision"]["decided_by"] is None and d["clarification_supported"] is False
    assert d["supported_decisions"] == ["approve", "reject"]
    assert e.get("/reviews/summary", "reader").json()["pending"] == 1
    assert e.env.executor.count == 0  # viewing a review executes nothing


def test_unauthorised_cannot_decide_and_nothing_changes(api):
    e = api()
    _, rec = pending_recovery(e)
    req = rec["approval_request_id"]
    assert decide(e, req, "reader").status_code == 403
    assert e.post(f"/approvals/{req}/decision", None, json={"decision": "approve"}).status_code == 401
    assert e.get("/reviews/summary", "reader").json()["pending"] == 1 and e.env.executor.count == 0


def test_decision_persists_moves_item_out_of_pending_and_is_not_repeatable(api):
    e = api()
    _, rec = pending_recovery(e)
    req = rec["approval_request_id"]
    assert decide(e, req, "admin", "reject").status_code == 200
    assert e.get("/reviews", "reader").json()["total"] == 0
    hist = e.get("/reviews?state=decided", "reader").json()
    assert hist["total"] == 1 and hist["items"][0]["review_state"] == "rejected" and hist["items"][0]["decided_by"]
    assert decide(e, req, "admin", "approve").status_code == 409
    assert e.env.executor.count == 0


def test_approval_goes_through_governed_workflow_only(api):
    e = api()
    _, rec = pending_recovery(e)
    assert decide(e, rec["approval_request_id"]).json()["recovery"]["status"] == "executed"
    assert e.env.executor.count == 1 and e.env.recorder.ledger.verify_integrity()
    assert e.get("/reviews", "reader").json()["total"] == 0
    assert e.get("/reviews?state=decided", "reader").json()["items"][0]["review_state"] == "approved"


def test_expired_request_is_not_pending(api):
    e = api()
    _, rec = pending_recovery(e)
    e.env.clock.advance(days=30)
    assert e.get("/reviews", "reader").json()["total"] == 0
    assert e.get("/reviews?state=expired", "reader").json()["total"] == 1


def test_unknown_review_and_auth(api):
    e = api()
    assert e.get("/reviews/nope", "reader").status_code == 404
    assert e.get("/reviews").status_code == 401


def test_demo_dataset_api_is_read_only_and_labelled(api):
    e = api()
    s = e.get("/demo/summary", "reader").json()
    assert s["total"] == 100 and s["synthetic"] is True and len(s["by_category"]) == 20
    page = e.get("/demo/incidents?limit=100", "reader").json()
    assert page["total"] == 100 and page["banner"] and all(i["synthetic"] and i["incident_id"].startswith("DEMO-") for i in page["items"])
    assert e.get("/demo/incidents?severity=critical", "reader").json()["total"] == s["by_severity"]["critical"]
    d = e.get("/demo/incidents/DEMO-0001", "reader").json()
    assert d["classification"] == "demo_synthetic" and d["statements"]["human_decision"] is None
    assert e.get("/demo/incidents/DEMO-9999", "reader").status_code == 404
    assert e.post("/demo/incidents", "admin", json={}).status_code in (404, 405)


def test_demo_data_never_enters_real_stores_or_review_queue(api):
    e = api()
    before = (e.get("/incidents?limit=100", "reader").json()["total"], len(list(e.env.recorder.ledger.store.all_entries())))
    e.get("/demo/incidents", "reader"); e.get("/dashboard/summary", "reader")
    after = (e.get("/incidents?limit=100", "reader").json()["total"], len(list(e.env.recorder.ledger.store.all_entries())))
    assert before == after
    assert e.get("/reviews", "reader").json()["total"] == 0  # demo "human_review_required" flags do not create queue items


def test_dashboard_summary_keeps_sources_separate(api):
    e = api()
    pending_recovery(e)
    s = e.get("/dashboard/summary", "reader").json()
    assert s["human_review"] == {"pending": 1, "available": True}
    assert s["simulator"]["available"] is True and s["demo"]["synthetic"] is True
    assert s["workflow_incident_total"] == 1  # demo and simulator incidents are not added to the workflow total
