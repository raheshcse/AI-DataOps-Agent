"""The agent's only route to diagnostic data: an MCP client for the Phase 4 server.

* Tools are discovered at runtime; only names on ``APPROVED_TOOLS`` that the server also marks read-only
  are ever offered to the model or called. Anything else the server advertises is ignored and reported.
* Arguments are validated against the discovered input schema (and unknown names refused) before a call.
* Calls are bounded by a timeout; results are checked for the expected structure; errors become outcomes,
  never exceptions, and carry only short redacted text.
* The server subprocess is started and stopped by the async context manager.

This module must not import the diagnostics, database or simulation packages (a test enforces it).
"""

from __future__ import annotations

import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio
from jsonschema import Draft202012Validator
from mcp import Client, StdioServerParameters

from app.agent.models import ToolOutcome, ToolSpec
from app.agent.safety import safe_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The Phase 4 tools. A name here is necessary but not sufficient: the server must also advertise it read-only.
APPROVED_TOOLS = frozenset({
    "get_pipeline_status", "inspect_pipeline_logs", "detect_duplicate_records", "diagnose_data_quality",
    "check_data_freshness", "reconcile_record_counts", "validate_schema_contract", "run_all_diagnostics",
})
REPORT_TOOLS = frozenset({"run_all_diagnostics"})  # return a roll-up (``results``) instead of one result
_RESULT_KEYS = {"diagnostic_name", "status", "severity", "summary"}
_STATUSES = {"ok", "warning", "issue_detected", "error"}


def _contains_timeout(exc: BaseException) -> bool:
    """True for a timeout (asyncio/anyio TimeoutError or the SDK's request-timeout error), also when wrapped in an ExceptionGroup."""
    if isinstance(exc, TimeoutError) or (type(exc).__name__ == "MCPError" and "timed out" in str(exc).lower()):
        return True
    return any(_contains_timeout(e) for e in getattr(exc, "exceptions", ()) or ())


class McpUnavailable(Exception):
    """The MCP server could not be started or did not answer (message is safe to show)."""


def default_server_params() -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"], cwd=str(PROJECT_ROOT))


def validate_arguments(spec: ToolSpec, arguments: Any) -> list[str]:
    """Problems with ``arguments`` against the tool's input schema; empty means valid."""
    if not isinstance(arguments, dict):
        return ["arguments must be a JSON object"]
    problems: list[str] = []
    allowed = set((spec.input_schema or {}).get("properties", {}))
    for key in sorted(set(arguments) - allowed):
        problems.append(f"unknown argument {safe_text(key, 40)!r}")
    validator = Draft202012Validator(spec.input_schema or {"type": "object"})
    for error in sorted(validator.iter_errors(arguments), key=lambda e: list(e.path))[:5]:
        where = ".".join(str(p) for p in error.path) or "arguments"
        problems.append(f"{where}: {safe_text(error.message, 120)}")
    return problems


def _result_shape_ok(name: str, data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if name in REPORT_TOOLS:
        results = data.get("results")
        return (isinstance(results, list) and data.get("overall_status") in _STATUSES
                and all(isinstance(r, dict) and _RESULT_KEYS <= set(r) and r["status"] in _STATUSES for r in results))
    return _RESULT_KEYS <= set(data) and data.get("status") in _STATUSES


class Toolbox:
    """Async context manager around one MCP client session."""

    def __init__(self, target: Any = None, *, tool_timeout: float = 60.0, connect_timeout: float = 30.0):
        self._target = target if target is not None else default_server_params()
        self._tool_timeout = tool_timeout
        self._connect_timeout = connect_timeout
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self.tools: dict[str, ToolSpec] = {}
        self.ignored_tools: list[str] = []
        self.missing_tools: list[str] = []

    async def __aenter__(self) -> "Toolbox":
        stack = AsyncExitStack()
        try:
            # No cancel scope around entering the client: its task group must be exited by this same task,
            # outside any scope that closed earlier. The SDK's read timeout (= the connect timeout) bounds the handshake;
            # each tool call gets its own timeout below.
            self._client = await stack.enter_async_context(Client(self._target, read_timeout_seconds=self._connect_timeout))
            with anyio.fail_after(self._connect_timeout):
                listed = (await self._client.list_tools()).tools
        except BaseException as exc:  # noqa: BLE001 - always release the subprocess, then re-raise safely
            try:
                await stack.aclose()
            except BaseException:  # noqa: BLE001
                pass
            if isinstance(exc, (KeyboardInterrupt, SystemExit)) or isinstance(exc, anyio.get_cancelled_exc_class()):
                raise
            reason = "timed out" if _contains_timeout(exc) else type(exc).__name__
            raise McpUnavailable(f"Could not start or reach the DataOps MCP server ({reason}). "
                                 "Run: python -m scripts.mcp_check --list") from None
        self._stack = stack
        for tool in listed:
            read_only = bool(tool.annotations and tool.annotations.read_only_hint)
            if tool.name in APPROVED_TOOLS and read_only:
                self.tools[tool.name] = ToolSpec(name=tool.name, description=(tool.description or "").strip(),
                                                 input_schema=dict(tool.input_schema or {}),
                                                 output_schema=dict(tool.output_schema) if tool.output_schema else None)
            else:
                self.ignored_tools.append(tool.name)
        self.missing_tools = sorted(APPROVED_TOOLS - set(self.tools))
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Call one approved tool. Never raises for tool, protocol or timeout problems."""
        spec = self.tools.get(name)
        if spec is None or self._client is None:
            return ToolOutcome(outcome="not_approved", detail=f"{safe_text(name, 60)!r} is not an approved, discovered tool.")
        problems = validate_arguments(spec, arguments)
        if problems:
            return ToolOutcome(outcome="invalid_arguments", detail="; ".join(problems))
        try:
            with anyio.fail_after(self._tool_timeout):
                result = await self._client.call_tool(name, arguments, read_timeout_seconds=self._tool_timeout + 5)
        except TimeoutError:
            return ToolOutcome(outcome="timeout", detail=f"No answer within {self._tool_timeout:g} seconds.")
        except Exception as exc:  # noqa: BLE001 - protocol/transport failure; text withheld
            return ToolOutcome(outcome="connection_error", detail=f"The MCP call failed ({type(exc).__name__}).")

        data = result.structured_content
        if not isinstance(data, dict):
            text = " ".join(getattr(c, "text", "") for c in (result.content or []) if getattr(c, "text", None))
            if result.is_error:
                return ToolOutcome(outcome="tool_error", detail=safe_text(text or "The tool reported an error.", 300))
            return ToolOutcome(outcome="malformed_response", detail="The tool returned no structured result.")
        if not _result_shape_ok(name, data):
            return ToolOutcome(outcome="malformed_response", detail="The structured result did not have the expected fields.")
        failed = (data.get("overall_status") if name in REPORT_TOOLS else data.get("status")) == "error"
        return ToolOutcome(outcome="diagnostic_error" if failed else "ok", structured=data)
