"""Lazy, failure-tolerant access to the DiagnosticsService.

The database connection is created on first use, not at import or server start, so the server can
always start and list its tools. If the connection cannot be configured, tools return a structured
``DIAG-DB-ERROR`` result (details withheld) instead of crashing the server.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from app.diagnostics import DiagnosticsService

logger = logging.getLogger("app.mcp_server")


def build_service_from_settings() -> DiagnosticsService:
    """The normal (non-admin) application connection; diagnostics only ever read through it."""
    from app.config import get_settings
    from app.db.connection import make_engine
    from app.db.repository import SqlRepository
    from app.simulation.runlog import RunLogStore

    settings = get_settings()
    engine = make_engine(settings)
    return DiagnosticsService(SqlRepository(engine), RunLogStore(Path(settings.sim_log_dir).resolve()),
                              settings.freshness_threshold_minutes)


class ServiceProvider:
    def __init__(self, factory: Callable[[], DiagnosticsService] = build_service_from_settings):
        self._factory = factory
        self._service: DiagnosticsService | None = None
        self._lock = threading.Lock()

    def get(self) -> DiagnosticsService | None:
        """The service, or None if it cannot be built right now (the next call tries again)."""
        with self._lock:
            if self._service is None:
                try:
                    self._service = self._factory()
                except Exception as exc:  # never echo the message: it can contain connection details
                    logger.warning("diagnostics service could not be created (%s)", type(exc).__name__)
                    return None
            return self._service
