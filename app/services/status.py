"""Derived workflow status. Computed at read time from the stored rows; never persisted, so it cannot contradict them.

new -> investigation_pending/running/completed/failed -> awaiting_approval -> recovery_running ->
recovery_completed | recovery_uncertain | recovery_failed | recovery_not_executed
"""

from __future__ import annotations

from typing import Any

from app.persistence import OperationsStore

_NOT_EXECUTED = {"rejected", "expired", "cancelled", "approval_invalid", "approval_already_used", "action_rejected",
                 "denied_human_review", "refused_suspended", "terminal_suspended", "blocked"}


def recovery_phase(recovery: dict[str, Any]) -> str:
    status = recovery["status"]
    if status == "awaiting_approval":
        return "awaiting_approval"
    if status == "resuming":
        return "recovery_running"
    if status == "executed":
        return "recovery_completed"
    if status == "recovery_uncertain" or recovery.get("verified_outcome") == "unknown":
        return "recovery_uncertain"
    if status in _NOT_EXECUTED:
        return "recovery_not_executed"
    return "recovery_failed"


def workflow_status(store: OperationsStore, incident: dict[str, Any]) -> str:
    investigations = store.list_investigations(incident["incident_id"])
    recoveries = store.list_recoveries(incident["incident_id"])
    latest_inv = investigations[0] if investigations else None
    latest_rec = recoveries[0] if recoveries else None
    if latest_rec is not None and (latest_inv is None or latest_rec["created_at"] >= latest_inv["created_at"]
                                   or latest_rec["status"] in ("awaiting_approval", "resuming")):
        return recovery_phase(latest_rec)
    if latest_inv is None:
        return "new"
    return {"pending": "investigation_pending", "running": "investigation_running", "completed": "investigation_completed",
            "failed": "investigation_failed"}[latest_inv["status"]]
