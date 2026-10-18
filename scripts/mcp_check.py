"""Check the MCP server end to end over stdio, the way an MCP client would use it.

    python -m scripts.mcp_check            # list the tools, then call run_all_diagnostics
    python -m scripts.mcp_check --list     # only list the tools (never touches the database)

It starts ``python -m app.mcp_server`` as a subprocess, so it reads your .env and, unless --list is
given, runs the read-only diagnostics against the configured database. Nothing is modified.
Exit codes: 0 ok/warning, 1 issue detected, 2 the server or a diagnostic could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]


async def check(list_only: bool) -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"], cwd=str(ROOT))
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        print(f"MCP server answered over stdio: {len(tools)} tools")
        for tool in tools:
            first_line = (tool.description or "").strip().splitlines()[0]
            print(f"  {tool.name}: {first_line}")
        if list_only:
            return 0
        result = await client.call_tool("run_all_diagnostics", {})
    report = result.structured_content
    if not isinstance(report, dict) or "results" not in report:
        print("run_all_diagnostics did not return a structured report.")
        return 2
    print(f"run_all_diagnostics: overall={report['overall_status']} highest_severity={report['highest_severity']} "
          f"counts={report['counts']}")
    for r in report["results"]:
        print(f"  [{r['status']}/{r['severity']}] {r['diagnostic_name']}: {r['summary']}")
    if report["overall_status"] == "error":
        return 2
    return 1 if report["overall_status"] == "issue_detected" else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="only list the tools; do not run any diagnostic")
    return asyncio.run(check(parser.parse_args().list))


if __name__ == "__main__":
    raise SystemExit(main())
