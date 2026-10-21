"""Pure view-model logic for the VerbaLedger Explorer (no Streamlit import).

Rules enforced here: only recorded fields are shown (nothing is invented or defaulted); recorded causality is kept
apart from chronology; an incomplete view or trace is always labelled; cryptographic integrity and the semantic audit
are never merged into one verdict.
"""

from __future__ import annotations

from typing import Any

ENTRY_TYPES = ("MONITOR", "PRE_NODE", "VERIFICATION", "TERMINAL", "SPECIFICATION_UPDATE", "HUMAN_AUTHORISED_TRANSITION",
               "RE_ENABLEMENT")
PAGE_SIZES = (25, 50, 100, 200)

# (label, entry types it applies to or None for all, payload key). Shown only when the key is actually recorded.
VSL_FIELDS: tuple[tuple[str, frozenset[str] | None, str], ...] = (
    ("PreNode decision", frozenset({"PRE_NODE"}), "decision"),
    ("PreNode result", frozenset({"PRE_NODE"}), "result"),
    ("Invariant", None, "invariant"),
    ("Verification result", frozenset({"VERIFICATION"}), "result"),
    ("Terminal state", frozenset({"TERMINAL"}), "terminal_state"),
    ("Specification update", frozenset({"SPECIFICATION_UPDATE"}), "specification"),
    ("Human authorisation by", frozenset({"HUMAN_AUTHORISED_TRANSITION", "RE_ENABLEMENT"}), "authorised_by"),
    ("Approved by", None, "approved_by"),
    ("Approver", None, "approver"),
    ("Outcome", frozenset({"MONITOR"}), "outcome"),
    ("Action", None, "action"),
    ("Denial reason", None, "denial_reason"),
    ("Reason code", None, "reason_code"),
    ("Reason", None, "reason"),
    ("Decision reason", None, "decision_reason"),
    ("Execution status", None, "execution_status"),
)


def banner(meta: dict[str, Any]) -> tuple[str, list[str]]:
    """``(tone, lines)``: how complete and how current this view is."""
    lines = [f"View as of {meta.get('as_of')} (fingerprint {meta.get('fingerprint')}); "
             + ("served from the snapshot cache - use Refresh for a re-read." if meta.get("cached") else "freshly read.")]
    problems: list[str] = []
    if meta.get("malformed_count"):
        bad = ", ".join(f"line {m['line']} ({m['reason']})" for m in (meta.get("malformed") or [])[:5])
        problems.append(f"INCOMPLETE: {meta['malformed_count']} line(s) could not be read and are NOT shown: {bad}.")
    if meta.get("scan_truncated"):
        problems.append("INCOMPLETE: the scan limit was reached; later records are not shown.")
    if meta.get("changed_during_read"):
        problems.append("The ledger changed while it was being read; refresh to get a consistent view.")
    if meta.get("unreadable"):
        problems.append("INCOMPLETE: " + str(meta["unreadable"]))
    return ("warning" if problems else "info"), problems + lines


def event_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"seq": i.get("sequence"), "time": i.get("time"), "type": i.get("entry_type"), "outcome": i.get("outcome") or "",
             "decision_id": i.get("decision_id") or "", "incident": i.get("incident_id") or "", "request": i.get("request_id") or "",
             "action": i.get("action") or "", "rule": i.get("rule_id") or "", "entry_id": i.get("entry_id")} for i in items]


def vsl_details(event: dict[str, Any]) -> list[tuple[str, str]]:
    """Only fields that are actually present in this record's payload. Nothing is defaulted or inferred."""
    payload = event.get("payload") or {}
    kind = event.get("entry_type")
    out: list[tuple[str, str]] = []
    for label, types, key in VSL_FIELDS:
        if (types is None or kind in types) and key in payload and payload[key] not in (None, ""):
            out.append((label, str(payload[key])))
    if kind == "TERMINAL" and payload.get("terminal_state") == "automation_suspended":
        out.append(("Meaning", "Automation was suspended by this record. Only a human-authorised transition recorded in the ledger lifts it."))
    return out


def trace_dot(trace: dict[str, Any]) -> str:
    """Graphviz DOT for the recorded cause -> effect links. Chronology plays no part in the edges."""
    uncertain = set(trace.get("uncertain_attempts") or [])
    lines = ["digraph trace {", "  rankdir=LR;", '  node [shape=box, fontsize=10];']
    for n in trace.get("nodes", []):
        label = f"#{n['sequence']} {n['entry_type']}\\n{(n.get('outcome') or n.get('result') or n.get('terminal_state') or '')[:28]}"
        style = ', color="#b45309", penwidth=2' if n["entry_id"] in uncertain else ""
        lines.append(f'  "{n["entry_id"]}" [label="{label}"{style}];')
    for g in trace.get("missing_links", []):
        lines.append(f'  "missing:{g["missing_cause"]}" [label="missing record\\n{g["missing_cause"][:12]}", style=dashed, color="#b91c1c"];')
        lines.append(f'  "missing:{g["missing_cause"]}" -> "{g["entry_id"]}" [style=dashed, color="#b91c1c", label="caused_by (record absent)"];')
    for e in trace.get("edges", []):
        lines.append(f'  "{e["cause"]}" -> "{e["effect"]}" [label="caused_by"];')
    lines.append("}")
    return "\n".join(lines)


def trace_flags(trace: dict[str, Any]) -> list[tuple[str, str]]:
    flags: list[tuple[str, str]] = []
    if trace.get("missing_links"):
        flags.append(("error", f"{len(trace['missing_links'])} missing link(s): a record names a cause that is not in the ledger."))
    if trace.get("uncertain_attempts"):
        flags.append(("error", "UNCERTAIN recovery outcome: an attempt is recorded with no result recorded for it."))
    if trace.get("capped"):
        flags.append(("warning", "The trace was capped; some related records are not shown."))
    if trace.get("incomplete") and not flags:
        flags.append(("warning", "The trace may be incomplete because the ledger view is incomplete."))
    if not trace.get("edges") and len(trace.get("nodes", [])) > 1:
        flags.append(("info", "These records share an identifier but have no recorded cause links between them."))
    return flags


def integrity_view(result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("status")
    tone = {"verified": "success", "failed": "error"}.get(status, "warning")
    heading = {"verified": "Hash chain verified", "failed": "Hash chain verification FAILED",
               "could_not_complete": "Verification could NOT complete"}.get(status, "Unknown verification state")
    lines = [f"Records examined: {result.get('records_examined')} (lines scanned: {result.get('lines_scanned')})",
             f"Verified at: {result.get('verified_at')}"]
    ff = result.get("first_failure")
    if ff:
        lines.append(f"First failing record: line {ff.get('line')}, sequence {ff.get('sequence')}, entry {ff.get('entry_id')}: "
                     f"{ff.get('kind')} - {ff.get('reason')}")
    lines.extend(result.get("errors") or [])
    return {"tone": tone, "heading": heading, "lines": lines, "limits": result.get("limits") or [],
            "checkpoint": result.get("checkpoint")}


def audit_view(result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("status")
    tone = {"passed": "success", "failed": "error"}.get(status, "warning")
    heading = {"passed": "Semantic audit passed", "failed": "Semantic audit FAILED",
               "could_not_complete": "Semantic audit could NOT complete"}.get(status, "Unknown audit state")
    rows = [{"check": c["title"], "name": c["name"], "result": c["status"].replace("_", " "), "examined": c.get("examined"),
             "violations": len(c.get("violations") or [])} for c in result.get("checks", [])]
    return {"tone": tone, "heading": heading, "rows": rows, "error": result.get("error"),
            "integrity_note": ("Hash-chain result reported by the audit tool: "
                               + {True: "intact", False: "NOT intact", None: "unknown"}[result.get("integrity_passed")]
                               + ". This is separate from the semantic checks; see the Integrity page."),
            "notes": result.get("notes") or []}


# ------------------------------------------------------------------------------------------------ Phase 11 additions
# Lifecycle stages shown on a trace. A stage is "recorded" only when a ledger record of the matching kind exists in the
# trace; otherwise it is reported as NOT RECORDED. Absence is never papered over, and nothing is inferred from time order.
# The first three stages happen in the operations store (incidents, investigations), not in the governance ledger, so the
# ledger by itself can never show them: the view says so and points to the Operations Dashboard.
LIFECYCLE: tuple[tuple[str, str, frozenset[str], frozenset[str]], ...] = (
    ("incident_detected", "Incident detected", frozenset(), frozenset()),
    ("investigation_started", "Investigation started", frozenset(), frozenset()),
    ("evidence_gathered", "Evidence gathered", frozenset(), frozenset()),
    ("governance_evaluated", "Governance evaluated", frozenset({"recovery_proposed", "authorised", "action_rejected", "refused_suspended",
                                                               "recovery_refused_suspended", "invariant_violated"}), frozenset({"PRE_NODE"})),
    ("human_review_requested", "Human review requested", frozenset({"approval_requested", "routed_to_human_review"}), frozenset()),
    ("human_decision", "Human decision", frozenset({"approval_granted", "denied_human_review", "approval_cancelled", "approval_attempt_refused"}),
     frozenset({"HUMAN_AUTHORISED_TRANSITION"})),
    ("recovery_proposal", "Recovery proposal", frozenset({"recovery_proposed"}), frozenset()),
    ("authorised_execution", "Authorised execution", frozenset({"authorised", "recovery_attempt", "executed", "recovery_succeeded"}), frozenset()),
    ("verification", "Verification", frozenset(), frozenset({"VERIFICATION"})),
)
LEDGER_EXTERNAL = {"incident_detected": "Recorded in the operations store, not in the governance ledger.",
                   "investigation_started": "Recorded in the operations store, not in the governance ledger.",
                   "evidence_gathered": "Evidence references are cited in recovery/approval records; the evidence itself is in the investigation report."}


def lifecycle_stages(trace: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = trace.get("nodes") or []
    out = []
    for key, label, outcomes, types in LIFECYCLE:
        hits = [n for n in nodes if n.get("outcome") in outcomes or n.get("entry_type") in types] if (outcomes or types) else []
        out.append({"stage": key, "label": label, "recorded": bool(hits),
                    "events": [{"entry_id": n["entry_id"], "sequence": n.get("sequence"), "outcome": n.get("outcome") or n.get("entry_type"), "time": n.get("time")} for n in hits],
                    "note": None if hits else LEDGER_EXTERNAL.get(key, "No matching record is present in this trace.")})
    return out


def overview_cards(summary: dict[str, Any]) -> list[tuple[str, Any, str]]:
    d = summary.get("decisions") or {}
    return [("Total events", summary.get("total_records"), "readable ledger records"),
            ("Latest event", (summary.get("last_time") or "-")[:16].replace("T", " ") or "-", "UTC (date and time of the newest ledger record)"),
            ("Allowed", d.get("allowed", 0), "authorised / approval granted outcomes"),
            ("Denied", d.get("denied", 0), "rejected / refused / invalid outcomes"),
            ("Suspended", d.get("suspended", 0), "refused because automation was suspended"),
            ("Human review", d.get("human_review", 0), "approval requested, decided or refused"),
            ("Recovery", summary.get("recovery_related", 0), "recovery attempts, results and recovery refusals (outcome names starting 'recovery_')")]


def latest_event_text(summary: dict[str, Any]) -> str | None:
    ev = summary.get("latest_event")
    if not ev:
        return None
    bits = [f"#{ev.get('sequence')}", str(ev.get("entry_type")), ev.get("outcome") or ev.get("action") or "", f"incident {ev['incident_id']}" if ev.get("incident_id") else ""]
    return f"Latest recorded event: {' '.join(b for b in bits if b)} at {(ev.get('time') or '-')[:19].replace('T', ' ')} UTC"


def incident_pairs(summary: dict[str, Any]) -> list[tuple[str, int]]:
    return sorted((summary.get("by_incident") or {}).items(), key=lambda kv: (-kv[1], kv[0]))


def day_pairs(summary: dict[str, Any]) -> list[tuple[str, int]]:
    return sorted((summary.get("by_day") or {}).items())
