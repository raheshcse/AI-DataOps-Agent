r"""One predictable local-development launcher: API + Operations Dashboard + VerbaLedger Explorer.

    .\.venv\Scripts\python.exe -m scripts.dev_stack            (PowerShell, from the project root)

Why this exists: the API only enables authentication from variables in ITS OWN process environment, and each Streamlit app
reads its token from ITS OWN environment. Typing them into separate windows kept going wrong. This launcher builds the right
environment for each child process itself, so they cannot disagree.

* DEVELOPMENT ONLY (`dev_static` authentication). Refuses to run when APP_ENV=production.
* Tokens and the approval signing key are generated randomly on first run and stored in `.dev/dev_auth.json`
  (git-ignored, owner-only where the OS supports it). Nothing is hardcoded, put on a command line or printed
  (use --show-approver-token to print the approver token once, because you type it into the Human Review form).
* It never kills anything. If a port is busy it tells you what answered and stops.
* It touches no database itself. The API uses DATABASE_URL / APPROVAL_STORE_PATH / GOVERNANCE_LEDGER_PATH exactly as
  your environment or defaults specify; the simulator database is only read, as before.
* Success is reported only after: ready.authentication == dev_static, viewer token -> 200, no token -> 401,
  explorer token on the ledger -> 200/503, and both dashboards answer their Streamlit health check.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
DEV_DIR = Path(os.environ.get("DEV_STACK_DIR") or ROOT / ".dev")  # DEV_STACK_DIR lets tests keep state elsewhere
AUTH_FILE = DEV_DIR / "dev_auth.json"
LOG_DIR = DEV_DIR / "logs"
SUBJECTS = {"viewer": ("dash.viewer", "viewer"), "explorer": ("ledger.viewer", "viewer"), "approver": ("the.approver", "recovery_admin"),
            "investigator": ("dash.operator", "operator")}  # can start investigations and request (not approve) recoveries


class LauncherError(Exception):
    """A clear, secret-free message for the operator."""


# ------------------------------------------------------------------------------------------------ credentials
def load_or_create_auth(path: Path = AUTH_FILE) -> dict[str, str]:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if all(isinstance(data.get(k), str) and len(data[k]) >= 32 for k in ("viewer", "explorer", "approver", "signing_key")):
            if "investigator" not in data:  # a file written before the Investigate button existed: add only the new credential
                data["investigator"] = secrets.token_urlsafe(32)
                path.write_text(json.dumps(data), encoding="utf-8")
            return data
        raise LauncherError(f"{path} exists but is malformed. Move it aside (do not delete the folder) and rerun to regenerate.")
    data = {k: secrets.token_urlsafe(32) for k in ("viewer", "explorer", "approver", "investigator", "signing_key")}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data


def _digest(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def api_env(auth: Mapping[str, str], base: Mapping[str, str], api_port: int) -> dict[str, str]:
    if (base.get("APP_ENV") or "").strip().lower() == "production":
        raise LauncherError("Refusing: dev_static authentication is development-only and APP_ENV=production is set.")
    env = dict(base)
    env["APPROVAL_AUTH_MODE"] = "dev_static"
    env["APPROVAL_DEV_CREDENTIALS"] = json.dumps(
        [{"subject": s, "token_sha256": _digest(auth[k]), "roles": [role]} for k, (s, role) in SUBJECTS.items()], separators=(",", ":"))
    env["APPROVAL_SIGNING_KEY"] = auth["signing_key"]
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + base["PYTHONPATH"] if base.get("PYTHONPATH") else "")
    return env


def ops_env(auth: Mapping[str, str], base: Mapping[str, str], api_port: int, ops_port: int, ledger_port: int) -> dict[str, str]:
    env = {k: v for k, v in base.items() if not k.startswith("APPROVAL_")}  # the UIs never need the approval secrets
    env.update(DASHBOARD_API_BASE_URL=f"http://127.0.0.1:{api_port}/api/v1", DASHBOARD_API_TOKEN=auth["viewer"], DASHBOARD_INVESTIGATE_TOKEN=auth["investigator"],
               OPS_DASHBOARD_URL=f"http://127.0.0.1:{ops_port}", LEDGER_EXPLORER_URL=f"http://127.0.0.1:{ledger_port}")
    env.pop("LEDGER_EXPLORER_API_TOKEN", None)
    return env


def ledger_env(auth: Mapping[str, str], base: Mapping[str, str], api_port: int, ops_port: int, ledger_port: int) -> dict[str, str]:
    env = ops_env(auth, base, api_port, ops_port, ledger_port)
    env.pop("DASHBOARD_API_TOKEN", None)  # the Explorer gets only its own token
    env.pop("DASHBOARD_INVESTIGATE_TOKEN", None)
    env["LEDGER_EXPLORER_API_TOKEN"] = auth["explorer"]
    return env


# ------------------------------------------------------------------------------------------------ commands
def api_command(port: int) -> list[str]:
    return [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)]


def streamlit_command(script: str, port: int) -> list[str]:
    return [sys.executable, "-m", "streamlit", "run", script, "--server.port", str(port), "--server.address", "127.0.0.1",
            "--server.headless", "true", "--browser.gatherUsageStats", "false"]


# ------------------------------------------------------------------------------------------------ probes
def port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def http_status(url: str, token: str | None = None, timeout: float = 5.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed loopback http URL
            return resp.status, resp.read(65536)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536)
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


def describe_busy_api(port: int) -> str:
    code, body = http_status(f"http://127.0.0.1:{port}/api/v1/ready")
    try:
        mode = json.loads(body)["checks"]["authentication"]
    except (ValueError, KeyError, TypeError):
        return f"Port {port} is in use by something that is not answering this project's /api/v1/ready (HTTP {code})."
    return (f"Port {port} already runs an API whose authentication is '{mode}'. Stop it first (Ctrl+C in its window; "
            f"Get-NetTCPConnection -LocalPort {port} shows its PID), then rerun. This launcher never kills processes.")


def recovery_status(auth: Mapping[str, str], api_port: int) -> tuple[bool | None, str]:
    """Ask the running API whether Human Review / recovery are available. (True|False|None=unknown, one-line explanation).

    Informational only: the stack is still usable for exploring, importing and investigating when recovery is off."""
    code, body = http_status(f"http://127.0.0.1:{api_port}/api/v1/dashboard/summary", auth["viewer"])
    try:
        data = json.loads(body)
    except ValueError:
        return None, f"could not read the API's dashboard summary (HTTP {code})"
    if code != 200 or not isinstance(data, dict) or "recovery_available" not in data:
        return None, f"could not read the API's dashboard summary (HTTP {code})"
    if data["recovery_available"]:
        return True, "AVAILABLE - Human Review and governed recovery are enabled."
    info = data.get("recovery_unavailable") or {}
    if info.get("required_database_suffix"):
        return False, (f"UNAVAILABLE - recovery only runs on a database whose name ends with '{info['required_database_suffix']}' and this API "
                       f"uses '{info.get('database')}'. Use a separate demo database, e.g. $env:DB_NAME = \"dataops_demo_test\" "
                       "(README: 'Enable Human Review and recovery').")
    return False, f"UNAVAILABLE - reason: {info.get('reason', 'unknown')}. {info.get('fix', '')}".strip()


def verify(auth: Mapping[str, str], api_port: int, ops_port: int | None, ledger_port: int | None, *, wait: float = 40.0) -> list[tuple[str, bool, str]]:
    base = f"http://127.0.0.1:{api_port}/api/v1"
    results: list[tuple[str, bool, str]] = []
    mode, deadline = None, time.monotonic() + wait
    while time.monotonic() < deadline and mode is None:
        code, body = http_status(f"{base}/ready")
        try:
            mode = json.loads(body)["checks"]["authentication"]
        except (ValueError, KeyError, TypeError):
            time.sleep(1)
    results.append(("API ready, authentication mode", mode == "dev_static", f"authentication={mode}"))
    c_view, _ = http_status(f"{base}/incidents?limit=1", auth["viewer"])
    c_none, _ = http_status(f"{base}/incidents?limit=1")
    c_bad, _ = http_status(f"{base}/incidents?limit=1", "x" * 40)
    c_led, _ = http_status(f"{base}/ledger/summary", auth["explorer"])
    results += [("viewer token  -> /incidents", c_view == 200, f"HTTP {c_view} (expect 200)"),
                ("no token      -> /incidents", c_none == 401, f"HTTP {c_none} (expect 401)"),
                ("wrong token   -> /incidents", c_bad == 401, f"HTTP {c_bad} (expect 401)"),
                ("explorer token -> /ledger/summary", c_led in (200, 503), f"HTTP {c_led} (expect 200; 503 = no ledger file yet)")]
    for name, port in (("Operations Dashboard", ops_port), ("VerbaLedger Explorer", ledger_port)):
        if port is None:
            continue
        ok, code = False, 0
        for _ in range(int(wait)):
            code, _ = http_status(f"http://127.0.0.1:{port}/_stcore/health")
            if code == 200:
                ok = True
                break
            time.sleep(1)
        results.append((f"{name} server on :{port}", ok, f"HTTP {code} (expect 200)"))
    return results


# ------------------------------------------------------------------------------------------------ main
def _tail(path: Path, n: int = 12) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return "(no log)"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.dev_stack", description=__doc__.split("\n")[0])
    p.add_argument("--api-port", type=int, default=8000)
    p.add_argument("--ops-port", type=int, default=8501)
    p.add_argument("--ledger-port", type=int, default=8502)
    p.add_argument("--no-ui", action="store_true", help="start only the API")
    p.add_argument("--open", action="store_true", help="open both dashboards in your browser once verified")
    p.add_argument("--show-approver-token", action="store_true", help="print the dev approver token once (you type it into Human Review)")
    p.add_argument("--check-only", action="store_true", help="verify an already running stack started by this launcher, then exit")
    a = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(line_buffering=True)  # show progress live even when redirected
    try:
        if sys.prefix == sys.base_prefix:
            print("WARNING: not running inside a virtual environment. Use .\\.venv\\Scripts\\python.exe -m scripts.dev_stack")
        auth = load_or_create_auth()
        if a.check_only:
            res = verify(auth, a.api_port, None if a.no_ui else a.ops_port, None if a.no_ui else a.ledger_port, wait=5)
            for name, ok, detail in res:
                print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
            return 0 if all(ok for _, ok, _ in res) else 1
        base = dict(os.environ)
        ports = [("API", a.api_port)] + ([] if a.no_ui else [("Operations Dashboard", a.ops_port), ("VerbaLedger Explorer", a.ledger_port)])
        for label, port in ports:
            if port_in_use(port):
                raise LauncherError(describe_busy_api(port) if label == "API" else f"Port {port} ({label}) is already in use. Close what uses it, or pass a different port.")
        if not a.no_ui:
            try:
                import plotly, streamlit  # noqa: F401,E401
            except ImportError as exc:
                raise LauncherError(f"Missing dashboard dependency ({exc.name}). Run: python -m pip install -r requirements-dashboard.txt") from exc
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        specs = [("api", api_command(a.api_port), api_env(auth, base, a.api_port))]
        if not a.no_ui:
            specs += [("ops", streamlit_command("dashboard/ops_app.py", a.ops_port), ops_env(auth, base, a.api_port, a.ops_port, a.ledger_port)),
                      ("ledger", streamlit_command("dashboard/ledger_app.py", a.ledger_port), ledger_env(auth, base, a.api_port, a.ops_port, a.ledger_port))]
    except LauncherError as exc:
        print(f"ERROR: {exc}")
        return 2

    procs: list[tuple[str, subprocess.Popen[Any]]] = []
    logs = []
    try:
        for name, cmd, env in specs:
            fh = open(LOG_DIR / f"{name}.log", "w", encoding="utf-8")
            logs.append(fh)
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            procs.append((name, subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT, creationflags=flags)))
            print(f"started {name} (pid {procs[-1][1].pid}); log: {LOG_DIR / (name + '.log')}")
        results = verify(auth, a.api_port, None if a.no_ui else a.ops_port, None if a.no_ui else a.ledger_port)
        for name, ok, detail in results:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        dead = [(n, pr) for n, pr in procs if pr.poll() is not None]
        for n, pr in dead:
            print(f"ERROR: {n} exited with code {pr.returncode}. Last log lines:\n{_tail(LOG_DIR / (n + '.log'))}")
        if dead or not all(ok for _, ok, _ in results):
            print("NOT VERIFIED - stopping everything this launcher started.")
            return 1
        print("\nVERIFIED. Open:")
        print(f"  API docs               http://127.0.0.1:{a.api_port}/docs")
        if not a.no_ui:
            print(f"  Operations Dashboard   http://127.0.0.1:{a.ops_port}")
            print(f"  VerbaLedger Explorer   http://127.0.0.1:{a.ledger_port}")
        _available, _note = recovery_status(auth, a.api_port)
        print(f"Human Review / recovery: {_note}")
        print(f"Dev credentials file: {AUTH_FILE} (git-ignored). Approver token for Human Review decisions: "
              + (auth["approver"] if a.show_approver_token else "run again with --show-approver-token to display it"))
        print("Press Ctrl+C to stop everything this launcher started.")
        if a.open and not a.no_ui:
            webbrowser.open(f"http://127.0.0.1:{a.ops_port}")
            webbrowser.open(f"http://127.0.0.1:{a.ledger_port}")
        while all(pr.poll() is None for _, pr in procs):
            time.sleep(1)
        for n, pr in procs:
            if pr.poll() is not None:
                print(f"ERROR: {n} stopped unexpectedly (code {pr.returncode}). Last log lines:\n{_tail(LOG_DIR / (n + '.log'))}")
        return 1
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        for _, pr in procs:
            if pr.poll() is None:
                pr.terminate()
        deadline = time.monotonic() + 10
        for _, pr in procs:
            try:
                pr.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pr.kill()
        for fh in logs:
            fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
