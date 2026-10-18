"""Inspect runs, validations, logs, incidents, freshness and row counts (read-only).

    python -m scripts.show runs [--pipeline order_ingestion] [--limit 10]
    python -m scripts.show run RUN_ID            # run record + validation results
    python -m scripts.show logs RUN_ID [--level ERROR]
    python -m scripts.show incidents
    python -m scripts.show freshness
    python -m scripts.show counts
"""

from __future__ import annotations

import argparse
import json

from scripts._common import EXIT_OK, EXIT_REFUSED, load_settings, make_service, print_target, run_cli, table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("runs")
    p.add_argument("--pipeline")
    p.add_argument("--limit", type=int, default=10)
    sub.add_parser("run").add_argument("run_id")
    p = sub.add_parser("logs")
    p.add_argument("run_id")
    p.add_argument("--level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub.add_parser("incidents")
    sub.add_parser("freshness")
    sub.add_parser("counts")
    args = parser.parse_args()
    settings = load_settings()
    print_target(settings)
    service = make_service(settings)
    repo = service.repo

    if args.command == "runs":
        rows = repo.list_runs(args.pipeline, max(1, min(args.limit, 200)))
        print(table(rows, [("run_id", "RUN_ID", 26), ("pipeline_id", "PIPELINE", 17), ("batch_date", "BATCH", 10),
                           ("status", "STATUS", 8), ("records_expected", "EXP", 4), ("records_processed", "PROC", 4),
                           ("error_code", "ERR", 6), ("started_at", "STARTED (sim, UTC)", 26)]))
    elif args.command == "run":
        run = repo.get_run(args.run_id)
        if run is None:
            print("No such run.")
            return EXIT_REFUSED
        for k, v in run.items():
            print(f"{k:18} {v.isoformat() if hasattr(v, 'isoformat') else v}")
        vals = repo.get_validations(args.run_id)
        if vals:
            print("\nValidation results:")
            print(table(vals, [("rule_name", "RULE", 32), ("outcome", "OUTCOME", 7), ("expected_value", "EXPECTED", 14),
                               ("observed_value", "OBSERVED", 22), ("record_ref", "RECORD REF", 40)]))
            for v in vals:
                if v["outcome"] != "pass":
                    print(f"  - {v['rule_name']}: {v['detail']}")
    elif args.command == "logs":
        events = service.logs.read(args.run_id)
        if not events:
            print("No log file for that run id (check the id with `show runs`).")
            return EXIT_REFUSED
        for e in events:
            if args.level and e["level"] != args.level:
                continue
            code = f" [{e['error_code']}]" if e["error_code"] else ""
            print(f"{e['timestamp']} {e['level']:7} {e['component']:17} {e['event']}{code}: {e['message']}")
        print(f"\n(raw JSON Lines: {service.logs.path_for(events[0]['pipeline_id'], args.run_id)})")
    elif args.command == "incidents":
        print(table(repo.list_incidents(50), [("incident_id", "ID", 9), ("category", "CATEGORY", 22), ("severity", "SEV", 8),
                                              ("status", "STATUS", 9), ("run_id", "RUN_ID", 26), ("title", "TITLE", 44)]))
    elif args.command == "freshness":
        f = service.freshness()
        print(json.dumps({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in f.items()}, indent=2))
        print("RESULT:", "STALE" if f["stale"] else "FRESH")
    else:
        for name, n in repo.table_counts().items():
            print(f"{name:24} {n}")
    return EXIT_OK


if __name__ == "__main__":
    run_cli(main)
