"""SQLite connection handling and explicit, forward-only, NON-destructive schema migrations.

* Only ``sqlite:///`` URLs are accepted. Any other scheme (including SQL Server) is refused, so this layer can
  never be pointed at ``dataops_sim``.
* Migrations only ever CREATE. There is no DROP, DELETE or TRUNCATE anywhere in this package (a test enforces it),
  and opening the database never resets anything.
* SQLite is single-host and has coarse write locking. Fine for local development and a single API process; it is
  NOT a multi-instance production database (see the Phase 8 document).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlparse

DEFAULT_OPS_DB = Path(__file__).resolve().parents[2] / "data" / "operations.sqlite3"


class DatabaseConfigError(Exception):
    """The operational database URL is unusable. The message never contains credentials."""


class StoreError(Exception):
    """A storage failure. Carries a safe, generic reason only (never driver text or SQL)."""

    def __init__(self, reason: str = "storage_error") -> None:
        self.reason = reason
        super().__init__(reason)


def database_path_from_url(url: str | None) -> Path:
    """``sqlite:///relative.db`` / ``sqlite:////abs/path.db`` / ``sqlite:///C:/path.db`` -> Path. None -> default."""
    if url is None or not url.strip():
        return DEFAULT_OPS_DB
    parsed = urlparse(url.strip())
    if parsed.scheme != "sqlite" or parsed.netloc:
        raise DatabaseConfigError("DATABASE_URL must be a sqlite:/// URL (other databases are not supported in Phase 8)")
    raw = unquote(parsed.path)
    if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":  # sqlite:///C:/x.db -> /C:/x.db
        raw = raw[1:]
    elif raw.startswith("//"):                                    # sqlite:////abs/x.db -> //abs/x.db
        raw = raw[1:]
    elif raw.startswith("/"):                                     # sqlite:///rel.db -> /rel.db  (relative)
        raw = raw[1:]
    if not raw or raw == ":memory:":
        raise DatabaseConfigError("DATABASE_URL must name a database file (in-memory databases are not supported)")
    return Path(raw)


_MIGRATION_1 = """
CREATE TABLE incidents (
    incident_id   TEXT PRIMARY KEY,
    external_ref  TEXT,
    idempotency_key TEXT,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL,
    incident_type TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('low','medium','high','critical')),
    status        TEXT NOT NULL CHECK (status IN ('new','investigating','investigated','investigation_failed')),
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_incidents_external_ref ON incidents(external_ref) WHERE external_ref IS NOT NULL;
CREATE UNIQUE INDEX ux_incidents_idempotency ON incidents(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX ix_incidents_status_created ON incidents(status, created_at);

CREATE TABLE investigations (
    investigation_id TEXT PRIMARY KEY,
    incident_id   TEXT NOT NULL REFERENCES incidents(incident_id),
    status        TEXT NOT NULL CHECK (status IN ('pending','running','completed','failed')),
    mode          TEXT NOT NULL,
    requested_by  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    completed_at  TEXT,
    report_status TEXT,
    summary       TEXT,
    report_json   TEXT,
    evidence_refs_json TEXT,
    error_kind    TEXT,
    error_message TEXT
);
CREATE UNIQUE INDEX ux_investigations_one_active ON investigations(incident_id) WHERE status IN ('pending','running');
CREATE INDEX ix_investigations_incident ON investigations(incident_id, created_at);

CREATE TABLE recoveries (
    recovery_id   TEXT PRIMARY KEY,
    incident_id   TEXT NOT NULL REFERENCES incidents(incident_id),
    investigation_id TEXT REFERENCES investigations(investigation_id),
    approval_request_id TEXT,
    action_type   TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    status        TEXT NOT NULL,
    execution_status TEXT NOT NULL DEFAULT 'not_run',
    reasons_json  TEXT NOT NULL DEFAULT '[]',
    decision_id   TEXT,
    ledger_entry_ids_json TEXT NOT NULL DEFAULT '[]',
    verified_outcome TEXT NOT NULL DEFAULT 'not_applicable'
        CHECK (verified_outcome IN ('verified','not_verified','unknown','not_applicable')),
    requested_by  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    started_at    TEXT,
    completed_at  TEXT
);
CREATE UNIQUE INDEX ux_recoveries_request ON recoveries(approval_request_id) WHERE approval_request_id IS NOT NULL;
CREATE UNIQUE INDEX ux_recoveries_one_active ON recoveries(incident_id) WHERE status IN ('awaiting_approval','resuming');
CREATE INDEX ix_recoveries_incident ON recoveries(incident_id, created_at);

CREATE TABLE ticket_associations (
    association_id TEXT PRIMARY KEY,
    incident_id   TEXT NOT NULL UNIQUE REFERENCES incidents(incident_id),
    provider      TEXT NOT NULL,
    repository    TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('creating','creation_unconfirmed','creation_failed','open','closed')),
    external_id   TEXT,
    external_number INTEGER,
    url           TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_sync_at  TEXT,
    last_sync_result TEXT
);
CREATE UNIQUE INDEX ux_ticket_external ON ticket_associations(provider, repository, external_number)
    WHERE external_number IS NOT NULL;

CREATE TABLE ticket_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    operation   TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    error_kind  TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX ix_ticket_events_incident ON ticket_events(incident_id, event_id);
"""

# (version, script). Append only; never edit a released migration.
MIGRATIONS: list[tuple[int, str]] = [(1, _MIGRATION_1)]
LATEST_VERSION = MIGRATIONS[-1][0]


class OperationsDatabase:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_OPS_DB

    def connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise StoreError("cannot_open_database") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def schema_version(self) -> int:
        try:
            with closing(self.connect()) as conn:
                row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
                if row is None:
                    return 0
                return int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
        except sqlite3.Error as exc:
            raise StoreError("schema_check_failed") from exc

    def migrate(self) -> int:
        """Apply any missing migrations, each in its own transaction. Safe to call repeatedly. Returns the version."""
        try:
            with closing(self.connect()) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                current = int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
                if current > LATEST_VERSION:
                    raise StoreError("database_newer_than_application")
                for version, script in MIGRATIONS:
                    if version <= current:
                        continue
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        for statement in _split(script):
                            conn.execute(statement)
                        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                                     (version,))
                        conn.execute("COMMIT")
                    except Exception:
                        conn.execute("ROLLBACK")
                        raise
                return LATEST_VERSION
        except StoreError:
            raise
        except sqlite3.Error as exc:
            raise StoreError("migration_failed") from exc


def _split(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
