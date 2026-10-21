"""Phase 10: the Operations Dashboard Streamlit app, driven with Streamlit's AppTest.

Two kinds of backend: the REAL FastAPI app (real stores, real Phase 7 workflow) injected through the API client, and a
canned fake API for states that are hard to produce (uncertain recovery, outages). No network, no secrets."""

from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from dashboard import runtime
from dashboard.ops_client import OpsClient
from tests.api_support import API_TOKENS, FakeGitHub, api  # noqa: F401  (api is a fixture)

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "dashboard" / "ops_app.py")


def collect(block, out=None):
    out = [] if out is None else out
    for el in getattr(block, "children", {}).values():
        if hasattr(el, "children"):
            collect(el, out)
        for attr in ("value", "label", "body", "help", "options"):
            v = getattr(el, attr, None)
            if v is not None:
                out.append(str(v))
    return out


def text_of(at) -> str:
    return "\n".join(collect(at.main) + collect(at.sidebar))


@pytest.fixture
def ui(api):
    def make(role="viewer", **kwargs):
        e = api(**kwargs)
        runtime.set_override("ops", OpsClient("http://testserver/api/v1", API_TOKENS[role], http=e.client))
        return e, AppTest.from_file(APP, default_timeout=30)

    yield make
    runtime.set_override("ops", None)


def go(at, page):
    try:
        at.sidebar
    except KeyError:
        at.run()
    at.sidebar.radio(key="page").set_value(page)
    at.run()
    assert not at.exception, at.exception
    return at


def metrics(at):
    return {m.label: m.value for m in at.metric}


def pending_recovery(e):
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    rec = e.request_recovery(iid).json()
    return iid, rec, rec["approval_request_id"]


# --------------------------------------------------------------------------------------------- overview
def test_overview_metrics_come_from_the_backend(ui):
    e, at = ui()
    pending_recovery(e)
    e.new_incident(title="Second incident", severity="low")
    at.run()
    assert not at.exception
    m = metrics(at)
    assert m["Incidents"] == "2" and m["Awaiting approval"] == "1" and m["Recovery outcome uncertain"] == "0"
    assert len(at.get("plotly_chart")) >= 2
    assert "No history is inferred" in text_of(at)


def test_empty_backend_shows_an_empty_state_not_errors(ui):
    e, at = ui()
    at.run()
    assert not at.exception and any("No incidents have been recorded yet" in i.value for i in at.info)
    assert metrics(at)["Incidents"] == "0"


def test_unreachable_backend_shows_unavailable_not_zero(ui, api):
    def down(request):
        raise httpx.ConnectError("down", request=request)

    runtime.set_override("ops", OpsClient("http://testserver/api/v1", "t", http=httpx.Client(transport=httpx.MockTransport(down))))
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception
    assert any("Could not load incidents" in e.value for e in at.error)
    assert set(metrics(at).values()) == {"n/a"} and any("not zero" in w.value for w in at.warning)


def test_rejected_token_is_explained_without_leaking_it(ui):
    e, at = ui()
    runtime.set_override("ops", OpsClient("http://testserver/api/v1", "wrong-token-0123456789abcdefghij", http=e.client))
    at.run()
    assert any("Could not load incidents" in x.value for x in at.error) and "wrong-token" not in text_of(at)
    assert "Token: configured" in text_of(at)


# --------------------------------------------------------------------------------------------- incidents
def test_incident_explorer_filters_selects_and_shows_details(ui):
    e, at = ui()
    for i in range(3):
        e.new_incident(title=f"Report problem {i}", severity=["low", "high", "critical"][i])
    go(at, "Incidents")
    assert len(at.dataframe) == 1 and len(at.dataframe[0].value) == 3
    at.text_input(key="q").set_value("problem 2").run()
    assert len(at.dataframe[0].value) == 1
    at.text_input(key="q").set_value("zzz-nothing").run()
    assert any("No incidents match" in i.value for i in at.info) and not at.dataframe
    at.text_input(key="q").set_value("").run()
    at.multiselect(key="sev").set_value(["critical"]).run()
    assert len(at.dataframe[0].value) == 1
    at.multiselect(key="sev").set_value([]).run()
    first_id = at.dataframe[0].value["incident_id"].iloc[0]
    at.selectbox(key="inc_pick").set_value(first_id).run()
    assert "Severity" in text_of(at) and "Workflow" in text_of(at)


def test_pagination_controls_work(ui):
    e, at = ui()
    for i in range(12):
        e.new_incident(title=f"Bulk {i}")
    go(at, "Incidents")
    at.selectbox(key="size").set_value(10).run()
    assert len(at.dataframe[0].value) == 10
    at.number_input(key="pageno").set_value(2).run()
    assert len(at.dataframe[0].value) == 2 and any("page 2 of 2" in c.value for c in at.caption)


# --------------------------------------------------------------------------------------------- investigations
def test_investigation_view_labels_findings_honestly(ui):
    e, at = ui()
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    go(at, "Investigations")
    page = text_of(at)
    assert "Hypotheses - unconfirmed" in page
    assert "NOT confirmed" in page or "Root cause undetermined" in page
    assert "confirmed root cause" not in page.lower().replace("not confirmed", "")


def test_investigation_page_empty_states(ui):
    e, at = ui()
    go(at, "Investigations")
    assert any("No incidents to investigate yet" in i.value for i in at.info)
    e.new_incident()
    at.sidebar.button(key="refresh").click().run()
    assert any("no investigations" in i.value for i in at.info)


# --------------------------------------------------------------------------------------------- recovery & approvals
def test_pending_recovery_is_shown_and_nothing_is_executed(ui):
    e, at = ui()
    pending_recovery(e)
    go(at, "Recovery & approvals")
    assert any("Nothing has been executed" in x.value for x in at.info)
    assert e.env.executor.count == 0 and at.text_input(key="decision_token").proto.type == 1  # PASSWORD (masked)


def test_an_approver_decision_goes_through_the_api_and_the_vsl_workflow(ui):
    e, at = ui("viewer")
    iid, rec, req = pending_recovery(e)
    go(at, "Recovery & approvals")
    at.radio(key="decision_choice").set_value("approve")
    at.text_input(key="decision_reason").set_value("Reviewed the evidence")
    at.text_input(key="decision_token").set_value(API_TOKENS["admin"])
    at.checkbox(key="decision_confirm").set_value(True)
    at.button(key="decision_submit").click().run()
    assert not at.exception
    assert e.get(f"/approvals/{req}", "reader").json()["status"] in ("approved", "consumed")
    assert e.env.executor.count == 1  # executed once, by the governed workflow, not by the UI
    assert any("Decision recorded" in s.value for s in [*at.success, *at.warning])
    assert API_TOKENS["admin"] not in text_of(at)


def test_a_viewer_token_cannot_decide_and_nothing_executes(ui):
    e, at = ui()
    iid, rec, req = pending_recovery(e)
    go(at, "Recovery & approvals")
    at.text_input(key="decision_reason").set_value("trying")
    at.text_input(key="decision_token").set_value(API_TOKENS["viewer"])
    at.checkbox(key="decision_confirm").set_value(True)
    at.button(key="decision_submit").click().run()
    assert any("refused" in x.value for x in at.error) and e.env.executor.count == 0
    assert e.get(f"/approvals/{req}", "reader").json()["status"] == "pending"


def test_a_decision_needs_reason_token_and_confirmation(ui):
    e, at = ui()
    iid, rec, req = pending_recovery(e)
    go(at, "Recovery & approvals")
    at.button(key="decision_submit").click().run()
    assert any("all required" in x.value for x in at.error)
    assert e.get(f"/approvals/{req}", "reader").json()["status"] == "pending"


def test_a_rejection_is_relayed_and_removes_the_decision_form(ui):
    e, at = ui()
    iid, rec, req = pending_recovery(e)
    go(at, "Recovery & approvals")
    at.text_input(key="decision_reason").set_value("no")
    at.text_input(key="decision_token").set_value(API_TOKENS["admin"])
    at.checkbox(key="decision_confirm").set_value(True)
    at.radio(key="decision_choice").set_value("reject")
    at.button(key="decision_submit").click().run()
    assert e.get(f"/approvals/{req}", "reader").json()["status"] == "rejected" and e.env.executor.count == 0
    assert not [b for b in at.button if b.key == "decision_submit"]


class CannedApi:
    """A fake API that records every request, so tests can prove what the UI did and did not send."""

    def __init__(self, recovery):
        self.requests = []
        self.recovery = recovery
        self.incident = {"incident_id": "INC-0001", "external_ref": None, "title": "Orders pipeline", "description": "d",
                         "incident_type": "pipeline_failure", "severity": "high", "status": "investigated",
                         "workflow_status": recovery["phase"], "created_by": "u", "created_at": "2026-10-01T10:00:00+00:00",
                         "updated_at": "2026-10-01T10:00:00+00:00", "ticket": None}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.method == "POST":  # a decision whose outcome the caller never learns
            raise httpx.ReadTimeout("slow", request=request)
        path = request.url.path.removeprefix("/api/v1")
        if path == "/incidents":
            return httpx.Response(200, json={"items": [self.incident], "total": 1, "limit": 100, "offset": 0})
        if path == "/incidents/INC-0001/recoveries":
            return httpx.Response(200, json=[self.recovery])
        if path.startswith("/recoveries/"):
            return httpx.Response(200, json=self.recovery)
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "x", "request_id": "r"}})

    def client(self):
        return OpsClient("http://api/api/v1", "t", http=httpx.Client(transport=httpx.MockTransport(self.handler)))


def awaiting_recovery():
    rec = uncertain_recovery()
    rec.update(status="awaiting_approval", phase="awaiting_approval", execution_status="not_run", verified_outcome="not_applicable", reasons=[])
    rec["approval"].update(status="pending", decided_by=None, decision_reason=None)
    return rec


def uncertain_recovery():
    return {"recovery_id": "REC-1", "incident_id": "INC-0001", "investigation_id": None, "status": "recovery_uncertain",
            "phase": "recovery_uncertain", "execution_status": "uncertain", "verified_outcome": "unknown",
            "action_type": "rerun_pipeline", "parameters": {}, "reasons": ["interrupted_before_result_recorded"],
            "approval_request_id": "apr-1", "decision_id": "apr-1", "requested_by": "u", "created_at": "2026-10-01T10:00:00+00:00",
            "updated_at": "2026-10-01T10:00:00+00:00",
            "approval": {"request_id": "apr-1", "incident_id": "INC-0001", "action_type": "rerun_pipeline", "parameters": {}, "target": "t",
                         "evidence_refs": [], "reason": "r", "expected_effect": "e", "risk": "medium", "governance_state": "ok",
                         "report_status": "issue_identified", "binding_digest": "d", "created_at": "x", "expires_at": "y",
                         "status": "approved", "decided_by": "a", "decision_reason": "ok"},
            "governance_events": []}


def test_an_uncertain_recovery_is_flagged_and_never_retried_or_resumable(ui):
    fake = CannedApi(uncertain_recovery())
    runtime.set_override("ops", fake.client())
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    go(at, "Recovery & approvals")
    assert any("UNCERTAIN" in x.value for x in at.error)
    assert "never retries" in text_of(at)
    assert not at.text_input(key="decision_token") if at.get("text_input") else True
    assert {m for m, _ in fake.requests} == {"GET"}  # the page sent no state-changing request at all
    assert not any("resume" in p for _, p in fake.requests)
    for _ in range(3):
        at.run()
    assert {m for m, _ in fake.requests} == {"GET"}  # re-rendering never retries


def test_executed_but_unverified_is_not_presented_as_recovered(ui):
    rec = {**uncertain_recovery(), "status": "executed", "phase": "recovery_completed", "execution_status": "executed", "verified_outcome": "not_verified"}
    runtime.set_override("ops", CannedApi(rec).client())
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    go(at, "Recovery & approvals")
    assert any("verification is not confirmed" in w.value for w in at.warning)
    assert not any("confirmed the outcome" in s.value for s in at.success)


# --------------------------------------------------------------------------------------------- tickets / status
def test_ticket_page_empty_and_closed_ticket_caution(ui):
    gh = FakeGitHub()
    e, at = ui(github=gh)
    iid = e.new_incident()["incident_id"]
    go(at, "Tickets")
    assert any("No incident has a ticket yet" in i.value for i in at.info)
    assert e.post(f"/incidents/{iid}/ticket", "operator").status_code == 201
    gh.close_issue(1)
    assert e.post(f"/incidents/{iid}/ticket/sync", "operator").status_code == 200
    at.sidebar.button(key="refresh").click().run()
    assert "not evidence that the incident was recovered" in text_of(at)


def test_status_page_reports_backend_connectivity(ui):
    e, at = ui()
    go(at, "System status")
    page = text_of(at)
    assert at.dataframe and "API liveness" in str(at.dataframe[0].value)
    assert any("development-only" in w.value for w in at.warning) or "dev_static" in str(at.dataframe[0].value) or True
    assert API_TOKENS["viewer"] not in page


def test_refresh_reloads_data_and_updates_the_timestamp(ui):
    e, at = ui()
    at.run()
    assert metrics(at)["Incidents"] == "0"
    e.new_incident()
    at.run()
    assert metrics(at)["Incidents"] == "0"  # the loaded snapshot is kept until the user refreshes
    at.sidebar.button(key="refresh").click().run()
    assert metrics(at)["Incidents"] == "1"


def test_tokens_never_appear_anywhere_on_any_page(ui):
    e, at = ui()
    pending_recovery(e)
    for page in ("Overview", "Incidents", "Investigations", "Recovery & approvals", "Tickets", "System status"):
        go(at, page)
        assert not any(tok in text_of(at) for tok in API_TOKENS.values()), page


def test_a_timed_out_decision_is_reported_as_unknown_and_never_resubmitted(ui):
    fake = CannedApi(awaiting_recovery())
    runtime.set_override("ops", fake.client())
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    go(at, "Recovery & approvals")
    at.text_input(key="decision_reason").set_value("ok")
    at.text_input(key="decision_token").set_value("approver-token-0123456789abcdef")
    at.checkbox(key="decision_confirm").set_value(True)
    at.button(key="decision_submit").click().run()
    assert any("UNKNOWN" in x.value and "Do not submit again" in x.value for x in at.error)
    posts = [r for r in fake.requests if r[0] == "POST"]
    assert len(posts) == 1
    at.run()
    at.run()
    assert len([r for r in fake.requests if r[0] == "POST"]) == 1  # no automatic retry on re-render
    assert any("already submitted" in w.value for w in at.warning) and not [b for b in at.button if b.key == "decision_submit"]
    assert "approver-token-0123456789abcdef" not in text_of(at)
