"""Guards for destructive operations (reset). Fail closed: any doubt refuses."""

from __future__ import annotations

from app.config import Settings
from app.db.connection import is_local_server

ALLOWED_DB_SUFFIXES = ("_sim", "_test")


class UnsafeTarget(Exception):
    """The requested destructive operation was refused (message is safe to show)."""


def assert_safe_reset_target(settings: Settings, database_arg: str | None, confirmed: bool) -> None:
    if not confirmed:
        raise UnsafeTarget("Refusing: pass --yes-delete-all-data to confirm you understand all "
                           "simulation rows in the target database will be deleted.")
    if settings.app_env == "production":
        raise UnsafeTarget("Refusing: APP_ENV=production.")
    if not is_local_server(settings.db_server):
        raise UnsafeTarget(f"Refusing: DB_SERVER '{settings.db_server}' is not a local server.")
    if not database_arg or database_arg != settings.db_name:
        raise UnsafeTarget("Refusing: --database must be given and must exactly match DB_NAME "
                           f"('{settings.db_name}').")
    if not settings.db_name.lower().endswith(ALLOWED_DB_SUFFIXES):
        raise UnsafeTarget("Refusing: the database name must end with one of "
                           f"{', '.join(ALLOWED_DB_SUFFIXES)} so a real database is never reset.")
