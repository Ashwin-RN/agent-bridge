"""Phase 1, not losing work: batch receipts, cancels that finish, broken streams, fenced final writes."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
from mcp import Client

from agent_bridge.config import Settings
from agent_bridge.jobs import JobManager
from agent_bridge.models import JobRecord
from agent_bridge.nesting import NOT_NESTED
from agent_bridge.process_control import is_pid_alive
from agent_bridge.runners import CodexRunner, NeonRunner
from agent_bridge.server import create_server


def _settings(tmp_path: Path, **overrides: object) -> Settings:
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


class StubCodexRunner(CodexRunner):
    def __init__(self, settings: Settings, script_path: Path) -> None:
        self._script_path = script_path
        super().__init__(settings)

    def resolve_command(self) -> list[str]:
        return [sys.executable, str(self._script_path)]


class StubAgyRunner(NeonRunner):
    read_only_guard = ""

    def __init__(self, settings: Settings, script_path: Path) -> None:
        self._script_path = script_path
        super().__init__(settings)

    def resolve_command(self) -> list[str]:
        return [sys.executable, str(self._script_path)]


def _holding_codex(tmp_path: Path) -> Path:
    """A Codex stand-in that records its pid under its prompt's name, starts a thread, then works for a minute."""
    script = tmp_path / "codex_hold.py"
    script.write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "prompt = sys.stdin.read().strip()\n"
        f"Path({str(tmp_path)!r}, 'pid-' + prompt).write_text(str(os.getpid()))\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'thr-' + prompt}), flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    return script


def _row_for_prompt(manager: JobManager, prompt: str) -> JobRecord:
    rows = [r for r in manager.store.list_jobs(limit=50) if r.prompt_preview == prompt]
    assert len(rows) == 1, rows
    return rows[0]


async def _wait_for_file(path: Path, timeout: float = 15.0) -> None:
    async with asyncio.timeout(timeout):
        while not path.exists():
            await asyncio.sleep(0.02)


# --- 1. Batch receipts ---


async def test_batch_that_fails_to_start_a_job_cancels_the_jobs_it_started_and_names_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, _holding_codex(tmp_path)))
    mcp, _ = create_server(settings=settings, manager=manager, nesting=NOT_NESTED)
    real_insert = manager.store.insert_job
    inserts = 0

    def insert_job(record: JobRecord) -> None:
        nonlocal inserts
        inserts += 1
        if inserts == 2:
            # Fail the second job's row only once the first job's CLI is really running.
            deadline = time.monotonic() + 15
            while not (tmp_path / "pid-one").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            raise sqlite3.OperationalError("disk I/O error")
        real_insert(record)

    monkeypatch.setattr(manager.store, "insert_job", insert_job)
    try:
        tasks = [{"backend": "codex", "prompt": p} for p in ("one", "two", "three")]
        with pytest.raises(Exception) as excinfo:
            await mcp.call_tool("run_parallel", {"tasks": tasks, "cwd": str(tmp_path), "wait": False})
        first = _row_for_prompt(manager, "one")
        # The host's error names the job that had started, and says what became of it.
        assert f"{first.job_id} (cancelled)" in str(excinfo.value)
        assert "Starting task 2 of 3 failed" in str(excinfo.value)
        assert "disk I/O error" in str(excinfo.value)
        assert first.status == "cancelled"
        assert not is_pid_alive(int((tmp_path / "pid-one").read_text()))
        assert inserts == 2  # the third task was never submitted
        assert len(manager.store.list_jobs(limit=50)) == 1
    finally:
        await manager.shutdown()


async def test_batch_cancelled_while_starting_its_jobs_cancels_the_ones_already_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, _holding_codex(tmp_path)))
    real_insert = manager.store.insert_job
    second_insert_entered = threading.Event()
    release = threading.Event()
    inserts = 0

    def insert_job(record: JobRecord) -> None:
        nonlocal inserts
        inserts += 1
        if inserts == 2:
            second_insert_entered.set()
            release.wait(20)
        real_insert(record)

    monkeypatch.setattr(manager.store, "insert_job", insert_job)
    requests = [manager.prepare_request(backend="codex", prompt=p) for p in ("one", "two")]
    batch = asyncio.create_task(manager.run_parallel(requests, wait=False))
    try:
        await _wait_for_file(tmp_path / "pid-one")
        async with asyncio.timeout(15):
            while not second_insert_entered.is_set():
                await asyncio.sleep(0.02)
        batch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await batch
        first = _row_for_prompt(manager, "one")
        assert first.status == "cancelled"
        assert not is_pid_alive(int((tmp_path / "pid-one").read_text()))
    finally:
        release.set()
        await manager.shutdown()


# --- 2. A cancel that finishes ---


@pytest.mark.parametrize(
    "tool,args",
    [
        ("run_agent", {"backend": "codex", "prompt": "hold"}),
        ("run_parallel", {"tasks": [{"backend": "codex", "prompt": "hold"}]}),
        ("review_code", {"backends": ["codex"]}),
    ],
)
async def test_host_cancel_of_a_waiting_call_stops_the_job_through_the_mcp_sdk(
    tmp_path: Path, tool: str, args: dict,
) -> None:
    """A host that gives up on a waiting call sends `notifications/cancelled` for it.

    The SDK then cancels the request's scope, and that scope interrupts every later await the handler
    makes. `mode="legacy"` makes the in-process client speak JSON-RPC over memory streams, as a host does
    over stdio. The default mode calls the handler directly in the caller's task, and would not show it.
    """
    pid_file = tmp_path / "pid.txt"
    script = tmp_path / "codex_wait.py"
    script.write_text(
        "import json, os, sys, time\n"
        f"tmp = {str(pid_file)!r} + '.tmp'\n"
        "open(tmp, 'w').write(str(os.getpid()))\n"
        f"os.replace(tmp, {str(pid_file)!r})\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'thr-wait'}), flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path, default_timeout_seconds=120)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, script))
    mcp, _ = create_server(settings=settings, manager=manager, nesting=NOT_NESTED)
    async with Client(mcp, mode="legacy") as client:
        call = asyncio.create_task(client.call_tool(tool, {**args, "cwd": str(tmp_path)}))
        await _wait_for_file(pid_file)
        pid = int(pid_file.read_text())
        [job] = manager.store.list_jobs(limit=5)
        assert job.status == "running"
        call.cancel()  # the client sends notifications/cancelled for this request
        with pytest.raises(asyncio.CancelledError):
            await call
        # Still inside the session, so the bridge's shutdown, which cancels every job, has not run.
        async with asyncio.timeout(10):
            while manager.store.get_job(job.job_id).status != "cancelled":
                await asyncio.sleep(0.05)
        assert not is_pid_alive(pid)
        assert manager.store.get_job(job.job_id).provider_state == "stopped"


# --- 3. A broken stream is a failure ---

CUT_SHORT = "the CLI's output ended without its final event"


async def test_codex_clean_exit_without_turn_completed_is_a_failure_not_retried(tmp_path: Path) -> None:
    calls = tmp_path / "calls.log"
    script = tmp_path / "codex_cut.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write('CALL\\n')\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'thr-cut'}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'half an answer'}}), flush=True)\n"
        # Prose that would read as a transient error, which the bridge would retry by itself.
        "sys.stderr.write('upstream said 503 Service Unavailable\\n')\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, script))
    try:
        rec = await manager.run(manager.prepare_request(backend="codex", prompt="go"))
        assert rec.status == "failed"
        assert rec.failure_reason == "cli_error"
        assert rec.error is not None and rec.error.startswith(CUT_SHORT) and "turn.completed" in rec.error
        assert rec.response == "half an answer"
        assert rec.session_id == "thr-cut"
        assert calls.read_text().splitlines() == ["CALL"]
    finally:
        await manager.shutdown()


async def test_codex_turn_failed_with_no_message_is_a_failure(tmp_path: Path) -> None:
    script = tmp_path / "codex_turn_failed.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'thr-tf'}), flush=True)\n"
        "print(json.dumps({'type': 'turn.failed'}), flush=True)\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, script))
    try:
        rec = await manager.run(manager.prepare_request(backend="codex", prompt="go"))
        assert rec.status == "failed"
        assert rec.error is not None and "turn failed" in rec.error
    finally:
        await manager.shutdown()


async def test_codex_builtin_review_still_completes_without_turn_completed(tmp_path: Path) -> None:
    """`codex exec review` reports no token counts and may print its review as plain text."""
    script = tmp_path / "codex_review.py"
    script.write_text(
        "import sys\n"
        "assert sys.argv[1:3] == ['exec', 'review'], sys.argv\n"
        "print('Review: no issues found in the uncommitted changes.', flush=True)\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, script))
    try:
        rec = await manager.run(manager.prepare_request(backend="codex", prompt="", review_scope="uncommitted"))
        assert rec.status == "completed", rec.error
        assert rec.response == "Review: no issues found in the uncommitted changes."
    finally:
        await manager.shutdown()


@pytest.mark.parametrize(
    "final_line",
    [
        None,  # the stream simply stops after its steps
        {"event": "result", "result": {"conversation_id": "conv-cut", "response": "half"}},  # no status
    ],
)
async def test_agy_clean_exit_without_a_result_carrying_a_status_is_a_failure(
    tmp_path: Path, final_line: dict | None,
) -> None:
    import json

    lines = [
        {"event": "init", "conversation_id": "conv-cut", "init": {"model": "gemini-4-neon-high"}},
        {"event": "step_update", "step_update": {
            "conversation_id": "conv-cut", "step_index": 1, "state": "DONE", "step_type": "agent_response",
            "text_delta": "half", "usage": {"input_tokens": 7, "output_tokens": 3},
        }},
    ]
    if final_line is not None:
        lines.append(final_line)
    script = tmp_path / "agy_cut.py"
    script.write_text(
        "import sys\n"
        "if 'models' in sys.argv:\n"
        "    print('gemini-4-neon-high\\tGemini 4 Neon (High)')\n"
        "    sys.exit(0)\n"
        + "".join(f"print({json.dumps(json.dumps(line))}, flush=True)\n" for line in lines),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    manager = JobManager(settings, neon_runner=StubAgyRunner(settings, script))
    try:
        rec = await manager.run(manager.prepare_request(backend="neon", prompt="go"))
        assert rec.status == "failed"
        assert rec.failure_reason == "cli_error"
        assert rec.error is not None and rec.error.startswith(CUT_SHORT)
        assert rec.response == "half"
        assert rec.usage_status == "partial"
    finally:
        await manager.shutdown()
