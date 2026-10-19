"""SQL Server integration tests for Phase 7 controlled recovery.

Same safety model as the other integration tests: only the isolated ``*_test`` database is written (the ``svc``
fixture resets it per test and refuses anything else), never ``dataops_sim``. Skipped unless
RUN_SQLSERVER_TESTS=1; a skip is NOT a pass. The approval store and ledger live under ``tmp_path`` (SQLite
file and JSONL), never in SQL Server. These prove that the governed recovery works over real T-SQL.
"""

import pytest

from app.agent import OfflineHeuristicModel
from app.recovery import proposal_from_report, suggest_recovery
from tests.agent_support import apply_scenario, run_agent, server_for
from tests.recovery_support import approve, kinds, make_env, prop, run, start


def test_the_executor_is_bound_to_the_test_database(svc):
    name = svc.repo.engine.url.database
    assert name.lower().endswith("_test") and name.lower() != "dataops_sim"


def test_governed_recovery_removes_an_injected_condition_in_the_test_database(svc, tmp_path):
    apply_scenario(svc, "duplicate-records")
    report = run_agent(server_for(svc), "orders look wrong", OfflineHeuristicModel())
    assert report.status == "issue_identified"
    proposal = proposal_from_report(report, *suggest_recovery(report))
    env = make_env(tmp_path, sim=svc)
    active = set(svc.active_scenario_ids())

    out = run(env.workflow.start(proposal))
    assert out.awaiting_approval and env.executor.count == 0 and set(svc.active_scenario_ids()) == active
    approve(env, out.request_id)
    done = run(env.workflow.resume(out.request_id))
    assert done.status == "executed" and env.executor.count == 1
    assert set(svc.active_scenario_ids()) < active  # verified by an independent read of SQL Server
    assert run(env.workflow.resume(out.request_id)).status == "approval_already_used" and env.executor.count == 1
    assert env.recorder.ledger.verify_integrity() and env.recorder.ledger.audit().all_passed


def test_a_rejected_request_leaves_the_test_database_untouched(svc, tmp_path):
    apply_scenario(svc, "duplicate-records")
    env = make_env(tmp_path, sim=svc)
    before = (svc.repo.table_counts(), set(svc.active_scenario_ids()))
    out = start(env, prop("remove_injected_condition", {"scenario_key": "duplicate-records"}))
    from tests.recovery_support import TOKENS
    env.approvals.reject(out.request_id, TOKENS["admin"], "no")
    assert run(env.workflow.resume(out.request_id)).status == "rejected"
    assert env.executor.count == 0 and (svc.repo.table_counts(), set(svc.active_scenario_ids())) == before
    assert "approval_rejected" in kinds(env)


def test_pipeline_rerun_records_exactly_one_new_run(svc, tmp_path):
    env = make_env(tmp_path, sim=svc)
    before = len(svc.repo.list_runs(limit=1000))
    out = start(env, prop("rerun_pipeline", {"pipeline_id": "order_ingestion"}))
    approve(env, out.request_id)
    assert run(env.workflow.resume(out.request_id)).status == "executed"
    assert len(svc.repo.list_runs(limit=1000)) == before + 1 and env.executor.count == 1
