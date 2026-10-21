"""scripts/check_auth_config.py confirms token matching without printing the token, its digest or the credential list."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "diagnostic-test-token-0123456789abcdef"
DIGEST = hashlib.sha256(TOKEN.encode()).hexdigest()
CREDS = json.dumps([{"subject": "dash.viewer", "token_sha256": DIGEST, "roles": ["viewer"]}])


def run(**env):
    base = {k: v for k, v in os.environ.items() if not k.startswith(("APPROVAL_", "CHECK_"))}
    return subprocess.run([sys.executable, "-m", "scripts.check_auth_config"], cwd=ROOT, env={**base, **env}, capture_output=True, text=True, timeout=60)


def test_match_wrong_token_disabled_and_broken_config():
    ok = run(APPROVAL_AUTH_MODE="dev_static", APPROVAL_DEV_CREDENTIALS=CREDS, CHECK_TOKEN=TOKEN)
    assert ok.returncode == 0 and "MATCH: subject=dash.viewer" in ok.stdout
    assert TOKEN not in ok.stdout + ok.stderr and DIGEST not in ok.stdout + ok.stderr
    assert run(APPROVAL_AUTH_MODE="dev_static", APPROVAL_DEV_CREDENTIALS=CREDS, CHECK_TOKEN=TOKEN[::-1]).returncode == 1
    assert run(APPROVAL_DEV_CREDENTIALS=CREDS, CHECK_TOKEN=TOKEN).returncode == 1  # mode defaults to disabled: nobody matches
    assert run(APPROVAL_AUTH_MODE="dev_static", APPROVAL_DEV_CREDENTIALS="{bad", CHECK_TOKEN=TOKEN).returncode == 2
