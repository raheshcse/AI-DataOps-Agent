"""Diagnose the local SQL Server setup (read-only; no data is changed).

    python -m scripts.check_prereqs
"""

from __future__ import annotations

import platform
import struct
import sys

from sqlalchemy import text

from scripts._common import EXIT_ENV, EXIT_OK, load_settings, print_target, run_cli


def main() -> int:
    settings = load_settings()
    ok = True
    print(f"Python {platform.python_version()} ({struct.calcsize('P') * 8}-bit) on {platform.system()}")
    try:
        import pyodbc
    except ImportError as exc:
        print(f"[FAIL] pyodbc cannot be imported ({type(exc).__name__}). "
              "Run `pip install -r requirements.txt` inside the virtual environment.")
        return EXIT_ENV
    drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
    print(f"[ OK ] pyodbc {pyodbc.version}; SQL Server ODBC drivers: {', '.join(drivers) or 'none'}")
    if settings.db_driver not in drivers:
        ok = False
        print(f"[FAIL] DB_DRIVER '{settings.db_driver}' is not installed. Set DB_DRIVER to one of the above.")
    print_target(settings, admin=True)

    from app.db.connection import check_connection, make_engine

    engine = make_engine(settings, database="master", admin=True)
    check_connection(engine)
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT @@SERVERNAME, CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS int), "
            "CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(32)), SUSER_SNAME(), "
            "(SELECT COUNT(*) FROM sys.databases WHERE name = :db)"), {"db": settings.db_name}).one()
    server, windows_only, version, who, db_exists = row
    print(f"[ OK ] Connected to {server} (SQL Server {version}) as {who} using Windows authentication")
    print(f"[ OK ] Authentication mode: {'Windows only' if windows_only else 'Mixed (Windows + SQL logins)'}")
    print(f"[ OK ] Database '{settings.db_name}' {'exists' if db_exists else 'does not exist yet (db_init creates it)'}")
    if settings.db_auth_mode == "sql" and windows_only:
        ok = False
        print("[FAIL] DB_AUTH_MODE=sql but the server only allows Windows authentication.")
    return EXIT_OK if ok else EXIT_ENV


if __name__ == "__main__":
    run_cli(main)
