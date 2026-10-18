"""Input cleaning, redaction, bounding and prompt-injection markers.

Everything the agent retrieves (log text, database values, tool summaries) is untrusted data. These helpers
keep it bounded and redacted, and flag instruction-like text so the report can say it was seen and ignored.
They do not make injected text safe to obey; the real defences are structural (see docs/agent.md).
"""

from __future__ import annotations

import re
from typing import Any

from app.core.logging import redact

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

INJECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "ignore-instructions": re.compile(r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|your)\b"
                                      r"[^.\n]{0,30}\b(instruction|rule|prompt|direction)s?\b", re.I),
    "new-instructions": re.compile(r"\b(new|updated|real)\s+(system\s+)?(instruction|prompt|rule)s?\b", re.I),
    "role-override": re.compile(r"\b(you are now|act as|pretend to be|from now on you)\b", re.I),
    "system-prompt": re.compile(r"\b(system prompt|developer message|reveal your (prompt|instructions))\b", re.I),
    "tool-directive": re.compile(r"\b(call|invoke|run|execute|use)\s+(the\s+)?(tool|function|command|sql|shell|script)\b", re.I),
    "secret-request": re.compile(r"\b(print|send|reveal|show|leak|exfiltrate|return)\b[^.\n]{0,30}\b(api[ _-]?key|password|secret|token|credential)s?\b", re.I),
    "chat-markup": re.compile(r"(<\|im_start\|>|<\|im_end\|>|^\s*(system|assistant)\s*:|\[/?INST\]|###\s*system)", re.I | re.M),
}


class InvalidIncident(ValueError):
    """The incident description is unusable (message is safe to show)."""


def clean_incident(text: object, max_chars: int = 2000) -> str:
    if not isinstance(text, str):
        raise InvalidIncident("The incident description must be text.")
    cleaned = redact(_CONTROL.sub("", text)).strip()  # a pasted key must never reach a prompt or a report
    if not cleaned:
        raise InvalidIncident("The incident description is empty. Describe what went wrong, e.g. "
                              "\"Yesterday's order data is missing from the sales report.\"")
    if len(cleaned) > max_chars:
        raise InvalidIncident(f"The incident description is {len(cleaned)} characters; the limit is {max_chars}. "
                              "Shorten it to the symptoms you observed.")
    return cleaned


def safe_text(value: Any, limit: int = 300) -> str:
    """Redact, collapse whitespace and cap length. For any retrieved or error text that will be stored/printed."""
    text = redact(" ".join(str(value).split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def find_injection_markers(text: str) -> list[str]:
    return [label for label, pattern in INJECTION_PATTERNS.items() if pattern.search(text)]


def _compact(value: Any, depth: int, str_limit: int) -> Any:
    if isinstance(value, str):
        return safe_text(value, str_limit)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        if depth <= 0:
            return f"{{…{len(value)} keys}}"
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= 14:
                out["…"] = f"{len(value) - 14} more keys"
                break
            out[safe_text(k, 60)] = _compact(v, depth - 1, str_limit)
        return out
    if isinstance(value, (list, tuple)):
        if depth <= 0:
            return f"[…{len(value)} items]"
        items = [_compact(v, depth - 1, str_limit) for v in value[:5]]
        if len(value) > 5:
            items.append(f"…{len(value) - 5} more of {len(value)}")
        return items
    return safe_text(value, str_limit)


def digest_result(structured: dict[str, Any], limit_chars: int) -> tuple[dict[str, Any], bool]:
    """A bounded, redacted digest of one diagnostic result. Returns (digest, truncated)."""
    import json

    digest: dict[str, Any] = {
        "observed_value": _compact(structured.get("observed_value"), 3, 160),
        "expected_value": _compact(structured.get("expected_value"), 3, 160),
        "notes": [safe_text(n, 200) for n in (structured.get("notes") or [])[:5]],
        "parameters": _compact(structured.get("parameters") or {}, 2, 100),
        "evidence": [],
    }
    truncated = False
    for entry in structured.get("evidence") or []:
        candidate = _compact(entry, 3, 160)
        digest["evidence"].append(candidate)
        if len(json.dumps(digest, default=str)) > limit_chars:
            digest["evidence"].pop()
            truncated = True
            break
    if truncated:
        digest["evidence_note"] = (f"Only the first {len(digest['evidence'])} of "
                                   f"{len(structured.get('evidence') or [])} evidence entries are shown.")
    return digest, truncated
