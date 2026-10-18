"""Deterministic offline stand-in for the language model (``--offline``).

This is NOT a language model and makes no network call and needs no key. It follows fixed keyword rules to pick
diagnostic tools, follows up on run ids that appear in the evidence, and ranks findings by status and severity.
It exists so the whole workflow (graph, MCP, report verification) can be developed, demonstrated and tested
without an API key. It reads only structured fields (status, severity, summary, affected_records); it never
interprets free text found in logs, so text inside tool output cannot steer it.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from app.agent.llm import Task

_STATUS_RANK = {"issue_detected": 0, "warning": 1}
_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unknown": 5}
_IMPACT_TOOLS = ("reconcile_record_counts", "detect_duplicate_records", "diagnose_data_quality", "check_data_freshness")

_KEYWORD_TOOLS: list[tuple[re.Pattern[str], list[str]]] = [
    (re.compile(r"stale|outdated|out of date|old data|not (been )?(updated|refreshed)|hasn.?t (updated|refreshed)|"
                r"\blate\b|delay|fresh|yesterday|latest|behind|overnight", re.I), ["check_data_freshness"]),
    (re.compile(r"duplicat|twice|double|repeated|replay|inflat|overcount|same order", re.I),
     ["detect_duplicate_records", "diagnose_data_quality"]),
    (re.compile(r"missing|incomplete|fewer|short\b|partial|count|\brows?\b|records?|dropped|gap|absent|"
                r"not (loaded|ingested|arriv)|reconcil|don.?t match|doesn.?t match", re.I), ["reconcile_record_counts"]),
    (re.compile(r"schema|column|data ?type|field|renam|drift|contract|format change", re.I), ["validate_schema_contract"]),
    (re.compile(r"quality|wrong|incorrect|invalid|total|amount|negative|future|null|corrupt|bad data|inconsisten", re.I),
     ["diagnose_data_quality"]),
    (re.compile(r"fail|error|crash|pipeline|\bjobs?\b|did(n.?t| not) run|timeout|timed out|stuck|abort|exception", re.I),
     ["get_pipeline_status"]),
]
_RUN_REF = re.compile(r"^run:(\S{1,100})$")


def _key(tool: str, arguments: dict[str, Any]) -> str:
    return tool + json.dumps(arguments, sort_keys=True)


class OfflineHeuristicModel:
    mode: Literal["offline"] = "offline"
    name = "offline-heuristic"

    async def complete_json(self, task: Task, system: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        if task == "plan":
            return self._plan(payload)
        if task == "report":
            return self._report(payload)
        return {"ok": True}

    # ------------------------------------------------------------------------------- planning
    def _plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        available = {t["name"] for t in payload.get("tools", [])}
        executed = payload.get("executed", [])
        done = {_key(e["tool"], e.get("arguments", {})) for e in executed}
        evidence = payload.get("untrusted_evidence", [])

        wanted: list[tuple[str, dict[str, Any]]] = []
        if not executed:
            incident = payload.get("incident", "")
            tools = ["get_pipeline_status"]
            matched = False
            for pattern, names in _KEYWORD_TOOLS:
                if pattern.search(incident):
                    matched = True
                    tools += [n for n in names if n not in tools]
            if not matched:
                tools = ["run_all_diagnostics"]
            wanted = [(t, {}) for t in tools]
            reason = "Round 1: tools chosen by keyword rules from the incident text."
        else:
            run_ids: list[str] = []
            for item in evidence:
                if item["status"] in _STATUS_RANK:
                    for ref in item.get("affected_records", []):
                        m = _RUN_REF.match(ref)
                        if m and m.group(1) not in run_ids:
                            run_ids.append(m.group(1))
            wanted = [("inspect_pipeline_logs", {"run_id": rid}) for rid in run_ids[:2]]
            reason = "Follow-up: inspect the logs of runs named in the evidence."

        calls = [(t, a) for t, a in wanted if t in available and _key(t, a) not in done]
        confirmed = any(e["status"] == "issue_detected" for e in evidence)
        if executed and not calls and not confirmed:  # nothing new to follow up and nothing confirmed: widen the sweep
            calls = [(t, a) for t, a in [("run_all_diagnostics", {})] if t in available and _key(t, a) not in done]
            reason = "Targeted checks found no confirmed issue; widening to the full diagnostic sweep."
        if not calls:
            return {"action": "finish", "tool_calls": [], "rationale": "No further relevant diagnostics to run."}
        return {"action": "call_tools", "rationale": reason,
                "tool_calls": [{"tool": t, "arguments_json": json.dumps(a)} for t, a in calls]}

    # ------------------------------------------------------------------------------ reporting
    def _report(self, payload: dict[str, Any]) -> dict[str, Any]:
        evidence = [e for e in payload.get("untrusted_evidence", []) if e["status"] in ("ok", "warning", "issue_detected")]
        issues = sorted((e for e in evidence if e["status"] in _STATUS_RANK),
                        key=lambda e: (_STATUS_RANK[e["status"]], _SEV_RANK.get(e["severity"], 5), e["evidence_id"]))
        firm = [e for e in issues if e["status"] == "issue_detected"]

        if not issues:
            summary = (f"No diagnostic reported a problem across {len(evidence)} result(s). The reported symptom "
                       "could not be reproduced with the available diagnostics.")
        else:
            summary = f"{len(issues)} finding(s) across {len(evidence)} diagnostic result(s). Most significant: {issues[0]['summary']}"

        if firm:
            top = firm[0]
            same = [e for e in firm if e["error_code"] and e["error_code"] == top["error_code"]]
            cited = [top["evidence_id"]] + [e["evidence_id"] for e in same if e["evidence_id"] != top["evidence_id"]]
            root = {"statement": f"Most probable cause: {top['summary']}", "evidence_ids": cited,
                    "explanation": "Offline mode selects the highest-severity confirmed finding and cites other findings with the "
                                   "same error code as corroboration. It ranks findings and does not reason about causality, so "
                                   "treat this as a lead to verify."}
        else:
            cited = []
            root = {"statement": None, "evidence_ids": [],
                    "explanation": "No diagnostic confirmed a failure, so no cause is supported by the evidence."}
        hypotheses = [{"statement": f"Related finding that may be connected to the reported symptom: {e['summary']}",
                       "evidence_ids": [e["evidence_id"]]} for e in issues if e["evidence_id"] not in cited][:3]

        impact_item = next((e for e in issues if e["tool"] in _IMPACT_TOOLS), None)
        impact = ({"statement": impact_item["summary"], "evidence_ids": [impact_item["evidence_id"]]}
                  if impact_item else {"statement": None, "evidence_ids": []})

        distinct = {e["tool"] for e in firm}
        confidence = "high" if len(distinct) >= 2 else "medium" if firm else "low"
        ran = {r["tool"] for r in payload.get("tool_runs", [])}
        steps: list[str] = []
        for e in issues:
            step = e.get("recommended_next_step")
            if step and step not in steps and not any(t in step for t in ran):
                steps.append(step)
        if not steps:
            steps.append("No further action is indicated by the diagnostics. If the symptom persists, restate the incident "
                         "with the affected report or table and the time window.")
        return {"summary": summary, "hypotheses": hypotheses, "root_cause": root, "impact": impact,
                "confidence": confidence,
                "confidence_explanation": (f"{len(distinct)} independent diagnostic(s) reported a confirmed issue." if firm
                                           else "No confirmed issue was reported."),
                "recommended_next_steps": steps[:5],
                "limitations": ["Offline mode: tools are chosen by keyword rules and findings are ranked by a fixed rule; "
                                "no language model was used."]}
