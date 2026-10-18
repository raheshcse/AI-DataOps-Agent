"""Evidence extraction, report verification and rendering.

The model (or the offline engine) only proposes interpretation. Here, in code:
* facts are taken from the tool results, never from model text;
* every cited evidence id must exist, and a root cause must cite a real finding (a warning or issue), or it
  is replaced by "undetermined";
* statements naming run ids, error codes, dates or DIAG codes that do not appear in the evidence are dropped;
* confidence is capped by rules (one source, failed tools, low severity, undetermined cause);
* the overall status is computed from the evidence, not taken from the model.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.agent.models import (
    Confidence,
    EvidenceItem,
    Finding,
    InvestigationReport,
    InvestigationStatus,
    ReportDraft,
    ReportError,
    RootCause,
    ToolRun,
)
from app.agent.safety import digest_result, find_injection_markers, safe_text

_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unknown": 5}
_CONF_RANK = {"low": 0, "medium": 1, "high": 2}
_FAILED_OUTCOMES = {"diagnostic_error", "tool_error", "timeout", "malformed_response", "connection_error"}
# The diagnostics that must all have produced usable results before "no issue detected" can be reported.
CORE_DIAGNOSTICS = frozenset({"get_pipeline_status", "check_data_freshness", "reconcile_record_counts",
                              "detect_duplicate_records", "diagnose_data_quality", "validate_schema_contract"})
_IDENTIFIER = re.compile(r"\b(RUN-[A-Z0-9][A-Z0-9-]*|E\d{4}|DIAG-[A-Z-]+|\d{4}-\d{2}-\d{2})\b")


def new_investigation_id() -> str:
    return f"INV-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


# ------------------------------------------------------------------------------------ evidence
def _item(evidence_id: str, call_id: str, data: dict[str, Any], limit: int) -> EvidenceItem:
    digest, truncated = digest_result(data, limit)
    return EvidenceItem(
        evidence_id=evidence_id, call_id=call_id, tool=safe_text(data.get("diagnostic_name"), 60),
        status=str(data.get("status")), severity=str(data.get("severity", "unknown")),
        summary=safe_text(data.get("summary"), 500), error_code=safe_text(data["error_code"], 40) if data.get("error_code") else None,
        recommended_next_step=safe_text(data["recommended_next_step"], 400) if data.get("recommended_next_step") else None,
        affected_records=[safe_text(r, 100) for r in (data.get("affected_records") or [])[:25]],
        digest=digest, truncated=truncated)


def build_evidence(call_id: str, structured: dict[str, Any], limit: int) -> list[EvidenceItem]:
    """One evidence item per diagnostic result; the run_all roll-up is split into its parts (E3.1, E3.2, ...)."""
    if "results" in structured:
        return [_item(f"{call_id}.{i}", call_id, r, limit) for i, r in enumerate(structured["results"], start=1)]
    return [_item(call_id, call_id, structured, limit)]


# ------------------------------------------------------------------------------------- status
def compute_status(evidence: list[EvidenceItem], tool_runs: list[ToolRun], error: ReportError | None) -> InvestigationStatus:
    usable = [e for e in evidence if e.status != "error"]
    attempts_failed = any(r.outcome in _FAILED_OUTCOMES for r in tool_runs)
    if not usable:
        return "failed" if (error or attempts_failed) else "inconclusive"
    if any(e.status == "issue_detected" for e in usable):
        return "issue_identified"
    warnings = [e for e in usable if e.status == "warning"]
    if attempts_failed or any(e.status == "error" for e in evidence) or any(_SEV_RANK.get(e.severity, 5) <= 2 for e in warnings):
        return "inconclusive"
    if CORE_DIAGNOSTICS - {e.tool for e in usable}:  # an unchecked area could hold the cause
        return "inconclusive"
    return "warnings_only" if warnings else "no_issue_detected"


# ----------------------------------------------------------------------------------- grounding
def _evidence_text(evidence: list[EvidenceItem]) -> str:
    return json.dumps([e.model_dump() for e in evidence], default=str)


def _ungrounded(text: str | None, blob: str) -> list[str]:
    return sorted({m for m in _IDENTIFIER.findall(text or "") if m not in blob})


# Markers that mean "someone wrote instructions into the data". tool-directive is excluded: ordinary log
# lines ("failed to run script") would trigger it, and it is still reported as a security note by the graph.
_TAINT_MARKERS = frozenset({"ignore-instructions", "new-instructions", "role-override", "system-prompt", "secret-request", "chat-markup"})


def _tainted_ids(evidence: list[EvidenceItem]) -> set[str]:
    """Evidence whose content contained instruction-like text. It stays visible as an observation, but it is
    never accepted as support for a root cause or an impact claim."""
    return {e.evidence_id for e in evidence
            if _TAINT_MARKERS & set(find_injection_markers(json.dumps(e.model_dump(), default=str)))}


def _valid_ids(ids: list[str], by_id: dict[str, EvidenceItem]) -> list[str]:
    seen: list[str] = []
    for i in ids:
        i = i.strip()
        if i in by_id and by_id[i].status != "error" and i not in seen:
            seen.append(i)
    return seen


def _cap_confidence(requested: Confidence, root: RootCause, by_id: dict[str, EvidenceItem], evidence: list[EvidenceItem],
                    tool_runs: list[ToolRun], status: InvestigationStatus) -> tuple[Confidence, list[str]]:
    reasons: list[str] = []
    level = _CONF_RANK[requested]

    def cap(to: Confidence, why: str) -> None:
        nonlocal level
        if _CONF_RANK[to] < level:
            level = _CONF_RANK[to]
            reasons.append(why)

    if root.status == "undetermined":
        cap("low", "no root cause is supported by the evidence")
    else:
        cited = [by_id[i] for i in root.evidence_ids if by_id[i].status in ("issue_detected", "warning")]
        if len({e.tool for e in cited}) < 2:
            cap("medium", "only one diagnostic supports the cause")
        if min((_SEV_RANK.get(e.severity, 5) for e in cited), default=5) >= 3:
            cap("low", "the supporting findings are low severity")
        if any(r.outcome in _FAILED_OUTCOMES for r in tool_runs) or any(e.status == "error" for e in evidence):
            cap("medium", "some diagnostics failed, so evidence is incomplete")
    if status in ("warnings_only", "inconclusive", "no_issue_detected", "failed"):
        cap("low", f"the investigation status is {status}")
    return ("low", "medium", "high")[level], reasons  # type: ignore[return-value]


# ------------------------------------------------------------------------------------ report
def finalize_report(*, investigation_id: str, incident: str, mode: str, model: str, tool_runs: list[ToolRun],
                    evidence: list[EvidenceItem], limitations: list[str], security_notes: list[str], rounds_used: int,
                    tool_calls_used: int, stop_reason: str, approved_tools: list[str], draft: ReportDraft | None = None,
                    error: ReportError | None = None) -> InvestigationReport:
    by_id = {e.evidence_id: e for e in evidence}
    blob = _evidence_text(evidence)
    tainted = _tainted_ids(evidence)
    notes = list(limitations)
    if tainted:
        notes.append("Evidence " + ", ".join(sorted(tainted)) + " contained instruction-like text; it was kept as an observation "
                     "but not accepted as support for a root cause or impact.")
    status = compute_status(evidence, tool_runs, error)

    # Observed facts: straight from the tools.
    ordered = sorted((e for e in evidence if e.status != "ok"), key=lambda e: (e.status == "error", _SEV_RANK.get(e.severity, 5), e.evidence_id))
    observed = [Finding(statement=f"[{e.tool}] {e.summary}" + (f" (error {e.error_code})" if e.error_code and e.status != "ok" else ""),
                        evidence_ids=[e.evidence_id]) for e in ordered]
    passing = [e for e in evidence if e.status == "ok"]
    if passing:
        observed.append(Finding(statement="Checks that reported no problem: " + ", ".join(sorted({e.tool for e in passing})) + ".",
                                evidence_ids=[e.evidence_id for e in passing]))

    # Model interpretation, verified.
    hypotheses: list[Finding] = []
    dropped = 0
    root = RootCause(status="undetermined", explanation="No model interpretation was available." if draft is None
                     else "The evidence does not directly support a root cause.")
    impact: Finding | None = None
    requested: Confidence = "low"
    explanation = ""
    steps: list[str] = []
    if draft is not None:
        for h in draft.hypotheses:
            ids = _valid_ids(h.evidence_ids, by_id)
            if ids and not _ungrounded(h.statement, blob):
                hypotheses.append(Finding(statement=safe_text(h.statement, 600), evidence_ids=ids))
            else:
                dropped += 1
        rc = draft.root_cause
        rc_ids = _valid_ids(rc.evidence_ids, by_id)
        if rc.statement:
            bad = _ungrounded(rc.statement, blob)
            supported = [i for i in rc_ids if by_id[i].status in ("issue_detected", "warning") and i not in tainted]
            if supported and not bad:
                root = RootCause(status="supported", statement=safe_text(rc.statement, 600), evidence_ids=supported,
                                 explanation=safe_text(rc.explanation or "Supported by the cited diagnostic findings.", 600))
            else:
                dropped += 1
                notes.append("A root cause proposed by the model was discarded: " + (
                    f"it named identifiers that are not in the evidence ({', '.join(bad)})." if bad else
                    "it did not cite a diagnostic finding that supports it."))
                root = RootCause(status="undetermined", explanation="The evidence does not directly support a root cause.")
        else:
            root = RootCause(status="undetermined", explanation=safe_text(rc.explanation or "The evidence does not directly support a root cause.", 600))
        if draft.impact.statement:
            ids = [i for i in _valid_ids(draft.impact.evidence_ids, by_id) if i not in tainted]
            if ids and not _ungrounded(draft.impact.statement, blob):
                impact = Finding(statement=safe_text(draft.impact.statement, 600), evidence_ids=ids)
            else:
                dropped += 1
        requested = draft.confidence
        explanation = safe_text(draft.confidence_explanation, 600)
        steps = [safe_text(s, 300) for s in draft.recommended_next_steps if s.strip()]
        notes += [safe_text(s, 300) for s in draft.limitations if s.strip()]
    if dropped:
        notes.append(f"{dropped} model statement(s) were discarded because they cited no valid evidence or named identifiers not in the evidence.")

    if not steps:
        steps = list(dict.fromkeys(e.recommended_next_step for e in ordered if e.recommended_next_step and e.status != "error"))[:5]
    failed = [r for r in tool_runs if r.outcome in _FAILED_OUTCOMES]
    for r in failed:
        notes.append(f"Tool call {r.call_id} ({r.tool}) did not complete: {r.outcome}" + (f" - {safe_text(r.detail, 160)}" if r.detail else "."))
    if any(e.truncated for e in evidence):
        notes.append("Some tool output was shortened to fit size limits; see the per-tool digests.")
    if passing:
        notes.append("A passing diagnostic does not prove the reported problem is absent or resolved; it only shows that check found nothing.")
    unchecked = sorted(CORE_DIAGNOSTICS - {e.tool for e in evidence if e.status != "error"})
    if evidence and unchecked and status in ("inconclusive", "failed"):
        notes.append("Diagnostics not run, or without a usable result: " + ", ".join(unchecked) + ".")
    if not evidence:
        notes.append("No diagnostic evidence was collected.")
    if failed and status in ("failed", "inconclusive"):
        steps.append("Fix the failed tool calls listed under limitations, then re-run the investigation.")

    confidence, reasons = _cap_confidence(requested, root, by_id, evidence, tool_runs, status)
    conf_text = (explanation + " " if explanation else "") + (f"Confidence set to {confidence}: " + "; ".join(reasons) + "." if reasons else "")
    if not conf_text.strip():
        conf_text = "Derived from the supporting evidence."

    if draft is not None and draft.summary.strip():
        summary = safe_text(draft.summary, 800)
    elif ordered:
        summary = f"{len([e for e in ordered if e.status != 'error'])} finding(s). Most significant: {ordered[0].summary}"
    else:
        summary = "No diagnostic evidence indicates a problem." if evidence else "No diagnostic evidence was collected."

    return InvestigationReport(
        investigation_id=investigation_id, incident=incident, status=status, mode=mode, model=model,  # type: ignore[arg-type]
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), summary=summary,
        observed_findings=observed, hypotheses=hypotheses, likely_root_cause=root, impact=impact, confidence=confidence,
        confidence_explanation=conf_text.strip(), recommended_next_steps=list(dict.fromkeys(steps))[:8],
        tools_executed=tool_runs, evidence=evidence, limitations=list(dict.fromkeys(notes)), security_notes=list(dict.fromkeys(security_notes)),
        rounds_used=rounds_used, tool_calls_used=tool_calls_used, stop_reason=stop_reason, error=error, approved_tools=approved_tools)


def failed_report(*, incident: str, mode: str, model: str, kind: str, message: str, investigation_id: str | None = None,
                  approved_tools: list[str] | None = None) -> InvestigationReport:
    """A report for an investigation that could not collect any evidence (for example the MCP server is unreachable)."""
    return finalize_report(investigation_id=investigation_id or new_investigation_id(), incident=incident, mode=mode, model=model,
                           tool_runs=[], evidence=[], limitations=[message], security_notes=[], rounds_used=0, tool_calls_used=0,
                           stop_reason=kind, approved_tools=approved_tools or [], error=ReportError(kind=kind, message=message))


# ----------------------------------------------------------------------------------- rendering
def render_text(report: InvestigationReport, *, show_evidence: bool = True) -> str:
    out: list[str] = []
    add = out.append
    add(f"Investigation {report.investigation_id}")
    if report.mode == "offline":
        add("MODE: OFFLINE heuristic - no language model was used; tool choice and ranking follow fixed rules.")
    add(f"Status: {report.status}    Confidence: {report.confidence}    Rounds: {report.rounds_used}    Tool calls: {report.tool_calls_used}    Stopped: {report.stop_reason}")
    add(f"Incident: {report.incident}")
    add("")
    add("SUMMARY")
    add(f"  {report.summary}")
    add("")
    add("OBSERVED FACTS (from diagnostic tools)")
    for f in report.observed_findings or []:
        add(f"  - {f.statement}  [{', '.join(f.evidence_ids[:6])}{'...' if len(f.evidence_ids) > 6 else ''}]")
    if not report.observed_findings:
        add("  - none")
    add("")
    add("HYPOTHESES (interpretation, not fact)")
    for h in report.hypotheses or []:
        add(f"  - {h.statement}  [{', '.join(h.evidence_ids)}]")
    if not report.hypotheses:
        add("  - none")
    add("")
    rc = report.likely_root_cause
    add("LIKELY ROOT CAUSE")
    if rc.status == "supported":
        add(f"  {rc.statement}  [{', '.join(rc.evidence_ids)}]")
    else:
        add("  Undetermined.")
    add(f"  {rc.explanation}")
    if report.impact:
        add("")
        add("POTENTIAL IMPACT")
        add(f"  {report.impact.statement}  [{', '.join(report.impact.evidence_ids)}]")
    add("")
    add(f"CONFIDENCE: {report.confidence}")
    add(f"  {report.confidence_explanation}")
    add("")
    add("RECOMMENDED NEXT STEPS")
    for s in report.recommended_next_steps or ["none"]:
        add(f"  - {s}")
    add("")
    add("TOOLS EXECUTED")
    for r in report.tools_executed:
        args = json.dumps(r.arguments, sort_keys=True) if r.arguments else "{}"
        add(f"  {r.call_id} (round {r.round}) {r.tool} {args} -> {r.outcome}")
    if show_evidence:
        add("")
        add("EVIDENCE")
        for e in report.evidence:
            code = f" {e.error_code}" if e.error_code else ""
            add(f"  {e.evidence_id} {e.tool}: {e.status}/{e.severity}{code} - {e.summary}")
            if e.affected_records:
                add(f"      affected: {', '.join(e.affected_records[:6])}{' ...' if len(e.affected_records) > 6 else ''}")
    add("")
    add("LIMITATIONS")
    for l in report.limitations or ["none recorded"]:
        add(f"  - {l}")
    if report.security_notes:
        add("")
        add("SECURITY NOTES")
        for n in report.security_notes:
            add(f"  - {n}")
    return "\n".join(out)
