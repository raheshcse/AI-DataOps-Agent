"""Liveness vs readiness. Neither exposes secrets, connection strings, paths or exception text."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.api.v1.errors import ApiError

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness only: the process is up. Touches no dependency."""
    return {"status": "ok", "version": __version__}


@router.get("/ready")
def ready(request: Request) -> JSONResponse:
    """Readiness: the operational store works and authentication is not misconfigured. Optional capabilities are
    reported as available/unavailable; they do not make the API unready."""
    try:
        services = request.app.state.provider.get()
    except ApiError:
        return JSONResponse(status_code=503, content={"status": "not_ready", "checks": {"service": "unavailable"}})
    checks: dict[str, str] = {}
    try:
        services.store.ping()
        checks["operations_database"] = "ok"
    except Exception:  # noqa: BLE001
        checks["operations_database"] = "unavailable"
    checks["authentication"] = services.auth_status
    checks["approvals"] = "configured" if services.approvals is not None else "unavailable"
    checks["recovery"] = "available" if services.recoveries is not None else "unavailable"
    checks["ticketing"] = services.ticket_status
    checks["investigation_mode"] = services.investigations.mode
    if services.recorder is not None:
        try:
            checks["governance"] = "suspended" if services.recorder.is_suspended()[0] else "active"
        except Exception:  # noqa: BLE001
            checks["governance"] = "ledger_unreadable"
    ok = checks["operations_database"] == "ok" and services.auth_status != "misconfigured"
    return JSONResponse(status_code=200 if ok else 503,
                        content={"status": "ready" if ok else "not_ready", "environment": services.app_env, "checks": checks})
