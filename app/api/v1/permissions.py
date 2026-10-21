"""What each role may do over HTTP.

This is separate from the Phase 7 *approval* permissions (``recovery:approve:<action>``). Those stay inside
``ApprovalService`` and are checked there on every decision. Here, ``approval:decide`` only lets a caller reach the
decision endpoint; it grants no ability to approve anything by itself.

Deliberately NOT exposed over HTTP: governance re-enablement (``governance_authority`` has no API permission) and any
way to clear a suspension.
"""

from __future__ import annotations

from typing import Iterable

READ = frozenset({"incident:read", "investigation:read", "approval:read", "recovery:read", "ticket:read"})
INVESTIGATE = frozenset({"incident:create", "investigation:run"})
OPERATE = frozenset({"recovery:request", "recovery:resume", "ticket:manage"})
DECIDE = frozenset({"approval:decide"})
LEDGER = frozenset({"ledger:read"})  # read-only VerbaLedger Explorer API; a separate boundary

API_PERMISSIONS: dict[str, frozenset[str]] = {
    "api_reader": READ | LEDGER,
    "viewer": READ | LEDGER,
    "investigator": READ | INVESTIGATE,
    "operator": READ | INVESTIGATE | OPERATE,
    "recovery_approver": READ | DECIDE | LEDGER,
    "recovery_admin": READ | DECIDE | LEDGER,
    "governance_authority": frozenset(),
}


def api_permissions(roles: Iterable[str]) -> frozenset[str]:
    granted: set[str] = set()
    for role in roles:
        granted |= API_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)
