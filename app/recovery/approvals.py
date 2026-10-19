"""Approval requests: lifecycle, exact-action binding, sealing, durable storage and atomic consumption.

Lifecycle (``status``)::

    pending --approve--> approved --consume--> consumed
       |  \\--reject--> rejected          (approved/pending also -> expired when past expires_at)
       \\--cancel--> cancelled

* **Binding.** ``binding_digest`` = SHA-256 of the canonical JSON (sorted keys, no spaces, ASCII) of
  ``{v, incident_id, action_type, parameters, target, evidence_refs(sorted), report_status}``. It is recomputed from the
  stored fields whenever an approval is approved, checked or consumed, so editing any of them is detected.
* **Authenticity.** A digest alone proves nothing, so an approval also carries ``seal`` = HMAC-SHA256 under a
  server-held key over ``{request_id, binding_digest, approver, decided_at, expires_at, decision}``. Without
  the key (or with a forged row) the seal fails and the approval is unusable. Missing/short key = no approvals.
* **Atomicity.** Consumption is one conditional ``UPDATE ... WHERE status='approved' AND expires_at>now AND
  ledger_decision_entry IS NOT NULL``; exactly one concurrent caller can see ``rowcount == 1``.
* **Durability.** SQLite file (standard library). State survives a restart. This is a local single-host
  store, not a distributed one.
* **Ledger.** Every transition is written to the governance ledger with ``caused_by`` links; an approval whose
  decision could not be recorded is unusable (``ledger_decision_entry`` stays NULL).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from app.governance.ledger import GovernanceLedgerError, GovernanceRecorder
from app.recovery.actions import ACTIONS, ActionRejected, ValidatedAction, validate_action
from app.recovery.auth import ApprovalConfigError, Authenticator, Identity, is_permitted

PENDING, APPROVED, REJECTED, EXPIRED, CANCELLED, CONSUMED = (
    "pending", "approved", "rejected", "expired", "cancelled", "consumed")
STATUSES = (PENDING, APPROVED, REJECTED, EXPIRED, CANCELLED, CONSUMED)

DEFAULT_APPROVAL_DB = Path(__file__).resolve().parents[2] / "data" / "approvals.sqlite3"
DEFAULT_TTL = timedelta(minutes=30)
MAX_TTL = timedelta(hours=24)
MIN_KEY_BYTES = 32

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
_REQ_RE = re.compile(r"^apr-[0-9a-f]{32}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ApprovalDenied(Exception):
    """An approval operation was refused. ``code`` is a stable machine-readable reason; no secrets inside."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def binding_digest(incident_id: str, action_type: str, parameters: Mapping[str, Any], target: str,
                   evidence_refs: tuple[str, ...] | list[str], report_status: str) -> str:
    payload = {"v": 1, "incident_id": incident_id, "action_type": action_type, "parameters": dict(parameters),
               "target": target, "evidence_refs": sorted(evidence_refs), "report_status": report_status}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _ts(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("naive datetime")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean(text: object, limit: int) -> str:
    return _CTRL_RE.sub(" ", str(text))[:limit].strip()


# ------------------------------------------------------------------------------------------------- sealing
class ApprovalSigner:
    def __init__(self, key: str | bytes) -> None:
        raw = key.encode("utf-8") if isinstance(key, str) else key
        if len(raw) < MIN_KEY_BYTES:
            raise ApprovalConfigError(f"the approval signing key must be at least {MIN_KEY_BYTES} bytes")
        self._key = raw

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "ApprovalSigner":
        key = environ.get("APPROVAL_SIGNING_KEY", "")
        if not key:
            raise ApprovalConfigError("APPROVAL_SIGNING_KEY is not set")
        return cls(key)

    def seal(self, *, request_id: str, digest: str, approver: str, decided_at: str, expires_at: str,
             decision: str) -> str:
        body = canonical_json({"request_id": request_id, "binding_digest": digest, "approver": approver,
                               "decided_at": decided_at, "expires_at": expires_at, "decision": decision})
        return hmac.new(self._key, body.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, seal: str | None, **fields: str) -> bool:
        if not isinstance(seal, str):
            return False
        return hmac.compare_digest(seal, self.seal(**fields))


# --------------------------------------------------------------------------------------------------- record
@dataclass(frozen=True)
class ApprovalRecord:
    request_id: str
    incident_id: str
    action_type: str
    parameters: dict[str, Any]
    target: str
    evidence_refs: tuple[str, ...]
    reason: str
    expected_effect: str
    risk: str
    governance_state: str
    report_status: str
    binding_digest: str
    requested_by: str
    created_at: str
    expires_at: str
    status: str
    decided_by: str | None = None
    decided_at: str | None = None
    decision_reason: str | None = None
    seal: str | None = None
    consumed_at: str | None = None
    consumed_by_decision: str | None = None
    ledger_request_entry: str | None = None
    ledger_decision_entry: str | None = None

    def recomputed_digest(self) -> str:
        return binding_digest(self.incident_id, self.action_type, self.parameters, self.target, self.evidence_refs,
                              self.report_status)

    def summary(self) -> dict[str, Any]:
        """Safe to show/log: no seal, no credentials."""
        return {"request_id": self.request_id, "incident_id": self.incident_id, "action_type": self.action_type,
                "parameters": self.parameters, "target": self.target, "evidence_refs": list(self.evidence_refs),
                "reason": self.reason, "expected_effect": self.expected_effect, "risk": self.risk,
                "governance_state": self.governance_state, "report_status": self.report_status,
                "binding_digest": self.binding_digest,
                "created_at": self.created_at, "expires_at": self.expires_at, "status": self.status,
                "decided_by": self.decided_by, "decision_reason": self.decision_reason}


_COLUMNS = ("request_id", "incident_id", "action_type", "parameters_json", "target", "evidence_json", "reason",
            "expected_effect", "risk", "governance_state", "report_status", "binding_digest", "requested_by", "created_at",
            "expires_at", "status", "decided_by", "decided_at", "decision_reason", "seal", "consumed_at",
            "consumed_by_decision", "ledger_request_entry", "ledger_decision_entry")
_SCHEMA = f"""CREATE TABLE IF NOT EXISTS approval_requests (
    request_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, action_type TEXT NOT NULL, parameters_json TEXT NOT NULL,
    target TEXT NOT NULL, evidence_json TEXT NOT NULL, reason TEXT NOT NULL, expected_effect TEXT NOT NULL,
    risk TEXT NOT NULL, governance_state TEXT NOT NULL, report_status TEXT NOT NULL, binding_digest TEXT NOT NULL, requested_by TEXT NOT NULL,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL, decided_by TEXT, decided_at TEXT,
    decision_reason TEXT, seal TEXT, consumed_at TEXT, consumed_by_decision TEXT, ledger_request_entry TEXT,
    ledger_decision_entry TEXT)"""


def _from_row(row: sqlite3.Row) -> ApprovalRecord:
    values = dict(row)
    return ApprovalRecord(
        request_id=values["request_id"], incident_id=values["incident_id"], action_type=values["action_type"],
        parameters=json.loads(values["parameters_json"]), target=values["target"],
        evidence_refs=tuple(json.loads(values["evidence_json"])), reason=values["reason"],
        expected_effect=values["expected_effect"], risk=values["risk"], governance_state=values["governance_state"],
        report_status=values["report_status"], binding_digest=values["binding_digest"], requested_by=values["requested_by"],
        created_at=values["created_at"], expires_at=values["expires_at"], status=values["status"],
        decided_by=values["decided_by"], decided_at=values["decided_at"], decision_reason=values["decision_reason"],
        seal=values["seal"], consumed_at=values["consumed_at"], consumed_by_decision=values["consumed_by_decision"],
        ledger_request_entry=values["ledger_request_entry"], ledger_decision_entry=values["ledger_decision_entry"])


class SqliteApprovalStore:
    """One short-lived connection per call; writes use ``BEGIN IMMEDIATE`` so they serialise across processes."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_APPROVAL_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def insert(self, rec: ApprovalRecord) -> None:
        values = (rec.request_id, rec.incident_id, rec.action_type, canonical_json(rec.parameters), rec.target,
                  canonical_json(list(rec.evidence_refs)), rec.reason, rec.expected_effect, rec.risk,
                  rec.governance_state, rec.report_status, rec.binding_digest, rec.requested_by, rec.created_at, rec.expires_at,
                  rec.status, rec.decided_by, rec.decided_at, rec.decision_reason, rec.seal, rec.consumed_at,
                  rec.consumed_by_decision, rec.ledger_request_entry, rec.ledger_decision_entry)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"INSERT INTO approval_requests ({','.join(_COLUMNS)}) VALUES ({','.join('?' * len(_COLUMNS))})", values)
            conn.execute("COMMIT")

    def get(self, request_id: str) -> ApprovalRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM approval_requests WHERE request_id = ?", (request_id,)).fetchone()
        return _from_row(row) if row else None

    def list(self, status: str | None = None) -> list[ApprovalRecord]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM approval_requests" + (" WHERE status = ?" if status else "")
                                + " ORDER BY created_at", (status,) if status else ()).fetchall()
        return [_from_row(r) for r in rows]

    def update_where(self, request_id: str, set_values: dict[str, Any], where_sql: str, where_args: tuple[Any, ...]) -> bool:
        """Atomic conditional update. True iff exactly one row matched the condition and was changed."""
        assignments = ", ".join(f"{col} = ?" for col in set_values)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(f"UPDATE approval_requests SET {assignments} WHERE request_id = ? AND {where_sql}",
                               (*set_values.values(), request_id, *where_args))
            conn.execute("COMMIT")
            return cur.rowcount == 1


# ------------------------------------------------------------------------------------------------- service
@dataclass(frozen=True)
class ApprovalCheck:
    """Read-only verdict on a request. ``state`` is 'approved_valid' only when every check passes."""
    state: str   # approved_valid | pending | rejected | expired | cancelled | consumed | invalid | missing
    reason: str
    record: ApprovalRecord | None = None


class ApprovalService:
    def __init__(self, store: SqliteApprovalStore, signer: ApprovalSigner, authenticator: Authenticator,
                 recorder: GovernanceRecorder, *, clock: Callable[[], datetime] = _utcnow,
                 ttl: timedelta = DEFAULT_TTL) -> None:
        if not timedelta(0) < ttl <= MAX_TTL:
            raise ApprovalConfigError("approval ttl must be positive and at most 24 hours")
        self.store, self.signer, self.auth, self.recorder = store, signer, authenticator, recorder
        self.clock, self.ttl = clock, ttl

    # -- creation ------------------------------------------------------------------------------------
    def create_request(self, *, incident_id: str, action: ValidatedAction, evidence_refs: list[str] | tuple[str, ...],
                       reason: str, governance_state: str, report_status: str, requested_by: str = "agent",
                       caused_by: str | None = None, decision_id: str | None = None) -> ApprovalRecord:
        if not isinstance(incident_id, str) or not _ID_RE.match(incident_id):
            raise ApprovalDenied("invalid_incident_id")
        refs = tuple(evidence_refs)
        if len(refs) > 20 or not all(isinstance(r, str) and _REF_RE.match(r) for r in refs):
            raise ApprovalDenied("invalid_evidence_refs")
        spec = ACTIONS.get(action.action_type)
        if spec is None:
            raise ApprovalDenied("unknown_action")
        again = validate_action(action.action_type, action.parameters)  # defence in depth: never trust the caller's object
        if again != action:
            raise ApprovalDenied("action_not_normalised")
        if not isinstance(report_status, str) or not _ID_RE.match(report_status):
            raise ApprovalDenied("invalid_report_status")
        now = self.clock()
        request_id = "apr-" + uuid.uuid4().hex
        record = ApprovalRecord(
            request_id=request_id, incident_id=incident_id, action_type=action.action_type,
            parameters=dict(action.parameters), target=action.target, evidence_refs=refs, reason=_clean(reason, 500),
            expected_effect=action.expected_effect, risk=action.risk, governance_state=_clean(governance_state, 40), report_status=report_status,
            binding_digest=binding_digest(incident_id, action.action_type, action.parameters, action.target, refs,
                                          report_status),
            requested_by=_clean(requested_by, 64), created_at=_ts(now), expires_at=_ts(now + self.ttl), status=PENDING)
        entry = self.recorder.monitor(decision_id=decision_id or request_id, drift=False, caused_by=caused_by,
                                      outcome="approval_requested", request_id=request_id, action=action.action_type,
                                      target=action.target, risk=action.risk, binding_digest=record.binding_digest,
                                      expires_at=record.expires_at)
        record = replace(record, ledger_request_entry=entry.entry_id)
        self.store.insert(record)
        return record

    # -- decisions -----------------------------------------------------------------------------------
    def _authorise(self, request_id: str, credential: object) -> tuple[ApprovalRecord, Identity]:
        if not isinstance(request_id, str) or not _REQ_RE.match(request_id):
            raise ApprovalDenied("not_found")
        record = self.store.get(request_id)
        if record is None:
            raise ApprovalDenied("not_found")
        identity = self.auth.authenticate(credential)
        if identity is None:
            self._note_attempt(record, "unauthenticated", None)
            raise ApprovalDenied("unauthenticated")
        spec = ACTIONS.get(record.action_type)
        if spec is None or not is_permitted(identity.roles, spec.permission):
            self._note_attempt(record, "not_permitted", identity.subject)
            raise ApprovalDenied("not_permitted")
        return record, identity

    def _note_attempt(self, record: ApprovalRecord, code: str, subject: str | None) -> None:
        try:
            self.recorder.monitor(decision_id=record.request_id, drift=False, caused_by=record.ledger_request_entry,
                                  outcome="approval_attempt_refused", request_id=record.request_id, code=code,
                                  subject=_clean(subject or "", 64))
        except GovernanceLedgerError:
            pass  # the refusal itself already happened; failing to log it must not turn into an approval

    def approve(self, request_id: str, credential: object, reason: str = "") -> ApprovalRecord:
        return self._decide(request_id, credential, reason, approve=True)

    def reject(self, request_id: str, credential: object, reason: str = "") -> ApprovalRecord:
        return self._decide(request_id, credential, reason, approve=False)

    def _decide(self, request_id: str, credential: object, reason: str, *, approve: bool) -> ApprovalRecord:
        record, identity = self._authorise(request_id, credential)
        if record.binding_digest != record.recomputed_digest():
            self._note_attempt(record, "binding_mismatch", identity.subject)
            raise ApprovalDenied("binding_mismatch")
        now = self.clock()
        if record.status == PENDING and _ts(now) >= record.expires_at:
            self.store.update_where(request_id, {"status": EXPIRED}, "status = ?", (PENDING,))
            self._note_attempt(record, "expired", identity.subject)
            raise ApprovalDenied("expired")
        if record.status != PENDING:
            self._note_attempt(record, f"not_pending:{record.status}", identity.subject)
            raise ApprovalDenied("not_pending", record.status)
        decided_at = _ts(now)
        new_status = APPROVED if approve else REJECTED
        fields: dict[str, Any] = {"status": new_status, "decided_by": identity.subject, "decided_at": decided_at,
                                  "decision_reason": _clean(reason, 300)}
        if approve:
            fields["seal"] = self.signer.seal(request_id=request_id, digest=record.binding_digest,
                                              approver=identity.subject, decided_at=decided_at,
                                              expires_at=record.expires_at, decision="approved")
        if not self.store.update_where(request_id, fields, "status = ? AND expires_at > ?", (PENDING, decided_at)):
            raise ApprovalDenied("not_pending", "changed concurrently")
        try:
            entry = self.recorder.monitor(
                decision_id=request_id, drift=False, caused_by=record.ledger_request_entry,
                outcome="approval_granted" if approve else "approval_rejected", request_id=request_id,
                approver=identity.subject, binding_digest=record.binding_digest,
                decision_reason=_clean(reason, 300))
            if not self.store.update_where(request_id, {"ledger_decision_entry": entry.entry_id}, "status = ?", (new_status,)):
                raise GovernanceLedgerError("could not link the decision to its ledger entry")
        except GovernanceLedgerError as exc:
            if approve:  # best-effort revert; if it fails the approval stays unusable (no ledger_decision_entry)
                self.store.update_where(request_id, {"status": PENDING, "seal": None, "decided_by": None, "decided_at": None},
                                       "status = ? AND ledger_decision_entry IS NULL", (APPROVED,))
            raise ApprovalDenied("ledger_error", type(exc).__name__) from exc
        updated = self.store.get(request_id)
        assert updated is not None
        return updated

    def cancel(self, request_id: str, credential: object, reason: str = "") -> ApprovalRecord:
        record, identity = self._authorise(request_id, credential)
        if record.status not in (PENDING, APPROVED):
            raise ApprovalDenied("not_cancellable", record.status)
        if not self.store.update_where(request_id, {"status": CANCELLED, "decided_by": identity.subject,
                                                    "decision_reason": _clean(reason, 300)},
                                       "status IN (?, ?)", (PENDING, APPROVED)):
            raise ApprovalDenied("not_cancellable", "changed concurrently")
        self.recorder.monitor(decision_id=request_id, drift=False, caused_by=record.ledger_request_entry,
                              outcome="approval_cancelled", request_id=request_id, approver=identity.subject)
        return self.store.get(request_id)  # type: ignore[return-value]

    # -- verification (read-only) ----------------------------------------------------------------------
    def check(self, request_id: str) -> ApprovalCheck:
        record = self.store.get(request_id) if isinstance(request_id, str) and _REQ_RE.match(request_id) else None
        if record is None:
            return ApprovalCheck("missing", "no such request")
        if record.binding_digest != record.recomputed_digest():
            return ApprovalCheck("invalid", "binding_mismatch", record)
        if validate_action_safely(record) is None:
            return ApprovalCheck("invalid", "action_no_longer_valid", record)
        now = _ts(self.clock())
        simple = {PENDING: "pending", REJECTED: "rejected", CANCELLED: "cancelled", CONSUMED: "consumed",
                  EXPIRED: "expired"}
        if record.status in (PENDING, APPROVED) and now >= record.expires_at:
            return ApprovalCheck("expired", "past expires_at", record)
        if record.status in simple:
            return ApprovalCheck(simple[record.status], record.status, record)
        if record.status != APPROVED:
            return ApprovalCheck("invalid", "unknown_status", record)
        if not record.decided_by or not record.decided_at or not self.signer.verify(
                record.seal, request_id=record.request_id, digest=record.binding_digest, approver=record.decided_by,
                decided_at=record.decided_at, expires_at=record.expires_at, decision="approved"):
            return ApprovalCheck("invalid", "seal_invalid", record)
        if not record.ledger_decision_entry:
            return ApprovalCheck("invalid", "decision_not_recorded", record)
        spec = ACTIONS[record.action_type]
        if not is_permitted(self.auth.current_roles(record.decided_by), spec.permission):
            return ApprovalCheck("invalid", "approver_no_longer_permitted", record)
        try:
            if self._terminal_since(record.ledger_request_entry):
                return ApprovalCheck("invalid", "governance_state_changed_since_request", record)
        except GovernanceLedgerError:
            return ApprovalCheck("invalid", "ledger_unreadable", record)
        return ApprovalCheck("approved_valid", "ok", record)

    def _terminal_since(self, request_entry_id: str | None) -> bool:
        """True if a TERMINAL ledger entry was written after the request (ordered by chain sequence)."""
        entries = self.recorder._entries()  # noqa: SLF001 - same package; one read of the chain
        anchor = next((e.sequence for e in entries if e.entry_id == request_entry_id), None)
        if anchor is None:
            return True  # the request is not in the ledger: not provably current, so unusable
        return any(e.entry_type.value == "TERMINAL" and e.sequence > anchor for e in entries)

    def consume(self, request_id: str, decision_id: str, expected_digest: str) -> ApprovalRecord:
        """Atomically spend an approval. Raises ``ApprovalDenied`` unless this caller won the single use."""
        verdict = self.check(request_id)
        if verdict.state != "approved_valid" or verdict.record is None:
            raise ApprovalDenied(verdict.state, verdict.reason)
        if not hmac.compare_digest(verdict.record.binding_digest, expected_digest):
            raise ApprovalDenied("binding_mismatch", "approval is for a different action")
        now = _ts(self.clock())
        won = self.store.update_where(
            request_id, {"status": CONSUMED, "consumed_at": now, "consumed_by_decision": _clean(decision_id, 64)},
            "status = ? AND expires_at > ? AND ledger_decision_entry IS NOT NULL AND binding_digest = ?",
            (APPROVED, now, expected_digest))
        if not won:
            raise ApprovalDenied("consumed", "approval already used or no longer valid")
        return self.store.get(request_id)  # type: ignore[return-value]


def validate_action_safely(record: ApprovalRecord) -> ValidatedAction | None:
    """Re-validate the stored action against the current allowlist and schemas (None if no longer acceptable)."""
    try:
        action = validate_action(record.action_type, record.parameters)
    except ActionRejected:
        return None
    return action if action.target == record.target else None
