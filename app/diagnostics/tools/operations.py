"""Phase 10 operations tools: run statistics, recent-error roll-up and diagnostics-service health.

All three are read-only, use the same ``ReadOnlyRepo`` as the Phase 4 tools and never state a root cause:
they count and summarise what the simulation database recorded.
"""

from __future__ import annotations

import statistics
from typing import Any

from app.diagnostics.models import (
    DIAG_UNKNOWN_PIPELINE,
    STATUS_ISSUE,
    STATUS_OK,
    STATUS_WARNING,
    DiagnosticResult,
    InvalidInput,
    error_result,
)
from app.diagnostics.tools._common import _text, check_pipeline_id, run_view

STATS_NAME = "get_pipeline_run_statistics"
ERRORS_NAME = "summarize_recent_errors"
HEALTH_NAME = "check_diagnostics_health"

LOW_SUCCESS_RATE = 0.8
DURATION_REGRESSION_FACTOR = 2.0


def _bounded_int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidInput(f"{name} must be an integer between {low} and {high}.")
    return value


def _pipeline_stats(pid: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """``runs`` newest first. Only recorded values are used; absent durations are not treated as zero."""
    counts = {"success": 0, "warning": 0, "failed": 0, "running": 0}
    for r in runs:
        if r["status"] in counts:
            counts[r["status"]] += 1
    finished = counts["success"] + counts["warning"] + counts["failed"]
    streak = 0
    for r in runs:
        if r["status"] == "failed":
            streak += 1
        elif r["status"] == "running":
            continue
        else:
            break
    durations = [r["duration_ms"] for r in runs if isinstance(r.get("duration_ms"), (int, float))]
    out: dict[str, Any] = {
        "pipeline_id": pid, "runs_examined": len(runs), "status_counts": counts,
        "success_rate": round(counts["success"] / finished, 4) if finished else None,
        "consecutive_failures": streak,
        "duration_ms": None, "duration_regression": False,
    }
    if durations:
        out["duration_ms"] = {
            "samples": len(durations), "last": durations[0], "mean": round(statistics.fmean(durations), 1),
            "median": statistics.median(durations), "max": max(durations),
        }
        baseline = durations[1:]
        if len(baseline) >= 3:
            median = statistics.median(baseline)
            out["duration_regression"] = bool(median > 0 and durations[0] > DURATION_REGRESSION_FACTOR * median)
    return out


def get_pipeline_run_statistics(repo, pipeline_id: str | None = None, window: int = 20) -> DiagnosticResult:
    params = {"pipeline_id": pipeline_id, "window": window}
    if pipeline_id is not None:
        check_pipeline_id(pipeline_id)
    _bounded_int(window, "window", 1, 100)
    known = [p["pipeline_id"] for p in repo.list_pipelines()]
    if pipeline_id is not None and pipeline_id not in known:
        return error_result(STATS_NAME, DIAG_UNKNOWN_PIPELINE, f"Unknown pipeline: {pipeline_id}", params,
                            evidence=[{"type": "known_pipelines", "pipeline_ids": known}],
                            next_step="Use one of the known pipeline ids listed in the evidence.")
    targets = [pipeline_id] if pipeline_id else known
    stats = [_pipeline_stats(pid, [run_view(r) for r in repo.list_runs(pid, window)]) for pid in targets]
    evidence: list[dict[str, Any]] = [{"type": "pipeline_statistics", "window": window, "pipelines": stats}]
    issues = [s for s in stats if s["consecutive_failures"] >= 2]
    warns = [s for s in stats if s not in issues and (
        s["consecutive_failures"] == 1 or s["duration_regression"]
        or (s["success_rate"] is not None and s["success_rate"] < LOW_SUCCESS_RATE))]
    observed = {s["pipeline_id"]: {"success_rate": s["success_rate"],
                                   "consecutive_failures": s["consecutive_failures"]} for s in stats}
    expected = {"success_rate": f">= {LOW_SUCCESS_RATE}", "consecutive_failures": 0}
    summary = "; ".join(
        f"{s['pipeline_id']}: {s['runs_examined']} runs, success rate "
        + ("n/a" if s["success_rate"] is None else f"{s['success_rate']:.0%}")
        + f", failure streak {s['consecutive_failures']}" for s in stats) or "No pipelines are defined."
    if issues:
        return DiagnosticResult(STATS_NAME, STATUS_ISSUE, "high", summary, evidence,
                                [f"pipeline:{s['pipeline_id']}" for s in issues], observed, expected, None,
                                "Inspect the latest failed run with get_pipeline_status and inspect_pipeline_logs.", params)
    if warns:
        return DiagnosticResult(STATS_NAME, STATUS_WARNING, "medium", summary, evidence,
                                [f"pipeline:{s['pipeline_id']}" for s in warns], observed, expected, None,
                                "Review the recent runs with get_pipeline_status.", params)
    if not any(s["runs_examined"] for s in stats):
        return DiagnosticResult(STATS_NAME, STATUS_WARNING, "low", "No pipeline runs are recorded.", evidence, [],
                                observed, expected, None, "Run a pipeline (scripts.run_pipeline) to create history.",
                                params)
    return DiagnosticResult(STATS_NAME, STATUS_OK, "info", summary, evidence, [], observed, expected, None, None, params)


def summarize_recent_errors(repo, limit: int = 20) -> DiagnosticResult:
    params = {"limit": limit}
    _bounded_int(limit, "limit", 1, 100)
    runs = [run_view(r) for st in ("failed", "warning") for r in repo.list_runs(None, limit, st)]
    runs.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    runs = runs[:limit]
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in runs:
        key = (str(r["pipeline_id"]), str(r["error_code"] or "unclassified"))
        g = groups.setdefault(key, {"pipeline_id": key[0], "error_code": key[1], "count": 0,
                                    "statuses": {}, "run_ids": [], "sample_message": None})
        g["count"] += 1
        g["statuses"][r["status"]] = g["statuses"].get(r["status"], 0) + 1
        g["run_ids"].append(r["run_id"])
        if g["sample_message"] is None and r.get("error_message"):
            g["sample_message"] = _text(r["error_message"], 200)
    grouped = sorted(groups.values(), key=lambda g: -g["count"])
    for g in grouped:
        g["run_ids"] = g["run_ids"][:10]
    evidence = [{"type": "error_groups", "runs_considered": len(runs), "groups": grouped}]
    if not runs:
        return DiagnosticResult(ERRORS_NAME, STATUS_OK, "info", "No failed or warning runs are recorded.",
                                evidence, [], 0, 0, None, None, params)
    failed = sum(1 for r in runs if r["status"] == "failed")
    summary = (f"{len(runs)} failed/warning run(s) in {len(grouped)} group(s); "
               f"most frequent: {grouped[0]['pipeline_id']} / {grouped[0]['error_code']} x{grouped[0]['count']}.")
    status, sev = (STATUS_ISSUE, "high") if failed else (STATUS_WARNING, "medium")
    return DiagnosticResult(ERRORS_NAME, status, sev, summary, evidence, [f"run:{r['run_id']}" for r in runs],
                            len(runs), 0, grouped[0]["error_code"],
                            "Inspect a run from the largest group with inspect_pipeline_logs.", params)


def check_diagnostics_health(repo, sim_now: Any = None) -> DiagnosticResult:
    """Reachability and integrity of the data the other tools read. Reaching here means the database answered."""
    counts = repo.table_counts()
    problems = list(repo.integrity_problems())
    evidence: list[dict[str, Any]] = [
        {"type": "database", "reachable": True, "seeded": True,
         "sim_now": sim_now.isoformat() if hasattr(sim_now, "isoformat") else sim_now},
        {"type": "table_counts", "counts": {k: int(v) for k, v in counts.items()}},
        {"type": "integrity_problems", "count": len(problems), "problems": [_text(p, 200) for p in problems[:20]]},
    ]
    if problems:
        return DiagnosticResult(HEALTH_NAME, STATUS_ISSUE, "medium",
                                f"The simulation database reports {len(problems)} integrity problem(s).", evidence,
                                [], len(problems), 0, None,
                                "Run python -m scripts.db_verify on the machine hosting the database.", {})
    return DiagnosticResult(HEALTH_NAME, STATUS_OK, "info", "The simulation database is reachable, seeded and consistent.",
                            evidence, [], 0, 0, None, None, {})
