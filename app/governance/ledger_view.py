"""Read-only view of the governance ledger for the VerbaLedger Explorer API (Phase 10).

This module only ever *reads* the JSONL file. It never creates, appends to, repairs, truncates or locks it
(``JsonlLedgerStore`` is deliberately not used here because its constructor creates a missing file). It adds:

* a bounded, tolerant scan: malformed or partially written lines are counted and listed, never skipped silently, and
  the view reports ``complete = False`` when anything was unreadable or the scan limits were hit;
* a compact in-memory snapshot cached per file *fingerprint* (size + mtime). Any change to the file invalidates it, an
  explicit ``refresh`` rebuilds it, and every answer carries ``as_of`` so a stale view is never presented as current;
* causal tracing that follows ONLY the recorded ``caused_by`` links (and shared ``decision_id`` values, labelled as
  such). It never infers causality from timestamps;
* an on-demand integrity pass that reports the first failing record. Integrity and audit results are never cached.

Limits that the API repeats to the user: a valid hash chain cannot show that trailing events were truncated, that an
older consistent chain was substituted, or that the recorded events are semantically correct.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from vsl_core.ledger import GENESIS_HASH, LedgerEntry, VerbaLedger, _compute_entry_hash

DEFAULT_MAX_SCAN_LINES = 200_000
DEFAULT_MAX_SCAN_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = 256 * 1024
MALFORMED_SAMPLE = 20
MAX_TRACE_NODES = 500
MAX_PAYLOAD_BYTES = 16 * 1024
MAX_STRING = 2000
MAX_DEPTH = 6

_SECRET_KEY = re.compile(r"token|secret|password|passwd|credential|authorization|api[_-]?key|private|signature|signing", re.I)
_REQUIRED = ("entry_id", "entry_type", "sequence", "timestamp", "entry_hash", "prev_hash", "payload")

INTEGRITY_LIMITS = (
    "A valid hash chain proves only that the records present are internally consistent and in order.",
    "It cannot detect events removed from the END of the file (truncation), or replacement of the whole file by an "
    "older, internally consistent chain; an externally held checkpoint (sequence + hash) is needed for that.",
    "It says nothing about whether the recorded events are semantically correct; that is the separate semantic audit.",
)


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------------------------------- scanning
@dataclass
class _ScanState:
    lines: int = 0
    bytes_read: int = 0
    truncated: bool = False
    unreadable: str | None = None


@dataclass(frozen=True)
class Record:
    entry: LedgerEntry
    line: int

    @property
    def payload(self) -> dict[str, Any]:
        return self.entry.payload if isinstance(self.entry.payload, dict) else {}


@dataclass
class Snapshot:
    fingerprint: str
    records: list[Record]
    malformed: list[dict[str, Any]]
    malformed_count: int
    lines_scanned: int
    truncated: bool
    as_of: str
    stable: bool = True
    unreadable: str | None = None
    by_id: dict[str, Record] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)
    by_decision: dict[str, list[str]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.malformed_count == 0 and not self.truncated and self.unreadable is None and self.stable

    def meta(self, *, cached: bool = False) -> dict[str, Any]:
        return {"as_of": self.as_of, "fingerprint": self.fingerprint, "complete": self.complete, "cached": cached,
                "records": len(self.records), "lines_scanned": self.lines_scanned, "scan_truncated": self.truncated,
                "malformed_count": self.malformed_count, "malformed": self.malformed[:MALFORMED_SAMPLE],
                "changed_during_read": not self.stable, "unreadable": self.unreadable}


def _lines(path: Path, state: _ScanState, max_lines: int, max_bytes: int) -> Iterator[tuple[int, bytes, bool]]:
    """Yield ``(line_no, raw, terminated)`` for each non-empty line, within the scan limits. Read-only."""
    try:
        handle = path.open("rb")
    except OSError as exc:
        state.unreadable = f"The ledger file could not be opened ({type(exc).__name__})."
        return
    with handle:
        line_no = 0
        try:
            for raw in handle:
                line_no += 1
                state.bytes_read += len(raw)
                if line_no > max_lines or state.bytes_read > max_bytes:
                    state.truncated = True
                    return
                state.lines = line_no
                if raw.strip():
                    yield line_no, raw, raw.endswith(b"\n")
        except OSError as exc:
            state.unreadable = f"The ledger file could not be read to the end ({type(exc).__name__})."


def _parse(raw: bytes, terminated: bool) -> tuple[LedgerEntry | None, str | None]:
    """``(entry, None)`` or ``(None, reason)``. Never raises."""
    if len(raw) > MAX_LINE_BYTES:
        return None, "line exceeds the size limit"
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "invalid JSON" + ("" if terminated else " (final line has no newline; it may be mid-write)")
    if not isinstance(obj, dict):
        return None, "not a JSON object"
    missing = [k for k in _REQUIRED if k not in obj]
    if missing:
        return None, "missing fields: " + ", ".join(missing)
    if not isinstance(obj["payload"], dict) or isinstance(obj["sequence"], bool) or not isinstance(obj["sequence"], int) \
            or not isinstance(obj["entry_id"], str) or not isinstance(obj["timestamp"], (int, float)):
        return None, "fields have the wrong type"
    try:
        return LedgerEntry.from_dict(obj), None
    except (KeyError, TypeError, ValueError):
        return None, "not a valid ledger entry"


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


class LedgerReader:
    """Reads one ledger file. Holds at most one cached snapshot; the path is never exposed."""

    def __init__(self, path: Path, max_lines: int | None = None, max_bytes: int | None = None) -> None:
        self._path = Path(path)
        self.max_lines = max_lines or _env_int("LEDGER_MAX_SCAN_LINES", DEFAULT_MAX_SCAN_LINES)
        self.max_bytes = max_bytes or _env_int("LEDGER_MAX_SCAN_BYTES", DEFAULT_MAX_SCAN_BYTES)
        self._lock = threading.Lock()
        self._cached: tuple[tuple[int, int], Snapshot] | None = None

    def available(self) -> bool:
        return self._path.is_file()

    def snapshot(self, *, refresh: bool = False) -> tuple[Snapshot, bool]:
        """``(snapshot, served_from_cache)``. Raises FileNotFoundError if the file does not exist (never creates it)."""
        before = _fingerprint(self._path)
        if before is None or not self._path.is_file():
            raise FileNotFoundError
        with self._lock:
            if not refresh and self._cached and self._cached[0] == before:
                return self._cached[1], True
            snap = self._scan(before)
            after = _fingerprint(self._path)
            snap.stable = after == before
            self._cached = (before, snap) if snap.stable else None
            return snap, False

    def _scan(self, fingerprint: tuple[int, int]) -> Snapshot:
        state = _ScanState()
        records: list[Record] = []
        malformed: list[dict[str, Any]] = []
        count = 0
        for line_no, raw, terminated in _lines(self._path, state, self.max_lines, self.max_bytes):
            entry, reason = _parse(raw, terminated)
            if entry is None:
                count += 1
                if len(malformed) < 200:
                    malformed.append({"line": line_no, "reason": reason})
            else:
                records.append(Record(entry, line_no))
        snap = Snapshot(
            fingerprint=hashlib.sha256(f"{fingerprint[0]}:{fingerprint[1]}".encode()).hexdigest()[:16],
            records=records, malformed=malformed, malformed_count=count, lines_scanned=state.lines,
            truncated=state.truncated, as_of=_now_iso(), unreadable=state.unreadable)
        for rec in records:
            e = rec.entry
            snap.by_id.setdefault(e.entry_id, rec)
            if e.caused_by:
                snap.children.setdefault(e.caused_by, []).append(e.entry_id)
            if e.decision_id:
                snap.by_decision.setdefault(e.decision_id, []).append(e.entry_id)
        return snap

    # ------------------------------------------------------------------------------- integrity (never cached)
    def verify(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "could_not_complete", "verified_at": _now_iso(), "records_examined": 0, "lines_scanned": 0,
            "first_failure": None, "errors": [], "checkpoint": None, "limits": list(INTEGRITY_LIMITS)}
        fp = _fingerprint(self._path)
        if fp is None or not self._path.is_file():
            result["errors"].append("The ledger file is not available.")
            return result
        state = _ScanState()
        expected_seq, expected_prev = 0, GENESIS_HASH
        failure: dict[str, Any] | None = None
        last_ok: LedgerEntry | None = None
        partial_tail = False
        for line_no, raw, terminated in _lines(self._path, state, self.max_lines, self.max_bytes):
            entry, reason = _parse(raw, terminated)
            if entry is None:
                partial_tail = not terminated
                failure = {"line": line_no, "sequence": None, "entry_id": None, "kind": "unparseable_record", "reason": reason}
                break
            problem = None
            if entry.sequence != expected_seq:
                problem = ("sequence_gap", f"expected sequence {expected_seq}, found {entry.sequence}")
            elif entry.prev_hash != expected_prev:
                problem = ("prev_hash_mismatch", "the record does not chain to the previous record's hash")
            elif _compute_entry_hash(entry) != entry.entry_hash:
                problem = ("entry_hash_mismatch", "the stored hash does not match the record's contents")
            if problem:
                failure = {"line": line_no, "sequence": entry.sequence, "entry_id": entry.entry_id,
                           "kind": problem[0], "reason": problem[1]}
                break
            result["records_examined"] += 1
            expected_seq, expected_prev, last_ok = expected_seq + 1, entry.entry_hash, entry
        result["lines_scanned"] = state.lines
        if last_ok is not None:
            result["checkpoint"] = {"sequence": last_ok.sequence, "entry_hash": last_ok.entry_hash}
        if state.unreadable:
            result["errors"].append(state.unreadable)
            return result
        if failure and partial_tail:
            result["first_failure"] = failure
            result["errors"].append("The final line is incomplete; it may still be being written. Re-run shortly.")
            return result
        if failure:
            result.update(status="failed", first_failure=failure)
            return result
        if state.truncated:
            result["errors"].append("The scan limit was reached before the end of the ledger; verification is incomplete.")
            return result
        if _fingerprint(self._path) != fp:
            result["errors"].append("The ledger changed while it was being verified; run the verification again.")
            return result
        # Cross-check with the library's own verifier (read-only: it only reads the store).
        try:
            from vsl_core.ledger import JsonlLedgerStore

            library_ok = VerbaLedger(JsonlLedgerStore(self._path)).verify_integrity()
        except (OSError, ValueError, KeyError, TypeError) as exc:  # reported, never raised (a read-only path; not a broad handler)
            result["errors"].append(f"The library verifier could not run ({type(exc).__name__}).")
            return result
        if not library_ok:
            result.update(status="failed", first_failure={"line": None, "sequence": None, "entry_id": None,
                          "kind": "library_verification_failed", "reason": "the library verifier rejected the chain"})
            return result
        result["status"] = "verified"
        return result


_READERS: dict[str, LedgerReader] = {}
_READERS_LOCK = threading.Lock()


def reader_for(path: Path) -> LedgerReader:
    """One reader (and one cached snapshot) per ledger path in this process."""
    key = str(Path(path))
    with _READERS_LOCK:
        reader = _READERS.get(key)
        if reader is None:
            reader = _READERS[key] = LedgerReader(Path(path))
        return reader


# ----------------------------------------------------------------------------------------------- shaping
def safe_payload(value: Any, depth: int = 0) -> Any:
    """Redact secret-like keys and bound strings, depth and size. The payload is data, never instructions."""
    if depth >= MAX_DEPTH:
        return "[truncated: nesting too deep]"
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if _SECRET_KEY.search(str(k)) else safe_payload(v, depth + 1))
                for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [safe_payload(v, depth + 1) for v in list(value)[:100]]
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "...[truncated]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:200]


def bounded_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    cleaned = safe_payload(payload)
    if len(json.dumps(cleaned, default=str)) <= MAX_PAYLOAD_BYTES:
        return cleaned, False
    kept: dict[str, Any] = {}
    size = 0
    for key, val in cleaned.items():
        chunk = len(json.dumps({key: val}, default=str))
        if size + chunk > MAX_PAYLOAD_BYTES:
            break
        kept[key], size = val, size + chunk
    return kept, True


def _first(*values: Any) -> Any:
    for v in values:
        if v not in (None, ""):
            return v
    return None


def summarise(rec: Record) -> dict[str, Any]:
    e, p = rec.entry, rec.payload
    request_id = _first(p.get("request_id"), e.decision_id if (e.decision_id or "").startswith("apr-") else None)
    return {
        "entry_id": e.entry_id, "sequence": e.sequence, "line": rec.line, "timestamp": e.timestamp, "time": iso(e.timestamp),
        "entry_type": e.entry_type.value, "decision_id": e.decision_id, "caused_by": e.caused_by,
        "outcome": _str(p.get("outcome")), "incident_id": _str(_first(p.get("incident"), p.get("incident_id"))),
        "request_id": _str(request_id), "action": _str(p.get("action")), "rule_id": _str(p.get("invariant")),
        "result": _str(p.get("result")), "terminal_state": _str(p.get("terminal_state")),
    }


def _str(value: Any) -> str | None:
    return None if value is None else str(value)[:200]


def detail(rec: Record) -> dict[str, Any]:
    payload, truncated = bounded_payload(rec.payload)
    e = rec.entry
    return {**summarise(rec), "instance_id": e.instance_id, "identity_key": e.identity_key, "cluster_key": e.cluster_key,
            "schema_version": e.schema_version, "prev_hash": e.prev_hash, "entry_hash": e.entry_hash,
            "payload": payload, "payload_truncated": truncated}


@dataclass(frozen=True)
class Filters:
    from_time: float | None = None
    to_time: float | None = None
    entry_type: str | None = None
    decision_id: str | None = None
    incident_id: str | None = None
    request_id: str | None = None
    outcome: str | None = None
    action: str | None = None
    rule_id: str | None = None
    q: str | None = None  # case-insensitive substring over ids, outcome, action, rule, type


def _matches(s: dict[str, Any], f: Filters) -> bool:
    if f.from_time is not None and s["timestamp"] < f.from_time:
        return False
    if f.to_time is not None and s["timestamp"] > f.to_time:
        return False
    for attr, key in (("entry_type", "entry_type"), ("decision_id", "decision_id"), ("incident_id", "incident_id"),
                      ("request_id", "request_id"), ("outcome", "outcome"), ("action", "action"), ("rule_id", "rule_id")):
        want = getattr(f, attr)
        if want is not None and s[key] != want:
            return False
    if f.q:
        needle = f.q.lower()
        haystack = " ".join(str(s.get(k) or "") for k in ("entry_id", "decision_id", "incident_id", "request_id", "outcome", "action",
                                                       "rule_id", "entry_type", "terminal_state", "result"))
        if needle not in haystack.lower():
            return False
    return True


def query(snap: Snapshot, f: Filters, *, limit: int, offset: int, descending: bool) -> tuple[list[dict[str, Any]], int]:
    """Filtered, ordered by ledger sequence. Returns ``(page, total_matching)``."""
    ordered = sorted(snap.records, key=lambda r: r.entry.sequence, reverse=descending)
    matched = [s for s in (summarise(r) for r in ordered) if _matches(s, f)]
    return matched[offset:offset + limit], len(matched)


# How recorded outcomes are grouped for the overview. Grouping is by outcome NAME only; classes may overlap, and any outcome
# not named here is counted as ``other_outcomes`` rather than guessed at.
DECISION_CLASSES: dict[str, frozenset[str]] = {
    "allowed": frozenset({"authorised", "approval_granted"}),
    "denied": frozenset({"action_rejected", "approval_invalid", "not_approved", "approval_not_usable", "approval_attempt_refused",
                         "denied_human_review", "invariant_violated", "approval_cancelled"}),
    "suspended": frozenset({"refused_suspended", "recovery_refused_suspended", "terminal_suspended"}),
    "human_review": frozenset({"approval_requested", "approval_granted", "approval_cancelled", "approval_attempt_refused",
                               "approval_not_usable", "routed_to_human_review", "denied_human_review"}),
}


def summary(snap: Snapshot) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    actions: dict[str, int] = {}
    rules: dict[str, int] = {}
    for rec in snap.records:
        s = summarise(rec)
        by_type[s["entry_type"]] = by_type.get(s["entry_type"], 0) + 1
        for bag, key in ((outcomes, "outcome"), (actions, "action"), (rules, "rule_id")):
            if s[key] is not None and (s[key] in bag or len(bag) < 100):
                bag[s[key]] = bag.get(s[key], 0) + 1
    by_incident: dict[str, int] = {}
    for rec in snap.records:
        inc = summarise(rec)["incident_id"]
        if inc is not None and (inc in by_incident or len(by_incident) < 500):
            by_incident[inc] = by_incident.get(inc, 0) + 1
    recovery_related = sum(n for o, n in outcomes.items() if o.startswith("recovery_"))
    latest = summarise(max(snap.records, key=lambda r: r.entry.sequence)) if snap.records else None
    times = [r.entry.timestamp for r in snap.records]
    by_day: dict[str, int] = {}
    for t in times:
        day = iso(t)[:10]
        by_day[day] = by_day.get(day, 0) + 1
    decisions = {name: sum(n for o, n in outcomes.items() if o in members) for name, members in DECISION_CLASSES.items()}
    decisions["human_review"] += by_type.get("HUMAN_AUTHORISED_TRANSITION", 0)
    classified = set().union(*DECISION_CLASSES.values())
    decisions["other_outcomes"] = sum(n for o, n in outcomes.items() if o not in classified)
    return {"total_records": len(snap.records), "by_entry_type": by_type, "by_outcome": outcomes, "by_action": actions,
            "by_rule_id": rules, "first_time": iso(min(times)) if times else None, "last_time": iso(max(times)) if times else None,
            "by_day": dict(sorted(by_day.items())), "decisions": decisions,
            "decision_classes": {k: sorted(v) for k, v in DECISION_CLASSES.items()},
            "recovery_related": recovery_related, "by_incident": dict(sorted(by_incident.items())),
            "latest_event": ({k: latest[k] for k in ("sequence", "time", "entry_type", "outcome", "incident_id", "action")} if latest else None),
            "origin": "real_recorded_events", "synthetic_events": 0}


# ----------------------------------------------------------------------------------------------- causal trace
def uncertain_attempts(snap: Snapshot) -> list[str]:
    """``recovery_attempt`` records with no recorded result caused by them (reuses the Phase 7 audit rule)."""
    from app.governance.audit_cli import recovery_attempts_without_result

    return recovery_attempts_without_result([r.entry for r in snap.records])


def trace(snap: Snapshot, *, entry_id: str | None = None, decision_id: str | None = None,
          request_id: str | None = None) -> dict[str, Any] | None:
    """The recorded cause/effect graph around a seed. ``None`` when nothing matches the seed.

    Edges come only from a record's own ``caused_by``. ``decision_id`` / ``request_id`` only choose the seed set (and
    are reported as ``grouped_by``); they are identifiers, not causes. Chronology is reported separately.
    """
    if entry_id is not None:
        seeds = [entry_id] if entry_id in snap.by_id else []
        grouped_by = {"entry_id": entry_id}
    elif decision_id is not None:
        seeds = list(snap.by_decision.get(decision_id, []))
        grouped_by = {"decision_id": decision_id}
    else:
        seeds = [r.entry.entry_id for r in snap.records
                 if summarise(r)["request_id"] == request_id or r.entry.decision_id == request_id]
        grouped_by = {"request_id": request_id}
    if not seeds:
        return None
    members: set[str] = set()
    frontier = list(seeds)
    capped = False
    while frontier:
        current = frontier.pop()
        if current in members:
            continue
        if len(members) >= MAX_TRACE_NODES:
            capped = True
            break
        members.add(current)
        rec = snap.by_id.get(current)
        if rec and rec.entry.caused_by and rec.entry.caused_by in snap.by_id:
            frontier.append(rec.entry.caused_by)
        frontier.extend(snap.children.get(current, []))
    nodes = sorted((snap.by_id[m] for m in members), key=lambda r: r.entry.sequence)
    edges, gaps, roots = [], [], []
    for rec in nodes:
        cause = rec.entry.caused_by
        if not cause:
            roots.append(rec.entry.entry_id)
        elif cause in members:
            edges.append({"cause": cause, "effect": rec.entry.entry_id, "basis": "caused_by"})
        else:
            gaps.append({"entry_id": rec.entry.entry_id, "missing_cause": cause,
                         "reason": "caused_by names a record that is not in the ledger" if not snap.truncated and snap.malformed_count == 0
                         else "caused_by names a record that was not found; it may lie in unreadable or unscanned data"})
    uncertain = sorted(set(uncertain_attempts(snap)) & members)
    notes = ["Edges are recorded caused_by links only. Chronology below is time order and is not causal evidence."]
    if not snap.complete:
        notes.append("The ledger view is incomplete, so absent links may exist in data that could not be read.")
    if capped:
        notes.append(f"The trace was capped at {MAX_TRACE_NODES} records.")
    return {
        "grouped_by": grouped_by, "nodes": [summarise(r) for r in nodes], "edges": edges, "roots": roots,
        "missing_links": gaps, "uncertain_attempts": uncertain,
        "chronology": [r.entry.entry_id for r in sorted(nodes, key=lambda r: (r.entry.timestamp, r.entry.sequence))],
        "incomplete": bool(gaps or capped or not snap.complete or uncertain), "capped": capped, "notes": notes,
    }


# ----------------------------------------------------------------------------------------------- semantic audit
AUDIT_CHECKS: dict[str, tuple[str, str]] = {
    "no_monitoring_gaps": ("Monitoring continuity",
                           "Consecutive MONITOR records are no further apart than the supplied threshold. Not run unless a threshold is given."),
    "drift_flagged_monitor_has_pre_node": ("Drift gets a PreNode decision",
                                           "Every MONITOR record flagged drift_detected has a PRE_NODE record that resolves it."),
    "pre_node_has_verification": ("PreNode gets a verification", "Every PRE_NODE record has a VERIFICATION record that resolves it."),
    "insufficient_verification_has_specification_update": (
        "Insufficient verification updates the specification",
        "Every VERIFICATION with result INSUFFICIENT has a SPECIFICATION_UPDATE that resolves it."),
    "terminal_has_human_authorised_transition": (
        "Terminal states need human authorisation",
        "Every TERMINAL record has a HUMAN_AUTHORISED_TRANSITION. An open terminal (a suspension not yet cleared by a human) fails this on purpose."),
    "recovery_attempt_has_result": ("Recovery attempt has a recorded result (Phase 7 extension)",
                                    "Every recovery_attempt record has a recovery result record caused by it. A failure means the outcome is unrecorded and must be treated as uncertain."),
}
AUDIT_NOTES = (
    "This is the SEMANTIC audit: whether the recorded events satisfy the VSL ordering rules. It is separate from "
    "cryptographic integrity (see the integrity verification).",
    "The VSL library matches checks 2-5 by caused_by where it is set and falls back to same-identity time ordering "
    "only where it is not. The causal trace in this explorer never uses that fallback.",
    "A passing audit does not show that events were not removed from the end of the ledger.",
)


def _examined(snap: Snapshot, check: str) -> list[str]:
    def ids(pred) -> list[str]:
        return [r.entry.entry_id for r in snap.records if pred(r)]

    t = lambda r: r.entry.entry_type.value  # noqa: E731
    return {
        "no_monitoring_gaps": ids(lambda r: t(r) == "MONITOR"),
        "drift_flagged_monitor_has_pre_node": ids(lambda r: t(r) == "MONITOR" and r.payload.get("drift_detected")),
        "pre_node_has_verification": ids(lambda r: t(r) == "PRE_NODE"),
        "insufficient_verification_has_specification_update": ids(
            lambda r: t(r) == "VERIFICATION" and r.payload.get("result") == "INSUFFICIENT"),
        "terminal_has_human_authorised_transition": ids(lambda r: t(r) == "TERMINAL"),
        "recovery_attempt_has_result": ids(lambda r: t(r) == "MONITOR" and r.payload.get("outcome") == "recovery_attempt"),
    }[check]


def semantic_audit(reader: LedgerReader, max_gap: float | None) -> dict[str, Any]:
    """Run the existing read-only audit (``audit_cli.run_audit``) and reshape it. The file path is dropped."""
    from app.governance.audit_cli import PHASE7_CHECK, run_audit

    out: dict[str, Any] = {"status": "could_not_complete", "audited_at": _now_iso(), "entries": None,
                           "integrity_passed": None, "checks": [], "vsl": {}, "checkpoint": None,
                           "max_monitor_gap_seconds": max_gap, "error": None, "notes": list(AUDIT_NOTES)}
    try:
        _code, rep = run_audit(reader._path, max_gap)
    except (OSError, ValueError, KeyError, TypeError) as exc:  # reported, never raised (a read-only path; not a broad handler)
        out["error"] = f"The audit could not run ({type(exc).__name__})."
        return out
    out["vsl"] = rep.get("vsl", {})
    if "error" in rep and rep.get("entries") is None:
        out["error"] = str(rep["error"])
        return out
    try:
        snap, _ = reader.snapshot()
    except FileNotFoundError:
        out["error"] = "ledger file not found"
        return out
    out.update(entries=rep.get("entries"), integrity_passed=rep.get("integrity"), checkpoint=rep.get("checkpoint"))
    violations: dict[str, list[str]] = dict(rep.get("violations") or {})
    passed, failed = set(rep.get("checks_passed") or ()), set(rep.get("checks_failed") or ())
    p7 = rep.get("phase7") or {}
    if p7:
        (passed if p7.get("passed") else failed).add(PHASE7_CHECK)
        if not p7.get("passed"):
            violations[PHASE7_CHECK] = list(p7.get("violations") or [])
    for name, (title, explanation) in AUDIT_CHECKS.items():
        if name == "no_monitoring_gaps" and max_gap is None:
            status = "not_run"
        elif name in failed:
            status = "failed"
        elif name in passed:
            status = "passed"
        else:
            status = "not_run"
        ids = violations.get(name, [])
        flat = [part for v in ids for part in str(v).split("->")]
        out["checks"].append({
            "name": name, "title": title, "status": status, "explanation": explanation,
            "examined": len(_examined(snap, name)) if status != "not_run" else None,
            "violations": ids,
            "events": [summarise(snap.by_id[i]) for i in dict.fromkeys(flat) if i in snap.by_id][:50],
        })
    out["status"] = "failed" if any(c["status"] == "failed" for c in out["checks"]) else "passed"
    return out
