"""Phase 8 security and governance tests: the API is never an alternate path to a protected executor.
Every denied protected action asserts the recovery side-effect counter (env.executor.count) is still zero."""

import ast
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from app.governance import GateInput, GovernedActionService
from app.governance.policy import build_policy
from app.recovery import RECOVERY_ACTIONS, RecoveryRunner, RecoveryWorkflow
from tests.api_support import API_TOKENS, FakeGitHub, api, auth  # noqa: F401  (api is a fixture)
from tests.recovery_support import TOKENS, DroppingStore, FailingStore, run, sqlite_update

ROOT = Path(__file__).resolve().parents[1]


def pending(e, **req):
    """An incident, a completed investigation and a recovery waiting for approval."""
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    r = e.request_recovery(iid, **req)
    assert r.status_code == 202, r.text
    rec = r.json()
    return iid, rec, rec["approval_request_id"]


def decide(e, request_id, role="admin", decision="approve", **extra):
    return e.post(f"/approvals/{request_id}/decision", role, json={"decision": decision, "reason": "checked", **extra})


# ------------------------------------------------------------------------------------------ requesting never executes
def test_a_recovery_request_stops_at_approval_and_never_executes(api):
    e = api()
    iid, rec, req = pending(e)
    assert rec["status"] == "awaiting_approval" and rec["phase"] == "awaiting_approval" and rec["execution_status"] == "not_run"
    assert rec["approval"]["status"] == "pending" and rec["action_type"] == "remove_injected_condition"
    assert [ev["outcome"] for ev in rec["governance_events"]] == ["recovery_proposed", "approval_requested"]
    assert e.env.executor.count == 0
    assert e.get(f"/incidents/{iid}/status", "reader").json()["workflow_status"] == "awaiting_approval"
    assert e.get(f"/recoveries/{rec['recovery_id']}", "reader").json()["status"] == "awaiting_approval"


def test_no_recovery_without_a_completed_investigation(api):
    e = api()
    iid = e.new_incident()["incident_id"]
    r = e.request_recovery(iid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_completed_investigation" and e.env.executor.count == 0


def test_only_one_active_recovery_per_incident(api):
    e = api()
    iid, rec, _ = pending(e)
    again = e.request_recovery(iid)
    assert again.status_code == 409 and again.json()["error"]["code"] == "recovery_in_progress"
    assert again.json()["error"]["details"]["recovery_id"] == rec["recovery_id"] and e.env.executor.count == 0


@pytest.mark.parametrize("body", [
    {"action_type": "drop_table", "parameters": {"x": "y"}}, {"action_type": "rerun_pipeline"}, {"parameters": {"pipeline_id": "order_ingestion"}},
    {"action_type": "rerun_pipeline", "parameters": {"pipeline_id": "order_ingestion"}, "approved": True},
    {"action_type": "rerun_pipeline", "parameters": {"pipeline_id": "order_ingestion"}, "approver": "admin.user"},
])
def test_unlisted_or_smuggled_fields_are_rejected_before_anything_happens(api, body):
    e = api()
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    r = e.request_recovery(iid, json=body)
    assert r.status_code == 422 and e.env.executor.count == 0 and e.store.list_recoveries(iid) == []


def test_an_allowlisted_action_with_bad_parameters_is_refused_by_phase7_validation(api):
    e = api()
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    r = e.request_recovery(iid, json={"action_type": "rerun_pipeline", "parameters": {"pipeline_id": "not_a_pipeline; DROP TABLE x"}})
    assert r.status_code == 409 and r.json()["status"] != "awaiting_approval" and e.env.executor.count == 0
    assert e.store.list_recoveries(iid)[0]["status"] == r.json()["status"]


def test_recovery_is_unavailable_when_not_configured(api):
    e = api(recovery=False)
    iid = e.new_incident()["incident_id"]
    r = e.request_recovery(iid)
    assert r.status_code == 503 and r.json()["error"]["code"] == "recovery_unavailable" and e.env.executor.count == 0
    assert e.post("/recoveries/rec-x/resume", "operator").status_code == 503
    assert e.client.get("/api/v1/ready").json()["checks"]["recovery"] == "unavailable"


# ------------------------------------------------------------------------------------------ identity & permission
def test_the_caller_cannot_name_the_approver_or_assert_approval(api):
    e = api()
    _, rec, req = pending(e)
    for extra in ({"approved": True}, {"approver": "admin.user"}, {"decided_by": "admin.user"}, {"roles": ["recovery_admin"]}):
        assert decide(e, req, "operator", **extra).status_code in (403, 422), extra   # refused either way
        assert decide(e, req, "admin", **extra).status_code == 422, extra
    assert e.post(f"/approvals/{req}/decision", "admin", json={"approved": True}).status_code == 422
    assert e.post(f"/approvals/{req}/decision", "admin", json={"decision": "maybe"}).status_code == 422
    assert e.get(f"/approvals/{req}", "reader").json()["status"] == "pending" and e.env.executor.count == 0


def test_the_approver_recorded_is_the_authenticated_identity(api):
    e = api()
    _, _, req = pending(e)
    r = e.client.post(f"/api/v1/approvals/{req}/decision", headers={**auth("admin"), "X-User": "someone.else"},
                      json={"decision": "approve", "reason": "ok"})
    assert r.status_code == 200 and r.json()["approval"]["decided_by"] == "admin.user"


@pytest.mark.parametrize("role", ["operator", "investigator", "reader", "viewer", "authority"])
def test_callers_without_approval_rights_cannot_decide(api, role):
    e = api()
    _, _, req = pending(e)
    r = decide(e, req, role)
    assert r.status_code == 403 and e.env.executor.count == 0
    assert e.get(f"/approvals/{req}", "reader").json()["status"] == "pending"


def test_an_approver_role_cannot_approve_an_action_it_is_not_permitted(api):
    e = api()
    _, _, req = pending(e)                       # remove_injected_condition: admin only
    r = decide(e, req, "approver")
    assert r.status_code == 403 and r.json()["error"]["details"]["reason"] == "not_permitted" and e.env.executor.count == 0
    assert e.get(f"/approvals/{req}", "reader").json()["status"] == "pending"


def test_unauthenticated_decisions_are_refused(api):
    e = api()
    _, _, req = pending(e)
    assert decide(e, req, None).status_code == 401 and e.env.executor.count == 0


def test_a_revoked_approver_cannot_decide(api):
    e = api(revoked={"admin"})
    _, _, req = pending(e)
    assert decide(e, req, "admin").status_code == 401 and e.env.executor.count == 0


def test_a_valid_api_token_is_not_an_approval(api):
    e = api()
    iid, rec, req = pending(e)
    e.post(f"/recoveries/{rec['recovery_id']}/resume", "operator")
    e.post(f"/recoveries/{rec['recovery_id']}/resume", "admin")
    assert e.get(f"/recoveries/{rec['recovery_id']}", "admin").json()["status"] == "awaiting_approval" and e.env.executor.count == 0


# ------------------------------------------------------------------------------------------ the approved path
def test_an_approved_recovery_executes_exactly_once_and_is_verified(api):
    e = api()
    iid, rec, req = pending(e)
    active_before = set(e.env.sim.active_scenario_ids())
    r = decide(e, req, "admin")
    assert r.status_code == 200
    out = r.json()["recovery"]
    assert out["status"] == "executed" and out["verified_outcome"] == "verified" and out["phase"] == "recovery_completed"
    assert out["approval"]["status"] == "consumed" and e.env.executor.count == 1
    assert set(e.env.sim.active_scenario_ids()) != active_before                      # the effect really happened
    kinds = [ev["outcome"] for ev in out["governance_events"]]
    assert kinds[:2] == ["recovery_proposed", "approval_requested"] and "approval_granted" in kinds
    assert kinds.index("authorised") < kinds.index("recovery_attempt") < kinds.index("recovery_succeeded") < kinds.index("executed")
    assert e.env.recorder.ledger.verify_integrity()
    from app.governance import audit_cli
    assert audit_cli.recovery_attempts_without_result(e.env.recorder.ledger.store.all_entries()) == []
    assert e.get(f"/incidents/{iid}/status", "reader").json()["workflow_status"] == "recovery_completed"


def test_replaying_the_decision_or_resume_never_executes_again(api):
    e = api()
    _, rec, req = pending(e)
    assert decide(e, req, "admin").status_code == 200
    for _ in range(3):
        again = decide(e, req, "admin")
        assert again.status_code == 409 and again.json()["error"]["details"]["reason"] in ("not_pending", "consumed")
        assert e.post(f"/recoveries/{rec['recovery_id']}/resume", "operator").json()["status"] == "executed"
    assert e.env.executor.count == 1


def test_a_concurrent_decision_race_executes_at_most_once(api):
    e = api()
    _, rec, req = pending(e)
    results, barrier = [], threading.Barrier(6)

    def go():
        barrier.wait()
        results.append(decide(e, req, "admin").status_code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(200) == 1 and e.env.executor.count == 1
    assert e.get(f"/recoveries/{rec['recovery_id']}", "reader").json()["status"] == "executed"


def test_a_rejected_recovery_never_executes_and_is_reported_honestly(api):
    e = api()
    iid, rec, req = pending(e)
    r = decide(e, req, "admin", "reject")
    out = r.json()
    assert r.status_code == 200 and out["approval"]["status"] == "rejected" and out["recovery"]["status"] == "rejected"
    assert out["recovery"]["phase"] == "recovery_not_executed" and out["recovery"]["verified_outcome"] == "not_applicable"
    assert e.env.executor.count == 0
    assert decide(e, req, "admin").status_code == 409                       # cannot approve a rejected request afterwards
    assert e.post(f"/recoveries/{rec['recovery_id']}/resume", "operator").json()["status"] == "rejected"
    assert e.env.executor.count == 0


def test_an_expired_approval_cannot_execute(api):
    e = api()
    _, rec, req = pending(e)
    e.env.clock.advance(minutes=31)
    r = decide(e, req, "admin")
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "expired" and e.env.executor.count == 0


def test_an_approval_edited_in_the_database_cannot_execute(api):
    e = api()
    _, rec, req = pending(e)
    sqlite_update(e.env.db_path, "UPDATE approval_requests SET status='approved', decided_by='admin.user' WHERE request_id=?", (req,))
    r = e.post(f"/recoveries/{rec['recovery_id']}/resume", "operator")
    assert r.json()["status"] != "executed" and e.env.executor.count == 0


def test_a_parameter_changed_after_approval_cannot_execute(api):
    e = api()
    _, rec, req = pending(e)
    e.env.approvals.approve(req, TOKENS["admin"], "ok")
    sqlite_update(e.env.db_path, "UPDATE approval_requests SET parameters_json=? WHERE request_id=?", ('{"scenario_key":"stale-data"}', req))
    out = e.post(f"/recoveries/{rec['recovery_id']}/resume", "operator").json()
    assert out["status"] != "executed" and e.env.executor.count == 0


# ------------------------------------------------------------------------------------------ VSL and suspension
def test_a_governance_suspension_blocks_an_otherwise_valid_approval(api):
    e = api()
    iid, rec, req = pending(e)
    bad = run(e.env.governed.govern(GateInput(action="drop_everything", report_status="issue_identified",
                                              cited_evidence_ids=("E1",), known_evidence_ids=("E1",))))
    assert bad.outcome == "terminal_suspended"
    r = decide(e, req, "admin")
    assert e.env.executor.count == 0
    assert (r.json().get("recovery") or {}).get("status") != "executed"
    assert e.env.recorder.is_suspended()[0]
    assert e.client.get("/api/v1/ready").json()["checks"]["governance"] == "suspended"
    assert e.get(f"/recoveries/{rec['recovery_id']}", "reader").json()["status"] != "executed"
    assert e.env.executor.count == 0 and e.env.recorder.is_suspended()[0]     # and nothing cleared it


def test_no_api_operation_can_clear_a_suspension(api):
    e = api()
    e.env.recorder.mark_suspended_locally()
    for role in ("admin", "authority", "operator"):
        for path in ("/governance/re-enable", "/governance/clear", "/governance"):
            assert e.post(path, role, json={}).status_code in (404, 405)
    assert e.env.recorder.is_suspended()[0]


def test_a_suspension_refuses_new_recovery_requests(api):
    e = api()
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    e.env.recorder.mark_suspended_locally()
    r = e.request_recovery(iid)
    assert r.status_code == 409 and r.json()["status"] == "refused_suspended" and e.env.executor.count == 0


def test_ledger_write_failure_blocks_the_recovery_before_the_side_effect(api):
    e = api()
    _, rec, req = pending(e)
    original = e.env.recorder.ledger.store
    e.env.recorder.ledger.store = FailingStore(original, allow=1)
    decide(e, req, "admin")
    assert e.env.executor.count == 0
    e.env.recorder.ledger.store = DroppingStore(original)
    e.post(f"/recoveries/{rec['recovery_id']}/resume", "operator")
    assert e.env.executor.count == 0
    e.env.recorder.ledger.store = original
    assert e.env.executor.count == 0


def test_a_failing_vsl_gate_blocks_execution(api):
    e = api()
    _, rec, req = pending(e)
    e.env.approvals.approve(req, TOKENS["admin"], "ok")

    async def broken(_c):
        raise RuntimeError("monitor crashed")
    policy = build_policy(RECOVERY_ACTIONS)
    policy = replace(policy, pre_node=replace(policy.pre_node, monitor=broken))
    governed = GovernedActionService(e.env.recorder, RecoveryRunner(e.env.executor, e.env.approvals, e.env.recorder), policy=policy)
    e.services.recoveries.workflow = RecoveryWorkflow(e.env.approvals, governed, e.env.recorder)
    out = e.post(f"/recoveries/{rec['recovery_id']}/resume", "operator").json()
    assert out["status"] == "governance_error" and e.env.executor.count == 0
    assert e.env.store.get(req).status == "approved"                         # unspent


# ------------------------------------------------------------------------------------------ failure honesty
@pytest.mark.parametrize("fault,status,verified", [("fail_with", "recovery_uncertain", "unknown"),
                                                   ("skip_effect", "recovery_verification_failed", "not_verified")])
def test_failed_or_uncertain_recoveries_are_reported_honestly_and_not_retried(api, fault, status, verified):
    e = api()
    iid, rec, req = pending(e)
    setattr(e.env.executor, fault, RuntimeError("simulated") if fault == "fail_with" else True)
    out = decide(e, req, "admin").json()["recovery"]
    assert out["status"] == status and out["verified_outcome"] == verified
    assert "executed" not in [ev["outcome"] for ev in out["governance_events"]]
    for _ in range(2):
        e.post(f"/recoveries/{rec['recovery_id']}/resume", "operator")
        assert decide(e, req, "admin").status_code == 409
    assert e.env.executor.count == 1                                          # never automatically retried
    assert e.get(f"/incidents/{iid}/status", "reader").json()["workflow_status"] in ("recovery_uncertain", "recovery_failed")
    assert "simulated" not in str(out)


# ------------------------------------------------------------------------------------------ tickets & investigation are not authority
def test_a_ticket_or_its_closure_never_authorises_or_proves_recovery(api):
    e = api(github=FakeGitHub())
    iid, rec, req = pending(e)
    assert e.post(f"/incidents/{iid}/ticket", "operator").status_code == 201
    e.github.close_issue(1)
    e.post(f"/incidents/{iid}/ticket/sync", "operator")
    assert e.get(f"/incidents/{iid}/ticket", "reader").json()["status"] == "closed"
    assert e.get(f"/approvals/{req}", "reader").json()["status"] == "pending" and e.env.executor.count == 0
    assert e.get(f"/incidents/{iid}/status", "reader").json()["workflow_status"] == "awaiting_approval"


def test_a_successful_investigation_alone_never_authorises_recovery(api):
    e = api()
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    assert e.get(f"/incidents/{iid}/recoveries", "reader").json() == [] and e.env.executor.count == 0


# ------------------------------------------------------------------------------------------ restart
def test_a_pending_approval_stays_pending_across_a_restart(api):
    first = api(name="r")
    iid, rec, req = pending(first)
    first.close()
    second = api(name="r", sim=first.env.sim, scenario=None)                 # a new process over the same files
    got = second.get(f"/recoveries/{rec['recovery_id']}", "reader").json()
    assert got["status"] == "awaiting_approval" and got["approval"]["status"] == "pending"
    assert second.env.executor.count == 0 and second.get(f"/approvals/{req}", "reader").json()["status"] == "pending"
    assert decide(second, req, "admin").json()["recovery"]["status"] == "executed" and second.env.executor.count == 1


def test_an_interrupted_resume_is_reported_uncertain_never_success_and_never_retried(api):
    e = api()
    iid, rec, req = pending(e)
    e.env.approvals.approve(req, TOKENS["admin"], "ok")
    assert e.store.claim_recovery_resume(rec["recovery_id"])                 # a worker took the claim ...
    sqlite_update(e.env.db_path, "UPDATE approval_requests SET status='consumed' WHERE request_id=?", (req,))  # ... spent the approval, then died
    got = e.get(f"/recoveries/{rec['recovery_id']}", "reader").json()
    assert got["status"] == "recovery_uncertain" and got["verified_outcome"] == "unknown" and got["phase"] == "recovery_uncertain"
    assert e.post(f"/recoveries/{rec['recovery_id']}/resume", "operator").json()["status"] == "recovery_uncertain"
    assert e.env.executor.count == 0


def test_a_stale_claim_without_a_spent_approval_is_released(api):
    e = api()
    _, rec, req = pending(e)
    assert e.store.claim_recovery_resume(rec["recovery_id"])
    got = e.get(f"/recoveries/{rec['recovery_id']}", "reader").json()
    assert got["status"] == "awaiting_approval" and e.env.executor.count == 0


# ------------------------------------------------------------------------------------------ production posture
def _services(tmp_path, **env):
    from app.services.container import build_services
    return build_services({"DATABASE_URL": f"sqlite:///{(tmp_path / 'o.sqlite3').as_posix()}", **env})


def test_default_configuration_denies_everyone(tmp_path):
    s = _services(tmp_path)
    assert s.authenticator.authenticate(API_TOKENS["admin"]) is None and s.recoveries is None


def test_a_broken_credential_configuration_denies_everyone(tmp_path):
    s = _services(tmp_path, APPROVAL_AUTH_MODE="dev_static", APPROVAL_DEV_CREDENTIALS="not json")
    assert s.auth_status == "misconfigured" and s.authenticator.authenticate(API_TOKENS["admin"]) is None


def test_production_never_yields_a_working_development_authenticator(tmp_path):
    from app.recovery.auth import token_digest
    creds = json.dumps([{"subject": "x", "token_sha256": token_digest(API_TOKENS["admin"]), "roles": ["recovery_admin"]}])
    s = _services(tmp_path, APP_ENV="production", APPROVAL_AUTH_MODE="dev_static", APPROVAL_DEV_CREDENTIALS=creds)
    assert s.auth_status == "misconfigured" and s.authenticator.authenticate(API_TOKENS["admin"]) is None


def test_dev_static_works_outside_production_only(tmp_path):
    from app.recovery.auth import token_digest
    creds = json.dumps([{"subject": "x.user", "token_sha256": token_digest(API_TOKENS["admin"]), "roles": ["recovery_admin"]}])
    s = _services(tmp_path, APP_ENV="development", APPROVAL_AUTH_MODE="dev_static", APPROVAL_DEV_CREDENTIALS=creds)
    assert s.authenticator.authenticate(API_TOKENS["admin"]).subject == "x.user" and s.auth_status == "dev_static"


# ------------------------------------------------------------------------------------------ static: no alternate path
def test_api_and_service_code_cannot_reach_a_protected_executor_directly():
    """The HTTP layer and services go only through RecoveryWorkflow/ApprovalService; they never import the executor or runner."""
    banned = {"RecoveryExecutor", "RecoveryRunner", "SimulationService", "GovernedActionService", "ReEnablementService"}
    paths = list((ROOT / "app" / "api" / "v1").rglob("*.py")) + [ROOT / "app" / "services" / n for n in ("recoveries.py", "tickets.py", "investigations.py")]
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not ({a.name for a in node.names} & banned), (path.name, node.module)
                assert not (node.module or "").startswith(("app.recovery.executor", "app.recovery.runner")), path.name
