"""Static guards for Phase 8 code and repository hygiene."""

import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PHASE8_DIRS = ["app/api/v1", "app/persistence", "app/integrations", "app/services"]


def sources():
    for d in PHASE8_DIRS:
        yield from (ROOT / d).rglob("*.py")


def test_no_exception_handler_silently_swallows_errors():
    """A bare ``except:`` or an ``except ...: pass`` hides failures (and would hide AutomationDeniedException)."""
    for path in sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ExceptHandler):
                assert node.type is not None, (path.name, node.lineno, "bare except")
                swallowed = all(isinstance(s, ast.Pass) for s in node.body)
                # the single allowed case: a best-effort ROLLBACK inside a handler that re-raises the original error
                allowed = path.name == "repositories.py" and ast.unparse(node.type) == "sqlite3.Error"
                assert not swallowed or allowed, (path.name, node.lineno, "except: pass")


def test_automation_denied_is_never_caught_by_name():
    for path in sources():
        text = path.read_text(encoding="utf-8")
        assert "except AutomationDeniedException" not in text, path.name


def test_credentials_are_never_passed_to_a_logger():
    sensitive = ("token", "credential", "authorization", "signing_key", "secret", "password")
    for path in sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("debug", "info", "warning", "error", "exception", "critical"):
                if isinstance(node.func.value, ast.Name) and node.func.value.id in ("logger", "log", "logging"):
                    rendered = ast.dump(node).lower()
                    assert not any(f"id='{w}" in rendered or f"attr='{w}" in rendered for w in sensitive), (path.name, node.lineno)


def test_no_hardcoded_credentials_in_application_code():
    for path in sources():
        text = path.read_text(encoding="utf-8")
        assert "ghp_" not in text and "github_pat_" not in text and "sk-" not in text.replace("task-", ""), path.name


def test_app_code_never_targets_the_simulation_database_for_writes():
    for path in (ROOT / "app" / "persistence").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and n.body
                      and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                assert "dataops_sim" not in node.value and "mssql" not in node.value.lower(), (path.name, node.value[:40])


def test_the_api_mutating_routes_all_require_a_permission():
    """Every non-health route in v1 must depend on ``require(...)``."""
    for path in (ROOT / "app" / "api" / "v1" / "routes").glob("*.py"):
        if path.name in ("health.py", "__init__.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and any(
                    isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("get", "post", "put", "patch", "delete")
                    for d in node.decorator_list):
                assert "require(" in ast.unparse(node.args), (path.name, node.name)


@pytest.mark.parametrize("name", [".env", "data/operations.sqlite3", "data/approvals.sqlite3", "data/governance_ledger.jsonl",
                                  "data/operations.sqlite3-wal", ".venv/x", "approvals.sqlite3", "x.sqlite3"])
def test_local_state_is_git_ignored(name):
    result = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT, capture_output=True)
    if result.returncode == 128:
        pytest.skip("not a git work tree here")
    assert result.returncode == 0, f"{name} is not ignored"


def test_env_example_has_placeholders_only():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("DATABASE_URL", "INVESTIGATION_MODE", "GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"):
        assert name in text
    assert "ghp_" not in text and "github_pat_" not in text
    for line in text.splitlines():
        if line.startswith("GITHUB_TOKEN="):
            assert line.strip() == "GITHUB_TOKEN="
