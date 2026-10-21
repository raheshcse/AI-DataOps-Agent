"""`streamlit run dashboard/<app>.py` must work from any working directory: Streamlit puts only the script's own folder on
sys.path, so each app has to make the project root importable itself. This runs the script the way Streamlit does."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("app", ["ops_app.py", "ledger_app.py"])
def test_app_script_imports_without_project_root_on_the_path(app, tmp_path):
    code = ("import runpy, sys\n"
            f"script = r'{ROOT / 'dashboard' / app}'\n"
            "sys.path[:] = [p for p in sys.path if p not in ('', r'" + str(ROOT) + "')]\n"
            "sys.path.insert(0, str(__import__('pathlib').Path(script).parent))\n"  # what Streamlit does
            "runpy.run_path(script, run_name='__main__')\n"
            "print('IMPORTED-OK')\n")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "DASHBOARD_API_TOKEN", "LEDGER_EXPLORER_API_TOKEN")}
    r = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90)
    assert "ModuleNotFoundError" not in r.stderr, r.stderr[-800:]
    assert "IMPORTED-OK" in r.stdout, r.stderr[-800:]
