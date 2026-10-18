"""Helpers shared by the diagnostic tools (no tool-specific logic here)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.core.logging import redact
from app.diagnostics.models import InvalidInput

PIPELINE_ID_RE = re.compile(r"[A-Za-z0-9_]{1,50}")
MAX_LOG_BYTES = 2_000_000
MAX_MESSAGE_CHARS = 1000


class LogTooLarge(Exception):
    """The log file exceeds ``MAX_LOG_BYTES`` and is not read."""


def check_pipeline_id(value: Any) -> str:
    if not isinstance(value, str) or not PIPELINE_ID_RE.fullmatch(value):
        raise InvalidInput("pipeline_id must be 1-50 letters, digits or underscores.")
    return value


def run_view(run: dict[str, Any] | None) -> dict[str, Any] | None:
    """The run fields diagnostics expose. ``scenario_id`` is deliberately not included:
    it is simulation ground truth, and diagnoses must rest on operational evidence."""
    if run is None:
        return None
    keys = ("run_id", "pipeline_id", "batch_date", "started_at", "finished_at", "status", "duration_ms",
            "records_expected", "records_processed", "error_code", "error_message", "trigger_type")
    return {k: run.get(k) for k in keys}


def _clean(value: Any, depth: int = 0) -> Any:
    """Redact every string inside a log value. Log content is untrusted input."""
    if isinstance(value, str):
        return redact(value)
    if depth >= 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {redact(str(k)): _clean(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v, depth + 1) for v in value]
    return value


def _text(value: Any, limit: int = MAX_MESSAGE_CHARS) -> str | None:
    if value is None:
        return None
    text = redact(str(value))
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def event_view(line_no: int, obj: dict[str, Any]) -> dict[str, Any]:
    level = obj.get("level")
    message = obj.get("message")
    data = obj.get("data")
    return {
        "line": line_no,
        "timestamp": obj.get("timestamp"),
        "severity": str(level).upper() if level is not None else None,
        "component": _text(obj.get("component"), 100),
        "event": _text(obj.get("event"), 100),
        "message": _text(message),
        "error_code": _text(obj.get("error_code"), 100),
        "data": _clean(data) if isinstance(data, dict) else None,
    }


def parse_log_file(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(events, malformed_lines)``. A bad line never hides the good ones around it."""
    if path.stat().st_size > MAX_LOG_BYTES:
        raise LogTooLarge
    events: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            malformed.append({"line": line_no, "reason": "invalid JSON", "excerpt": redact(raw[:80])})
            continue
        if not isinstance(obj, dict):
            malformed.append({"line": line_no, "reason": "not a JSON object", "excerpt": redact(raw[:80])})
            continue
        events.append(event_view(line_no, obj))
    return events, malformed
