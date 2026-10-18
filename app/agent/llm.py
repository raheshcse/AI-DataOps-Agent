"""The model interface and the OpenAI adapter.

``ChatModel`` is the only thing the graph knows about a model: one async method that takes a task name, a
system prompt and a JSON-able payload and returns a JSON object. Implementations:

* ``OpenAIChatModel`` (live): needs a valid OPENAI_API_KEY, which is checked *before* any client is built, so
  a missing/placeholder key can never produce a network request. Provider errors are mapped to ``LLMError``
  with fixed, key-free messages; provider error text is never copied (it can echo parts of a key).
* ``app.agent.offline.OfflineHeuristicModel``: deterministic, no network, no key. Not a language model.
* tests use scripted fakes.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from app.agent.config import AgentConfig

Task = Literal["plan", "report", "smoke"]
LLMErrorKind = Literal["auth", "rate_limit", "timeout", "connection", "bad_request", "server", "refused",
                       "empty", "malformed", "internal"]

_MESSAGES: dict[str, str] = {
    "auth": "OpenAI rejected the request as unauthorised. Check OPENAI_API_KEY and that the model is available to your account.",
    "rate_limit": "OpenAI rate limit or quota reached. Check your plan and usage, then retry.",
    "timeout": "The OpenAI request timed out.",
    "connection": "Could not reach the OpenAI API (network or proxy problem).",
    "bad_request": "OpenAI rejected the request as invalid (check OPENAI_MODEL supports structured outputs).",
    "server": "The OpenAI service returned an error.",
    "refused": "The model declined to answer.",
    "empty": "The model returned an empty answer.",
    "malformed": "The model's answer was not valid JSON of the expected shape.",
    "internal": "The model client failed unexpectedly.",
}
# Failures that retrying within this investigation cannot fix.
FATAL_KINDS = frozenset({"auth", "rate_limit", "timeout", "connection", "bad_request", "server", "refused", "internal"})


class LLMError(Exception):
    def __init__(self, kind: LLMErrorKind, status_code: int | None = None):
        self.kind = kind
        self.status_code = status_code
        suffix = f" (HTTP {status_code})" if status_code else ""
        super().__init__(_MESSAGES[kind] + suffix)


class ChatModel(Protocol):
    mode: Literal["live", "offline", "scripted"]
    name: str

    async def complete_json(self, task: Task, system: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]: ...


# ----------------------------------------------------------------- JSON schemas (OpenAI strict mode)
def _arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


_STR = {"type": "string"}
_NULLABLE_STR = {"type": ["string", "null"]}
_IDS = _arr(_STR)

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["action", "tool_calls", "rationale"],
    "properties": {
        "action": {"type": "string", "enum": ["call_tools", "finish"]},
        "tool_calls": _arr({"type": "object", "additionalProperties": False, "required": ["tool", "arguments_json"],
                            "properties": {"tool": _STR, "arguments_json": _STR}}),
        "rationale": _STR,
    },
}

REPORT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "hypotheses", "root_cause", "impact", "confidence", "confidence_explanation",
                 "recommended_next_steps", "limitations"],
    "properties": {
        "summary": _STR,
        "hypotheses": _arr({"type": "object", "additionalProperties": False, "required": ["statement", "evidence_ids"],
                            "properties": {"statement": _STR, "evidence_ids": _IDS}}),
        "root_cause": {"type": "object", "additionalProperties": False, "required": ["statement", "evidence_ids", "explanation"],
                       "properties": {"statement": _NULLABLE_STR, "evidence_ids": _IDS, "explanation": _STR}},
        "impact": {"type": "object", "additionalProperties": False, "required": ["statement", "evidence_ids"],
                   "properties": {"statement": _NULLABLE_STR, "evidence_ids": _IDS}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "confidence_explanation": _STR,
        "recommended_next_steps": _arr(_STR),
        "limitations": _arr(_STR),
    },
}

SMOKE_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False, "required": ["ok"],
                                "properties": {"ok": {"type": "boolean"}}}

_MAX_TOKENS = {"plan": 1200, "report": 3000, "smoke": 30}
_MAX_RESPONSE_CHARS = 100_000


class OpenAIChatModel:
    """Live adapter. Constructing it demands a valid key; no client exists until the first request."""

    mode: Literal["live"] = "live"

    def __init__(self, config: AgentConfig, client: Any = None):
        self._config = config
        self.name = config.model
        self._api_key = config.require_api_key() if client is None else ""  # MissingApiKey before anything else
        self._client = client
        self.last_usage: dict[str, int] | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            import openai

            self._client = openai.AsyncOpenAI(api_key=self._api_key, timeout=self._config.llm_timeout_seconds,
                                              max_retries=self._config.llm_max_retries)
        return self._client

    async def complete_json(self, task: Task, system: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        import openai

        user = json.dumps(payload, ensure_ascii=True, default=str)
        try:
            response = await self._get_client().chat.completions.create(
                model=self._config.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_schema", "json_schema": {"name": f"dataops_{task}", "strict": True, "schema": schema}},
                max_completion_tokens=_MAX_TOKENS[task],
            )
        except openai.APITimeoutError:
            raise LLMError("timeout") from None
        except openai.APIConnectionError:
            raise LLMError("connection") from None
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMError("auth", getattr(exc, "status_code", None)) from None
        except openai.RateLimitError as exc:
            raise LLMError("rate_limit", getattr(exc, "status_code", None)) from None
        except openai.BadRequestError as exc:
            raise LLMError("bad_request", getattr(exc, "status_code", None)) from None
        except openai.APIStatusError as exc:
            raise LLMError("server", getattr(exc, "status_code", None)) from None
        except openai.OpenAIError:
            raise LLMError("internal") from None

        try:
            choice = response.choices[0]
            if getattr(choice.message, "refusal", None):
                raise LLMError("refused")
            content = choice.message.content
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.last_usage = {"prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                                   "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0)}
        except LLMError:
            raise
        except Exception:  # noqa: BLE001 - unexpected response object
            raise LLMError("malformed") from None
        if not content or not str(content).strip():
            raise LLMError("empty")
        if len(content) > _MAX_RESPONSE_CHARS:
            raise LLMError("malformed")
        try:
            data = json.loads(content)
        except ValueError:
            raise LLMError("malformed") from None
        if not isinstance(data, dict):
            raise LLMError("malformed")
        return data
