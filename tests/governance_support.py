"""Helpers for the Phase 6 governance tests: no database, no network, no model."""

from __future__ import annotations

from vsl_core.governance import GovernanceAuthority
from vsl_core.identity import Evidence

from app.governance import (
    ApprovalContext,
    GateInput,
    GovernanceRecorder,
    GovernedActionService,
    NoOpProtectedAction,
    open_ledger,
)

ACTION = "rerun_validation_dry_run"
AUTHORITY = GovernanceAuthority(name="DataOps owner", escalation_contact="owner@example.invalid")
EVIDENCE = Evidence(root_cause_analysis="Allowlist bypass attempt was a test input.",
                    specification_update_proposal="Keep the allowlist; add a review of proposal sources.")


def good(action: str = ACTION, **overrides) -> GateInput:
    base = dict(action=action, report_status="issue_identified", cited_evidence_ids=("E1",),
                known_evidence_ids=("E1", "E2"), approval=ApprovalContext("alice", action, "TICKET-1"))
    base.update(overrides)
    return GateInput(**base)


def make(tmp_path, *, runner=None, name="ledger.jsonl", **service_kwargs):
    runner = runner if runner is not None else NoOpProtectedAction()
    recorder = GovernanceRecorder(open_ledger(tmp_path / name, fsync=False))
    service = GovernedActionService(recorder, runner, **service_kwargs)
    return service, recorder, runner


def types(recorder) -> list[str]:
    return [e.entry_type.value for e in recorder.ledger.store.all_entries()]
