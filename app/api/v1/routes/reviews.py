"""Human Review queue: READ-ONLY views over persisted workflow state.

An item is *pending* only when a stored recovery is ``awaiting_approval`` AND its approval request is still ``pending``
and unexpired. Nothing is derived from the simulator, the demo dataset or the ledger. Deciding is done through the
existing ``POST /approvals/{request_id}/decision`` (authenticated, per-action permission, single-use, recorded in the
ledger); this module adds no write route and no execution path. Clarification requests are not part of the Phase 7
workflow and are therefore not offered.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Response

from app.api.v1.errors import ApiError
from app.api.v1.security import Caller, get_services, require
from app.services.container import Services, recovery_unavailable_info
from app.services.status import recovery_phase

router = APIRouter(tags=["reviews"], prefix="/reviews")
ReviewState = Literal["pending", "expired", "approved", "rejected", "other"]


def _now(services: Services | None = None) -> datetime:
    clock = getattr(getattr(services, 'approvals', None), 'clock', None)  # the approval service's own clock decides expiry
    return clock() if callable(clock) else datetime.now(timezone.utc)


def _parse(value: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def review_state(recovery: dict[str, Any], approval: dict[str, Any] | None, now: datetime | None = None) -> str:
    if approval is None:
        return "other"
    status = approval["status"]
    if status == "pending":
        exp = _parse(approval["expires_at"])
        return "expired" if exp is not None and exp <= (now or _now()) else "pending"
    if status in ("approved", "consumed"):
        return "approved"
    if status == "rejected":
        return "rejected"
    if status == "expired":
        return "expired"
    return "other"


def _item(services: Services, rec: dict[str, Any], *, detail: bool, now: datetime) -> dict[str, Any] | None:
    approval = services.recoveries.approval_summary(rec["approval_request_id"]) if rec["approval_request_id"] else None
    incident = services.store.get_incident(rec["incident_id"])
    if approval is None or incident is None:
        return None
    state = review_state(rec, approval, now)
    out: dict[str, Any] = {
        "review_id": rec["recovery_id"], "recovery_id": rec["recovery_id"], "request_id": approval["request_id"],
        "incident_id": rec["incident_id"], "incident_title": incident["title"], "incident_severity": incident["severity"],
        "action_type": approval["action_type"], "target": approval["target"], "risk": approval["risk"],
        "governance_state": approval["governance_state"], "requested_by": rec["requested_by"],
        "created_at": approval["created_at"], "expires_at": approval["expires_at"], "review_state": state,
        "awaiting_decision": state == "pending", "recovery_status": rec["status"], "recovery_phase": recovery_phase(rec),
        "verified_outcome": rec["verified_outcome"], "decided_by": approval["decided_by"], "decision_reason": approval["decision_reason"],
        "approval_status": approval["status"],
    }
    if detail:
        inv = services.store.get_investigation(rec["investigation_id"]) if rec.get("investigation_id") else None
        report = (inv or {}).get("report") or {}
        root = report.get("likely_root_cause") or {}
        cited = set(approval["evidence_refs"] or [])
        evidence = report.get("evidence") if isinstance(report.get("evidence"), list) else []
        # The diagnostic results the proposal cites (facts only), taken from the stored, code-verified report.
        findings = [{"evidence_id": e.get("evidence_id"), "tool": e.get("tool"), "status": e.get("status"), "severity": e.get("severity"),
                     "error_code": e.get("error_code"), "summary": e.get("summary")}
                    for e in evidence if isinstance(e, dict) and e.get("evidence_id") in cited][:20]
        out.update({
            "parameters": approval["parameters"], "expected_effect": approval["expected_effect"],
            # The four kinds of statement are kept apart on purpose:
            "confirmed_evidence": {"evidence_refs": approval["evidence_refs"], "report_status": approval["report_status"],
                                   "findings": findings},
            "agent_hypothesis": {"status": root.get("status"), "statement": root.get("statement") or root.get("explanation"),
                                 "confidence": report.get("confidence"), "note": "A hypothesis from the investigation, not a confirmed cause."},
            "recommendation": {"action_type": approval["action_type"], "reason": approval["reason"], "risk": approval["risk"],
                               "expected_effect": approval["expected_effect"]},
            "human_decision": {"state": state, "decided_by": approval["decided_by"], "reason": approval["decision_reason"]},
            "investigation_id": rec.get("investigation_id"), "investigation_summary": (inv or {}).get("summary"),
            "governance_events": services.recoveries.governance_events(rec["ledger_entry_ids"], rec["approval_request_id"], rec["decision_id"]),
            "decision_endpoint": f"/api/v1/approvals/{approval['request_id']}/decision",
            "supported_decisions": ["approve", "reject"], "clarification_supported": False,
        })
    return out


def _all(services: Services) -> list[dict[str, Any]]:
    if services.recoveries is None:
        message, details = recovery_unavailable_info(services)
        raise ApiError(503, "recovery_unavailable", message, details=details)
    now = _now(services)
    items = [i for r in services.store.recent_recoveries(500) if (i := _item(services, r, detail=False, now=now)) is not None]
    return items


@router.get("")
async def list_reviews(response: Response, state: Literal["pending", "decided", "expired", "all"] = "pending",
                       severity: Literal["low", "medium", "high", "critical"] | None = None,
                       q: str | None = Query(None, min_length=1, max_length=100),
                       limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
                       _: Caller = Depends(require("approval:read")), services: Services = Depends(get_services)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    items = _all(services)
    want = {"pending": {"pending"}, "decided": {"approved", "rejected"}, "expired": {"expired"}}.get(state)
    if want:
        items = [i for i in items if i["review_state"] in want]
    if severity:
        items = [i for i in items if i["incident_severity"] == severity]
    if q:
        n = q.lower()
        items = [i for i in items if n in " ".join(str(i[k]) for k in ("incident_id", "incident_title", "action_type", "target", "request_id", "requested_by")).lower()]
    return {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset, "state": state}


@router.get("/summary")
async def reviews_summary(response: Response, _: Caller = Depends(require("approval:read")),
                          services: Services = Depends(get_services)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    items = _all(services)
    counts: dict[str, int] = {}
    for i in items:
        counts[i["review_state"]] = counts.get(i["review_state"], 0) + 1
    pending = [i for i in items if i["review_state"] == "pending"]
    return {"pending": len(pending), "by_state": counts, "oldest_pending": min((i["created_at"] for i in pending), default=None),
            "pending_by_severity": {s: sum(1 for i in pending if i["incident_severity"] == s) for s in ("low", "medium", "high", "critical")}}


@router.get("/{review_id}")
async def get_review(response: Response, review_id: str = Path(min_length=1, max_length=64),
                     _: Caller = Depends(require("approval:read")), services: Services = Depends(get_services)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    _all(services)  # raises 503 when unavailable
    rec = services.store.get_recovery(review_id)
    item = _item(services, rec, detail=True, now=_now(services)) if rec else None
    if item is None:
        raise ApiError(404, "review_not_found", "No such review item.")
    return item
