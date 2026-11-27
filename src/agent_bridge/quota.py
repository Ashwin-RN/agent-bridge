"""Subscription quota enrichment, reset countdowns, and weekly-reset routing advisory."""

from __future__ import annotations

import copy
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any

from agent_bridge.backends.agy import (
    fetch_neon_quota,
    parse_neon_usage_envelope,
)
from agent_bridge.backends.claude import (
    CLAUDE_QUOTA_NOTE,
    claude_quota_from_rate_limit,
    merge_claude_quota,
)
from agent_bridge.backends.codex import (
    fetch_codex_quota,
    parse_codex_rate_limits,
)
from agent_bridge.backends.registry import get_adapter_optional, get_default_registry

logger = logging.getLogger("agent_bridge.quota")

_FIVE_HOUR_MAX_MINS = 6 * 60
_WEEKLY_MIN_MINS = 6 * 1440
_REASON_LIMIT = 160


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _epoch_to_iso(ts: int | float | None) -> str | None:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")


def _parse_iso_utc(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or text == "1970-01-01T00:00:00Z":
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _window_label(mins: int | float | None) -> str:
    if isinstance(mins, bool) or not isinstance(mins, (int, float)) or not math.isfinite(mins) or mins <= 0:
        return "unknown"
    if mins <= _FIVE_HOUR_MAX_MINS:
        return "5h"
    if mins >= _WEEKLY_MIN_MINS:
        return "weekly"
    m = int(mins)
    if m % 1440 == 0:
        return f"{m // 1440}d"
    if m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}m"


def _window_duration_seconds(label: str | None) -> int | None:
    if not label:
        return None
    low = label.strip().lower()
    if low in {"weekly", "week", "7d"} or "week" in low:
        return 7 * 86_400
    if low in {"5h", "five_hour", "five hour"} or "five hour" in low or "5-hour" in low:
        return 5 * 3_600
    m = re.fullmatch(r"(\d+)\s*([dhm])", low)
    if not m:
        return None
    num = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        return num * 86_400
    if unit == "h":
        return num * 3_600
    return num * 60


def _canonical_window_kind(label: str | None, bucket_id: str | None = None) -> str:
    combined = f"{label or ''} {bucket_id or ''}".lower()
    if "week" in combined or "7d" in combined:
        return "weekly"
    if "5h" in combined or "five hour" in combined or "5-hour" in combined:
        return "5h"
    dur = _window_duration_seconds(label)
    if dur is not None and dur > 0:
        if dur <= _FIVE_HOUR_MAX_MINS * 60:
            return "5h"
        if dur >= _WEEKLY_MIN_MINS * 60:
            return "weekly"
    return (label or "unknown").strip().lower()


def _format_duration_human(seconds: int) -> str:
    if seconds <= 0:
        return "now"
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    mins = rem // 60
    if days > 0:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    if mins > 0:
        return f"{mins}m"
    return f"{seconds}s"


def _as_number(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
        return None
    return float(val)


def _enrich_window(
    win: dict[str, Any],
    *,
    now_utc: datetime,
) -> dict[str, Any]:
    """Attach live reset countdowns, time-remaining %, and weekly-reset awareness signals."""
    enriched = dict(win)
    kind = _canonical_window_kind(enriched.get("window"), enriched.get("id"))
    enriched["window_kind"] = kind
    rem_pct = _as_number(enriched.get("remaining_percent"))
    reset_dt = _parse_iso_utc(enriched.get("reset_time"))
    dur_sec = _window_duration_seconds(kind) or _window_duration_seconds(enriched.get("window"))

    if reset_dt is None:
        enriched["resets_in_seconds"] = None
        if rem_pct is not None and rem_pct >= 99.0:
            enriched["resets_in"] = "inactive (starts on first use)"
            enriched["signal"] = "idle_full"
        else:
            enriched["resets_in"] = None
            enriched["signal"] = "exhausted" if (rem_pct is not None and rem_pct <= 2.0) else "on_track"
        return enriched

    delta_sec = int((reset_dt - now_utc).total_seconds())
    if delta_sec <= 0:
        enriched["stale_reset"] = True
        enriched["resets_in_seconds"] = 0
        enriched["resets_in"] = "reset passed (stale — refresh quota)"
        if dur_sec and dur_sec > 0:
            enriched["time_remaining_percent"] = 0.0
        enriched["signal"] = "stale_reset"
        return enriched

    enriched["stale_reset"] = False
    resets_in_sec = delta_sec
    enriched["resets_in_seconds"] = resets_in_sec
    enriched["resets_in"] = _format_duration_human(resets_in_sec)

    time_rem_pct: float | None = None
    if dur_sec and dur_sec > 0:
        time_rem_pct = round(min(100.0, max(0.0, (resets_in_sec / dur_sec) * 100.0)), 1)
        enriched["time_remaining_percent"] = time_rem_pct

    # Classify quota pacing and expiring-before-reset opportunity.
    signal = "on_track"
    if rem_pct is not None:
        if rem_pct <= 2.0:
            signal = "exhausted"
        elif kind == "weekly" and (
            (resets_in_sec <= 24 * 3_600 and rem_pct >= 20.0)
            or (
                (resets_in_sec <= 42 * 3_600 or (time_rem_pct is not None and time_rem_pct <= 25.0))
                and rem_pct >= 20.0
                and (time_rem_pct is None or rem_pct >= time_rem_pct + 10.0)
            )
        ):
            signal = "expiring_soon_use_it"
        elif kind == "5h" and resets_in_sec <= 45 * 60 and rem_pct >= 35.0:
            signal = "expiring_soon_use_it"
        elif rem_pct <= 15.0:
            signal = "low"
        elif time_rem_pct is not None and rem_pct >= time_rem_pct + 15.0:
            signal = "ahead_of_pace"
        elif time_rem_pct is not None and rem_pct + 15.0 < time_rem_pct and rem_pct <= 45.0:
            signal = "burning_fast"
    enriched["signal"] = signal
    return enriched


def has_passed_reset(
    reading: dict[str, Any] | None,
    *,
    now_utc: datetime | None = None,
) -> bool:
    """Return True if any window in a quota reading has a `reset_time` that has already passed."""
    if not isinstance(reading, dict) or reading.get("status") != "ok":
        return False
    now = now_utc or datetime.now(timezone.utc)
    windows: list[dict[str, Any]] = []
    if isinstance(reading.get("windows"), list):
        windows.extend(w for w in reading["windows"] if isinstance(w, dict))
    if isinstance(reading.get("groups"), list):
        for g in reading["groups"]:
            if isinstance(g, dict) and isinstance(g.get("windows"), list):
                windows.extend(w for w in g["windows"] if isinstance(w, dict))
    for w in windows:
        reset_dt = _parse_iso_utc(w.get("reset_time"))
        if reset_dt is not None and (reset_dt - now).total_seconds() <= 0:
            return True
    return False


def without_passed_windows(
    reading: dict[str, Any] | None,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    """A reading without the windows whose reset has passed. None when no window is left."""
    if not isinstance(reading, dict) or reading.get("status") != "ok":
        return None
    now = now_utc or datetime.now(timezone.utc)
    windows = [w for w in reading.get("windows") or [] if isinstance(w, dict)]
    kept: list[dict[str, Any]] = []
    for w in windows:
        reset_dt = _parse_iso_utc(w.get("reset_time"))
        if reset_dt is not None and (reset_dt - now).total_seconds() <= 0:
            continue
        kept.append(w)
    if not kept:
        return None
    out = {**reading, "windows": kept}
    if len(kept) < len(windows):
        # The limit the last job hit may be the one that has since reset.
        out["rate_limit_reached"] = any((_as_number(w.get("remaining_percent")) or 0.0) <= 0.0 for w in kept)
    return out


def enrich_quota_reading(
    reading: dict[str, Any],
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Return a copy of a quota reading with live reset countdowns and pacing signals."""
    now = now_utc or datetime.now(timezone.utc)
    out = copy.deepcopy(reading)
    if out.get("status") != "ok":
        return out

    enrich_win = lambda w: _enrich_window(w, now_utc=now)
    adapter = get_adapter_optional(out.get("backend"))
    if adapter is not None:
        return adapter.enrich_quota_reading(out, enrich_win)

    if isinstance(out.get("windows"), list):
        out["pool"] = out.get("pool") or "codex"
        out["windows"] = [enrich_win(w) for w in out["windows"]]
    if isinstance(out.get("groups"), list):
        for candidate in get_default_registry().adapters():
            if len(candidate.quota_pools) > 1:
                out = candidate.enrich_quota_reading(out, enrich_win)
                break
    return out


def build_quota_advisory(
    quota_by_backend: dict[str, dict[str, Any]],
    *,
    now_utc: datetime | None = None,
    neon_pool_models: dict[str, list[str]] | None = None,
    codex_default_label: str | None = None,
    claude_default_label: str | None = None,
) -> dict[str, Any]:
    """Build a unified, weekly-reset-aware routing advisory across all quota pools."""
    now = now_utc or datetime.now(timezone.utc)
    pools_info: list[dict[str, Any]] = []
    expiring_weekly_alerts: list[str] = []
    upcoming_resets: list[dict[str, Any]] = []
    registry = get_default_registry()
    spec_by_pool = {spec.pool: spec for spec in registry.quota_pools()}
    default_labels = {"codex": codex_default_label, "claude": claude_default_label}

    def _summarize_pool(
        *,
        pool: str,
        backend: str,
        label: str,
        windows: list[dict[str, Any]],
        models: list[str],
        rate_limit_reached: bool = False,
        shared_with_own_use: bool = False,
    ) -> None:
        win_5h = next((w for w in windows if w.get("window_kind") == "5h"), None)
        win_weekly = next((w for w in windows if w.get("window_kind") == "weekly"), None)

        rem_values = [
            float(w["remaining_percent"])
            for w in windows
            if isinstance(w.get("remaining_percent"), (int, float))
        ]
        eff_rem = round(min(rem_values), 1) if rem_values else None

        def _compact_win(w: dict[str, Any] | None) -> dict[str, Any] | None:
            if w is None:
                return None
            c: dict[str, Any] = {
                "remaining_percent": w.get("remaining_percent"),
                "used_percent": w.get("used_percent"),
                "reset_time": w.get("reset_time"),
                "resets_in": w.get("resets_in"),
                "signal": w.get("signal"),
            }
            if w.get("resets_in_seconds") is not None:
                c["resets_in_seconds"] = w["resets_in_seconds"]
            if w.get("time_remaining_percent") is not None:
                c["time_remaining_percent"] = w["time_remaining_percent"]
            return c

        # Record only FUTURE active resets so parent AI can inspect upcoming reset schedules accurately.
        for w in windows:
            if (
                w.get("reset_time")
                and not w.get("stale_reset")
                and isinstance(w.get("resets_in_seconds"), int)
                and int(w["resets_in_seconds"]) > 0
            ):
                upcoming_resets.append(
                    {
                        "pool": pool,
                        "backend": backend,
                        "window": w.get("window_kind") or w.get("window"),
                        "remaining_percent": w.get("remaining_percent"),
                        "reset_time": w.get("reset_time"),
                        "resets_in": w.get("resets_in"),
                        "resets_in_seconds": w.get("resets_in_seconds"),
                        "signal": w.get("signal"),
                        "models": models[:6],
                    }
                )

        w5_sig = win_5h.get("signal") if win_5h else None
        wk_sig = win_weekly.get("signal") if win_weekly else None
        w5_rem = _as_number(win_5h.get("remaining_percent")) if win_5h else None
        wk_rem = _as_number(win_weekly.get("remaining_percent")) if win_weekly else None
        has_stale = any(bool(w.get("stale_reset")) for w in windows)
        spec = spec_by_pool.get(pool)

        # Determine routing priority & human recommendation.
        if has_stale:
            priority = "stale"
            rec = (
                f"Quota reading for {label} is stale (a reset timestamp has already passed). "
                "Call usage(quota=True) again for fresh post-reset capacity."
            )
        elif rate_limit_reached or w5_sig == "exhausted" or wk_sig == "exhausted":
            priority = "avoid"
            blocked_win = win_weekly if wk_sig == "exhausted" else win_5h
            reset_note = (
                f" resets in {blocked_win.get('resets_in')} ({blocked_win.get('reset_time')})"
                if blocked_win and blocked_win.get("resets_in")
                else ""
            )
            rec = f"Quota exhausted on {label};{reset_note}. Route to another eligible pool until reset."
        elif shared_with_own_use and (wk_sig == "expiring_soon_use_it" or w5_sig == "expiring_soon_use_it"):
            # What is left of this plan before its reset is also what the user's own sessions run on.
            # Say what is there, and never urge a host to spend it.
            priority = "normal"
            shared_tail = (
                spec.shared_advisory_note
                if spec is not None and spec.shared_advisory_note
                else "It is the same allowance the user's own sessions use."
            )
            rec = (
                f"{label} has weekly {wk_rem if wk_rem is not None else '?'}% left, resetting in "
                f"{win_weekly.get('resets_in') if win_weekly else 'unknown'}. {shared_tail}"
            )
        elif wk_sig == "expiring_soon_use_it" and (w5_rem is None or w5_rem > 5.0):
            priority = "use_before_reset"
            rec = (
                f"Weekly quota on {label} has {wk_rem:.1f}% unused and resets in "
                f"{win_weekly.get('resets_in') if win_weekly else 'soon'} — prioritize this pool to use "
                "expiring weekly capacity before reset!"
            )
            expiring_weekly_alerts.append(
                f"{pool}: {wk_rem:.1f}% weekly remaining, resets in "
                f"{win_weekly.get('resets_in') if win_weekly else 'soon'} "
                f"({win_weekly.get('reset_time') if win_weekly else ''}) — prioritize before reset."
            )
        elif w5_sig == "expiring_soon_use_it" and wk_sig not in {"low", "burning_fast"}:
            priority = "high"
            rec = (
                f"5h quota on {label} has {w5_rem:.1f}% remaining and resets in "
                f"{win_5h.get('resets_in') if win_5h else 'soon'} — good window to burst work now."
            )
        elif wk_sig == "idle_full" and w5_sig == "idle_full":
            priority = "high"
            rec = (
                f"Fresh separate {label} pool (100% weekly & 5h untouched; window timer starts on first use)."
            )
        elif wk_sig in {"low", "burning_fast"} or w5_sig == "low":
            priority = "conserve"
            rec = (
                f"Conserve {label} (weekly {wk_rem if wk_rem is not None else '?'}%, "
                f"5h {w5_rem if w5_rem is not None else '?'}%; weekly resets in "
                f"{win_weekly.get('resets_in') if win_weekly else 'unknown'})."
            )
        elif (eff_rem is not None and eff_rem >= 70.0) or wk_sig == "ahead_of_pace":
            priority = "high"
            rec = (
                f"Healthy headroom on {label} (weekly {wk_rem if wk_rem is not None else '?'}% left, "
                f"resets in {win_weekly.get('resets_in') if win_weekly else 'unknown'}; "
                f"5h {w5_rem if w5_rem is not None else '?'}% left, "
                f"resets in {win_5h.get('resets_in') if win_5h else 'unknown'})."
            )
        else:
            priority = "normal"
            rec = (
                f"Moderate headroom on {label} (weekly {wk_rem if wk_rem is not None else '?'}% left, "
                f"resets in {win_weekly.get('resets_in') if win_weekly else 'unknown'})."
            )

        pool_entry: dict[str, Any] = {
            "pool": pool,
            "backend": backend,
            "label": label,
            "models": models[:8],
            "effective_remaining_percent": eff_rem,
            "five_hour": _compact_win(win_5h),
            "weekly": _compact_win(win_weekly),
            "priority": priority,
            "recommendation": rec,
        }
        if has_stale:
            pool_entry["stale"] = True
        if shared_with_own_use:
            pool_entry["shared_with_own_use"] = True
        pools_info.append(pool_entry)

    for adapter in registry.dashboard_backends():
        reading = quota_by_backend.get(adapter.name)
        if not isinstance(reading, dict) or reading.get("status") != "ok":
            continue
        enriched = enrich_quota_reading(reading, now_utc=now)
        for descriptor in adapter.advisory_pools(
            enriched,
            neon_pool_models=neon_pool_models,
            default_label=default_labels.get(adapter.name),
        ):
            _summarize_pool(**descriptor)

    priority_rank = {
        "use_before_reset": 0,
        "high": 1,
        "normal": 2,
        "conserve": 3,
        "stale": 4,
        "avoid": 5,
    }

    def _pool_sort_key(p: dict[str, Any]) -> tuple[int, float, float]:
        pr = priority_rank.get(str(p.get("priority")), 3)
        wk = p.get("weekly") or {}
        wk_secs = wk.get("resets_in_seconds")
        eff = float(p.get("effective_remaining_percent") or 0.0)
        if pr == 0:
            return (pr, float(wk_secs if wk_secs is not None else 10**9), -eff)
        return (pr, -eff, float(wk_secs if wk_secs is not None else 10**9))

    pools_sorted = sorted(pools_info, key=_pool_sort_key)
    eligible_pools = [p for p in pools_sorted if p.get("priority") not in {"avoid", "stale"}]
    stale_pools = [p for p in pools_sorted if p.get("priority") == "stale"]
    # A pool the user's own sessions run on is never offered for routing. It is used when a job names it.
    routable_pools = [p for p in eligible_pools if not p.get("shared_with_own_use")]
    recommended_order = [p["pool"] for p in routable_pools]
    upcoming_resets.sort(key=lambda item: int(item.get("resets_in_seconds") or 10**9))

    next_reset = upcoming_resets[0] if upcoming_resets else None
    reset_part = (
        f" Next reset: {next_reset['pool']} {next_reset['window']} in {next_reset['resets_in']}."
        if next_reset
        else ""
    )

    if expiring_weekly_alerts:
        summary_text = "WEEKLY RESET PRIORITY: " + " | ".join(expiring_weekly_alerts)
    elif routable_pools:
        top = routable_pools[0]
        summary_text = (
            f"Best available pool: {top['pool']} ({top['effective_remaining_percent']}% effective remaining)."
            f"{reset_part}"
        )
    elif eligible_pools:
        own_names = ", ".join(p["pool"] for p in eligible_pools)
        summary_text = (
            f"No pool to route to. Only {own_names} has room, and that is the allowance the user's own "
            f"sessions use: name it in the job to use it.{reset_part}"
        )
    elif stale_pools:
        stale_names = ", ".join(p["pool"] for p in stale_pools)
        summary_text = (
            f"No fresh eligible pools ({stale_names} reading(s) passed reset time); "
            "call usage(quota=True) again."
        )
    elif pools_sorted:
        summary_text = (
            f"No eligible pools available: all {len(pools_sorted)} pool(s) are exhausted.{reset_part}"
        )
    else:
        summary_text = "No live quota windows available; check individual backend quota errors."

    return {
        "generated_at": _iso_now(),
        "pools": pools_sorted,
        "expiring_weekly_alerts": expiring_weekly_alerts,
        "recommended_order": recommended_order,
        "upcoming_resets": upcoming_resets,
        "summary": summary_text,
    }


def _clean_pct(val: float) -> int | float:
    rounded = round(val, 1)
    return int(rounded) if rounded.is_integer() else rounded


def _short_reason(text: Any) -> str:
    line = " ".join(str(text or "").split())
    if len(line) <= _REASON_LIMIT:
        return line
    return line[: _REASON_LIMIT - 3].rstrip() + "..."


def _oldest_iso(times: list[Any]) -> str | None:
    parsed: list[tuple[datetime, str]] = []
    for raw in times:
        if not isinstance(raw, str):
            continue
        dt = _parse_iso_utc(raw)
        if dt is not None:
            parsed.append((dt, raw.strip()))
    return min(parsed, key=lambda item: item[0])[1] if parsed else None


def _view_window(win: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(win, dict):
        return None
    rem = _as_number(win.get("remaining_percent"))
    if rem is None:
        return None
    sig = win.get("signal")
    reset_raw = win.get("reset_time")
    reset_iso = reset_raw.strip() if isinstance(reset_raw, str) and _parse_iso_utc(reset_raw) is not None else None
    if win.get("stale_reset") or sig == "stale_reset":
        state = "stale"
    elif sig == "idle_full":
        state = "not_started"
    elif sig == "exhausted":
        state = "exhausted"
    elif sig == "expiring_soon_use_it":
        state = "expiring"
    elif sig == "low":
        state = "low"
    else:
        state = "ok"
    has_countdown = (
        reset_iso is not None
        and state not in {"stale", "not_started"}
        and isinstance(win.get("resets_in_seconds"), int)
        and int(win["resets_in_seconds"]) > 0
    )
    return {
        "left_percent": _clean_pct(rem),
        "resets_in": win.get("resets_in") if has_countdown else None,
        "reset_time": reset_iso,
        "state": state,
    }


def _pct_summary(vw_5h: dict[str, Any] | None, vw_wk: dict[str, Any] | None) -> str:
    parts: list[str] = []
    if vw_wk is not None:
        parts.append(f"weekly {vw_wk['left_percent']}%")
    if vw_5h is not None:
        parts.append(f"5h {vw_5h['left_percent']}%")
    return ", ".join(parts)


def build_quota_view(quota: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Build one compact quota view with every pool listed once in fixed order."""
    now_utc = datetime.fromtimestamp(float(now), tz=timezone.utc) if now is not None else datetime.now(timezone.utc)
    readings = quota if isinstance(quota, dict) else {}
    registry = get_default_registry()
    pool_specs = registry.quota_pools()
    spec_by_pool = {spec.pool: spec for spec in pool_specs}

    raw_windows_by_pool: dict[str, list[dict[str, Any]]] = {}
    unrecognised_entries: list[tuple[str, str, str]] = []

    for adapter in registry.adapters():
        reading = readings.get(adapter.name)
        if isinstance(reading, dict) and reading.get("status") == "ok":
            extracted, unrec = adapter.extract_quota_windows(reading)
            for pool_name, wins in extracted.items():
                raw_windows_by_pool.setdefault(pool_name, []).extend(wins)
            for gname in unrec:
                unrecognised_entries.append(adapter.format_unrecognised_quota_group(gname))

    pools_out: list[dict[str, Any]] = []
    unread: list[dict[str, str]] = []
    failed_unread_names: list[str] = []
    used_read_times: list[Any] = []
    sort_meta: dict[str, tuple[str, float, float]] = {}

    for spec in pool_specs:
        pool = spec.pool
        backend = spec.backend
        label = spec.label
        reading = readings.get(backend)
        reading_dict = reading if isinstance(reading, dict) else {}
        pool_raw_wins = raw_windows_by_pool.get(pool)
        enriched_wins = [_enrich_window(w, now_utc=now_utc) for w in (pool_raw_wins or [])]

        win_5h = next(
            (w for w in enriched_wins if w.get("window_kind") == "5h" and _as_number(w.get("remaining_percent")) is not None),
            None,
        )
        win_wk = next(
            (
                w
                for w in enriched_wins
                if w.get("window_kind") == "weekly" and _as_number(w.get("remaining_percent")) is not None
            ),
            None,
        )
        vw_5h = _view_window(win_5h)
        vw_wk = _view_window(win_wk)

        if vw_5h is None and vw_wk is None:
            if spec.quota_view_no_reading and not reading_dict.get("error") and reading_dict.get("status") != "ok":
                reason = spec.quota_view_no_reading
                failed = False
            elif reading_dict.get("error"):
                reason = _short_reason(reading_dict["error"])
                failed = True
            elif pool_raw_wins is not None:
                reason = "reading holds no 5h or weekly window"
                failed = True
            elif reading_dict.get("status") == "ok":
                source_label = spec.source_label or label
                reason = f"{source_label} reported no window for this pool"
                failed = True
            elif not reading_dict:
                reason = "no reading provided"
                failed = True
            else:
                reason = "could not be read"
                failed = True

            pools_out.append(
                {
                    "pool": pool,
                    "backend": backend,
                    "label": label,
                    "five_hour": None,
                    "weekly": None,
                    "priority": "unknown",
                    "note": reason,
                }
            )
            unread.append({"pool": pool, "reason": reason})
            if failed:
                failed_unread_names.append(pool)
            continue

        for src_win in (win_5h, win_wk):
            if src_win is not None:
                used_read_times.append(src_win.get("fetched_at") or reading_dict.get("fetched_at"))

        w5_sig = win_5h.get("signal") if win_5h else None
        wk_sig = win_wk.get("signal") if win_wk else None
        w5_rem = _as_number(win_5h.get("remaining_percent")) if win_5h else None
        wk_rem = _as_number(win_wk.get("remaining_percent")) if win_wk else None
        rem_vals = [v for v in (w5_rem, wk_rem) if v is not None]
        eff_rem = round(min(rem_vals), 1) if rem_vals else 0.0
        has_stale = any(w is not None and w["state"] == "stale" for w in (vw_5h, vw_wk))
        rate_limit_reached = bool(reading_dict.get("rate_limit_reached"))
        w5_state = vw_5h["state"] if vw_5h else None
        wk_state = vw_wk["state"] if vw_wk else None
        shared = spec.shared_with_own_use

        if has_stale:
            priority = "stale"
            note = "reset time has passed; refresh quota"
        elif rate_limit_reached or w5_state == "exhausted" or wk_state == "exhausted":
            priority = "avoid"
            blocked = vw_wk if wk_state == "exhausted" else vw_5h
            if blocked and blocked.get("resets_in"):
                note = f"exhausted, resets in {blocked['resets_in']}"
            elif rate_limit_reached:
                note = "rate limit reached"
            else:
                note = "exhausted"
        elif shared:
            suffix = spec.shared_note_suffix
            if wk_sig in {"low", "burning_fast"} or w5_sig == "low":
                priority = "conserve"
                note = f"running low ({_pct_summary(vw_5h, vw_wk)}); {suffix}"
            elif wk_sig == "expiring_soon_use_it" or w5_sig == "expiring_soon_use_it":
                priority = "normal"
                note = f"{_pct_summary(vw_5h, vw_wk)} left; {suffix}"
            elif eff_rem >= 70.0 or wk_sig == "ahead_of_pace":
                priority = "high"
                note = f"healthy headroom ({_pct_summary(vw_5h, vw_wk)}); {suffix}"
            else:
                priority = "normal"
                note = f"moderate headroom ({_pct_summary(vw_5h, vw_wk)}); {suffix}"
        elif (
            wk_sig == "expiring_soon_use_it"
            and wk_rem is not None
            and wk_rem >= 20.0
            and (w5_rem is None or w5_rem > 5.0)
            and vw_wk is not None
        ):
            priority = "use_before_reset"
            resets_part = f", resets in {vw_wk['resets_in']}" if vw_wk.get("resets_in") else ""
            note = f"{vw_wk['left_percent']}% of the week unused{resets_part}"
        elif w5_sig == "expiring_soon_use_it" and wk_sig not in {"low", "burning_fast"} and vw_5h is not None:
            priority = "high"
            resets_part = f", resets in {vw_5h['resets_in']}" if vw_5h.get("resets_in") else ""
            note = f"{vw_5h['left_percent']}% of 5h window unused{resets_part}"
        elif (wk_sig == "idle_full" or vw_wk is None) and (w5_sig == "idle_full" or vw_5h is None):
            priority = "high"
            note = "untouched (100% left; timer starts on first use)"
        elif wk_sig in {"low", "burning_fast"} or w5_sig == "low":
            priority = "conserve"
            note = f"running low ({_pct_summary(vw_5h, vw_wk)})"
        elif eff_rem >= 70.0 or wk_sig == "ahead_of_pace":
            priority = "high"
            note = f"healthy headroom ({_pct_summary(vw_5h, vw_wk)})"
        else:
            priority = "normal"
            note = f"moderate headroom ({_pct_summary(vw_5h, vw_wk)})"

        pools_out.append(
            {
                "pool": pool,
                "backend": backend,
                "label": label,
                "five_hour": vw_5h,
                "weekly": vw_wk,
                "priority": priority,
                "note": note,
            }
        )
        wk_secs_raw = win_wk.get("resets_in_seconds") if win_wk else None
        wk_secs = float(wk_secs_raw) if isinstance(wk_secs_raw, int) and wk_secs_raw > 0 else float(10**9)
        sort_meta[pool] = (priority, eff_rem, wk_secs)

    for pool_key, reason, failed_label in unrecognised_entries:
        unread.append({"pool": pool_key, "reason": reason})
        failed_unread_names.append(failed_label)

    priority_rank = {
        "use_before_reset": 0,
        "high": 1,
        "normal": 2,
        "conserve": 3,
    }

    routable = [
        p
        for p in pools_out
        if not spec_by_pool[p["pool"]].shared_with_own_use
        and p["priority"] not in {"avoid", "stale", "unknown"}
    ]

    def _order_key(p: dict[str, Any]) -> tuple[int, float, float]:
        prio, eff, wk_secs = sort_meta[p["pool"]]
        rank = priority_rank.get(prio, 3)
        if rank == 0:
            return (rank, wk_secs, -eff)
        return (rank, -eff, wk_secs)

    routable_sorted = sorted(routable, key=_order_key)
    order = [p["pool"] for p in routable_sorted]

    stale_names = [p["pool"] for p in pools_out if p["priority"] == "stale"]
    avoid_names = [p["pool"] for p in pools_out if p["priority"] == "avoid"]
    shared_usable = [
        p["pool"]
        for p in pools_out
        if spec_by_pool[p["pool"]].shared_with_own_use
        and p["priority"] not in {"avoid", "stale", "unknown"}
    ]

    if routable_sorted:
        top = routable_sorted[0]
        if top["priority"] == "use_before_reset" and top["weekly"] is not None:
            wk = top["weekly"]
            resets_part = f", resets in {wk['resets_in']}" if wk.get("resets_in") else ""
            headline = f"Use {top['pool']} first ({wk['left_percent']}% weekly unused{resets_part})"
        else:
            _, top_eff, _ = sort_meta[top["pool"]]
            headline = f"Route to {top['pool']} first ({_clean_pct(top_eff)}% left)"
    elif shared_usable:
        headline = f"No routable pool available; only {', '.join(shared_usable)} has room (shared with your own sessions)"
    elif stale_names:
        headline = f"No fresh usable pools ({', '.join(stale_names)} stale); refresh quota"
    elif avoid_names:
        headline = "No usable pools available; all read pools are exhausted"
    else:
        headline = "No quota readings available"

    if failed_unread_names:
        summary = f"{headline}; could not read {', '.join(failed_unread_names)}."
    else:
        summary = f"{headline}."

    return {
        "read_at": _oldest_iso(used_read_times),
        "pools": pools_out,
        "order": order,
        "unread": unread,
        "summary": summary,
    }


__all__ = [
    "CLAUDE_QUOTA_NOTE",
    "build_quota_advisory",
    "build_quota_view",
    "claude_quota_from_rate_limit",
    "enrich_quota_reading",
    "fetch_codex_quota",
    "fetch_neon_quota",
    "has_passed_reset",
    "merge_claude_quota",
    "parse_codex_rate_limits",
    "parse_neon_usage_envelope",
    "without_passed_windows",
]
