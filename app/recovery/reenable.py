"""Authenticated entry point for clearing a terminal suspension.

Only an identity holding ``governance:re_enable`` (role ``governance_authority``) may call it. It then uses the
existing VSL path (``GovernanceRecorder.re_enable`` -> ``request_re_enablement``), which writes
SPECIFICATION_UPDATE, HUMAN_AUTHORISED_TRANSITION (caused_by the TERMINAL) and RE_ENABLEMENT.

Nothing in the workflows calls this. A stale approval issued before the terminal is invalid afterwards
(see ``ApprovalService.check``), so re-enabling never revives old approvals.
The ``GovernanceAuthority`` below is a clearly labelled development stand-in: no real authority is configured.
"""

from __future__ import annotations

from vsl_core.governance import GovernanceAuthority
from vsl_core.identity import Evidence, Instance

from app.governance.ledger import GovernanceLedgerError, GovernanceRecorder
from app.recovery.approvals import ApprovalDenied
from app.recovery.auth import PERM_RE_ENABLE, Authenticator, is_permitted

DEV_AUTHORITY = GovernanceAuthority(name="dev governance authority (role governance_authority)",
                                    escalation_contact="not configured")


class ReEnablementService:
    def __init__(self, recorder: GovernanceRecorder, authenticator: Authenticator,
                 authority: GovernanceAuthority = DEV_AUTHORITY) -> None:
        self.recorder, self.auth, self.authority = recorder, authenticator, authority

    def re_enable(self, credential: object, terminal_entry_id: str, *, root_cause_analysis: str,
                  specification_update_proposal: str) -> Instance:
        identity = self.auth.authenticate(credential)
        if identity is None:
            raise ApprovalDenied("unauthenticated")
        if not is_permitted(identity.roles, PERM_RE_ENABLE):
            raise ApprovalDenied("not_permitted")
        try:
            evidence = Evidence(root_cause_analysis, specification_update_proposal)
        except ValueError as exc:
            raise ApprovalDenied("invalid_evidence") from exc
        try:
            return self.recorder.re_enable(terminal_entry_id=terminal_entry_id, authority=self.authority,
                                           authorised_by=identity.subject, evidence=evidence)
        except GovernanceLedgerError as exc:
            raise ApprovalDenied("ledger_error", str(exc)[:80]) from exc
