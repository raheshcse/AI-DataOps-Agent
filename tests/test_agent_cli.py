"""Phase 5 command-line tests: ``scripts.investigate`` and ``scripts.openai_smoke``.

The MCP target is the in-memory server (injected) and no OpenAI client is ever constructed: the live model
class is replaced by fakes where a "live" path must be exercised.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.investigate as cli
import scripts.openai_smoke as smoke
from app.agent import LLMError, MissingApiKey
from app.agent.models import InvestigationReport
from tests.agent_support import FINISH, ScriptedModel, apply_scenario, draft, plan, server_for, tc

ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "sk-test-FAKEFAKEFAKE1234567890"


def run_main(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["investigate", *argv])
    code = cli.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def inject(monkeypatch, service):
    """Send the CLI's investigation to the in-memory MCP server instead of spawning the real one."""
    real = cli.investigate_sync
    monkeypatch.setattr(cli, "investigate_sync", lambda incident, **kw: real(incident, mcp_target=server_for(service), **kw))
    return service


# -------------------------------------------------------------------------------- configuration
def test_check_config_reports_key_state_without_the_key_or_any_network(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setattr(cli, "investigate_sync", lambda *a, **k: pytest.fail("an investigation was started"))
    code, out, err = run_main(monkeypatch, capsys, "--check-config")
    assert code == 0 and "configured" in out and "ready" in out and FAKE_KEY not in out + err


@pytest.mark.parametrize("value,state", [(None, "missing"), ("replace-with-your-openai-api-key", "placeholder")])
def test_check_config_without_a_usable_key_exits_2(monkeypatch, capsys, value, state):
    if value:
        monkeypatch.setenv("OPENAI_API_KEY", value)
    code, out, _ = run_main(monkeypatch, capsys, "--check-config")
    assert code == 2 and state in out and "--offline" in out


def test_live_mode_without_a_key_fails_before_any_mcp_or_network_activity(monkeypatch, capsys):
    monkeypatch.setattr(cli, "investigate_sync", lambda *a, **k: pytest.fail("MCP/agent started without a key"))
    code, out, err = run_main(monkeypatch, capsys, "Orders are missing")
    assert code == 2 and "Cannot start a live investigation" in out and "OPENAI_API_KEY" in out and "--offline" in out


def test_a_placeholder_key_is_treated_as_missing_and_not_echoed(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "replace-with-your-openai-api-key")
    code, out, _ = run_main(monkeypatch, capsys, "Orders are missing")
    assert code == 2 and "placeholder" in out and "replace-with-your-openai-api-key" not in out


def test_live_wiring_uses_the_configured_key_and_marks_the_run_billable(monkeypatch, capsys, inject):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    built = []

    def fake_live(config):
        built.append(config.key_state)
        return ScriptedModel([plan(tc("get_pipeline_status")), FINISH], report=draft())

    monkeypatch.setattr(cli, "OpenAIChatModel", fake_live)
    code, out, err = run_main(monkeypatch, capsys, "The pipeline failed")
    assert built == ["configured"] and "billable" in err and FAKE_KEY not in out + err and code in (0, 1)


# ---------------------------------------------------------------------------------- input handling
@pytest.mark.parametrize("argv", [[], ["   ", "--offline"], ["x" * 2001, "--offline"], ["x", "--offline", "--max-rounds", "99"],
                                  ["x", "--offline", "--max-tool-calls", "0"]])
def test_bad_input_exits_3_with_a_clear_message(monkeypatch, capsys, inject, argv):
    code, out, _ = run_main(monkeypatch, capsys, *argv)
    assert code == 3 and out.strip()


# ----------------------------------------------------------------------------------- offline runs
def test_offline_healthy_system_exits_0_and_prints_a_labelled_text_report(monkeypatch, capsys, inject):
    code, out, err = run_main(monkeypatch, capsys, "The orders report looks wrong", "--offline")
    assert code == 0 and "MODE: OFFLINE heuristic" in out and "LIKELY ROOT CAUSE" in out and "Undetermined." in out
    assert "Mode: offline" in err and "billable" not in err


def test_offline_confirmed_issue_exits_1(monkeypatch, capsys, inject):
    apply_scenario(inject, "pipeline-failure")
    code, out, _ = run_main(monkeypatch, capsys, "The nightly order pipeline failed", "--offline")
    assert code == 1 and "issue_identified" in out and "E1001" in out


def test_offline_unreachable_database_exits_2(monkeypatch, capsys):
    from app.mcp_server import ServiceProvider, create_server

    def boom():
        raise RuntimeError("password=hunter2 SECRETHOST")

    real = cli.investigate_sync
    monkeypatch.setattr(cli, "investigate_sync", lambda incident, **kw: real(incident, mcp_target=create_server(ServiceProvider(boom)), **kw))
    code, out, err = run_main(monkeypatch, capsys, "orders look wrong", "--offline")
    assert code == 2 and "failed" in out and "hunter2" not in out + err and "SECRETHOST" not in out + err


def test_json_output_is_pure_valid_report_json_and_progress_goes_to_stderr(monkeypatch, capsys, inject):
    apply_scenario(inject, "stale-data")
    code, out, err = run_main(monkeypatch, capsys, "Data is stale", "--offline", "--json")
    report = InvestigationReport.model_validate(json.loads(out))
    assert report.status == "issue_identified" and code == 1 and "check_data_freshness" in err


def test_quiet_suppresses_progress_and_no_evidence_trims_the_text(monkeypatch, capsys, inject):
    code, out, err = run_main(monkeypatch, capsys, "orders look wrong", "--offline", "--quiet", "--no-evidence")
    assert err == "" and "\nEVIDENCE\n" not in out and "LIMITATIONS" in out


def test_nothing_secret_is_printed_even_when_a_key_is_configured(monkeypatch, capsys, inject):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    code, out, err = run_main(monkeypatch, capsys, f"Orders missing, my key is {FAKE_KEY}", "--offline")
    assert FAKE_KEY not in out + err and "[REDACTED]" in out


def test_the_script_runs_as_a_module_and_reports_config_without_the_key(tmp_path):
    # A deliberately small environment: none of the developer's own OPENAI_*/DB_*/AGENT_* variables leak in. The
    # operating-system variables a child Python needs are passed through unchanged - on Windows an empty or
    # missing SYSTEMROOT makes interpreter start-up fail ("failed to get random numbers").
    os_vars = ("SYSTEMROOT", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP", "TMPDIR")
    env = {k: v for k, v in os.environ.items() if k.upper() in os_vars}
    env.update({"PYTHONPATH": str(ROOT), "OPENAI_API_KEY": FAKE_KEY})
    out = subprocess.run([sys.executable, "-m", "scripts.investigate", "--check-config"], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and "configured" in out.stdout and FAKE_KEY not in out.stdout + out.stderr


# ------------------------------------------------------------------------------------ smoke script
def run_smoke(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["openai_smoke", *argv])
    code = smoke.main()
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_smoke_test_without_confirmation_makes_no_request(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setattr(smoke, "OpenAIChatModel", lambda *a, **k: pytest.fail("a model was constructed"))
    code, out = run_smoke(monkeypatch, capsys)
    assert code == 3 and "No request made" in out and FAKE_KEY not in out


def test_smoke_test_with_confirmation_but_no_key_exits_2(monkeypatch, capsys):
    code, out = run_smoke(monkeypatch, capsys, "--yes-call-openai")
    assert code == 2 and "OPENAI_API_KEY" in out


class OkModel:
    last_usage = {"prompt_tokens": 5, "completion_tokens": 1}

    async def complete_json(self, task, system, payload, schema):
        assert task == "smoke"
        return {"ok": True}


class BadModel:
    last_usage = None

    async def complete_json(self, *a, **k):
        raise LLMError("auth", 401)


def test_smoke_test_success_and_failure_paths_never_print_the_key(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setattr(smoke, "OpenAIChatModel", lambda config: OkModel())
    code, out = run_smoke(monkeypatch, capsys, "--yes-call-openai")
    assert code == 0 and "Smoke test OK" in out and FAKE_KEY not in out
    monkeypatch.setattr(smoke, "OpenAIChatModel", lambda config: BadModel())
    code, out = run_smoke(monkeypatch, capsys, "--yes-call-openai")
    assert code == 2 and "FAILED" in out and "HTTP 401" in out and FAKE_KEY not in out
