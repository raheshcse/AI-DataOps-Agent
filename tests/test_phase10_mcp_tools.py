"""Phase 10: the three added read-only MCP tools (statistics, error roll-up, diagnostics health)."""

import json

import pytest

from app.diagnostics.tools import operations
from app.mcp_server import create_server, ServiceProvider
from tests.agent_support import apply_scenario
from tests.test_mcp_server import call, expect_structured, list_tools

NEW = ("get_pipeline_run_statistics", "summarize_recent_errors", "check_diagnostics_health")


def run(pid, status, duration=None, code=None, msg=None):
    return {"run_id": f"r-{id(object())}", "pipeline_id": pid, "status": status, "duration_ms": duration,
            "error_code": code, "error_message": msg, "started_at": None, "finished_at": None,
            "batch_date": None, "records_expected": None, "records_processed": None, "trigger_type": "manual"}


# ---------------------------------------------------------------- statistics (pure)
def test_statistics_streak_rate_and_duration_use_only_recorded_values():
    runs = [run("p", "failed", 100), run("p", "failed", None), run("p", "success", 40), run("p", "success", 60)]
    s = operations._pipeline_stats("p", runs)
    assert s["consecutive_failures"] == 2 and s["success_rate"] == 0.5
    assert s["duration_ms"]["samples"] == 3 and s["duration_ms"]["last"] == 100
    assert s["duration_regression"] is False  # fewer than three earlier samples


def test_statistics_duration_regression_needs_a_baseline():
    runs = [run("p", "success", 500)] + [run("p", "success", 100)] * 3
    assert operations._pipeline_stats("p", runs)["duration_regression"] is True


def test_statistics_no_runs_has_no_invented_rate():
    s = operations._pipeline_stats("p", [])
    assert s["success_rate"] is None and s["duration_ms"] is None and s["consecutive_failures"] == 0


# ---------------------------------------------------------------- service behaviour
def test_statistics_flags_a_failing_pipeline_and_does_not_write(service, dx):
    apply_scenario(service, "pipeline-failure")
    before = (service.repo.table_counts(), len(service.repo.list_runs(None, 100)))
    r = dx.get_pipeline_run_statistics()
    assert r.status in ("warning", "issue_detected") and r.diagnostic_name == "get_pipeline_run_statistics"
    assert (service.repo.table_counts(), len(service.repo.list_runs(None, 100))) == before


@pytest.mark.parametrize("kwargs", [{"window": 0}, {"window": 101}, {"window": True}, {"pipeline_id": "bad id!"}])
def test_statistics_rejects_invalid_input(dx, kwargs):
    r = dx.get_pipeline_run_statistics(**kwargs)
    assert (r.status, r.error_code) == ("error", "DIAG-INVALID-INPUT")


def test_statistics_unknown_pipeline_is_a_structured_error(dx):
    assert dx.get_pipeline_run_statistics("nope_pipeline").status == "error"


def test_error_summary_groups_by_code_and_redacts(service, dx):
    apply_scenario(service, "pipeline-failure")
    r = dx.summarize_recent_errors()
    assert r.status == "issue_detected"
    groups = r.evidence[0]["groups"]
    assert groups and all(g["count"] >= 1 for g in groups)
    assert r.error_code == groups[0]["error_code"]


def test_error_summary_redacts_secret_like_text(repo):
    class Repo:
        def list_runs(self, pid=None, limit=20, status=None):
            return [run("p", "failed", code="E1", msg="boom password=hunter2 " + "x" * 600)] if status == "failed" else []

    out = operations.summarize_recent_errors(Repo(), 5)
    text = json.dumps(out.to_dict())
    assert "hunter2" not in text and len(out.evidence[0]["groups"][0]["sample_message"]) <= 230


def test_error_summary_is_ok_without_failures():
    class Empty:
        def list_runs(self, pid=None, limit=20, status=None):
            return []

    r = operations.summarize_recent_errors(Empty(), 5)
    assert r.status == "ok" and r.evidence[0]["groups"] == []


@pytest.mark.parametrize("limit", [0, 101, True])
def test_error_summary_rejects_invalid_limit(dx, limit):
    assert dx.summarize_recent_errors(limit).error_code == "DIAG-INVALID-INPUT"


def test_health_ok_and_reports_counts_without_secrets(dx):
    r = dx.check_diagnostics_health()
    assert r.status == "ok" and any(e["type"] == "table_counts" for e in r.evidence)
    assert "password" not in json.dumps(r.to_dict()).lower()


def test_health_reports_integrity_problems(service, dx, monkeypatch):
    monkeypatch.setattr(service.repo, "integrity_problems", lambda: ["orders: orphan row"])
    r = dx.check_diagnostics_health()
    assert r.status == "issue_detected" and r.evidence[2]["count"] == 1


def test_health_unseeded_database_is_a_structured_error(repo, tmp_path):
    from app.diagnostics import DiagnosticsService
    from app.simulation.runlog import RunLogStore

    r = DiagnosticsService(repo, RunLogStore(tmp_path / "l")).check_diagnostics_health()
    assert (r.status, r.error_code) == ("error", "DIAG-NOT-SEEDED")


# ---------------------------------------------------------------- over MCP
@pytest.fixture
def server(dx):
    return create_server(ServiceProvider(lambda: dx))


def test_new_tools_are_registered_read_only_with_no_required_arguments(server):
    tools = {t.name: t for t in list_tools(server)}
    for name in NEW:
        t = tools[name]
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
        assert not t.input_schema.get("required")
        assert t.output_schema


@pytest.mark.parametrize("tool", NEW)
def test_new_tools_return_structured_results(server, tool):
    data = expect_structured(call(server, tool), error=False)
    assert data["diagnostic_name"] == tool and data["status"] in ("ok", "warning", "issue_detected", "error")


@pytest.mark.parametrize("tool,args", [("get_pipeline_run_statistics", {"window": "5"}),
                                       ("get_pipeline_run_statistics", {"window": 500}),
                                       ("summarize_recent_errors", {"limit": 0}),
                                       ("check_diagnostics_health", {"sql": "select 1"})])
def test_new_tools_refuse_bad_or_unknown_arguments(server, tool, args):
    assert call(server, tool, args).is_error is True


def test_run_all_is_unchanged_by_the_new_tools(dx):
    names = {r.diagnostic_name for r in dx.run_all().results}
    assert not names & set(NEW)


def test_no_sql_shell_or_write_tool_is_exposed(server):
    for t in list_tools(server):
        assert not any(w in t.name for w in ("sql", "shell", "exec", "write", "recover", "approve", "ticket"))
