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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_bridge import dashboard as dashboard_module
from agent_bridge import jobs as jobs_module
from agent_bridge.config import Settings
from agent_bridge.dashboard import (
    BACKENDS,
    QUOTA_POOLS,
    build_dashboard,
    build_quota_section,
    render_text,
    resolve_window,
)
from agent_bridge.jobs import JobManager
from agent_bridge.models import JobRecord
from agent_bridge.nesting import Nesting
from agent_bridge.quota import (
    claude_quota_from_rate_limit,
    enrich_quota_reading,
    merge_claude_quota,
    parse_codex_rate_limits,
    parse_neon_usage_envelope,
)
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
    provider_state: str | None = None,
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
    record.provider_state = provider_state  # type: ignore[assignment]
    store.finalize_job(record, touched_provider=invoked, attempts=attempts)


def _dump(db_path: Path) -> list[str]:
    """Every table's schema and rows, as SQL text, read without writing."""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def _cannot_tell(d: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {j["job_id"]: j for j in d["not_in_totals"]["cannot_tell"]["jobs"]}


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
    assert len(d["left_out"]) == 1 and "cost estimate" in d["left_out"][0]
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
    # No launch recorded, but the bridge could not confirm the process tree stopped.
    _finish(manager, "codex-unconfirmed", status="cancelled", tokens=None, usage_status="missing",
            invoked=False, provider_state="possibly_running")
    with sqlite3.connect(manager.store.db_path) as conn:
        conn.execute("DELETE FROM jobs WHERE job_id = 'codex-pruned'")

    d = await manager.usage_dashboard(period="all")

    nit = d["not_in_totals"]
    assert nit["cli_started_no_count"]["job_ids"] == ["codex-lost", "codex-review"]
    assert nit["cli_started_no_count"]["by_status"] == {"cancelled": 1, "completed": 1}
    # Neither a pruned job row nor an unconfirmed end is read as "nothing spent".
    reasons = {job_id: entry["reason"] for job_id, entry in _cannot_tell(d).items()}
    assert reasons == {"codex-pruned": "job_row_pruned", "codex-unconfirmed": "end_not_confirmed"}
    assert d["counted_as_nothing_spent"]["job_ids"] == ["codex-never"]
    assert (d["totals"]["runs"], _total(d["totals"])) == (5, 0)
    await manager.shutdown()


GONE_PID = 999_999


def _manager_with_a_closed_window(tmp_path: Path) -> JobManager:
    """A bridge, plus a job left running by another window whose process has since gone."""
    manager = JobManager(
        _settings(tmp_path), owner_id=ME, pid=os.getpid(), is_pid_alive_fn=lambda pid: pid != GONE_PID
    )
    manager.store.register_owner("owner-gone", GONE_PID)
    left = _record("codex-left", owner="owner-gone", backend="codex", model="gpt-6.2-sol", status="running")
    left.started_at = time.time() - 120
    manager.store.insert_job(left)
    # It had finished a first attempt before the window closed.
    manager.store.record_attempt_usage(
        left,
        {"status": "failed", "usage_status": "exact", "usage": _usage(300), "duration_seconds": 5.0},
        attempt_number=1,
    )
    return manager


async def test_the_tool_writes_nothing_and_lists_a_job_whose_window_closed_under_cannot_tell(tmp_path: Path) -> None:
    manager = _manager_with_a_closed_window(tmp_path)
    mcp, _ = create_server(settings=manager.settings, manager=manager)
    before = _dump(manager.store.db_path)

    raw = await mcp.call_tool("usage", {"period": "all", "view": "dashboard", "verbose": True})
    d = raw[1] if isinstance(raw, tuple) else json.loads(raw.content[0].text)

    assert _dump(manager.store.db_path) == before
    stored = manager.store.get_job("codex-left")
    assert stored is not None and stored.status == "running"  # not recovered: the tool only read
    entry = _cannot_tell(d)["codex-left"]
    assert entry["reason"] == "window_closed" and entry["status_on_record"] == "running"
    assert entry["tokens_recorded_for_earlier_attempts"] == 300
    assert "closed" in d["not_in_totals"]["cannot_tell"]["reasons"]["window_closed"]
    assert "codex-left (its window closed" in d["text"]
    assert d["not_in_totals"]["running_or_queued"]["count"] == 0
    assert (d["totals"]["runs"], _total(d["totals"]), d["counted_as_nothing_spent"]["count"]) == (0, 0, 0)
    await manager.shutdown()


async def test_the_usage_summary_still_recovers_the_job_and_the_dashboard_reads_it_the_same_after(tmp_path: Path) -> None:
    manager = _manager_with_a_closed_window(tmp_path)

    await manager.get_usage_summary(period="all")

    stored = manager.store.get_job("codex-left")
    assert stored is not None and (stored.status, stored.failure_reason) == ("failed", "owner_exited")
    d = await manager.usage_dashboard(period="all")
    assert _cannot_tell(d)["codex-left"]["reason"] == "window_closed"
    assert d["counted_as_nothing_spent"]["count"] == 0
    await manager.shutdown()


async def test_the_view_says_what_it_does_not_cover(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    d = await manager.usage_dashboard(period="all")

    assert d["covers"] == "Covers jobs run through the bridge only."
    assert d["not_covered"] == [
        "the host session's own tokens",
        "a host's own subagents",
        "CLI runs made outside the bridge",
    ]
    assert (
        "Covers jobs run through the bridge only. Not in it: the host session's own tokens, "
        "a host's own subagents, CLI runs made outside the bridge."
    ) in d["text"].splitlines()
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


async def test_claude_whole_job_usage_includes_subagents_in_dashboard_totals(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _finish(manager, "claude-full", backend="claude", tokens=1200,
            metrics={"provider": "claude", "native_subagents": 2, "usage_scope": "whole_job"})

    dashboard = await manager.usage_dashboard(period="all")

    assert _total(dashboard["totals"]) == 1200
    assert dashboard["not_in_totals"]["claude_subagents"]["job_ids"] == []
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

    assert _line(text, "  total").split() == ["total", "2", "12,345", "4s"]
    assert "CLI started, no count" in text and "codex-b" in text
    headings = ("QUOTA LEFT", "USED THROUGH THE BRIDGE", "NOT IN THE TOTALS")
    assert [line.split("  ")[0] for line in text.splitlines() if line.startswith(headings)] == list(headings)
    assert render_text({k: v for k, v in d.items() if k != "text"}) == text
    await manager.shutdown()


async def test_the_tool_answers_inside_a_nested_bridge(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _finish(manager, "codex-a", tokens=7)
    nested = Nesting(nested=True, via="env", parent_job="codex-parent")
    mcp, _ = create_server(settings=manager.settings, manager=manager, nesting=nested)

    raw = await mcp.call_tool("usage", {"period": "all", "view": "dashboard", "verbose": True})
    data = raw[1] if isinstance(raw, tuple) else json.loads(raw.content[0].text)
    assert _total(data["totals"]) == 7 and data["text"].startswith("agent-bridge usage, all")
    # Without `verbose`, the view is the text alone.
    raw = await mcp.call_tool("usage", {"period": "all", "view": "dashboard"})
    data = raw[1] if isinstance(raw, tuple) else json.loads(raw.content[0].text)
    assert list(data) == ["text"]
    await manager.shutdown()


# --- The quota section ---


def _from_now(hours: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _codex_reading(
    *, five_used: float = 44, week_used: float = 64, five_resets: float = 3, week_resets: float = 20
) -> dict[str, Any]:
    """A reading as `codex app-server` gives it, through the bridge's own parser."""
    return parse_codex_rate_limits(
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": five_used,
                    "windowDurationMins": 300,
                    "resetsAt": _from_now(five_resets).timestamp(),
                },
                "secondary": {
                    "usedPercent": week_used,
                    "windowDurationMins": 10080,
                    "resetsAt": _from_now(week_resets).timestamp(),
                },
            }
        }
    )


def _agy_reading(*, gemini_week_resets: float = 140) -> dict[str, Any]:
    """A reading as `agy --print=/usage` gives it: two pools, the second with a 5h window not yet started."""

    def bucket(bucket_id: str, window: str, left: float, reset: str) -> dict[str, Any]:
        return {"id": bucket_id, "window": window, "remaining_fraction": left, "reset_time": reset}

    def iso(hours: float) -> str:
        return _from_now(hours).isoformat(timespec="seconds")

    return parse_neon_usage_envelope(
        {
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                bucket("gemini-weekly", "weekly", 0.793, iso(gemini_week_resets)),
                                bucket("gemini-5h", "5h", 1.0, iso(5)),
                            ],
                        },
                        {
                            "name": "Claude and GPT models",
                            "buckets": [
                                bucket("3p-weekly", "weekly", 0.5, iso(120)),
                                bucket("3p-5h", "5h", 1.0, "1970-01-01T00:00:00Z"),
                            ],
                        },
                    ]
                }
            }
        }
    )


def _stand_in_clis(
    manager: JobManager,
    monkeypatch: pytest.MonkeyPatch,
    *,
    codex: dict[str, Any] | None = None,
    agy: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Answer the two quota probes from here, and count them. No CLI is started."""
    calls = {"codex": 0, "agy": 0}

    async def fetch_codex(_cmd: list[str]) -> dict[str, Any]:
        calls["codex"] += 1
        return codex if codex is not None else _codex_reading()

    async def fetch_agy(_cmd: list[str]) -> dict[str, Any]:
        calls["agy"] += 1
        return agy if agy is not None else _agy_reading()

    monkeypatch.setattr(jobs_module, "fetch_codex_quota", fetch_codex)
    monkeypatch.setattr(jobs_module, "fetch_neon_quota", fetch_agy)
    monkeypatch.setattr(manager.codex_runner, "resolve_command", lambda: ["codex"])
    monkeypatch.setattr(manager.neon_runner, "resolve_command", lambda: ["agy"])
    return calls


def _pools(d: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["pool"]: p for p in d["quota"]["pools"]}


def _line(text: str, start: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(start))


async def test_the_view_opens_with_what_is_left_of_each_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _stand_in_clis(manager, monkeypatch)
    _finish(manager, "codex-a", tokens=1000)

    d = await manager.usage_dashboard(period="all")

    # Every pool, in the same order each time, whether or not it could be read.
    assert [p["pool"] for p in d["quota"]["pools"]] == ["codex", "agy_gemini", "agy_claude_gpt", "claude"]
    pools = _pools(d)
    assert (pools["codex"]["five_hour"]["remaining_percent"], pools["codex"]["weekly"]["remaining_percent"]) == (56, 36)
    assert pools["agy_gemini"]["weekly"]["remaining_percent"] == 79.3
    assert pools["codex"]["source"] == "live" and pools["codex"]["as_of"]

    text = d["text"]
    lines = text.splitlines()
    assert lines[2].startswith("QUOTA LEFT") and lines[2:] == render_text(
        {k: v for k, v in d.items() if k != "text"}
    ).splitlines()[2:]
    assert text.index("QUOTA LEFT") < text.index("USED THROUGH THE BRIDGE")
    codex_line = _line(text, "  Codex")
    assert "\u2588" * 6 + "\u2591" * 4 + "  56%" in codex_line  # the 5h window first
    assert "\u2588" * 4 + "\u2591" * 6 + "  36%" in codex_line  # then the week
    assert codex_line.index("56%") < codex_line.index("36%")
    assert "\u2588" * 8 + "\u2591" * 2 + "  79%" in _line(text, "  Antigravity Gemini")
    # A week about to reset with quota unused is the one line worth acting on.
    assert any(
        note.startswith("Codex: 36% of the week is unused and resets in ") and note.endswith("Use it first.")
        for note in d["quota"]["notes"]
    )
    assert "  * Codex: 36% of the week is unused" in text
    await manager.shutdown()


async def test_a_window_not_started_spent_out_of_date_or_missing_is_shown_as_what_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    spent = _codex_reading(five_used=100, week_used=10, five_resets=2, week_resets=100)
    # A reading the CLI gave with a weekly reset that has already passed.
    _stand_in_clis(manager, monkeypatch, codex=spent, agy=_agy_reading(gemini_week_resets=-1))
    # The Claude plan is known only from its last job, which reported one window, yesterday.
    reported = claude_quota_from_rate_limit(
        {"unifiedWindows": {"seven_day": {"utilization": 0.25, "resetsAt": _from_now(90).timestamp()}}},
        fetched_at=(datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds"),
    )
    assert reported is not None
    manager.store.set_cached_quota("claude", reported)

    d = await manager.usage_dashboard(period="all")

    pools, text = _pools(d), d["text"]
    assert pools["codex"]["five_hour"]["state"] == "exhausted" and pools["codex"]["priority"] == "avoid"
    assert any(n.startswith("Codex: out of quota until it resets in ") for n in d["quota"]["notes"])
    assert "\u2591" * 10 + "   0%" in _line(text, "  Codex")

    assert pools["agy_claude_gpt"]["five_hour"]["state"] == "not_started"
    assert "100%  not started" in _line(text, "  Antigravity Claude+GPT")

    assert pools["agy_gemini"]["weekly"]["state"] == "reset_passed"
    assert "reset passed" in _line(text, "  Antigravity Gemini")
    assert "Antigravity Gemini: a reset time in this reading has passed, so it is out of date." in d["quota"]["notes"]

    claude = pools["claude"]
    assert claude["five_hour"] is None and claude["weekly"]["remaining_percent"] == 75.0
    assert claude["source"] == "last_claude_job" and len(claude["as_of"]) == len("11-26 21:14")  # dated: not today
    claude_line = _line(text, "  Claude plan")
    assert "no reading" in claude_line and "75%" in claude_line and claude["as_of"] in claude_line
    assert any("Your own Claude Code and claude.ai use draw on it too" in n for n in d["quota"]["notes"])
    await manager.shutdown()


async def test_a_pool_that_cannot_be_read_is_still_listed_and_says_why(tmp_path: Path) -> None:
    manager = _manager(tmp_path)  # no CLI at any of its paths

    d = await manager.usage_dashboard(period="all")

    pools = _pools(d)
    assert list(pools) == list(QUOTA_POOLS)
    assert all(p["status"] == "unknown" and p["reason"] for p in pools.values())
    assert "AGENT_BRIDGE_CODEX_BIN does not exist" in pools["codex"]["reason"]
    assert "AGENT_BRIDGE_AGY_BIN does not exist" in pools["agy_claude_gpt"]["reason"]
    assert "after a Claude job runs through the bridge" in pools["claude"]["reason"]
    for label in ("Codex", "Antigravity Gemini", "Antigravity Claude+GPT", "Claude plan"):
        assert "no reading: " in _line(d["text"], f"  {label}")
    assert all(len(p["reason"]) <= 80 for p in pools.values())
    await manager.shutdown()


async def test_a_quota_reading_taken_for_the_view_is_not_stored_and_a_fresh_cached_one_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    calls = _stand_in_clis(manager, monkeypatch)
    _finish(manager, "codex-a", tokens=7)
    mcp, _ = create_server(settings=manager.settings, manager=manager)
    before = _dump(manager.store.db_path)

    raw = await mcp.call_tool("usage", {"period": "all", "view": "dashboard", "quota": True})
    d = raw[1] if isinstance(raw, tuple) else json.loads(raw.content[0].text)

    # The quota view is built from the dashboard's own readings: one probe per CLI, not two.
    assert calls == {"codex": 1, "agy": 1}
    assert _pools(d)["codex"]["five_hour"] is not None and "Codex" in d["text"]
    assert _dump(manager.store.db_path) == before  # the reading was shown, and nothing was written

    # A reading the summary view has stored is used while it is fresh, with no second probe.
    await manager.get_quota(["codex"])
    assert calls["codex"] == 2
    again = await manager.usage_dashboard(period="all")
    assert calls == {"codex": 2, "agy": 2}
    assert _pools(again)["codex"]["source"] == "cached" and _pools(again)["agy_gemini"]["source"] == "live"
    await manager.shutdown()


async def test_the_text_lists_models_under_their_pool_and_only_the_caveats_that_hold_something(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    first = time.time() - 3600
    _finish(manager, "codex-a", tokens=1000, finished_at=first)
    _finish(manager, "neon-a", backend="neon", model="gemini-4-neon-high", tokens=70)
    _finish(manager, "neon-b", backend="neon", model="claude-sonnet-5-5-high", tokens=200)

    d = await manager.usage_dashboard(period="all")

    assert d["by_model"]["neon:gemini-4-neon-high"]["quota_pool"] == "agy_gemini"
    assert d["by_model"]["neon:claude-sonnet-5-5-high"]["quota_pool"] == "agy_claude_gpt"
    lines = d["text"].splitlines()
    used = lines[lines.index(_line(d["text"], "USED THROUGH THE BRIDGE")) :]
    names = [line.split("  ")[1 if not line.startswith("    ") else 2] for line in used[2:9]]
    assert names == [
        "Codex", "gpt-6.2-sol", "Antigravity Gemini", "gemini-4-neon-high",
        "Antigravity Claude+GPT", "claude-sonnet-5-5-high", "Claude plan",
    ]
    assert used[9].split() == ["total", "3", "1,270", "6s"]
    assert "  outcomes: 3 completed" in lines

    # Nothing is under any caveat but the stretch before the first record, which is not a zero.
    assert d["not_in_totals"]["partial_counts"]["count"] == 0
    caveats = lines[lines.index("NOT IN THE TOTALS") + 1 : lines.index("", lines.index("NOT IN THE TOTALS"))]
    assert len(caveats) == 1 and caveats[0].split("  ")[1] == "before records begin"
    assert "nothing on record before" in caveats[0]

    later = datetime.fromtimestamp(first + 600).isoformat(timespec="seconds")
    after_first = await manager.usage_dashboard(since=later)
    assert "NOT IN THE TOTALS: nothing" in after_first["text"].splitlines()
    assert "before records begin" not in after_first["text"]
    await manager.shutdown()


def _empty_view() -> dict[str, Any]:
    """The dict of a period in which nothing ran, as `build_dashboard` gives it, with no quota section."""
    records = {"usage_rows": [], "running_jobs": [], "first_record_ts": None, "session_started_at": None}
    now = time.time()
    view = build_dashboard(records, window=resolve_window("session", None, None, now_ts=now), owner_id=ME, now_ts=now)
    return {k: v for k, v in view.items() if k != "text"}


EMPTY_VIEW = _empty_view()


# --- After review: odd readings, and what the quota section must not touch ---


def _weekly(left: float, *, resets: float | None = 50, kind: str = "weekly", **extra: Any) -> dict[str, Any]:
    reset = _from_now(resets).isoformat(timespec="seconds") if resets is not None else None
    return {"window": kind, "used_percent": 100 - left, "remaining_percent": left, "reset_time": reset, **extra}


def _section(readings: dict[str, Any]) -> dict[str, Any]:
    enriched = {name: enrich_quota_reading(reading) for name, reading in readings.items()}
    return build_quota_section(enriched, now_ts=time.time())


def _cells(section: dict[str, Any], label: str) -> str:
    text = render_text({**EMPTY_VIEW, "quota": section})
    return _line(text, f"  {label}")


async def test_quota_readings_change_no_total_and_a_bad_period_or_one_failing_cli_does_not_spoil_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    calls = _stand_in_clis(manager, monkeypatch)
    _finish(manager, "codex-a", tokens=1000)
    _finish(manager, "codex-b", status="cancelled", tokens=None, usage_status="missing")

    read = await manager.usage_dashboard(period="all")
    assert all(p["status"] == "ok" for p in list(_pools(read).values())[:3])

    # The readings are shown beside the records and added to nothing: with every pool read, the
    # figures are the ones the records alone give.
    records = manager.store.dashboard_records(
        since_ts=None, until_ts=None, session_owner_id=ME, only_session=False, is_pid_alive_fn=lambda _pid: True
    )
    alone = build_dashboard(records, window=resolve_window("all", None, None, now_ts=time.time()), owner_id=ME,
                            now_ts=time.time())
    assert alone["quota"] is None and "QUOTA LEFT" not in alone["text"]
    for key in ("totals", "by_backend", "by_quota_pool", "by_model", "not_in_totals", "counted_as_nothing_spent"):
        assert read[key] == alone[key], key
    assert _total(read["totals"]) == 1000

    # A period that is refused is refused before any CLI is asked.
    asked = dict(calls)
    with pytest.raises(ValueError, match="Unknown period"):
        await manager.usage_dashboard(period="fortnight")
    assert calls == asked

    # Nor is one asked when the records cannot be read.
    def unreadable(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("records unreadable")

    with monkeypatch.context() as patched:
        patched.setattr(manager.store, "dashboard_records", unreadable)
        with pytest.raises(RuntimeError, match="records unreadable"):
            await manager.usage_dashboard(period="all")
    assert calls == asked

    # One CLI that fails says so on its own row; the other pools are still read.
    async def broken(_cmd: list[str]) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs_module, "fetch_codex_quota", broken)
    d = await manager.usage_dashboard(period="all")
    pools = _pools(d)
    assert pools["codex"]["status"] == "unknown" and "boom" in pools["codex"]["reason"]
    assert pools["agy_gemini"]["status"] == "ok" and pools["agy_claude_gpt"]["status"] == "ok"
    await manager.shutdown()


def test_an_odd_reading_is_shown_for_what_it_says() -> None:
    section = _section(
        {
            # 1.5% is nearly out, not out: nothing in the reading says the limit was reached.
            "codex": {"backend": "codex", "status": "ok", "fetched_at": _from_now(0).isoformat(),
                      "windows": [_weekly(1.5)]},
            "neon": {
                "backend": "neon", "status": "ok", "fetched_at": _from_now(0).isoformat(),
                "groups": [
                    # One pool reported as two groups: neither drops the other.
                    {"name": "Gemini 5h", "pool": "agy_gemini", "windows": [_weekly(90, resets=3, kind="5h")]},
                    {"name": "Gemini weekly", "pool": "agy_gemini", "windows": [_weekly(60)]},
                    # A group that names another backend's pool is not taken for it.
                    {"name": "stray", "pool": "codex", "windows": [_weekly(11, resets=3, kind="5h")]},
                    # 99.5% with no reset time is not a window that never started.
                    {"name": "Claude and GPT models", "pool": "agy_claude_gpt",
                     "windows": [_weekly(99.5, resets=None, kind="5h")]},
                ],
            },
            # A limit on one model only is not the plan's week.
            "claude": {"backend": "claude", "status": "ok", "fetched_at": _from_now(0).isoformat(),
                       "windows": [_weekly(40, kind="seven_day_sonnet")]},
        }
    )
    pools = {p["pool"]: p for p in section["pools"]}
    assert [p["pool"] for p in section["pools"]] == ["codex", "agy_gemini", "agy_claude_gpt", "claude"]

    assert pools["codex"]["weekly"]["remaining_percent"] == 1.5 and pools["codex"]["weekly"]["state"] == "ok"
    assert pools["codex"]["five_hour"] is None
    assert any(n.startswith("Codex: nearly out of quota, 2% left until it resets in ") for n in section["notes"])
    assert not any("Codex: out of quota" in n for n in section["notes"])

    gemini = pools["agy_gemini"]
    assert (gemini["five_hour"]["remaining_percent"], gemini["weekly"]["remaining_percent"]) == (90, 60)

    third_party = pools["agy_claude_gpt"]["five_hour"]
    assert third_party["state"] == "ok" and third_party["resets_in"] is None
    assert "100%  reset unknown" in _cells(section, "Antigravity Claude+GPT")
    assert "not started" not in _cells(section, "Antigravity Claude+GPT")

    assert pools["claude"]["status"] == "unknown"
    assert pools["claude"]["reason"] == "the reading holds no 5h or weekly window"
    assert "None" not in render_text({**EMPTY_VIEW, "quota": section})


async def test_the_text_is_rendered_without_the_clock_and_shows_each_caveat_that_holds_something(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _stand_in_clis(manager, monkeypatch)
    _finish(manager, "codex-a", tokens=1000)
    _finish(manager, "claude-partial", backend="claude", model="claude-sonnet-5-5", status="failed", tokens=50,
            usage_status="partial", metrics={"provider": "claude", "native_subagents": 0})
    _finish(manager, "claude-sub", backend="claude", model="claude-sonnet-5-5", tokens=300,
            metrics={"provider": "claude", "native_subagents": 2})
    _finish(manager, "codex-never", status="cancelled", tokens=None, usage_status="missing", invoked=False)
    running = _record("codex-running", owner=ME, backend="codex", model="gpt-6.2-sol", status="running")
    running.started_at = time.time() - 30
    manager.store.insert_job(running)

    d = await manager.usage_dashboard(period="all")
    text = d["text"]


    def row(label: str) -> str:
        return " ".join(_line(text, f"  {label}").split())

    assert row("partial count") == "partial count 1 50 tokens reported, not added: claude-partial"
    assert row("Claude subagents") == "Claude subagents 1 2 subagents, tokens not reported: claude-sub"
    assert row("still queued or running").startswith("still queued or running 1 codex-running (running ")
    assert "  CLI never started, counted as nothing spent: 1  codex-never" in text
    for absent in ("cannot tell", "CLI started, no count", "subagents not on record"):
        assert absent not in text

    # Rendering reads no clock: every time and countdown in the text is already in the dict.
    class NoClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            raise AssertionError("render_text read the clock")

        @classmethod
        def fromtimestamp(cls, *args: Any, **kwargs: Any) -> datetime:
            raise AssertionError("render_text read the clock")

    monkeypatch.setattr(dashboard_module, "datetime", NoClock)
    monkeypatch.setattr(time, "time", lambda: (_ for _ in ()).throw(AssertionError("render_text read the clock")))
    assert render_text({k: v for k, v in d.items() if k != "text"}) == text
    monkeypatch.undo()
    await manager.shutdown()


def test_a_period_with_time_but_no_finished_run_is_not_called_empty() -> None:
    view = json.loads(json.dumps(EMPTY_VIEW))
    assert "  no runs in this period" in render_text(view).splitlines()
    view["totals"]["duration_seconds"] = 5.0  # a failed first attempt, whose retry ended after the period
    view["by_quota_pool"]["codex"]["duration_seconds"] = 5.0
    text = render_text(view)
    assert "no runs in this period" not in text
    assert _line(text, "  total").split() == ["total", "0", "0", "5s"]


async def test_a_claude_window_is_kept_until_its_own_reset_and_carries_its_own_read_time(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    three_days_ago = (datetime.now().astimezone() - timedelta(days=3)).isoformat(timespec="seconds")
    week = claude_quota_from_rate_limit(
        {"unifiedWindows": {"seven_day": {"utilization": 0.4, "resetsAt": _from_now(60).timestamp()}}},
        fetched_at=three_days_ago,
    )
    # A later job hit the 5h limit and reported that window only.
    five = claude_quota_from_rate_limit(
        {"status": "rejected", "rateLimitType": "five_hour", "utilization": 1.0, "resetsAt": _from_now(2).timestamp()}
    )
    assert week is not None and five is not None
    merged = merge_claude_quota(week, five)
    manager.store.set_cached_quota("claude", merged)

    claude = _pools(await manager.usage_dashboard(period="all"))["claude"]
    assert (claude["five_hour"]["remaining_percent"], claude["weekly"]["remaining_percent"]) == (0.0, 60.0)
    # The week was read three days ago, and the row does not pass it off as read today.
    assert claude["five_hour"]["read_at"] == five["fetched_at"] and claude["weekly"]["read_at"] == three_days_ago
    assert claude["read_at"] == three_days_ago and len(claude["as_of"]) == len("11-24 21:14")

    # Once the 5h window has reset, the week is still on show, and the limit that was hit is not.
    for window in merged["windows"]:
        if window["window"] == "5h":
            window["reset_time"] = _from_now(-1).isoformat(timespec="seconds")
    manager.store.set_cached_quota("claude", merged)
    reading = (await manager.get_quota(["claude"]))["claude"]
    assert reading["status"] == "ok" and [w["window"] for w in reading["windows"]] == ["weekly"]
    assert reading["rate_limit_reached"] is False
    d = await manager.usage_dashboard(period="all")
    claude = _pools(d)["claude"]
    assert claude["five_hour"] is None and claude["weekly"]["remaining_percent"] == 60.0
    claude_line = _line(d["text"], "  Claude plan")
    assert "no reading" in claude_line and "60%" in claude_line and "out of quota" not in d["text"]

    # When every window has reset, there is no reading.
    for window in merged["windows"]:
        window["reset_time"] = _from_now(-1).isoformat(timespec="seconds")
    manager.store.set_cached_quota("claude", merged)
    claude = _pools(await manager.usage_dashboard(period="all"))["claude"]
    assert claude["status"] == "unknown" and "after a Claude job runs through the bridge" in claude["reason"]
    await manager.shutdown()
