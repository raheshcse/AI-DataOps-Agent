"""DELIBERATE, GUARDED reset: delete all simulation rows and re-seed the clean baseline.

    python -m scripts.db_reset --database dataops_sim --yes-delete-all-data

What is deleted: every row in all 12 simulation tables of the database named by DB_NAME
(and generated run logs under the log directory). The tables themselves are kept, then the
baseline is re-seeded. Refuses unless: the confirmation flag is given, --database exactly
equals DB_NAME, DB_NAME ends in _sim or _test, DB_SERVER is this machine and APP_ENV is not
production. Never runs automatically (not at startup, seeding or in ordinary tests).
"""

from __future__ import annotations

import argparse

from app.db.safety import assert_safe_reset_target
from scripts._common import EXIT_OK, load_settings, log_dir, make_service, print_target, run_cli


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", help="must exactly equal DB_NAME")
    parser.add_argument("--yes-delete-all-data", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    print_target(settings, admin=True)
    assert_safe_reset_target(settings, args.database, args.yes_delete_all_data)
    service = make_service(settings, admin=True)
    counts = service.repo.table_counts()
    print(f"About to delete {sum(counts.values())} rows from database '{settings.db_name}' "
          f"and clear run logs in {log_dir(settings)}")
    print(service.reset())
    print("Verify: python -m scripts.db_verify")
    return EXIT_OK


if __name__ == "__main__":
    run_cli(main)
