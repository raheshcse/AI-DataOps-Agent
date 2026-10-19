"""Phase 7 approval-store tests: lifecycle, exact binding, sealing, expiry, replay, concurrency, persistence."""

import threading
from datetime import timedelta

import pytest

from app.governance.ledger import GovernanceLedgerError
from app.recovery import ApprovalConfigError, ApprovalDenied, ApprovalService, ApprovalSigner, SqliteApprovalStore, validate_action
from app.recovery.approvals import binding_digest
from tests.recovery_support import (
    SIGNING_KEY,
    TOKENS,
    DroppingStore,
    FailingStore,
    kinds,
    make_auth,
    make_env,
    sqlite_update,
)

ACT = validate_action("rerun_pipeline", {"pipeline_id": "order_ingestion"})
REMOVE = validate_action("remove_injected_condition", {"scenario_key": "duplicate-records"})


def new_request(env, action=ACT, **kw):
    args = dict(incident_id="INV-1", action=action, evidence_refs=("E1",), reason="why", governance_state="active",
                report_status="issue_identified")
    args.update(kw)
    return env.approvals.create_request(**args)


# ------------------------------------------------------------------------------------- creation
def test_a_request_has_a_unique_id_a_binding_an_expiry_and_a_ledger_record(tmp_path):
    env = make_env(tmp_path)
    a, b = new_request(env), new_request(env)
    assert a.request_id != b.request_id and a.request_id.startswith("apr-") and len(a.request_id) == 36
    assert a.status == "pending" and a.risk == "medium" and a.target == "pipeline:order_ingestion"
    assert a.binding_digest == binding_digest("INV-1", "rerun_pipeline", {"pipeline_id": "order_ingestion"},
                                              "pipeline:order_ingestion", ("E1",), "issue_identified")
    assert a.expires_at > a.created_at
    first = env.recorder.get_entry(a.ledger_request_entry)
    assert first.payload["outcome"] == "approval_requested" and first.payload["binding_digest"] == a.binding_digest
    assert "seal" not in a.summary() and a.summary()["status"] == "pending"


@pytest.mark.parametrize("kw", [{"incident_id": "bad id!"}, {"incident_id": ""}, {"incident_id": "x" * 65},
                                {"evidence_refs": ("E1; DROP",)}, {"evidence_refs": tuple(f"E{i}" for i in range(25))},
                                {"report_status": "bad status"}])
def test_invalid_incident_evidence_or_status_is_refused_and_nothing_is_stored(tmp_path, kw):
    env = make_env(tmp_path)
    with pytest.raises(ApprovalDenied):
        new_request(env, **kw)
    assert env.store.list() == [] and kinds(env) == []


def test_free_text_is_bounded_and_stripped_of_control_characters(tmp_path):
    env = make_env(tmp_path)
    rec = new_request(env, reason="line1\nline2\x00\x1b[31m" + "z" * 900)
    assert "\n" not in rec.reason and "\x00" not in rec.reason and len(rec.reason) <= 500


def test_a_request_whose_action_object_was_altered_is_refused(tmp_path):
    env = make_env(tmp_path)
    from dataclasses import replace
    with pytest.raises(ApprovalDenied):
        new_request(env, action=replace(ACT, parameters={"pipeline_id": "reporting_prep"}))  # target no longer matches
    with pytest.raises(ApprovalDenied):
        new_request(env, action=replace(ACT, action_type="drop_table"))


# ----------------------------------------------------------------------------------- decisions
def test_an_authorised_human_can_approve_and_the_approval_is_sealed_and_recorded(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    done = env.approvals.approve(req.request_id, TOKENS["admin"], "ok")
    assert done.status == "approved" and done.decided_by == "admin.user" and done.seal and done.ledger_decision_entry
    granted = env.recorder.get_entry(done.ledger_decision_entry)
    assert granted.payload["outcome"] == "approval_granted" and granted.caused_by == req.ledger_request_entry
    assert env.approvals.check(req.request_id).state == "approved_valid"


@pytest.mark.parametrize("credential", [None, "", "nope", "test-admin-token-WRONGWRONGWRONG", 42])
def test_unauthenticated_attempts_never_change_the_request_and_are_recorded(tmp_path, credential):
    env = make_env(tmp_path)
    req = new_request(env)
    with pytest.raises(ApprovalDenied) as info:
        env.approvals.approve(req.request_id, credential)
    assert info.value.code == "unauthenticated"
    assert env.store.get(req.request_id).status == "pending"
    assert kinds(env)[-1] == "approval_attempt_refused"


def test_an_approver_without_the_actions_permission_is_refused(tmp_path):
    env = make_env(tmp_path)
    risky = new_request(env, action=REMOVE)
    for who in ("approver", "viewer", "authority"):
        with pytest.raises(ApprovalDenied) as info:
            env.approvals.approve(risky.request_id, TOKENS[who])
        assert info.value.code == "not_permitted"
    assert env.store.get(risky.request_id).status == "pending"
    assert env.approvals.approve(risky.request_id, TOKENS["admin"]).status == "approved"
    # a plain approver may approve the lower-risk action
    low = new_request(env)
    assert env.approvals.approve(low.request_id, TOKENS["approver"]).status == "approved"


def test_an_explicit_rejection_is_final_and_has_no_seal(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    done = env.approvals.reject(req.request_id, TOKENS["admin"], "not now")
    assert done.status == "rejected" and done.seal is None and done.decision_reason == "not now"
    assert env.recorder.get_entry(done.ledger_decision_entry).payload["outcome"] == "approval_rejected"
    with pytest.raises(ApprovalDenied) as info:
        env.approvals.approve(req.request_id, TOKENS["admin"])
    assert info.value.code == "not_pending"
    assert env.approvals.check(req.request_id).state == "rejected"


def test_unknown_or_malformed_request_ids_are_not_found(tmp_path):
    env = make_env(tmp_path)
    for rid in ("apr-" + "0" * 32, "nope", "", None, "apr-1; DROP TABLE approval_requests"):
        with pytest.raises(ApprovalDenied) as info:
            env.approvals.approve(rid, TOKENS["admin"])
        assert info.value.code == "not_found"
    assert env.approvals.check("apr-" + "0" * 32).state == "missing"


def test_cancel_works_for_pending_and_blocks_later_use(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    assert env.approvals.cancel(req.request_id, TOKENS["admin"], "changed mind").status == "cancelled"
    assert env.approvals.check(req.request_id).state == "cancelled"
    with pytest.raises(ApprovalDenied):
        env.approvals.approve(req.request_id, TOKENS["admin"])


# ------------------------------------------------------------------------------------------ expiry
def test_a_request_cannot_be_approved_after_it_expires(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    env.clock.advance(minutes=31)
    with pytest.raises(ApprovalDenied) as info:
        env.approvals.approve(req.request_id, TOKENS["admin"])
    assert info.value.code == "expired" and env.store.get(req.request_id).status == "expired"
    assert env.approvals.check(req.request_id).state == "expired"


def test_an_approval_expires_before_it_is_consumed(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    env.approvals.approve(req.request_id, TOKENS["admin"])
    env.clock.advance(minutes=31)
    assert env.approvals.check(req.request_id).state == "expired"
    with pytest.raises(ApprovalDenied):
        env.approvals.consume(req.request_id, "dec-x", req.binding_digest)
    assert env.store.get(req.request_id).status == "approved"  # never consumed


def test_the_ttl_must_be_positive_and_bounded(tmp_path):
    env = make_env(tmp_path)
    for ttl in (timedelta(0), timedelta(seconds=-5), timedelta(days=2)):
        with pytest.raises(ApprovalConfigError):
            ApprovalService(env.store, ApprovalSigner(SIGNING_KEY), make_auth(), env.recorder, ttl=ttl)


# --------------------------------------------------------------------------------- exact binding
def test_consuming_with_a_digest_for_a_different_action_incident_or_parameters_is_refused(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    env.approvals.approve(req.request_id, TOKENS["admin"])
    wrong = {
        "action": binding_digest("INV-1", "remove_injected_condition", {"scenario_key": "data-quality"},
                                 "scenario:data-quality", ("E1",), "issue_identified"),
        "incident": binding_digest("INV-OTHER", "rerun_pipeline", {"pipeline_id": "order_ingestion"},
                                   "pipeline:order_ingestion", ("E1",), "issue_identified"),
        "params": binding_digest("INV-1", "rerun_pipeline", {"pipeline_id": "reporting_prep"},
                                 "pipeline:reporting_prep", ("E1",), "issue_identified"),
        "evidence": binding_digest("INV-1", "rerun_pipeline", {"pipeline_id": "order_ingestion"},
                                   "pipeline:order_ingestion", ("E9",), "issue_identified"),
        "target": binding_digest("INV-1", "rerun_pipeline", {"pipeline_id": "order_ingestion"},
                                 "pipeline:other", ("E1",), "issue_identified"),
    }
    for name, digest in wrong.items():
        with pytest.raises(ApprovalDenied) as info:
            env.approvals.consume(req.request_id, "dec", digest)
        assert info.value.code == "binding_mismatch", name
    assert env.store.get(req.request_id).status == "approved"


@pytest.mark.parametrize("column,value", [
    ("parameters_json", '{"pipeline_id":"reporting_prep"}'), ("target", "pipeline:reporting_prep"),
    ("incident_id", "INV-OTHER"), ("action_type", "remove_injected_condition"),
    ("evidence_json", '["E9"]'), ("report_status", "inconclusive")])
def test_editing_the_stored_request_after_approval_invalidates_it(tmp_path, column, value):
    env = make_env(tmp_path)
    req = new_request(env)
    env.approvals.approve(req.request_id, TOKENS["admin"])
    sqlite_update(env.db_path, f"UPDATE approval_requests SET {column} = ? WHERE request_id = ?", (value, req.request_id))
    verdict = env.approvals.check(req.request_id)
    assert verdict.state == "invalid" and verdict.reason in ("binding_mismatch", "action_no_longer_valid")
    with pytest.raises(ApprovalDenied):
        env.approvals.consume(req.request_id, "dec", req.binding_digest)


def test_a_forged_approval_row_without_a_valid_seal_is_unusable(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    sqlite_update(env.db_path, "UPDATE approval_requests SET status='approved', decided_by='admin.user', "
                  "decided_at=?, ledger_decision_entry='x' WHERE request_id=?", (req.created_at, req.request_id))
    assert env.approvals.check(req.request_id).reason == "seal_invalid"
    forged = ApprovalSigner("some-other-key-" + "y" * 40).seal(
        request_id=req.request_id, digest=req.binding_digest, approver="admin.user", decided_at=req.created_at,
        expires_at=req.expires_at, decision="approved")
    sqlite_update(env.db_path, "UPDATE approval_requests SET seal=? WHERE request_id=?", (forged, req.request_id))
    assert env.approvals.check(req.request_id).reason == "seal_invalid"
    with pytest.raises(ApprovalDenied):
        env.approvals.consume(req.request_id, "dec", req.binding_digest)


def test_changing_who_approved_breaks_the_seal(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    env.approvals.approve(req.request_id, TOKENS["approver"])
    sqlite_update(env.db_path, "UPDATE approval_requests SET decided_by='admin.user' WHERE request_id=?", (req.request_id,))
    assert env.approvals.check(req.request_id).reason == "seal_invalid"


def test_an_approval_with_no_ledger_record_of_the_decision_is_unusable(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    env.approvals.approve(req.request_id, TOKENS["admin"])
    sqlite_update(env.db_path, "UPDATE approval_requests SET ledger_decision_entry=NULL WHERE request_id=?", (req.request_id,))
    assert env.approvals.check(req.request_id).reason == "decision_not_recorded"


def test_an_approver_who_loses_their_role_invalidates_the_approval(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    env.approvals.approve(req.request_id, TOKENS["admin"])
    later = ApprovalService(env.store, ApprovalSigner(SIGNING_KEY), make_auth(revoked={"admin"}), env.recorder,
                            clock=env.clock)
    assert later.check(req.request_id).reason == "approver_no_longer_permitted"


def test_a_terminal_state_after_the_request_invalidates_earlier_approvals(tmp_path):
    from vsl_core.ledger import LedgerEntryType
    env = make_env(tmp_path)
    req = new_request(env)
    env.approvals.approve(req.request_id, TOKENS["admin"])
    env.recorder.entry(LedgerEntryType.TERMINAL, decision_id="d", caused_by=None)
    assert env.approvals.check(req.request_id).reason == "governance_state_changed_since_request"


# ----------------------------------------------------------------------- replay and concurrency
def test_an_approval_can_be_consumed_exactly_once(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    env.approvals.approve(req.request_id, TOKENS["admin"])
    spent = env.approvals.consume(req.request_id, "dec-1", req.binding_digest)
    assert spent.status == "consumed" and spent.consumed_by_decision == "dec-1"
    for _ in range(3):
        with pytest.raises(ApprovalDenied) as info:
            env.approvals.consume(req.request_id, "dec-2", req.binding_digest)
        assert info.value.code == "consumed"
    assert env.store.get(req.request_id).consumed_by_decision == "dec-1"
    with pytest.raises(ApprovalDenied):
        env.approvals.approve(req.request_id, TOKENS["admin"])  # a consumed approval cannot be re-approved


def test_a_pending_request_cannot_be_consumed(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    with pytest.raises(ApprovalDenied) as info:
        env.approvals.consume(req.request_id, "dec", req.binding_digest)
    assert info.value.code == "pending"


def test_concurrent_consumers_race_and_exactly_one_wins(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    env.approvals.approve(req.request_id, TOKENS["admin"])
    barrier, results = threading.Barrier(8), []

    def attempt(i):
        # each thread uses its own service + store objects (like separate workers sharing one database file)
        svc = ApprovalService(SqliteApprovalStore(env.db_path), ApprovalSigner(SIGNING_KEY), make_auth(),
                              env.recorder, clock=env.clock)
        barrier.wait()
        try:
            svc.consume(req.request_id, f"dec-{i}", req.binding_digest)
            results.append("won")
        except ApprovalDenied as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("won") == 1 and len(results) == 8
    assert env.store.get(req.request_id).status == "consumed"


def test_concurrent_approve_and_reject_cannot_both_succeed(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    barrier, results = threading.Barrier(2), []

    def decide(fn):
        barrier.wait()
        try:
            fn(req.request_id, TOKENS["admin"])
            results.append("ok")
        except ApprovalDenied as exc:
            results.append(exc.code)

    ts = [threading.Thread(target=decide, args=(f,)) for f in (env.approvals.approve, env.approvals.reject)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 1 and env.store.get(req.request_id).status in ("approved", "rejected")


# ------------------------------------------------------------------------------------ persistence
def test_requests_and_decisions_survive_a_restart_and_pending_never_becomes_approved(tmp_path):
    env = make_env(tmp_path)
    pending, decided = new_request(env), new_request(env)
    env.approvals.approve(decided.request_id, TOKENS["admin"])
    again = make_env(tmp_path)  # a fresh object graph over the same files = a restart
    assert again.store.get(pending.request_id).status == "pending"
    assert again.approvals.check(pending.request_id).state == "pending"
    assert again.approvals.check(decided.request_id).state == "approved_valid"
    again.approvals.consume(decided.request_id, "dec", decided.binding_digest)
    third = make_env(tmp_path)
    assert third.approvals.check(decided.request_id).state == "consumed"
    assert [r.status for r in third.store.list()] == ["pending", "consumed"]


# ------------------------------------------------------------------------------- ledger failures
def test_a_ledger_failure_creating_a_request_stores_nothing(tmp_path):
    env = make_env(tmp_path)
    env.recorder.ledger.store = FailingStore(env.recorder.ledger.store, allow=0)
    with pytest.raises(GovernanceLedgerError):
        new_request(env)
    assert env.store.list() == []


def test_a_ledger_failure_while_approving_leaves_the_request_unusable(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    env.recorder.ledger.store = FailingStore(env.recorder.ledger.store, allow=0)
    with pytest.raises(ApprovalDenied) as info:
        env.approvals.approve(req.request_id, TOKENS["admin"])
    assert info.value.code == "ledger_error"
    assert env.store.get(req.request_id).status == "pending"  # reverted; and never usable without a ledger record
    assert env.approvals.check(req.request_id).state == "pending"


def test_a_silently_dropped_ledger_write_is_noticed_by_a_read_back(tmp_path):
    env = make_env(tmp_path)
    env.recorder.ledger.store = DroppingStore(env.recorder.ledger.store)
    entry = env.recorder.monitor(decision_id="d", drift=False, outcome="x")
    assert env.recorder.get_entry(entry.entry_id) is None


# ------------------------------------------------------------------------------------- secrets
def test_no_token_digest_or_signing_key_appears_in_the_database_or_ledger(tmp_path):
    env = make_env(tmp_path)
    req = new_request(env)
    env.approvals.approve(req.request_id, TOKENS["admin"], "fine")
    with pytest.raises(ApprovalDenied):
        env.approvals.approve(req.request_id, "test-admin-token-WRONGWRONGWRONG")
    blob = env.db_path.read_bytes() + env.ledger_path.read_bytes() + repr(req.summary()).encode()
    for secret in [*TOKENS.values(), SIGNING_KEY, "WRONGWRONGWRONG"]:
        assert secret.encode() not in blob


def test_a_short_signing_key_is_refused():
    for key in ("", "short", b"x" * 31):
        with pytest.raises(ApprovalConfigError):
            ApprovalSigner(key)
    with pytest.raises(ApprovalConfigError):
        ApprovalSigner.from_environ({})
