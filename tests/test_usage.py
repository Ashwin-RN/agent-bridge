"""Tests for usage normalization, per-turn agy deltas, quota parsing, and SQLite reporting."""

from __future__ import annotations

import time
from pathlib import Path

from agent_bridge.models import JobRecord
from agent_bridge.quota import parse_codex_rate_limits, parse_neon_usage_envelope
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

    # Malformed returns unknown gracefully
    assert parse_neon_usage_envelope({"response": "garbage"})["status"] == "unknown"


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
