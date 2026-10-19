"""Fail-safe start-up validation for production mode (Phase 9).

Only ``APP_ENV=production`` is checked; development and test behave exactly as before. In production the process
REFUSES TO START when a setting that decides where durable data and governance records live is not stated
explicitly, or when a development-only authenticator is selected. Nothing is defaulted silently, and the error names
the variables only: never their values.

This adds no new setting and no new authentication mode. It only validates the existing ones.
"""

from __future__ import annotations

import os
from typing import Mapping

from pydantic import ValidationError

from app.config import get_settings

# Where durable state goes must be explicit in production, so it lands on the persistent volume and never on an
# ephemeral working directory or the default simulation database.
REQUIRED_IN_PRODUCTION = ("DATABASE_URL", "APPROVAL_STORE_PATH", "GOVERNANCE_LEDGER_PATH", "DB_NAME")


class StartupConfigError(RuntimeError):
    """Raised at start-up in production. ``problems`` holds variable names and fixed phrases, never values."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Invalid production configuration: " + "; ".join(problems))


def _app_env(env: Mapping[str, str]) -> str:
    try:
        return get_settings().app_env
    except ValidationError:
        return (env.get("APP_ENV") or "development").strip().lower()


def production_config_problems(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    if _app_env(env) != "production":
        return []
    problems = [f"{name} must be set explicitly" for name in REQUIRED_IN_PRODUCTION if not (env.get(name) or "").strip()]
    if (env.get("APPROVAL_AUTH_MODE") or "disabled").strip().lower() == "dev_static":
        problems.append("APPROVAL_AUTH_MODE=dev_static is development-only and is refused in production")
    try:
        get_settings()
    except ValidationError as exc:
        problems.append("invalid settings: " + ", ".join(sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})))
    return problems


def validate_startup(environ: Mapping[str, str] | None = None) -> None:
    problems = production_config_problems(environ)
    if problems:
        raise StartupConfigError(problems)
