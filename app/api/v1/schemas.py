"""Request and response models for /api/v1. Requests forbid unknown fields, so an attempt to smuggle an identity or
an "approved" flag in a body is rejected (422) rather than ignored silently."""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IncidentType = Literal["pipeline_failure", "data_quality", "stale_data", "duplicate_records", "incomplete_processing",
                       "schema_mismatch", "other"]
Severity = Literal["low", "medium", "high", "critical"]
EXTERNAL_REF = r"^[A-Za-z0-9_.:/#-]{1,100}$"

T = TypeVar("T")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IncidentCreate(Strict):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10, max_length=1700)
    incident_type: IncidentType = "other"
    severity: Severity = "medium"
    external_ref: str | None = Field(default=None, pattern=EXTERNAL_REF)


class DecisionIn(Strict):
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=300)


class RecoveryRequestIn(Strict):
    """Optional. Without a body the action is derived by code from the investigation's evidence. With one, only
    allowlisted action types are accepted and Phase 7 still validates the parameters strictly."""
    action_type: Literal["rerun_pipeline", "remove_injected_condition"] | None = None
    parameters: dict[str, str] | None = None

    @model_validator(mode="after")
    def _both_or_neither(self) -> "RecoveryRequestIn":
        if (self.action_type is None) != (self.parameters is None):
            raise ValueError("action_type and parameters must be given together")
        return self

    @field_validator("parameters")
    @classmethod
    def _small(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None and (len(value) > 5 or any(len(k) > 40 or len(v) > 100 for k, v in value.items())):
            raise ValueError("parameters are too large")
        return value


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] | None = None


class ErrorOut(BaseModel):
    error: ErrorBody


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class TicketOut(BaseModel):
    association_id: str
    incident_id: str
    provider: str
    repository: str
    status: str
    external_id: str | None = None
    number: int | None = None
    url: str | None = None
    created_at: str
    updated_at: str
    last_sync_at: str | None = None
    last_sync_result: str | None = None


class IncidentOut(BaseModel):
    incident_id: str
    external_ref: str | None
    title: str
    description: str
    incident_type: str
    severity: str
    status: str
    workflow_status: str
    created_by: str
    created_at: str
    updated_at: str
    ticket: TicketOut | None = None


class InvestigationErrorOut(BaseModel):
    kind: str
    message: str | None = None


class InvestigationOut(BaseModel):
    investigation_id: str
    incident_id: str
    status: str
    mode: str
    requested_by: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    report_status: str | None = None
    summary: str | None = None
    evidence_refs: list[str] = []
    error: InvestigationErrorOut | None = None


class InvestigationDetailOut(InvestigationOut):
    report: dict[str, Any] | None = None


class WorkflowStatusOut(BaseModel):
    incident_id: str
    workflow_status: str
    incident_status: str
    latest_investigation_id: str | None = None
    latest_recovery_id: str | None = None


class ApprovalOut(BaseModel):
    request_id: str
    incident_id: str
    action_type: str
    parameters: dict[str, Any]
    target: str
    evidence_refs: list[str]
    reason: str
    expected_effect: str
    risk: str
    governance_state: str
    report_status: str
    binding_digest: str
    created_at: str
    expires_at: str
    status: str
    decided_by: str | None = None
    decision_reason: str | None = None


class GovernanceEventOut(BaseModel):
    entry_id: str
    outcome: str | None = None
    type: str
    caused_by: str | None = None


class RecoveryOut(BaseModel):
    recovery_id: str
    incident_id: str
    investigation_id: str | None
    status: str
    phase: str
    execution_status: str
    verified_outcome: str
    action_type: str
    parameters: dict[str, Any]
    reasons: list[str]
    approval_request_id: str | None
    decision_id: str | None
    requested_by: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None
    approval: ApprovalOut | None = None
    governance_events: list[GovernanceEventOut] = []


class DecisionOut(BaseModel):
    approval: ApprovalOut
    recovery: RecoveryOut | None = None
