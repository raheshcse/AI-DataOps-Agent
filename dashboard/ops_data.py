"""Pure data and view-model logic for the Operations Dashboard (no Streamlit import, so it is unit-testable).

Principles enforced here:
* Missing data is not zero: a metric whose source failed has ``value=None`` and a reason.
* Nothing is invented: charts are built only from the incidents actually loaded, and say how many were loaded.
* A hypothesis or an evidence-supported cause is never labelled confirmed.
* A closed ticket is never evidence of recovery or authorisation.
* "Executed" is never shown as "verified" unless the API reports ``verified_outcome == verified``.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dashboard.http_core import Problem

SEVERITIES = ("critical", "high", "medium", "low")
WORKFLOW_ORDER = ("new", "investigation_pending", "investigation_running", "investigation_completed", "investigation_failed",
                  "awaiting_approval", "recovery_running", "recovery_completed", "recovery_uncertain", "recovery_failed",
                  "recovery_not_executed")
RECOVERY_WORKFLOWS = frozenset(w for w in WORKFLOW_ORDER if w.startswith("recovery_") or w == "awaiting_approval")
SORT_KEYS = {"created_at": "Created", "updated_at": "Updated", "severity": "Severity", "title": "Title",
             "workflow_status": "Workflow status", "incident_id": "Incident id"}
PAGE_SIZES = (10, 25, 50, 100)


# ------------------------------------------------------------------------------------------------ loading
@dataclass
class LoadedIncidents:
    items: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None  # None: unknown (nothing could be loaded)
    problem: Problem | None = None
    truncated: bool = False  # more incidents exist than were loaded
    partial: bool = False  # a later page failed after earlier pages succeeded
    loaded_at: str | None = None

    @property
    def available(self) -> bool:
        return self.total is not None


def load_incidents(client, *, max_items: int = 300, page_size: int = 100) -> LoadedIncidents:
    """Bounded paging through ``GET /incidents``. Never raises; failures are recorded on the result."""
    out = LoadedIncidents(loaded_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    offset = 0
    while offset < max_items:
        res = client.incidents(limit=min(page_size, max_items - offset), offset=offset)
        if not res.ok:
            out.problem = res.problem
            out.partial = bool(out.items)
            if not out.items:
                out.total = None
            return out
        data = res.data if isinstance(res.data, dict) else {}
        items = data.get("items")
        total = data.get("total")
        if not isinstance(items, list) or not isinstance(total, int):
            out.problem = Problem("bad_response", "The incident list did not have the expected shape.")
            out.partial = bool(out.items)
            return out
        out.items.extend(i for i in items if isinstance(i, dict))
        out.total = total
        offset += len(items)
        if not items or offset >= total:
            break
    out.truncated = out.total is not None and len(out.items) < out.total
    return out


# ------------------------------------------------------------------------------------------------ tables
def incident_row(i: dict[str, Any]) -> dict[str, Any]:
    ticket = i.get("ticket") if isinstance(i.get("ticket"), dict) else None
    return {
        "incident_id": i.get("incident_id"), "title": i.get("title") or "", "type": i.get("incident_type"),
        "severity": i.get("severity"), "status": i.get("status"), "workflow_status": i.get("workflow_status"),
        "ticket": (f"#{ticket['number']} {ticket.get('status')}" if ticket and ticket.get("number") else ticket.get("status"))
                  if ticket else "none",
        "created_at": i.get("created_at"), "updated_at": i.get("updated_at"),
    }


def _sev_rank(value: Any) -> int:
    return SEVERITIES.index(value) if value in SEVERITIES else len(SEVERITIES)


@dataclass(frozen=True)
class ViewPage:
    rows: list[dict[str, Any]]
    total_matching: int
    page: int
    page_count: int
    page_size: int


def apply_view(rows: list[dict[str, Any]], *, text: str = "", severities: tuple[str, ...] = (),
               workflows: tuple[str, ...] = (), sort_by: str = "created_at", descending: bool = True,
               page: int = 1, page_size: int = 25) -> ViewPage:
    """Filter, sort and paginate already-loaded rows. Out-of-range pages are clamped; bad sort keys fall back."""
    needle = text.strip().lower()
    out = [r for r in rows
           if (not needle or needle in " ".join(str(r.get(k) or "") for k in ("incident_id", "title", "type", "ticket")).lower())
           and (not severities or r.get("severity") in severities) and (not workflows or r.get("workflow_status") in workflows)]
    key = sort_by if sort_by in SORT_KEYS else "created_at"
    if key == "severity":
        out.sort(key=lambda r: _sev_rank(r.get("severity")), reverse=not descending)  # descending = most severe first
    else:
        out.sort(key=lambda r: str(r.get(key) or ""), reverse=descending)
        out.sort(key=lambda r: r.get(key) is None)  # missing values last in both directions (stable)
    size = page_size if page_size in PAGE_SIZES else 25
    count = max(1, math.ceil(len(out) / size))
    page = min(max(1, page), count)
    return ViewPage(out[(page - 1) * size: page * size], len(out), page, count, size)


# ------------------------------------------------------------------------------------------------ overview
@dataclass(frozen=True)
class Metric:
    label: str
    value: int | None  # None = not available (distinct from a real zero)
    note: str = ""


def overview_metrics(loaded: LoadedIncidents, ready: dict[str, Any] | None = None) -> list[Metric]:
    if not loaded.available:
        why = "unavailable: the incident list could not be loaded"
        return [Metric(label, None, why) for label in ("Incidents", "Open high/critical", "Awaiting approval",
                                                        "Recovery outcome uncertain", "Investigations failed")]
    scope = "" if not loaded.truncated else f" (counted over the {len(loaded.items)} most recent of {loaded.total})"
    wf = Counter(i.get("workflow_status") for i in loaded.items)
    high = sum(1 for i in loaded.items if i.get("severity") in ("high", "critical")
               and i.get("workflow_status") not in ("recovery_completed", "recovery_not_executed"))
    return [
        Metric("Incidents", loaded.total, "total recorded" + ("" if not loaded.partial else " (list partially loaded)")),
        Metric("Open high/critical", high, "high or critical, not yet recovered" + scope),
        Metric("Awaiting approval", wf.get("awaiting_approval", 0), "recovery requests waiting for a human" + scope),
        Metric("Recovery outcome uncertain", wf.get("recovery_uncertain", 0), "needs manual review; never retried here" + scope),
        Metric("Investigations failed", wf.get("investigation_failed", 0), "investigation did not complete" + scope),
    ]


def count_by(items: list[dict[str, Any]], key: str, order: tuple[str, ...]) -> list[tuple[str, int]]:
    c = Counter(i.get(key) or "unknown" for i in items)
    ordered = [(k, c[k]) for k in order if k in c]
    return ordered + sorted(((k, v) for k, v in c.items() if k not in order), key=lambda kv: kv[0])


def created_per_day(items: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Incident creation counts per calendar day, from the loaded incidents' real ``created_at`` only."""
    days: Counter[str] = Counter()
    for i in items:
        stamp = str(i.get("created_at") or "")
        if len(stamp) >= 10 and stamp[4] == "-" and stamp[7] == "-":
            days[stamp[:10]] += 1
    return sorted(days.items())


# ------------------------------------------------------------------------------------------------ recovery
def recovery_summary(rec: dict[str, Any]) -> dict[str, Any]:
    """Honest, specific wording for one recovery. Execution and verification are reported separately."""
    phase = rec.get("phase") or rec.get("status")
    verified = rec.get("verified_outcome") or "not_applicable"
    execution = rec.get("execution_status") or "not_run"
    notes: list[str] = []
    if phase == "awaiting_approval":
        headline, tone = "Awaiting human approval. Nothing has been executed.", "info"
    elif phase == "recovery_running":
        headline, tone = "Resuming after an approval. The result is not recorded yet.", "info"
    elif phase == "recovery_uncertain" or verified == "unknown":
        headline, tone = "Outcome UNCERTAIN: the action may or may not have taken effect.", "error"
        notes.append("Review the system and the governance trail manually. This dashboard never retries an uncertain recovery.")
    elif phase == "recovery_completed":
        if verified == "verified":
            headline, tone = "Executed, and the recorded verification confirmed the outcome.", "success"
        else:
            headline, tone = "Executed, but verification is not confirmed.", "warning"
            notes.append("Execution is not the same as a verified recovery.")
    elif phase == "recovery_not_executed":
        headline, tone = "Not executed: " + (", ".join(rec.get("reasons") or []) or str(rec.get("status"))) + ".", "warning"
    else:
        headline, tone = "The recovery did not succeed (" + str(rec.get("status")) + ").", "error"
    if rec.get("reasons") and phase != "recovery_not_executed":
        notes.append("Reasons recorded: " + ", ".join(str(r) for r in rec["reasons"]))
    return {"headline": headline, "tone": tone, "execution_status": execution, "verified_outcome": verified,
            "uncertain": tone == "error" and verified == "unknown", "notes": notes}


def approval_actionable(rec: dict[str, Any]) -> bool:
    approval = rec.get("approval") if isinstance(rec.get("approval"), dict) else None
    return bool(approval and approval.get("status") == "pending" and rec.get("status") == "awaiting_approval"
                and rec.get("approval_request_id"))


def decision_outcome_text(result) -> tuple[str, str]:
    """``(tone, text)`` for the result of relaying a decision. Never suggests a blind retry."""
    if result.ok:
        data = result.data or {}
        rec = data.get("recovery") if isinstance(data, dict) else None
        state = (data.get("approval") or {}).get("status") if isinstance(data, dict) else None
        text = f"Decision recorded (approval status: {state})."
        if isinstance(rec, dict):
            text += " " + recovery_summary(rec)["headline"]
        return ("warning" if isinstance(rec, dict) and recovery_summary(rec)["uncertain"] else "success"), text
    p = result.problem
    if p.outcome_unknown:
        return "error", ("The outcome of the decision is UNKNOWN (" + p.kind + "). Do not submit again until you have "
                         "refreshed and checked the approval and recovery status.")
    reason = (p.details or {}).get("reason")
    suffix = f" Reason: {reason}." if reason else ""
    return "error", f"The decision was refused ({p.code or p.kind}).{suffix}"


# ------------------------------------------------------------------------------------------------ investigate
ACTIVE_STATES = ("pending", "running")


def investigate_start_outcome(result) -> tuple[str, str, str | None]:
    """``(tone, text, investigation_id)`` for the answer to "start an investigation". Success is only claimed when the
    backend accepted it (202 with a record); even then the text says it STARTED, not that it found anything."""
    if result.ok:
        data = result.data if isinstance(result.data, dict) else {}
        iid = data.get("investigation_id")
        if not iid:
            return "error", "The API answered without an investigation record. Nothing is shown as started.", None
        return "info", f"Investigation {iid} was accepted by the API (status: {data.get('status')}). It is not finished yet.", str(iid)
    p = result.problem
    if p.code == "investigation_in_progress":
        iid = (p.details or {}).get("investigation_id")
        return "warning", "An investigation for this incident is already pending or running. Showing it instead of starting another.", (str(iid) if iid else None)
    if p.outcome_unknown:
        return "error", ("The outcome of the request is UNKNOWN (" + p.kind + "). Check the incident's investigations before "
                         "starting another."), None
    if p.kind == "unauthenticated":
        return "error", "The token was missing or rejected, so no investigation was started.", None
    if p.kind == "forbidden":
        return "error", "This token's role may not start investigations (it needs investigation:run). Nothing was started.", None
    if p.kind == "unavailable":
        return "error", f"Investigations are unavailable in the API's current configuration ({p.code or p.kind}). Nothing was started.", None
    return "error", f"The investigation was not started ({p.code or p.kind}).", None


def import_outcome(result) -> tuple[str, str, str | None]:
    """``(tone, text, workflow_incident_id)`` for the answer to a simulator-incident import."""
    if result.ok:
        data = result.data if isinstance(result.data, dict) else {}
        wid = (data.get("incident") or {}).get("incident_id")
        if not wid:
            return "error", "The API answered without a workflow incident. Nothing is shown as imported.", None
        if data.get("created"):
            return "success", f"Imported. Workflow incident {wid} was created. Nothing has been investigated or approved yet.", str(wid)
        return "info", f"Already imported earlier as workflow incident {wid}. Nothing was duplicated.", str(wid)
    p = result.problem
    if p.outcome_unknown:
        return "error", ("The outcome of the import is UNKNOWN (" + p.kind + "). Press 'Refresh data' and check whether the "
                         "incident now shows as imported before trying again."), None
    if p.kind == "unauthenticated":
        return "error", "The token was missing or rejected, so nothing was imported.", None
    if p.kind == "forbidden":
        return "error", "This token's role may not import incidents (it needs incident:create). Nothing was imported.", None
    if p.kind == "unavailable":
        return "error", f"The simulator is unavailable ({p.code or p.kind}). Nothing was imported.", None
    return "error", f"The import was refused ({p.code or p.kind}).", None


def recovery_request_outcome(result) -> tuple[str, str]:
    """``(tone, text)`` for asking the API to create a recovery proposal. Success means a PENDING approval request only."""
    if result.ok:
        data = result.data if isinstance(result.data, dict) else {}
        action = data.get("action_type")
        return "warning", (f"Proposal created: {action or 'a recovery action'} is awaiting a human decision "
                           f"(approval request {data.get('approval_request_id') or 'recorded'}). Nothing has been executed. "
                           "Open the Human Review page to approve or reject it.")
    p = result.problem
    if p.code == "no_recovery_suggested":
        return "info", ("No recovery was proposed: the investigation evidence maps to no allowlisted recovery action, so there is "
                        "nothing to send to Human Review. This is expected for synthetic simulator incidents.")
    if p.code == "recovery_in_progress":
        return "warning", "A recovery is already in progress for this incident. See Recovery & approvals."
    if p.code == "no_completed_investigation":
        return "error", "Run an investigation to completion first."
    if p.status == 409:
        return "warning", f"The request was recorded but not left pending ({p.code or p.kind}). See Recovery & approvals."
    if p.outcome_unknown:
        return "error", ("The outcome is UNKNOWN (" + p.kind + "). Check Human Review and Recovery & approvals before requesting "
                         "again.")
    if p.kind == "unauthenticated":
        return "error", "The token was missing or rejected, so nothing was requested."
    if p.kind == "forbidden":
        return "error", "This token's role may not request recoveries (it needs recovery:request, an operator role). Nothing was requested."
    if p.kind == "unavailable":
        return "error", f"Recovery is unavailable in the API's current configuration ({p.code or p.kind})."
    return "error", f"The recovery request was refused ({p.code or p.kind})."


def investigation_progress(detail: dict[str, Any]) -> tuple[str, str]:
    """``(tone, headline)`` from the backend's own status. Never reports success before ``completed``."""
    status = detail.get("status")
    if status in ACTIVE_STATES:
        return "info", f"Investigation is {status}. Press 'Check status' to refresh."
    if status == "completed":
        rs = detail.get("report_status")
        if rs in ("inconclusive", "failed"):
            return "warning", f"Investigation finished but its outcome is '{rs}'. Treat the findings as partial or absent."
        return "success", (f"Investigation completed (outcome: {rs or 'unspecified'}). Findings below are labelled as observed "
                           "evidence or unconfirmed hypotheses.")
    if status == "failed":
        kind = (detail.get("error") or {}).get("kind") or "unknown"
        return "error", f"Investigation failed ({kind}). No findings are available."
    return "warning", f"Investigation is in an unexpected state ({status})."

# ------------------------------------------------------------------------------------------------ tickets
def ticket_view(ticket: dict[str, Any] | None) -> dict[str, Any]:
    if not ticket:
        return {"present": False, "caution": None}
    closed = str(ticket.get("status") or "").lower() in ("closed", "done", "resolved")
    return {"present": True, "closed": closed,
            "caution": ("A closed ticket is not evidence that the incident was recovered or that any action was authorised."
                        if closed else None)}


def ticket_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i in items:
        t = i.get("ticket")
        if isinstance(t, dict):
            out.append({"incident_id": i.get("incident_id"), "provider": t.get("provider"), "repository": t.get("repository"),
                        "number": t.get("number"), "status": t.get("status"), "last_sync_result": t.get("last_sync_result"),
                        "last_sync_at": t.get("last_sync_at"), "url": t.get("url")})
    return out


# ------------------------------------------------------------------------------------------------ investigations
def report_view(detail: dict[str, Any]) -> dict[str, Any]:
    """The investigation shown with explicit epistemic labels."""
    report = detail.get("report") if isinstance(detail.get("report"), dict) else None
    out: dict[str, Any] = {"status": detail.get("status"), "report_status": detail.get("report_status"),
                           "summary": detail.get("summary"), "has_report": report is not None, "error": detail.get("error")}
    if not report:
        return out
    cause = report.get("likely_root_cause") or {}
    if cause.get("status") == "supported":
        label = "Likely cause - supported by the cited evidence, NOT confirmed"
    else:
        label = "Root cause undetermined"
    out.update(
        observed=[f.get("statement") for f in report.get("observed_findings") or []],
        hypotheses=[f.get("statement") for f in report.get("hypotheses") or []],
        hypothesis_label="Hypotheses - unconfirmed",
        cause_label=label, cause_statement=cause.get("statement"), cause_explanation=cause.get("explanation"),
        confidence=report.get("confidence"), confidence_explanation=report.get("confidence_explanation"),
        next_steps=list(report.get("recommended_next_steps") or []), limitations=list(report.get("limitations") or []),
        tools=[{"tool": t.get("tool"), "outcome": t.get("outcome")} for t in report.get("tools_executed") or []],
        evidence=[{"id": e.get("evidence_id"), "tool": e.get("tool"), "status": e.get("status"), "summary": e.get("summary")}
                  for e in report.get("evidence") or []])
    return out


# ------------------------------------------------------------------------------------------------ system status
def status_rows(health: Any, ready: Any) -> tuple[list[dict[str, str]], list[str]]:
    """Rows for the status page and a list of warnings. ``health``/``ready`` are :class:`Result` objects."""
    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    if health.ok:
        rows.append({"check": "API liveness", "state": str((health.data or {}).get("status")), "detail": f"version {(health.data or {}).get('version')}"})
    else:
        rows.append({"check": "API liveness", "state": health.problem.kind, "detail": health.problem.message})
    body = ready.data if ready.ok else (ready.problem.body if ready.problem else None)  # 503 not_ready still carries checks
    if isinstance(body, dict) and "checks" in body:
        rows.append({"check": "API readiness", "state": str(body.get("status")), "detail": f"environment {body.get('environment')}"})
        for name, state in sorted((body.get("checks") or {}).items()):
            rows.append({"check": name.replace("_", " "), "state": str(state), "detail": ""})
        if (body.get("checks") or {}).get("authentication") == "dev_static":
            warnings.append("Authentication is the development-only static-token mode. It is not suitable for production.")
        if (body.get("checks") or {}).get("governance") == "suspended":
            warnings.append("VSL governance reports automation SUSPENDED. A human must clear it through the governance authority path; this UI cannot.")
    else:
        rows.append({"check": "API readiness", "state": ready.problem.kind if ready.problem else "unknown",
                     "detail": ready.problem.message if ready.problem else ""})
    return rows, warnings


# ------------------------------------------------------------------------------------------------ Phase 11 additions
def summary_metrics(summary: Any) -> list[Metric]:
    """Cards for the sources the overview summary reports. A source that is unavailable is ``None``, never zero.
    Real workflow, simulator and synthetic demo numbers are separate cards and are never added together."""
    labels = ("Pending human review", "Simulator incidents (SQL Server)", "Simulator incidents not imported", "Demo incidents (synthetic)")
    if not summary.ok or not isinstance(summary.data, dict):
        return [Metric(label, None, "unavailable: the summary could not be loaded") for label in labels]
    s = summary.data
    hr = s.get("human_review") or {}
    sim = s.get("simulator") or {}
    demo = s.get("demo") or {}
    sim_ok = bool(sim.get("available"))
    return [
        Metric(labels[0], hr.get("pending") if hr.get("available") else None, "approval requests awaiting a human decision (persisted state)"),
        Metric(labels[1], sim.get("incident_total") if sim_ok else None, "incidents recorded in the SQL Server simulator" if sim_ok else "simulator database unavailable"),
        Metric(labels[2], (sim["incident_total"] - sim.get("imported", 0)) if sim_ok else None, "visible under Incidents > SQL Server simulator; import is explicit"),
        Metric(labels[3], demo.get("incident_total"), "isolated synthetic demo dataset - not real"),
    ]


def review_row(item: dict[str, Any]) -> dict[str, Any]:
    return {"review_id": item.get("review_id"), "incident_id": item.get("incident_id"), "incident": (item.get("incident_title") or "")[:70],
            "severity": item.get("incident_severity"), "action": item.get("action_type"), "risk": item.get("risk"),
            "state": item.get("review_state"), "requested_by": item.get("requested_by"), "created_at": item.get("created_at"),
            "expires_at": item.get("expires_at"), "decided_by": item.get("decided_by")}


ENABLE_RECOVERY_STEPS = (
    '# 1. Stop the running stack (Ctrl+C in its window).',
    '# 2. Use a SEPARATE demo database whose name ends with _test (this session only; your .env is not changed):',
    '$env:DB_NAME = "dataops_demo_test"',
    'python -m scripts.db_init',
    'python -m scripts.db_seed',
    'python -m scripts.db_seed_demo_incidents',
    'python -m scripts.scenario activate duplicate-records',
    '# 3. Start the stack from the SAME window:',
    '.\\.venv\\Scripts\\python.exe -m scripts.dev_stack --show-approver-token',
)


def recovery_guidance(problem: Any) -> dict[str, Any] | None:
    """When the API says recovery/approvals are switched off, return an honest explanation for the page (else None).

    Pure: it reads only the API's own error (code, message, details) and never invents a queue, a zero or a success."""
    if problem is None or getattr(problem, "code", None) != "recovery_unavailable":
        return None
    details = getattr(problem, "details", None) or {}
    guard = bool(details.get("required_database_suffix"))
    return {"title": "Human Review is switched off in this configuration", "message": getattr(problem, "message", ""),
            "reason": details.get("reason"), "database": details.get("database"), "fix": details.get("fix"),
            "request_id": getattr(problem, "request_id", None), "steps": list(ENABLE_RECOVERY_STEPS) if guard else []}


def review_actionable(item: dict[str, Any]) -> bool:
    return bool(item.get("awaiting_decision") and item.get("review_state") == "pending" and item.get("request_id"))


def sim_row(item: dict[str, Any]) -> dict[str, Any]:
    return {"incident_id": item.get("incident_id"), "title": (item.get("title") or "")[:80], "severity": item.get("severity"),
            "status": item.get("status"), "category": item.get("category"), "pipeline": item.get("pipeline_id"), "run_id": item.get("run_id"),
            "created_at": item.get("created_at"), "imported": "yes" if item.get("imported") else "no"}


def demo_row(item: dict[str, Any]) -> dict[str, Any]:
    return {"incident_id": item.get("incident_id"), "title": (item.get("title") or "")[:80], "category": item.get("category"),
            "severity": item.get("severity"), "status": item.get("status"), "pipeline": item.get("affected_pipeline"),
            "detected_at": item.get("detected_at"), "review_flag": "yes" if item.get("human_review_required") else "no"}
