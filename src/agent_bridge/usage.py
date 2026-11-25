"""Normalized usage tracking and persistent JSONL ledger visible to the parent AI."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger("agent_bridge.usage")

UsagePeriod = Literal["session", "today", "7d", "30d", "all"]


def normalize_usage(backend: str, raw_usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize `agy` and `codex` token counters into one consistent schema."""
    if not raw_usage or not isinstance(raw_usage, dict):
        return None

    def _int(key: str) -> int:
        val = raw_usage.get(key)
        return int(val) if isinstance(val, (int, float)) else 0

    input_tokens = _int("input_tokens")
    output_tokens = _int("output_tokens")

    if backend == "neon":
        cached_tokens = _int("cache_read_tokens") or _int("cached_tokens")
        reasoning_tokens = _int("thinking_tokens") or _int("reasoning_tokens")
        total_tokens = _int("total_tokens") or (input_tokens + output_tokens + reasoning_tokens)
    else:
        # In OpenAI/Codex usage, cached_input_tokens is a subset of input_tokens and
        # reasoning_output_tokens is a subset of output_tokens.
        cached_tokens = _int("cached_input_tokens") or _int("cached_tokens")
        reasoning_tokens = _int("reasoning_output_tokens") or _int("reasoning_tokens")
        total_tokens = _int("total_tokens") or (input_tokens + output_tokens)

    normalized: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }
    cache_write = _int("cache_write_input_tokens")
    if cache_write:
        normalized["cache_write_tokens"] = cache_write
    return normalized


def _empty_bucket() -> dict[str, Any]:
    return {
        "runs": 0,
        "completed": 0,
        "failed": 0,
        "timed_out": 0,
        "cancelled": 0,
        "duration_seconds": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }


def _accumulate(bucket: dict[str, Any], entry: dict[str, Any]) -> None:
    bucket["runs"] += 1
    status = str(entry.get("status", ""))
    if status in {"completed", "failed", "timed_out", "cancelled"}:
        bucket[status] += 1
    dur = entry.get("duration_seconds")
    if isinstance(dur, (int, float)):
        bucket["duration_seconds"] = round(bucket["duration_seconds"] + float(dur), 2)

    usage = entry.get("usage")
    if isinstance(usage, dict):
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            val = usage.get(key)
            if isinstance(val, (int, float)):
                bucket[key] += int(val)


def _period_cutoff_ts(period: UsagePeriod) -> float | None:
    now = time.time()
    if period == "all" or period == "session":
        return None
    if period == "today":
        local_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return local_midnight.timestamp()
    if period == "7d":
        return now - (7 * 86_400)
    if period == "30d":
        return now - (30 * 86_400)
    raise ValueError(f"Unknown period {period!r}. Expected: session, today, 7d, 30d, all.")


class UsageTracker:
    """Appends per-run usage to `usage.jsonl` and aggregates stats for the parent AI."""

    def __init__(self, state_dir: Path, *, session_key: str | None = None) -> None:
        self._state_dir = state_dir
        self._ledger_path = state_dir / "usage.jsonl"
        self.session_key = session_key or uuid.uuid4().hex[:12]
        self._session_entries: list[dict[str, Any]] = []

    def record(
        self,
        *,
        job_id: str,
        backend: str,
        model: str | None,
        mode: str,
        status: str,
        duration_seconds: float | None,
        cwd: str,
        label: str | None,
        usage: dict[str, Any] | None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "iso_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "session_key": self.session_key,
            "job_id": job_id,
            "backend": backend,
            "model": model or f"{backend}-default",
            "mode": mode,
            "status": status,
            "duration_seconds": round(float(duration_seconds or 0.0), 2),
            "cwd": cwd,
            "label": label,
            "usage": usage,
        }
        self._session_entries.append(entry)
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            with self._ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except Exception as exc:
            logger.warning("Could not append usage record to %s: %s", self._ledger_path, exc)

    def _load_entries(self, period: UsagePeriod) -> list[dict[str, Any]]:
        if period == "session":
            return list(self._session_entries)
        if not self._ledger_path.exists():
            return list(self._session_entries)

        cutoff = _period_cutoff_ts(period)
        entries: list[dict[str, Any]] = []
        try:
            with self._ledger_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    ts = obj.get("ts")
                    if cutoff is not None and (not isinstance(ts, (int, float)) or ts < cutoff):
                        continue
                    entries.append(obj)
        except Exception as exc:
            logger.warning("Could not read usage ledger %s: %s", self._ledger_path, exc)
            return list(self._session_entries)
        return entries

    def session_compact_summary(self) -> dict[str, Any]:
        """Lightweight inline summary of current MCP session usage by backend."""
        by_backend: dict[str, dict[str, Any]] = {}
        for entry in self._session_entries:
            backend = str(entry.get("backend", "unknown"))
            bucket = by_backend.setdefault(
                backend,
                {"runs": 0, "total_tokens": 0, "duration_seconds": 0.0},
            )
            bucket["runs"] += 1
            dur = entry.get("duration_seconds")
            if isinstance(dur, (int, float)):
                bucket["duration_seconds"] = round(bucket["duration_seconds"] + float(dur), 2)
            usage = entry.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), (int, float)):
                bucket["total_tokens"] += int(usage["total_tokens"])
        return by_backend

    def summarize(
        self,
        *,
        period: UsagePeriod = "today",
        backend: str | None = None,
        recent_limit: int = 10,
    ) -> dict[str, Any]:
        entries = self._load_entries(period)
        if backend:
            entries = [e for e in entries if e.get("backend") == backend]

        totals = _empty_bucket()
        by_backend: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}

        for entry in entries:
            _accumulate(totals, entry)
            b = str(entry.get("backend", "unknown"))
            _accumulate(by_backend.setdefault(b, _empty_bucket()), entry)
            m_key = f"{b}:{entry.get('model') or f'{b}-default'}"
            _accumulate(by_model.setdefault(m_key, _empty_bucket()), entry)

        recent_n = max(0, min(int(recent_limit), 50))
        recent: list[dict[str, Any]] = []
        if recent_n > 0:
            for entry in reversed(entries[-recent_n:]):
                usage = entry.get("usage") or {}
                item: dict[str, Any] = {
                    "time": entry.get("iso_time"),
                    "job_id": entry.get("job_id"),
                    "backend": entry.get("backend"),
                    "model": entry.get("model"),
                    "status": entry.get("status"),
                    "duration_seconds": entry.get("duration_seconds"),
                    "total_tokens": usage.get("total_tokens") if isinstance(usage, dict) else None,
                }
                if entry.get("label"):
                    item["label"] = entry["label"]
                recent.append(item)

        return {
            "period": period,
            "session_key": self.session_key,
            "ledger_path": str(self._ledger_path),
            "totals": totals,
            "by_backend": by_backend,
            "by_model": by_model,
            "recent": recent,
        }
