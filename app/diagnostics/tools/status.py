"""Tool 1 - pipeline status: recent run history and the latest run per pipeline."""

from __future__ import annotations

from typing import Any

from app.db.schema import RUN_STATUSES
from app.diagnostics.models import (
    DIAG_UNKNOWN_PIPELINE,
    STATUS_ISSUE,
    STATUS_OK,
    STATUS_WARNING,
    DiagnosticResult,
    InvalidInput,
    error_result,
)
from app.diagnostics.tools._common import check_pipeline_id, run_view

NAME = "get_pipeline_status"


def get_pipeline_status(repo, pipeline_id: str | None = None, status: str | None = None,
                        limit: int = 10) -> DiagnosticResult:
    params = {"pipeline_id": pipeline_id, "status": status, "limit": limit}
    if pipeline_id is not None:
        check_pipeline_id(pipeline_id)
    if status is not None and status not in RUN_STATUSES:
        raise InvalidInput(f"status must be one of: {', '.join(RUN_STATUSES)}.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise InvalidInput("limit must be an integer between 1 and 100.")

    known = [p["pipeline_id"] for p in repo.list_pipelines()]
    if pipeline_id is not None and pipeline_id not in known:
        return error_result(NAME, DIAG_UNKNOWN_PIPELINE, f"Unknown pipeline: {pipeline_id}", params,
                            evidence=[{"type": "known_pipelines", "pipeline_ids": known}],
                            next_step="Use one of the known pipeline ids listed in the evidence.")

    targets = [pipeline_id] if pipeline_id else known
    latest_by = {pid: (repo.list_runs(pid, 1) or [None])[0] for pid in targets}
    history = repo.list_runs(pipeline_id, limit, status)

    evidence: list[dict[str, Any]] = [
        {"type": "latest_runs", "by_pipeline": {pid: run_view(r) for pid, r in latest_by.items()}},
        {"type": "run_history", "filters": {"pipeline_id": pipeline_id, "status": status},
         "returned": len(history), "runs": [run_view(r) for r in history]},
    ]
    observed = {pid: (r["status"] if r else "no_runs") for pid, r in latest_by.items()}
    expected = {pid: "success" for pid in targets}
    parts = [f"{pid}: " + (f"latest run {r['run_id']} {r['status']}" + (f" ({r['error_code']})" if r["error_code"] else "")
                           if r else "no runs recorded") for pid, r in latest_by.items()]
    summary = "; ".join(parts)

    failed = [r for r in latest_by.values() if r and r["status"] == "failed"]
    other = [r for r in latest_by.values() if r and r["status"] in ("warning", "running")]
    if failed:
        first = failed[0]
        return DiagnosticResult(
            NAME, STATUS_ISSUE, "high", summary, evidence, [f"run:{r['run_id']}" for r in failed + other],
            observed, expected, first["error_code"],
            f"Inspect the log of run {first['run_id']} with inspect_pipeline_logs and its validation results.",
            params)
    if other:
        return DiagnosticResult(NAME, STATUS_WARNING, "medium", summary, evidence,
                                [f"run:{r['run_id']}" for r in other], observed, expected,
                                other[0]["error_code"],
                                f"Inspect the log of run {other[0]['run_id']} with inspect_pipeline_logs.", params)
    if not any(latest_by.values()):
        return DiagnosticResult(NAME, STATUS_WARNING, "low", "No pipeline runs are recorded.", evidence, [],
                                observed, expected, None,
                                "Run a pipeline (scripts.run_pipeline) to create history.", params)
    return DiagnosticResult(NAME, STATUS_OK, "info", summary, evidence, [], observed, expected, None, None, params)
