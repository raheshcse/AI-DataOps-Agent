"""Phase 10: Operations Dashboard logic that needs no Streamlit - HTTP layer, clients, view models, static guards."""

import ast
import json
from pathlib import Path

import httpx
import pytest

from dashboard import config, ops_data as d
from dashboard.http_core import HttpCore, Problem, Result, seg
from dashboard.ledger_client import LedgerClient
from dashboard.ops_client import OpsClient

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "dash-secret-token-0123456789abcdef"


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def counting(status=200, body=None, exc=None, headers=None):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        if exc:
            raise exc("simulated", request=request)
        return httpx.Response(status, json=body if body is not None else {}, headers=headers)

    return calls, mock_client(handler)


# --------------------------------------------------------------------------------------------- HTTP layer
def test_clients_only_reach_their_allow_listed_paths_and_methods():
    calls, http = counting()
    ops = OpsClient("http://api/api/v1", TOKEN, http=http)
    with pytest.raises(ValueError):
        ops._core.get("/ledger/events")
    with pytest.raises(ValueError):
        ops._core.request("DELETE", "/incidents/x")
    with pytest.raises(ValueError):
        ops._core.get("/incidents/../ledger/events")
    with pytest.raises(ValueError):
        ops._core.get("/incidents?x=1")
    led = LedgerClient("http://api/api/v1", TOKEN, http=http)
    for bad in ("/incidents", "/approvals/x"):
        with pytest.raises(ValueError):
            led._core.get(bad)
    with pytest.raises(ValueError):
        led._core.request("POST", "/ledger/events")
    assert calls == []  # nothing was ever sent


def test_path_segments_are_quoted():
    assert seg("a/b?c#d") == "a%2Fb%3Fc%23d"
    calls, http = counting(200, {})
    OpsClient("http://api/api/v1", TOKEN, http=http).incident("x/../../ledger/events")
    assert calls[0].url.raw_path.startswith(b"/api/v1/incidents/x%2F..%2F..%2Fledger%2Fevents")


@pytest.mark.parametrize("exc,kind", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "unreachable")])
def test_a_request_is_sent_at_most_once_and_transport_failures_are_typed(exc, kind):
    calls, http = counting(exc=exc)
    res = OpsClient("http://api/api/v1", TOKEN, http=http).decide("apr-1", "approve", "ok", "approver-token-0123456789abcd")
    assert not res.ok and res.problem.kind == kind and res.problem.outcome_unknown is True and len(calls) == 1


def test_server_errors_are_not_retried():
    calls, http = counting(502, {"error": {"code": "boom", "message": "bad", "request_id": "req-1"}})
    res = OpsClient("http://api/api/v1", TOKEN, http=http).decide("apr-1", "approve", "ok", "approver-token-0123456789abcd")
    assert res.problem.kind == "server_error" and res.problem.outcome_unknown and len(calls) == 1


def test_redirects_are_not_followed_and_never_forward_the_token():
    calls, http = counting(302, {}, headers={"location": "http://evil.example/steal"})
    res = OpsClient("http://api/api/v1", TOKEN, http=http).health()
    assert not res.ok and len(calls) == 1


@pytest.mark.parametrize("status,kind", [(401, "unauthenticated"), (403, "forbidden"), (404, "not_found"), (422, "validation"),
                                         (429, "rate_limited"), (503, "unavailable"), (500, "server_error"), (418, "client_error")])
def test_error_envelope_is_mapped_to_a_problem(status, kind):
    _, http = counting(status, {"error": {"code": "c", "message": "m", "request_id": "req-9", "details": {"reason": "r"}}})
    p = OpsClient("http://api/api/v1", TOKEN, http=http).incidents().problem
    assert (p.kind, p.status, p.code, p.request_id, p.details) == (kind, status, "c", "req-9", {"reason": "r"})


def test_non_json_success_is_a_bad_response_not_data():
    http = mock_client(lambda r: httpx.Response(200, text="<html>nope</html>"))
    assert OpsClient("http://api/api/v1", TOKEN, http=http).health().problem.kind == "bad_response"


def test_tokens_are_never_in_repr_or_messages():
    ops = OpsClient("http://api/api/v1", TOKEN)
    assert TOKEN not in repr(ops) and TOKEN not in str(ops._core) and TOKEN not in repr(LedgerClient("http://x/api", TOKEN))
    _, http = counting(401, {"error": {"code": "unauthenticated", "message": "Authentication failed.", "request_id": "r"}})
    p = OpsClient("http://api/api/v1", TOKEN, http=http).health().problem
    assert TOKEN not in repr(p)


def test_decision_uses_only_the_approvers_token_and_a_minimal_body():
    calls, http = counting(200, {"approval": {"status": "approved"}})
    OpsClient("http://api/api/v1", TOKEN, http=http).decide("apr-1", "approve", "looks right", "approver-token-0123456789abcd")
    req = calls[0]
    assert req.method == "POST" and req.url.path.endswith("/approvals/apr-1/decision")
    assert req.headers["authorization"] == "Bearer approver-token-0123456789abcd"
    assert json.loads(req.content) == {"decision": "approve", "reason": "looks right"}
    with pytest.raises(ValueError):
        OpsClient("http://api/api/v1", TOKEN, http=http).decide("apr-1", "execute", "x", "t")


def test_config_validation(monkeypatch):
    assert config.validate_base_url("http://127.0.0.1:8000/api/v1/") == "http://127.0.0.1:8000/api/v1"
    for bad in ("ftp://x", "http://user:pw@host/api", "http://h/api?x=1", "nothing"):
        with pytest.raises(config.ConfigError) as exc:
            config.validate_base_url(bad)
        assert "pw" not in str(exc.value)
    monkeypatch.setenv("DASHBOARD_MAX_INCIDENTS", "999999")
    assert config.max_incidents() == 1000
    monkeypatch.setenv("DASHBOARD_REQUEST_TIMEOUT_SECONDS", "abc")
    assert config.request_timeout() == 10.0


# --------------------------------------------------------------------------------------------- loading and metrics
def inc(n, sev="high", wf="new", created="2026-10-01T10:00:00+00:00", ticket=None):
    return {"incident_id": f"INC-{n:04d}", "external_ref": None, "title": f"Incident {n}", "description": "d", "incident_type": "duplicate_records",
            "severity": sev, "status": "new", "workflow_status": wf, "created_by": "u", "created_at": created, "updated_at": created, "ticket": ticket}


class FakeOps:
    def __init__(self, items, fail_at=None):
        self.items, self.fail_at, self.calls = items, fail_at, []

    def incidents(self, *, limit=100, offset=0):
        self.calls.append((limit, offset))
        if self.fail_at is not None and offset >= self.fail_at:
            return Result(problem=Problem("unreachable", "down"))
        return Result(data={"items": self.items[offset:offset + limit], "total": len(self.items), "limit": limit, "offset": offset})


def test_loading_is_bounded_and_truncation_is_reported():
    client = FakeOps([inc(i) for i in range(250)])
    out = d.load_incidents(client, max_items=120, page_size=50)
    assert len(out.items) == 120 and out.total == 250 and out.truncated and not out.partial
    assert [c[1] for c in client.calls] == [0, 50, 100] and max(c[0] for c in client.calls) <= 50
    full = d.load_incidents(FakeOps([inc(1), inc(2)]), max_items=300)
    assert full.total == 2 and not full.truncated


def test_unavailable_data_is_not_shown_as_zero():
    out = d.load_incidents(FakeOps([], fail_at=0))
    assert not out.available and out.problem.kind == "unreachable"
    metrics = d.overview_metrics(out)
    assert all(m.value is None for m in metrics) and "unavailable" in metrics[0].note
    genuine = d.overview_metrics(d.load_incidents(FakeOps([])))
    assert all(m.value == 0 for m in genuine)


def test_a_failed_later_page_is_flagged_partial():
    out = d.load_incidents(FakeOps([inc(i) for i in range(150)], fail_at=100), page_size=100)
    assert out.partial and len(out.items) == 100 and out.problem is not None and out.available


def test_metrics_count_real_states():
    items = [inc(1, "critical", "awaiting_approval"), inc(2, "low", "recovery_uncertain"), inc(3, "high", "recovery_completed"),
             inc(4, "high", "investigation_failed")]
    m = {x.label: x.value for x in d.overview_metrics(d.load_incidents(FakeOps(items)))}
    assert m == {"Incidents": 4, "Open high/critical": 2, "Awaiting approval": 1, "Recovery outcome uncertain": 1, "Investigations failed": 1}


def test_charts_use_only_loaded_incidents():
    items = [inc(1, created="2026-10-01T10:00:00+00:00"), inc(2, created="2026-10-01T11:00:00+00:00"), inc(3, created="2026-10-03T09:00:00+00:00")]
    assert d.created_per_day(items) == [("2026-10-01", 2), ("2026-10-03", 1)]  # no invented 10-02 bucket
    assert d.created_per_day([]) == []
    from dashboard import charts

    assert charts.severity_chart([]) is None and charts.timeline_chart([]) is None
    assert charts.severity_chart(d.count_by(items, "severity", d.SEVERITIES)) is not None


# --------------------------------------------------------------------------------------------- filter / sort / page
def rows(n=30):
    sev = ["low", "medium", "high", "critical"]
    return [d.incident_row(inc(i, sev[i % 4], "awaiting_approval" if i % 5 == 0 else "new", f"2026-10-{(i % 28) + 1:02d}T00:00:00+00:00"))
            for i in range(n)]


def test_pagination_clamps_and_counts():
    page = d.apply_view(rows(), page=99, page_size=10)
    assert page.page == 3 and page.page_count == 3 and len(page.rows) == 10 and page.total_matching == 30
    assert d.apply_view(rows(), page=0, page_size=10).page == 1
    assert d.apply_view([], page=1).page_count == 1 and d.apply_view(rows(), page_size=7).page_size == 25  # unsupported size falls back


def test_filters_search_and_sort():
    r = rows()
    assert {x["severity"] for x in d.apply_view(r, severities=("critical",), page_size=100).rows} == {"critical"}
    assert all(x["workflow_status"] == "awaiting_approval" for x in d.apply_view(r, workflows=("awaiting_approval",), page_size=100).rows)
    assert [x["incident_id"] for x in d.apply_view(r, text="inc-0007", page_size=100).rows] == ["INC-0007"]
    assert d.apply_view(r, text="no-such-thing").total_matching == 0
    sev = d.apply_view(r, sort_by="severity", descending=True, page_size=100).rows
    assert sev[0]["severity"] == "critical" and sev[-1]["severity"] == "low"
    asc = d.apply_view(r, sort_by="created_at", descending=False, page_size=100).rows
    assert [x["created_at"] for x in asc] == sorted(x["created_at"] for x in asc)
    assert d.apply_view(r, sort_by="not-a-key", page_size=100).total_matching == 30  # falls back, never raises


def test_missing_values_sort_last_in_both_directions():
    r = [{"incident_id": "a", "title": None}, {"incident_id": "b", "title": "x"}, {"incident_id": "c", "title": "y"}]
    assert d.apply_view(r, sort_by="title", descending=True).rows[-1]["incident_id"] == "a"
    assert d.apply_view(r, sort_by="title", descending=False).rows[-1]["incident_id"] == "a"


# --------------------------------------------------------------------------------------------- wording rules
def test_recovery_states_are_worded_honestly():
    base = {"status": "x", "phase": "x", "execution_status": "not_run", "verified_outcome": "not_applicable", "reasons": []}
    s = d.recovery_summary({**base, "phase": "recovery_uncertain", "status": "recovery_uncertain", "verified_outcome": "unknown"})
    assert s["uncertain"] and s["tone"] == "error" and "never retries" in " ".join(s["notes"])
    ex = d.recovery_summary({**base, "phase": "recovery_completed", "status": "executed", "execution_status": "executed", "verified_outcome": "not_verified"})
    assert ex["tone"] == "warning" and "not confirmed" in ex["headline"] and "verified" not in ex["headline"].replace("not confirmed", "")
    ok = d.recovery_summary({**base, "phase": "recovery_completed", "verified_outcome": "verified"})
    assert ok["tone"] == "success"
    wait = d.recovery_summary({**base, "phase": "awaiting_approval", "status": "awaiting_approval"})
    assert "Nothing has been executed" in wait["headline"]
    no = d.recovery_summary({**base, "phase": "recovery_not_executed", "status": "rejected", "reasons": ["rejected"]})
    assert no["headline"].startswith("Not executed")


def test_actionable_only_when_pending_and_awaiting():
    rec = {"status": "awaiting_approval", "approval_request_id": "apr-1", "approval": {"status": "pending"}}
    assert d.approval_actionable(rec)
    for change in ({"status": "executed"}, {"approval": {"status": "approved"}}, {"approval_request_id": None}, {"approval": None}):
        assert not d.approval_actionable({**rec, **change})


def test_decision_text_never_suggests_a_blind_retry():
    tone, text = d.decision_outcome_text(Result(problem=Problem("timeout", "slow")))
    assert tone == "error" and "UNKNOWN" in text and "Do not submit again" in text
    tone, text = d.decision_outcome_text(Result(problem=Problem("forbidden", "no", status=403, code="approval_denied", details={"reason": "not_permitted"})))
    assert "refused" in text and "not_permitted" in text
    tone, text = d.decision_outcome_text(Result(data={"approval": {"status": "approved"}, "recovery": None}))
    assert tone == "success" and "approved" in text


def test_hypotheses_are_never_labelled_confirmed():
    detail = {"status": "completed", "report_status": "issue_identified", "summary": "s", "report": {
        "observed_findings": [{"statement": "fact"}], "hypotheses": [{"statement": "maybe"}],
        "likely_root_cause": {"status": "supported", "statement": "dupes", "explanation": "e"}, "confidence": "medium",
        "confidence_explanation": "c", "recommended_next_steps": [], "limitations": [], "tools_executed": [], "evidence": []}}
    v = d.report_view(detail)
    assert "NOT confirmed" in v["cause_label"] and "unconfirmed" in v["hypothesis_label"].lower()
    detail["report"]["likely_root_cause"]["status"] = "undetermined"
    assert d.report_view(detail)["cause_label"] == "Root cause undetermined"
    assert d.report_view({"status": "running"})["has_report"] is False


def test_closed_tickets_carry_the_caution():
    assert "not evidence" in d.ticket_view({"status": "closed"})["caution"]
    assert d.ticket_view({"status": "open"})["caution"] is None and d.ticket_view(None)["present"] is False


def test_status_rows_report_degraded_and_unsafe_modes():
    ready = Result(problem=Problem("unavailable", "x", status=503, body={"status": "not_ready", "environment": "test",
                                                                           "checks": {"operations_database": "unavailable", "authentication": "dev_static", "governance": "suspended"}}))
    rows, warnings = d.status_rows(Result(data={"status": "ok", "version": "1"}), ready)
    assert any(r["check"] == "operations database" and r["state"] == "unavailable" for r in rows)
    assert len(warnings) == 2 and any("development-only" in w for w in warnings) and any("SUSPENDED" in w for w in warnings)
    rows, warnings = d.status_rows(Result(problem=Problem("unreachable", "down")), Result(problem=Problem("unreachable", "down")))
    assert all(r["state"] == "unreachable" for r in rows)


# --------------------------------------------------------------------------------------------- static guarantees
FORBIDDEN_IMPORTS = ("sqlalchemy", "sqlite3", "pyodbc", "vsl_core", "vsl_langgraph", "app.persistence", "app.governance",
                      "app.recovery", "app.services", "app.db", "app.simulation", "app.diagnostics", "app.agent", "app.integrations", "app.api")


def imports_of(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
            found |= {f"{node.module}.{a.name}" for a in node.names}
    return found


@pytest.mark.parametrize("path", sorted((ROOT / "dashboard").glob("*.py")), ids=lambda p: p.name)
def test_ui_never_imports_storage_governance_or_backend_packages(path):
    bad = [i for i in imports_of(path) if any(i == f or i.startswith(f + ".") for f in FORBIDDEN_IMPORTS)]
    assert not bad, bad


def test_the_two_apps_are_isolated_from_each_other():
    ops_files = ["ops_app.py", "ops_data.py", "ops_client.py", "charts.py"]
    for name in ops_files:
        text = (ROOT / "dashboard" / name).read_text(encoding="utf-8")
        assert "ledger_client" not in text and "ledger_data" not in text and "/ledger" not in text, name
    for name in ("ledger_app.py", "ledger_data.py", "ledger_client.py"):
        got = imports_of(ROOT / "dashboard" / name)
        assert not any("ops_" in i or i.endswith("charts") for i in got), (name, got)


def test_no_ui_module_writes_files_or_runs_commands():
    for path in (ROOT / "dashboard").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                assert name not in {"write_text", "write_bytes", "unlink", "system", "Popen", "run", "check_output", "eval", "exec"}, (path.name, name)
                if name == "open":
                    assert not (len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and set("wax+") & set(str(node.args[1].value)))
