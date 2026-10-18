"""Static guarantees about the governance code (AST-based; nothing is executed)."""

import ast
from pathlib import Path

GOV = Path(__file__).resolve().parents[1] / "app" / "governance"
APP = GOV.parent


def parse(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def imported_modules(tree):
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


def test_no_handler_can_swallow_automation_denied_exception():
    """Any bare/Exception/BaseException handler must be preceded, in the same try, by a handler for
    AutomationDeniedException (or InvariantViolation) that re-raises or returns a block."""
    offenders = []
    for path in GOV.glob("*.py"):
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Try):
                continue
            seen_denial = False
            for h in node.handlers:
                names = handler_names(h)
                if names & {"AutomationDeniedException", "InvariantViolation"}:
                    seen_denial = True
                if names & {"<bare>", "Exception", "BaseException"} and not seen_denial:
                    offenders.append(f"{path.name}:{h.lineno}")
    assert offenders == []


def test_policy_imports_only_vsl_core_and_the_standard_library():
    mods = imported_modules(parse(GOV / "policy.py"))
    allowed_roots = {"__future__", "dataclasses", "vsl_core"}
    assert {m.split(".")[0] for m in mods} <= allowed_roots


def test_only_the_workflow_entry_point_imports_the_gates_module():
    importers = []
    for path in APP.rglob("*.py"):
        if any(m == "app.governance.gates" for m in imported_modules(parse(path))):
            importers.append(path.relative_to(APP).as_posix())
    assert importers == ["governance/workflow.py"]


def test_the_agent_and_mcp_packages_do_not_import_governance():
    for pkg in ("agent", "mcp_server", "diagnostics"):
        for path in (APP / pkg).rglob("*.py"):
            assert not any(m.startswith("app.governance") for m in imported_modules(parse(path))), path


def test_the_human_reenablement_primitive_is_not_wired_into_the_graph():
    src = (GOV / "workflow.py").read_text()
    assert "re_enable" not in src and "request_re_enablement" not in src


def test_the_side_effect_is_called_in_exactly_one_place_after_the_gate_nodes():
    tree = parse(GOV / "workflow.py")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "execute" and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "runner"]
    assert len(calls) == 1
    fn = next(f for f in ast.walk(tree) if isinstance(f, ast.AsyncFunctionDef) and f.name == "execute_action")
    assert calls[0] in list(ast.walk(fn))


def test_execute_action_has_a_single_incoming_edge_from_the_prenode_gate():
    from app.governance import GovernanceRecorder, GovernedActionService, open_ledger
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        service = GovernedActionService(GovernanceRecorder(open_ledger(Path(d) / "l.jsonl", fsync=False)))
        edges = service.graph.get_graph().edges
        incoming = [(e.source, e.conditional) for e in edges if e.target == "execute_action"]
        assert incoming == [("guard_prenode", True)]


def test_ledger_data_and_env_are_git_ignored():
    text = (APP.parent / ".gitignore").read_text().splitlines()
    assert "data/" in text and ".env" in text
