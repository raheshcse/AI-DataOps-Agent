"""Phase 11: Ops Dashboard pages driven with Streamlit AppTest against the REAL API (no network, no secrets).

Browser rendering is NOT covered here: AppTest runs the script headlessly and inspects the element tree."""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from dashboard import runtime
from dashboard.ops_client import OpsClient
from tests.api_support import API_TOKENS, api  # noqa: F401
from tests.test_phase10_dashboard_app import collect, go, pending_recovery, text_of

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "ops_app.py")


@pytest.fixture
def ui(api):
    def make(role="viewer", **kwargs):
        e = api(**kwargs)
        runtime.set_override("ops", OpsClient("http://testserver/api/v1", API_TOKENS[role], http=e.client))
        return e, AppTest.from_file(APP, default_timeout=30)

    yield make
    runtime.set_override("ops", None)


def test_simulator_incident_is_findable_and_labelled_as_simulator_data(ui):
    e, at = ui()
    go(at, "Incidents")
    at.radio(key="inc_source").set_value("SQL Server simulator").run()
    assert not at.exception
    sim_id = e.get("/simulator/incidents", "reader").json()["items"][0]["incident_id"]
    assert sim_id in text_of(at) and "simulator data" in text_of(at) and "Not imported" in text_of(at)
    at.text_input(key="sim_q").set_value(sim_id).run()
    assert sim_id in text_of(at)
    assert e.env.sim.repo.list_incidents(1000)  # read-only: still there, unchanged


def test_demo_incidents_are_visibly_labelled_synthetic(ui):
    e, at = ui()
    go(at, "Incidents")
    at.radio(key="inc_source").set_value("Synthetic demo (100)").run()
    assert not at.exception
    t = text_of(at)
    assert "SYNTHETIC DEMONSTRATION DATA" in t or any("SYNTHETIC" in str(m.value) for m in at.markdown)
    assert "DEMO-" in t and any(m.label == "Demo incidents" and m.value == "100" for m in at.metric)
    assert "hypothesis, unconfirmed" in t and "Human review flag" in t


def test_human_review_empty_state_when_nothing_pending(ui):
    e, at = ui()
    e.new_incident()
    go(at, "Human Review")
    assert any("Nothing is awaiting a human decision" in x.value for x in at.info)
    assert {m.label: m.value for m in at.metric}["Pending human review"] == "0"


def test_human_review_shows_only_real_pending_items_and_keeps_statements_apart(ui):
    e, at = ui()
    iid, rec, req = pending_recovery(e)
    go(at, "Human Review")
    assert {m.label: m.value for m in at.metric}["Pending human review"] == "1"
    t = text_of(at)
    assert iid in t and "Confirmed evidence" in t and "Agent hypothesis" in t and "Recommendation" in t and "Human decision" in t
    assert "Clarification requests are not supported" in t
    assert e.env.executor.count == 0


def test_human_review_decision_goes_through_governed_workflow(ui):
    e, at = ui()
    iid, rec, req = pending_recovery(e)
    go(at, "Human Review")
    at.text_input(key="decision_reason").set_value("Reviewed")
    at.text_input(key="decision_token").set_value(API_TOKENS["admin"])
    at.checkbox(key="decision_confirm").set_value(True)
    at.button(key="decision_submit").click().run()
    assert not at.exception
    assert e.get(f"/approvals/{req}", "reader").json()["status"] in ("approved", "consumed")
    assert e.env.executor.count == 1 and e.env.recorder.ledger.verify_integrity()
    assert API_TOKENS["admin"] not in text_of(at)
    go(at, "Human Review")
    assert any("Nothing is awaiting" in x.value for x in at.info)


def test_unauthorised_decision_from_review_page_changes_nothing(ui):
    e, at = ui()
    iid, rec, req = pending_recovery(e)
    go(at, "Human Review")
    at.text_input(key="decision_reason").set_value("trying")
    at.text_input(key="decision_token").set_value(API_TOKENS["viewer"])
    at.checkbox(key="decision_confirm").set_value(True)
    at.button(key="decision_submit").click().run()
    assert any("refused" in x.value for x in at.error) and e.env.executor.count == 0
    assert e.get("/reviews/summary", "reader").json()["pending"] == 1


def test_pipelines_page_and_overview_sources_are_separate(ui):
    e, at = ui()
    go(at, "Pipelines & runs")
    assert not at.exception and "SQL Server simulator" in text_of(at)
    go(at, "Overview")
    labels = {m.label: m.value for m in at.metric}
    assert labels["Demo incidents (synthetic)"] == "100" and labels["Incidents"] == "0"  # never summed with real incidents


def test_deep_link_opens_the_requested_page_and_incident(ui):
    e, at = ui()
    iid = e.new_incident()["incident_id"]
    at.query_params["page"] = "Incidents"
    at.query_params["incident"] = iid
    at.run()
    assert not at.exception and at.sidebar.radio(key="page").value == "Incidents"
    assert at.selectbox(key="inc_pick").value == iid


def test_navigation_link_to_the_ledger_explorer_exists(ui):
    e, at = ui()
    at.run()
    assert any("VerbaLedger" in str(getattr(b, "label", "")) for b in at.sidebar.get("link_button"))


def test_backend_down_shows_error_states_not_zeros(ui):
    e, at = ui()
    runtime.set_override("ops", OpsClient("http://127.0.0.1:9/api/v1", API_TOKENS["viewer"], timeout=1.0))
    at.run()
    go(at, "Human Review")
    assert at.error and not any(m.label == "Pending human review" for m in at.metric)
    go(at, "Pipelines & runs")
    assert at.error
