"""End-to-end stdio MCP protocol test (verifies JSON-RPC stdio framing, the five tools, and default env timeout)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_bridge.quota import claude_quota_from_rate_limit
from agent_bridge.storage import BridgeStore


async def test_stdio_mcp_handshake_tools_and_default_timeout(
    tmp_path: Path, plain_host_env: dict[str, str],
) -> None:
    env = plain_host_env
    env["AGENT_BRIDGE_STATE_DIR"] = str(tmp_path / "state")
    env["AGENT_BRIDGE_DEFAULT_CWD"] = str(tmp_path)
    env["AGENT_BRIDGE_DEFAULT_TIMEOUT"] = "185"
    env["AGENT_BRIDGE_AGY_BIN"] = str(tmp_path / "missing-agy.exe")
    env["AGENT_BRIDGE_CODEX_BIN"] = str(tmp_path / "missing-codex.exe")
    env["AGENT_BRIDGE_CLAUDE_BIN"] = str(tmp_path / "missing-claude.exe")
    # This test is about a bridge that a host started, wherever the test run itself was started from.
    env.pop("AGENT_BRIDGE_PARENT_JOB", None)
    env.pop("AGENT_BRIDGE_PARENT_OWNER", None)

    # The one quota reading that needs no CLI: what an earlier Claude job reported, kept in the database.
    reading = claude_quota_from_rate_limit(
        {"unifiedWindows": {"five_hour": {"utilization": 0.25, "resetsAt": time.time() + 3600}}}
    )
    assert reading is not None
    BridgeStore(tmp_path / "state").set_cached_quota("claude", reading)

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
            assert tool_names == {"run", "jobs", "cancel", "usage", "backends"}
            # `codex exec` refuses a tool that is not marked read-only unless approvals are switched off.
            # `usage` asks each CLI for its quota, so it is open-world as well.
            tools = {t.name: t.annotations for t in tools_result.tools}
            assert (tools["usage"].read_only_hint, tools["usage"].open_world_hint) == (True, True)
            assert (tools["jobs"].read_only_hint, tools["backends"].read_only_hint) == (True, True)
            assert (tools["run"].read_only_hint, tools["cancel"].read_only_hint) == (False, False)

            dash_res = await session.call_tool("usage", {"period": "session", "view": "dashboard", "quota": True})
            assert not dash_res.is_error
            # The view reaches a host that reads only the text content as JSON, and a host that reads only
            # the structured part as the same object.
            dash_payload = json.loads(dash_res.content[0].text)
            assert dash_res.structured_content == dash_payload
            assert dash_payload["text"].startswith("agent-bridge usage, session")
            # The quota bars are not ASCII, and reach the host whole.
            assert "  Claude plan             " + "\u2588" * 8 + "\u2591" * 2 + "  75%" in dash_payload["text"]
            claude_pool = dash_payload["quota"]["pools"][-1]
            assert (claude_pool["pool"], claude_pool["five_hour"]["left_percent"]) == ("claude", 75)

            usage_res = await session.call_tool("usage", {"period": "session"})
            assert not usage_res.is_error
            payload = json.loads(usage_res.content[0].text)
            assert payload["scope"]["period"] == "session"
            assert payload["totals"] == {"runs": 0, "duration_seconds": 0.0, "total_tokens": 0}
            assert "quota" not in payload

            jobs_res = await session.call_tool("jobs", {})
            assert not jobs_res.is_error
            jobs_payload = json.loads(jobs_res.content[0].text)
            assert jobs_payload["count"] == 0

            # `backends` honours AGENT_BRIDGE_DEFAULT_TIMEOUT and reports invalid bins honestly.
            lb_res = await session.call_tool("backends", {})
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
            assert "whichever CLI you are" in init.instructions
            assert "your Claude Code subscription" not in init.instructions
            for backend_name in ("`codex`", "`neon`", "`claude`"):
                assert backend_name in init.instructions

            # A refusal reaches the host with its reason, not as a bare "Error executing tool".
            bad = await session.call_tool("run", {"backend": "bogus", "prompt": "x"})
            assert bad.is_error
            assert "Unknown backend 'bogus'" in bad.content[0].text
            wt = await session.call_tool(
                "run",
                {"backend": "neon", "prompt": "x", "mode": "edit", "worktree": True},
            )
            assert wt.is_error
            assert "only supported for the 'codex' and 'claude' backends" in wt.content[0].text
