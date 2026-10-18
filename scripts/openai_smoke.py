"""OPTIONAL live smoke test: one tiny OpenAI request. BILLABLE (a few tokens) and never run by the test suite.

    python -m scripts.openai_smoke                      # prints what it would do; makes NO request
    python -m scripts.openai_smoke --yes-call-openai    # makes exactly one small structured-output request

It proves only that the key is accepted and the configured model answers a strict JSON-schema request. It does
not prove the agent's planning or report quality. The key is never printed.
Exit codes: 0 ok; 2 key missing/rejected or the request failed; 3 not confirmed.
"""

from __future__ import annotations

import argparse
import asyncio

from app.agent import LLMError, MissingApiKey, OpenAIChatModel, load_agent_config
from app.agent.llm import SMOKE_SCHEMA
from app.agent.prompts import SMOKE_SYSTEM
from scripts._common import EXIT_ENV, EXIT_REFUSED, load_settings, run_cli


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.openai_smoke", description=__doc__.splitlines()[0])
    parser.add_argument("--yes-call-openai", action="store_true", help="confirm that one billable request may be made")
    args = parser.parse_args()
    load_settings()
    config = load_agent_config()
    print(f"OPENAI_API_KEY: {config.key_state}    model: {config.model}")
    if not args.yes_call_openai:
        print("No request made. Re-run with --yes-call-openai to send ONE small billable request.")
        return EXIT_REFUSED
    try:
        llm = OpenAIChatModel(config)
    except MissingApiKey as exc:
        print(f"Cannot run the smoke test: {exc}")
        return EXIT_ENV
    try:
        data = asyncio.run(llm.complete_json("smoke", SMOKE_SYSTEM, {"instruction": "Reply with ok=true."}, SMOKE_SCHEMA))
    except LLMError as exc:
        print(f"Smoke test FAILED: {exc}")
        return EXIT_ENV
    usage = llm.last_usage or {}
    print(f"Smoke test OK: model answered {data}  usage={usage}")
    return 0


if __name__ == "__main__":
    run_cli(main)
