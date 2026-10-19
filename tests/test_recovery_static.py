"""Static guarantees about the Phase 7 recovery code (AST-based; nothing is executed)."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
REC = APP / "recovery"


def parse(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def imports(tree):
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    return mods


def handler_names(handler):
    t = handler.type
    if t is None:
        return {"<bare>"}
    elts = t.elts if isinstance(t, ast.Tuple) else [t]
    return {e.id if isinstance(e, ast.Name) else getattr(e, "attr", "?") for e in elts}


def test_no_handler_in_recovery_can_swallow_automation_denied_exception():
    offenders = []
    for path in REC.glob("*.py"):
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Try):
                continue
            seen = False
            for h in node.handlers:
                names = handler_names(h)
                if "AutomationDeniedException" in names:
                    seen = True
                if names & {"<bare>", "Exception", "BaseException"} and not seen:
                    offenders.append(f"{path.name}:{h.lineno}")
    assert not offenders, offenders


def test_recovery_has_no_dynamic_execution_or_shell_access():
    banned_modules = {"subprocess", "os", "pty", "ctypes", "importlib", "runpy", "socket"}
    offenders = []
    for path in REC.glob("*.py"):
        tree = parse(path)
        offenders += [f"{path.name}: imports {m}" for m in imports(tree) if m.split(".")[0] in banned_modules]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Name) and f.id in {"eval", "exec", "compile", "__import__", "open_shell"}:
                offenders.append(f"{path.name}:{node.lineno} calls builtin {f.id}")  # re.compile is an Attribute, not this
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in {"subprocess", "os"}:
                offenders.append(f"{path.name}:{node.lineno} calls {f.value.id}.{f.attr}")
    assert not offenders, offenders


def test_the_executor_is_only_driven_by_the_runner():
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name in {"runner.py", "actions.py"} and path.parent == REC:
            continue
        for node in ast.walk(parse(path)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("check_precondition", "execute") and "executor" in ast.unparse(node.func.value)):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, offenders
    runner = (REC / "runner.py").read_text(encoding="utf-8")
    assert "self.executor.execute(" in runner and runner.count("self.executor.execute(") == 1


def test_production_authentication_has_no_permissive_implementation():
    src = (REC / "auth.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert classes >= {"DenyAllAuthenticator", "StaticTokenAuthenticator"}
    assert "AllowAll" not in src and "AlwaysAllow" not in src and "NoAuth" not in src
    assert "production" in src  # the dev authenticator names and refuses the production environment


def test_re_enablement_is_not_reachable_from_any_graph_or_agent_code():
    allowed = {REC / "reenable.py", REC / "__init__.py", APP / "governance" / "ledger.py"}
    offenders = [str(p.relative_to(ROOT)) for p in APP.rglob("*.py") if p not in allowed
                 and ("ReEnablementService" in p.read_text(encoding="utf-8") or ".re_enable(" in p.read_text(encoding="utf-8"))]
    assert not offenders, offenders


def test_the_original_investigation_code_does_not_import_governance_or_recovery():
    for sub in ("agent", "mcp_server", "simulation"):
        d = APP / sub
        if not d.exists():
            continue
        for path in d.rglob("*.py"):
            bad = {m for m in imports(parse(path)) if m.startswith(("app.recovery", "app.governance"))}
            assert not bad, (path, bad)


def test_recovery_reuses_the_governance_service_and_does_not_import_gate_internals():
    mods = set()
    for path in REC.glob("*.py"):
        mods |= imports(parse(path))
    assert "app.governance.gates" not in mods
    assert (REC / "workflow.py").read_text(encoding="utf-8").count("governed.govern(") >= 1


def test_secrets_and_local_state_stay_out_of_git():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("data/", ".env", "*.sqlite3", "*.db"):
        assert pattern in ignore
