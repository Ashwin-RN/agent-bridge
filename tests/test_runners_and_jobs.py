"""Tests for runners, stream-json usage, timeouts, session serialization, multi-window SQLite, and health checks."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from agent_bridge.config import Settings
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
        agy_bin=None,
        codex_bin=None,
        default_cwd=tmp_path,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class StubAgyRunner(NeonRunner):
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
    assert g2.usage_status == "exact" and g2.usage and g2.usage["total_tokens"] == 70
    assert g3.usage_status == "partial"
    assert g3.usage is None  # Cumulative-only blob kept in provider_metrics, NOT counted as per-turn delta!
    assert g3.provider_metrics is not None and g3.provider_metrics.get("cumulative_only") is True

    gap_summary = await mgr.get_usage_summary(period="session", parent_run="gap", compact=True)
    assert gap_summary["by_backend"]["neon"]["tokens"]["total_tokens"] == 170  # Never 280!
    assert gap_summary["by_backend"]["neon"]["usage_coverage"] == {
        "exact": 2,
        "delta": 0,
        "partial": 1,
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
                    kwargs["on_provider_start"]()
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
    mgr.store.insert_job(done_t1)
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
