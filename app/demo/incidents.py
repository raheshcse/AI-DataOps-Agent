"""One hundred deterministic, synthetic data-incident records for demonstrations.

* 20 categories x 5 scenario variants. Each variant has its own pipeline, numbers and detail, so records differ in
  substance, not only in name.
* Pure functions: no database, no network, no randomness (same output on every run, which the tests rely on).
* Every record carries ``source = "demo_synthetic"`` and ``synthetic = True``. IDs are ``DEMO-0001``..``DEMO-0100`` and
  cannot collide with simulator (``INC-nnnn``) or workflow (``inc-<hex>``) identifiers.
* Probable causes are hypotheses, never findings. No personal or sensitive data appears anywhere.
* ``human_review_required`` on a demo record is descriptive only. It does NOT put anything in the real Human Review queue,
  which lists persisted workflow states only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

DEMO_SOURCE = "demo_synthetic"
DEMO_ID_PREFIX = "DEMO-"
DEMO_ANCHOR = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)  # fixed "now" for the fixture, so output is repeatable
SEVERITIES = ("low", "medium", "high", "critical")
STATUSES = ("open", "investigating", "awaiting_review", "resolved", "closed")
INCIDENT_TYPES = ("pipeline_failure", "data_quality", "stale_data", "duplicate_records", "incomplete_processing",
                  "schema_mismatch", "other")

PIPELINES = {  # synthetic pipeline -> dataset it loads
    "order_ingestion": "staging.orders", "customer_sync": "staging.customers", "inventory_snapshot": "warehouse.inventory",
    "payments_ledger": "warehouse.payments", "shipping_events": "staging.shipments", "product_catalog": "warehouse.products",
    "returns_processing": "staging.returns", "marketing_attribution": "warehouse.attribution",
}
_P = list(PIPELINES)

# key -> (incident_type, title, description, expected label, cause hypothesis, next step, evidence kind)
# Placeholders: {p} pipeline, {d} dataset, {a}/{b}/{c} numbers, {t} detail text.
_CATEGORIES: dict[str, dict[str, Any]] = {
    "pipeline_execution_failure": dict(type="pipeline_failure", title="{p} run failed with {t}",
        desc="The scheduled {p} run terminated before completion with {t}. {a} of {b} expected records were processed.",
        metric="records_processed", cause="An unhandled error in the load step ({t}) may have aborted the run.",
        next="Read the run log for the failing step and compare with the previous successful run.", kind="run_log",
        variants=[(0, 4200, 0, "a connection reset"), (3, 1800, 640, "an out-of-memory error"), (6, 950, 120, "a lock timeout"),
                  (1, 5200, 2100, "a disk-full condition"), (4, 760, 0, "a missing config key")]),
    "missing_source_records": dict(type="incomplete_processing", title="Missing source records in {d}",
        desc="{a} source records dated {t} are absent from {d}, which expected {b}.", metric="row_count",
        cause="Records may have been filtered or dropped before staging ({t}).",
        next="Reconcile the source extract against {d} for the affected date.", kind="reconciliation",
        variants=[(4180, 4200, "the previous day"), (940, 1000, "the last 6 hours"), (2310, 2400, "the month end"),
                  (610, 640, "one region"), (88, 120, "a late batch")]),
    "duplicate_records": dict(type="duplicate_records", title="Duplicate event ids in {d}",
        desc="{a} source_event_id values appear more than once in {d} ({b} rows scanned).", metric="duplicate_event_ids",
        cause="A retried batch may have been loaded twice ({t}).", next="List the duplicated ids and the runs that loaded them.",
        kind="duplicate_scan", variants=[(37, 4200, "retry after timeout"), (112, 5000, "replayed file"),
                                         (9, 800, "double scheduler trigger"), (260, 9000, "manual re-run"), (4, 300, "overlapping window")]),
    "stale_delayed_data": dict(type="stale_data", title="{d} has not refreshed for {a} minutes",
        desc="The newest record in {d} is {a} minutes old; the freshness threshold is {b} minutes.", metric="data_age_minutes",
        cause="The upstream extract may be late or the scheduler may have skipped a run ({t}).",
        next="Check the last successful run time for {p}.", kind="freshness",
        variants=[(1620, 1500, "skipped run"), (310, 240, "late upstream file"), (95, 60, "slow load"), (2900, 1500, "paused schedule"), (200, 180, "queue backlog")]),
    "schema_mismatch": dict(type="schema_mismatch", title="Schema drift detected on {d}",
        desc="Column {t} on {d} no longer matches the expected contract ({a} columns observed, {b} expected).",
        metric="column_count", cause="An upstream change may have altered the column ({t}).",
        next="Compare the observed schema with the contract and identify the change.", kind="schema_contract",
        variants=[(13, 12, "unit_price type changed"), (11, 12, "currency_code removed"), (14, 12, "two extra columns"), (12, 12, "order_ts renamed"), (12, 12, "status length widened")]),
    "incomplete_batch": dict(type="incomplete_processing", title="Batch for {p} only partly processed",
        desc="{a} of {b} batch records were processed; the run was marked complete.", metric="records_processed",
        cause="The run may have exited early without error ({t}).", next="Compare feed rows, run metadata and target rows.",
        kind="reconciliation", variants=[(3900, 4200, "silent early exit"), (1500, 1800, "chunk skipped"), (600, 950, "commit lost"), (4900, 5200, "last page missed"), (700, 760, "timeout on tail")]),
    "failed_api_ingestion": dict(type="pipeline_failure", title="API ingestion for {p} failing ({t})",
        desc="Calls to the upstream API returned errors for {a} of {b} requests ({t}).", metric="failed_requests",
        cause="The upstream service may be degraded ({t}).", next="Check upstream status and recent response codes.", kind="api_log",
        variants=[(48, 60, "HTTP 502"), (12, 40, "HTTP 500"), (30, 30, "HTTP 503"), (7, 90, "malformed JSON"), (21, 25, "empty bodies")]),
    "malformed_records": dict(type="data_quality", title="Malformed records rejected from {d}",
        desc="{a} records in {d} failed parsing ({t}) out of {b}.", metric="rejected_rows", cause="The producer may have changed its format ({t}).",
        next="Sample the rejected rows and identify the common pattern.", kind="validation",
        variants=[(140, 4200, "bad date format"), (33, 900, "truncated JSON"), (610, 6000, "wrong encoding"), (8, 400, "stray delimiter"), (57, 2200, "nested array")]),
    "null_required_fields": dict(type="data_quality", title="Null values in required column of {d}",
        desc="{a} rows in {d} have NULL in a required column ({t}); {b} rows checked.", metric="null_rows",
        cause="The source may have stopped populating {t}.", next="Trace the null rows to their source batch.", kind="validation",
        variants=[(210, 4200, "customer_id"), (15, 800, "order_total"), (480, 5000, "created_at"), (66, 1300, "sku"), (9, 300, "status")]),
    "unexpected_row_count_change": dict(type="other", title="Row count for {d} changed unexpectedly",
        desc="{d} received {a} rows versus a typical {b}.", metric="row_count", cause="A volume change may be real demand or a filter fault ({t}).",
        next="Compare against the same weekday and the source system's own count.", kind="row_count",
        variants=[(1200, 4200, "sudden drop"), (9800, 4200, "sudden spike"), (0, 3000, "empty load"), (2100, 4000, "half volume"), (6400, 3100, "doubled volume")]),
    "referential_integrity": dict(type="data_quality", title="Orphaned references in {d}",
        desc="{a} rows in {d} reference keys missing from the parent table ({b} rows checked).", metric="orphan_rows",
        cause="The parent load may have run after the child load ({t}).", next="List orphan keys and check parent load ordering.", kind="integrity",
        variants=[(45, 4200, "load order"), (3, 800, "deleted parent"), (210, 6000, "partial parent load"), (18, 900, "late-arriving dimension"), (72, 2500, "key format mismatch")]),
    "transformation_failure": dict(type="pipeline_failure", title="Transformation step failed for {p}",
        desc="The transformation step errored on {a} of {b} records ({t}).", metric="failed_rows", cause="A rule may not handle an edge case ({t}).",
        next="Reproduce the failing rule on the offending rows.", kind="run_log",
        variants=[(120, 4200, "division by zero"), (14, 800, "unexpected null in join"), (300, 5200, "type cast error"), (5, 150, "regex mismatch"), (88, 1700, "date overflow")]),
    "source_to_staging_mismatch": dict(type="incomplete_processing", title="Source-to-staging mismatch for {p}",
        desc="The source reports {b} rows but staging holds {a}.", metric="staged_rows", cause="Rows may have been lost during extract ({t}).",
        next="Reconcile extract file counts with staging counts for the batch.", kind="reconciliation",
        variants=[(4100, 4200, "extract truncation"), (1700, 1800, "rejected rows"), (900, 950, "filter too strict"), (5100, 5200, "boundary timestamp"), (740, 760, "duplicates removed")]),
    "staging_to_target_mismatch": dict(type="incomplete_processing", title="Staging-to-target mismatch for {d}",
        desc="Staging holds {b} rows but the target holds {a}.", metric="target_rows", cause="The merge may have skipped rows ({t}).",
        next="Diff staging and target keys for the batch.", kind="reconciliation",
        variants=[(4050, 4200, "merge conflict"), (1750, 1800, "constraint reject"), (930, 950, "late commit"), (5000, 5200, "partition missing"), (755, 760, "upsert key issue")]),
    "freshness_sla_breach": dict(type="stale_data", title="Freshness SLA breached for {d}",
        desc="{d} was {a} minutes behind against a {b}-minute SLA ({t}).", metric="sla_minutes", cause="Processing may have been delayed by {t}.",
        next="Identify the slowest stage in the latest run.", kind="freshness",
        variants=[(190, 120, "queue contention"), (75, 60, "slow source"), (480, 240, "failed retry"), (130, 120, "backfill running"), (1500, 720, "weekend gap")]),
    "authentication_permission_failure": dict(type="pipeline_failure", title="Permission failure reading source for {p}",
        desc="The {p} job received an authorisation error ({t}) on {a} of {b} attempts.", metric="denied_attempts",
        cause="A credential or grant may have expired or changed ({t}).", next="Verify the service account's grants through the owning team.", kind="api_log",
        variants=[(3, 3, "401 unauthorised"), (5, 5, "403 forbidden"), (1, 4, "token expired"), (2, 2, "role revoked"), (6, 6, "access denied on schema")]),
    "upstream_dependency_outage": dict(type="pipeline_failure", title="Upstream dependency unavailable for {p}",
        desc="{p} could not reach its upstream dependency ({t}) for {a} minutes (limit {b}).", metric="outage_minutes",
        cause="The dependency may be down ({t}).", next="Check dependency status and retry policy; do not re-run until confirmed.", kind="api_log",
        variants=[(45, 15, "connection refused"), (120, 30, "DNS failure"), (20, 10, "TLS error"), (240, 60, "maintenance window"), (8, 5, "no route to host")]),
    "rate_limit_timeout": dict(type="pipeline_failure", title="Rate limiting or timeouts affecting {p}",
        desc="{a} of {b} requests to the upstream were throttled or timed out ({t}).", metric="throttled_requests",
        cause="Request volume may exceed the allowed rate ({t}).", next="Review request pacing and the upstream's quota.", kind="api_log",
        variants=[(22, 100, "HTTP 429"), (9, 60, "read timeout"), (40, 400, "burst limit"), (3, 30, "gateway timeout"), (15, 75, "retry storm")]),
    "data_quality_check_failure": dict(type="data_quality", title="Data-quality check failed on {d}",
        desc="Rule '{t}' failed: observed {a}, expected {b}.", metric="rule_value", cause="Upstream values may have shifted ({t}).",
        next="Review the failing rule and sample the offending rows.", kind="validation",
        variants=[(-12, 0, "non_negative_totals"), (31, 0, "future_dated_orders"), (4, 0, "total_equals_lines"), (7, 0, "status_in_set"), (19, 0, "unique_event_id")]),
    "suspicious_change": dict(type="other", title="Suspicious change on {d} needs investigation",
        desc="An unexplained change ({t}) altered {a} rows in {d}; baseline expectation was {b}.", metric="changed_rows",
        cause="Cause unknown; possibilities include an unplanned job or a manual edit ({t}).",
        next="Review change history and confirm with the data owner before any action.", kind="change_log",
        variants=[(350, 0, "bulk update"), (12, 0, "price column rewritten"), (90, 0, "status flipped"), (4, 0, "deleted rows"), (1200, 0, "timestamp shift")]),
}


def _detected_at(i: int, status: str) -> datetime:
    recent = i % 5 == 0 and status not in ("resolved", "closed")
    hours = (i * 7) % 40 if recent else 30 + (i * 53) % (24 * 150)
    if status in ("resolved", "closed"):
        hours += 24 * 3
    return DEMO_ANCHOR - timedelta(hours=hours, minutes=(i * 11) % 60)


def generate_demo_incidents() -> list[dict[str, Any]]:
    """Exactly 100 records, identical on every call."""
    out: list[dict[str, Any]] = []
    sev_cycle = ("medium", "high", "low", "critical", "medium", "high", "low", "medium", "high", "critical", "low", "medium")
    status_cycle = ("open", "investigating", "awaiting_review", "resolved", "open", "closed", "investigating", "awaiting_review", "open", "resolved")
    n = 0
    for c_idx, (key, c) in enumerate(_CATEGORIES.items()):
        for v_idx, variant in enumerate(c["variants"]):
            n += 1
            pipeline = _P[(c_idx + 3 * v_idx) % len(_P)]
            dataset = PIPELINES[pipeline]
            *nums, detail = variant
            a, b = nums[0], nums[1]
            if len(nums) > 2:
                a, b = nums[0], nums[1]
            ctx = dict(p=pipeline, d=dataset, a=a, b=b, c=nums[2] if len(nums) > 2 else 0, t=detail)
            severity = sev_cycle[(n * 5 + c_idx) % len(sev_cycle)]
            status = status_cycle[(n * 3 + c_idx) % len(status_cycle)]
            detected = _detected_at(n, status)
            batch = (detected - timedelta(days=0)).date()
            run_ref = f"DEMO-RUN-{pipeline}-{batch:%Y%m%d}-{n:03d}"
            review = status == "awaiting_review" or (severity == "critical" and status in ("open", "investigating"))
            out.append({
                "incident_id": f"{DEMO_ID_PREFIX}{n:04d}", "source": DEMO_SOURCE, "synthetic": True,
                "title": c["title"].format(**ctx), "description": c["desc"].format(**ctx),
                "category": key, "incident_type": c["type"], "severity": severity, "status": status,
                "affected_pipeline": pipeline, "affected_dataset": dataset, "detected_at": detected.isoformat(timespec="seconds"),
                "run_ref": run_ref, "batch_date": batch.isoformat(),
                "metric": c["metric"], "expected_value": b if c["metric"] not in ("rule_value", "changed_rows") else 0,
                "observed_value": a,
                "evidence": [{"kind": c["kind"], "label": "Synthetic diagnostic evidence", "reference": f"{run_ref}/{c['kind']}",
                              "summary": f"{c['metric']} observed {a}; expected {b if c['metric'] not in ('rule_value', 'changed_rows') else 0}.",
                              "confirmed": True}],
                "probable_cause": c["cause"].format(**ctx), "probable_cause_status": "hypothesis_unconfirmed",
                "recommended_next_step": c["next"].format(**ctx), "human_review_required": review,
            })
    return out


def validate_demo_incidents(items: list[dict[str, Any]]) -> list[str]:
    """Problems found (empty list = valid). Used by tests and the export script."""
    problems: list[str] = []
    if len(items) != 100:
        problems.append(f"expected 100 incidents, found {len(items)}")
    ids = [i["incident_id"] for i in items]
    if len(set(ids)) != len(ids):
        problems.append("duplicate incident ids")
    runs = [i["run_ref"] for i in items]
    if len(set(runs)) != len(runs):
        problems.append("duplicate run references")
    for i in items:
        iid = i["incident_id"]
        if not iid.startswith(DEMO_ID_PREFIX) or i.get("source") != DEMO_SOURCE or i.get("synthetic") is not True:
            problems.append(f"{iid}: not labelled synthetic")
        if i["severity"] not in SEVERITIES or i["status"] not in STATUSES or i["incident_type"] not in INCIDENT_TYPES:
            problems.append(f"{iid}: invalid enumeration value")
        if i["affected_pipeline"] not in PIPELINES or i["affected_dataset"] != PIPELINES[i["affected_pipeline"]]:
            problems.append(f"{iid}: inconsistent pipeline/dataset")
        if i["affected_pipeline"] not in i["run_ref"]:
            problems.append(f"{iid}: run reference does not match its pipeline")
        if i["status"] == "awaiting_review" and not i["human_review_required"]:
            problems.append(f"{iid}: awaiting review without human_review_required")
        if i["probable_cause_status"] != "hypothesis_unconfirmed":
            problems.append(f"{iid}: cause not marked as hypothesis")
        try:
            datetime.fromisoformat(i["detected_at"])
        except ValueError:
            problems.append(f"{iid}: bad timestamp")
    return problems
