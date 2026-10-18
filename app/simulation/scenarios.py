"""The six controlled failure scenarios: definitions and pure data builders.

Activation is always explicit (``scripts.scenario``). Activation never touches the
baseline schema, and rows it injects carry ``scenario_id`` so they are
distinguishable from baseline data and removable without affecting anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Scenario:
    key: str
    scenario_id: str
    title: str
    mechanism: str
    how_to_observe: str


SCENARIOS: dict[str, Scenario] = {s.key: s for s in [
    Scenario("pipeline-failure", "SCN-01-PIPELINE-FAILURE", "Pipeline failure",
             "While active, every order_ingestion run fails with E1001 (feed connection timeout).",
             "Run order_ingestion: failed run, error E1001, ERROR log, open incident."),
    Scenario("data-quality", "SCN-02-DATA-QUALITY", "Data-quality failure",
             "Injects 4 tagged orders: 3 whose total differs from their line items, 1 timestamped in the future.",
             "Run order_validation: rule order_total_matches_line_items fails (E2003) with order references; "
             "order_timestamp_not_in_future warns."),
    Scenario("duplicate-records", "SCN-03-DUPLICATE-RECORDS", "Duplicate records",
             "Injects 3 tagged orders that reuse the source_event_id of existing orders.",
             "Run order_validation: rule no_duplicate_source_events fails (E2002); or query orders grouped "
             "by source_event_id."),
    Scenario("stale-data", "SCN-04-STALE-DATA", "Stale data",
             "Advances the simulated clock by 30 hours without a new successful ingestion.",
             "Run order_validation (or scripts.show freshness): data_freshness fails (E2006), age exceeds "
             "the threshold."),
    Scenario("incomplete-processing", "SCN-05-INCOMPLETE-PROCESSING", "Incomplete processing",
             "While active, order_ingestion processes only 60% of the feed rows.",
             "Run order_ingestion on a pending batch: warning run E1004 (e.g. 18 of 30). Then order_validation: "
             "batch_completeness fails (E2001)."),
    Scenario("schema-mismatch", "SCN-06-SCHEMA-MISMATCH", "Schema mismatch",
             "While active, order_validation checks the drifted fixture table fx_order_feed_drifted "
             "instead of the real feed table.",
             "Run order_validation: feed_schema_contract fails (E2005): missing currency, unexpected "
             "legacy_flag, order_total type mismatch."),
]}
BY_ID = {s.scenario_id: s for s in SCENARIOS.values()}

# Which scenario causes which failure. An incident is attributed to exactly ONE scenario
# (``incident_records.scenario_id`` is single-valued), and only if that scenario is active.
# Runs record the full set of active scenarios separately (``pipeline_runs.scenario_id``).
FAILURE_CODE_TO_SCENARIO: dict[str, str] = {
    "E1001": SCENARIOS["pipeline-failure"].scenario_id,
    "E2001": SCENARIOS["incomplete-processing"].scenario_id,
    "E2002": SCENARIOS["duplicate-records"].scenario_id,
    "E2003": SCENARIOS["data-quality"].scenario_id,
    "E2004": SCENARIOS["data-quality"].scenario_id,
    "E2005": SCENARIOS["schema-mismatch"].scenario_id,
    "E2006": SCENARIOS["stale-data"].scenario_id,
}


def scenario_for_failure(error_code: str | None, active: set[str]) -> str | None:
    """The single scenario that explains ``error_code``, or None if it is not active/known."""
    scenario_id = FAILURE_CODE_TO_SCENARIO.get(error_code or "")
    return scenario_id if scenario_id in active else None


STALE_ADVANCE = timedelta(hours=30)
LAST_HISTORY_BATCH = date(2026, 1, 13)


def _copy_items(order: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"line_no": i["line_no"], "product_id": i["product_id"], "quantity": i["quantity"],
             "unit_price": i["unit_price"], "line_total": i["line_total"]} for i in order["items"]]


def build_bad_orders(templates: list[dict[str, Any]], now: datetime, scenario_id: str) -> list[dict[str, Any]]:
    """3 orders with a wrong total + 1 order dated in the future (deterministic)."""
    if len(templates) < 4:
        raise ValueError("Need at least 4 baseline orders to build the data-quality scenario.")
    out = []
    for n, t in enumerate(templates[:4], start=1):
        items = _copy_items(t)
        items_total = sum((i["line_total"] for i in items), Decimal("0.00"))
        bad_total = n <= 3
        out.append({
            "customer_id": t["customer_id"], "source_event_id": f"SCN2-{n:04d}",
            "batch_date": LAST_HISTORY_BATCH,
            "order_ts": t["order_ts"] if bad_total else now + timedelta(days=3),
            "status": "paid", "currency": t["currency"], "scenario_id": scenario_id, "items": items,
            "total_amount": items_total + Decimal("10.00") if bad_total else items_total,
        })
    return out


def build_duplicate_orders(templates: list[dict[str, Any]], scenario_id: str) -> list[dict[str, Any]]:
    """3 copies of existing orders: same source_event_id, new order id (assigned on insert)."""
    if len(templates) < 3:
        raise ValueError("Need at least 3 baseline orders to build the duplicate scenario.")
    return [{
        "customer_id": t["customer_id"], "source_event_id": t["source_event_id"],
        "batch_date": t["batch_date"], "order_ts": t["order_ts"], "status": t["status"],
        "total_amount": t["total_amount"], "currency": t["currency"],
        "scenario_id": scenario_id, "items": _copy_items(t),
    } for t in templates[:3]]
