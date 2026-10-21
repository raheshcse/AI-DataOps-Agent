"""VerbaLedger Explorer API: READ-ONLY views of the governance ledger.

Nothing here can append to, modify, repair or truncate the ledger, clear a suspension, approve or execute a
recovery, change policy or re-enable an agent. There is deliberately no write route in this module (a test enforces
it). The file path is never accepted from a client and never returned. Access needs ``ledger:read``, a permission held
by the viewer/reader/approver roles only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Response
from starlette.concurrency import run_in_threadpool

from app.api.v1.errors import ApiError
from app.api.v1.security import Caller, get_services, require
from app.governance import ledger_view as lv
from app.services.container import Services

router = APIRouter(tags=["ledger"], prefix="/ledger")

Ident = Annotated[str | None, Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$")]
EntryType = Literal["MONITOR", "PRE_NODE", "VERIFICATION", "TERMINAL", "SPECIFICATION_UPDATE",
                    "HUMAN_AUTHORISED_TRANSITION", "RE_ENABLEMENT"]


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _reader(services: Services) -> lv.LedgerReader:
    if services.recorder is None:
        raise ApiError(503, "ledger_unavailable", "The governance ledger is not available in this configuration.")
    reader = lv.reader_for(services.recorder.ledger.store.path)
    if not reader.available():
        raise ApiError(503, "ledger_unavailable", "The governance ledger has not been created or cannot be read.")
    return reader


async def _snapshot(reader: lv.LedgerReader, refresh: bool) -> tuple[lv.Snapshot, bool]:
    try:
        return await run_in_threadpool(reader.snapshot, refresh=refresh)
    except FileNotFoundError:
        raise ApiError(503, "ledger_unavailable", "The governance ledger has not been created or cannot be read.")


def _epoch(value: datetime | None) -> float | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


@router.get("/summary")
async def ledger_summary(response: Response, refresh: bool = False, _: Caller = Depends(require("ledger:read")),
                         services: Services = Depends(get_services)) -> dict[str, Any]:
    _no_store(response)
    reader = _reader(services)
    snap, cached = await _snapshot(reader, refresh)
    suspended: bool | None
    try:  # recomputed on every call: authorisation-adjacent state is never cached
        suspended = bool(services.recorder.is_suspended()[0])
    except Exception:  # noqa: BLE001 - unknown is reported as unknown, not as "not suspended"
        suspended = None
    return {"view": snap.meta(cached=cached), "suspended": suspended, **lv.summary(snap)}


@router.get("/events")
async def list_events(
    response: Response, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=1_000_000),
    order: Literal["asc", "desc"] = "desc", from_time: datetime | None = None, to_time: datetime | None = None,
    entry_type: EntryType | None = None, decision_id: Ident = None, incident_id: Ident = None, request_id: Ident = None,
    outcome: Ident = None, action: Ident = None, rule_id: Ident = None, refresh: bool = False,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    _: Caller = Depends(require("ledger:read")), services: Services = Depends(get_services),
) -> dict[str, Any]:
    _no_store(response)
    snap, cached = await _snapshot(_reader(services), refresh)
    filters = lv.Filters(_epoch(from_time), _epoch(to_time), entry_type, decision_id, incident_id, request_id, outcome,
                         action, rule_id, q)
    items, total = lv.query(snap, filters, limit=limit, offset=offset, descending=order == "desc")
    return {"items": items, "total": total, "limit": limit, "offset": offset, "view": snap.meta(cached=cached)}


@router.get("/events/{entry_id}")
async def get_event(response: Response, entry_id: str = Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$"),
                    _: Caller = Depends(require("ledger:read")), services: Services = Depends(get_services)) -> dict[str, Any]:
    _no_store(response)
    snap, cached = await _snapshot(_reader(services), False)
    rec = snap.by_id.get(entry_id)
    if rec is None:
        raise ApiError(404, "ledger_event_not_found", "No such ledger event in the readable part of the ledger.",
                       details={"view_complete": snap.complete})
    return {"event": lv.detail(rec), "view": snap.meta(cached=cached)}


@router.get("/trace")
async def causal_trace(response: Response, entry_id: Ident = None, decision_id: Ident = None, request_id: Ident = None,
                       refresh: bool = False, _: Caller = Depends(require("ledger:read")),
                       services: Services = Depends(get_services)) -> dict[str, Any]:
    _no_store(response)
    given = [name for name, v in (("entry_id", entry_id), ("decision_id", decision_id), ("request_id", request_id)) if v]
    if len(given) != 1:
        raise ApiError(422, "validation_error", "Provide exactly one of entry_id, decision_id or request_id.")
    snap, cached = await _snapshot(_reader(services), refresh)
    result = lv.trace(snap, entry_id=entry_id, decision_id=decision_id, request_id=request_id)
    if result is None:
        raise ApiError(404, "ledger_trace_not_found", "No recorded events match that identifier.",
                       details={"view_complete": snap.complete})
    return {**result, "view": snap.meta(cached=cached)}


@router.get("/integrity")
async def verify_integrity(response: Response, _: Caller = Depends(require("ledger:read")),
                           services: Services = Depends(get_services)) -> dict[str, Any]:
    """Recomputed on every call, never cached. Read-only: it reports the first failing record and repairs nothing."""
    _no_store(response)
    return await run_in_threadpool(_reader(services).verify)


@router.get("/audit")
async def semantic_audit(response: Response,
                         max_monitor_gap_seconds: float | None = Query(None, gt=0, le=31_536_000),
                         _: Caller = Depends(require("ledger:read")),
                         services: Services = Depends(get_services)) -> dict[str, Any]:
    """Recomputed on every call, never cached. Distinct from the cryptographic integrity verification."""
    _no_store(response)
    return await run_in_threadpool(lv.semantic_audit, _reader(services), max_monitor_gap_seconds)
