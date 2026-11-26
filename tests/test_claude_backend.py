"""Tests for the Claude backend: dispatch, pool, usage, resume, permissions, worktree, quota and failures.

A stand-in script plays the `claude` CLI. No test starts a real CLI or spends quota: every other
backend is pointed at a path that does not exist.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_bridge.claude_runner import (
    CLAUDE_EDIT_DENY_RULES,
    ClaudeRunner,
    parse_claude_stream_events,
)
from agent_bridge.config import (
    Settings,
    classify_quota_pool,
    normalize_backend,
    normalize_quota_pool,
    split_claude_model_and_effort,
)
from agent_bridge.jobs import JobManager
from agent_bridge.models import RunRequest
from agent_bridge.quota import claude_quota_from_rate_limit, merge_claude_quota
from agent_bridge.server import create_server
from agent_bridge.usage import normalize_usage

FAKE_CLAUDE = r'''
import json, os, sys, time, uuid

args = sys.argv[1:]
if "--version" in args:
    print("9.9.9 (Claude Code)")
    sys.exit(0)

prompt = sys.stdin.read()

def flag(name):
    return args[args.index(name) + 1] if name in args else None

sid = flag("--resume") or str(uuid.uuid4())
model = flag("--model") or "cli-default"
live_model = "claude-%s-9-9" % model

def emit(obj):
    print(json.dumps(obj), flush=True)

emit({"type": "system", "subtype": "init", "session_id": sid, "model": live_model,
      "permissionMode": flag("--permission-mode"), "mcp_servers": []})

if "FAIL_AUTH" in prompt:
    emit({"type": "result", "subtype": "success", "is_error": True, "session_id": sid,
          "result": "Invalid API key - Please run /login", "api_error_status": 401})
    sys.exit(1)

if "DENIED_EMPTY" in prompt:
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": "",
          "permission_denials": [{"tool_name": "Write", "tool_use_id": "t1", "tool_input": {}}],
          "usage": {"input_tokens": 1, "output_tokens": 1}})
    sys.exit(0)

if "WRITE_FILE" in prompt:
    with open(os.path.join(os.getcwd(), "made_by_claude.txt"), "w", encoding="utf-8") as fh:
        fh.write("x")

seen = {
    "argv": args,
    "prompt": prompt,
    "cwd": os.getcwd(),
    "marker": os.environ.get("AGENT_BRIDGE_PARENT_JOB"),
    "owner": os.environ.get("AGENT_BRIDGE_PARENT_OWNER"),
    "has_claudecode": "CLAUDECODE" in os.environ,
    "has_session_id": "CLAUDE_CODE_SESSION_ID" in os.environ,
    "has_oauth_token": "CLAUDE_CODE_OAUTH_TOKEN" in os.environ,
}
emit({"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
      "message": {"content": [{"type": "text", "text": "thinking aloud"}]}})
emit({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {
    "status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.86,
    "resetsAt": int(time.time()) + 3 * 86400,
    "unifiedWindows": {"seven_day": {"utilization": 0.86, "resetsAt": int(time.time()) + 3 * 86400}}}})
emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
      "result": json.dumps(seen), "num_turns": 2, "total_cost_usd": 0.0034, "permission_denials": [],
      "usage": {"input_tokens": 4, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 5000,
                "output_tokens": 132, "output_tokens_details": {"thinking_tokens": 30}}})
'''


def _make_settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, Any] = dict(
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
        # A path that does not exist: if a job were ever sent to the wrong backend, it fails at once
        # instead of starting a real CLI.
        agy_bin=str(tmp_path / "missing-agy.exe"),
        codex_bin=str(tmp_path / "missing-codex.exe"),
        claude_bin=str(tmp_path / "missing-claude.exe"),
        default_cwd=tmp_path,
    )
    base.update(overrides)
    return Settings(**base)


class StubClaudeRunner(ClaudeRunner):
    def __init__(self, settings: Settings, script_path: Path) -> None:
        self._script_path = script_path
        super().__init__(settings)

    def resolve_command(self) -> list[str]:
        return [sys.executable, str(self._script_path)]


def _manager(tmp_path: Path, **settings_overrides: object) -> JobManager:
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE.strip(), encoding="utf-8")
    settings = _make_settings(tmp_path, **settings_overrides)
    return JobManager(settings, claude_runner=StubClaudeRunner(settings, script))


def _seen(record: Any) -> dict[str, Any]:
    return json.loads(record.response)


def _value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _tool_data(raw: Any) -> dict[str, Any]:
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
        return raw[1]
    if isinstance(raw, dict):
        return raw
    content_list = raw.content if hasattr(raw, "content") else raw
    return json.loads(content_list[0].text)


async def test_claude_job_goes_to_the_claude_runner_and_is_recorded_under_its_own_pool(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    rec = await manager.run(manager.prepare_request(backend="claude", prompt="hello"))

    assert rec.status == "completed", rec.error
    assert rec.backend == "claude"
    assert rec.job_id.startswith("claude-")
    assert rec.quota_pool == "claude"
    assert rec.session_id
    # The name asked for is kept, and the model the CLI says it resolved to is what is reported.
    assert rec.requested_model is None
    assert rec.model == "claude-sonnet-9-9"
    assert rec.effort == "high"
    assert _seen(rec)["prompt"] == "hello"

    stored = manager.store.get_job(rec.job_id)
    assert stored is not None and stored.backend == "claude" and stored.quota_pool == "claude"

    summary = await manager.get_usage_summary(period="session")
    assert summary["by_backend"]["claude"]["runs"] == 1
    assert summary["by_quota_pool"]["claude"]["runs"] == 1
    assert "agy_claude_gpt" not in summary["by_quota_pool"]
    await manager.shutdown()


def test_claude_backend_names_and_pool_do_not_collide_with_agy_claude_models() -> None:
    assert normalize_backend("claude") == "claude"
    assert normalize_backend("claude-code") == "claude"
    assert normalize_backend("agy-claude") == "neon"
    assert normalize_quota_pool("claude") == "claude"

    for model in ("sonnet", "opus", "haiku", "fable", "claude-opus-5-5", None):
        assert classify_quota_pool("claude", model) == "claude"
    # The same model names on agy still belong to the agy pool.
    assert classify_quota_pool("neon", "claude-sonnet-high") == "agy_claude_gpt"
    assert classify_quota_pool("neon", "neon-high") == "agy_gemini"
    assert classify_quota_pool("codex", "gpt-6.2-sol") == "codex"


async def test_rows_left_without_a_pool_are_filled_in_by_backend_first(tmp_path: Path) -> None:
    """A Claude row whose model is named `sonnet` must not be filed under the agy Claude pool."""
    import sqlite3

    manager = _manager(tmp_path)
    rec = await manager.run(manager.prepare_request(backend="claude", model="sonnet", prompt="hello"))
    await manager.shutdown()
    db_path = manager.store.db_path

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE jobs SET quota_pool = NULL WHERE job_id = ?", (rec.job_id,))
    conn.execute("UPDATE usage_events SET quota_pool = NULL WHERE job_id = ?", (rec.job_id,))
    conn.commit()
    conn.close()

    # Opening the store again runs the same fill-in that an upgrade runs.
    reopened = JobManager(_make_settings(tmp_path))
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT quota_pool FROM jobs WHERE job_id = ?", (rec.job_id,)).fetchone() == ("claude",)
    assert conn.execute(
        "SELECT DISTINCT quota_pool FROM usage_events WHERE job_id = ?", (rec.job_id,)
    ).fetchall() == [("claude",)]
    conn.close()
    await reopened.shutdown()


async def test_claude_usage_row_counts_cache_tokens_as_input(tmp_path: Path) -> None:
    usage, metrics = normalize_usage(
        "claude",
        {
            "input_tokens": 4,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 5000,
            "output_tokens": 132,
            "output_tokens_details": {"thinking_tokens": 30},
        },
    )
    assert usage == {
        "input_tokens": 6004,
        "output_tokens": 132,
        "cached_tokens": 5000,
        "reasoning_tokens": 30,
        "total_tokens": 6136,
        "cache_write_tokens": 1000,
    }
    assert metrics is not None and metrics["provider"] == "claude" and metrics["input_tokens"] == 4

    manager = _manager(tmp_path)
    rec = await manager.run(manager.prepare_request(backend="claude", prompt="count"))
    assert rec.usage_status == "exact"
    assert rec.usage is not None and rec.usage["total_tokens"] == 6136
    assert rec.provider_metrics is not None
    assert rec.provider_metrics["cost_usd_estimate"] == pytest.approx(0.0034)
    assert rec.provider_metrics["num_turns"] == 2

    summary = await manager.get_usage_summary(period="session")
    tokens = summary["by_quota_pool"]["claude"]["tokens"]
    assert tokens["total_tokens"] == 6136
    assert tokens["cached_tokens"] == 5000
    await manager.shutdown()


async def test_claude_resume_continues_the_same_session_and_inherits_model(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first = await manager.run(
        manager.prepare_request(backend="claude", model="opus", effort="low", prompt="first")
    )
    assert first.status == "completed"
    assert "--resume" not in _seen(first)["argv"]

    # No backend, model or effort given: all three come from the job being resumed.
    second = await manager.run(manager.prepare_request(backend=None, prompt="second", resume_from=first.job_id))
    assert second.status == "completed", second.error
    argv = _seen(second)["argv"]
    assert _value_after(argv, "--resume") == first.session_id
    assert second.session_id == first.session_id
    assert second.backend == "claude"
    assert _value_after(argv, "--model") == "opus"
    assert _value_after(argv, "--effort") == "low"

    with pytest.raises(ValueError, match="belongs to backend 'claude'"):
        manager.prepare_request(backend="codex", prompt="x", resume_from=first.job_id)
    await manager.shutdown()


async def test_claude_read_only_and_edit_runs_get_different_permissions(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    read_only = await manager.run(manager.prepare_request(backend="claude", prompt="look"))
    edit = await manager.run(manager.prepare_request(backend="claude", prompt="change", mode="edit"))

    ro_argv = _seen(read_only)["argv"]
    assert _value_after(ro_argv, "--permission-mode") == "dontAsk"
    assert "--disallowedTools" not in ro_argv

    edit_argv = _seen(edit)["argv"]
    assert _value_after(edit_argv, "--permission-mode") == "auto"
    assert "--disallowedTools" in edit_argv
    for rule in CLAUDE_EDIT_DENY_RULES:
        assert rule in edit_argv
    assert "Bash(git push *)" in CLAUDE_EDIT_DENY_RULES

    for argv in (ro_argv, edit_argv):
        assert argv[0] == "-p"
        # No MCP servers, nobody to answer a prompt, never the unguarded modes, never the mode that
        # would skip the subscription login.
        assert "--strict-mcp-config" in argv
        assert "--mcp-config" not in argv
        assert _value_after(argv, "--permission-prompts") == "none"
        assert "bypassPermissions" not in argv
        assert "--dangerously-skip-permissions" not in argv
        assert "--bare" not in argv
    await manager.shutdown()


async def test_claude_edit_job_runs_in_a_bridge_worktree_and_agy_still_cannot(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", *git_env, "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    manager = _manager(tmp_path)
    rec = await manager.run(
        manager.prepare_request(
            backend="claude",
            prompt="WRITE_FILE",
            cwd=str(repo),
            mode="edit",
            worktree=True,
        )
    )
    assert rec.status == "completed", rec.error
    assert rec.worktree_path is not None
    wt = Path(rec.worktree_path)
    assert wt.is_relative_to((tmp_path / "state" / "worktrees").resolve())
    assert rec.worktree_branch == f"agent-bridge/{rec.job_id}"
    assert rec.worktree_commit
    assert Path(_seen(rec)["cwd"]).resolve() == wt
    assert (wt / "made_by_claude.txt").exists()
    assert not (repo / "made_by_claude.txt").exists()

    # A resume stays in that worktree without being told where it is.
    again = await manager.run(manager.prepare_request(backend=None, prompt="more", resume_from=rec.job_id))
    assert again.status == "completed", again.error
    assert Path(_seen(again)["cwd"]).resolve() == wt
    assert again.worktree_path == rec.worktree_path

    with pytest.raises(ValueError, match="worktree=True is only supported"):
        manager.prepare_request(backend="neon", prompt="x", cwd=str(repo), mode="edit", worktree=True)
    with pytest.raises(ValueError, match="requires mode='edit'"):
        manager.prepare_request(backend="claude", prompt="x", cwd=str(repo), worktree=True)
    await manager.shutdown()


async def test_claude_subagent_gets_the_nesting_marker_and_not_the_parent_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As if the bridge itself had been started by a Claude Code session.
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "left-alone")

    manager = _manager(tmp_path)
    rec = await manager.run(manager.prepare_request(backend="claude", prompt="env"))
    seen = _seen(rec)
    assert seen["marker"] == rec.job_id
    assert seen["owner"] == manager.owner_id
    assert seen["has_claudecode"] is False
    assert seen["has_session_id"] is False
    # How the user signed in is not the bridge's business: it is passed on untouched.
    assert seen["has_oauth_token"] is True
    await manager.shutdown()


async def test_claude_quota_is_what_the_last_job_reported(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    before = await manager.get_usage_summary(period="session", quota_pool="claude", include_quota=True)
    assert set(before["quota"]) == {"claude"}
    assert before["quota"]["claude"]["status"] == "unknown"
    assert "no command that prints the plan limits" in before["quota"]["claude"]["note"]

    await manager.run(manager.prepare_request(backend="claude", prompt="quota"))

    after = await manager.get_usage_summary(period="session", quota_pool="claude", include_quota=True)
    reading = after["quota"]["claude"]
    assert reading["status"] == "ok"
    assert reading["source"] == "last_claude_job"
    weekly = next(w for w in reading["windows"] if w["window_kind"] == "weekly")
    assert weekly["used_percent"] == 86.0
    assert weekly["remaining_percent"] == 14.0
    assert weekly["resets_in_seconds"] > 0

    pool = next(p for p in after["quota_advisory"]["pools"] if p["pool"] == "claude")
    assert pool["backend"] == "claude"
    assert pool["weekly"]["remaining_percent"] == 14.0
    assert pool["priority"] == "conserve"
    await manager.shutdown()


def test_claude_rate_limit_reading_and_merge() -> None:
    assert claude_quota_from_rate_limit(None) is None
    assert claude_quota_from_rate_limit({"status": "allowed"}) is None

    weekly = claude_quota_from_rate_limit(
        {
            "status": "allowed_warning",
            "rateLimitType": "seven_day",
            "utilization": 0.5,
            "resetsAt": 4102444800,
            "unifiedWindows": {"seven_day": {"utilization": 0.5, "resetsAt": 4102444800}},
        }
    )
    assert weekly is not None and weekly["pool"] == "claude" and weekly["rate_limit_reached"] is False
    assert [(w["window"], w["used_percent"]) for w in weekly["windows"]] == [("weekly", 50.0)]

    five = claude_quota_from_rate_limit(
        {"status": "rejected", "rateLimitType": "five_hour", "utilization": 1.2, "resetsAt": 4102444800}
    )
    assert five is not None and five["rate_limit_reached"] is True
    assert [(w["window"], w["used_percent"], w["remaining_percent"]) for w in five["windows"]] == [
        ("5h", 100.0, 0.0)
    ]

    # A job reports one window at a time. The other one is kept from the reading before.
    merged = merge_claude_quota(weekly, five)
    assert sorted(w["window"] for w in merged["windows"]) == ["5h", "weekly"]
    # A window whose reset time has passed is not carried over.
    stale = {**weekly, "windows": [{**weekly["windows"][0], "reset_time": "2001-01-01T00:00:00+00:00"}]}
    assert [w["window"] for w in merge_claude_quota(stale, five)["windows"]] == ["5h"]


def test_advisory_never_urges_spending_the_claude_plan_before_its_reset() -> None:
    """13% left with a day to go reads "use it before reset" on the other pools. Not on this one."""
    from datetime import datetime, timedelta, timezone

    from agent_bridge.quota import build_quota_advisory

    now = datetime(2026, 11, 26, 14, 0, tzinfo=timezone.utc)
    reset = (now + timedelta(hours=21)).isoformat(timespec="seconds")
    window = {"window": "weekly", "used_percent": 87.0, "remaining_percent": 13.0, "reset_time": reset}

    claude_only = build_quota_advisory(
        {"claude": {"backend": "claude", "pool": "claude", "status": "ok", "windows": [dict(window, id="seven_day")]}},
        now_utc=now,
    )
    pool = claude_only["pools"][0]
    assert pool["pool"] == "claude"
    assert pool["weekly"]["signal"] == "expiring_soon_use_it"
    assert pool["priority"] == "normal"
    assert "same allowance" in pool["recommendation"]
    assert claude_only["expiring_weekly_alerts"] == []
    assert "WEEKLY RESET PRIORITY" not in claude_only["summary"]

    # The same numbers on Codex do raise the alert: the rule is unchanged for the other pools.
    codex_only = build_quota_advisory(
        {"codex": {"backend": "codex", "pool": "codex", "status": "ok", "windows": [dict(window, slot="secondary")]}},
        now_utc=now,
    )
    assert codex_only["pools"][0]["priority"] == "use_before_reset"
    assert len(codex_only["expiring_weekly_alerts"]) == 1


async def test_claude_failures_are_classified(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    auth = await manager.run(manager.prepare_request(backend="claude", prompt="FAIL_AUTH"))
    assert auth.status == "failed"
    assert auth.failure_reason == "auth_error"
    assert "Invalid API key" in (auth.error or "")
    assert "claude auth login" in (auth.retry_guidance or "")

    denied = await manager.run(manager.prepare_request(backend="claude", prompt="DENIED_EMPTY"))
    assert denied.status == "failed"
    assert denied.failure_reason == "permission_error"
    assert "Write" in (denied.error or "")
    await manager.shutdown()


async def test_claude_cli_that_is_not_installed_fails_the_job_cleanly(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    manager = JobManager(settings)  # the real runner, pointed at a path that does not exist
    rec = await manager.run(manager.prepare_request(backend="claude", prompt="x"))
    assert rec.status == "failed"
    assert "AGENT_BRIDGE_CLAUDE_BIN" in (rec.error or "")
    assert rec.provider_invoked is False
    await manager.shutdown()


def test_claude_model_and_effort_names(tmp_path: Path) -> None:
    assert split_claude_model_and_effort("opus-high") == ("opus", "high")
    assert split_claude_model_and_effort("claude-sonnet-medium") == ("sonnet", "medium")
    assert split_claude_model_and_effort("claude-sonnet") == ("sonnet", None)
    assert split_claude_model_and_effort("Haiku") == ("haiku", None)
    # A full model id is passed through as it is.
    assert split_claude_model_and_effort("claude-opus-5-5") == ("claude-opus-5-5", None)
    assert split_claude_model_and_effort(None) == (None, None)

    runner = ClaudeRunner(_make_settings(tmp_path))
    assert runner.effective_model_and_effort(None, None) == ("sonnet", "high")
    assert runner.effective_model_and_effort("opus-low", None) == ("opus", "low")
    assert runner.effective_model_and_effort("opus-low", "xhigh") == ("opus", "xhigh")
    assert runner.effective_model_and_effort("default", None) == (None, "high")

    inherit = ClaudeRunner(_make_settings(tmp_path, default_claude_model=None, default_claude_effort=None))
    assert inherit.effective_model_and_effort(None, None) == (None, None)
    request = RunRequest(backend="claude", prompt="p", cwd=str(tmp_path))
    argv = inherit.build_args.__func__(  # type: ignore[attr-defined]
        StubClaudeRunner(_make_settings(tmp_path), tmp_path / "x.py"),
        request,
        effective_model=None,
        effective_effort=None,
    )
    assert "--model" not in argv and "--effort" not in argv


def test_claude_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    for name in ("AGENT_BRIDGE_CLAUDE_MODEL", "AGENT_BRIDGE_CLAUDE_EFFORT", "AGENT_BRIDGE_CLAUDE_BIN"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings.from_env()
    assert (defaults.default_claude_model, defaults.default_claude_effort) == ("sonnet", "high")
    assert defaults.claude_bin is None

    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_MODEL", "inherit")
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_EFFORT", "med")
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_BIN", "C:/somewhere/claude.exe")
    custom = Settings.from_env()
    assert custom.default_claude_model is None
    assert custom.default_claude_effort == "medium"
    assert custom.claude_bin == "C:/somewhere/claude.exe"


def test_parse_claude_stream_keeps_the_reply_and_ignores_noise() -> None:
    lines = [
        "not json at all",
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x"}),
        json.dumps(
            {
                "type": "assistant",
                "parent_tool_use_id": "tool-1",
                "message": {"content": [{"type": "text", "text": "a subagent's words"}]},
            }
        ),
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial reply"}]}}
        ),
    ]
    cut_short = parse_claude_stream_events(lines)
    assert cut_short["session_id"] == "s1" and cut_short["model"] == "claude-x"
    assert cut_short["result_seen"] is False
    assert cut_short["response"] == "partial reply"

    done = parse_claude_stream_events(
        [*lines, json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "final"})]
    )
    assert done["result_seen"] is True and done["response"] == "final" and done["errors"] == []

    stopped = parse_claude_stream_events(
        [*lines, json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True})]
    )
    assert stopped["is_error"] is True
    assert stopped["errors"] == ["claude ended with error_max_turns"]
    assert stopped["response"] == "partial reply"


async def test_list_backends_and_review_code_know_the_claude_backend(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    mcp, _ = create_server(settings=manager.settings, manager=manager)

    backends = _tool_data(await mcp.call_tool("list_backends", {}))
    claude = backends["backends"]["claude"]
    assert claude["healthy"] is True and "9.9.9" in claude["version"]
    assert claude["quota_pool"] == "claude"
    assert (claude["default_model"], claude["default_effort"]) == ("sonnet", "high")
    assert backends["nesting"] == {"nested": False}
    # The two CLIs that are not installed here are reported as such, not hidden.
    assert backends["backends"]["codex"]["command_found"] is False
    assert backends["backends"]["neon"]["command_found"] is False

    review = _tool_data(
        await mcp.call_tool(
            "review_code",
            {"backends": ["claude"], "cwd": str(tmp_path), "claude_model": "haiku", "focus": "bugs only"},
        )
    )
    assert [r["backend"] for r in review["reviews"]] == ["claude"]
    result = review["reviews"][0]
    assert result["status"] == "completed"
    seen = json.loads(result["response"])
    assert _value_after(seen["argv"], "--model") == "haiku"
    assert _value_after(seen["argv"], "--permission-mode") == "dontAsk"
    assert "bugs only" in seen["prompt"]
    await manager.shutdown()
