"""Read-only API over the isolated synthetic demonstration dataset (100 incidents).

The data is generated in memory by ``app.demo`` on every call: it is never written to SQL Server, the operations
SQLite database or the governance ledger, so it cannot alter or pollute real records. Every record is labelled
``demo_synthetic``. A demo incident cannot be investigated, approved or recovered, and appears in no Human Review queue.
"""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Response

from app.api.v1.errors import ApiError
from app.api.v1.security import Caller, require
from app.demo import generate_demo_incidents

router = APIRouter(tags=["demo"], prefix="/demo")
BANNER = "SYNTHETIC DEMONSTRATION DATA - not real incidents, not stored in any real system."
Severity = Literal["low", "medium", "high", "critical"]
Status = Literal["open", "investigating", "awaiting_review", "resolved", "closed"]


@lru_cache
def _dataset() -> tuple[dict[str, Any], ...]:
    return tuple(generate_demo_incidents())


def _brief(i: dict[str, Any]) -> dict[str, Any]:
    keys = ("incident_id", "title", "category", "incident_type", "severity", "status", "affected_pipeline", "detected_at", "run_ref",
            "human_review_required", "source", "synthetic")
    return {k: i[k] for k in keys}


@router.get("/incidents")
async def list_demo_incidents(
    response: Response, category: Annotated[str | None, Query(pattern=r"^[a-z_]{3,60}$")] = None, severity: Severity | None = None,
    status: Status | None = None, pipeline: Annotated[str | None, Query(pattern=r"^[a-z_]{3,40}$")] = None,
    human_review_required: bool | None = None, q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=1000),
    _: Caller = Depends(require("incident:read"))) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    items = list(_dataset())
    items = [i for i in items if (category is None or i["category"] == category) and (severity is None or i["severity"] == severity)
             and (status is None or i["status"] == status) and (pipeline is None or i["affected_pipeline"] == pipeline)
             and (human_review_required is None or i["human_review_required"] == human_review_required)
             and (q is None or q.lower() in (i["incident_id"] + " " + i["title"] + " " + i["description"]).lower())]
    items.sort(key=lambda i: i["detected_at"], reverse=True)
    return {"items": [_brief(i) for i in items[offset:offset + limit]], "total": len(items), "limit": limit, "offset": offset,
            "source": "demo_synthetic", "synthetic": True, "banner": BANNER}


@router.get("/summary")
async def demo_summary(response: Response, _: Caller = Depends(require("incident:read"))) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    d = _dataset()
    return {"total": len(d), "by_severity": dict(Counter(i["severity"] for i in d)), "by_status": dict(Counter(i["status"] for i in d)),
            "by_category": dict(sorted(Counter(i["category"] for i in d).items())), "by_pipeline": dict(sorted(Counter(i["affected_pipeline"] for i in d).items())),
            "human_review_flagged": sum(1 for i in d if i["human_review_required"]), "source": "demo_synthetic", "synthetic": True, "banner": BANNER}


@router.get("/incidents/{incident_id}")
async def demo_incident(response: Response, incident_id: str = Path(pattern=r"^DEMO-\d{4}$"),
                        _: Caller = Depends(require("incident:read"))) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    found = next((i for i in _dataset() if i["incident_id"] == incident_id), None)
    if found is None:
        raise ApiError(404, "incident_not_found", "No such demo incident.")
    return {**found, "banner": BANNER, "classification": "demo_synthetic",
            "statements": {"confirmed_evidence": found["evidence"], "agent_hypothesis": found["probable_cause"],
                           "recommendation": found["recommended_next_step"], "human_decision": None},
            "note": "Descriptive only: this record is not in the Human Review queue and cannot be approved or executed."}
