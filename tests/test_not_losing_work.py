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
from agent_bridge.models import JobRecord, RunResult
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


# --- 4. Cancel keeps "possibly running" ---


@pytest.mark.parametrize("agy_says_still_running", [True, False])
async def test_cancelled_agy_turn_that_reported_remote_work_keeps_the_session_blocked(
    tmp_path: Path, agy_says_still_running: bool,
) -> None:
    signal = (
        "sys.stderr.write('print timeout elapsed while turn is still in progress\\n')\n"
        "sys.stderr.flush()\n"
        if agy_says_still_running
        else ""
    )
    script = tmp_path / "agy_remote.py"
    script.write_text(
        "import json, sys, time\n"
        "if 'models' in sys.argv:\n"
        "    print('gemini-4-neon-high\\tGemini 4 Neon (High)')\n"
        "    sys.exit(0)\n"
        + signal
        + "print(json.dumps({'event': 'init', 'conversation_id': 'conv-rip', "
        "'init': {'model': 'gemini-4-neon-high'}}), flush=True)\n"
        "print(json.dumps({'event': 'step_update', 'step_update': {'conversation_id': 'conv-rip', "
        "'step_index': 1, 'state': 'ACTIVE', 'step_type': 'agent_response', 'text_delta': 'working'}}), flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    manager = JobManager(settings, neon_runner=StubAgyRunner(settings, script))
    try:
        job = await manager.start_async(manager.prepare_request(backend="neon", prompt="go"))
        async with asyncio.timeout(15):
            while manager._local_jobs[job.job_id].last_activity != "agent_response (ACTIVE)":
                await asyncio.sleep(0.02)
        await asyncio.sleep(0.5)  # let the stderr reader take the line in
        await manager.cancel(job.job_id)
        stored = manager.store.get_job(job.job_id)
        assert stored is not None and stored.status == "cancelled"
        block = manager.store.get_session_safety_block("neon", "conv-rip")
        if not agy_says_still_running:
            assert stored.provider_state == "stopped"
            assert block is None
            return
        # As on the timeout path: the session stays blocked, and a resume is refused until confirmed.
        assert stored.provider_state == "possibly_running"
        assert block == {"blocking_job_id": job.job_id, "provider_state": "possibly_running"}
        resumed = await manager.run(manager.prepare_request(backend="neon", prompt="again", resume_from=job.job_id))
        assert resumed.status == "failed"
        assert resumed.failure_reason == "prior_turn_uncertain"
    finally:
        await manager.shutdown()


class _AgyRunnerEndingAsTheCancelComes(NeonRunner):
    """Plays a runner whose CLI had ended with agy's remote in-progress signal as the cancel came."""

    def __init__(self, settings: Settings, started: asyncio.Event) -> None:
        super().__init__(settings)
        self._started = started

    async def run(self, request, *, deadline=None, prior_watermark=None, on_session=None,  # type: ignore[override]
                  on_activity=None, on_model=None, on_provider_start=None, on_attempt=None) -> RunResult:
        await on_provider_start()
        on_session("conv-late")
        self._started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass  # its own cleanup is done, and it reports what agy had said
        return RunResult(
            status="timed_out", session_id="conv-late", failure_reason="provider_timeout_still_running",
            provider_state="possibly_running", provider_invoked=True, error="turn is still in progress",
        )


async def test_cancel_keeps_possibly_running_from_an_agy_result_that_ended_as_the_cancel_came(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    started = asyncio.Event()
    manager = JobManager(settings, neon_runner=_AgyRunnerEndingAsTheCancelComes(settings, started))
    try:
        job = await manager.start_async(manager.prepare_request(backend="neon", prompt="go"))
        async with asyncio.timeout(10):
            await started.wait()
            while manager.store.get_job(job.job_id).session_id != "conv-late":
                await asyncio.sleep(0.02)
        await manager.cancel(job.job_id)
        stored = manager.store.get_job(job.job_id)
        assert stored is not None and stored.status == "cancelled"
        assert stored.provider_state == "possibly_running"
        assert manager.store.get_session_safety_block("neon", "conv-late") == {
            "blocking_job_id": job.job_id, "provider_state": "possibly_running",
        }
    finally:
        await manager.shutdown()


# --- 5. Fenced final writes ---


def _quick_codex(tmp_path: Path, *, thread_id: str = "thr-quick", sleep_for: dict[str, float] | None = None) -> Path:
    """A Codex stand-in that starts `thread_id`, works for `sleep_for[prompt]` seconds, then answers."""
    script = tmp_path / "codex_quick.py"
    script.write_text(
        "import json, sys, time\n"
        "prompt = sys.stdin.read().strip()\n"
        f"print(json.dumps({{'type': 'thread.started', 'thread_id': {thread_id!r}}}), flush=True)\n"
        f"time.sleep({sleep_for or {}!r}.get(prompt, 0))\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'OK:' + prompt}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 2}}), flush=True)\n",
        encoding="utf-8",
    )
    return script


def _claims_of(manager: JobManager, job_id: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(manager.store.db_path)
    try:
        return conn.execute(
            "SELECT backend, session_id FROM session_claims WHERE job_id = ?", (job_id,)
        ).fetchall()
    finally:
        conn.close()


async def test_finalize_never_writes_a_different_outcome_over_a_stored_result(tmp_path: Path) -> None:
    from dataclasses import replace

    from agent_bridge.storage import BridgeStore

    store = BridgeStore(tmp_path / "state")
    store.register_owner("win", 1)
    record = JobRecord(
        job_id="codex-0000feed", owner_id="win", backend="codex", cwd=str(tmp_path), mode="read_only",
        requested_model=None, model="m", prompt_preview="p", status="running", started_at=time.time(),
        provider_invoked=True,
    )
    store.insert_job(record)
    done = replace(record, status="completed", response="the answer", finished_at=time.time(), duration_seconds=1.0)
    assert store.finalize_job(done) == "written"
    late = replace(
        record, status="cancelled", failure_reason="cancelled", provider_state="stopped", error="Job cancelled.",
        finished_at=time.time(), duration_seconds=2.0,
    )
    assert store.finalize_job(late) == "kept"
    stored = store.get_job(record.job_id)
    assert stored is not None
    assert (stored.status, stored.failure_reason, stored.response) == ("completed", None, "the answer")
    # The same outcome may be written again, as a retried write would.
    assert store.finalize_job(done) == "written"


async def test_a_final_write_that_fails_is_kept_and_retried_until_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, _quick_codex(tmp_path)))
    manager._finalize_error_backoff = (0.05, 0.2)
    real_finalize = manager.store.finalize_job
    failures = 0

    def finalize_job(record: JobRecord, **kwargs: object) -> str:
        nonlocal failures
        if failures < 3:
            failures += 1
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_finalize(record, **kwargs)

    monkeypatch.setattr(manager.store, "finalize_job", finalize_job)
    try:
        with caplog.at_level(logging.WARNING, logger="agent_bridge.jobs"):
            rec = await manager.run(manager.prepare_request(backend="codex", prompt="go"))
            # Readable at once from this window, whether or not the row has landed yet.
            assert (rec.status, rec.response) == ("completed", "OK:go")
            async with asyncio.timeout(10):
                while manager.store.get_job(rec.job_id).status != "completed":
                    await asyncio.sleep(0.05)
        assert failures == 3
        assert manager.store.get_job(rec.job_id).response == "OK:go"
        assert _claims_of(manager, rec.job_id) == []  # its session claim went with the write
        assert "Could not store the result of job" in caplog.text and "tried again" in caplog.text
    finally:
        await manager.shutdown()


async def test_a_final_write_still_failing_is_tried_once_more_at_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, _quick_codex(tmp_path)))
    manager._finalize_error_backoff = (600.0, 600.0)  # only the retry at shutdown can land it
    real_finalize = manager.store.finalize_job
    broken = True

    def finalize_job(record: JobRecord, **kwargs: object) -> str:
        if broken:
            raise sqlite3.DatabaseError("disk I/O error")
        return real_finalize(record, **kwargs)

    monkeypatch.setattr(manager.store, "finalize_job", finalize_job)
    rec = await manager.run(manager.prepare_request(backend="codex", prompt="go"))
    assert rec.status == "completed"
    assert manager.store.get_job(rec.job_id).status == "running"  # not stored yet
    broken = False
    await asyncio.wait_for(manager.shutdown(), timeout=30)
    stored = manager.store.get_job(rec.job_id)
    assert stored is not None and (stored.status, stored.response) == ("completed", "OK:go")


async def test_a_turn_revealing_a_session_another_live_job_holds_is_stopped_and_does_not_take_it(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    script = _quick_codex(tmp_path, thread_id="thr-shared", sleep_for={"A": 5.0, "B": 5.0})
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, script))
    try:
        first = await manager.start_async(manager.prepare_request(backend="codex", prompt="A"))
        async with asyncio.timeout(15):
            while manager.store.get_job(first.job_id).session_id != "thr-shared":
                await asyncio.sleep(0.05)
        second = await manager.run(manager.prepare_request(backend="codex", prompt="B"))
        assert second.status == "failed"
        assert second.failure_reason == "cli_error"
        assert second.error is not None and first.job_id in second.error and "thr-shared" in second.error
        assert second.session_id is None
        assert manager.store.get_job(second.job_id).session_id is None
        assert (second.duration_seconds or 0) < 4.0  # stopped, not left to run its five seconds
        # The holder is untouched: it keeps its claim while it runs, and ends normally.
        assert _claims_of(manager, first.job_id) == [("codex", "thr-shared")]
        [done] = await manager.wait_for([first.job_id], wait_seconds=20)
        assert (done.status, done.session_id, done.response) == ("completed", "thr-shared", "OK:A")
    finally:
        await manager.shutdown()
