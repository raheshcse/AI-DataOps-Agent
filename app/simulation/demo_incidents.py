"""Synthetic demonstration incidents for the SQL Server simulator (additive and idempotent).

``python -m scripts.db_seed_demo_incidents`` adds ~98 distinct, clearly synthetic incidents to an already seeded
simulator, next to the baseline ``INC-0001`` and ``INC-0002``. Each incident is backed by a real failed run row, its
validation rows (where a rule applies) and a JSON Lines run log written through the normal log store, so the existing
read-only diagnostics, the API and the dashboards see the same kind of records a live simulator run produces.

Safety properties (see ``tests/test_phase12_demo_incidents.py``):
* only INSERTs: nothing is updated, deleted, truncated or reset, and existing rows are never touched;
* idempotent: a demo run is identified by its fixed ``RUN-DEMO-...`` id; if it exists the entry is skipped;
* deterministic: no clock or randomness, so every machine gets the same catalogue;
* the demo runs are dated BEFORE the baseline window, so "latest successful run" and freshness logic are unchanged;
* no scenario is activated (``scenario_id`` stays empty), so scenario restore logic can never remove these rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.simulation.clock import ANCHOR_NOW
from app.simulation.events import PIPELINE_ABBREV, build_run_events, failure_steps, make_incident, make_run_row, step
from app.simulation.runlog import RunLogStore
from app.simulation.validators import (RULE_COMPLETENESS, RULE_DUPLICATES, RULE_FRESHNESS, RULE_FUTURE, RULE_SCHEMA,
                                       RULE_TOTALS, ValidationResult)

DEMO_RUN_PREFIX = "RUN-DEMO-"
SYNTHETIC_NOTE = "Synthetic demonstration incident (generated test data, not a real outage)."
RULE_RECONCILIATION = "reconciliation_row_totals"
# Synthetic incidents use their own error-code namespace. The code-only recovery mapping (app.recovery.workflow) is keyed
# on the real scenario error codes (E1001, E2002, ...), so reusing those would make the system propose removing a
# "scenario" that was never injected. Synthetic incidents therefore never produce a recovery proposal.
CODE_PREFIX = "DEMO-"
FIRST_RUN_START = ANCHOR_NOW - timedelta(days=8)  # the baseline window is the 7 days before the anchor

_STATUS_CYCLE = ("open", "resolved", "open", "investigating", "resolved", "open", "investigating", "resolved", "open", "resolved")
_SEVERITY_CYCLES = (("high", "medium", "critical", "medium", "low", "high", "medium"),
                    ("medium", "high", "low", "medium", "critical", "medium", "high"),
                    ("critical", "high", "medium", "high", "low", "medium", "medium"))


@dataclass(frozen=True)
class Category:
    key: str
    count: int
    pipelines: tuple[str, ...]
    code: str
    component: str
    event: str
    sources: tuple[str, ...]
    titles: tuple[str, ...]
    messages: tuple[str, ...]


# {src} = source/system name, {pl} = pipeline display name. Counts add up to 98 (+ the 2 baseline incidents = 100).
CATEGORIES: tuple[Category, ...] = (
    Category("ingestion_failure", 10, ("order_ingestion",), "E1002", "feed_reader", "feed_parse_failed",
             ("ERP order export", "web shop feed", "partner marketplace feed", "call-centre orders file"),
             ("Order ingestion failed: {src} could not be parsed", "Ingestion aborted after unreadable {src}",
              "Order load rejected: malformed records in {src}"),
             ("Feed reader rejected {src}: {bad} of {exp} rows could not be parsed; no orders were loaded.",
              "Ingestion stopped on {src} after {bad} malformed rows; 0 of {exp} rows loaded.")),
    Category("pipeline_failure", 9, ("order_ingestion", "order_validation", "reporting_prep"), "E9001", "pipeline_runner",
             "step_failed", ("scheduler", "worker pool", "orchestrator"),
             ("{pl} failed before completing", "{pl} aborted: worker terminated", "{pl} run did not finish"),
             ("The {pl} run terminated unexpectedly in the {src}; the last completed step was not committed.",
              "{pl} exited with a non-zero status raised by the {src}; partial results were discarded.")),
    Category("source_timeout", 9, ("order_ingestion",), "E1001", "source_connector", "feed_connection_timeout",
             ("order system API", "inventory service", "customer master service"),
             ("Source system timeout: {src} did not respond", "Order feed timed out waiting for {src}",
              "{src} timeout after 30s retries"),
             ("Connecting to the {src} timed out after 3 attempts (30 s each); 0 of {exp} expected rows were read.",
              "The {src} did not answer within the timeout; the batch was not started.")),
    Category("missing_data", 8, ("order_ingestion", "reporting_prep"), "E3001", "report_builder", "no_source_data",
             ("daily order file", "regional sales extract", "returns extract"),
             ("Missing data: no rows received from {src}", "Expected {src} not delivered", "Empty batch from {src}"),
             ("No rows were found in the {src} for the batch; {exp} rows were expected.",
              "The {src} was absent or empty at the scheduled time; downstream steps had no input.")),
    Category("schema_mismatch", 9, ("order_ingestion", "order_validation"), "E2005", "schema_checker", "schema_contract_violation",
             ("order feed", "line-item feed", "customer feed"),
             ("Schema mismatch in {src}", "{src} column type changed", "Unexpected column removed from {src}"),
             ("The {src} no longer matches the agreed schema: {detail}. The batch was stopped before loading.",
              "Schema check failed for the {src}: {detail}.")),
    Category("duplicate_records", 8, ("order_ingestion", "order_validation"), "E2002", "validator", "duplicate_events_detected",
             ("order events", "payment events", "shipment events"),
             ("Duplicate records detected in {src}", "{src}: same event delivered more than once", "Repeated {src} found after load"),
             ("{bad} duplicate source events were found among the {src}; the duplicate rule failed.",
              "Duplicate check failed: {bad} repeated event ids in the {src}.")),
    Category("data_quality", 9, ("order_validation",), "E2003", "validator", "data_quality_rule_failed",
             ("order totals", "order timestamps", "customer references"),
             ("Data quality violation: {src}", "Quality rule failed for {src}", "{src} outside accepted limits"),
             ("The {src} rule failed on {bad} records (see validation results).",
              "{bad} records breached the {src} quality rule; the batch is flagged for review.")),
    Category("row_count_variance", 8, ("order_ingestion", "reporting_prep"), "E3002", "pipeline_runner", "row_count_deviation",
             ("daily orders", "order lines", "customer updates"),
             ("Unexpected row count in {src}", "{src} volume far below normal", "Row-count variance for {src}"),
             ("Processed {proc} of {exp} expected {src} ({pct}%); this is outside the accepted variance.",
              "Only {proc} {src} arrived against an expected {exp}.")),
    Category("transformation_failure", 8, ("order_ingestion", "reporting_prep"), "E4001", "transformer", "transformation_failed",
             ("currency conversion", "order-total calculation", "customer-region mapping"),
             ("Transformation failed: {src}", "{src} step raised an error", "Failed {src} on a batch"),
             ("The {src} step failed on {bad} rows (division by zero or unmapped value); the batch was rolled back.",
              "Transformation error in the {src} step for {bad} rows; no output was written.")),
    Category("connectivity", 7, ("order_ingestion", "order_validation", "reporting_prep"), "E1004", "db_connector", "database_unreachable",
             ("warehouse database", "staging database", "reporting database"),
             ("Database connectivity problem: {src}", "Cannot reach {src}", "Connection to {src} dropped"),
             ("The {pl} could not open a connection to the {src}: connection refused after 3 retries.",
              "Lost the connection to the {src} while the {pl} was running; the run was stopped.")),
    Category("stale_data", 7, ("order_validation",), "E2006", "validator", "freshness_sla_violation",
             ("order table", "order-feed staging table", "reporting snapshot"),
             ("Data freshness SLA violated: {src}", "{src} older than the agreed limit", "Stale {src}"),
             ("The {src} was last updated {mins} minutes ago; the freshness SLA is 60 minutes.",
              "Freshness check failed: {src} is {mins} minutes old (limit 60).")),
    Category("reconciliation_mismatch", 6, ("order_validation", "reporting_prep"), "E2007", "reconciler", "reconciliation_mismatch",
             ("order totals vs finance ledger", "daily revenue vs source", "order count vs shipment count"),
             ("Reconciliation mismatch: {src}", "{src} do not agree", "Reconciliation difference in {src}"),
             ("Reconciliation of {src} differs by {bad} records between the two systems.",
              "{src}: {bad} records do not reconcile; totals disagree.")),
)

assert sum(c.count for c in CATEGORIES) == 98

_PIPELINE_NAMES = {"order_ingestion": "order ingestion pipeline", "order_validation": "order validation pipeline",
                   "reporting_prep": "reporting preparation pipeline"}
_SCHEMA_DETAILS = ("column 'order_total' changed from decimal to text", "required column 'customer_id' is missing",
                   "new unexpected column 'promo_flag' appeared", "column 'order_ts' renamed to 'created_ts'",
                   "column 'quantity' changed from integer to decimal")
_NO_ROWS = ("ingestion_failure", "source_timeout", "missing_data", "transformation_failure", "connectivity")


@dataclass
class DemoIncident:
    run: dict[str, Any]
    validations: list[ValidationResult]
    steps: list[dict[str, Any]]
    category: str
    severity: str
    status: str
    title: str
    description: str


@dataclass
class SeedReport:
    existing_before: int = 0
    catalogue_size: int = 0
    inserted: int = 0
    skipped_duplicates: int = 0
    final_total: int = 0
    inserted_ids: list[str] = field(default_factory=list)
    dry_run: bool = False


def _validation(cat: Category, j: int, exp: int, proc: int, bad: int, mins: int, src: str, pipeline: str) -> list[ValidationResult]:
    ref = f"synthetic-demo-{cat.key}-{j:02d}"
    if cat.key == "duplicate_records":
        return [ValidationResult(RULE_DUPLICATES, "fail", "0 duplicate source events", f"{bad} duplicate source events", ref, src)]
    if cat.key == "data_quality":
        rule = (RULE_TOTALS, RULE_FUTURE, RULE_TOTALS)[j % 3]
        wording = {RULE_TOTALS: ("order totals equal line-item sums", f"{bad} orders differ"),
                   RULE_FUTURE: ("no order timestamp in the future", f"{bad} orders dated in the future")}[rule]
        return [ValidationResult(rule, "fail", wording[0], wording[1], ref, src)]
    if cat.key == "schema_mismatch" and pipeline == "order_validation":
        return [ValidationResult(RULE_SCHEMA, "fail", "feed matches schema contract v1", _SCHEMA_DETAILS[j % len(_SCHEMA_DETAILS)], ref, src)]
    if cat.key == "stale_data":
        return [ValidationResult(RULE_FRESHNESS, "fail", "age <= 60 minutes", f"age {mins} minutes", ref, src)]
    if cat.key in ("missing_data", "row_count_variance"):
        return [ValidationResult(RULE_COMPLETENESS, "fail", f"{exp} rows", f"{proc} rows", ref, src)]
    if cat.key == "reconciliation_mismatch":
        return [ValidationResult(RULE_RECONCILIATION, "fail", "systems agree", f"{bad} records differ", ref, src)]
    return []


def build_catalogue() -> list[DemoIncident]:
    """The fixed list of synthetic incidents. Pure and deterministic."""
    items: list[DemoIncident] = []
    n = 0
    for ci, cat in enumerate(CATEGORIES):
        for j in range(cat.count):
            n += 1
            pipeline = cat.pipelines[j % len(cat.pipelines)]
            src = cat.sources[j % len(cat.sources)]
            started = FIRST_RUN_START - timedelta(hours=8 * n)
            batch = started.date()
            exp = {"order_ingestion": 25 + (j % 5) * 3, "order_validation": 6, "reporting_prep": 25 + (j % 4) * 2}[pipeline]
            bad = 3 + (j * 7 + ci) % 19
            if cat.key in _NO_ROWS:
                proc = 0
            else:
                proc = max(0, exp - 4 - (j * 3 + ci) % max(exp - 4, 1))
            mins = 75 + (j * 17 + ci * 5) % 160
            pct = round(100 * proc / exp) if exp else 0
            ctx = dict(src=src, pl=_PIPELINE_NAMES[pipeline], bad=bad, exp=exp, proc=proc, pct=pct, mins=mins,
                       detail=_SCHEMA_DETAILS[(j + ci) % len(_SCHEMA_DETAILS)])
            title = cat.titles[j % len(cat.titles)].format(**ctx) + f" ({batch:%d %b %Y}, {PIPELINE_ABBREV[pipeline]})"
            message = cat.messages[j % len(cat.messages)].format(**ctx)
            run_id = f"{DEMO_RUN_PREFIX}{PIPELINE_ABBREV[pipeline]}-{n:03d}"
            run = make_run_row(run_id=run_id, pipeline_id=pipeline, batch_date=batch, started_at=started,
                               duration_ms=900 + 137 * ((n * 7) % 40), status="failed", expected=exp, processed=proc,
                               error_code=CODE_PREFIX + cat.code, error_message=message[:480], scenario_id=None, trigger_type="scheduled")
            validations = _validation(cat, j, exp, proc, bad, mins, src, pipeline)
            steps = [step("INFO", "pipeline_runner", "synthetic_demo_run", SYNTHETIC_NOTE, {"synthetic": True}),
                     step("INFO", cat.component, "step_started", f"Starting {cat.component} for batch {batch}"),
                     *failure_steps(cat.component, cat.event, CODE_PREFIX + cat.code, message)]
            severity = _SEVERITY_CYCLES[ci % 3][j % 7]
            if cat.key == "stale_data" and mins > 180:
                severity = "critical"
            description = f"{message} Error {CODE_PREFIX}{cat.code} from {cat.component}. {SYNTHETIC_NOTE}"
            items.append(DemoIncident(run, validations, steps, cat.key, severity, _STATUS_CYCLE[(n + ci) % len(_STATUS_CYCLE)],
                                      title[:200], description[:990]))
    return items


def seed_demo_incidents(repo: Any, logs: RunLogStore | None = None, *, dry_run: bool = False) -> SeedReport:
    """Insert the catalogue entries that are not yet present. Never updates, deletes or resets anything."""
    if repo.get_state() is None:
        raise RuntimeError("The simulator has not been seeded with its baseline. Run `python -m scripts.db_seed` first.")
    catalogue = build_catalogue()
    report = SeedReport(existing_before=len(repo.list_incidents(1_000_000)), catalogue_size=len(catalogue), dry_run=dry_run)
    for item in catalogue:
        run = item.run
        if repo.run_exists(run["run_id"]):
            report.skipped_duplicates += 1
            continue
        report.inserted += 1
        if dry_run:
            continue
        evidence = (logs.relative_ref(run["pipeline_id"], run["run_id"]) if logs
                    else f"pipeline_runs/{run['pipeline_id']}/{run['run_id']}.jsonl")
        incident = make_incident(incident_id=repo.next_incident_id(), category=item.category, severity=item.severity, title=item.title,
                                 description=item.description, status=item.status, created_at=run["finished_at"], run=run,
                                 evidence_ref=evidence)
        repo.save_run(run, [v.to_row(run["run_id"], run["finished_at"]) for v in item.validations], incident)  # one transaction
        if logs is not None:
            logs.write(run["pipeline_id"], run["run_id"], build_run_events(run, item.steps, item.validations))
        report.inserted_ids.append(incident["incident_id"])
    report.final_total = report.existing_before + (0 if dry_run else report.inserted)
    return report
