"""Phase 11: the Investigate button on the Ops Dashboard, driven with Streamlit AppTest against the REAL API.

The investigation itself runs the real service path (read-only MCP diagnostics against the in-memory simulator and the
deterministic offline planner). Live OpenAI is NOT exercised here, and neither is a browser."""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from dashboard import ops_data as d
from dashboard import runtime
from dashboard.http_core import Problem, Result
from dashboard.ops_client import OpsClient
from tests.api_support import API_TOKENS, api  # noqa: F401
from tests.test_phase10_dashboard_app import go, text_of

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "ops_app.py")


@pytest.fixture
def ui(api, monkeypatch):
    monkeypatch.delenv("DASHBOARD_INVESTIGATE_TOKEN", raising=False)

    def make(role="viewer", **kwargs):
        e = api(**kwargs)
        runtime.set_override("ops", OpsClient("http://testserver/api/v1", API_TOKENS[role], http=e.client))
        return e, AppTest.from_file(APP, default_timeout=30)

    yield make
    runtime.set_override("ops", None)


def open_workflow_incident(at):
    go(at, "Incidents")
    return at


def submit(at, scope, token):
    if token is not None:
        at.text_input(key=f"inv_token_{scope}").set_value(token)
    at.button(key=f"inv_go_{scope}").click().run()
    assert not at.exception, at.exception


def investigations_of(e, iid):
    return e.get(f"/incidents/{iid}/investigations", "reader").json()


def test_button_starts_a_real_investigation_and_shows_the_backend_result(ui):
    e, at = ui()
    iid = e.new_incident()["incident_id"]
    open_workflow_incident(at)
    assert "Investigate" in text_of(at) and "Mode: offline" in text_of(at)
    submit(at, f"wf:{iid}", API_TOKENS["investigator"])
    rows = investigations_of(e, iid)
    assert len(rows) == 1 and rows[0]["status"] == "completed" and rows[0]["requested_by"] == "investigator.user"
    t = text_of(at)
    assert rows[0]["investigation_id"] in t or "Investigation completed" in t
    assert "Observed findings" in t and "Evidence" in t
    assert "NOT confirmed" in t or "Root cause undetermined" in t
    assert API_TOKENS["investigator"] not in t  # the typed token is never rendered
    assert e.env.executor.count == 0 and e.get(f"/incidents/{iid}/recoveries", "reader").json() == []  # nothing was recovered


def test_viewer_token_is_refused_by_the_server_and_nothing_is_started(ui):
    e, at = ui()
    iid = e.new_incident()["incident_id"]
    open_workflow_incident(at)
    submit(at, f"wf:{iid}", API_TOKENS["viewer"])
    t = text_of(at)
    assert "may not start investigations" in t and "Investigation completed" not in t
    assert API_TOKENS["viewer"] not in t
    assert investigations_of(e, iid) == []


def test_missing_token_starts_nothing_and_says_so(ui):
    e, at = ui()
    iid = e.new_incident()["incident_id"]
    open_workflow_incident(at)
    submit(at, f"wf:{iid}", "")
    assert "required" in text_of(at) and investigations_of(e, iid) == []


def test_wrong_token_is_reported_as_rejected(ui):
    e, at = ui()
    iid = e.new_incident()["incident_id"]
    open_workflow_incident(at)
    submit(at, f"wf:{iid}", "x" * 40)
    assert "missing or rejected" in text_of(at) and investigations_of(e, iid) == []


def test_configured_token_removes_the_token_field(ui, monkeypatch):
    e, at = ui()
    iid = e.new_incident()["incident_id"]
    monkeypatch.setenv("DASHBOARD_INVESTIGATE_TOKEN", API_TOKENS["investigator"])
    open_workflow_incident(at)
    assert not [w for w in at.text_input if w.key == f"inv_token_wf:{iid}"]
    submit(at, f"wf:{iid}", None)
    assert [r["status"] for r in investigations_of(e, iid)] == ["completed"]
    assert API_TOKENS["investigator"] not in text_of(at)


def test_existing_active_investigation_is_shown_not_duplicated(ui):
    e, at = ui()
    iid = e.new_incident()["incident_id"]
    pending = e.store.create_investigation(incident_id=iid, mode="offline", requested_by="someone")
    open_workflow_incident(at)
    submit(at, f"wf:{iid}", API_TOKENS["investigator"])
    t = text_of(at)
    assert "already pending or running" in t and "Investigation is pending" in t
    assert pending["investigation_id"] and len(investigations_of(e, iid)) == 1
    assert "Investigation completed" not in t  # success is never shown before the backend says completed


def test_simulator_incident_is_imported_then_investigated(ui):
    e, at = ui()
    sim_id = e.get("/simulator/incidents", "reader").json()["items"][0]["incident_id"]
    go(at, "Incidents")
    at.radio(key="inc_source").set_value("SQL Server simulator").run()
    submit(at, f"sim:{sim_id}", API_TOKENS["investigator"])
    wf = e.get(f"/simulator/incidents/{sim_id}", "reader").json()
    assert wf["imported"] and wf["workflow_incident_id"]
    rows = investigations_of(e, wf["workflow_incident_id"])
    assert len(rows) == 1 and rows[0]["status"] in ("completed", "failed")
    assert rows[0]["status"] in text_of(at)  # the page shows what the backend recorded, whichever it was
    assert e.env.sim.repo.list_incidents(1000)  # the simulator itself was only read
    assert e.env.executor.count == 0


def test_demo_incidents_have_no_investigate_button_and_say_why(ui):
    e, at = ui()
    go(at, "Incidents")
    at.radio(key="inc_source").set_value("Synthetic demo (100)").run()
    t = text_of(at)
    assert "cannot be investigated" in t
    assert not [b for b in at.button if (b.key or "").startswith("inv_go_")]


def test_investigations_page_offers_the_button_for_the_chosen_incident(ui):
    e, at = ui()
    iid = e.new_incident()["incident_id"]
    go(at, "Investigations")
    submit(at, f"wf:{iid}", API_TOKENS["investigator"])
    assert [r["status"] for r in investigations_of(e, iid)] == ["completed"]
    assert "Observed findings" in text_of(at)


# ----------------------------------------------------------------------------- pure view-model behaviour
def prob(kind, code=None, status=None, details=None):
    return Result(problem=Problem(kind, "m", status=status, code=code, details=details or {}))


def test_start_outcome_never_claims_findings_and_handles_failures():
    tn, text, iid = d.investigate_start_outcome(Result(data={"investigation_id": "inv_1", "status": "pending"}))
    assert tn == "info" and iid == "inv_1" and "not finished" in text
    assert d.investigate_start_outcome(Result(data={}))[0] == "error"
    tn, _, iid = d.investigate_start_outcome(prob("client_error", "investigation_in_progress", 409, {"investigation_id": "inv_9"}))
    assert tn == "warning" and iid == "inv_9"
    for kind in ("timeout", "unreachable", "server_error"):
        tn, text, iid = d.investigate_start_outcome(prob(kind))
        assert tn == "error" and "UNKNOWN" in text and iid is None
    assert "needs investigation:run" in d.investigate_start_outcome(prob("forbidden", status=403))[1]
    assert "unavailable" in d.investigate_start_outcome(prob("unavailable", "live_mode_unavailable", 503))[1]


@pytest.mark.parametrize("status,report_status,tone", [("pending", None, "info"), ("running", None, "info"),
                                                       ("completed", "issue_identified", "success"),
                                                       ("completed", "no_issue_detected", "success"),
                                                       ("completed", "inconclusive", "warning"),
                                                       ("failed", None, "error"), ("weird", None, "warning")])
def test_progress_reflects_only_the_backend_status(status, report_status, tone):
    assert d.investigation_progress({"status": status, "report_status": report_status, "error": {"kind": "model_error"}})[0] == tone


def test_client_posts_use_the_callers_own_token_and_only_allowlisted_paths():
    import httpx

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        return httpx.Response(202, json={"investigation_id": "inv_1", "status": "pending"})

    c = OpsClient("http://t/api/v1", "viewer-token-0123456789abcdef", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.start_investigation("INC-1", "operator-token-0123456789abc").ok
    assert seen == [("POST", "/api/v1/incidents/INC-1/investigations", "Bearer operator-token-0123456789abc")]
    assert "operator-token" not in repr(c)
