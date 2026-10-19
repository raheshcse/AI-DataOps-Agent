"""Operational store: incidents, investigations, recoveries, ticket associations.

Every multi-row change runs in one ``BEGIN IMMEDIATE`` transaction. Duplicate-prevention is enforced by unique
indexes in the database (not only by application checks), so concurrent requests cannot both win.

Status is stored exactly once:
* ``incidents.status`` only describes the INVESTIGATION lifecycle (new / investigating / investigated /
  investigation_failed). Recovery state lives on ``recoveries.status`` and ticket state on the association.
* The workflow status shown by the API is derived from those rows at read time; it is never a second stored field.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from app.persistence.database import OperationsDatabase, StoreError

ACTIVE_RECOVERY = ("awaiting_approval", "resuming")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class DuplicateExternalRef(Exception):
    def __init__(self, incident_id: str) -> None:
        self.incident_id = incident_id
        super().__init__("external_ref_exists")


class InvestigationActive(Exception):
    def __init__(self, investigation_id: str) -> None:
        self.investigation_id = investigation_id
        super().__init__("investigation_active")


class RecoveryActive(Exception):
    def __init__(self, recovery_id: str) -> None:
        self.recovery_id = recovery_id
        super().__init__("recovery_active")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {k: row[k] for k in row.keys()}


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class OperationsStore:
    def __init__(self, database: OperationsDatabase, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.database = database
        self.clock = clock

    # ---------------------------------------------------------------------------------- plumbing
    def _now(self) -> str:
        return _ts(self.clock())

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.database.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as exc:
            raise StoreError("storage_error") from exc
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self.database.connect()
        try:
            yield conn
        except sqlite3.Error as exc:
            raise StoreError("storage_error") from exc
        finally:
            conn.close()

    def ping(self) -> bool:
        with self._read() as conn:
            conn.execute("SELECT 1").fetchone()
        return True

    # ---------------------------------------------------------------------------------- incidents
    def create_incident(self, *, title: str, description: str, incident_type: str, severity: str,
                        external_ref: str | None, idempotency_key: str | None, created_by: str
                        ) -> tuple[dict[str, Any], bool]:
        """Returns (incident, created). The same idempotency key returns the original incident (created=False).
        A reused external reference raises ``DuplicateExternalRef``."""
        now = self._now()
        incident_id = "inc-" + uuid.uuid4().hex[:20]
        with self._tx() as conn:
            if idempotency_key:
                existing = _row(conn.execute("SELECT * FROM incidents WHERE idempotency_key = ?", (idempotency_key,)).fetchone())
                if existing:
                    return existing, False
            if external_ref:
                clash = conn.execute("SELECT incident_id FROM incidents WHERE external_ref = ?", (external_ref,)).fetchone()
                if clash:
                    raise DuplicateExternalRef(clash["incident_id"])
            conn.execute(
                "INSERT INTO incidents(incident_id, external_ref, idempotency_key, title, description, incident_type,"
                " severity, status, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'new', ?,?,?)",
                (incident_id, external_ref, idempotency_key, title, description, incident_type, severity, created_by, now, now))
            return _row(conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()), True

    def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            return _row(conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone())

    def list_incidents(self, *, status: str | None, severity: str | None, limit: int, offset: int
                       ) -> tuple[list[dict[str, Any]], int]:
        where, args = [], []
        if status:
            where.append("status = ?"); args.append(status)
        if severity:
            where.append("severity = ?"); args.append(severity)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._read() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM incidents{clause}", args).fetchone()[0]
            rows = conn.execute(f"SELECT * FROM incidents{clause} ORDER BY created_at DESC, incident_id DESC LIMIT ? OFFSET ?",
                                (*args, limit, offset)).fetchall()
        return [_row(r) for r in rows], int(total)

    # ---------------------------------------------------------------------------------- investigations
    def create_investigation(self, *, incident_id: str, mode: str, requested_by: str) -> dict[str, Any]:
        """One active (pending/running) investigation per incident; a second raises ``InvestigationActive``."""
        now = self._now()
        investigation_id = "inv-" + uuid.uuid4().hex[:20]
        with self._tx() as conn:
            active = conn.execute("SELECT investigation_id FROM investigations WHERE incident_id = ? AND status IN ('pending','running')",
                                  (incident_id,)).fetchone()
            if active:
                raise InvestigationActive(active["investigation_id"])
            conn.execute("INSERT INTO investigations(investigation_id, incident_id, status, mode, requested_by, created_at)"
                         " VALUES (?,?, 'pending', ?,?,?)", (investigation_id, incident_id, mode, requested_by, now))
            conn.execute("UPDATE incidents SET status='investigating', updated_at=? WHERE incident_id=?", (now, incident_id))
            return self._inv(_row(conn.execute("SELECT * FROM investigations WHERE investigation_id = ?", (investigation_id,)).fetchone()))

    def mark_investigation_running(self, investigation_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute("UPDATE investigations SET status='running', started_at=? WHERE investigation_id=? AND status='pending'",
                               (self._now(), investigation_id))
            return cur.rowcount == 1

    def complete_investigation(self, investigation_id: str, *, report: dict[str, Any], report_status: str, summary: str,
                               evidence_refs: list[str]) -> bool:
        now = self._now()
        with self._tx() as conn:
            row = conn.execute("SELECT incident_id FROM investigations WHERE investigation_id=? AND status IN ('pending','running')",
                               (investigation_id,)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE investigations SET status='completed', completed_at=?, report_status=?, summary=?,"
                         " report_json=?, evidence_refs_json=? WHERE investigation_id=?",
                         (now, report_status, summary, _dumps(report), _dumps(evidence_refs), investigation_id))
            conn.execute("UPDATE incidents SET status='investigated', updated_at=? WHERE incident_id=?", (now, row["incident_id"]))
            return True

    def fail_investigation(self, investigation_id: str, *, error_kind: str, error_message: str) -> bool:
        now = self._now()
        with self._tx() as conn:
            row = conn.execute("SELECT incident_id FROM investigations WHERE investigation_id=? AND status IN ('pending','running')",
                               (investigation_id,)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE investigations SET status='failed', completed_at=?, error_kind=?, error_message=? WHERE investigation_id=?",
                         (now, error_kind[:60], error_message[:300], investigation_id))
            conn.execute("UPDATE incidents SET status='investigation_failed', updated_at=? WHERE incident_id=?", (now, row["incident_id"]))
            return True

    def sweep_interrupted_investigations(self) -> int:
        """Called at start-up: an investigation that was pending/running when the process died did NOT finish.
        Mark it failed ('interrupted') so it is never shown as running forever. Never touches completed rows."""
        now = self._now()
        with self._tx() as conn:
            rows = conn.execute("SELECT investigation_id, incident_id FROM investigations WHERE status IN ('pending','running')").fetchall()
            for r in rows:
                conn.execute("UPDATE investigations SET status='failed', completed_at=?, error_kind='interrupted',"
                             " error_message='The API process stopped before this investigation finished. Request a new one.'"
                             " WHERE investigation_id=?", (now, r["investigation_id"]))
                conn.execute("UPDATE incidents SET status='investigation_failed', updated_at=? WHERE incident_id=?", (now, r["incident_id"]))
            return len(rows)

    def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            return self._inv(_row(conn.execute("SELECT * FROM investigations WHERE investigation_id = ?", (investigation_id,)).fetchone()))

    def list_investigations(self, incident_id: str) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM investigations WHERE incident_id = ? ORDER BY created_at DESC, investigation_id DESC",
                                (incident_id,)).fetchall()
        return [self._inv(_row(r)) for r in rows]

    def latest_completed_investigation(self, incident_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            return self._inv(_row(conn.execute(
                "SELECT * FROM investigations WHERE incident_id=? AND status='completed' ORDER BY completed_at DESC, investigation_id DESC LIMIT 1",
                (incident_id,)).fetchone()))

    @staticmethod
    def _inv(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is not None:
            row["report"] = _loads(row.pop("report_json"), None)
            row["evidence_refs"] = _loads(row.pop("evidence_refs_json"), [])
        return row

    # ---------------------------------------------------------------------------------- recoveries
    def create_recovery(self, *, incident_id: str, investigation_id: str | None, action_type: str,
                        parameters: dict[str, Any], requested_by: str) -> dict[str, Any]:
        """Reserve the single active recovery slot for an incident (status 'awaiting_approval')."""
        now = self._now()
        recovery_id = "rec-" + uuid.uuid4().hex[:20]
        with self._tx() as conn:
            active = conn.execute("SELECT recovery_id FROM recoveries WHERE incident_id=? AND status IN ('awaiting_approval','resuming')",
                                  (incident_id,)).fetchone()
            if active:
                raise RecoveryActive(active["recovery_id"])
            conn.execute("INSERT INTO recoveries(recovery_id, incident_id, investigation_id, action_type, parameters_json, status,"
                         " requested_by, created_at, updated_at) VALUES (?,?,?,?,?, 'awaiting_approval', ?,?,?)",
                         (recovery_id, incident_id, investigation_id, action_type, _dumps(parameters), requested_by, now, now))
            return self._rec(_row(conn.execute("SELECT * FROM recoveries WHERE recovery_id=?", (recovery_id,)).fetchone()))

    def record_recovery_outcome(self, recovery_id: str, *, status: str, execution_status: str = "not_run",
                                reasons: list[str] | tuple[str, ...] = (), approval_request_id: str | None = None,
                                decision_id: str | None = None, ledger_entry_ids: list[str] | tuple[str, ...] = (),
                                verified_outcome: str = "not_applicable", started: bool = False) -> bool:
        """Write the outcome of a workflow step. Only an active (awaiting_approval/resuming) or already-terminal-by-
        this-call row is updated; a row that reached a final status is never rewritten."""
        now = self._now()
        with self._tx() as conn:
            row = conn.execute("SELECT status, approval_request_id FROM recoveries WHERE recovery_id=?", (recovery_id,)).fetchone()
            if row is None or row["status"] not in ACTIVE_RECOVERY:
                return False
            final = status not in ACTIVE_RECOVERY
            conn.execute(
                "UPDATE recoveries SET status=?, execution_status=?, reasons_json=?, approval_request_id=COALESCE(?, approval_request_id),"
                " decision_id=COALESCE(?, decision_id), ledger_entry_ids_json=?, verified_outcome=?, updated_at=?,"
                " started_at=CASE WHEN ? THEN COALESCE(started_at, ?) ELSE started_at END,"
                " completed_at=CASE WHEN ? THEN ? ELSE completed_at END WHERE recovery_id=?",
                (status, execution_status, _dumps(list(reasons)), approval_request_id, decision_id, _dumps(list(ledger_entry_ids)),
                 verified_outcome, now, 1 if started else 0, now, 1 if final else 0, now, recovery_id))
            return True

    def claim_recovery_resume(self, recovery_id: str) -> bool:
        """Atomically move awaiting_approval -> resuming. Only the winner may call the workflow's resume."""
        with self._tx() as conn:
            cur = conn.execute("UPDATE recoveries SET status='resuming', started_at=COALESCE(started_at, ?), updated_at=?"
                               " WHERE recovery_id=? AND status='awaiting_approval'", (self._now(), self._now(), recovery_id))
            return cur.rowcount == 1

    def release_recovery_claim(self, recovery_id: str) -> bool:
        """resuming -> awaiting_approval, only when nothing was executed (the workflow reported awaiting_approval)."""
        with self._tx() as conn:
            cur = conn.execute("UPDATE recoveries SET status='awaiting_approval', updated_at=? WHERE recovery_id=? AND status='resuming'",
                               (self._now(), recovery_id))
            return cur.rowcount == 1

    def get_recovery(self, recovery_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            return self._rec(_row(conn.execute("SELECT * FROM recoveries WHERE recovery_id = ?", (recovery_id,)).fetchone()))

    def get_recovery_by_request(self, request_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            return self._rec(_row(conn.execute("SELECT * FROM recoveries WHERE approval_request_id = ?", (request_id,)).fetchone()))

    def list_recoveries(self, incident_id: str) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM recoveries WHERE incident_id=? ORDER BY created_at DESC, recovery_id DESC", (incident_id,)).fetchall()
        return [self._rec(_row(r)) for r in rows]

    def list_resuming(self) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM recoveries WHERE status='resuming'").fetchall()
        return [self._rec(_row(r)) for r in rows]

    @staticmethod
    def _rec(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is not None:
            row["parameters"] = _loads(row.pop("parameters_json"), {})
            row["reasons"] = _loads(row.pop("reasons_json"), [])
            row["ledger_entry_ids"] = _loads(row.pop("ledger_entry_ids_json"), [])
        return row

    # ---------------------------------------------------------------------------------- tickets
    def begin_ticket(self, *, incident_id: str, provider: str, repository: str) -> tuple[dict[str, Any], bool]:
        """Claim the ticket slot for an incident. Returns (association, claimed). ``claimed`` is True only when this
        caller may now talk to the provider; an existing open/closed/in-progress ticket is returned unclaimed."""
        now = self._now()
        with self._tx() as conn:
            row = _row(conn.execute("SELECT * FROM ticket_associations WHERE incident_id=?", (incident_id,)).fetchone())
            if row is None:
                assoc_id = "tkt-" + uuid.uuid4().hex[:20]
                conn.execute("INSERT INTO ticket_associations(association_id, incident_id, provider, repository, status, created_at, updated_at)"
                             " VALUES (?,?,?,?, 'creating', ?,?)", (assoc_id, incident_id, provider, repository, now, now))
                return _row(conn.execute("SELECT * FROM ticket_associations WHERE association_id=?", (assoc_id,)).fetchone()), True
            if row["status"] in ("creation_failed", "creation_unconfirmed"):
                conn.execute("UPDATE ticket_associations SET status='creating', updated_at=? WHERE association_id=?", (now, row["association_id"]))
                row["status"], row["updated_at"] = "creating", now
                return row, True
            return row, False

    def finish_ticket(self, incident_id: str, *, status: str, external_id: str | None = None, external_number: int | None = None,
                      url: str | None = None, sync_result: str | None = None) -> dict[str, Any] | None:
        now = self._now()
        with self._tx() as conn:
            conn.execute(
                "UPDATE ticket_associations SET status=?, external_id=COALESCE(?, external_id), external_number=COALESCE(?, external_number),"
                " url=COALESCE(?, url), updated_at=?, last_sync_at=CASE WHEN ? IS NULL THEN last_sync_at ELSE ? END,"
                " last_sync_result=COALESCE(?, last_sync_result) WHERE incident_id=?",
                (status, external_id, external_number, url, now, sync_result, now, sync_result, incident_id))
            return _row(conn.execute("SELECT * FROM ticket_associations WHERE incident_id=?", (incident_id,)).fetchone())

    def get_ticket(self, incident_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            return _row(conn.execute("SELECT * FROM ticket_associations WHERE incident_id=?", (incident_id,)).fetchone())

    def add_ticket_event(self, incident_id: str, operation: str, outcome: str, error_kind: str | None = None) -> None:
        with self._tx() as conn:
            conn.execute("INSERT INTO ticket_events(incident_id, operation, outcome, error_kind, at) VALUES (?,?,?,?,?)",
                         (incident_id, operation[:40], outcome[:40], (error_kind or None) and error_kind[:60], self._now()))

    def list_ticket_events(self, incident_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute("SELECT operation, outcome, error_kind, at FROM ticket_events WHERE incident_id=? ORDER BY event_id DESC LIMIT ?",
                                (incident_id, limit)).fetchall()
        return [_row(r) for r in rows]
