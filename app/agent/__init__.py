"""Phase 5: an AI DataOps investigation agent (OpenAI + LangGraph + the Phase 4 MCP server).

The agent reaches diagnostics only through MCP. It has no database access, no SQL, and no write or recovery
capability. Governance (Phase 6) is not part of this package.
"""

from app.agent.config import AgentConfig, MissingApiKey, classify_key, load_agent_config
from app.agent.llm import ChatModel, LLMError, OpenAIChatModel
from app.agent.models import InvestigationReport
from app.agent.offline import OfflineHeuristicModel
from app.agent.report import render_text
from app.agent.runner import investigate, investigate_sync
from app.agent.safety import InvalidIncident

__all__ = ["AgentConfig", "ChatModel", "InvalidIncident", "InvestigationReport", "LLMError", "MissingApiKey",
           "OfflineHeuristicModel", "OpenAIChatModel", "classify_key", "investigate", "investigate_sync",
           "load_agent_config", "render_text"]
