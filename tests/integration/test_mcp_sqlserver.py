"""SQL Server integration tests for the Phase 4 MCP server.

Same safety model as the other integration tests: only the isolated ``*_test`` database is touched
(the ``svc`` fixture wipes and re-seeds it per test), never ``dataops_sim``. Skipped unless
RUN_SQLSERVER_TESTS=1; a skip is not a pass. These prove what the in-memory tests cannot: the MCP
tools produce the same results over real T-SQL, and the server issues no write statements.
"""

import asyncio
import json
import re

from mcp import Client
from sqlalchemy import event

from app.diagnostics import DiagnosticsService
from app.mcp_server import TOOL_CATALOGUE, ServiceProvider, create_server

WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|DENY|REVOKE)\b",
                       re.IGNORECASE)


def call(server, name, arguments=None):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, arguments or {})

    return asyncio.run(go())


def fingerprint(svc):
    return (svc.repo.table_counts(), svc.repo.get_state()["sim_now"],
            {k: v["status"] for k, v in svc.repo.get_scenarios().items()},
            [r["run_id"] for r in svc.repo.list_runs(limit=100)])


def test_every_tool_matches_the_diagnostics_service_and_changes_nothing(svc, engine):
    dx = DiagnosticsService(svc.repo, svc.logs, svc.threshold)
    server = create_server(ServiceProvider(lambda: dx))
    run_id = svc.repo.list_runs(None, 1)[0]["run_id"]
    expected = {
        "get_pipeline_status": ({}, dx.get_pipeline_status),
        "inspect_pipeline_logs": ({"run_id": run_id}, lambda: dx.inspect_pipeline_logs(run_id)),
        "detect_duplicate_records": ({}, dx.detect_duplicate_records),
        "diagnose_data_quality": ({}, dx.diagnose_data_quality),
        "check_data_freshness": ({}, dx.check_data_freshness),
        "reconcile_record_counts": ({}, dx.reconcile_record_counts),
        "validate_schema_contract": ({}, dx.validate_schema_contract),
        "run_all_diagnostics": ({}, dx.run_all),
    }
    assert set(expected) == set(TOOL_CATALOGUE)

    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    before = fingerprint(svc)
    try:
        for name, (args, direct) in expected.items():
            result = call(server, name, args)
            content = result.structured_content
            assert content == json.loads(json.dumps(direct().to_dict(), default=str)), name
            assert result.is_error is (content.get("status", content.get("overall_status")) == "error"), name
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert fingerprint(svc) == before
    assert statements, "no SQL was captured"
    assert not [s for s in statements if WRITE_SQL.match(s)], "the MCP server issued a write statement"


def test_findings_and_errors_over_real_sql_server(svc):
    dx = DiagnosticsService(svc.repo, svc.logs, svc.threshold)
    server = create_server(ServiceProvider(lambda: dx))
    baseline = call(server, "run_all_diagnostics").structured_content
    assert baseline["overall_status"] in {"ok", "warning"} and len(baseline["results"]) == 7
    freshness = next(r for r in baseline["results"] if r["diagnostic_name"] == "check_data_freshness")
    assert freshness["status"] == "ok" and freshness["observed_value"]["age_minutes"] == 1380.0

    svc.activate("duplicate-records")
    quality = call(server, "diagnose_data_quality")
    assert quality.is_error is False and quality.structured_content["status"] == "issue_detected"

    missing = call(server, "inspect_pipeline_logs", {"run_id": "RUN-DOES-NOT-EXIST"})
    assert missing.is_error is True and missing.structured_content["error_code"] == "DIAG-UNKNOWN-RUN"
    assert call(server, "get_pipeline_status", {"limit": 0}).is_error is True
