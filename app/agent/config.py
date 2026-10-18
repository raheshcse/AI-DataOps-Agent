"""Agent configuration: model name, API-key state and the investigation budgets.

The OpenAI key is read exactly like the rest of the application (environment or the git-ignored ``.env``)
through ``app.config``. Nothing here requires a key to import or to build a config; a key is only demanded
when a live model request is about to be made, and its value is never printed, logged or put in an error.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

KeyState = Literal["configured", "missing", "placeholder", "malformed"]

_PLACEHOLDER_MARKERS = ("replace-with", "your-openai", "your_openai", "placeholder", "changeme", "change-me",
                        "<your", "xxxxxxxx", "example")


class MissingApiKey(Exception):
    """A live model request was attempted without a usable OPENAI_API_KEY."""

    def __init__(self, state: KeyState):
        self.state = state
        super().__init__(
            {"missing": "OPENAI_API_KEY is not set.",
             "placeholder": "OPENAI_API_KEY still contains the .env.example placeholder.",
             "malformed": "OPENAI_API_KEY does not look like an API key (too short or contains whitespace).",
             "configured": "OPENAI_API_KEY is configured."}[state]
            + " Add your key to the local .env file, or use --offline for the deterministic offline mode.")


def classify_key(value: str | None) -> KeyState:
    """Classify a key without ever echoing it."""
    if value is None or not value.strip():
        return "missing"
    text = value.strip()
    if any(marker in text.lower() for marker in _PLACEHOLDER_MARKERS):
        return "placeholder"
    if len(text) < 20 or any(ch.isspace() for ch in text):
        return "malformed"
    return "configured"


class _AgentEnv(BaseSettings):
    """AGENT_* overrides. Same .env handling as the application settings."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      case_sensitive=False, env_prefix="AGENT_")
    max_rounds: int = Field(default=3, ge=1, le=6)
    max_tool_calls: int = Field(default=12, ge=1, le=20)
    max_calls_per_round: int = Field(default=4, ge=1, le=8)
    tool_timeout_seconds: float = Field(default=60.0, ge=1, le=300)
    llm_timeout_seconds: float = Field(default=60.0, ge=1, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    max_incident_chars: int = Field(default=2000, ge=50, le=10000)


class AgentConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str = Field(default="gpt-4o-mini", min_length=1)
    api_key: SecretStr | None = Field(default=None, repr=False)
    max_rounds: int = Field(default=3, ge=1, le=6)
    max_tool_calls: int = Field(default=12, ge=1, le=20)
    max_calls_per_round: int = Field(default=4, ge=1, le=8)
    tool_timeout_seconds: float = Field(default=60.0, ge=1, le=300)
    llm_timeout_seconds: float = Field(default=60.0, ge=1, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    max_incident_chars: int = Field(default=2000, ge=50, le=10000)
    max_invalid_calls: int = Field(default=3, ge=1, le=10)
    max_evidence_chars: int = Field(default=4000, ge=500, le=20000)
    max_prompt_evidence_chars: int = Field(default=24000, ge=2000, le=100000)

    @property
    def key_state(self) -> KeyState:
        return classify_key(self.api_key.get_secret_value() if self.api_key else None)

    def require_api_key(self) -> str:
        """The key, or MissingApiKey. Only the live OpenAI adapter calls this."""
        state = self.key_state
        if state != "configured" or self.api_key is None:
            raise MissingApiKey(state)
        return self.api_key.get_secret_value().strip()


def load_agent_config(**overrides: object) -> AgentConfig:
    """Build the config from OPENAI_* (application settings) and AGENT_* variables; ``overrides`` win."""
    from app.config import get_settings

    settings = get_settings()
    env = _AgentEnv()
    values: dict[str, object] = {
        "model": settings.openai_model, "api_key": settings.openai_api_key,
        "max_rounds": env.max_rounds, "max_tool_calls": env.max_tool_calls,
        "max_calls_per_round": env.max_calls_per_round, "tool_timeout_seconds": env.tool_timeout_seconds,
        "llm_timeout_seconds": env.llm_timeout_seconds, "llm_max_retries": env.llm_max_retries,
        "max_incident_chars": env.max_incident_chars,
    }
    values.update({k: v for k, v in overrides.items() if v is not None})
    return AgentConfig(**values)
