"""Write the 100 deterministic synthetic demo incidents to a JSON file. Touches no database and no network.

    python scripts/export_demo_incidents.py demo_incidents.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.demo import generate_demo_incidents, validate_demo_incidents  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/export_demo_incidents.py <output.json>")
        return 2
    items = generate_demo_incidents()
    problems = validate_demo_incidents(items)
    if problems:
        print("dataset invalid:", *problems, sep="\n  ")
        return 1
    out = Path(argv[1])
    if out.exists():
        print(f"refusing to overwrite existing file: {out}")
        return 1
    out.write_text(json.dumps({"classification": "demo_synthetic", "incidents": items}, indent=2), encoding="utf-8")
    print(f"wrote {len(items)} synthetic incidents to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
