"""Usage normalization, per-turn step aggregation, and verified counter-delta calculation."""

from __future__ import annotations

from typing import Any


def _to_int(raw: dict[str, Any], key: str) -> int:
    val = raw.get(key)
    return int(val) if isinstance(val, (int, float)) else 0


def normalize_usage(
    backend: str,
    raw_usage: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Normalize `agy` and `codex` token counters while preserving provider-specific metrics.

    Returns `(normalized_usage, provider_metrics)`.
    """
    if not raw_usage or not isinstance(raw_usage, dict):
        return None, None

    input_tokens = _to_int(raw_usage, "input_tokens")
    output_tokens = _to_int(raw_usage, "output_tokens")

    if backend == "neon":
        cache_read = _to_int(raw_usage, "cache_read_tokens") or _to_int(raw_usage, "cached_tokens")
        thinking = _to_int(raw_usage, "thinking_tokens") or _to_int(raw_usage, "reasoning_tokens")
        total = _to_int(raw_usage, "total_tokens") or (input_tokens + output_tokens)
        normalized: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cache_read,
            "reasoning_tokens": thinking,
            "total_tokens": total,
        }
        provider_metrics: dict[str, Any] = {
            "provider": "antigravity",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "thinking_tokens": thinking,
            "total_tokens": total,
        }
        return normalized, provider_metrics

    cached_input = _to_int(raw_usage, "cached_input_tokens") or _to_int(raw_usage, "cached_tokens")
    cache_write = _to_int(raw_usage, "cache_write_input_tokens") or _to_int(raw_usage, "cache_write_tokens")
    reasoning_out = _to_int(raw_usage, "reasoning_output_tokens") or _to_int(raw_usage, "reasoning_tokens")
    # In OpenAI/Codex usage, cached_input_tokens is a subset of input_tokens and
    # reasoning_output_tokens is a subset of output_tokens.
    total = _to_int(raw_usage, "total_tokens") or (input_tokens + output_tokens)

    normalized = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_input,
        "reasoning_tokens": reasoning_out,
        "total_tokens": total,
    }
    if cache_write:
        normalized["cache_write_tokens"] = cache_write

    provider_metrics = {
        "provider": "codex",
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_out,
        "total_tokens": total,
    }
    return normalized, provider_metrics


def sum_agy_step_usages(step_usages: list[dict[str, Any]]) -> dict[str, int] | None:
    """Sum per-step `usage` objects from a single `agy --output-format stream-json` turn."""
    valid = [u for u in step_usages if isinstance(u, dict) and u]
    if not valid:
        return None
    keys = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
    totals = {k: sum(_to_int(u, k) for u in valid) for k in keys}
    if totals["total_tokens"] == 0 and (totals["input_tokens"] or totals["output_tokens"]):
        totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    return totals


def compute_agy_usage_delta(
    current_cumulative: dict[str, Any] | None,
    previous_cumulative: dict[str, Any] | None,
) -> dict[str, int] | None:
    """Compute a verified monotonic per-turn delta between cumulative `agy` envelopes."""
    if not isinstance(current_cumulative, dict) or not isinstance(previous_cumulative, dict):
        return None
    keys = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
    delta: dict[str, int] = {}
    for k in keys:
        cur = _to_int(current_cumulative, k)
        prev = _to_int(previous_cumulative, k)
        if cur < prev:
            return None
        delta[k] = cur - prev
    return delta
