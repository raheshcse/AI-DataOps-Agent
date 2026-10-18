"""Run one investigation: validate the incident, connect to MCP, run the graph, return a report."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from app.agent.config import AgentConfig
from app.agent.graph import AgentState, GraphDeps, build_graph
from app.agent.llm import ChatModel
from app.agent.mcp_client import McpUnavailable, Toolbox
from app.agent.models import InvestigationReport
from app.agent.report import failed_report, new_investigation_id
from app.agent.safety import clean_incident


async def investigate(incident: str, *, config: AgentConfig, llm: ChatModel, mcp_target: Any = None,
                      progress: Callable[[str], None] | None = None) -> InvestigationReport:
    """Investigate an incident and return the structured report.

    Raises ``InvalidIncident`` for an unusable description. Everything else (server unreachable, model
    failure, tool failures, budgets) is reported inside the returned report, never raised.
    """
    text = clean_incident(incident, config.max_incident_chars)
    investigation_id = new_investigation_id()
    say = progress or (lambda _m: None)
    try:
        async with Toolbox(mcp_target, tool_timeout=config.tool_timeout_seconds) as toolbox:
            say(f"Connected to the DataOps MCP server: {len(toolbox.tools)} approved read-only tools"
                + (f" ({len(toolbox.ignored_tools)} other tool(s) ignored)" if toolbox.ignored_tools else ""))
            graph = build_graph(GraphDeps(llm=llm, toolbox=toolbox, config=config, progress=say))
            state: AgentState = await graph.ainvoke(
                {"investigation_id": investigation_id, "incident": text},
                config={"recursion_limit": 8 * config.max_rounds + 20})
            report = InvestigationReport.model_validate(state["report"])
            if toolbox.missing_tools:
                report.limitations.append("Expected tools not offered by the server: " + ", ".join(toolbox.missing_tools) + ".")
            if toolbox.ignored_tools:
                report.limitations.append("Tools offered by the server but not approved for the agent were ignored: "
                                          + ", ".join(sorted(toolbox.ignored_tools)) + ".")
            return report
    except McpUnavailable as exc:
        return failed_report(incident=text, mode=llm.mode, model=llm.name, kind="mcp_unavailable", message=str(exc),
                             investigation_id=investigation_id)


def investigate_sync(incident: str, **kwargs: Any) -> InvestigationReport:
    return asyncio.run(investigate(incident, **kwargs))
