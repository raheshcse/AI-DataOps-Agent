# The simulated operational environment (Phase 2)

> Status: **implemented**, unit-tested (in-memory), and **not yet verified against your SQL Server**
> until you complete the exercises in [learning-exercises.md](learning-exercises.md).
> No MCP server, LangGraph agent, VSL governance, OpenAI call or automatic recovery exists yet.

The project has no company environment, so this phase builds a small, realistic, **synthetic**
one: relational business tables, three pipelines that run against them, structured logs, and
six failure scenarios that each leave inspectable evidence. It is general-purpose and has
**no Datamart or analytics warehouse** - only ordinary operational tables.

## Components and responsibilities

| Layer | Module | Responsibility |
|---|---|---|
| Configuration | `app/config.py` | Env-based settings (DB target, auth, seed, freshness threshold). Secrets are `SecretStr`. |
| Connection | `app/db/connection.py` | Builds the ODBC connection string, masks secrets, maps failures to safe errors. `pyodbc` is imported lazily. |
| Schema | `app/db/schema.py` | The 12 tables (SQLAlchemy Core). |
| Storage | `app/db/repository.py` | `SqlRepository`: every query is a bound-parameter SQLAlchemy expression. |
| Interface | `app/simulation/ports.py` | `SimRepository` protocol: the simulation depends on this, not on SQL. |
| Data | `app/simulation/generator.py` | Deterministic baseline (fixed seed, fixed simulated clock). |
| Logic | `app/simulation/pipelines.py`, `validators.py`, `scenarios.py` | Pipelines, pure validation rules, scenario definitions/data builders. No SQL. |
| Orchestration | `app/simulation/service.py` | seed / reset / run pipeline / activate & restore scenarios / verify / freshness. |
| Logs | `app/simulation/events.py`, `runlog.py` | Builds events from what the run actually did; JSON Lines persistence. |
| Safety | `app/db/safety.py`, `app/db/permissions.py` | Reset guard; least-privilege grants. |
| API | `app/api/simulation.py` | Read-only `/sim/*` endpoints. |
| CLI | `scripts/*.py` | The commands you run (`python -m scripts.<name>`). |

### Data flow

```
db_init (admin)  -> database + 12 tables
db_seed (admin)  -> baseline rows + history run logs      (idempotent)
                       |
run_pipeline --> reads stg_order_feed / orders --> pipeline function returns what happened
                       |                                   |
                       v                                   v
        pipeline_runs + validation_results        build_run_events(): the JSONL log is derived
        (+ incident_records if the run FAILED)    from the same outcome, so it cannot disagree
                       |
         simulated clock advances 60 s after each run (sim_state.sim_now)
```

How runs, logs and incidents relate: every run has a `run_id`. `validation_results.run_id`
and `incident_records.run_id` point at it, the log file is named `<run_id>.jsonl`, and every
log line repeats `run_id` and `pipeline_id` (a **correlation id**), so evidence from SQL and
from files can be joined on one value. A run with status `failed` opens an incident
(`warning` runs do not). `incident_records.evidence_ref` names the log file.

## Database schema

All timestamps are DATETIME2(3) in **UTC**, returned by the app as timezone-aware datetimes.
Rows that scenarios inject carry a `scenario_id` so they are distinguishable from baseline data.

**Scenario attribution.** `pipeline_runs.scenario_id` lists *every* scenario active while the run
executed (comma-separated, `VARCHAR(200)`, sized for all six). `incident_records.scenario_id` is
different: it names the **one** scenario that caused the incident (`VARCHAR(40)`, longest id is 28
characters), chosen from the failure's error code (E1001→SCN-01, E2001→SCN-05, E2002→SCN-03,
E2003/E2004→SCN-02, E2005→SCN-06, E2006→SCN-04) and only if that scenario is active; otherwise NULL.
Leaving an earlier scenario active therefore never changes which scenario an incident is blamed on.

| Table | Purpose | Key relationships / constraints |
|---|---|---|
| `customers` | Synthetic customers (`example.test` emails) | PK `customer_id`; unique `customer_code` |
| `products` | Synthetic catalogue | PK `product_id`; unique `sku`; `unit_price >= 0` |
| `orders` | Order headers | FK `customer_id`; `source_event_id` is **indexed but deliberately not unique** (so duplicates are detectable); status CHECK; `total_amount >= 0`; `scenario_id` tag |
| `order_items` | Line items | FK `order_id`, FK `product_id`; unique (`order_id`,`line_no`); `quantity > 0` |
| `stg_order_feed` | Raw incoming feed read by ingestion (no FKs: untrusted input) | PK `feed_id`; index `batch_date` |
| `fx_order_feed_drifted` | **Fixture** for scenario 6: a copy of the feed with deliberate drift. Never used in normal runs | - |
| `pipeline_definitions` | The three pipelines | PK `pipeline_id` |
| `pipeline_runs` | One row per execution (status, timing, expected/processed counts, error code/message, scenario) | FK `pipeline_id`; status CHECK; index (`pipeline_id`,`started_at`) |
| `validation_results` | One row per rule per validation run (outcome, expected/observed, record reference, detail) | FK `run_id` |
| `incident_records` | Simulated incidents (category, severity, status, links to pipeline/run, evidence reference) | FK `pipeline_id`, FK `run_id` |
| `scenario_activations` | Which scenarios are active/restored and how they were introduced | PK `scenario_id` |
| `sim_state` | Single row: baseline version, seed, **simulated clock**, pending batch date | CHECK `id = 1` |

Relationships: `customers 1-* orders 1-* order_items *-1 products`;
`pipeline_definitions 1-* pipeline_runs 1-* validation_results`;
`pipeline_runs 1-* incident_records`.

## Baseline dataset (deterministic, seed `20260115`)

| Table | Rows |
|---|---|
| customers | 50 |
| products | 20 |
| orders | 173 (7 batches, 2026-01-07 .. 2026-01-13) |
| order_items | 448 |
| stg_order_feed | 203 (173 loaded + 30 for pending batch 2026-01-14) |
| fx_order_feed_drifted | 0 (schema only) |
| pipeline_definitions | 3 |
| pipeline_runs | 22 (7 days x 3 pipelines + 1 failed-and-retried ingestion on 2026-01-11) |
| validation_results | 42 (7 validation runs x 6 rules, all pass) |
| incident_records | 1 (INC-0001, resolved: the historical failed ingestion) |
| scenario_activations | 0 |
| sim_state | 1 |

The story: the simulated clock stands at **2026-01-15 01:00 UTC**, before the daily run that will
process batch **2026-01-14** ("yesterday's sales"). The last successful ingestion finished
2026-01-14 02:00 (23.0 h old) so the data is fresh against the 1500-minute threshold.
`tests/test_generator.py` pins these numbers; `scripts.db_verify` checks them in SQL Server.

Seeding is **idempotent**: a second `db_seed` reports "Already seeded" and changes nothing. If tables
hold unknown data (or were seeded with a different seed) it refuses instead of mixing data.

## Pipelines

| Pipeline | What it does | Records expected / processed |
|---|---|---|
| `order_ingestion` | Reads feed rows for a batch, loads orders + line items that are not already present (idempotent by `source_event_id`) | feed rows / rows handled |
| `order_validation` | Six rules (below) over the batch and the orders table | total orders / total orders |
| `reporting_prep` | Aggregates a loaded batch into metrics (count, revenue, distinct customers); stores nothing, so there is no mart | feed rows / orders present |

Validation rules: `batch_completeness` (E2001), `no_duplicate_source_events` (E2002),
`order_total_matches_line_items` (E2003), `order_timestamp_not_in_future` (E2004, warning),
`feed_schema_contract` (E2005), `data_freshness` (E2006). A run's error code is that of its first
failing rule. Duration is deterministic (e.g. ingestion = 1500 ms + 12 ms per record).

Run statuses: `success`, `warning` (finished but something is off, e.g. partial load), `failed`.

## Structured logs

`logs/pipeline_runs/<pipeline_id>/<run_id>.jsonl` - one JSON object per line (git-ignored; `logs/.gitkeep`
keeps the folder). Fields: `timestamp` (simulated clock, UTC), `level`, `component`, `pipeline_id`,
`run_id`, `event`, `message`, `error_code`, `scenario_id`, `data`. The closing event is always one of
`run_completed`, `run_completed_with_warnings`, `run_failed` and **is derived from the run record**, so a
failed run can never carry a success event (`tests/test_simulation.py::test_logs_correspond_to_run_status`).
Messages are passed through the redaction filter. Run ids are validated before touching the filesystem
(no path traversal). Read them with `python -m scripts.show logs RUN_ID [--level ERROR]`.

## The six scenarios

Activation is always explicit (`python -m scripts.scenario activate <key>`); setup and seeding never activate one.
Repeated activation is a no-op ("Already active"). Restoring twice is harmless.

| # | Key | ID | How it is introduced | Evidence |
|---|---|---|---|---|
| 1 | `pipeline-failure` | SCN-01-PIPELINE-FAILURE | While active, ingestion fails | Failed run `E1001`, ERROR log, open incident |
| 2 | `data-quality` | SCN-02-DATA-QUALITY | Injects 4 tagged orders (`SCN2-0001..4`): 3 with a wrong total, 1 future-dated | `order_total_matches_line_items` fails (E2003) naming orders; timestamp rule warns |
| 3 | `duplicate-records` | SCN-03-DUPLICATE-RECORDS | Injects 3 tagged copies of existing orders | `no_duplicate_source_events` fails (E2002) with event ids and both order ids; `scenario_activations.details_json` records how |
| 4 | `stale-data` | SCN-04-STALE-DATA | Advances the **simulated** clock 30 h (no waiting) | `data_freshness` fails (E2006): last success time, 1500-min threshold, computed age |
| 5 | `incomplete-processing` | SCN-05-INCOMPLETE-PROCESSING | While active, ingestion handles only 60% of rows | Run `warning` E1004 (18 of 30); validation `batch_completeness` fails (30 vs 18, shortfall 12) |
| 6 | `schema-mismatch` | SCN-06-SCHEMA-MISMATCH | While active, the schema rule inspects the drifted fixture table | `feed_schema_contract` fails (E2005): missing `currency`, unexpected `legacy_flag`, `order_total` string vs decimal |

The baseline schema is never altered. Scenario 6 reads real column metadata from SQL Server for the fixture table.

### Restoring

* `scenario restore <key>` stops the condition and deletes injected rows (2, 3), moves the clock back (4, only if no runs were
  recorded meanwhile - otherwise it refuses and points you to the reset), or just clears the flag (1, 5, 6). **Run history,
  validation results and incidents stay** as evidence.
* `db_reset` (guarded) returns *everything* to the clean baseline: [see below](#guarded-reset).
* Recovery from the effects of a scenario is manual and visible: e.g. after scenario 5, re-run `order_ingestion` to load the
  missing 12 rows. Nothing recovers automatically (out of scope for this phase).

Scenario 5 needs the pending batch to be unloaded, so run it on a fresh baseline.

## Guarded reset

`python -m scripts.db_reset --database dataops_sim --yes-delete-all-data` deletes every row in all 12 tables of the database
named by `DB_NAME` plus generated run logs, then re-seeds. It **refuses** unless all hold: the confirmation flag is given,
`--database` exactly equals `DB_NAME`, `DB_NAME` ends in `_sim` or `_test`, `DB_SERVER` is this machine, and `APP_ENV` is not
`production`. It prints the target and row count first. It is never called by startup, seeding, the API or ordinary tests,
and there is no API endpoint for it.

## API (read-only)

`GET /sim/db-status`, `/sim/pipelines`, `/sim/runs?pipeline_id=&limit=`, `/sim/runs/{run_id}`, `/sim/scenarios`.
Database trouble returns a generic `503`; `/ready` is unchanged and does **not** check SQL Server, so a ready application
never implies a healthy database. Use `/sim/db-status` (`connected` / `unreachable` / `misconfigured`, plus whether the
baseline is seeded).

## Concepts, briefly

* **Relational tables and foreign keys** - rows live in tables; a foreign key (e.g. `orders.customer_id`) makes the database refuse
  a row whose parent does not exist. `db_verify` also checks for orphans.
* **Connection strings and authentication** - the ODBC string names driver, server\instance, database and how to log in. *Windows
  (integrated)* auth proves who you are with your Windows session (no password in config); *SQL* auth uses a login/password
  that must come from the environment, never source code.
* **Idempotent setup and seeding** - running it twice leaves the same result as once; that is what makes setup safe to repeat.
* **Pipeline run status and execution metadata** - each run records when it ran, how long, how many records it expected vs
  handled, and an error code. That record is the first thing an operator (or agent) reads.
* **Structured logs and correlation IDs** - JSON lines with fixed fields instead of free text, and the same `run_id` in every
  record, so SQL rows and log lines about one run can be joined.
* **Data-quality validation** - explicit rules (completeness, totals, timestamps) that produce pass/warn/fail with the offending
  record references.
* **Data freshness** - how old the latest successful load is compared to a threshold. Old data can be wrong even if nothing "failed".
* **Duplicate detection** - group by the business key (`source_event_id`) and look for counts above one; a unique constraint would
  have hidden the problem by rejecting inserts, which is why the key is intentionally non-unique here.
* **Schema validation** - compare the columns and types a table actually has with the contract the pipeline expects.
* **Unit vs integration tests** - unit tests check logic in isolation and need no database; integration tests prove the same
  behaviour against the real SQL Server.

## Known limitations

* SQL Server behaviour is verified only by the integration tests you run; the in-memory test double proves logic, not T-SQL.
* `order_id`/`order_item_id` are assigned as `MAX+1` inside one transaction (fine for a single-user simulation, not for concurrent writers).
* The simulated clock is global and advances 60 s after every run; restoring `stale-data` after runs is refused by design.
* `reset` wipes and re-seeds in two transactions; if the re-seed were interrupted the tables would be empty - just run `db_seed` again.
* `db_create_app_login` requires Mixed Mode authentication; with Windows-only mode the app runs under your own (privileged) account.
