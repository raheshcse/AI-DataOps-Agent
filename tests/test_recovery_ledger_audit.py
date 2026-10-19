"""Phase 7 ledger and audit tests: causal chain, integrity, tamper, Phase 6 audit preserved, Phase 7 extension, CLI."""

import json

from vsl_core.ledger import LedgerEntryType

from app.governance import audit_cli
from tests.recovery_support import TOKENS, approve, kinds, make_env, prop, run, start


def finished(tmp_path, **kw):
    env = make_env(tmp_path)
    out = start(env)
    approve(env, out.request_id)
    return env, out, run(env.workflow.resume(out.request_id))


def test_a_successful_recovery_passes_integrity_and_the_five_semantic_checks(tmp_path):
    env, _, done = finished(tmp_path)
    assert done.status == "executed"
    report = env.recorder.ledger.audit(max_monitor_gap_seconds=3600)
    assert env.recorder.ledger.verify_integrity() and report.all_passed
    assert len(report.checks_passed) == 5 and report.checks_failed == ()


def test_the_cli_reports_a_clean_ledger_and_includes_the_phase_7_check(tmp_path):
    env, *_ = finished(tmp_path)
    code, report = audit_cli.run_audit(env.ledger_path, None)
    assert code == 0 and report["phase7"]["passed"] and report["phase7"]["violations"] == []
    assert audit_cli.main(["--ledger", str(env.ledger_path)]) == 0


def test_an_attempt_without_a_result_fails_the_phase_7_check_and_the_cli(tmp_path, capsys):
    env, *_ = finished(tmp_path)
    entries = env.recorder.ledger.store.all_entries()
    assert audit_cli.recovery_attempts_without_result(entries) == []
    lines = env.ledger_path.read_text().splitlines()
    kept = [ln for ln in lines if '"recovery_succeeded"' not in ln and '"executed"' not in ln]
    # an attempt whose result never landed: build a fresh ledger that stops right after the attempt
    env2 = make_env(tmp_path, name="b")
    out = start(env2)
    approve(env2, out.request_id)
    env2.executor.fail_with = None
    from tests.recovery_support import FailingStore
    env2.recorder.ledger.store = FailingStore(env2.recorder.ledger.store, allow=2)  # authorised + attempt, then disk full
    run(env2.workflow.resume(out.request_id))
    assert audit_cli.recovery_attempts_without_result(env2.recorder.ledger.store.all_entries())
    code, report = audit_cli.run_audit(env2.ledger_path, None)
    assert code == 1 and not report["phase7"]["passed"]
    assert audit_cli.main(["--ledger", str(env2.ledger_path)]) == 1
    assert "Phase 7" in capsys.readouterr().out
    assert kept  # (sanity: the first ledger was untouched by the second scenario)


def test_uncertain_and_verification_failed_outcomes_are_recorded_as_results(tmp_path):
    for fault, expect in (("fail_with", "recovery_uncertain"), ("skip_effect", "recovery_verification_failed")):
        env = make_env(tmp_path, name=fault)
        out = start(env)
        approve(env, out.request_id)
        setattr(env.executor, fault, RuntimeError("x") if fault == "fail_with" else True)
        run(env.workflow.resume(out.request_id))
        assert expect in kinds(env) and "executed" not in kinds(env)
        assert audit_cli.recovery_attempts_without_result(env.recorder.ledger.store.all_entries()) == []
        assert env.recorder.ledger.verify_integrity()


def test_denied_attempts_are_recorded_with_their_reason_and_leave_the_audit_clean(tmp_path):
    env = make_env(tmp_path)
    start(env, prop("rm_rf", {}))
    start(env, prop(status="inconclusive"))
    out = start(env)
    env.approvals.reject(out.request_id, TOKENS["admin"], "no")
    run(env.workflow.resume(out.request_id))
    assert {"recovery_action_rejected", "approval_rejected", "approval_not_usable"} <= set(kinds(env))
    assert env.recorder.ledger.verify_integrity() and env.recorder.ledger.audit().all_passed
    assert audit_cli.run_audit(env.ledger_path, None)[0] == 0


def test_editing_any_entry_breaks_integrity_and_the_cli_exit_code(tmp_path):
    env, *_ = finished(tmp_path)
    text = env.ledger_path.read_text().replace("recovery_attempt", "recovery_attemps", 1)
    env.ledger_path.write_text(text, encoding="utf-8")
    code, _ = audit_cli.run_audit(env.ledger_path, None)
    assert code == 1


def test_deleting_an_entry_is_detected(tmp_path):
    env, *_ = finished(tmp_path)
    lines = env.ledger_path.read_text().splitlines()
    del lines[3]
    env.ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert audit_cli.run_audit(env.ledger_path, None)[0] == 1


def test_a_missing_ledger_is_not_audited_and_exits_2(tmp_path):
    assert audit_cli.run_audit(tmp_path / "nope.jsonl", None)[0] == 2
    assert audit_cli.main(["--ledger", str(tmp_path / "nope.jsonl")]) == 2


def test_a_suspension_still_requires_the_human_authorised_transition(tmp_path):
    from app.governance import GateInput
    env = make_env(tmp_path)
    run(env.governed.govern(GateInput(action="drop_everything", report_status="issue_identified",
                                      cited_evidence_ids=("E1",), known_evidence_ids=("E1",))))
    assert "terminal_has_human_authorised_transition" in env.recorder.ledger.audit().checks_failed
    assert audit_cli.run_audit(env.ledger_path, None)[0] == 1


def test_a_ledger_failure_prevents_any_new_protected_action(tmp_path):
    from tests.recovery_support import FailingStore
    env = make_env(tmp_path)
    env.recorder.ledger.store = FailingStore(env.recorder.ledger.store, allow=0)
    out = start(env)
    assert out.status == "governance_error" and out.request_id is None
    assert env.store.list() == [] and env.executor.count == 0  # no request can even be opened without a working ledger


def test_every_ledger_entry_is_json_with_no_credentials(tmp_path):
    env, *_ = finished(tmp_path)
    blob = env.ledger_path.read_text()
    for line in blob.splitlines():
        json.loads(line)
    for secret in list(TOKENS.values()) + ["test-signing-key"]:
        assert secret not in blob
    assert env.recorder.ledger.store.all_entries().__next__().entry_type in (LedgerEntryType.MONITOR,)
