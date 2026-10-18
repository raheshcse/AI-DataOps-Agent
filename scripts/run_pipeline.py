"""Run one simulated pipeline and record the result.

    python -m scripts.run_pipeline --pipeline order_ingestion [--batch-date 2026-01-14]

Pipelines: order_ingestion, order_validation, reporting_prep. The batch defaults to the
pending batch stored in the simulation state (2026-01-14 in the baseline).
"""

from __future__ import annotations

import argparse
from datetime import date

from app.simulation.pipelines import PIPELINE_FUNCTIONS
from scripts._common import EXIT_OK, load_settings, log_dir, make_service, print_target, run_cli


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pipeline", required=True, choices=sorted(PIPELINE_FUNCTIONS))
    parser.add_argument("--batch-date", type=date.fromisoformat, help="YYYY-MM-DD (default: pending batch)")
    args = parser.parse_args()
    settings = load_settings()
    print_target(settings)
    result = make_service(settings).run_pipeline(args.pipeline, args.batch_date)
    run = result["run"]
    print(f"\nRun {run['run_id']}: {run['status'].upper()}  pipeline={run['pipeline_id']}  batch={run['batch_date']}")
    print(f"  started={run['started_at'].isoformat()}  finished={run['finished_at'].isoformat()}  "
          f"duration={run['duration_ms']} ms")
    print(f"  records expected={run['records_expected']}  processed={run['records_processed']}")
    if run["error_code"]:
        print(f"  error {run['error_code']}: {run['error_message']}")
    if run["scenario_id"]:
        print(f"  active scenario(s): {run['scenario_id']}")
    for v in result["validations"]:
        print(f"  [{v['outcome'].upper():4}] {v['rule_name']}: expected={v['expected_value']} observed={v['observed_value']}")
        if v["outcome"] != "pass":
            print(f"         {v['detail']}\n         refs: {v['record_ref']}")
    if result["incident"]:
        print(f"  incident {result['incident']['incident_id']} opened ({result['incident']['category']})")
    print(f"  log: {log_dir(settings) / result['log_ref']}")
    print(f"Inspect: python -m scripts.show run {run['run_id']}")
    return EXIT_OK


if __name__ == "__main__":
    run_cli(main)
