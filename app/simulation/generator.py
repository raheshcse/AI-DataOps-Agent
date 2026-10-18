"""Deterministic synthetic baseline data.

Everything is derived from a fixed seed and a fixed simulated clock, so two runs
produce byte-identical data. All values are synthetic (``example.test`` emails,
invented names); nothing here resembles real customer information.

Baseline story ("the simulated company's last week"):
* 7 history batches (2026-01-07 .. 2026-01-13) were loaded by daily runs of three
  pipelines; on 2026-01-11 the ingestion run failed once and was retried.
* The feed for batch 2026-01-14 ("yesterday's sales") is waiting, not yet loaded.
* The simulated clock stands at 2026-01-15 01:00 UTC, before the next daily run.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.simulation.clock import ANCHOR_NOW
from app.simulation.events import (
    build_run_events,
    failure_steps,
    ingestion_steps,
    make_incident,
    make_run_id,
    make_run_row,
    reporting_steps,
)
from app.simulation.schema_contract import ORDER_FEED_CONTRACT
from app.simulation.validators import RuleInputs, evaluate_rules, run_status_for

BASELINE_VERSION = "1"
CURRENT_BATCH = date(2026, 1, 14)
HISTORY_BATCHES = [date(2026, 1, 7) + timedelta(days=d) for d in range(7)]
FAILED_INGESTION_BATCH = date(2026, 1, 10)
N_CUSTOMERS, N_PRODUCTS, PENDING_BATCH_SIZE = 50, 20, 30

FIRST = ["Aroha", "Liam", "Mei", "Noah", "Priya", "Oliver", "Sofia", "Ethan", "Hana", "Lucas",
         "Isla", "Mateo", "Zara", "Jack", "Leila", "Ben", "Chloe", "Ravi", "Emma", "Kai"]
LAST = ["Walker", "Nguyen", "Patel", "Smith", "Kumar", "Taylor", "Lee", "Brown", "Singh", "Wilson",
        "Anderson", "Martin", "Clarke", "Hughes", "Reid", "Young", "Shaw", "Grant", "Hart", "Ford"]
REGIONS = ["APAC", "EMEA", "AMER", "LATAM"]
PRODUCT_CATALOGUE = [
    ("Desk Lamp", "Home Office"), ("Ergonomic Chair", "Home Office"), ("Standing Desk", "Home Office"),
    ("Monitor Arm", "Home Office"), ("Webcam HD", "Electronics"), ("USB-C Hub", "Electronics"),
    ("Wireless Mouse", "Electronics"), ("Mechanical Keyboard", "Electronics"),
    ("Noise Cancelling Headset", "Electronics"), ("Laptop Stand", "Accessories"),
    ("Cable Organiser", "Accessories"), ("Notebook Set", "Stationery"), ("Gel Pen Pack", "Stationery"),
    ("Desk Mat", "Accessories"), ("Water Bottle", "Lifestyle"), ("Travel Mug", "Lifestyle"),
    ("Backpack", "Lifestyle"), ("Phone Stand", "Accessories"), ("Whiteboard Kit", "Stationery"),
    ("Air Purifier", "Home Office"),
]
STATUSES = ["placed", "paid", "shipped", "cancelled"]
STATUS_WEIGHTS = [10, 45, 40, 5]


@dataclass
class BaselineData:
    customers: list[dict] = field(default_factory=list)
    products: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)  # each has an 'items' list
    feed_rows: list[dict] = field(default_factory=list)
    pipeline_definitions: list[dict] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    validations: list[dict] = field(default_factory=list)
    incidents: list[dict] = field(default_factory=list)
    run_events: dict[str, list[dict]] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)

    def expected_counts(self) -> dict[str, int]:
        """Row counts the verification script expects for an untouched baseline."""
        return {
            "customers": len(self.customers),
            "products": len(self.products),
            "orders": len(self.orders),
            "order_items": sum(len(o["items"]) for o in self.orders),
            "stg_order_feed": len(self.feed_rows),
            "fx_order_feed_drifted": 0,
            "pipeline_definitions": len(self.pipeline_definitions),
            "pipeline_runs": len(self.runs),
            "validation_results": len(self.validations),
            "incident_records": len(self.incidents),
            "scenario_activations": 0,
            "sim_state": 1,
        }


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _make_batch(rng: random.Random, batch: date, n: int, products: list[dict]) -> list[dict]:
    day_start = datetime.combine(batch, time(0, 0), tzinfo=timezone.utc)
    orders = []
    for seq in range(1, n + 1):
        picked = rng.sample(products, rng.randint(1, 4))
        items, total = [], Decimal("0.00")
        for line_no, product in enumerate(picked, start=1):
            qty = rng.randint(1, 5)
            line_total = _money(product["unit_price"] * qty)
            total += line_total
            items.append({"line_no": line_no, "product_id": product["product_id"], "quantity": qty,
                          "unit_price": product["unit_price"], "line_total": line_total})
        orders.append({
            "customer_id": rng.randint(1, N_CUSTOMERS),
            "source_event_id": f"EVT-{batch:%Y%m%d}-{seq:04d}",
            "batch_date": batch,
            "order_ts": day_start + timedelta(seconds=rng.randint(0, 86399)),
            "status": rng.choices(STATUSES, STATUS_WEIGHTS)[0],
            "total_amount": total,
            "currency": "NZD",
            "scenario_id": None,
            "items": items,
        })
    orders.sort(key=lambda o: o["order_ts"])
    return orders


def _feed_row(feed_id: int, order: dict) -> dict:
    return {
        "feed_id": feed_id, "batch_date": order["batch_date"],
        "source_event_id": order["source_event_id"], "customer_id": order["customer_id"],
        "order_ts": order["order_ts"], "order_total": order["total_amount"],
        "currency": order["currency"],
        "items_json": json.dumps(
            [{"product_id": i["product_id"], "quantity": i["quantity"], "unit_price": str(i["unit_price"])}
             for i in order["items"]], sort_keys=True),
        "scenario_id": None,
    }


PIPELINES = [
    {"pipeline_id": "order_ingestion", "name": "Order ingestion",
     "description": "Reads the daily order feed for a batch and loads new orders and line items.",
     "schedule": "Daily 02:00 UTC", "expected_records_per_run": 25,
     "config_json": json.dumps({"source": "stg_order_feed", "target": "orders,order_items"}, sort_keys=True)},
    {"pipeline_id": "order_validation", "name": "Order validation",
     "description": "Runs completeness, duplicate, total, timestamp, schema and freshness rules.",
     "schedule": "Daily 02:10 UTC", "expected_records_per_run": 6,
     "config_json": json.dumps({"rules": 6}, sort_keys=True)},
    {"pipeline_id": "reporting_prep", "name": "Operational reporting-data preparation",
     "description": "Aggregates a loaded batch into operational report metrics (no separate store).",
     "schedule": "Daily 02:20 UTC", "expected_records_per_run": 25,
     "config_json": json.dumps({"metrics": ["order_count", "revenue", "distinct_customers"]}, sort_keys=True)},
]


def generate_baseline(seed: int = 20260115, freshness_threshold_minutes: int = 1500) -> BaselineData:
    rng = random.Random(seed)
    data = BaselineData()

    for i in range(1, N_CUSTOMERS + 1):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        data.customers.append({
            "customer_id": i, "customer_code": f"CUST-{i:04d}", "full_name": f"{first} {last}",
            "email": f"{first.lower()}.{last.lower()}{i}@example.test", "region": rng.choice(REGIONS),
            "created_at": ANCHOR_NOW - timedelta(days=rng.randint(30, 400), seconds=rng.randint(0, 86399)),
        })
    for i, (name, category) in enumerate(PRODUCT_CATALOGUE[:N_PRODUCTS], start=1):
        data.products.append({
            "product_id": i, "sku": f"SKU-{i:04d}", "name": name, "category": category,
            "unit_price": _money(rng.randint(500, 25000) / 100), "is_active": True,
        })
    data.pipeline_definitions = [dict(p, is_active=True) for p in PIPELINES]

    batches = {b: _make_batch(rng, b, 25 + rng.randint(-3, 3), data.products) for b in HISTORY_BATCHES}
    pending = _make_batch(rng, CURRENT_BATCH, PENDING_BATCH_SIZE, data.products)

    order_id = item_id = feed_id = 0
    for b in HISTORY_BATCHES:
        for order in batches[b]:
            order_id += 1
            order["order_id"] = order_id
            for it in order["items"]:
                item_id += 1
                it["order_item_id"] = item_id
            data.orders.append(order)
    for b in HISTORY_BATCHES:
        for order in batches[b]:
            feed_id += 1
            data.feed_rows.append(_feed_row(feed_id, order))
    for order in pending:
        feed_id += 1
        data.feed_rows.append(_feed_row(feed_id, order))

    _build_history(data, batches, freshness_threshold_minutes)
    data.state = {"id": 1, "baseline_version": BASELINE_VERSION, "seed": seed,
                  "sim_now": ANCHOR_NOW, "current_batch_date": CURRENT_BATCH, "seeded_at": ANCHOR_NOW}
    return data


def _build_history(data: BaselineData, batches: dict[date, list[dict]], threshold: int) -> None:
    cumulative = 0
    incident_no = 0

    def add_run(row: dict, steps: list, validations: list) -> None:
        data.runs.append(row)
        for v in validations:
            data.validations.append(v.to_row(row["run_id"], row["finished_at"]))
        data.run_events[row["run_id"]] = build_run_events(row, steps, validations)

    for b in HISTORY_BATCHES:
        orders = batches[b]
        n = len(orders)
        day = datetime.combine(b + timedelta(days=1), time(0, 0), tzinfo=timezone.utc)
        t = day + timedelta(hours=2)

        if b == FAILED_INGESTION_BATCH:
            msg = "Source feed connection reset by peer (simulated)"
            started = t
            row = make_run_row(run_id=make_run_id("order_ingestion", started), pipeline_id="order_ingestion",
                               batch_date=b, started_at=started, duration_ms=30000, status="failed",
                               expected=n, processed=0, error_code="E1002", error_message=msg,
                               trigger_type="scheduled")
            add_run(row, failure_steps("source_connector", "feed_connection_failed", "E1002", msg), [])
            incident_no += 1
            data.incidents.append(make_incident(
                incident_id=f"INC-{incident_no:04d}", category="pipeline_failure", severity="high",
                title=f"order_ingestion failed for batch {b}",
                description=f"{msg}. The run was retried successfully 30 minutes later.",
                status="resolved", created_at=row["finished_at"] + timedelta(minutes=1), run=row,
                evidence_ref=f"pipeline_runs/order_ingestion/{row['run_id']}.jsonl"))
            t = t + timedelta(minutes=30)

        ing = make_run_row(run_id=make_run_id("order_ingestion", t), pipeline_id="order_ingestion",
                           batch_date=b, started_at=t, duration_ms=1500 + 12 * n, status="success",
                           expected=n, processed=n, trigger_type="scheduled")
        add_run(ing, ingestion_steps(b, n, n, n, 0), [])
        cumulative += n

        tv = ing["finished_at"] + timedelta(minutes=10)
        feed_ids = [o["source_event_id"] for o in orders]
        val_started = tv
        val_duration = 2000 + 2 * cumulative
        results = evaluate_rules(RuleInputs(
            batch_date=b, feed_event_ids=feed_ids, loaded_event_ids=set(feed_ids),
            observed_schema=dict(ORDER_FEED_CONTRACT), last_ingestion_finished=ing["finished_at"],
            now=val_started + timedelta(milliseconds=val_duration),
            freshness_threshold_minutes=threshold))
        val = make_run_row(run_id=make_run_id("order_validation", val_started), pipeline_id="order_validation",
                           batch_date=b, started_at=val_started, duration_ms=val_duration,
                           status=run_status_for(results), expected=cumulative, processed=cumulative,
                           trigger_type="scheduled")
        add_run(val, [], results)

        tr = val["finished_at"] + timedelta(minutes=10)
        revenue = sum((o["total_amount"] for o in orders), Decimal("0.00"))
        metrics = {"order_count": n, "revenue": str(revenue),
                   "distinct_customers": len({o["customer_id"] for o in orders})}
        rpt = make_run_row(run_id=make_run_id("reporting_prep", tr), pipeline_id="reporting_prep",
                           batch_date=b, started_at=tr, duration_ms=1000 + 5 * n, status="success",
                           expected=n, processed=n, trigger_type="scheduled")
        add_run(rpt, reporting_steps(b, metrics), [])
