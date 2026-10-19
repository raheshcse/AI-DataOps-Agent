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
| 2 | Simulated operational environment: SQL Server schema, deterministic data, pipeline simulator, JSON Lines logs, six failure scenarios, guarded reset, read-only `/sim/*` endpoints | Done: you reported the SQL Server integration suite and baseline verification passed. `dataops_app` login and `/sim/*` endpoints remain unverified |
| 3 | Read-only diagnostic tools returning structured evidence ([docs/diagnostics.md](docs/diagnostics.md)) | Done and committed (`6eda154`); you reported the SQL Server integration suite passed |
| 4 | Custom read-only DataOps MCP server over the diagnostics ([docs/mcp-server.md](docs/mcp-server.md)) | Done and committed (`e3dd81d`); you reported 30 SQL Server integration tests passing |
| 5 | LangGraph + OpenAI investigation agent over the MCP server ([docs/agent.md](docs/agent.md)) | **Implemented; awaiting your review.** Offline mode and all tests run without a key; **live OpenAI behaviour is unverified** |
| 6 | VSL governance of protected actions: policy, gates, hash-chained ledger, audit CLI ([docs/vsl-governance.md](docs/vsl-governance.md)) | **Implemented; awaiting your review.** Gates a no-op stub only; no real recovery action |
| 7 | Human approval and controlled recovery: authenticated, exact-action, single-use approvals; allowlisted recovery behind the Phase 6 gates ([docs/phase7-human-approval-recovery.md](docs/phase7-human-approval-recovery.md)) | **Implemented; awaiting your review.** Recovery acts on the simulation only; authentication is fail-closed (a local dev verifier exists, no production identity provider) |
| 8+ | Persistence hardening, final demo | **Not started** |

**Not implemented yet:** a production identity provider, an operator UI, and any recovery against a real system (Phase 7 recovers the simulation only, behind human approval). No OpenAI request has ever been
made by this project; the agent's live mode exists but is unverified (see [docs/agent.md](docs/agent.md)). The
web application itself makes no OpenAI request.

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
| `OPENAI_API_KEY` | *(none)* | Secret; blank = missing. The Phase 5 agent also treats the `.env.example` placeholder as missing |
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

## Run the diagnostic tools (Phase 3)

```powershell
python -m scripts.diagnose all --brief      # read-only; see docs/diagnostics.md for every tool
```

## Run the MCP server (Phase 4)

```powershell
python -m scripts.mcp_check --list   # starts the server over stdio and lists its 8 read-only tools (no database)
python -m scripts.mcp_check          # also runs run_all_diagnostics against your configured database (read-only)
python -m app.mcp_server             # the server itself (stdio); an MCP client normally launches it
```

See [docs/mcp-server.md](docs/mcp-server.md) for the tools, client configuration and limits.

## Investigate an incident (Phase 5)

```powershell
python -m scripts.investigate --check-config                                  # key state; no network
python -m scripts.investigate "Yesterday's orders are missing" --offline      # deterministic, no key, no cost
python -m scripts.investigate "Yesterday's orders are missing"                # live OpenAI (needs a key; billable)
```

The agent reaches data only through the read-only MCP server. See [docs/agent.md](docs/agent.md) for the
architecture, API-key setup, how to read reports and confidence, limitations and the manual checklist.

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

1. Foundation (done) 2. Simulated operational environment (done) 3. Diagnostic tools (done)
4. Custom DataOps MCP server (done) 5. LangGraph + OpenAI agent (implemented) 6. VSL governance integration (implemented)
7. Human approval and controlled recovery 8. FastAPI, persistence and ticketing (GitHub Issues) 9. Testing and demonstration.
Future enhancements (not started): cloud integrations, Streamlit dashboard, expanded MCP ecosystem, benchmarking and hardening.
