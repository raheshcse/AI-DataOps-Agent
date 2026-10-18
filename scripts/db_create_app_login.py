"""Create the restricted application login and grant it least-privilege rights.

    python -m scripts.db_create_app_login [--login dataops_app]

Run after db_init. The password is typed at a hidden prompt: it is never a command-line
argument, never printed and never written to a file by this script. Afterwards put it in
your .env yourself (DB_AUTH_MODE=sql, DB_USER, DB_PASSWORD). Requires a server in Mixed
authentication mode.
"""

from __future__ import annotations

import argparse
import getpass

from sqlalchemy import text

from app.db.connection import make_engine
from app.db.permissions import (
    APP_DELETE,
    APP_INSERT,
    APP_UPDATE,
    DEFAULT_APP_LOGIN,
    create_login_sql,
    grant_statements,
    quote_ident,
    validate_identifier,
)
from scripts._common import EXIT_ENV, EXIT_OK, load_settings, print_target, run_cli

MIN_PASSWORD_LENGTH = 12


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--login", default=DEFAULT_APP_LOGIN)
    args = parser.parse_args()
    login = validate_identifier(args.login)
    settings = load_settings()
    print_target(settings, admin=True)

    master = make_engine(settings, database="master", admin=True)
    with master.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        windows_only = conn.execute(text(
            "SELECT CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS int)")).scalar_one()
        if windows_only:
            print("This SQL Server only allows Windows authentication, so a SQL login cannot be used.\n"
                  "Either enable Mixed Mode (SSMS > Server Properties > Security, then restart the service)\n"
                  "or keep DB_AUTH_MODE=windows (see docs/windows-setup.md for the trade-off).")
            return EXIT_ENV
        exists = conn.execute(text("SELECT COUNT(*) FROM sys.server_principals WHERE name = :n"),
                              {"n": login}).scalar_one()
        if exists:
            print(f"Login '{login}' already exists - its password is NOT changed.")
        else:
            password = getpass.getpass(f"Choose a password for new login '{login}' (hidden): ")
            if password != getpass.getpass("Repeat password (hidden): "):
                print("Passwords did not match; nothing was created.")
                return EXIT_ENV
            if len(password) < MIN_PASSWORD_LENGTH:
                print(f"Use at least {MIN_PASSWORD_LENGTH} characters; nothing was created.")
                return EXIT_ENV
            conn.execute(text(create_login_sql(login, password)))
            del password
            print(f"Created login '{login}' (password policy enforced).")

    db_engine = make_engine(settings, admin=True)
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        has_user = conn.execute(text("SELECT COUNT(*) FROM sys.database_principals WHERE name = :n"),
                                {"n": login}).scalar_one()
        for statement in grant_statements(login):
            if statement.startswith("CREATE USER") and has_user:
                continue
            conn.execute(text(statement))
    print(f"Granted in '{settings.db_name}': SELECT on all tables; INSERT on {', '.join(APP_INSERT)}; "
          f"UPDATE on {', '.join(APP_UPDATE)}; DELETE on {', '.join(APP_DELETE)}. No DDL, not db_owner.")
    print(f"Now add to .env:  DB_AUTH_MODE=sql   DB_USER={login}   DB_PASSWORD=<the password you chose>")
    return EXIT_OK


if __name__ == "__main__":
    run_cli(main)
