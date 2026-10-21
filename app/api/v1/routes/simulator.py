"""SQL Server simulator records, made discoverable through the authenticated /api/v1 API.

Source of truth (see README): the simulator database (``dataops_sim``) owns pipeline runs, validation results and the
simulator's own incident records. The operations store (SQLite) owns workflow state: investigations, recoveries,
approvals and tickets. Nothing here writes to the simulator database (a test enforces it). The only write is the
explicit, idempotent *import*, which creates one workflow incident in the operations store with the stable external
reference ``sim:<INC-id>``. An import records no investigation, approval or governance event: the imported incident is
``new`` and says so.
"""

from __future__ import annotations

from typing import Annotated, Any, Callable, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1.errors import ApiError
from app.api.v1.mappers import incident_out
from app.api.v1.security import Caller, get_services, require
from app.persistence import DuplicateExternalRef
from app.services.container import Services

router = APIRouter(tags=["simulator"], prefix="/simulator")
SIM_REF_PREFIX = "sim:"
SimIncidentId = Annotated[str, Path(pattern=r"^INC-\d{4,8}$")]
_TYPE_MAP = {"pipeline_failure": "pipeline_failure", "data_quality": "data_quality", "stale_data": "stale_data",
             "duplicate_records": "duplicate_records", "incomplete_processing": "incomplete_processing",
             "schema_mismatch": "schema_mismatch",
             # Phase 12 synthetic categories map to the closest existing workflow incident type
             "ingestion_failure": "pipeline_failure", "source_timeout": "pipeline_failure", "connectivity": "pipeline_failure",
             "transformation_failure": "pipeline_failure", "missing_data": "incomplete_processing",
             "row_count_variance": "incomplete_processing", "reconciliation_mismatch": "data_quality"}


def _repo(request: Request):
    factory: Callable[[], Any] | None = getattr(request.app.state, "sim_repository_factory", None)
    if factory is None:
        raise ApiError(503, "simulator_unavailable", "The simulator database is not configured for this API.")
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001 - connection details must never reach a client; only the type is logged
        raise ApiError(503, "simulator_unavailable", "The simulator database is unavailable.") from exc


async def _call(fn: Callable[[], Any]) -> Any:
    try:
        return await run_in_threadpool(fn)
    except (SQLAlchemyError, OSError) as exc:
        raise ApiError(503, "simulator_unavailable", "The simulator database is unavailable.") from exc


def _view(row: dict[str, Any], imported: dict[str, str]) -> dict[str, Any]:
    iid = row["incident_id"]
    return {
        "incident_id": iid, "source": "simulator", "synthetic": True,
        "category": row["category"], "severity": row["severity"], "status": row["status"], "title": row["title"],
        "description": row["description"], "created_at": row["created_at"], "pipeline_id": row.get("pipeline_id"),
        "run_id": row.get("run_id"), "scenario_id": row.get("scenario_id"), "evidence_ref": row.get("evidence_ref"),
        "workflow_incident_id": imported.get(SIM_REF_PREFIX + iid), "imported": (SIM_REF_PREFIX + iid) in imported,
    }


@router.get("/incidents")
async def list_simulator_incidents(
    request: Request, response: Response, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100000),
    status: Literal["open", "investigating", "resolved"] | None = None,
    severity: Literal["low", "medium", "high", "critical"] | None = None,
    q: str | None = Query(None, min_length=1, max_length=100), imported: bool | None = None,
    _: Caller = Depends(require("incident:read")), services: Services = Depends(get_services)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    repo = _repo(request)
    rows = await _call(lambda: repo.list_incidents(1000))
    refs = services.store.external_refs(SIM_REF_PREFIX)
    needle = q.lower() if q else None
    items = [_view(r, refs) for r in rows]
    items = [i for i in items if (status is None or i["status"] == status) and (severity is None or i["severity"] == severity)
             and (imported is None or i["imported"] == imported)
             and (needle is None or needle in " ".join(str(i[k]) for k in ("incident_id", "title", "description", "pipeline_id", "run_id", "category")).lower())]
    return {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset,
            "source": "simulator", "synthetic": True}


@router.get("/incidents/{incident_id}")
async def simulator_incident(incident_id: SimIncidentId, request: Request, response: Response,
                             _: Caller = Depends(require("incident:read")), services: Services = Depends(get_services)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    repo = _repo(request)
    rows = await _call(lambda: repo.list_incidents(1000))
    row = next((r for r in rows if r["incident_id"] == incident_id), None)
    if row is None:
        raise ApiError(404, "incident_not_found", "No such simulator incident.")
    refs = services.store.external_refs(SIM_REF_PREFIX)
    out = _view(row, refs)
    run = validations = None
    if row.get("run_id"):
        run = await _call(lambda: repo.get_run(row["run_id"]))
        validations = await _call(lambda: repo.get_validations(row["run_id"]))
    out["run"] = run
    out["validations"] = validations or []
    out["evidence_note"] = "Run and validation records are confirmed simulator data; they are not an investigation result."
    return out


@router.post("/incidents/{incident_id}/import")
async def import_simulator_incident(incident_id: SimIncidentId, request: Request, response: Response,
                                    caller: Caller = Depends(require("incident:create")),
                                    services: Services = Depends(get_services)) -> dict[str, Any]:
    """Idempotent: importing the same simulator incident again returns the existing workflow incident (200)."""
    repo = _repo(request)
    rows = await _call(lambda: repo.list_incidents(1000))
    row = next((r for r in rows if r["incident_id"] == incident_id), None)
    if row is None:
        raise ApiError(404, "incident_not_found", "No such simulator incident.")
    ref = SIM_REF_PREFIX + incident_id
    existing = services.store.get_incident_by_external_ref(ref)
    if existing is not None:
        return {"created": False, "incident": incident_out(services, existing).model_dump(mode="json")}
    description = f"{row['description']}\n\n[Imported from simulator incident {incident_id}; run {row.get('run_id') or 'n/a'}; pipeline {row.get('pipeline_id') or 'n/a'}.]"
    try:
        created, was_created = services.store.create_incident(
            title=row["title"][:200], description=description[:4000], incident_type=_TYPE_MAP.get(row["category"], "other"),
            severity=row["severity"], external_ref=ref, idempotency_key=None, created_by=caller.subject)
    except DuplicateExternalRef as exc:  # lost a race with a concurrent import: return the winner
        existing = services.store.get_incident(exc.incident_id)
        return {"created": False, "incident": incident_out(services, existing).model_dump(mode="json")}
    response.status_code = 201 if was_created else 200
    return {"created": was_created, "incident": incident_out(services, created).model_dump(mode="json")}


# ---- pipelines and runs (authenticated, read-only mirror of the unauthenticated root /sim routes)
@router.get("/pipelines")
async def simulator_pipelines(request: Request, response: Response, _: Caller = Depends(require("incident:read"))) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    repo = _repo(request)
    rows = await _call(repo.list_pipelines)
    return {"items": rows, "total": len(rows), "source": "simulator", "synthetic": True}


@router.get("/runs")
async def simulator_runs(request: Request, response: Response, pipeline_id: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_]{0,49}$")] = None,
                         status: Annotated[str | None, Query(pattern=r"^[a-z_]{3,20}$")] = None, limit: int = Query(25, ge=1, le=100),
                         _: Caller = Depends(require("incident:read"))) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    repo = _repo(request)
    rows = await _call(lambda: repo.list_runs(pipeline_id, limit, status))
    return {"items": rows, "total": len(rows), "limit": limit, "source": "simulator", "synthetic": True}
