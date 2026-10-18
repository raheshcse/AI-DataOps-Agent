from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.simulation import validators as v
from app.simulation.schema_contract import ORDER_FEED_CONTRACT, diff_schema

NOW = datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)
D = date(2026, 1, 14)


def test_completeness_pass_and_fail_with_shortfall():
    ok = v.check_batch_completeness(["a", "b"], {"a", "b"}, D)
    assert ok.outcome == "pass"
    bad = v.check_batch_completeness(["a", "b", "c"], {"a"}, D)
    assert (bad.outcome, bad.expected_value, bad.observed_value) == ("fail", "3", "1")
    assert bad.record_ref == "b, c" and "shortfall 2" in bad.detail


def test_record_reference_is_bounded():
    ids = [f"E{i}" for i in range(20)]
    res = v.check_batch_completeness(ids, set(), D)
    assert res.record_ref.endswith("(+15 more)")


def test_duplicates_identify_events_and_orders():
    res = v.check_no_duplicates([{"source_event_id": "EVT-1", "copies": 2, "order_ids": [1, 9]}])
    assert res.outcome == "fail" and "EVT-1 x2 (orders 1,9)" in res.record_ref
    assert v.check_no_duplicates([]).outcome == "pass"


def test_total_mismatch():
    row = {"order_id": 5, "source_event_id": "E", "total_amount": Decimal("20.00"), "items_total": Decimal("10.00")}
    res = v.check_order_totals([row])
    assert res.outcome == "fail" and "order 5" in res.record_ref and "20.00 vs items 10.00" in res.record_ref


def test_future_orders_warn_not_fail():
    res = v.check_future_orders([{"order_id": 1, "source_event_id": "E", "order_ts": NOW + timedelta(days=3)}])
    assert res.outcome == "warn"


def test_schema_diff_reports_each_kind_of_mismatch():
    observed = dict(ORDER_FEED_CONTRACT)
    del observed["currency"]
    observed["legacy_flag"] = "bool"
    observed["order_total"] = "string"
    diff = diff_schema(ORDER_FEED_CONTRACT, observed)
    assert diff == {"missing": ["currency"], "unexpected": ["legacy_flag"],
                    "type_mismatch": ["order_total: expected decimal, observed string"]}
    res = v.check_schema(observed, "fx_table")
    assert res.outcome == "fail" and res.record_ref == "fx_table"
    for text in ("missing: currency", "unexpected: legacy_flag", "order_total"):
        assert text in res.detail
    assert v.check_schema(dict(ORDER_FEED_CONTRACT), "stg_order_feed").outcome == "pass"


def test_freshness_fresh_stale_and_missing():
    last = NOW - timedelta(minutes=100)
    assert v.check_freshness(last, NOW, 120).outcome == "pass"
    stale = v.check_freshness(last, NOW, 60)
    assert stale.outcome == "fail" and "age 100.0 min" in stale.observed_value and "threshold 60" in stale.detail
    assert v.check_freshness(None, NOW, 60).outcome == "fail"
    assert v.calc_freshness(last, NOW, 100)["stale"] is False  # boundary: equal is fresh


def test_run_status_precedence():
    p = v.ValidationResult("r", "pass", "", "")
    w = v.ValidationResult("r", "warn", "", "")
    f = v.ValidationResult("r", "fail", "", "")
    assert v.run_status_for([p]) == "success"
    assert v.run_status_for([p, w]) == "warning"
    assert v.run_status_for([p, w, f]) == "failed"
