"""The recovery runner: the ONLY code that performs a recovery side effect.

It is plugged into the existing Phase 6 ``GovernedActionService`` as its action runner, so it is reached only
after the VSL suspension invariant, allowlist invariant and PreNode have passed and an authorisation record
has been written. Even then it re-checks everything that matters, in this order, immediately before acting:

1. write a ``recovery_attempt`` record and read it back (ledger persistence + chain verified before acting)
2. suspension re-check (time-of-check/time-of-use)
3. read-only precondition
4. atomic, single-use consumption of the approval (binding, seal, expiry, approver role all re-validated)
5. execute, then verify the postcondition independently
6. write the result record

Outcome vocabulary returned to the workflow (``status``): ``executed`` (done AND verified), ``blocked``
(nothing happened), ``failed`` (precondition failed, nothing happened), ``verification_failed`` (it ran but the
postcondition is not met), ``uncertain`` (an exception during/after the side effect: the effect is unknown),
``succeeded_unrecorded`` (done and verified but the result record could not be written).

Nothing is retried automatically. A consumed approval is never reusable, so a second attempt needs a new
request and a new human decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vsl_core.exceptions import AutomationDeniedException

from app.governance.ledger import GovernanceLedgerError, GovernanceRecorder
from app.recovery.actions import PreconditionFailed, RecoveryExecutor
from app.recovery.approvals import ApprovalDenied, ApprovalService, validate_action_safely


@dataclass(frozen=True)
class RecoveryContext:
    """Identifies WHICH approved request to run. It carries no authority and no parameters."""
    request_id: str
    binding_digest: str
    decision_id: str


class RecoveryRunner:
    def __init__(self, executor: RecoveryExecutor, approvals: ApprovalService, recorder: GovernanceRecorder) -> None:
        self.executor, self.approvals, self.recorder = executor, approvals, recorder

    def execute(self, action: str, authorisation_entry_id: str, *, context: Any = None) -> dict[str, Any]:
        if not isinstance(context, RecoveryContext) or not authorisation_entry_id:
            return {"status": "blocked", "reasons": ["no_recovery_context"]}
        record = self.approvals.store.get(context.request_id)
        validated = validate_action_safely(record) if record else None
        if record is None or validated is None or record.action_type != action \
                or record.binding_digest != context.binding_digest:
            return {"status": "blocked", "reasons": ["request_does_not_match_action"]}
        rec, did = self.recorder, context.decision_id

        # 1. attempt record, proven to be persisted before anything else happens
        try:
            attempt = rec.monitor(decision_id=did, drift=False, caused_by=authorisation_entry_id,
                                  outcome="recovery_attempt", request_id=record.request_id, action=action,
                                  target=record.target)
            rec.verify_or_raise()
            if rec.get_entry(attempt.entry_id) is None:
                raise GovernanceLedgerError("attempt record not found after writing")
        except GovernanceLedgerError:
            return {"status": "blocked", "reasons": ["ledger_not_verified_before_execution"]}

        def finish(status: str, outcome: str, reasons: list[str], **payload: Any) -> dict[str, Any]:
            try:
                rec.monitor(decision_id=did, drift=False, caused_by=attempt.entry_id, outcome=outcome,
                            request_id=record.request_id, action=action, **payload)
            except GovernanceLedgerError:
                if status == "executed":
                    return {"status": "succeeded_unrecorded", "reasons": ["result_record_failed", *reasons]}
                if status == "uncertain":
                    return {"status": "uncertain", "reasons": ["result_record_failed", *reasons]}
            return {"status": status, "reasons": reasons}

        # 2. suspension re-check right before the side effect
        try:
            suspended, _ = rec.is_suspended()
        except GovernanceLedgerError:
            return finish("blocked", "recovery_blocked", ["ledger_unreadable_before_execution"])
        if suspended:
            return finish("blocked", "recovery_blocked", ["suspended_before_execution"])

        # 3. read-only precondition (no side effect has happened)
        try:
            facts = self.executor.check_precondition(validated)
        except PreconditionFailed as exc:
            return finish("failed", "recovery_failed", ["precondition_failed"], detail=str(exc)[:120],
                          side_effect="none")
        except AutomationDeniedException:
            raise
        except Exception as exc:  # noqa: BLE001 - read-only step; nothing has changed, so this is a safe failure
            return finish("failed", "recovery_failed", ["precondition_error"], error=type(exc).__name__,
                          side_effect="none")

        # 4. atomic single-use consumption (re-validates binding, seal, expiry and approver role)
        try:
            self.approvals.consume(record.request_id, did, record.binding_digest)
        except ApprovalDenied as exc:
            return finish("blocked", "recovery_blocked", [f"approval_not_consumable:{exc.code}"])
        except AutomationDeniedException:
            raise
        except Exception as exc:  # noqa: BLE001 - store failure before any side effect: block
            return finish("blocked", "recovery_blocked", [f"approval_store_error:{type(exc).__name__}"])

        # 5. side effect, then independent verification
        try:
            detail = self.executor.execute(validated, facts)
        except AutomationDeniedException:
            raise
        except Exception as exc:  # noqa: BLE001 - the effect may be partial: report UNCERTAIN, never success
            return finish("uncertain", "recovery_uncertain", ["exception_during_execution"], error=type(exc).__name__)
        try:
            result = self.executor.verify(validated, facts, detail)
        except AutomationDeniedException:
            raise
        except Exception as exc:  # noqa: BLE001
            return finish("uncertain", "recovery_uncertain", ["exception_during_verification"], error=type(exc).__name__)
        if not result.verified:
            return finish("verification_failed", "recovery_verification_failed", ["postcondition_not_met"],
                          verification=result.verification)
        return finish("executed", "recovery_succeeded", [], verification=result.verification)
