"""Local VSL ledger recording, suspension state and the human re-enablement primitive.

The ledger is a hash-chained JSONL file (``vsl_core.ledger.JsonlLedgerStore``) under ``data/`` (git-ignored).
Every causal link uses ``caused_by``. Any failure to read, verify or write the ledger raises
``GovernanceLedgerError``; the workflow treats that as "no protected action".

Known limits (also in docs/vsl-governance.md): ``verify_integrity()`` cannot detect truncation or replacement
with an older consistent chain; suspension is rehydrated from the ledger, so deleting or truncating the file
loses it; the file is single-host with no durability claim beyond ``fsync``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from vsl_core.exceptions import AutomationDeniedException
from vsl_core.governance import GovernanceAuthority, request_re_enablement
from vsl_core.identity import Evidence, Instance
from vsl_core.ledger import (
    JsonlLedgerStore,
    LedgerEntry,
    LedgerEntryType,
    VerbaLedger,
    VerificationResult,
)

DEFAULT_LEDGER_PATH = Path(__file__).resolve().parents[2] / "data" / "governance_ledger.jsonl"
IDENTITY_KEY = "dataops-agent-governance"


class GovernanceConfigError(Exception):
    """Invalid governance configuration; no protected action may run."""


class GovernanceLedgerError(Exception):
    """The ledger could not be read, verified or written; no protected action may run."""


def open_ledger(path: Path | str | None = None, *, fsync: bool = True) -> VerbaLedger:
    target = Path(path) if path else DEFAULT_LEDGER_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        return VerbaLedger(JsonlLedgerStore(target, fsync=fsync))
    except OSError as exc:
        raise GovernanceConfigError(f"cannot open the governance ledger at {target}: {type(exc).__name__}") from exc


def _short(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


class GovernanceRecorder:
    """Writes the governance trail and answers 'is automation suspended?' from the ledger."""

    def __init__(self, ledger: VerbaLedger, *, instance: Instance | None = None) -> None:
        self.ledger = ledger
        self.instance = instance or Instance.new()
        self._suspended_locally = False  # set before the TERMINAL write so a failed write still blocks

    # ---- reading ---------------------------------------------------------------------------------------
    def _entries(self) -> list[LedgerEntry]:
        try:
            return list(self.ledger.store.all_entries())
        except AutomationDeniedException:
            raise
        except Exception as exc:  # noqa: BLE001 - a read failure must fail closed, as a ledger error
            raise GovernanceLedgerError(f"ledger unreadable: {type(exc).__name__}") from exc

    def verify_or_raise(self) -> None:
        try:
            ok = self.ledger.verify_integrity()
        except AutomationDeniedException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GovernanceLedgerError(f"ledger unreadable: {type(exc).__name__}") from exc
        if not ok:
            raise GovernanceLedgerError("ledger hash-chain integrity check failed")

    def open_terminal_id(self) -> str | None:
        """Entry id of the latest TERMINAL with no HUMAN_AUTHORISED_TRANSITION caused_by it, else None."""
        entries = self._entries()
        cleared = {e.caused_by for e in entries if e.entry_type == LedgerEntryType.HUMAN_AUTHORISED_TRANSITION}
        open_ids = [e.entry_id for e in entries if e.entry_type == LedgerEntryType.TERMINAL and e.entry_id not in cleared]
        return open_ids[-1] if open_ids else None

    def is_suspended(self) -> tuple[bool, str | None]:
        terminal_id = self.open_terminal_id()
        return (terminal_id is not None or self._suspended_locally), terminal_id

    def mark_suspended_locally(self) -> None:
        self._suspended_locally = True

    # ---- writing ---------------------------------------------------------------------------------------
    def _write(self, kind: str, fn: Any, **kwargs: Any) -> LedgerEntry:
        try:
            return fn(identity_key=IDENTITY_KEY, instance_id=self.instance.instance_id, **kwargs)
        except AutomationDeniedException:
            raise
        except Exception as exc:  # noqa: BLE001 - any write failure must stop the protected action
            raise GovernanceLedgerError(f"ledger write failed ({kind}): {type(exc).__name__}") from exc

    def monitor(self, *, decision_id: str, drift: bool, caused_by: str | None = None, **payload: Any) -> LedgerEntry:
        return self._write("MONITOR", self.ledger.write_monitor, decision_id=decision_id, drift_detected=drift,
                           caused_by=caused_by, extra_payload=payload)

    def entry(self, entry_type: LedgerEntryType, *, decision_id: str | None, caused_by: str | None,
              **payload: Any) -> LedgerEntry:
        return self._write(entry_type.value, lambda **kw: self.ledger.write(entry_type, payload=payload, **kw),
                           decision_id=decision_id, caused_by=caused_by)

    def verification(self, *, decision_id: str, result: VerificationResult, caused_by: str, **payload: Any) -> LedgerEntry:
        return self._write("VERIFICATION", self.ledger.write_verification, decision_id=decision_id, result=result,
                           caused_by=caused_by, extra_payload=payload)

    # ---- human-authorised re-enablement (not callable by the agent graph) -----------------------------
    def re_enable(self, *, terminal_entry_id: str, authority: GovernanceAuthority, authorised_by: str,
                  evidence: Evidence) -> Instance:
        """Clear one open TERMINAL. Writes SPECIFICATION_UPDATE (when the terminal followed an INSUFFICIENT
        verification), HUMAN_AUTHORISED_TRANSITION (caused_by the TERMINAL) and RE_ENABLEMENT.

        Raises ``ValueError`` for an empty approver or evidence, and ``GovernanceLedgerError`` if the terminal
        is unknown or already cleared. Nothing is written before every check has passed.
        """
        entries = self._entries()
        by_id = {e.entry_id: e for e in entries}
        terminal = by_id.get(terminal_entry_id)
        if terminal is None or terminal.entry_type != LedgerEntryType.TERMINAL:
            raise GovernanceLedgerError("no such terminal entry")
        if any(e.entry_type == LedgerEntryType.HUMAN_AUTHORISED_TRANSITION and e.caused_by == terminal_entry_id
               for e in entries):
            raise GovernanceLedgerError("terminal already cleared")
        new_instance, transition = request_re_enablement(  # validates approver + evidence before any write
            self.instance, authority, authorised_by=authorised_by, evidence=evidence)
        decision_id = terminal.decision_id
        verification = by_id.get(terminal.caused_by) if terminal.caused_by else None
        if verification is not None and verification.entry_type == LedgerEntryType.VERIFICATION \
                and verification.payload.get("result") == VerificationResult.INSUFFICIENT.value:
            self.entry(LedgerEntryType.SPECIFICATION_UPDATE, decision_id=decision_id, caused_by=verification.entry_id,
                       proposal=_short(evidence.specification_update_proposal), authorised_by=authorised_by)
        hat = self.entry(LedgerEntryType.HUMAN_AUTHORISED_TRANSITION, decision_id=decision_id,
                         caused_by=terminal_entry_id, authority=authority.name, authorised_by=authorised_by,
                         root_cause=_short(evidence.root_cause_analysis),
                         evidence_hash=transition.authorisation_evidence_hash)
        self.entry(LedgerEntryType.RE_ENABLEMENT, decision_id=decision_id, caused_by=hat.entry_id,
                   new_instance_id=new_instance.instance_id, generation=new_instance.generation)
        self.instance = new_instance
        self._suspended_locally = False
        return new_instance


def new_decision_id() -> str:
    return "dec-" + uuid.uuid4().hex[:12]
