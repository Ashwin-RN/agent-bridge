"""Regression tests for the second job-safety repair."""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_bridge.config import Settings
from agent_bridge.jobs import JobManager
from agent_bridge.models import JobRecord, RunRequest
from agent_bridge.runners import NeonRunner
from agent_bridge.storage import BridgeStore


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("AGENT_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    return Settings(
        state_dir=tmp_path / "state",
        default_cwd=tmp_path,
        default_neon_model="neon-high",
        default_codex_model=None,
        default_codex_effort=None,
        default_timeout_seconds=30,
        max_concurrent_jobs=4,
        max_stored_jobs=50,
        models_cache_ttl_seconds=900.0,
        quota_cache_ttl_seconds=120.0,
        owner_stale_seconds=30.0,
        agy_bin=str(tmp_path / "missing-agy"),
        codex_bin=str(tmp_path / "missing-codex"),
        claude_bin=str(tmp_path / "missing-claude"),
    )


def _job(job_id: str, owner_id: str, cwd: Path) -> JobRecord:
    return JobRecord(
        job_id=job_id, owner_id=owner_id, backend="neon", cwd=str(cwd), mode="read_only",
        requested_model=None, model=None, prompt_preview="test", status="running",
        last_activity="running", provider_invoked=True,
    )


def _stand_in(
    runner: NeonRunner, script: Path, source: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script.write_text(source, encoding="utf-8")
    monkeypatch.setattr(runner, "resolve_command", lambda: [sys.executable, str(script)])


def test_final_collision_is_published_after_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = BridgeStore(tmp_path / "state")
    store.register_owner("owner", os.getpid())
    holder = _job("holder", "owner", tmp_path)
    active = _job("candidate", "owner", tmp_path)
    store.insert_job(holder)
    store.insert_job(active)
    store.update_job_progress(holder.job_id, owner_id=holder.owner_id, session_id="shared-session")
    result = replace(active, session_id="shared-session", status="completed", last_activity="completed")
    before = replace(result)
    real_tx = store._tx

    @contextmanager
    def fail_before_commit(**kwargs):
        with real_tx(**kwargs) as cur:
            yield cur
            raise sqlite3.OperationalError("injected commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_tx", fail_before_commit)
        with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
            store.finalize_job(result)

    assert store.peek_job(active.job_id).status == "running"
    assert result.session_id == before.session_id, "rollback discarded the candidate session"
    assert result == before
    assert store.finalize_job(result) == "written"
    saved = store.peek_job(active.job_id)
    assert saved is not None
    assert (saved.status, saved.last_activity, saved.session_id) == ("failed", "failed", None)
    assert (result.status, result.last_activity, result.session_id) == ("failed", "failed", None)
    assert saved.failure_reason == result.failure_reason == "cli_error"


async def test_agy_default_retry_replaces_model_and_resume_skips_placeholder(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = JobManager(settings)
    rejected = "claude-sonnet-5-5-medium"
    _stand_in(
        manager.neon_runner, tmp_path / "agy.py",
        "import sys\n"
        "if sys.argv[1:2] == ['models']:\n"
        f"    print({rejected!r} + '\\tClaude Sonnet 5.5 (Medium)')\n"
        "    print('gemini-4-neon-high\\tGemini 4 Neon (High)')\n"
        "    sys.exit(0)\n"
        "if '--resume' in sys.argv:\n"
        "    print('{\"conversation_id\":\"placeholder-session\",\"status\":\"SUCCESS\",\"response\":\"resumed\"}')\n"
        "    sys.exit(0)\n"
        "if '--model' in sys.argv:\n"
        f"    sys.stderr.write('Error: unavailable model ' + {rejected!r} + '\\n')\n"
        "    sys.exit(1)\n"
        "print('{\"conversation_id\":\"default-session\",\"status\":\"SUCCESS\",\"response\":\"default ran\"}')\n",
        monkeypatch,
    )
    try:
        result = await manager.run(manager.prepare_request(
            backend="neon", prompt="hello", model="claude-sonnet-medium", allow_cross_pool_fallback=True,
        ))
        assert (result.status, result.response) == ("completed", "default ran")
        assert (result.model_value(), result.model_source, result.quota_pool) == (None, "default", "agy_gemini")
        assert result.requested_model == "claude-sonnet-medium"
        with manager.store._tx(write=False) as cur:
            attempts = cur.execute(
                "SELECT model, quota_pool FROM usage_events WHERE job_id = ? ORDER BY attempt_number",
                (result.job_id,),
            ).fetchall()
        assert [tuple(row) for row in attempts] == [(rejected, "agy_claude_gpt"), ("neon-default", "agy_gemini")]

        resume = manager.prepare_request(backend="neon", prompt="again", resume_from="placeholder-session")
        prior = replace(
            _job("default-turn", manager.owner_id, tmp_path), status="completed",
            session_id=resume.session_id, model="neon-default", model_source="default",
        )
        manager.store.insert_job(prior)
        assert manager.prepare_request(backend=None, prompt="again", resume_from=prior.job_id).model is None
        assert manager.prepare_request(backend="neon", prompt="again", resume_from=resume.session_id).model is None
        resumed = await manager.run(resume)
        assert (resumed.status, resumed.response) == ("completed", "resumed")
        assert resume.model is None  # Also check the session context refresh just before launch.
    finally:
        await manager.shutdown()


def test_peek_detaches_the_live_record_and_nested_data(settings: Settings, tmp_path: Path) -> None:
    manager = JobManager(settings)
    live = replace(
        _job("live", manager.owner_id, tmp_path),
        usage={"total_tokens": 7}, provider_metrics={"steps": [{"label": "original"}]},
    )
    manager._local_jobs[live.job_id] = live

    snapshot = manager.peek(live.job_id)
    assert snapshot is not None
    snapshot.last_activity = "changed"
    snapshot.usage["total_tokens"] = 900
    snapshot.provider_metrics["steps"][0]["label"] = "changed"

    assert live.last_activity == "running"
    assert live.usage == {"total_tokens": 7}
    assert live.provider_metrics == {"steps": [{"label": "original"}]}


def test_late_final_write_preserves_pruned_job_ledger(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "state", max_stored_jobs=20)
    store.register_owner("owner", os.getpid())
    first = replace(_job("first", "owner", tmp_path), created_at=1_700_000_000)
    store.insert_job(first)
    finished = replace(first, status="completed", finished_at=first.created_at + 1)
    attempts = [{"usage": {"total_tokens": n}} for n in (5, 10, 50)]
    assert store.finalize_job(finished, attempts=attempts) == "written"
    for index in range(store.max_stored_jobs):
        other = replace(_job(f"other-{index}", "owner", tmp_path), created_at=first.created_at + 2 + index)
        store.insert_job(other)
        store.finalize_job(replace(other, status="completed", finished_at=other.created_at))
    assert store.peek_job(first.job_id) is None

    late = [{"usage": {"total_tokens": n}} for n in (900, 800)]
    assert store.finalize_job(finished, attempts=late) == "missing"
    incomplete = replace(finished, job_id="missing-with-prior-attempt")
    store.record_attempt_usage(incomplete, attempts[0], attempt_number=1)
    assert store.finalize_job(incomplete, attempts=late) == "missing"
    with store._tx(write=False) as cur:
        rows = cur.execute(
            "SELECT attempt_number, is_final_attempt, total_tokens FROM usage_events "
            "WHERE job_id = ? ORDER BY attempt_number", (first.job_id,),
        ).fetchall()
        incomplete_rows = cur.execute(
            "SELECT attempt_number, is_final_attempt, total_tokens FROM usage_events "
            "WHERE job_id = ? ORDER BY attempt_number", (incomplete.job_id,),
        ).fetchall()
    assert [tuple(row) for row in rows] == [(1, 0, 5), (2, 0, 10), (3, 1, 50)]
    assert [tuple(row) for row in incomplete_rows] == [(1, 0, 5), (2, 1, 800)]


async def test_agy_flat_response_and_status_need_no_session_id(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = NeonRunner(settings)
    _stand_in(
        runner, tmp_path / "agy.py",
        "import sys\n"
        "if sys.argv[1:2] == ['models']:\n"
        "    print('gemini-4-neon-high\\tGemini 4 Neon (High)')\n"
        "elif any(arg.startswith('--print=') and arg.endswith('full reply') for arg in sys.argv):\n"
        "    print('{\"status\":\"SUCCESS\",\"response\":\"done\"}')\n"
        "else:\n"
        "    print('{\"status\":\"OK\"}')\n",
        monkeypatch,
    )
    request = RunRequest(backend="neon", prompt="full reply", cwd=str(tmp_path))
    noise = await runner.run(replace(request, prompt="status only"))
    assert noise.status == "failed"
    reply = await runner.run(request)
    assert (reply.status, reply.response, reply.session_id) == ("completed", "done", None)
