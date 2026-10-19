"""Approval status and decisions. Decisions are RELAYED to the Phase 7 ApprovalService with the caller's own
credential; the service authenticates it and checks the per-action permission. A body can only say approve/reject:
it cannot name an approver, and the only authority is the verified token."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from app.api.v1.errors import ApiError
from app.api.v1.mappers import approval_out, recovery_out
from app.api.v1.schemas import ApprovalOut, DecisionIn, DecisionOut
from app.api.v1.security import Caller, get_services, require
from app.recovery import ApprovalDenied
from app.services.container import Services
from app.services.recoveries import denial_status

router = APIRouter(tags=["approvals"])
RequestId = Path(min_length=1, max_length=64)


def _approvals(services: Services):
    if services.approvals is None:
        raise ApiError(503, "approvals_unavailable", "Approvals are not available in this configuration.")
    return services.approvals


@router.get("/approvals/{request_id}", response_model=ApprovalOut)
async def get_approval(request_id: str = RequestId, _: Caller = Depends(require("approval:read")),
                       services: Services = Depends(get_services)) -> ApprovalOut:
    record = _approvals(services).store.get(request_id)
    if record is None:
        raise ApiError(404, "approval_not_found", "No such approval request.")
    return approval_out(record.summary())


@router.post("/approvals/{request_id}/decision", response_model=DecisionOut)
async def decide(body: DecisionIn, request_id: str = RequestId, caller: Caller = Depends(require("approval:decide")),
                 services: Services = Depends(get_services)) -> DecisionOut:
    approvals = _approvals(services)
    try:
        if services.recoveries is not None:
            summary, recovery = await services.recoveries.decide(request_id=request_id, decision=body.decision,
                                                                 credential=caller.credential, reason=body.reason)
        else:  # approvals exist but recovery is off: record the decision only; nothing can execute
            record = approvals.approve(request_id, caller.credential, body.reason) if body.decision == "approve" \
                else approvals.reject(request_id, caller.credential, body.reason)
            summary, recovery = record.summary(), None
    except ApprovalDenied as exc:
        raise ApiError(denial_status(exc.code), "approval_denied", "The approval decision was refused.", details={"reason": exc.code})
    return DecisionOut(approval=approval_out(summary), recovery=recovery_out(services, recovery) if recovery else None)
