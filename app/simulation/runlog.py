"""JSON Lines persistence for operational run logs.

Layout: ``<log_dir>/pipeline_runs/<pipeline_id>/<run_id>.jsonl`` (one file per run,
one JSON object per line). Names are validated so a crafted run id cannot escape
the log directory.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")


def _safe(name: str) -> str:
    if not _SAFE.match(name) or ".." in name:
        raise ValueError("Invalid identifier for a log path.")
    return name


class RunLogStore:
    def __init__(self, base_dir: str | Path):
        self.root = Path(base_dir) / "pipeline_runs"

    def path_for(self, pipeline_id: str, run_id: str) -> Path:
        return self.root / _safe(pipeline_id) / f"{_safe(run_id)}.jsonl"

    def relative_ref(self, pipeline_id: str, run_id: str) -> str:
        return f"pipeline_runs/{_safe(pipeline_id)}/{_safe(run_id)}.jsonl"

    def write(self, pipeline_id: str, run_id: str, events: list[dict[str, Any]]) -> Path:
        path = self.path_for(pipeline_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for event in events:
                fh.write(json.dumps(event, sort_keys=True, default=str) + "\n")
        return path

    def read(self, run_id: str) -> list[dict[str, Any]]:
        _safe(run_id)
        for candidate in sorted(self.root.glob(f"*/{run_id}.jsonl")):
            return [json.loads(line) for line in candidate.read_text(encoding="utf-8").splitlines() if line]
        return []

    def clear(self) -> int:
        """Remove generated run logs (``*.jsonl`` under ``pipeline_runs`` only)."""
        removed = 0
        if self.root.exists():
            for f in self.root.glob("*/*.jsonl"):
                f.unlink()
                removed += 1
        return removed
