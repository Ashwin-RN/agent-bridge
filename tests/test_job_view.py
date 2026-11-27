"""Read-only job views and the metadata shared by live and persisted readers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from agent_bridge.backends import codex as codex_backend
from agent_bridge.config import Settings
from agent_bridge.jobs import JobManager
from agent_bridge.models import JobRecord
from agent_bridge.status_cmd import read_snapshot
from agent_bridge.status_text import SEP, format_job_line
from agent_bridge.storage import BridgeStore


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(codex_backend, "read_codex_local_defaults", lambda: {})
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


def _legacy_insert(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO jobs (job_id, owner_id, backend, cwd, mode, prompt_preview, status,
                          created_at, last_activity, model)
        VALUES ('legacy', 'old-window', 'codex', '.', 'read_only', 'hello', 'queued',
                1, 'queued', 'old-model')
        """
    )


def _record(job_id: str, owner_id: str, **fields) -> JobRecord:
    return replace(
        JobRecord(
            job_id=job_id, owner_id=owner_id, backend="codex", cwd=".", mode="read_only",
            requested_model=None, model=None, prompt_preview="hello",
        ),
        **fields,
    )


def _set_command(monkeypatch: pytest.MonkeyPatch, runner, script: Path, source: str) -> None:
    script.write_text(source, encoding="utf-8")
    monkeypatch.setattr(runner, "resolve_command", lambda: [sys.executable, str(script)])


def _emit(event: dict) -> str:
    return f"print({json.dumps(event)!r}, flush=True)\n"


_CODEX_RESULT = _emit({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
_CODEX_RESULT += _emit({"type": "turn.completed"})


def test_additive_migrations_leave_old_rows_null(tmp_path: Path) -> None:
    # Omitting each ALTER TABLE independently made the column schema check fail.
    before = BridgeStore(tmp_path)
    expected = {"last_activity_at": "REAL", "model_source": "TEXT", "response_chars": "INTEGER"}
    with closing(sqlite3.connect(before.db_path)) as conn, conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name in expected.keys() & columns:
            conn.execute(f"ALTER TABLE jobs DROP COLUMN {name}")
        _legacy_insert(conn)
    old = before.peek_job("legacy")
    assert old is not None and old.last_activity_at is None
    with closing(sqlite3.connect(before.db_path)) as conn:
        assert not expected.keys() & {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}

    migrated = BridgeStore(tmp_path)
    with closing(sqlite3.connect(migrated.db_path)) as conn:
        actual = {row[1]: (row[2], row[3], row[4]) for row in conn.execute("PRAGMA table_info(jobs)")}
    assert {name: actual.get(name) for name in expected} == {
        name: (column_type, 0, None) for name, column_type in expected.items()
    }
    old = migrated.peek_job("legacy")
    assert old is not None and (old.last_activity, old.model) == ("queued", "old-model")
    assert (old.last_activity_at, old.model_source, old.response_chars) == (None, None, None)


def test_legacy_insert_still_works(tmp_path: Path) -> None:
    # Making response_chars required with no default made the old insert fail.
    store = BridgeStore(tmp_path)
    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        _legacy_insert(conn)
    old = store.peek_job("legacy")
    assert old is not None and (old.last_activity, old.model) == ("queued", "old-model")
    assert (old.last_activity_at, old.model_source, old.response_chars) == (None, None, None)


async def test_activity_pair_survives_dropped_progress(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # Omitting the progress timestamp or making the queued timestamp precede creation failed these checks.
    manager = JobManager(settings)
    enter_runner = asyncio.Event()
    run = manager.codex_runner.run

    async def wait_then_run(*args, **kwargs):
        await enter_runner.wait()
        return await run(*args, **kwargs)

    def command(text: str) -> str:
        return _emit({"type": "item.started", "item": {"type": "command_execution", "command": text}})

    _set_command(
        monkeypatch, manager.codex_runner, tmp_path / "codex.py",
        "from pathlib import Path\nimport time\n"
        + command("first")
        + "while not Path('next').exists(): time.sleep(0.01)\n"
        + command("second")
        + "while not Path('finish').exists(): time.sleep(0.01)\n"
        + _CODEX_RESULT,
    )
    monkeypatch.setattr(manager.codex_runner, "run", wait_then_run)
    caplog.set_level(logging.DEBUG, logger="agent_bridge.jobs")
    try:
        job = await manager.start_async(manager.prepare_request(backend="codex", prompt="test"))

        async def stored_activity(text: str) -> JobRecord:
            async with asyncio.timeout(10):
                while True:
                    record = manager.store.peek_job(job.job_id)
                    if record is not None and record.last_activity == text:
                        return record
                    await asyncio.sleep(0.01)

        queued = await stored_activity("queued")
        starting = await stored_activity("starting codex")
        assert queued.last_activity_at == queued.created_at
        assert starting.last_activity_at == starting.started_at
        assert starting.last_activity_at >= queued.last_activity_at
        enter_runner.set()
        first = await stored_activity("running command: first")
        assert first.last_activity_at > starting.last_activity_at
        assert first.last_activity_at == manager.peek(job.job_id).last_activity_at
        with closing(sqlite3.connect(manager.store.db_path)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            (tmp_path / "next").touch()
            async with asyncio.timeout(10):
                while "Skipping locked progress write" not in caplog.text:
                    await asyncio.sleep(0.01)
            still_first = manager.store.peek_job(job.job_id)
            assert (still_first.last_activity, still_first.last_activity_at) == (
                first.last_activity, first.last_activity_at,
            )
            live = manager.peek(job.job_id)
            assert live.last_activity == "running command: second"
            assert live.last_activity_at > first.last_activity_at
            lock.rollback()
        (tmp_path / "finish").touch()
        [finished] = await manager.wait_for([job.job_id], wait_seconds=10)
        assert finished.last_activity == "completed"
        assert finished.last_activity_at == finished.finished_at
    finally:
        await manager.shutdown()


@pytest.mark.parametrize(
    "backend, requested, cli_model, expected_model, source",
    [
        pytest.param("codex", "gpt-test", None, "gpt-test", "requested", id="codex-requested"),
        pytest.param("codex", None, None, "codex-default", "default", id="codex-default"),
        pytest.param("codex", "retired-gpt", None, "codex-default", "default", id="codex-retry"),
        pytest.param("neon", "neon-high", "gemini-observed", "gemini-observed", "observed", id="agy-init"),
        pytest.param("neon", "neon-high", None, "gemini-4-neon-high", "requested", id="agy-no-init"),
        pytest.param(
            "claude", "claude-sonnet-4-6", "claude-sonnet-4-6", "claude-sonnet-4-6", "observed",
            id="claude-same-model-init",
        ),
    ],
)
async def test_model_source(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    backend: str, requested: str | None, cli_model: str | None, expected_model: str, source: str,
) -> None:
    # Inverting Codex labels, relabeling agy callbacks, and ignoring same-model Claude
    # confirmations each made the corresponding provenance assertion fail.
    manager = JobManager(settings)
    if backend == "codex":
        script = (
            "import sys\n"
            "if 'retired-gpt' in sys.argv:\n"
            "    " + _emit({"type": "error", "message": "model_not_found: retired-gpt"})
            + "    sys.exit(1)\n" + _CODEX_RESULT
        )
    elif backend == "neon":
        script = (
            "import sys\n"
            "if sys.argv[1] == 'models':\n"
            "    print('gemini-4-neon-high\\tGemini 4 Neon (High)')\n"
            "    sys.exit(0)\n"
        )
        if cli_model:
            script += _emit({"event": "init", "init": {"model": cli_model}})
        script += _emit({"event": "result", "result": {"status": "SUCCESS", "response": "done"}})
    else:
        script = _emit({"type": "system", "subtype": "init", "model": cli_model})
        script += _emit({"type": "result", "subtype": "success", "result": "done"})
    _set_command(monkeypatch, manager._runner_for(backend), tmp_path / "cli.py", script)
    try:
        job = await manager.run(manager.prepare_request(backend=backend, model=requested, prompt="test"))
        saved = manager.store.peek_job(job.job_id)
        assert saved is not None and saved.status == "completed"
        assert saved.model_source == source
        assert saved.model == expected_model
    finally:
        await manager.shutdown()


def test_response_chars_precedes_storage_cut(tmp_path: Path) -> None:
    # Measuring the sliced response made the stored count too small.
    store = BridgeStore(tmp_path)
    job = _record("long", "writer")
    store.insert_job(job)
    response = "\N{GREEK SMALL LETTER PI}" * 200_001
    store.finalize_job(replace(job, status="completed", response=response))
    saved = store.peek_job(job.job_id)
    assert saved is not None
    assert saved.response == response[:200_000]
    assert saved.response_chars == len(response)


def test_peek_preserves_database(settings: Settings) -> None:
    # Using get() recovered the exited job, ignoring local state lost its live activity,
    # and adding a heartbeat write changed the database dump.
    manager = JobManager(settings)
    store = manager.store
    store.register_owner("departed", os.getpid())
    store.insert_job(_record("done", "departed", status="completed", response="finished reply"))
    store.insert_job(_record("abandoned", "departed", status="running"))
    store.update_job_progress("abandoned", owner_id="departed", session_id="old-session")
    store.mark_owner_exited("departed")
    # Leave a claim as an exited owner could after a crash between its exit and claim removal.
    with closing(sqlite3.connect(store.db_path)) as observer, observer:
        observer.execute(
            "INSERT OR REPLACE INTO session_claims VALUES ('codex', 'old-session', 'abandoned', 'departed', 1)"
        )
        observer.commit()
        local = _record("live", manager.owner_id, status="running", last_activity="stored")
        store.insert_job(local)
        manager._local_jobs[local.job_id] = replace(local, last_activity="live")
        terminal = _record("pending", manager.owner_id, status="running")
        store.insert_job(terminal)
        manager._terminal_uncommitted[terminal.job_id] = replace(terminal, status="completed", response="pending reply")

        before = list(observer.iterdump())
        owners = observer.execute("SELECT * FROM owners ORDER BY owner_id").fetchall()
        version = observer.execute("PRAGMA data_version").fetchone()
        for _ in range(5):
            assert store.peek_job("missing") is None and manager.peek("missing") is None
            assert manager.peek("done").response == "finished reply"
            assert manager.peek("live").last_activity == "live"
            assert store.peek_job("live").last_activity == "stored"
            assert manager.peek("pending").response == "pending reply"
            assert store.peek_job("pending").status == "running"
            assert manager.peek("abandoned").status == "running"
        assert list(observer.iterdump()) == before
        assert observer.execute("SELECT * FROM owners ORDER BY owner_id").fetchall() == owners
        assert observer.execute("PRAGMA data_version").fetchone() == version


def test_peek_connection_refuses_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Opening a writable connection made the SQL probe succeed and the test fail.
    store = BridgeStore(tmp_path)
    connect = sqlite3.connect

    class CheckConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT * FROM jobs"):
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    super().execute("UPDATE owners SET last_heartbeat = 0")
            return super().execute(sql, parameters)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, factory=CheckConnection, **kw))
    assert store.peek_job("missing") is None


def test_status_formats_persisted_job_view(tmp_path: Path) -> None:
    # Replacing the supplied timestamp on insert made the line lose the original age.
    store = BridgeStore(tmp_path)
    store.register_owner("writer", os.getpid())
    now = time.time()
    store.insert_job(_record(
        "codex", "writer", model="gpt-test", model_source="requested", status="running",
        last_activity="reading jobs.py", last_activity_at=now - 43,
    ))
    store.insert_job(_record("agy", "writer", backend="neon", status="running"))
    store.update_job_progress("agy", owner_id="writer", model="gemini-observed", model_source="observed")

    snapshot = read_snapshot(store.db_path, now=now, recent_seconds=0)
    jobs = {job["job_id"]: job for job in snapshot["active"]}
    assert jobs["codex"]["model_source"] == "requested"
    assert jobs["agy"]["model_source"] == "observed"
    codex = format_job_line(jobs["codex"], now=now).split(SEP)
    agy = format_job_line(jobs["agy"], now=now).split(SEP)
    assert codex[1] == "requested gpt-test"
    assert agy[1] == "gemini-observed"
    assert codex[-1] == "reading jobs.py (43s ago)"
