"""Tool 4 - data-quality diagnostics over the orders data.

Reuses the Phase 2 validators for totals, future timestamps and duplicates, and adds the
checks the schema cannot express (blank required values, negative amounts, invalid status,
duplicate-driven overcounts). Completeness, freshness and schema have their own tools.
Nothing is modified.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.diagnostics.models import (
    STATUS_ISSUE,
    STATUS_OK,
    STATUS_WARNING,
    DiagnosticResult,
    bounded,
    worst_severity,
)
from app.simulation.validators import (
    RULE_DUPLICATES,
    RULE_ERROR_CODES,
    RULE_FUTURE,
    RULE_INCIDENT,
    RULE_TOTALS,
    check_future_orders,
    check_no_duplicates,
    check_order_totals,
)

NAME = "diagnose_data_quality"
RULE_OVERCOUNT = "duplicate_driven_overcount"
RULE_REQUIRED = "required_values_present"
RULE_AMOUNTS = "amounts_non_negative"
RULE_STATUS = "order_status_valid"


def _finding(rule: str, outcome: str, severity: str, affected: list[str], observed: Any, expected: Any,
             detail: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    refs, notes = bounded(affected)
    return {"type": "rule_result", "rule_name": rule, "outcome": outcome,
            "severity": severity if outcome != "pass" else "info", "affected_records": refs,
            "observed_value": observed, "expected_condition": expected, "detail": detail,
            "rows": rows[:20], "notes": notes}


def diagnose_data_quality(repo, now) -> DiagnosticResult:
    findings: list[dict[str, Any]] = []

    mismatches = repo.order_total_mismatches()
    v = check_order_totals(mismatches)
    findings.append(_finding(
        RULE_TOTALS, v.outcome, RULE_INCIDENT[RULE_TOTALS][1], [f"order:{m['order_id']}" for m in mismatches],
        {"orders_with_mismatch": len(mismatches)}, "order total equals the sum of its line items", v.detail,
        [{"order_id": m["order_id"], "source_event_id": m["source_event_id"], "total_amount": m["total_amount"],
          "items_total": m["items_total"], "difference": m["total_amount"] - m["items_total"]}
         for m in mismatches]))

    future = repo.future_dated_orders(now)
    v = check_future_orders(future)
    findings.append(_finding(
        RULE_FUTURE, v.outcome, RULE_INCIDENT[RULE_FUTURE][1], [f"order:{r['order_id']}" for r in future],
        {"future_dated_orders": len(future)}, "order timestamp is not after the current simulated time", v.detail,
        [{"order_id": r["order_id"], "source_event_id": r["source_event_id"], "order_ts": r["order_ts"]}
         for r in future]))

    dups = repo.duplicate_events()
    v = check_no_duplicates(dups)
    findings.append(_finding(
        RULE_DUPLICATES, v.outcome, RULE_INCIDENT[RULE_DUPLICATES][1],
        [f"order:{oid}" for d in dups for oid in d["order_ids"]], {"duplicate_keys": len(dups)},
        "each source_event_id appears in exactly one order", v.detail,
        [{"source_event_id": d["source_event_id"], "copies": d["copies"], "order_ids": d["order_ids"]}
         for d in dups]))

    groups = repo.duplicate_order_details() if dups else []
    extra_orders = [o for g in groups for o in g["orders"][1:]]
    excess = sum((o["total_amount"] for o in extra_orders), Decimal("0"))
    findings.append(_finding(
        RULE_OVERCOUNT, "fail" if extra_orders else "pass", "medium",
        [f"order:{o['order_id']}" for o in extra_orders],
        {"surplus_order_rows": len(extra_orders), "excess_revenue": excess}, "no surplus order rows",
        (f"{len(extra_orders)} surplus order row(s) from repeated source events add {excess} to revenue."
         if extra_orders else "Order count equals the number of distinct source events."),
        [{"order_id": o["order_id"], "source_event_id": o["source_event_id"], "total_amount": o["total_amount"]}
         for o in extra_orders]))

    scan = repo.order_quality_scan()
    blank, negative, bad_status = scan["blank_required"], scan["negative_amounts"], scan["invalid_status"]
    findings.append(_finding(
        RULE_REQUIRED, "fail" if blank else "pass", "medium", [f"order:{r['order_id']}" for r in blank],
        {"orders_with_blank_values": len(blank)}, "source_event_id and currency are present and non-blank",
        f"{len(blank)} order(s) have a blank source_event_id or currency." if blank
        else "No blank required values in orders.", blank))
    findings.append(_finding(
        RULE_AMOUNTS, "fail" if negative else "pass", "medium",
        [f"order_item:{r['order_item_id']}" if r["kind"] == "order_item" else f"order:{r['order_id']}"
         for r in negative], {"records_with_invalid_amounts": len(negative)},
        "order totals and unit/line prices >= 0 and quantities > 0",
        f"{len(negative)} record(s) have a negative amount or non-positive quantity." if negative
        else "No negative amounts or non-positive quantities.", negative))
    findings.append(_finding(
        RULE_STATUS, "fail" if bad_status else "pass", "medium", [f"order:{r['order_id']}" for r in bad_status],
        {"orders_with_invalid_status": len(bad_status)}, "status is one of placed, paid, shipped, cancelled",
        f"{len(bad_status)} order(s) have a status outside the allowed set." if bad_status
        else "All order statuses are valid.", bad_status))

    failed = [f for f in findings if f["outcome"] == "fail"]
    warned = [f for f in findings if f["outcome"] == "warn"]
    evidence = [{"type": "scope", "checked": [f["rule_name"] for f in findings],
                 "not_covered": "batch completeness, freshness and schema drift have separate tools"}] + findings
    affected, notes = bounded(sorted({r for f in failed + warned for r in f["affected_records"]}))
    notes += [f"{f['rule_name']}: {n}" for f in failed + warned for n in f["notes"]]  # surface per-rule truncation
    observed = {"failed_rules": [f["rule_name"] for f in failed], "warned_rules": [f["rule_name"] for f in warned]}
    expected = {"failed_rules": [], "warned_rules": []}
    code = next((RULE_ERROR_CODES[f["rule_name"]] for f in failed + warned if f["rule_name"] in RULE_ERROR_CODES),
                None)
    if failed:
        return DiagnosticResult(
            NAME, STATUS_ISSUE, worst_severity(*(f["severity"] for f in failed)),
            f"{len(failed)} data-quality rule(s) failed"
            + (f" and {len(warned)} warned" if warned else "") + ": "
            + ", ".join(f["rule_name"] for f in failed + warned) + ".",
            evidence, affected, observed, expected, code,
            "Review the affected records per rule. Do not edit data until the source of each problem "
            "is understood; this tool changes nothing.", {}, notes)
    if warned:
        return DiagnosticResult(
            NAME, STATUS_WARNING, "low", f"{len(warned)} data-quality warning(s): "
            + ", ".join(f["rule_name"] for f in warned) + ".", evidence, affected, observed, expected, code,
            "Check the timestamps of the listed records against the simulated clock.", {}, notes)
    return DiagnosticResult(NAME, STATUS_OK, "info", f"All {len(findings)} data-quality rules passed.", evidence,
                            [], observed, expected, None, None, {}, notes)
