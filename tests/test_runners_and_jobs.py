"""Tests for CLI runners, self-healing model fallback, and JobManager lifecycle."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_bridge.config import Settings
from agent_bridge.jobs import JobManager
from agent_bridge.models import RunRequest
from agent_bridge.runners import (
    CodexRunner,
    NeonRunner,
    parse_agy_json_output,
    parse_codex_jsonl_events,
)


def _make_settings(tmp_path: Path, **overrides: object) -> Settings:
    base = dict(
        default_neon_model="neon-high",
        default_codex_model=None,
        default_timeout_seconds=60,
        max_concurrent_jobs=4,
        max_stored_jobs=50,
        models_cache_ttl_seconds=900.0,
        state_dir=tmp_path / "state",
        agy_bin=None,
        codex_bin=None,
        default_cwd=tmp_path,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_parse_agy_json_output_with_preamble() -> None:
    noisy = 'Fetching models...\n{"conversation_id":"conv-1","status":"SUCCESS","response":"Hello\\n","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}\n'
    parsed = parse_agy_json_output(noisy)
    assert parsed is not None
    assert parsed["conversation_id"] == "conv-1"
    assert parsed["response"] == "Hello\n"


def test_parse_codex_jsonl_events() -> None:
    lines = [
        '{"type":"thread.started","thread_id":"thr-42"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"First part"}}',
        '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"Second part"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":30,"reasoning_output_tokens":5}}',
    ]
    parsed = parse_codex_jsonl_events(lines)
    assert parsed["session_id"] == "thr-42"
    assert parsed["response"] == "First part\n\nSecond part"
    assert parsed["usage"]["input_tokens"] == 100
    assert parsed["errors"] == []


class StubAgyRunner(NeonRunner):
    """Points NeonRunner at a tiny Python script simulating `agy` behavior."""

    def __init__(self, settings: Settings, script_path: Path) -> None:
        self._script_path = script_path
        super().__init__(settings)

    def resolve_command(self) -> list[str]:
        return [sys.executable, str(self._script_path)]


class StubCodexRunner(CodexRunner):
    """Points CodexRunner at a tiny Python script simulating `codex` behavior."""

    def __init__(self, settings: Settings, script_path: Path) -> None:
        self._script_path = script_path
        super().__init__(settings)

    def resolve_command(self) -> list[str]:
        return [sys.executable, str(self._script_path)]


async def test_neon_runner_self_heals_retired_cached_model(tmp_path: Path) -> None:
    # Write fake `agy` script: rejects gemini-4-neon-high as deprecated, succeeds with gemini-5-neon-high.
    fake_agy = tmp_path / "fake_agy.py"
    fake_agy.write_text(
        """
import json, sys

args = sys.argv[1:]
if args and args[0] == "models":
    print("gemini-5-neon-high\\tGemini 5 Neon (High)")
    sys.exit(0)

model = None
if "--model" in args:
    model = args[args.index("--model") + 1]

if model == "gemini-4-neon-high":
    sys.stderr.write("Error: deprecated model gemini-4-neon-high\\n")
    sys.exit(1)

print(json.dumps({
    "conversation_id": "conv-auto-upgraded",
    "status": "SUCCESS",
    "response": f"OK via {model}",
    "duration_seconds": 0.1,
    "usage": {"input_tokens": 50, "output_tokens": 10, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 60}
}))
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    # Seed stale cache claiming gemini-4-neon-high is still current.
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    import time

    (settings.state_dir / "agy_models_cache.json").write_text(
        json.dumps(
            {
                "fetched_at": time.time(),
                "models": [{"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)"}],
            }
        ),
        encoding="utf-8",
    )

    runner = StubAgyRunner(settings, fake_agy)
    req = RunRequest(backend="neon", prompt="hello", cwd=str(tmp_path), model="neon-high")
    res = await runner.run(req)

    assert res.status == "completed"
    assert res.model == "gemini-5-neon-high"
    assert res.response == "OK via gemini-5-neon-high"
    assert res.session_id == "conv-auto-upgraded"
    assert res.model_notice is not None
    assert "gemini-5-neon-high" in res.model_notice
    assert res.usage is not None and res.usage["total_tokens"] == 60


async def test_codex_runner_stdin_and_model_fallback(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(
        """
import json, sys

args = sys.argv[1:]
prompt = sys.stdin.read()
model = None
if "-m" in args:
    model = args[args.index("-m") + 1]

if model == "retired-gpt-model":
    print(json.dumps({"type": "error", "message": "model_not_found: retired-gpt-model"}))
    sys.exit(1)

print(json.dumps({"type": "thread.started", "thread_id": "thr-fallback-1"}))
print(json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": f"ECHO:{prompt.strip()}|MODEL:{model or 'default'}"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 120, "cached_input_tokens": 10, "output_tokens": 20, "reasoning_output_tokens": 4}}))
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    runner = StubCodexRunner(settings, fake_codex)
    req = RunRequest(
        backend="codex",
        prompt="--leading-dash & %SPECIAL% multiline\nprompt",
        cwd=str(tmp_path),
        model="retired-gpt-model",
    )
    res = await runner.run(req)

    assert res.status == "completed"
    assert res.model == "codex-default"
    assert res.model_notice is not None
    assert res.session_id == "thr-fallback-1"
    assert "--leading-dash & %SPECIAL% multiline\nprompt" in res.response
    assert res.usage is not None and res.usage["total_tokens"] == 140


async def test_job_manager_parallel_resume_cancel_and_persistence(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex_jm.py"
    fake_codex.write_text(
        """
import json, sys, time

args = sys.argv[1:]
prompt = sys.stdin.read().strip()

if "SLEEP" in prompt:
    print(json.dumps({"type": "thread.started", "thread_id": "thr-sleep"}), flush=True)
    time.sleep(30)

is_resume = "resume" in args
tid = args[args.index("resume") + 6] if False else ("thr-resumed" if is_resume else "thr-new-1")
if is_resume:
    # Last two args are <session_id> "-"
    tid = args[-2]

print(json.dumps({"type": "thread.started", "thread_id": tid}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": f"RES:{tid}:{prompt}"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    codex_runner = StubCodexRunner(settings, fake_codex)
    manager = JobManager(settings, codex_runner=codex_runner)

    # 1. Parallel run
    reqs = [
        manager.prepare_request(backend="codex", prompt="task A", cwd=str(tmp_path), label="a"),
        manager.prepare_request(backend="codex", prompt="task B", cwd=str(tmp_path), label="b"),
    ]
    records = await manager.run_parallel(reqs, wait=True, wait_seconds=15)
    assert len(records) == 2
    assert all(r.status == "completed" for r in records)
    assert records[0].session_id == "thr-new-1"

    # 2. Resume using job_id (auto-infers backend, cwd, session_id!)
    follow_req = manager.prepare_request(
        backend=None,
        prompt="follow up",
        resume_from=records[0].job_id,
    )
    assert follow_req.backend == "codex"
    assert follow_req.session_id == "thr-new-1"
    follow_rec = await manager.run(follow_req)
    assert follow_rec.status == "completed"
    assert "RES:thr-new-1:follow up" in follow_rec.response

    # 3. Cancel a long-running background job
    sleep_req = manager.prepare_request(backend="codex", prompt="SLEEP forever", cwd=str(tmp_path))
    sleep_rec = manager.start(sleep_req)
    await asyncio.sleep(0.4)
    cancelled = await manager.cancel(sleep_rec.job_id)
    assert cancelled.status == "cancelled"

    # 4. Usage tracker recorded all 4 jobs
    usage = manager.usage_tracker.summarize(period="session")
    assert usage["totals"]["runs"] == 4
    assert usage["totals"]["completed"] == 3
    assert usage["totals"]["cancelled"] == 1

    # 5. Restarting JobManager loads persisted jobs cleanly
    manager2 = JobManager(settings, codex_runner=codex_runner)
    listed = manager2.list_jobs(limit=10)
    assert len(listed) == 4
    # Can still resume from a persisted job_id after restart!
    resumed_after_restart = manager2.prepare_request(
        backend="codex",
        prompt="after restart",
        resume_from=records[0].job_id,
    )
    assert resumed_after_restart.session_id == "thr-new-1"


async def test_codex_large_jsonl_line_and_large_stdin(tmp_path: Path) -> None:
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


async def test_cancel_immediately_before_execute_and_wait_any_returns_fast(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex_edge.py"
    fake_codex.write_text(
        """
import json, sys, time

prompt = sys.stdin.read().strip()
if "SLOW" in prompt:
    time.sleep(20)
print(json.dumps({"type": "thread.started", "thread_id": "thr-edge"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "DONE"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    settings = _make_settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, fake_codex))

    # Immediate cancel before event loop enters _execute()
    rec_pre = manager.start(manager.prepare_request(backend="codex", prompt="SLOW", cwd=str(tmp_path)))
    cancelled = await manager.cancel(rec_pre.job_id)
    assert cancelled.status == "cancelled"
    assert cancelled.is_terminal

    # wait_mode="any" returns immediately if one requested job is already done
    done_rec = await manager.run(manager.prepare_request(backend="codex", prompt="FAST", cwd=str(tmp_path)))
    slow_rec = manager.start(manager.prepare_request(backend="codex", prompt="SLOW", cwd=str(tmp_path)))

    import time

    t0 = time.monotonic()
    waited = await manager.wait_for([done_rec.job_id, slow_rec.job_id], wait_seconds=15, wait_mode="any")
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0
    assert waited[0].status == "completed"
    await manager.cancel(slow_rec.job_id)
