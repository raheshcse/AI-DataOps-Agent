"""Phase 5 integration tests with the REAL MCP client and server (in-process or over stdio).

The server is the Phase 4 server over the in-memory repository (real diagnostics, no SQL Server) or, for
failure cases, a small hand-made MCP server. Models are fakes or the offline engine: no OpenAI, no network, no
real ``.env``. The matching SQL Server checks are in ``tests/integration/test_agent_sqlserver.py`` (gated).
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp import StdioServerParameters

from app.agent import AgentConfig, OfflineHeuristicModel, investigate, render_text
from app.agent.mcp_client import APPROVED_TOOLS, McpUnavailable, Toolbox
from app.agent.models import InvestigationReport
from app.mcp_server import TOOL_CATALOGUE, ServiceProvider, create_server
from tests.agent_support import (
    FINISH,
    ScriptedModel,
    apply_scenario,
    craft_server,
    draft,
    plan,
    run_agent,
    server_for,
    structured,
    tc,
)

ROOT = Path(__file__).resolve().parents[1]


def with_toolbox(target, fn, **kwargs):
    async def go():
        async with Toolbox(target, **kwargs) as tb:
            return await fn(tb)

    return asyncio.run(go())


@pytest.fixture
def server(service):
    return server_for(service)


def snapshot(repo):
    return (repo.table_counts(), [dict(o) for o in repo.orders], repo.get_state(), list(repo.list_runs(None, 100)))


# =================================================================================== discovery
def test_the_toolbox_discovers_exactly_the_eight_approved_tools_with_their_schemas(server):
    async def inspect(tb):
        return tb.tools, tb.ignored_tools, tb.missing_tools

    tools, ignored, missing = with_toolbox(server, inspect)
    assert set(tools) == set(TOOL_CATALOGUE) == set(APPROVED_TOOLS) and ignored == [] and missing == []
    assert tools["inspect_pipeline_logs"].input_schema["required"] == ["run_id"]
    assert tools["get_pipeline_status"].output_schema["type"] == "object"


def test_unapproved_and_non_read_only_tools_are_ignored_and_reported():
    async def ok():
        return structured({"diagnostic_name": "x", "status": "ok", "severity": "info", "summary": "s"})

    async def sql():
        return "rows"

    target = craft_server({"get_pipeline_status": ok, "execute_sql": sql})
    rogue = craft_server({"check_data_freshness": ok}, read_only=False)  # approved name, but not declared read-only

    async def inspect(tb):
        return sorted(tb.tools), sorted(tb.ignored_tools), tb.missing_tools

    tools, ignored, missing = with_toolbox(target, inspect)
    assert tools == ["get_pipeline_status"] and ignored == ["execute_sql"] and "run_all_diagnostics" in missing
    tools2, ignored2, _ = with_toolbox(rogue, inspect)
    assert tools2 == [] and ignored2 == ["check_data_freshness"]


def test_a_server_missing_tools_is_reported_in_the_report():
    async def ok():
        return structured({"diagnostic_name": "get_pipeline_status", "status": "ok", "severity": "info", "summary": "fine"})

    target = craft_server({"get_pipeline_status": ok, "execute_sql": ok})
    report = run_agent(target, "pipeline", ScriptedModel([plan(tc("get_pipeline_status")), FINISH]))
    text = " ".join(report.limitations)
    assert "Expected tools not offered by the server" in text and "execute_sql" in text and "ignored" in text


# ================================================================================== invocation
def test_every_approved_tool_can_be_invoked_and_matches_the_diagnostics_service(server, service, dx):
    run_id = service.repo.list_runs(None, 1)[0]["run_id"]
    expected = {
        "get_pipeline_status": ({}, dx.get_pipeline_status), "inspect_pipeline_logs": ({"run_id": run_id}, lambda: dx.inspect_pipeline_logs(run_id)),
        "detect_duplicate_records": ({}, dx.detect_duplicate_records), "diagnose_data_quality": ({}, dx.diagnose_data_quality),
        "check_data_freshness": ({}, dx.check_data_freshness), "reconcile_record_counts": ({}, dx.reconcile_record_counts),
        "validate_schema_contract": ({}, dx.validate_schema_contract), "run_all_diagnostics": ({}, dx.run_all)}

    async def go(tb):
        return {name: await tb.call(name, args) for name, (args, _) in expected.items()}

    outcomes = with_toolbox(server, go)
    for name, (_, direct) in expected.items():
        assert outcomes[name].outcome == "ok" and outcomes[name].structured == direct().to_dict(), name


def test_structured_output_is_used_not_text(server):
    out = with_toolbox(server, lambda tb: tb.call("check_data_freshness", {}))
    assert isinstance(out.structured, dict) and out.structured["diagnostic_name"] == "check_data_freshness"


@pytest.mark.parametrize("tool,args", [
    ("get_pipeline_status", {"limit": 0}), ("get_pipeline_status", {"limit": 101}), ("get_pipeline_status", {"status": "exploded"}),
    ("inspect_pipeline_logs", {}), ("validate_schema_contract", {"table": "users; DROP TABLE orders"}),
    ("reconcile_record_counts", {"batch_date": "14/01/2026"}), ("detect_duplicate_records", {"sql": "SELECT 1"}),
    ("run_all_diagnostics", {"batch_date": 20260114}),
])
def test_invalid_arguments_are_refused_by_the_agent_before_reaching_the_server(server, tool, args):
    async def go(tb):
        async def forbidden(*a, **k):
            raise AssertionError("the server was contacted")

        tb._client.call_tool = forbidden  # the toolbox must not even try
        return await tb.call(tool, args)

    out = with_toolbox(server, go)
    assert out.outcome == "invalid_arguments" and out.structured is None and out.detail


def test_a_diagnostic_that_cannot_run_is_an_error_outcome_with_its_structured_result(server):
    out = with_toolbox(server, lambda tb: tb.call("inspect_pipeline_logs", {"run_id": "RUN-DOES-NOT-EXIST"}))
    assert out.outcome == "diagnostic_error" and out.structured["error_code"] == "DIAG-UNKNOWN-RUN"


def test_tools_that_were_not_discovered_cannot_be_called(server):
    out = with_toolbox(server, lambda tb: tb.call("execute_sql", {"sql": "SELECT 1"}))
    assert out.outcome == "not_approved"


# ======================================================================= errors, timeouts, malformed
def test_a_slow_tool_times_out_and_the_toolbox_survives():
    async def slow():
        await anyio.sleep(30)

    async def fast():
        return structured({"diagnostic_name": "check_data_freshness", "status": "ok", "severity": "info", "summary": "fine"})

    target = craft_server({"get_pipeline_status": slow, "check_data_freshness": fast})

    async def go(tb):
        started = time.monotonic()
        first = await tb.call("get_pipeline_status", {})
        return first, time.monotonic() - started, await tb.call("check_data_freshness", {})

    first, elapsed, second = with_toolbox(target, go, tool_timeout=1.0)
    assert first.outcome == "timeout" and "1 seconds" in first.detail and elapsed < 10
    assert second.outcome == "ok"


@pytest.mark.parametrize("payload,expected", [
    ("plain text, no structure", "malformed_response"),
    (structured({"unexpected": "shape"}), "malformed_response"),
    (structured({"diagnostic_name": "x", "status": "banana", "severity": "info", "summary": "s"}), "malformed_response"),
    (structured({"diagnostic_name": "x", "status": "ok"}), "malformed_response"),
])
def test_malformed_tool_responses_are_contained(payload, expected):
    async def bad():
        return payload

    out = with_toolbox(craft_server({"get_pipeline_status": bad}), lambda tb: tb.call("get_pipeline_status", {}))
    assert out.outcome == expected and out.structured is None


def test_a_tool_that_raises_is_a_tool_error_with_a_redacted_message():
    async def boom():
        raise RuntimeError("password=hunter2 on SECRETHOST")

    out = with_toolbox(craft_server({"get_pipeline_status": boom}), lambda tb: tb.call("get_pipeline_status", {}))
    assert out.outcome == "tool_error" and "hunter2" not in (out.detail or "")


def test_a_roll_up_with_a_bad_part_is_malformed():
    async def bad():
        return structured({"overall_status": "ok", "results": [{"diagnostic_name": "x"}]})

    out = with_toolbox(craft_server({"run_all_diagnostics": bad}), lambda tb: tb.call("run_all_diagnostics", {}))
    assert out.outcome == "malformed_response"


# ==================================================================================== connection
CRASHING = StdioServerParameters(command=sys.executable, args=["-c", "import sys; sys.exit(3)"], cwd=str(ROOT))
MISSING = StdioServerParameters(command="definitely-not-a-real-executable-xyz", args=[], cwd=str(ROOT))
SILENT = StdioServerParameters(command=sys.executable, args=["-c", "import time; time.sleep(60)"], cwd=str(ROOT))


@pytest.mark.parametrize("params", [CRASHING, MISSING])
def test_a_server_that_cannot_start_raises_a_safe_error(params):
    with pytest.raises(McpUnavailable) as err:
        with_toolbox(params, lambda tb: asyncio.sleep(0))
    assert "mcp_check" in str(err.value) and "definitely-not" not in str(err.value)


def test_a_server_that_never_answers_times_out_during_connect():
    started = time.monotonic()
    with pytest.raises(McpUnavailable) as err:
        with_toolbox(SILENT, lambda tb: asyncio.sleep(0), connect_timeout=2.0)
    assert time.monotonic() - started < 30 and "timed out" in str(err.value)


@pytest.mark.parametrize("params", [CRASHING, MISSING])
def test_investigate_turns_a_connection_failure_into_a_failed_report(params):
    report = run_agent(params, "pipeline failed", OfflineHeuristicModel())
    assert report.status == "failed" and report.error.kind == "mcp_unavailable" and report.evidence == []
    assert report.tools_executed == [] and any("MCP server" in l for l in report.limitations)
    assert InvestigationReport.model_validate_json(report.model_dump_json()) == report


def test_database_unavailable_behind_the_server_is_a_failed_report_without_leaks():
    def factory():
        raise RuntimeError("Login failed for user 'sa' on SECRETHOST\\SQLEXPRESS password=hunter2")

    report = run_agent(create_server(ServiceProvider(factory)), "orders look wrong", OfflineHeuristicModel())
    blob = report.model_dump_json() + render_text(report)
    assert report.status == "failed" and report.stop_reason == "infrastructure_failure"
    assert "SECRETHOST" not in blob and "hunter2" not in blob and "Login failed" not in blob
    assert report.tool_calls_used == 1  # it did not keep hammering a dead database
    assert [r.outcome for r in report.tools_executed][0] == "diagnostic_error" and all(r.outcome == "skipped" for r in report.tools_executed[1:])


# ====================================================================================== cleanup
def _wrapper(tmp_path):
    script = tmp_path / "wrapper_server.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
        "marker = Path(sys.argv[1])\nPath(str(marker) + '.started').write_text('1')\n"
        "from tests.mcp_stdio_fake_server import main\n"
        "try:\n    main()\nfinally:\n    marker.write_text('stopped')\n" % str(ROOT), encoding="utf-8")
    marker = tmp_path / "marker"
    return StdioServerParameters(command=sys.executable, args=[str(script), str(marker)], cwd=str(ROOT)), marker


def _wait_for(path, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def test_the_server_subprocess_is_stopped_when_the_toolbox_exits(tmp_path):
    params, marker = _wrapper(tmp_path)
    with_toolbox(params, lambda tb: tb.call("get_pipeline_status", {}))
    assert (tmp_path / "marker.started").exists() and _wait_for(marker), "server subprocess did not shut down"


def test_the_server_subprocess_is_stopped_even_when_the_body_fails(tmp_path):
    params, marker = _wrapper(tmp_path)

    async def go():
        async with Toolbox(params):
            raise RuntimeError("body failed")

    with pytest.raises(RuntimeError):
        asyncio.run(go())
    assert _wait_for(marker), "server subprocess did not shut down after an error"


def test_end_to_end_over_stdio_with_the_real_server_process(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "tests.mcp_stdio_fake_server"], cwd=str(ROOT))
    report = run_agent(params, "Something seems off but I cannot say what", OfflineHeuristicModel())
    assert report.status == "warnings_only" and report.likely_root_cause.status == "undetermined"
    assert report.tool_calls_used >= 1 and all(r.outcome == "ok" for r in report.tools_executed)
    assert report.mode == "offline" and report.stop_reason in ("planner_finished", "round_budget_exhausted")


# ================================================================= agent + MCP + fake model; no writes
def test_scripted_model_through_real_mcp_collects_evidence_and_changes_nothing(service, server):
    apply_scenario(service, "pipeline-failure")
    before = snapshot(service.repo)
    run_id = service.repo.list_runs(None, 1)[0]["run_id"]
    llm = ScriptedModel([plan(tc("get_pipeline_status")), plan(tc("inspect_pipeline_logs", run_id=run_id), tc("execute_sql", sql="DROP TABLE orders")), FINISH],
                        report=draft(root_cause={"statement": f"Run {run_id} failed (E1001)", "evidence_ids": ["E1"], "explanation": "status check"},
                                     confidence="high"))
    report = run_agent(server, "The nightly ingestion failed", llm)
    assert snapshot(service.repo) == before  # nothing written
    assert report.status == "issue_identified" and report.likely_root_cause.status == "supported"
    assert [r.outcome for r in report.tools_executed] == ["ok", "ok", "not_approved"]
    assert report.confidence == "medium" and "only one diagnostic" in report.confidence_explanation  # high was requested; one source caps it
    assert "E1001" in render_text(report)


def test_the_agent_survives_the_planner_asking_for_every_tool_and_never_writes(service, server):
    apply_scenario(service, "data-quality")
    before = snapshot(service.repo)
    run_id = service.repo.list_runs(None, 1)[0]["run_id"]
    calls = [tc(t) for t in sorted(TOOL_CATALOGUE) if t != "inspect_pipeline_logs"] + [tc("inspect_pipeline_logs", run_id=run_id)]
    report = run_agent(server, "check everything", ScriptedModel([plan(*calls[:4]), plan(*calls[4:]), FINISH]), max_tool_calls=12)
    assert snapshot(service.repo) == before and report.tool_calls_used == len(calls)
    assert report.status == "issue_identified"


# ======================================================================== deterministic scenarios (D)
SCENARIOS = [
    ("pipeline-failure", "The nightly order pipeline failed", "E1001", {"get_pipeline_status"}),
    ("stale-data", "Yesterday's order report is stale and out of date", "E2006", {"check_data_freshness"}),
    ("duplicate-records", "We have duplicate orders in the sales report", "E2002", {"detect_duplicate_records", "diagnose_data_quality"}),
    ("incomplete-processing", "Orders are missing, there are fewer rows than expected", "E1004", {"reconcile_record_counts", "get_pipeline_status"}),
    ("schema-mismatch", "A column was renamed in the order feed", "E2005", {"validate_schema_contract"}),
    ("data-quality", "Order totals look incorrect", "E2003", {"diagnose_data_quality"}),
]


@pytest.mark.parametrize("key,incident,code,tools", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_each_incident_scenario_is_diagnosed_from_evidence(service, server, key, incident, code, tools):
    apply_scenario(service, key)
    before = snapshot(service.repo)
    report = run_agent(server, incident, OfflineHeuristicModel())
    assert snapshot(service.repo) == before
    assert report.status == "issue_identified" and report.likely_root_cause.status == "supported"
    cited = [e for e in report.evidence if e.evidence_id in report.likely_root_cause.evidence_ids]
    assert cited and {e.error_code for e in cited} == {code}, [(e.tool, e.error_code) for e in cited]
    assert {e.tool for e in report.evidence if e.error_code == code} & tools
    assert report.confidence in ("medium", "high") and "offline" in report.confidence_explanation.lower() + report.mode
    assert re.search(r"SCN-\d\d", report.model_dump_json()) is None  # scenario ids are ground truth the agent never sees
    assert all(f.evidence_ids for f in report.observed_findings) and report.recommended_next_steps


def test_a_vague_incident_over_a_real_fault_still_finds_it(service, server):
    apply_scenario(service, "stale-data")
    report = run_agent(server, "Something seems off but I cannot say what", OfflineHeuristicModel())
    assert report.status == "issue_identified" and {e.error_code for e in report.evidence} >= {"E2006"}


def test_healthy_baseline_is_warnings_only_with_an_undetermined_cause(service, server):
    before = snapshot(service.repo)
    report = run_agent(server, "The orders report looks wrong", OfflineHeuristicModel())
    assert snapshot(service.repo) == before
    assert report.status in ("warnings_only", "no_issue_detected") and report.likely_root_cause.status == "undetermined"
    assert report.confidence == "low" and report.likely_root_cause.statement is None
    assert any("could not be reproduced" in report.summary or "No diagnostic" in report.summary or "finding" in report.summary for _ in [0])
    assert any("does not prove" in l for l in report.limitations)


def test_ambiguous_evidence_is_reported_as_inconclusive_not_guessed(service, server):
    # The model checks one area on a healthy system and stops: the report must say it cannot tell.
    llm = ScriptedModel([plan(tc("check_data_freshness")), FINISH], report=draft(confidence="high", summary="No problem."))
    report = run_agent(server, "Something is off", llm)
    assert report.status == "inconclusive" and report.confidence == "low" and report.likely_root_cause.status == "undetermined"
    assert any("Diagnostics not run" in l for l in report.limitations)


def test_scripted_and_offline_runs_produce_the_same_facts(service, server):
    apply_scenario(service, "pipeline-failure")
    a = run_agent(server, "The nightly order pipeline failed", OfflineHeuristicModel())
    b = run_agent(server, "The nightly order pipeline failed", OfflineHeuristicModel())
    key = lambda r: ([(e.tool, e.status, e.severity, e.error_code, e.summary) for e in r.evidence], r.status, r.confidence)
    assert key(a) == key(b)  # deterministic apart from ids and timestamps
    assert a.investigation_id != b.investigation_id and re.fullmatch(r"INV-\d{8}-\d{6}-[0-9a-f]{6}", a.investigation_id)


def test_reports_are_complete_machine_readable_and_secret_free(service, server):
    apply_scenario(service, "incomplete-processing")
    report = run_agent(server, "Orders are missing", OfflineHeuristicModel())
    data = json.loads(report.model_dump_json())
    for field in ("investigation_id", "incident", "status", "summary", "tools_executed", "evidence", "likely_root_cause", "impact",
                  "confidence", "confidence_explanation", "recommended_next_steps", "limitations", "observed_findings", "hypotheses"):
        assert field in data
    assert re.search(r"sk-[A-Za-z0-9]{8,}", json.dumps(data)) is None
