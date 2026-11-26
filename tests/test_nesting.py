"""Tests for the guard against a subagent calling the bridge back into itself."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_bridge.config import Settings
from agent_bridge.jobs import JobManager
from agent_bridge.nesting import (
    ENV_PARENT_JOB,
    ENV_PARENT_OWNER,
    NestedBridgeError,
    Nesting,
    ancestor_chain,
    detect_nesting,
    parse_proc_stat,
    parse_ps_table,
    subagent_env,
)
from agent_bridge.runners import CodexRunner, NeonRunner
from agent_bridge.server import create_server
from agent_bridge.storage import BridgeStore

JOB_TOOLS: dict[str, dict[str, Any]] = {
    "run_agent": {"backend": "claude", "prompt": "hi"},
    "start_agent": {"backend": "claude", "prompt": "hi"},
    "run_parallel": {"tasks": [{"backend": "claude", "prompt": "hi"}]},
    "review_code": {"backends": ["claude"]},
}


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        default_neon_model="neon-high",
        default_codex_model=None,
        default_codex_effort=None,
        default_timeout_seconds=60,
        max_concurrent_jobs=4,
        max_stored_jobs=50,
        models_cache_ttl_seconds=900.0,
        quota_cache_ttl_seconds=120.0,
        owner_stale_seconds=30.0,
        state_dir=tmp_path / "state",
        agy_bin=str(tmp_path / "missing-agy.exe"),
        codex_bin=str(tmp_path / "missing-codex.exe"),
        claude_bin=str(tmp_path / "missing-claude.exe"),
        default_cwd=tmp_path,
    )


def _server_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """An environment for a real bridge process that can reach no real CLI and no real database."""
    env = {k: v for k, v in os.environ.items() if k not in {ENV_PARENT_JOB, ENV_PARENT_OWNER}}
    env["AGENT_BRIDGE_STATE_DIR"] = str(tmp_path / "state")
    env["AGENT_BRIDGE_DEFAULT_CWD"] = str(tmp_path)
    env["AGENT_BRIDGE_AGY_BIN"] = str(tmp_path / "missing-agy.exe")
    env["AGENT_BRIDGE_CODEX_BIN"] = str(tmp_path / "missing-codex.exe")
    env["AGENT_BRIDGE_CLAUDE_BIN"] = str(tmp_path / "missing-claude.exe")
    env.update(extra)
    return env


def _text(result: Any) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


def test_marker_in_the_environment_means_nested() -> None:
    assert detect_nesting(environ={}, live_owners=(), ancestors=()) == Nesting(nested=False)

    found = detect_nesting(
        environ={ENV_PARENT_JOB: "codex-1a2b3c4d", ENV_PARENT_OWNER: "pid-1-abc"},
        live_owners=(),
        ancestors=(),
    )
    assert found.nested is True and found.via == "env"
    assert found.parent_job == "codex-1a2b3c4d" and found.parent_owner == "pid-1-abc"
    assert "nested_bridge" in found.refusal("run_agent") and "codex-1a2b3c4d" in found.refusal("run_agent")


def test_live_bridge_among_the_ancestors_means_nested_even_with_no_marker() -> None:
    now = time.time()
    owners = [("pid-500-aaa", 500, now - 60.0), ("pid-900-bbb", 900, now - 60.0)]

    # 500 is a bridge and it is the grandparent: nested, whatever the environment says.
    found = detect_nesting(
        environ={},
        live_owners=owners,
        ancestors=[(700, now - 5.0), (500, now - 61.0), (4, now - 9999.0)],
        own_pid=800,
    )
    assert found == Nesting(nested=True, via="ancestry", parent_owner="pid-500-aaa", parent_pid=500)

    # A bridge that is running but is not an ancestor does not count.
    assert detect_nesting(
        environ={}, live_owners=owners, ancestors=[(700, now - 5.0), (4, now - 9999.0)], own_pid=800
    ).nested is False

    # This process's own row is not its ancestor.
    assert detect_nesting(
        environ={}, live_owners=[("me", 800, now)], ancestors=[(800, now)], own_pid=800
    ).nested is False

    # The row of a bridge that died without marking itself, whose pid a newer process took over.
    assert detect_nesting(
        environ={}, live_owners=[("pid-700-old", 700, now - 5000.0)], ancestors=[(700, now - 5.0)], own_pid=800
    ).nested is False
    # Where the start time cannot be read, the pid alone decides.
    assert detect_nesting(
        environ={}, live_owners=[("pid-700-old", 700, now - 5000.0)], ancestors=[(700, None)], own_pid=800
    ).nested is True


def test_subagent_env_carries_the_marker_and_drops_what_it_is_told_to() -> None:
    env = subagent_env(
        job_id="claude-12345678",
        owner_id="pid-1-abc",
        base={"PATH": "p", "CLAUDECODE": "1", "KEEP": "k"},
        drop=("CLAUDECODE", "NOT_THERE"),
    )
    assert env == {
        "PATH": "p",
        "KEEP": "k",
        ENV_PARENT_JOB: "claude-12345678",
        ENV_PARENT_OWNER: "pid-1-abc",
    }


def test_process_table_parsers() -> None:
    # A command name may hold spaces and brackets: the fields are counted from the last bracket.
    stat = "4242 (my (odd) name) S 4000 4242 4242 0 -1 4194304 100 0 0 0 1 2 0 0 20 0 1 0 987654 1000 10"
    assert parse_proc_stat(stat) == (4000, 987654)
    assert parse_proc_stat("garbage") is None
    assert parse_proc_stat("1 (x) S 2") is None

    assert parse_ps_table("  1     0\n 200     1\nbad line\n 300   200\n") == {1: 0, 200: 1, 300: 200}


def test_ancestor_chain_of_a_real_child_includes_this_process() -> None:
    code = (
        "import json; from agent_bridge.nesting import ancestor_chain; "
        "print(json.dumps([p for p, _ in ancestor_chain()]))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60)
    assert os.getpid() in json.loads(out.stdout.strip().splitlines()[-1])
    assert os.getpid() not in [p for p, _ in ancestor_chain()]


async def test_job_manager_refuses_to_start_anything_when_nested(tmp_path: Path) -> None:
    manager = JobManager(_make_settings(tmp_path))
    manager.nesting = Nesting(nested=True, via="env", parent_job="codex-1")
    request = manager.prepare_request(backend="claude", prompt="hi")
    with pytest.raises(NestedBridgeError, match="nested_bridge"):
        await manager.run(request)
    with pytest.raises(NestedBridgeError, match="nested_bridge"):
        await manager.start_async(request)
    assert manager.store.list_jobs(limit=10) == []
    await manager.shutdown()


async def test_tools_refuse_in_process_when_nested_and_read_tools_still_answer(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    mcp, manager = create_server(
        settings=settings,
        nesting=Nesting(nested=True, via="env", parent_job="neon-99"),
    )
    for tool, args in JOB_TOOLS.items():
        with pytest.raises(Exception, match="nested_bridge") as excinfo:
            await mcp.call_tool(tool, args)
        # The tool itself refuses, before it looks at its arguments or reaches the job manager.
        assert f"`{tool}` is refused" in str(excinfo.value)
    assert manager.store.list_jobs(limit=10) == []
    await manager.shutdown()


async def test_real_bridge_with_the_marker_refuses_every_job_tool(tmp_path: Path) -> None:
    env = _server_env(tmp_path, **{ENV_PARENT_JOB: "codex-feedbeef"})
    params = StdioServerParameters(command=sys.executable, args=["-m", "agent_bridge"], env=env)

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            assert "INSIDE one of its own jobs" in (init.instructions or "")

            for tool, args in JOB_TOOLS.items():
                res = await session.call_tool(tool, args)
                assert res.is_error, tool
                assert "nested_bridge" in _text(res), tool
                assert "codex-feedbeef" in _text(res), tool

            jobs = await session.call_tool("check_jobs", {})
            assert not jobs.is_error
            assert json.loads(jobs.content[0].text)["count"] == 0

            usage = await session.call_tool("get_usage", {"period": "session", "compact": True})
            assert not usage.is_error

            backends = await session.call_tool("list_backends", {})
            assert not backends.is_error
            nesting = json.loads(backends.content[0].text)["nesting"]
            assert nesting == {"nested": True, "via": "env", "parent_job": "codex-feedbeef"}


async def test_real_bridge_under_a_live_bridge_process_refuses_with_no_marker(tmp_path: Path) -> None:
    """The case a marker cannot cover: a CLI that does not pass its environment to its MCP servers."""
    # This test process plays the outer bridge: it is registered as a live owner in the shared
    # database, and the bridge started below is its descendant.
    BridgeStore(tmp_path / "state").register_owner("pid-outer-bridge", os.getpid())

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_bridge"],
        env=_server_env(tmp_path),
    )
    assert ENV_PARENT_JOB not in (params.env or {})

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            res = await session.call_tool("run_agent", {"backend": "claude", "prompt": "hi"})
            assert res.is_error
            assert "nested_bridge" in _text(res)
            assert "detected by ancestry" in _text(res)

            backends = await session.call_tool("list_backends", {})
            nesting = json.loads(backends.content[0].text)["nesting"]
            assert nesting["nested"] is True and nesting["via"] == "ancestry"
            assert nesting["parent_owner"] == "pid-outer-bridge"
            assert nesting["parent_pid"] == os.getpid()


async def test_real_bridge_started_by_a_plain_host_is_not_nested(tmp_path: Path) -> None:
    """The control for the two tests above: same setup, no marker, no bridge above it."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_bridge"],
        env=_server_env(tmp_path),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            assert "whichever CLI you are yourself" in (init.instructions or "")

            backends = await session.call_tool("list_backends", {})
            assert json.loads(backends.content[0].text)["nesting"] == {"nested": False}

            # The job is accepted and then fails for the reason set up here: there is no CLI.
            res = await session.call_tool("run_agent", {"backend": "claude", "prompt": "hi"})
            assert "nested_bridge" not in _text(res)
            detail = json.loads(res.content[0].text)
            assert detail["status"] == "failed"
            assert "AGENT_BRIDGE_CLAUDE_BIN" in detail["error"]


FAKE_CODEX_ENV = r'''
import json, os, sys
if "--version" in sys.argv[1:]:
    print("codex-cli 0.99.0")
    sys.exit(0)
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-env"}), flush=True)
text = json.dumps({"marker": os.environ.get("AGENT_BRIDGE_PARENT_JOB"), "owner": os.environ.get("AGENT_BRIDGE_PARENT_OWNER")})
print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": text}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
'''

FAKE_AGY_ENV = r'''
import json, os, sys
args = sys.argv[1:]
if "--version" in args:
    print("1.9.9")
    sys.exit(0)
if args and args[0] == "models":
    print("gemini-4-neon-high\tGemini 4 Neon (High)")
    sys.exit(0)
cid = "conv-env"
text = json.dumps({"marker": os.environ.get("AGENT_BRIDGE_PARENT_JOB"), "owner": os.environ.get("AGENT_BRIDGE_PARENT_OWNER")})
usage = {"input_tokens": 1, "output_tokens": 1, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 2}
print(json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "gemini-4-neon-high"}}), flush=True)
print(json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": text, "usage": usage}}), flush=True)
print(json.dumps({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": text, "num_turns": 1, "usage": usage}}), flush=True)
'''


class _StubCodex(CodexRunner):
    def __init__(self, settings: Settings, script: Path) -> None:
        self._script = script
        super().__init__(settings)

    def resolve_command(self) -> list[str]:
        return [sys.executable, str(self._script)]


class _StubAgy(NeonRunner):
    read_only_guard = ""

    def __init__(self, settings: Settings, script: Path) -> None:
        self._script = script
        super().__init__(settings)

    def resolve_command(self) -> list[str]:
        return [sys.executable, str(self._script)]


async def test_codex_and_agy_subagents_carry_the_marker_too(tmp_path: Path) -> None:
    codex_script = tmp_path / "fake_codex_env.py"
    codex_script.write_text(FAKE_CODEX_ENV.strip(), encoding="utf-8")
    agy_script = tmp_path / "fake_agy_env.py"
    agy_script.write_text(FAKE_AGY_ENV.strip(), encoding="utf-8")

    settings = _make_settings(tmp_path)
    manager = JobManager(
        settings,
        codex_runner=_StubCodex(settings, codex_script),
        neon_runner=_StubAgy(settings, agy_script),
    )
    for backend in ("codex", "neon"):
        rec = await manager.run(manager.prepare_request(backend=backend, prompt="env"))
        assert rec.status == "completed", (backend, rec.error)
        seen = json.loads(rec.response)
        assert seen == {"marker": rec.job_id, "owner": manager.owner_id}, backend
    await manager.shutdown()
