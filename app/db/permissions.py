"""Least-privilege model for the application login.

Two kinds of principal are used:

* **Setup / admin** (your Windows account): creates the database and tables, seeds and
  resets. Needs DDL (CREATE DATABASE, CREATE TABLE) and bulk INSERT/DELETE. Used only by
  ``db_init``, ``db_create_app_login``, ``db_seed`` and ``db_reset``.
* **Application** (``dataops_app``): used by the API and the day-to-day scripts. It may
  read every simulation table and write only what pipelines and scenarios must write.
  It cannot create/alter/drop objects, cannot delete run history, and is not db_owner.
"""

from __future__ import annotations

import re

from app.db.schema import TABLE_NAMES

DEFAULT_APP_LOGIN = "dataops_app"
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

# What the application login may write (everything else is read-only for it).
APP_INSERT = ["orders", "order_items", "pipeline_runs", "validation_results",
              "incident_records", "scenario_activations"]
APP_UPDATE = ["sim_state", "scenario_activations"]
APP_DELETE = ["orders", "order_items"]  # removal of scenario-injected rows only (by code path)


def validate_identifier(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError("Names must be 1-64 characters: letters, digits, underscore; start with a letter.")
    return name


def quote_ident(name: str) -> str:
    return "[" + validate_identifier(name) + "]"


def quote_literal(value: str) -> str:
    """N'...' literal with embedded quotes doubled (used only for the CREATE LOGIN password)."""
    return "N'" + value.replace("'", "''") + "'"


def create_login_sql(login: str, password: str) -> str:
    return (f"CREATE LOGIN {quote_ident(login)} WITH PASSWORD = {quote_literal(password)}, "
            "CHECK_POLICY = ON, CHECK_EXPIRATION = OFF, DEFAULT_DATABASE = [master]")


def grant_statements(login: str) -> list[str]:
    """T-SQL (run inside the simulation database) that creates the user and grants rights."""
    user = quote_ident(login)
    statements = [f"CREATE USER {user} FOR LOGIN {user}"]
    for table in TABLE_NAMES:
        statements.append(f"GRANT SELECT ON OBJECT::dbo.{quote_ident(table)} TO {user}")
    for verb, tables in (("INSERT", APP_INSERT), ("UPDATE", APP_UPDATE), ("DELETE", APP_DELETE)):
        for table in tables:
            statements.append(f"GRANT {verb} ON OBJECT::dbo.{quote_ident(table)} TO {user}")
    return statements
