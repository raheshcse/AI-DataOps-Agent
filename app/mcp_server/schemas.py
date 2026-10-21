"""Wire-level shapes of the MCP tools: input types/bounds and the structured output models.

These only *describe* what ``DiagnosticResult.to_dict()`` / ``DiagnosticReport.to_dict()`` already
return (``tests/test_mcp_server.py`` fails if the two drift apart). Semantic validation of inputs
stays in the diagnostics themselves, so a bad value they reject still comes back as a structured
``DIAG-INVALID-INPUT`` result rather than a bare protocol error.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.schema import RUN_STATUSES
from app.diagnostics.tools.schema_contract import ALLOWED_TABLES

Status = Literal["ok", "issue_detected", "warning", "error"]
Severity = Literal["info", "low", "medium", "high", "critical", "unknown"]

# --------------------------------------------------------------------------- inputs
PipelineId = Annotated[str, Field(
    max_length=50, description="Pipeline identifier: 1-50 letters, digits or underscores, e.g. 'order_ingestion'.")]
RunId = Annotated[str, Field(
    min_length=1, max_length=100, description="A pipeline run id such as 'RUN-ING-20260111-020000'. "
                                              "Get ids from get_pipeline_status.")]
RunStatus = Literal[RUN_STATUSES]  # type: ignore[valid-type]
FeedTable = Literal[ALLOWED_TABLES]  # type: ignore[valid-type]
Limit = Annotated[int, Field(strict=True, ge=1, le=100, description="How many runs of history to return (1-100).")]
Window = Annotated[int, Field(strict=True, ge=1, le=100, description="How many recent runs per pipeline to analyse (1-100).")]
MaxEvents = Annotated[int, Field(strict=True, ge=1, le=1000, description="Maximum log events to return (1-1000).")]
BatchDate = Annotated[str, Field(
    pattern=r"^\d{4}-\d{2}-\d{2}$", description="ISO batch date, YYYY-MM-DD. Defaults to the current batch.")]


# -------------------------------------------------------------------------- outputs
class DiagnosticResultModel(BaseModel):
    """One diagnostic finding. Mirrors ``app.diagnostics.models.DiagnosticResult.to_dict()``."""

    model_config = ConfigDict(extra="forbid")

    diagnostic_name: str
    status: Status = Field(description="ok | issue_detected | warning | error. 'error' means the diagnostic "
                                       "could not run; it is not a finding about the data.")
    severity: Severity
    summary: str
    evidence: list[dict[str, Any]]
    affected_records: list[str]
    observed_value: Any = None
    expected_value: Any = None
    error_code: str | None = None
    recommended_next_step: str | None = None
    parameters: dict[str, Any]
    notes: list[str]
    evaluated_at: str | None = Field(default=None, description="Simulated-clock time, not the machine clock.")


class DiagnosticReportModel(BaseModel):
    """Roll-up of several results. Mirrors ``DiagnosticReport.to_dict()``."""

    model_config = ConfigDict(extra="forbid")

    overall_status: Status
    highest_severity: Severity
    counts: dict[str, int]
    evaluated_at: str | None = None
    results: list[DiagnosticResultModel]
