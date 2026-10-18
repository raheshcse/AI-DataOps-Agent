"""The protected-action boundary. Phase 6 has no real recovery action, so this is a no-op stub.

``NoOpProtectedAction`` touches no database, file or network. It only records that it was reached, so tests
can prove a denied path never gets here. It refuses to run without an authorisation ledger entry id.
"""

from __future__ import annotations

from typing import Any, Protocol


class ProtectedActionRunner(Protocol):
    def execute(self, action: str, authorisation_entry_id: str) -> dict[str, Any]: ...


class UnauthorisedExecution(Exception):
    """Raised if the runner is reached without an authorisation record (a wiring bug; fails closed)."""


class NoOpProtectedAction:
    def __init__(self) -> None:
        self.executed: list[tuple[str, str]] = []

    def execute(self, action: str, authorisation_entry_id: str) -> dict[str, Any]:
        if not authorisation_entry_id:
            raise UnauthorisedExecution("protected action reached without an authorisation record")
        self.executed.append((action, authorisation_entry_id))
        return {"action": action, "effect": "none (no-op stub)"}
