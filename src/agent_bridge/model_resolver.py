"""Dynamic, effort-preserving, provider-aware, self-healing model resolution for `agy` and Codex."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_bridge.config import classify_quota_pool

logger = logging.getLogger("agent_bridge.model_resolver")

KNOWN_TIERS: tuple[str, ...] = ("xhigh", "max", "high", "medium", "low")
TIER_ALIASES: dict[str, str] = {
    "xh": "xhigh",
    "xhigh": "xhigh",
    "extra-high": "xhigh",
    "extrahigh": "xhigh",
    "max": "max",
    "hi": "high",
    "high": "high",
    "med": "medium",
    "medium": "medium",
    "lo": "low",
    "low": "low",
}
TIER_FALLBACK_ORDER: dict[str, tuple[str, ...]] = {
    "max": ("max", "xhigh", "high", "medium", "low"),
    "xhigh": ("xhigh", "max", "high", "medium", "low"),
    "high": ("high", "xhigh", "medium", "low"),
    "medium": ("medium", "high", "low", "xhigh"),
    "low": ("low", "medium", "high", "xhigh"),
}
DEFAULT_INTENT_TOKENS: frozenset[str] = frozenset({"", "default", "latest", "auto"})

_CLAUDE_FAMILIES: frozenset[str] = frozenset({"opus", "sonnet", "haiku"})
_GPT_FAMILIES: frozenset[str] = frozenset({"oss"})

MODEL_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(unknown|invalid|unsupported|unavailable|deprecated|retired)\s+model\b", re.I),
    re.compile(
        r"\bmodel\b[^\n]{0,60}\b(not\s+found|does\s+not\s+exist|not\s+supported|unavailable|deprecated|invalid)\b",
        re.I,
    ),
    re.compile(r"\bno\s+such\s+model\b", re.I),
    re.compile(r"\bmodel_not_found\b", re.I),
)


def is_model_unavailable_error(text: str | None) -> bool:
    """Detect CLI error messages caused by an outdated or unavailable model ID."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in MODEL_ERROR_PATTERNS)


def parse_codex_model_family_version(model_id: str | None) -> tuple[str, tuple[int, ...]] | None:
    """Extract `(family_key, version_tuple)` from Codex model IDs like `gpt-6.2-sol` -> `('gpt-sol', (6, 2))`."""
    if not model_id or not model_id.strip():
        return None
    tokens = [t for t in model_id.strip().lower().split("-") if t]
    ver: list[int] = []
    family_parts: list[str] = []
    for tok in tokens:
        if re.fullmatch(r"\d+(?:\.\d+)*", tok):
            ver.extend(int(piece) for piece in tok.split("."))
        else:
            family_parts.append(tok)
    if not ver or not family_parts:
        return None
    return "-".join(family_parts), tuple(ver)


def is_newer_codex_model_in_same_family(candidate: str | None, baseline: str | None) -> bool:
    """Return True if `candidate` is a strictly newer version of the same Codex model family as `baseline`."""
    p_cand = parse_codex_model_family_version(candidate)
    p_base = parse_codex_model_family_version(baseline)
    if p_cand is None or p_base is None:
        return False
    return p_cand[0] == p_base[0] and p_cand[1] > p_base[1]


class ModelPoolUnavailableError(ValueError):
    """Raised when an explicitly requested model's quota pool has no matching live model."""

    def __init__(
        self,
        *,
        requested: str,
        requested_pool: str,
        available_pools: list[str],
    ) -> None:
        self.requested = requested
        self.requested_pool = requested_pool
        self.available_pools = available_pools
        avail_text = ", ".join(available_pools) if available_pools else "none"
        super().__init__(
            f"No live agy model in quota pool {requested_pool!r} matched {requested!r} "
            f"(available pools: {avail_text}). Refusing cross-pool fallback to avoid spending "
            "a different quota pool; pass allow_cross_pool_fallback=True to opt in."
        )


@dataclass(frozen=True, slots=True)
class ParsedModel:
    model_id: str
    label: str
    provider: str
    version: tuple[int, ...]
    family: str
    tier: str | None
    is_orchestrator: bool


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    requested: str | None
    resolved: str | None
    notice: str | None = None


def parse_agy_model_id(model_id: str, label: str = "") -> ParsedModel:
    """Parse an `agy` model ID like `gemini-4-neon-xhigh`, `claude-opus-5-5-high`, or `gpt-oss-120b-medium`."""
    lower = model_id.strip().lower()
    tokens = [t for t in lower.split("-") if t]

    tier: str | None = None
    if tokens and tokens[-1] in KNOWN_TIERS:
        tier = tokens.pop()

    provider = tokens[0] if tokens else "unknown"
    rest = tokens[1:] if len(tokens) > 1 else []

    is_orchestrator = "orchestrator" in rest
    version_parts: list[int] = []
    family_parts: list[str] = []

    for tok in rest:
        if re.fullmatch(r"\d+(?:\.\d+)*", tok):
            version_parts.extend(int(piece) for piece in tok.split("."))
        elif re.fullmatch(r"\d+b", tok):
            # Parameter count token such as `120b` in `gpt-oss-120b-medium`.
            version_parts.append(int(tok[:-1]))
        else:
            family_parts.append(tok)

    family = "-".join(family_parts) if family_parts else provider
    return ParsedModel(
        model_id=model_id.strip(),
        label=label.strip() or model_id.strip(),
        provider=provider,
        version=tuple(version_parts),
        family=family,
        tier=tier,
        is_orchestrator=is_orchestrator,
    )


def _parse_intent(
    requested: str,
    *,
    effort_override: str | None = None,
) -> tuple[str | None, str | None, str | None, bool, bool]:
    """Return `(desired_provider, family, desired_tier, tier_explicit, is_versioned_literal)`."""
    raw = requested.strip().lower()
    norm_effort = TIER_ALIASES.get(effort_override.strip().lower()) if effort_override else None

    if raw in DEFAULT_INTENT_TOKENS:
        if norm_effort:
            return ("gemini", "neon", norm_effort, True, False)
        return ("gemini", "neon", "high", False, False)

    if raw in TIER_ALIASES:
        tier = norm_effort or TIER_ALIASES[raw]
        return ("gemini", "neon", tier, True, False)

    # Convenience pool/provider shortcuts like `agy-claude`, `agy-opus`, `agy-sonnet`, `agy-gpt`.
    if raw.startswith("agy-"):
        raw = raw[4:]
    elif raw.startswith("agy_"):
        raw = raw[4:]

    has_version = bool(re.search(r"(?:^|-)(\d+(?:\.\d+)?b?)(?:-|$)", raw))
    if has_version:
        parsed = parse_agy_model_id(raw)
        tier = norm_effort or parsed.tier
        prov = parsed.provider if parsed.provider in {"gemini", "claude", "gpt"} else None
        return (prov, parsed.family, tier, bool(tier), True)

    tokens = [t for t in raw.split("-") if t]
    tier: str | None = None
    tier_explicit = False
    if tokens and tokens[-1] in TIER_ALIASES:
        tier = TIER_ALIASES[tokens.pop()]
        tier_explicit = True

    desired_provider: str | None = None
    if tokens and tokens[0] in {"gemini", "claude", "gpt"}:
        desired_provider = tokens[0]
        if len(tokens) > 1:
            tokens = tokens[1:]
        else:
            # Bare provider shortcut: `claude` -> claude/sonnet, `gpt` -> gpt/oss, `gemini` -> gemini/neon
            if desired_provider == "claude":
                tokens = ["sonnet"]
            elif desired_provider == "gpt":
                tokens = ["oss"]
            else:
                tokens = ["neon"]

    family = "-".join(tokens) if tokens else "neon"
    if desired_provider is None:
        if family in _CLAUDE_FAMILIES or any(f in family.split("-") for f in _CLAUDE_FAMILIES):
            desired_provider = "claude"
        elif family in _GPT_FAMILIES or "oss" in family.split("-"):
            desired_provider = "gpt"
        else:
            desired_provider = "gemini"

    if norm_effort:
        tier = norm_effort
        tier_explicit = True
    elif tier is None:
        tier = "high" if desired_provider != "gpt" else "medium"
    return (desired_provider, family, tier, tier_explicit, False)


def _select_from_catalog(
    requested: str,
    catalog: list[dict[str, str]],
    *,
    effort_override: str | None = None,
    allow_cross_pool_fallback: bool = False,
) -> ResolvedModel | None:
    """Resolve `requested` against a non-empty `agy models` catalog.

    Preserves both the requested quota pool (`agy_gemini` vs `agy_claude_gpt`) and effort tier by
    default. Refuses silent cross-pool fallback unless `allow_cross_pool_fallback=True`, and always
    attaches a `notice` when substituting a tier, upgrading a retired versioned ID, or falling back
    to another family.
    """
    if not catalog:
        return None

    req_clean = requested.strip()
    req_lower = req_clean.lower()

    # Exact ID match wins unless caller explicitly asked for a different effort tier.
    if not effort_override:
        for entry in catalog:
            mid = entry.get("id", "").strip()
            if mid.lower() == req_lower:
                return ResolvedModel(requested=req_clean, resolved=mid)

    parsed_catalog = [
        parse_agy_model_id(entry["id"], entry.get("label", ""))
        for entry in catalog
        if entry.get("id")
    ]
    if not parsed_catalog:
        return None

    desired_provider, family, desired_tier, tier_explicit, is_versioned_literal = _parse_intent(
        req_clean,
        effort_override=effort_override,
    )
    want_orchestrator = bool(family and "orchestrator" in family)

    pool = [
        m for m in parsed_catalog
        if want_orchestrator or not m.is_orchestrator
    ] or parsed_catalog

    requested_quota_pool = (
        "agy_claude_gpt"
        if desired_provider in {"claude", "gpt"}
        else classify_quota_pool("neon", req_clean)
    )
    same_quota_pool = [
        m for m in pool if classify_quota_pool("neon", m.model_id) == requested_quota_pool
    ]

    if (
        not same_quota_pool
        and not allow_cross_pool_fallback
        and req_lower not in DEFAULT_INTENT_TOKENS
    ):
        available_pools = sorted({classify_quota_pool("neon", m.model_id) for m in pool})
        raise ModelPoolUnavailableError(
            requested=req_clean,
            requested_pool=requested_quota_pool,
            available_pools=available_pools,
        )

    scoped_pool = same_quota_pool or pool
    provider_pool = (
        [m for m in scoped_pool if m.provider == desired_provider]
        if desired_provider
        else scoped_pool
    )
    active_pool = provider_pool or scoped_pool

    family_matches = [m for m in active_pool if family and m.family == family]
    if not family_matches and family:
        family_matches = [
            m for m in active_pool
            if family in m.family or m.family in family
        ]

    used_family_fallback = False
    candidates = family_matches
    if not candidates:
        if provider_pool:
            # Stay inside requested provider (e.g., claude or gpt) before ever falling back across families.
            candidates = provider_pool
            used_family_fallback = req_lower not in {"claude", "gpt", "agy-claude", "agy-gpt"}
        elif same_quota_pool:
            candidates = same_quota_pool
            used_family_fallback = True
        else:
            used_family_fallback = True
            gemini_pool = [m for m in pool if m.provider == "gemini"]
            candidates = gemini_pool or pool

    tier_order = TIER_FALLBACK_ORDER.get(desired_tier or "high", ("high", "xhigh", "medium", "low"))

    def _sort_key(m: ParsedModel) -> tuple[int, tuple[int, ...], int]:
        tier_score = len(tier_order) - tier_order.index(m.tier) if m.tier in tier_order else 0
        prefer_provider = 1 if (desired_provider and m.provider == desired_provider) else (
            1 if (desired_provider is None and m.provider == "gemini") else 0
        )
        return (prefer_provider, m.version, tier_score)

    tier_substituted = False
    if desired_tier:
        exact_tier = [m for m in candidates if m.tier == desired_tier]
        if exact_tier:
            best = max(exact_tier, key=_sort_key)
        else:
            best = max(candidates, key=_sort_key)
            tier_substituted = best.tier != desired_tier
    else:
        best = max(candidates, key=_sort_key)

    notices: list[str] = []
    if used_family_fallback and req_lower not in DEFAULT_INTENT_TOKENS:
        notices.append(
            f"No live agy model matched {req_clean!r}; fell back to {best.model_id!r}."
        )
    elif is_versioned_literal and best.model_id.lower() != req_lower:
        notices.append(
            f"Requested model {req_clean!r} was resolved to {best.model_id!r}."
        )
    if tier_substituted and (tier_explicit or req_lower not in DEFAULT_INTENT_TOKENS):
        notices.append(
            f"Requested effort tier {desired_tier!r} is unavailable in this family; "
            f"substituted {best.model_id!r} (tier={best.tier or 'unspecified'})."
        )

    return ResolvedModel(
        requested=req_clean,
        resolved=best.model_id,
        notice=" ".join(notices) if notices else None,
    )


class NeonModelCatalog:
    """Caches `agy models` (memory + disk) and reports fresh/cached/stale status."""

    def __init__(
        self,
        *,
        fetcher: Callable[[], Awaitable[list[dict[str, str]]]],
        cache_path: Path,
        ttl_seconds: float = 900.0,
    ) -> None:
        self._fetcher = fetcher
        self._cache_path = cache_path
        self._ttl_seconds = max(30.0, ttl_seconds)
        self._models: list[dict[str, str]] = []
        self._fetched_at: float = 0.0
        self._last_status: str = "unavailable"
        self._load_disk_cache()

    def _load_disk_cache(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("models"), list):
                self._models = [
                    {"id": str(m["id"]), "label": str(m.get("label", m["id"]))}
                    for m in payload["models"]
                    if isinstance(m, dict) and m.get("id")
                ]
                self._fetched_at = float(payload.get("fetched_at", 0.0))
                if self._models:
                    self._last_status = "cached" if self.is_fresh() else "stale_cache"
        except Exception as exc:
            logger.debug("Ignoring unreadable model cache %s: %s", self._cache_path, exc)

    def _save_disk_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(
                    {"fetched_at": self._fetched_at, "models": self._models},
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._cache_path)
        except Exception as exc:
            logger.debug("Could not write model cache %s: %s", self._cache_path, exc)

    def is_fresh(self) -> bool:
        return bool(self._models) and (time.time() - self._fetched_at) < self._ttl_seconds

    def status_info(self) -> dict[str, Any]:
        if not self._models:
            return {
                "models_status": "unavailable",
                "models_fetched_at": None,
                "models_age_seconds": None,
            }
        age = max(0.0, time.time() - self._fetched_at)
        status = self._last_status
        if status in {"live", "cached"} and age >= self._ttl_seconds:
            status = "stale_cache"
        return {
            "models_status": status,
            "models_fetched_at": datetime.fromtimestamp(
                self._fetched_at, tz=timezone.utc
            ).isoformat(timespec="seconds"),
            "models_age_seconds": round(age, 1),
        }

    async def get_models(
        self,
        *,
        refresh: bool = False,
        timeout_seconds: float | None = None,
    ) -> list[dict[str, str]]:
        if not refresh and self.is_fresh():
            if self._last_status != "live":
                self._last_status = "cached"
            return list(self._models)
        try:
            coro = self._fetcher()
            live = await (
                asyncio.wait_for(coro, timeout=timeout_seconds)
                if timeout_seconds is not None
                else coro
            )
            if live:
                self._models = live
                self._fetched_at = time.time()
                self._last_status = "live"
                self._save_disk_cache()
                return list(self._models)
        except Exception as exc:
            logger.warning("Failed refreshing agy models catalog: %s", exc)
            if self._models:
                self._last_status = "stale_cache"
                return list(self._models)
            self._last_status = "unavailable"
            return []
        return list(self._models)

    async def resolve(
        self,
        requested: str | None,
        *,
        default_intent: str = "neon-high",
        effort_override: str | None = None,
        force_refresh: bool = False,
        timeout_seconds: float | None = None,
        allow_cross_pool_fallback: bool = False,
    ) -> ResolvedModel:
        req = (requested or "").strip()
        is_explicit_request = bool(req and req.lower() not in DEFAULT_INTENT_TOKENS)
        effective = req if is_explicit_request else default_intent

        catalog = await self.get_models(refresh=force_refresh, timeout_seconds=timeout_seconds)
        if not catalog:
            desired_prov, _, _, _, is_versioned = _parse_intent(effective, effort_override=effort_override)
            if is_versioned and not effort_override:
                return ResolvedModel(requested=requested or effective, resolved=effective)
            requested_pool = (
                "agy_claude_gpt"
                if desired_prov in {"claude", "gpt"}
                else classify_quota_pool("neon", effective)
            )
            if not allow_cross_pool_fallback and req.lower() not in {"default", "latest", "auto"}:
                raise ModelPoolUnavailableError(
                    requested=effective,
                    requested_pool=requested_pool,
                    available_pools=[],
                )
            return ResolvedModel(
                requested=requested or effective,
                resolved=None,
                notice="Could not fetch live agy model list; letting agy choose its default model.",
            )

        pool_err: ModelPoolUnavailableError | None = None
        selected: ResolvedModel | None = None
        try:
            selected = _select_from_catalog(
                effective,
                catalog,
                effort_override=effort_override,
                allow_cross_pool_fallback=allow_cross_pool_fallback,
            )
        except ModelPoolUnavailableError as exc:
            pool_err = exc

        if (
            (pool_err is not None or (selected is not None and selected.notice is not None))
            and not force_refresh
            and (time.time() - self._fetched_at) > 5.0
        ):
            fresh_catalog = await self.get_models(
                refresh=True,
                timeout_seconds=timeout_seconds,
            )
            if fresh_catalog:
                pool_err = None
                selected = _select_from_catalog(
                    effective,
                    fresh_catalog,
                    effort_override=effort_override,
                    allow_cross_pool_fallback=allow_cross_pool_fallback,
                )

        if pool_err is not None:
            raise pool_err

        if selected is None:
            if not allow_cross_pool_fallback and req.lower() not in {"default", "latest", "auto"}:
                desired_prov, _, _, _, _ = _parse_intent(effective, effort_override=effort_override)
                requested_pool = (
                    "agy_claude_gpt"
                    if desired_prov in {"claude", "gpt"}
                    else classify_quota_pool("neon", effective)
                )
                raise ModelPoolUnavailableError(
                    requested=effective,
                    requested_pool=requested_pool,
                    available_pools=[],
                )
            return ResolvedModel(
                requested=requested or effective,
                resolved=None,
                notice="No matching model found in agy catalog; letting agy choose its default model.",
            )
        return ResolvedModel(
            requested=requested or effective,
            resolved=selected.resolved,
            notice=selected.notice,
        )

    def summarize_latest_aliases(self) -> dict[str, str]:
        if not self._models:
            return {}
        shortcuts = (
            "neon",
            "neon-xhigh",
            "neon-high",
            "neon-medium",
            "neon-low",
            "hydrogen",
            "hydrogen-xhigh",
            "hydrogen-high",
            "hydrogen-medium",
            "carbon",
            "argon",
            "flash",
            "flash-low",
            "claude",
            "claude-sonnet",
            "claude-sonnet-high",
            "claude-sonnet-medium",
            "claude-opus",
            "claude-opus-high",
            "claude-opus-medium",
            "gpt-oss",
        )
        mapping: dict[str, str] = {}
        for alias in shortcuts:
            try:
                res = _select_from_catalog(alias, self._models)
            except ModelPoolUnavailableError:
                continue
            if res and res.resolved and res.notice is None:
                mapping[alias] = res.resolved
        return mapping

    def group_models_by_quota_pool(self) -> dict[str, list[str]]:
        """Group cached `agy` model IDs by their independent Antigravity quota pool."""
        grouped: dict[str, list[str]] = {"agy_gemini": [], "agy_claude_gpt": []}
        for entry in self._models:
            mid = entry.get("id")
            if not mid:
                continue
            pool = classify_quota_pool("neon", mid)
            grouped.setdefault(pool, []).append(mid)
        return grouped
