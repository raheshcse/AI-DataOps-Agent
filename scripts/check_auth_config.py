"""Check, WITHOUT printing any secret, that a token matches the API's configured credentials.

Run it in the SAME PowerShell window (same environment) that you start uvicorn from:

    $env:CHECK_TOKEN = $viewer ; python -m scripts.check_auth_config ; Remove-Item Env:CHECK_TOKEN

It uses the API's own authenticator on the process environment. It prints the auth mode, whether the credentials parse,
whether the token matches (and the matching subject and roles), never the token, a digest or the credential list.
Exit 0 = token matches, 1 = no match / not configured, 2 = configuration error.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.v1.permissions import api_permissions  # noqa: E402
from app.recovery.auth import MIN_TOKEN_CHARS, ApprovalConfigError, DenyAllAuthenticator, build_authenticator  # noqa: E402


def main() -> int:
    mode = (os.environ.get("APPROVAL_AUTH_MODE") or "disabled").strip().lower()
    print(f"APPROVAL_AUTH_MODE = {mode}")
    print(f"APPROVAL_DEV_CREDENTIALS set: {bool((os.environ.get('APPROVAL_DEV_CREDENTIALS') or '').strip())}")
    try:
        auth = build_authenticator(os.environ)
    except ApprovalConfigError as exc:
        print(f"CONFIG ERROR (the API would reject every token): {exc}")
        return 2
    if isinstance(auth, DenyAllAuthenticator):
        print("Mode is 'disabled': the API rejects EVERY token. Set APPROVAL_AUTH_MODE=dev_static before starting uvicorn.")
        return 1
    token = os.environ.get("CHECK_TOKEN") or ""
    print(f"CHECK_TOKEN set: {bool(token)}; length ok (>= {MIN_TOKEN_CHARS}): {len(token) >= MIN_TOKEN_CHARS}; "
          f"has leading/trailing whitespace: {token != token.strip()}")
    ident = auth.authenticate(token.strip() if token else token)
    if ident is None:
        print("NO MATCH: this token's digest is not in APPROVAL_DEV_CREDENTIALS in this environment.")
        return 1
    perms = sorted(p for p in api_permissions(ident.roles) if p.endswith(":read"))
    print(f"MATCH: subject={ident.subject} roles={sorted(ident.roles)} read-permissions={perms}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
