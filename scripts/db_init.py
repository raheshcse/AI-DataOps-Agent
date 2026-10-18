"""Create the simulation database and its tables (idempotent, never deletes data).

    python -m scripts.db_init

Runs with YOUR Windows account (setup privilege): creating a database and tables needs
more rights than the application login should have.
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from app.db.connection import check_connection, make_engine
from app.db.permissions import quote_ident
from app.db.safety import ALLOWED_DB_SUFFIXES, UnsafeTarget
from app.db.schema import TABLE_NAMES, metadata
from scripts._common import EXIT_OK, load_settings, print_target, run_cli


def main() -> int:
    settings = load_settings()
    if not settings.db_name.lower().endswith(ALLOWED_DB_SUFFIXES):
        raise UnsafeTarget(f"Refusing: DB_NAME must end with one of {', '.join(ALLOWED_DB_SUFFIXES)} "
                           "so tables are never created inside an existing real database.")
    print_target(settings, admin=True)
    master = make_engine(settings, database="master", admin=True)
    check_connection(master)
    with master.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        exists = conn.execute(text("SELECT COUNT(*) FROM sys.databases WHERE name = :n"),
                              {"n": settings.db_name}).scalar_one()
        if exists:
            print(f"Database '{settings.db_name}' already exists - leaving it as is.")
        else:
            conn.execute(text(f"CREATE DATABASE {quote_ident(settings.db_name)}"))
            print(f"Created database '{settings.db_name}'.")
    engine = make_engine(settings, admin=True)
    before = set(inspect(engine).get_table_names())
    metadata.create_all(engine)  # checkfirst: existing tables are left untouched
    created = [t for t in TABLE_NAMES if t not in before]
    print(f"Tables created: {', '.join(created) if created else 'none (all already existed)'}")
    print("Next: python -m scripts.db_seed")
    return EXIT_OK


if __name__ == "__main__":
    run_cli(main)
