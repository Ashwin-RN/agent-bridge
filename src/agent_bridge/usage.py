"""Usage normalization routed through the backend registry."""

from __future__ import annotations

from typing import Any

from agent_bridge.backends.agy import compute_agy_usage_delta, sum_agy_step_usages
from agent_bridge.backends.base import to_int
from agent_bridge.backends.registry import get_adapter_optional


def normalize_usage(
    backend: str,
    raw_usage: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Normalize a backend's token counters while preserving provider-specific metrics.

    Returns `(normalized_usage, provider_metrics)`.
    """
    if not raw_usage or not isinstance(raw_usage, dict):
        return None, None

    adapter = get_adapter_optional(backend)
    if adapter is not None:
        return adapter.normalize_usage(raw_usage)

    input_tokens = to_int(raw_usage, "input_tokens")
    output_tokens = to_int(raw_usage, "output_tokens")
    cached_tokens = to_int(raw_usage, "cached_tokens")
    cache_write = to_int(raw_usage, "cache_write_tokens")
    reasoning = to_int(raw_usage, "reasoning_tokens")
    total = to_int(raw_usage, "total_tokens") or (input_tokens + output_tokens)
    normalized: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
    }
    if cache_write:
        normalized["cache_write_tokens"] = cache_write
    provider_metrics: dict[str, Any] = {
        "provider": backend,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
    }
    return normalized, provider_metrics


__all__ = [
    "compute_agy_usage_delta",
    "normalize_usage",
    "sum_agy_step_usages",
]
