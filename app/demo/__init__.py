"""Synthetic demonstration data. Everything here is generated, deterministic, labelled ``demo_synthetic`` and kept apart
from every real store: it is never written to SQL Server, the operations SQLite database or the governance ledger."""

from app.demo.incidents import (
    DEMO_ID_PREFIX,
    DEMO_SOURCE,
    STATUSES,
    generate_demo_incidents,
    validate_demo_incidents,
)

__all__ = ["DEMO_ID_PREFIX", "DEMO_SOURCE", "STATUSES", "generate_demo_incidents", "validate_demo_incidents"]
