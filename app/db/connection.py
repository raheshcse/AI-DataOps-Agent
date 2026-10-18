"""SQL Server connection handling.

Secrets never leave this module in clear text: ``describe_target`` and
``build_odbc_connection_string(..., mask=True)`` are the only forms that may be
logged or shown. ``pyodbc`` is imported lazily (by SQLAlchemy) so the rest of the
application, and the unit tests, work on machines without an ODBC driver.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.core.logging import redact

LOCAL_HOSTS = {"localhost", ".", "(local)", "127.0.0.1", "::1", "(localdb)"}


class DatabaseConfigError(Exception):
    """The database settings are incomplete or inconsistent (message is safe to show)."""


class DatabaseUnavailable(Exception):
    """SQL Server (or its Python driver) cannot be reached (message is safe to show)."""


def _braced(value: str) -> str:
    """Quote an ODBC attribute value so ``;`` and ``}`` cannot inject attributes."""
    return "{" + value.replace("}", "}}") + "}"


def build_odbc_connection_string(
    settings: Settings,
    *,
    database: str | None = None,
    admin: bool = False,
    mask: bool = False,
) -> str:
    """Build the ODBC connection string.

    ``admin=True`` always uses Windows integrated authentication (setup tasks such
    as creating the database). Otherwise ``DB_AUTH_MODE`` decides.
    """
    parts = [
        f"DRIVER={_braced(settings.db_driver)}",
        f"SERVER={settings.db_server}",
        f"DATABASE={database or settings.db_name}",
        f"Encrypt={'yes' if settings.db_encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if settings.db_trust_server_certificate else 'no'}",
    ]
    if admin or settings.db_auth_mode == "windows":
        parts.append("Trusted_Connection=yes")
    else:
        if not settings.db_user or settings.db_password is None:
            raise DatabaseConfigError(
                "DB_AUTH_MODE=sql requires DB_USER and DB_PASSWORD to be set."
            )
        password = "***" if mask else settings.db_password.get_secret_value()
        parts.append(f"UID={_braced(settings.db_user)}")
        parts.append(f"PWD={_braced(password)}")
    return ";".join(parts) + ";"


def describe_target(settings: Settings, *, admin: bool = False) -> dict[str, str]:
    """Non-secret description of where connections go (safe to print or log)."""
    return {
        "server": settings.db_server,
        "database": settings.db_name,
        "driver": settings.db_driver,
        "auth": "windows (integrated)" if admin or settings.db_auth_mode == "windows" else "sql login",
        "encrypt": str(settings.db_encrypt).lower(),
        "trust_server_certificate": str(settings.db_trust_server_certificate).lower(),
    }


def is_local_server(server: str) -> bool:
    """True if ``server`` (``host``, ``host\\INSTANCE``, ``tcp:host,port``) is this machine."""
    host = server.strip().lower()
    if host.startswith("tcp:"):
        host = host[4:]
    host = host.split("\\", 1)[0].split(",", 1)[0]
    return host in LOCAL_HOSTS


def make_engine(
    settings: Settings, *, database: str | None = None, admin: bool = False
) -> Engine:
    conn_str = build_odbc_connection_string(settings, database=database, admin=admin)
    url = URL.create("mssql+pyodbc", query={"odbc_connect": conn_str})
    try:
        return create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"timeout": settings.db_login_timeout_seconds},
        )
    except ImportError as exc:  # pyodbc or the ODBC runtime is missing
        raise DatabaseUnavailable(
            "The SQL Server Python driver (pyodbc / ODBC runtime) is not available."
        ) from exc


def safe_error_summary(exc: BaseException) -> str:
    """One redacted line describing a driver error, for local CLI troubleshooting.

    Not used in API responses (driver messages can contain server details).
    """
    inner = getattr(exc, "orig", None) or exc
    first_line = str(inner).strip().splitlines()[0] if str(inner).strip() else type(inner).__name__
    return redact(first_line)[:300]


def check_connection(engine: Engine) -> None:
    """Run ``SELECT 1``; raise ``DatabaseUnavailable`` (safe message) on any failure."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, ImportError, OSError) as exc:
        raise DatabaseUnavailable(
            "Could not connect to SQL Server. Check that the service is running, the "
            "server/instance name, the ODBC driver and the credentials."
        ) from exc
