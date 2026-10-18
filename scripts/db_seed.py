"""Seed the deterministic baseline (idempotent: a second run changes nothing).

    python -m scripts.db_seed
"""

from __future__ import annotations

from scripts._common import EXIT_OK, load_settings, log_dir, make_service, print_target, run_cli


def main() -> int:
    settings = load_settings()
    print_target(settings, admin=True)
    print(f"Run logs directory: {log_dir(settings)}")
    print(make_service(settings, admin=True).seed())
    print("Next: python -m scripts.db_verify")
    return EXIT_OK


if __name__ == "__main__":
    run_cli(main)
