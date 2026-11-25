"""Tests for usage normalization, per-turn agy deltas, quota parsing, and SQLite reporting."""

from __future__ import annotations

import time
from pathlib import Path

from agent_bridge.models import JobRecord
from agent_bridge.quota import (
    build_quota_advisory,
    parse_codex_rate_limits,
    parse_neon_usage_envelope,
)
from agent_bridge.storage import BridgeStore
from agent_bridge.usage import (
    compute_agy_usage_delta,
    normalize_usage,
    sum_agy_step_usages,
)


def test_normalize_usage_preserves_provider_metrics() -> None:
    neon, neon_meta = normalize_usage(
        "neon",
        {
            "input_tokens": 3084,
            "output_tokens": 4,
            "thinking_tokens": 0,
            "cache_read_tokens": 8727,
            "total_tokens": 3088,
        },
    )
    assert neon == {
        "input_tokens": 3084,
        "output_tokens": 4,
        "cached_tokens": 8727,
        "reasoning_tokens": 0,
        "total_tokens": 3088,
    }
    assert neon_meta is not None
    assert neon_meta["provider"] == "antigravity"
    assert neon_meta["cache_read_tokens"] == 8727

    codex, codex_meta = normalize_usage(
        "codex",
        {
            "input_tokens": 2000,
            "cached_input_tokens": 500,
            "cache_write_input_tokens": 100,
            "output_tokens": 300,
            "reasoning_output_tokens": 80,
        },
    )
    assert codex == {
        "input_tokens": 2000,
        "output_tokens": 300,
        "cached_tokens": 500,
        "reasoning_tokens": 80,
        "total_tokens": 2300,
        "cache_write_tokens": 100,
    }
    assert codex_meta is not None
    assert codex_meta["provider"] == "codex"
    assert codex_meta["cached_input_tokens"] == 500


def test_sum_agy_step_usages_and_delta() -> None:
    step_total = sum_agy_step_usages(
        [
            {"input_tokens": 1000, "output_tokens": 50, "thinking_tokens": 10, "cache_read_tokens": 200, "total_tokens": 1050},
            {"input_tokens": 500, "output_tokens": 25, "thinking_tokens": 5, "cache_read_tokens": 800, "total_tokens": 525},
        ]
    )
    assert step_total == {
        "input_tokens": 1500,
        "output_tokens": 75,
        "thinking_tokens": 15,
        "cache_read_tokens": 1000,
        "total_tokens": 1575,
    }

    delta = compute_agy_usage_delta(
        {"input_tokens": 13846, "output_tokens": 51, "thinking_tokens": 44, "cache_read_tokens": 8727, "total_tokens": 13897},
        {"input_tokens": 10762, "output_tokens": 47, "thinking_tokens": 44, "cache_read_tokens": 0, "total_tokens": 10809},
    )
    assert delta == {
        "input_tokens": 3084,
        "output_tokens": 4,
        "thinking_tokens": 0,
        "cache_read_tokens": 8727,
        "total_tokens": 3088,
    }

    # Non-monotonic counters return None (cannot trust delta).
    assert compute_agy_usage_delta({"total_tokens": 50}, {"total_tokens": 100}) is None


def test_quota_parsers_structured_fallback_and_unknown() -> None:
    codex_parsed = parse_codex_rate_limits(
        {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "planType": "plus",
                "primary": {"usedPercent": 13, "windowDurationMins": 300, "resetsAt": 1795618922},
                "secondary": {"usedPercent": 18, "windowDurationMins": 10080, "resetsAt": 1795888732},
                "spendControlReached": False,
                "rateLimitReachedType": None,
            },
        }
    )
    assert codex_parsed["status"] == "ok"
    assert codex_parsed["plan_type"] == "plus"
    assert codex_parsed["windows"][0]["window"] == "5h"
    assert codex_parsed["windows"][0]["remaining_percent"] == 87.0
    assert codex_parsed["windows"][1]["window"] == "weekly"

    codex_bad = parse_codex_rate_limits({})
    assert codex_bad["status"] == "unknown"

    neon_structured = parse_neon_usage_envelope(
        {
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "id": "gemini-5h",
                                    "window": "5h",
                                    "remaining_fraction": 0.8937,
                                    "reset_time": "2026-11-25T14:40:15Z",
                                }
                            ],
                        }
                    ]
                },
            }
        }
    )
    assert neon_structured["status"] == "ok"
    assert neon_structured["groups"][0]["windows"][0]["remaining_percent"] == 89.4
    assert neon_structured["groups"][0]["windows"][0]["used_percent"] == 10.6

    # Text fallback when command.data is missing
    neon_text = parse_neon_usage_envelope(
        {"response": "Gemini Models\tFive Hour Limit Remaining\t89%\t2026-11-25T14:40:15Z\n"}
    )
    assert neon_text["status"] == "ok"
    assert neon_text["groups"][0]["windows"][0]["remaining_percent"] == 89.0

    # Malformed or empty windows MUST return unknown (never ok!).
    assert parse_neon_usage_envelope({"response": "garbage"})["status"] == "unknown"
    assert parse_codex_rate_limits({"rateLimits": {"primary": {}}})["status"] == "unknown"
    assert parse_codex_rate_limits({"rateLimits": {"primary": {"usedPercent": True}}})["status"] == "unknown"
    assert (
        parse_neon_usage_envelope(
            {"command": {"data": {"groups": [{"name": "Gemini Models", "buckets": []}]}}}
        )["status"]
        == "unknown"
    )
    assert (
        parse_neon_usage_envelope(
            {"command": {"data": {"groups": [{"name": "Gemini Models", "buckets": [{}]}]}}}
        )["status"]
        == "unknown"
    )
    assert (
        parse_neon_usage_envelope(
            {"response": "Gemini Models\tFive Hour\tN/A\t2026-11-25T14:40:15Z\n"}
        )["status"]
        == "unknown"
    )


def test_sqlite_usage_scopes_coverage_and_compact(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "state")
    store.register_owner("owner-1", 12345)

    proj_a = str((tmp_path / "proj_a").resolve())
    proj_b = str((tmp_path / "proj_b").resolve())

    j1 = JobRecord(
        job_id="neon-11111111",
        owner_id="owner-1",
        backend="neon",
        cwd=proj_a,
        mode="read_only",
        requested_model="neon-medium",
        model="gemini-4-neon-medium",
        prompt_preview="review a",
        parent_run="pr-42",
        status="completed",
        started_at=time.time() - 4,
        finished_at=time.time(),
        duration_seconds=4.0,
        usage={"input_tokens": 1000, "output_tokens": 100, "cached_tokens": 2000, "reasoning_tokens": 20, "total_tokens": 1100},
        usage_status="exact",
    )
    store.insert_job(j1)
    store.finalize_job(j1)

    j2 = JobRecord(
        job_id="codex-22222222",
        owner_id="owner-1",
        backend="codex",
        cwd=proj_b,
        mode="edit",
        requested_model=None,
        model="gpt-6.2-sol",
        effort="medium",
        prompt_preview="edit b",
        parent_run="pr-99",
        status="failed",
        started_at=time.time() - 2,
        finished_at=time.time(),
        duration_seconds=2.0,
        usage=None,
        usage_status="missing",
    )
    store.insert_job(j2)
    store.finalize_job(j2)

    # In-flight job in proj_a
    j3 = JobRecord(
        job_id="codex-33333333",
        owner_id="owner-1",
        backend="codex",
        cwd=proj_a,
        mode="read_only",
        requested_model=None,
        model="gpt-6.2-sol",
        prompt_preview="running",
        parent_run="pr-42",
        status="running",
    )
    store.insert_job(j3)

    scoped = store.summarize_usage(
        period="today",
        owner_id="owner-1",
        cwd=proj_a,
        parent_run="pr-42",
        compact=True,
    )
    assert scoped["overview"]["runs"] == 1
    assert scoped["overview"]["in_flight"] == 1
    assert scoped["overview"]["usage_coverage"]["exact"] == 1
    assert scoped["by_backend"]["neon"]["tokens"]["total_tokens"] == 1100
    assert scoped["by_backend"]["codex"]["in_flight"] == 1
    assert "recent" not in scoped

    all_summary = store.summarize_usage(period="all", owner_id="owner-1")
    assert all_summary["overview"]["runs"] == 2
    assert all_summary["overview"]["usage_coverage"]["exact"] == 1
    assert all_summary["overview"]["usage_coverage"]["missing"] == 1
    assert len(all_summary["recent"]) == 2


def test_legacy_jobs_and_usage_upgrade_import_is_idempotent(tmp_path: Path) -> None:
    """Upgrade migration imports v1 jobs.json & usage.jsonl once without duplicates."""
    import json

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = str((tmp_path / "repo").resolve())

    (state_dir / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "job_id": "codex-leg00001",
                    "backend": "codex",
                    "cwd": repo_dir,
                    "mode": "read_only",
                    "requested_model": None,
                    "model": "gpt-6.2-sol",
                    "prompt_preview": "legacy completed",
                    "session_id": "thr-legacy-1",
                    "status": "completed",
                    "created_at": time.time() - 100,
                    "started_at": time.time() - 99,
                    "finished_at": time.time() - 95,
                    "duration_seconds": 4.0,
                    "response": "legacy answer",
                    "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                },
                {
                    "job_id": "neon-leg00002",
                    "backend": "neon",
                    "cwd": repo_dir,
                    "mode": "read_only",
                    "requested_model": "neon-high",
                    "model": "gemini-4-neon-high",
                    "prompt_preview": "legacy interrupted",
                    "session_id": "conv-legacy-2",
                    "status": "running",
                    "created_at": time.time() - 50,
                },
            ]
        ),
        encoding="utf-8",
    )
    (state_dir / "usage.jsonl").write_text(
        json.dumps(
            {
                "ts": time.time() - 95,
                "iso_time": "2026-11-25T10:00:00+05:30",
                "session_key": "sess-v1",
                "job_id": "codex-leg00001",
                "backend": "codex",
                "model": "gpt-6.2-sol",
                "mode": "read_only",
                "status": "completed",
                "duration_seconds": 4.0,
                "cwd": repo_dir,
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    store1 = BridgeStore(state_dir)
    rec1 = store1.get_job("codex-leg00001")
    assert rec1 is not None
    assert rec1.session_id == "thr-legacy-1"
    assert rec1.status == "completed"
    assert rec1.response == "legacy answer"

    rec2 = store1.get_job("neon-leg00002")
    assert rec2 is not None
    assert rec2.status == "failed"
    assert rec2.failure_reason == "owner_exited"
    assert rec2.provider_state == "unknown"

    summary = store1.summarize_usage(period="all", owner_id="new-owner", compact=True)
    assert summary["overview"]["runs"] == 1
    assert summary["by_backend"]["codex"]["tokens"]["total_tokens"] == 120

    # Re-opening BridgeStore does not duplicate legacy rows.
    store2 = BridgeStore(state_dir)
    summary2 = store2.summarize_usage(period="all", owner_id="new-owner", compact=True)
    assert summary2["overview"]["runs"] == 1


def test_weekly_reset_awareness_and_three_pool_advisory() -> None:
    """Weekly quota about to reset with unused capacity gets top `use_before_reset` priority & alert."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 11, 28, 4, 0, 0, tzinfo=timezone.utc)
    codex_weekly_reset = int((now + timedelta(hours=14)).timestamp())  # 14h left in week!
    codex_5h_reset = int((now + timedelta(hours=3)).timestamp())
    gemini_weekly_reset = (now + timedelta(days=6, hours=10)).isoformat().replace("+00:00", "Z")
    gemini_5h_reset = (now + timedelta(hours=2, minutes=30)).isoformat().replace("+00:00", "Z")

    codex_q = parse_codex_rate_limits(
        {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "planType": "plus",
                "primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": codex_5h_reset},
                "secondary": {"usedPercent": 45, "windowDurationMins": 10080, "resetsAt": codex_weekly_reset},
            },
        },
        now_utc=now,
    )
    neon_q = parse_neon_usage_envelope(
        {
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "description": "Models within this group: Gemini 4 Neon, Gemini 3.8 Flash",
                            "buckets": [
                                {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.92, "reset_time": gemini_weekly_reset},
                                {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.75, "reset_time": gemini_5h_reset},
                            ],
                        },
                        {
                            "name": "Claude and GPT models",
                            "description": "Models within this group: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B",
                            "buckets": [
                                {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1.0, "reset_time": "1970-01-01T00:00:00Z"},
                                {"id": "3p-5h", "window": "5h", "remaining_fraction": 1.0, "reset_time": "1970-01-01T00:00:00Z"},
                            ],
                        },
                    ]
                },
            }
        },
        now_utc=now,
    )

    advisory = build_quota_advisory(
        {"codex": codex_q, "neon": neon_q},
        now_utc=now,
        codex_default_label="gpt-6.2-sol (effort=high)",
    )
    assert advisory["recommended_order"][0] == "codex"
    assert len(advisory["expiring_weekly_alerts"]) == 1
    assert "codex" in advisory["expiring_weekly_alerts"][0].lower()

    pools_by_name = {p["pool"]: p for p in advisory["pools"]}
    assert set(pools_by_name.keys()) == {"codex", "agy_gemini", "agy_claude_gpt"}
    assert pools_by_name["codex"]["priority"] == "use_before_reset"
    assert pools_by_name["codex"]["weekly"]["signal"] == "expiring_soon_use_it"
    assert pools_by_name["codex"]["weekly"]["resets_in"] == "14h"
    assert pools_by_name["agy_claude_gpt"]["weekly"]["signal"] == "idle_full"
    assert pools_by_name["agy_claude_gpt"]["priority"] == "high"
    assert "Claude Opus 5.5" in pools_by_name["agy_claude_gpt"]["models"]

    # Upcoming resets sorted soonest-first across all involved models!
    assert len(advisory["upcoming_resets"]) == 4
    assert advisory["upcoming_resets"][0]["pool"] == "agy_gemini"
    assert advisory["upcoming_resets"][0]["window"] == "5h"
    assert advisory["upcoming_resets"][2]["pool"] == "codex"
    assert advisory["upcoming_resets"][2]["window"] == "weekly"


def test_sqlite_usage_tracks_agy_claude_gpt_pool_separately(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "state_pools")
    store.register_owner("o1", 4242)
    repo = str((tmp_path / "repo").resolve())

    j_gem = JobRecord(
        job_id="neon-gem1",
        owner_id="o1",
        backend="neon",
        quota_pool="agy_gemini",
        cwd=repo,
        mode="read_only",
        requested_model="neon-high",
        model="gemini-4-neon-high",
        prompt_preview="gemini review",
        status="completed",
        started_at=time.time() - 3,
        finished_at=time.time(),
        duration_seconds=3.0,
        usage={"input_tokens": 500, "output_tokens": 50, "total_tokens": 550},
        usage_status="exact",
    )
    j_3p = JobRecord(
        job_id="neon-3p1",
        owner_id="o1",
        backend="neon",
        quota_pool="agy_claude_gpt",
        cwd=repo,
        mode="read_only",
        requested_model="claude-sonnet-high",
        model="claude-sonnet-5-5-high",
        prompt_preview="claude via agy",
        status="completed",
        started_at=time.time() - 2,
        finished_at=time.time(),
        duration_seconds=2.0,
        usage={"input_tokens": 800, "output_tokens": 100, "total_tokens": 900},
        usage_status="exact",
    )
    for j in (j_gem, j_3p):
        store.insert_job(j)
        store.finalize_job(j)

    summary = store.summarize_usage(period="today", owner_id="o1", compact=True)
    assert summary["by_backend"]["neon"]["runs"] == 2
    assert summary["by_quota_pool"]["agy_gemini"]["runs"] == 1
    assert summary["by_quota_pool"]["agy_gemini"]["tokens"]["total_tokens"] == 550
    assert summary["by_quota_pool"]["agy_claude_gpt"]["runs"] == 1
    assert summary["by_quota_pool"]["agy_claude_gpt"]["tokens"]["total_tokens"] == 900

    only_3p = store.summarize_usage(period="today", owner_id="o1", quota_pool="agy_claude_gpt", compact=True)
    assert only_3p["overview"]["runs"] == 1
    assert list(only_3p["by_quota_pool"].keys()) == ["agy_claude_gpt"]
