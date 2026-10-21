"""Phase 12: the VerbaLedger Explorer shows the real persisted event counts prominently (AppTest + real read-only API)."""
from __future__ import annotations

from tests.api_support import api  # noqa: F401
from tests.test_phase10_explorer import go, text_of, ui  # noqa: F401
from tests.test_phase10_ledger_api import flow


def test_counts_are_prominent_on_every_page_and_match_the_ledger(ui):
    e, at = ui()
    iid, rec, req = flow(e)
    summary = e.get("/ledger/summary", "viewer").json()
    for page in ("Overview", "Events"):
        go(at, page)
        side = {m.label: m.value for m in at.sidebar.metric}
        assert side["Governance events recorded"] == str(summary["total_records"])
    go(at, "Overview")
    m = {x.label: x.value for x in at.metric}
    assert m["Total events"] == str(summary["total_records"]) and m["Recovery"] == str(summary["recovery_related"])
    assert "Latest recorded event: #" in text_of(at)
    assert iid in text_of(at)  # "Events by incident" lists the incident that really has ledger records


def test_events_page_shows_the_count_for_one_incident_and_the_integrity_state(ui):
    e, at = ui()
    iid, rec, req = flow(e)
    expected = e.get(f"/ledger/events?incident_id={iid}&limit=200", "viewer").json()["total"]
    at.session_state["f_incident"] = iid
    go(at, "Events")
    assert any(m.label == f"Events for incident {iid}" and m.value == str(expected) for m in at.metric)
    assert "Integrity: not verified in this session" in text_of(at)


def test_empty_ledger_explains_how_to_generate_real_events(ui):
    e, at = ui()
    go(at, "Overview")
    t = text_of(at)
    assert "no readable records yet" in t.lower() and "governed workflow" in t
    assert all(m.value in ("0",) for m in at.sidebar.metric if m.label == "Governance events recorded")


def test_no_bare_conditional_expression_statements_in_the_dashboards():
    """Streamlit 'magic' renders a bare expression statement. ``a.plotly_chart(x) if x else a.info(y)`` would therefore print a
    raw DeltaGenerator object under the chart (seen in a real screenshot), so such statements are not allowed."""
    import ast
    from pathlib import Path

    for path in sorted((Path(__file__).resolve().parents[1] / "dashboard").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bad = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.IfExp)]
        assert not bad, f"{path.name}: bare conditional expression statement at line(s) {bad}"


def test_the_overview_renders_no_object_repr(ui):
    e, at = ui()
    flow(e)
    go(at, "Overview")
    assert "DeltaGenerator" not in text_of(at) and not at.exception
