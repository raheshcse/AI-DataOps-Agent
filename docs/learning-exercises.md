# Hands-on learning checklist (Phase 2)

Status key: **Pending** = you have not done it yet; **Evidence** = something you sent shows part of it, but you should still
complete the step yourself. Nothing here is marked *Done* - only you can complete these. Tick the boxes as you go.

Work in PowerShell in the project folder with `.venv` active. Scenario exercises are easiest if you return to the baseline between
them (`db_reset`, shown at the end of each). Expected values come from the deterministic simulation; **your SQL Server run should
match them exactly** - if it does not, that difference is a finding worth understanding.

## A. Prerequisites

| # | Task | Command | Expected | Status |
|---|---|---|---|---|
| A1 | SQL Server installed and running | `Get-Service 'MSSQL*'` | `MSSQL$SQLEXPRESS03` Running | **Evidence** (your earlier output showed it running) |
| A2 | Instance and authentication method | `sqlcmd -S .\SQLEXPRESS03 -E -C -Q "SELECT @@SERVERNAME, SERVERPROPERTY('IsIntegratedSecurityOnly')"` | server name `...\SQLEXPRESS03`; `1` = Windows-only, `0` = Mixed | **Pending** (the earlier attempt used `localhost`, which is the default instance) |
| A3 | ODBC driver installed | `Get-OdbcDriver \| Where-Object Name -like '*SQL Server*'` | `ODBC Driver 18 for SQL Server` 64-bit | **Evidence** (17 and 18, 64-bit present) |
| A4 | Python is 64-bit | `python -c "import struct;print(struct.calcsize('P')*8)"` | `64` | **Evidence** (3.10.11, 64-bit) |
| A5 | Install new dependencies | `pip install -r requirements.txt` | installs `sqlalchemy`, `pyodbc` with no errors | **Pending** |

## B. Configure and connect

| # | Task | Command / action | Expected | Status |
|---|---|---|---|---|
| B1 | Configure `.env` safely | Append the Phase 2 variables (see [windows-setup.md](windows-setup.md) section 4); then `git check-ignore -v .env` | prints the `.gitignore` rule for `.env` | **Pending** |
| B2 | Diagnose connectivity | `python -m scripts.check_prereqs` | all `[ OK ]`, ends without `[FAIL]`; shows your auth mode and whether `dataops_sim` exists | **Pending** |
| B3 | (Optional, Mixed Mode only) restricted login | `python -m scripts.db_create_app_login`, then edit `.env`: `DB_AUTH_MODE=sql`, `DB_USER=dataops_app`, `DB_PASSWORD=<yours>` | "Created login ... Granted ..." | **Pending / skip if Windows-only** |

## C. Initialise, seed, verify

| # | Task | Command | Expected | Status |
|---|---|---|---|---|
| C1 | Create database and tables | `python -m scripts.db_init` | "Created database 'dataops_sim'" and 12 tables; re-running says everything exists | **Pending** |
| C2 | Seed baseline | `python -m scripts.db_seed` | "Seeded baseline: customers=50, products=20, orders=173, order_items=448, ..."; running it again prints "Already seeded" | **Pending** |
| C3 | Verify tables, relationships, counts | `python -m scripts.db_verify` | every line `[PASS]`, final line `BASELINE HEALTHY` | **Pending** |
| C4 | Look at the tables yourself | `sqlcmd -S .\SQLEXPRESS03 -E -C -d dataops_sim -Q "SELECT name FROM sys.tables ORDER BY name"` and `... -Q "SELECT OBJECT_NAME(parent_object_id) AS child, OBJECT_NAME(referenced_object_id) AS parent FROM sys.foreign_keys ORDER BY 1"` | the 12 tables; FK pairs such as `orders -> customers`, `order_items -> orders/products` | **Pending** |
| C5 | See idempotency | run C2 again, then `python -m scripts.show counts` | counts unchanged | **Pending** |

## D. A successful pipeline run

| # | Task | Command | Expected | Status |
|---|---|---|---|---|
| D1 | Run ingestion | `python -m scripts.run_pipeline --pipeline order_ingestion` | `Run RUN-ING-20260115-010000: SUCCESS`, expected=30 processed=30, duration 1860 ms | **Pending** |
| D2 | Inspect that run in SQL Server | `sqlcmd ... -d dataops_sim -Q "SELECT TOP 2 run_id,status,records_expected,records_processed,duration_ms FROM pipeline_runs ORDER BY started_at DESC"` | newest row is the run from D1 | **Pending** |
| D3 | Inspect its structured log | `python -m scripts.show logs RUN-ING-20260115-010000`; then open `logs\pipeline_runs\order_ingestion\RUN-ING-20260115-010000.jsonl` | events `run_started, feed_connected, feed_read, orders_loaded, run_completed`, all INFO; each JSON line has the same `run_id` | **Pending** |
| D4 | Validate and report | `... --pipeline order_validation` then `... --pipeline reporting_prep` | validation SUCCESS with six `PASS` rules (203 orders); reporting SUCCESS 30/30 | **Pending** |
| D5 | Compare record and log | `python -m scripts.show run RUN-VAL-20260115-010101` | six validation rows, all `pass` | **Pending** |

Return to baseline before the scenarios: `python -m scripts.db_reset --database dataops_sim --yes-delete-all-data`

## E. Failure scenarios

For each, start from a clean baseline. `<RUN>` is the run id printed by `run_pipeline`.

| # | Scenario | Steps | Evidence to find and understand |
|---|---|---|---|
| E1 | **Pipeline failure** | `python -m scripts.scenario activate pipeline-failure` -> `python -m scripts.run_pipeline --pipeline order_ingestion` | Run **FAILED**, error `E1001` "Source feed connection timed out after 30s", 0 of 30; `show logs <RUN>` ends with an ERROR `run_failed`; `show incidents` lists **INC-0002** `pipeline_failure` open; SQL: `SELECT error_code,error_message FROM pipeline_runs WHERE status='failed' ORDER BY started_at DESC`. Restore: `scenario restore pipeline-failure`. |
| E2 | **Data quality** | ingestion -> `scenario activate data-quality` -> `run_pipeline --pipeline order_validation` | Rule `order_total_matches_line_items` **fail** (E2003) naming `order 204 (SCN2-0001): total 650.92 vs items 640.92` (and 205, 206); `order_timestamp_not_in_future` **warn** for order 207. SQL: `SELECT o.order_id,o.total_amount,SUM(i.line_total) FROM orders o JOIN order_items i ON i.order_id=o.order_id GROUP BY o.order_id,o.total_amount HAVING o.total_amount<>SUM(i.line_total)`. Restore removes the 4 tagged orders. |
| E3 | **Duplicate records** | ingestion -> `scenario activate duplicate-records` -> validation | Rule `no_duplicate_source_events` **fail** (E2002): `EVT-20260107-0004 x2 (orders 2,205)`, `...0008 x2 (3,206)`, `...0017 x2 (1,204)`. SQL: `SELECT source_event_id,COUNT(*) FROM orders GROUP BY source_event_id HAVING COUNT(*)>1`. Also read `SELECT details_json FROM scenario_activations` to see how it was introduced. |
| E4 | **Stale data** | ingestion -> `scenario activate stale-data` -> `python -m scripts.show freshness` -> validation | Freshness **STALE**: last success `2026-01-15T01:00:01.860`, threshold 1500 min, age **1801.0** min; validation fails `data_freshness` (E2006). Understand: nothing "failed", the data is simply too old. Restore only works before further runs; otherwise `db_reset`. (On a fresh baseline without ingesting first, the age after activating is 3180 min.) |
| E5 | **Incomplete processing** | (fresh baseline!) `scenario activate incomplete-processing` -> ingestion -> validation -> `run_pipeline --pipeline reporting_prep` | Ingestion **WARNING** `E1004`: expected **30**, processed **18**; validation `batch_completeness` **fail**: expected 30, observed 18, shortfall **12**, with 5 sample event ids; reporting WARNING E3002. Then `scenario restore incomplete-processing` and re-run ingestion: it loads the missing 12 and validation passes. SQL: `SELECT COUNT(*) FROM stg_order_feed f WHERE f.batch_date='2026-01-14' AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.source_event_id=f.source_event_id)` (12 before the re-run, 0 after). |
| E6 | **Schema mismatch** | ingestion -> `scenario activate schema-mismatch` -> validation | `feed_schema_contract` **fail** (E2005): `missing: currency | unexpected: legacy_flag | type mismatch: order_total: expected decimal, observed string`. SQL: `SELECT COLUMN_NAME,DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME IN ('stg_order_feed','fx_order_feed_drifted') ORDER BY TABLE_NAME,ORDINAL_POSITION`. |

Mark each only after you have found the evidence yourself.

## F. Restore and re-verify

| # | Task | Command | Expected | Status |
|---|---|---|---|---|
| F1 | Light restore of one scenario | `python -m scripts.scenario restore <key>` / `restore-all` | message describing what was removed; history stays | **Pending** |
| F2 | Full baseline restore | `python -m scripts.db_reset --database dataops_sim --yes-delete-all-data` | prints target and "About to delete N rows", then "Seeded baseline" | **Pending** |
| F3 | Try the guard | `python -m scripts.db_reset --database dataops_sim` (no confirmation flag) and `... --database master --yes-delete-all-data` | both **refuse** with an explanatory message and exit code 3 | **Pending** |
| F4 | Baseline healthy again | `python -m scripts.db_verify` | `BASELINE HEALTHY` | **Pending** |

## G. Automated tests

| # | Task | Command | Expected | Status |
|---|---|---|---|---|
| G1 | Unit tests (no SQL Server) | `pytest` | `179 passed, 14 skipped` (the 14 are the integration tests) on the machine I used; yours should show 0 failed | **Pending** |
| G2 | Integration tests (real SQL Server) | `$env:RUN_SQLSERVER_TESTS='1'; $env:DB_SERVER='.\SQLEXPRESS03'; pytest -m integration -v` | `14 passed`. They create/use database **`dataops_sim_test`** only (never `dataops_sim`) with Windows authentication | **Pending - never run by me** |
| G3 | Interpret a failure | If one fails read the first `E` line: connection errors = see [windows-setup.md](windows-setup.md) section 6; `AssertionError` = real behavioural difference between SQL Server and the in-memory model - report it | - | **Pending** |

Unset the integration variables afterwards (`Remove-Item Env:RUN_SQLSERVER_TESTS`) so a plain `pytest` skips them again.
