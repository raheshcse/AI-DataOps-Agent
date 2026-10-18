"""The investigation workflow as a LangGraph state machine.

    receive_incident -> plan -> execute_tools -> evaluate -> plan ...        (bounded loop)
                          \\______________________________________\\-> generate_report -> END

* plan: the model chooses tools (or finishes). Output is validated; one corrective retry on bad JSON.
* execute_tools: each planned call is checked (approved? arguments valid? budget? duplicate?) and run through
  the MCP toolbox. Failures become recorded outcomes, never crashes. Evidence is stored as bounded digests.
* evaluate: pure code decides whether to continue: budgets, no new evidence, repeated invalid calls, or an
  unreachable server/database all stop the loop.
* generate_report: the model proposes interpretation; ``report.finalize_report`` verifies it against the evidence.

The graph holds no database or diagnostic code. Tools exist only through ``Toolbox``.
"""

from __future__ import annotations

import json
import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.agent.config import AgentConfig
from app.agent.llm import FATAL_KINDS, PLAN_SCHEMA, REPORT_SCHEMA, ChatModel, LLMError
from app.agent.mcp_client import Toolbox
from app.agent.models import (
    EvidenceItem,
    InvestigationReport,
    PlanDecision,
    ReportDraft,
    ReportError,
    ToolRun,
)
from app.agent.prompts import PLAN_SYSTEM, REPORT_SYSTEM, plan_payload, report_payload
from app.agent.report import build_evidence, finalize_report
from app.agent.safety import find_injection_markers, safe_text

_OK_OUTCOMES = {"ok", "diagnostic_error"}
_INFRA_CODES = {"DIAG-DB-ERROR", "DIAG-NOT-SEEDED"}


class AgentState(TypedDict, total=False):
    investigation_id: str
    incident: str
    round: int
    call_counter: int
    tool_calls_used: int
    invalid_calls: int
    pending_calls: list[dict[str, Any]]
    round_new_evidence: int
    round_infra_failure: bool
    tool_runs: Annotated[list[dict[str, Any]], operator.add]
    evidence: Annotated[list[dict[str, Any]], operator.add]
    limitations: Annotated[list[str], operator.add]
    security_notes: Annotated[list[str], operator.add]
    plan_log: Annotated[list[dict[str, Any]], operator.add]
    stop_reason: str | None
    error: dict[str, str] | None
    report: dict[str, Any] | None


@dataclass
class GraphDeps:
    llm: ChatModel
    toolbox: Toolbox
    config: AgentConfig
    progress: Callable[[str], None] = field(default=lambda _msg: None)


class ModelOutputInvalid(Exception):
    pass


def _call_key(tool: str, arguments: dict[str, Any]) -> str:
    return tool + json.dumps(arguments, sort_keys=True, default=str)


async def ask_model(deps: GraphDeps, task: str, system: str, payload: dict[str, Any], schema: dict[str, Any], model_cls: type) -> Any:
    """Ask for JSON, validate it, and retry once with a corrective note if it is malformed."""
    attempt_payload = payload
    for attempt in range(2):
        try:
            data = await deps.llm.complete_json(task, system, attempt_payload, schema)  # type: ignore[arg-type]
            return model_cls.model_validate(data)
        except LLMError as exc:
            if exc.kind in FATAL_KINDS:
                raise
        except ValidationError as exc:
            attempt_payload = {**payload, "correction": f"Your previous reply had {exc.error_count()} schema problem(s). "
                                                        "Reply again with only JSON that matches the required schema."}
        else:  # pragma: no cover
            pass
        if attempt == 0:
            attempt_payload = attempt_payload if attempt_payload is not payload else {
                **payload, "correction": "Your previous reply was not valid JSON of the required shape. Reply again with only the JSON."}
    raise ModelOutputInvalid()


def build_graph(deps: GraphDeps):
    cfg = deps.config

    async def receive_incident(state: AgentState) -> dict[str, Any]:
        return {"round": 0, "call_counter": 0, "tool_calls_used": 0, "invalid_calls": 0, "pending_calls": [],
                "round_new_evidence": 0, "round_infra_failure": False, "stop_reason": None, "error": None}

    async def plan(state: AgentState) -> dict[str, Any]:
        executed = [{"tool": r["tool"], "arguments": r["arguments"], "outcome": r["outcome"]} for r in state.get("tool_runs", [])]
        payload = plan_payload(
            incident=state["incident"], round_no=state["round"],
            limits={"max_rounds": cfg.max_rounds, "max_tool_calls": cfg.max_tool_calls,
                    "tool_calls_used": state["tool_calls_used"], "max_calls_per_round": cfg.max_calls_per_round},
            tools=deps.toolbox.tools, executed=executed, evidence=[EvidenceItem(**e) for e in state.get("evidence", [])],
            budget_chars=cfg.max_prompt_evidence_chars)
        try:
            decision: PlanDecision = await ask_model(deps, "plan", PLAN_SYSTEM, payload, PLAN_SCHEMA, PlanDecision)
        except LLMError as exc:
            return {"stop_reason": "llm_error", "pending_calls": [], "error": {"kind": f"llm_{exc.kind}", "message": str(exc)}}
        except ModelOutputInvalid:
            return {"stop_reason": "model_output_invalid", "pending_calls": [],
                    "limitations": ["The planning model returned invalid output twice, so the investigation stopped early."]}

        log = {"round": state["round"] + 1, "action": decision.action, "rationale": safe_text(decision.rationale, 300)}
        if decision.action == "finish" or not decision.tool_calls:
            note = [] if state.get("evidence") else ["The planner chose not to run any diagnostic tool."]
            return {"stop_reason": "planner_finished", "pending_calls": [], "plan_log": [log], "limitations": note}

        pending: list[dict[str, Any]] = []
        for call in decision.tool_calls[: cfg.max_calls_per_round]:
            try:
                args = json.loads(call.arguments_json) if call.arguments_json.strip() else {}
                pending.append({"tool": call.tool, "arguments": args if isinstance(args, dict) else None,
                                "parse_error": None if isinstance(args, dict) else "arguments must be a JSON object"})
            except ValueError:
                pending.append({"tool": call.tool, "arguments": None, "parse_error": "arguments_json is not valid JSON"})
        extra = len(decision.tool_calls) - len(pending)
        return {"pending_calls": pending, "plan_log": [log],
                "limitations": [f"{extra} planned call(s) beyond the per-round limit of {cfg.max_calls_per_round} were not run."] if extra > 0 else []}

    async def execute_tools(state: AgentState) -> dict[str, Any]:
        round_no = state["round"] + 1
        used, invalid, counter = state["tool_calls_used"], state["invalid_calls"], state["call_counter"]
        seen = {_call_key(r["tool"], r["arguments"]) for r in state.get("tool_runs", []) if r["outcome"] in _OK_OUTCOMES}
        runs: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        notes: list[str] = []
        attempted = usable = 0
        infra = []
        for call in state.get("pending_calls", []):
            counter += 1
            call_id = f"E{counter}"
            tool, args = call["tool"], call.get("arguments")
            run = ToolRun(call_id=call_id, round=round_no, tool=safe_text(tool, 60), arguments=args or {}, outcome="ok")
            if args is None:
                run.outcome, run.detail = "invalid_arguments", call.get("parse_error")
                invalid += 1
            elif tool not in deps.toolbox.tools:
                run.outcome, run.detail = "not_approved", f"{safe_text(tool, 60)!r} is not an approved, discovered tool."
                invalid += 1
            elif used >= cfg.max_tool_calls:
                run.outcome, run.detail = "budget_exceeded", f"The tool-call budget of {cfg.max_tool_calls} is used up."
            elif _call_key(tool, args) in seen:
                run.outcome, run.detail = "duplicate_skipped", "Identical call already completed."
            elif infra and infra[-1] and usable == 0:  # the server/database just failed: do not keep calling it
                run.outcome, run.detail = "skipped", "Skipped: the MCP server or its database is unavailable."
            else:
                outcome = await deps.toolbox.call(tool, args)
                used += 1
                attempted += 1
                run.outcome, run.detail = outcome.outcome, outcome.detail
                if outcome.outcome == "invalid_arguments":
                    invalid += 1
                if outcome.structured is not None:
                    items = build_evidence(call_id, outcome.structured, cfg.max_evidence_chars)
                    run.evidence_ids = [i.evidence_id for i in items]
                    evidence += [i.model_dump() for i in items]
                    usable += sum(1 for i in items if i.status != "error")
                    markers = find_injection_markers(json.dumps(outcome.structured, default=str))
                    if markers:
                        notes.append(f"Instruction-like text ({', '.join(markers)}) appeared in the output of {run.tool} ({call_id}); "
                                     "it was treated as data and ignored.")
                    if all(i.status == "error" and i.error_code in _INFRA_CODES for i in items):
                        infra.append(True)
                    else:
                        infra.append(False)
                else:
                    infra.append(outcome.outcome in ("connection_error", "timeout"))
                seen.add(_call_key(tool, args))
            runs.append(run.model_dump())
            deps.progress(f"[round {round_no}] {run.tool} {json.dumps(run.arguments, sort_keys=True) if run.arguments else ''} -> {run.outcome}"
                          + (f" ({', '.join(sorted({e['status'] + '/' + e['severity'] for e in evidence if e['call_id'] == call_id}))})"
                             if run.evidence_ids else ""))
        return {"tool_runs": runs, "evidence": evidence, "security_notes": notes, "tool_calls_used": used, "invalid_calls": invalid,
                "call_counter": counter, "pending_calls": [], "round_new_evidence": usable,
                "round_infra_failure": bool(attempted) and all(infra) and usable == 0}

    async def evaluate(state: AgentState) -> dict[str, Any]:
        done = state["round"] + 1
        if state.get("round_infra_failure"):
            stop = "infrastructure_failure"
        elif state["tool_calls_used"] >= cfg.max_tool_calls:
            stop = "tool_budget_exhausted"
        elif state["invalid_calls"] >= cfg.max_invalid_calls:
            stop = "invalid_calls_limit"
        elif state.get("round_new_evidence", 0) == 0:
            stop = "no_new_evidence"
        elif done >= cfg.max_rounds:
            stop = "round_budget_exhausted"
        else:
            stop = None
        return {"round": done, "stop_reason": stop}

    async def generate_report(state: AgentState) -> dict[str, Any]:
        evidence = [EvidenceItem(**e) for e in state.get("evidence", [])]
        runs = [ToolRun(**r) for r in state.get("tool_runs", [])]
        stop = state.get("stop_reason") or "planner_finished"
        limitations = list(state.get("limitations", []))
        error = ReportError(**state["error"]) if state.get("error") else None
        draft: ReportDraft | None = None
        if error is None and evidence:
            payload = report_payload(incident=state["incident"], evidence=evidence,
                                     tool_runs=[{k: r.model_dump()[k] for k in ("call_id", "tool", "arguments", "outcome")} for r in runs],
                                     stop_reason=stop, budget_chars=cfg.max_prompt_evidence_chars)
            try:
                draft = await ask_model(deps, "report", REPORT_SYSTEM, payload, REPORT_SCHEMA, ReportDraft)
            except LLMError as exc:
                error = ReportError(kind=f"llm_{exc.kind}", message=str(exc))
            except ModelOutputInvalid:
                limitations.append("The report-writing model returned invalid output twice; this report contains tool facts only, "
                                   "with no model interpretation.")
        if error:
            limitations.append(error.message)
        limitations += {
            "tool_budget_exhausted": [f"Stopped at the tool-call budget ({cfg.max_tool_calls}); more diagnostics could add evidence."],
            "round_budget_exhausted": [f"Stopped at the round budget ({cfg.max_rounds}); more diagnostics could add evidence."],
            "invalid_calls_limit": ["Stopped after repeated invalid or unapproved tool calls from the planner."],
            "infrastructure_failure": ["The MCP server or its database was unavailable; no further calls were attempted."],
        }.get(stop, [])
        report: InvestigationReport = finalize_report(
            investigation_id=state["investigation_id"], incident=state["incident"], mode=deps.llm.mode, model=deps.llm.name,
            tool_runs=runs, evidence=evidence, limitations=limitations, security_notes=state.get("security_notes", []),
            rounds_used=state["round"], tool_calls_used=state["tool_calls_used"], stop_reason=stop,
            approved_tools=sorted(deps.toolbox.tools), draft=draft, error=error)
        return {"report": report.model_dump(mode="json")}

    def after_plan(state: AgentState) -> str:
        return "generate_report" if state.get("stop_reason") or not state.get("pending_calls") else "execute_tools"

    def after_evaluate(state: AgentState) -> str:
        return "generate_report" if state.get("stop_reason") else "plan"

    graph = StateGraph(AgentState)
    graph.add_node("receive_incident", receive_incident)
    graph.add_node("plan", plan)
    graph.add_node("execute_tools", execute_tools)
    graph.add_node("evaluate", evaluate)
    graph.add_node("generate_report", generate_report)
    graph.add_edge(START, "receive_incident")
    graph.add_edge("receive_incident", "plan")
    graph.add_conditional_edges("plan", after_plan, {"execute_tools": "execute_tools", "generate_report": "generate_report"})
    graph.add_edge("execute_tools", "evaluate")
    graph.add_conditional_edges("evaluate", after_evaluate, {"plan": "plan", "generate_report": "generate_report"})
    graph.add_edge("generate_report", END)
    return graph.compile()
