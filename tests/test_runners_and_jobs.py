"""Tests for runners, stream-json usage, timeouts, session serialization, multi-window SQLite, and health checks."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_bridge.config import Settings
from agent_bridge.claude_runner import ClaudeRunner
from agent_bridge.jobs import JobManager
from agent_bridge.models import RunRequest
from agent_bridge.runners import (
    CodexRunner,
    NeonRunner,
    _parse_git_worktree_porcelain,
    parse_agy_json_output,
    parse_codex_jsonl_events,
)
from agent_bridge.server import create_server


def _make_settings(tmp_path: Path, **overrides: object) -> Settings:
    base = dict(
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
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class StubAgyRunner(NeonRunner):
    # The stand-in scripts match prompts word for word, so the read-only guard is off here.
    read_only_guard = ""

    def __init__(self, settings: Settings, script_path: Path) -> None:
        self._script_path = script_path
        super().__init__(settings)

    def resolve_command(self) -> list[str]:
        return [sys.executable, str(self._script_path)]


class StubCodexRunner(CodexRunner):
    def __init__(self, settings: Settings, script_path: Path) -> None:
        self._script_path = script_path
        super().__init__(settings)

    def resolve_command(self) -> list[str]:
        return [sys.executable, str(self._script_path)]


@pytest.mark.parametrize("backend", ["claude", "codex", "neon"])
@pytest.mark.parametrize("deadline_expires_before_spawn", [True, False])
async def test_runner_startup_deadline_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, deadline_expires_before_spawn: bool,
) -> None:
    from agent_bridge import claude_runner, process_control, runners

    settings = _make_settings(tmp_path)
    script = tmp_path / "stand_in.py"
    script.write_text("raise AssertionError('must not start')", encoding="utf-8")
    runner = {
        "claude": ClaudeRunner(settings),
        "codex": StubCodexRunner(settings, script),
        "neon": StubAgyRunner(settings, script),
    }[backend]
    monkeypatch.setattr(runner, "resolve_command", lambda: [sys.executable, str(script)])
    module = claude_runner if backend == "claude" else runners
    now = 100.0
    deadline = 100.125
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now))
    spawned = False

    async def fake_spawn(*args, **kwargs):
        nonlocal spawned
        spawned = True
        process_control._SPAWN_CAPTURE.get()(SimpleNamespace(pid=123))
        await asyncio.Event().wait()

    async def unverified_cleanup(*_args):
        return False

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(process_control, "_terminate_windows_early", unverified_cleanup)
    monkeypatch.setattr(process_control, "_terminate_posix_pgid", unverified_cleanup)

    async def launch_bookkeeping():
        nonlocal now
        if deadline_expires_before_spawn:
            now = deadline

    request = RunRequest(backend=backend, prompt="test", cwd=str(tmp_path), timeout_seconds=1)
    kwargs = dict(
        deadline=deadline, model_notice=None, on_session=None, on_activity=None,
        on_model=None, on_provider_start=launch_bookkeeping,
    )
    if backend == "neon":
        result = await runner._run_once(
            request, resolved_model=None, prompt_text="test", extra_dirs=[], prior_watermark=None, **kwargs,
        )
    else:
        result = await runner._run_once(request, effective_model=None, effective_effort=None, **kwargs)
    assert spawned is not deadline_expires_before_spawn
    assert result.status == "timed_out"
    assert result.provider_state == ("stopped" if deadline_expires_before_spawn else "possibly_running")


@pytest.mark.parametrize(
    "backend,event,activity,expected_status",
    [
        ("codex", {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}},
         "turn completed", "exact"),
        ("neon", {"event": "step_update", "step_update": {
            "step_index": 1, "step_type": "agent_response", "state": "DONE",
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }}, "agent_response (DONE)", "partial"),
    ],
)
async def test_cancelled_job_keeps_streamed_usage(
    tmp_path: Path, backend: str, event: dict, activity: str, expected_status: str,
) -> None:
    script = tmp_path / "stand_in.py"
    script.write_text(
        "import sys, time\n"
        "if 'models' in sys.argv:\n"
        "    print('gemini-4-neon-high\\tGemini 4 Neon (High)')\n"
        "    sys.exit(0)\n"
        f"print({json.dumps(event)!r}, flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    settings = _make_settings(tmp_path)
    manager = JobManager(
        settings, codex_runner=StubCodexRunner(settings, script), neon_runner=StubAgyRunner(settings, script),
    )
    try:
        job = await manager.start_async(manager.prepare_request(backend=backend, prompt="test"))
        async with asyncio.timeout(10):
            while manager._local_jobs[job.job_id].last_activity != activity:
                await asyncio.sleep(0.01)
        await manager.cancel(job.job_id)
        stored = manager.store.get_job(job.job_id)
        assert stored is not None and stored.status == "cancelled"
        assert stored.usage_status == expected_status
        assert stored.usage is not None and stored.usage["total_tokens"] == 10
    finally:
        await manager.shutdown()


def test_parse_agy_json_output_and_codex_jsonl() -> None:
    noisy = 'Fetching...\n{"conversation_id":"c1","status":"SUCCESS","response":"Hi\\n"}\n'
    parsed = parse_agy_json_output(noisy)
    assert parsed is not None and parsed["conversation_id"] == "c1"

    lines = [
        '{"type":"thread.started","thread_id":"thr-42"}',
        '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"Part 1"}}',
        '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"Part 2"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":30,"reasoning_output_tokens":5}}',
    ]
    c = parse_codex_jsonl_events(lines)
    assert c["session_id"] == "thr-42"
    assert c["response"] == "Part 1\n\nPart 2"
    assert c["errors"] == []


def test_parse_git_worktree_porcelain() -> None:
    sample = (
        "worktree C:/repo\n"
        "HEAD 1111111111111111111111111111111111111111\n"
        "branch refs/heads/main\n\n"
        "worktree C:/repo/.codex/worktrees/wt-1\n"
        "HEAD 2222222222222222222222222222222222222222\n"
        "branch refs/heads/codex/wt-1\n"
    )
    wts = _parse_git_worktree_porcelain(sample)
    assert len(wts) == 2
    assert wts[1]["path"] == "C:/repo/.codex/worktrees/wt-1"
    assert wts[1]["branch"] == "codex/wt-1"
    assert wts[1]["commit"] == "2222222222222222222222222222222222222222"


async def test_agy_stream_json_per_turn_usage_no_double_count_and_wall_time(tmp_path: Path) -> None:
    """Finding 2: resumed agy turn uses per-turn step_update tokens & wall duration (not cumulative)."""
    fake_agy = tmp_path / "fake_agy_stream.py"
    fake_agy.write_text(
        """
import json, sys

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-medium\\tGemini 4 Neon (Medium)")
    sys.exit(0)

is_resume = "--resume" in args
cid = "conv-stream-1"
print(json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "gemini-4-neon-medium", "permission_mode": "request-review"}}), flush=True)
if not is_resume:
    print(json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "TURN1", "usage": {"input_tokens": 10000, "output_tokens": 50, "thinking_tokens": 10, "cache_read_tokens": 0, "total_tokens": 10050}}}), flush=True)
    print(json.dumps({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": "TURN1", "duration_seconds": 99.0, "num_turns": 1, "usage": {"input_tokens": 10000, "output_tokens": 50, "thinking_tokens": 10, "cache_read_tokens": 0, "total_tokens": 10050}}}), flush=True)
else:
    # Cumulative result reports 13070 tokens and 250.0s cumulative duration, while turn 2 step only used 3020!
    print(json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 3, "state": "DONE", "step_type": "agent_response", "text_delta": "TURN2", "usage": {"input_tokens": 3000, "output_tokens": 20, "thinking_tokens": 0, "cache_read_tokens": 9000, "total_tokens": 3020}}}), flush=True)
    print(json.dumps({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": "TURN2", "duration_seconds": 250.0, "num_turns": 2, "usage": {"input_tokens": 13000, "output_tokens": 70, "thinking_tokens": 10, "cache_read_tokens": 9000, "total_tokens": 13070}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    manager = JobManager(settings, neon_runner=StubAgyRunner(settings, fake_agy))

    r1 = await manager.run(manager.prepare_request(backend="neon", model="neon-medium", prompt="first"))
    assert r1.status == "completed"
    assert r1.usage is not None and r1.usage["total_tokens"] == 10050
    assert r1.usage_status == "exact"
    assert (r1.duration_seconds or 0) < 15.0  # wall clock, not 99.0!

    r2 = await manager.run(manager.prepare_request(backend="neon", model="neon-medium", prompt="second", resume_from=r1.job_id))
    assert r2.status == "completed"
    assert r2.usage is not None and r2.usage["total_tokens"] == 3020
    assert r2.usage["cached_tokens"] == 9000
    assert r2.usage_status == "exact"
    assert (r2.duration_seconds or 0) < 15.0  # wall clock, not 250.0!

    summary = await manager.get_usage_summary(period="session")
    # Sum must be 10050 + 3020 = 13070, NOT 10050 + 13070 = 23120!
    assert summary["by_backend"]["neon"]["tokens"]["total_tokens"] == 13070
    assert summary["by_backend"]["neon"]["usage_coverage"]["exact"] == 2
    await manager.shutdown()


async def test_agy_exit_code_3_classified_as_provider_timeout_still_running(tmp_path: Path) -> None:
    """Finding 3: agy exit code 3 preserves partial output + session_id and marks possibly_running."""
    fake_agy = tmp_path / "fake_agy_ec3.py"
    fake_agy.write_text(
        """
import json, sys

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-high\\tGemini 4 Neon (High)")
    sys.exit(0)

cid = "conv-timeout-3"
print(json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "gemini-4-neon-high"}}), flush=True)
print(json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "Partial finding 1"}}), flush=True)
sys.stderr.write("print timeout elapsed while turn is still in progress\\n")
sys.exit(3)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    manager = JobManager(settings, neon_runner=StubAgyRunner(settings, fake_agy))
    rec = await manager.run(manager.prepare_request(backend="neon", prompt="audit"))
    assert rec.status == "timed_out"
    assert rec.failure_reason == "provider_timeout_still_running"
    assert rec.provider_state == "possibly_running"
    assert rec.session_id == "conv-timeout-3"
    assert "Partial finding 1" in rec.response
    assert rec.retry_guidance is not None
    await manager.shutdown()


async def test_multi_window_sqlite_isolation_cross_window_resume_and_dead_recovery(tmp_path: Path) -> None:
    """Finding 1: two live managers sharing state_dir do NOT corrupt each other; dead owners DO recover."""
    fake_codex = tmp_path / "fake_codex_mw.py"
    fake_codex.write_text(
        """
import json, sys, time

prompt = sys.stdin.read().strip()
if "HOLD" in prompt:
    print(json.dumps({"type": "thread.started", "thread_id": "thr-win1"}), flush=True)
    time.sleep(1.2)
else:
    print(json.dumps({"type": "thread.started", "thread_id": "thr-win2"}), flush=True)
    time.sleep(0.2)
print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": f"OK:{prompt}"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 10}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    alive_pids = {1001, 1002}

    def is_alive(pid: int) -> bool:
        return pid in alive_pids

    mgr1 = JobManager(
        settings,
        codex_runner=StubCodexRunner(settings, fake_codex),
        owner_id="win-1",
        pid=1001,
        is_pid_alive_fn=is_alive,
    )
    job1 = mgr1.start(mgr1.prepare_request(backend="codex", prompt="HOLD job1"))
    await asyncio.sleep(0.25)

    # Opening Window 2 while Window 1 has an active job MUST NOT mark job1 failed!
    mgr2 = JobManager(
        settings,
        codex_runner=StubCodexRunner(settings, fake_codex),
        owner_id="win-2",
        pid=1002,
        is_pid_alive_fn=is_alive,
    )
    seen_from_w2 = mgr2.get(job1.job_id)
    assert seen_from_w2.status in {"queued", "running"}

    # Window 2 refuses to cancel Window 1's live job.
    with pytest.raises(RuntimeError, match="another live bridge process"):
        await mgr2.cancel(job1.job_id)

    # Window 2 runs its own job concurrently.
    job2 = await mgr2.run(mgr2.prepare_request(backend="codex", prompt="FAST job2"))
    assert job2.status == "completed"

    # Window 2 can wait_for Window 1's job across processes!
    waited = await mgr2.wait_for([job1.job_id], wait_seconds=10, wait_mode="all")
    assert waited[0].status == "completed"
    assert "OK:HOLD job1" in waited[0].response

    # Both jobs survive in both windows and Window 2 can resume Window 1's job_id!
    listed_ids = {j["job_id"] for j in mgr1.list_jobs(limit=10)}
    assert {job1.job_id, job2.job_id}.issubset(listed_ids)

    resumed_req = mgr2.prepare_request(backend=None, prompt="from win2", resume_from=job1.job_id)
    assert resumed_req.session_id == "thr-win1"

    # Simulate a third crashed owner (pid 9999 dead) with a stuck running job.
    mgr1.store.register_owner("dead-win", 9999)
    from agent_bridge.models import JobRecord

    orphan = JobRecord(
        job_id="codex-orphan01",
        owner_id="dead-win",
        backend="codex",
        cwd=str(tmp_path),
        mode="read_only",
        requested_model=None,
        model="codex-default",
        prompt_preview="crashed",
        status="running",
        started_at=time.time() - 5,
    )
    mgr1.store.insert_job(orphan)
    recovered_ids = mgr1.store.recover_dead_owner_jobs("win-1", is_pid_alive_fn=is_alive)
    assert "codex-orphan01" in recovered_ids
    rec_orphan = mgr1.get("codex-orphan01")
    assert rec_orphan.status == "failed"
    assert rec_orphan.failure_reason == "owner_exited"
    assert rec_orphan.provider_state == "unknown"

    await mgr1.shutdown()
    await mgr2.shutdown()


async def test_sync_waiter_cancellation_stops_job_and_separate_wait_keeps_job_alive(tmp_path: Path) -> None:
    """Finding 3: cancelling sync `run()` cancels underlying job; short `wait_seconds` leaves job active."""
    fake_codex = tmp_path / "fake_codex_wait.py"
    fake_codex.write_text(
        """
import json, sys, time

prompt = sys.stdin.read().strip()
print(json.dumps({"type": "thread.started", "thread_id": "thr-w"}), flush=True)
time.sleep(1.5 if "MEDIUM" in prompt else 20)
print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "FINISHED"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, fake_codex))

    # 1. Cancelling a synchronous `run()` cancels its underlying job!
    sync_task = asyncio.create_task(
        manager.run(manager.prepare_request(backend="codex", prompt="FOREVER"))
    )
    await asyncio.sleep(0.35)
    sync_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sync_task

    jobs = manager.list_jobs(limit=5)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "cancelled"

    # 2. Separate wait_seconds expires cleanly while leaving the job running!
    partial = await manager.run(
        manager.prepare_request(backend="codex", prompt="MEDIUM", timeout_seconds=30),
        wait_seconds=1,
    )
    assert partial.status in {"queued", "running"}
    done = await manager.wait_for([partial.job_id], wait_seconds=10, wait_mode="all")
    assert done[0].status == "completed"
    assert done[0].response == "FINISHED"
    await manager.shutdown()


async def test_session_serialization_and_resume_validation_and_codex_flags(tmp_path: Path) -> None:
    """Finding 4 & 5: per-session serialization, strict validation, effort override, review sandbox."""
    overlap_log = tmp_path / "overlap.log"
    fake_codex = tmp_path / "fake_codex_ser.py"
    fake_codex.write_text(
        f"""
import json, sys, time
from pathlib import Path

log_path = Path({str(overlap_log)!r})
prompt = sys.stdin.read().strip()
with log_path.open("a", encoding="utf-8") as fh:
    fh.write(f"START:{{prompt}}\\n")
time.sleep(0.4)
with log_path.open("a", encoding="utf-8") as fh:
    fh.write(f"END:{{prompt}}\\n")

print(json.dumps({{"type": "thread.started", "thread_id": "thr-ser"}}), flush=True)
print(json.dumps({{"type": "item.completed", "item": {{"id": "i0", "type": "agent_message", "text": f"OK:{{prompt}}"}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 5}}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    runner = StubCodexRunner(settings, fake_codex)
    manager = JobManager(settings, codex_runner=runner)

    # Validation: worktree requires mode="edit"
    with pytest.raises(ValueError, match="mode='edit'"):
        manager.prepare_request(backend="codex", prompt="hi", mode="read_only", worktree=True)

    # Validation: Codex resume rejects extra_dirs
    with pytest.raises(ValueError, match="extra_dirs"):
        manager.prepare_request(
            backend="codex",
            prompt="hi",
            resume_from="thr-ser",
            extra_dirs=[str(tmp_path)],
        )

    # Codex native review explicitly passes read-only sandbox + effort override
    rev_req = manager.prepare_request(
        backend="codex",
        prompt="",
        effort="medium",
        review_scope="uncommitted",
    )
    rev_argv, use_stdin = runner.build_args(rev_req, effective_model=None, effective_effort="medium")
    assert not use_stdin
    assert 'sandbox_mode="read-only"' in rev_argv
    assert 'model_reasoning_effort="medium"' in rev_argv

    # Simultaneous turns on the SAME session_id serialize strictly (START->END before next START)
    r_a = manager.prepare_request(backend="codex", prompt="A", resume_from="thr-ser")
    r_b = manager.prepare_request(backend="codex", prompt="B", resume_from="thr-ser")
    results = await manager.run_parallel([r_a, r_b], wait=True, wait_seconds=15)
    assert all(r.status == "completed" for r in results)

    events = overlap_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(events) == 4
    # First turn must finish before second turn starts!
    first_tag = events[0].split(":")[1]
    assert events[1] == f"END:{first_tag}"
    await manager.shutdown()


async def test_transient_retry_readonly_only_and_default_timeout_honored(tmp_path: Path) -> None:
    """Finding 3: transient retry happens once for read_only, never for edit; default timeout respected."""
    counter_file = tmp_path / "calls.txt"
    fake_codex = tmp_path / "fake_codex_trans.py"
    fake_codex.write_text(
        f"""
import json, sys
from pathlib import Path

p = Path({str(counter_file)!r})
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
_ = sys.stdin.read()

if n == 1:
    sys.stderr.write("HTTP 503 Service Unavailable\\n")
    sys.exit(1)

print(json.dumps({{"type": "thread.started", "thread_id": "thr-ok"}}))
print(json.dumps({{"type": "item.completed", "item": {{"id": "i0", "type": "agent_message", "text": f"CALL_{{n}}"}}}}))
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 5, "output_tokens": 2}}}}))
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path, default_timeout_seconds=123)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, fake_codex))

    # Default timeout from Settings is honored when omitted!
    req = manager.prepare_request(backend="codex", prompt="check", mode="read_only")
    assert req.timeout_seconds == 123

    # Read-only retries transient 503 once and succeeds on call 2!
    rec = await manager.run(req)
    assert rec.status == "completed"
    assert rec.response == "CALL_2"

    # Reset counter: edit mode MUST NOT auto-retry transient 503!
    counter_file.write_text("0")
    edit_rec = await manager.run(
        manager.prepare_request(backend="codex", prompt="modify", mode="edit")
    )
    assert edit_rec.status == "failed"
    assert edit_rec.failure_reason == "transient_service"
    assert int(counter_file.read_text()) == 1
    await manager.shutdown()


async def test_list_backends_reports_unhealthy_for_invalid_binary(tmp_path: Path) -> None:
    """Finding 5: list_backends distinguishes command_found vs healthy and rejects invalid binaries."""
    settings = _make_settings(
        tmp_path,
        agy_bin=str(tmp_path / "does-not-exist-agy.exe"),
        codex_bin=str(tmp_path / "does-not-exist-codex.exe"),
    )
    _mcp, manager = create_server(settings=settings)
    nh = await manager.neon_runner.check_health()
    ch = await manager.codex_runner.check_health()
    assert nh["command_found"] is False
    assert nh["healthy"] is False
    assert nh["available"] is False
    assert ch["command_found"] is False
    assert ch["healthy"] is False
    assert ch["available"] is False
    await manager.shutdown()


async def test_codex_large_jsonl_and_large_stdin(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex_big.py"
    fake_codex.write_text(
        """
import json, sys

prompt = sys.stdin.read()
big_payload = "X" * 150_000
print(json.dumps({"type": "thread.started", "thread_id": "thr-big"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": f"LEN:{len(prompt)}:{big_payload}"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    runner = StubCodexRunner(settings, fake_codex)
    big_prompt = "P" * 130_000
    res = await runner.run(RunRequest(backend="codex", prompt=big_prompt, cwd=str(tmp_path)))
    assert res.status == "completed"
    assert res.response.startswith("LEN:130000:")
    assert len(res.response) > 150_000


async def test_neon_self_healing_model_retry_and_codex_model_fallback(tmp_path: Path) -> None:
    fake_agy = tmp_path / "fake_agy_heal.py"
    fake_agy.write_text(
        """
import json, sys

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-5-neon-high\\tGemini 5 Neon (High)")
    sys.exit(0)

model = args[args.index("--model") + 1] if "--model" in args else None
if model == "gemini-4-neon-high":
    sys.stderr.write("Error: deprecated model gemini-4-neon-high\\n")
    sys.exit(1)

print(json.dumps({
    "conversation_id": "conv-heal",
    "status": "SUCCESS",
    "response": f"OK via {model}",
    "num_turns": 1,
    "usage": {"input_tokens": 50, "output_tokens": 10, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 60}
}))
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "agy_models_cache.json").write_text(
        json.dumps(
            {
                "fetched_at": time.time(),
                "models": [{"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)"}],
            }
        ),
        encoding="utf-8",
    )
    nrunner = StubAgyRunner(settings, fake_agy)
    nres = await nrunner.run(RunRequest(backend="neon", prompt="hi", cwd=str(tmp_path), model="neon-high"))
    assert nres.status == "completed"
    assert nres.model == "gemini-5-neon-high"
    assert nres.model_notice is not None

    fake_codex = tmp_path / "fake_codex_fb.py"
    fake_codex.write_text(
        """
import json, sys

args = sys.argv[1:]
_ = sys.stdin.read()
model = args[args.index("-m") + 1] if "-m" in args else None
if model == "retired-gpt":
    print(json.dumps({"type": "error", "message": "model_not_found: retired-gpt"}))
    sys.exit(1)
print(json.dumps({"type": "thread.started", "thread_id": "thr-fb"}))
print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": f"MODEL:{model or 'default'}"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 10}}))
""".strip(),
        encoding="utf-8",
    )
    crunner = StubCodexRunner(settings, fake_codex)
    cres = await crunner.run(RunRequest(backend="codex", prompt="hi", cwd=str(tmp_path), model="retired-gpt"))
    assert cres.status == "completed"
    assert cres.model_notice is not None
    assert "MODEL:default" in cres.response


async def test_cross_window_simultaneous_resumes_do_not_deadlock_and_same_window_turn1_no_overlap(
    tmp_path: Path,
) -> None:
    """Re-audit High #1: simultaneous cross-window resumes serialize without deadlocking, and same-window
    resume submitted right after Turn 1 publishes session_id waits until Turn 1 finishes."""
    overlap_log = tmp_path / "session_events.log"
    fake_codex = tmp_path / "fake_codex_claims.py"
    fake_codex.write_text(
        f"""
import json, sys, time
from pathlib import Path

log_path = Path({str(overlap_log)!r})
prompt = sys.stdin.read().strip()
tid = "thr-shared-xwin" if "XWIN" in prompt else "thr-samewin-1"
print(json.dumps({{"type": "thread.started", "thread_id": tid}}), flush=True)
with log_path.open("a", encoding="utf-8") as fh:
    fh.write(f"START:{{prompt}}\\n")
time.sleep(0.5 if "TURN1" in prompt else 0.35)
with log_path.open("a", encoding="utf-8") as fh:
    fh.write(f"END:{{prompt}}\\n")
print(json.dumps({{"type": "item.completed", "item": {{"id": "i0", "type": "agent_message", "text": f"DONE:{{prompt}}"}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 5}}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    alive_pids = {2001, 2002}
    is_alive = lambda pid: pid in alive_pids  # noqa: E731

    mgr1 = JobManager(
        settings,
        codex_runner=StubCodexRunner(settings, fake_codex),
        owner_id="xwin-1",
        pid=2001,
        is_pid_alive_fn=is_alive,
    )
    mgr2 = JobManager(
        settings,
        codex_runner=StubCodexRunner(settings, fake_codex),
        owner_id="xwin-2",
        pid=2002,
        is_pid_alive_fn=is_alive,
    )

    # Part A: Two windows simultaneously submit resumes for the SAME session_id -> NO deadlock!
    req_w1 = mgr1.prepare_request(backend="codex", prompt="XWIN_A", resume_from="thr-shared-xwin", timeout_seconds=15)
    req_w2 = mgr2.prepare_request(backend="codex", prompt="XWIN_B", resume_from="thr-shared-xwin", timeout_seconds=15)
    res_w1, res_w2 = await asyncio.gather(mgr1.run(req_w1), mgr2.run(req_w2))
    assert res_w1.status == "completed", res_w1.error
    assert res_w2.status == "completed", res_w2.error

    xwin_events = overlap_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(xwin_events) == 4
    first_x = xwin_events[0].split(":")[1]
    assert xwin_events[1] == f"END:{first_x}"

    # Part B: Same-window Turn 1 starts without session_id, emits session_id mid-run; Turn 2 queued immediately!
    overlap_log.write_text("", encoding="utf-8")
    job1 = mgr1.start(mgr1.prepare_request(backend="codex", prompt="TURN1", timeout_seconds=15))

    # Wait until Turn 1 publishes its session_id while STILL running.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        cur = mgr1.get(job1.job_id)
        if cur.session_id:
            assert cur.status == "running"
            break
        await asyncio.sleep(0.03)
    else:
        pytest.fail("Turn 1 never published session_id while running")

    job2 = mgr1.start(
        mgr1.prepare_request(backend="codex", prompt="TURN2", resume_from=job1.job_id, timeout_seconds=15)
    )
    done = await mgr1.wait_for([job1.job_id, job2.job_id], wait_seconds=15, wait_mode="all")
    assert all(d.status == "completed" for d in done)

    same_events = overlap_log.read_text(encoding="utf-8").strip().splitlines()
    assert same_events == ["START:TURN1", "END:TURN1", "START:TURN2", "END:TURN2"]

    # Completed jobs must be released from in-memory dicts!
    assert len(mgr1._local_jobs) == 0
    assert len(mgr1._events) == 0
    assert len(mgr1._tasks) == 0
    assert len(mgr1._trackers) == 0
    assert len(mgr2._local_jobs) == 0

    await mgr1.shutdown()
    await mgr2.shutdown()


async def test_parallel_codex_worktrees_get_distinct_paths_and_queued_resume_refreshes_cwd(
    tmp_path: Path,
) -> None:
    """Re-audit High #2: parallel worktree edits each get their own pre-assigned worktree, and a queued
    resume refreshes the prior turn's final effective_cwd/worktree_path before launching."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    (repo / "README.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    fake_codex = tmp_path / "fake_codex_wt.py"
    fake_codex.write_text(
        """
import json, os, sys, time
from pathlib import Path

prompt = sys.stdin.read().strip()
cwd = Path(os.getcwd()).resolve()
tag = prompt.split()[0]
tid = f"thr-{tag}" if not tag.startswith("FOLLOWUP") else "thr-EDIT_A"
print(json.dumps({"type": "thread.started", "thread_id": tid}), flush=True)
time.sleep(0.3)
(cwd / f"{tag}.marker").write_text(str(cwd), encoding="utf-8")
print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": f"CWD:{cwd}"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path, default_cwd=repo)
    mgr = JobManager(settings, codex_runner=StubCodexRunner(settings, fake_codex))

    req_a = mgr.prepare_request(backend="codex", prompt="EDIT_A first", cwd=str(repo), mode="edit", worktree=True)
    req_b = mgr.prepare_request(backend="codex", prompt="EDIT_B second", cwd=str(repo), mode="edit", worktree=True)
    res_a, res_b = await mgr.run_parallel([req_a, req_b], wait=True, wait_seconds=20)

    assert res_a.status == "completed", res_a.error
    assert res_b.status == "completed", res_b.error
    assert res_a.worktree_path is not None and res_b.worktree_path is not None
    assert res_a.worktree_path != res_b.worktree_path
    assert res_a.worktree_branch != res_b.worktree_branch
    assert res_a.effective_cwd == res_a.worktree_path
    assert res_b.effective_cwd == res_b.worktree_path
    assert (Path(res_a.worktree_path) / "EDIT_A.marker").exists()
    assert not (Path(res_a.worktree_path) / "EDIT_B.marker").exists()
    assert (Path(res_b.worktree_path) / "EDIT_B.marker").exists()
    assert not (Path(res_b.worktree_path) / "EDIT_A.marker").exists()
    assert not (repo / "EDIT_A.marker").exists()
    assert not (repo / "EDIT_B.marker").exists()

    # Now verify queued resume refreshes final effective_cwd even if Turn 1 hadn't published its
    # final effective_cwd at prepare_request() time!
    delayed_wt = tmp_path / "delayed_checkout"
    delayed_wt.mkdir()

    class DelayedCwdCodexRunner(StubCodexRunner):
        async def run(self, request, **kwargs):  # type: ignore[override]
            if request.prompt.startswith("TURN1_DELAY"):
                if kwargs.get("on_session"):
                    kwargs["on_session"]("thr-delayed-cwd")
                await asyncio.sleep(0.4)
                from agent_bridge.models import RunResult

                return RunResult(
                    status="completed",
                    response="turn1 done",
                    session_id="thr-delayed-cwd",
                    effective_cwd=str(delayed_wt.resolve()),
                    worktree_path=str(delayed_wt.resolve()),
                    worktree_branch="agent-bridge/delayed",
                    usage_status="missing",
                    provider_state="stopped",
                )
            return await super().run(request, **kwargs)

    mgr_d = JobManager(settings, codex_runner=DelayedCwdCodexRunner(settings, fake_codex))
    j1 = mgr_d.start(mgr_d.prepare_request(backend="codex", prompt="TURN1_DELAY", cwd=str(repo), mode="edit"))

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if mgr_d.get(j1.job_id).session_id == "thr-delayed-cwd":
            break
        await asyncio.sleep(0.02)

    # At this moment Turn 1's effective_cwd in SQLite is still `repo` (not yet `delayed_wt`).
    assert mgr_d.get(j1.job_id).effective_cwd == str(repo.resolve())

    j2 = mgr_d.start(
        mgr_d.prepare_request(backend="codex", prompt="FOLLOWUP_DELAY", resume_from=j1.job_id, mode="edit")
    )
    done_d = await mgr_d.wait_for([j1.job_id, j2.job_id], wait_seconds=15, wait_mode="all")
    assert all(d.status == "completed" for d in done_d)
    assert done_d[1].effective_cwd == str(delayed_wt.resolve())
    assert f"CWD:{delayed_wt.resolve()}" in done_d[1].response
    assert (delayed_wt / "FOLLOWUP_DELAY.marker").exists()

    await mgr.shutdown()
    await mgr_d.shutdown()


async def test_cancel_during_model_discovery_and_callback_error_and_agy_strict_deadline_and_dead_owner(
    tmp_path: Path,
) -> None:
    """Re-audit High #3: cancelling during agy models kills child, SQLite callback error kills Codex child,
    Antigravity strict deadline rejects late success, and dead-owner check is consistent everywhere."""
    import sqlite3

    from agent_bridge.models import JobRecord
    from agent_bridge.process_control import is_pid_alive

    # 3a: Cancel Neon during `agy models` discovery -> child PID is terminated before reporting stopped.
    pid_file_agy = tmp_path / "agy_models.pid"
    slow_models_agy = tmp_path / "slow_models_agy.py"
    slow_models_agy.write_text(
        f"""
import os, sys, time
from pathlib import Path

if len(sys.argv) > 1 and sys.argv[1] == "models":
    Path({str(pid_file_agy)!r}).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(30)
sys.exit(0)
""".strip(),
        encoding="utf-8",
    )
    settings = _make_settings(tmp_path)
    mgr_agy = JobManager(settings, neon_runner=StubAgyRunner(settings, slow_models_agy))
    j_disc = mgr_agy.start(mgr_agy.prepare_request(backend="neon", prompt="hello"))

    wait_dl = time.monotonic() + 5.0
    while time.monotonic() < wait_dl and not pid_file_agy.exists():
        await asyncio.sleep(0.05)
    assert pid_file_agy.exists(), "agy models subprocess never started"
    models_pid = int(pid_file_agy.read_text(encoding="utf-8").strip())
    assert is_pid_alive(models_pid)

    cancelled_rec = await mgr_agy.cancel(j_disc.job_id)
    assert cancelled_rec.status == "cancelled"
    assert cancelled_rec.provider_state == "stopped"
    assert not is_pid_alive(models_pid), f"agy models child pid {models_pid} leaked after cancel!"
    await mgr_agy.shutdown()

    # 3b: Injected SQLite error inside Codex progress callback still kills Codex child process!
    pid_file_codex = tmp_path / "codex_cb.pid"
    slow_codex = tmp_path / "slow_codex_cb.py"
    slow_codex.write_text(
        f"""
import json, os, sys, time
from pathlib import Path

_ = sys.stdin.read()
Path({str(pid_file_codex)!r}).write_text(str(os.getpid()), encoding="utf-8")
print(json.dumps({{"type": "thread.started", "thread_id": "thr-err-cb"}}), flush=True)
time.sleep(30)
""".strip(),
        encoding="utf-8",
    )
    mgr_cb = JobManager(settings, codex_runner=StubCodexRunner(settings, slow_codex))
    orig_update = mgr_cb.store.update_job_progress

    def flaky_update(job_id: str, **kwargs: object) -> None:
        if kwargs.get("session_id") == "thr-err-cb":
            raise sqlite3.OperationalError("injected sqlite failure")
        orig_update(job_id, **kwargs)  # type: ignore[arg-type]

    mgr_cb.store.update_job_progress = flaky_update  # type: ignore[method-assign]
    cb_rec = await mgr_cb.run(mgr_cb.prepare_request(backend="codex", prompt="trigger cb error"))
    assert cb_rec.status == "failed"
    assert cb_rec.provider_state == "stopped"
    assert "injected sqlite failure" in (cb_rec.error or "")
    assert pid_file_codex.exists()
    codex_pid = int(pid_file_codex.read_text(encoding="utf-8").strip())
    assert not is_pid_alive(codex_pid), f"codex child pid {codex_pid} leaked after callback exception!"
    await mgr_cb.shutdown()

    # 3c: Antigravity strict deadline rejects a result arriving past deadline (no +5s cushion!).
    late_agy = tmp_path / "late_agy.py"
    late_agy.write_text(
        """
import json, sys, time

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-high\\tGemini 4 Neon (High)")
    sys.exit(0)
time.sleep(2.4)
print(json.dumps({
    "event": "result",
    "result": {"conversation_id": "conv-late", "status": "SUCCESS", "response": "TOO LATE", "num_turns": 1}
}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    late_runner = StubAgyRunner(settings, late_agy)
    # Pre-warm model catalog so the 1.0s deadline applies directly to execution.
    await late_runner.list_models(refresh=True)
    t0 = time.monotonic()
    late_res = await late_runner.run(
        RunRequest(backend="neon", prompt="be late", cwd=str(tmp_path), timeout_seconds=10),
        deadline=t0 + 1.0,
    )
    assert late_res.status == "timed_out"
    assert late_res.failure_reason == "timeout"
    assert late_res.provider_state == "stopped"
    assert (time.monotonic() - t0) < 2.2

    # 3d: Unified dead-owner check across is_owner_alive, cancel(), and wait_for(wait_seconds=0).
    mgr_dead = JobManager(
        settings,
        owner_id="live-win",
        pid=3001,
        is_pid_alive_fn=lambda pid: pid == 3001,
    )
    mgr_dead.store.register_owner("fresh-dead-win", 9998)
    for jid in ("codex-dead-a", "codex-dead-b"):
        mgr_dead.store.insert_job(
            JobRecord(
                job_id=jid,
                owner_id="fresh-dead-win",
                backend="codex",
                cwd=str(tmp_path),
                mode="read_only",
                requested_model=None,
                model="codex-default",
                prompt_preview="dead",
                status="running",
                started_at=time.time(),
            )
        )
    # Even with a brand-new heartbeat, dead PID is immediately dead for status AND cancel!
    assert mgr_dead.store.is_owner_alive("fresh-dead-win", is_pid_alive_fn=lambda pid: pid == 3001) is False
    waited_zero = await mgr_dead.wait_for(["codex-dead-a"], wait_seconds=0)
    assert waited_zero[0].status == "failed"
    assert waited_zero[0].failure_reason == "owner_exited"

    # Re-insert another fresh dead-owner job to test cancel() directly without prior recovery.
    mgr_dead.store.register_owner("fresh-dead-win-2", 9997)
    mgr_dead.store.insert_job(
        JobRecord(
            job_id="codex-dead-c",
            owner_id="fresh-dead-win-2",
            backend="codex",
            cwd=str(tmp_path),
            mode="read_only",
            requested_model=None,
            model="codex-default",
            prompt_preview="dead2",
            status="running",
            started_at=time.time(),
        )
    )
    cancelled_dead = await mgr_dead.cancel("codex-dead-c")
    assert cancelled_dead.status == "failed"
    assert cancelled_dead.failure_reason == "owner_exited"
    await mgr_dead.shutdown()


async def test_agy_missing_envelope_no_overcount_and_interrupted_step_marked_partial(
    tmp_path: Path,
) -> None:
    """Re-audit Medium #4: missing envelope invalidates watermark so Turn 3 never overcounts (<=210, not 280),
    valid adjacent turns still compute exact delta, and a timed-out turn with 1 step is labeled partial."""
    fake_agy = tmp_path / "fake_agy_turns.py"
    fake_agy.write_text(
        """
import json, sys, time

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-medium\\tGemini 4 Neon (Medium)")
    sys.exit(0)

prompt = [a for a in args if a.startswith("--print=")][0][len("--print="):]

if prompt == "GAP_T1":
    cid = "conv-gap"
    print(json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "gemini-4-neon-medium"}}), flush=True)
    print(json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "T1", "usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}}}), flush=True)
    print(json.dumps({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": "T1", "num_turns": 1, "usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}}}), flush=True)
elif prompt == "GAP_T2":
    # Step emits 70 tokens, but final result envelope is MISSING!
    cid = "conv-gap"
    print(json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "gemini-4-neon-medium"}}), flush=True)
    print(json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 2, "state": "DONE", "step_type": "agent_response", "text_delta": "T2", "usage": {"input_tokens": 60, "output_tokens": 10, "total_tokens": 70}}}), flush=True)
elif prompt == "GAP_T3":
    # No step_update, only cumulative envelope (210 total across all 3 turns, num_turns omitted!).
    cid = "conv-gap"
    print(json.dumps({"conversation_id": cid, "status": "SUCCESS", "response": "T3", "usage": {"input_tokens": 180, "output_tokens": 30, "total_tokens": 210}}), flush=True)
elif prompt == "ADJ_T1":
    cid = "conv-adj"
    print(json.dumps({"conversation_id": cid, "status": "SUCCESS", "response": "A1", "num_turns": 1, "usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}}), flush=True)
elif prompt == "ADJ_T2":
    cid = "conv-adj"
    print(json.dumps({"conversation_id": cid, "status": "SUCCESS", "response": "A2", "num_turns": 2, "usage": {"input_tokens": 150, "output_tokens": 20, "total_tokens": 170}}), flush=True)
elif prompt == "ADJ_T3":
    cid = "conv-adj"
    print(json.dumps({"conversation_id": cid, "status": "SUCCESS", "response": "A3", "num_turns": 3, "usage": {"input_tokens": 180, "output_tokens": 30, "total_tokens": 210}}), flush=True)
elif prompt == "PARTIAL_TIMEOUT":
    cid = "conv-part"
    print(json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "gemini-4-neon-medium"}}), flush=True)
    print(json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "step1", "usage": {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60}}}), flush=True)
    time.sleep(5.0)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    mgr = JobManager(settings, neon_runner=StubAgyRunner(settings, fake_agy))

    # Case 1: 3-turn gap fixture (T1=100, T2=70 w/o envelope, T3=cumulative 210 w/o step) -> MUST NOT sum to 280!
    g1 = await mgr.run(mgr.prepare_request(backend="neon", model="neon-medium", prompt="GAP_T1", parent_run="gap"))
    g2 = await mgr.run(mgr.prepare_request(backend="neon", model="neon-medium", prompt="GAP_T2", resume_from=g1.job_id, parent_run="gap"))
    g3 = await mgr.run(mgr.prepare_request(backend="neon", model="neon-medium", prompt="GAP_T3", resume_from=g2.job_id, parent_run="gap"))
    assert g1.usage_status == "exact" and g1.usage and g1.usage["total_tokens"] == 100
    # T2's stream ends with no result envelope, so it was cut short: a failed job, its step count kept as partial.
    assert g2.status == "failed" and g2.failure_reason == "cli_error"
    assert g2.usage_status == "partial" and g2.usage and g2.usage["total_tokens"] == 70
    assert g3.usage_status == "partial"
    assert g3.usage is None  # Cumulative-only blob kept in provider_metrics, NOT counted as per-turn delta!
    assert g3.provider_metrics is not None and g3.provider_metrics.get("cumulative_only") is True

    gap_summary = await mgr.get_usage_summary(period="session", parent_run="gap", compact=True)
    # Never 280. T2's partial count is listed apart, not added in.
    assert gap_summary["by_backend"]["neon"]["tokens"]["total_tokens"] == 100
    assert gap_summary["by_backend"]["neon"]["usage_coverage"] == {
        "exact": 1,
        "delta": 0,
        "partial": 2,
        "missing": 0,
    }

    # Case 2: Adjacent 3-turn sequence with explicit num_turns 1 -> 2 -> 3 computes exact deltas totaling 210!
    a1 = await mgr.run(mgr.prepare_request(backend="neon", model="neon-medium", prompt="ADJ_T1", parent_run="adj"))
    a2 = await mgr.run(mgr.prepare_request(backend="neon", model="neon-medium", prompt="ADJ_T2", resume_from=a1.job_id, parent_run="adj"))
    a3 = await mgr.run(mgr.prepare_request(backend="neon", model="neon-medium", prompt="ADJ_T3", resume_from=a2.job_id, parent_run="adj"))
    assert a1.usage_status == "exact" and a1.usage and a1.usage["total_tokens"] == 100
    assert a2.usage_status == "delta" and a2.usage and a2.usage["total_tokens"] == 70
    assert a3.usage_status == "delta" and a3.usage and a3.usage["total_tokens"] == 40
    adj_summary = await mgr.get_usage_summary(period="session", parent_run="adj", compact=True)
    assert adj_summary["by_backend"]["neon"]["tokens"]["total_tokens"] == 210
    assert adj_summary["by_backend"]["neon"]["usage_coverage"]["delta"] == 2

    # Case 3: Timed-out turn with one completed step MUST be labeled `partial`, NOT `exact`!
    res_part = await mgr.neon_runner.run(
        RunRequest(backend="neon", model="neon-medium", prompt="PARTIAL_TIMEOUT", cwd=str(tmp_path), timeout_seconds=10),
        deadline=time.monotonic() + 1.0,
    )
    assert res_part.status == "timed_out"
    assert res_part.usage_status == "partial"
    assert res_part.usage is not None and res_part.usage["total_tokens"] == 60

    await mgr.shutdown()


async def test_codex_sol_62_high_norm_and_agy_claude_gpt_shortcuts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex defaults to gpt-6.2-sol @ high (riding newer local config versions automatically),
    and `agy-claude`/`agy-opus`/`agy-gpt` route cleanly to the separate `agy_claude_gpt` pool."""
    # Verify Settings.from_env() defaults to gpt-6.2-sol + high when env vars are unset.
    monkeypatch.delenv("AGENT_BRIDGE_CODEX_MODEL", raising=False)
    monkeypatch.delenv("AGENT_BRIDGE_CODEX_EFFORT", raising=False)
    env_cfg = Settings.from_env()
    assert env_cfg.default_codex_model == "gpt-6.2-sol"
    assert env_cfg.default_codex_effort == "high"

    # Verify opt-out (`auto` / `inherit`) still works.
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_MODEL", "auto")
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_EFFORT", "inherit")
    opt_out = Settings.from_env()
    assert opt_out.default_codex_model is None
    assert opt_out.default_codex_effort is None

    # Verify CodexRunner applies gpt-6.2-sol @ high by default, and rides newer local config gpt-6.3-sol!
    settings = _make_settings(
        tmp_path,
        default_codex_model="gpt-6.2-sol",
        default_codex_effort="high",
    )
    fake_codex = tmp_path / "fake_codex_norm.py"
    fake_codex.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
    crunner = StubCodexRunner(settings, fake_codex)
    assert crunner._effective_model(None, local_defaults={"model": "gpt-6.2-sol"}) == "gpt-6.2-sol"
    assert crunner._effective_model(None, local_defaults={"model": None}) == "gpt-6.2-sol"
    assert crunner._effective_model(None, local_defaults={"model": "gpt-6.3-sol"}) is None
    assert crunner._effective_effort(None) == "high"
    assert crunner._effective_effort("low") == "low"

    # Verify agy-claude / agy-opus / agy-gpt shortcuts resolve and track `agy_claude_gpt` pool!
    fake_agy = tmp_path / "fake_agy_3p.py"
    fake_agy.write_text(
        """
import json, sys

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-high\\tGemini 4 Neon (High)")
    print("claude-sonnet-5-5-high\\tClaude Sonnet 5.5 (High)")
    print("claude-opus-5-5-high\\tClaude Opus 5.5 (High)")
    print("gpt-oss-120b-medium\\tGPT-OSS 120B (Medium)")
    sys.exit(0)

model = args[args.index("--model") + 1] if "--model" in args else "unknown"
cid = f"conv-{model}"
print(json.dumps({"event": "init", "conversation_id": cid, "init": {"model": model}}), flush=True)
print(json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": f"USED:{model}", "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}}), flush=True)
print(json.dumps({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": f"USED:{model}", "num_turns": 1, "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    mgr = JobManager(settings, neon_runner=StubAgyRunner(settings, fake_agy))
    r_claude = await mgr.run(mgr.prepare_request(backend="agy-claude", prompt="hi"))
    r_opus = await mgr.run(mgr.prepare_request(backend="agy-opus", prompt="hi"))
    r_gpt = await mgr.run(mgr.prepare_request(backend="agy-gpt", prompt="hi"))
    r_gem = await mgr.run(mgr.prepare_request(backend="neon", prompt="hi"))

    assert r_claude.model == "claude-sonnet-5-5-high" and r_claude.quota_pool == "agy_claude_gpt"
    assert r_opus.model == "claude-opus-5-5-high" and r_opus.quota_pool == "agy_claude_gpt"
    assert r_gpt.model == "gpt-oss-120b-medium" and r_gpt.quota_pool == "agy_claude_gpt"
    assert r_gem.model == "gemini-4-neon-high" and r_gem.quota_pool == "agy_gemini"

    usage = await mgr.get_usage_summary(period="session", compact=True)
    assert usage["by_quota_pool"]["agy_claude_gpt"]["runs"] == 3
    assert usage["by_quota_pool"]["agy_gemini"]["runs"] == 1
    await mgr.shutdown()


async def test_prelaunch_cancel_preserves_session_context_and_agy_checkpoint(tmp_path: Path) -> None:
    """Audit High #1: cancelling a queued resume before launch MUST NOT overwrite the session's
    worktree context with the parent checkout and MUST NOT erase a valid Antigravity watermark."""
    parent_dir = tmp_path / "parent"
    worktree_dir = tmp_path / "wt_checkout"
    parent_dir.mkdir()
    worktree_dir.mkdir()

    # Part A: Codex worktree session context preserved when queued Turn 2 is cancelled before launch.
    fake_codex = tmp_path / "fake_codex_ctx.py"
    fake_codex.write_text(
        """
import json, os, sys
from pathlib import Path

_ = sys.stdin.read()
cwd = str(Path(os.getcwd()).resolve())
print(json.dumps({"type": "thread.started", "thread_id": "thr-wt-keep"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": f"RAN_IN:{cwd}"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    class HoldTurn1CodexRunner(StubCodexRunner):
        def __init__(self, settings: Settings, script_path: Path) -> None:
            super().__init__(settings, script_path)
            self.turn1_started = asyncio.Event()
            self.release_turn1 = asyncio.Event()

        async def run(self, request, **kwargs):  # type: ignore[override]
            if request.prompt == "TURN1":
                if kwargs.get("on_provider_start"):
                    res_cb = kwargs["on_provider_start"]()
                    if asyncio.iscoroutine(res_cb):
                        await res_cb
                if kwargs.get("on_session"):
                    kwargs["on_session"]("thr-wt-keep")
                self.turn1_started.set()
                await self.release_turn1.wait()
                from agent_bridge.models import RunResult

                return RunResult(
                    status="completed",
                    response="t1 done",
                    session_id="thr-wt-keep",
                    effective_cwd=str(worktree_dir.resolve()),
                    worktree_path=str(worktree_dir.resolve()),
                    worktree_branch="agent-bridge/t1",
                    usage_status="missing",
                    provider_state="stopped",
                    provider_invoked=True,
                )
            return await super().run(request, **kwargs)

    settings = _make_settings(tmp_path, default_cwd=parent_dir)
    hold_runner = HoldTurn1CodexRunner(settings, fake_codex)
    mgr = JobManager(settings, codex_runner=hold_runner)

    t1 = mgr.start(mgr.prepare_request(backend="codex", prompt="TURN1", cwd=str(parent_dir), mode="edit"))
    await asyncio.wait_for(hold_runner.turn1_started.wait(), timeout=5.0)
    # Wait for Turn 1's session claim to commit to SQLite before resuming it.
    claim_dl = time.monotonic() + 3.0
    while time.monotonic() < claim_dl and mgr.get(t1.job_id).session_id != "thr-wt-keep":
        await asyncio.sleep(0.01)
    assert mgr.get(t1.job_id).session_id == "thr-wt-keep"

    # Queue Turn 2 while Turn 1 holds the session (Turn 2 starts with parent_dir and no worktree_path yet).
    t2 = mgr.start(mgr.prepare_request(backend="codex", prompt="TURN2_CANCEL", resume_from=t1.job_id, mode="edit"))
    await asyncio.sleep(0.15)
    # Cancel queued Turn 2 BEFORE launch!
    t2_done = await mgr.cancel(t2.job_id)
    assert t2_done.status == "cancelled"
    assert t2_done.provider_invoked is False

    # Now let Turn 1 finish publishing worktree_dir!
    hold_runner.release_turn1.set()
    t1_done = (await mgr.wait_for([t1.job_id], wait_seconds=10))[0]
    assert t1_done.status == "completed"
    assert t1_done.effective_cwd == str(worktree_dir.resolve())

    # Also cancel a SECOND queued Turn 2b AFTER Turn 1 finished (before it executes) to be doubly sure!
    mgr._semaphore = asyncio.Semaphore(0)  # Block concurrency slot so Turn 2b stays pre-launch.
    t2b = mgr.start(mgr.prepare_request(backend="codex", prompt="TURN2B_CANCEL", cwd=str(parent_dir), resume_from=t1.job_id, mode="edit"))
    await asyncio.sleep(0.15)
    t2b_done = await mgr.cancel(t2b.job_id)
    assert t2b_done.status == "cancelled"
    assert t2b_done.provider_invoked is False
    mgr._semaphore = asyncio.Semaphore(4)

    # Turn 3 with implicit cwd MUST run in worktree_dir and keep worktree metadata!
    t3_done = await mgr.run(mgr.prepare_request(backend="codex", prompt="TURN3", resume_from=t1.job_id, mode="edit"))
    assert t3_done.status == "completed"
    assert t3_done.effective_cwd == str(worktree_dir.resolve())
    assert t3_done.worktree_path == str(worktree_dir.resolve())
    assert f"RAN_IN:{worktree_dir.resolve()}" in t3_done.response

    # Part B: Antigravity usage checkpoint preserved when queued resume is cancelled before launch!
    fake_agy = tmp_path / "fake_agy_prelaunch.py"
    fake_agy.write_text(
        """
import json, sys

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-medium\\tGemini 4 Neon (Medium)")
    sys.exit(0)

prompt = [a for a in args if a.startswith("--print=")][0][len("--print="):]
if prompt == "N1":
    print(json.dumps({"conversation_id": "conv-keep-wm", "status": "SUCCESS", "response": "N1", "num_turns": 1, "usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}}), flush=True)
elif prompt == "N3":
    print(json.dumps({"conversation_id": "conv-keep-wm", "status": "SUCCESS", "response": "N3", "num_turns": 2, "usage": {"input_tokens": 140, "output_tokens": 20, "total_tokens": 160}}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    mgr_n = JobManager(settings, neon_runner=StubAgyRunner(settings, fake_agy))
    n1 = await mgr_n.run(mgr_n.prepare_request(backend="neon", model="neon-medium", prompt="N1"))
    assert n1.status == "completed"
    assert mgr_n.store.get_session_watermark("neon", "conv-keep-wm") is not None

    # Block semaphore so queued N2 cannot launch, then cancel N2 before launch.
    mgr_n._semaphore = asyncio.Semaphore(0)
    n2 = mgr_n.start(mgr_n.prepare_request(backend="neon", model="neon-medium", prompt="N2_CANCEL", resume_from=n1.job_id))
    await asyncio.sleep(0.15)
    n2_done = await mgr_n.cancel(n2.job_id)
    assert n2_done.status == "cancelled"
    assert n2_done.provider_invoked is False
    mgr_n._semaphore = asyncio.Semaphore(4)

    # Watermark MUST still exist so N3 computes exact adjacent delta (160 - 100 = 60)!
    assert mgr_n.store.get_session_watermark("neon", "conv-keep-wm") is not None
    n3 = await mgr_n.run(mgr_n.prepare_request(backend="neon", model="neon-medium", prompt="N3", resume_from=n1.job_id))
    assert n3.status == "completed"
    assert n3.usage_status == "delta"
    assert n3.usage is not None and n3.usage["total_tokens"] == 60

    await mgr.shutdown()
    await mgr_n.shutdown()


async def test_worktree_lock_respects_deadline_and_cancel_mid_checkout_reports_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit Medium #3: worktree lock wait honors job deadline, and cancelling during `git worktree add`
    still reports the planned worktree_path/branch on the cancelled JobRecord."""
    import os
    import subprocess

    from agent_bridge import runners as runners_mod

    repo = tmp_path / "wt_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    # Part A: Hold repo worktree lock for 3.5s -> a job with 0.6s deadline times out quickly!
    lock = runners_mod._repo_worktree_lock(os.path.normcase(str(repo.resolve())))
    await lock.acquire()
    try:
        t0 = time.monotonic()
        with pytest.raises(asyncio.TimeoutError, match="worktree lock"):
            await runners_mod.prepare_codex_worktree(
                job_id="codex-lock-test",
                cwd=str(repo),
                state_dir=tmp_path / "state",
                deadline=t0 + 0.6,
            )
        assert (time.monotonic() - t0) < 1.8
    finally:
        lock.release()

    # Part B: Cancel mid-`git worktree add` -> cancelled JobRecord reports planned worktree_path & branch!
    orig_run_git = runners_mod._run_git
    in_checkout = asyncio.Event()

    async def slow_worktree_add(args: list[str], *, cwd: str, timeout_seconds: float = 15.0):
        if args and args[0] == "worktree" and len(args) >= 5 and args[1] == "add":
            # Create the worktree directory before stalling so it simulates a partial/registered checkout.
            Path(args[4]).mkdir(parents=True, exist_ok=True)
            in_checkout.set()
            await asyncio.sleep(10.0)
        return await orig_run_git(args, cwd=cwd, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(runners_mod, "_run_git", slow_worktree_add)
    settings = _make_settings(tmp_path, default_cwd=repo)
    mgr = JobManager(settings)
    job = mgr.start(mgr.prepare_request(backend="codex", prompt="edit", cwd=str(repo), mode="edit", worktree=True))
    await asyncio.wait_for(in_checkout.wait(), timeout=5.0)
    cancelled = await mgr.cancel(job.job_id)
    assert cancelled.status == "cancelled"
    assert cancelled.worktree_path is not None
    assert Path(cancelled.worktree_path).exists()
    assert cancelled.worktree_branch == f"agent-bridge/{job.job_id}"
    assert cancelled.provider_invoked is False
    await mgr.shutdown()


async def test_agy_empty_usage_envelope_does_not_advance_checkpoint_and_quota_cache_refreshes_past_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit Medium #4 & #5: empty `{}` agy usage envelope invalidates checkpoint (never 380 vs 210),
    and cached quota whose reset timestamp has already passed triggers automatic live refresh."""
    fake_agy = tmp_path / "fake_agy_empty_usage.py"
    fake_agy.write_text(
        """
import json, sys

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-medium\\tGemini 4 Neon (Medium)")
    sys.exit(0)

prompt = [a for a in args if a.startswith("--print=")][0][len("--print="):]
cid = "conv-empty-u"
if prompt == "E1":
    print(json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "gemini-4-neon-medium"}}), flush=True)
    print(json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "E1", "usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}}}), flush=True)
    print(json.dumps({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": "E1", "num_turns": 1, "usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}}}), flush=True)
elif prompt == "E2":
    # Step reports 70 tokens, but final envelope has an EMPTY usage `{}` with num_turns=2!
    print(json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "gemini-4-neon-medium"}}), flush=True)
    print(json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 2, "state": "DONE", "step_type": "agent_response", "text_delta": "E2", "usage": {"input_tokens": 60, "output_tokens": 10, "total_tokens": 70}}}), flush=True)
    print(json.dumps({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": "E2", "num_turns": 2, "usage": {}}}), flush=True)
elif prompt == "E3":
    # Envelope-only Turn 3 reports cumulative 210 with num_turns=3 -> MUST NOT subtract empty {} to get 210!
    print(json.dumps({"conversation_id": cid, "status": "SUCCESS", "response": "E3", "num_turns": 3, "usage": {"input_tokens": 180, "output_tokens": 30, "total_tokens": 210}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    mgr = JobManager(settings, neon_runner=StubAgyRunner(settings, fake_agy))
    e1 = await mgr.run(mgr.prepare_request(backend="neon", model="neon-medium", prompt="E1", parent_run="emp"))
    e2 = await mgr.run(mgr.prepare_request(backend="neon", model="neon-medium", prompt="E2", resume_from=e1.job_id, parent_run="emp"))
    assert mgr.store.get_session_watermark("neon", "conv-empty-u") is None  # Empty `{}` invalidated checkpoint!
    e3 = await mgr.run(mgr.prepare_request(backend="neon", model="neon-medium", prompt="E3", resume_from=e2.job_id, parent_run="emp"))
    assert e3.usage_status == "partial"
    assert e3.usage is None

    summary = await mgr.get_usage_summary(period="session", parent_run="emp", compact=True)
    assert summary["by_backend"]["neon"]["tokens"]["total_tokens"] == 170  # Never 380!

    # Cached quota whose reset timestamp already passed MUST bypass cache and fetch fresh!
    from datetime import datetime, timedelta, timezone
    from agent_bridge import jobs as jobs_mod

    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    future_iso = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(timespec="seconds")
    mgr.store.set_cached_quota(
        "codex",
        {
            "backend": "codex",
            "pool": "codex",
            "status": "ok",
            "fetched_at": past_iso,
            "windows": [
                {"slot": "secondary", "window": "weekly", "used_percent": 50.0, "remaining_percent": 50.0, "reset_time": past_iso}
            ],
        },
    )

    fetch_calls = 0

    async def fake_fetch_codex(_cmd: list[str]) -> dict[str, object]:
        nonlocal fetch_calls
        fetch_calls += 1
        return {
            "backend": "codex",
            "pool": "codex",
            "status": "ok",
            "fetched_at": future_iso,
            "windows": [
                {"slot": "secondary", "window": "weekly", "used_percent": 0.0, "remaining_percent": 100.0, "reset_time": future_iso}
            ],
        }

    monkeypatch.setattr(jobs_mod, "fetch_codex_quota", fake_fetch_codex)
    monkeypatch.setattr(mgr.codex_runner, "resolve_command", lambda: ["codex"])

    refreshed = await mgr.get_quota(["codex"], refresh=False)
    assert fetch_calls == 1
    assert refreshed["codex"]["cached"] is False
    assert refreshed["codex"]["windows"][0]["remaining_percent"] == 100.0

    # Second call with future reset_time SHOULD hit cache!
    cached_hit = await mgr.get_quota(["codex"], refresh=False)
    assert fetch_calls == 1
    assert cached_hit["codex"]["cached"] is True

    await mgr.shutdown()


async def test_posix_process_group_escalation_when_grandchild_survives_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit High #1: on POSIX, if leader exits on SIGTERM but a grandchild in pgid survives,
    terminate() escalates to SIGKILL and only reports stopped once pgid is gone."""
    import signal

    from agent_bridge import process_control as pc

    sigterm = signal.SIGTERM
    sigkill = getattr(signal, "SIGKILL", 9)

    monkeypatch.setattr(pc.sys, "platform", "linux")
    monkeypatch.setattr(pc.ManagedProcess, "_POSIX_GRACE_SECONDS", 0.08)
    monkeypatch.setattr(pc.ManagedProcess, "_POSIX_KILL_WAIT_SECONDS", 0.1)

    class FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode: int | None = None

        async def wait(self) -> int:
            while self.returncode is None:
                await asyncio.sleep(0.01)
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    # Case A: Leader dies on SIGTERM, grandchild ignores SIGTERM and dies on SIGKILL -> verified True!
    state = {"grandchild_alive": True, "signals": []}
    proc_a = FakeProc(pid=4242)

    def fake_killpg_a(pgid: int, sig: int) -> None:
        assert pgid == 4242
        if sig == 0:
            if state["grandchild_alive"] or proc_a.returncode is None:
                return
            raise ProcessLookupError()
        state["signals"].append(sig)
        if sig == sigterm:
            proc_a.returncode = -15  # Leader exits immediately, grandchild stays alive!
        elif sig == sigkill:
            state["grandchild_alive"] = False

    monkeypatch.setattr(pc.os, "killpg", fake_killpg_a, raising=False)
    mp_a = pc.ManagedProcess(proc_a)  # type: ignore[arg-type]
    stopped_a = await mp_a.terminate()
    assert stopped_a is True
    assert state["signals"] == [sigterm, sigkill]
    assert mp_a.is_alive() is False

    # Case B: Grandchild survives even SIGKILL -> terminate() MUST return False (never lie 'stopped')!
    proc_b = FakeProc(pid=5353)
    signals_b: list[int] = []

    def fake_killpg_b(pgid: int, sig: int) -> None:
        assert pgid == 5353
        if sig == 0:
            return  # Grandchild still alive!
        signals_b.append(sig)
        if sig == sigterm:
            proc_b.returncode = -15

    monkeypatch.setattr(pc.os, "killpg", fake_killpg_b, raising=False)
    mp_b = pc.ManagedProcess(proc_b)  # type: ignore[arg-type]
    stopped_b = await mp_b.terminate()
    assert stopped_b is False
    assert signals_b == [sigterm, sigkill]
    assert mp_b.is_alive() is True


async def test_uncertain_prior_turn_blocks_automatic_resume_until_confirmed(tmp_path: Path) -> None:
    """Audit High #2: when Turn 1 ends with provider_state=possibly_running, a queued follow-up in
    another window is blocked with `prior_turn_uncertain` until `confirm_prior_stopped=True`."""
    calls_log = tmp_path / "agy_calls.log"
    fake_agy = tmp_path / "fake_agy_uncertain.py"
    fake_agy.write_text(
        f"""
import json, sys, time
from pathlib import Path

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-medium\\tGemini 4 Neon (Medium)")
    sys.exit(0)

prompt = [a for a in args if a.startswith("--print=")][0][len("--print="):]
with Path({str(calls_log)!r}).open("a", encoding="utf-8") as fh:
    fh.write(prompt + "\\n")

cid = "conv-uncertain-1"
print(json.dumps({{"event": "init", "conversation_id": cid, "init": {{"model": "gemini-4-neon-medium"}}}}), flush=True)
if prompt == "T1_TIMEOUT3":
    time.sleep(0.45)
    sys.stderr.write("print timeout elapsed while turn is still in progress\\n")
    sys.exit(3)
print(json.dumps({{"event": "step_update", "step_update": {{"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": f"OK:{{prompt}}", "usage": {{"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}}}}}}), flush=True)
print(json.dumps({{"event": "result", "result": {{"conversation_id": cid, "status": "SUCCESS", "response": f"OK:{{prompt}}", "num_turns": 2, "usage": {{"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}}}}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    alive_pids = {6001, 6002}
    is_alive = lambda pid: pid in alive_pids  # noqa: E731

    mgr1 = JobManager(
        settings,
        neon_runner=StubAgyRunner(settings, fake_agy),
        owner_id="uwin-1",
        pid=6001,
        is_pid_alive_fn=is_alive,
    )
    mgr2 = JobManager(
        settings,
        neon_runner=StubAgyRunner(settings, fake_agy),
        owner_id="uwin-2",
        pid=6002,
        is_pid_alive_fn=is_alive,
    )

    j1 = mgr1.start(mgr1.prepare_request(backend="neon", model="neon-medium", prompt="T1_TIMEOUT3"))
    dl = time.monotonic() + 5.0
    while time.monotonic() < dl and not mgr1.get(j1.job_id).session_id:
        await asyncio.sleep(0.03)
    assert mgr1.get(j1.job_id).session_id == "conv-uncertain-1"

    # Window 2 queues a follow-up while Turn 1 is still running!
    j2 = mgr2.start(
        mgr2.prepare_request(backend="neon", model="neon-medium", prompt="T2_BLOCKED", resume_from=j1.job_id)
    )
    r1 = (await mgr1.wait_for([j1.job_id], wait_seconds=10))[0]
    r2 = (await mgr2.wait_for([j2.job_id], wait_seconds=10))[0]

    assert r1.status == "timed_out"
    assert r1.provider_state == "possibly_running"
    assert r2.status == "failed"
    assert r2.failure_reason == "prior_turn_uncertain"
    assert r2.provider_state == "stopped"
    assert r2.provider_invoked is False
    assert "confirm_prior_stopped=True" in (r2.retry_guidance or "")
    # Turn 2 MUST NOT have invoked agy!
    assert calls_log.read_text(encoding="utf-8").strip().splitlines() == ["T1_TIMEOUT3"]

    # Explicit confirmation unblocks the session cleanly!
    r3 = await mgr2.run(
        mgr2.prepare_request(
            backend="neon",
            model="neon-medium",
            prompt="T3_CONFIRMED",
            resume_from=j1.job_id,
            confirm_prior_stopped=True,
        )
    )
    assert r3.status == "completed"
    assert "OK:T3_CONFIRMED" in r3.response

    # Subsequent turn without confirm_prior_stopped now succeeds because T3 completed cleanly!
    r4 = await mgr2.run(
        mgr2.prepare_request(backend="neon", model="neon-medium", prompt="T4_NORMAL", resume_from=r3.job_id)
    )
    assert r4.status == "completed"

    await mgr1.shutdown()
    await mgr2.shutdown()


async def test_worktree_probes_obey_deadline_and_dead_owner_queued_recovery_keeps_checkpoint_and_pool_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit Medium #3, #4, #5: worktree metadata probes strictly share remaining deadline, dead-owner
    recovery preserves Antigravity checkpoints for unlaunched queued jobs, and NeonRunner never spawns
    agy on an unavailable quota pool."""
    from agent_bridge import runners as runners_mod
    from agent_bridge.models import JobRecord

    # #3a: Existing worktree reuse probes recalculate remaining deadline across git commands!
    wt_dir = tmp_path / "existing_wt"
    wt_dir.mkdir()
    git_calls = 0

    async def slow_git(args: list[str], *, cwd: str, timeout_seconds: float = 15.0):
        nonlocal git_calls
        git_calls += 1
        await asyncio.sleep(min(0.25, timeout_seconds))
        raise asyncio.TimeoutError("simulated slow git")

    monkeypatch.setattr(runners_mod, "_run_git", slow_git)
    t0 = time.monotonic()
    info = await runners_mod.prepare_codex_worktree(
        job_id="codex-reuse-dl",
        cwd=str(wt_dir),
        state_dir=tmp_path / "state",
        existing_worktree_path=str(wt_dir),
        existing_worktree_branch="agent-bridge/persisted",
        existing_worktree_commit="abc1234",
        deadline=t0 + 0.18,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 0.35, f"worktree probes overran 0.18s deadline: took {elapsed:.2f}s"
    assert git_calls == 1  # Stopped after first probe exhausted deadline (didn't run 3x)!
    assert info["worktree_path"] == str(wt_dir.resolve())
    assert info["worktree_branch"] == "agent-bridge/persisted"
    assert info["worktree_commit"] == "abc1234"

    # #3b: Post-run inspection in CodexRunner is skipped when deadline already expired!
    inspect_called = False

    async def fail_if_called(*args, **kwargs):
        nonlocal inspect_called
        inspect_called = True
        return None, None, None

    monkeypatch.setattr(runners_mod, "_inspect_git_checkout", fail_if_called)
    slow_codex = tmp_path / "slow_codex_post.py"
    slow_codex.write_text("import sys, time; _ = sys.stdin.read(); time.sleep(5)\n", encoding="utf-8")
    settings = _make_settings(tmp_path)
    crunner = StubCodexRunner(settings, slow_codex)
    req_wt = RunRequest(
        backend="codex",
        prompt="timeout",
        cwd=str(wt_dir),
        mode="edit",
        worktree=True,
        worktree_path=str(wt_dir),
        worktree_branch="agent-bridge/persisted",
        worktree_commit="abc1234",
        timeout_seconds=10,
    )
    res_to = await crunner.run(req_wt, deadline=time.monotonic() + 0.4)
    assert res_to.status == "timed_out"
    assert inspect_called is False
    assert res_to.worktree_path == str(wt_dir)
    assert res_to.worktree_branch == "agent-bridge/persisted"

    # #4: Dead-owner recovery preserves Antigravity watermark for a queued (provider_invoked=False) job!
    mgr = JobManager(
        settings,
        owner_id="live-rec",
        pid=7001,
        is_pid_alive_fn=lambda pid: pid == 7001,
    )
    done_t1 = JobRecord(
        job_id="neon-t1-done",
        owner_id="live-rec",
        backend="neon",
        cwd=str(tmp_path),
        mode="read_only",
        requested_model="neon-medium",
        model="gemini-4-neon-medium",
        prompt_preview="t1",
        session_id="conv-rec-wm",
        status="completed",
        started_at=time.time() - 5,
        finished_at=time.time() - 4,
        duration_seconds=1.0,
        usage={"input_tokens": 90, "output_tokens": 10, "total_tokens": 100},
        usage_status="exact",
        provider_invoked=True,
    )
    # A row starts active; finalize_job is what ends it, and an ended row is never rewritten.
    mgr.store.insert_job(replace(done_t1, status="running", finished_at=None, duration_seconds=None))
    mgr.store.finalize_job(
        done_t1,
        watermark_cumulative={"input_tokens": 90, "output_tokens": 10, "total_tokens": 100},
        watermark_turns=1,
        touched_provider=True,
    )
    assert mgr.store.get_session_watermark("neon", "conv-rec-wm") is not None

    # Dead owner left a QUEUED follow-up that never invoked the provider.
    mgr.store.register_owner("dead-queued-win", 9991)
    queued_orphan = JobRecord(
        job_id="neon-q-orphan",
        owner_id="dead-queued-win",
        backend="neon",
        cwd=str(tmp_path),
        mode="read_only",
        requested_model="neon-medium",
        model="gemini-4-neon-medium",
        prompt_preview="queued orphan",
        session_id="conv-rec-wm",
        status="queued",
        started_at=None,
        provider_invoked=False,
    )
    mgr.store.insert_job(queued_orphan)
    recovered = mgr.store.recover_dead_owner_jobs("live-rec", is_pid_alive_fn=lambda pid: pid == 7001)
    assert "neon-q-orphan" in recovered
    rec_q = mgr.get("neon-q-orphan")
    assert rec_q.status == "failed"
    assert rec_q.provider_state == "stopped"
    # Watermark MUST still be preserved!
    assert mgr.store.get_session_watermark("neon", "conv-rec-wm") is not None

    # Contrast: dead owner with provider_invoked=True DOES invalidate the watermark!
    mgr.store.register_owner("dead-run-win", 9992)
    running_orphan = JobRecord(
        job_id="neon-r-orphan",
        owner_id="dead-run-win",
        backend="neon",
        cwd=str(tmp_path),
        mode="read_only",
        requested_model="neon-medium",
        model="gemini-4-neon-medium",
        prompt_preview="running orphan",
        session_id="conv-rec-wm",
        status="running",
        started_at=time.time() - 2,
        provider_invoked=True,
    )
    mgr.store.insert_job(running_orphan)
    mgr.store.recover_dead_owner_jobs("live-rec", is_pid_alive_fn=lambda pid: pid == 7001)
    assert mgr.get("neon-r-orphan").provider_state == "unknown"
    assert mgr.store.get_session_watermark("neon", "conv-rec-wm") is None

    # #5: NeonRunner fails cleanly without spawning agy when requested Claude pool is missing!
    spawn_marker = tmp_path / "should_not_spawn.txt"
    gem_only_agy = tmp_path / "gem_only_agy.py"
    gem_only_agy.write_text(
        f"""
import sys
from pathlib import Path

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-high\\tGemini 4 Neon (High)")
    sys.exit(0)
Path({str(spawn_marker)!r}).write_text("SPENT_GEMINI", encoding="utf-8")
sys.exit(0)
""".strip(),
        encoding="utf-8",
    )
    nrunner = StubAgyRunner(settings, gem_only_agy)
    res_pool = await nrunner.run(
        RunRequest(backend="neon", model="claude-sonnet-medium", prompt="hi", cwd=str(tmp_path))
    )
    assert res_pool.status == "failed"
    assert res_pool.failure_reason == "model_unavailable"
    assert res_pool.provider_invoked is False
    assert not spawn_marker.exists(), "agy should NEVER have been spawned on wrong quota pool!"
    await mgr.shutdown()


async def test_windows_preexecution_containment_and_normal_exit_descendant_cleanup(
    tmp_path: Path,
) -> None:
    """Audit High #1: pre-execution containment kills children spawned immediately on startup,
    and normal exit (`managed.finish()`) reaps any lingering descendant before reporting stopped."""
    from agent_bridge.process_control import is_pid_alive

    # Part A: Worker spawns a grandchild as its VERY FIRST action, then hangs until cancelled.
    gc_pid_cancel = tmp_path / "gc_cancel.pid"
    early_spawn_codex = tmp_path / "early_spawn_codex.py"
    early_spawn_codex.write_text(
        f"""
import json, subprocess, sys, time
from pathlib import Path

g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path({str(gc_pid_cancel)!r}).write_text(str(g.pid), encoding="utf-8")
_ = sys.stdin.read()
print(json.dumps({{"type": "thread.started", "thread_id": "thr-early"}}), flush=True)
time.sleep(60)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    mgr = JobManager(settings, codex_runner=StubCodexRunner(settings, early_spawn_codex))
    j = mgr.start(mgr.prepare_request(backend="codex", prompt="cancel me"))

    dl = time.monotonic() + 6.0
    while time.monotonic() < dl and not gc_pid_cancel.exists():
        await asyncio.sleep(0.05)
    assert gc_pid_cancel.exists(), "grandchild never spawned"
    gc_pid = int(gc_pid_cancel.read_text(encoding="utf-8").strip())
    assert is_pid_alive(gc_pid)

    cancelled = await mgr.cancel(j.job_id)
    assert cancelled.status == "cancelled"
    assert cancelled.provider_state == "stopped"
    assert not is_pid_alive(gc_pid), f"Grandchild pid {gc_pid} escaped pre-execution containment!"

    # Part B: Worker spawns a lingering grandchild and exits cleanly with exit code 0!
    gc_pid_normal = tmp_path / "gc_normal.pid"
    linger_codex = tmp_path / "linger_normal_codex.py"
    linger_codex.write_text(
        f"""
import json, subprocess, sys
from pathlib import Path

g = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path({str(gc_pid_normal)!r}).write_text(str(g.pid), encoding="utf-8")
_ = sys.stdin.read()
print(json.dumps({{"type": "thread.started", "thread_id": "thr-linger"}}), flush=True)
print(json.dumps({{"type": "item.completed", "item": {{"id": "i0", "type": "agent_message", "text": "DONE_WITH_CHILD"}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 5}}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    mgr2 = JobManager(settings, codex_runner=StubCodexRunner(settings, linger_codex))
    rec2 = await mgr2.run(mgr2.prepare_request(backend="codex", prompt="normal exit with child"))
    assert rec2.status == "completed"
    assert rec2.provider_state == "stopped"
    assert gc_pid_normal.exists()
    gc_pid2 = int(gc_pid_normal.read_text(encoding="utf-8").strip())
    assert not is_pid_alive(gc_pid2), f"Lingering grandchild pid {gc_pid2} survived normal exit!"
    await mgr.shutdown()
    await mgr2.shutdown()


async def test_job_history_pruning_keeps_session_safety_block_and_confirmation_is_turn_bound(
    tmp_path: Path,
) -> None:
    """Audit High #2: durable session_safety_state survives jobs pruning, and confirmation is bound
    to the exact prior blocking_job_id."""
    from agent_bridge.models import JobRecord

    fake_agy = tmp_path / "fake_agy_prune_block.py"
    fake_agy.write_text(
        """
import json, sys

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-medium\\tGemini 4 Neon (Medium)")
    sys.exit(0)

prompt = [a for a in args if a.startswith("--print=")][0][len("--print="):]
cid = "conv-prune-safe"
print(json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "gemini-4-neon-medium"}}), flush=True)
if "TIMEOUT3" in prompt:
    sys.stderr.write("print timeout elapsed while turn is still in progress\\n")
    sys.exit(3)
print(json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": f"OK:{prompt}", "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}}}), flush=True)
print(json.dumps({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": f"OK:{prompt}", "num_turns": 2, "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    # Low max_stored_jobs (20) so we can easily prune Turn 1 out of the `jobs` table.
    settings = _make_settings(tmp_path, max_stored_jobs=20)
    mgr = JobManager(settings, neon_runner=StubAgyRunner(settings, fake_agy))

    t1 = await mgr.run(mgr.prepare_request(backend="neon", model="neon-medium", prompt="T1_TIMEOUT3"))
    assert t1.status == "timed_out"
    assert t1.provider_state == "possibly_running"

    # Backdate `t1` and insert & finalize 25 newer unrelated jobs so `t1` is completely pruned from `jobs`!
    old_ts = time.time() - 500.0
    with mgr.store._tx(write=True) as cur:
        cur.execute(
            "UPDATE jobs SET created_at = ?, started_at = ?, finished_at = ? WHERE job_id = ?",
            (old_ts, old_ts, old_ts + 0.1, t1.job_id),
        )
    base_ts = time.time() - 100.0
    for i in range(25):
        filler = JobRecord(
            job_id=f"codex-fill{i:03d}",
            owner_id=mgr.owner_id,
            backend="codex",
            cwd=str(tmp_path),
            mode="read_only",
            requested_model=None,
            model="gpt-6.2-sol",
            prompt_preview=f"fill {i}",
            status="completed",
            created_at=base_ts + i,
            started_at=base_ts + i,
            finished_at=base_ts + i + 0.1,
            duration_seconds=0.1,
            provider_state="stopped",
            provider_invoked=True,
        )
        mgr.store.insert_job(replace(filler, status="running", finished_at=None, duration_seconds=None))
        mgr.store.finalize_job(filler, touched_provider=True)

    assert mgr.store.get_job(t1.job_id) is None, "t1 should have been pruned from jobs table"

    # 1. Resume by session_id WITHOUT confirmation MUST STILL BE BLOCKED even after pruning!
    blocked = await mgr.run(
        mgr.prepare_request(backend="neon", model="neon-medium", prompt="AFTER_PRUNE", resume_from="conv-prune-safe")
    )
    assert blocked.status == "failed"
    assert blocked.failure_reason == "prior_turn_uncertain"
    assert t1.job_id in (blocked.error or "")

    # 2. Wrong confirm_prior_job_id MUST STILL BE BLOCKED!
    wrong_confirm = await mgr.run(
        mgr.prepare_request(
            backend="neon",
            model="neon-medium",
            prompt="WRONG_CONFIRM",
            resume_from="conv-prune-safe",
            confirm_prior_job_id="neon-wrongid",
        )
    )
    assert wrong_confirm.status == "failed"
    assert wrong_confirm.failure_reason == "prior_turn_uncertain"

    # 3. Confirmed resume for t1 ALSO times out -> new blocker becomes t2 (so confirming t1 again fails!).
    t2 = await mgr.run(
        mgr.prepare_request(
            backend="neon",
            model="neon-medium",
            prompt="T2_TIMEOUT3",
            resume_from="conv-prune-safe",
            confirm_prior_job_id=t1.job_id,
        )
    )
    assert t2.status == "timed_out"
    assert t2.provider_state == "possibly_running"

    stale_confirm = await mgr.run(
        mgr.prepare_request(
            backend="neon",
            model="neon-medium",
            prompt="STALE_CONFIRM",
            resume_from="conv-prune-safe",
            confirm_prior_job_id=t1.job_id,
        )
    )
    assert stale_confirm.status == "failed"
    assert stale_confirm.failure_reason == "prior_turn_uncertain"
    assert t2.job_id in (stale_confirm.error or "")

    # 4. Exact confirmation for t2 succeeds and clears the durable safety block!
    t3 = await mgr.run(
        mgr.prepare_request(
            backend="neon",
            model="neon-medium",
            prompt="T3_OK",
            resume_from="conv-prune-safe",
            confirm_prior_job_id=t2.job_id,
        )
    )
    assert t3.status == "completed"
    assert "OK:T3_OK" in t3.response
    assert mgr.store.get_session_safety_block("neon", "conv-prune-safe") is None
    await mgr.shutdown()


async def test_runtime_model_retry_never_crosses_quota_pool_when_agy_rejects_advertised_model(
    tmp_path: Path,
) -> None:
    """Audit Medium #3: when agy rejects an advertised Claude model at runtime and refresh finds no
    different eligible model in agy_claude_gpt, NeonRunner returns model_unavailable instead of
    dropping --model and spending Gemini quota."""
    invocations_log = tmp_path / "agy_invocations.log"
    fake_agy = tmp_path / "fake_agy_retry_pool.py"
    fake_agy.write_text(
        f"""
import json, sys
from pathlib import Path

args = sys.argv[1:]
if args and args[0] == "models":
    print("claude-sonnet-5-5-medium\\tClaude Sonnet 5.5 (Medium)")
    print("gemini-4-neon-high\\tGemini 4 Neon (High)")
    sys.exit(0)

model = args[args.index("--model") + 1] if "--model" in args else "DEFAULT_GEMINI"
with Path({str(invocations_log)!r}).open("a", encoding="utf-8") as fh:
    fh.write(model + "\\n")

if model == "claude-sonnet-5-5-medium":
    sys.stderr.write("Error: unavailable model claude-sonnet-5-5-medium\\n")
    sys.exit(1)

print(json.dumps({{
    "conversation_id": "conv-gem-fb",
    "status": "SUCCESS",
    "response": f"RAN:{{model}}",
    "num_turns": 1,
    "usage": {{"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}},
}}))
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    nrunner = StubAgyRunner(settings, fake_agy)

    # Default (allow_cross_pool_fallback=False): MUST NOT drop --model to DEFAULT_GEMINI!
    res = await nrunner.run(
        RunRequest(
            backend="neon",
            model="claude-sonnet-medium",
            prompt="hello",
            cwd=str(tmp_path),
            allow_cross_pool_fallback=False,
        )
    )
    assert res.status == "failed"
    assert res.failure_reason == "model_unavailable"
    calls = invocations_log.read_text(encoding="utf-8").strip().splitlines()
    assert calls == ["claude-sonnet-5-5-medium"], f"Unexpected cross-pool retry calls: {calls}"

    # Opt-in (allow_cross_pool_fallback=True): allowed to retry without --model.
    res_opt = await nrunner.run(
        RunRequest(
            backend="neon",
            model="claude-sonnet-medium",
            prompt="hello",
            cwd=str(tmp_path),
            allow_cross_pool_fallback=True,
        )
    )
    assert res_opt.status == "completed"
    assert "RAN:DEFAULT_GEMINI" in res_opt.response
    assert res_opt.model_notice is not None


async def test_windows_containment_failure_terminates_suspended_worker_without_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit High #1: if Windows Job Object attachment fails, NEVER resume the suspended worker;
    terminate it while still suspended and return a clear launch_error."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific Job Object containment test")

    from agent_bridge import process_control as pc

    ran_marker = tmp_path / "worker_should_not_run.txt"
    gc_marker = tmp_path / "child_should_not_spawn.pid"
    worker = tmp_path / "forbidden_worker.py"
    worker.write_text(
        f"""
import subprocess, sys
from pathlib import Path

Path({str(ran_marker)!r}).write_text("RAN", encoding="utf-8")
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path({str(gc_marker)!r}).write_text(str(g.pid), encoding="utf-8")
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(pc._kernel32, "AssignProcessToJobObject", lambda _j, _p: 0)

    # 1. Direct spawn_managed_process raises ProcessContainmentError and never runs the worker!
    with pytest.raises(pc.ProcessContainmentError, match="AssignProcessToJobObject"):
        await pc.spawn_managed_process(sys.executable, str(worker))
    await asyncio.sleep(0.15)
    assert not ran_marker.exists(), "Suspended worker resumed despite Job Object containment failure!"
    assert not gc_marker.exists(), "Child spawned despite Job Object containment failure!"

    # 2. Through JobManager -> returns structured failed/launch_error with provider_state=stopped!
    settings = _make_settings(tmp_path)
    mgr = JobManager(settings, codex_runner=StubCodexRunner(settings, worker))
    rec = await mgr.run(mgr.prepare_request(backend="codex", prompt="must fail closed"))
    assert rec.status == "failed"
    assert rec.failure_reason == "launch_error"
    assert rec.provider_state == "stopped"
    assert rec.provider_invoked is False
    assert "Job Object" in (rec.error or "")
    await asyncio.sleep(0.15)
    assert not ran_marker.exists()
    assert not gc_marker.exists()
    await mgr.shutdown()


async def test_cancellation_during_posix_spawn_pipe_setup_captures_and_cleans_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit High #2: cancelling during POSIX asyncio pipe setup waits atomically for launch +
    registration so the child's process group is obtained, killed, and verified stopped."""
    import signal

    from agent_bridge import process_control as pc

    sigterm = signal.SIGTERM
    sigkill = getattr(signal, "SIGKILL", 9)

    monkeypatch.setattr(pc.sys, "platform", "linux")
    monkeypatch.setattr(pc.ManagedProcess, "_POSIX_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(pc.ManagedProcess, "_POSIX_KILL_WAIT_SECONDS", 0.1)

    entered_pipe_setup = asyncio.Event()
    release_pipe_setup = asyncio.Event()
    state = {"grandchild_alive": True, "signals": []}

    class FakePosixProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode: int | None = None

        async def wait(self) -> int:
            while self.returncode is None:
                await asyncio.sleep(0.01)
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    fake_proc = FakePosixProc(pid=7788)

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        entered_pipe_setup.set()
        await release_pipe_setup.wait()
        return fake_proc

    def fake_killpg(pgid: int, sig: int) -> None:
        assert pgid == 7788
        if sig == 0:
            if state["grandchild_alive"] or fake_proc.returncode is None:
                return
            raise ProcessLookupError()
        state["signals"].append(sig)
        if sig == sigterm:
            fake_proc.returncode = -15
        elif sig == sigkill:
            state["grandchild_alive"] = False

    monkeypatch.setattr(pc.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(pc.os, "killpg", fake_killpg, raising=False)

    tracker = pc.ProcessTracker()

    async def runner_task_fn() -> None:
        token = pc.CURRENT_PROCESS_TRACKER.set(tracker)
        try:
            await pc.spawn_managed_process("fake-cmd")
        finally:
            pc.CURRENT_PROCESS_TRACKER.reset(token)

    task = asyncio.create_task(runner_task_fn())
    await asyncio.wait_for(entered_pipe_setup.wait(), timeout=2.0)
    task.cancel()
    await asyncio.sleep(0.05)
    release_pipe_setup.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert state["signals"] == [sigterm, sigkill]
    assert state["grandchild_alive"] is False
    assert await tracker.terminate_all_and_verify() is True


async def test_mixed_version_old_writer_timeout_blocks_new_window_resume(tmp_path: Path) -> None:
    """Audit High #3: when an older window writes an uncertain terminal job directly to `jobs`
    after the DB has already migrated, the new window reconciles it and blocks automatic resume
    (both live and after reopening)."""
    from agent_bridge.storage import BridgeStore

    fake_agy = tmp_path / "fake_agy_mixed.py"
    fake_agy.write_text(
        """
import json, sys

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-medium\\tGemini 4 Neon (Medium)")
    sys.exit(0)

prompt = [a for a in args if a.startswith("--print=")][0][len("--print="):]
print(json.dumps({
    "conversation_id": "conv-mixed-ver",
    "status": "SUCCESS",
    "response": f"OK:{prompt}",
    "num_turns": 2,
    "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
}))
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    mgr = JobManager(settings, neon_runner=StubAgyRunner(settings, fake_agy))
    # DB is now fully migrated (`session_safety_backfilled` is already populated in `meta`).

    # Simulate an older window (which never touches `session_safety_state`) recording a timed_out
    # job with `provider_state='possibly_running'` and default `provider_invoked=0`!
    now = time.time()
    with mgr.store._tx(write=True) as cur:
        cur.execute(
            """
            INSERT INTO jobs (
                job_id, owner_id, backend, quota_pool, cwd, effective_cwd, mode,
                prompt_preview, session_id, status, failure_reason, provider_state,
                created_at, started_at, finished_at, duration_seconds, usage_status,
                provider_invoked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "neon-oldwin1",
                "old-window-1",
                "neon",
                "agy_gemini",
                str(tmp_path),
                str(tmp_path),
                "read_only",
                "old turn",
                "conv-mixed-ver",
                "timed_out",
                "provider_timeout_still_running",
                "possibly_running",
                now - 5.0,
                now - 4.5,
                now - 1.0,
                3.5,
                "missing",
                0,  # Simulates pre-0b40240 writer omitting `provider_invoked`!
            ),
        )

    # 1. Live already-open new window MUST block unconfirmed resume and persist into session_safety_state!
    blocked_live = await mgr.run(
        mgr.prepare_request(
            backend="neon",
            model="neon-medium",
            prompt="SHOULD_BLOCK_LIVE",
            resume_from="conv-mixed-ver",
        )
    )
    assert blocked_live.status == "failed"
    assert blocked_live.failure_reason == "prior_turn_uncertain"
    assert "neon-oldwin1" in (blocked_live.error or "")

    # 2. Simulate a SECOND older-window uncertain timeout and verify reopening ALSO catches it!
    with mgr.store._tx(write=True) as cur:
        cur.execute("DELETE FROM session_safety_state WHERE session_id = 'conv-mixed-ver'")
        cur.execute(
            """
            INSERT INTO jobs (
                job_id, owner_id, backend, quota_pool, cwd, effective_cwd, mode,
                prompt_preview, session_id, status, failure_reason, provider_state,
                created_at, started_at, finished_at, duration_seconds, usage_status,
                provider_invoked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "neon-oldwin2",
                "old-window-2",
                "neon",
                "agy_gemini",
                str(tmp_path),
                str(tmp_path),
                "read_only",
                "old turn 2",
                "conv-mixed-ver",
                "timed_out",
                "timeout",
                "possibly_running",
                now - 0.8,
                now - 0.7,
                now - 0.1,
                0.6,
                "missing",
                1,
            ),
        )

    reopened_store = BridgeStore(settings.state_dir, max_stored_jobs=settings.max_stored_jobs)
    block_after_reopen = reopened_store.get_session_safety_block("neon", "conv-mixed-ver")
    assert block_after_reopen == {
        "blocking_job_id": "neon-oldwin2",
        "provider_state": "possibly_running",
    }

    mgr2 = JobManager(settings, neon_runner=StubAgyRunner(settings, fake_agy), store=reopened_store)
    blocked_reopen = await mgr2.run(
        mgr2.prepare_request(
            backend="neon",
            model="neon-medium",
            prompt="SHOULD_BLOCK_REOPEN",
            resume_from="conv-mixed-ver",
        )
    )
    assert blocked_reopen.status == "failed"
    assert blocked_reopen.failure_reason == "prior_turn_uncertain"
    assert "neon-oldwin2" in (blocked_reopen.error or "")

    # 3. Confirmed resume succeeds and clears the block cleanly!
    confirmed = await mgr2.run(
        mgr2.prepare_request(
            backend="neon",
            model="neon-medium",
            prompt="CONFIRMED_OK",
            resume_from="conv-mixed-ver",
            confirm_prior_stopped=True,
        )
    )
    assert confirmed.status == "completed"
    assert "OK:CONFIRMED_OK" in confirmed.response
    assert reopened_store.get_session_safety_block("neon", "conv-mixed-ver") is None

    await mgr.shutdown()
    await mgr2.shutdown()


async def test_normal_exit_with_pipe_inheriting_descendant_completes_without_timeout(
    tmp_path: Path,
) -> None:
    """Audit Medium #4: when a worker emits a valid result and exits 0 while a sleeping child
    inherits its stdout/stderr pipes, the bridge detects leader exit, cleans up the child, drains
    remaining output, and reports `completed` (never falsely `timed_out`)."""
    from agent_bridge.process_control import is_pid_alive

    # Part A: Codex worker with pipe-inheriting sleeping descendant
    codex_gc_pid = tmp_path / "codex_pipe_gc.pid"
    codex_script = tmp_path / "codex_pipe_inherit.py"
    codex_script.write_text(
        f"""
import json, subprocess, sys
from pathlib import Path

# Deliberately inherit stdout & stderr pipes (NO DEVNULL)!
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path({str(codex_gc_pid)!r}).write_text(str(g.pid), encoding="utf-8")
_ = sys.stdin.read()
print(json.dumps({{"type": "thread.started", "thread_id": "thr-pipe-ok"}}), flush=True)
print(json.dumps({{"type": "item.completed", "item": {{"id": "m1", "type": "agent_message", "text": "CODEX_PIPE_SUCCESS"}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 12, "output_tokens": 8}}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    # Part B: Neon worker with pipe-inheriting sleeping descendant
    neon_gc_pid = tmp_path / "neon_pipe_gc.pid"
    neon_script = tmp_path / "neon_pipe_inherit.py"
    neon_script.write_text(
        f"""
import json, subprocess, sys
from pathlib import Path

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-medium\\tGemini 4 Neon (Medium)")
    sys.exit(0)

g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path({str(neon_gc_pid)!r}).write_text(str(g.pid), encoding="utf-8")
print(json.dumps({{"event": "init", "conversation_id": "conv-pipe-ok", "init": {{"model": "gemini-4-neon-medium"}}}}), flush=True)
print(json.dumps({{"event": "result", "result": {{"conversation_id": "conv-pipe-ok", "status": "SUCCESS", "response": "NEON_PIPE_SUCCESS", "num_turns": 1, "usage": {{"input_tokens": 15, "output_tokens": 5, "total_tokens": 20}}}}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    mgr = JobManager(
        settings,
        codex_runner=StubCodexRunner(settings, codex_script),
        neon_runner=StubAgyRunner(settings, neon_script),
    )

    t0 = time.monotonic()
    c_rec = await mgr.run(mgr.prepare_request(backend="codex", prompt="go", timeout_seconds=10))
    assert (time.monotonic() - t0) < 4.5
    assert c_rec.status == "completed"
    assert c_rec.provider_state == "stopped"
    assert "CODEX_PIPE_SUCCESS" in c_rec.response
    assert codex_gc_pid.exists()
    assert not is_pid_alive(int(codex_gc_pid.read_text(encoding="utf-8").strip()))

    t1 = time.monotonic()
    n_rec = await mgr.run(
        mgr.prepare_request(backend="neon", model="neon-medium", prompt="go", timeout_seconds=10)
    )
    assert (time.monotonic() - t1) < 4.5
    assert n_rec.status == "completed"
    assert n_rec.provider_state == "stopped"
    assert "NEON_PIPE_SUCCESS" in n_rec.response
    assert neon_gc_pid.exists()
    assert not is_pid_alive(int(neon_gc_pid.read_text(encoding="utf-8").strip()))

    await mgr.shutdown()


async def test_empty_initial_catalog_refuses_unverified_cli_default_on_neon_low(
    tmp_path: Path,
) -> None:
    """Audit Medium #5: with `neon-low`, cross-pool fallback disabled, and an empty catalog,
    NeonRunner MUST NOT omit `--model` and accidentally spend the CLI default's Claude/GPT pool."""
    invocations_log = tmp_path / "empty_cat_calls.log"
    fake_agy = tmp_path / "fake_agy_empty_cat.py"
    fake_agy.write_text(
        f"""
import json, sys
from pathlib import Path

args = sys.argv[1:]
if args and args[0] == "models":
    sys.stderr.write("temporary catalog outage\\n")
    sys.exit(1)

model = args[args.index("--model") + 1] if "--model" in args else "CLI_DEFAULT_SONNET"
with Path({str(invocations_log)!r}).open("a", encoding="utf-8") as fh:
    fh.write(model + "\\n")

print(json.dumps({{
    "conversation_id": "conv-sonnet-default",
    "status": "SUCCESS",
    "response": f"RAN:{{model}}",
    "num_turns": 1,
    "usage": {{"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
}}))
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    nrunner = StubAgyRunner(settings, fake_agy)

    # Default (allow_cross_pool_fallback=False): MUST fail with model_unavailable without spawning!
    res = await nrunner.run(
        RunRequest(
            backend="neon",
            model="neon-low",
            prompt="hello",
            cwd=str(tmp_path),
            allow_cross_pool_fallback=False,
        )
    )
    assert res.status == "failed"
    assert res.failure_reason == "model_unavailable"
    assert res.provider_invoked is False
    assert not invocations_log.exists(), "agy should NEVER have run with unverified CLI default!"

    # Opt-in (allow_cross_pool_fallback=True): permitted to fall back to CLI default with notice.
    res_opt = await nrunner.run(
        RunRequest(
            backend="neon",
            model="neon-low",
            prompt="hello",
            cwd=str(tmp_path),
            allow_cross_pool_fallback=True,
        )
    )
    assert res_opt.status == "completed"
    assert "RAN:CLI_DEFAULT_SONNET" in res_opt.response
    assert res_opt.model_notice is not None


async def test_worktree_resume_preserves_worktree_when_parent_cwd_passed_unless_relocate_cwd(
    tmp_path: Path,
) -> None:
    """Audit High #1: passing parent checkout `cwd` on a worktree resume preserves the recorded
    worktree unless `relocate_cwd=True` is explicitly supplied."""
    import subprocess

    repo = tmp_path / "wt_preserve_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    fake_codex = tmp_path / "fake_codex_wt_resume.py"
    fake_codex.write_text(
        """
import json, os, sys

_ = sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-wt-preserve"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": f"CWD={os.getcwd()}"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path, default_cwd=repo)
    mgr = JobManager(settings, codex_runner=StubCodexRunner(settings, fake_codex))

    t1 = await mgr.run(
        mgr.prepare_request(
            backend="codex",
            prompt="edit in worktree",
            cwd=str(repo),
            mode="edit",
            worktree=True,
        )
    )
    assert t1.status == "completed"
    assert t1.worktree_path is not None
    wt_resolved = str(Path(t1.worktree_path).resolve())
    assert t1.effective_cwd == wt_resolved
    assert f"CWD={wt_resolved}" in t1.response

    # Resume passing the PARENT checkout `cwd=str(repo)` WITHOUT `relocate_cwd=True`:
    # MUST stay inside the worktree and preserve worktree metadata!
    t2 = await mgr.run(
        mgr.prepare_request(
            backend="codex",
            prompt="follow up with parent cwd",
            cwd=str(repo),
            mode="edit",
            resume_from=t1.job_id,
        )
    )
    assert t2.status == "completed"
    assert t2.worktree_path == wt_resolved
    assert t2.effective_cwd == wt_resolved
    assert f"CWD={wt_resolved}" in t2.response

    # Explicit `relocate_cwd=True` moves the session back to `repo` and drops worktree_path.
    t3 = await mgr.run(
        mgr.prepare_request(
            backend="codex",
            prompt="explicitly relocate",
            cwd=str(repo),
            mode="edit",
            resume_from=t1.job_id,
            relocate_cwd=True,
        )
    )
    assert t3.status == "completed"
    assert t3.worktree_path is None
    assert t3.effective_cwd == str(repo.resolve())
    assert f"CWD={repo.resolve()}" in t3.response
    await mgr.shutdown()


async def test_sqlite_write_lock_keeps_event_loop_responsive_and_commits_finalization_durably(
    tmp_path: Path,
) -> None:
    """Audit High #2: holding a real SQLite write lock does NOT freeze the MCP event loop and
    terminal finalization retries idempotently (preserving response, usage, and session claim
    until SQLite commits)."""
    import sqlite3

    fake_codex = tmp_path / "fake_codex_lock.py"
    fake_codex.write_text(
        """
import json, sys, time

_ = sys.stdin.read()
time.sleep(0.15)
print(json.dumps({"type": "thread.started", "thread_id": "thr-lock-safe"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "LOCK_SURVIVED_RESULT"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 30, "output_tokens": 12}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    mgr = JobManager(settings, codex_runner=StubCodexRunner(settings, fake_codex))
    mgr._finalize_retry_seconds = 6.0

    # Launch the job and wait until launch intent (`provider_invoked`) commits before holding the lock.
    job = await mgr.start_async(mgr.prepare_request(backend="codex", prompt="finish under lock"))
    launch_dl = time.monotonic() + 3.0
    while time.monotonic() < launch_dl:
        cur = mgr.get(job.job_id)
        if cur.provider_invoked:
            break
        await asyncio.sleep(0.01)
    assert mgr.get(job.job_id).provider_invoked is True

    # Hold a real external SQLite write lock for 2.2 seconds!
    locker = sqlite3.connect(str(mgr.store.db_path), isolation_level=None, timeout=1.0)
    locker.execute("BEGIN IMMEDIATE")

    ticks = 0
    stop_ticker = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop_ticker:
            ticks += 1
            await asyncio.sleep(0.05)

    tick_task = asyncio.create_task(ticker())
    try:
        # Let worker complete while lock is held, proving event loop stays responsive AND WAL reads work!
        wait_dl = time.monotonic() + 2.0
        during_lock = None
        while time.monotonic() < wait_dl:
            during_lock = await mgr.get_async(job.job_id)
            if during_lock.status == "completed":
                break
            await asyncio.sleep(0.05)
        assert ticks >= 6, f"Event loop stalled during SQLite write lock (only {ticks} ticks)"
        assert during_lock is not None and during_lock.status == "completed"
        assert "LOCK_SURVIVED_RESULT" in during_lock.response
        # WAL-concurrent read-only query succeeds immediately while write lock is held!
        summaries = await mgr.list_jobs_async(limit=5)
        assert any(s["job_id"] == job.job_id and s["status"] == "completed" for s in summaries)

        await asyncio.sleep(0.8)
    finally:
        locker.execute("ROLLBACK")
        locker.close()
        stop_ticker = True
        await tick_task

    # Once the write lock releases, finalization commits cleanly to SQLite with full response + usage!
    dl = time.monotonic() + 5.0
    persisted = None
    while time.monotonic() < dl:
        persisted = mgr.store.get_job(job.job_id)
        if persisted is not None and persisted.status == "completed":
            break
        await asyncio.sleep(0.05)

    assert persisted is not None
    assert persisted.status == "completed"
    assert "LOCK_SURVIVED_RESULT" in persisted.response
    assert persisted.usage is not None and persisted.usage["total_tokens"] == 42
    summary = await mgr.get_usage_summary(period="session", compact=True)
    assert summary["by_backend"]["codex"]["tokens"]["total_tokens"] == 42
    await mgr.shutdown()


async def test_retry_guards_uncertain_and_resumed_turns_and_preserves_attempt_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit High #3: automatic retries never replace an uncertain (`possibly_running`) attempt or
    a resumed transient error, and valid retries preserve duration + available/partial usage for
    every attempt."""
    from agent_bridge.models import RunResult

    settings = _make_settings(tmp_path)

    # Part A: Uncertain attempt (`provider_state="possibly_running"`) MUST NOT trigger retry!
    calls_a = 0
    runner_a = CodexRunner(settings)

    async def fake_run_once_uncertain(*_args, **_kwargs) -> RunResult:
        nonlocal calls_a
        calls_a += 1
        return RunResult(
            status="failed",
            session_id="thr-unc",
            model="gpt-6.2-sol",
            duration_seconds=1.1,
            usage={"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
            usage_status="partial",
            error="HTTP 503 Service Unavailable",
            failure_reason="transient_service",
            provider_state="possibly_running",
            provider_invoked=True,
        )

    monkeypatch.setattr(runner_a, "_run_once", fake_run_once_uncertain)
    res_a = await runner_a.run(
        RunRequest(backend="codex", prompt="check", cwd=str(tmp_path), mode="read_only")
    )
    assert calls_a == 1
    assert res_a.status == "failed"
    assert res_a.provider_state == "possibly_running"

    # Part B: Resumed turn hitting `transient_service` (even with `stopped`) MUST NOT auto-retry!
    calls_b = 0
    runner_b = CodexRunner(settings)

    async def fake_run_once_resumed_transient(*_args, **_kwargs) -> RunResult:
        nonlocal calls_b
        calls_b += 1
        return RunResult(
            status="failed",
            session_id="thr-existing",
            model="gpt-6.2-sol",
            duration_seconds=0.9,
            usage_status="missing",
            error="HTTP 503 Service Unavailable",
            failure_reason="transient_service",
            provider_state="stopped",
            provider_invoked=True,
        )

    monkeypatch.setattr(runner_b, "_run_once", fake_run_once_resumed_transient)
    res_b = await runner_b.run(
        RunRequest(
            backend="codex",
            prompt="follow-up",
            cwd=str(tmp_path),
            mode="read_only",
            session_id="thr-existing",
        )
    )
    assert calls_b == 1
    assert res_b.status == "failed"
    assert res_b.failure_reason == "transient_service"

    # Part C: New read-only turn where Attempt 1 fails with partial usage (50 tokens, 1.2s) and
    # Attempt 2 succeeds with exact usage (25 tokens, 0.8s) -> BOTH attempts preserved in detail & SQLite!
    from agent_bridge import runners as runners_mod

    monkeypatch.setattr(runners_mod, "TRANSIENT_RETRY_BACKOFF_SECONDS", 0.01)
    runner_c = CodexRunner(settings)
    attempt_num = 0

    async def fake_run_once_two_attempts(*_args, **_kwargs) -> RunResult:
        nonlocal attempt_num
        attempt_num += 1
        if attempt_num == 1:
            return RunResult(
                status="failed",
                model="gpt-6.2-sol",
                duration_seconds=1.2,
                usage={"input_tokens": 40, "output_tokens": 10, "total_tokens": 50},
                usage_status="partial",
                error="HTTP 503 Service Unavailable",
                failure_reason="transient_service",
                provider_state="stopped",
                provider_invoked=True,
            )
        return RunResult(
            status="completed",
            response="ATTEMPT_2_OK",
            session_id="thr-retry-ok",
            model="gpt-6.2-sol",
            duration_seconds=0.8,
            usage={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
            usage_status="exact",
            provider_state="stopped",
            provider_invoked=True,
        )

    monkeypatch.setattr(runner_c, "_run_once", fake_run_once_two_attempts)
    mgr = JobManager(settings, codex_runner=runner_c)
    rec = await mgr.run(
        mgr.prepare_request(backend="codex", prompt="new ro", mode="read_only", parent_run="retry-wf")
    )
    assert rec.status == "completed"
    assert rec.duration_seconds == 2.0  # 1.2 + 0.8!
    detail = rec.to_detail()
    assert len(detail["attempts"]) == 2
    assert detail["attempts"][0]["usage"]["total_tokens"] == 50
    assert detail["attempts"][0]["usage_status"] == "partial"
    assert detail["attempts"][1]["usage"]["total_tokens"] == 25
    assert detail["attempts"][1]["usage_status"] == "exact"

    usage_sum = await mgr.get_usage_summary(period="session", parent_run="retry-wf", compact=False)
    codex_bucket = usage_sum["by_backend"]["codex"]
    assert usage_sum["overview"]["runs"] == 1
    assert usage_sum["overview"]["attempts"] == 2
    assert codex_bucket["runs"] == 1
    assert codex_bucket["attempts"] == 2
    assert codex_bucket["completed"] == 1
    assert codex_bucket["failed"] == 0
    assert codex_bucket["duration_seconds"] == 2.0
    assert codex_bucket["tokens"]["total_tokens"] == 25
    assert codex_bucket["partial_total_tokens"] == 50
    assert codex_bucket["usage_coverage"]["exact"] == 1
    assert codex_bucket["usage_coverage"]["partial"] == 1
    assert len(usage_sum["recent"]) == 2
    assert all(entry["job_id"] == rec.job_id for entry in usage_sum["recent"])
    assert all(mgr.get(entry["job_id"]).job_id == rec.job_id for entry in usage_sum["recent"])
    await mgr.shutdown()


async def test_agy_resume_inherits_model_effort_parent_run_and_discloses_pool_change(
    tmp_path: Path,
) -> None:
    """Audit Medium #4: resuming by job_id inherits prior model intent, effort, and parent_run
    instead of silently switching from Sonnet to Gemini or disappearing from workflow rollups;
    explicit cross-pool override is surfaced in `model_notice`."""
    fake_agy = tmp_path / "fake_agy_inherit.py"
    fake_agy.write_text(
        """
import json, sys

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-low\\tGemini 4 Neon (Low)")
    print("gemini-4-neon-high\\tGemini 4 Neon (High)")
    print("claude-sonnet-5-5-medium\\tClaude Sonnet 5.5 (Medium)")
    print("claude-sonnet-5-5-high\\tClaude Sonnet 5.5 (High)")
    sys.exit(0)

model = args[args.index("--model") + 1] if "--model" in args else "UNKNOWN"
prompt = [a for a in args if a.startswith("--print=")][0][len("--print="):]
cid = "conv-inherit-pool"
print(json.dumps({"event": "init", "conversation_id": cid, "init": {"model": model}}), flush=True)
print(json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": f"RAN:{model}:{prompt}", "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}}}), flush=True)
print(json.dumps({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": f"RAN:{model}:{prompt}", "num_turns": 1, "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    mgr = JobManager(settings, neon_runner=StubAgyRunner(settings, fake_agy))

    # Turn 1: agy-claude (Sonnet) with effort=medium and parent_run="wf-pool"
    t1 = await mgr.run(
        mgr.prepare_request(
            backend="agy-claude",
            prompt="turn1",
            effort="medium",
            parent_run="wf-pool",
        )
    )
    assert t1.status == "completed"
    assert t1.model == "claude-sonnet-5-5-medium"
    assert t1.quota_pool == "agy_claude_gpt"
    assert t1.parent_run == "wf-pool"

    # Turn 2: resume with generic `backend="neon"`, omitting `model`, `effort`, and `parent_run`!
    t2 = await mgr.run(
        mgr.prepare_request(
            backend="neon",
            prompt="turn2",
            resume_from=t1.job_id,
        )
    )
    assert t2.status == "completed"
    assert t2.model == "claude-sonnet-5-5-medium"
    assert t2.quota_pool == "agy_claude_gpt"
    assert t2.effort == "medium"
    assert t2.parent_run == "wf-pool"
    assert t2.model_notice is None

    # Turn 3: explicitly override model to `neon-low` (agy_gemini) -> MUST disclose pool change!
    t3 = await mgr.run(
        mgr.prepare_request(
            backend="neon",
            model="neon-low",
            prompt="turn3",
            resume_from=t2.job_id,
        )
    )
    assert t3.status == "completed"
    assert t3.model == "gemini-4-neon-low"
    assert t3.quota_pool == "agy_gemini"
    assert t3.parent_run == "wf-pool"
    assert t3.model_notice is not None
    assert "agy_claude_gpt" in t3.model_notice and "agy_gemini" in t3.model_notice

    # All 3 turns appear under `parent_run="wf-pool"`!
    rollup = await mgr.get_usage_summary(period="session", parent_run="wf-pool", compact=True)
    assert rollup["overview"]["runs"] == 3
    assert rollup["by_quota_pool"]["agy_claude_gpt"]["runs"] == 2
    assert rollup["by_quota_pool"]["agy_gemini"]["runs"] == 1
    await mgr.shutdown()


async def test_startup_deadline_enforced_on_git_probe_and_cleanup_seconds_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit Medium #5: startup honors the execution deadline (delayed spawn cannot let a 50ms git
    probe succeed after 400ms+), and cleanup time is reported separately from work duration."""
    from agent_bridge import process_control as pc
    from agent_bridge import runners as runners_mod

    # Part A: Delayed startup against a 60ms Git probe MUST time out quickly!
    orig_create = pc.asyncio.create_subprocess_exec

    async def slow_create(*args, **kwargs):
        await asyncio.sleep(0.45)
        return await orig_create(*args, **kwargs)

    monkeypatch.setattr(pc.asyncio, "create_subprocess_exec", slow_create)
    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await runners_mod._run_git(["--version"], cwd=str(tmp_path), timeout_seconds=0.06)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.3, f"Delayed startup bypassed 60ms git timeout (took {elapsed:.3f}s)"
    monkeypatch.setattr(pc.asyncio, "create_subprocess_exec", orig_create)

    # Part B: Timed-out job reports `cleanup_seconds` separately in `to_detail()`!
    sleeper = tmp_path / "sleeper_cleanup.py"
    sleeper.write_text("import sys, time; _ = sys.stdin.read(); time.sleep(30)\n", encoding="utf-8")
    settings = _make_settings(tmp_path)
    mgr = JobManager(settings, codex_runner=StubCodexRunner(settings, sleeper))
    req = mgr.prepare_request(backend="codex", prompt="timeout", timeout_seconds=10)
    res = await mgr.codex_runner.run(req, deadline=time.monotonic() + 0.35)
    assert res.status == "timed_out"
    assert res.cleanup_seconds is not None and res.cleanup_seconds >= 0.0
    assert res.duration_seconds is not None and res.duration_seconds < 1.0
    await mgr.shutdown()


async def test_list_backends_isolates_empty_agy_catalog_and_returns_codex_health(
    tmp_path: Path,
) -> None:
    """Audit Medium #6: an empty/failing Antigravity model catalog does NOT break `list_backends`;
    it reports the catalog failure as an Antigravity diagnostic while returning healthy Codex info."""
    healthy_empty_agy = tmp_path / "agy_empty_cat.py"
    healthy_empty_agy.write_text(
        """
import sys

args = sys.argv[1:]
if "--version" in args:
    print("agy 1.2.3")
    sys.exit(0)
if args and args[0] == "models":
    sys.stderr.write("catalog outage\\n")
    sys.exit(1)
sys.exit(0)
""".strip(),
        encoding="utf-8",
    )
    healthy_codex = tmp_path / "codex_ok.py"
    healthy_codex.write_text(
        """
import sys

if "--version" in sys.argv[1:]:
    print("codex-cli 0.99.0")
    sys.exit(0)
sys.exit(0)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    mgr = JobManager(
        settings,
        neon_runner=StubAgyRunner(settings, healthy_empty_agy),
        codex_runner=StubCodexRunner(settings, healthy_codex),
    )
    mcp, _ = create_server(settings=settings, manager=mgr)
    raw = await mcp.call_tool("list_backends", {"refresh_models": True})
    # FastMCP returns either (content, structured_dict) or content list with JSON text
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
        data = raw[1]
    elif isinstance(raw, dict):
        data = raw
    else:
        content_list = raw.content if hasattr(raw, "content") else raw
        data = json.loads(content_list[0].text)

    assert data["backends"]["codex"]["healthy"] is True
    assert data["backends"]["codex"]["available"] is True
    assert "0.99.0" in (data["backends"]["codex"]["version"] or "")
    assert data["backends"]["neon"]["healthy"] is True
    assert data["backends"]["neon"]["available"] is False
    assert data["backends"]["neon"]["resolved_default_model"] is None
    assert data["backends"]["neon"]["models_status"] == "unavailable"
    assert data["backends"]["neon"].get("catalog_error")
    await mgr.shutdown()


async def test_missing_worktree_fails_resume_unless_relocate_or_new_worktree(
    tmp_path: Path,
) -> None:
    """Audit High #1: if a session's recorded worktree was removed, resuming with
    `relocate_cwd=False` and `worktree=False` MUST fail (both during `prepare_request` and
    pre-launch refresh) instead of targeting the parent checkout."""
    import subprocess

    repo = tmp_path / "wt_missing_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    invocations_log = tmp_path / "wt_missing_invocations.log"
    fake_codex = tmp_path / "fake_codex_wt_missing.py"
    fake_codex.write_text(
        f"""
import json, os, sys
from pathlib import Path

prompt = sys.stdin.read().strip()
with Path({str(invocations_log)!r}).open("a", encoding="utf-8") as fh:
    fh.write(f"{{prompt}}|{{os.getcwd()}}\\n")
tid = "thr-wt-missing-2" if "second-session" in prompt else "thr-wt-missing-1"
print(json.dumps({{"type": "thread.started", "thread_id": tid}}), flush=True)
print(json.dumps({{"type": "item.completed", "item": {{"id": "m1", "type": "agent_message", "text": f"CWD={{os.getcwd()}}"}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 5}}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path, default_cwd=repo)
    mgr = JobManager(settings, codex_runner=StubCodexRunner(settings, fake_codex))

    t1 = await mgr.run(
        mgr.prepare_request(
            backend="codex",
            prompt="first-session",
            cwd=str(repo),
            mode="edit",
            worktree=True,
        )
    )
    assert t1.status == "completed"
    assert t1.worktree_path is not None
    wt1 = Path(t1.worktree_path)
    assert wt1.is_dir()

    # Remove the recorded worktree with Git.
    subprocess.run(["git", "worktree", "remove", "--force", str(wt1)], cwd=repo, check=True, capture_output=True)
    assert not wt1.exists()

    # Resuming by job_id OR raw session_id with parent cwd and relocate_cwd=False MUST fail!
    with pytest.raises(ValueError, match="no longer exists"):
        mgr.prepare_request(
            backend="codex",
            prompt="resume-should-fail",
            cwd=str(repo),
            mode="edit",
            resume_from=t1.job_id,
        )
    with pytest.raises(ValueError, match="no longer exists"):
        mgr.prepare_request(
            backend="codex",
            prompt="resume-sid-should-fail",
            cwd=str(repo),
            mode="edit",
            resume_from=t1.session_id,
        )

    # Pre-launch refresh guard: prepare while worktree exists, remove it before execution starts!
    t2 = await mgr.run(
        mgr.prepare_request(
            backend="codex",
            prompt="second-session",
            cwd=str(repo),
            mode="edit",
            worktree=True,
        )
    )
    assert t2.status == "completed" and t2.worktree_path is not None
    wt2 = Path(t2.worktree_path)
    req_prepared = mgr.prepare_request(
        backend="codex",
        prompt="prepared-before-removal",
        cwd=str(repo),
        mode="edit",
        resume_from=t2.job_id,
    )
    subprocess.run(["git", "worktree", "remove", "--force", str(wt2)], cwd=repo, check=True, capture_output=True)
    assert not wt2.exists()

    before_lines = invocations_log.read_text(encoding="utf-8").splitlines()
    failed_rec = await mgr.run(req_prepared)
    assert failed_rec.status == "failed"
    assert failed_rec.failure_reason == "worktree_missing"
    assert failed_rec.provider_state == "stopped"
    assert failed_rec.provider_invoked is False
    # Provider MUST NOT have been invoked in the parent checkout!
    assert invocations_log.read_text(encoding="utf-8").splitlines() == before_lines

    # Explicit opt-in #1: `relocate_cwd=True` succeeds in parent checkout.
    reloc = await mgr.run(
        mgr.prepare_request(
            backend="codex",
            prompt="relocate-ok",
            cwd=str(repo),
            mode="edit",
            resume_from=t1.job_id,
            relocate_cwd=True,
        )
    )
    assert reloc.status == "completed"
    assert reloc.worktree_path is None
    assert reloc.effective_cwd == str(repo.resolve())

    # Explicit opt-in #2: `worktree=True` creates a fresh worktree instead of reusing deleted `wt2`.
    new_wt = await mgr.run(
        mgr.prepare_request(
            backend="codex",
            prompt="new-worktree-ok",
            cwd=str(repo),
            mode="edit",
            resume_from=t2.job_id,
            worktree=True,
        )
    )
    assert new_wt.status == "completed"
    assert new_wt.worktree_path is not None
    assert Path(new_wt.worktree_path).resolve() != wt2.resolve()
    assert Path(new_wt.worktree_path).is_dir()
    await mgr.shutdown()


async def test_safety_critical_commits_block_session_publication_and_launch_until_sqlite_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit High #2:
    (A) Window 1 MUST NOT expose a newly discovered session_id before SQLite commits its
        session_claims row, so Window 2 can never start a concurrent turn on that session.
    (B) A SQLite lock before launch MUST prevent spawning the provider while the DB row still
        says `queued`, and when unlocked, `status='running'` + `provider_invoked=1` commit
        BEFORE the provider executes.
    """
    import sqlite3
    import threading

    # --- Part A: Two-window session publication ordering ---
    gate1 = tmp_path / "gate1.go"
    order_log = tmp_path / "order.log"
    fake_codex_two = tmp_path / "fake_codex_two_win.py"
    fake_codex_two.write_text(
        f"""
import json, sys, time
from pathlib import Path

prompt = sys.stdin.read().strip()
log_p = Path({str(order_log)!r})
print(json.dumps({{"type": "thread.started", "thread_id": "thr-two-win-atomic"}}), flush=True)
with log_p.open("a", encoding="utf-8") as fh:
    fh.write(f"START:{{prompt}}\\n")
if prompt == "turn1":
    gate = Path({str(gate1)!r})
    dl = time.monotonic() + 10.0
    while not gate.exists() and time.monotonic() < dl:
        time.sleep(0.02)
with log_p.open("a", encoding="utf-8") as fh:
    fh.write(f"END:{{prompt}}\\n")
print(json.dumps({{"type": "item.completed", "item": {{"id": "m1", "type": "agent_message", "text": f"DONE:{{prompt}}"}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 5}}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    mgr1 = JobManager(settings, codex_runner=StubCodexRunner(settings, fake_codex_two), owner_id="win-1")
    mgr2 = JobManager(settings, codex_runner=StubCodexRunner(settings, fake_codex_two), owner_id="win-2")

    claim_entered = threading.Event()
    allow_claim_commit = threading.Event()
    orig_update = mgr1.store.update_job_progress

    def gated_update(*args, **kwargs):
        if kwargs.get("session_id") == "thr-two-win-atomic":
            claim_entered.set()
            allow_claim_commit.wait(timeout=5.0)
        return orig_update(*args, **kwargs)

    monkeypatch.setattr(mgr1.store, "update_job_progress", gated_update)

    j1 = await mgr1.start_async(mgr1.prepare_request(backend="codex", prompt="turn1", timeout_seconds=15))
    dl = time.monotonic() + 4.0
    while not claim_entered.is_set() and time.monotonic() < dl:
        await asyncio.sleep(0.01)
    assert claim_entered.is_set(), "Window 1 never attempted session claim commit"

    # BEFORE SQLite commits the claim, NEITHER Window 1 NOR Window 2 may see `session_id`!
    assert mgr1.get(j1.job_id).session_id is None
    assert (await mgr1.get_async(j1.job_id)).session_id is None
    db_row_before = mgr2.store.get_job(j1.job_id)
    assert db_row_before is not None and db_row_before.session_id is None

    # Allow Window 1's session claim to commit to SQLite.
    allow_claim_commit.set()
    dl2 = time.monotonic() + 4.0
    while time.monotonic() < dl2:
        if mgr1.get(j1.job_id).session_id == "thr-two-win-atomic":
            break
        await asyncio.sleep(0.01)
    assert mgr1.get(j1.job_id).session_id == "thr-two-win-atomic"

    # Now Window 2 starts Turn 2 on that session while Turn 1 is STILL running -> MUST wait!
    j2 = await mgr2.start_async(
        mgr2.prepare_request(
            backend="codex",
            prompt="turn2",
            resume_from="thr-two-win-atomic",
            timeout_seconds=15,
        )
    )
    await asyncio.sleep(0.25)
    lines_while_running = order_log.read_text(encoding="utf-8").splitlines()
    assert lines_while_running == ["START:turn1"], f"Turn 2 overlapped Turn 1! {lines_while_running}"

    # Release Turn 1 -> Turn 1 finishes, THEN Turn 2 starts and finishes sequentially!
    gate1.write_text("go", encoding="utf-8")
    r1 = (await mgr1.wait_for([j1.job_id], wait_seconds=10))[0]
    r2 = (await mgr2.wait_for([j2.job_id], wait_seconds=10))[0]
    assert r1.status == "completed" and r2.status == "completed"
    assert order_log.read_text(encoding="utf-8").splitlines() == [
        "START:turn1",
        "END:turn1",
        "START:turn2",
        "END:turn2",
    ]
    await mgr1.shutdown()
    await mgr2.shutdown()

    # --- Part B: Launch state commits BEFORE spawning (locked DB blocks spawn!) ---
    settings_b = _make_settings(tmp_path / "b_root", default_cwd=tmp_path)
    state_b = settings_b.state_dir
    spawned_flag = tmp_path / "should_not_spawn.flag"
    db_snapshot_flag = tmp_path / "db_snapshot.json"
    fake_codex_launch = tmp_path / "fake_codex_launch_check.py"
    fake_codex_launch.write_text(
        f"""
import json, sqlite3, sys
from pathlib import Path

prompt = sys.stdin.read().strip()
if prompt == "locked-run":
    Path({str(spawned_flag)!r}).write_text("SPAWNED", encoding="utf-8")
else:
    conn = sqlite3.connect({str(state_b / "bridge.db")!r})
    row = conn.execute("SELECT status, provider_invoked FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    conn.close()
    Path({str(db_snapshot_flag)!r}).write_text(json.dumps({{"status": row[0], "provider_invoked": row[1]}}), encoding="utf-8")
print(json.dumps({{"type": "thread.started", "thread_id": "thr-launch-ok"}}), flush=True)
print(json.dumps({{"type": "item.completed", "item": {{"id": "m1", "type": "agent_message", "text": "OK"}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 5, "output_tokens": 2}}}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    mgr_b = JobManager(settings_b, codex_runner=StubCodexRunner(settings_b, fake_codex_launch))
    mgr_b._finalize_retry_seconds = 3.0

    # Hold a real external SQLite write lock right after `insert_job` commits the `queued` row!
    locker = sqlite3.connect(
        str(mgr_b.store.db_path),
        isolation_level=None,
        timeout=1.0,
        check_same_thread=False,
    )
    orig_insert = mgr_b.store.insert_job
    lock_acquired = False

    def insert_then_lock(rec):
        nonlocal lock_acquired
        orig_insert(rec)
        if not lock_acquired:
            locker.execute("BEGIN IMMEDIATE")
            lock_acquired = True

    monkeypatch.setattr(mgr_b.store, "insert_job", insert_then_lock)

    async def release_lock_later() -> None:
        await asyncio.sleep(1.3)
        if lock_acquired:
            locker.execute("ROLLBACK")
        locker.close()

    rel_task = asyncio.create_task(release_lock_later())
    req_locked = mgr_b.prepare_request(backend="codex", prompt="locked-run", timeout_seconds=10)
    req_locked.timeout_seconds = 0.8  # type: ignore[assignment]
    res_locked = await mgr_b.run(req_locked)
    await rel_task
    assert not spawned_flag.exists(), "Provider spawned while SQLite row was still queued under lock!"
    assert res_locked.status == "timed_out"
    assert res_locked.provider_invoked is False
    assert res_locked.provider_state == "stopped"

    # Now run unlocked and verify the DB ALREADY shows `status='running'` & `provider_invoked=1`
    # when the provider process begins executing!
    monkeypatch.setattr(mgr_b.store, "insert_job", orig_insert)
    res_ok = await mgr_b.run(mgr_b.prepare_request(backend="codex", prompt="unlocked-run", timeout_seconds=10))
    assert res_ok.status == "completed"
    snap = json.loads(db_snapshot_flag.read_text(encoding="utf-8"))
    assert snap["status"] == "running"
    assert snap["provider_invoked"] == 1
    await mgr_b.shutdown()


async def test_graceful_shutdown_waits_for_delayed_terminal_write_before_marking_owner_exited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit High #3: `shutdown()` MUST NOT cancel a job that has already reached terminal
    finalization, and MUST wait for delayed terminal writes to commit before marking the owner
    exited so another window cannot overwrite the completed result as `owner_exited`."""
    import threading

    fake_codex = tmp_path / "fake_codex_shutdown.py"
    fake_codex.write_text(
        """
import json, sys

_ = sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-shutdown-safe"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "PRESERVED_COMPLETED_RESULT"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 25, "output_tokens": 10}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    mgr1 = JobManager(settings, codex_runner=StubCodexRunner(settings, fake_codex), owner_id="win-shutdown-1")

    finalize_entered = threading.Event()
    orig_finalize = mgr1.store.finalize_job

    def delayed_finalize(*args, **kwargs):
        finalize_entered.set()
        time.sleep(0.75)
        return orig_finalize(*args, **kwargs)

    monkeypatch.setattr(mgr1.store, "finalize_job", delayed_finalize)

    job = await mgr1.start_async(mgr1.prepare_request(backend="codex", prompt="finish cleanly"))
    dl = time.monotonic() + 5.0
    while not finalize_entered.is_set() and time.monotonic() < dl:
        await asyncio.sleep(0.01)
    assert finalize_entered.is_set(), "Job never reached terminal finalization"

    # Trigger graceful shutdown RIGHT in the middle of the delayed terminal write!
    await mgr1.shutdown(flush_timeout=10.0)

    # Window 2 opens the same DB (with Window 1's PID treated as dead) and runs recovery:
    mgr2 = JobManager(
        settings,
        codex_runner=StubCodexRunner(settings, fake_codex),
        owner_id="win-shutdown-2",
        is_pid_alive_fn=lambda _pid: False,
    )
    rec = mgr2.get(job.job_id)
    assert rec.status == "completed"
    assert rec.failure_reason is None
    assert "PRESERVED_COMPLETED_RESULT" in rec.response
    assert rec.usage is not None and rec.usage["total_tokens"] == 35
    await mgr2.shutdown()


async def test_agy_remote_in_progress_survives_local_timeout_and_observed_turn_advance_blocks_edit_retry(
    tmp_path: Path,
) -> None:
    """Audit High #4:
    (A) Antigravity's remote in-progress signal (`exit_code=3` or timeout stderr) MUST remain
        `provider_timeout_still_running` / `possibly_running` even when local timeout cleanup stops
        the client process.
    (B) An observed `num_turns` increase on a failed Antigravity edit envelope MUST block automatic
        model-fallback retry even when usage checkpoint fields are absent.
    """
    from agent_bridge.runners import classify_failure

    # Part A1: Direct classifier check for exit_code=3 + timed_out_locally=True + process_stopped=True.
    st, reason, pstate, _guidance = classify_failure(
        backend="neon",
        exit_code=3,
        error_text="Neon subagent timed out after 1s.",
        stderr_text="",
        timed_out_locally=True,
        process_stopped=True,
    )
    assert st == "timed_out"
    assert reason == "provider_timeout_still_running"
    assert pstate == "possibly_running"

    # Part A2: End-to-end NeonRunner local timeout with remote in-progress stderr signal!
    fake_agy_remote = tmp_path / "fake_agy_remote_timeout.py"
    fake_agy_remote.write_text(
        """
import json, sys, time

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-medium\\tGemini 4 Neon (Medium)")
    sys.exit(0)

print(json.dumps({"event": "init", "conversation_id": "conv-remote-still-going", "init": {"model": "gemini-4-neon-medium"}}), flush=True)
sys.stderr.write("Warning: remote turn still in progress after wait deadline\\n")
sys.stderr.flush()
time.sleep(30)
""".strip(),
        encoding="utf-8",
    )
    settings = _make_settings(tmp_path)
    runner_a = StubAgyRunner(settings, fake_agy_remote)
    # Long enough for the stand-in's Python to start and write its stderr line on a loaded machine.
    res_a = await runner_a.run(
        RunRequest(backend="neon", model="neon-medium", prompt="slow", cwd=str(tmp_path), timeout_seconds=10),
        deadline=time.monotonic() + 3.0,
    )
    assert res_a.status == "timed_out"
    assert res_a.failure_reason == "provider_timeout_still_running"
    assert res_a.provider_state == "possibly_running"

    # Part B: Resumed edit where failed model rejection already advanced `num_turns` (1 -> 2) with NO usage!
    edit_calls_log = tmp_path / "edit_retry_calls.log"
    fake_agy_edit = tmp_path / "fake_agy_edit_turn_adv.py"
    fake_agy_edit.write_text(
        f"""
import json, sys
from pathlib import Path

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-medium\\tGemini 4 Neon (Medium)")
    print("gemini-4-neon-high\\tGemini 4 Neon (High)")
    sys.exit(0)

with Path({str(edit_calls_log)!r}).open("a", encoding="utf-8") as fh:
    fh.write("CALL\\n")

print(json.dumps({{
    "conversation_id": "conv-edit-adv",
    "status": "ERROR",
    "error": "Requested model is not available",
    "num_turns": 2,
}}), flush=True)
sys.exit(1)
""".strip(),
        encoding="utf-8",
    )
    runner_b = StubAgyRunner(settings, fake_agy_edit)
    res_b = await runner_b.run(
        RunRequest(
            backend="neon",
            model="gemini-4-neon-medium",
            prompt="apply patch",
            cwd=str(tmp_path),
            mode="edit",
            session_id="conv-edit-adv",
            timeout_seconds=30,
        ),
        prior_watermark={
            "num_turns": 1,
            "cumulative": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
    )
    assert res_b.status == "failed"
    assert res_b.observed_num_turns == 2
    assert edit_calls_log.read_text(encoding="utf-8").splitlines() == ["CALL"], (
        "NeonRunner retried a resumed edit after observed num_turns advanced!"
    )


async def test_cancellation_during_start_async_submission_cleans_up_committed_queued_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit Medium #5: cancelling `start_async()` while `insert_job` is running in a worker
    thread MUST clean up any `queued` row that finishes committing instead of leaving an orphan."""
    import sqlite3
    import threading

    settings = _make_settings(tmp_path)
    mgr = JobManager(settings)

    insert_entered = threading.Event()
    orig_insert = mgr.store.insert_job

    def slow_insert(rec):
        insert_entered.set()
        time.sleep(0.25)
        return orig_insert(rec)

    monkeypatch.setattr(mgr.store, "insert_job", slow_insert)

    req = mgr.prepare_request(backend="codex", prompt="cancel during submit")
    submit_task = asyncio.create_task(mgr.start_async(req))

    dl = time.monotonic() + 3.0
    while not insert_entered.is_set() and time.monotonic() < dl:
        await asyncio.sleep(0.01)
    assert insert_entered.is_set()

    submit_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submit_task

    await mgr.shutdown(flush_timeout=5.0)

    conn = sqlite3.connect(str(mgr.store.db_path))
    try:
        queued_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0]
        total_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()
    assert queued_count == 0, f"Orphaned queued row remained in SQLite ({queued_count})"
    assert total_count == 0


async def test_cancel_during_retry_backoff_preserves_attempt1_usage_and_real_job_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit Medium #6: cancelling a job during retry backoff preserves Attempt 1's reported
    usage and duration (recorded BEFORE backoff starts), exposes distinct job vs attempt counts,
    and uses the real `job_id` on every `usage_events` row."""
    from agent_bridge import runners as runners_mod
    from agent_bridge.models import RunResult

    monkeypatch.setattr(runners_mod, "TRANSIENT_RETRY_BACKOFF_SECONDS", 6.0)
    settings = _make_settings(tmp_path)
    runner = CodexRunner(settings)
    attempt1_done = asyncio.Event()
    calls = 0

    async def fake_run_once_backoff(*_args, **_kwargs) -> RunResult:
        nonlocal calls
        calls += 1
        attempt1_done.set()
        return RunResult(
            status="failed",
            model="gpt-6.2-sol",
            duration_seconds=1.1,
            usage={"input_tokens": 35, "output_tokens": 15, "total_tokens": 50},
            usage_status="partial",
            error="HTTP 503 Service Unavailable",
            failure_reason="transient_service",
            provider_state="stopped",
            provider_invoked=True,
        )

    monkeypatch.setattr(runner, "_run_once", fake_run_once_backoff)
    mgr = JobManager(settings, codex_runner=runner)

    job = await mgr.start_async(
        mgr.prepare_request(
            backend="codex",
            prompt="cancel during backoff",
            mode="read_only",
            parent_run="backoff-cancel-wf",
        )
    )
    await asyncio.wait_for(attempt1_done.wait(), timeout=3.0)

    # Confirm Attempt 1 was ALREADY persisted to `usage_events` BEFORE backoff finished / Attempt 2 started!
    pre_dl = time.monotonic() + 3.0
    pre_sum = None
    while time.monotonic() < pre_dl:
        pre_sum = await mgr.get_usage_summary(period="session", parent_run="backoff-cancel-wf", compact=False)
        if pre_sum["overview"]["attempts"] >= 1:
            break
        await asyncio.sleep(0.02)
    assert pre_sum is not None and pre_sum["overview"]["attempts"] == 1
    assert pre_sum["by_backend"]["codex"]["partial_total_tokens"] == 50

    # Now cancel DURING retry backoff sleep!
    cancelled = await mgr.cancel(job.job_id)
    assert calls == 1, "Attempt 2 should not have started during backoff"
    assert cancelled.status == "cancelled"
    assert cancelled.usage is not None and cancelled.usage["total_tokens"] == 50
    assert cancelled.usage_status == "partial"
    assert cancelled.duration_seconds is not None and cancelled.duration_seconds >= 1.1

    detail = cancelled.to_detail()
    assert len(detail["attempts"]) == 2
    assert detail["attempts"][0]["attempt"] == 1
    assert detail["attempts"][0]["usage"]["total_tokens"] == 50
    assert detail["attempts"][1]["attempt"] == 2
    assert detail["attempts"][1]["status"] == "cancelled"

    usage_sum = await mgr.get_usage_summary(period="session", parent_run="backoff-cancel-wf", compact=False)
    assert usage_sum["overview"]["runs"] == 1
    assert usage_sum["overview"]["attempts"] == 2
    codex_bucket = usage_sum["by_backend"]["codex"]
    assert codex_bucket["runs"] == 1
    assert codex_bucket["attempts"] == 2
    assert codex_bucket["cancelled"] == 1
    assert codex_bucket["failed"] == 0
    assert codex_bucket["partial_total_tokens"] == 50
    assert codex_bucket["duration_seconds"] >= 1.1
    assert len(usage_sum["recent"]) == 2
    for entry in usage_sum["recent"]:
        assert entry["job_id"] == job.job_id
        assert "#attempt-" not in entry["job_id"]
        assert mgr.get(entry["job_id"]).job_id == job.job_id
    await mgr.shutdown()


async def test_agy_runs_without_mode_flag_and_read_only_gets_the_guard(tmp_path: Path) -> None:
    """agy is started with no --mode; a read_only prompt carries the guard, an edit prompt does not."""
    fake_agy = tmp_path / "fake_agy_args.py"
    fake_agy.write_text(
        """
import json, sys

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-medium\tGemini 4 Neon (Medium)")
    sys.exit(0)

prompt = next(a for a in args if a.startswith("--print="))[len("--print="):]
reply = json.dumps({"has_mode": "--mode" in args, "prompt": prompt})
print(json.dumps({"event": "init", "conversation_id": "conv-args", "init": {"model": "gemini-4-neon-medium"}}), flush=True)
print(json.dumps({"event": "result", "result": {"conversation_id": "conv-args", "status": "SUCCESS", "response": reply, "num_turns": 1}}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    from agent_bridge.runners import AGY_READ_ONLY_GUARD

    class GuardedStub(StubAgyRunner):
        read_only_guard = AGY_READ_ONLY_GUARD

    settings = _make_settings(tmp_path)
    manager = JobManager(settings, neon_runner=GuardedStub(settings, fake_agy))

    ro = await manager.run(manager.prepare_request(backend="neon", model="neon-medium", prompt="look around"))
    assert ro.status == "completed"
    seen = json.loads(ro.response)
    assert seen["has_mode"] is False
    assert seen["prompt"] == AGY_READ_ONLY_GUARD + "look around"

    ed = await manager.run(
        manager.prepare_request(backend="neon", model="neon-medium", prompt="fix it", mode="edit")
    )
    assert ed.status == "completed"
    seen = json.loads(ed.response)
    assert seen["has_mode"] is False
    assert seen["prompt"] == "fix it"
    await manager.shutdown()


async def test_agy_denied_action_with_empty_reply_is_a_permission_failure(tmp_path: Path) -> None:
    """Headless agy exits 0 with SUCCESS and no reply after a denied action: that is failed, not completed."""
    fake_agy = tmp_path / "fake_agy_denied.py"
    fake_agy.write_text(
        """
import json, sys

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-4-neon-medium\tGemini 4 Neon (Medium)")
    sys.exit(0)

cid = "conv-denied"
print(json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "gemini-4-neon-medium"}}), flush=True)
print(json.dumps({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": "", "num_turns": 1, "denied_actions": [{"action": "command", "display_name": "Bash"}]}}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    settings = _make_settings(tmp_path)
    manager = JobManager(settings, neon_runner=StubAgyRunner(settings, fake_agy))

    rec = await manager.run(manager.prepare_request(backend="neon", model="neon-medium", prompt="look around"))
    assert rec.status == "failed"
    assert rec.failure_reason == "permission_error"
    assert rec.provider_state == "stopped"
    assert "command" in (rec.error or "")
    assert rec.session_id == "conv-denied"
    await manager.shutdown()
