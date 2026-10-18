"""Tool 6 - record-count reconciliation for one feed batch.

Compares, for a batch date: feed rows -> run metadata (expected / processed) -> log counters
(inserted / skipped) -> orders present. Existing tables and run metadata only; nothing new is
stored. Events skipped because they were already loaded are idempotent by design and are
reported as information, not as a processing failure.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.diagnostics.models import (
    STATUS_ISSUE,
    STATUS_OK,
    STATUS_WARNING,
    DiagnosticResult,
    InvalidInput,
    bounded,
)
from app.diagnostics.tools._common import LogTooLarge, check_pipeline_id, parse_log_file, run_view
from app.simulation.runlog import RunLogStore

NAME = "reconcile_record_counts"


def _cmp(name: str, description: str, left_label: str, left: Any, right_label: str, right: Any) -> dict[str, Any]:
    if left is None or right is None:
        outcome, diff = "not_available", None
    else:
        diff = left - right
        outcome = "match" if diff == 0 else "mismatch"
    return {"type": "comparison", "name": name, "description": description,
            "left": {"label": left_label, "value": left}, "right": {"label": right_label, "value": right},
            "difference": diff, "outcome": outcome}


def _log_counters(logs: RunLogStore, run_id: str) -> dict[str, Any] | None:
    try:
        path = logs.locate(run_id)
        if path is None:
            return None
        events, _ = parse_log_file(path)
    except (LogTooLarge, ValueError, OSError):
        return None
    loaded = next((e for e in events if e["event"] == "orders_loaded" and e["data"]), None)
    return loaded["data"] if loaded else None


def reconcile_record_counts(repo, logs: RunLogStore, state: dict[str, Any], batch_date: date | str | None = None,
                            pipeline_id: str = "order_ingestion") -> DiagnosticResult:
    check_pipeline_id(pipeline_id)
    if batch_date is None:
        batch = state["current_batch_date"]
    elif isinstance(batch_date, datetime):
        raise InvalidInput("batch_date must be a date such as 2026-01-14, not a date-time.")
    elif isinstance(batch_date, date):
        batch = batch_date
    else:
        try:
            batch = date.fromisoformat(str(batch_date))
        except ValueError:
            raise InvalidInput("batch_date must be an ISO date such as 2026-01-14.") from None
    params = {"batch_date": batch, "pipeline_id": pipeline_id}

    feed = repo.get_feed_rows(batch)
    if not feed:
        return DiagnosticResult(
            NAME, STATUS_WARNING, "low", f"No feed rows exist for batch {batch}; there is nothing to reconcile.",
            [{"type": "feed", "batch_date": batch, "rows": 0}], [], {"feed_rows": 0}, None, None,
            "Check the batch date (the current batch is " + str(state["current_batch_date"]) + ").", params)

    ids = [r["source_event_id"] for r in feed]
    distinct = set(ids)
    present = repo.known_event_ids(ids)
    missing = sorted(distinct - present)
    orders_in_batch = repo.order_summary(batch)["order_count"]
    runs = [r for r in repo.list_runs(pipeline_id, 100) if r["batch_date"] == batch]
    latest = runs[0] if runs else None
    counters = _log_counters(logs, latest["run_id"]) if latest else None

    comps: list[dict[str, Any]] = []
    if latest:
        comps.append(_cmp("feed_rows_vs_run_expected",
                          "Feed rows now present for the batch vs the records_expected the latest run recorded.",
                          "feed rows", len(feed), "run records_expected", latest["records_expected"]))
        comps.append(_cmp("run_expected_vs_processed",
                          "Records the run expected vs records it processed (a shortfall means a partial load).",
                          "records_expected", latest["records_expected"], "records_processed",
                          latest["records_processed"]))
        if counters and "inserted" in counters:
            comps.append(_cmp("processed_vs_inserted_plus_skipped",
                              "Processed rows must equal inserted + skipped (skipped = already present).",
                              "records_processed (log)", counters.get("records_processed"),
                              "inserted + skipped (log)", counters["inserted"] + counters.get("skipped_existing", 0)))
        else:
            comps.append({"type": "comparison", "name": "processed_vs_inserted_plus_skipped",
                          "description": "Needs the run log counters.", "outcome": "not_available",
                          "left": None, "right": None, "difference": None})
    comps.append(_cmp("feed_events_vs_orders_present",
                      "Distinct feed events vs those found in orders (missing events were never loaded).",
                      "distinct feed events", len(distinct), "present in orders", len(present)))
    comps.append(_cmp("orders_in_batch_vs_feed_events",
                      "Orders dated to this batch vs distinct feed events (more orders suggests duplicates).",
                      "orders for batch", orders_in_batch, "distinct feed events", len(distinct)))
    comps.append({"type": "comparison", "name": "rejected_records", "outcome": "not_available",
                  "description": "These pipelines have no reject stage, so rejected counts are not recorded; "
                                 "read, processed, inserted and skipped are compared instead.",
                  "left": None, "right": None, "difference": None})

    by_name = {c["name"]: c for c in comps}
    hard = [n for n in ("run_expected_vs_processed", "processed_vs_inserted_plus_skipped",
                        "feed_events_vs_orders_present") if by_name.get(n, {}).get("outcome") == "mismatch"]
    soft = [n for n in ("feed_rows_vs_run_expected", "orders_in_batch_vs_feed_events")
            if by_name.get(n, {}).get("outcome") == "mismatch"]
    skipped = counters.get("skipped_existing") if counters else None
    evidence: list[dict[str, Any]] = [
        {"type": "batch", "batch_date": batch, "feed_rows": len(feed), "distinct_feed_events": len(distinct),
         "repeated_feed_rows": len(ids) - len(distinct), "events_present_in_orders": len(present),
         "events_missing_from_orders": len(missing)},
        {"type": "ingestion_run", "latest_run": run_view(latest), "runs_for_batch": len(runs),
         "log_counters": counters}] + comps
    if skipped:
        evidence.append({"type": "idempotent_skips", "skipped_existing": skipped,
                         "note": "Skipped rows were already in orders; this is normal re-run behaviour, "
                                 "not a processing failure."})
    affected, notes = bounded([f"event:{e}" for e in missing])
    observed = {"feed_events": len(distinct), "present_in_orders": len(present), "missing_from_orders": len(missing),
                "orders_for_batch": orders_in_batch}
    expected = {"missing_from_orders": 0}
    code = latest["error_code"] if latest else None

    if latest is None and missing:
        return DiagnosticResult(
            NAME, STATUS_WARNING, "low",
            f"Batch {batch} has not been ingested: no {pipeline_id} run is recorded and {len(missing)} of "
            f"{len(distinct)} feed events are not yet in orders. This is expected before the first run.",
            evidence, affected, observed, expected, None,
            f"Run {pipeline_id} for this batch, then reconcile again.", params, notes)
    if hard:
        short = by_name["run_expected_vs_processed"]
        shortfall = f"{short['left']['value']} expected, {short['right']['value']} processed" \
            if short["outcome"] == "mismatch" else None
        return DiagnosticResult(
            NAME, STATUS_ISSUE, "high",
            f"Counts do not reconcile for batch {batch}: {len(missing)} of {len(distinct)} feed events are "
            f"missing from orders" + (f" ({shortfall})" if shortfall else "") + f". Failed comparisons: {', '.join(hard)}.",
            evidence, affected, observed, expected, code,
            "Inspect the latest ingestion run's log (inspect_pipeline_logs) to see where the load stopped. "
            "The counts show that records are missing, not why.", params, notes)
    if soft:
        return DiagnosticResult(
            NAME, STATUS_WARNING, "medium", f"Counts for batch {batch} differ: {', '.join(soft)}.",
            evidence, affected, observed, expected, code,
            "Check for duplicate orders (detect_duplicate_records) or a feed that changed after the run.",
            params, notes)
    return DiagnosticResult(
        NAME, STATUS_OK, "info",
        f"Batch {batch} reconciles: {len(distinct)} feed events, all present in orders"
        + (f" ({skipped} skipped as already loaded)" if skipped else "") + ".",
        evidence, [], observed, expected, None, None, params, notes)
