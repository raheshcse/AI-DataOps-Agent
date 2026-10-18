"""Shared helpers for the Phase 5 agent tests (not a test module).

* ``ScriptedModel``  - a fake chat model that replays prepared plan/report answers (no network, no key).
* ``FakeToolbox``    - a stand-in for the MCP toolbox with scripted outcomes, for graph-level tests.
* ``server_for``     - the REAL Phase 4 MCP server over the in-memory repository.
* ``craft_server``   - a small hand-made MCP server for failure cases (slow, malformed, unapproved tools).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from app.agent.config import AgentConfig
from app.agent.graph import GraphDeps, build_graph
from app.agent.models import InvestigationReport, ToolOutcome, ToolSpec
from app.agent.runner import investigate
from app.diagnostics import DiagnosticsService
from app.mcp_server import ServiceProvider, create_server
from app.mcp_server.server import READ_ONLY

# What the scenario needs to be inspectable (see tests/test_diagnostics_tools.py for the same sequences).
SCENARIO_RUNS = {
    "baseline": [], "pipeline-failure": ["order_ingestion"], "stale-data": [], "duplicate-records": [],
    "incomplete-processing": ["order_ingestion"], "schema-mismatch": [], "data-quality": [],
}


def apply_scenario(service, key: str) -> None:
    if key != "baseline":
        service.activate(key)
    for pipeline in SCENARIO_RUNS[key]:
        service.run_pipeline(pipeline)


def server_for(service) -> MCPServer:
    """The real MCP server over the same repository/logs as ``service`` (in memory)."""
    return create_server(ServiceProvider(lambda: DiagnosticsService(service.repo, service.logs, service.threshold)))


def tc(tool: str, **arguments: Any) -> dict[str, str]:
    return {"tool": tool, "arguments_json": json.dumps(arguments)}


def plan(*calls: dict[str, str], rationale: str = "test") -> dict[str, Any]:
    return {"action": "call_tools", "tool_calls": list(calls), "rationale": rationale}


FINISH: dict[str, Any] = {"action": "finish", "tool_calls": [], "rationale": "done"}


def draft(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "summary": "Summary.", "hypotheses": [],
        "root_cause": {"statement": None, "evidence_ids": [], "explanation": "Not determined."},
        "impact": {"statement": None, "evidence_ids": []},
        "confidence": "high", "confidence_explanation": "", "recommended_next_steps": [], "limitations": []}
    base.update(overrides)
    return base


class ScriptedModel:
    """Replays prepared answers. An answer may be a dict, a callable(payload) -> dict, or an Exception to raise."""

    mode = "scripted"
    name = "scripted-test-model"

    def __init__(self, plans: list[Any] | None = None, report: Any = None):
        self.plans = list(plans or [])
        self.report = report if report is not None else draft()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []  # (task, system, payload)

    async def complete_json(self, task, system, payload, schema):
        self.calls.append((task, system, json.loads(json.dumps(payload, default=str))))
        if task == "plan":
            item = self.plans.pop(0) if self.plans else FINISH
        else:
            item = self.report
        if callable(item):
            item = item(payload)
        if isinstance(item, Exception):
            raise item
        return item

    def tasks(self) -> list[str]:
        return [c[0] for c in self.calls]


def result_dict(name: str, status: str = "ok", severity: str = "info", summary: str | None = None,
                error_code: str | None = None, affected: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    """A DiagnosticResult-shaped dict as the MCP server would return it."""
    data = {"diagnostic_name": name, "status": status, "severity": severity, "summary": summary or f"{name} {status}",
            "error_code": error_code, "affected_records": affected or [], "evidence": [], "notes": [],
            "parameters": {}, "observed_value": None, "expected_value": None, "recommended_next_step": None}
    data.update(extra)
    return data


def fake_spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description=f"{name} (test)", input_schema={"type": "object", "properties": {}})


class FakeToolbox:
    """Scripted ``Toolbox`` stand-in: ``outcomes[name]`` is a ToolOutcome, a dict (-> ok) or a callable(args)."""

    def __init__(self, outcomes: dict[str, Any] | None = None, names: list[str] | None = None):
        self.outcomes = outcomes or {}
        self.tools = {n: fake_spec(n) for n in (names or list(self.outcomes) or ["get_pipeline_status"])}
        self.ignored_tools: list[str] = []
        self.missing_tools: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self.calls.append((name, arguments))
        out = self.outcomes.get(name)
        if callable(out):
            out = out(arguments)
        if out is None:
            out = result_dict(name)
        if isinstance(out, dict):
            report_like = "results" in out
            return ToolOutcome(outcome="ok", structured=out) if report_like or out.get("status") != "error" else \
                ToolOutcome(outcome="diagnostic_error", structured=out)
        return out


def run_graph(llm, toolbox, *, incident: str = "orders look wrong", progress: Callable[[str], None] | None = None,
              **config: Any) -> InvestigationReport:
    cfg = AgentConfig(**config)
    graph = build_graph(GraphDeps(llm=llm, toolbox=toolbox, config=cfg, progress=progress or (lambda _m: None)))

    async def go():
        state = await graph.ainvoke({"investigation_id": "INV-TEST", "incident": incident},
                                    config={"recursion_limit": 8 * cfg.max_rounds + 20})
        return InvestigationReport.model_validate(state["report"])

    return asyncio.run(go())


def run_agent(target, incident: str, llm, **config: Any) -> InvestigationReport:
    return asyncio.run(investigate(incident, config=AgentConfig(**config), llm=llm, mcp_target=target))


def craft_server(tools: dict[str, Callable[..., Any]], *, read_only: bool = True) -> MCPServer:
    """A hand-made MCP server. ``tools`` maps a tool name to an (async) function with no parameters that
    returns a CallToolResult or a plain value."""
    server = MCPServer("crafted-test-server")
    for name, fn in tools.items():
        server.tool(name=name, annotations=READ_ONLY if read_only else None)(fn)
    return server


def structured(data: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=json.dumps(data))], structured_content=data, is_error=is_error)
