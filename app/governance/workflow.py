"""The governed protected-action workflow (the entry point of Phase 6 governance).

    load_context -> guard_suspension -> guard_allowlist -> guard_prenode -> execute_action -> END
                        | denied            | denied            | denied
                  record_refusal      record_terminal    route_to_human_review      (each -> END)

Every gate completes before the node it guards. ``execute_action`` is reachable only along the approved edge
and refuses to run without an authorisation ledger entry. Any other failure (ledger unreadable, broken hash
chain, ledger write failure, a gate that errors) ends as ``governance_error``: nothing is executed, and an
error is never approval.

Investigation is separate and unaffected: it uses only read-only MCP tools and is not gated.
"""

from __future__ import annotations

import asyncio
import operator
from dataclasses import dataclass, replace
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from vsl_core.exceptions import AutomationDeniedException, InvariantViolation
from vsl_core.ledger import LedgerEntryType, VerificationResult
from vsl_langgraph import gated_node, route_on_denial

from app.agent.models import InvestigationReport
from app.governance.actions import NoOpProtectedAction, ProtectedActionRunner, UnauthorisedExecution
from app.governance.gates import (
    DENIAL_ALLOWLIST,
    DENIAL_KEYS,
    DENIAL_PRENODE,
    DENIAL_SUSPENDED,
    candidate_of,
    compile_gates,
)
from app.governance.ledger import GovernanceLedgerError, GovernanceRecorder, new_decision_id
from app.governance.policy import (
    ApprovalContext,
    GateInput,
    Policy,
    build_policy,
    failed_preconditions,
)

OUTCOMES = ("executed", "denied_human_review", "terminal_suspended", "refused_suspended", "governance_error",
            "blocked")


class GovState(TypedDict, total=False):
    decision_id: str
    proposal: GateInput
    candidate: GateInput
    open_terminal_id: str | None
    vsl_denial_suspended: Any
    vsl_denial_allowlist: Any
    vsl_denial_prenode: Any
    authorisation_entry_id: str
    outcome: str
    reasons: list[str]
    entry_ids: Annotated[list[str], operator.add]


@dataclass(frozen=True)
class GovernanceResult:
    outcome: str
    decision_id: str
    action: str
    executed: bool
    reasons: tuple[str, ...] = ()
    entry_ids: tuple[str, ...] = ()

    @property
    def approved(self) -> bool:
        return self.outcome == "executed"


def proposal_from_report(report: InvestigationReport, action: str,
                         approval: ApprovalContext | None = None) -> GateInput:
    """Turn a Phase 5 report into gate input. Only the report's status and evidence ids are used."""
    known = tuple(item.evidence_id for item in report.evidence)
    cited = tuple(dict.fromkeys(report.likely_root_cause.evidence_ids)) if report.likely_root_cause.status == "supported" else ()
    return GateInput(action=action, report_status=report.status, cited_evidence_ids=cited,
                     known_evidence_ids=known, approval=approval)


def _safe(text: Any, limit: int = 100) -> str:
    return str(text)[:limit]


class GovernedActionService:
    """Decides whether a proposed protected action may run, records the trail, and runs the stub if allowed."""

    def __init__(self, recorder: GovernanceRecorder, runner: ProtectedActionRunner | None = None,
                 policy: Policy | None = None, adapter: Any = None) -> None:
        self.recorder = recorder
        self.runner: ProtectedActionRunner = runner if runner is not None else NoOpProtectedAction()
        self.policy = policy or build_policy()
        self.gates = compile_gates(self.policy, adapter)  # raises GovernanceConfigError when invalid
        self._lock = asyncio.Lock()
        self._executed_decision: str | None = None
        self.graph = self._build_graph()

    # ---- graph -----------------------------------------------------------------------------------------
    def _build_graph(self):
        rec = self.recorder

        async def load_context(state: GovState) -> dict[str, Any]:
            rec.verify_or_raise()
            suspended, terminal_id = rec.is_suspended()
            # The caller's own `suspended` claim is ignored: the ledger decides.
            return {"candidate": replace(state["proposal"], suspended=suspended), "open_terminal_id": terminal_id}

        def passthrough(_state: GovState) -> dict[str, Any]:
            return {}

        def record_approval(state: GovState) -> dict[str, Any]:
            cand = candidate_of(state)
            entry = rec.monitor(decision_id=state["decision_id"], drift=False, outcome="authorised",
                                action=_safe(cand.action),
                                approved_by=_safe(cand.approval.approved_by if cand.approval else ""))
            return {"authorisation_entry_id": entry.entry_id, "entry_ids": [entry.entry_id]}

        guard_suspension = gated_node(self.gates.suspension, passthrough, candidate_input_fn=candidate_of,
                                      denial_key=DENIAL_SUSPENDED)
        guard_allowlist = gated_node(self.gates.allowlist, passthrough, candidate_input_fn=candidate_of,
                                     denial_key=DENIAL_ALLOWLIST)
        guard_prenode = gated_node(self.gates.prenode, record_approval, candidate_input_fn=candidate_of,
                                   denial_key=DENIAL_PRENODE)

        async def record_refusal(state: GovState) -> dict[str, Any]:
            cand = candidate_of(state)
            entry = rec.monitor(decision_id=state["decision_id"], drift=False, caused_by=state.get("open_terminal_id"),
                                outcome="refused_suspended", action=_safe(cand.action))
            return {"outcome": "refused_suspended", "reasons": ["automation_suspended"], "entry_ids": [entry.entry_id]}

        async def record_terminal(state: GovState) -> dict[str, Any]:
            cand = candidate_of(state)
            violation = state[DENIAL_ALLOWLIST]
            name = violation.invariant_name if isinstance(violation, InvariantViolation) else "unknown_invariant"
            rec.mark_suspended_locally()  # suspend first, so a failed write below still blocks further actions
            monitor = rec.monitor(decision_id=state["decision_id"], drift=False, outcome="invariant_violated",
                                  invariant=name, action=_safe(cand.action))
            verification = rec.verification(decision_id=state["decision_id"], result=VerificationResult.INSUFFICIENT,
                                            caused_by=monitor.entry_id, invariant=name)
            terminal = rec.entry(LedgerEntryType.TERMINAL, decision_id=state["decision_id"],
                                 caused_by=verification.entry_id, terminal_state=self.policy.terminal.name,
                                 invariant=name)
            return {"outcome": "terminal_suspended", "reasons": [name],
                    "entry_ids": [monitor.entry_id, verification.entry_id, terminal.entry_id]}

        async def route_to_human_review(state: GovState) -> dict[str, Any]:
            cand = candidate_of(state)
            failed = list(failed_preconditions(cand))
            monitor = rec.monitor(decision_id=state["decision_id"], drift=True, outcome="preconditions_failed",
                                  action=_safe(cand.action), failed=failed)
            pre_node = rec.entry(LedgerEntryType.PRE_NODE, decision_id=state["decision_id"], caused_by=monitor.entry_id,
                                 pre_node=self.policy.pre_node.name, failed=failed)
            verification = rec.verification(decision_id=state["decision_id"], result=VerificationResult.SUFFICIENT,
                                            caused_by=pre_node.entry_id, outcome="routed_to_human_review")
            return {"outcome": "denied_human_review", "reasons": failed,
                    "entry_ids": [monitor.entry_id, pre_node.entry_id, verification.entry_id]}

        async def execute_action(state: GovState) -> dict[str, Any]:
            auth = state.get("authorisation_entry_id")
            if not auth or any(state.get(key) is not None for key in DENIAL_KEYS):
                raise UnauthorisedExecution("execute_action reached without a passing gate sequence")
            cand = candidate_of(state)
            self.runner.execute(cand.action, auth)
            self._executed_decision = state["decision_id"]
            done = rec.monitor(decision_id=state["decision_id"], drift=False, caused_by=auth, outcome="executed",
                               action=_safe(cand.action))
            return {"outcome": "executed", "reasons": [], "entry_ids": [done.entry_id]}

        g = StateGraph(GovState)
        for name, fn in (("load_context", load_context), ("guard_suspension", guard_suspension),
                         ("guard_allowlist", guard_allowlist), ("guard_prenode", guard_prenode),
                         ("record_refusal", record_refusal), ("record_terminal", record_terminal),
                         ("route_to_human_review", route_to_human_review), ("execute_action", execute_action)):
            g.add_node(name, fn)
        g.add_edge(START, "load_context")
        g.add_edge("load_context", "guard_suspension")
        g.add_conditional_edges("guard_suspension", route_on_denial("record_refusal", "guard_allowlist",
                                                                   denial_key=DENIAL_SUSPENDED),
                                ["record_refusal", "guard_allowlist"])
        g.add_conditional_edges("guard_allowlist", route_on_denial("record_terminal", "guard_prenode",
                                                                  denial_key=DENIAL_ALLOWLIST),
                                ["record_terminal", "guard_prenode"])
        g.add_conditional_edges("guard_prenode", route_on_denial("route_to_human_review", "execute_action",
                                                                denial_key=DENIAL_PRENODE),
                                ["route_to_human_review", "execute_action"])
        for end in ("record_refusal", "record_terminal", "route_to_human_review", "execute_action"):
            g.add_edge(end, END)
        return g.compile()

    # ---- entry points ----------------------------------------------------------------------------------
    async def govern(self, proposal: GateInput) -> GovernanceResult:
        decision_id = new_decision_id()
        action = _safe(proposal.action)
        async with self._lock:
            try:
                state = await self.graph.ainvoke({"decision_id": decision_id, "proposal": proposal, "entry_ids": []})
            except AutomationDeniedException:
                # A denial that escaped its gate: still a block, never an approval.
                return GovernanceResult("blocked", decision_id, action, False, ("unexpected_denial",))
            except Exception as exc:  # noqa: BLE001 - fail closed: any other failure means no protected action
                executed = self._executed_decision == decision_id
                reason = type(exc).__name__ if isinstance(exc, (GovernanceLedgerError, UnauthorisedExecution)) \
                    else "gate_or_graph_failure:" + type(exc).__name__
                return GovernanceResult("governance_error", decision_id, action, executed, (reason,))
        outcome = state.get("outcome", "governance_error")
        return GovernanceResult(outcome, decision_id, action, outcome == "executed",
                                tuple(state.get("reasons", ())), tuple(state.get("entry_ids", ())))

    def govern_sync(self, proposal: GateInput) -> GovernanceResult:
        return asyncio.run(self.govern(proposal))
