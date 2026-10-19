from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Response

from app.agent import InvalidIncident
from app.api.v1.errors import ApiError
from app.api.v1.mappers import incident_out, investigation_detail_out, investigation_out
from app.api.v1.schemas import (
    IncidentCreate,
    IncidentOut,
    InvestigationDetailOut,
    InvestigationOut,
    Page,
    WorkflowStatusOut,
)
from app.api.v1.security import Caller, get_services, require
from app.persistence import DuplicateExternalRef, InvestigationActive
from app.services.container import Services
from app.services.investigations import validate_incident_text
from app.services.status import workflow_status

router = APIRouter(tags=["incidents"])
IncidentId = Path(min_length=1, max_length=64)


def incident_or_404(services: Services, incident_id: str) -> dict:
    row = services.store.get_incident(incident_id)
    if row is None:
        raise ApiError(404, "incident_not_found", "No such incident.")
    return row


@router.post("/incidents", response_model=IncidentOut, status_code=201)
async def create_incident(body: IncidentCreate, response: Response, caller: Caller = Depends(require("incident:create")),
                          services: Services = Depends(get_services),
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", pattern=r"^[A-Za-z0-9_.:-]{8,64}$")
                          ) -> IncidentOut:
    """Persist an incident. A repeated ``Idempotency-Key`` returns the original incident (200) instead of a duplicate."""
    try:
        validate_incident_text(body.title, body.description, services.investigations.max_incident_chars)
    except InvalidIncident:
        raise ApiError(422, "invalid_incident", "The incident text is not acceptable.")
    try:
        row, created = services.store.create_incident(
            title=body.title, description=body.description, incident_type=body.incident_type, severity=body.severity,
            external_ref=body.external_ref, idempotency_key=idempotency_key, created_by=caller.subject)
    except DuplicateExternalRef as exc:
        raise ApiError(409, "external_ref_exists", "An incident with this external reference already exists.",
                       details={"incident_id": exc.incident_id})
    if not created:
        response.status_code = 200
    return incident_out(services, row)


@router.get("/incidents", response_model=Page[IncidentOut])
async def list_incidents(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                         status: Literal["new", "investigating", "investigated", "investigation_failed"] | None = None,
                         severity: Literal["low", "medium", "high", "critical"] | None = None,
                         _: Caller = Depends(require("incident:read")), services: Services = Depends(get_services)) -> Page[IncidentOut]:
    rows, total = services.store.list_incidents(status=status, severity=severity, limit=limit, offset=offset)
    return Page(items=[incident_out(services, r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/incidents/{incident_id}", response_model=IncidentOut)
async def get_incident(incident_id: str = IncidentId, _: Caller = Depends(require("incident:read")),
                       services: Services = Depends(get_services)) -> IncidentOut:
    return incident_out(services, incident_or_404(services, incident_id))


@router.get("/incidents/{incident_id}/status", response_model=WorkflowStatusOut)
async def get_status(incident_id: str = IncidentId, _: Caller = Depends(require("incident:read")),
                     services: Services = Depends(get_services)) -> WorkflowStatusOut:
    row = incident_or_404(services, incident_id)
    invs, recs = services.store.list_investigations(incident_id), services.store.list_recoveries(incident_id)
    return WorkflowStatusOut(incident_id=incident_id, workflow_status=workflow_status(services.store, row), incident_status=row["status"],
                             latest_investigation_id=invs[0]["investigation_id"] if invs else None,
                             latest_recovery_id=recs[0]["recovery_id"] if recs else None)


@router.post("/incidents/{incident_id}/investigations", response_model=InvestigationOut, status_code=202)
async def start_investigation(background: BackgroundTasks, incident_id: str = IncidentId,
                              caller: Caller = Depends(require("investigation:run")),
                              services: Services = Depends(get_services)) -> InvestigationOut:
    """Accept an investigation (202) and run the existing agent in the background. The record says 'pending' until the
    agent actually finishes; poll ``GET /investigations/{id}``."""
    incident = incident_or_404(services, incident_id)
    try:
        row = services.investigations.start(incident, caller.subject)
    except InvestigationActive as exc:
        raise ApiError(409, "investigation_in_progress", "An investigation is already running for this incident.",
                       details={"investigation_id": exc.investigation_id})
    background.add_task(services.investigations.execute, row["investigation_id"])
    return investigation_out(row)


@router.get("/incidents/{incident_id}/investigations", response_model=list[InvestigationOut])
async def list_investigations(incident_id: str = IncidentId, _: Caller = Depends(require("investigation:read")),
                              services: Services = Depends(get_services)) -> list[InvestigationOut]:
    incident_or_404(services, incident_id)
    return [investigation_out(r) for r in services.store.list_investigations(incident_id)]


@router.get("/incidents/{incident_id}/report", response_model=InvestigationDetailOut)
async def latest_report(incident_id: str = IncidentId, _: Caller = Depends(require("investigation:read")),
                        services: Services = Depends(get_services)) -> InvestigationDetailOut:
    incident_or_404(services, incident_id)
    row = services.store.latest_completed_investigation(incident_id)
    if row is None:
        raise ApiError(404, "report_not_found", "No completed investigation exists for this incident.")
    return investigation_detail_out(row)


@router.get("/investigations/{investigation_id}", response_model=InvestigationDetailOut)
async def get_investigation(investigation_id: str = Path(min_length=1, max_length=64),
                            _: Caller = Depends(require("investigation:read")),
                            services: Services = Depends(get_services)) -> InvestigationDetailOut:
    row = services.store.get_investigation(investigation_id)
    if row is None:
        raise ApiError(404, "investigation_not_found", "No such investigation.")
    return investigation_detail_out(row)
