"""Test helper (not a test): serve the real MCP tools over stdio on the in-memory repository.

Run as ``python -m tests.mcp_stdio_fake_server`` from the project root. No database, no .env.
"""

import tempfile
from pathlib import Path

from app.diagnostics import DiagnosticsService
from app.mcp_server import ServiceProvider, create_server
from app.simulation.runlog import RunLogStore
from app.simulation.service import SimulationService
from tests.fakes import InMemoryRepo


def main() -> None:
    repo = InMemoryRepo()
    sim = SimulationService(repo, RunLogStore(Path(tempfile.mkdtemp()) / "logs"))
    sim.seed()
    create_server(ServiceProvider(lambda: DiagnosticsService(repo, sim.logs, sim.threshold))).run("stdio")


if __name__ == "__main__":
    main()
