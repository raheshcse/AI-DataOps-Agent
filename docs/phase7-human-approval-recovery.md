# Phase 7: Human approval and controlled recovery

**Status: implemented, awaiting your review.** Nothing here is committed or pushed. The default behaviour is **denial**: with no
configuration, nobody can approve anything and no recovery action can run.

## What it proves

A protected recovery action cannot execute unless **all** of these hold at the moment of execution:

1. the action is on a fixed allowlist and its parameters pass a strict schema;
2. an **authenticated** human with the **specific permission** for that action approved **that exact request**;
3. the approval is unexpired, unconsumed, untampered and still bound to the same incident, action, parameters and target;
4. the VSL gates pass (no open suspension, no invariant violation, PreNode satisfied) and the ledger can record what is about to happen;
5. the approval is consumed atomically, exactly once, **before** the side effect.

A model's recommendation or confidence is never authorisation. Text inside an incident, log or proposal ("approved by admin") is just text.

## Flow

```
investigation (Phase 5, read-only)  ->  suggest_recovery / proposal_from_report   (UNTRUSTED proposal)
   propose_action -> validate_action -> create_approval_request
                                              |  (graph pauses: interrupt_before await_human_approval)
        human decides out-of-band:  python -m scripts.approvals approve|reject|cancel
                                              |
   resume(request_id) -> validate_approval -> run_governed_recovery
        -> Phase 6 GovernedActionService:  load_context -> guard_suspension -> guard_allowlist -> guard_prenode -> execute_action
        -> RecoveryRunner:  attempt record (read back) -> suspension re-check -> read-only precondition
                            -> ATOMIC consume -> execute -> independent verification -> result record
```

There is **one** governance engine: Phase 7 reuses `GovernedActionService`. The recovery workflow only validates and collects a sealed human
approval, then hands the candidate (with a `RecoveryContext`) to `govern`. Early denials (report not actionable, evidence uncited or unknown)
go through `govern` with no approval, so Phase 6 records them as ordinary SUFFICIENT PreNode denials, not failures.

### Why a static breakpoint and not `interrupt()`

LangGraph 1.2.16's `interrupt()` raised `Called get_config outside of a runnable context` in async nodes on Python 3.10 (and in my sync
attempt). The graph is therefore compiled with `interrupt_before=["await_human_approval"]` and an in-memory checkpointer; it is resumed
with `ainvoke(None, config)`. Values a caller passes on resume carry **no authority** (tested): the only source of an approval is the
approval store.

## Authentication and roles

* `APPROVAL_AUTH_MODE` defaults to `disabled`, which selects `DenyAllAuthenticator` (nobody authenticates). Fail-closed.
* `dev_static` (local development only) reads `APPROVAL_DEV_CREDENTIALS`: a JSON list of
  `{"subject": "...", "token_sha256": "<64 hex>", "roles": ["recovery_admin"]}`. Tokens must be at least 24 characters, are compared in
  constant time, and are never stored. It **refuses to start when `APP_ENV=production`**. `python -m scripts.approvals hash-token` prints a digest.
* Roles: `recovery_approver` (may approve `rerun_pipeline` only), `recovery_admin` (both actions), `governance_authority`
  (`governance:re_enable` only), `viewer` (nothing). Permission strings are `recovery:approve:<action>`.
* The approver's **current** roles are re-checked when the approval is used, so a revoked approver's earlier approval stops working.
* The CLI reads the token from `APPROVAL_TOKEN` or a hidden prompt, never from argv.

**This is not a production identity provider.** A real verifier would implement the small `Authenticator` protocol (`authenticate`,
`current_roles`); nothing else would change. Until one is configured, production stays closed.

## Recovery actions (allowlist)

| Action | Parameters | Risk | Postcondition (verified independently) |
|---|---|---|---|
| `rerun_pipeline` | `pipeline_id` in `order_ingestion`, `order_validation`, `reporting_prep` | medium | a new run exists and its status is `success` |
| `remove_injected_condition` | `scenario_key` in the six scenario keys | high | scenario inactive and the tagged orders reduced by the injected count |

Strict models (unknown fields rejected, no coercion). There is no shell, dynamic Python, free-form SQL or arbitrary MCP call. The executor
only runs against an in-memory repository or a SQL repository whose database name ends in `_test`; `dataops_sim` is refused. The eight
MCP tools are unchanged and read-only.

`rerun_pipeline` is state-dependent: `order_validation` run on its own, without fresh ingestion, fails its freshness check, and the
verifier reports `verification_failed` rather than success (tested). Rerun `order_ingestion` first.

## Exact-action binding

`binding_digest` = SHA-256 of canonical JSON (sorted keys, no spaces, ASCII) of `{v, incident_id, action_type, parameters, target,
evidence_refs (sorted), report_status}`. The approval is sealed with HMAC-SHA256 under a server key (`APPROVAL_SIGNING_KEY`, at least
32 bytes) over `{request_id, binding_digest, approver, decided_at, expires_at, decision}`. Changing the incident, action, parameters,
target or evidence after approval breaks the digest or the seal; editing the SQLite row to "approved" without the key fails the seal.

## Approval lifecycle and single use

`pending -> approved | rejected | cancelled | expired`, then `approved -> consumed`. Default TTL 30 minutes, maximum 24 hours.
Consumption is one conditional `UPDATE ... WHERE status='approved' AND expires_at>now AND ledger_decision_entry IS NOT NULL AND
binding_digest=?` under `BEGIN IMMEDIATE`; only `rowcount == 1` proceeds. Eight racing threads and four racing workers produce exactly one
winner (tested). An approval whose decision was never recorded in the ledger is unusable. A later TERMINAL ledger entry invalidates earlier
approvals, so a suspension cannot be "outlived" by a stale approval.

## VSL enforcement and fail-closed behaviour

* Suspension is checked in the graph **and again** in the runner immediately before the side effect (defence in depth; a mutation test
  showed the second check needed its own test, which was added).
* An invariant violation (for example an action outside the policy's allowlist) opens a TERMINAL state. Approval cannot override it.
* PreNode denial is SUFFICIENT; INSUFFICIENT is reserved for invariant violations. Assurance stays honestly LOW.
* The ledger is written and **read back** before the side effect. A write failure, a silently dropped write, a tampered or unreadable
  ledger, or a gate that raises, blocks execution and leaves the approval unspent.
* `AutomationDeniedException` is never swallowed (AST test over `app/recovery`).
* **Re-enablement** after a terminal state is a separate, authenticated flow (`ReEnablementService`, requires `governance:re_enable`).
  Nothing in any graph can call it, and nothing clears a suspension automatically (tested).

## Failure, uncertain outcomes and results

`executed`, `blocked`, `failed` (precondition), `verification_failed`, `uncertain`, `succeeded_unrecorded`.
An exception during or after the side effect is **uncertain**, never success, and is **never retried automatically**; the consumed
approval cannot be reused. If the effect succeeded but the result record could not be written, the outcome is `succeeded_unrecorded`
and `GovernanceResult.executed` is true, so the workflow reports what really happened (a bug in how this was reported when only the
closing record failed was found by the tests and fixed).

## Ledger events and causal links

Ledger entry types are still only those VSL defines. Phase 7 events are `MONITOR` entries with a payload `outcome`:

`recovery_proposed -> approval_requested -> approval_granted -> authorised -> recovery_attempt -> recovery_succeeded -> executed`

each `caused_by` the previous one (rejections, expiry, refused attempts and `approval_not_usable` are recorded the same way). Entries
share one governance decision id; `approval_granted` carries the request id because the approval service writes it.
The five Phase 6 audit checks are unchanged. Phase 7 adds a separate check, reported as `phase7`:
`recovery_attempt_has_result` (an attempt with no result record is a violation, exit 1). `python -m app.governance.audit_cli` exits
0 clean, 1 on a violation or tampering, 2 when the ledger is missing (NOT AUDITED). Credentials, tokens and exception text never reach the
ledger, approval store or reports (scanned in tests).

## Persistence and restarts

* **Approval store (SQLite) and ledger (JSONL): durable.** Tested by building a fresh object graph over the same files: an unresolved
  approval stays pending, an approval granted before the restart executes exactly once after it, and a consumed one never runs again.
* **LangGraph checkpoint: in-memory, lost on restart.** `resume(request_id)` therefore re-enters at `validate_approval` from the store;
  it does not trust any remembered graph state.
* LangGraph logs a warning that its checkpointer deserialises our dataclasses via an unregistered type. It is cosmetic with the in-memory
  saver but would need an allow-list if a persistent checkpointer were ever added.

## Configuration

| Variable | Meaning |
|---|---|
| `APPROVAL_AUTH_MODE` | `disabled` (default) or `dev_static` |
| `APPROVAL_DEV_CREDENTIALS` | JSON list for `dev_static` (digests only) |
| `APPROVAL_SIGNING_KEY` | server-held HMAC key, at least 32 bytes; unset means the CLI refuses to run |
| `APPROVAL_STORE_PATH` | SQLite file (default `data/approvals.sqlite3`, git-ignored) |
| `GOVERNANCE_LEDGER_PATH` | ledger file (default `data/governance_ledger.jsonl`, git-ignored) |
| `APPROVAL_TOKEN` | the approver's token for one CLI call (otherwise a hidden prompt) |

## CLI

```
python -m scripts.approvals list [--status pending]
python -m scripts.approvals show REQUEST_ID
python -m scripts.approvals approve REQUEST_ID [--reason TEXT]
python -m scripts.approvals reject  REQUEST_ID [--reason TEXT]
python -m scripts.approvals cancel  REQUEST_ID
python -m scripts.approvals hash-token
```

Exit 0 ok, 1 refused, 2 misconfigured or storage error. The CLI only records a decision; it executes nothing. On Linux (temp paths, `dev_static`)
I drove it for real: viewer and wrong-token approvals were refused (exit 1), the admin approval succeeded, a second decision was refused,
`disabled` mode refused everyone, a missing signing key exited 2, and a fresh process then resumed and executed exactly once with a clean
audit and no token or key in the ledger or store. That run is not a Windows result.

## Tests

```
python -m pytest tests/test_recovery_*.py -q                      # Phase 7 (non-SQL)
python -m pytest tests/test_governance_*.py -q                    # Phase 6, unchanged behaviour
python -m pytest -q                                               # everything
$env:RUN_SQLSERVER_TESTS = "1"; python -m pytest tests/integration/test_recovery_sqlserver.py -q   # dataops_sim_test only
```

The seven required end-to-end scenarios are in `tests/test_recovery_e2e.py` (real offline investigation, real MCP tools, real VSL gates,
real SQLite approval store and ledger; the recovery effect is the deterministic in-memory simulation). The SQL Server tests mirror the
happy path and rejection over real T-SQL in `dataops_sim_test` only and are skipped unless `RUN_SQLSERVER_TESTS=1`; **a skip is not a pass**.

## Dependency versions and the unresolved Core pin

Tested: `vsl-langgraph 0.1.0` at `72379e9300597b23a340ad9d479502d8603c28ce`, `super-semantics-vsl 0.1.0` at
`6c0e52575a0a4410c11807a59197a16ceb584bb5` (what `requirements.txt` resolves; the spec names `a5fcfc78...`), langgraph 1.2.16,
pydantic 2.14.1. The Phase 6 and 7 tests also pass with Core `a5fcfc78` installed `--no-deps`. Pinning Core as a second direct URL
makes pip fail with `ResolutionImpossible`, because the adapter already declares an unpinned Core URL. **Not changed.** Options, awaiting your decision:

1. keep the adapter-only requirement (Core follows `main`, as now);
2. install the adapter with `--no-deps` and pin Core separately (tested working, but two install steps);
3. a constraints or lock file (untested).

## Limitations (read before trusting this)

* **No production identity provider.** `dev_static` is a development stand-in; production remains deny-all.
* **No application entry point drives the workflow.** The CLI records decisions; `RecoveryWorkflow` is used by tests. Wiring it into a
  service or operator UI is later work.
* The approval store and ledger are local single-host files: no multi-host, no clock-skew handling, and **truncating or replacing the
  ledger with an older consistent chain is not detectable** (a Phase 6 limitation; use `current_checkpoint()` as an outside anchor).
* Two actions on the simulated environment only. There is no destructive or production recovery.
* The SQLite approval store failed with "disk I/O error" on the Linux sandbox's mounted repository folder (file locking). Tests use temp
  directories; the default `data/` path is expected to work on Windows but was **not verified** there.
* The signing key lives in an environment variable on the same host as the store; an attacker with both can forge approvals.
* Assurance is LOW by design; no Specification claims a real authority.
