"""Phase 12: Incident Explorer with ~100 simulator incidents, explicit import, investigation, recovery proposal, Human Review
and the VerbaLedger counts, driven through the Ops Dashboard (AppTest) against the REAL API and persisted stores.

The simulator is the in-memory repository (the same code path the API uses with SQL Server); no browser or SQL Server is
involved, and investigations use the deterministic offline mode (no OpenAI call)."""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.simulation.demo_incidents import seed_demo_incidents
from dashboard import runtime
from dashboard.ops_client import OpsClient
from tests.api_support import API_TOKENS, api  # noqa: F401
from tests.test_phase10_dashboard_app import go, text_of

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "ops_app.py")
OP = API_TOKENS["operator"]


@pytest.fixture
def ui(api, monkeypatch):
    monkeypatch.delenv("DASHBOARD_INVESTIGATE_TOKEN", raising=False)

    def make(role="viewer", **kwargs):
        e = api(**kwargs)
        runtime.set_override("ops", OpsClient("http://testserver/api/v1", API_TOKENS[role], http=e.client))
        return e, AppTest.from_file(APP, default_timeout=30)

    yield make
    runtime.set_override("ops", None)


def metrics(at):
    return {m.label: m.value for m in at.metric}


def open_simulator(at):
    go(at, "Incidents")
    at.radio(key="inc_source").set_value("SQL Server simulator").run()
    assert not at.exception, at.exception


def test_explorer_shows_all_simulator_incidents_with_totals_pages_and_filters(ui):
    e, at = ui(scenario=None)
    base = e.get("/simulator/incidents", "reader").json()["total"]
    seed_demo_incidents(e.env.sim.repo, e.env.sim.logs)
    open_simulator(at)
    m = metrics(at)
    assert m["Simulator incidents (all)"] == str(base + 98) and m["Imported into workflow"] == "0" and m["Not imported"] == str(base + 98)
    assert f"{base + 98} simulator incidents match" in text_of(at)
    first_page = {r for r in at.dataframe[0].value["incident_id"]}
    assert len(first_page) == 25
    at.number_input(key="sim_page").set_value(2).run()
    second_page = {r for r in at.dataframe[0].value["incident_id"]}
    assert len(second_page) == 25 and not (first_page & second_page)
    at.number_input(key="sim_page").set_value(1).run()
    at.selectbox(key="sim_sev").set_value("critical").run()
    sev = set(at.dataframe[0].value["severity"])
    assert sev == {"critical"}
    at.selectbox(key="sim_sev").set_value("any").run()
    at.text_input(key="sim_q").set_value("source_timeout").run()
    assert set(at.dataframe[0].value["category"]) == {"source_timeout"}
    assert "synthetic" in text_of(at).lower() or "simulator data" in text_of(at)


def test_explicit_import_then_refresh_shows_workflow_incident_and_is_idempotent(ui, monkeypatch):
    e, at = ui(scenario=None)
    seed_demo_incidents(e.env.sim.repo, e.env.sim.logs)
    monkeypatch.setenv("DASHBOARD_INVESTIGATE_TOKEN", OP)
    workflow_before = e.get("/incidents", "reader").json()["total"]
    open_simulator(at)
    at.text_input(key="sim_q").set_value("source_timeout").run()
    sim_id = at.selectbox(key="sim_pick").value
    assert "Import into the workflow store" in text_of(at)
    at.button(key=f"imp_go_{sim_id}").click().run()
    assert not at.exception, at.exception
    t = text_of(at)
    assert "Workflow incident" in t and "was created" in t or "Imported as workflow incident" in t
    assert e.get("/incidents", "reader").json()["total"] == workflow_before + 1
    assert metrics(at)["Imported into workflow"] == "1"
    # a second attempt cannot duplicate (the panel now only reports the existing import)
    assert "was created" in text_of(at) and not [b for b in at.button if (b.key or "") == f"imp_go_{sim_id}"]
    at.selectbox(key="sim_imp").set_value("imported").run()  # a later visit (notice for another record) says it is already imported
    at.session_state["import_notice"] = ("other", "info", "x")
    at.run()
    assert "Imported as workflow incident" in text_of(at)
    assert e.post(f"/simulator/incidents/{sim_id}/import", "operator").json()["created"] is False
    assert e.get("/incidents", "reader").json()["total"] == workflow_before + 1
    # the workflow dashboard lists it without restarting anything
    go(at, "Incidents")
    at.radio(key="inc_source").set_value("Workflow incidents").run()
    assert any(sim_id in str(r) or "source_timeout" in str(r).lower() or "timeout" in str(r).lower() for r in at.dataframe[0].value["title"])


def test_import_with_a_viewer_token_is_refused_and_creates_nothing(ui):
    e, at = ui(scenario=None)
    seed_demo_incidents(e.env.sim.repo, e.env.sim.logs)
    n = e.get("/incidents", "reader").json()["total"]
    open_simulator(at)
    sim_id = at.selectbox(key="sim_pick").value
    at.text_input(key=f"imp_token_{sim_id}").set_value(API_TOKENS["viewer"])
    at.button(key=f"imp_go_{sim_id}").click().run()
    assert "may not import incidents" in text_of(at) and API_TOKENS["viewer"] not in text_of(at)
    assert e.get("/incidents", "reader").json()["total"] == n


def test_seeded_incident_flow_investigates_from_its_own_run_log_and_proposes_no_recovery(ui, monkeypatch):
    e, at = ui(scenario=None)
    seed_demo_incidents(e.env.sim.repo, e.env.sim.logs)
    monkeypatch.setenv("DASHBOARD_INVESTIGATE_TOKEN", OP)
    open_simulator(at)
    at.selectbox(key="sim_sev").set_value("high").run()
    at.text_input(key="sim_q").set_value("source_timeout").run()
    sim_id = at.selectbox(key="sim_pick").value
    at.button(key=f"inv_go_sim:{sim_id}").click().run()
    assert not at.exception, at.exception
    t = text_of(at)
    assert "Investigation completed" in t and "RUN-DEMO-" in t and "feed_connection_timeout" in t  # evidence from its own log
    assert "Observed findings" in t and "NOT confirmed" in t
    iid = at.session_state["investigations"][f"sim:{sim_id}"]
    wid = e.get(f"/investigations/{iid}", "reader").json()["incident_id"]
    at.button(key=f"rec_go_{iid}").click().run()
    assert "No recovery was proposed" in text_of(at)
    assert e.get(f"/incidents/{wid}/recoveries", "reader").json() == [] and e.env.executor.count == 0
    assert e.get("/reviews", "reader").json()["total"] == 0  # nothing invented for Human Review


def test_real_scenario_flow_reaches_human_review_and_the_ledger_counts_match(ui, monkeypatch):
    """Active duplicate-records scenario -> incident -> UI investigation -> UI recovery proposal -> Human Review -> approval."""
    e, at = ui()  # default fixture has the duplicate-records scenario active (a real injected condition)
    iid = e.new_incident()["incident_id"]
    monkeypatch.setenv("DASHBOARD_INVESTIGATE_TOKEN", OP)
    go(at, "Incidents")
    at.button(key=f"inv_go_wf:{iid}").click().run()
    assert "Investigation completed" in text_of(at)
    inv = at.session_state["investigations"][f"wf:{iid}"]
    at.button(key=f"rec_go_{inv}").click().run()
    assert "Proposal created" in text_of(at) and "Nothing has been executed" in text_of(at)
    assert e.env.executor.count == 0
    queue = e.get("/reviews", "reader").json()
    assert queue["total"] == 1 and queue["items"][0]["incident_id"] == iid and queue["items"][0]["awaiting_decision"] is True
    go(at, "Human Review")
    t = text_of(at)
    assert iid in t and "Confirmed evidence" in t and "Human decision" in t
    detail = e.get(f"/reviews/{queue['items'][0]['review_id']}", "reader").json()
    assert detail["confirmed_evidence"]["findings"], "the cited diagnostic results must be shown to the reviewer"
    assert {f["evidence_id"] for f in detail["confirmed_evidence"]["findings"]} <= set(detail["confirmed_evidence"]["evidence_refs"])
    assert all(f["summary"] and f["tool"] for f in detail["confirmed_evidence"]["findings"])
    assert detail["agent_hypothesis"]["confidence"] in ("low", "medium", "high")
    assert "requested_by" in detail and detail["created_at"]
    # only an authorised approver may decide; an operator token cannot
    req = queue["items"][0]["request_id"]
    assert e.post(f"/approvals/{req}/decision", "operator", json={"decision": "approve", "reason": "x"}).status_code == 403
    assert e.post(f"/approvals/{req}/decision", "viewer", json={"decision": "approve", "reason": "x"}).status_code == 403
    before = e.get("/ledger/summary", "viewer").json()
    assert e.post(f"/approvals/{req}/decision", "admin", json={"decision": "approve", "reason": "checked"}).status_code == 200
    again = e.post(f"/approvals/{req}/decision", "admin", json={"decision": "reject", "reason": "changed my mind"})
    assert again.status_code in (404, 409)  # a decided request cannot be silently overwritten
    summary = e.get("/ledger/summary", "viewer").json()
    events = e.get("/ledger/events?limit=200", "viewer").json()
    assert summary["total_records"] == events["total"] == len(events["items"]) > before["total_records"]
    assert sum(summary["by_entry_type"].values()) == summary["total_records"]
    per_incident = e.get(f"/ledger/events?incident_id={iid}&limit=200", "viewer").json()
    assert per_incident["total"] == summary["by_incident"].get(iid, 0) and per_incident["total"] >= 1
    assert summary["recovery_related"] == sum(n for o, n in summary["by_outcome"].items() if o.startswith("recovery_"))
    assert summary["recovery_related"] >= 1 and summary["decisions"]["human_review"] >= 1
    assert summary["latest_event"]["sequence"] == max(i["sequence"] for i in events["items"])
    assert e.get("/ledger/integrity", "viewer").json()["status"] == "verified"


def test_returning_visit_shows_the_latest_investigation_and_offers_the_recovery_proposal(ui, monkeypatch):
    e, at = ui()
    iid = e.new_incident()["incident_id"]
    done = e.investigate(iid)  # recorded earlier, outside this browser session
    monkeypatch.setenv("DASHBOARD_INVESTIGATE_TOKEN", OP)
    go(at, "Incidents")
    t = text_of(at)
    assert "most recent investigation recorded for this incident" in t and "Investigation completed" in t
    at.button(key=f"rec_go_{done['investigation_id']}").click().run()
    assert "Proposal created" in text_of(at) and e.get("/reviews", "reader").json()["total"] == 1
    assert e.env.executor.count == 0


def test_ui_decision_outcome_stays_visible_after_the_item_leaves_the_pending_list(ui):
    e, at = ui()
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    assert e.request_recovery(iid).status_code == 202
    go(at, "Human Review")
    at.text_input(key="decision_reason").set_value("Evidence reviewed")
    at.text_input(key="decision_token").set_value(API_TOKENS["admin"])
    at.checkbox(key="decision_confirm").check()
    at.button(key="decision_submit").click().run()
    assert not at.exception, at.exception
    t = text_of(at)
    assert "Decision recorded" in t and "Nothing is awaiting a human decision" in t and API_TOKENS["admin"] not in t
    assert e.get("/reviews?state=decided", "reader").json()["total"] == 1
