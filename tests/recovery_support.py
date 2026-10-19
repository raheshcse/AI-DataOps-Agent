"""Helpers for the Phase 7 tests: deterministic, no network, no OpenAI, no SQL Server, no real credentials.

The tokens below are test fixtures only. The permissive pieces here (fake clock, counting executor) live under
tests/ on purpose: nothing in ``app`` can accept an unverified approver.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.agent import OfflineHeuristicModel
from app.governance import GovernanceRecorder, GovernedActionService, open_ledger
from app.governance.policy import build_policy
from app.recovery import (
    RECOVERY_ACTIONS,
    ApprovalService,
    ApprovalSigner,
    ProposedRecovery,
    RecoveryExecutor,
    RecoveryRunner,
    RecoveryWorkflow,
    SqliteApprovalStore,
    StaticTokenAuthenticator,
)
from app.recovery.auth import token_digest
from app.simulation.runlog import RunLogStore
from app.simulation.service import SimulationService
from tests.agent_support import apply_scenario, run_agent, server_for
from tests.fakes import InMemoryRepo

TOKENS = {"admin": "test-admin-token-0123456789abcd", "approver": "test-approver-token-0123456789ab",
          "authority": "test-authority-token-0123456789a", "viewer": "test-viewer-token-0123456789abc"}
ROLES = {"admin": "recovery_admin", "approver": "recovery_approver", "authority": "governance_authority",
         "viewer": "viewer"}
SIGNING_KEY = "test-signing-key-" + "x" * 32
RERUN = ("rerun_pipeline", {"pipeline_id": "order_ingestion"})


def make_auth(revoked: set[str] = frozenset()) -> StaticTokenAuthenticator:
    return StaticTokenAuthenticator([{"subject": f"{name}.user", "token_sha256": token_digest(tok), "roles": [ROLES[name]]}
                                     for name, tok in TOKENS.items() if name not in revoked])


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 19, 1, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class CountingExecutor(RecoveryExecutor):
    """The real executor, plus an execution counter and optional injected faults."""

    def __init__(self, service: SimulationService) -> None:
        super().__init__(service)
        self.count = 0
        self._lock = threading.Lock()
        self.fail_with: Exception | None = None   # raised AFTER the effect, so the outcome is genuinely uncertain
        self.skip_effect = False                  # claims success but changes nothing -> postcondition must fail

    def execute(self, action, facts):
        with self._lock:
            self.count += 1
        if self.skip_effect:
            return {"run_id": "RUN-DOES-NOT-EXIST", "changed": True}
        detail = super().execute(action, facts)
        if self.fail_with is not None:
            raise self.fail_with
        return detail


@dataclass
class Env:
    sim: SimulationService
    executor: CountingExecutor
    recorder: GovernanceRecorder
    store: SqliteApprovalStore
    approvals: ApprovalService
    governed: GovernedActionService
    workflow: RecoveryWorkflow
    clock: Clock
    ledger_path: Path
    db_path: Path


def make_env(tmp_path: Path, *, name: str = "a", clock: Clock | None = None, sim: SimulationService | None = None,
             allowed: frozenset[str] = RECOVERY_ACTIONS, revoked: set[str] = frozenset(), auth: Any = None) -> Env:
    """A complete Phase 7 stack. Calling it twice with the same ``name`` and ``tmp_path`` but a fresh Python
    object graph simulates a process restart over the same ledger file and approval database."""
    clock = clock or Clock()
    if sim is None:
        sim = SimulationService(InMemoryRepo(), RunLogStore(tmp_path / "logs"))
        sim.seed()
    ledger_path, db_path = tmp_path / f"{name}-ledger.jsonl", tmp_path / f"{name}-approvals.sqlite3"
    recorder = GovernanceRecorder(open_ledger(ledger_path, fsync=False))
    store = SqliteApprovalStore(db_path)
    approvals = ApprovalService(store, ApprovalSigner(SIGNING_KEY), auth or make_auth(revoked), recorder, clock=clock)
    executor = CountingExecutor(sim)
    governed = GovernedActionService(recorder, RecoveryRunner(executor, approvals, recorder),
                                     policy=build_policy(allowed))
    return Env(sim, executor, recorder, store, approvals, governed, RecoveryWorkflow(approvals, governed, recorder),
               clock, ledger_path, db_path)


def prop(action: str = RERUN[0], params: dict[str, Any] | None = None, *, status: str = "issue_identified",
         refs: tuple[str, ...] = ("E1",), known: tuple[str, ...] = ("E1", "E2"), incident: str = "INV-TEST-1",
         reason: str = "test") -> ProposedRecovery:
    return ProposedRecovery(incident, action, dict(RERUN[1] if params is None else params), refs, reason, status, known)


def run(coro):
    return asyncio.run(coro)


def start(env: Env, proposal: ProposedRecovery | None = None):
    out = run(env.workflow.start(proposal or prop()))
    return out


def approve(env: Env, request_id: str, who: str = "admin"):
    return env.approvals.approve(request_id, TOKENS[who], "looks right")


def kinds(env: Env) -> list[str]:
    return [str(e.payload.get("outcome", e.entry_type.value)) for e in env.recorder.ledger.store.all_entries()]


def investigate(tmp_path: Path, scenario: str):
    """The REAL investigation (real MCP server + diagnostics over the in-memory simulation, offline model)."""
    sim = SimulationService(InMemoryRepo(), RunLogStore(tmp_path / "inv-logs"))
    sim.seed()
    apply_scenario(sim, scenario)
    report = run_agent(server_for(sim), "orders look wrong", OfflineHeuristicModel())
    return sim, report


class FailingStore:
    """Wraps a ledger store: reads work, appends fail after ``allow`` successful ones (a full disk, say)."""

    def __init__(self, inner, allow: int = 0):
        self.inner, self.allow = inner, allow

    def append(self, entry):
        if self.allow <= 0:
            raise OSError("disk full")
        self.allow -= 1
        return self.inner.append(entry)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class DroppingStore(FailingStore):
    """Pretends to append but persists nothing (the worst kind of ledger failure: a silent one)."""

    def append(self, entry):
        return entry


def sqlite_update(db_path: Path, sql: str, args: tuple = ()) -> None:
    """Edit the approval database behind the service's back (simulates tampering)."""
    import sqlite3
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(sql, args)
