# Phase 3 - DataOps diagnostic tools

Read-only tools that investigate the simulated operational environment and return **structured,
evidence-based findings**. A later phase will expose them to an AI agent through MCP; in Phase 3 they are
plain Python functions plus a CLI. Nothing here calls OpenAI, changes data, repairs anything or starts a
pipeline.

## Architecture and data flow

```
scripts/diagnose.py (CLI, prints JSON)         future: MCP tool wrappers (Phase 4)
            \                                      /
             v                                    v
      app/diagnostics/service.py  DiagnosticsService    <- the one public entry point
        - validates input, adds simulated-clock timestamp, converts every failure into an `error` result
        - holds a ReadOnlyRepo (allow-list of read methods; writes raise AttributeError)
            |
            v
      app/diagnostics/tools/*.py   one pure function per tool -> DiagnosticResult
            |   reuses: validators (check_order_totals, check_future_orders, check_no_duplicates,
            |           check_freshness, check_schema), schema_contract.diff_schema, SimClock,
            |           RunLogStore, feed_table_for, the freshness threshold
            v
      SimRepository (SqlRepository on SQL Server | InMemoryRepo in unit tests) + JSON Lines run logs
```

* Diagnostic logic never prints; presentation lives in `scripts/diagnose.py`.
* Time comes from the simulated clock (`sim_state`), never the machine clock, so results are reproducible.
* `scenario_id` is deliberately **not** in any result: it is simulation ground truth, and an agent should
  reach conclusions from operational evidence (runs, logs, counts, records), not from the answer key.
  (You can still see it with `python -m scripts.show` and `scripts.scenario list`.)
* Additive Phase 2 changes: `SimRepository` gained three read-only queries (`duplicate_order_details`,
  `duplicate_feed_events`, `order_quality_scan`) and a `status` filter on `list_runs`;
  `RunLogStore.locate()`; `pipelines.feed_table_for()` (shared so the schema tool checks the same table
  `order_validation` does). Existing behaviour and tests are unchanged.

## The common result (Tool 8)

Every tool returns a `DiagnosticResult` (`.to_dict()` is plain JSON):

| Field | Meaning |
|---|---|
| `diagnostic_name` | Tool name, e.g. `reconcile_record_counts` |
| `status` | `ok` (check passed) · `issue_detected` (a rule failed) · `warning` · `error` (the diagnostic could not run) |
| `severity` | `info`, `low`, `medium`, `high`, `critical`, or `unknown` (only for `error`) |
| `summary` | One factual sentence |
| `evidence` | List of fact objects (each has a `type`): row ids, counts, log lines, values read from the database |
| `affected_records` | Identifiers such as `order:204`, `run:RUN-...`, `column:currency` (max 50; `notes` says if cut) |
| `observed_value` / `expected_value` | What was found / what the rule requires |
| `error_code` | The pipeline or rule code when the finding corresponds to one (`E1001`, `E2002` ...), or a `DIAG-*` code |
| `recommended_next_step` | A *suggestion* for a human or agent; never presented as a confirmed cause |
| `parameters`, `notes`, `evaluated_at` | The arguments used, caveats, simulated time of evaluation |

`DiagnosticReport` (from `run_all`) rolls results up: `overall_status` (error > issue > warning > ok),
`highest_severity`, counts per status. It states no root cause.

Diagnostic error codes: `DIAG-INVALID-INPUT`, `DIAG-UNKNOWN-PIPELINE`, `DIAG-UNKNOWN-RUN`, `DIAG-NOT-SEEDED`,
`DIAG-DB-ERROR`, `DIAG-INTERNAL`, `DIAG-LOG-MISSING`, `DIAG-LOG-MALFORMED`, `DIAG-LOG-TOO-LARGE`,
`DIAG-INVALID-TIMESTAMP`. Database/driver exception text is never copied into a result (it can contain server
names); you get a generic message plus `python -m scripts.check_prereqs` as the next step.

## The tools

| CLI | Service method | What it answers |
|---|---|---|
| `status` | `get_pipeline_status(pipeline_id, status, limit)` | Which run was last for each pipeline, did it succeed, recent history (filterable) |
| `logs RUN_ID` | `inspect_pipeline_logs(run_id, max_events)` | What the run's JSON Lines log says; errors/warnings highlighted, nothing removed; bad lines listed |
| `duplicates` | `detect_duplicate_records()` | Is any `source_event_id` in more than one order; are the copies identical or conflicting; feed repeats |
| `quality` | `diagnose_data_quality()` | Per-rule results: totals vs line items, future timestamps, duplicates and their overcount, blank required values, negative amounts, invalid status |
| `freshness` | `check_data_freshness(pipeline_id)` | Age of the last successful ingestion vs the threshold, on the simulated clock |
| `reconcile` | `reconcile_record_counts(batch_date)` | Feed rows vs run metadata vs log counters vs orders present, with differences quantified |
| `schema` | `validate_schema_contract(table)` | Missing / unexpected / mistyped columns against the feed contract, from real database metadata |
| `all` | `run_all(batch_date)` | All of the above plus the latest run's log, as a report |

Notes on judgement calls (so you can disagree with them):

* **Duplicates** use `orders.source_event_id` (one order per source event). Orders that merely look alike
  (same customer/amount/time) with different event ids are separate business events and are not reported.
  Ids are compared case-insensitively, as SQL Server does.
* **Reconciliation** treats a batch with no recorded ingestion run as *pending* (low warning), not a failure.
  Rows skipped because they were already loaded are reported as idempotent skips, not discrepancies. The
  project has no "rejected" stage, so that comparison is reported as `not_available`.
* **Schema** with no `--table` checks the table `order_validation` inspects (the real feed, or the drifted
  fixture when schema drift is being simulated); `--table` overrides it with either allowed table.
* **Freshness** returns `error` (not a guess) for a missing, timezone-less or future `finished_at`.
* **Read-only** is enforced in the application (`ReadOnlyRepo`) and checked by tests, not by the database
  login (see Limitations).

## Run them (PowerShell, project root `C:\Rahesh\AI Agents\AI DataOps Agent`, `.venv` active)

```powershell
python -m scripts.diagnose all --brief                 # one line per tool
python -m scripts.diagnose all > diag-all.json         # full JSON (target line goes to stderr)
python -m scripts.diagnose status
python -m scripts.diagnose status --pipeline order_ingestion --status failed --limit 5
python -m scripts.diagnose logs RUN-ING-20260111-020000
python -m scripts.diagnose duplicates
python -m scripts.diagnose quality
python -m scripts.diagnose freshness
python -m scripts.diagnose reconcile --batch-date 2026-01-14
python -m scripts.diagnose schema

# read the evidence
python -m scripts.diagnose quality | ConvertFrom-Json | Select-Object -ExpandProperty evidence
(python -m scripts.diagnose reconcile | ConvertFrom-Json).evidence | Where-Object type -eq comparison | Format-Table name, outcome, difference
```

Exit codes: `0` ok/warning, `1` an issue was detected, `2` the diagnostic could not run. An exit code of 1
is a *finding*, not a failure of the tool.

Healthy baseline expectation: every tool `ok` except `reconcile`, which is a low `warning` (the 2026-01-14
batch is pending: 30 feed events, no ingestion run yet). `freshness` shows `age_minutes: 1380.0`.

## Tests

```powershell
pytest tests/test_diagnostics_core.py tests/test_diagnostics_tools.py -v   # unit tests, no database
pytest                                                                     # everything; integration tests skip

$env:RUN_SQLSERVER_TESTS = "1"; $env:DB_SERVER = "localhost\SQLEXPRESS03"
pytest tests/integration/test_diagnostics_sqlserver.py -v -rs              # SQL Server, dataops_sim_test only
pytest tests/integration -v -rs                                            # Phase 2 + Phase 3 integration
Remove-Item Env:RUN_SQLSERVER_TESTS, Env:DB_SERVER
```

A skipped test is not a pass. The integration tests never touch `dataops_sim`; compare
`python -m scripts.show counts` and `python -m scripts.scenario list` before and after to confirm.

## Limitations

* The application, not the database, enforces read-only access. Diagnostics use the normal connection (your
  Windows account). A dedicated `db_datareader`-only login would enforce it in SQL Server; the existing
  `dataops_app` login can write, so it is not a read-only identity. Listed for a later hardening step.
* `dataops_app` and the `/sim/*` endpoints are still unverified from Phase 2.
* Findings describe *what is observed*. They never name a root cause; correlating them is the later agent's job.
* Quality rules cover the `orders`/`order_items` tables; the feed table is checked only for schema and repeats.
* Logs larger than 2 MB are refused; at most 1000 events are returned per call.

## Known Limitations and Test Notes

### Duplicate events within a single ingestion batch

The current ingestion process can insert duplicate event IDs within the same pending batch because its duplicate check compares incoming events with previously loaded orders, rather than also detecting duplicates within the current batch. The diagnostic tools can identify duplicate records, but the ingestion-side prevention fix is deferred.

**Impact:** Duplicate records may enter the database if a batch contains repeated event IDs.

**Follow-up:** Add duplicate detection within the pending batch and test that duplicate events are rejected or handled according to the ingestion contract.

### Skipped test

The local full test suite completed with 283 passed and 29 skipped. The SQL Server integration suite completed with 28 passed.

Document the skipped unit test's name and reason based on the actual pytest skip report. Confirm whether the skip is expected and whether the test should be enabled or corrected in a future change.
