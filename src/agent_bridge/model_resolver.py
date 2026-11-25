"""Dynamic, self-healing model resolution for Neon (`agy`) and Codex (`codex`)."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

MODEL_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(unknown|invalid|unsupported|unavailable|deprecated|retired)\s+model\b", re.I),
    re.compile(r"\bmodel\b[^\n]{0,60}\b(not\s+found|does\s+not\s+exist|not\s+supported|unavailable|deprecated|invalid)\b", re.I),
    re.compile(r"\bno\s+such\s+model\b", re.I),
    re.compile(r"\bmodel_not_found\b", re.I),
)


def is_model_unavailable_error(text: str | None) -> bool:
    """Detect CLI error messages caused by an outdated or unavailable model ID."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in MODEL_ERROR_PATTERNS)


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
    """Parse an `agy` model ID like `gemini-4-neon-xhigh` or `gemini-3.8-flash-high`."""
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
            if not family_parts:
                version_parts.extend(int(piece) for piece in tok.split("."))
            else:
                # Handles IDs like `claude-opus-5-5-high` where version follows family name.
                version_parts.extend(int(piece) for piece in tok.split("."))
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


def _parse_intent(requested: str) -> tuple[str | None, str | None, bool]:
    """Extract `(family, tier, is_versioned_literal)` from a user/caller model spec."""
    raw = requested.strip().lower()
    if raw in DEFAULT_INTENT_TOKENS:
        return ("neon", "high", False)

    # Shorthand effort-only requests like "xhigh" -> neon + xhigh.
    if raw in TIER_ALIASES:
        return ("neon", TIER_ALIASES[raw], False)

    # Check if caller passed a versioned exact ID like `gemini-4-neon-high`.
    has_version = bool(re.search(r"(?:^|-)(\d+(?:\.\d+)?)(?:-|$)", raw))
    if has_version:
        parsed = parse_agy_model_id(raw)
        return (parsed.family, parsed.tier, True)

    tokens = [t for t in raw.split("-") if t]
    tier: str | None = None
    if tokens and tokens[-1] in TIER_ALIASES:
        tier = TIER_ALIASES[tokens.pop()]

    if tokens and tokens[0] == "gemini" and len(tokens) > 1:
        tokens = tokens[1:]

    family = "-".join(tokens) if tokens else "neon"
    if tier is None:
        tier = "high"
    return (family, tier, False)


def _select_from_catalog(
    requested: str,
    catalog: list[dict[str, str]],
) -> ResolvedModel | None:
    """Resolve `requested` against a non-empty `agy models` catalog."""
    if not catalog:
        return None

    req_clean = requested.strip()
    req_lower = req_clean.lower()

    # 1. Exact ID match (case-insensitive) always wins.
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

    family, desired_tier, is_versioned_literal = _parse_intent(req_clean)
    want_orchestrator = bool(family and "orchestrator" in family)

    pool = [
        m for m in parsed_catalog
        if want_orchestrator or not m.is_orchestrator
    ] or parsed_catalog

    # Prefer exact family match, then suffix/substring family match.
    family_matches = [m for m in pool if family and m.family == family]
    if not family_matches and family:
        family_matches = [
            m for m in pool
            if family in m.family or m.family in family
        ]

    # If the requested family disappeared entirely, fall back to the newest Gemini model.
    used_family_fallback = False
    candidates = family_matches
    if not candidates:
        used_family_fallback = True
        gemini_pool = [m for m in pool if m.provider == "gemini"]
        candidates = gemini_pool or pool

    tier_order = TIER_FALLBACK_ORDER.get(desired_tier or "high", ("high", "xhigh", "medium", "low"))

    def _sort_key(m: ParsedModel) -> tuple[int, tuple[int, ...], int]:
        # Always prefer newest version within the matching family, then closest desired tier.
        if m.tier in tier_order:
            tier_score = len(tier_order) - tier_order.index(m.tier)
        else:
            tier_score = 0
        prefer_gemini = 1 if m.provider == "gemini" else 0
        return (prefer_gemini, m.version, tier_score)

    best = max(candidates, key=_sort_key)

    notice: str | None = None
    if is_versioned_literal:
        notice = (
            f"Requested model {req_clean!r} is not in the live agy catalog; "
            f"automatically resolved to {best.model_id!r}."
        )
    elif used_family_fallback and req_lower not in DEFAULT_INTENT_TOKENS:
        notice = (
            f"No live agy model matched {req_clean!r}; "
            f"automatically fell back to {best.model_id!r}."
        )

    return ResolvedModel(requested=req_clean, resolved=best.model_id, notice=notice)


class NeonModelCatalog:
    """Caches `agy models` (memory + disk) and dynamically resolves versionless or outdated model IDs."""

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

    async def get_models(self, *, refresh: bool = False) -> list[dict[str, str]]:
        if not refresh and self.is_fresh():
            return list(self._models)
        try:
            live = await self._fetcher()
            if live:
                self._models = live
                self._fetched_at = time.time()
                self._save_disk_cache()
        except Exception as exc:
            logger.warning("Failed refreshing agy models catalog: %s", exc)
            if not self._models:
                return []
        return list(self._models)

    async def resolve(
        self,
        requested: str | None,
        *,
        default_intent: str = "neon-high",
        force_refresh: bool = False,
    ) -> ResolvedModel:
        req = (requested or "").strip()
        effective = req if req and req.lower() not in DEFAULT_INTENT_TOKENS else default_intent

        catalog = await self.get_models(refresh=force_refresh)
        if not catalog:
            # If agy models could not be fetched at all, omit --model unless caller gave an exact versioned ID.
            _, _, is_versioned = _parse_intent(effective)
            if is_versioned:
                return ResolvedModel(requested=requested or effective, resolved=effective)
            return ResolvedModel(
                requested=requested or effective,
                resolved=None,
                notice="Could not fetch live agy model list; letting agy choose its default model.",
            )

        selected = _select_from_catalog(effective, catalog)

        # If caller gave a specific model ID that wasn't in a cached list, refresh live once before falling back!
        if (
            selected is not None
            and selected.notice is not None
            and not force_refresh
            and (time.time() - self._fetched_at) > 5.0
        ):
            fresh_catalog = await self.get_models(refresh=True)
            if fresh_catalog:
                selected = _select_from_catalog(effective, fresh_catalog)

        if selected is None:
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
        """Compute live shortcut -> resolved model ID mapping for `list_backends`."""
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
            "carbon",
            "argon",
            "flash",
            "flash-low",
        )
        mapping: dict[str, str] = {}
        for alias in shortcuts:
            res = _select_from_catalog(alias, self._models)
            if res and res.resolved and res.notice is None:
                mapping[alias] = res.resolved
        return mapping
