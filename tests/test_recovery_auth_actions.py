"""Phase 7 unit tests: authentication, authorisation, the action allowlist and the database-target guard."""

import json
from types import SimpleNamespace

import pytest

from app.recovery import (
    ACTIONS,
    ActionRejected,
    ApprovalConfigError,
    DenyAllAuthenticator,
    RecoveryExecutor,
    StaticTokenAuthenticator,
    build_authenticator,
    validate_action,
)
from app.recovery.actions import UnsafeRecoveryTarget, assert_recovery_target
from app.recovery.auth import approve_permission, is_permitted, token_digest
from tests.recovery_support import TOKENS, make_auth


# ---------------------------------------------------------------------------------- authentication
def test_a_valid_token_authenticates_to_its_subject_and_roles():
    ident = make_auth().authenticate(TOKENS["admin"])
    assert ident is not None and ident.subject == "admin.user" and ident.roles == {"recovery_admin"}


@pytest.mark.parametrize("credential", [None, "", "short", 12345, b"bytes", "x" * 600, "test-admin-token-WRONGWRONGWRONG",
                                         TOKENS["admin"] + " ", TOKENS["admin"].upper()])
def test_unknown_missing_or_malformed_credentials_do_not_authenticate(credential):
    assert make_auth().authenticate(credential) is None


def test_the_default_verifier_denies_everyone_even_a_correct_looking_token():
    auth = DenyAllAuthenticator()
    assert auth.authenticate(TOKENS["admin"]) is None and auth.current_roles("admin.user") == frozenset()
    assert isinstance(build_authenticator({}), DenyAllAuthenticator)
    assert isinstance(build_authenticator({"APPROVAL_AUTH_MODE": "disabled"}), DenyAllAuthenticator)


def test_the_dev_verifier_is_refused_in_production_and_when_misconfigured():
    creds = json.dumps([{"subject": "a", "token_sha256": token_digest("t" * 30), "roles": ["recovery_admin"]}])
    ok = build_authenticator({"APPROVAL_AUTH_MODE": "dev_static", "APPROVAL_DEV_CREDENTIALS": creds})
    assert isinstance(ok, StaticTokenAuthenticator) and ok.authenticate("t" * 30) is not None
    for env in ({"APPROVAL_AUTH_MODE": "dev_static", "APPROVAL_DEV_CREDENTIALS": creds, "APP_ENV": "production"},
                {"APPROVAL_AUTH_MODE": "dev_static"},
                {"APPROVAL_AUTH_MODE": "dev_static", "APPROVAL_DEV_CREDENTIALS": "not json"},
                {"APPROVAL_AUTH_MODE": "dev_static", "APPROVAL_DEV_CREDENTIALS": "[]"},
                {"APPROVAL_AUTH_MODE": "allow_all"}, {"APPROVAL_AUTH_MODE": "true"}):
        with pytest.raises(ApprovalConfigError):
            build_authenticator(env)


@pytest.mark.parametrize("entry", [
    {"subject": "bad subject!", "token_sha256": "a" * 64, "roles": ["viewer"]},
    {"subject": "ok", "token_sha256": "A" * 64, "roles": ["viewer"]},
    {"subject": "ok", "token_sha256": "a" * 63, "roles": ["viewer"]},
    {"subject": "ok", "token_sha256": "a" * 64, "roles": []},
    {"subject": "ok", "token_sha256": "a" * 64, "roles": ["superuser"]},
])
def test_invalid_credential_entries_are_rejected_without_echoing_them(entry):
    with pytest.raises(ApprovalConfigError) as info:
        StaticTokenAuthenticator([entry])
    assert "a" * 64 not in str(info.value)


def test_duplicate_subjects_or_tokens_are_rejected():
    e = {"subject": "x", "token_sha256": "a" * 64, "roles": ["viewer"]}
    with pytest.raises(ApprovalConfigError):
        StaticTokenAuthenticator([e, dict(e, subject="y")])
    with pytest.raises(ApprovalConfigError):
        StaticTokenAuthenticator([e, dict(e, token_sha256="b" * 64)])


# ------------------------------------------------------------------------------------ authorisation
def test_permissions_follow_roles_and_a_viewer_can_approve_nothing():
    auth = make_auth()
    rerun, remove = approve_permission("rerun_pipeline"), approve_permission("remove_injected_condition")
    roles = lambda who: auth.authenticate(TOKENS[who]).roles  # noqa: E731
    assert is_permitted(roles("admin"), rerun) and is_permitted(roles("admin"), remove)
    assert is_permitted(roles("approver"), rerun) and not is_permitted(roles("approver"), remove)
    assert not is_permitted(roles("viewer"), rerun) and not is_permitted(roles("authority"), rerun)
    assert not is_permitted(frozenset({"unknown_role"}), rerun)


def test_every_action_has_a_matching_permission_and_a_risk_class():
    for name, spec in ACTIONS.items():
        assert spec.permission == f"recovery:approve:{name}" and spec.risk in ("low", "medium", "high")


# ---------------------------------------------------------------------------------------- allowlist
def test_valid_actions_are_normalised_and_get_a_target():
    a = validate_action("rerun_pipeline", {"pipeline_id": "order_ingestion"})
    assert a.target == "pipeline:order_ingestion" and a.risk == "medium"
    b = validate_action("remove_injected_condition", {"scenario_key": "duplicate-records"})
    assert b.target == "scenario:duplicate-records" and b.risk == "high"


@pytest.mark.parametrize("action,params", [
    ("drop_table", {}), ("rerun_pipeline; rm -rf /", {"pipeline_id": "order_ingestion"}), ("", {}), (None, {}),
    ("RERUN_PIPELINE", {"pipeline_id": "order_ingestion"}), (["rerun_pipeline"], {}),
    ("rerun_pipeline", None), ("rerun_pipeline", []), ("rerun_pipeline", "order_ingestion"),
    ("rerun_pipeline", {}), ("rerun_pipeline", {"pipeline_id": "orders; DROP TABLE orders"}),
    ("rerun_pipeline", {"pipeline_id": "../../etc/passwd"}), ("rerun_pipeline", {"pipeline_id": 5}),
    ("rerun_pipeline", {"pipeline_id": "order_ingestion", "sql": "DELETE FROM orders"}),
    ("rerun_pipeline", {"pipeline_id": "order_ingestion", "target": "dataops_sim"}),
    ("remove_injected_condition", {"scenario_key": "everything"}), ("remove_injected_condition", {"scenario_key": None}),
])
def test_unknown_actions_and_bad_parameters_are_rejected(action, params):
    with pytest.raises(ActionRejected):
        validate_action(action, params)


def test_the_allowlist_contains_only_the_two_simulated_actions():
    assert set(ACTIONS) == {"rerun_pipeline", "remove_injected_condition"}


# ------------------------------------------------------------------------------- database-target guard
def fake_service(repo):
    return SimpleNamespace(repo=repo)


class SqlRepo:
    """Looks like app.db.repository.SqlRepository for the guard (same module prefix and an engine with a URL)."""
    __module__ = "app.db.repository"

    def __init__(self, database):
        self.engine = SimpleNamespace(url=SimpleNamespace(database=database))


@pytest.mark.parametrize("database", ["dataops_sim", "DATAOPS_SIM", "dataops", "master", "", None, "dataops_sim_test_x"])
def test_recovery_refuses_any_database_not_named_test(database):
    with pytest.raises(UnsafeRecoveryTarget):
        assert_recovery_target(fake_service(SqlRepo(database)))
    with pytest.raises(UnsafeRecoveryTarget):
        RecoveryExecutor(fake_service(SqlRepo(database)))


def test_recovery_allows_a_test_database_and_the_in_memory_simulation(service):
    assert_recovery_target(fake_service(SqlRepo("dataops_sim_test")))
    assert_recovery_target(service)


def test_a_sql_repository_without_an_engine_is_refused():
    broken = SimpleNamespace(__module__="app.db.repository")
    repo = type("R", (), {"__module__": "app.db.repository"})()
    with pytest.raises(UnsafeRecoveryTarget):
        assert_recovery_target(fake_service(repo))
