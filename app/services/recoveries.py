"""API-side coordination of Phase 7 controlled recovery.

This module is an ACCESS LAYER. It contains no governance logic and no path to a protected executor:

* a recovery request only calls ``RecoveryWorkflow.start`` (validate -> create approval request -> pause);
* a decision is relayed to ``ApprovalService.approve/reject`` WITH THE CALLER'S OWN CREDENTIAL; the approval service
  authenticates it and checks the per-action permission. No identity or "approved" flag from a request body is used;
* execution only ever happens inside ``RecoveryWorkflow.resume`` -> ``GovernedActionService`` (VSL gates) ->
  ``RecoveryRunner`` (attempt record, suspension re-check, atomic single-use consumption, execute, verify).

The operational ``recoveries`` row records what the workflow reported. It is never an authority: a stale or edited
row cannot cause an execution, because the approval store and the VSL gates decide every time.
"""

from __future__ import annotations

from typing import Any

from app.agent.models import InvestigationReport
from app.governance.ledger import GovernanceRecorder
from app.persistence import OperationsStore, RecoveryActive
from app.recovery import ApprovalDenied, ApprovalService, RecoveryOutcome, RecoveryWorkflow, proposal_from_report, suggest_recovery

__all__ = ["NoRecoverySuggested", "RecoveryActive", "RecoveryService", "RecoveryUnavailable"]


class RecoveryUnavailable(Exception):
    """Recovery cannot be offered in this configuration (for example the target database is not a *_test database)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class NoRecoverySuggested(Exception):
    """The investigation's evidence maps to no allowlisted recovery action."""


_VERIFIED = {"executed": "verified", "recovery_verification_failed": "not_verified", "recovery_uncertain": "unknown"}


def verified_outcome(outcome: RecoveryOutcome) -> str:
    if outcome.status in _VERIFIED:
        return _VERIFIED[outcome.status]
    if outcome.status == "governance_error":  # an error AFTER the effect must not be shown as "nothing happened"
        return {"executed": "verified", "succeeded_unrecorded": "verified", "uncertain": "unknown"}.get(outcome.execution_status, "not_applicable")
    return "not_applicable"


class RecoveryService:
    def __init__(self, store: OperationsStore, workflow: RecoveryWorkflow, approvals: ApprovalService,
                 recorder: GovernanceRecorder) -> None:
        self.store, self.workflow, self.approvals, self.recorder = store, workflow, approvals, recorder
        self._inflight: set[str] = set()

    # ------------------------------------------------------------------------------------------ request
    async def request(self, *, incident: dict[str, Any], investigation: dict[str, Any], requested_by: str,
                      explicit: tuple[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Create a recovery in 'awaiting_approval'. NEVER executes anything."""
        report = InvestigationReport.model_validate(investigation["report"])
        chosen = explicit or suggest_recovery(report)
        if chosen is None:
            raise NoRecoverySuggested()
        action_type, parameters = chosen
        proposal = proposal_from_report(report, action_type, parameters, incident_id=incident["incident_id"],
                                        reason=f"Requested via the API by {requested_by}")
        row = self.store.create_recovery(incident_id=incident["incident_id"], investigation_id=investigation["investigation_id"],
                                         action_type=action_type, parameters=parameters, requested_by=requested_by)
        outcome = await self.workflow.start(proposal)
        self.store.record_recovery_outcome(
            row["recovery_id"], status=outcome.status, execution_status=outcome.execution_status, reasons=outcome.reasons,
            approval_request_id=outcome.request_id, decision_id=outcome.decision_id, ledger_entry_ids=outcome.entry_ids,
            verified_outcome=verified_outcome(outcome))
        return self.store.get_recovery(row["recovery_id"])

    # ------------------------------------------------------------------------------------------ decision
    async def decide(self, *, request_id: str, decision: str, credential: str, reason: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Relay a human decision to the approval service, then let the Phase 7 workflow act on it.

        Raises ``ApprovalDenied`` (unauthenticated, not_permitted, expired, not_pending, ...) unchanged."""
        if decision == "approve":
            record = self.approvals.approve(request_id, credential, reason)
        else:
            record = self.approvals.reject(request_id, credential, reason)
        linked = self.store.get_recovery_by_request(request_id)
        recovery = await self.resume(linked["recovery_id"]) if linked else None
        return record.summary(), recovery

    # ------------------------------------------------------------------------------------------ resume
    async def resume(self, recovery_id: str) -> dict[str, Any] | None:
        """Re-evaluate a recovery after a human decision. Idempotent: only one caller wins the claim, and the
        workflow itself spends an approval at most once."""
        row = self.store.get_recovery(recovery_id)
        if row is None:
            return None
        row = self.reconcile(row)
        if row["status"] != "awaiting_approval" or not row["approval_request_id"]:
            return row
        if not self.store.claim_recovery_resume(recovery_id):
            return self.store.get_recovery(recovery_id)
        self._inflight.add(recovery_id)
        try:
            outcome = await self.workflow.resume(row["approval_request_id"])
            if outcome.awaiting_approval:  # still undecided: nothing ran, give the slot back
                self.store.release_recovery_claim(recovery_id)
            else:
                self.store.record_recovery_outcome(
                    recovery_id, status=outcome.status, execution_status=outcome.execution_status, reasons=outcome.reasons,
                    decision_id=outcome.decision_id, ledger_entry_ids=outcome.entry_ids,
                    verified_outcome=verified_outcome(outcome), started=True)
        finally:
            self._inflight.discard(recovery_id)
        return self.store.get_recovery(recovery_id)

    # ------------------------------------------------------------------------------------------ restart safety
    def reconcile(self, row: dict[str, Any]) -> dict[str, Any]:
        """A row stuck in 'resuming' (this process is not running it) means the process stopped mid-resume.
        If the approval was already spent we cannot know what happened: report UNCERTAIN, never success and never
        retry. If it was not spent, nothing ran and the slot is released."""
        if row["status"] != "resuming" or row["recovery_id"] in self._inflight:
            return row
        record = self.approvals.store.get(row["approval_request_id"]) if row["approval_request_id"] else None
        if record is not None and record.status == "consumed":
            self.store.record_recovery_outcome(
                row["recovery_id"], status="recovery_uncertain", execution_status="uncertain",
                reasons=["interrupted_before_result_recorded"], verified_outcome="unknown", started=True,
                ledger_entry_ids=row["ledger_entry_ids"])
        else:
            self.store.release_recovery_claim(row["recovery_id"])
        return self.store.get_recovery(row["recovery_id"]) or row

    def reconcile_all(self) -> int:
        stale = [r for r in self.store.list_resuming() if r["recovery_id"] not in self._inflight]
        for r in stale:
            self.reconcile(r)
        return len(stale)

    # ------------------------------------------------------------------------------------------ views
    def approval_summary(self, request_id: str) -> dict[str, Any] | None:
        record = self.approvals.store.get(request_id)
        return None if record is None else record.summary()

    def governance_events(self, entry_ids: list[str], request_id: str | None = None, decision_id: str | None = None) -> list[dict[str, Any]]:
        """Read-only view of the ledger trail of a recovery, in ledger order (outcome labels only). It includes the
        entries the workflow returned plus the approval's own request and decision entries."""
        wanted = set(entry_ids)
        if request_id:
            record = self.approvals.store.get(request_id)
            if record is not None:
                wanted |= {x for x in (record.ledger_request_entry, record.ledger_decision_entry) if x}
        try:
            entries = [e for e in self.recorder.ledger.store.all_entries() if e.entry_id in wanted or (decision_id and getattr(e, "decision_id", None) == decision_id)]
        except Exception:  # noqa: BLE001 - an unreadable ledger must not break a read endpoint
            return []
        return [{"entry_id": e.entry_id, "outcome": e.payload.get("outcome"), "type": e.entry_type.value, "caused_by": e.caused_by}
                for e in entries]


def denial_status(code: str) -> int:
    """HTTP status for an ApprovalDenied code."""
    if code == "unauthenticated":
        return 401
    if code == "not_permitted":
        return 403
    if code == "not_found":
        return 404
    if code == "ledger_error":
        return 503
    return 409
