"""Pure data-quality / operational validators (no database access).

Each ``check_*`` takes plain data and returns a ``ValidationResult``.
``evaluate_rules`` runs all of them; the pipeline simulator and the baseline
generator both use it, so a rule means the same thing everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.simulation.schema_contract import ORDER_FEED_CONTRACT, diff_schema

RULE_COMPLETENESS = "batch_completeness"
RULE_DUPLICATES = "no_duplicate_source_events"
RULE_TOTALS = "order_total_matches_line_items"
RULE_FUTURE = "order_timestamp_not_in_future"
RULE_SCHEMA = "feed_schema_contract"
RULE_FRESHNESS = "data_freshness"

ALL_RULES = [
    RULE_COMPLETENESS,
    RULE_DUPLICATES,
    RULE_TOTALS,
    RULE_FUTURE,
    RULE_SCHEMA,
    RULE_FRESHNESS,
]

# Error code reported by a validation run for its first failing rule, and the
# incident category / severity used when that failure opens an incident.
RULE_ERROR_CODES = {
    RULE_COMPLETENESS: "E2001",
    RULE_DUPLICATES: "E2002",
    RULE_TOTALS: "E2003",
    RULE_FUTURE: "E2004",
    RULE_SCHEMA: "E2005",
    RULE_FRESHNESS: "E2006",
}
RULE_INCIDENT = {
    RULE_COMPLETENESS: ("incomplete_processing", "high"),
    RULE_DUPLICATES: ("duplicate_records", "medium"),
    RULE_TOTALS: ("data_quality", "medium"),
    RULE_FUTURE: ("data_quality", "low"),
    RULE_SCHEMA: ("schema_mismatch", "high"),
    RULE_FRESHNESS: ("stale_data", "high"),
}


@dataclass
class ValidationResult:
    rule_name: str
    outcome: str  # 'pass' | 'fail' | 'warn'
    expected_value: str
    observed_value: str
    record_ref: str | None = None
    detail: str | None = None

    def to_row(self, run_id: str, created_at: datetime) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "rule_name": self.rule_name,
            "outcome": self.outcome,
            "expected_value": self.expected_value,
            "observed_value": self.observed_value,
            "record_ref": self.record_ref,
            "detail": self.detail,
            "created_at": created_at,
        }


def _refs(items: list[str], limit: int = 5) -> str | None:
    """A safe, bounded record reference list (identifiers only, never row contents)."""
    if not items:
        return None
    shown = ", ".join(items[:limit])
    more = len(items) - limit
    return shown + (f" (+{more} more)" if more > 0 else "")


def check_batch_completeness(feed_ids: list[str], loaded_ids: set[str], batch_date: date) -> ValidationResult:
    missing = [e for e in feed_ids if e not in loaded_ids]
    expected, observed = len(feed_ids), len(feed_ids) - len(missing)
    if not missing:
        return ValidationResult(RULE_COMPLETENESS, "pass", str(expected), str(observed),
                                detail=f"All feed events for batch {batch_date} are present in orders.")
    return ValidationResult(
        RULE_COMPLETENESS, "fail", str(expected), str(observed), _refs(missing),
        f"{len(missing)} of {expected} feed events for batch {batch_date} are missing from orders "
        f"(shortfall {len(missing)}).",
    )


def check_no_duplicates(duplicates: list[dict[str, Any]]) -> ValidationResult:
    if not duplicates:
        return ValidationResult(RULE_DUPLICATES, "pass", "0", "0",
                                detail="Every source_event_id appears in exactly one order.")
    refs = [f"{d['source_event_id']} x{d['copies']} (orders {','.join(map(str, d['order_ids']))})"
            for d in duplicates]
    return ValidationResult(
        RULE_DUPLICATES, "fail", "0", str(len(duplicates)), _refs(refs, 3),
        f"{len(duplicates)} source_event_id value(s) appear in more than one order.",
    )


def check_order_totals(mismatches: list[dict[str, Any]]) -> ValidationResult:
    if not mismatches:
        return ValidationResult(RULE_TOTALS, "pass", "0", "0",
                                detail="Every order total equals the sum of its line items.")
    refs = [f"order {m['order_id']} ({m['source_event_id']}): total {m['total_amount']} "
            f"vs items {m['items_total']}" for m in mismatches]
    return ValidationResult(
        RULE_TOTALS, "fail", "0", str(len(mismatches)), _refs(refs, 3),
        f"{len(mismatches)} order(s) have a total that differs from the sum of their line items.",
    )


def check_future_orders(rows: list[dict[str, Any]]) -> ValidationResult:
    if not rows:
        return ValidationResult(RULE_FUTURE, "pass", "0", "0",
                                detail="No order is timestamped after the current (simulated) time.")
    refs = [f"order {r['order_id']} ({r['source_event_id']}) at {r['order_ts'].isoformat()}" for r in rows]
    return ValidationResult(
        RULE_FUTURE, "warn", "0", str(len(rows)), _refs(refs, 3),
        f"{len(rows)} order(s) have a timestamp in the future.",
    )


def check_schema(observed: dict[str, str], table_name: str,
                 expected: dict[str, str] | None = None) -> ValidationResult:
    expected = ORDER_FEED_CONTRACT if expected is None else expected
    diff = diff_schema(expected, observed)
    exp_text, obs_text = f"{len(expected)} columns", f"{len(observed)} columns"
    if not any(diff.values()):
        return ValidationResult(RULE_SCHEMA, "pass", exp_text, obs_text, table_name,
                                f"Table {table_name} matches the order feed contract.")
    parts = []
    if diff["missing"]:
        parts.append("missing: " + ", ".join(diff["missing"]))
    if diff["unexpected"]:
        parts.append("unexpected: " + ", ".join(diff["unexpected"]))
    if diff["type_mismatch"]:
        parts.append("type mismatch: " + "; ".join(diff["type_mismatch"]))
    return ValidationResult(RULE_SCHEMA, "fail", exp_text, obs_text, table_name,
                            f"Table {table_name} does not match the contract. " + " | ".join(parts))


def calc_freshness(last_finished: datetime | None, now: datetime, threshold_minutes: int) -> dict[str, Any]:
    """Freshness of a dataset = age of its latest successful load vs. a threshold."""
    if last_finished is None:
        return {"last_success_at": None, "age_minutes": None,
                "threshold_minutes": threshold_minutes, "stale": True}
    age = round((now - last_finished).total_seconds() / 60, 1)
    return {"last_success_at": last_finished, "age_minutes": age,
            "threshold_minutes": threshold_minutes, "stale": age > threshold_minutes}


def check_freshness(last_finished: datetime | None, now: datetime, threshold_minutes: int) -> ValidationResult:
    f = calc_freshness(last_finished, now, threshold_minutes)
    expected = f"age <= {threshold_minutes} min"
    if f["last_success_at"] is None:
        return ValidationResult(RULE_FRESHNESS, "fail", expected, "no successful run",
                                detail="No successful order_ingestion run exists.")
    detail = (f"Latest successful order_ingestion finished at {f['last_success_at'].isoformat()}; "
              f"age {f['age_minutes']} min vs threshold {threshold_minutes} min.")
    return ValidationResult(RULE_FRESHNESS, "fail" if f["stale"] else "pass", expected,
                            f"age {f['age_minutes']} min", None, detail)


@dataclass
class RuleInputs:
    batch_date: date
    feed_event_ids: list[str]
    loaded_event_ids: set[str]
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    total_mismatches: list[dict[str, Any]] = field(default_factory=list)
    future_orders: list[dict[str, Any]] = field(default_factory=list)
    observed_schema: dict[str, str] = field(default_factory=dict)
    schema_table: str = "stg_order_feed"
    last_ingestion_finished: datetime | None = None
    now: datetime | None = None
    freshness_threshold_minutes: int = 1500


def evaluate_rules(i: RuleInputs) -> list[ValidationResult]:
    assert i.now is not None, "RuleInputs.now is required"
    return [
        check_batch_completeness(i.feed_event_ids, i.loaded_event_ids, i.batch_date),
        check_no_duplicates(i.duplicates),
        check_order_totals(i.total_mismatches),
        check_future_orders(i.future_orders),
        check_schema(i.observed_schema, i.schema_table),
        check_freshness(i.last_ingestion_finished, i.now, i.freshness_threshold_minutes),
    ]


def run_status_for(results: list[ValidationResult]) -> str:
    if any(r.outcome == "fail" for r in results):
        return "failed"
    if any(r.outcome == "warn" for r in results):
        return "warning"
    return "success"
