"""Phase 9 end-to-end validation: the scenarios Phase 8 did not already prove, through the real FastAPI stack.
Scenarios 1, 3, 4 (and parts of 5-7) are covered by tests/test_api_e2e.py and tests/test_api_governance.py; this module adds
what was missing: a failing diagnostic tool, ledger-integrity failure, evidence recorded for a governance denial, and
restart behaviour for interrupted investigations and consumed approvals. Offline and deterministic; GitHub is the fake;
SQL Server is NOT involved (scenario 8 is run separately on a machine that has it)."""

import json

import pytest

from app.agent.mcp_client import APPROVED_TOOLS
from app.governance import GateInput
from tests.agent_support import craft_server
from tests.api_support import FakeGitHub, api  # noqa: F401  (api is a fixture)
from tests.recovery_support import run


def decide(e, req, role="admin", decision="approve"):
    return e.post(f"/approvals/{req}/decision", role, json={"decision": decision, "reason": "reviewed"})


def pending(e):
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    rec = e.request_recovery(iid).json()
    assert rec["status"] == "awaiting_approval"
    return iid, rec, rec["approval_request_id"]


# ------------------------------------------------------------------------------------------ scenario 2
def test_scenario_2_a_failing_diagnostic_tool_is_recorded_and_never_turned_into_success(api):
    e = api()

    def failing(_name):
        async def tool():
            raise RuntimeError("simulated diagnostic failure: connection reset")
        return tool

    e.services.investigations.mcp_target_factory = lambda: craft_server({name: failing(name) for name in APPROVED_TOOLS})
    iid = e.new_incident()["incident_id"]
    done = e.investigate(iid)
    # whichever way the agent stops, it must not claim an issue, cite evidence it never got, or leak the raw failure
    assert done["report_status"] != "issue_identified" and done["evidence_refs"] == []
    assert done["status"] in ("completed", "failed")
    blob = json.dumps(done)
    assert "connection reset" not in blob and "Traceback" not in blob
    if done["status"] == "completed":
        assert done["report_status"] in ("inconclusive", "failed", "no_issue_detected") and done["report"]["limitations"]
        assert done["report_status"] != "no_issue_detected"       # no evidence is not evidence of health
    assert e.get(f"/incidents/{iid}", "reader").json()["status"] in ("investigated", "investigation_failed")
    # nothing downstream is possible on the strength of a failed diagnosis
    r = e.request_recovery(iid)
    assert r.status_code in (409, 422) and e.env.executor.count == 0
    assert e.get(f"/incidents/{iid}/recoveries", "reader").json() == [] or r.json().get("status") != "awaiting_approval"


def test_scenario_2b_an_unreachable_diagnostic_service_is_a_failed_investigation(api):
    e = api()

    def unreachable():
        raise OSError("mcp unreachable: 10.0.0.5:1433")

    e.services.investigations.mcp_target_factory = unreachable
    iid = e.new_incident()["incident_id"]
    done = e.investigate(iid)
    assert done["status"] == "failed" and done["report"] is None and done["evidence_refs"] == []
    assert "10.0.0.5" not in json.dumps(done)
    assert e.get(f"/incidents/{iid}/report", "reader").status_code == 404
    assert e.get(f"/incidents/{iid}/status", "reader").json()["workflow_status"] == "investigation_failed"


# ------------------------------------------------------------------------------------------ scenario 3 (extra)
def test_scenario_3_a_retried_unconfirmed_ticket_across_a_restart_never_duplicates(api):
    first = api(name="t", github=FakeGitHub())
    iid = first.new_incident()["incident_id"]
    first.github.mode = "timeout_after_create"
    assert first.post(f"/incidents/{iid}/ticket", "operator").status_code == 503      # outcome unknown: the issue exists
    first.close()
    second = api(name="t", github=first.github, sim=first.env.sim, scenario=None)
    assert second.store.get_ticket(iid)["status"] == "creation_unconfirmed"
    second.github.mode = "ok"
    again = second.post(f"/incidents/{iid}/ticket", "operator")
    assert again.status_code == 200 and again.json()["number"] == 1
    assert second.github.creates == 1 and len(second.github.issues) == 1


# ------------------------------------------------------------------------------------------ scenario 5
def test_scenario_5_a_denial_is_recorded_with_evidence_and_the_api_cannot_clear_it(api):
    e = api()
    iid, rec, req = pending(e)
    denied = run(e.env.governed.govern(GateInput(action="drop_everything", report_status="issue_identified",
                                                 cited_evidence_ids=("E1",), known_evidence_ids=("E1",))))
    assert denied.outcome == "terminal_suspended" and e.env.recorder.is_suspended()[0]
    entries = list(e.env.recorder.ledger.store.all_entries())
    assert any(en.entry_type.value == "TERMINAL" for en in entries)                      # the denial is on the ledger
    assert any(en.payload.get("outcome") == "approval_requested" for en in entries)       # ... next to the evidence trail
    decide(e, req)
    assert e.env.executor.count == 0
    for role in ("admin", "authority", "operator"):                                       # no HTTP route clears it
        for path in ("/governance/re-enable", "/governance/clear", "/governance/resume", "/suspension"):
            assert e.post(path, role, json={}).status_code in (404, 405)
    assert e.env.recorder.is_suspended()[0]
    assert e.env.recorder.ledger.verify_integrity()
    # the audit reports the missing human re-enablement instead of hiding it
    assert "terminal_has_human_authorised_transition" in e.env.recorder.ledger.audit().checks_failed


# ------------------------------------------------------------------------------------------ scenario 6
def test_scenario_6_a_tampered_ledger_fails_closed_for_consequential_actions(api):
    e = api()
    iid, rec, req = pending(e)
    e.env.approvals.approve(req, __import__("tests.recovery_support", fromlist=["TOKENS"]).TOKENS["admin"], "ok")
    lines = e.env.ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2
    entry = json.loads(lines[0])
    entry["payload"] = {**entry.get("payload", {}), "outcome": "forged"}
    lines[0] = json.dumps(entry)
    e.env.ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")                # break the hash chain on disk
    out = e.post(f"/recoveries/{rec['recovery_id']}/resume", "operator")
    assert out.status_code in (200, 409, 503) and out.json().get("status", "") != "executed"
    assert e.env.executor.count == 0
    assert e.get(f"/recoveries/{rec['recovery_id']}", "reader").json()["status"] != "executed"


def test_scenario_6b_an_unreadable_ledger_is_reported_and_blocks_new_recoveries(api):
    e = api()
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    e.env.recorder.is_suspended = lambda: (_ for _ in ()).throw(OSError("ledger unreadable"))
    assert e.client.get("/api/v1/ready").json()["checks"]["governance"] == "ledger_unreadable"
    r = e.request_recovery(iid)
    assert r.status_code != 202 and e.env.executor.count == 0


# ------------------------------------------------------------------------------------------ scenario 7
def test_scenario_7_an_interrupted_investigation_is_shown_as_failed_after_a_restart(api):
    first = api(name="i")
    iid = first.new_incident()["incident_id"]
    inv = first.store.create_investigation(incident_id=iid, mode="offline", requested_by="operator.user")
    first.store.mark_investigation_running(inv["investigation_id"])                       # the process "dies" here
    first.close()
    second = api(name="i", sim=first.env.sim, scenario=None)
    got = second.get(f"/investigations/{inv['investigation_id']}", "reader").json()
    assert got["status"] == "failed" and got["error"]["kind"] == "interrupted" and got["report"] is None
    assert second.get(f"/incidents/{iid}", "reader").json()["status"] == "investigation_failed"
    assert second.investigate(iid)["status"] == "completed"                               # and it can be run again


def test_scenario_7_a_consumed_approval_with_an_uncertain_outcome_is_never_reused_or_retried_after_a_restart(api):
    first = api(name="u")
    iid, rec, req = pending(first)
    first.env.executor.fail_with = RuntimeError("simulated crash after the side effect")
    out = decide(first, req).json()["recovery"]
    assert out["status"] == "recovery_uncertain" and first.env.executor.count == 1
    first.close()

    second = api(name="u", sim=first.env.sim, scenario=None)                               # restart over the same files
    got = second.get(f"/recoveries/{rec['recovery_id']}", "reader").json()
    assert got["status"] == "recovery_uncertain" and got["verified_outcome"] == "unknown"
    assert got["approval"]["status"] == "consumed"
    for _ in range(2):
        assert decide(second, req).status_code == 409
        assert second.post(f"/recoveries/{rec['recovery_id']}/resume", "operator").json()["status"] == "recovery_uncertain"
    assert second.env.executor.count == 0                                                  # nothing ran again, ever
    assert second.get(f"/incidents/{iid}/status", "reader").json()["workflow_status"] == "recovery_uncertain"
    assert second.request_recovery(iid).status_code == 409 or second.env.executor.count == 0
