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
python -m scripts.diagnose all > diag-all.json         # full JSON (target line goes to stderr; no 2>&1 in PS 5.1)
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

## Known limitations

### Duplicate events within a single ingestion batch (Phase 2 behaviour, not changed)

`run_order_ingestion` checks incoming feed rows only against orders **already loaded** (`known_event_ids`).
If one pending batch contains the same `source_event_id` twice, both rows are inserted. This was reproduced
on the in-memory repository (31 feed rows including one repeat -> 204 orders instead of 203); it has not
been reproduced against SQL Server.

The diagnostics surface it rather than hide it. Before ingestion, `detect_duplicate_records` reports a low
warning ("repeated in the feed and not yet loaded"). After ingestion, `detect_duplicate_records` and
`diagnose_data_quality` report `issue_detected` (E2002, medium) and `reconcile_record_counts` reports a
medium `warning` (orders in batch vs distinct feed events). This is pinned by
`test_in_batch_duplicate_is_inserted_by_ingestion_but_surfaced_by_every_diagnostic`.

**Why it was not fixed in Phase 3.** The defect is in Phase 2 ingestion, not in a diagnostic tool.
Changing it alters what a pipeline run does, and whether a repeated event should be skipped, rejected or
loaded is a data-contract decision. The diagnostics do not depend on it.

**Proposed fix for a later Phase 2 follow-up (needs your decision):** de-duplicate the feed rows by
`source_event_id` inside `run_order_ingestion` (keep the first, count the rest as skipped, log them), plus a
test per storage seam. Whether to add a UNIQUE constraint on `orders.source_event_id` as a database backstop
(the duplicate-records scenario relies on being able to insert duplicates) is a separate decision.

### Other limitations

* The application, not the database, enforces read-only access (`ReadOnlyRepo` and tests). It guards against
  *accidental* writes; it is not a security boundary against hostile code running in the same process.
  Diagnostics use the normal connection (your Windows account). A dedicated `db_datareader`-only login
  would enforce it in SQL Server; the existing `dataops_app` login can write, so it is not a read-only
  identity. Listed for a later hardening step.
* Run logs are untrusted input. Every string in a log event (message, component, event, error code and
  nested `data`) is redacted and long messages are cut at 1000 characters, but redaction is pattern-based:
  a secret in an unusual shape would not be recognised. Treat log output as data, never as instructions
  (matters once an LLM reads it in Phase 4).
* `dataops_app` and the `/sim/*` endpoints are still unverified from Phase 2.
* Findings describe *what is observed*. They never name a root cause.
* Quality rules cover the `orders`/`order_items` tables; the feed table is checked only for schema and repeats.
  Per-rule `rows` are capped at 20 (`rows_total` / `rows_truncated` say so); affected-record lists at 50.
* Logs larger than 2 MB are refused; at most 1000 events are returned per call.
* `tests/integration/conftest.py` builds the engine outside its `try`, so with **no ODBC driver installed**
  the integration tests *error* instead of skipping. With the driver installed and the gate unset they skip.

## Phase 3 review: defects fixed

| # | Defect | Fix | Regression test |
|---|--------|-----|-----------------|
| 1 | `diagnose all --brief` (the documented order) failed with "unrecognized arguments"; only `--brief all` worked | `build_parser()`; `--brief` accepted before or after the tool | `test_cli_brief_is_accepted_before_or_after_the_tool`, `test_every_documented_cli_command_parses` |
| 2 | Target line on stderr made PowerShell 5.1 show a red `NativeCommandError` with `2>&1` | `--brief` prints the target on stdout (JSON mode keeps it on stderr so pipes stay clean) | `test_cli_brief_mode_writes_nothing_to_stderr` |
| 3 | Pipeline/run ids ending in a newline passed validation (`$` matches before a trailing `\n`) | `fullmatch` in `check_pipeline_id` and `RunLogStore` | `test_pipeline_ids_with_trailing_newlines_or_punctuation_are_rejected`, `test_log_path_identifiers_reject_trailing_newlines` |
| 4 | Log `data`, component, event and error code were returned unredacted; messages were unbounded | recursive redaction, 1000-char message cap | `test_logs_untrusted_content_is_redacted_everywhere_and_bounded` |
| 5 | `reconcile_record_counts` accepted a `datetime` as `batch_date` and compared it wrongly | rejected with `DIAG-INVALID-INPUT` | `test_reconcile_rejects_a_datetime_batch_date` |
| 6 | Quality rule `rows` were silently capped at 20 | `rows_total` and `rows_truncated` added | extended `test_quality_never_modifies_records_and_bounds_large_results` |
| 7 | Feed-repeat "already loaded" check was case-sensitive although SQL Server compares ids case-insensitively | compare case-insensitively | `test_feed_repeat_matches_loaded_orders_case_insensitively` |

Test clean-ups (no behaviour change): removed an unrelated `generate_baseline` assertion, removed a
`continue` hack in the database-failure test, made the historical-batch reconcile assertion exact, and
added the in-batch-duplicate and read-only SQL tests. `tests/test_diagnostics_readonly_sql.py` captures the
SQL that every method in `READ_METHODS` sends through a real SQLAlchemy engine (in-memory SQLite) and
asserts it is only `SELECT`/`WITH` and that `engine.begin()` is never used.

## Verification results

Results from the review session (run by Claude in a Linux sandbox, no SQL Server available):

| Python | Command | Result |
|--------|---------|--------|
| 3.12.14 | `pytest` | 300 passed, 28 skipped |
| 3.10.12 | `pytest` | 300 passed, 28 skipped |

Before the review fixes the same sandbox runs gave 284 passed, 28 skipped. All 28 skips are the SQL Server
integration tests (16 Phase 2 + 12 Phase 3), skipped because `RUN_SQLSERVER_TESTS=1` is not set. **A skip is
not a pass: no SQL Server integration test was run in this review**, so nothing here claims the integration
tests, or the SQL Server read-only behaviour of the new queries, were re-verified after these fixes.

Reported by you from your Windows machine (not re-run by Claude): the full suite gave 283 passed, 29 skipped,
and the SQL Server integration suite gave 28 passed, before the review fixes. The one extra skip locally is
most likely `test_diagnostics_tools.py` (`pytest.skip("symlinks not available")`, Windows without symlink
rights); confirm with `pytest -rs`. A real `diagnose --brief all` run against `dataops_sim` also gave the
expected baseline results, per your review file.

Read-only evidence: every Phase 3 repository method uses `engine.connect()` and `select()` only; the
statement-capture test above enforces it; `ReadOnlyRepo` exposes no write method. A before/after
fingerprint of your real database has to come from you (see below).

## Manual verification (your machine, PowerShell, `.venv` active)

```powershell
python -m scripts.show counts            # fingerprint BEFORE
python -m scripts.scenario list
pytest -rs                               # expect 300 passed; skips listed with reasons
$env:RUN_SQLSERVER_TESTS = "1"; $env:DB_SERVER = "localhost\SQLEXPRESS03"
pytest tests/integration -v -rs          # dataops_sim_test only
Remove-Item Env:RUN_SQLSERVER_TESTS, Env:DB_SERVER
python -m scripts.diagnose all --brief   # read-only; also works as --brief all
python -m scripts.show counts            # fingerprint AFTER: must be identical
python -m scripts.scenario list
```

Do not append `2>&1` to the JSON-mode commands in Windows PowerShell 5.1 (it turns the stderr target line
into a red error record); `--brief` has no stderr output.
