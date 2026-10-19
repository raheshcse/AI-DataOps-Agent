# Phase 9: end-to-end validation and cloud infrastructure foundations

**Status: implemented, awaiting your review. Nothing is committed, pushed, published or deployed.** No cloud resource exists, no image
has been pushed, and **no container build or GitHub Actions run has been executed yet** (Docker and Actions were not available to the
author; see [What was and was not run](#what-was-and-was-not-run)).

## Repository state found at the start

* Branch `main`, HEAD `0289e60` "Add API persistence and ticketing"; `origin/main` is the same commit. There was **no remote divergence**,
  so the earlier `d4b130e` push rejection had already been resolved by you. Nothing was reset, rebased, overwritten or force-pushed.
* The working tree reported 39 modified files. They differ from HEAD **only in line endings** (`git diff --ignore-space-at-eol` is empty),
  not in content. I left them alone: normalising them is a separate decision (a `.gitattributes` would settle it).
* No Dockerfile, `.dockerignore`, Compose file or CI existed.

## Gap analysis

| Requirement | Already existed | Added in Phase 9 |
|---|---|---|
| E2E 1 investigation + reload | `test_api_e2e` (1, 7), persistence tests | reused |
| E2E 2 diagnostic failure | agent/MCP unit tests, unreachable-MCP API test | **failing tool through the API** (`test_scenario_2*`) |
| E2E 3 ticket idempotency/timeout | `test_api_github`, `test_api_e2e` (2, 6) | **retry after a restart** (`test_scenario_3_*`) |
| E2E 4 approval needed, no client-supplied approval | `test_api_governance` | reused |
| E2E 5 VSL denial recorded; cannot clear | suspension tests | **evidence on the ledger, audit flag, no clearing route** |
| E2E 6 ledger/gate failure fails closed | ledger/gate fault tests | **tampered ledger via the API; unreadable ledger** |
| E2E 7 restart | pending approval, stale claim | **interrupted investigation; consumed+uncertain never reused after restart** |
| E2E 8 DB safety | `scripts.db_verify`, `tests/integration/*` (dataops_sim_test only) | not runnable here: **NOT RUN** |
| Container | none | `Dockerfile`, `.dockerignore`, `docker-compose.yml` |
| Config | `Settings`, `build_services` | **production start-up validation** (`app/core/startup.py`) |
| CI | none | `.github/workflows/ci.yml` |
| Docs | Phase 7/8 docs | this document, README, architecture, `.env.example` |

## End-to-end scenarios and actual outcomes

All scenarios run through the real FastAPI stack (mounted as in `app.main`): real SQLite operations/approval stores, the real
hash-chained ledger, the real Phase 7 workflow and VSL gates, the real offline agent over real MCP tools. The recovery side effect is the
deterministic in-memory simulation and GitHub is the offline `httpx.MockTransport` fake. They are **not** live-GitHub or SQL Server tests.

| # | Scenario | Result | Where |
|---|---|---|---|
| 1 | Incident, investigation, report, reload | PASSED | `test_api_e2e::test_e2e_1`, `_7` |
| 2 | Diagnostic failure is recorded, never invented | PASSED | `test_phase9_e2e::test_scenario_2*` |
| 3 | Ticket creation, retry, timeout, restart | PASSED | `test_api_e2e::_2`, `_6`; `test_phase9_e2e::test_scenario_3` |
| 4 | Recovery needs an authorised human; no client `approved`/`approver` | PASSED | `test_api_governance`, `test_api_e2e::_3`, `_4` |
| 5 | VSL denial: not executed, recorded, API cannot clear it | PASSED | `test_api_e2e::_5`, `test_phase9_e2e::test_scenario_5` |
| 6 | Ledger/gate failure fails closed | PASSED | `test_api_governance`, `test_phase9_e2e::test_scenario_6*` |
| 7 | Restart: durable records, interrupted work, consumed approval not reused or retried | PASSED | `test_api_e2e::_7`, `test_phase9_e2e::test_scenario_7*` |
| 8 | `scripts.db_verify` and SQL Server integration (`dataops_sim_test` only) | **NOT RUN** here | see below |

What scenario 2 shows: when every diagnostic tool raises, the investigation completes with `report_status = failed`, stop reason
`no_new_evidence`, **no evidence references**, and a recovery request is refused (422). An unreachable MCP target becomes a `failed`
investigation with no report. Raw exception text and addresses never reach the response. Note the two fields mean different things:
`status` is the run state (did the run finish?), `report_status` is the agent's finding.

Scenario 8: `python -m scripts.db_verify` was attempted in the authoring sandbox and exited 2 (`The SQL Server Python driver (pyodbc / ODBC
runtime) is not available`; the sandbox has neither the ODBC runtime nor SQL Server). It is read-only and was never run against anything. The
44 SQL Server integration tests are **skipped, not passed**. Run them yourself (commands below).

## Container

```
docker build -t ai-dataops-agent:local .
# only if you need SQL Server from inside the container (you accept Microsoft's licence terms by passing Y):
docker build --build-arg INSTALL_MSSQL_DRIVER=true --build-arg ACCEPT_MSODBC_EULA=Y -t ai-dataops-agent:local .
```

* Multi-stage: dependencies are installed from the **unchanged** `requirements.txt` (`pip check` runs in the build). `git` is present only
  in the build stage (the VSL adapter installs from a pinned git commit). The runtime image has only `unixodbc`; the Microsoft ODBC driver
  is opt-in.
* Runs as non-root uid 10001. `/data` is the only writable location and is a `VOLUME`. Nothing local is copied in: `.dockerignore` excludes
  `.env*`, `.git`, `.venv`, `data/`, `logs/`, databases, ledgers, tests and docs.
* Secrets are **never** in the image: pass `APPROVAL_SIGNING_KEY`, `GITHUB_TOKEN`, `OPENAI_API_KEY`, `DB_PASSWORD` at run time.
* `ENV` defaults (non-secret): `APP_ENV=production`, `DATABASE_URL=sqlite:////data/operations.sqlite3`,
  `APPROVAL_STORE_PATH=/data/approvals.sqlite3`, `GOVERNANCE_LEDGER_PATH=/data/governance_ledger.jsonl`, `SIM_LOG_DIR=/data/logs`.
  **`DB_NAME` is deliberately not defaulted**; production start-up refuses to run without it.
* One uvicorn worker, exec-form `CMD` (graceful SIGTERM). `HEALTHCHECK` calls `/api/v1/health` (liveness; touches no dependency).
  Use `/api/v1/ready` for readiness.
* `docker-compose.yml`: one service, published on `127.0.0.1:8000` only, read-only root filesystem, all capabilities dropped, secrets passed
  through from your shell (no values in the file), one named volume. No database server is included: SQL Server stays on your machine.

Local trial (no authentication is possible in production mode by default, so a trial uses development mode and the development verifier):

```powershell
$env:APP_ENV = "development"; $env:DB_NAME = "dataops_sim_test"
docker compose up --build
curl http://127.0.0.1:8000/api/v1/health
```

## Production start-up validation (`app/core/startup.py`)

Only `APP_ENV=production` is checked. The process **refuses to start** (uvicorn exits non-zero) unless `DATABASE_URL`,
`APPROVAL_STORE_PATH`, `GOVERNANCE_LEDGER_PATH` and `DB_NAME` are set explicitly, `APPROVAL_AUTH_MODE` is not `dev_static`, and the
settings validate. The error names variables, never values. No new setting or authentication mode was introduced. Observed locally
(uvicorn, production env): missing `DB_NAME` -> `Application startup failed. Exiting.` (exit 3); complete env -> serves, `/api/v1/health` 200,
unauthenticated `/api/v1/incidents` 401, SIGTERM -> `Application shutdown complete`.

Caveat carried from Phase 8: `DATABASE_URL`, `APPROVAL_*`, `GOVERNANCE_LEDGER_PATH`, `GITHUB_*` and `INVESTIGATION_MODE` are read from the
**process environment**, not from `.env` (only the pydantic `Settings` fields such as `DB_*`, `OPENAI_*`, `APP_ENV` read `.env`).

## CI (`.github/workflows/ci.yml`)

On push to `main`, pull requests and manual runs: Python 3.10 and 3.12 matrix -> `pip install -r requirements.txt` -> `pip check` -> focused tests
-> full suite (JUnit uploaded) -> VSL conformance (fails the job on any finding). A second job builds the image (never pushed, no registry
login), checks it runs as uid 10001 with no local state inside, that production start-up refuses to run without `DB_NAME`, and that the
container answers `/api/v1/health`. CI needs **no secrets** and makes no paid LLM call. There is **no SQL Server job**: no safe,
secret-free SQL Server test service is configured, so those tests are reported as not run rather than passed. No linter is configured in the
repository, so none was added. **The workflow has not been executed**; it is validated only by static tests and YAML parsing.

## Cloud deployment foundations (nothing deployed)

* Recommended first path: **one container on one host/VM or a single-instance container service with an attached persistent disk**, TLS
  terminated by a reverse proxy or the platform, not exposed publicly until a real identity provider exists.
* Required settings: see the table above plus `APPROVAL_SIGNING_KEY` (32+ bytes, from a secret store) if recovery is wanted, and
  `GITHUB_TOKEN/OWNER/REPO` for ticketing. Network: outbound to GitHub (if ticketing) and to the SQL Server (if used); inbound only 8000
  from the proxy. Health: `/api/v1/health` (liveness), `/api/v1/ready` (readiness).
* **Ephemeral vs durable:** anything outside `/data` disappears with the container. `/data` must be a persistent volume or disk, backed up, and
  mounted by exactly one container.
* **Why single-host:** the operations DB and approval store are SQLite (single-writer file locking) and the ledger is one hash-chained
  JSONL file appended by one process. Two replicas would corrupt the chain order, race approvals' single-use consumption across processes,
  and split the in-memory auth throttle. Horizontal scaling needs a multi-writer database for operations and approvals, a ledger service with
  a single sequencer (or an external anchor), a shared throttle/rate limiter and a job queue for investigations.
* `dev_static` authentication is **not** suitable for any public deployment; production refuses it.

## Security and governance review

No governance, authentication, approval, suspension, ledger or recovery control was weakened. The tests added here exercise those controls
more, not less. Container defaults are least-privilege (non-root, read-only filesystem in Compose, capabilities dropped, loopback publish).
Nothing in the image or CI holds credentials.

## Known issues and untested areas

* Docker build, Compose run and the GitHub Actions workflow have **not been run**. Expect first-run fixes (for example the apt/ODBC step or the
  git-installed VSL adapter in the build stage).
* SQL Server baseline (`db_verify`) and the SQL Server integration tests: **NOT RUN** by the author.
* VSL-Core pin discrepancy unchanged: `requirements.txt` resolves Core `6c0e525` (spec `a5fcfc7`). The governance/recovery/API subset also
  passes with `a5fcfc78` installed `--no-deps`. A separate compatibility investigation is still proposed.
* Line-ending-only differences in 39 tracked files (see above); `tests/leftover_repro_tmp.txt` is still a tracked stray file.
* In-memory auth throttle, single-host SQLite/JSONL, in-process investigations: unchanged Phase 8 limitations (Phase 11 territory).

## Windows PowerShell

```powershell
cd "C:\Rahesh\AI Agents\AI DataOps Agent"
.\.venv\Scripts\Activate.ps1
python -m pytest -q                                              # expect: 1015 passed, 44 skipped (the 44 are SQL Server tests)
python -m pytest tests/test_phase9_e2e.py tests/test_phase9_deployment.py -q   # expect: 32 passed
python -c "from vsl_core.conformance.suite import run_conformance_suite; from vsl_langgraph import LangGraphAdapter; print(run_conformance_suite(LangGraphAdapter()) or 'CONFORMANT')"
python -m scripts.db_verify                                       # read-only; expect BASELINE HEALTHY
$env:DB_NAME = "dataops_sim_test"; $env:RUN_SQLSERVER_TESTS = "1"
python -m pytest tests/integration -q                             # dataops_sim_test ONLY; never point DB_NAME at dataops_sim here
docker build -t ai-dataops-agent:local .                          # needs Docker Desktop
```
