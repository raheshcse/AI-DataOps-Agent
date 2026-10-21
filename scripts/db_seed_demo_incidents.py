"""Add ~98 synthetic demonstration incidents to the already seeded simulator (additive, safe to repeat).

    python -m scripts.db_seed_demo_incidents --dry-run     # show what would be inserted, change nothing
    python -m scripts.db_seed_demo_incidents               # insert the missing ones

* Only INSERTs. Existing rows (INC-0001, INC-0002, anything else) are never updated, deleted or reset.
* Safe to repeat: entries already present (matched by their fixed RUN-DEMO-... run id) are skipped, never duplicated.
* Needs the baseline first (``python -m scripts.db_seed``). Nothing seeds automatically at application start-up.
* ``python -m scripts.db_reset`` (the guarded reset) removes these rows together with everything else; this script is
  never run by it.
"""

from __future__ import annotations

import sys

from app.simulation.demo_incidents import seed_demo_incidents
from scripts._common import EXIT_CHECK_FAILED, EXIT_OK, load_settings, log_dir, make_service, print_target, run_cli


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    dry = "--dry-run" in args
    settings = load_settings()
    print_target(settings, admin=True)
    service = make_service(settings, admin=True)
    if service.repo.get_state() is None:
        print("The simulator has no baseline yet. Run `python -m scripts.db_seed` first.")
        return EXIT_CHECK_FAILED
    print(f"Run logs directory: {log_dir(settings)}")
    report = seed_demo_incidents(service.repo, service.logs, dry_run=dry)
    print(f"{'DRY RUN - nothing was written. ' if dry else ''}Synthetic demonstration incidents")
    print(f"  Incidents before:     {report.existing_before}")
    print(f"  Catalogue size:       {report.catalogue_size}")
    print(f"  {'Would insert' if dry else 'Newly inserted'}:      {report.inserted}")
    print(f"  Skipped duplicates:   {report.skipped_duplicates}")
    print(f"  Final total:          {report.existing_before + report.inserted}")
    if report.inserted_ids:
        print(f"  New ids:              {report.inserted_ids[0]} .. {report.inserted_ids[-1]}")
    print("Next: python -m scripts.db_verify   (synthetic rows are reported separately from the baseline)")
    return EXIT_OK


if __name__ == "__main__":
    run_cli(main)
