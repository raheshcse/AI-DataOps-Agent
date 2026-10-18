"""Phase 6 unit tests: policy declarations, assurance derivation, conformance."""

import asyncio
import warnings

import pytest
from vsl_core.conformance.suite import run_conformance_suite
from vsl_core.exceptions import AutomationDeniedException, InvariantViolation
from vsl_core.metrics import AssuranceLevel
from vsl_langgraph import LangGraphAdapter

from app.governance import GovernanceConfigError, build_policy
from app.governance.gates import compile_gates
from app.governance.policy import ALLOWED_ACTIONS, FAIL_GAMMA, GateInput, failed_preconditions
from tests.governance_support import good


def run(coro):
    return asyncio.run(coro)


def test_the_official_conformance_suite_passes_for_the_langgraph_adapter():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert run_conformance_suite(LangGraphAdapter()) == []


def test_every_construct_derives_low_assurance_from_stated_f1_f2_facts():
    policy = build_policy()
    for construct in (policy.pre_node, policy.allowlist_invariant, policy.suspension_invariant):
        assert construct.assurance_basis.f1_pre_commitment is True
        assert construct.assurance_basis.f2_modification.value == "NONE"
        assert construct.assurance_level is AssuranceLevel.LOW


def test_the_policy_declares_a_terminal_that_needs_a_human_and_invariants_that_enter_it():
    p = build_policy()
    assert p.terminal.requires_human_authorised_transition is True
    assert p.allowlist_invariant.on_violation is p.terminal and p.suspension_invariant.on_violation is p.terminal
    assert p.allowlist_invariant.cannot_be_bypassed and p.suspension_invariant.cannot_be_bypassed


def test_the_allowlist_is_small_and_contains_no_destructive_names():
    assert ALLOWED_ACTIONS == {"rerun_validation_dry_run", "restart_stage_dry_run"}


@pytest.mark.parametrize("override,expected", [
    ({}, ()),
    ({"report_status": "inconclusive"}, ("report_status_not_actionable",)),
    ({"report_status": "no_issue_detected"}, ("report_status_not_actionable",)),
    ({"cited_evidence_ids": ()}, ("evidence_not_cited_or_unknown",)),
    ({"cited_evidence_ids": ("E9",)}, ("evidence_not_cited_or_unknown",)),
    ({"approval": None}, ("approval_missing_or_mismatched",)),
])
def test_preconditions(override, expected):
    assert failed_preconditions(good(**override)) == expected


def test_an_approval_for_another_action_or_by_nobody_is_invalid():
    from app.governance import ApprovalContext
    assert failed_preconditions(good(approval=ApprovalContext("alice", "restart_stage_dry_run")))
    assert failed_preconditions(good(approval=ApprovalContext("  ", "rerun_validation_dry_run")))


def test_prenode_allows_when_every_precondition_holds_and_denies_otherwise():
    gates = compile_gates(build_policy())
    run(gates.prenode(good()))  # returns normally
    for bad in (good(approval=None), good(report_status="failed"), good(cited_evidence_ids=())):
        with pytest.raises(AutomationDeniedException) as info:
            run(gates.prenode(bad))
        assert not isinstance(info.value, InvariantViolation)  # a soft denial, not an invariant violation


def test_the_monitor_gamma_is_a_binary_encoding_not_a_confidence():
    estimate = run(build_policy().pre_node.monitor(good(approval=None)))
    assert estimate.gamma_hat == FAIL_GAMMA and not estimate.sufficient(build_policy().pre_node.gamma_threshold)


def test_invariants_raise_the_specific_violation_naming_the_terminal_state():
    gates = compile_gates(build_policy())
    with pytest.raises(InvariantViolation) as info:
        run(gates.allowlist(good(action="drop_everything")))
    assert info.value.invariant_name == "action_within_allowlist"
    assert info.value.terminal_state_name == "automation_suspended"
    with pytest.raises(InvariantViolation) as info:
        run(gates.suspension(good(suspended=True)))
    assert info.value.invariant_name == "no_execution_while_suspended"
    run(gates.allowlist(good()))
    run(gates.suspension(good()))


def test_an_invalid_policy_is_refused_at_construction():
    from dataclasses import replace
    with pytest.raises(GovernanceConfigError):
        compile_gates(replace(build_policy(), allowed_actions=frozenset()))
    base = build_policy()
    with pytest.raises(GovernanceConfigError):
        compile_gates(replace(base, allowlist_invariant=replace(base.allowlist_invariant, on_violation=None)))
