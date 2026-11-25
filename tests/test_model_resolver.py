"""Tests for dynamic, effort-preserving, self-healing model resolution."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent_bridge.model_resolver import (
    NeonModelCatalog,
    is_model_unavailable_error,
    is_newer_codex_model_in_same_family,
    parse_agy_model_id,
)

SAMPLE_CATALOG_V4 = [
    {"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)"},
    {"id": "gemini-4-neon-xhigh", "label": "Gemini 4 Neon (XHigh)"},
    {"id": "gemini-4-neon-medium", "label": "Gemini 4 Neon (Medium)"},
    {"id": "gemini-4-neon-low", "label": "Gemini 4 Neon (Low)"},
    {"id": "gemini-4-hydrogen-high", "label": "Gemini 4 Hydrogen (High)"},
    {"id": "gemini-4-hydrogen-xhigh", "label": "Gemini 4 Hydrogen (XHigh)"},
    {"id": "gemini-pro-orchestrator-neon", "label": "Orchestrator (Neon)"},
    {"id": "gemini-3.8-flash-high", "label": "Gemini 3.8 Flash (High)"},
    {"id": "gemini-3.8-flash-low", "label": "Gemini 3.8 Flash (Low)"},
]

SAMPLE_CATALOG_V5 = [
    *SAMPLE_CATALOG_V4,
    {"id": "gemini-4.5-neon-high", "label": "Gemini 4.5 Neon (High)"},
    {"id": "gemini-5-neon-high", "label": "Gemini 5 Neon (High)"},
    {"id": "gemini-5-neon-xhigh", "label": "Gemini 5 Neon (XHigh)"},
    {"id": "gemini-5-neon-medium", "label": "Gemini 5 Neon (Medium)"},
    {"id": "gemini-4.2-flash-high", "label": "Gemini 4.2 Flash (High)"},
]


def test_parse_agy_model_id() -> None:
    p = parse_agy_model_id("gemini-4-neon-xhigh")
    assert p.provider == "gemini"
    assert p.version == (4,)
    assert p.family == "neon"
    assert p.tier == "xhigh"
    assert not p.is_orchestrator

    p2 = parse_agy_model_id("gemini-3.8-flash-low")
    assert p2.version == (3, 8)
    assert p2.family == "flash"
    assert p2.tier == "low"

    p3 = parse_agy_model_id("gemini-pro-orchestrator-neon")
    assert p3.is_orchestrator


async def test_versionless_intent_picks_highest_version_with_matching_tier(tmp_path: Path) -> None:
    async def fetch() -> list[dict[str, str]]:
        return list(SAMPLE_CATALOG_V5)

    catalog = NeonModelCatalog(fetcher=fetch, cache_path=tmp_path / "models.json")

    res_default = await catalog.resolve(None, default_intent="neon-high")
    assert res_default.resolved == "gemini-5-neon-high"
    assert res_default.notice is None

    res_med = await catalog.resolve("neon-medium")
    assert res_med.resolved == "gemini-5-neon-medium"
    assert res_med.notice is None

    res_xhigh = await catalog.resolve("neon-xhigh")
    assert res_xhigh.resolved == "gemini-5-neon-xhigh"
    assert res_xhigh.notice is None

    res_flash = await catalog.resolve("flash")
    assert res_flash.resolved == "gemini-4.2-flash-high"
    assert res_flash.notice is None


async def test_preserves_requested_effort_tier_when_newer_version_lacks_tier(tmp_path: Path) -> None:
    mixed = [
        {"id": "gemini-4-neon-medium", "label": "Gemini 4 Neon (Medium)"},
        {"id": "gemini-5-neon-high", "label": "Gemini 5 Neon (High)"},
    ]

    async def fetch() -> list[dict[str, str]]:
        return list(mixed)

    catalog = NeonModelCatalog(fetcher=fetch, cache_path=tmp_path / "models.json")
    # Must preserve requested `medium` effort instead of silently upgrading to `high`!
    res = await catalog.resolve("neon-medium")
    assert res.resolved == "gemini-4-neon-medium"
    assert res.notice is None


async def test_discloses_tier_substitution_when_requested_tier_unavailable(tmp_path: Path) -> None:
    only_high = [
        {"id": "gemini-5-neon-high", "label": "Gemini 5 Neon (High)"},
        {"id": "gemini-5-neon-xhigh", "label": "Gemini 5 Neon (XHigh)"},
    ]

    async def fetch() -> list[dict[str, str]]:
        return list(only_high)

    catalog = NeonModelCatalog(fetcher=fetch, cache_path=tmp_path / "models.json")
    res = await catalog.resolve("neon-medium")
    assert res.resolved == "gemini-5-neon-high"
    assert res.notice is not None
    assert "medium" in res.notice.lower()
    assert "gemini-5-neon-high" in res.notice


async def test_retired_exact_model_auto_upgrades_seamlessly(tmp_path: Path) -> None:
    only_v5 = [
        {"id": "gemini-5-neon-high", "label": "Gemini 5 Neon (High)"},
        {"id": "gemini-5-neon-medium", "label": "Gemini 5 Neon (Medium)"},
        {"id": "gemini-5-neon-xhigh", "label": "Gemini 5 Neon (XHigh)"},
    ]

    async def fetch() -> list[dict[str, str]]:
        return list(only_v5)

    catalog = NeonModelCatalog(fetcher=fetch, cache_path=tmp_path / "models.json")
    res = await catalog.resolve("gemini-4-neon-high")
    assert res.resolved == "gemini-5-neon-high"
    assert res.notice is not None
    assert "gemini-5-neon-high" in res.notice


async def test_effort_override_on_neon(tmp_path: Path) -> None:
    async def fetch() -> list[dict[str, str]]:
        return list(SAMPLE_CATALOG_V5)

    catalog = NeonModelCatalog(fetcher=fetch, cache_path=tmp_path / "models.json")
    res = await catalog.resolve(None, default_intent="neon-high", effort_override="medium")
    assert res.resolved == "gemini-5-neon-medium"
    assert res.notice is None


async def test_stale_cache_status_and_refresh_on_new_exact_model(tmp_path: Path) -> None:
    cache_path = tmp_path / "models.json"
    cache_path.write_text(
        json.dumps({"fetched_at": time.time() - 60, "models": SAMPLE_CATALOG_V4}),
        encoding="utf-8",
    )
    calls = 0

    async def fetch() -> list[dict[str, str]]:
        nonlocal calls
        calls += 1
        return list(SAMPLE_CATALOG_V5)

    catalog = NeonModelCatalog(fetcher=fetch, cache_path=cache_path, ttl_seconds=900)
    assert catalog.status_info()["models_status"] == "cached"

    res = await catalog.resolve("gemini-5-neon-xhigh")
    assert calls == 1
    assert res.resolved == "gemini-5-neon-xhigh"
    assert res.notice is None
    assert catalog.status_info()["models_status"] == "live"


async def test_empty_catalog_falls_back_to_cli_default(tmp_path: Path) -> None:
    async def fetch() -> list[dict[str, str]]:
        raise RuntimeError("offline")

    catalog = NeonModelCatalog(fetcher=fetch, cache_path=tmp_path / "models.json")
    res = await catalog.resolve("neon-high")
    assert res.resolved is None
    assert res.notice is not None
    assert catalog.status_info()["models_status"] == "unavailable"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Error: unknown model 'gemini-3-neon-high'", True),
        ("model `gpt-4.9-old` not found", True),
        ("Unsupported model specified", True),
        ("API returned model_not_found", True),
        ("SyntaxError: unexpected token in foo.py", False),
        ("Permission denied reading file", False),
    ],
)
def test_is_model_unavailable_error(text: str, expected: bool) -> None:
    assert is_model_unavailable_error(text) is expected


async def test_agy_claude_and_gpt_pool_resolution_and_future_upgrades(tmp_path: Path) -> None:
    full_catalog = [
        *SAMPLE_CATALOG_V4,
        {"id": "claude-opus-5-5-low", "label": "Claude Opus 5.5 (Low)"},
        {"id": "claude-opus-5-5-medium", "label": "Claude Opus 5.5 (Medium)"},
        {"id": "claude-opus-5-5-high", "label": "Claude Opus 5.5 (High)"},
        {"id": "claude-sonnet-5-5-low", "label": "Claude Sonnet 5.5 (Low)"},
        {"id": "claude-sonnet-5-5-medium", "label": "Claude Sonnet 5.5 (Medium)"},
        {"id": "claude-sonnet-5-5-high", "label": "Claude Sonnet 5.5 (High)"},
        {"id": "gpt-oss-120b-medium", "label": "GPT-OSS 120B (Medium)"},
    ]

    async def fetch() -> list[dict[str, str]]:
        return list(full_catalog)

    catalog = NeonModelCatalog(fetcher=fetch, cache_path=tmp_path / "models_3p.json")

    assert (await catalog.resolve("claude")).resolved == "claude-sonnet-5-5-high"
    assert (await catalog.resolve("claude-sonnet-medium")).resolved == "claude-sonnet-5-5-medium"
    assert (await catalog.resolve("claude-opus")).resolved == "claude-opus-5-5-high"
    assert (await catalog.resolve("opus-low")).resolved == "claude-opus-5-5-low"
    assert (await catalog.resolve("agy-opus")).resolved == "claude-opus-5-5-high"
    assert (await catalog.resolve("gpt-oss")).resolved == "gpt-oss-120b-medium"
    assert (await catalog.resolve("agy-gpt")).resolved == "gpt-oss-120b-medium"

    pools = catalog.group_models_by_quota_pool()
    assert "gemini-4-neon-high" in pools["agy_gemini"]
    assert "claude-opus-5-5-high" in pools["agy_claude_gpt"]
    assert "claude-sonnet-5-5-high" in pools["agy_claude_gpt"]
    assert "gpt-oss-120b-medium" in pools["agy_claude_gpt"]
    assert "claude-opus-5-5-high" not in pools["agy_gemini"]

    # Future versions of Claude / GPT-OSS auto-upgrade seamlessly.
    upgraded = [
        *full_catalog,
        {"id": "claude-sonnet-6-high", "label": "Claude Sonnet 6 (High)"},
        {"id": "gpt-oss-240b-medium", "label": "GPT-OSS 240B (Medium)"},
    ]

    async def fetch_v2() -> list[dict[str, str]]:
        return list(upgraded)

    cat2 = NeonModelCatalog(fetcher=fetch_v2, cache_path=tmp_path / "models_3p_v2.json")
    assert (await cat2.resolve("claude-sonnet-high")).resolved == "claude-sonnet-6-high"
    assert (await cat2.resolve("gpt-oss")).resolved == "gpt-oss-240b-medium"


def test_codex_same_family_version_comparison() -> None:
    assert is_newer_codex_model_in_same_family("gpt-6.3-sol", "gpt-6.2-sol") is True
    assert is_newer_codex_model_in_same_family("gpt-7-sol", "gpt-6.2-sol") is True
    assert is_newer_codex_model_in_same_family("gpt-6.2-sol", "gpt-6.2-sol") is False
    assert is_newer_codex_model_in_same_family("gpt-6.1-sol", "gpt-6.2-sol") is False
    assert is_newer_codex_model_in_same_family("o4-mini", "gpt-6.2-sol") is False
