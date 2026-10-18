"""A read-only view of the simulation repository.

Diagnostics may *look at* everything the simulation stores but must never change it.
``ReadOnlyRepo`` forwards an explicit allow-list of read methods and refuses everything
else (``insert_orders``, ``save_run``, ``wipe``, ``seed`` ...), so a diagnostic bug cannot
write even though the underlying repository could.

This is an application-level guarantee. For a database-level one, give the diagnostics a
SQL login with only ``db_datareader`` (listed as a future hardening step in the docs).
"""

from __future__ import annotations

from typing import Any

READ_METHODS = frozenset({
    "get_state", "table_counts", "integrity_problems", "list_pipelines", "get_pipeline",
    "list_runs", "get_run", "run_exists", "latest_successful_run", "list_incidents",
    "get_validations", "get_feed_rows", "known_event_ids", "order_summary", "duplicate_events",
    "order_total_mismatches", "future_dated_orders", "observed_schema", "get_scenarios",
    "duplicate_order_details", "duplicate_feed_events", "order_quality_scan",
})


class ReadOnlyRepo:
    def __init__(self, repo: Any):
        object.__setattr__(self, "_repo", repo)

    def __getattr__(self, name: str) -> Any:
        if name in READ_METHODS:
            return getattr(object.__getattribute__(self, "_repo"), name)
        raise AttributeError(f"'{name}' is not available through the read-only diagnostics interface")

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("the read-only diagnostics interface cannot be modified")
