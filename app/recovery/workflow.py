"""The human-in-the-loop recovery workflow (LangGraph) built around the Phase 6 governed workflow.

    route_entry
      |-- new proposal --> propose_action -> validate_action -> create_approval_request -> await_human_approval
      |                                                              (PreNode-style early denial)   |  pause (interrupt_before)
      \\-- request id only (after restart) ------------------------------------------> validate_approval <-+
                                   pending: back to await | usable: run_governed_recovery | else: record_unusable

``run_governed_recovery`` hands the request to the EXISTING ``GovernedActionService`` (suspension invariant,
allowlist invariant, PreNode, authorisation record) whose action runner is ``RecoveryRunner``. There is no
second governance engine and no path to the runner that skips the VSL gates.

Resuming carries NO authority. The pause is a static breakpoint (``interrupt_before``; ``interrupt()`` itself
cannot run in async nodes on Python 3.10) and nothing passed on resume is read; the decision exists only in the approval store,
where it was written by an authenticated, authorised human and sealed. A restart loses the in-memory graph
checkpoint but not the request: ``resume(request_id)`` then re-enters at ``validate_approval``.
"""

from __future__ import annotations

import operator
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from vsl_core.exceptions import AutomationDeniedException

from app.agent.models import InvestigationReport
from app.governance.ledger import GovernanceLedgerError, GovernanceRecorder, new_decision_id
from app.governance.policy import ApprovalContext, GateInput, failed_preconditions
from app.governance.workflow import GovernanceResult, GovernedActionService
from app.recovery.actions import ActionRejected, ValidatedAction, validate_action
from app.recovery.approvals import ApprovalDenied, ApprovalService, binding_digest
from app.recovery.runner import RecoveryContext
from app.simulation.scenarios import BY_ID, FAILURE_CODE_TO_SCENARIO


@dataclass(frozen=True)
class ProposedRecovery:
    """An UNTRUSTED proposal (from a model or a person). It is validated before anything happens."""
    incident_id: str
    action_type: str
    parameters: dict[str, Any]
    evidence_refs: tuple[str, ...]
    reason: str
    report_status: str
    known_evidence_ids: tuple[str, ...]


def proposal_from_report(report: InvestigationReport, action_type: str, parameters: dict[str, Any], *,
                         incident_id: str | None = None, reason: str = "") -> ProposedRecovery:
    cited = tuple(dict.fromkeys(report.likely_root_cause.evidence_ids)) if report.likely_root_cause.status == "supported" else ()
    return ProposedRecovery(
        incident_id=incident_id or report.investigation_id, action_type=action_type, parameters=dict(parameters),
        evidence_refs=cited, reason=reason or (report.likely_root_cause.statement or "")[:500],
        report_status=report.status, known_evidence_ids=tuple(e.evidence_id for e in report.evidence))


def suggest_recovery(report: InvestigationReport) -> tuple[str, dict[str, Any]] | None:
    """Deterministic, code-only suggestion: map a known failure code in the evidence to its simulated remedy.
    A model never chooses the action. This is a suggestion, not an authorisation."""
    for item in report.evidence:
        scenario_id = FAILURE_CODE_TO_SCENARIO.get(item.error_code or "")
        if scenario_id:
            return "remove_injected_condition", {"scenario_key": BY_ID[scenario_id].key}
    return None


@dataclass(frozen=True)
class RecoveryOutcome:
    status: str
    request_id: str | None = None
    decision_id: str | None = None
    reasons: tuple[str, ...] = ()
    execution_status: str = "not_run"
    entry_ids: tuple[str, ...] = ()
    governance: GovernanceResult | None = field(default=None, compare=False)

    @property
    def awaiting_approval(self) -> bool:
        return self.status == "awaiting_approval"

    @property
    def executed(self) -> bool:
        return self.status == "executed"


class RecState(TypedDict, total=False):
    proposal: ProposedRecovery
    validated: ValidatedAction
    request_id: str
    decision_id: str
    cause_entry_id: str | None
    outcome: str
    reasons: list[str]
    execution_status: str
    entry_ids: Annotated[list[str], operator.add]
    governance: GovernanceResult


_UNUSABLE = {"rejected": "rejected", "expired": "expired", "cancelled": "cancelled",
             "consumed": "approval_already_used", "invalid": "approval_invalid", "missing": "approval_invalid"}


class RecoveryWorkflow:
    def __init__(self, approvals: ApprovalService, governed: GovernedActionService, recorder: GovernanceRecorder,
                 *, checkpointer: Any = None) -> None:
        self.approvals, self.governed, self.recorder = approvals, governed, recorder
        self._checkpointer = checkpointer or InMemorySaver()
        self._threads: dict[str, str] = {}  # request_id -> thread id (in-process only; lost on restart)
        self.graph = self._build()

    # ------------------------------------------------------------------------------------------ graph
    def _build(self):
        rec, approvals, governed = self.recorder, self.approvals, self.governed

        def note(state: RecState, outcome: str, **payload: Any):
            return rec.monitor(decision_id=state["decision_id"], drift=False, caused_by=state.get("cause_entry_id"),
                               outcome=outcome, **payload)

        def route_entry(state: RecState) -> str:
            return "validate_approval" if state.get("request_id") and not state.get("proposal") else "propose_action"

        async def propose_action(state: RecState) -> dict[str, Any]:
            p = state["proposal"]
            entry = note(state, "recovery_proposed", action=str(p.action_type)[:60], incident=str(p.incident_id)[:64])
            return {"cause_entry_id": entry.entry_id, "entry_ids": [entry.entry_id]}

        async def validate_action_node(state: RecState) -> dict[str, Any]:
            p = state["proposal"]
            try:
                return {"validated": validate_action(p.action_type, p.parameters)}
            except ActionRejected as exc:
                entry = note(state, "recovery_action_rejected", reason=str(exc)[:120])
                return {"outcome": "action_rejected", "reasons": [str(exc)[:120]], "entry_ids": [entry.entry_id]}

        def after_validation(state: RecState) -> str:
            return "create_approval_request" if state.get("validated") else END

        async def create_approval_request(state: RecState) -> dict[str, Any]:
            p, validated = state["proposal"], state["validated"]
            suspended, terminal_id = rec.is_suspended()
            if suspended:
                entry = rec.monitor(decision_id=state["decision_id"], drift=False, caused_by=terminal_id,
                                    outcome="recovery_refused_suspended", action=validated.action_type)
                return {"outcome": "refused_suspended", "reasons": ["automation_suspended"], "entry_ids": [entry.entry_id]}
            early = GateInput(action=validated.action_type, report_status=p.report_status,
                              cited_evidence_ids=p.evidence_refs, known_evidence_ids=p.known_evidence_ids)
            failed = [f for f in failed_preconditions(early) if f != "approval_missing_or_mismatched"]
            if failed:  # a normal PreNode denial recorded by the Phase 6 gates (SUFFICIENT: governance worked)
                res = await governed.govern(early, decision_id=state["decision_id"], caused_by=state.get("cause_entry_id"))
                return {"outcome": res.outcome, "reasons": list(res.reasons), "entry_ids": list(res.entry_ids),
                        "governance": res}
            try:
                record = approvals.create_request(
                    incident_id=p.incident_id, action=validated, evidence_refs=p.evidence_refs, reason=p.reason,
                    governance_state="active", report_status=p.report_status,
                    caused_by=state.get("cause_entry_id"), decision_id=state["decision_id"])
            except ApprovalDenied as exc:
                entry = note(state, "recovery_action_rejected", reason=exc.code)
                return {"outcome": "action_rejected", "reasons": [exc.code], "entry_ids": [entry.entry_id]}
            return {"request_id": record.request_id, "entry_ids": [record.ledger_request_entry or ""]}

        def after_create(state: RecState) -> str:
            return "await_human_approval" if state.get("request_id") and not state.get("outcome") else END

        async def await_human_approval(state: RecState) -> dict[str, Any]:
            # The pause happens BEFORE this node (``interrupt_before``): a LangGraph static breakpoint. The
            # node itself does nothing, and whatever a caller passes when resuming is never read.
            return {}

        async def validate_approval(state: RecState) -> dict[str, Any]:
            verdict = approvals.check(state["request_id"])
            validated, p = state.get("validated"), state.get("proposal")
            if verdict.state == "approved_valid" and validated and p and verdict.record and \
                    verdict.record.binding_digest != binding_digest(
                        p.incident_id, validated.action_type, validated.parameters, validated.target,
                        p.evidence_refs, p.report_status):
                return {"outcome": "approval_invalid", "reasons": ["workflow_state_differs_from_approved_request"],
                        "execution_status": "invalid"}
            return {"reasons": [verdict.reason],
                    "outcome": "" if verdict.state in ("approved_valid", "pending")
                    else _UNUSABLE.get(verdict.state, "approval_invalid"),
                    "execution_status": verdict.state}

        def after_validate(state: RecState) -> str:
            if state.get("outcome"):
                return "record_unusable"
            return "run_governed_recovery" if state.get("execution_status") == "approved_valid" else "await_human_approval"

        async def record_unusable(state: RecState) -> dict[str, Any]:
            record = approvals.store.get(state["request_id"])
            entry = rec.monitor(decision_id=state["decision_id"], drift=False,
                                caused_by=record.ledger_request_entry if record else None,
                                outcome="approval_not_usable", request_id=state["request_id"],
                                state=state["outcome"], reason=(state.get("reasons") or [""])[0][:80])
            return {"entry_ids": [entry.entry_id], "execution_status": "not_run"}

        async def run_governed_recovery(state: RecState) -> dict[str, Any]:
            record = approvals.store.get(state["request_id"])
            if record is None:
                return {"outcome": "approval_invalid", "reasons": ["missing"], "execution_status": "not_run"}
            candidate = GateInput(
                action=record.action_type, report_status=record.report_status,
                cited_evidence_ids=record.evidence_refs, known_evidence_ids=record.evidence_refs,
                approval=ApprovalContext(approved_by=record.decided_by or "", approved_action=record.action_type,
                                         reference=record.request_id),
                context=RecoveryContext(record.request_id, record.binding_digest, state["decision_id"]))
            res = await governed.govern(candidate, decision_id=state["decision_id"], caused_by=record.ledger_decision_entry)
            return {"outcome": res.outcome, "reasons": list(res.reasons), "entry_ids": list(res.entry_ids),
                    "execution_status": res.execution_status, "governance": res}

        g = StateGraph(RecState)
        for name, fn in (("propose_action", propose_action), ("validate_action", validate_action_node),
                         ("create_approval_request", create_approval_request),
                         ("await_human_approval", await_human_approval), ("validate_approval", validate_approval),
                         ("record_unusable", record_unusable), ("run_governed_recovery", run_governed_recovery)):
            g.add_node(name, fn)
        g.add_conditional_edges(START, route_entry, ["propose_action", "validate_approval"])
        g.add_edge("propose_action", "validate_action")
        g.add_conditional_edges("validate_action", after_validation, ["create_approval_request", END])
        g.add_conditional_edges("create_approval_request", after_create, ["await_human_approval", END])
        g.add_edge("await_human_approval", "validate_approval")
        g.add_conditional_edges("validate_approval", after_validate,
                                ["record_unusable", "run_governed_recovery", "await_human_approval"])
        g.add_edge("record_unusable", END)
        g.add_edge("run_governed_recovery", END)
        return g.compile(checkpointer=self._checkpointer, interrupt_before=["await_human_approval"])

    # ------------------------------------------------------------------------------------- entry points
    async def _run(self, payload: Any, thread_id: str) -> RecoveryOutcome:
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 30}
        try:
            await self.graph.ainvoke(payload, config)
            snapshot = await self.graph.aget_state(config)
        except AutomationDeniedException:
            return RecoveryOutcome("blocked", reasons=("unexpected_denial",))
        except Exception as exc:  # noqa: BLE001 - fail closed: any failure means nothing further is executed
            reason = type(exc).__name__ if isinstance(exc, (GovernanceLedgerError, ApprovalDenied)) \
                else "workflow_failure:" + type(exc).__name__
            return RecoveryOutcome("governance_error", reasons=(reason,))
        values = snapshot.values
        request_id = values.get("request_id")
        if request_id:
            self._threads[request_id] = thread_id
        if "await_human_approval" in snapshot.next:
            return RecoveryOutcome("awaiting_approval", request_id, values.get("decision_id"),
                                   entry_ids=tuple(values.get("entry_ids", ())))
        return RecoveryOutcome(values.get("outcome") or "governance_error", request_id, values.get("decision_id"),
                               tuple(values.get("reasons", ())), values.get("execution_status", "not_run"),
                               tuple(e for e in values.get("entry_ids", ()) if e), values.get("governance"))

    async def start(self, proposal: ProposedRecovery) -> RecoveryOutcome:
        """Validate the proposal and create an approval request, then pause for a human. Nothing is executed."""
        thread = "rec-" + uuid.uuid4().hex
        return await self._run({"proposal": proposal, "decision_id": new_decision_id(), "entry_ids": []}, thread)

    async def resume(self, request_id: str) -> RecoveryOutcome:
        """Re-evaluate a request after a human has (or has not) decided. Safe to call repeatedly: an approval
        can be spent once. Works after a restart because the decision lives in the store, not in the graph."""
        thread = self._threads.get(request_id)
        if thread is not None:
            snapshot = await self.graph.aget_state({"configurable": {"thread_id": thread}})
            if snapshot.next:
                return await self._run(None, thread)  # continue the paused thread; nothing is passed in
        return await self._run({"request_id": request_id, "decision_id": new_decision_id(), "entry_ids": []},
                               "rec-" + uuid.uuid4().hex)
