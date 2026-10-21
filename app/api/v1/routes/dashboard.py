"""One read-only summary for the Operations Dashboard overview. Every number is computed from a store at request time;
real counts, simulator counts and synthetic demo counts are reported separately and never added together."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from app.api.v1.routes.demo import _dataset
from app.api.v1.routes.reviews import _all as _all_reviews
from app.api.v1.routes.simulator import SIM_REF_PREFIX
from app.api.v1.security import Caller, get_services, require
from app.services.container import Services, recovery_unavailable_info

router = APIRouter(tags=["dashboard"], prefix="/dashboard")


@router.get("/summary")
async def dashboard_summary(request: Request, response: Response, _: Caller = Depends(require("incident:read")),
                            services: Services = Depends(get_services)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    counts = services.store.counts()
    out: dict[str, Any] = {"workflow": counts, "workflow_incident_total": sum(counts.get("incidents_by_status", {}).values()),
                           "recovery_available": services.recoveries is not None, "auth_status": services.auth_status}
    if services.recoveries is None:  # say WHY (an exception type name and a database name only - never a secret)
        out["recovery_unavailable"] = recovery_unavailable_info(services)[1]
    try:
        reviews = _all_reviews(services)
        out["human_review"] = {"pending": sum(1 for i in reviews if i["review_state"] == "pending"), "available": True}
    except Exception:  # noqa: BLE001 - ApiError 503 when recovery is off: reported as unavailable, not as zero
        out["human_review"] = {"pending": None, "available": False}
    sim: dict[str, Any] = {"available": False}
    factory = getattr(request.app.state, "sim_repository_factory", None)
    if factory is not None:
        try:
            repo = factory()
            rows = await run_in_threadpool(lambda: repo.list_incidents(1000))
            runs = await run_in_threadpool(lambda: repo.list_runs(None, 100))
            refs = services.store.external_refs(SIM_REF_PREFIX)
            by_status: dict[str, int] = {}
            for r in rows:
                by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            by_run: dict[str, int] = {}
            for r in runs:
                by_run[r["status"]] = by_run.get(r["status"], 0) + 1
            sim = {"available": True, "incident_total": len(rows), "incidents_by_status": by_status,
                   "imported": sum(1 for r in rows if SIM_REF_PREFIX + r["incident_id"] in refs), "recent_runs_by_status": by_run}
        except Exception:  # noqa: BLE001 - connection details never reach the client
            sim = {"available": False}
    out["simulator"] = sim
    out["demo"] = {"incident_total": len(_dataset()), "synthetic": True}
    return out
