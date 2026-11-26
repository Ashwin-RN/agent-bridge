"""Tests for the usage dashboard.

Totals hold only what the bridge's records vouch for; everything else is listed apart and never added in.
Rows are written through the store's own write paths (`insert_job`, `finalize_job`, `record_attempt_usage`),
so the dashboard reads them as the bridge stores them. No CLI is started.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_bridge.config import Settings
from agent_bridge.dashboard import BACKENDS, QUOTA_POOLS, render_text
from agent_bridge.jobs import JobManager
from agent_bridge.models import JobRecord
from agent_bridge.nesting import Nesting
from agent_bridge.server import create_server

ME = "owner-me"
OTHER = "owner-other"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
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


def _manager(tmp_path: Path) -> JobManager:
    # Every owner counts as alive, so that no job here is recovered as owner_exited behind the test's back.
    manager = JobManager(_settings(tmp_path), owner_id=ME, pid=os.getpid(), is_pid_alive_fn=lambda _pid: True)
    manager.store.register_owner(OTHER, os.getpid())
    return manager


def _usage(total: int) -> dict[str, int]:
    output = total // 4
    return {
        "input_tokens": total - output,
        "output_tokens": output,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": total,
    }


def _record(job_id: str, *, owner: str, backend: str, model: str, status: str = "queued") -> JobRecord:
    return JobRecord(
        job_id=job_id,
        owner_id=owner,
        backend=backend,  # type: ignore[arg-type]
        cwd="/repo",
        mode="read_only",
        requested_model=model,
        model=model,
        prompt_preview="x",
        status=status,  # type: ignore[arg-type]
    )


def _finish(
    manager: JobManager,
    job_id: str,
    *,
    owner: str = ME,
    backend: str = "codex",
    model: str = "gpt-6.2-sol",
    status: str = "completed",
    tokens: int | None = 100,
    usage_status: str = "exact",
    metrics: dict[str, Any] | None = None,
    invoked: bool = True,
    failure_reason: str | None = None,
    finished_at: float | None = None,
    attempts: list[dict[str, Any]] | None = None,
) -> None:
    store = manager.store
    record = _record(job_id, owner=owner, backend=backend, model=model)
    store.insert_job(record)
    end = finished_at if finished_at is not None else time.time() - 60
    record.status = status  # type: ignore[assignment]
    record.started_at = end - 2.0 if invoked else None
    record.finished_at = end
    record.duration_seconds = 2.0 if invoked else 0.0
    record.usage = _usage(tokens) if tokens is not None else None
    record.usage_status = usage_status  # type: ignore[assignment]
    record.provider_metrics = metrics
    record.failure_reason = failure_reason
    store.finalize_job(record, touched_provider=invoked, attempts=attempts)


def _total(bucket: dict[str, Any]) -> int:
    return int(bucket["tokens"]["total_tokens"])


async def test_totals_hold_only_finished_runs_with_exact_or_delta_counts(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _finish(manager, "codex-exact", tokens=1000)
    _finish(manager, "neon-delta", backend="neon", model="gemini-4-neon-high", tokens=300, usage_status="delta")
    _finish(
        manager,
        "claude-partial",
        backend="claude",
        model="claude-sonnet-5-5",
        status="failed",
        tokens=50,
        usage_status="partial",
        metrics={"provider": "claude", "native_subagents": 0, "session_cost_usd_estimate": 0.75},
    )
    _finish(manager, "codex-lost", status="cancelled", tokens=None, usage_status="missing")

    d = await manager.usage_dashboard(period="all")

    assert _total(d["totals"]) == 1300
    assert d["totals"]["runs"] == 4 and d["totals"]["cancelled"] == 1 and d["totals"]["failed"] == 1
    partial = d["not_in_totals"]["partial_counts"]
    assert partial["job_ids"] == ["claude-partial"] and partial["reported_total_tokens"] == 50
    assert d["not_in_totals"]["cli_started_no_count"]["job_ids"] == ["codex-lost"]
    # Claude Code's cost estimate is not a figure the dashboard shows or adds.
    assert "0.75" not in json.dumps(d) and "cost_usd" not in json.dumps(d)
    assert any("quota" in line for line in d["left_out"])
    await manager.shutdown()


async def test_every_backend_pool_and_model_is_shown_and_the_parts_add_up(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _finish(manager, "codex-mine", tokens=1000)
    _finish(manager, "neon-other", owner=OTHER, backend="neon", model="claude-sonnet-5-5-high", tokens=200)
    _finish(manager, "neon-mine", backend="neon", model="gemini-4-neon-high", tokens=70)
    # A retried job whose second attempt ran on another model: each attempt's tokens go to its own model,
    # and the run is counted once, under the model its last attempt ran on.
    _finish(
        manager,
        "neon-retried",
        owner=OTHER,
        backend="neon",
        model="gemini-3.8-flash-low",
        tokens=100,
        attempts=[
            {"attempt": 1, "status": "failed", "model": "gemini-4-neon-high", "usage_status": "exact",
             "usage": _usage(40), "duration_seconds": 1.0},
            {"attempt": 2, "status": "completed", "model": "gemini-3.8-flash-low", "usage_status": "exact",
             "usage": _usage(60), "duration_seconds": 1.0},
        ],
    )

    d = await manager.usage_dashboard(period="all")

    # Every backend and pool is listed, with zeros where nothing ran.
    assert set(BACKENDS) <= set(d["by_backend"]) and set(QUOTA_POOLS) <= set(d["by_quota_pool"])
    assert d["by_backend"]["claude"]["runs"] == 0 and d["by_quota_pool"]["claude"]["runs"] == 0

    total_runs, total_tokens = d["totals"]["runs"], _total(d["totals"])
    assert (total_runs, total_tokens) == (4, 1370)
    for grouping in ("by_backend", "by_quota_pool", "by_model"):
        parts = d[grouping].values()
        assert sum(p["runs"] for p in parts) == total_runs, grouping
        assert sum(_total(p) for p in parts) == total_tokens, grouping
    mine, others = d["this_session"], d["other_sessions"]
    assert (mine["runs"], _total(mine)) == (2, 1070)
    assert (others["runs"], _total(others)) == (2, 300)

    models = d["by_model"]
    assert (models["neon:gemini-3.8-flash-low"]["runs"], _total(models["neon:gemini-3.8-flash-low"])) == (1, 60)
    assert (models["neon:gemini-4-neon-high"]["runs"], _total(models["neon:gemini-4-neon-high"])) == (1, 110)
    assert _total(d["by_quota_pool"]["agy_claude_gpt"]) == 200

    session_only = await manager.usage_dashboard(period="session")
    assert (session_only["totals"]["runs"], _total(session_only["totals"])) == (2, 1070)
    assert session_only["other_sessions"]["runs"] == 0
    await manager.shutdown()


async def test_a_running_job_and_its_recorded_attempts_stay_out_of_the_totals(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _finish(manager, "codex-done", tokens=500)
    running = _record("codex-running", owner=ME, backend="codex", model="gpt-6.2-sol", status="running")
    running.started_at = time.time() - 30
    manager.store.insert_job(running)
    # A first attempt that ended before a retry: its row is written while the job is still running.
    manager.store.record_attempt_usage(
        running,
        {"status": "failed", "usage_status": "exact", "usage": _usage(900), "duration_seconds": 5.0},
        attempt_number=1,
    )
    manager.store.insert_job(_record("neon-queued", owner=OTHER, backend="neon", model="gemini-4-neon-high"))

    d = await manager.usage_dashboard(period="all")

    assert (d["totals"]["runs"], _total(d["totals"])) == (1, 500)
    listed = {j["job_id"]: j for j in d["not_in_totals"]["running_or_queued"]["jobs"]}
    assert set(listed) == {"codex-running", "neon-queued"}
    assert listed["codex-running"]["tokens_recorded_for_earlier_attempts"] == 900
    assert listed["codex-running"]["session"] == "this" and listed["neon-queued"]["session"] == "other"
    await manager.shutdown()


async def test_a_run_without_a_count_is_marked_by_whether_its_cli_started(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _finish(manager, "codex-lost", status="cancelled", tokens=None, usage_status="missing")
    _finish(manager, "codex-review", status="completed", tokens=None, usage_status="missing")
    _finish(manager, "codex-never", status="cancelled", tokens=None, usage_status="missing", invoked=False)
    _finish(manager, "codex-pruned", status="failed", tokens=None, usage_status="missing")
    with sqlite3.connect(manager.store.db_path) as conn:
        conn.execute("DELETE FROM jobs WHERE job_id = 'codex-pruned'")

    d = await manager.usage_dashboard(period="all")

    nit = d["not_in_totals"]
    assert nit["cli_started_no_count"]["job_ids"] == ["codex-lost", "codex-review"]
    assert nit["cli_started_no_count"]["by_status"] == {"cancelled": 1, "completed": 1}
    # A pruned job row says nothing about whether the CLI ran: never read as "nothing spent".
    assert nit["cannot_tell_if_cli_started"]["job_ids"] == ["codex-pruned"]
    assert d["counted_as_nothing_spent"]["job_ids"] == ["codex-never"]
    assert (d["totals"]["runs"], _total(d["totals"])) == (4, 0)
    await manager.shutdown()


async def test_claude_subagents_are_marked_and_an_unreported_count_is_not_taken_for_none(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _finish(manager, "claude-sub", backend="claude", model="claude-sonnet-5-5", tokens=1000,
            metrics={"provider": "claude", "native_subagents": 2})
    _finish(manager, "claude-none", backend="claude", model="claude-sonnet-5-5", tokens=300,
            metrics={"provider": "claude", "native_subagents": 0})
    _finish(manager, "claude-old", backend="claude", model="claude-sonnet-5-5", tokens=200,
            metrics={"provider": "claude"})

    d = await manager.usage_dashboard(period="all")

    assert _total(d["totals"]) == 1500  # the main agents' own counts
    sub = d["not_in_totals"]["claude_subagents"]
    assert sub["job_ids"] == ["claude-sub"] and sub["subagents"] == 2
    assert d["not_in_totals"]["claude_subagents_not_on_record"]["job_ids"] == ["claude-old"]
    await manager.shutdown()


async def test_since_and_until_pick_the_period_and_a_date_until_takes_in_the_whole_day(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    day = date.today() - timedelta(days=2)

    def at(d: date, hour: int, minute: int = 0) -> float:
        return datetime(d.year, d.month, d.day, hour, minute).timestamp()

    _finish(manager, "codex-morning", tokens=1, finished_at=at(day, 10))
    _finish(manager, "codex-late", tokens=10, finished_at=at(day, 23, 30))
    _finish(manager, "codex-next-day", tokens=100, finished_at=at(day + timedelta(days=1), 9))

    whole_day = await manager.usage_dashboard(since=day.isoformat(), until=day.isoformat())
    assert _total(whole_day["totals"]) == 11
    from_noon = await manager.usage_dashboard(since=f"{day.isoformat()}T12:00")
    assert _total(from_noon["totals"]) == 110

    with pytest.raises(ValueError, match="either period or since/until"):
        await manager.usage_dashboard(period="7d", since=day.isoformat())
    with pytest.raises(ValueError, match="since='last tuesday'"):
        await manager.usage_dashboard(since="last tuesday")
    with pytest.raises(ValueError, match="earlier than until"):
        await manager.usage_dashboard(since=day.isoformat(), until=(day - timedelta(days=1)).isoformat())
    await manager.shutdown()


async def test_a_period_reaching_back_before_the_first_record_is_marked_not_zero(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    empty = await manager.usage_dashboard(period="all")
    assert "no usage records at all" in empty["not_in_totals"]["before_records_begin"]["note"]

    first = time.time() - 3600
    _finish(manager, "codex-first", tokens=5, finished_at=first)

    everything = await manager.usage_dashboard(period="all")
    before = everything["not_in_totals"]["before_records_begin"]
    assert before is not None and before["records_begin"] == everything["records_begin"]
    assert "nothing on record before" in everything["text"]

    later = datetime.fromtimestamp(first + 600).isoformat(timespec="seconds")
    after_first = await manager.usage_dashboard(since=later)
    assert after_first["not_in_totals"]["before_records_begin"] is None
    await manager.shutdown()


async def test_the_text_view_is_rendered_from_the_same_figures(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _finish(manager, "codex-a", tokens=12_345)
    _finish(manager, "codex-b", status="cancelled", tokens=None, usage_status="missing")

    d = await manager.usage_dashboard(period="all")
    text = d["text"]

    assert "tokens 12,345  (input" in text
    assert "CLI started, no count" in text and "codex-b" in text
    for heading in ("TOTAL", "SESSIONS", "BY BACKEND", "BY QUOTA POOL", "BY MODEL", "NOT IN THE TOTALS", "LEFT OUT"):
        assert heading in text
    assert render_text({k: v for k, v in d.items() if k != "text"}) == text
    await manager.shutdown()


async def test_the_tool_answers_inside_a_nested_bridge(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _finish(manager, "codex-a", tokens=7)
    nested = Nesting(nested=True, via="env", parent_job="codex-parent")
    mcp, _ = create_server(settings=manager.settings, manager=manager, nesting=nested)

    raw = await mcp.call_tool("usage_dashboard", {"period": "all"})
    data = raw[1] if isinstance(raw, tuple) else json.loads(raw.content[0].text)
    assert _total(data["totals"]) == 7 and data["text"].startswith("agent-bridge usage, all")
    await manager.shutdown()
