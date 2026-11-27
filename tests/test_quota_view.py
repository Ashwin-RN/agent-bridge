"""Pure tests for `build_quota_view` and quota classification / timeout fixes."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from agent_bridge import process_control as process_control_module
from agent_bridge import quota as quota_module
from agent_bridge.quota import (
    CLAUDE_QUOTA_NOTE,
    build_quota_advisory,
    build_quota_view,
    claude_quota_from_rate_limit,
    fetch_codex_quota,
    parse_codex_rate_limits,
    parse_neon_usage_envelope,
)

NOW_DT = datetime(2026, 11, 28, 10, 0, 0, tzinfo=timezone.utc)
NOW = NOW_DT.timestamp()


def _iso_offset(**kwargs: float) -> str:
    return (NOW_DT + timedelta(**kwargs)).isoformat(timespec="seconds")


def _ts_offset(**kwargs: float) -> int:
    return int((NOW_DT + timedelta(**kwargs)).timestamp())


def _full_readings(*, include_claude: bool = True) -> dict[str, Any]:
    codex = parse_codex_rate_limits(
        {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "planType": "plus",
                "primary": {"usedPercent": 29, "windowDurationMins": 300, "resetsAt": _ts_offset(hours=2, minutes=16)},
                "secondary": {"usedPercent": 70, "windowDurationMins": 10080, "resetsAt": _ts_offset(days=1, hours=3)},
            },
        },
        fetched_at=_iso_offset(minutes=-4),
        now_utc=NOW_DT,
    )
    neon = parse_neon_usage_envelope(
        {
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "description": "Models within this group: Gemini 4 Neon, Gemini 3.8 Flash",
                            "buckets": [
                                {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.88, "reset_time": _iso_offset(hours=3, minutes=40)},
                                {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.82, "reset_time": _iso_offset(days=5, hours=6)},
                            ],
                        },
                        {
                            "name": "Claude and GPT models",
                            "description": "Models within this group: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B",
                            "buckets": [
                                {"id": "3p-5h", "window": "5h", "remaining_fraction": 1.0, "reset_time": "1970-01-01T00:00:00Z"},
                                {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1.0, "reset_time": "1970-01-01T00:00:00Z"},
                            ],
                        },
                    ]
                },
            }
        },
        fetched_at=_iso_offset(minutes=-1),
        now_utc=NOW_DT,
    )
    if include_claude:
        claude: dict[str, Any] = claude_quota_from_rate_limit(
            {
                "status": "allowed",
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.15, "resetsAt": _ts_offset(hours=4)},
                    "seven_day": {"utilization": 0.40, "resetsAt": _ts_offset(days=4, hours=2)},
                },
            },
            fetched_at=_iso_offset(minutes=-12),
        ) or {}
    else:
        claude = {
            "backend": "claude",
            "pool": "claude",
            "status": "unknown",
            "fetched_at": _iso_offset(minutes=-2),
            "note": CLAUDE_QUOTA_NOTE,
        }
    return {"codex": codex, "neon": neon, "claude": claude}


def test_full_reading_shape_order_read_at_and_compact_size() -> None:
    view = build_quota_view(_full_readings(include_claude=True), now=NOW)

    assert set(view.keys()) == {"read_at", "pools", "order", "unread", "summary"}
    assert [p["pool"] for p in view["pools"]] == ["codex", "agy_gemini", "agy_claude_gpt", "claude"]
    # Oldest reading used is Claude's (-12m).
    assert view["read_at"] == _iso_offset(minutes=-12)
    assert view["unread"] == []
    assert view["order"] == ["codex", "agy_claude_gpt", "agy_gemini"]
    assert "claude" not in view["order"]

    by_pool = {p["pool"]: p for p in view["pools"]}
    assert by_pool["codex"]["five_hour"] == {
        "left_percent": 71,
        "resets_in": "2h 16m",
        "reset_time": _iso_offset(hours=2, minutes=16),
        "state": "ok",
    }
    assert by_pool["codex"]["weekly"] == {
        "left_percent": 30,
        "resets_in": "1d 3h",
        "reset_time": _iso_offset(days=1, hours=3),
        "state": "expiring",
    }
    assert by_pool["codex"]["priority"] == "use_before_reset"
    assert by_pool["agy_claude_gpt"]["five_hour"]["state"] == "not_started"
    assert by_pool["agy_claude_gpt"]["five_hour"]["reset_time"] is None
    assert by_pool["agy_claude_gpt"]["five_hour"]["resets_in"] is None

    compact = json.dumps(view, separators=(",", ":"))
    assert len(compact.encode("utf-8")) <= 2500
    assert '"models"' not in compact


def test_unread_claude_not_flagged_as_failure_while_failed_pools_named_in_summary() -> None:
    readings = _full_readings(include_claude=False)
    view_ok = build_quota_view(readings, now=NOW)
    by_pool = {p["pool"]: p for p in view_ok["pools"]}
    assert by_pool["claude"]["five_hour"] is None
    assert by_pool["claude"]["weekly"] is None
    assert by_pool["claude"]["priority"] == "unknown"
    assert view_ok["unread"] == [{"pool": "claude", "reason": "no reading: no Claude job has reported one"}]
    assert "could not read" not in view_ok["summary"].lower()
    assert "claude" not in view_ok["summary"].lower()

    # Now fail codex and neon: every failed pool must appear in unread AND in summary.
    readings_failed = {
        "codex": {"backend": "codex", "status": "unknown", "error": "Timed out reading Codex rate limits after 12s."},
        "neon": {"backend": "neon", "status": "unknown", "error": "agy /usage exited 1"},
        "claude": readings["claude"],
    }
    view_failed = build_quota_view(readings_failed, now=NOW)
    assert view_failed["order"] == []
    assert view_failed["read_at"] is None
    unread_by_pool = {u["pool"]: u["reason"] for u in view_failed["unread"]}
    assert set(unread_by_pool.keys()) == {"codex", "agy_gemini", "agy_claude_gpt", "claude"}
    assert "12s" in unread_by_pool["codex"]
    assert "agy /usage exited 1" in unread_by_pool["agy_gemini"]
    for failed_pool in ("codex", "agy_gemini", "agy_claude_gpt"):
        assert failed_pool in view_failed["summary"]
    assert "could not read codex, agy_gemini, agy_claude_gpt." in view_failed["summary"]


def test_window_states_priorities_and_order_exclusions() -> None:
    readings = {
        "codex": {
            "backend": "codex",
            "status": "ok",
            "fetched_at": _iso_offset(minutes=-1),
            "windows": [
                {"window": "5h", "remaining_percent": 0.0, "reset_time": _iso_offset(hours=1)},
                {"window": "weekly", "remaining_percent": 50.0, "reset_time": _iso_offset(days=3)},
            ],
        },
        "neon": {
            "backend": "neon",
            "status": "ok",
            "fetched_at": _iso_offset(minutes=-1),
            "groups": [
                {
                    "name": "Gemini Models",
                    "windows": [
                        {"id": "gemini-5h", "window": "5h", "remaining_percent": 12.0, "reset_time": _iso_offset(hours=2)},
                        {"id": "gemini-weekly", "window": "weekly", "remaining_percent": 50.0, "reset_time": _iso_offset(days=4)},
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "windows": [
                        {"id": "3p-weekly", "window": "weekly", "remaining_percent": 60.0, "reset_time": _iso_offset(minutes=-5)},
                    ],
                },
            ],
        },
        "claude": {
            "backend": "claude",
            "status": "ok",
            "fetched_at": _iso_offset(minutes=-1),
            "windows": [
                {"id": "seven_day", "window": "weekly", "remaining_percent": 40.0, "reset_time": _iso_offset(hours=10)},
            ],
        },
    }
    view = build_quota_view(readings, now=NOW)
    by_pool = {p["pool"]: p for p in view["pools"]}

    assert by_pool["codex"]["five_hour"]["state"] == "exhausted"
    assert by_pool["codex"]["priority"] == "avoid"

    assert by_pool["agy_gemini"]["five_hour"]["state"] == "low"
    assert by_pool["agy_gemini"]["priority"] == "conserve"

    assert by_pool["agy_claude_gpt"]["five_hour"] is None
    assert by_pool["agy_claude_gpt"]["weekly"]["state"] == "stale"
    assert by_pool["agy_claude_gpt"]["priority"] == "stale"

    assert by_pool["claude"]["five_hour"] is None
    assert by_pool["claude"]["weekly"]["state"] == "expiring"
    assert by_pool["claude"]["priority"] == "normal"

    # Only agy_gemini is routable: codex is avoid, agy_claude_gpt is stale, claude is never ordered.
    assert view["order"] == ["agy_gemini"]


def test_unrecognised_agy_group_not_counted_as_gemini_and_no_duplicate_order() -> None:
    neon_unknown = parse_neon_usage_envelope(
        {
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Experimental Video Models",
                            "buckets": [
                                {"id": "video-5h", "window": "5h", "remaining_fraction": 0.95, "reset_time": _iso_offset(hours=4)},
                                {"id": "video-weekly", "window": "weekly", "remaining_fraction": 0.95, "reset_time": _iso_offset(days=6)},
                            ],
                        },
                        {
                            "name": "Claude and GPT models",
                            "buckets": [
                                {"id": "3p-5h", "window": "5h", "remaining_fraction": 0.80, "reset_time": _iso_offset(hours=4)},
                                {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.80, "reset_time": _iso_offset(days=5)},
                            ],
                        },
                    ]
                },
            }
        },
        fetched_at=_iso_offset(minutes=-1),
        now_utc=NOW_DT,
    )
    view = build_quota_view({"neon": neon_unknown}, now=NOW)
    by_pool = {p["pool"]: p for p in view["pools"]}

    assert by_pool["agy_gemini"]["priority"] == "unknown"
    assert by_pool["agy_gemini"]["five_hour"] is None
    assert by_pool["agy_gemini"]["weekly"] is None
    assert "agy_gemini" not in view["order"]
    assert view["order"] == ["agy_claude_gpt"]
    assert any("Experimental Video Models" in u["reason"] for u in view["unread"])
    assert "Experimental Video Models" in view["summary"]

    # Two groups for the same pool must never produce a duplicate entry in order or advisory recommended_order.
    neon_dup = {
        "backend": "neon",
        "status": "ok",
        "fetched_at": _iso_offset(minutes=-1),
        "groups": [
            {"name": "Gemini 5h", "windows": [{"id": "gemini-5h", "window": "5h", "remaining_percent": 80, "reset_time": _iso_offset(hours=3)}]},
            {"name": "Gemini weekly", "windows": [{"id": "gemini-weekly", "window": "weekly", "remaining_percent": 75, "reset_time": _iso_offset(days=5)}]},
        ],
    }
    view_dup = build_quota_view({"neon": neon_dup}, now=NOW)
    assert view_dup["order"] == ["agy_gemini"]
    adv_dup = build_quota_advisory({"neon": neon_dup}, now_utc=NOW_DT)
    assert adv_dup["recommended_order"] == ["agy_gemini"]


def test_use_before_reset_requires_at_least_20_percent_weekly_left() -> None:
    def _codex_at(weekly_left: float) -> dict[str, Any]:
        return parse_codex_rate_limits(
            {
                "ordinaryUsageAllowed": True,
                "rateLimits": {
                    "planType": "plus",
                    "primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": _ts_offset(hours=3)},
                    "secondary": {"usedPercent": 100 - weekly_left, "windowDurationMins": 10080, "resetsAt": _ts_offset(hours=12)},
                },
            },
            fetched_at=_iso_offset(minutes=-1),
            now_utc=NOW_DT,
        )

    neon_full = parse_neon_usage_envelope(
        {
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.90, "reset_time": _iso_offset(hours=4)},
                                {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.90, "reset_time": _iso_offset(days=6)},
                            ],
                        }
                    ]
                },
            }
        },
        fetched_at=_iso_offset(minutes=-1),
        now_utc=NOW_DT,
    )

    # 10% left with 12h to go: must NOT trigger use_before_reset, and must rank behind 90%-left Gemini.
    low_view = build_quota_view({"codex": _codex_at(10.0), "neon": neon_full}, now=NOW)
    low_codex = next(p for p in low_view["pools"] if p["pool"] == "codex")
    assert low_codex["priority"] != "use_before_reset"
    assert low_codex["weekly"]["state"] != "expiring"
    assert low_view["order"][0] == "agy_gemini"

    # 19% left with 12h to go: still below 20%, must NOT trigger use_before_reset.
    below_view = build_quota_view({"codex": _codex_at(19.0), "neon": neon_full}, now=NOW)
    below_codex = next(p for p in below_view["pools"] if p["pool"] == "codex")
    assert below_codex["priority"] != "use_before_reset"
    assert below_codex["weekly"]["state"] != "expiring"
    assert below_view["order"][0] == "agy_gemini"

    # 20% left with 12h to go: meets threshold, triggers use_before_reset ahead of Gemini.
    at_view = build_quota_view({"codex": _codex_at(20.0), "neon": neon_full}, now=NOW)
    at_codex = next(p for p in at_view["pools"] if p["pool"] == "codex")
    assert at_codex["priority"] == "use_before_reset"
    assert at_codex["weekly"]["state"] == "expiring"
    assert at_view["order"][0] == "codex"


def test_codex_window_kinds_classified_by_duration_range() -> None:
    parsed_near = parse_codex_rate_limits(
        {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "planType": "plus",
                "primary": {"usedPercent": 25, "windowDurationMins": 299, "resetsAt": _ts_offset(hours=3)},
                "secondary": {"usedPercent": 40, "windowDurationMins": 10079, "resetsAt": _ts_offset(days=4)},
            },
        },
        fetched_at=_iso_offset(minutes=-1),
        now_utc=NOW_DT,
    )
    assert [w["window"] for w in parsed_near["windows"]] == ["5h", "weekly"]
    view_near = build_quota_view({"codex": parsed_near}, now=NOW)
    codex_near = next(p for p in view_near["pools"] if p["pool"] == "codex")
    assert codex_near["five_hour"]["left_percent"] == 75
    assert codex_near["weekly"]["left_percent"] == 60

    # Range boundaries: 360m (6h) -> 5h, 8640m (6d) -> weekly, 1440m (1d) -> neither.
    parsed_bounds = parse_codex_rate_limits(
        {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "planType": "plus",
                "primary": {"usedPercent": 10, "windowDurationMins": 360, "resetsAt": _ts_offset(hours=5)},
                "secondary": {"usedPercent": 20, "windowDurationMins": 8640, "resetsAt": _ts_offset(days=5)},
            },
        },
        fetched_at=_iso_offset(minutes=-1),
        now_utc=NOW_DT,
    )
    assert [w["window"] for w in parsed_bounds["windows"]] == ["5h", "weekly"]

    parsed_mid = parse_codex_rate_limits(
        {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "planType": "plus",
                "primary": {"usedPercent": 10, "windowDurationMins": 1440, "resetsAt": _ts_offset(hours=12)},
            },
        },
        fetched_at=_iso_offset(minutes=-1),
        now_utc=NOW_DT,
    )
    assert parsed_mid["windows"][0]["window"] == "1d"


async def test_fetch_codex_quota_timeout_reports_wait_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    terminated = False

    class _FakeStdin:
        def write(self, _data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

    class _FakeStdout:
        async def readline(self) -> bytes:
            await asyncio.sleep(5)
            return b""

    class _FakeProc:
        stdin = _FakeStdin()
        stdout = _FakeStdout()

    class _FakeManaged:
        async def terminate(self) -> None:
            nonlocal terminated
            terminated = True

    async def _fake_spawn(*_args: Any, **_kwargs: Any) -> tuple[_FakeProc, _FakeManaged]:
        return _FakeProc(), _FakeManaged()

    monkeypatch.setattr(process_control_module, "spawn_managed_process", _fake_spawn)

    res = await fetch_codex_quota(["nonexistent-codex"], timeout_seconds=0.1)
    assert terminated is True
    assert res["status"] == "unknown"
    assert res["error"] != "TimeoutError: "
    assert "0.1s" in res["error"]
    assert "timed out" in res["error"].lower()
