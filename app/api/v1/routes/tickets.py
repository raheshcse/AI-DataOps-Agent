"""Ticket association for an incident. Three fixed operations: create/associate, read, sync. Nothing here can update a
ticket's content or reach arbitrary GitHub APIs, and a ticket's state never affects approval or governance."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.api.v1.errors import ApiError
from app.api.v1.mappers import ticket_out
from app.api.v1.routes.incidents import IncidentId, incident_or_404
from app.api.v1.schemas import TicketOut
from app.api.v1.security import Caller, get_services, require
from app.integrations import TicketError
from app.services.container import Services
from app.services.tickets import NoTicket, TicketBusy, TicketsUnavailable

router = APIRouter(tags=["tickets"])


class TicketDetailOut(TicketOut):
    recent_events: list[dict] = []


def _map(exc: TicketError, incident_id: str) -> ApiError:
    details = {"incident_id": incident_id, "incident_preserved": True, "retryable": exc.kind in ("rate_limited", "timeout", "unavailable", "service_error")}
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    status = 503 if exc.kind in ("rate_limited", "timeout", "unavailable", "service_error") else 502
    return ApiError(status, f"ticket_provider_{exc.kind}", "The ticket provider could not complete the request.",
                    headers=headers, details=details)


@router.post("/incidents/{incident_id}/ticket", response_model=TicketOut, status_code=201)
async def create_ticket(response: Response, incident_id: str = IncidentId, _: Caller = Depends(require("ticket:manage")),
                        services: Services = Depends(get_services)) -> TicketOut:
    """Create the incident's ticket, or return the existing one (200). Retrying after a failure is safe: the provider is
    searched for the incident marker before anything is created."""
    incident = incident_or_404(services, incident_id)
    try:
        row, created = await services.tickets.create_for_incident(incident, services.store.latest_completed_investigation(incident_id))
    except TicketsUnavailable:
        raise ApiError(503, "ticketing_unavailable", "Ticket integration is not configured.", details={"incident_preserved": True})
    except TicketBusy:
        raise ApiError(409, "ticket_operation_in_progress", "A ticket operation is already in progress for this incident.")
    except TicketError as exc:
        raise _map(exc, incident_id)
    if not created:
        response.status_code = 200
    return ticket_out(row)


@router.get("/incidents/{incident_id}/ticket", response_model=TicketDetailOut)
async def get_ticket(incident_id: str = IncidentId, _: Caller = Depends(require("ticket:read")),
                     services: Services = Depends(get_services)) -> TicketDetailOut:
    incident_or_404(services, incident_id)
    row = services.tickets.get(incident_id)
    if row is None:
        raise ApiError(404, "ticket_not_found", "This incident has no ticket.")
    return TicketDetailOut(**ticket_out(row).model_dump(), recent_events=services.store.list_ticket_events(incident_id, 20))


@router.post("/incidents/{incident_id}/ticket/sync", response_model=TicketOut)
async def sync_ticket(incident_id: str = IncidentId, _: Caller = Depends(require("ticket:manage")),
                      services: Services = Depends(get_services)) -> TicketOut:
    incident_or_404(services, incident_id)
    try:
        return ticket_out(await services.tickets.sync(incident_id))
    except TicketsUnavailable:
        raise ApiError(503, "ticketing_unavailable", "Ticket integration is not configured.", details={"incident_preserved": True})
    except NoTicket:
        raise ApiError(404, "ticket_not_found", "This incident has no synchronisable ticket.")
    except TicketError as exc:
        raise _map(exc, incident_id)
