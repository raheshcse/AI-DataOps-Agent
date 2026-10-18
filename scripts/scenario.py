"""Activate / restore the six controlled failure scenarios (always explicit).

    python -m scripts.scenario list
    python -m scripts.scenario activate pipeline-failure
    python -m scripts.scenario restore pipeline-failure
    python -m scripts.scenario restore-all

Keys: pipeline-failure, data-quality, duplicate-records, stale-data,
incomplete-processing, schema-mismatch. `restore` stops the condition and removes injected
rows; run history and incidents stay as evidence. For a fully clean baseline use
`python -m scripts.db_reset`.
"""

from __future__ import annotations

import argparse

from app.simulation.scenarios import SCENARIOS
from scripts._common import EXIT_OK, load_settings, make_service, print_target, run_cli, table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("restore-all")
    for name in ("activate", "restore"):
        sub.add_parser(name).add_argument("key", choices=sorted(SCENARIOS))
    args = parser.parse_args()
    settings = load_settings()
    print_target(settings)
    service = make_service(settings)

    if args.command == "list":
        rows = service.scenario_status()
        print(table(rows, [("key", "KEY", 24), ("scenario_id", "ID", 30), ("status", "STATUS", 9),
                           ("activated_at", "ACTIVATED (sim time)", 26)]))
        for r in rows:
            print(f"\n{r['key']}: {r['mechanism']}\n  observe: {r['how_to_observe']}")
    elif args.command == "activate":
        res = service.activate(args.key)
        print(f"{res.scenario_id}: {res.message}")
    elif args.command == "restore":
        res = service.restore(args.key)
        print(f"{res.scenario_id}: {res.message}")
    else:
        results = service.restore_all()
        print("\n".join(f"{r.scenario_id}: {r.message}" for r in results) or "No scenario was active.")
    return EXIT_OK


if __name__ == "__main__":
    run_cli(main)
