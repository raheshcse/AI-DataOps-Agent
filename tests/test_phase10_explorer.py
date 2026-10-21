"""Phase 10: the VerbaLedger Explorer - view models and the Streamlit app (AppTest) over the real read-only ledger API."""

import hashlib
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from dashboard import ledger_data as ld, runtime
from dashboard.ledger_client import LedgerClient
from tests.api_support import API_TOKENS, api  # noqa: F401  (api is a fixture)
from tests.test_phase10_dashboard_app import collect
from tests.test_phase10_ledger_api import flow, violate

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "dashboard" / "ledger_app.py")


def text_of(at) -> str:
    return "\n".join(collect(at.main) + collect(at.sidebar))


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


@pytest.fixture
def ui(api):
    def make(role="viewer", **kw):
        e = api(**kw)
        runtime.set_override("ledger", LedgerClient("http://testserver/api/v1", API_TOKENS[role], http=e.client))
        return e, AppTest.from_file(APP, default_timeout=30)

    yield make
    runtime.set_override("ledger", None)


def go(at, page):
    try:
        at.sidebar
    except KeyError:
        at.run()
    at.sidebar.radio(key="page").set_value(page)
    at.run()
    assert not at.exception, at.exception
    return at


# ------------------------------------------------------------------------------------------ view models
def test_banner_marks_incomplete_views_and_names_the_lines():
    tone, lines = ld.banner({"as_of": "t", "fingerprint": "f", "cached": True, "malformed_count": 2,
                             "malformed": [{"line": 7, "reason": "invalid JSON"}], "scan_truncated": True})
    assert tone == "warning" and any("INCOMPLETE" in x and "line 7" in x for x in lines) and any("scan limit" in x for x in lines)
    assert ld.banner({"as_of": "t", "fingerprint": "f"})[0] == "info"


def test_vsl_details_show_only_recorded_fields():
    ev = {"entry_type": "TERMINAL", "payload": {"invariant": "action_within_allowlist", "terminal_state": "automation_suspended"}}
    labels = dict(ld.vsl_details(ev))
    assert labels["Invariant"] == "action_within_allowlist" and labels["Terminal state"] == "automation_suspended" and "Meaning" in labels
    assert ld.vsl_details({"entry_type": "PRE_NODE", "payload": {}}) == []  # nothing invented
    assert "Terminal state" not in dict(ld.vsl_details({"entry_type": "MONITOR", "payload": {"terminal_state": "x"}}))


def test_trace_dot_and_flags_separate_recorded_links_from_gaps():
    trace = {"nodes": [{"entry_id": "a", "sequence": 0, "entry_type": "MONITOR", "outcome": "x"},
                       {"entry_id": "b", "sequence": 1, "entry_type": "MONITOR", "outcome": "recovery_attempt"}],
             "edges": [{"cause": "a", "effect": "b", "basis": "caused_by"}],
             "missing_links": [{"entry_id": "b", "missing_cause": "zzz"}], "uncertain_attempts": ["b"], "incomplete": True}
    dot = ld.trace_dot(trace)
    assert '"a" -> "b"' in dot and "missing:zzz" in dot and "dashed" in dot
    kinds = [k for k, _ in ld.trace_flags(trace)]
    assert kinds.count("error") == 2


def test_integrity_and_audit_views_never_merge_their_verdicts():
    v = ld.integrity_view({"status": "could_not_complete", "records_examined": 3, "errors": ["limit"], "limits": ["l"]})
    assert v["tone"] == "warning" and "NOT" in v["heading"]
    f = ld.integrity_view({"status": "failed", "first_failure": {"line": 2, "sequence": 1, "entry_id": "e", "kind": "entry_hash_mismatch", "reason": "r"}})
    assert f["tone"] == "error" and any("line 2" in x for x in f["lines"])
    a = ld.audit_view({"status": "passed", "integrity_passed": False, "checks": [{"title": "t", "name": "n", "status": "passed", "violations": []}]})
    assert a["tone"] == "success" and "NOT intact" in a["integrity_note"] and "separate" in a["integrity_note"]


# ------------------------------------------------------------------------------------------ app
def test_events_page_lists_filters_and_shows_vsl_detail(ui):
    e, at = ui()
    violate(e)
    go(at, "Events")
    assert len(at.dataframe[0].value) == 3 and "Automation suspended" in [m.label for m in at.metric]
    assert {m.label: m.value for m in at.metric}["Automation suspended"] == "yes"
    first = at.dataframe[0].value["entry_id"].iloc[-1]
    at.selectbox(key="event_pick").set_value(first).run()
    page = text_of(at)
    assert "Recorded VSL details" in page and "action_within_allowlist" in page


def test_empty_and_unavailable_ledgers_are_explained(ui):
    e, at = ui()
    at.run()
    e.env.ledger_path.unlink()
    at.sidebar.button(key="refresh").click().run()
    assert any("Could not load the ledger summary" in x.value for x in at.error) and not at.exception
    assert not e.env.ledger_path.exists()  # looking never creates the file


def test_malformed_lines_are_flagged_as_incomplete_in_the_ui(ui):
    e, at = ui()
    flow(e)
    with e.env.ledger_path.open("ab") as fh:
        fh.write(b"not json\n")
    go(at, "Events")
    assert any("INCOMPLETE" in w.value and "NOT shown" in w.value for w in at.warning)


def test_causal_trace_page_shows_links_gaps_and_uncertainty(ui):
    e, at = ui()
    e.services.recorder.monitor(decision_id="apr-unc000000001", drift=False, outcome="recovery_attempt", request_id="apr-unc000000001")
    go(at, "Causal trace")
    assert any("Enter a decision id" in i.value for i in at.info)
    at.radio(key="trace_kind").set_value("request_id")
    at.text_input(key="trace_value").set_value("apr-unc000000001").run()
    assert any("UNCERTAIN" in x.value for x in at.error)
    assert "not causal evidence" in text_of(at)
    at.text_input(key="trace_value").set_value("dec-nothing-here").run()
    assert any("No recorded events match" in i.value for i in at.info)


def test_integrity_page_runs_only_on_request_and_reports_the_first_failure(ui):
    e, at = ui()
    flow(e)
    go(at, "Integrity verification")
    assert any("Not run yet" in i.value for i in at.info)
    at.button(key="run_integrity").click().run()
    assert any("verified" in s.value for s in at.success)
    rows = [json.loads(x) for x in e.env.ledger_path.read_text().splitlines()]
    rows[1]["payload"]["x"] = 1
    e.env.ledger_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    at.button(key="run_integrity").click().run()
    assert any("FAILED" in x.value for x in at.error) and "line 2" in text_of(at)
    assert "What this does NOT prove" in text_of(at) or at.expander


def test_audit_page_separates_semantic_checks_from_integrity(ui):
    e, at = ui()
    violate(e)
    go(at, "Semantic audit")
    at.button(key="run_audit").click().run()
    page = text_of(at)
    assert any("FAILED" in x.value for x in at.error) and "separate" in page and "Hash-chain result" in page
    assert "Not run" in page  # the monitoring-gap check is not run without a threshold


def test_explorer_is_read_only_whatever_the_user_does(ui):
    e, at = ui()
    flow(e)
    violate(e)
    before = digest(e.env.ledger_path)
    for page in ("Events", "Causal trace", "Integrity verification", "Semantic audit", "About this explorer"):
        go(at, page)
        for key in ("run_integrity", "run_audit"):
            if [b for b in at.button if b.key == key]:
                at.button(key=key).click().run()
    at.sidebar.button(key="refresh").click().run()
    assert digest(e.env.ledger_path) == before


def test_a_role_without_ledger_read_gets_a_clear_error_and_no_data(ui):
    e, at = ui("operator")
    flow(e)
    at.run()
    assert any("Could not load the ledger summary" in x.value for x in at.error)
    assert not at.dataframe and API_TOKENS["operator"] not in text_of(at)


def test_tokens_and_paths_never_appear_in_the_explorer(ui):
    e, at = ui()
    flow(e)
    for page in ("Events", "Causal trace", "Integrity verification", "Semantic audit", "About this explorer"):
        go(at, page)
        t = text_of(at)
        assert not any(tok in t for tok in API_TOKENS.values()) and str(e.env.ledger_path) not in t and ".jsonl" not in t
