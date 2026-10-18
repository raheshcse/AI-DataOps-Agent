"""Controlled (simulated) clock.

Simulation timestamps come from the ``sim_state`` row, not the machine clock, so
runs, logs and freshness results are reproducible and the stale-data scenario does
not depend on waiting.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.simulation.ports import SimRepository

# "Now" in the baseline: shortly before the daily run that will process the pending batch.
ANCHOR_NOW = datetime(2026, 1, 15, 1, 0, 0, tzinfo=timezone.utc)


class SimulationNotSeeded(Exception):
    """The simulation database has not been seeded (run the seed script first)."""


class SimClock:
    def __init__(self, repo: SimRepository):
        self._repo = repo

    def now(self) -> datetime:
        state = self._repo.get_state()
        if state is None:
            raise SimulationNotSeeded("The simulation database has not been seeded.")
        return state["sim_now"]

    def advance_to(self, when: datetime) -> None:
        self._repo.set_sim_now(when)
