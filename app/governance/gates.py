"""Compile the policy into VSL gates with the LangGraph adapter.

Each gate has its own denial key so one gate's denial can never be read as another's. Only the workflow
entry point (``app.governance.workflow``) imports this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from vsl_core.conformance.protocol import CompiledGate
from vsl_langgraph import LangGraphAdapter

from app.governance.ledger import GovernanceConfigError
from app.governance.policy import GateInput, Policy

DENIAL_SUSPENDED = "vsl_denial_suspended"
DENIAL_ALLOWLIST = "vsl_denial_allowlist"
DENIAL_PRENODE = "vsl_denial_prenode"
DENIAL_KEYS = (DENIAL_SUSPENDED, DENIAL_ALLOWLIST, DENIAL_PRENODE)


@dataclass(frozen=True)
class Gates:
    suspension: CompiledGate
    allowlist: CompiledGate
    prenode: CompiledGate


def validate_policy(policy: Policy) -> None:
    if not policy.allowed_actions:
        raise GovernanceConfigError("the action allowlist is empty")
    if policy.allowlist_invariant.on_violation is None or policy.suspension_invariant.on_violation is None:
        raise GovernanceConfigError("every invariant must name its TerminalState")
    if not policy.terminal.requires_human_authorised_transition:
        raise GovernanceConfigError("the terminal state must require a human-authorised transition")


def compile_gates(policy: Policy, adapter: Any = None) -> Gates:
    validate_policy(policy)
    vsl = adapter or LangGraphAdapter()
    return Gates(
        suspension=vsl.compile_invariant(policy.suspension_invariant),
        allowlist=vsl.compile_invariant(policy.allowlist_invariant),
        prenode=vsl.compile_pre_node(policy.pre_node),
    )


def candidate_of(state: Mapping[str, Any]) -> GateInput:
    return state["candidate"]
