"""Verify tables, relationships and record counts against the documented baseline.

    python -m scripts.db_verify                  # integrity + baseline row counts
    python -m scripts.db_verify --integrity-only # integrity only (use after running pipelines)

Read-only.
"""

from __future__ import annotations

import argparse

from scripts._common import EXIT_CHECK_FAILED, EXIT_OK, load_settings, make_service, print_target, run_cli


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--integrity-only", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    print_target(settings)
    report = make_service(settings).verify(strict=not args.integrity_only)
    for c in report["checks"]:
        print(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['check']}: {c['detail']}")
    print("\nBASELINE HEALTHY" if report["ok"] else "\nNOT AT BASELINE (see FAIL lines above)")
    return EXIT_OK if report["ok"] else EXIT_CHECK_FAILED


if __name__ == "__main__":
    run_cli(main)
