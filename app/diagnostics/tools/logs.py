"""Tool 2 - pipeline log inspection for one run (read-only, path-safe)."""

from __future__ import annotations

from typing import Any

from app.diagnostics.models import (
    DIAG_INVALID_INPUT,
    DIAG_LOG_MALFORMED,
    DIAG_LOG_MISSING,
    DIAG_LOG_TOO_LARGE,
    DIAG_UNKNOWN_RUN,
    STATUS_ISSUE,
    STATUS_OK,
    STATUS_WARNING,
    DiagnosticResult,
    InvalidInput,
    error_result,
)
from app.diagnostics.tools._common import LogTooLarge, parse_log_file
from app.simulation.runlog import RunLogStore

NAME = "inspect_pipeline_logs"
_TERMINAL = {"run_completed": "success", "run_completed_with_warnings": "warning", "run_failed": "failed"}


def inspect_pipeline_logs(repo, logs: RunLogStore, run_id: str, max_events: int = 200) -> DiagnosticResult:
    params = {"run_id": run_id, "max_events": max_events}
    if not isinstance(run_id, str):
        raise InvalidInput("run_id must be a string.")
    if isinstance(max_events, bool) or not isinstance(max_events, int) or not 1 <= max_events <= 1000:
        raise InvalidInput("max_events must be an integer between 1 and 1000.")
    try:
        path = logs.locate(run_id)  # validates the id; cannot leave the log directory
    except ValueError:
        return error_result(NAME, DIAG_INVALID_INPUT,
                            "run_id may only contain letters, digits, '.', '_' and '-' (max 81 characters).", params)

    run = repo.get_run(run_id)
    if run is None and path is None:
        return error_result(NAME, DIAG_UNKNOWN_RUN, f"No run or log found for run id {run_id}.", params,
                            next_step="List runs with get_pipeline_status to find a valid run id.")
    if path is None:
        return DiagnosticResult(
            NAME, STATUS_WARNING, "medium", f"Run {run_id} exists but has no log file.",
            [{"type": "run", "run_id": run_id, "status": run["status"]}], [f"run:{run_id}"],
            "no log file", "a JSON Lines log for the run", DIAG_LOG_MISSING,
            "Check whether log files were cleared (a baseline reset removes them) or never written.", params)

    log_ref = f"pipeline_runs/{path.parent.name}/{path.name}"
    try:
        events, malformed = parse_log_file(path)
    except LogTooLarge:
        return error_result(NAME, DIAG_LOG_TOO_LARGE, "The log file is too large to inspect safely.", params)

    errors = [e for e in events if e["severity"] in ("ERROR", "CRITICAL")]
    warnings = [e for e in events if e["severity"] == "WARNING"]
    shown = events[:max_events]
    counts = {"events": len(events), "errors": len(errors), "warnings": len(warnings),
              "malformed_lines": len(malformed)}
    evidence: list[dict[str, Any]] = [{"type": "log_summary", "log_ref": log_ref, "counts": counts}]
    evidence += [{"type": "highlight", "severity": e["severity"], "line": e["line"], "event": e["event"],
                  "component": e["component"], "message": e["message"], "error_code": e["error_code"]}
                 for e in errors + warnings]
    evidence.append({"type": "events", "returned": len(shown), "events": shown})
    if malformed:
        evidence.append({"type": "malformed_lines", "lines": malformed})
    notes = [f"Showing {max_events} of {len(events)} events."] if len(events) > max_events else []

    terminal = next((e for e in reversed(events) if e["event"] in _TERMINAL), None)
    mismatch = bool(run and terminal and _TERMINAL[terminal["event"]] != run["status"])
    if mismatch:
        evidence.append({"type": "log_run_mismatch", "run_status": run["status"],
                         "log_final_event": terminal["event"], "line": terminal["line"]})

    code = next((e["error_code"] for e in errors + warnings if e["error_code"]), None)
    affected = [f"run:{run_id}"]
    if errors:
        return DiagnosticResult(
            NAME, STATUS_ISSUE, "high", f"Log for {run_id} contains {len(errors)} error event(s): "
            + "; ".join(f"{e['component']}/{e['event']}" for e in errors[:3]), evidence, affected,
            counts, {"errors": 0}, code,
            "Review the highlighted ERROR events and the surrounding events in order.", params, notes)
    empty = not events and not malformed
    if warnings or malformed or mismatch or empty:
        why = []
        if empty:
            why.append("no events (the log file is empty)")
        if warnings:
            why.append(f"{len(warnings)} warning event(s)")
        if malformed:
            why.append(f"{len(malformed)} malformed line(s)")
        if mismatch:
            why.append("final log event disagrees with the recorded run status")
        return DiagnosticResult(
            NAME, STATUS_WARNING, "medium" if (warnings or mismatch or empty) else "low",
            f"Log for {run_id} has " + ", ".join(why) + ".", evidence, affected, counts,
            {"errors": 0, "warnings": 0, "malformed_lines": 0},
            code or (DIAG_LOG_MALFORMED if malformed and not warnings else None),
            "Review the highlighted entries; malformed lines are listed so no evidence is hidden.",
            params, notes)
    return DiagnosticResult(NAME, STATUS_OK, "info", f"Log for {run_id} has {len(events)} events and no "
                            "errors or warnings.", evidence, affected, counts, {"errors": 0, "warnings": 0},
                            None, None, params, notes)
