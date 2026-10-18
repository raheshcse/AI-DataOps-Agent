"""Structured run events (the content of the JSON Lines operational logs).

A pipeline reports what it actually did as *steps*; ``build_run_events`` turns the
steps plus the final run record and validation results into timestamped events, so
the log can never claim a different outcome than the run record.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.logging import redact

PIPELINE_ABBREV = {"order_ingestion": "ING", "order_validation": "VAL", "reporting_prep": "RPT"}


def make_run_id(pipeline_id: str, started_at: datetime, existing: set[str] | None = None) -> str:
    base = f"RUN-{PIPELINE_ABBREV.get(pipeline_id, 'PIPE')}-{started_at:%Y%m%d-%H%M%S}"
    run_id, n = base, 1
    while existing and run_id in existing:
        n += 1
        run_id = f"{base}-{n}"
    return run_id


def step(level: str, component: str, event: str, message: str,
         data: dict[str, Any] | None = None, error_code: str | None = None) -> dict[str, Any]:
    return {"level": level, "component": component, "event": event, "message": message,
            "data": data or {}, "error_code": error_code}


# ---- step builders shared by the simulator and the baseline generator -------------

def ingestion_steps(batch_date: Any, expected: int, processed: int, inserted: int, skipped: int) -> list[dict]:
    return [
        step("INFO", "source_connector", "feed_connected", "Connected to order feed"),
        step("INFO", "feed_reader", "feed_read", f"Read {expected} feed rows for batch {batch_date}",
             {"records_expected": expected}),
        step("INFO", "orders_loader", "orders_loaded",
             f"Processed {processed} rows: inserted {inserted} new orders, skipped {skipped} already present",
             {"records_processed": processed, "inserted": inserted, "skipped_existing": skipped}),
    ]


def failure_steps(component: str, event: str, error_code: str, message: str) -> list[dict]:
    return [step("ERROR", component, event, message, error_code=error_code)]


def reporting_steps(batch_date: Any, metrics: dict[str, Any]) -> list[dict]:
    return [
        step("INFO", "report_builder", "orders_aggregated",
             f"Aggregated {metrics['order_count']} orders for batch {batch_date}", metrics),
        step("INFO", "report_builder", "report_prepared", "Operational reporting data prepared", metrics),
    ]


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_run_events(run: dict[str, Any], steps: list[dict[str, Any]],
                     validations: list[Any]) -> list[dict[str, Any]]:
    """Events for one run. ``validations`` are ValidationResult-like (attributes)."""
    pid, rid, scenario = run["pipeline_id"], run["run_id"], run.get("scenario_id")
    status = run["status"]

    raw: list[dict[str, Any]] = [step("INFO", "pipeline_runner", "run_started",
                                      f"Starting {pid} for batch {run['batch_date']}",
                                      {"trigger": run.get("trigger_type", "manual")})]
    raw.extend(steps)
    level_for = {"pass": "INFO", "warn": "WARNING", "fail": "ERROR"}
    for v in validations:
        raw.append(step(level_for[v.outcome], "validator", "validation_rule_evaluated",
                        f"{v.rule_name}: {v.outcome} ({v.detail or ''})".strip(),
                        {"rule": v.rule_name, "outcome": v.outcome, "expected": v.expected_value,
                         "observed": v.observed_value, "record_ref": v.record_ref}))
    if status == "success":
        raw.append(step("INFO", "pipeline_runner", "run_completed",
                        f"{pid} completed successfully", {"duration_ms": run["duration_ms"]}))
    elif status == "warning":
        raw.append(step("WARNING", "pipeline_runner", "run_completed_with_warnings",
                        run.get("error_message") or f"{pid} completed with warnings",
                        {"duration_ms": run["duration_ms"]}, run.get("error_code")))
    else:
        raw.append(step("ERROR", "pipeline_runner", "run_failed",
                        run.get("error_message") or f"{pid} failed",
                        {"duration_ms": run["duration_ms"]}, run.get("error_code")))

    started, duration = run["started_at"], run["duration_ms"] or 0
    last = max(len(raw) - 1, 1)
    events = []
    for i, r in enumerate(raw):
        ts = started + timedelta(milliseconds=round(duration * i / last))
        events.append({
            "timestamp": _iso(ts),
            "level": r["level"],
            "component": r["component"],
            "pipeline_id": pid,
            "run_id": rid,
            "event": r["event"],
            "message": redact(r["message"]),
            "error_code": r["error_code"],
            "scenario_id": scenario,
            "data": r["data"],
        })
    return events


def make_run_row(*, run_id: str, pipeline_id: str, batch_date: Any, started_at: datetime,
                 duration_ms: int, status: str, expected: int | None, processed: int | None,
                 error_code: str | None = None, error_message: str | None = None,
                 scenario_id: str | None = None, trigger_type: str = "manual") -> dict[str, Any]:
    return {
        "run_id": run_id, "pipeline_id": pipeline_id, "batch_date": batch_date,
        "started_at": started_at, "finished_at": started_at + timedelta(milliseconds=duration_ms),
        "status": status, "duration_ms": duration_ms, "records_expected": expected,
        "records_processed": processed, "error_code": error_code,
        "error_message": error_message, "scenario_id": scenario_id, "trigger_type": trigger_type,
    }


def make_incident(*, incident_id: str, category: str, severity: str, title: str, description: str,
                  status: str, created_at: datetime, run: dict[str, Any], evidence_ref: str,
                  scenario_id: str | None = None) -> dict[str, Any]:
    return {
        "incident_id": incident_id, "category": category, "severity": severity, "title": title,
        "description": description, "status": status, "created_at": created_at,
        "pipeline_id": run["pipeline_id"], "run_id": run["run_id"],
        "scenario_id": scenario_id, "evidence_ref": evidence_ref,
    }
