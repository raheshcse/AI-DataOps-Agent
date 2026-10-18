"""Phase 4: a read-only MCP server that exposes the Phase 3 diagnostic tools.

The server is a thin adapter over ``app.diagnostics.DiagnosticsService``: it declares the
tool schemas, runs the existing diagnostics and returns their structured results. It adds no
diagnostic logic, no SQL, and no write or recovery capability.
"""

from app.mcp_server.provider import ServiceProvider, build_service_from_settings
from app.mcp_server.server import TOOL_CATALOGUE, create_server

__all__ = ["ServiceProvider", "build_service_from_settings", "create_server", "TOOL_CATALOGUE"]
