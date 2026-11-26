"""End-to-end stdio MCP protocol test (verifies JSON-RPC stdio framing, 8 tools, and default env timeout)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_mcp_handshake_tools_and_default_timeout(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["AGENT_BRIDGE_STATE_DIR"] = str(tmp_path / "state")
    env["AGENT_BRIDGE_DEFAULT_CWD"] = str(tmp_path)
    env["AGENT_BRIDGE_DEFAULT_TIMEOUT"] = "185"
    env["AGENT_BRIDGE_AGY_BIN"] = str(tmp_path / "missing-agy.exe")
    env["AGENT_BRIDGE_CODEX_BIN"] = str(tmp_path / "missing-codex.exe")
    env["AGENT_BRIDGE_CLAUDE_BIN"] = str(tmp_path / "missing-claude.exe")
    # This test is about a bridge that a host started, wherever the test run itself was started from.
    env.pop("AGENT_BRIDGE_PARENT_JOB", None)
    env.pop("AGENT_BRIDGE_PARENT_OWNER", None)

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

            usage_res = await session.call_tool(
                "get_usage",
                {"period": "session", "compact": True},
            )
            assert not usage_res.is_error
            payload = json.loads(usage_res.content[0].text)
            assert payload["scope"]["period"] == "session"
            assert payload["overview"]["runs"] == 0
            assert payload["overview"]["in_flight"] == 0

            jobs_res = await session.call_tool("check_jobs", {})
            assert not jobs_res.is_error
            jobs_payload = json.loads(jobs_res.content[0].text)
            assert jobs_payload["count"] == 0

            # Verify list_backends honors AGENT_BRIDGE_DEFAULT_TIMEOUT & reports invalid bins honestly!
            lb_res = await session.call_tool("list_backends", {})
            assert not lb_res.is_error
            lb_payload = json.loads(lb_res.content[0].text)
            assert lb_payload["default_timeout_seconds"] == 185
            assert lb_payload["backends"]["neon"]["command_found"] is False
            assert lb_payload["backends"]["neon"]["healthy"] is False
            assert lb_payload["backends"]["neon"]["available"] is False
            assert lb_payload["backends"]["codex"]["command_found"] is False
            assert lb_payload["backends"]["codex"]["healthy"] is False
            assert lb_payload["backends"]["codex"]["available"] is False
            assert lb_payload["backends"]["codex"]["default_model"] == "gpt-6.2-sol"
            assert lb_payload["backends"]["codex"]["default_effort"] == "high"
            assert lb_payload["backends"]["claude"]["command_found"] is False
            assert lb_payload["backends"]["claude"]["available"] is False
            assert lb_payload["backends"]["claude"]["default_model"] == "sonnet"
            assert lb_payload["backends"]["claude"]["default_effort"] == "high"
            assert lb_payload["nesting"] == {"nested": False}

            # The host is not assumed to be any one CLI, and every backend is offered to it.
            assert "whichever CLI you are yourself" in init.instructions
            assert "your Claude Code subscription" not in init.instructions
            for backend_name in ("`codex`", "`neon`", "`claude`"):
                assert backend_name in init.instructions

            # A refusal reaches the host with its reason, not as a bare "Error executing tool".
            bad = await session.call_tool("run_agent", {"backend": "bogus", "prompt": "x"})
            assert bad.is_error
            assert "Unknown backend 'bogus'" in bad.content[0].text
            wt = await session.call_tool(
                "run_agent",
                {"backend": "neon", "prompt": "x", "mode": "edit", "worktree": True},
            )
            assert wt.is_error
            assert "only supported for the 'codex' and 'claude' backends" in wt.content[0].text
