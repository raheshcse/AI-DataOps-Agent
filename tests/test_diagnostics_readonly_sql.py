"""Proof that every repository method the diagnostics may call only issues SELECT statements.

The statements are captured from a real SQLAlchemy engine (in-memory SQLite, schema created from
``app.db.schema``). SQLite cannot run every T-SQL-specific query, so a method may raise; what is
asserted is that whatever statement reached the cursor was a read, and that ``begin()`` (the write path
used by the repository's mutating methods) is never used.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.db import schema
from app.db.repository import SqlRepository
from app.diagnostics.readonly import READ_METHODS

SAMPLE_ARGS = {
    "future_dated_orders": (datetime(2026, 1, 15, tzinfo=timezone.utc),),
    "get_feed_rows": (date(2026, 1, 14),),
    "get_pipeline": ("order_ingestion",),
    "get_run": ("run-x",),
    "get_validations": ("run-x",),
    "known_event_ids": (["evt-1"],),
    "latest_successful_run": ("order_ingestion",),
    "observed_schema": ("stg_order_feed",),
    "order_summary": (date(2026, 1, 14),),
    "run_exists": ("run-x",),
}


def test_every_diagnostic_repository_method_only_reads():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    schema.metadata.create_all(engine)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    def no_begin(*_a, **_k):
        raise AssertionError("a diagnostic read used engine.begin() (the write path)")

    engine.begin = no_begin  # type: ignore[method-assign]
    repo = SqlRepository(engine)
    statements.clear()
    for name in sorted(READ_METHODS):
        method = getattr(repo, name)
        args = SAMPLE_ARGS.get(name, ())
        assert len(args) >= sum(
            1 for p in inspect.signature(method).parameters.values() if p.default is p.empty), name
        try:
            method(*args)
        except AssertionError:
            raise
        except Exception:  # noqa: BLE001 - T-SQL-only constructs may not run on SQLite
            pass
    assert statements, "no statements were captured"
    for statement in statements:
        head = statement.lstrip().upper()
        assert head.startswith(("SELECT", "WITH", "PRAGMA")), statement[:80]
        for word in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ", "EXEC", "MERGE "):
            assert word not in head, (word, statement[:80])
