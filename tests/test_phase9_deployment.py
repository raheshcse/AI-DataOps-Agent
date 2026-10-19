"""Phase 9 deployment-configuration guards: production start-up validation, Dockerfile, compose, .dockerignore, CI.
These read files and run the application in-process; nothing here builds an image or contacts a registry."""

import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.api_support import reset_main_services
from app.core.startup import REQUIRED_IN_PRODUCTION, StartupConfigError, production_config_problems, validate_startup

ROOT = Path(__file__).resolve().parents[1]
GOOD = {"APP_ENV": "production", "DATABASE_URL": "sqlite:////data/operations.sqlite3", "APPROVAL_STORE_PATH": "/data/approvals.sqlite3",
        "GOVERNANCE_LEDGER_PATH": "/data/governance_ledger.jsonl", "DB_NAME": "dataops_sim_test"}


@pytest.fixture(autouse=True)
def fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def set_env(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


# ------------------------------------------------------------------------------------------ start-up validation
def test_development_and_test_are_never_blocked(monkeypatch):
    for mode in ("development", "test"):
        set_env(monkeypatch, {"APP_ENV": mode, "APPROVAL_AUTH_MODE": "dev_static"})
        assert production_config_problems() == []
        validate_startup()


def test_production_with_everything_explicit_starts(monkeypatch):
    set_env(monkeypatch, GOOD)
    assert production_config_problems() == []


@pytest.mark.parametrize("missing", REQUIRED_IN_PRODUCTION)
def test_production_refuses_to_start_without_an_explicit_target(monkeypatch, missing):
    env = {k: v for k, v in GOOD.items() if k != missing}
    set_env(monkeypatch, env)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(StartupConfigError) as info:
        validate_startup()
    assert missing in str(info.value) and info.value.problems


def test_production_refuses_the_development_authenticator(monkeypatch):
    set_env(monkeypatch, {**GOOD, "APPROVAL_AUTH_MODE": "dev_static", "APPROVAL_DEV_CREDENTIALS": "[]"})
    with pytest.raises(StartupConfigError) as info:
        validate_startup()
    assert "dev_static" in str(info.value)


def test_startup_errors_name_variables_but_never_values(monkeypatch):
    secret = "hunter2-SECRET-VALUE"
    set_env(monkeypatch, {**GOOD, "APPROVAL_AUTH_MODE": "dev_static", "APPROVAL_DEV_CREDENTIALS": secret,
                          "APPROVAL_SIGNING_KEY": secret, "GITHUB_TOKEN": secret, "DB_PASSWORD": secret})
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(StartupConfigError) as info:
        validate_startup()
    assert secret not in str(info.value) and secret not in repr(info.value)


def test_invalid_settings_in_production_stop_the_start(monkeypatch):
    set_env(monkeypatch, {**GOOD, "DB_NAME": "bad name;DROP"})
    with pytest.raises(StartupConfigError) as info:
        validate_startup()
    assert "db_name" in str(info.value).lower() and "DROP" not in str(info.value)


def test_the_application_itself_refuses_to_start_in_unsafe_production(monkeypatch):
    from app.main import app
    set_env(monkeypatch, {"APP_ENV": "production"})
    for k in REQUIRED_IN_PRODUCTION:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(StartupConfigError):
        with TestClient(app):
            pass


def test_the_application_starts_in_correctly_configured_production(monkeypatch, tmp_path):
    from app.main import app
    reset_main_services(app)
    set_env(monkeypatch, {**GOOD, "DATABASE_URL": f"sqlite:///{(tmp_path / 'o.sqlite3').as_posix()}",
                          "APPROVAL_STORE_PATH": str(tmp_path / "a.sqlite3"), "GOVERNANCE_LEDGER_PATH": str(tmp_path / "l.jsonl")})
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        r = client.get("/api/v1/incidents", headers={"Authorization": "Bearer anything-0123456789abcdefghij"})
        assert r.status_code == 401                         # production default: nobody authenticates
        ready = client.get("/api/v1/ready").json()
        assert ready["environment"] == "production" and ready["checks"]["authentication"] == "disabled"
    reset_main_services(app)


# ------------------------------------------------------------------------------------------ Dockerfile
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def instructions():
    return [line.strip() for line in DOCKERFILE.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_dockerfile_runs_as_a_non_root_numeric_user():
    users = [i for i in instructions() if i.startswith("USER ")]
    assert users and users[-1].split()[1].split(":")[0] not in ("root", "0")
    assert re.fullmatch(r"USER \d{4,}(:\d{4,})?", users[-1])


def test_dockerfile_bakes_in_no_secrets_or_local_state():
    forbidden = ("APPROVAL_SIGNING_KEY=", "GITHUB_TOKEN=", "OPENAI_API_KEY=", "DB_PASSWORD=", "APPROVAL_DEV_CREDENTIALS=", "APPROVAL_AUTH_MODE=dev_static")
    for line in instructions():
        assert not any(f in line for f in forbidden), line
        if line.startswith(("COPY", "ADD")):
            assert ".env" not in line and "data" not in line.split()[1:-1] and ".sqlite" not in line and ".jsonl" not in line, line
    assert "DB_NAME" not in "\n".join(i for i in instructions() if i.startswith("ENV") or i.startswith("    "))  # target must be explicit


def test_dockerfile_state_is_on_one_volume_and_runs_one_worker():
    text = DOCKERFILE
    assert 'VOLUME ["/data"]' in text and "/data/operations.sqlite3" in text and "/data/governance_ledger.jsonl" in text
    cmd = [i for i in instructions() if i.startswith("CMD")][-1]   # the last CMD (the first is the HEALTHCHECK probe)
    assert '"--workers", "1"' in cmd and "--reload" not in cmd and cmd.startswith('CMD ["uvicorn"')   # exec form, graceful SIGTERM


def test_dockerfile_has_a_health_check_on_the_liveness_endpoint():
    health = next(i for i in instructions() if i.startswith("HEALTHCHECK") or "/api/v1/health" in i)
    assert "/api/v1/health" in DOCKERFILE and "HEALTHCHECK" in DOCKERFILE and health


def test_microsoft_driver_licence_is_opt_in():
    assert "INSTALL_MSSQL_DRIVER=false" in DOCKERFILE and "ACCEPT_MSODBC_EULA=N" in DOCKERFILE
    assert DOCKERFILE.index("ACCEPT_EULA=Y") > DOCKERFILE.index('"$INSTALL_MSSQL_DRIVER" = "true"')


def test_dockerfile_installs_the_unchanged_requirements_reproducibly():
    assert "pip install -r /tmp/requirements.txt" in DOCKERFILE and "pip check" in DOCKERFILE
    assert "--no-deps" not in DOCKERFILE and "--force" not in DOCKERFILE


def test_dockerignore_keeps_secrets_and_local_state_out_of_the_build_context():
    lines = {l.strip() for l in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")}
    for needed in (".git", ".env", ".env.*", ".venv", "data", "logs", "**/__pycache__", "**/*.sqlite3", "**/*.jsonl", "tests"):
        assert needed in lines, needed
    assert "!.env.example" in lines


# ------------------------------------------------------------------------------------------ compose
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_publishes_on_loopback_only_and_holds_no_secret_values():
    api = COMPOSE["services"]["api"]
    assert all(p.startswith("127.0.0.1:") for p in api["ports"])
    env = api["environment"]
    for secret in ("APPROVAL_SIGNING_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY", "DB_PASSWORD", "APPROVAL_DEV_CREDENTIALS", "DB_USER"):
        assert env[secret] is None, secret                      # pass-through from the shell, never a literal
    assert "DB_NAME" in env and ":?" in env["DB_NAME"]          # the database target must be stated
    assert api["read_only"] is True and "ALL" in api["cap_drop"] and "deploy" not in api and "replicas" not in str(api)
    assert "dataops_data" in COMPOSE["volumes"]


# ------------------------------------------------------------------------------------------ CI
CI_TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
CI = yaml.safe_load(CI_TEXT)


def test_ci_needs_no_secrets_and_publishes_nothing():
    assert "secrets." not in CI_TEXT and "OPENAI" not in CI_TEXT.upper()
    assert "docker login" not in CI_TEXT and "docker push" not in CI_TEXT and "push: true" not in CI_TEXT
    assert not re.search(r"\b(deploy|terraform|az |aws |gcloud|kubectl)\b", re.sub(r"#.*", "", CI_TEXT), re.I)
    assert CI["permissions"] == {"contents": "read"}


def test_ci_covers_the_supported_pythons_and_the_required_checks():
    matrix = CI["jobs"]["tests"]["strategy"]["matrix"]["python-version"]
    assert {"3.10", "3.12"} <= set(matrix)
    steps = " ".join(str(s.get("run", "")) for s in CI["jobs"]["tests"]["steps"])
    for needed in ("pip check", "pytest", "run_conformance_suite", "requirements.txt"):
        assert needed in steps, needed
    assert "docker build" in " ".join(str(s.get("run", "")) for s in CI["jobs"]["docker-build"]["steps"])


def test_ci_triggers_on_push_and_pull_request():
    triggers = CI.get("on") or CI.get(True)
    assert "push" in triggers and "pull_request" in triggers


def test_ci_installs_only_from_requirements_and_never_changes_the_vsl_pins():
    runs = " ".join(str(st.get("run", "")) for job in CI["jobs"].values() for st in job["steps"])
    assert "git+" not in runs and "--no-deps" not in runs and "--force" not in runs and "pip install -r requirements.txt" in runs


def test_env_example_documents_phase9_names_without_values():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "APP_ENV" in text
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("APPROVAL_SIGNING_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY", "DB_PASSWORD"):
            assert value.strip() == "" or value.strip().startswith(("#", "replace-with")), line
