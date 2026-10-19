"""Run the EXISTING Phase 5 investigation workflow for a stored incident and persist the result.

The agent, its MCP tools and its report model are reused unchanged. This module only (1) records that a run was
requested, (2) runs ``app.agent.investigate`` and (3) stores the report or a safe failure. It never reports success
before the agent has actually returned, and a process restart marks unfinished runs as failed ('interrupted').
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.agent import AgentConfig, ChatModel, InvalidIncident, LLMError, MissingApiKey, investigate
from app.agent.safety import clean_incident
from app.persistence import OperationsStore

logger = logging.getLogger("app.investigations")


def incident_text(incident: dict[str, Any]) -> str:
    return f"{incident['title']}: {incident['description']}"


def validate_incident_text(title: str, description: str, max_chars: int) -> None:
    """Raises InvalidIncident exactly as the agent would, so the API rejects early what the agent would refuse."""
    clean_incident(f"{title}: {description}", max_chars)


class InvestigationService:
    def __init__(self, store: OperationsStore, *, llm_factory: Callable[[], ChatModel],
                 mcp_target_factory: Callable[[], Any] = lambda: None, agent_config: AgentConfig | None = None,
                 mode: str = "offline") -> None:
        self.store, self.llm_factory, self.mcp_target_factory = store, llm_factory, mcp_target_factory
        self.config = agent_config or AgentConfig()
        self.mode = mode

    @property
    def max_incident_chars(self) -> int:
        return self.config.max_incident_chars

    def start(self, incident: dict[str, Any], requested_by: str) -> dict[str, Any]:
        """Record the request (raises InvestigationActive if one is already pending/running). Nothing runs yet."""
        return self.store.create_investigation(incident_id=incident["incident_id"], mode=self.mode, requested_by=requested_by)

    async def execute(self, investigation_id: str) -> None:
        """Run the agent and persist the outcome. Never raises: every failure becomes a stored, safe status."""
        try:
            if not self.store.mark_investigation_running(investigation_id):
                return
            row = self.store.get_investigation(investigation_id)
            incident = self.store.get_incident(row["incident_id"]) if row else None
            if row is None or incident is None:
                self.store.fail_investigation(investigation_id, error_kind="not_found", error_message="The incident no longer exists.")
                return
            try:
                report = await investigate(incident_text(incident), config=self.config, llm=self.llm_factory(),
                                           mcp_target=self.mcp_target_factory())
            except InvalidIncident:
                self.store.fail_investigation(investigation_id, error_kind="invalid_incident",
                                              error_message="The incident text was rejected by the investigation agent.")
                return
            except MissingApiKey:
                self.store.fail_investigation(investigation_id, error_kind="live_mode_unavailable",
                                              error_message="Live investigation mode needs a configured OpenAI key.")
                return
            except LLMError:
                self.store.fail_investigation(investigation_id, error_kind="model_error", error_message="The language model request failed.")
                return
            if report.error is not None:
                self.store.fail_investigation(investigation_id, error_kind=report.error.kind, error_message="The investigation could not collect evidence.")
                return
            self.store.complete_investigation(
                investigation_id, report=report.model_dump(mode="json"), report_status=report.status,
                summary=report.summary[:1000], evidence_refs=[e.evidence_id for e in report.evidence])
        except Exception as exc:  # noqa: BLE001 - last resort: record a generic failure, never raise into the server
            logger.error("investigation %s failed unexpectedly (%s)", investigation_id, type(exc).__name__)
            try:
                self.store.fail_investigation(investigation_id, error_kind="agent_failure", error_message="The investigation failed unexpectedly.")
            except Exception:  # noqa: BLE001
                logger.error("could not record the failure of investigation %s", investigation_id)
