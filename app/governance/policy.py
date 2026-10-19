"""VSL policy declarations for protected actions. Imports ``vsl_core`` (and the standard library) only.

What is governed
----------------
Only a *protected action*: a hypothetical write/recovery step proposed after an investigation. Phase 6 has no
real recovery action; the thing behind the gates is a no-op stub (``app.governance.actions``). The eight
read-only MCP tools and the investigation graph are NOT gated.

Constructs
----------
* Invariant ``no_execution_while_suspended``   - hard: nothing runs while a TerminalState is open.
* Invariant ``action_within_allowlist``        - hard: an action not on ``ALLOWED_ACTIONS`` is never allowed.
* PreNode  ``verified_report_and_approval``    - soft: a failed precondition routes to human review.
* TerminalState ``automation_suspended``       - entered on an invariant violation; cleared only by a
  human-authorised transition (see ``app.governance.ledger``).

Honesty notes
-------------
* The PreNode monitor is deterministic. VSL's PreNode contract needs a ``GammaEstimate``, so the monitor
  encodes "every precondition holds" as ``PASS_GAMMA`` and "at least one fails" as ``FAIL_GAMMA``. These are
  NOT calibrated confidences and not a measured Gamma, and model confidence is never an input.
* Assurance is derived from F1/F2 facts: the gates run before the action (F1 true) but do not modify any
  model energy function (F2 NONE), so every construct derives LOW. That is the honest level.
* ``ApprovalContext`` is a plain, unauthenticated value in Phase 6. Nothing issues or verifies approvals until
  the Phase 7 approval workflow, so by default no action is approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vsl_core.constructs import Invariant, PreNode, TerminalState
from vsl_core.metrics import (
    ROBUST_GAMMA_DEFAULT_THRESHOLD,
    AssuranceBasis,
    F2Modification,
    GammaEstimate,
)

# Actions the governed boundary may ever run. Both are dry-run labels; neither touches a database.
ALLOWED_ACTIONS: frozenset[str] = frozenset({"rerun_validation_dry_run", "restart_stage_dry_run"})

# Report status that justifies proposing any action at all.
ACTIONABLE_REPORT_STATUS = "issue_identified"

# Binary encoding of "all preconditions hold" for the PreNode contract (see module docstring).
PASS_GAMMA = 2.0
FAIL_GAMMA = 0.0

TERMINAL_NAME = "automation_suspended"
PRE_NODE_NAME = "verified_report_and_approval"
INVARIANT_ALLOWLIST = "action_within_allowlist"
INVARIANT_SUSPENDED = "no_execution_while_suspended"

# F1: the gate completes before the action. F2 NONE: it does not touch a model's energy function.
GATE_BASIS = AssuranceBasis(f1_pre_commitment=True, f2_modification=F2Modification.NONE)


@dataclass(frozen=True)
class ApprovalContext:
    """Who approved which action. Unauthenticated in Phase 6 (see module docstring)."""

    approved_by: str
    approved_action: str
    reference: str = ""

    def is_valid_for(self, action: str) -> bool:
        return bool(self.approved_by.strip()) and self.approved_action == action


@dataclass(frozen=True)
class GateInput:
    """Everything a gate may look at. All of it is verifiable data; none of it is model confidence."""

    action: str
    report_status: str
    cited_evidence_ids: tuple[str, ...] = ()
    known_evidence_ids: tuple[str, ...] = ()
    approval: ApprovalContext | None = None
    suspended: bool = False
    # Opaque, caller-built data handed to the action runner (never read by a gate). Phase 7 puts the
    # validated request id and parameters here.
    context: object | None = None


def failed_preconditions(candidate: GateInput) -> tuple[str, ...]:
    """Names of the PreNode preconditions that do not hold (empty = all hold)."""
    failed: list[str] = []
    if candidate.report_status != ACTIONABLE_REPORT_STATUS:
        failed.append("report_status_not_actionable")
    known = set(candidate.known_evidence_ids)
    cited = candidate.cited_evidence_ids
    if not cited or not all(item in known for item in cited):
        failed.append("evidence_not_cited_or_unknown")
    if candidate.approval is None or not candidate.approval.is_valid_for(candidate.action):
        failed.append("approval_missing_or_mismatched")
    return tuple(failed)


async def _preconditions_monitor(candidate: GateInput) -> GammaEstimate:
    return GammaEstimate(gamma_hat=FAIL_GAMMA if failed_preconditions(candidate) else PASS_GAMMA)


async def _not_suspended_rule(candidate: GateInput) -> bool:
    return not candidate.suspended


@dataclass(frozen=True)
class Policy:
    pre_node: PreNode
    allowlist_invariant: Invariant
    suspension_invariant: Invariant
    terminal: TerminalState
    allowed_actions: frozenset[str] = field(default=ALLOWED_ACTIONS)


def build_policy(allowed_actions: frozenset[str] = ALLOWED_ACTIONS) -> Policy:
    allowed = frozenset(allowed_actions)

    async def _allowlist_rule(candidate: GateInput) -> bool:
        return candidate.action in allowed

    terminal = TerminalState(
        name=TERMINAL_NAME,
        description="Protected actions are suspended. Cleared only by a human-authorised transition.",
        entry_conditions=("an invariant was violated",),
        requires_human_authorised_transition=True,
    )
    return Policy(
        pre_node=PreNode(
            name=PRE_NODE_NAME,
            description="Actionable report, cited known evidence and a matching approval are required.",
            monitor=_preconditions_monitor,
            assurance_basis=GATE_BASIS,
            gamma_threshold=ROBUST_GAMMA_DEFAULT_THRESHOLD,
        ),
        allowlist_invariant=Invariant(
            name=INVARIANT_ALLOWLIST,
            description="Only allowlisted protected actions may ever run.",
            rule=_allowlist_rule,
            assurance_basis=GATE_BASIS,
            on_violation=terminal,
        ),
        suspension_invariant=Invariant(
            name=INVARIANT_SUSPENDED,
            description="No protected action runs while a terminal state is open.",
            rule=_not_suspended_rule,
            assurance_basis=GATE_BASIS,
            on_violation=terminal,
        ),
        terminal=terminal,
        allowed_actions=allowed,
    )
