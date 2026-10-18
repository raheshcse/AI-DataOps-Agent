# AI DataOps Agent with VSL Governance

An AI agent that investigates data pipeline failures from real tool evidence, recommends fixes, and
runs approved recovery under VSL governance. It runs against a **simulated** operational environment
(synthetic data, local services), is general-purpose rather than tied to any company, and does not
require a DataMart.

See [docs/architecture.md](docs/architecture.md) for the planned design and
[docs/simulation.md](docs/simulation.md) for the Phase 2 environment.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Foundation: config, FastAPI `/health` + `/ready`, log redaction, tests | Done (you confirmed on Windows) |
| 2 | Simulated operational environment: SQL Server schema, deterministic data, pipeline simulator, JSON Lines logs, six failure scenarios, guarded reset, read-only `/sim/*` endpoints | **Implemented; awaiting your SQL Server verification** ([exercises](docs/learning-exercises.md)) |
| 3+ | Diagnostic tools, MCP server, LangGraph + OpenAI agent, VSL governance, approval and recovery, persistence, final demo | **Not started** |

**Not implemented yet:** diagnostic tools, MCP server, LangGraph agent, VSL integration, OpenAI calls, automatic
recovery. The application makes no OpenAI request.

## Prerequisites

* Python 3.10 or newer, 64-bit (developed on 3.12, tested on 3.10 and 3.12)
* Git
* For Phase 2: a local SQL Server instance, Microsoft ODBC Driver 17/18 for SQL Server - see
  [docs/windows-setup.md](docs/windows-setup.md)
* An OpenAI API key only for `/ready` to report ready; no test needs one

## Setup

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # first time only; otherwise append the new variables to your .env
```

macOS / Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

If PowerShell blocks activation: `Set-ExecutionPolicy -Scope Process RemoteSigned` (this session only).

## Configuration

Environment variables or a git-ignored `.env` (real environment variables override `.env`). Never commit `.env`.
Invalid values make `/ready` report `not_ready` with the field names only; values are never echoed.

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | *(none)* | Secret; blank = missing |
| `OPENAI_MODEL` | `gpt-4o-mini` | Placeholder; confirm availability |
| `APP_ENV` | `development` | `development` / `test` / `production` (`production` blocks the reset) |
| `LOG_LEVEL` | `INFO` | `DEBUG` ... `CRITICAL` |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | used by `python -m app.main` |
| `DB_SERVER` | `localhost` | `server` or `server\INSTANCE` (yours: `localhost\SQLEXPRESS03`) |
| `DB_NAME` | `dataops_sim` | must end in `_sim` or `_test` for init/reset |
| `DB_AUTH_MODE` | `windows` | `windows` (integrated) or `sql` |
| `DB_USER`, `DB_PASSWORD` | *(none)* | only for `sql` mode; secret |
| `DB_DRIVER` | `ODBC Driver 18 for SQL Server` | must match an installed driver |
| `DB_ENCRYPT` | `true` | |
| `DB_TRUST_SERVER_CERTIFICATE` | `false` | set `true` for **local** self-signed certs only |
| `DB_LOGIN_TIMEOUT_SECONDS` | `5` | 1-60 |
| `SIM_SEED` | `20260115` | baseline seed |
| `FRESHNESS_THRESHOLD_MINUTES` | `1500` | stale if the last successful load is older |
| `SIM_LOG_DIR` | `logs` | generated run logs (git-ignored) |

## Run the application

```powershell
uvicorn app.main:app --reload          # or: python -m app.main
```

* `GET /health` - liveness. `GET /ready` - application configuration only (it does **not** check SQL Server).
* `GET /sim/db-status`, `/sim/pipelines`, `/sim/runs`, `/sim/runs/{run_id}`, `/sim/scenarios` - read-only; see
  [docs/simulation.md](docs/simulation.md). Interactive docs at `/docs`.

## Run the simulated environment

All commands are `python -m scripts.<name>` from the project root with `.venv` active.

| Command | Does | Needs |
|---|---|---|
| `check_prereqs` | read-only diagnosis of driver, connection, auth mode | - |
| `db_init` | create database + tables (idempotent, never deletes) | admin (your Windows account) |
| `db_create_app_login [--login dataops_app]` | optional restricted SQL login (password via hidden prompt) | admin, Mixed Mode |
| `db_seed` | seed the deterministic baseline (idempotent) | admin |
| `db_verify [--integrity-only]` | check tables, relationships, baseline counts | read |
| `run_pipeline --pipeline <id> [--batch-date YYYY-MM-DD]` | run `order_ingestion`, `order_validation` or `reporting_prep` | app |
| `scenario list \| activate <key> \| restore <key> \| restore-all` | the six failure scenarios | app |
| `show runs \| run <id> \| logs <id> \| incidents \| freshness \| counts` | inspect evidence | read |
| `db_reset --database <DB_NAME> --yes-delete-all-data` | guarded full reset to the clean baseline | admin |

Typical first session: `check_prereqs` -> `db_init` -> `db_seed` -> `db_verify` -> `run_pipeline --pipeline order_ingestion`
-> `show logs <run id>`. The guided checklist with expected output is [docs/learning-exercises.md](docs/learning-exercises.md).

## Test

```powershell
pytest                                     # unit tests; SQL Server integration tests are skipped
$env:RUN_SQLSERVER_TESTS='1'; $env:DB_SERVER='.\SQLEXPRESS03'
pytest -m integration -v                   # needs a local SQL Server; uses database dataops_sim_test only
```

Unit tests need no database and no OpenAI key and ignore your real `.env`. A skipped integration test is **not** a pass.

## Troubleshooting

See [docs/windows-setup.md](docs/windows-setup.md) section 6 for database errors. General:

* `/ready` is 503 with `openai_api_key: missing` - set `OPENAI_API_KEY` and restart.
* `/ready` shows `invalid_fields` - the named variable is invalid (value deliberately not shown).
* `.env` changes ignored - restart the process; run from the project root.
* `String or binary data would be truncated ... incident_records ... scenario_id` - fixed in the code (an incident now names one scenario). No schema change is needed; see `docs/simulation.md` ("Scenario attribution"). If you still see it, run `python -m scripts.scenario list` to see which scenarios are active.
* `ModuleNotFoundError: app` / `scripts` - run from the project root with the venv active.

## Planned phases

1. Foundation (done) 2. Simulated operational environment (implemented) 3. Diagnostic and operational tools
4. Custom DataOps MCP server 5. LangGraph + OpenAI agent 6. VSL governance integration
7. Human approval and controlled recovery 8. FastAPI, persistence and ticketing (GitHub Issues) 9. Testing and demonstration.
Future enhancements (not started): cloud integrations, Streamlit dashboard, expanded MCP ecosystem, benchmarking and hardening.
