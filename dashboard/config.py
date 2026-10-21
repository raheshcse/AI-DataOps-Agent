"""UI configuration from environment variables. No secrets are stored here, defaulted or displayed."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000/api/v1"


class ConfigError(ValueError):
    """Invalid UI configuration. The message never contains a token."""


def _num(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return min(max(value, low), high)


def validate_base_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("DASHBOARD_API_BASE_URL must be an http(s) URL such as http://127.0.0.1:8000/api/v1.")
    if parts.username or parts.password:
        raise ConfigError("DASHBOARD_API_BASE_URL must not contain credentials.")
    if parts.query or parts.fragment:
        raise ConfigError("DASHBOARD_API_BASE_URL must not contain a query string or fragment.")
    return url.strip().rstrip("/")


def api_base_url() -> str:
    return validate_base_url(os.environ.get("DASHBOARD_API_BASE_URL") or DEFAULT_API_BASE_URL)


def dashboard_token() -> str | None:
    """Token for the Operations Dashboard's read calls. Never displayed or logged."""
    return os.environ.get("DASHBOARD_API_TOKEN") or None


def investigate_token() -> str | None:
    """Optional token for starting investigations (needs ``investigation:run``; the viewer token cannot). When unset, the
    page asks the operator for their own token. Never displayed or logged."""
    return os.environ.get("DASHBOARD_INVESTIGATE_TOKEN") or None


def explorer_token() -> str | None:
    """Separate token for the VerbaLedger Explorer (needs ``ledger:read``). Never displayed or logged."""
    return os.environ.get("LEDGER_EXPLORER_API_TOKEN") or None


def _link(name: str, default: str) -> str:
    """A navigation link to the sibling app. Only http(s) URLs without credentials are accepted; otherwise the default."""
    raw = (os.environ.get(name) or "").strip()
    parts = urlsplit(raw) if raw else None
    if parts and parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password:
        return raw.rstrip("/")
    return default


def ops_dashboard_url() -> str:
    return _link("OPS_DASHBOARD_URL", "http://127.0.0.1:8501")


def ledger_explorer_url() -> str:
    return _link("LEDGER_EXPLORER_URL", "http://127.0.0.1:8502")


def request_timeout() -> float:
    return _num("DASHBOARD_REQUEST_TIMEOUT_SECONDS", 10.0, 1.0, 60.0)


def max_incidents() -> int:
    return int(_num("DASHBOARD_MAX_INCIDENTS", 300, 20, 1000))
