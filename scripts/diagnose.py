"""Run the read-only diagnostic tools and print their structured result as JSON.

    python -m scripts.diagnose status [--pipeline order_ingestion] [--status failed] [--limit 10]
    python -m scripts.diagnose logs RUN_ID [--max-events 200]
    python -m scripts.diagnose duplicates
    python -m scripts.diagnose quality
    python -m scripts.diagnose freshness [--pipeline order_ingestion]
    python -m scripts.diagnose reconcile [--batch-date 2026-01-14]
    python -m scripts.diagnose schema [--table stg_order_feed|fx_order_feed_drifted]
    python -m scripts.diagnose all [--batch-date 2026-01-14]

Add --brief (before or after the tool name) for one line per result. Nothing is ever modified.
In JSON mode the JSON goes to stdout and the connection target to stderr, so
`... | ConvertFrom-Json` works; in --brief mode the target line is printed on stdout too, so
nothing is written to stderr (PowerShell 5.1 shows stderr as an error with 2>&1).
Exit codes: 0 ok/warning, 1 issue detected, 2 the diagnostic could not run.
"""

from __future__ import annotations

import argparse
import json
import sys

from app.db.connection import make_engine
from app.db.repository import SqlRepository
from app.diagnostics import DiagnosticsService
from app.diagnostics.models import DiagnosticReport, DiagnosticResult
from app.simulation.runlog import RunLogStore
from scripts._common import EXIT_CHECK_FAILED, EXIT_ENV, EXIT_OK, load_settings, log_dir, print_target, run_cli


def build_service(brief: bool = False) -> DiagnosticsService:
    settings = load_settings()
    if brief:
        print_target(settings)
    else:
        print_target_stderr(settings)
    engine = make_engine(settings)  # the normal (non-admin) connection; diagnostics only read
    return DiagnosticsService(SqlRepository(engine), RunLogStore(log_dir(settings)),
                              settings.freshness_threshold_minutes)


def print_target_stderr(settings) -> None:
    stdout, sys.stdout = sys.stdout, sys.stderr
    try:
        print_target(settings)
    finally:
        sys.stdout = stdout


def exit_code(results: list[DiagnosticResult]) -> int:
    if any(r.status == "error" for r in results):
        return EXIT_ENV
    return EXIT_CHECK_FAILED if any(r.status == "issue_detected" for r in results) else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--brief", action="store_true", help="one line per result instead of JSON")
    sub = parser.add_subparsers(dest="tool", required=True)

    def add(name: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name)
        # SUPPRESS keeps `--brief all` working: the subparser must not overwrite the parent's value.
        sp.add_argument("--brief", action="store_true", default=argparse.SUPPRESS,
                        help="one line per result instead of JSON")
        return sp

    p = add("status")
    p.add_argument("--pipeline")
    p.add_argument("--status", dest="status_filter")
    p.add_argument("--limit", type=int, default=10)
    p = add("logs")
    p.add_argument("run_id")
    p.add_argument("--max-events", type=int, default=200)
    add("duplicates")
    add("quality")
    add("freshness").add_argument("--pipeline", default="order_ingestion")
    add("reconcile").add_argument("--batch-date")
    add("schema").add_argument("--table")
    add("all").add_argument("--batch-date")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    service = build_service(args.brief)
    if args.tool == "all":
        report: DiagnosticReport = service.run_all(args.batch_date)
        results, payload = report.results, report.to_dict()
    else:
        result = {
            "status": lambda: service.get_pipeline_status(args.pipeline, args.status_filter, args.limit),
            "logs": lambda: service.inspect_pipeline_logs(args.run_id, args.max_events),
            "duplicates": service.detect_duplicate_records,
            "quality": service.diagnose_data_quality,
            "freshness": lambda: service.check_data_freshness(args.pipeline),
            "reconcile": lambda: service.reconcile_record_counts(args.batch_date),
            "schema": lambda: service.validate_schema_contract(args.table),
        }[args.tool]()
        results, payload = [result], result.to_dict()

    if args.brief:
        for r in results:
            print(f"[{r.status}/{r.severity}] {r.diagnostic_name}: {r.summary}")
    else:
        print(json.dumps(payload, indent=2))
    return exit_code(results)


if __name__ == "__main__":
    run_cli(main)
