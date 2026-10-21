"""DiagnosticsService: the one entry point for all diagnostic tools.

Every public method returns a ``DiagnosticResult`` and never raises: bad input, an
unseeded database or a database failure becomes a structured ``error`` result. Error
messages are written by this module; driver/exception text is never copied into a result
because it can contain server names or connection details.

The methods are thin and independent so MCP tool wrappers can map one-to-one onto them
in a later phase.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable

from sqlalchemy.exc import SQLAlchemyError

from app.db.connection import DatabaseUnavailable
from app.diagnostics.models import (
    DIAG_DB_ERROR,
    DIAG_INTERNAL,
    DIAG_INVALID_INPUT,
    DIAG_NOT_SEEDED,
    DiagnosticReport,
    DiagnosticResult,
    InvalidInput,
    error_result,
)
from app.diagnostics.readonly import ReadOnlyRepo
from app.diagnostics.tools import (
    duplicates,
    freshness,
    logs as log_tool,
    operations,
    quality,
    reconciliation,
    schema_contract,
    status,
)
from app.simulation.clock import SimulationNotSeeded
from app.simulation.ports import SimRepository
from app.simulation.runlog import RunLogStore

logger = logging.getLogger("app.diagnostics")

TOOL_NAMES = (
    status.NAME, log_tool.NAME, duplicates.NAME, quality.NAME, freshness.NAME,
    reconciliation.NAME, schema_contract.NAME,
)
OPERATIONS_TOOL_NAMES = (operations.STATS_NAME, operations.ERRORS_NAME, operations.HEALTH_NAME)


class DiagnosticsService:
    def __init__(self, repo: SimRepository, logs: RunLogStore, freshness_threshold_minutes: int = 1500):
        self.repo = ReadOnlyRepo(repo)
        self.logs = logs
        self.threshold = freshness_threshold_minutes

    # ------------------------------------------------------------------ plumbing
    def _run(self, name: str, params: dict[str, Any],
             fn: Callable[[dict[str, Any], Any], DiagnosticResult]) -> DiagnosticResult:
        try:
            state = self.repo.get_state()
            if state is None:
                raise SimulationNotSeeded
            result = fn(state, state["sim_now"])
            result.evaluated_at = state["sim_now"].isoformat()
        except InvalidInput as exc:
            result = error_result(name, DIAG_INVALID_INPUT, str(exc), params)
        except SimulationNotSeeded:
            result = error_result(name, DIAG_NOT_SEEDED, "The simulation database has not been seeded.", params,
                                  next_step="Run python -m scripts.db_init and python -m scripts.db_seed.")
        except (SQLAlchemyError, DatabaseUnavailable, ImportError):
            result = error_result(name, DIAG_DB_ERROR, "A database error stopped this diagnostic "
                                  "(details withheld).", params,
                                  next_step="Run python -m scripts.check_prereqs to diagnose connectivity.")
        except Exception as exc:  # a bug in a tool must not crash the caller
            result = error_result(name, DIAG_INTERNAL, f"Unexpected internal error ({type(exc).__name__}).", params)
        logger.info("diagnostic %s finished status=%s severity=%s code=%s",
                    name, result.status, result.severity, result.error_code)
        return result

    # --------------------------------------------------------------------- tools
    def get_pipeline_status(self, pipeline_id: str | None = None, status_filter: str | None = None,
                            limit: int = 10) -> DiagnosticResult:
        params = {"pipeline_id": pipeline_id, "status": status_filter, "limit": limit}
        return self._run(status.NAME, params,
                         lambda s, now: status.get_pipeline_status(self.repo, pipeline_id, status_filter, limit))

    def inspect_pipeline_logs(self, run_id: str, max_events: int = 200) -> DiagnosticResult:
        params = {"run_id": run_id, "max_events": max_events}
        return self._run(log_tool.NAME, params,
                         lambda s, now: log_tool.inspect_pipeline_logs(self.repo, self.logs, run_id, max_events))

    def detect_duplicate_records(self) -> DiagnosticResult:
        return self._run(duplicates.NAME, {}, lambda s, now: duplicates.detect_duplicate_records(self.repo))

    def diagnose_data_quality(self) -> DiagnosticResult:
        return self._run(quality.NAME, {}, lambda s, now: quality.diagnose_data_quality(self.repo, now))

    def check_data_freshness(self, pipeline_id: str = "order_ingestion") -> DiagnosticResult:
        params = {"pipeline_id": pipeline_id}
        return self._run(freshness.NAME, params,
                         lambda s, now: freshness.check_data_freshness(self.repo, now, self.threshold, pipeline_id))

    def reconcile_record_counts(self, batch_date: date | str | None = None) -> DiagnosticResult:
        params = {"batch_date": batch_date}
        return self._run(reconciliation.NAME, params,
                         lambda s, now: reconciliation.reconcile_record_counts(self.repo, self.logs, s, batch_date))

    def validate_schema_contract(self, table: str | None = None) -> DiagnosticResult:
        params = {"table": table}
        return self._run(schema_contract.NAME, params,
                         lambda s, now: schema_contract.validate_schema_contract(self.repo, table))

    # ------------------------------------------------- Phase 10 operations tools (not in run_all)
    def get_pipeline_run_statistics(self, pipeline_id: str | None = None, window: int = 20) -> DiagnosticResult:
        params = {"pipeline_id": pipeline_id, "window": window}
        return self._run(operations.STATS_NAME, params,
                         lambda s, now: operations.get_pipeline_run_statistics(self.repo, pipeline_id, window))

    def summarize_recent_errors(self, limit: int = 20) -> DiagnosticResult:
        return self._run(operations.ERRORS_NAME, {"limit": limit},
                         lambda s, now: operations.summarize_recent_errors(self.repo, limit))

    def check_diagnostics_health(self) -> DiagnosticResult:
        return self._run(operations.HEALTH_NAME, {}, lambda s, now: operations.check_diagnostics_health(self.repo, now))

    # ------------------------------------------------------------------- roll-up
    def run_all(self, batch_date: date | str | None = None) -> DiagnosticReport:
        """Every tool that needs no run id, plus the log of the most recent run (if any)."""
        results = [
            self.get_pipeline_status(), self.check_data_freshness(), self.reconcile_record_counts(batch_date),
            self.detect_duplicate_records(), self.diagnose_data_quality(), self.validate_schema_contract(),
        ]
        try:
            latest = self.repo.list_runs(None, 1)
        except Exception:  # the roll-up still reports the other results
            latest = []
        if latest:
            results.append(self.inspect_pipeline_logs(latest[0]["run_id"]))
        stamp = next((r.evaluated_at for r in results if r.evaluated_at), None)
        return DiagnosticReport(results, stamp)
