"""End-to-end proof that a test-only fourth backend works through the adapter registry with zero core changes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_bridge import dashboard as dashboard_module
from agent_bridge.backends import (
    BackendAdapter,
    BackendDefaults,
    Capabilities,
    CommandSpec,
    ParsedStream,
    QuotaPoolSpec,
    register_adapter,
    unregister_adapter,
)
from agent_bridge.config import Settings
from agent_bridge.models import RunRequest
from agent_bridge.nesting import NOT_NESTED
from agent_bridge.server import create_server

FAKE_ECHO_CLI = r'''
import json, sys

args = sys.argv[1:]
if "--version" in args:
    print("echo-cli 1.0.0")
    sys.exit(0)

prompt = sys.stdin.read().strip()
if "CUT_SHORT" in prompt:
    print(json.dumps({"partial": "halfway"}))
    sys.exit(0)

print(json.dumps({
    "done": True,
    "session_id": "echo-sess-1",
    "model": "echo-v1-live",
    "reply": f"echo:{prompt}",
    "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
}))
'''


class EchoAdapter(BackendAdapter):
    """Minimal test-only fourth backend backed by a one-line stand-in script."""

    name = "echo"
    cli_name = "echo-cli"
    aliases = ("echo-cli",)
    capabilities = Capabilities(
        resume=True,
        worktree=False,
        builtin_review=False,
        model_catalog=False,
        fast_tier=False,
        extra_dirs=True,
        extra_dirs_on_resume=True,
    )
    missing_terminal_event_name = "done"

    def __init__(self, script_path: Path) -> None:
        self._script_path = script_path

    def defaults(self, settings: Settings) -> BackendDefaults:
        return BackendDefaults(model="echo-v1", effort=None, timeout_seconds=settings.default_timeout_seconds)

    def resolve_command(self, settings: Settings) -> list[str]:
        _ = settings
        return [sys.executable, str(self._script_path)]

    def build_command(
        self,
        request: RunRequest,
        *,
        cmd: list[str],
        model: str | None = None,
        effort: str | None = None,
        **kwargs: Any,
    ) -> CommandSpec:
        _ = (effort, kwargs)
        argv = [*cmd]
        if model:
            argv.extend(["--model", model])
        if request.session_id:
            argv.extend(["--resume", request.session_id])
        return CommandSpec(argv=argv, stdin_text=request.prompt)

    def parse_stream(
        self,
        lines: list[str],
        *,
        request: RunRequest | None = None,
    ) -> ParsedStream:
        _ = request
        for raw in reversed(lines):
            stripped = raw.strip()
            if not stripped.startswith("{"):
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("done") is True:
                return ParsedStream(
                    session_id=obj.get("session_id"),
                    model=obj.get("model"),
                    response=str(obj.get("reply") or ""),
                    usage=obj.get("usage") if isinstance(obj.get("usage"), dict) else None,
                    terminal_event_seen=True,
                )
        return ParsedStream(response="".join(lines).strip(), terminal_event_seen=False)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        default_neon_model="neon-high",
        default_codex_model=None,
        default_codex_effort=None,
        default_timeout_seconds=30,
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


def _data(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple):
        return dict(result[1])
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    return json.loads(result.content[0].text)


async def test_fourth_backend_runs_end_to_end_via_registry(tmp_path: Path) -> None:
    script = tmp_path / "fake_echo.py"
    script.write_text(FAKE_ECHO_CLI.strip() + "\n", encoding="utf-8")
    adapter = EchoAdapter(script)
    register_adapter(adapter)
    try:
        mcp, manager = create_server(settings=_settings(tmp_path), nesting=NOT_NESTED)
        try:
            # 1. Appears in `backends` with health, version, and defaults.
            backends_out = _data(await mcp.call_tool("backends", {}))
            assert "echo" in backends_out["backends"]
            echo_info = backends_out["backends"]["echo"]
            assert echo_info["healthy"] is True
            assert echo_info["available"] is True
            assert echo_info["version"] == "echo-cli 1.0.0"
            assert echo_info["default_model"] == "echo-v1"

            # 2. Runs end to end through `run` and `jobs` (using its alias too).
            receipt = _data(await mcp.call_tool("run", {"backend": "echo-cli", "prompt": "hello fourth"}))
            assert receipt["backend"] == "echo"
            job_id = receipt["job_id"]
            assert job_id.startswith("echo-")

            collected = _data(await mcp.call_tool("jobs", {"job_ids": [job_id], "wait_seconds": 30}))
            assert collected["all_done"] is True
            [job] = collected["jobs"]
            assert job["status"] == "completed"
            assert job["backend"] == "echo"
            assert job["response"] == "echo:hello fourth"
            assert job["session_id"] == "echo-sess-1"
            assert job["model"] == "echo-v1-live"
            assert job["quota_pool"] == "echo"
            assert job["usage"]["total_tokens"] == 18

            listed = _data(await mcp.call_tool("jobs", {"backend": "echo"}))
            assert listed["count"] == 1
            assert listed["jobs"][0]["job_id"] == job_id

            # 3. Counted in `usage` (summary and dashboard views).
            summary = _data(await mcp.call_tool("usage", {"period": "all", "backend": "echo"}))
            assert summary["totals"]["runs"] == 1
            assert summary["totals"]["completed"] == 1
            assert summary["totals"]["total_tokens"] == 18
            assert summary["by_quota_pool"]["echo"]["total_tokens"] == 18

            dashboard = _data(await mcp.call_tool("usage", {"period": "all", "view": "dashboard", "verbose": True}))
            assert dashboard["totals"]["runs"] == 1
            assert dashboard["totals"]["tokens"]["total_tokens"] == 18
            assert dashboard["by_backend"]["echo"]["tokens"]["total_tokens"] == 18

            # 4. Stream without terminal event fails cleanly even on exit code 0.
            cut = _data(await mcp.call_tool("run", {"backend": "echo", "prompt": "CUT_SHORT", "wait_seconds": 30}))
            assert cut["status"] == "failed"
            assert cut["failure_reason"] == "cli_error"
            assert "without its final event" in cut["error"]

            # 5. Capability flags enforced generically without core branches.
            fast_res = _data(await mcp.call_tool("run", {"backend": "echo", "prompt": "fast?", "fast": True, "wait_seconds": 30}))
            assert fast_res["status"] == "completed"
            assert "echo has no fast tier." in fast_res["warning"]
            with pytest.raises(Exception, match="worktree=True is only supported"):
                await mcp.call_tool("run", {"backend": "echo", "prompt": "wt", "mode": "edit", "worktree": True})
        finally:
            await manager.shutdown()
    finally:
        unregister_adapter(adapter.name)


async def test_fourth_backend_without_declared_pool_in_quota_view(tmp_path: Path) -> None:
    script = tmp_path / "fake_echo.py"
    script.write_text(FAKE_ECHO_CLI.strip() + "\n", encoding="utf-8")
    adapter = EchoAdapter(script)
    register_adapter(adapter)
    try:
        mcp, manager = create_server(settings=_settings(tmp_path), nesting=NOT_NESTED)
        try:
            out = _data(await mcp.call_tool("usage", {"period": "all", "quota": True}))
            quota = out["quota"]
            pools = quota["pools"]
            assert [p["pool"] for p in pools] == ["codex", "agy_gemini", "agy_claude_gpt", "claude", "echo"]
            echo_pool = pools[-1]
            assert echo_pool["backend"] == "echo"
            assert echo_pool["priority"] == "unknown"
            assert echo_pool["note"] == "echo has no quota read-out."
            assert "echo" not in quota["summary"]
        finally:
            await manager.shutdown()
    finally:
        unregister_adapter(adapter.name)


class LabeledEchoAdapter(EchoAdapter):
    """Fourth backend that declares a named quota pool with a distinct dashboard label."""

    name = "echo_labeled"
    cli_name = "echo-labeled-cli"
    aliases = ()
    quota_pools = (
        QuotaPoolSpec(
            pool="echo_pool",
            backend="echo_labeled",
            label="Echo Labeled",
            dashboard_label="Echo Plan",
        ),
    )


async def test_fourth_backend_with_declared_pool_in_dashboard_text(tmp_path: Path) -> None:
    script = tmp_path / "fake_echo.py"
    script.write_text(FAKE_ECHO_CLI.strip() + "\n", encoding="utf-8")
    adapter = LabeledEchoAdapter(script)
    register_adapter(adapter)
    try:
        assert "echo_labeled" in dashboard_module.BACKENDS
        assert "echo_pool" in dashboard_module.QUOTA_POOLS
        assert dashboard_module.POOL_LABELS["echo_pool"] == "Echo Plan"

        mcp, manager = create_server(settings=_settings(tmp_path), nesting=NOT_NESTED)
        try:
            done = _data(await mcp.call_tool("run", {"backend": "echo_labeled", "prompt": "hi", "wait_seconds": 30}))
            assert done["status"] == "completed"
            assert done["quota_pool"] == "echo_pool"

            out = _data(await mcp.call_tool("usage", {"period": "all", "view": "dashboard", "verbose": True}))
            text = out["text"]
            lines = text.splitlines()
            quota_start = next(i for i, line in enumerate(lines) if line.startswith("QUOTA LEFT"))
            used_start = next(i for i, line in enumerate(lines) if line.startswith("USED THROUGH THE BRIDGE"))
            caveats_start = next(i for i, line in enumerate(lines) if line.startswith("NOT IN THE TOTALS"))
            quota_lines = lines[quota_start:used_start]
            used_lines = lines[used_start:caveats_start]

            quota_row = next(line for line in quota_lines if line.startswith("  Echo Plan"))
            assert "no reading: echo_labeled has no quota read-out." in quota_row

            used_row = next((line for line in used_lines if line.startswith("  Echo Plan")), None)
            assert used_row is not None, f"Expected 'Echo Plan' row in used section, got:\n" + "\n".join(used_lines)
            assert "1" in used_row and "18" in used_row
            assert not any(line.startswith("  echo_pool") for line in used_lines)
        finally:
            await manager.shutdown()
    finally:
        unregister_adapter(adapter.name)
