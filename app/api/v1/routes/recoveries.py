"""Controlled recovery over HTTP. A request here can only reach 'awaiting_approval' (or a recorded refusal): execution
happens solely inside the Phase 7 workflow after an authenticated human approval and the VSL gates."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Response

from app.api.v1.errors import ApiError
from app.api.v1.mappers import recovery_out
from app.api.v1.routes.incidents import IncidentId, incident_or_404
from app.api.v1.schemas import Page, RecoveryOut, RecoveryRequestIn
from app.api.v1.security import Caller, get_services, require
from app.persistence import RecoveryActive
from app.services.container import Services, recovery_unavailable_info
from app.services.recoveries import NoRecoverySuggested, RecoveryService

router = APIRouter(tags=["recoveries"])


def _recoveries(services: Services) -> RecoveryService:
    if services.recoveries is None:
        message, details = recovery_unavailable_info(services)
        raise ApiError(503, "recovery_unavailable", message, details=details)
    return services.recoveries


@router.post("/incidents/{incident_id}/recoveries", response_model=RecoveryOut, status_code=202)
async def request_recovery(response: Response, body: RecoveryRequestIn | None = None, incident_id: str = IncidentId,
                           caller: Caller = Depends(require("recovery:request")),
                           services: Services = Depends(get_services)) -> RecoveryOut:
    """Create a recovery request. Nothing is executed. 202 means 'awaiting_approval'; any other recorded outcome (for
    example a suspension or a rejected action) is answered with 409 and the stored recovery, so it stays inspectable."""
    recoveries = _recoveries(services)
    incident = incident_or_404(services, incident_id)
    investigation = services.store.latest_completed_investigation(incident_id)
    if investigation is None:
        raise ApiError(409, "no_completed_investigation", "Run an investigation to completion before requesting a recovery.")
    explicit = (body.action_type, dict(body.parameters or {})) if body and body.action_type else None
    try:
        row = await recoveries.request(incident=incident, investigation=investigation, requested_by=caller.subject, explicit=explicit)
    except NoRecoverySuggested:
        raise ApiError(422, "no_recovery_suggested", "The investigation evidence maps to no allowlisted recovery action.")
    except RecoveryActive as exc:
        raise ApiError(409, "recovery_in_progress", "A recovery is already in progress for this incident.",
                       details={"recovery_id": exc.recovery_id})
    if row["status"] != "awaiting_approval":
        response.status_code = 409
    return recovery_out(services, row)


@router.get("/recoveries", response_model=Page[RecoveryOut])
async def list_all_recoveries(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=10000),
                              status: str | None = Query(None, pattern=r"^[a-z_]{3,40}$"),
                              _: Caller = Depends(require("recovery:read")), services: Services = Depends(get_services)) -> Page[RecoveryOut]:
    """Recent recoveries across incidents (summary rows; fetch one for governance detail)."""
    rows = [r for r in services.store.recent_recoveries(500) if status is None or r["status"] == status]
    return Page(items=[recovery_out(services, r, detail=False) for r in rows[offset:offset + limit]], total=len(rows), limit=limit, offset=offset)


@router.get("/incidents/{incident_id}/recoveries", response_model=list[RecoveryOut])
async def list_recoveries(incident_id: str = IncidentId, _: Caller = Depends(require("recovery:read")),
                          services: Services = Depends(get_services)) -> list[RecoveryOut]:
    incident_or_404(services, incident_id)
    return [recovery_out(services, r, detail=False) for r in services.store.list_recoveries(incident_id)]


@router.get("/recoveries/{recovery_id}", response_model=RecoveryOut)
async def get_recovery(recovery_id: str = Path(min_length=1, max_length=64), _: Caller = Depends(require("recovery:read")),
                       services: Services = Depends(get_services)) -> RecoveryOut:
    row = services.store.get_recovery(recovery_id)
    if row is None:
        raise ApiError(404, "recovery_not_found", "No such recovery.")
    if services.recoveries is not None:
        row = services.recoveries.reconcile(row)
    return recovery_out(services, row)


@router.post("/recoveries/{recovery_id}/resume", response_model=RecoveryOut)
async def resume_recovery(recovery_id: str = Path(min_length=1, max_length=64), _: Caller = Depends(require("recovery:resume")),
                          services: Services = Depends(get_services)) -> RecoveryOut:
    """Re-evaluate after a decision made elsewhere (for example the CLI). Idempotent and safe to retry: a spent
    approval can never run again, and only one caller can win the resume claim."""
    recoveries = _recoveries(services)
    row = await recoveries.resume(recovery_id)
    if row is None:
        raise ApiError(404, "recovery_not_found", "No such recovery.")
    return recovery_out(services, row)
