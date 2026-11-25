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
