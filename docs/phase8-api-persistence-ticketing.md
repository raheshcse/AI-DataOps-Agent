# Phase 8: API, persistence and ticketing integration

**Status: implemented, awaiting your review. Nothing is committed or pushed.** This is **not production-ready**: see
[Limitations and production prerequisites](#limitations-and-production-prerequisites).

The existing agent, approval workflow and VSL-governed recovery are exposed through a FastAPI backend under `/api/v1`,
incident/investigation/recovery/ticket history is persisted, and GitHub Issues can be linked to incidents. **Nothing in this
phase gives the API a way to execute a recovery without a human approval and the VSL gates** (see
[How approval and VSL are enforced](#how-approval-and-vsl-are-enforced)).

## Architecture

```
HTTP client --Bearer--> /api/v1 (sub-app mounted by app.main; own error format and OpenAPI)
   routes (thin) -> security.require(permission) -> services
        InvestigationService -> Phase 5 agent (read-only MCP tools; offline model by default)
        RecoveryService      -> Phase 7 RecoveryWorkflow / ApprovalService  (the ONLY path to a recovery)
        TicketService        -> GitHubIssuesAdapter (3 fixed operations, one repository)
        OperationsStore      -> SQLite operational database (incidents, investigations, recoveries, tickets)
```

* The existing root `/health`, `/ready` and read-only `/sim` routes are untouched. `/api/v1/health` (liveness) and
  `/api/v1/ready` (dependency-aware readiness) are separate.
* Routes never import the executor, the runner, `GovernedActionService` or the GitHub client (a static test enforces it).
* No new dependency was added (`requirements.txt` is unchanged). `httpx`, `fastapi` and the stdlib `sqlite3` are used.
* The eight MCP tools are unchanged and read-only. No MCP tool was added.

## Authentication and authorisation

There is **one** identity system: Phase 7's. `Authorization: Bearer <token>` is verified by the configured Phase 7 authenticator.

| Variable | Meaning |
|---|---|
| `APPROVAL_AUTH_MODE` | `disabled` (default: nobody authenticates) or `dev_static` (**development only**) |
| `APPROVAL_DEV_CREDENTIALS` | JSON list of `{"subject","token_sha256","roles"}`; digests only |
| `APP_ENV=production` | `dev_static` is refused; the API then denies everyone (`auth_status=misconfigured`) |

A separate `API_AUTH_MODE` was intentionally **not** introduced: a second switch could disagree with the approval one.
`dev_static` is a development stand-in, not production identity. Production stays closed until a real verifier implementing the
small Phase 7 `Authenticator` protocol is supplied.

| Role | API permissions |
|---|---|
| `api_reader`, `viewer` | read everything |
| `investigator` | read + create incidents + run investigations |
| `operator` | investigator + request/resume recoveries + manage tickets |
| `recovery_approver`, `recovery_admin` | read + reach the decision endpoint (the **Phase 7 service** still decides per action) |
| `governance_authority` | nothing over HTTP (re-enablement is deliberately not exposed) |

Identity comes only from the verified token. Request bodies forbid unknown fields, so `approved`, `approver`, `decided_by` or `roles`
in a body produce 422. Ten failed authentications per minute per client address lock that address out for a short time (429 with
`Retry-After`); the throttle is in-memory and per process.

## Endpoint catalogue (`/api/v1`)

| Method and path | Permission | Success |
|---|---|---|
| `GET /health`, `GET /ready` | none | 200 (`/ready` is 503 if the operations DB is down or auth is misconfigured) |
| `POST /incidents` (`Idempotency-Key` optional) | incident:create | 201 (200 on replay) |
| `GET /incidents` (`limit` 1-100, `offset`, `status`, `severity`) | read | 200 `{items,total,limit,offset}` |
| `GET /incidents/{id}`, `/status`, `/report` | read | 200 |
| `POST /incidents/{id}/investigations`, `GET .../investigations`, `GET /investigations/{id}` | investigation:run / read | 202 / 200 |
| `POST /incidents/{id}/recoveries` | recovery:request | **202 `awaiting_approval`** (never executes); other recorded outcomes 409 |
| `GET /incidents/{id}/recoveries`, `GET /recoveries/{id}` | read | 200 |
| `POST /recoveries/{id}/resume` | recovery:resume | 200 (idempotent) |
| `GET /approvals/{id}` | read | 200 |
| `POST /approvals/{id}/decision` body `{"decision":"approve\|reject","reason":""}` | approval:decide | 200 |
| `POST /incidents/{id}/ticket`, `GET .../ticket`, `POST .../ticket/sync` | ticket:manage / read | 201 (200 if it exists) / 200 |

Example:

```
POST /api/v1/incidents            {"title":"Duplicate orders","description":"The same order appears twice.","severity":"high"}
POST /api/v1/incidents/inc-.../investigations        -> 202 {"investigation_id":"inv-...","status":"pending",...}
POST /api/v1/incidents/inc-.../recoveries            -> 202 {"status":"awaiting_approval","approval_request_id":"apr-...",...}
POST /api/v1/approvals/apr-.../decision  {"decision":"approve","reason":"checked"}   (as a recovery_admin)
                                                    -> 200 {"approval":{...,"status":"consumed"},"recovery":{"status":"executed",...}}
```

OpenAPI: `/api/v1/openapi.json` and `/api/v1/docs`.

### Error structure

`{"error": {"code","message","request_id","details"?}}`. Statuses: 401 unauthenticated, 403 forbidden, 404 not found, 409 conflict
(duplicate external reference, active investigation/recovery, refused or replayed approval), 422 validation (field and rule only, never
the submitted value), 429 auth lockout, 500 generic (never exception text), 502 provider auth/permission/bad response, 503
storage/ticketing/recovery unavailable or provider timeout/rate limit. `X-Request-ID` is accepted if it matches `^[A-Za-z0-9_.-]{8,64}$`
and is always echoed.

## Persistence

* **Database:** `DATABASE_URL`, SQLite only (`sqlite:///data/operations.sqlite3` by default, git-ignored). Any other scheme (including SQL Server)
  is refused, and the error never echoes the URL. It never points at `dataops_sim`.
* **Schema management:** explicit and forward-only (`schema_migrations`). Migrations only create; there is no DROP/DELETE/TRUNCATE
  (a static test enforces it), a newer-than-app database is refused, and each migration is one transaction. The API runs the migration at
  start-up (idempotent, non-destructive) and marks investigations left `pending`/`running` by a previous process as failed (`interrupted`).
  Nothing destructive ever runs at start-up.
* **Tables:** `incidents`, `investigations`, `recoveries`, `ticket_associations`, `ticket_events`. Foreign keys are enforced, timestamps are
  UTC ISO-8601, and constraints are in the database: unique `external_ref` and `Idempotency-Key`, one active investigation and one active
  recovery per incident (partial unique indexes), one ticket association per incident, unique external issue, CHECKs on severity/status/outcome.
* **One source of truth per fact:** `incidents.status` covers investigation only; `recoveries.status` stores the Phase 7 outcome; the
  overall `workflow_status` is **derived at read time**, so stored fields cannot contradict each other.
* **Restart:** everything survives. A pending approval stays pending; a recovery stuck in `resuming` is reconciled: approval already
  spent -> `recovery_uncertain` (never success, never retried); not spent -> the claim is released.
* **SQLite limits:** single host, coarse write locking, no replication. Fine for one API process; not a multi-instance production database.

## Status transitions

Incident: `new -> investigating -> investigated | investigation_failed` (a failed one may be investigated again).
Derived `workflow_status`: `new`, `investigation_pending|running|completed|failed`, `awaiting_approval`, `recovery_running`,
`recovery_completed`, `recovery_uncertain`, `recovery_failed`, `recovery_not_executed`.
Recovery: `awaiting_approval -> resuming -> executed | rejected | expired | blocked | recovery_uncertain | recovery_verification_failed | ...`
(the Phase 7 vocabulary). `verified_outcome` is `verified`, `not_verified`, `unknown` or `not_applicable`.

## Ticketing (GitHub Issues)

Configure `GITHUB_TOKEN`, `GITHUB_OWNER`, `GITHUB_REPO` (optional `GITHUB_API_URL`, https only). If any of the first three is missing,
ticketing is `not_configured`: endpoints answer 503 `ticketing_unavailable` and **no id or URL is ever invented**. The token is only placed in
the `Authorization` header: it is excluded from `repr`, logs, responses and errors, and response bodies are never copied into errors.

* **Duplicate prevention:** one local association row per incident, a claim, and a lookup by the stable body marker
  `<!-- dataops-incident:{incident_id} -->` (issues labelled `dataops-incident`) before anything is created.
* **Retries:** `POST` (create) is never retried automatically. A create with an unknown outcome (timeout, unreachable, 5xx, bad response) is
  stored as `creation_unconfirmed`; the next attempt finds the issue by marker and adopts it instead of creating another. Reads retry up to 2
  times with back-off on timeout/unreachable/5xx only.
* **Failure mapping:** timeout/unreachable/5xx/rate limit -> 503 (with `Retry-After` when GitHub says so); 401/403/404/bad response -> 502.
  The incident is always preserved (`details.incident_preserved`).
* **Lifecycle mapping:** open issue -> `open`; closed issue -> `closed` after `sync`. This is **information only**. A closed issue does not
  prove the pipeline recovered, and a ticket never approves, authorises or triggers anything.

## How approval and VSL are enforced

1. `POST .../recoveries` calls `RecoveryWorkflow.start`, which stops at `awaiting_approval`. The API has no code path that executes.
2. `POST /approvals/{id}/decision` relays the **caller's own bearer token** to `ApprovalService.approve|reject`. The service re-authenticates
   it and checks the per-action permission (`recovery:approve:<action>`); an `operator`, `investigator`, `reader` or `governance_authority` is refused.
3. Only then is `RecoveryWorkflow.resume` called, which validates the sealed approval (exact-action binding, HMAC, expiry), runs the Phase 6
   `GovernedActionService` (suspension, allowlist, PreNode) and the runner (ledger read-back, atomic single-use consume, execute, independent
   verification). All Phase 7 guarantees apply unchanged: failed or uncertain outcomes are reported as such and are never retried automatically.
4. A valid API token, a successful investigation, an LLM recommendation, or an open/closed GitHub issue is never authorisation.
5. If the ledger cannot record, or a gate fails, execution is blocked before the side effect and the approval stays unspent.
6. A governance suspension is never cleared by the API, and re-enablement is not exposed over HTTP.
7. Recovery is unavailable when the simulation database is `dataops_sim` (Phase 7 refuses any database not ending `_test`); use
   `DB_NAME=dataops_sim_test` or the in-memory simulation. `GET /api/v1/ready` reports `recovery: unavailable` otherwise.

## Running

```powershell
# development only: generate a token digest with: python -m scripts.approvals hash-token
$env:APPROVAL_AUTH_MODE = "dev_static"; $env:APPROVAL_DEV_CREDENTIALS = '[{"subject":"you","token_sha256":"<digest>","roles":["operator"]}]'
$env:APPROVAL_SIGNING_KEY = "<your own 32+ byte key>"      # needed for recovery; never commit it
uvicorn app.main:app --reload                              # API at http://127.0.0.1:8000/api/v1/docs
```

## Tests

```powershell
python -m pytest tests/test_api_persistence.py tests/test_api_github.py tests/test_api_v1.py tests/test_api_governance.py tests/test_api_e2e.py tests/test_api_static.py -q
python -m pytest -q                       # everything (SQL Server tests are skipped unless RUN_SQLSERVER_TESTS=1)
```

All of these are offline: GitHub is an `httpx.MockTransport` fake, the agent is the offline model, and the recovery effect is the in-memory
simulation. **They are not live-GitHub or SQL Server tests** and are reported as such. Every test database is a temporary file; `dataops_sim` is never touched.

## Limitations and production prerequisites

* No production identity provider (`dev_static` only); no per-request audit of reads; no OAuth/OIDC, no token rotation or revocation list.
* The auth lockout is in-memory and per process; there is no general rate limiting.
* SQLite and the JSONL ledger are single-host files; the signing key sits in an environment variable on the same host as the store.
  Truncating the ledger to an older consistent chain is not detectable (Phase 6 limitation).
* Investigations run in-process background tasks (lost on a crash, marked `interrupted` on restart); there is no job queue.
* Recovery only touches the simulation, and only against a `*_test` database or the in-memory simulation.
* The LangGraph checkpoint is in memory; `resume` re-validates from the approval store.
* GitHub: tested against a fake only; a live check needs your own scoped token and repository. Issue state is not reconciled automatically (explicit `sync`).
* VSL-Core pin discrepancy is unchanged: `requirements.txt` resolves Core `6c0e525` (spec: `a5fcfc7`); the Phase 6-8 governance tests also pass with `a5fcfc78` installed `--no-deps`.
* Before any production use: a real verifier, TLS termination, a multi-writer database, a shared throttle, secret management, and an external anchor for the ledger.
