# Operations Dashboard fix, Human Review, demo dataset and VerbaLedger redesign (Phase 10 follow-up)

Status: implemented and tested headlessly; **not verified in a real browser, on Windows, against SQL Server, in Docker or in CI**.

## 1. Root cause of what you saw

1. **Swagger showed only `/sim/*`, `/health`, `/ready`.** The versioned API is a separate FastAPI app mounted at `/api/v1`.
   FastAPI does not put a mounted sub-app's routes in the parent's `/openapi.json`, so `/docs` could not show them even
   though they were served (the dashboard was already using them). Fixed by `app/api/openapi_merge.py`: the root document now
   lists `/api/v1/*`. Routing and authentication did not change.
2. **INC-0001 / INC-0002 were invisible.** They live in the SQL Server simulator (`incident_records`). The dashboard reads
   the SQLite *workflow* store, which only holds incidents created through the API. Nothing ever bridged the two. Fixed by
   authenticated read-only `/api/v1/simulator/*` endpoints plus an explicit, idempotent import.

## 2. Source of truth per record type

| Record | Owner | Notes |
|---|---|---|
| Pipelines, runs, validation results, simulator incidents | SQL Server `dataops_sim` | read-only everywhere in the API/UI |
| Workflow incidents, investigations, recoveries, tickets | SQLite operations store | `external_ref = sim:<INC-id>` for imported simulator incidents |
| Approval requests and decisions | SQLite approval store | the only decision path is `POST /api/v1/approvals/{id}/decision` |
| Governance events | hash-chained ledger file | read-only via `/api/v1/ledger/*` |
| 100 demo incidents | generated in memory by `app/demo` | stored nowhere; labelled `demo_synthetic` |

Import creates a `new` workflow incident. It writes **no** investigation, approval or governance event and never touches SQL Server.

## 3. Endpoints added or repaired (all under `/api/v1`, Bearer token, no secrets in responses)

* `GET /simulator/incidents` (filter `status`, `severity`, `q`, `imported`; paged), `GET /simulator/incidents/{INC-id}` (with run + validations),
  `POST /simulator/incidents/{INC-id}/import` (needs `incident:create`; idempotent: 201 first, 200 afterwards),
  `GET /simulator/pipelines`, `GET /simulator/runs`
* `GET /reviews` (`state=pending|decided|expired|all`, `severity`, `q`), `GET /reviews/summary`, `GET /reviews/{recovery_id}`
* `GET /demo/summary`, `GET /demo/incidents` (filters + paging), `GET /demo/incidents/{DEMO-id}`
* `GET /dashboard/summary` (workflow, human review, simulator and demo counts, kept separate)
* `GET /incidents` gained `q` (search) and `origin=simulator`; `GET /recoveries` (global list)
* `GET /ledger/events` gained `q`; `GET /ledger/summary` gained `by_day`, `decisions`, `decision_classes`, `origin`
* Root `/openapi.json` / `/docs` now include all of the above plus the existing `/api/v1` routes

Already existing and unchanged: incident create/list/get/status, start/list/get investigations, report, tickets, recoveries, approvals get/decision, ledger events/trace/integrity/audit.

## 4. The 100 synthetic incidents

`app/demo/incidents.py`: 20 categories x 5 variants, IDs `DEMO-0001`..`DEMO-0100`, deterministic (no randomness), varied severity
(low/medium/high/critical), status (open, investigating, awaiting_review, resolved, closed), pipelines and timestamps. Fields: id, title,
description, category, severity, status, affected pipeline/dataset, detection time, synthetic run ref and batch date, expected/observed
values, diagnostic evidence, probable cause (marked `hypothesis_unconfirmed`), recommended next step, `human_review_required`, `source=demo_synthetic`.
Isolation: the package imports no database/network module (a test enforces it), nothing is written anywhere, and `human_review_required`
is descriptive only: demo records never enter the Human Review queue. Export a copy with `python scripts/export_demo_incidents.py out.json`.

## 5. Human Review

A queue item exists only when a stored recovery is `awaiting_approval` **and** its approval request is `pending` and unexpired
(expiry uses the approval service's own clock). The detail view keeps four kinds of statement apart: confirmed evidence, agent hypothesis,
recommendation, human decision. Approve/Reject use the existing endpoint with the approver's own token (masked field, used once, not kept);
the server checks the per-action permission, makes approvals single-use, and executes only inside the VSL-gated workflow. Clarification
requests are not supported by the workflow, so none are offered. A viewer token can read the queue but cannot decide.

## 6. VerbaLedger Explorer

Overview (total events, latest event, allowed/denied/suspended/human-review counts grouped by outcome *name* with the grouping shown,
events per day, events by type, on-demand integrity check), searchable event table, event detail with links to the incident/review,
causal trace with a **lifecycle stage table** that marks only stages with a matching record as recorded (incident detection, investigation
and evidence live in the operations store, so the ledger alone shows them as not recorded and says why), integrity verification, semantic audit.
All events are real; the explorer has no way to create or display synthetic ledger records.

## 7. Start commands (Windows PowerShell)

**Recommended: one launcher** (it builds the correct environment for each process, which was the recurring failure):

```powershell
cd "C:\Rahesh\AI Agents\AI DataOps Agent"
.\.venv\Scripts\python.exe -m scripts.dev_stack --show-approver-token
```

It generates random dev tokens and a signing key once into `.dev/dev_auth.json` (git-ignored), starts the API (:8000), Operations Dashboard (:8501) and
Explorer (:8502) with the right environment each, and prints PASS/FAIL for: `ready.authentication == dev_static`, viewer token 200, no token 401, wrong token 401,
explorer token on the ledger, and both Streamlit servers. Logs are in `.dev/logs/`. Ctrl+C stops what it started; it never kills other processes and
refuses `APP_ENV=production`. Why it exists: authentication is read only from the API process's own environment (never `.env`), and each dashboard reads its token
from its own environment, so values typed into one window never reach another. If a port is busy it tells you what answered and exits.

Manual alternative (same result, more steps):

Restart the API (the running process still has the old code), then the two UIs. Use the commands in
[phase10-operations-dashboard-verbaledger-mcp.md](phase10-operations-dashboard-verbaledger-mcp.md) section 3 for tokens and env.
Optional: `$env:OPS_DASHBOARD_URL` / `$env:LEDGER_EXPLORER_URL` (defaults `http://127.0.0.1:8501` / `:8502`) control the cross-navigation links.

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
streamlit run dashboard/ops_app.py --server.port 8501
streamlit run dashboard/ledger_app.py --server.port 8502
# Optional, explicit and idempotent: bring simulator incidents into the workflow store
$env:IMPORT_API_TOKEN = "<operator token>"; python scripts/import_simulator_incidents.py --yes
```

## 8. Limitations

* Tested with Streamlit's headless `AppTest` and FastAPI's test client, not a real browser; layout, colours and focus order are unverified visually.
* SQL Server was not available to the test environment; simulator endpoints are tested over the in-memory repository with the same interface.
  The simulator list is capped at 1000 incidents.
* `dev_static` authentication is development-only. Token handling in the UI is for local use.
* The Human Review queue reads recoveries (500 most recent). Old pending items beyond that window would not be listed.

## 9. Verification record

* Automated, run on a Linux VM: full suite on Python 3.10 and 3.12 each **1257 passed, 44 skipped (SQL Server integration, gated), 0 failed**; `pip check` clean.
  New tests: `tests/test_phase11_*.py` (OpenAPI, simulator API/import, reviews/demo/dashboard API, 100-incident dataset, Ops UI, Explorer).
* Also exercised the real `app.main` app with an unreachable SQL Server: simulator endpoints answer a clean 503 (`simulator_unavailable`) with no server
  name leaked, and the dashboard summary and demo endpoints still answer.
* **Not run (could not be):** real browser rendering, SQL Server integration tests, Docker build, GitHub Actions, Windows.
* Line endings: `.env.example` is LF in git but CRLF in your Windows working tree (autocrlf). `git diff --check` therefore reports "trailing whitespace" for
  every line of that file; with `-c core.whitespace=cr-at-eol` it is clean. This is pre-existing and harmless; the real content change is +16 lines.

## Investigate button (Ops Dashboard)

**What it does.** *Incidents* (workflow and simulator) and *Investigations* show an **Investigate** form. Submitting it calls the
existing `POST /api/v1/incidents/{id}/investigations` with the caller's own token. The API records a pending investigation and
runs it in the background: the agent starts the read-only MCP diagnostic server, collects evidence (run status, validations,
row counts, schema and duplicate checks), a planner chooses further read-only tools within fixed budgets, and a report is built
and verified in code (every cited evidence id must exist). The page shows the backend's own status, then the findings:
observed facts, unconfirmed hypotheses, the likely root cause (or "undetermined"), evidence, next steps and limitations.
**No recovery is executed**; recovery stays behind Human Review and an approver's decision.

**Model use.** `INVESTIGATION_MODE=offline` (default): deterministic heuristic planner, no network, no key, no cost.
`INVESTIGATION_MODE=live`: OpenAI (`OPENAI_API_KEY`, `OPENAI_MODEL`, default `gpt-4o-mini`) is called for up to three planning
rounds plus one report call, with a strict JSON schema; incident text and diagnostic evidence are sent as untrusted data and the
output is validated in code. The panel states which mode the API is in.

**Sources.** A workflow incident is investigated directly. A simulator incident is first imported (idempotent, operations store
only, `POST /simulator/incidents/{id}/import`), then investigated. Synthetic demo incidents have no workflow record and show no
button.

**Auth.** Starting needs `investigation:run` (roles `investigator`, `operator`); importing needs `incident:create`. The viewer
token cannot. Set `DASHBOARD_INVESTIGATE_TOKEN` for the dashboard process, or leave it unset and type your own token into the
masked field (never stored or displayed). `scripts/dev_stack.py` creates a dev `investigator` credential and gives it to the
Operations Dashboard only.

**Limits.** Status refresh is manual (*Check status*). Results are only as good as the simulator diagnostics. Live OpenAI, a
browser and SQL Server were not exercised by the automated tests.

## Human Review "recovery_unavailable" (HTTP 503) - root cause and fix

**Cause.** `build_services` creates `RecoveryService` only after `RecoveryExecutor(sim)` succeeds, and the executor calls
`assert_recovery_target`, which accepts an in-memory repository or a SQL repository whose database name ends in `_test`, and
refuses everything else (`UnsafeRecoveryTarget`). With `DB_NAME=dataops_sim` the guard raises, the container records
`recovery_unavailable = "recovery_target_unavailable:UnsafeRecoveryTarget"`, `services.recoveries` stays `None`, and
`/reviews`, `/reviews/summary`, `/reviews/{id}` and `POST /incidents/{id}/recoveries` answer 503. Approvals themselves are
configured; only the recovery target is refused. This is the Phase 7 invariant "`dataops_sim` is never writable by recovery"
and is **unchanged**.

**Fix (no guard weakened).** (1) The 503 now carries an accurate message plus `details` (reason, database name,
`required_database_suffix`, fix) via `recovery_unavailable_info`; `/dashboard/summary` exposes the same under
`recovery_unavailable`. (2) The Human Review page shows that explanation and the PowerShell steps instead of a bare error,
draws no queue and invents no zero counts. (3) `scripts.dev_stack` reports `Human Review / recovery: AVAILABLE|UNAVAILABLE`.
(4) The supported way to run the demo is a separate `*_test` database (README: *Enable Human Review and recovery*).
(5) Also fixed: the dashboard cached the incident list for the whole session, so after a decision the Recovery page still
showed the pre-decision state until *Refresh data*; the cache is now dropped after import, investigation, recovery request and
decision. Tests: `tests/test_phase13_human_review_config.py`.
