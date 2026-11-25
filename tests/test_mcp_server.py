"""End-to-end stdio MCP protocol test (verifies real JSON-RPC stdio framing & tool schemas)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_mcp_handshake_and_tools(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["AGENT_BRIDGE_STATE_DIR"] = str(tmp_path / "state")
    env["AGENT_BRIDGE_DEFAULT_CWD"] = str(tmp_path)

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_bridge"],
        env=env,
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            assert init.server_info.name == "agent-bridge"
            assert init.instructions is not None
            assert "agent-bridge" in init.instructions

            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            assert tool_names == {
                "run_agent",
                "start_agent",
                "run_parallel",
                "check_jobs",
                "cancel_job",
                "review_code",
                "get_usage",
                "list_backends",
            }

            usage_res = await session.call_tool("get_usage", {"period": "session"})
            assert not usage_res.is_error
            payload = json.loads(usage_res.content[0].text)
            assert payload["period"] == "session"
            assert payload["totals"]["runs"] == 0

            jobs_res = await session.call_tool("check_jobs", {})
            assert not jobs_res.is_error
            jobs_payload = json.loads(jobs_res.content[0].text)
            assert jobs_payload["count"] == 0
