"""Investigate an incident with the Phase 5 DataOps agent.

    python -m scripts.investigate "Yesterday's orders are missing from the sales report" --offline
    python -m scripts.investigate "..." --offline --json
    python -m scripts.investigate "..."                  # live: uses OpenAI (needs OPENAI_API_KEY; billable)
    python -m scripts.investigate --check-config         # key/config status; no network, no MCP, no OpenAI

The agent reaches data only through the read-only DataOps MCP server (started as a subprocess; it reads your
.env and queries the configured database, changing nothing). ``--offline`` uses a deterministic rule-based
stand-in instead of a language model: no key, no network, no cost.

Exit codes: 0 no issue / warnings only; 1 issue identified or inconclusive; 2 failed (configuration, MCP,
database or model error); 3 invalid input or live call refused.
"""

from __future__ import annotations

import argparse
import sys

from app.agent import (
    InvalidIncident,
    LLMError,
    MissingApiKey,
    OfflineHeuristicModel,
    OpenAIChatModel,
    investigate_sync,
    load_agent_config,
    render_text,
)
from scripts._common import EXIT_ENV, EXIT_REFUSED, load_settings, run_cli

EXIT_BY_STATUS = {"no_issue_detected": 0, "warnings_only": 0, "issue_identified": 1, "inconclusive": 1, "failed": 2}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m scripts.investigate", description=__doc__.splitlines()[0])
    p.add_argument("incident", nargs="?", help="what went wrong, in plain words (quote it)")
    p.add_argument("--offline", action="store_true", help="deterministic rule-based mode: no OpenAI, no key, no cost")
    p.add_argument("--json", action="store_true", help="print the report as JSON instead of text")
    p.add_argument("--no-evidence", action="store_true", help="omit the per-tool evidence section from the text report")
    p.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    p.add_argument("--check-config", action="store_true", help="show configuration and key status, then exit (no network)")
    p.add_argument("--model", help="OpenAI model for this run (default: OPENAI_MODEL)")
    p.add_argument("--max-rounds", type=int, help="planning rounds (default 3, max 6)")
    p.add_argument("--max-tool-calls", type=int, help="total MCP tool calls (default 12, max 20)")
    return p


def _check_config(config) -> int:
    print("Agent configuration (no network calls made)")
    print(f"  OPENAI_API_KEY : {config.key_state}")  # never the value
    print(f"  model          : {config.model}")
    print(f"  budgets        : rounds={config.max_rounds} tool_calls={config.max_tool_calls} "
          f"per_round={config.max_calls_per_round} tool_timeout={config.tool_timeout_seconds:g}s")
    print(f"  offline mode   : always available (use --offline)")
    if config.key_state == "configured":
        print("  live mode      : ready (a live run calls OpenAI and is billable)")
        return 0
    print("  live mode      : NOT ready - add a real OPENAI_API_KEY to .env (see docs/agent.md)")
    return EXIT_ENV


def main() -> int:
    args = _parser().parse_args()
    settings = load_settings()  # prints a safe message and exits 2 if .env is invalid
    del settings
    overrides = {"model": args.model, "max_rounds": args.max_rounds, "max_tool_calls": args.max_tool_calls}
    try:
        config = load_agent_config(**overrides)
    except ValueError as exc:
        print(f"Invalid option: {exc.__class__.__name__} (check --max-rounds 1-6 and --max-tool-calls 1-20)")
        return EXIT_REFUSED
    if args.check_config:
        return _check_config(config)
    if args.incident is None:
        print("Give an incident description, e.g.  python -m scripts.investigate \"Orders are missing\" --offline")
        return EXIT_REFUSED

    try:
        llm = OfflineHeuristicModel() if args.offline else OpenAIChatModel(config)  # key checked here, before any I/O
    except MissingApiKey as exc:
        print(f"Cannot start a live investigation: {exc}")
        return EXIT_ENV
    progress = None if args.quiet else (lambda m: print(m, file=sys.stderr, flush=True))
    if progress:
        progress(f"Mode: {llm.mode} ({llm.name})" + ("" if args.offline else " - this run calls OpenAI and is billable"))
    try:
        report = investigate_sync(args.incident, config=config, llm=llm, progress=progress)
    except InvalidIncident as exc:
        print(f"Invalid incident: {exc}")
        return EXIT_REFUSED
    except LLMError as exc:  # defensive: the graph reports model failures inside the report
        print(f"Model error: {exc}")
        return EXIT_ENV

    print(report.model_dump_json(indent=2) if args.json else render_text(report, show_evidence=not args.no_evidence))
    return EXIT_BY_STATUS[report.status]


if __name__ == "__main__":
    run_cli(main)
