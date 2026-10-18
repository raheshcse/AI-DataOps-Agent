"""The common, machine-readable result returned by every diagnostic tool.

Design rules (so a future MCP tool / agent can trust the output):

* ``evidence`` holds observed *facts* (row identifiers, counts, log lines, values read
  from the database). A suggestion or hypothesis never goes there; it belongs in
  ``recommended_next_step`` and is worded as a suggestion, not as a confirmed cause.
* ``status`` separates a passing check, a detected issue, a warning and a diagnostic that
  could not run at all (``error``).
* Everything is converted to plain JSON types on construction, so results are
  deterministic and identical whether read from Python or serialised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

STATUS_OK = "ok"
STATUS_ISSUE = "issue_detected"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"
STATUSES = (STATUS_OK, STATUS_ISSUE, STATUS_WARNING, STATUS_ERROR)

SEVERITIES = ("info", "low", "medium", "high", "critical", "unknown")
_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

# Codes for problems with the diagnostic itself (pipeline/rule codes such as E1001 or
# E2002 are used, unchanged, when a finding corresponds to one).
DIAG_INVALID_INPUT = "DIAG-INVALID-INPUT"
DIAG_UNKNOWN_PIPELINE = "DIAG-UNKNOWN-PIPELINE"
DIAG_UNKNOWN_RUN = "DIAG-UNKNOWN-RUN"
DIAG_NOT_SEEDED = "DIAG-NOT-SEEDED"
DIAG_DB_ERROR = "DIAG-DB-ERROR"
DIAG_INTERNAL = "DIAG-INTERNAL"
DIAG_LOG_MISSING = "DIAG-LOG-MISSING"
DIAG_LOG_MALFORMED = "DIAG-LOG-MALFORMED"
DIAG_LOG_TOO_LARGE = "DIAG-LOG-TOO-LARGE"
DIAG_INVALID_TIMESTAMP = "DIAG-INVALID-TIMESTAMP"

MAX_AFFECTED = 50  # bound list sizes so results stay small and predictable


class InvalidInput(ValueError):
    """A tool argument is invalid. The message is safe to show to the caller."""


def jsonable(value: Any) -> Any:
    """Recursively convert to plain JSON types (deterministic ordering for sets)."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(v) for v in value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def bounded(items: list[str], limit: int = MAX_AFFECTED) -> tuple[list[str], list[str]]:
    """``(items[:limit], notes)``; the note says how many were left out."""
    if len(items) <= limit:
        return items, []
    return items[:limit], [f"affected_records shows {limit} of {len(items)}; the rest are omitted."]


def worst_severity(*levels: str) -> str:
    return max(levels, key=lambda lv: _SEVERITY_RANK[lv]) if levels else "info"


@dataclass
class DiagnosticResult:
    diagnostic_name: str
    status: str
    severity: str
    summary: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    affected_records: list[str] = field(default_factory=list)
    observed_value: Any = None
    expected_value: Any = None
    error_code: str | None = None
    recommended_next_step: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    evaluated_at: str | None = None  # simulated-clock time, never the machine clock

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"invalid diagnostic status: {self.status!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"invalid diagnostic severity: {self.severity!r}")
        self.evidence = jsonable(self.evidence)
        self.observed_value = jsonable(self.observed_value)
        self.expected_value = jsonable(self.expected_value)
        self.parameters = jsonable(self.parameters)
        self.evaluated_at = jsonable(self.evaluated_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnostic_name": self.diagnostic_name, "status": self.status, "severity": self.severity,
            "summary": self.summary, "evidence": self.evidence, "affected_records": self.affected_records,
            "observed_value": self.observed_value, "expected_value": self.expected_value,
            "error_code": self.error_code, "recommended_next_step": self.recommended_next_step,
            "parameters": self.parameters, "notes": self.notes, "evaluated_at": self.evaluated_at,
        }


def error_result(name: str, code: str, message: str, parameters: dict[str, Any] | None = None,
                 evidence: list[dict[str, Any]] | None = None, next_step: str | None = None) -> DiagnosticResult:
    """The diagnostic could not be completed (bad input, missing data, database problem)."""
    return DiagnosticResult(name, STATUS_ERROR, "unknown", message, evidence=evidence or [],
                            error_code=code, recommended_next_step=next_step, parameters=parameters or {})


@dataclass
class DiagnosticReport:
    """Roll-up of several results. It never states a root cause; it only counts findings."""

    results: list[DiagnosticResult]
    evaluated_at: str | None = None

    @property
    def overall_status(self) -> str:
        statuses = {r.status for r in self.results}
        for status in (STATUS_ERROR, STATUS_ISSUE, STATUS_WARNING):
            if status in statuses:
                return status
        return STATUS_OK

    @property
    def highest_severity(self) -> str:
        return worst_severity(*(r.severity for r in self.results if r.status != STATUS_ERROR))

    def to_dict(self) -> dict[str, Any]:
        counts = {s: sum(1 for r in self.results if r.status == s) for s in STATUSES}
        return {"overall_status": self.overall_status, "highest_severity": self.highest_severity,
                "counts": counts, "evaluated_at": self.evaluated_at,
                "results": [r.to_dict() for r in self.results]}
