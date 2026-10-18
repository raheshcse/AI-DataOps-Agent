"""Deterministic pipeline simulations.

Each pipeline reads and writes through a ``SimRepository`` and returns an
``Outcome`` describing what *actually happened*. Scenario behaviour is limited to
reading the set of active scenario ids; there is no random failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

from app.simulation.events import failure_steps, ingestion_steps, reporting_steps, step
from app.simulation.ports import SimRepository
from app.simulation.scenarios import SCENARIOS
from app.simulation.schema_contract import DRIFTED_FEED_TABLE, FEED_TABLE
from app.simulation.validators import (
    RULE_ERROR_CODES,
    RuleInputs,
    ValidationResult,
    evaluate_rules,
    run_status_for,
)

SCN_FAIL = SCENARIOS["pipeline-failure"].scenario_id
SCN_INCOMPLETE = SCENARIOS["incomplete-processing"].scenario_id
SCN_SCHEMA = SCENARIOS["schema-mismatch"].scenario_id
PARTIAL_LOAD_FRACTION = 0.6


@dataclass
class Outcome:
    status: str
    expected: int | None
    processed: int | None
    error_code: str | None = None
    error_message: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    validations: list[ValidationResult] = field(default_factory=list)
    duration_ms: int | None = None


@dataclass
class Context:
    repo: SimRepository
    batch_date: date
    now: datetime
    active: set[str]
    freshness_threshold_minutes: int


def default_duration_ms(pipeline_id: str, processed: int | None) -> int:
    n = processed or 0
    return {"order_ingestion": 1500 + 12 * n, "order_validation": 2000 + 2 * n,
            "reporting_prep": 1000 + 5 * n}.get(pipeline_id, 1000)


def feed_to_order(feed_row: dict[str, Any]) -> dict[str, Any]:
    items = []
    for line_no, raw in enumerate(json.loads(feed_row["items_json"]), start=1):
        price = Decimal(raw["unit_price"])
        items.append({"line_no": line_no, "product_id": raw["product_id"], "quantity": raw["quantity"],
                      "unit_price": price, "line_total": price * raw["quantity"]})
    return {"customer_id": feed_row["customer_id"], "source_event_id": feed_row["source_event_id"],
            "batch_date": feed_row["batch_date"], "order_ts": feed_row["order_ts"], "status": "placed",
            "total_amount": feed_row["order_total"], "currency": feed_row["currency"],
            "scenario_id": None, "items": items}


def run_order_ingestion(ctx: Context) -> Outcome:
    feed = ctx.repo.get_feed_rows(ctx.batch_date)
    expected = len(feed)
    if SCN_FAIL in ctx.active:
        msg = "Source feed connection timed out after 30s (simulated scenario)"
        return Outcome("failed", expected, 0, "E1001", msg,
                       failure_steps("source_connector", "feed_connection_timeout", "E1001", msg),
                       duration_ms=30000)
    if expected == 0:
        msg = f"No feed rows found for batch {ctx.batch_date}"
        return Outcome("failed", 0, 0, "E1003", msg,
                       failure_steps("feed_reader", "feed_empty", "E1003", msg))

    rows = feed[: int(expected * PARTIAL_LOAD_FRACTION)] if SCN_INCOMPLETE in ctx.active else feed
    present = ctx.repo.known_event_ids([r["source_event_id"] for r in rows])
    to_load = [feed_to_order(r) for r in rows if r["source_event_id"] not in present]
    inserted = ctx.repo.insert_orders(to_load)
    processed, skipped = len(rows), len(rows) - len(to_load)
    steps = ingestion_steps(ctx.batch_date, expected, processed, inserted, skipped)
    if processed < expected:
        msg = f"Processed {processed} of {expected} expected records (partial load)"
        steps.append(step("WARNING", "orders_loader", "incomplete_load", msg,
                          {"records_expected": expected, "records_processed": processed}, "E1004"))
        return Outcome("warning", expected, processed, "E1004", msg, steps)
    return Outcome("success", expected, processed, steps=steps)


def run_order_validation(ctx: Context) -> Outcome:
    feed_ids = [r["source_event_id"] for r in ctx.repo.get_feed_rows(ctx.batch_date)]
    table = DRIFTED_FEED_TABLE if SCN_SCHEMA in ctx.active else FEED_TABLE
    last = ctx.repo.latest_successful_run("order_ingestion")
    results = evaluate_rules(RuleInputs(
        batch_date=ctx.batch_date, feed_event_ids=feed_ids,
        loaded_event_ids=ctx.repo.known_event_ids(feed_ids),
        duplicates=ctx.repo.duplicate_events(),
        total_mismatches=ctx.repo.order_total_mismatches(),
        future_orders=ctx.repo.future_dated_orders(ctx.now),
        observed_schema=ctx.repo.observed_schema(table), schema_table=table,
        last_ingestion_finished=last["finished_at"] if last else None, now=ctx.now,
        freshness_threshold_minutes=ctx.freshness_threshold_minutes))
    status = run_status_for(results)
    total_orders = ctx.repo.table_counts()["orders"]
    code = message = None
    if status != "success":
        wanted = "fail" if status == "failed" else "warn"
        hits = [r for r in results if r.outcome == wanted]
        code = RULE_ERROR_CODES[hits[0].rule_name]
        label = "Validation failed" if status == "failed" else "Validation completed with warnings"
        message = f"{label}: " + ", ".join(r.rule_name for r in hits)
    return Outcome(status, total_orders, total_orders, code, message, [], results)


def run_reporting_prep(ctx: Context) -> Outcome:
    expected = len(ctx.repo.get_feed_rows(ctx.batch_date))
    summary = ctx.repo.order_summary(ctx.batch_date)
    count = summary["order_count"]
    if count == 0:
        msg = f"No ingested orders found for batch {ctx.batch_date}; run order_ingestion first"
        return Outcome("failed", expected, 0, "E3001", msg,
                       failure_steps("report_builder", "no_source_data", "E3001", msg))
    metrics = {"order_count": count, "revenue": str(summary["revenue"]),
               "distinct_customers": summary["distinct_customers"]}
    steps = reporting_steps(ctx.batch_date, metrics)
    if count < expected:
        msg = f"Report built from an incomplete batch: {count} of {expected} orders present"
        steps.append(step("WARNING", "report_builder", "incomplete_source", msg, metrics, "E3002"))
        return Outcome("warning", expected, count, "E3002", msg, steps)
    return Outcome("success", expected, count, steps=steps)


PIPELINE_FUNCTIONS: dict[str, Callable[[Context], Outcome]] = {
    "order_ingestion": run_order_ingestion,
    "order_validation": run_order_validation,
    "reporting_prep": run_reporting_prep,
}
