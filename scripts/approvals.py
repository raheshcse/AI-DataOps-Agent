"""Review recovery approval requests: list, show, approve, reject, cancel, hash-token.

    python -m scripts.approvals list [--status pending]
    python -m scripts.approvals show REQUEST_ID
    python -m scripts.approvals approve REQUEST_ID [--reason TEXT]
    python -m scripts.approvals reject REQUEST_ID [--reason TEXT]
    python -m scripts.approvals hash-token            (prints the SHA-256 to put in APPROVAL_DEV_CREDENTIALS)

The approver's token is read from the environment variable APPROVAL_TOKEN or a hidden prompt - never from the
command line, and it is never printed or stored. Configuration (see docs/phase7-human-approval-recovery.md):
APPROVAL_AUTH_MODE (default 'disabled' = nobody can approve), APPROVAL_DEV_CREDENTIALS, APPROVAL_SIGNING_KEY,
APPROVAL_STORE_PATH. This tool only records the human decision; it executes nothing. Exit 0 ok, 1 refused, 2 misconfigured.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path

from app.governance.ledger import GovernanceConfigError, GovernanceLedgerError, GovernanceRecorder, open_ledger
from app.recovery.approvals import ApprovalDenied, ApprovalService, ApprovalSigner, SqliteApprovalStore
from app.recovery.auth import ApprovalConfigError, build_authenticator, token_digest


def _service() -> ApprovalService:
    env = os.environ
    signer, authenticator = ApprovalSigner.from_environ(env), build_authenticator(env)  # validate config first
    store = SqliteApprovalStore(env.get("APPROVAL_STORE_PATH") or None)
    recorder = GovernanceRecorder(open_ledger(env.get("GOVERNANCE_LEDGER_PATH") or None))
    return ApprovalService(store, signer, authenticator, recorder)


def _credential() -> str:
    return os.environ.get("APPROVAL_TOKEN") or getpass.getpass("Approver token (hidden): ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.approvals", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--status", choices=["pending", "approved", "rejected", "expired", "cancelled", "consumed"])
    sub.add_parser("show").add_argument("request_id")
    for name in ("approve", "reject", "cancel"):
        p = sub.add_parser(name)
        p.add_argument("request_id")
        p.add_argument("--reason", default="")
    sub.add_parser("hash-token")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    try:
        if args.cmd == "hash-token":
            token = _credential()
            if len(token) < 24:
                print("Refusing: use a random token of at least 24 characters.")
                return 1
            print(token_digest(token))
            return 0
        svc = _service()
        if args.cmd == "list":
            for rec in svc.store.list(args.status):
                print(f"{rec.request_id}  {rec.status:<9} {rec.action_type} {rec.target} risk={rec.risk} expires={rec.expires_at}")
            return 0
        if args.cmd == "show":
            rec = svc.store.get(args.request_id)
            if rec is None:
                print("No such request.")
                return 1
            for key, value in rec.summary().items():
                print(f"{key}: {value}")
            return 0
        action = {"approve": svc.approve, "reject": svc.reject, "cancel": svc.cancel}[args.cmd]
        rec = action(args.request_id, _credential(), args.reason)
        print(f"{args.request_id}: {rec.status} by {rec.decided_by}")
        return 0
    except ApprovalConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2
    except ApprovalDenied as exc:
        print(f"Refused: {exc.code}")
        return 1
    except (GovernanceLedgerError, GovernanceConfigError, sqlite3.Error, OSError) as exc:
        print(f"Storage error: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
