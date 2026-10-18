"""Logging setup with redaction of sensitive values."""

import logging
import re

_REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # OpenAI-style keys
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), _REDACTED),
    # "Bearer <token>" (handled first so the token itself is masked)
    (re.compile(r"(?i)\bbearer\s+[^\s,;\"']+"), f"Bearer {_REDACTED}"),
    # key=value or key: value for common secret names
    (
        re.compile(
            r"(?i)\b(api[_-]?key|authorization|token|secret|password)"
            r"(\s*[=:]\s*|\s+)([^\s,;&\"']+)"
        ),
        rf"\1\2{_REDACTED}",
    ),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactionFilter(logging.Filter):
    """Scrubs secrets from log records before they are emitted.

    String arguments are redacted individually so the record keeps its shape
    (uvicorn's access formatter unpacks ``record.args``). If the formatted message
    still contains a secret, the message is replaced by its redacted form.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact(a) if isinstance(a, str) else a for a in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                k: redact(v) if isinstance(v, str) else v
                for k, v in record.args.items()
            }
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
    for handler in root.handlers:
        if not any(isinstance(f, RedactionFilter) for f in handler.filters):
            handler.addFilter(RedactionFilter())
    # Uvicorn's access log includes the query string, so scrub it as well.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        named = logging.getLogger(name)
        if not any(isinstance(f, RedactionFilter) for f in named.filters):
            named.addFilter(RedactionFilter())
    root.setLevel(level)
