"""scripts/dev_stack.py: credential generation, per-process environments, safety refusals. Starts no server."""
from __future__ import annotations

import json
import os
import socket
import stat

import pytest

from app.recovery.auth import build_authenticator
from scripts import dev_stack as ds


def test_credentials_are_random_stored_once_and_reused(tmp_path):
    path = tmp_path / "dev" / "dev_auth.json"
    a = ds.load_or_create_auth(path)
    assert all(len(a[k]) >= 32 for k in ("viewer", "explorer", "approver", "signing_key"))
    assert len({a["viewer"], a["explorer"], a["approver"], a["signing_key"]}) == 4
    assert ds.load_or_create_auth(path) == a  # stable across restarts, so pending approvals stay verifiable
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_malformed_credentials_file_is_refused_not_overwritten(tmp_path):
    path = tmp_path / "dev_auth.json"
    path.write_text(json.dumps({"viewer": "short"}))
    with pytest.raises(ds.LauncherError):
        ds.load_or_create_auth(path)
    assert json.loads(path.read_text()) == {"viewer": "short"}


def test_api_environment_enables_dev_static_and_the_project_authenticator_accepts_each_token(tmp_path):
    auth = ds.load_or_create_auth(tmp_path / "a.json")
    env = ds.api_env(auth, {"PATH": "x"}, 8000)
    assert env["APPROVAL_AUTH_MODE"] == "dev_static" and env["APPROVAL_SIGNING_KEY"] == auth["signing_key"]
    authenticator = build_authenticator(env)  # the API's own loader
    assert authenticator.authenticate(auth["viewer"]).subject == "dash.viewer"
    assert authenticator.authenticate(auth["explorer"]).subject == "ledger.viewer"
    assert "recovery_admin" in authenticator.authenticate(auth["approver"]).roles
    assert authenticator.authenticate("y" * 40) is None and authenticator.authenticate("") is None


def test_each_ui_gets_only_its_own_token_and_no_approval_secrets(tmp_path):
    auth = ds.load_or_create_auth(tmp_path / "a.json")
    base = {"APPROVAL_SIGNING_KEY": "leak", "APPROVAL_DEV_CREDENTIALS": "[]", "LEDGER_EXPLORER_API_TOKEN": "stale", "PATH": "x"}
    ops = ds.ops_env(auth, base, 8000, 8501, 8502)
    led = ds.ledger_env(auth, base, 8000, 8501, 8502)
    assert ops["DASHBOARD_API_TOKEN"] == auth["viewer"] and "LEDGER_EXPLORER_API_TOKEN" not in ops
    assert led["LEDGER_EXPLORER_API_TOKEN"] == auth["explorer"] and "DASHBOARD_API_TOKEN" not in led
    for env in (ops, led):
        assert not any(k.startswith("APPROVAL_") for k in env)
        assert env["DASHBOARD_API_BASE_URL"] == "http://127.0.0.1:8000/api/v1"
        assert auth["approver"] not in env.values() and auth["signing_key"] not in env.values()


def test_no_secret_ever_appears_on_a_command_line(tmp_path):
    auth = ds.load_or_create_auth(tmp_path / "a.json")
    cmds = [ds.api_command(8000), ds.streamlit_command("dashboard/ops_app.py", 8501), ds.streamlit_command("dashboard/ledger_app.py", 8502)]
    joined = " ".join(" ".join(c) for c in cmds)
    assert not any(v in joined for v in auth.values())
    assert all(c[0] for c in cmds) and "--host" in cmds[0] and "127.0.0.1" in cmds[0]


def test_refuses_production(tmp_path):
    auth = ds.load_or_create_auth(tmp_path / "a.json")
    with pytest.raises(ds.LauncherError):
        ds.api_env(auth, {"APP_ENV": "production"}, 8000)


def test_port_in_use_detection_and_busy_api_message():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); s.listen(1)
        port = s.getsockname()[1]
        assert ds.port_in_use(port)
        assert "never kills" in ds.describe_busy_api(port) or "not answering" in ds.describe_busy_api(port)
    assert not ds.port_in_use(port)


def test_investigator_credential_is_added_to_an_older_file_and_only_the_ops_ui_gets_it(tmp_path):
    path = tmp_path / "old.json"
    old = {k: "o" * 43 + str(i) for i, k in enumerate(("viewer", "explorer", "approver", "signing_key"))}
    path.write_text(json.dumps(old))
    a = ds.load_or_create_auth(path)
    assert all(a[k] == old[k] for k in old) and len(a["investigator"]) >= 32  # nothing existing was changed
    assert ds.load_or_create_auth(path) == a
    ops = ds.ops_env(a, {}, 8000, 8501, 8502)
    led = ds.ledger_env(a, {}, 8000, 8501, 8502)
    assert ops["DASHBOARD_INVESTIGATE_TOKEN"] == a["investigator"] and "DASHBOARD_INVESTIGATE_TOKEN" not in led
    authenticator = build_authenticator(ds.api_env(a, {}, 8000))
    roles = authenticator.authenticate(a["investigator"]).roles
    assert "operator" in roles and "recovery_admin" not in roles and "recovery_approver" not in roles  # it can never approve
