"""Tests for usage normalization and persistent ledger reporting."""

from __future__ import annotations

from pathlib import Path

from agent_bridge.usage import UsageTracker, normalize_usage


def test_normalize_usage_neon_and_codex() -> None:
    neon = normalize_usage(
        "neon",
        {
            "input_tokens": 1000,
            "output_tokens": 200,
            "thinking_tokens": 50,
            "cache_read_tokens": 400,
            "total_tokens": 1250,
        },
    )
    assert neon == {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cached_tokens": 400,
        "reasoning_tokens": 50,
        "total_tokens": 1250,
    }

    codex = normalize_usage(
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


def test_usage_tracker_persists_and_aggregates(tmp_path: Path) -> None:
    tracker = UsageTracker(tmp_path, session_key="sess-1")
    tracker.record(
        job_id="neon-11111111",
        backend="neon",
        model="gemini-4-neon-high",
        mode="read_only",
        status="completed",
        duration_seconds=3.5,
        cwd=str(tmp_path),
        label="arch-review",
        usage={"input_tokens": 1000, "output_tokens": 100, "cached_tokens": 0, "reasoning_tokens": 20, "total_tokens": 1120},
    )
    tracker.record(
        job_id="codex-22222222",
        backend="codex",
        model="codex-default",
        mode="edit",
        status="completed",
        duration_seconds=8.2,
        cwd=str(tmp_path),
        label="tests",
        usage={"input_tokens": 3000, "output_tokens": 400, "cached_tokens": 1000, "reasoning_tokens": 50, "total_tokens": 3400},
    )

    compact = tracker.session_compact_summary()
    assert compact["neon"]["runs"] == 1
    assert compact["neon"]["total_tokens"] == 1120
    assert compact["codex"]["runs"] == 1
    assert compact["codex"]["total_tokens"] == 3400

    # New tracker instance simulates restarted MCP server reading persistent ledger.
    tracker2 = UsageTracker(tmp_path, session_key="sess-2")
    assert tracker2.summarize(period="session")["totals"]["runs"] == 0

    today = tracker2.summarize(period="today")
    assert today["totals"]["runs"] == 2
    assert today["totals"]["completed"] == 2
    assert today["totals"]["total_tokens"] == 4520
    assert set(today["by_backend"].keys()) == {"neon", "codex"}
    assert "neon:gemini-4-neon-high" in today["by_model"]
    assert "codex:codex-default" in today["by_model"]
    assert len(today["recent"]) == 2

    only_neon = tracker2.summarize(period="all", backend="neon")
    assert only_neon["totals"]["runs"] == 1
    assert only_neon["totals"]["total_tokens"] == 1120
