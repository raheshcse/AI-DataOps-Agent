"""Phase 11: VerbaLedger Explorer overview, search, stage-based trace and cross-navigation (AppTest + real read-only API)."""
from __future__ import annotations

from dashboard import ledger_data as ld
from tests.api_support import api  # noqa: F401
from tests.test_phase10_explorer import digest, go, text_of, ui  # noqa: F401
from tests.test_phase10_ledger_api import flow, violate


# ---- API additions
def test_summary_has_real_counts_decision_classes_and_no_synthetic_events(api):
    e = api()
    flow(e)
    s = e.get("/ledger/summary", "viewer").json()
    assert s["total_records"] > 0 and s["origin"] == "real_recorded_events" and s["synthetic_events"] == 0
    assert sum(s["by_day"].values()) == s["total_records"]
    d = s["decisions"]
    assert d["allowed"] >= 1 and d["human_review"] >= 1 and d["suspended"] == 0
    assert d["allowed"] == sum(n for o, n in s["by_outcome"].items() if o in s["decision_classes"]["allowed"])
    violate(e)
    assert e.get("/ledger/summary", "viewer").json()["total_records"] > s["total_records"]


def test_event_text_search_is_a_bound_filter(api):
    e = api()
    iid, rec, req = flow(e)
    hit = e.get("/ledger/events?q=approval_granted", "viewer").json()
    assert hit["total"] >= 1 and all("approval_granted" in (i["outcome"] or "") for i in hit["items"])
    assert e.get("/ledger/events?q=zzzz-nothing", "viewer").json()["total"] == 0
    assert e.get("/ledger/events?q=x'%3B--", "viewer").status_code == 200
    assert e.get("/ledger/events?q=", "viewer").status_code == 422


# ---- pure view model
def test_lifecycle_marks_unrecorded_stages_missing_and_never_invents():
    trace = {"nodes": [{"entry_id": "a", "sequence": 1, "entry_type": "PRE_NODE", "outcome": "recovery_proposed", "time": "t"},
                       {"entry_id": "b", "sequence": 2, "entry_type": "MONITOR", "outcome": "approval_requested", "time": "t"}]}
    stages = {s["stage"]: s for s in ld.lifecycle_stages(trace)}
    assert stages["governance_evaluated"]["recorded"] and stages["recovery_proposal"]["recorded"] and stages["human_review_requested"]["recorded"]
    for missing in ("incident_detected", "investigation_started", "human_decision", "authorised_execution", "verification"):
        assert stages[missing]["recorded"] is False and stages[missing]["note"]
    assert "operations store" in stages["incident_detected"]["note"]


def test_lifecycle_for_a_full_real_flow_shows_decision_and_execution(api):
    e = api()
    iid, rec, req = flow(e)
    t = e.get(f"/ledger/trace?request_id={req}", "viewer").json()
    stages = {s["stage"]: s for s in ld.lifecycle_stages(t)}
    assert stages["human_review_requested"]["recorded"] and stages["human_decision"]["recorded"] and stages["authorised_execution"]["recorded"]


# ---- app
def test_overview_shows_counts_from_the_real_ledger_and_labels_real_events(ui):
    e, at = ui()
    flow(e)
    go(at, "Overview")
    m = {x.label: x.value for x in at.metric}
    total = e.get("/ledger/summary", "viewer").json()["total_records"]
    assert m["Total events"] == str(total) and "Human review" in m
    assert "never creates, imports or displays synthetic ledger records" in text_of(at)
    assert any("Not verified in this session" in x.value for x in at.info)  # integrity is not claimed until run
    at.button(key="overview_integrity").click().run()
    assert any("Hash chain verified" in x.value for x in at.success)


def test_overview_empty_ledger_is_an_honest_empty_state(ui):
    e, at = ui()
    go(at, "Overview")
    assert any("no readable records" in x.value for x in at.info) or at.error  # empty or unavailable, never fake numbers
    assert not any(x.label == "Total events" for x in at.metric)


def test_trace_page_shows_stage_table_and_links_to_ops(ui):
    e, at = ui()
    iid, rec, req = flow(e)
    go(at, "Causal trace")
    at.radio(key="trace_kind").set_value("request_id")
    at.text_input(key="trace_value").set_value(req).run()
    assert not at.exception
    t = text_of(at)
    assert "Lifecycle stages" in t
    labels = [str(getattr(b, "label", "")) for b in at.get("link_button")]
    assert any("Operations Dashboard" in x for x in labels) and any("review for request" in x for x in labels)


def test_deep_link_opens_events_filtered_by_incident(ui):
    e, at = ui()
    iid, rec, req = flow(e)
    at.query_params["page"] = "Events"
    at.query_params["request_id"] = req
    at.run()
    assert not at.exception and at.sidebar.radio(key="page").value == "Events"
    assert at.text_input(key="f_request").value == req


def test_explorer_stays_read_only(ui):
    e, at = ui()
    flow(e)
    path = e.env.recorder.ledger.store.path
    before = digest(path)
    for page in ("Overview", "Events", "Integrity verification", "Semantic audit"):
        go(at, page)
    assert digest(path) == before
    assert any("Operations Dashboard" in str(getattr(b, "label", "")) for b in at.sidebar.get("link_button"))
