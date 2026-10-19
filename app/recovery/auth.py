"""Approver authentication and authorisation (Phase 7).

Three separate questions, three separate pieces of code:

* **Authentication** - *who is this?* An ``Authenticator`` turns a credential into an ``Identity`` or ``None``.
* **Authorisation** - *may they approve this kind of action?* ``is_permitted`` checks the identity's roles.
* **Approval** - *did they approve this exact request?* That is ``app.recovery.approvals``.

There is no identity provider in this project. Two authenticators exist:

* ``DenyAllAuthenticator`` - the default. Every credential fails, so nothing can be approved until a verifier
  is configured (fail closed).
* ``StaticTokenAuthenticator`` - a LOCAL DEVELOPMENT verifier. Each approver has a long random bearer token;
  only its SHA-256 digest is configured, compared in constant time. It refuses to load when ``APP_ENV`` is
  ``production``. It is not enterprise identity: no expiry, rotation, MFA or revocation beyond editing config.

A permissive "accept anyone" authenticator is deliberately NOT in this package; tests define their own.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Mapping, Protocol

ROLE_APPROVER = "recovery_approver"        # may approve low/medium-risk recovery
ROLE_ADMIN = "recovery_admin"              # may approve every recovery action
ROLE_AUTHORITY = "governance_authority"    # may re-enable after a terminal state
ROLE_VIEWER = "viewer"                     # may look, never approve

PERM_RE_ENABLE = "governance:re_enable"


def approve_permission(action_type: str) -> str:
    return f"recovery:approve:{action_type}"


ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_APPROVER: frozenset({approve_permission("rerun_pipeline")}),
    ROLE_ADMIN: frozenset({approve_permission("rerun_pipeline"), approve_permission("remove_injected_condition")}),
    ROLE_AUTHORITY: frozenset({PERM_RE_ENABLE}),
    ROLE_VIEWER: frozenset(),
}

_SUBJECT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MIN_TOKEN_CHARS = 24  # shorter credentials are rejected without looking anything up


class ApprovalConfigError(Exception):
    """Invalid or unsafe approval configuration. Callers must treat this as 'no approvals possible'."""


@dataclass(frozen=True)
class Identity:
    subject: str
    roles: frozenset[str]


class Authenticator(Protocol):
    def authenticate(self, credential: object) -> Identity | None: ...

    def current_roles(self, subject: str) -> frozenset[str]: ...


def is_permitted(roles: frozenset[str], permission: str) -> bool:
    return any(permission in ROLE_PERMISSIONS.get(role, frozenset()) for role in roles)


class DenyAllAuthenticator:
    """Fail-closed default: nobody can authenticate."""

    def authenticate(self, credential: object) -> Identity | None:
        return None

    def current_roles(self, subject: str) -> frozenset[str]:
        return frozenset()


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class StaticTokenAuthenticator:
    """LOCAL DEVELOPMENT ONLY - see the module docstring."""

    def __init__(self, entries: list[Mapping[str, object]]) -> None:
        self._by_subject: dict[str, tuple[str, frozenset[str]]] = {}
        digests: set[str] = set()
        for entry in entries:
            subject, digest, roles = entry.get("subject"), entry.get("token_sha256"), entry.get("roles")
            if not isinstance(subject, str) or not _SUBJECT_RE.match(subject):
                raise ApprovalConfigError("an approver entry has an invalid subject")
            if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
                raise ApprovalConfigError("an approver entry has an invalid token_sha256 (need 64 lowercase hex chars)")
            if not isinstance(roles, list) or not roles or any(r not in ROLE_PERMISSIONS for r in roles):
                raise ApprovalConfigError("an approver entry has missing or unknown roles")
            if subject in self._by_subject or digest in digests:
                raise ApprovalConfigError("duplicate approver subject or token")
            digests.add(digest)
            self._by_subject[subject] = (digest, frozenset(roles))

    def authenticate(self, credential: object) -> Identity | None:
        if not isinstance(credential, str) or len(credential) < MIN_TOKEN_CHARS or len(credential) > 512:
            return None
        presented = token_digest(credential)
        found: Identity | None = None
        for subject, (digest, roles) in self._by_subject.items():  # no early exit: constant work per entry
            if hmac.compare_digest(presented, digest):
                found = Identity(subject, roles)
        return found

    def current_roles(self, subject: str) -> frozenset[str]:
        entry = self._by_subject.get(subject)
        return entry[1] if entry else frozenset()


def build_authenticator(environ: Mapping[str, str]) -> Authenticator:
    """Choose the verifier from the environment. Anything unexpected raises (callers fail closed)."""
    mode = environ.get("APPROVAL_AUTH_MODE", "disabled").strip().lower()
    if mode == "disabled":
        return DenyAllAuthenticator()
    if mode == "dev_static":
        if environ.get("APP_ENV", "development").strip().lower() == "production":
            raise ApprovalConfigError("APPROVAL_AUTH_MODE=dev_static is refused when APP_ENV=production")
        raw = environ.get("APPROVAL_DEV_CREDENTIALS", "").strip()
        if not raw:
            raise ApprovalConfigError("APPROVAL_DEV_CREDENTIALS is not set")
        try:
            entries = json.loads(raw)
        except ValueError as exc:
            raise ApprovalConfigError("APPROVAL_DEV_CREDENTIALS is not valid JSON") from exc
        if not isinstance(entries, list) or not entries or not all(isinstance(e, dict) for e in entries):
            raise ApprovalConfigError("APPROVAL_DEV_CREDENTIALS must be a non-empty JSON list of objects")
        return StaticTokenAuthenticator(entries)
    raise ApprovalConfigError("APPROVAL_AUTH_MODE must be 'disabled' or 'dev_static'")
