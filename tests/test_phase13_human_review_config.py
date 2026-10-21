"""Phase 13: the Human Review 503 ("recovery_unavailable") - root cause, honest reporting, and the supported working setup.

Root cause (reproduced here with the real service container): recovery is built only if ``assert_recovery_target`` accepts
the simulator database, and that guard (a deliberate Phase 7 safety rule, unchanged) refuses ``dataops_sim`` - only a
database whose name ends in ``_test`` is a permitted recovery target. With ``DB_NAME=dataops_sim`` the container therefore
leaves ``services.recoveries`` unset and every review endpoint answers 503.

No SQL Server is used: the engine is replaced by a stand-in carrying only the database NAME, which is all the guard reads."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import app.db.connection as connection
from app.config import Settings
from app.services.container import build_services, recovery_unavailable_info
from dashboard import ops_data as d, runtime
from dashboard.http_core import Problem
from dashboard.ops_client import OpsClient
from scripts import dev_stack as ds
from tests.api_support import API_TOKENS, api  # noqa: F401
from tests.test_phase10_dashboard_app import go, text_of

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "ops_app.py")


class _NamedEngine:
    def __init__(self, name: str) -> None:
        from sqlalchemy.engine import make_url
        self.url = make_url(f"mssql+pyodbc://user@host/{name}")


def _container(tmp_path, monkeypatch, db_name):
    monkeypatch.setattr(connection, "make_engine", lambda settings: _NamedEngine(settings.db_name))
    env = {"APPROVAL_AUTH_MODE": "dev_static", "APPROVAL_SIGNING_KEY": "s" * 40,
           "APPROVAL_DEV_CREDENTIALS": json.dumps({"k" * 32: {"subject": "the.approver", "role": "recovery_admin"}}),
           "APPROVAL_STORE_PATH": str(tmp_path / "approvals.db"), "GOVERNANCE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
           "DATABASE_URL": f"sqlite:///{tmp_path}/ops.db", "SIM_LOG_DIR": str(tmp_path / "logs")}
    settings = Settings(_env_file=None, db_server="localhost\\SQLEXPRESS03", db_name=db_name, db_trust_server_certificate=True)
    return build_services(env, settings)


def test_root_cause_dataops_sim_disables_recovery_but_a_test_database_enables_it(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    bad = _container(tmp_path / "a", monkeypatch, "dataops_sim")
    assert bad.recoveries is None and bad.recovery_unavailable == "recovery_target_unavailable:UnsafeRecoveryTarget"
    assert bad.approvals is not None and bad.recovery_database == "dataops_sim"  # approvals are configured; only the recovery target is refused
    (tmp_path / "b").mkdir()
    good = _container(tmp_path / "b", monkeypatch, "dataops_demo_test")
    assert good.recoveries is not None and good.recovery_unavailable is None


def test_the_safety_guard_still_refuses_every_non_test_name(tmp_path, monkeypatch):
    for i, name in enumerate(("dataops_sim", "dataops_prod", "dataops", "dataops_test_sim")):
        (tmp_path / str(i)).mkdir()
        assert _container(tmp_path / str(i), monkeypatch, name).recoveries is None, name


def test_the_explanation_names_the_cause_and_the_fix_and_carries_no_secret():
    class S:  # the attributes recovery_unavailable_info reads
        recovery_unavailable, recovery_database = "recovery_target_unavailable:UnsafeRecoveryTarget", "dataops_sim"
    message, details = recovery_unavailable_info(S)
    assert "_test" in message and "dataops_sim" in message and details["required_database_suffix"] == "_test"
    assert "dataops_demo_test" in details["fix"] and set(details) == {"reason", "database", "required_database_suffix", "fix"}
    S.recovery_unavailable = "approvals_not_configured:ApprovalConfigError"
    assert "APPROVAL_SIGNING_KEY" in recovery_unavailable_info(S)[0]
    S.recovery_unavailable = "recovery_target_unavailable:DatabaseUnavailable"
    assert "DB_SERVER" in recovery_unavailable_info(S)[0] and "required_database_suffix" not in recovery_unavailable_info(S)[1]
    S.recovery_unavailable = "not_configured"
    assert "not available" in recovery_unavailable_info(S)[0]


def _guarded(api):
    e = api(recovery=False)
    e.services.recovery_unavailable = "recovery_target_unavailable:UnsafeRecoveryTarget"
    e.services.recovery_database = "dataops_sim"
    return e


def test_review_endpoints_answer_503_with_the_real_reason_and_nothing_executes(api):
    e = _guarded(api)
    iid = e.new_incident()["incident_id"]
    for path in ("/reviews", "/reviews/summary"):
        r = e.get(path, "viewer")
        body = r.json()["error"]
        assert r.status_code == 503 and body["code"] == "recovery_unavailable"
        assert "'dataops_sim'" in body["message"] and "_test" in body["message"]
        assert body["details"]["reason"].endswith("UnsafeRecoveryTarget") and body["details"]["database"] == "dataops_sim"
    e.investigate(iid)
    rec = e.request_recovery(iid)
    assert rec.status_code == 503 and "_test" in rec.json()["error"]["message"] and e.env.executor.count == 0
    summary = e.get("/dashboard/summary", "viewer").json()
    assert summary["recovery_available"] is False and summary["human_review"] == {"pending": None, "available": False}
    assert summary["recovery_unavailable"]["database"] == "dataops_sim"


def test_unauthenticated_and_viewer_checks_are_unchanged_when_recovery_is_off(api):
    e = _guarded(api)
    assert e.get("/reviews").status_code == 401  # authentication is still enforced first


@pytest.fixture
def ui(api):
    def make(role="viewer", guarded=False, **kwargs):
        e = _guarded(api) if guarded else api(**kwargs)
        runtime.set_override("ops", OpsClient("http://testserver/api/v1", API_TOKENS[role], http=e.client))
        return e, AppTest.from_file(APP, default_timeout=30)

    yield make
    runtime.set_override("ops", None)


def test_human_review_page_is_honest_when_recovery_is_switched_off(ui):
    e, at = ui(guarded=True)
    go(at, "Human Review")
    t = text_of(at)
    assert "Human Review is switched off in this configuration" in t and "'dataops_sim'" in t and "UnsafeRecoveryTarget" in t
    assert "dataops_demo_test" in "\n".join(c.value for c in at.code) and "scripts.db_seed_demo_incidents" in "\n".join(c.value for c in at.code)
    assert not [m for m in at.metric if m.label.startswith("Pending")], "no zero counts may be invented"
    assert not at.dataframe and "Nothing is awaiting a human decision" not in t and "Decision recorded" not in t
    assert not at.error, "the capability is off - that is a warning with guidance, not a working page and not a raw error"


def test_guidance_is_only_for_the_recovery_unavailable_code():
    assert d.recovery_guidance(None) is None
    assert d.recovery_guidance(Problem(kind="unavailable", message="x", status=503, code="not_ready")) is None
    g = d.recovery_guidance(Problem(kind="unavailable", message="m", status=503, code="recovery_unavailable",
                                    details={"reason": "approvals_not_configured:X", "fix": "do y"}))
    assert g["steps"] == [] and g["fix"] == "do y"


def test_working_configuration_shows_a_genuine_pending_request_with_evidence(ui, monkeypatch):
    e, at = ui()
    monkeypatch.setenv("DASHBOARD_INVESTIGATE_TOKEN", API_TOKENS["operator"])
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    assert e.request_recovery(iid).status_code == 202
    assert e.env.executor.count == 0, "requesting must not execute"
    go(at, "Human Review")
    m = {x.label: x.value for x in at.metric}
    assert m["Pending human review"] == "1" and not at.error
    t = text_of(at)
    assert iid in t and "Confirmed evidence" in t and "remove_injected_condition" in t
    item = e.get("/reviews", "viewer").json()["items"][0]
    assert item["review_state"] == "pending" and item["awaiting_decision"] is True and item["action_type"] and item["risk"]
    detail = e.get(f"/reviews/{item['review_id']}", "viewer").json()
    assert detail["confirmed_evidence"]["findings"] and detail["recommendation"]["action_type"] == "remove_injected_condition"


def _pending(e):
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    assert e.request_recovery(iid).status_code == 202
    return iid, e.get("/reviews", "viewer").json()["items"][0]["request_id"]


def test_rejection_is_persisted_visible_after_refresh_recorded_in_the_ledger_and_executes_nothing(ui):
    e, at = ui()
    iid, req = _pending(e)
    before = e.get("/ledger/summary", "viewer").json()["total_records"]
    for role in ("viewer", "reader", "investigator", "operator"):  # backend refuses every non-approver, whatever the page does
        assert e.post(f"/approvals/{req}/decision", role, json={"decision": "reject", "reason": "no"}).status_code == 403, role
    assert e.get("/reviews/summary", "viewer").json()["pending"] == 1, "refused attempts decide nothing"
    assert e.post(f"/approvals/{req}/decision", "admin", json={"decision": "reject", "reason": "not safe to run"}).status_code == 200
    assert e.env.executor.count == 0
    # persisted: visible to a completely fresh request and a fresh page load, in the decided list and not the pending one
    assert e.get("/reviews?state=pending", "viewer").json()["total"] == 0
    decided = e.get("/reviews?state=decided", "viewer").json()["items"]
    assert len(decided) == 1 and decided[0]["review_state"] == "rejected" and decided[0]["decided_by"]
    go(at, "Human Review")
    at.radio(key="rev_state").set_value("decided").run()
    assert iid in text_of(at) and not at.exception
    # governance events recorded (real ledger records, none inserted by the test)
    summary = e.get("/ledger/summary", "viewer").json()
    assert summary["total_records"] > before and summary["by_incident"].get(iid, 0) >= 1
    assert e.get("/ledger/integrity", "viewer").json()["status"] == "verified"
    again = e.post(f"/approvals/{req}/decision", "admin", json={"decision": "approve", "reason": "flip"})
    assert again.status_code in (404, 409) and e.env.executor.count == 0, "a decision cannot be silently overwritten"


def test_approval_runs_recovery_only_through_the_governed_workflow_and_is_audited(ui):
    e, at = ui()
    iid, req = _pending(e)
    assert e.post(f"/approvals/{req}/decision", "operator", json={"decision": "approve", "reason": "x"}).status_code == 403
    assert e.env.executor.count == 0
    assert e.post(f"/approvals/{req}/decision", "admin", json={"decision": "approve", "reason": "evidence checked"}).status_code == 200
    assert e.env.executor.count == 1, "exactly one governed execution after the human approval"
    rec = e.get(f"/incidents/{iid}/recoveries", "viewer").json()[0]
    assert rec["status"] == "executed"
    events = e.get(f"/ledger/events?incident_id={iid}&limit=200", "viewer").json()
    outcomes = {i["outcome"] for i in events["items"]}
    assert any(o.startswith("recovery_") for o in outcomes) and e.get("/ledger/summary", "viewer").json()["recovery_related"] >= 1


def test_launcher_reports_recovery_availability_from_the_api(monkeypatch):
    auth = {"viewer": "v" * 40}

    def fake(payload, code=200):
        monkeypatch.setattr(ds, "http_status", lambda url, token=None, timeout=5.0: (code, json.dumps(payload).encode()))
        return ds.recovery_status(auth, 8000)

    assert fake({"recovery_available": True})[0] is True
    ok, note = fake({"recovery_available": False, "recovery_unavailable": {"reason": "x", "database": "dataops_sim", "required_database_suffix": "_test"}})
    assert ok is False and "dataops_sim" in note and "_test" in note and "dataops_demo_test" in note
    assert fake({"recovery_available": False, "recovery_unavailable": {"reason": "approvals_not_configured:E", "fix": "set keys"}})[1].endswith("set keys")
    assert fake({}, 500)[0] is None


def test_after_a_decision_the_other_pages_show_the_new_state_without_pressing_refresh(ui):
    """Regression: the incident list was cached for the whole session, so after approving, the Recovery page still counted the
    request as awaiting approval (or said there was none) until 'Refresh data' was pressed."""
    e, at = ui()
    iid, req = _pending(e)
    go(at, "Recovery & approvals")  # loads and caches the incident list while the request is still awaiting approval
    assert {m.label: m.value for m in at.metric}["Awaiting approval"] == "1"
    go(at, "Human Review")
    at.text_input(key="decision_reason").set_value("Evidence reviewed")
    at.text_input(key="decision_token").set_value(API_TOKENS["admin"])
    at.checkbox(key="decision_confirm").check()
    at.button(key="decision_submit").click().run()
    assert not at.exception, at.exception
    assert e.env.executor.count == 1
    go(at, "Recovery & approvals")
    assert {m.label: m.value for m in at.metric}["Awaiting approval"] == "0"
    assert "executed" in text_of(at).lower() and "verified" in text_of(at).lower()
    assert API_TOKENS["admin"] not in text_of(at)
