"""Stored rows -> response models. Only whitelisted fields are copied, so a new column can never leak by accident."""

from __future__ import annotations

from typing import Any

from app.api.v1.schemas import (
    ApprovalOut,
    GovernanceEventOut,
    IncidentOut,
    InvestigationDetailOut,
    InvestigationErrorOut,
    InvestigationOut,
    RecoveryOut,
    TicketOut,
)
from app.services.container import Services
from app.services.status import recovery_phase, workflow_status


def ticket_out(row: dict[str, Any]) -> TicketOut:
    return TicketOut(association_id=row["association_id"], incident_id=row["incident_id"], provider=row["provider"],
                     repository=row["repository"], status=row["status"], external_id=row["external_id"],
                     number=row["external_number"], url=row["url"], created_at=row["created_at"], updated_at=row["updated_at"],
                     last_sync_at=row["last_sync_at"], last_sync_result=row["last_sync_result"])


def incident_out(services: Services, row: dict[str, Any]) -> IncidentOut:
    ticket = services.store.get_ticket(row["incident_id"])
    return IncidentOut(incident_id=row["incident_id"], external_ref=row["external_ref"], title=row["title"],
                       description=row["description"], incident_type=row["incident_type"], severity=row["severity"],
                       status=row["status"], workflow_status=workflow_status(services.store, row), created_by=row["created_by"],
                       created_at=row["created_at"], updated_at=row["updated_at"],
                       ticket=ticket_out(ticket) if ticket else None)


def _investigation_fields(row: dict[str, Any]) -> dict[str, Any]:
    error = InvestigationErrorOut(kind=row["error_kind"], message=row["error_message"]) if row["error_kind"] else None
    return dict(investigation_id=row["investigation_id"], incident_id=row["incident_id"], status=row["status"], mode=row["mode"],
                requested_by=row["requested_by"], created_at=row["created_at"], started_at=row["started_at"],
                completed_at=row["completed_at"], report_status=row["report_status"], summary=row["summary"],
                evidence_refs=row["evidence_refs"], error=error)


def investigation_out(row: dict[str, Any]) -> InvestigationOut:
    return InvestigationOut(**_investigation_fields(row))


def investigation_detail_out(row: dict[str, Any]) -> InvestigationDetailOut:
    return InvestigationDetailOut(**_investigation_fields(row), report=row["report"])


def approval_out(summary: dict[str, Any]) -> ApprovalOut:
    return ApprovalOut(**{k: summary[k] for k in ApprovalOut.model_fields})


def recovery_out(services: Services, row: dict[str, Any], *, detail: bool = True) -> RecoveryOut:
    approval, events = None, []
    if detail and services.recoveries is not None:
        if row["approval_request_id"]:
            summary = services.recoveries.approval_summary(row["approval_request_id"])
            approval = approval_out(summary) if summary else None
        events = [GovernanceEventOut(**e) for e in services.recoveries.governance_events(row["ledger_entry_ids"], row["approval_request_id"], row["decision_id"])]
    return RecoveryOut(
        recovery_id=row["recovery_id"], incident_id=row["incident_id"], investigation_id=row["investigation_id"],
        status=row["status"], phase=recovery_phase(row), execution_status=row["execution_status"],
        verified_outcome=row["verified_outcome"], action_type=row["action_type"], parameters=row["parameters"],
        reasons=row["reasons"], approval_request_id=row["approval_request_id"], decision_id=row["decision_id"],
        requested_by=row["requested_by"], created_at=row["created_at"], updated_at=row["updated_at"],
        started_at=row["started_at"], completed_at=row["completed_at"], approval=approval, governance_events=events)
