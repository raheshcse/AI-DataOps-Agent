# Phase 10 - Operations Dashboard, VerbaLedger Explorer and expanded MCP tools

Status: **implemented; awaiting your review.** Not committed, not pushed, not deployed. This is not production-ready
(that is Phase 11). Sections marked *not run* describe checks that could not be executed in the build environment.

## 1. Architecture

```
 Browser ──> Operations Dashboard (Streamlit, port 8501)  ──HTTP, Bearer token A──┐
 Browser ──> VerbaLedger Explorer (Streamlit, port 8502)  ──HTTP, Bearer token B──┤
                                                                                  v
                                       FastAPI  /api/v1   (existing auth, error format, rate limiting)
                                            ├── incidents / investigations / recoveries / approvals / tickets   (Phase 8)
                                            └── /ledger/*  (NEW, read-only, permission ledger:read)
                                                     │ reads (never writes)
                                                     v
                            data/governance_ledger.jsonl  (hash-chained, append-only, written only by the Phase 6/7 workflow)
```

* `dashboard/` is a separate package. Neither app imports SQLAlchemy, SQLite, `vsl_core`, or any `app.*` backend
  package (a test enforces it), so neither can touch SQLite, the ledger or SQL Server directly.
* The two apps are separate Streamlit programs with separate clients, tokens and navigation. The dashboard has no ledger
  client; the explorer has no incident/approval client (also enforced by tests).
* `dashboard/http_core.py` is the only HTTP layer: path and method allow-lists, no redirects, **one attempt per request
  (no retries anywhere)**, token kept out of `repr` and messages, typed `Problem` results instead of silent defaults.

## 2. Install

Dashboard dependencies are kept out of `requirements.txt` so the API / Docker image does not grow:

```powershell
cd "C:\Rahesh\AI Agents\AI DataOps Agent"
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r requirements-dashboard.txt     # streamlit 1.65.0, plotly 7.1.0, pandas 2.3.3
python -m pip check                                     # expected: No broken requirements found.
```

These versions were verified together with the pinned API stack on Python 3.10 and 3.12 in a Linux VM (not on your
Windows machine).

## 3. Launch (Windows PowerShell)

Terminal 1 - the API (development-only static-token authentication; never commit real values):

```powershell
function Get-Digest([string]$t) { -join ([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($t)) | ForEach-Object { $_.ToString("x2") }) }
$viewer   = "<your own viewer token, 32+ characters>"
$explorer = "<your own explorer token, 32+ characters>"
$approver = "<your own approver token, 32+ characters>"
$env:APPROVAL_AUTH_MODE = "dev_static"
$env:APPROVAL_DEV_CREDENTIALS = (ConvertTo-Json -Compress @(
  @{subject="dash.viewer";   token_sha256=(Get-Digest $viewer);   roles=@("viewer")},
  @{subject="ledger.viewer"; token_sha256=(Get-Digest $explorer); roles=@("viewer")},
  @{subject="the.approver";  token_sha256=(Get-Digest $approver); roles=@("recovery_admin")}))
$env:APPROVAL_SIGNING_KEY = "<your own 32+ byte key>"
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Terminal 2 - Operations Dashboard:

```powershell
$env:DASHBOARD_API_BASE_URL = "http://127.0.0.1:8000/api/v1"
$env:DASHBOARD_API_TOKEN = $viewer
streamlit run dashboard/ops_app.py --server.port 8501
```

Terminal 3 - VerbaLedger Explorer (a different token; the role must hold `ledger:read`):

```powershell
$env:DASHBOARD_API_BASE_URL = "http://127.0.0.1:8000/api/v1"
$env:LEDGER_EXPLORER_API_TOKEN = $explorer
streamlit run dashboard/ledger_app.py --server.port 8502
```

Expected output: Streamlit prints `Local URL: http://localhost:8501` (or 8502). The sidebar shows `Token: configured`
(the value is never displayed). An approval decision is typed into a masked field using the approver's own token.

Troubleshooting:

| Symptom | Likely cause |
|---|---|
| "Could not load incidents: The API could not be reached" | API not running, or wrong `DASHBOARD_API_BASE_URL` |
| HTTP 401 `unauthenticated` | token not set in this terminal, or its SHA-256 digest is not in `APPROVAL_DEV_CREDENTIALS` |
| HTTP 403 on the Explorer | the token's role lacks `ledger:read` (investigator, operator and governance_authority do not have it) |
| `ledger_unavailable` (503) | the ledger file does not exist yet or cannot be read; the explorer never creates it |
| `ModuleNotFoundError: streamlit` | `requirements-dashboard.txt` not installed in the active venv |
| Metrics show `n/a` | the incident list could not be loaded (deliberately not shown as zero) |

## 4. Configuration (no secrets in files)

| Variable | Used by | Meaning |
|---|---|---|
| `DASHBOARD_API_BASE_URL` | both apps | FastAPI base URL (default `http://127.0.0.1:8000/api/v1`; http(s), no credentials) |
| `DASHBOARD_API_TOKEN` | dashboard | read token for the dashboard |
| `LEDGER_EXPLORER_API_TOKEN` | explorer | separate token with `ledger:read` |
| `DASHBOARD_REQUEST_TIMEOUT_SECONDS` | both | 1-60, default 10 |
| `DASHBOARD_MAX_INCIDENTS` | dashboard | 20-1000, default 300 incidents loaded |
| `LEDGER_MAX_SCAN_LINES` / `LEDGER_MAX_SCAN_BYTES` | API | scan limits per ledger read (default 200000 lines / 128 MiB) |

They are read from the process environment, not from `.env`. `.env.example` lists the names with placeholders only.

## 5. Operations Dashboard

Pages: Overview (metrics + Plotly charts), Incidents (search, severity/workflow filters, sorting, pagination, detail
panel), Investigations, Recovery & approvals, Tickets, System status. Every page has loading, empty and error states.

API dependencies: `GET /incidents`, `/incidents/{id}/investigations`, `/investigations/{id}`, `/incidents/{id}/recoveries`,
`/recoveries/{id}`, `/incidents/{id}/ticket`, `/health`, `/ready`, and `POST /approvals/{id}/decision`.

Rules the code enforces (each has a test):

* Metrics come from the API. If the list cannot be loaded they show **n/a**, never 0. Charts use only loaded incidents;
  no history is inferred. A truncated load (more incidents than `DASHBOARD_MAX_INCIDENTS`) is stated.
* An investigation's cause is labelled "supported by the cited evidence, NOT confirmed" or "undetermined"; hypotheses are
  labelled "unconfirmed".
* "Executed" is not shown as recovered unless the API reports `verified_outcome == verified`. An **uncertain** outcome is
  shown in red and the dashboard offers no resume or retry (it has no such call at all).
* A closed ticket carries the note that it is not evidence of recovery or authorisation.
* Approvals: the page shows the action, target, risk and expiry, then relays the decision to the existing endpoint with
  **the approver's own token** (masked field, used once, not kept). The server authenticates it and checks the per-action
  permission; the page executes nothing. A timeout is reported as "outcome UNKNOWN - do not submit again", the request is
  sent once, and a second submission for the same request is blocked in that session until you refresh and check status.

## 6. VerbaLedger Explorer and the ledger API

Read-only endpoints (permission `ledger:read`): `GET /ledger/summary`, `/ledger/events` (filters: time range, event type,
decision id, incident id, request/recovery id, outcome, action, rule id; limit 1-200; offset), `/ledger/events/{entry_id}`,
`/ledger/trace`, `/ledger/integrity`, `/ledger/audit`. There is no write route under `/ledger` (tested). The client never
supplies a file path; responses never contain one.

**Loading.** The file is streamed line by line within scan limits. Unreadable lines are counted and listed (line numbers
and reasons) and the view is flagged `complete: false`; the Explorer shows an INCOMPLETE banner. A partially written final
line is reported as such. The file is never created by a read. A compact snapshot is cached per file fingerprint
(size + mtime); any change invalidates it, "Refresh ledger view" forces a re-read, and every answer carries `as_of`.
Suspension state, integrity and audit results are **never cached**.

**Causal trace.** Edges are only the recorded `caused_by` links. `decision_id` / `request_id` only select which records
to include. Chronology is listed separately and labelled "not causal evidence". A `caused_by` naming an absent record is
a flagged *missing link*; a `recovery_attempt` with no recorded result is flagged *uncertain*.

**Integrity vs semantic audit - two different questions.**

| | Integrity verification | Semantic audit |
|---|---|---|
| Question | Is the hash chain intact (sequence, previous hash, record hash)? | Do the recorded events satisfy the VSL ordering rules? |
| Output | verified / failed / could not complete; records examined; **first failing record**; checkpoint | per-check passed / failed / not run, examined count, violating events, explanation |
| Reuses | recomputation + the library verifier | `app.governance.audit_cli.run_audit` |

Neither can detect events removed from the **end** of the ledger, or replacement by an older consistent chain; both say
so. The VSL library's audit checks 2-5 may fall back to time ordering when `caused_by` is unset; the trace view never does.
The monitoring-gap check is *not run* unless you supply a threshold. Nothing here repairs, truncates or rewrites the ledger.

## 7. Added MCP tools

See [mcp-server.md](mcp-server.md) (section "Phase 10 tools") for purpose, schemas, permissions, error behaviour, data
boundary and tests: `get_pipeline_run_statistics`, `summarize_recent_errors`, `check_diagnostics_health`. All read-only,
bounded, no required arguments. A ticket/GitHub tool, any write-capable recovery tool, and any SQL/shell tool were
deliberately not added.

## 8. How VSL still protects consequential actions

Nothing in Phase 10 adds an execution path. Recovery still happens only inside the Phase 7 workflow after a human approval
bound to the exact action, with the VSL gates, invariants and suspension checks unchanged. The dashboard can only relay
an approve/reject decision to the existing authenticated endpoint; the Explorer has no mutating call. `ledger:read` is not
granted to `investigator`, `operator` or `governance_authority`, and re-enablement is still not exposed over HTTP.

## 9. Tests

```powershell
python -m pytest tests/test_phase10_mcp_tools.py tests/test_phase10_ledger_api.py tests/test_phase10_dashboard_units.py tests/test_phase10_dashboard_app.py tests/test_phase10_explorer.py -q
python -m pytest -q -rs          # full regression
```

The Streamlit tests use Streamlit's `AppTest` against the real FastAPI app (real stores and workflow) or a canned fake API.
No network, no secrets, no paid calls. CI (`.github/workflows/ci.yml`) installs `requirements-dashboard.txt` and runs these
as a focused step; **CI has not been run**.

## 10. Known limitations

* The dashboard's tokens are development-style static bearer tokens; production identity is Phase 11. A token entered for
  an approval lives only in the browser form and the one request.
* The dashboard loads at most `DASHBOARD_MAX_INCIDENTS` incidents and does per-incident calls only for the one you select.
* The ledger snapshot holds parsed records in memory (bounded by the scan limits); very large ledgers are reported
  incomplete rather than paged from disk.
* The integrity pass and the audit each re-read the file; they are on-demand by design.
* Streamlit's own session handling is not hardened here; run both apps on loopback only. No Docker image or CI job for the UIs.
* AppTest does not render charts or the Graphviz view in a browser; those were not visually checked.
* Not run in the build environment: Docker build, GitHub Actions, SQL Server integration tests, anything on Windows 3.10.11,
  and a real browser session.

## 11. Deferred to Phase 11

Production identity provider and per-user sessions for the UIs, containerising/hosting the UIs, TLS and reverse proxy,
read-only ticket MCP tool inside the API process, alerting, and any externally anchored ledger checkpoints.
