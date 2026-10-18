"""Tool 5 - data freshness: age of the latest successful ingestion vs the threshold.

The reference time is the simulated clock (passed in by the service), never the machine
clock, so results are reproducible. Reuses ``calc_freshness`` / ``check_freshness``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.diagnostics.models import (
    DIAG_INVALID_TIMESTAMP,
    DIAG_UNKNOWN_PIPELINE,
    STATUS_ISSUE,
    STATUS_OK,
    DiagnosticResult,
    error_result,
)
from app.diagnostics.tools._common import check_pipeline_id, run_view
from app.simulation.validators import RULE_ERROR_CODES, RULE_FRESHNESS, calc_freshness, check_freshness

NAME = "check_data_freshness"
CLOCK_SOURCE = "simulated clock (sim_state.sim_now)"


def _valid(ts: Any) -> bool:
    return isinstance(ts, datetime) and ts.tzinfo is not None


def check_data_freshness(repo, now: datetime, threshold_minutes: int,
                         pipeline_id: str = "order_ingestion") -> DiagnosticResult:
    params = {"pipeline_id": pipeline_id, "threshold_minutes": threshold_minutes}
    check_pipeline_id(pipeline_id)
    if repo.get_pipeline(pipeline_id) is None:
        return error_result(NAME, DIAG_UNKNOWN_PIPELINE, f"Unknown pipeline: {pipeline_id}", params)
    expected = f"age <= {threshold_minutes} min"
    reference = {"reference_time": now, "reference_source": CLOCK_SOURCE, "threshold_minutes": threshold_minutes}

    last = repo.latest_successful_run(pipeline_id)
    if last is None:
        return DiagnosticResult(
            NAME, STATUS_ISSUE, "high", f"No successful {pipeline_id} run exists, so freshness cannot be met.",
            [dict(type="freshness", last_successful_run=None, **reference)], [], "no successful run", expected,
            RULE_ERROR_CODES[RULE_FRESHNESS],
            f"Check recent {pipeline_id} runs with get_pipeline_status to see why none succeeded.", params)

    finished = last["finished_at"]
    if not _valid(finished) or not _valid(now) or finished > now:
        reason = ("finished_at is missing or has no timezone" if not _valid(finished) else
                  "the reference time is invalid" if not _valid(now) else
                  "the last success finished after the reference time")
        return error_result(
            NAME, DIAG_INVALID_TIMESTAMP, f"Cannot compute freshness: {reason}.", params,
            evidence=[dict(type="freshness", last_successful_run=run_view(last), **reference)],
            next_step="Inspect the run record and the simulated clock for inconsistent timestamps.")

    f = calc_freshness(finished, now, threshold_minutes)
    verdict = check_freshness(finished, now, threshold_minutes)
    since = [run_view(r) for r in repo.list_runs(pipeline_id, 20) if r["started_at"] > finished]
    evidence = [dict(type="freshness", last_successful_run=run_view(last), age_minutes=f["age_minutes"],
                     minutes_over_threshold=round(max(f["age_minutes"] - threshold_minutes, 0), 1),
                     margin_minutes=round(threshold_minutes - f["age_minutes"], 1), stale=f["stale"],
                     **reference),
                {"type": "runs_since_last_success", "count": len(since), "runs": since}]
    observed = {"age_minutes": f["age_minutes"], "last_success_at": finished}
    if f["stale"]:
        return DiagnosticResult(
            NAME, STATUS_ISSUE, "high",
            f"Data is stale: last successful {pipeline_id} finished {f['age_minutes']} min before the "
            f"reference time (threshold {threshold_minutes} min). {verdict.detail}",
            evidence, [f"run:{last['run_id']}"], observed, expected, RULE_ERROR_CODES[RULE_FRESHNESS],
            "Look at runs since the last success (in the evidence) to see whether ingestion ran and failed "
            "or did not run at all. Staleness alone does not identify the cause.", params)
    return DiagnosticResult(
        NAME, STATUS_OK, "info", f"Data is fresh: {f['age_minutes']} min old against a {threshold_minutes} min "
        "threshold.", evidence, [f"run:{last['run_id']}"], observed, expected, None, None, params)
