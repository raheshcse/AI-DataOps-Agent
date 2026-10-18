"""Phase 6 integration tests: the real LangGraph governed workflow with a counting no-op action."""

import asyncio
from dataclasses import replace

import pytest
from vsl_core.exceptions import AutomationDeniedException

from app.governance import (
    GovernanceConfigError,
    GovernanceLedgerError,
    GovernanceRecorder,
    GovernedActionService,
    NoOpProtectedAction,
    build_policy,
    open_ledger,
)
from app.governance.actions import UnauthorisedExecution
from app.governance.gates import DENIAL_KEYS
from tests.governance_support import ACTION, AUTHORITY, EVIDENCE, good, make, types


class Boom(Exception):
    pass


def test_an_approved_action_reaches_the_stub_after_the_ledger_records_authorisation(tmp_path):
    service, rec, runner = make(tmp_path)
    result = service.govern_sync(good())
    assert result.outcome == "executed" and result.executed and result.approved
    assert len(runner.executed) == 1 and runner.executed[0][0] == ACTION
    entries = list(rec.ledger.store.all_entries())
    assert [e.entry_type.value for e in entries] == ["MONITOR", "MONITOR"]
    assert runner.executed[0][1] == entries[0].entry_id  # the stub was handed the authorisation entry
    assert entries[1].caused_by == entries[0].entry_id
    assert not any(e.payload.get("drift_detected") for e in entries)
    assert rec.ledger.verify_integrity() and rec.ledger.audit().all_passed


@pytest.mark.parametrize("override", [
    {"approval": None}, {"report_status": "inconclusive"}, {"cited_evidence_ids": ("E404",)}, {"cited_evidence_ids": ()},
])
def test_a_prenode_denial_routes_to_human_review_without_the_side_effect(tmp_path, override):
    service, rec, runner = make(tmp_path)
    result = service.govern_sync(good(**override))
    assert result.outcome == "denied_human_review" and not result.executed and result.reasons
    assert runner.executed == []
    entries = list(rec.ledger.store.all_entries())
    assert [e.entry_type.value for e in entries] == ["MONITOR", "PRE_NODE", "VERIFICATION"]
    monitor, pre, ver = entries
    assert monitor.payload["drift_detected"] is True  # exactly because a PRE_NODE follows
    assert pre.caused_by == monitor.entry_id and ver.caused_by == pre.entry_id
    assert ver.payload["result"] == "SUFFICIENT"  # governance worked
    assert rec.ledger.verify_integrity()
    assert rec.ledger.audit().all_passed  # an ordinary denial never fails the audit
    assert not rec.is_suspended()[0]


def test_an_ordinary_denial_does_not_suspend_automation(tmp_path):
    service, _, runner = make(tmp_path)
    assert service.govern_sync(good(approval=None)).outcome == "denied_human_review"
    assert service.govern_sync(good()).outcome == "executed"
    assert len(runner.executed) == 1


def test_an_invariant_violation_enters_the_terminal_state_even_with_an_approval(tmp_path):
    service, rec, runner = make(tmp_path)
    result = service.govern_sync(good(action="drop_all_tables"))
    assert result.outcome == "terminal_suspended" and not result.executed
    assert runner.executed == []
    monitor, ver, terminal = list(rec.ledger.store.all_entries())
    assert (monitor.entry_type.value, ver.entry_type.value, terminal.entry_type.value) == ("MONITOR", "VERIFICATION", "TERMINAL")
    assert monitor.payload["drift_detected"] is False
    assert ver.payload["result"] == "INSUFFICIENT" and ver.caused_by == monitor.entry_id
    assert terminal.caused_by == ver.entry_id
    assert rec.ledger.verify_integrity()  # the chain is intact, but the incident is open:
    report = rec.ledger.audit()
    assert set(report.checks_failed) == {"insufficient_verification_has_specification_update",
                                         "terminal_has_human_authorised_transition"}
    assert rec.is_suspended() == (True, terminal.entry_id)


def test_nothing_runs_while_suspended_and_a_callers_not_suspended_claim_is_ignored(tmp_path):
    service, rec, runner = make(tmp_path)
    service.govern_sync(good(action="drop_all_tables"))
    count = len(types(rec))
    result = service.govern_sync(good(suspended=False))
    assert result.outcome == "refused_suspended" and not result.executed
    assert runner.executed == []
    assert types(rec)[count:] == ["MONITOR"]  # recorded, but no second TERMINAL
    assert types(rec).count("TERMINAL") == 1


def test_suspension_survives_a_restart_because_it_is_rehydrated_from_the_ledger(tmp_path):
    service, _, _ = make(tmp_path)
    service.govern_sync(good(action="drop_all_tables"))
    service2, rec2, runner2 = make(tmp_path)  # a brand-new process view of the same file
    assert service2.govern_sync(good()).outcome == "refused_suspended"
    assert runner2.executed == []


def test_only_a_human_authorised_transition_clears_the_terminal_and_the_audit_then_passes(tmp_path):
    service, rec, runner = make(tmp_path)
    service.govern_sync(good(action="drop_all_tables"))
    terminal_id = rec.open_terminal_id()
    old_instance = rec.instance
    new_instance = rec.re_enable(terminal_entry_id=terminal_id, authority=AUTHORITY, authorised_by="carol",
                                 evidence=EVIDENCE)
    assert new_instance.instance_id != old_instance.instance_id and new_instance.generation == 1
    assert types(rec)[-3:] == ["SPECIFICATION_UPDATE", "HUMAN_AUTHORISED_TRANSITION", "RE_ENABLEMENT"]
    assert rec.ledger.verify_integrity() and rec.ledger.audit().all_passed
    assert not rec.is_suspended()[0]
    assert service.govern_sync(good()).outcome == "executed" and len(runner.executed) == 1
    with pytest.raises(GovernanceLedgerError):
        rec.re_enable(terminal_entry_id=terminal_id, authority=AUTHORITY, authorised_by="carol", evidence=EVIDENCE)


def test_reenablement_without_a_named_human_or_evidence_writes_nothing(tmp_path):
    from vsl_core.identity import Evidence
    service, rec, _ = make(tmp_path)
    service.govern_sync(good(action="drop_all_tables"))
    before = types(rec)
    with pytest.raises(ValueError):
        rec.re_enable(terminal_entry_id=rec.open_terminal_id(), authority=AUTHORITY, authorised_by="  ", evidence=EVIDENCE)
    with pytest.raises(ValueError):
        Evidence(root_cause_analysis="", specification_update_proposal="x")
    with pytest.raises(GovernanceLedgerError):
        rec.re_enable(terminal_entry_id="no-such-entry", authority=AUTHORITY, authorised_by="carol", evidence=EVIDENCE)
    assert types(rec) == before and rec.is_suspended()[0]


def test_each_gate_has_its_own_denial_key_and_only_the_failing_one_is_set(tmp_path):
    service, _, _ = make(tmp_path)
    state = asyncio.run(service.graph.ainvoke({"decision_id": "d", "proposal": good(approval=None), "entry_ids": []}))
    assert set(DENIAL_KEYS) == {"vsl_denial_suspended", "vsl_denial_allowlist", "vsl_denial_prenode"}
    assert isinstance(state["vsl_denial_prenode"], AutomationDeniedException)
    assert state.get("vsl_denial_allowlist") is None and state.get("vsl_denial_suspended") is None


# ------------------------------------------------------------------------------------------ fail closed
class FailingStore:
    """Wraps a real store; reads work, appends fail after `allow` successful ones."""

    def __init__(self, inner, allow=0):
        self.inner, self.allow = inner, allow

    def append(self, entry):
        if self.allow <= 0:
            raise OSError("disk full")
        self.allow -= 1
        return self.inner.append(entry)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_a_ledger_write_failure_blocks_the_action_before_the_side_effect(tmp_path):
    service, rec, runner = make(tmp_path)
    rec.ledger.store = FailingStore(rec.ledger.store, allow=0)
    result = service.govern_sync(good())
    assert result.outcome == "governance_error" and not result.executed
    assert runner.executed == [] and result.reasons == ("GovernanceLedgerError",)


def test_a_failed_post_execution_record_is_reported_as_an_error_not_a_success(tmp_path):
    service, rec, runner = make(tmp_path)
    rec.ledger.store = FailingStore(rec.ledger.store, allow=1)  # authorisation is written, the executed record fails
    result = service.govern_sync(good())
    assert result.outcome == "governance_error"
    assert result.executed is True and len(runner.executed) == 1  # honest: the stub did run


def test_a_tampered_ledger_blocks_everything(tmp_path):
    service, rec, runner = make(tmp_path)
    service.govern_sync(good())
    path = rec.ledger.store.path
    path.write_text(path.read_text().replace("alice", "mallory"), encoding="utf-8")
    result = service.govern_sync(good())
    assert result.outcome == "governance_error" and len(runner.executed) == 1  # only the earlier, legitimate run


def test_an_unreadable_ledger_blocks_everything(tmp_path):
    service, rec, runner = make(tmp_path)
    rec.ledger.store.path.write_text("{not json\n", encoding="utf-8")
    assert service.govern_sync(good()).outcome == "governance_error" and runner.executed == []


def test_a_gate_that_errors_is_not_approval(tmp_path):
    policy = build_policy()

    async def broken(_candidate):
        raise Boom("monitor crashed")

    bad_policy = replace(policy, pre_node=replace(policy.pre_node, monitor=broken))
    service, _, runner = make(tmp_path, policy=bad_policy)
    result = service.govern_sync(good())
    assert result.outcome == "governance_error" and not result.executed and runner.executed == []
    assert result.reasons == ("gate_or_graph_failure:Boom",)


def test_an_unexpected_escaped_denial_is_a_block_not_a_crash(tmp_path):
    service, _, runner = make(tmp_path)

    class Escaping:
        async def ainvoke(self, *_a, **_k):
            raise AutomationDeniedException(reason="escaped", identity_key="x")

    service.graph = Escaping()
    assert service.govern_sync(good()).outcome == "blocked" and runner.executed == []


def test_suspension_holds_in_process_even_if_the_terminal_write_fails(tmp_path):
    service, rec, runner = make(tmp_path)
    rec.ledger.store = FailingStore(rec.ledger.store, allow=2)  # MONITOR and VERIFICATION written, TERMINAL fails
    assert service.govern_sync(good(action="drop_all_tables")).outcome == "governance_error"
    rec.ledger.store = rec.ledger.store.inner  # the disk recovers
    assert service.govern_sync(good()).outcome == "refused_suspended" and runner.executed == []


def test_the_stub_refuses_to_run_without_an_authorisation_record():
    with pytest.raises(UnauthorisedExecution):
        NoOpProtectedAction().execute(ACTION, "")


def test_invalid_configuration_means_no_service(tmp_path):
    policy = replace(build_policy(), allowed_actions=frozenset())
    with pytest.raises(GovernanceConfigError):
        GovernedActionService(GovernanceRecorder(open_ledger(tmp_path / "l.jsonl", fsync=False)), policy=policy)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(GovernanceConfigError):
        open_ledger(blocker / "sub" / "l.jsonl")


def test_investigation_still_works_while_automation_is_suspended(tmp_path):
    from app.agent.models import InvestigationReport  # noqa: F401
    from tests.agent_support import FINISH, FakeToolbox, ScriptedModel, run_graph
    service, rec, _ = make(tmp_path)
    service.govern_sync(good(action="drop_all_tables"))
    assert rec.is_suspended()[0]
    report = run_graph(ScriptedModel([FINISH]), FakeToolbox({}, names=["get_pipeline_status"]))
    assert report.status == "inconclusive"  # read-only investigation is unaffected by suspension


def test_a_report_becomes_gate_input_using_only_status_and_evidence_ids():
    from app.governance import proposal_from_report
    from tests.agent_support import FINISH, FakeToolbox, ScriptedModel, run_graph
    report = run_graph(ScriptedModel([FINISH]), FakeToolbox({}, names=["get_pipeline_status"]))
    candidate = proposal_from_report(report, ACTION)
    assert candidate.report_status == "inconclusive" and candidate.approval is None
    assert candidate.cited_evidence_ids == ()
