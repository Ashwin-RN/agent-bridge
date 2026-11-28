"""The usage dashboard: what is left of each subscription, then what the bridge has run.

The view opens with the quota left in every pool, since that is what the reader came for: the 5h and
weekly windows of Codex, of Antigravity's two pools and of the Claude plan, each with its reset and the
time it was read. Those readings come from each CLI, not from the bridge's records, and no figure from
them is added to a total. Every pool is always listed; one that could not be read says why.

Below it is what the bridge ran in the period, from its own records only. A total holds only what the
records can vouch for: finished runs whose CLI gave an exact count, or a per-turn (delta) count.
Everything else is listed apart by job id and never added in:

- jobs still queued or running in a window that is still open, with any tokens already recorded for
  their earlier attempts;
- partial counts;
- runs whose CLI was started but left no count (cancelled, timed out, failed, or a CLI that reports
  none, such as Codex's built-in review);
- runs the records cannot speak for: the window that ran the job closed before it finished, the job row
  has been pruned, or the bridge could not confirm whether the job's CLI ran;
- Claude subagent tokens outside the reported counts, and Claude runs that did not say whether they
  started subagents;
- any part of the period from before the bridge's first record.

A run whose CLI was never started is counted, as a run that spent nothing. Claude Code's cost estimate is
left out: it is not a record the bridge can add up.

It covers jobs run through the bridge only, and says so in the view.

It reads and never writes: a job left running by a closed window is found by reading the owners table,
not by marking the job, which the summary view of `usage` still does, and a quota reading taken for the view is not put
in the cache.

The text view is rendered from the same dict that is returned, so the two cannot disagree. It shows a
caveat only when there is something under it; the dict always holds every entry.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from agent_bridge.quota import build_quota_advisory

BACKENDS: tuple[str, ...] = ("codex", "neon", "claude")
QUOTA_POOLS: tuple[str, ...] = ("codex", "agy_gemini", "agy_claude_gpt", "claude")
NAMED_PERIODS: tuple[str, ...] = ("today", "7d", "30d", "all", "session")
TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
)
COUNTED_USAGE: frozenset[str] = frozenset({"exact", "delta"})
UNCERTAIN_PROVIDER_STATES: frozenset[str] = frozenset({"unknown", "possibly_running"})
OUTCOMES: tuple[str, ...] = ("completed", "failed", "timed_out", "cancelled")
JOB_LIST_LIMIT = 10

PERIOD_RULE = "A run counts in the period its record was written, which is when it finished."
COVERS = "Covers jobs run through the bridge only."
NOT_COVERED: tuple[str, ...] = (
    "the host session's own tokens",
    "a host's own subagents",
    "CLI runs made outside the bridge",
)
CANNOT_TELL_REASONS: dict[str, str] = {
    "window_closed": "The window that ran the job closed before the job finished, so the records cannot say "
    "how far it got or what it spent.",
    "job_row_pruned": "No count, and the job's row has been pruned (the newest jobs are kept), so it is not "
    "known whether its CLI was started.",
    "end_not_confirmed": "No count, and the bridge could not confirm whether the job's CLI ran or stopped.",
}
_REASON_LABELS: dict[str, str] = {
    "window_closed": "its window closed",
    "job_row_pruned": "job row pruned",
    "end_not_confirmed": "end not confirmed",
}
LEFT_OUT: tuple[str, ...] = (
    "Claude Code's cost estimate: it is not a bill, and on a resumed session it repeats the earlier turns.",
)
TOKEN_NOTE = (
    "Token figures are each CLI's own counts, and their kinds are not alike: for claude, input_tokens "
    "includes cache reads and writes; for codex, cached_tokens is part of input_tokens; for agy, "
    "cached_tokens can exceed total_tokens."
)

POOL_LABELS: dict[str, str] = {
    "codex": "Codex",
    "agy_gemini": "Antigravity Gemini",
    "agy_claude_gpt": "Antigravity Claude+GPT",
    "claude": "Claude plan",
}
# Which backend's reading holds each pool.
_POOL_BACKEND: dict[str, str] = {
    "codex": "codex",
    "agy_gemini": "neon",
    "agy_claude_gpt": "neon",
    "claude": "claude",
}
QUOTA_SOURCE = (
    "Read from each CLI, not from the bridge's records. Each pool carries the time it was read. The "
    "Claude plan has no live read-out: its reading is what the Claude jobs run through the bridge "
    "reported, each window kept until its own reset, and its time is that of its oldest figure."
)
CLAUDE_NO_READING = "Claude Code prints none; it shows after a Claude job runs through the bridge"
NO_PLAN_WINDOW = "the reading holds no 5h or weekly window"
_AGY_POOLS: frozenset[str] = frozenset({"agy_gemini", "agy_claude_gpt"})
CLAUDE_SHARED_NOTE = (
    "Claude plan: as its bridge jobs reported it, at the read time or later. Your own Claude Code and "
    "claude.ai use draw on it too."
)
BAR_WIDTH = 10
REASON_LIMIT = 70


@dataclass(frozen=True, slots=True)
class Window:
    """The stretch of time asked for. `until_ts` is exclusive; None means up to now."""

    name: str
    since_ts: float | None
    until_ts: float | None
    only_session: bool


def _parse_bound(raw: str, *, field: str, end_of_day: bool) -> float:
    text = raw.strip()
    try:
        day = date.fromisoformat(text) if len(text) == 10 else None
    except ValueError:
        day = None
    if day is not None:
        midnight = datetime(day.year, day.month, day.day)
        # A date alone as `until` takes in the whole of that day.
        return (midnight + timedelta(days=1)).timestamp() if end_of_day else midnight.timestamp()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{field}={raw!r} is not a date or a time. Give a date such as 2026-11-25 or a time such as "
            "2026-11-25T14:30 (local time unless it carries an offset)."
        ) from None
    return moment.timestamp()  # a time with no offset is read as local time


def resolve_window(
    period: str | None,
    since: str | None,
    until: str | None,
    *,
    now_ts: float,
) -> Window:
    since = since if since and since.strip() else None
    until = until if until and until.strip() else None
    if period is not None and (since or until):
        raise ValueError("Give either period or since/until, not both.")
    if since or until:
        since_ts = _parse_bound(since, field="since", end_of_day=False) if since else None
        until_ts = _parse_bound(until, field="until", end_of_day=True) if until else None
        if since_ts is not None and until_ts is not None and since_ts >= until_ts:
            raise ValueError("since must be earlier than until.")
        return Window("custom", since_ts, until_ts, False)

    name = period or "today"
    if name == "today":
        midnight = datetime.fromtimestamp(now_ts).replace(hour=0, minute=0, second=0, microsecond=0)
        return Window(name, midnight.timestamp(), None, False)
    if name == "7d":
        return Window(name, now_ts - 7 * 86_400, None, False)
    if name == "30d":
        return Window(name, now_ts - 30 * 86_400, None, False)
    if name == "all":
        return Window(name, None, None, False)
    if name == "session":
        return Window(name, None, None, True)
    raise ValueError(f"Unknown period {period!r}. Expected one of: {', '.join(NAMED_PERIODS)}.")


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _bucket() -> dict[str, Any]:
    return {
        "runs": 0,
        **{outcome: 0 for outcome in OUTCOMES},
        "duration_seconds": 0.0,
        "tokens": {name: 0 for name in TOKEN_FIELDS},
    }


def _metrics(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _id_list(ids: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"count": len(ids), "job_ids": ids[:JOB_LIST_LIMIT]}
    if len(ids) > JOB_LIST_LIMIT:
        out["more"] = len(ids) - JOB_LIST_LIMIT
    return out


def _quota_window(win: dict[str, Any] | None) -> dict[str, Any] | None:
    """One limit window, cut down to what the view shows. None when the reading has no such window."""
    left = win.get("remaining_percent") if isinstance(win, dict) else None
    if isinstance(left, bool) or not isinstance(left, (int, float)) or not math.isfinite(left):
        return None
    # The states say what the reading says. The pacing signals are thresholds (2% left reads as
    # "exhausted", 99% as "idle"), so they are not taken as fact here.
    if win.get("stale_reset"):
        state = "reset_passed"
    elif win.get("signal") == "idle_full" and left >= 100:
        state = "not_started"
    elif left <= 0:
        state = "exhausted"
    else:
        state = "ok"
    seconds = win.get("resets_in_seconds")
    counting = state in ("ok", "exhausted") and isinstance(seconds, int) and seconds > 0
    countdown = win.get("resets_in") if counting else None
    return {
        "remaining_percent": left,
        "used_percent": win.get("used_percent"),
        "reset_time": win.get("reset_time"),
        "resets_in": countdown,
        "state": state,
        "read_at": win.get("fetched_at"),  # set only when it was read at another time than the reading
    }


def _first_window(windows: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    """The first usable window of a kind: one the view cannot show does not hide one it can."""
    for win in windows:
        if win.get("window_kind") == kind and (shown := _quota_window(win)) is not None:
            return shown
    return None


def _as_of(fetched_at: Any, *, now_ts: float) -> str | None:
    """The time a reading was taken, as local clock time; with its date when that is not today."""
    try:
        taken = datetime.fromisoformat(str(fetched_at)).astimezone()
    except (TypeError, ValueError):
        return None
    today = datetime.fromtimestamp(now_ts).astimezone().date()
    return taken.strftime("%H:%M") if taken.date() == today else taken.strftime("%m-%d %H:%M")


def _oldest(times: list[Any]) -> str | None:
    """The earliest of some ISO times, as it was given. One that cannot be read is passed over."""
    read: list[tuple[datetime, str]] = []
    for raw in times:
        try:
            read.append((datetime.fromisoformat(str(raw)).astimezone(), str(raw)))
        except (TypeError, ValueError):
            continue
    return min(read)[1] if read else None


def _short_reason(text: Any) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= REASON_LIMIT else line[: REASON_LIMIT - 3].rstrip() + "..."


def build_quota_section(readings: dict[str, Any], *, now_ts: float) -> dict[str, Any]:
    """What is left in every pool, from the readings `JobManager.get_quota` returns.

    Every pool is listed in a fixed order. One with no usable reading has status `unknown` and a reason.
    """
    windows_by_pool: dict[str, list[dict[str, Any]]] = {}
    for backend in ("codex", "claude"):
        reading = readings.get(backend)
        if isinstance(reading, dict) and reading.get("status") == "ok":
            windows_by_pool[backend] = [w for w in reading.get("windows") or [] if isinstance(w, dict)]
    neon = readings.get("neon")
    if isinstance(neon, dict) and neon.get("status") == "ok":
        for group in neon.get("groups") or []:
            # Only Antigravity's own pools, and every group of a pool: one group cannot drop another.
            if isinstance(group, dict) and group.get("pool") in _AGY_POOLS:
                windows_by_pool.setdefault(str(group["pool"]), []).extend(
                    w for w in group.get("windows") or [] if isinstance(w, dict)
                )

    try:
        priorities = {p["pool"]: p["priority"] for p in build_quota_advisory(readings)["pools"]}
    except (AttributeError, KeyError, TypeError, ValueError):
        priorities = {}  # the windows are still shown; only the notes that rest on a priority are lost

    pools: list[dict[str, Any]] = []
    notes: list[str] = []
    for pool in QUOTA_POOLS:
        label = POOL_LABELS[pool]
        backend = _POOL_BACKEND[pool]
        reading = readings.get(backend)
        reading = reading if isinstance(reading, dict) else {}
        entry: dict[str, Any] = {"pool": pool, "label": label}

        wins = windows_by_pool.get(pool)
        five = _first_window(wins, "5h") if wins else None
        week = _first_window(wins, "weekly") if wins else None
        if five is None and week is None:
            if wins is not None:
                reason = NO_PLAN_WINDOW  # such as a limit on one model only
            elif reading.get("error"):
                reason = _short_reason(reading["error"])
            elif backend == "claude":
                reason = CLAUDE_NO_READING
            elif reading.get("status") == "ok":
                reason = "the CLI reported no window for this pool"
            else:
                reason = "could not be read"
            pools.append({**entry, "status": "unknown", "reason": reason})
            continue

        priority = priorities.get(pool)
        shown = [w for w in (week, five) if w is not None]
        for w in shown:
            w["read_at"] = w["read_at"] or reading.get("fetched_at")
        # A pool is as fresh as its oldest figure.
        read_at = _oldest([w["read_at"] for w in shown])
        entry.update(
            status="ok",
            as_of=_as_of(read_at, now_ts=now_ts),
            read_at=read_at,
            source="last_claude_job" if backend == "claude" else ("cached" if reading.get("cached") else "live"),
            five_hour=five,
            weekly=week,
            priority=priority,
        )
        pools.append(entry)

        tight = min(shown, key=lambda w: w["remaining_percent"])
        if priority == "stale" or any(w["state"] == "reset_passed" for w in shown):
            notes.append(f"{label}: a reset time in this reading has passed, so it is out of date.")
        elif priority == "use_before_reset" and week is not None and week["resets_in"]:
            notes.append(
                f"{label}: {_percent(week['remaining_percent'])} of the week is unused and resets in "
                f"{week['resets_in']}. Use it first."
            )
        elif priority == "avoid":
            until = f" until it resets in {tight['resets_in']}" if tight["resets_in"] else ""
            if tight["state"] == "exhausted":
                notes.append(f"{label}: out of quota{until}.")
            elif reading.get("rate_limit_reached"):
                notes.append(f"{label}: the CLI reports its limit reached.")
            else:
                notes.append(f"{label}: nearly out of quota, {_percent(tight['remaining_percent'])} left{until}.")
        elif priority == "conserve":
            left = ", ".join(
                f"{name} {_percent(w['remaining_percent'])}"
                for name, w in (("week", week), ("5h", five))
                if w is not None
            )
            notes.append(f"{label}: running low ({left} left).")
        if backend == "claude":
            notes.append(CLAUDE_SHARED_NOTE)

    return {"source": QUOTA_SOURCE, "pools": pools, "notes": notes}


def build_dashboard(
    records: dict[str, Any],
    *,
    window: Window,
    owner_id: str,
    now_ts: float,
    quota: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn the rows from `BridgeStore.dashboard_records` into the dashboard, text view included.

    `quota` is the readings from `JobManager.get_quota`. Without it the view has no quota section.
    """
    total = _bucket()
    by_backend: dict[str, dict[str, Any]] = {name: _bucket() for name in BACKENDS}
    by_pool: dict[str, dict[str, Any]] = {name: _bucket() for name in QUOTA_POOLS}
    by_model: dict[str, dict[str, Any]] = {}
    this_session = _bucket()
    other_sessions = _bucket()

    final_seen: set[str] = set()
    running_recorded: dict[str, int] = {}
    partial: dict[str, int] = {}
    started_no_count: dict[str, str] = {}
    cannot_tell: dict[str, dict[str, Any]] = {}
    never_started: dict[str, None] = {}
    subagents: dict[str, int] = {}
    subagents_unreported: dict[str, None] = {}

    for row in records["usage_rows"]:
        job_id = str(row["job_id"])
        job_on_record = row["job_row_id"] is not None
        usage_status = str(row["usage_status"] or "missing")

        if job_on_record and row["job_status"] in ("queued", "running"):
            # An earlier attempt of a job that has not finished: real tokens, but not a finished run.
            reported = int(row["total_tokens"] or 0) if usage_status in COUNTED_USAGE | {"partial"} else 0
            running_recorded[job_id] = running_recorded.get(job_id, 0) + reported
            continue

        backend = str(row["backend"])
        model_key = f"{backend}:{row['model']}"
        model_bucket = by_model.setdefault(model_key, _bucket())
        model_bucket.setdefault("quota_pool", str(row["quota_pool"]))
        groups = (
            total,
            by_backend.setdefault(backend, _bucket()),
            by_pool.setdefault(str(row["quota_pool"]), _bucket()),
            model_bucket,
            this_session if row["owner_id"] == owner_id else other_sessions,
        )
        is_final_run = bool(row["is_final_attempt"]) and job_id not in final_seen
        if is_final_run:
            final_seen.add(job_id)
        status = str(row["status"])
        counted = usage_status in COUNTED_USAGE
        for group in groups:
            group["duration_seconds"] += float(row["duration_seconds"] or 0.0)
            if is_final_run:
                group["runs"] += 1
                if status in OUTCOMES:
                    group[status] += 1
            if counted:
                for name in TOKEN_FIELDS:
                    group["tokens"][name] += int(row[name] or 0)

        if usage_status == "partial":
            partial[job_id] = partial.get(job_id, 0) + int(row["total_tokens"] or 0)
        elif not counted:
            uncertain_end = row["job_provider_state"] in UNCERTAIN_PROVIDER_STATES
            if job_on_record and row["job_failure_reason"] == "owner_exited" and uncertain_end:
                # Recovered after its window closed: read the same as one not yet recovered (below).
                cannot_tell[job_id] = {"job_id": job_id, "reason": "window_closed"}
            elif row["job_provider_invoked"]:
                started_no_count[job_id] = status
            elif not job_on_record:
                cannot_tell[job_id] = {"job_id": job_id, "reason": "job_row_pruned"}
            elif uncertain_end:
                cannot_tell[job_id] = {"job_id": job_id, "reason": "end_not_confirmed"}
            else:
                # The launch is recorded before the CLI starts, and the bridge saw the job stop.
                never_started[job_id] = None

        if backend == "claude" and (counted or usage_status == "partial"):
            metrics = _metrics(row["provider_metrics_json"])
            spawned = metrics.get("native_subagents")
            if isinstance(spawned, int) and not isinstance(spawned, bool):
                if spawned > 0 and metrics.get("usage_scope") != "whole_job":
                    subagents[job_id] = subagents.get(job_id, 0) + spawned
            else:
                subagents_unreported[job_id] = None

    window_reaches_now = window.until_ts is None or window.until_ts > now_ts
    running: list[dict[str, Any]] = []
    for job in records["running_jobs"]:
        job_id = str(job["job_id"])
        if not (window_reaches_now or job_id in running_recorded):
            continue
        if not job["owner_alive"]:
            # Its row still says queued or running, but the window that owned it is gone. The records
            # cannot say how far it got; it is not shown as running.
            closed: dict[str, Any] = {"job_id": job_id, "reason": "window_closed", "status_on_record": job["status"]}
            if job_id in running_recorded:
                closed["tokens_recorded_for_earlier_attempts"] = running_recorded[job_id]
            cannot_tell[job_id] = closed
            continue
        began = job["started_at"] if job["started_at"] is not None else job["created_at"]
        entry: dict[str, Any] = {
            "job_id": job_id,
            "backend": job["backend"],
            "quota_pool": job["quota_pool"],
            "model": job["model"] or job["requested_model"] or f"{job['backend']}-default",
            "status": job["status"],
            "session": "this" if job["owner_id"] == owner_id else "other",
            "elapsed_seconds": round(max(0.0, now_ts - float(began)), 1),
        }
        if job_id in running_recorded:
            entry["tokens_recorded_for_earlier_attempts"] = running_recorded[job_id]
        running.append(entry)

    first_ts = records["first_record_ts"]
    before_records: dict[str, Any] | None = None
    if not window.only_session:
        if first_ts is None:
            before_records = {
                "records_begin": None,
                "note": "The bridge has no usage records at all yet. No time is covered.",
            }
        elif window.since_ts is None or window.since_ts < first_ts:
            before_records = {
                "records_begin": _iso(first_ts),
                "note": "Nothing is on record before this time. It is not covered, which is not the same as zero.",
            }

    session_started = records["session_started_at"]
    if window.only_session:
        from_ts = session_started
    elif window.since_ts is not None:
        from_ts = window.since_ts
    else:
        from_ts = first_ts
    to_ts = window.until_ts if window.until_ts is not None and window.until_ts <= now_ts else now_ts

    for group in (total, this_session, other_sessions, *by_backend.values(), *by_pool.values(), *by_model.values()):
        group["duration_seconds"] = round(group["duration_seconds"], 2)

    started_by_status: dict[str, int] = {}
    for status in started_no_count.values():
        started_by_status[status] = started_by_status.get(status, 0) + 1

    dashboard: dict[str, Any] = {
        "period": {
            "name": window.name,
            "from": _iso(from_ts),
            "to": _iso(to_ts),
            "until_is_future": window.until_ts is not None and window.until_ts > now_ts,
            "rule": PERIOD_RULE,
        },
        "covers": COVERS,
        "not_covered": list(NOT_COVERED),
        "records_begin": _iso(first_ts),
        "quota": build_quota_section(quota, now_ts=now_ts) if quota is not None else None,
        "totals": total,
        "this_session": {"owner_id": owner_id, "started_at": _iso(session_started), **this_session},
        "other_sessions": other_sessions,
        "by_backend": by_backend,
        "by_quota_pool": by_pool,
        "by_model": dict(
            sorted(by_model.items(), key=lambda kv: (-kv[1]["tokens"]["total_tokens"], kv[0]))
        ),
        "not_in_totals": {
            "running_or_queued": {
                "count": len(running),
                "jobs": running[:JOB_LIST_LIMIT],
                **({"more": len(running) - JOB_LIST_LIMIT} if len(running) > JOB_LIST_LIMIT else {}),
            },
            "partial_counts": {
                **_id_list(list(partial)),
                "reported_total_tokens": sum(partial.values()),
                "why": "The CLI reported a count for a turn that did not finish cleanly; it may be short.",
            },
            "cli_started_no_count": {
                **_id_list(list(started_no_count)),
                "by_status": started_by_status,
                "why": "The CLI was started, so tokens may have been spent, but no count was recorded.",
            },
            "cannot_tell": {
                "count": len(cannot_tell),
                "jobs": list(cannot_tell.values())[:JOB_LIST_LIMIT],
                **({"more": len(cannot_tell) - JOB_LIST_LIMIT} if len(cannot_tell) > JOB_LIST_LIMIT else {}),
                "by_reason": {
                    reason: n
                    for reason in CANNOT_TELL_REASONS
                    if (n := sum(1 for e in cannot_tell.values() if e["reason"] == reason))
                },
                "reasons": dict(CANNOT_TELL_REASONS),
            },
            "claude_subagents": {
                **_id_list(list(subagents)),
                "subagents": sum(subagents.values()),
                "why": "These runs counted the main agent's tokens only. Whole-job counts from new "
                "sessions include subagent usage when Claude Code reports it.",
            },
            "claude_subagents_not_on_record": {
                **_id_list(list(subagents_unreported)),
                "why": "These Claude runs did not record whether they started subagents: written before the "
                "bridge kept that count, or Claude Code did not report it.",
            },
            "before_records_begin": before_records,
        },
        "counted_as_nothing_spent": {
            **_id_list(list(never_started)),
            "why": "Their CLI was never started, so nothing was spent.",
        },
        "left_out": list(LEFT_OUT),
        "token_note": TOKEN_NOTE,
    }
    return {"text": render_text(dashboard), **dashboard}


# --- The text view ---


def _num(n: int) -> str:
    return f"{n:,}"


def _runs(n: int) -> str:
    return f"{_num(n)} run" if n == 1 else f"{_num(n)} runs"


def _duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def _local(iso: str | None) -> str:
    if iso is None:
        return "the first record"
    return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")


def _table(headers: list[str], rows: list[list[str]], *, left_columns: int = 1) -> list[str]:
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return "  " + "  ".join(
            cell.ljust(width) if i < left_columns else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(cells, widths))
        ).rstrip()

    return [line(headers), *(line(r) for r in rows)]


def _ids(listing: dict[str, Any]) -> str:
    shown = ", ".join(listing["job_ids"])
    if listing.get("more"):
        shown += f" and {listing['more']} more"
    return shown


def _percent(value: float) -> str:
    whole = int(value + 0.5)
    return "<1%" if whole == 0 and value > 0 else f"{whole}%"


def _bar(remaining: float) -> str:
    filled = max(0, min(BAR_WIDTH, int(remaining / 100 * BAR_WIDTH + 0.5)))
    return "\u2588" * filled + "\u2591" * (BAR_WIDTH - filled)


def _window_cells(win: dict[str, Any] | None) -> tuple[str, str]:
    """What is left in a window, and when it resets."""
    if win is None:
        return "no reading", ""
    left = f"{_bar(win['remaining_percent'])} {_percent(win['remaining_percent']):>4}"
    if win["state"] == "not_started":
        return left, "not started"
    if win["state"] == "reset_passed":
        return left, "reset passed"
    return left, f"resets {win['resets_in']}" if win["resets_in"] else "reset unknown"


def _quota_lines(quota: dict[str, Any]) -> list[str]:
    pools = quota["pools"]
    cells = {
        p["pool"]: (*_window_cells(p["five_hour"]), *_window_cells(p["weekly"]), p["as_of"] or "")
        for p in pools
        if p["status"] == "ok"
    }
    headers = ("5h window", "", "week", "", "read")
    widths = [max([len(h)] + [len(c[i]) for c in cells.values()]) for i, h in enumerate(headers)]
    label_width = max(len("subscription"), *(len(p["label"]) for p in pools))

    def row(label: str, parts: tuple[str, ...]) -> str:
        five = f"{parts[0]:<{widths[0]}}  {parts[1]:<{widths[1]}}"
        week = f"{parts[2]:<{widths[2]}}  {parts[3]:<{widths[3]}}"
        return f"  {label:<{label_width}}  {five}   {week}   {parts[4]}".rstrip()

    lines = ["QUOTA LEFT  bars show what is left"]
    if cells:
        lines.append(row("subscription", headers))
    for p in pools:
        if p["status"] == "ok":
            lines.append(row(p["label"], cells[p["pool"]]))
        else:
            lines.append(f"  {p['label']:<{label_width}}  no reading: {p['reason']}")
    lines += [f"  * {note}" for note in quota["notes"]]
    return lines


def _used_lines(d: dict[str, Any]) -> list[str]:
    total = d["totals"]
    lines = ["USED THROUGH THE BRIDGE  finished runs; tokens from exact or delta counts only"]
    if total["runs"] == 0 and total["tokens"]["total_tokens"] == 0 and not total["duration_seconds"]:
        lines.append("  no runs in this period")
    else:

        def cells(name: str, b: dict[str, Any]) -> list[str]:
            return [name, _num(b["runs"]), _num(b["tokens"]["total_tokens"]), _duration(b["duration_seconds"])]

        models = d["by_model"]
        rows: list[list[str]] = []
        for pool, bucket in d["by_quota_pool"].items():
            rows.append(cells(POOL_LABELS.get(pool, pool), bucket))
            rows += [
                cells("  " + key.split(":", 1)[-1], b) for key, b in models.items() if b.get("quota_pool") == pool
            ]
        # A model with no pool on it (a dict from an older bridge) is still shown.
        rows += [cells(key, b) for key, b in models.items() if b.get("quota_pool") not in d["by_quota_pool"]]
        rows.append(cells("total", total))
        lines += _table(["", "runs", "tokens", "time"], rows)
        outcomes = ", ".join(
            f"{_num(total[outcome])} {outcome.replace('_', ' ')}" for outcome in OUTCOMES if total[outcome]
        )
        mine, others = d["this_session"], d["other_sessions"]
        lines += [
            f"  outcomes: {outcomes or 'none'}",
            f"  this session: {_runs(mine['runs'])}, {_num(mine['tokens']['total_tokens'])} tokens; "
            f"other sessions: {_runs(others['runs'])}, {_num(others['tokens']['total_tokens'])} tokens",
        ]
    nothing = d["counted_as_nothing_spent"]
    if nothing["count"]:
        lines.append(f"  CLI never started, counted as nothing spent: {_num(nothing['count'])}  {_ids(nothing)}")
    return lines


def _not_in_totals_lines(d: dict[str, Any]) -> list[str]:
    """One row for each caveat that has something under it."""
    nit = d["not_in_totals"]
    rows: list[list[str]] = []
    running = nit["running_or_queued"]
    if running["count"]:
        running_desc = ", ".join(
            f"{j['job_id']} ({j['status']} {_duration(j['elapsed_seconds'])}, {j['session']} session"
            + (
                f", {_num(j['tokens_recorded_for_earlier_attempts'])} tokens recorded so far"
                if "tokens_recorded_for_earlier_attempts" in j
                else ""
            )
            + ")"
            for j in running["jobs"]
        )
        if running.get("more"):
            running_desc += f" and {running['more']} more"
        rows.append(["still queued or running", _num(running["count"]), running_desc])
    part = nit["partial_counts"]
    if part["count"]:
        rows.append(
            [
                "partial count",
                _num(part["count"]),
                f"{_num(part['reported_total_tokens'])} tokens reported, not added: {_ids(part)}",
            ]
        )
    lost = nit["cli_started_no_count"]
    if lost["count"]:
        lost_desc = ", ".join(f"{s.replace('_', ' ')} {n}" for s, n in sorted(lost["by_status"].items()))
        rows.append(["CLI started, no count", _num(lost["count"]), f"{lost_desc}: {_ids(lost)}"])
    unknown = nit["cannot_tell"]
    if unknown["count"]:
        unknown_desc = ", ".join(
            f"{j['job_id']} ({_REASON_LABELS.get(j['reason'], j['reason'])}"
            + (
                f", {_num(j['tokens_recorded_for_earlier_attempts'])} tokens recorded so far"
                if "tokens_recorded_for_earlier_attempts" in j
                else ""
            )
            + ")"
            for j in unknown["jobs"]
        )
        if unknown.get("more"):
            unknown_desc += f" and {unknown['more']} more"
        rows.append(
            ["cannot tell", _num(unknown["count"]), f"the records cannot speak for these: {unknown_desc}"]
        )
    sub = nit["claude_subagents"]
    if sub["count"]:
        rows.append(
            [
                "Claude subagents",
                _num(sub["count"]),
                f"{_num(sub['subagents'])} subagents, tokens not reported: {_ids(sub)}",
            ]
        )
    unrec = nit["claude_subagents_not_on_record"]
    if unrec["count"]:
        rows.append(["subagents not on record", _num(unrec["count"]), f"Claude runs that did not say: {_ids(unrec)}"])
    before = nit["before_records_begin"]
    if before:
        # Not a zero: the stretch before the first record is not covered, so it is always said.
        rows.append(
            [
                "before records begin",
                "",
                f"nothing on record before {_local(before['records_begin'])}"
                if before["records_begin"]
                else before["note"],
            ]
        )
    if not rows:
        return ["NOT IN THE TOTALS: nothing"]
    label_width = max(len(r[0]) for r in rows)
    count_width = max(len("runs"), *(len(r[1]) for r in rows))
    lines = ["NOT IN THE TOTALS"]
    if any(count for _, count, _ in rows):
        lines.append(f"  {'':<{label_width}}  {'runs':>{count_width}}")
    lines += [f"  {label:<{label_width}}  {count:>{count_width}}  {desc}".rstrip() for label, count, desc in rows]
    return lines


def render_text(d: dict[str, Any]) -> str:
    period = d["period"]
    offset = datetime.fromisoformat(period["to"]).strftime("%z") if period["to"] else ""
    zone = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local time"
    lines: list[str] = [
        f"agent-bridge usage, {period['name']}: {_local(period['from'])} to {_local(period['to'])} ({zone})",
    ]
    if period["until_is_future"]:
        lines.append("The period runs past now; it is shown up to now.")

    quota = d.get("quota")
    if quota:
        lines += ["", *_quota_lines(quota)]
    lines += ["", *_used_lines(d), "", *_not_in_totals_lines(d)]
    lines += [
        "",
        f"{d['covers']} Not in it: {', '.join(d['not_covered'])}.",
        "Tokens are each CLI's own count, and their kinds differ by CLI. A run counts in the period it finished.",
    ]
    return "\n".join(lines)
