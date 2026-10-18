"""SQL Server integration tests for the Phase 5 agent.

Same safety model as the other integration tests: only the isolated ``*_test`` database is touched (the ``svc``
fixture wipes and re-seeds it per test), never ``dataops_sim``. Skipped unless RUN_SQLSERVER_TESTS=1; a skip is
NOT a pass. The agent runs with the offline engine or a scripted model (no OpenAI) against the real MCP server
over real T-SQL, in-process, so the server can only reach the ``*_test`` database. They prove what the
in-memory tests cannot: the whole path agent -> MCP -> diagnostics -> SQL Server works, and the agent's run
issues no write statement and leaves the database unchanged.
"""

import asyncio
import re

import pytest
from sqlalchemy import event

from app.agent import AgentConfig, OfflineHeuristicModel, investigate
from app.agent.mcp_client import Toolbox
from app.diagnostics import DiagnosticsService
from app.mcp_server import TOOL_CATALOGUE, ServiceProvider, create_server
from tests.agent_support import FINISH, ScriptedModel, draft, plan, tc

WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|DENY|REVOKE)\b",
                       re.IGNORECASE)

# (scenario, extra pipeline runs, incident text, expected error code)
CASES = [
    ("pipeline-failure", ["order_ingestion"], "The nightly order pipeline failed", "E1001"),
    ("stale-data", [], "Yesterday's order report is stale", "E2006"),
    ("duplicate-records", [], "We have duplicate orders", "E2002"),
    ("incomplete-processing", ["order_ingestion"], "Orders are missing, fewer rows than expected", "E1004"),
    ("schema-mismatch", [], "A column was renamed in the order feed", "E2005"),
    ("data-quality", [], "Order totals look incorrect", "E2003"),
]


def fingerprint(svc):
    return (svc.repo.table_counts(), svc.repo.get_state()["sim_now"],
            {k: v["status"] for k, v in svc.repo.get_scenarios().items()},
            [r["run_id"] for r in svc.repo.list_runs(limit=100)])


def server_over(svc):
    dx = DiagnosticsService(svc.repo, svc.logs, svc.threshold)
    return create_server(ServiceProvider(lambda: dx))


def run(svc, incident, llm, **cfg):
    return asyncio.run(investigate(incident, config=AgentConfig(**cfg), llm=llm, mcp_target=server_over(svc)))


def test_the_agent_discovers_all_tools_over_a_real_database(svc):
    async def go():
        async with Toolbox(server_over(svc)) as tb:
            return set(tb.tools), tb.ignored_tools, tb.missing_tools

    tools, ignored, missing = asyncio.run(go())
    assert tools == set(TOOL_CATALOGUE) and ignored == [] and missing == []


@pytest.mark.parametrize("scenario,runs,incident,code", CASES, ids=[c[0] for c in CASES])
def test_each_scenario_is_diagnosed_end_to_end_without_writing(svc, engine, scenario, runs, incident, code):
    svc.activate(scenario)
    for pipeline in runs:
        svc.run_pipeline(pipeline)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    before = fingerprint(svc)
    try:
        report = run(svc, incident, OfflineHeuristicModel())
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert fingerprint(svc) == before
    assert statements and not [s for s in statements if WRITE_SQL.match(s)], "the agent's run issued a write statement"
    assert report.status == "issue_identified" and report.likely_root_cause.status == "supported"
    cited = [e for e in report.evidence if e.evidence_id in report.likely_root_cause.evidence_ids]
    assert {e.error_code for e in cited} == {code}, [(e.tool, e.error_code) for e in cited]
    assert all(r.outcome in ("ok", "duplicate_skipped") for r in report.tools_executed)


def test_a_healthy_baseline_is_not_reported_as_a_failure(svc):
    before = fingerprint(svc)
    report = run(svc, "The orders report looks wrong", OfflineHeuristicModel())
    assert fingerprint(svc) == before
    assert report.status in ("warnings_only", "no_issue_detected") and report.likely_root_cause.status == "undetermined"
    assert report.confidence == "low"


def test_scripted_model_plus_real_database_stays_inside_the_approved_tools(svc):
    svc.activate("pipeline-failure")
    svc.run_pipeline("order_ingestion")
    before = fingerprint(svc)
    run_id = svc.repo.list_runs(None, 1)[0]["run_id"]
    llm = ScriptedModel([plan(tc("get_pipeline_status"), tc("execute_sql", sql="DROP TABLE orders")),
                         plan(tc("inspect_pipeline_logs", run_id=run_id)), FINISH],
                        report=draft(root_cause={"statement": f"Run {run_id} failed (E1001)", "evidence_ids": ["E1"],
                                                 "explanation": "status check"}))
    report = run(svc, "The nightly ingestion failed", llm)
    assert fingerprint(svc) == before and svc.repo.table_counts()["orders"] > 0
    assert [r.outcome for r in report.tools_executed] == ["ok", "not_approved", "ok"]
    assert report.status == "issue_identified" and report.likely_root_cause.status == "supported"


def test_unknown_run_id_from_a_model_becomes_a_recorded_diagnostic_error(svc):
    llm = ScriptedModel([plan(tc("inspect_pipeline_logs", run_id="RUN-DOES-NOT-EXIST")), FINISH])
    report = run(svc, "check a run", llm)
    assert report.tools_executed[0].outcome == "diagnostic_error" and report.evidence[0].error_code == "DIAG-UNKNOWN-RUN"
    assert report.status in ("failed", "inconclusive")
