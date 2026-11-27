"""Entry point for `python -m agent_bridge`. No argument starts the stdio MCP server."""

from agent_bridge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
