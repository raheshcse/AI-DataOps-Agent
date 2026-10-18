"""Shared helpers for the command-line scripts (run them as ``python -m scripts.<name>``)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings
from app.db.connection import (
    DatabaseConfigError,
    DatabaseUnavailable,
    describe_target,
    make_engine,
    safe_error_summary,
)
from app.db.repository import SqlRepository
from app.db.safety import UnsafeTarget
from app.simulation.clock import SimulationNotSeeded
from app.simulation.runlog import RunLogStore
from app.simulation.service import (
    ScenarioRestoreRefused,
    SeedConflict,
    SimulationService,
    UnknownPipeline,
    UnknownScenario,
)

EXIT_OK, EXIT_CHECK_FAILED, EXIT_ENV, EXIT_REFUSED = 0, 1, 2, 3


def load_settings() -> Settings:
    try:
        return get_settings()
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
        print(f"Configuration error: invalid value(s) for {', '.join(fields)} (values are not shown).")
        print("Check your .env against .env.example.")
        raise SystemExit(EXIT_ENV)


def log_dir(settings: Settings) -> Path:
    return Path(settings.sim_log_dir).resolve()


def make_service(settings: Settings, *, admin: bool = False) -> SimulationService:
    engine = make_engine(settings, admin=admin)
    return SimulationService(SqlRepository(engine), RunLogStore(log_dir(settings)),
                             seed=settings.sim_seed,
                             freshness_threshold_minutes=settings.freshness_threshold_minutes)


def print_target(settings: Settings, *, admin: bool = False) -> None:
    t = describe_target(settings, admin=admin)
    print(f"Target: server={t['server']}  database={t['database']}  auth={t['auth']}  "
          f"driver={t['driver']}  encrypt={t['encrypt']}  trust_cert={t['trust_server_certificate']}")


def run_cli(main: Callable[[], int]) -> None:
    """Run ``main`` and turn expected failures into short, secret-free messages."""
    try:
        code = main()
    except DatabaseConfigError as exc:
        print(f"Configuration error: {exc}")
        code = EXIT_ENV
    except DatabaseUnavailable as exc:
        print(f"Database unavailable: {exc}")
        print("Run `python -m scripts.check_prereqs` for a step-by-step diagnosis.")
        code = EXIT_ENV
    except SimulationNotSeeded:
        print("The database has no baseline yet. Run: python -m scripts.db_init  then  python -m scripts.db_seed")
        code = EXIT_ENV
    except SQLAlchemyError as exc:
        summary = safe_error_summary(exc)
        print(f"Database error: {summary}")
        if "Invalid object name" in summary:
            print("The tables do not exist yet. Run: python -m scripts.db_init  then  python -m scripts.db_seed")
        elif "Cannot open database" in summary or "Login failed" in summary:
            print("Check DB_NAME / DB_AUTH_MODE / credentials, and that the database exists "
                  "(python -m scripts.db_init).")
        else:
            print("Run `python -m scripts.check_prereqs` for a step-by-step diagnosis.")
        code = EXIT_ENV
    except UnsafeTarget as exc:
        print(str(exc))
        code = EXIT_REFUSED
    except (SeedConflict, ScenarioRestoreRefused, UnknownPipeline, UnknownScenario) as exc:
        print(str(exc))
        code = EXIT_REFUSED
    sys.exit(code)


def table(rows: list[dict], columns: list[tuple[str, str, int]]) -> str:
    """Fixed-width text table. ``columns`` = (key, header, width)."""
    def cell(v, w):
        text = "" if v is None else (v.isoformat() if hasattr(v, "isoformat") else str(v))
        return (text[: w - 1] + "…" if len(text) > w else text).ljust(w)

    head = "  ".join(h.ljust(w) for _, h, w in columns)
    lines = [head, "  ".join("-" * w for _, _, w in columns)]
    lines += ["  ".join(cell(r.get(k), w) for k, _, w in columns) for r in rows]
    return "\n".join(lines)
