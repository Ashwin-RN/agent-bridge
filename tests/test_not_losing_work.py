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


# --- 1. A run cancelled while its job is being started ---


async def test_run_cancelled_while_its_job_row_is_written_leaves_no_queued_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, _holding_codex(tmp_path)))
    real_insert = manager.store.insert_job
    insert_entered = threading.Event()
    insert_done = threading.Event()
    release = threading.Event()

    def insert_job(record: JobRecord) -> None:
        insert_entered.set()
        release.wait(20)
        real_insert(record)
        insert_done.set()

    monkeypatch.setattr(manager.store, "insert_job", insert_job)
    call = asyncio.create_task(manager.run(manager.prepare_request(backend="codex", prompt="two"), wait_seconds=0))
    try:
        async with asyncio.timeout(15):
            while not insert_entered.is_set():
                await asyncio.sleep(0.02)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        # The job's row lands only now, after the cancel: it must not stay behind as a queued orphan,
        # and its CLI must never start.
        release.set()
        async with asyncio.timeout(15):
            while not insert_done.is_set():
                await asyncio.sleep(0.02)
            while any(r.prompt_preview == "two" for r in manager.store.list_jobs(limit=50)):
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        assert not (tmp_path / "pid-two").exists()
        assert manager.store.list_jobs(limit=50) == []
    finally:
        release.set()
        await manager.shutdown()


# --- 2. A cancel that finishes ---


@pytest.mark.parametrize(
    "tool,args",
    [
        ("run", {"backend": "codex", "prompt": "hold", "wait_seconds": 60}),
        ("run", {"backend": "codex", "review": {"scope": "uncommitted"}, "wait_seconds": 60}),
    ],
    ids=["run", "review"],
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


# --- Receipts kept under the cancel handler until the reply is assembled ---


async def test_run_cancelled_during_its_last_lookup_cancels_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, _holding_codex(tmp_path)))
    real_get = manager.get_async
    entered = asyncio.Event()
    first_call = True

    async def get_async(job_id: str) -> JobRecord:
        nonlocal first_call
        if first_call:
            first_call = False
            entered.set()
            await asyncio.Event().wait()  # the caller is cancelled while this lookup waits
        return await real_get(job_id)

    monkeypatch.setattr(manager, "get_async", get_async)
    try:
        call = asyncio.create_task(manager.run(manager.prepare_request(backend="codex", prompt="one"), wait_seconds=0))
        await _wait_for_file(tmp_path / "pid-one")
        async with asyncio.timeout(10):
            await entered.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        job = _row_for_prompt(manager, "one")
        async with asyncio.timeout(10):
            while manager.store.get_job(job.job_id).status != "cancelled":
                await asyncio.sleep(0.05)
        assert not is_pid_alive(int((tmp_path / "pid-one").read_text()))
    finally:
        await manager.shutdown()


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


def _agy_signalling_remote_work(tmp_path: Path, *, conversation: str = "conv-rip") -> Path:
    """An agy stand-in that says its turn goes on at the server, then keeps working."""
    script = tmp_path / "agy_remote_signal.py"
    script.write_text(
        "import json, sys, time\n"
        "if 'models' in sys.argv:\n"
        "    print('gemini-4-neon-high\\tGemini 4 Neon (High)')\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('print timeout elapsed while turn is still in progress\\n')\n"
        "sys.stderr.flush()\n"
        f"print(json.dumps({{'event': 'init', 'conversation_id': {conversation!r}, "
        "'init': {'model': 'gemini-4-neon-high'}}), flush=True)\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'conversation_id': {conversation!r}, "
        "'step_index': 1, 'state': 'DONE', 'step_type': 'planner_response', "
        "'usage': {'input_tokens': 7, 'output_tokens': 3}}}), flush=True)\n"
        f"print(json.dumps({{'event': 'step_update', 'step_update': {{'conversation_id': {conversation!r}, "
        "'step_index': 2, 'state': 'ACTIVE', 'step_type': 'agent_response', 'text_delta': 'working'}}), flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    return script


async def test_a_second_cancel_from_shutdown_does_not_cut_the_first_cancel_short(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = JobManager(settings, neon_runner=StubAgyRunner(settings, _agy_signalling_remote_work(tmp_path)))
    job = await manager.start_async(manager.prepare_request(backend="neon", prompt="go"))
    async with asyncio.timeout(15):
        while manager._local_jobs[job.job_id].last_activity != "agent_response (ACTIVE)":
            await asyncio.sleep(0.02)
    await asyncio.sleep(0.5)  # let the stderr reader take the line in
    # Slow the job's own cleanup, so that a second cancel has time to land inside it.
    tracker = manager._trackers[job.job_id]
    real_terminate = tracker.terminate_all_and_verify
    cleanup_entered = asyncio.Event()

    async def slow_terminate() -> bool:
        stopped = await real_terminate()
        cleanup_entered.set()
        await asyncio.sleep(0.5)
        return stopped

    tracker.terminate_all_and_verify = slow_terminate  # type: ignore[method-assign]
    first = asyncio.create_task(manager.cancel(job.job_id))
    async with asyncio.timeout(15):
        await cleanup_entered.wait()
    await manager.shutdown()  # cancels every job it still sees running: this one, a second time
    await first
    stored = manager.store.get_job(job.job_id)
    assert stored is not None and stored.status == "cancelled"
    # The job's own cleanup ran to its end: only it keeps the usage the CLI had streamed.
    assert stored.usage is not None and stored.usage["total_tokens"] == 10
    assert stored.provider_state == "possibly_running"
    assert manager.store.get_session_safety_block("neon", "conv-rip") == {
        "blocking_job_id": job.job_id, "provider_state": "possibly_running",
    }


class _AgyRunnerEndingAsTheCancelComes(StubAgyRunner):
    """The real agy runner, its stream read and its process gone, held at the moment it returns, so that
    the cancel lands just as its result comes back."""

    def __init__(self, settings: Settings, script_path: Path, returned: asyncio.Event) -> None:
        super().__init__(settings, script_path)
        self._returned = returned
        self.result: RunResult | None = None

    async def run(self, request, **kwargs) -> RunResult:  # type: ignore[override]
        self.result = await super().run(request, **kwargs)
        self._returned.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass  # the result is in hand; the cancel only reaches the caller
        return self.result


async def test_cancel_keeps_possibly_running_from_an_agy_result_that_ended_as_the_cancel_came(
    tmp_path: Path,
) -> None:
    # agy says the turn goes on at the server, and its CLI exits 3, as it does after its print timeout.
    script = tmp_path / "agy_exit3.py"
    script.write_text(
        "import json, os, sys\n"
        "if 'models' in sys.argv:\n"
        "    print('gemini-4-neon-high\\tGemini 4 Neon (High)')\n"
        "    sys.exit(0)\n"
        f"open({str(tmp_path / 'pid-agy')!r}, 'w').write(str(os.getpid()))\n"
        "print(json.dumps({'event': 'init', 'conversation_id': 'conv-late', "
        "'init': {'model': 'gemini-4-neon-high'}}), flush=True)\n"
        "sys.stderr.write('print timeout elapsed while turn is still in progress\\n')\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    returned = asyncio.Event()
    runner = _AgyRunnerEndingAsTheCancelComes(settings, script, returned)
    manager = JobManager(settings, neon_runner=runner)
    try:
        job = await manager.start_async(manager.prepare_request(backend="neon", prompt="go"))
        async with asyncio.timeout(15):
            await returned.wait()
            while manager.store.get_job(job.job_id).session_id != "conv-late":
                await asyncio.sleep(0.02)
        # The runner's own reading of the stream, unaided.
        assert runner.result is not None
        assert (runner.result.failure_reason, runner.result.provider_state) == (
            "provider_timeout_still_running", "possibly_running",
        )
        assert not is_pid_alive(int((tmp_path / "pid-agy").read_text()))
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


def _quick_codex(
    tmp_path: Path, *, thread_id: str = "thr-quick", sleep_for: dict[str, float] | None = None,
    record_pids: bool = False,
) -> Path:
    """A Codex stand-in that starts `thread_id`, works for `sleep_for[prompt]` seconds, then answers.

    With `record_pids`, each run first writes its pid to `pid-<prompt>` in `tmp_path`.
    """
    script = tmp_path / "codex_quick.py"
    pid_line = (
        f"import os; from pathlib import Path; Path({str(tmp_path)!r}, 'pid-' + prompt).write_text(str(os.getpid()))\n"
        if record_pids
        else ""
    )
    script.write_text(
        "import json, sys, time\n"
        "prompt = sys.stdin.read().strip()\n"
        + pid_line +
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


def _usage_tokens(store: object, job_id: str) -> list[int | None]:
    conn = sqlite3.connect(store.db_path)  # type: ignore[attr-defined]
    try:
        return [r[0] for r in conn.execute("SELECT total_tokens FROM usage_events WHERE job_id = ?", (job_id,))]
    finally:
        conn.close()


async def test_finalize_never_rewrites_a_stored_result_whatever_its_status(tmp_path: Path) -> None:
    from dataclasses import replace

    from agent_bridge.storage import BridgeStore

    store = BridgeStore(tmp_path / "state")
    store.register_owner("win", 1)
    record = replace(_running_job("neon-0000feed", "win", tmp_path), backend="neon", session_id="conv-fence")
    store.insert_job(record)
    done = replace(
        record, status="completed", response="the answer", finished_at=time.time(), duration_seconds=1.0,
        usage={"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}, usage_status="exact",
    )
    newer = {"input_tokens": 300, "output_tokens": 30, "total_tokens": 330}
    assert store.finalize_job(done, watermark_cumulative=newer, watermark_turns=2) == "written"

    late_cancel = replace(
        record, status="cancelled", failure_reason="cancelled", provider_state="stopped", error="Job cancelled.",
        finished_at=time.time(), duration_seconds=2.0,
    )
    # The same status, with an older reply, other usage and an older checkpoint.
    stale_done = replace(
        done, response="an older answer", usage={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
    )
    older = {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}
    outcomes = [
        store.finalize_job(late_cancel),
        store.finalize_job(stale_done, watermark_cumulative=older, watermark_turns=1),
    ]
    stored = store.get_job(record.job_id)
    assert stored is not None
    assert (stored.status, stored.failure_reason, stored.response) == ("completed", None, "the answer")
    assert stored.usage is not None and stored.usage["total_tokens"] == 100
    assert _usage_tokens(store, record.job_id) == [100]
    watermark = store.get_session_watermark("neon", "conv-fence")
    assert watermark is not None and watermark["num_turns"] == 2  # not rolled back
    assert outcomes == ["kept", "kept"]


async def test_finalize_of_a_row_that_is_gone_records_usage_and_touches_no_session_state(tmp_path: Path) -> None:
    from dataclasses import replace

    from agent_bridge.storage import BridgeStore

    store = BridgeStore(tmp_path / "state")
    store.register_owner("win", 1)
    # The session already has a checkpoint from an earlier turn.
    earlier = replace(_running_job("neon-0000aaaa", "win", tmp_path), backend="neon", session_id="conv-gone")
    store.insert_job(earlier)
    checkpoint = {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}
    store.finalize_job(
        replace(earlier, status="completed", finished_at=time.time(), duration_seconds=1.0),
        watermark_cumulative=checkpoint, watermark_turns=1,
    )
    # A job whose row is not there (pruned), ending with an uncertain state on that session.
    gone = replace(
        _running_job("neon-0000bbbb", "win", tmp_path), backend="neon", session_id="conv-gone",
        status="timed_out", failure_reason="timeout", provider_state="possibly_running",
        finished_at=time.time(), duration_seconds=3.0,
        usage={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}, usage_status="partial",
    )
    outcome = store.finalize_job(gone)
    assert store.get_session_watermark("neon", "conv-gone") is not None  # not deleted
    assert store.get_session_safety_block("neon", "conv-gone") is None  # not written
    assert _usage_tokens(store, "neon-0000bbbb") == [25]  # the run's usage still counts
    assert outcome == "missing"


async def test_a_final_write_that_fails_is_kept_and_retried_until_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, _quick_codex(tmp_path)))
    manager._finalize_error_backoff = (0.05, 0.2)
    real_finalize = manager.store.finalize_job
    failures = 0
    broken = True

    def finalize_job(record: JobRecord, **kwargs: object) -> str:
        nonlocal failures
        if broken:
            failures += 1
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_finalize(record, **kwargs)

    monkeypatch.setattr(manager.store, "finalize_job", finalize_job)
    try:
        with caplog.at_level(logging.WARNING, logger="agent_bridge.jobs"):
            rec = await manager.run(manager.prepare_request(backend="codex", prompt="go"))
            # Readable at once from this window, whether or not the row has landed yet.
            assert (rec.status, rec.response) == ("completed", "OK:go")
            # While the write keeps failing, and after more than one retry: the row still reads running,
            # and the session stays claimed, so no other turn can take it meanwhile.
            async with asyncio.timeout(10):
                while failures < 3:
                    await asyncio.sleep(0.02)
            assert manager.store.get_job(rec.job_id).status == "running"
            assert _claims_of(manager, rec.job_id) == [("codex", "thr-quick")]
            assert (await manager.get_async(rec.job_id)).status == "completed"
            broken = False
            async with asyncio.timeout(10):
                while manager.store.get_job(rec.job_id).status != "completed":
                    await asyncio.sleep(0.05)
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


async def test_a_final_write_that_never_lands_gets_one_attempt_at_shutdown_and_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, _quick_codex(tmp_path)))
    manager._finalize_error_backoff = (600.0, 600.0)
    attempts = 0

    def finalize_job(record: JobRecord, **kwargs: object) -> str:
        nonlocal attempts
        attempts += 1
        raise sqlite3.DatabaseError("disk I/O error")

    monkeypatch.setattr(manager.store, "finalize_job", finalize_job)
    rec = await manager.run(manager.prepare_request(backend="codex", prompt="go"))
    before = attempts
    with caplog.at_level(logging.WARNING, logger="agent_bridge.jobs"):
        started = time.monotonic()
        await manager.shutdown(flush_timeout=20)
        took = time.monotonic() - started
    assert took < 10  # one last attempt, not a retry loop for the whole flush timeout
    assert attempts == before + 1
    assert "at shutdown either" in caplog.text
    assert f"could not be stored ({rec.job_id})" in caplog.text
    assert manager.store.get_job(rec.job_id).status == "running"  # left for the next window to recover


async def test_a_final_write_held_up_by_a_lock_gets_one_last_attempt_at_shutdown_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, _quick_codex(tmp_path)))
    manager._finalize_retry_seconds = 0.3  # run() answers from memory soon; the write keeps being retried
    lock_waits: list[float] = []
    flushing = False

    def finalize_job(record: JobRecord, **kwargs: object) -> str:
        if flushing:
            lock_waits.append(float(kwargs["lock_timeout"]))  # type: ignore[arg-type]
        raise sqlite3.OperationalError("database is locked")  # another writer holds the database

    monkeypatch.setattr(manager.store, "finalize_job", finalize_job)
    rec = await manager.run(manager.prepare_request(backend="codex", prompt="go"))
    assert rec.status == "completed"
    flushing = True
    with caplog.at_level(logging.WARNING, logger="agent_bridge.jobs"):
        started = time.monotonic()
        await manager.shutdown(flush_timeout=20)
        took = time.monotonic() - started
    assert took < 10  # not the whole flush timeout
    # At most the attempt already under way when shutdown began, then the one last attempt, which may
    # wait a few seconds for the lock.
    assert 1 <= len(lock_waits) <= 2 and lock_waits[-1] == manager._shutdown_write_lock_seconds
    assert "at shutdown either" in caplog.text and f"could not be stored ({rec.job_id})" in caplog.text


async def test_a_turn_revealing_a_session_another_live_job_holds_is_stopped_and_does_not_take_it(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    script = _quick_codex(tmp_path, thread_id="thr-shared", sleep_for={"A": 5.0, "B": 5.0}, record_pids=True)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, script))
    try:
        first = await manager.start_async(manager.prepare_request(backend="codex", prompt="A"))
        async with asyncio.timeout(15):
            while manager.store.get_job(first.job_id).session_id != "thr-shared":
                await asyncio.sleep(0.05)
        started = time.monotonic()
        second = await manager.run(manager.prepare_request(backend="codex", prompt="B"))
        took = time.monotonic() - started  # wall clock to the answer, cleanup included
        assert second.status == "failed"
        assert second.failure_reason == "cli_error"
        assert second.error is not None and first.job_id in second.error and "thr-shared" in second.error
        assert second.session_id is None
        assert manager.store.get_job(second.job_id).session_id is None
        assert took < 4.0  # stopped, not left to run its five seconds
        # By the time the answer is back, its CLI is gone, and the row says it was seen to stop.
        assert not is_pid_alive(int((tmp_path / "pid-B").read_text()))
        assert manager.store.get_job(second.job_id).provider_state == "stopped"
        # The holder is untouched: it keeps its claim while it runs, and ends normally.
        assert _claims_of(manager, first.job_id) == [("codex", "thr-shared")]
        [done] = await manager.wait_for([first.job_id], wait_seconds=20)
        assert (done.status, done.session_id, done.response) == ("completed", "thr-shared", "OK:A")
    finally:
        await manager.shutdown()


def _give_claim(manager: JobManager, backend: str, session_id: str, job_id: str, owner_id: str) -> None:
    conn = sqlite3.connect(manager.store.db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO session_claims (backend, session_id, job_id, owner_id, claimed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (backend, session_id, job_id, owner_id, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


async def test_a_revealed_session_whose_holder_window_has_died_is_taken_and_its_block_stays(tmp_path: Path) -> None:
    from dataclasses import replace

    dead_pid = 999_999_991
    settings = _settings(tmp_path)
    manager = JobManager(
        settings,
        codex_runner=StubCodexRunner(settings, _quick_codex(tmp_path, thread_id="thr-orphan")),
        is_pid_alive_fn=lambda pid: pid != dead_pid,
    )
    try:
        # A window that died while its turn held the session; added after this manager started, so its own
        # start-up recovery has not seen it.
        manager.store.register_owner("dead-win", dead_pid)
        manager.store.insert_job(replace(_running_job("codex-0deadbee", "dead-win", tmp_path), session_id="thr-orphan"))
        _give_claim(manager, "codex", "thr-orphan", "codex-0deadbee", "dead-win")

        rec = await manager.run(manager.prepare_request(backend="codex", prompt="go"))
        assert (rec.status, rec.session_id) == ("completed", "thr-orphan"), rec.error  # not refused
        assert manager.store.get_job("codex-0deadbee").failure_reason == "owner_exited"
        # The dead turn may still be going at the provider: its block stays, for the user to confirm.
        assert manager.store.get_session_safety_block("codex", "thr-orphan") == {
            "blocking_job_id": "codex-0deadbee", "provider_state": "unknown",
        }
    finally:
        await manager.shutdown()


async def test_a_collision_seen_only_at_the_final_write_is_stored_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    script = _quick_codex(tmp_path, thread_id="thr-shared", sleep_for={"A": 6.0})
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, script))
    try:
        first = await manager.start_async(manager.prepare_request(backend="codex", prompt="A"))
        async with asyncio.timeout(15):
            while manager.store.get_job(first.job_id).session_id != "thr-shared":
                await asyncio.sleep(0.05)
        real_update = manager.store.update_job_progress

        def update_job_progress(job_id: str, **kwargs: object) -> None:
            if kwargs.get("session_id") == "thr-shared" and job_id != first.job_id:
                time.sleep(1.0)  # the claim check is still waiting on SQLite when the run ends
                raise sqlite3.OperationalError("database is locked")
            real_update(job_id, **kwargs)

        monkeypatch.setattr(manager.store, "update_job_progress", update_job_progress)
        second = await manager.run(manager.prepare_request(backend="codex", prompt="B"))
        assert (second.status, second.failure_reason) == ("failed", "cli_error")
        assert second.error is not None and first.job_id in second.error and "thr-shared" in second.error
        assert "had ended completed" in second.error
        assert manager.store.get_job(second.job_id).session_id is None
        assert _claims_of(manager, first.job_id) == [("codex", "thr-shared")]
    finally:
        await manager.shutdown()


async def test_a_queued_resume_cancelled_while_the_holder_runs_keeps_its_own_outcome(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    script = _quick_codex(tmp_path, thread_id="thr-q", sleep_for={"A": 6.0})
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, script))
    try:
        first = await manager.start_async(manager.prepare_request(backend="codex", prompt="A"))
        async with asyncio.timeout(15):
            while manager.store.get_job(first.job_id).session_id != "thr-q":
                await asyncio.sleep(0.05)
        waiting = await manager.start_async(manager.prepare_request(backend="codex", prompt="B", resume_from="thr-q"))
        async with asyncio.timeout(15):
            while not (manager._local_jobs[waiting.job_id].last_activity or "").startswith("waiting for session"):
                await asyncio.sleep(0.05)
        await manager.cancel(waiting.job_id)
        # It asked for that session and was waiting its turn: not a collision.
        stored = manager.store.get_job(waiting.job_id)
        assert (stored.status, stored.failure_reason) == ("cancelled", "cancelled")
        assert _claims_of(manager, first.job_id) == [("codex", "thr-q")]
    finally:
        await manager.shutdown()


# --- 6. Owner liveness that survives PID reuse ---


def _running_job(job_id: str, owner_id: str, cwd: Path) -> JobRecord:
    return JobRecord(
        job_id=job_id, owner_id=owner_id, backend="codex", cwd=str(cwd), mode="read_only",
        requested_model=None, model="m", prompt_preview="p", status="running", started_at=time.time(),
        provider_invoked=True,
    )


def _identity_or_skip(pid: int) -> str:
    from agent_bridge.nesting import process_identity

    identity = process_identity(pid)
    if identity is None:
        pytest.skip("no stable process identity on this platform")
    return identity


async def test_an_owner_whose_pid_now_belongs_to_another_process_is_not_live(tmp_path: Path) -> None:
    import subprocess

    from agent_bridge.nesting import process_identity
    from agent_bridge.storage import BridgeStore

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = _identity_or_skip(other.pid)
        assert process_identity(other.pid) == identity  # read twice, the same token
        kind, _, ticks = identity.rpartition(":")
        store = BridgeStore(tmp_path / "state")
        check = dict(is_pid_alive_fn=is_pid_alive, process_identity_fn=process_identity)
        store.register_owner("same", other.pid, process_identity=identity)
        # Registered by an earlier process that had this pid, created one tick before: the pid is alive,
        # but it is not that process. The comparison is exact, so no tolerance hides a fast reuse.
        store.register_owner("reused", other.pid, process_identity=f"{kind}:{int(ticks) - 1}")
        store.register_owner("older-bridge", other.pid)  # a row from a bridge that recorded none
        assert store.is_owner_alive("same", **check) is True
        assert store.is_owner_alive("reused", **check) is False
        assert store.is_owner_alive("older-bridge", **check) is True
        store.insert_job(_running_job("codex-0000same", "same", tmp_path))
        store.insert_job(_running_job("codex-000reuse", "reused", tmp_path))
        assert store.recover_dead_owner_jobs("me", **check) == ["codex-000reuse"]
        assert store.get_job("codex-000reuse").failure_reason == "owner_exited"
        assert store.get_job("codex-0000same").status == "running"
    finally:
        other.kill()
        other.wait()


def test_owner_identity_is_read_from_the_kernel_and_never_from_the_clock() -> None:
    import os

    from agent_bridge.nesting import linux_process_identity, process_identity

    # Linux: the boot id and the process's start ticks since boot. A clock step moves /proc/stat's btime,
    # which is not read, and neither of these.
    # Fields after the name: state ppid pgrp session tty tpgid flags, 12 counters, then starttime.
    stat = "4242 (python3 (worker) x) S 1 4242 4242 0 -1 4194560 " + " ".join(["0"] * 12) + " 987654 1 2"
    assert linux_process_identity(stat, "1b2c-boot\n") == "linux:1b2c-boot:987654"
    assert linux_process_identity(stat, "other-boot") == "linux:other-boot:987654"  # after a reboot
    assert linux_process_identity("garbage", "1b2c-boot") is None
    assert linux_process_identity(stat, "") is None
    # Where no such token exists without the clock, none is made: the pid alone decides.
    assert process_identity(os.getpid(), platform="darwin") is None


async def test_a_bridge_records_its_own_process_identity(tmp_path: Path) -> None:
    import os

    manager = JobManager(_settings(tmp_path))
    try:
        conn = sqlite3.connect(manager.store.db_path)
        try:
            (recorded,) = conn.execute(
                "SELECT process_identity FROM owners WHERE owner_id = ?", (manager.owner_id,)
            ).fetchone()
        finally:
            conn.close()
        assert recorded == _identity_or_skip(os.getpid())
    finally:
        await manager.shutdown()


def test_an_older_owners_table_gains_the_column_and_its_rows_read_as_unknown(tmp_path: Path) -> None:
    import os

    from agent_bridge.nesting import process_identity
    from agent_bridge.storage import BridgeStore

    state = tmp_path / "state"
    state.mkdir()
    conn = sqlite3.connect(state / "bridge.db")
    conn.execute(
        "CREATE TABLE owners (owner_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started_at REAL NOT NULL, "
        "last_heartbeat REAL NOT NULL, exited_at REAL)"
    )
    now = time.time()
    conn.execute("INSERT INTO owners VALUES ('legacy', ?, ?, ?, NULL)", (os.getpid(), now, now))
    conn.commit()
    conn.close()

    store = BridgeStore(state)
    conn = sqlite3.connect(state / "bridge.db")
    try:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(owners)")}
        (value,) = conn.execute("SELECT process_identity FROM owners WHERE owner_id = 'legacy'").fetchone()
    finally:
        conn.close()
    assert "process_identity" in columns and columns["process_identity"][2] == "TEXT"
    assert columns["process_identity"][3] == 0  # nullable
    assert value is None
    # Unknown: the pid check decides alone, as before.
    assert store.is_owner_alive(
        "legacy", is_pid_alive_fn=is_pid_alive, process_identity_fn=process_identity
    ) is True


# --- 7. Small refusals ---


def _tool_data(raw: object) -> dict:
    import json

    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
        return raw[1]
    if isinstance(raw, dict):
        return raw
    content = raw.content if hasattr(raw, "content") else raw  # type: ignore[attr-defined]
    return json.loads(content[0].text)


def test_the_stored_job_cap_counts_only_finished_jobs(tmp_path: Path) -> None:
    from dataclasses import replace

    from agent_bridge.storage import BridgeStore

    store = BridgeStore(tmp_path / "state", max_stored_jobs=20)
    store.register_owner("win", 1)
    t0 = time.time() - 1000

    def finish(job_id: str, created: float, finished: float) -> None:
        rec = replace(_running_job(job_id, "win", tmp_path), created_at=created)
        store.insert_job(rec)
        store.finalize_job(replace(rec, status="completed", finished_at=finished, duration_seconds=1.0))

    for i in range(20):
        finish(f"codex-done{i:04d}", t0 + i, t0 + i + 0.5)
    for j in range(5):
        store.insert_job(replace(_running_job(f"codex-live{j:04d}", "win", tmp_path), created_at=t0 + 100 + j))
    finish("codex-donelast", t0 + 200, t0 + 201)  # the 21st finished job: only the oldest goes

    rows = store.list_jobs(limit=100)
    finished = {r.job_id for r in rows if r.status == "completed"}
    running = {r.job_id for r in rows if r.status == "running"}
    assert len(finished) == 20
    assert "codex-done0000" not in finished and "codex-donelast" in finished
    assert len(running) == 5


async def test_resume_from_a_job_id_that_is_not_found_is_refused_not_sent_as_a_session_id(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    mcp, manager = create_server(settings=settings, nesting=NOT_NESTED)
    try:
        with pytest.raises(ValueError, match=r"No job 'codex-deadbeef' was found to resume"):
            manager.prepare_request(backend="codex", prompt="go on", resume_from="codex-deadbeef")
        with pytest.raises(Exception, match=r"No job 'neon-0badc0de' was found to resume"):
            await mcp.call_tool("run", {"backend": "neon", "prompt": "go on", "resume_from": "neon-0badc0de"})
        # Anything not shaped like a job id is still a session id.
        session_id = "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"
        req = manager.prepare_request(backend="codex", prompt="go on", resume_from=session_id)
        assert req.session_id == session_id
        assert manager.store.list_jobs(limit=10) == []
    finally:
        await manager.shutdown()


async def test_jobs_refuses_an_unknown_status_and_lists_the_valid_ones(tmp_path: Path) -> None:
    mcp, manager = create_server(settings=_settings(tmp_path), nesting=NOT_NESTED)
    try:
        with pytest.raises(Exception) as excinfo:
            await mcp.call_tool("jobs", {"status": "done"})
        assert "Unknown status 'done'" in str(excinfo.value)
        assert "queued, running, completed, failed, cancelled, timed_out" in str(excinfo.value)
        assert _tool_data(await mcp.call_tool("jobs", {"status": "completed"}))["count"] == 0
    finally:
        await manager.shutdown()


async def test_jobs_gives_an_unknown_id_a_not_found_entry_and_still_answers_for_the_rest(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    script = _quick_codex(tmp_path, sleep_for={"any": 2.0, "all": 2.0})
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, script))
    mcp, _ = create_server(settings=settings, manager=manager, nesting=NOT_NESTED)
    try:
        for mode in ("any", "all"):
            # A peer still running: the unknown id must neither end an "any" wait nor hold up an "all" one.
            peer = await manager.start_async(manager.prepare_request(backend="codex", prompt=mode))
            started = time.monotonic()
            out = _tool_data(await mcp.call_tool(
                "jobs", {"job_ids": ["codex-00000000", peer.job_id], "wait_seconds": 30, "wait_mode": mode},
            ))
            took = time.monotonic() - started
            assert 1.0 < took < 15, (mode, took)  # waited for the peer, and only for it
            assert [j["job_id"] for j in out["jobs"]] == ["codex-00000000", peer.job_id]
            assert out["jobs"][0]["status"] == "not_found" and "No job 'codex-00000000'" in out["jobs"][0]["error"]
            assert out["jobs"][1]["status"] == "completed"
            assert (out["all_done"], out["any_done"]) == (True, True)
        # Ids that are all unknown do not hold an "any" wait open.
        started = time.monotonic()
        out = _tool_data(await mcp.call_tool(
            "jobs", {"job_ids": ["codex-00000000"], "wait_seconds": 30, "wait_mode": "any"},
        ))
        assert time.monotonic() - started < 5
        assert [j["status"] for j in out["jobs"]] == ["not_found"]
        assert out["any_done"] is False
    finally:
        await manager.shutdown()


# --- 8. Honest annotation and a trimmed child environment ---


async def test_run_is_not_marked_read_only(tmp_path: Path) -> None:
    mcp, manager = create_server(settings=_settings(tmp_path), nesting=NOT_NESTED)
    try:
        tools = {t.name: t for t in await mcp.list_tools()}
        annotations = tools["run"].annotations
        assert annotations is not None and annotations.read_only_hint is False
    finally:
        await manager.shutdown()


def test_credential_like_names_are_picked_out_and_agent_sockets_kept() -> None:
    from agent_bridge.nesting import credential_env_names

    env = {
        "PATH": "x", "HOME": "x", "OPENAI_API_KEY": "x", "gh_token": "x", "MY_SECRET": "x",
        "DB_PASSWORD": "x", "SMTP_PASSWD": "x", "AWS_CREDENTIALS_FILE": "x",
        "SSH_AUTH_SOCK": "x", "GNOME_KEYRING_CONTROL": "x", "AGENT_BRIDGE_PARENT_JOB": "x",
        # Git reads config from these three; dropping the key alone makes every git command fail.
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.pager", "GIT_CONFIG_VALUE_0": "cat",
        "GCM_CREDENTIAL_STORE": "secretservice",
    }
    assert credential_env_names(env) == [
        "AWS_CREDENTIALS_FILE", "DB_PASSWORD", "MY_SECRET", "OPENAI_API_KEY", "SMTP_PASSWD", "gh_token",
    ]


def test_an_unknown_child_env_setting_stops_the_bridge_from_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_BRIDGE_CHILD_ENV", raising=False)
    assert Settings.from_env().child_env == "trim"
    monkeypatch.setenv("AGENT_BRIDGE_CHILD_ENV", "Inherit")
    assert Settings.from_env().child_env == "inherit"
    monkeypatch.setenv("AGENT_BRIDGE_CHILD_ENV", "everything")
    with pytest.raises(ValueError, match="Unknown AGENT_BRIDGE_CHILD_ENV 'everything'"):
        Settings.from_env()


_ENV_PROBE = (
    "import os\n"
    "seen = 'key=%s sock=%s marker=%s' % ('TEST_BRIDGE_API_KEY' in os.environ, os.environ.get('SSH_AUTH_SOCK'), "
    "os.environ.get('AGENT_BRIDGE_PARENT_JOB', '')[:5])\n"
)


@pytest.mark.parametrize("backend", ["codex", "neon"])
@pytest.mark.parametrize("child_env", ["trim", "inherit"])
async def test_codex_and_agy_jobs_get_no_credential_variables_unless_inherit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, backend: str, child_env: str,
) -> None:
    import logging

    monkeypatch.setenv("TEST_BRIDGE_API_KEY", "value-that-must-not-be-logged")
    monkeypatch.setenv("SSH_AUTH_SOCK", "agent.sock")
    script = tmp_path / "env_probe.py"
    if backend == "codex":
        script.write_text(
            "import json, sys\n" + _ENV_PROBE + "sys.stdin.read()\n"
            "print(json.dumps({'type': 'thread.started', 'thread_id': 'thr-env'}), flush=True)\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': seen}}), flush=True)\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}), flush=True)\n",
            encoding="utf-8",
        )
    else:
        script.write_text(
            "import json, sys\n"
            "if 'models' in sys.argv:\n"
            "    print('gemini-4-neon-high\\tGemini 4 Neon (High)')\n"
            "    sys.exit(0)\n"
            + _ENV_PROBE
            + "print(json.dumps({'event': 'result', 'result': {'conversation_id': 'conv-env', 'status': 'SUCCESS', "
            "'response': seen}}), flush=True)\n",
            encoding="utf-8",
        )
    settings = _settings(tmp_path, child_env=child_env)
    manager = JobManager(
        settings, codex_runner=StubCodexRunner(settings, script), neon_runner=StubAgyRunner(settings, script),
    )
    try:
        with caplog.at_level(logging.INFO, logger="agent_bridge.runners"):
            rec = await manager.run(manager.prepare_request(backend=backend, prompt="env"))
        assert rec.status == "completed", rec.error
        expected_key = "True" if child_env == "inherit" else "False"
        # The nesting marker still travels: it carries the job id.
        assert rec.response == f"key={expected_key} sock=agent.sock marker={rec.job_id[:5]}"
        assert "value-that-must-not-be-logged" not in caplog.text
        if child_env == "trim":
            assert "TEST_BRIDGE_API_KEY" in caplog.text and rec.job_id in caplog.text
        else:
            assert "TEST_BRIDGE_API_KEY" not in caplog.text
    finally:
        await manager.shutdown()
