from datetime import timedelta
from decimal import Decimal

from app.simulation.clock import ANCHOR_NOW
from app.simulation.generator import (
    CURRENT_BATCH,
    HISTORY_BATCHES,
    generate_baseline,
)
from app.simulation.validators import calc_freshness


def test_generation_is_deterministic():
    a, b = generate_baseline(), generate_baseline()
    assert a.customers == b.customers and a.orders == b.orders
    assert a.feed_rows == b.feed_rows and a.runs == b.runs and a.run_events == b.run_events


def test_different_seed_gives_different_data():
    assert generate_baseline(1).orders != generate_baseline(2).orders


def test_documented_baseline_counts():
    # These numbers are quoted in docs/simulation.md; if they change, update the docs.
    assert generate_baseline().expected_counts() == {
        "customers": 50, "products": 20, "orders": 173, "order_items": 448,
        "stg_order_feed": 203, "fx_order_feed_drifted": 0, "pipeline_definitions": 3,
        "pipeline_runs": 22, "validation_results": 42, "incident_records": 1,
        "scenario_activations": 0, "sim_state": 1,
    }


def test_relationships_are_consistent():
    d = generate_baseline()
    customer_ids = {c["customer_id"] for c in d.customers}
    product_ids = {p["product_id"] for p in d.products}
    run_ids = {r["run_id"] for r in d.runs}
    assert len({o["order_id"] for o in d.orders}) == len(d.orders)
    for o in d.orders:
        assert o["customer_id"] in customer_ids
        assert all(i["product_id"] in product_ids for i in o["items"])
    assert {v["run_id"] for v in d.validations} <= run_ids
    assert {i["run_id"] for i in d.incidents} <= run_ids
    assert len(run_ids) == len(d.runs)  # run ids are unique


def test_baseline_data_is_valid():
    d = generate_baseline()
    events = [o["source_event_id"] for o in d.orders]
    assert len(events) == len(set(events))  # no duplicates in the baseline
    for o in d.orders:
        assert o["total_amount"] == sum((i["line_total"] for i in o["items"]), Decimal("0.00"))
        assert o["order_ts"] <= ANCHOR_NOW and o["scenario_id"] is None
        assert all(i["quantity"] > 0 for i in o["items"])
    assert all(v["outcome"] == "pass" for v in d.validations)


def test_feed_matches_orders_and_pending_batch_is_unloaded():
    d = generate_baseline()
    loaded = {o["source_event_id"] for o in d.orders}
    for row in d.feed_rows:
        if row["batch_date"] in HISTORY_BATCHES:
            assert row["source_event_id"] in loaded
        else:
            assert row["batch_date"] == CURRENT_BATCH and row["source_event_id"] not in loaded
    assert sum(1 for r in d.feed_rows if r["batch_date"] == CURRENT_BATCH) == 30


def test_synthetic_data_is_clearly_synthetic():
    d = generate_baseline()
    assert all(c["email"].endswith("@example.test") for c in d.customers)
    assert all(c["customer_code"].startswith("CUST-") for c in d.customers)


def test_history_has_one_failed_run_with_matching_incident_and_retry():
    d = generate_baseline()
    failed = [r for r in d.runs if r["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["error_code"] == "E1002"
    assert d.incidents[0]["run_id"] == failed[0]["run_id"] and d.incidents[0]["status"] == "resolved"
    retry = [r for r in d.runs if r["pipeline_id"] == "order_ingestion" and r["status"] == "success"
             and r["batch_date"] == failed[0]["batch_date"]]
    assert len(retry) == 1 and retry[0]["started_at"] > failed[0]["finished_at"]


def test_baseline_is_fresh_at_the_anchor_time():
    d = generate_baseline()
    last = max(r["finished_at"] for r in d.runs if r["pipeline_id"] == "order_ingestion" and r["status"] == "success")
    f = calc_freshness(last, d.state["sim_now"], 1500)
    assert not f["stale"] and f["age_minutes"] < 1500
    assert calc_freshness(last, d.state["sim_now"] + timedelta(hours=30), 1500)["stale"]


def test_history_logs_match_run_outcomes():
    d = generate_baseline()
    for run in d.runs:
        events = d.run_events[run["run_id"]]
        assert events[0]["event"] == "run_started" and events[-1]["run_id"] == run["run_id"]
        final = events[-1]["event"]
        assert (final == "run_failed") == (run["status"] == "failed")
        assert not any(e["event"] == "run_completed" for e in events) or run["status"] == "success"
