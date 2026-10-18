"""Start the MCP server over stdio:  python -m app.mcp_server

stdout carries the MCP protocol, so nothing here (or in the tools) may print to it; logs go to stderr.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    # MCP clients start the server from an arbitrary directory. Settings (.env) and the run-log
    # directory are relative to the project root, so run from there. The .env file is only read.
    os.chdir(PROJECT_ROOT)

    from app.core.logging import configure_logging
    from app.mcp_server import create_server

    try:
        from app.config import get_settings

        configure_logging(get_settings().log_level)
    except Exception:  # a bad .env must not stop the server from starting; tools report DB problems
        configure_logging("INFO")
    create_server().run("stdio")


if __name__ == "__main__":
    main()
