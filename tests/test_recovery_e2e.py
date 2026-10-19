"""Phase 7 end-to-end scenarios. Real offline investigation -> real MCP tools -> real VSL gates -> real
SQLite approval store -> real hash-chained ledger; only the recovery EFFECT is the deterministic in-memory
simulation (a counting executor over the in-memory repository). Nothing here touches SQL Server."""

import pytest

from app.governance import GateInput, audit_cli
from app.recovery import proposal_from_report, suggest_recovery
from tests.recovery_support import TOKENS, approve, investigate, kinds, make_env, prop, run, start


def propose_from_investigation(tmp_path, scenario):
    sim, report = investigate(tmp_path, scenario)
    assert report.status == "issue_identified", report.status
    suggestion = suggest_recovery(report)
    assert suggestion is not None
    return sim, proposal_from_report(report, *suggestion)


def assert_clean_ledger(env):
    assert env.recorder.ledger.verify_integrity()
    assert env.recorder.ledger.audit().all_passed
    assert audit_cli.run_audit(env.ledger_path, None)[0] == 0


# 1 -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("scenario", ["duplicate-records", "stale-data", "data-quality"])
def test_e2e_1_successful_controlled_recovery(tmp_path, scenario):
    sim, proposal = propose_from_investigation(tmp_path, scenario)
    env = make_env(tmp_path, sim=sim)
    assert proposal.action_type == "remove_injected_condition"
    assert sim.active_scenario_ids()  # the incident is live
    active_before = set(sim.active_scenario_ids())

    out = run(env.workflow.start(proposal))
    assert out.awaiting_approval and env.executor.count == 0 and set(sim.active_scenario_ids()) == active_before

    approve(env, out.request_id)
    done = run(env.workflow.resume(out.request_id))
    assert done.status == "executed" and done.execution_status == "executed"
    assert env.executor.count == 1
    assert set(sim.active_scenario_ids()) < active_before  # the injected condition is gone (postcondition verified)
    assert env.store.get(out.request_id).status == "consumed"
    assert kinds(env) == ["recovery_proposed", "approval_requested", "approval_granted", "authorised",
                          "recovery_attempt", "recovery_succeeded", "executed"]
    assert_clean_ledger(env)


def test_e2e_1b_pipeline_rerun_recovery_in_dependency_order(tmp_path):
    env = make_env(tmp_path)
    runs_before = len(env.sim.repo.list_runs(None, 1000))
    for pipeline in ("order_ingestion", "order_validation"):  # validation needs fresh ingested data to pass
        out = start(env, prop("rerun_pipeline", {"pipeline_id": pipeline}))
        approve(env, out.request_id)
        done = run(env.workflow.resume(out.request_id))
        assert done.status == "executed", (pipeline, done)
    assert env.executor.count == 2 and len(env.sim.repo.list_runs(None, 1000)) == runs_before + 2
    assert_clean_ledger(env)


def test_e2e_1c_a_rerun_that_does_not_succeed_is_not_reported_as_success(tmp_path):
    env = make_env(tmp_path)
    out = start(env, prop("rerun_pipeline", {"pipeline_id": "order_validation"}))  # no fresh ingestion yet -> fails
    approve(env, out.request_id)
    done = run(env.workflow.resume(out.request_id))
    assert done.status == "recovery_verification_failed" and not done.executed
    assert "executed" not in kinds(env) and env.recorder.ledger.verify_integrity()


# 2 -------------------------------------------------------------------------------------------------
def test_e2e_2_rejected_approval(tmp_path):
    sim, proposal = propose_from_investigation(tmp_path, "duplicate-records")
    env = make_env(tmp_path, sim=sim)
    out = run(env.workflow.start(proposal))
    active = set(sim.active_scenario_ids())
    env.approvals.reject(out.request_id, TOKENS["admin"], "not now")  # high-risk action: admin role
    done = run(env.workflow.resume(out.request_id))
    assert done.status == "rejected" and env.executor.count == 0 and set(sim.active_scenario_ids()) == active
    assert env.store.get(out.request_id).status == "rejected"
    assert "approval_rejected" in kinds(env) and "authorised" not in kinds(env)
    assert_clean_ledger(env)


# 3 -------------------------------------------------------------------------------------------------
def test_e2e_3_invalid_tampered_and_reused_approvals(tmp_path):
    from tests.recovery_support import sqlite_update
    sim, proposal = propose_from_investigation(tmp_path, "duplicate-records")
    env = make_env(tmp_path, sim=sim)
    active = set(sim.active_scenario_ids())

    # unauthenticated / unknown / unauthorised approvers cannot even produce an approval
    out = run(env.workflow.start(proposal))
    for credential in (None, "wrong-token-wrong-token-wrong", TOKENS["viewer"]):
        with pytest.raises(Exception):
            env.approvals.approve(out.request_id, credential)
    assert run(env.workflow.resume(out.request_id)).awaiting_approval and env.executor.count == 0

    # a tampered approved request is refused and the refusal is recorded
    approve(env, out.request_id)
    sqlite_update(env.db_path, "UPDATE approval_requests SET parameters_json=? WHERE request_id=?",
                  ('{"scenario_key":"stale-data"}', out.request_id))
    tampered = run(env.workflow.resume(out.request_id))
    assert tampered.status == "approval_invalid" and env.executor.count == 0 and set(sim.active_scenario_ids()) == active
    assert kinds(env)[-1] == "approval_not_usable"

    # a legitimately executed approval cannot be reused
    out2 = run(env.workflow.start(proposal))
    approve(env, out2.request_id)
    assert run(env.workflow.resume(out2.request_id)).status == "executed"
    for _ in range(2):
        assert run(env.workflow.resume(out2.request_id)).status == "approval_already_used"
    assert env.executor.count == 1
    assert_clean_ledger(env)


# 4 -------------------------------------------------------------------------------------------------
def test_e2e_4_governance_suspension_cannot_be_overridden_by_an_approval(tmp_path):
    sim, proposal = propose_from_investigation(tmp_path, "duplicate-records")
    env = make_env(tmp_path, sim=sim)
    out = run(env.workflow.start(proposal))
    approve(env, out.request_id)  # a perfectly valid approval exists...
    active = set(sim.active_scenario_ids())

    bad = run(env.governed.govern(GateInput(action="drop_everything", report_status="issue_identified",
                                            cited_evidence_ids=("E1",), known_evidence_ids=("E1",))))
    assert bad.outcome == "terminal_suspended" and env.recorder.is_suspended()[0]

    # ...but the suspension (and the terminal-state invalidation) beat it
    assert run(env.workflow.resume(out.request_id)).status in ("approval_invalid", "refused_suspended")
    assert run(env.workflow.start(proposal)).status == "refused_suspended"
    assert env.executor.count == 0 and set(sim.active_scenario_ids()) == active
    assert env.recorder.is_suspended()[0]  # nothing cleared it automatically
    # the five-check audit correctly flags the missing human re-enablement
    assert "terminal_has_human_authorised_transition" in env.recorder.ledger.audit().checks_failed
    # read-only investigation is unaffected
    (tmp_path / "again").mkdir()
    _, report = investigate(tmp_path / "again", "stale-data")
    assert report.status == "issue_identified"


# 5 -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fault,status,exec_status", [
    ("fail_with", "recovery_uncertain", "uncertain"),
    ("skip_effect", "recovery_verification_failed", "verification_failed"),
])
def test_e2e_5_failed_or_uncertain_recovery_is_never_reported_as_success(tmp_path, fault, status, exec_status):
    sim, proposal = propose_from_investigation(tmp_path, "duplicate-records")
    env = make_env(tmp_path, sim=sim)
    out = run(env.workflow.start(proposal))
    approve(env, out.request_id)
    setattr(env.executor, fault, RuntimeError("simulated fault") if fault == "fail_with" else True)
    done = run(env.workflow.resume(out.request_id))
    assert done.status == status and done.execution_status == exec_status and not done.executed
    assert "executed" not in kinds(env) and "recovery_succeeded" not in kinds(env)
    assert run(env.workflow.resume(out.request_id)).status == "approval_already_used"  # no automatic retry
    assert env.executor.count == 1
    assert audit_cli.recovery_attempts_without_result(env.recorder.ledger.store.all_entries()) == []
    assert env.recorder.ledger.verify_integrity()


# 6 -------------------------------------------------------------------------------------------------
def test_e2e_6_fail_closed_when_the_ledger_or_a_vsl_gate_fails_just_before_execution(tmp_path):
    from dataclasses import replace
    from app.governance import GovernedActionService
    from app.governance.policy import build_policy
    from app.recovery import RECOVERY_ACTIONS, RecoveryRunner, RecoveryWorkflow
    from tests.recovery_support import DroppingStore, FailingStore

    sim, proposal = propose_from_investigation(tmp_path, "duplicate-records")
    env = make_env(tmp_path, sim=sim)
    out = run(env.workflow.start(proposal))
    approve(env, out.request_id)
    active = set(sim.active_scenario_ids())

    # (a) a ledger write fails right before the side effect
    original = env.recorder.ledger.store
    env.recorder.ledger.store = FailingStore(original, allow=1)
    assert run(env.workflow.resume(out.request_id)).status in ("governance_error", "recovery_blocked")
    # (b) the ledger silently drops writes
    env.recorder.ledger.store = DroppingStore(original)
    assert run(env.workflow.resume(out.request_id)).status in ("governance_error", "recovery_blocked")
    env.recorder.ledger.store = original
    # (c) a VSL gate raises
    async def broken(_c):
        raise RuntimeError("monitor crashed")
    policy = build_policy(RECOVERY_ACTIONS)
    policy = replace(policy, pre_node=replace(policy.pre_node, monitor=broken))
    governed = GovernedActionService(env.recorder, RecoveryRunner(env.executor, env.approvals, env.recorder), policy=policy)
    assert run(RecoveryWorkflow(env.approvals, governed, env.recorder).resume(out.request_id)).status == "governance_error"

    assert env.executor.count == 0 and set(sim.active_scenario_ids()) == active
    assert env.store.get(out.request_id).status == "approved"  # never consumed: nothing got near the executor
    # once the ledger is healthy again the SAME approval still works, exactly once
    assert run(env.workflow.resume(out.request_id)).status == "executed" and env.executor.count == 1


# 7 -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("scenario", ["baseline", "pipeline-failure", "stale-data", "duplicate-records",
                                      "incomplete-processing", "schema-mismatch", "data-quality"])
def test_e2e_7_the_original_investigation_workflow_is_unchanged(tmp_path, scenario):
    sim, report = investigate(tmp_path, scenario)
    expected = "warnings_only" if scenario == "baseline" else "issue_identified"  # statuses as the original workflow reports them
    assert report.status == expected
    # investigation is read-only: it neither needs the governance stack nor changes the simulated state
    before = sorted(sim.active_scenario_ids())
    assert sorted(sim.active_scenario_ids()) == before  # reading the report changed nothing
