"""Opt-in live subscription quota lookups, reset countdowns, and weekly-reset routing advisory."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from agent_bridge.process_control import ManagedProcess, subprocess_creation_kwargs

logger = logging.getLogger("agent_bridge.quota")


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
    if isinstance(mins, bool) or not isinstance(mins, (int, float)) or mins <= 0:
        return "unknown"
    m = int(mins)
    if m == 300:
        return "5h"
    if m == 10080:
        return "weekly"
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
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return float(val)


def _infer_agy_group_pool(group_name: str, buckets: list[dict[str, Any]]) -> str:
    low = group_name.lower()
    if any(tok in low for tok in ("claude", "gpt", "3p", "third")):
        return "agy_claude_gpt"
    for b in buckets:
        bid = str(b.get("id") or "").lower()
        if bid.startswith("3p-"):
            return "agy_claude_gpt"
    return "agy_gemini"


def _extract_models_from_description(description: str | None) -> list[str]:
    if not description or not isinstance(description, str):
        return []
    m = re.search(r"models\s+within\s+this\s+group\s*:\s*(.+)$", description.strip(), re.I)
    if not m:
        return []
    return [part.strip() for part in m.group(1).split(",") if part.strip()]


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

    resets_in_sec = max(0, int((reset_dt - now_utc).total_seconds()))
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
            (resets_in_sec <= 24 * 3_600 and rem_pct >= 10.0)
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


def enrich_quota_reading(
    reading: dict[str, Any],
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Return a copy of a Codex or Neon quota reading with live reset countdowns and pacing signals."""
    now = now_utc or datetime.now(timezone.utc)
    out = copy.deepcopy(reading)
    if out.get("status") != "ok":
        return out

    if isinstance(out.get("windows"), list):
        out["pool"] = out.get("pool") or "codex"
        out["windows"] = [_enrich_window(w, now_utc=now) for w in out["windows"]]

    if isinstance(out.get("groups"), list):
        for g in out["groups"]:
            if not isinstance(g, dict):
                continue
            wins = g.get("windows") or []
            g["pool"] = g.get("pool") or _infer_agy_group_pool(str(g.get("name") or ""), wins)
            g["windows"] = [_enrich_window(w, now_utc=now) for w in wins]

    return out


def build_quota_advisory(
    quota_by_backend: dict[str, dict[str, Any]],
    *,
    now_utc: datetime | None = None,
    neon_pool_models: dict[str, list[str]] | None = None,
    codex_default_label: str | None = None,
) -> dict[str, Any]:
    """Build a unified, weekly-reset-aware routing advisory across all 3 quota pools."""
    now = now_utc or datetime.now(timezone.utc)
    pools_info: list[dict[str, Any]] = []
    expiring_weekly_alerts: list[str] = []
    upcoming_resets: list[dict[str, Any]] = []

    def _summarize_pool(
        *,
        pool: str,
        backend: str,
        label: str,
        windows: list[dict[str, Any]],
        models: list[str],
        rate_limit_reached: bool = False,
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

        # Record all active resets so parent AI can inspect exact reset times of all involved models.
        for w in windows:
            if w.get("reset_time") and w.get("resets_in_seconds") is not None:
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

        # Determine routing priority & human recommendation.
        if rate_limit_reached or w5_sig == "exhausted" or wk_sig == "exhausted":
            priority = "avoid"
            blocked_win = win_weekly if wk_sig == "exhausted" else win_5h
            reset_note = (
                f" resets in {blocked_win.get('resets_in')} ({blocked_win.get('reset_time')})"
                if blocked_win and blocked_win.get("resets_in")
                else ""
            )
            rec = f"Quota exhausted on {label};{reset_note}. Route to another eligible pool until reset."
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

        pools_info.append(
            {
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
        )

    # Codex pool
    codex_q = quota_by_backend.get("codex")
    if isinstance(codex_q, dict) and codex_q.get("status") == "ok":
        enriched_codex = enrich_quota_reading(codex_q, now_utc=now)
        codex_models = [codex_default_label] if codex_default_label else ["gpt-6.2-sol (high)"]
        _summarize_pool(
            pool="codex",
            backend="codex",
            label="Codex CLI",
            windows=enriched_codex.get("windows") or [],
            models=codex_models,
            rate_limit_reached=bool(enriched_codex.get("rate_limit_reached")),
        )

    # Antigravity pools (both `Gemini Models` AND `Claude and GPT models`!)
    neon_q = quota_by_backend.get("neon")
    if isinstance(neon_q, dict) and neon_q.get("status") == "ok":
        enriched_neon = enrich_quota_reading(neon_q, now_utc=now)
        for group in enriched_neon.get("groups") or []:
            if not isinstance(group, dict):
                continue
            pool_name = str(group.get("pool") or "agy_gemini")
            group_models = (
                (neon_pool_models or {}).get(pool_name)
                or group.get("models")
                or []
            )
            _summarize_pool(
                pool=pool_name,
                backend="neon",
                label=str(group.get("name") or pool_name),
                windows=group.get("windows") or [],
                models=list(group_models),
            )

    priority_rank = {
        "use_before_reset": 0,
        "high": 1,
        "normal": 2,
        "conserve": 3,
        "avoid": 4,
    }

    def _pool_sort_key(p: dict[str, Any]) -> tuple[int, float, float]:
        pr = priority_rank.get(str(p.get("priority")), 3)
        wk = p.get("weekly") or {}
        wk_secs = wk.get("resets_in_seconds")
        # Among equally prioritized pools: for `use_before_reset`, soonest reset first;
        # for others, higher remaining % first, then sooner weekly reset!
        eff = float(p.get("effective_remaining_percent") or 0.0)
        if pr == 0:
            return (pr, float(wk_secs if wk_secs is not None else 10**9), -eff)
        return (pr, -eff, float(wk_secs if wk_secs is not None else 10**9))

    pools_sorted = sorted(pools_info, key=_pool_sort_key)
    recommended_order = [p["pool"] for p in pools_sorted if p.get("priority") != "avoid"]
    upcoming_resets.sort(key=lambda item: int(item.get("resets_in_seconds") or 10**9))

    if expiring_weekly_alerts:
        summary_text = "WEEKLY RESET PRIORITY: " + " | ".join(expiring_weekly_alerts)
    elif pools_sorted:
        top = pools_sorted[0]
        next_reset = upcoming_resets[0] if upcoming_resets else None
        reset_part = (
            f" Next reset: {next_reset['pool']} {next_reset['window']} in {next_reset['resets_in']}."
            if next_reset
            else ""
        )
        summary_text = (
            f"Best available pool: {top['pool']} ({top['effective_remaining_percent']}% effective remaining)."
            f"{reset_part}"
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


def parse_codex_rate_limits(
    result: dict[str, Any],
    *,
    fetched_at: str | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Parse `codex app-server` `account/rateLimits/read` result into a clean quota summary."""
    ts = fetched_at or _iso_now()
    rl = result.get("rateLimits")
    if not isinstance(rl, dict):
        return {
            "backend": "codex",
            "pool": "codex",
            "status": "unknown",
            "fetched_at": ts,
            "error": "Missing rateLimits object in Codex app-server response.",
        }

    windows: list[dict[str, Any]] = []
    for slot in ("primary", "secondary"):
        bucket = rl.get(slot)
        if not isinstance(bucket, dict):
            continue
        used_num = _as_number(bucket.get("usedPercent"))
        if used_num is None or used_num < 0.0 or used_num > 100.0:
            continue
        used_pct = round(used_num, 1)
        rem_pct = round(max(0.0, 100.0 - used_pct), 1)
        windows.append(
            {
                "slot": slot,
                "window": _window_label(bucket.get("windowDurationMins")),
                "used_percent": used_pct,
                "remaining_percent": rem_pct,
                "reset_time": _epoch_to_iso(bucket.get("resetsAt")),
            }
        )

    if not windows:
        return {
            "backend": "codex",
            "pool": "codex",
            "status": "unknown",
            "fetched_at": ts,
            "plan_type": rl.get("planType"),
            "windows": [],
            "error": "Codex rateLimits contained no usable quota windows.",
        }

    out: dict[str, Any] = {
        "backend": "codex",
        "pool": "codex",
        "status": "ok",
        "fetched_at": ts,
        "plan_type": rl.get("planType"),
        "ordinary_usage_allowed": result.get("ordinaryUsageAllowed"),
        "rate_limit_reached": bool(rl.get("rateLimitReachedType")) or bool(rl.get("spendControlReached")),
        "windows": windows,
    }
    if rl.get("rateLimitReachedType"):
        out["rate_limit_reached_type"] = rl.get("rateLimitReachedType")
    return enrich_quota_reading(out, now_utc=now_utc)


def parse_neon_usage_envelope(
    payload: dict[str, Any],
    *,
    fetched_at: str | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Parse `agy --output-format json --print=/usage` envelope (structured JSON + text fallback)."""
    ts = fetched_at or _iso_now()
    groups_out: list[dict[str, Any]] = []

    cmd_data = payload.get("command", {}).get("data") if isinstance(payload.get("command"), dict) else None
    raw_groups = cmd_data.get("groups") if isinstance(cmd_data, dict) else None

    if isinstance(raw_groups, list):
        for g in raw_groups:
            if not isinstance(g, dict):
                continue
            windows: list[dict[str, Any]] = []
            for b in g.get("buckets") or []:
                if not isinstance(b, dict):
                    continue
                rem_frac = _as_number(b.get("remaining_fraction"))
                if rem_frac is None or rem_frac < 0.0 or rem_frac > 1.0:
                    continue
                rem_pct = round(rem_frac * 100.0, 1)
                used_pct = round(max(0.0, 100.0 - rem_pct), 1)
                reset = b.get("reset_time")
                if reset == "1970-01-01T00:00:00Z":
                    reset = None
                windows.append(
                    {
                        "id": b.get("id"),
                        "window": b.get("window") or b.get("name"),
                        "used_percent": used_pct,
                        "remaining_percent": rem_pct,
                        "reset_time": reset,
                    }
                )
            if windows:
                gname = str(g.get("name") or "unknown")
                group_entry: dict[str, Any] = {
                    "name": gname,
                    "pool": _infer_agy_group_pool(gname, windows),
                    "windows": windows,
                }
                models_list = _extract_models_from_description(g.get("description"))
                if models_list:
                    group_entry["models"] = models_list
                groups_out.append(group_entry)

    # Fallback: parse tab-separated response text if command.data had no usable windows.
    if not groups_out and isinstance(payload.get("response"), str):
        by_group: dict[str, list[dict[str, Any]]] = {}
        for line in payload["response"].splitlines():
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            if len(parts) < 3:
                continue
            gname, wname, pct_raw = parts[0], parts[1], parts[2]
            reset = parts[3] if len(parts) >= 4 else None
            if reset == "1970-01-01T00:00:00Z":
                reset = None
            m = re.search(r"(\d+(?:\.\d+)?)%", pct_raw)
            if not m:
                continue
            rem_num = float(m.group(1))
            if rem_num < 0.0 or rem_num > 100.0:
                continue
            rem_pct = round(rem_num, 1)
            used_pct = round(max(0.0, 100.0 - rem_pct), 1)
            by_group.setdefault(gname, []).append(
                {
                    "window": wname,
                    "used_percent": used_pct,
                    "remaining_percent": rem_pct,
                    "reset_time": reset,
                }
            )
        groups_out = [
            {"name": k, "pool": _infer_agy_group_pool(k, v), "windows": v}
            for k, v in by_group.items()
            if v
        ]

    if not groups_out or not any(g.get("windows") for g in groups_out):
        return {
            "backend": "neon",
            "status": "unknown",
            "fetched_at": ts,
            "error": "Could not parse usable quota windows from agy /usage output.",
        }

    return enrich_quota_reading(
        {
            "backend": "neon",
            "status": "ok",
            "fetched_at": ts,
            "groups": groups_out,
        },
        now_utc=now_utc,
    )


async def fetch_codex_quota(codex_cmd: list[str], *, timeout_seconds: float = 12.0) -> dict[str, Any]:
    """Perform a short-lived one-shot `codex app-server` `account/rateLimits/read` RPC."""
    ts = _iso_now()
    managed: ManagedProcess | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *codex_cmd,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_creation_kwargs(),
        )
        managed = ManagedProcess(proc)
        assert proc.stdin is not None and proc.stdout is not None

        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "agent-bridge", "version": "0.3.0"},
                    "capabilities": {},
                },
            },
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
        ]
        payload = "".join(json.dumps(m) + "\n" for m in messages).encode("utf-8")
        proc.stdin.write(payload)
        await proc.stdin.drain()

        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "backend": "codex",
                    "pool": "codex",
                    "status": "unknown",
                    "fetched_at": ts,
                    "error": f"Timed out reading Codex rate limits after {timeout_seconds:.0f}s.",
                }
            raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not raw_line:
                break
            text = raw_line.decode("utf-8", errors="replace").strip()
            if not text.startswith("{"):
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == 2:
                if "result" in msg and isinstance(msg["result"], dict):
                    return parse_codex_rate_limits(msg["result"], fetched_at=ts)
                err = msg.get("error", {})
                return {
                    "backend": "codex",
                    "pool": "codex",
                    "status": "unknown",
                    "fetched_at": ts,
                    "error": str(err.get("message") if isinstance(err, dict) else err),
                }

        return {
            "backend": "codex",
            "pool": "codex",
            "status": "unknown",
            "fetched_at": ts,
            "error": "Codex app-server closed before returning rate limits.",
        }
    except Exception as exc:
        return {
            "backend": "codex",
            "pool": "codex",
            "status": "unknown",
            "fetched_at": ts,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if managed is not None:
            await managed.terminate()


async def fetch_neon_quota(agy_cmd: list[str], *, timeout_seconds: float = 15.0) -> dict[str, Any]:
    """Perform a short-lived one-shot `agy --output-format json --print=/usage` probe."""
    ts = _iso_now()
    managed: ManagedProcess | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *agy_cmd,
            "--mode",
            "plan",
            "--output-format",
            "json",
            "--print-timeout",
            f"{max(1, int(timeout_seconds))}s",
            "--print=/usage",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_creation_kwargs(),
        )
        managed = ManagedProcess(proc)
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        managed.close()

        stdout_text = stdout_b.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or not stdout_text:
            err = stderr_b.decode("utf-8", errors="replace").strip() or f"agy /usage exited {proc.returncode}"
            return {
                "backend": "neon",
                "status": "unknown",
                "fetched_at": ts,
                "error": err[:400],
            }

        envelope: dict[str, Any] | None = None
        try:
            parsed = json.loads(stdout_text)
            if isinstance(parsed, dict):
                envelope = parsed
        except json.JSONDecodeError:
            for line in reversed(stdout_text.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        parsed = json.loads(line)
                        if isinstance(parsed, dict):
                            envelope = parsed
                            break
                    except json.JSONDecodeError:
                        continue

        if envelope is None:
            return {
                "backend": "neon",
                "status": "unknown",
                "fetched_at": ts,
                "error": "Non-JSON response from agy /usage.",
            }

        return parse_neon_usage_envelope(envelope, fetched_at=ts)
    except Exception as exc:
        return {
            "backend": "neon",
            "status": "unknown",
            "fetched_at": ts,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if managed is not None:
            await managed.terminate()
