"""Phase 8 end-to-end scenarios 1-7, driven through the actual FastAPI stack (mounted exactly like app.main):
real SQLite operations/approval stores, the real hash-chained ledger, the real Phase 7 workflow and VSL gates,
the real offline agent over the real MCP tools. The recovery effect is the deterministic in-memory simulation and
GitHub is the offline fake: these are NOT live-GitHub or SQL Server tests."""

from app.governance import GateInput
from tests.api_support import FakeGitHub, api  # noqa: F401  (api is a fixture)
from tests.recovery_support import run


def decide(e, req, role="admin", decision="approve"):
    return e.post(f"/approvals/{req}/decision", role, json={"decision": decision, "reason": "reviewed"})


def test_e2e_1_incident_investigation(api):
    e = api()
    inc = e.new_incident(external_ref="OPS-1")
    started = e.post(f"/incidents/{inc['incident_id']}/investigations", "investigator")
    assert started.status_code == 202
    done = e.get(f"/investigations/{started.json()['investigation_id']}", "reader").json()
    assert done["status"] == "completed" and done["report_status"] == "issue_identified" and done["evidence_refs"]
    report = e.get(f"/incidents/{inc['incident_id']}/report", "reader").json()
    assert report["report"]["status"] == "issue_identified"
    assert e.get(f"/incidents/{inc['incident_id']}/status", "reader").json()["workflow_status"] == "investigation_completed"
    assert e.env.executor.count == 0                                          # read-only: no recovery happened


def test_e2e_2_ticket_creation_and_duplicate_request(api):
    e = api(github=FakeGitHub())
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    first, second = e.post(f"/incidents/{iid}/ticket", "operator"), e.post(f"/incidents/{iid}/ticket", "operator")
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["url"] == second.json()["url"] == "https://github.com/acme/dataops/issues/1"
    assert e.github.creates == 1 and len(e.github.issues) == 1
    assert e.get(f"/incidents/{iid}", "reader").json()["ticket"]["number"] == 1
    assert e.env.executor.count == 0


def test_e2e_3_human_approval_and_governed_recovery(api):
    e = api()
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    rec = e.request_recovery(iid).json()
    assert rec["status"] == "awaiting_approval" and e.env.executor.count == 0
    done = decide(e, rec["approval_request_id"]).json()["recovery"]
    assert done["status"] == "executed" and done["verified_outcome"] == "verified" and e.env.executor.count == 1
    again = decide(e, rec["approval_request_id"])
    assert again.status_code == 409 and e.env.executor.count == 1
    outcomes = [ev["outcome"] for ev in done["governance_events"]]
    assert outcomes[:3] == ["recovery_proposed", "approval_requested", "approval_granted"] and outcomes[-1] == "executed"
    assert e.env.recorder.ledger.verify_integrity()
    assert e.get(f"/incidents/{iid}/status", "reader").json()["workflow_status"] == "recovery_completed"


def test_e2e_4_rejected_recovery(api):
    e = api()
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    rec = e.request_recovery(iid).json()
    out = decide(e, rec["approval_request_id"], "admin", "reject").json()
    assert out["recovery"]["status"] == "rejected" and out["recovery"]["phase"] == "recovery_not_executed"
    assert e.env.executor.count == 0 and decide(e, rec["approval_request_id"]).status_code == 409
    assert e.get(f"/incidents/{iid}/status", "reader").json()["workflow_status"] == "recovery_not_executed"


def test_e2e_5_governance_suspension_blocks(api):
    e = api()
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    rec = e.request_recovery(iid).json()
    run(e.env.governed.govern(GateInput(action="drop_everything", report_status="issue_identified",
                                        cited_evidence_ids=("E1",), known_evidence_ids=("E1",))))
    out = decide(e, rec["approval_request_id"])
    assert (out.json().get("recovery") or {}).get("status") != "executed" and e.env.executor.count == 0
    assert e.env.recorder.is_suspended()[0] and e.client.get("/api/v1/ready").json()["checks"]["governance"] == "suspended"
    assert e.get(f"/recoveries/{rec['recovery_id']}", "reader").json()["status"] != "executed"


def test_e2e_6_github_timeout_and_rate_limit(api):
    e = api(github=FakeGitHub())
    iid = e.new_incident()["incident_id"]
    e.github.mode = "timeout"
    t = e.post(f"/incidents/{iid}/ticket", "operator")
    assert t.status_code == 503 and t.json()["error"]["details"]["incident_preserved"] is True
    e.github.mode = "rate_limit"
    r = e.post(f"/incidents/{iid}/ticket", "operator")
    assert r.status_code == 503 and int(r.headers["Retry-After"]) >= 1
    assert e.get(f"/incidents/{iid}", "reader").status_code == 200 and e.store.get_ticket(iid)["url"] is None
    e.github.mode = "ok"
    assert e.post(f"/incidents/{iid}/ticket", "operator").status_code == 201 and len(e.github.issues) == 1


def test_e2e_7_persistence_and_restart(api):
    first = api(name="persist", github=FakeGitHub())
    iid = first.new_incident(external_ref="OPS-7")["incident_id"]
    first.investigate(iid)
    first.post(f"/incidents/{iid}/ticket", "operator")
    rec = first.request_recovery(iid).json()
    first.close()

    second = api(name="persist", github=first.github, sim=first.env.sim, scenario=None)     # restart over the same files
    assert second.get(f"/incidents/{iid}", "reader").json()["ticket"]["number"] == 1
    assert second.get(f"/incidents/{iid}/report", "reader").json()["report_status"] == "issue_identified"
    again = second.get(f"/recoveries/{rec['recovery_id']}", "reader").json()
    assert again["status"] == "awaiting_approval" and again["approval"]["status"] == "pending"   # NEVER silently approved
    assert second.env.executor.count == 0
    assert second.post(f"/incidents/{iid}/ticket", "operator").status_code == 200              # still no duplicate
    assert second.github.creates == 1
    assert decide(second, rec["approval_request_id"]).json()["recovery"]["status"] == "executed"
    assert second.env.executor.count == 1
