# Phase 6: VSL governance

Status: implemented, **awaiting review**. Verified on Linux (Python 3.10.12 and 3.12.14) only; the Windows 3.10.11
`.venv` run is yours to do (checklist at the end).

## What VSL adds beyond prompts
A prompt asks a model to behave. VSL (Super Semantics' *Verified, Evidence-based, Rules-Bound Authority* layer) puts
**code outside the model** in front of a protected action: a gate must complete *before* the side effect, a violation
ends in a terminal state that only a named human can clear, and every decision is written to a hash-chained ledger
that can be audited afterwards. Nothing here reads model confidence as permission.

## Package selection and pins
Python + LangGraph `StateGraph` means the **VSL-Langgraph** adapter only. It installs `super-semantics-vsl` (import
name `vsl_core`) itself. Do not pin Core separately (two direct URLs can fail to resolve) and do not
`pip install vsl-core` (an unrelated stub). The LangChain, MAF and JS adapters and the hosted-ledger client are not used.

```
vsl-langgraph @ git+https://github.com/supersemantics/VSL-Langgraph.git@72379e9300597b23a340ad9d479502d8603c28ce
```

**Core drift.** The adapter leaves Core unpinned, so pip resolves Core's current `main` (`6c0e525`, which adds an
optional immudb store), not the spec's `a5fcfc7`. Phase 6 was tested against both (see Tests). `python -m app.governance.audit_cli`
prints the installed VSL commits so a different resolution is visible. Pinning Core as a second URL is a future option if
resolution proves stable.

Installing the adapter changed nothing already pinned (langgraph 1.2.16, pydantic, mcp, openai); `pip check` is clean.

## Roles
| Part | Role |
|---|---|
| `app/governance/policy.py` | Declares the PreNode, two Invariants and the TerminalState. Imports `vsl_core` only. |
| `app/governance/gates.py` | Compiles them with `LangGraphAdapter`; one denial key per gate. Imported only by `workflow.py`. |
| `app/governance/workflow.py` | The governed LangGraph workflow and the `GovernedActionService` entry point. |
| `app/governance/ledger.py` | Local `JsonlLedgerStore` ledger, suspension state rehydrated from it, and the human re-enablement primitive. |
| `app/governance/actions.py` | The protected-action boundary: a **no-op stub** (no DB, file or network). |
| `app/governance/audit_cli.py` | Read-only integrity + semantic audit; non-zero exit on failure. |

## Where governance sits
```
investigation (Phase 5, read-only MCP tools, NOT gated)  ->  report
                                                              |  proposal_from_report(report, action, approval)
load_context -> guard_suspension -> guard_allowlist -> guard_prenode -> execute_action (stub) -> END
                   | denied           | denied            | denied
             record_refusal     record_terminal    route_to_human_review
```
`execute_action` has exactly one incoming edge (from the PreNode gate's approved branch), refuses to run without an
authorisation ledger entry, and the authorisation is written **before** it runs. The investigation graph, the eight MCP tools and
Phase 1-5 behaviour are unchanged.

## Governed now vs. later
* **Now:** a proposed protected action (`rerun_validation_dry_run`, `restart_stage_dry_run`) reaching a no-op stub.
* **Not governed:** reading/diagnosing (harmless), anything the model says. There is **no real recovery action yet**.
* **Phase 7:** real approvals, a human-review queue, real recovery behind the same gates, wiring `re_enable` to an operator flow.

## Meanings
* **PreNode denial (soft).** A failed precondition (report not `issue_identified`, evidence not cited or unknown, approval
  missing/mismatched) routes to human review. Nothing runs. Ledger: `MONITOR(drift=True) -> PRE_NODE -> VERIFICATION(SUFFICIENT)`.
  SUFFICIENT because governance *worked*. Does not suspend automation and does not fail the audit.
* **Invariant violation (hard).** An action outside the allowlist enters terminal state `automation_suspended`.
  Ledger: `MONITOR -> VERIFICATION(INSUFFICIENT) -> TERMINAL`, linked by `caused_by`.
* **Terminal suspension.** Every later proposal is refused (`refused_suspended`, one `MONITOR` each, no extra TERMINAL), whatever
  it claims. Nothing clears it automatically: no timeout, no retry, no model request.
* **Human re-enablement.** `GovernanceRecorder.re_enable(...)` uses VSL's `request_re_enablement` and writes
  `SPECIFICATION_UPDATE` (caused_by the INSUFFICIENT verification), `HUMAN_AUTHORISED_TRANSITION` (caused_by the TERMINAL) and
  `RE_ENABLEMENT`. It needs a `GovernanceAuthority`, a named approver and non-empty `Evidence`. It is **not** reachable from the graph
  (a test enforces this) and there is no operator UI yet; that is Phase 7. The open terminal intentionally **fails** checks 4 and 5 until then.
* **Fail closed.** Gate error, unreadable or tampered ledger, ledger write failure, invalid config, or an escaped denial -> no protected
  action (`governance_error`/`blocked`). An error is never approval. If the very last "executed" record fails after the stub ran, the
  result is `governance_error` with `executed=True` (honest, not hidden).

## Assurance and Gamma
Assurance is derived from facts, never asserted: the gates run before the action (F1 true) but modify no energy function
(F2 `NONE`), so every construct is **LOW**. The PreNode monitor is deterministic; VSL's contract needs a `GammaEstimate`, so
"all preconditions hold" is encoded as 2.0 and "any fails" as 0.0 against the default 1.1 threshold. That is a binary encoding,
**not a measured or calibrated Gamma**, and not X-Verba Gamma. No model confidence is used.

## Specification
`vsl_core.governance.Specification.activate()` needs a verified `Proofing`, a `Binding` and a named approving `GovernanceAuthority`.
None exists yet, and fabricating them would claim an approval that did not happen, so **no Specification is created or activated**.
Phase 7's approval workflow is the place for it. `Fallback` is left at its default; the adapter does not consume it.

## Ledger
`data/governance_ledger.jsonl` (git-ignored; never commit it). One JSON object per line, hash-chained, `fsync` on. Untrusted text
(the action string) is length-bounded before recording; no secrets or incident text are stored. The five audit checks:
`no_monitoring_gaps` (only when `--max-monitor-gap-seconds` is given), `drift_flagged_monitor_has_pre_node`, `pre_node_has_verification`,
`insufficient_verification_has_specification_update`, `terminal_has_human_authorised_transition`. Every causal link uses `caused_by`.

```
python -m app.governance.audit_cli                    # default ledger
python -m app.governance.audit_cli --ledger PATH --max-monitor-gap-seconds 3600 --json
```
Exit 0 = integrity and all checks pass; 1 = integrity or a check failed; 2 = ledger missing / bad arguments. It never writes.

## Tests
```
python -m pytest -q                                   # everything (SQL Server tests skip unless enabled)
python -m pytest tests/test_governance_*.py -q        # Phase 6 only (58 tests)
python -c "from vsl_core.conformance.suite import run_conformance_suite; from vsl_langgraph import LangGraphAdapter; print(run_conformance_suite(LangGraphAdapter()) or 'CONFORMANT')"
```
Executed on Linux only: Python 3.10.12 and 3.12.14 each gave 634 passed, 40 skipped (the skips are the SQL Server tests) with Core `main`;
the 58 governance tests also pass with Core `a5fcfc7` installed; conformance returns `[]`/CONFORMANT on all. Governance touches no database, so there are no
new SQL Server tests. Five deliberate breakages of the gates (always-pass PreNode, open allowlist, ignored suspension, denied path still
executing, errors treated as success) were each caught by the suite.

## Limitations
* Suspension is held by the ledger and rehydrated at each decision, so a restart is safe, but deleting or **truncating** the file loses it:
  `verify_integrity()` cannot detect truncation or replacement by an older consistent chain. `current_checkpoint()` (printed by the CLI) is for an outside anchor.
* `ApprovalContext` is unauthenticated until Phase 7. Today nothing issues approvals, so by default every action is routed to human review.
* One decision at a time per service (an `asyncio.Lock`); a local single-host JSONL file; no durability or multi-writer production claim.
* `Fallback` fields are declarative only; the adapter does not retry. LOW assurance is the honest ceiling.
* Core tracks `main` unless you pin it.

## Windows checklist (you run these)
1. `.venv\Scripts\activate` then `pip install -r requirements.txt` (needs git on PATH to fetch the adapter).
2. `pip check` is clean; `python -m pytest -q` (expect 634 passed, 40 skipped without SQL Server enabled; run your SQL Server suite as before).
3. The conformance command above prints `CONFORMANT`.
4. `python -m app.governance.audit_cli` on a missing ledger prints NOT AUDITED, exit 2 (`echo %ERRORLEVEL%`).
5. `python -m scripts.db_verify` baseline unchanged; `git status` shows only the Phase 6 files and no `data/` or `.env`.
