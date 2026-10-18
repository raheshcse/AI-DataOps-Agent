"""Tool 3 - duplicate-record detection.

Uniqueness key: ``orders.source_event_id``. The schema deliberately does not enforce it
(so duplicates can exist and be found), and the pipelines treat it as "one order per
source event". Orders that merely look alike (same customer, amount or time) but carry
different event ids are separate business events and are NOT reported.
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
)
from app.simulation.validators import RULE_ERROR_CODES, RULE_DUPLICATES

NAME = "detect_duplicate_records"
_CONTENT_KEYS = ("customer_id", "batch_date", "order_ts", "total_amount", "currency")


def _classify(orders: list[dict[str, Any]]) -> str:
    first = tuple(orders[0][k] for k in _CONTENT_KEYS)
    same = all(tuple(o[k] for k in _CONTENT_KEYS) == first for o in orders[1:])
    return "identical_replay" if same else "conflicting_content"


def detect_duplicate_records(repo) -> DiagnosticResult:
    order_groups = repo.duplicate_order_details()
    feed_groups = repo.duplicate_feed_events()
    feed_pending = []
    if feed_groups:
        loaded = repo.known_event_ids([g["source_event_id"] for g in feed_groups])
        feed_pending = [g for g in feed_groups if g["source_event_id"] not in loaded]

    evidence: list[dict[str, Any]] = [{
        "type": "key_definition", "key": "orders.source_event_id",
        "rule": "At most one order per source_event_id.",
        "not_duplicates": "Orders with the same customer, amount or timestamp but different "
                          "source_event_id values are separate business events and are not reported."}]
    affected: list[str] = []
    extra_rows, excess = 0, Decimal("0")
    conflicting = 0
    for g in order_groups:
        orders = g["orders"]
        kind = _classify(orders)
        conflicting += kind == "conflicting_content"
        extra = orders[1:]
        extra_rows += len(extra)
        excess += sum((o["total_amount"] for o in extra), Decimal("0"))
        affected += [f"order:{o['order_id']}" for o in orders]
        evidence.append({
            "type": "duplicate_group", "source_event_id": g["source_event_id"], "copies": g["copies"],
            "classification": kind, "order_ids": [o["order_id"] for o in orders],
            "order_timestamps": [o["order_ts"] for o in orders],
            "batch_dates": sorted({o["batch_date"] for o in orders}),
            "total_amounts": [o["total_amount"] for o in orders]})
    for g in feed_groups:
        pending = g in feed_pending
        evidence.append({
            "type": "feed_repeat", "source_event_id": g["source_event_id"], "copies": g["copies"],
            "feed_ids": g["feed_ids"], "batch_dates": g["batch_dates"],
            "already_loaded_into_orders": not pending,
            "note": ("Event already loaded; ingestion skips events present in orders (idempotent)."
                     if not pending else
                     "Repeated in the feed and not yet loaded. Ingestion checks only against orders "
                     "already loaded, so loading this batch would insert it more than once.")})
    if feed_pending:
        affected += [f"feed:{fid}" for g in feed_pending for fid in g["feed_ids"]]
    affected, notes = bounded(affected)

    observed = {"duplicate_keys": len(order_groups), "extra_order_rows": extra_rows,
                "excess_revenue": excess, "repeated_in_feed_not_loaded": len(feed_pending)}
    expected = {"duplicate_keys": 0, "extra_order_rows": 0, "repeated_in_feed_not_loaded": 0}

    if order_groups:
        severity = "high" if conflicting else "medium"
        detail = f", {conflicting} with conflicting content" if conflicting else " (identical replays)"
        return DiagnosticResult(
            NAME, STATUS_ISSUE, severity,
            f"{len(order_groups)} source_event_id value(s) appear in more than one order{detail}; "
            f"{extra_rows} surplus order row(s) inflate order count and revenue by {excess}.",
            evidence, affected, observed, expected, RULE_ERROR_CODES[RULE_DUPLICATES],
            "Compare the listed copies, decide which is authoritative, and find how the repeat entered "
            "orders (for example a replayed feed event). Nothing has been changed.", {}, notes)
    if feed_pending:
        return DiagnosticResult(
            NAME, STATUS_WARNING, "low",
            f"No duplicate orders, but {len(feed_pending)} event id(s) repeat in the feed and are not "
            "loaded yet.", evidence, affected, observed, expected, None,
            "Remove or de-duplicate the repeated feed rows before ingesting the batch.", {}, notes)
    summary = "No duplicate source_event_id values in orders."
    if feed_groups:
        summary += f" {len(feed_groups)} feed event id(s) repeat but are already loaded (skipped by ingestion)."
    return DiagnosticResult(NAME, STATUS_OK, "info", summary, evidence, [], observed, expected, None, None,
                            {}, notes)
