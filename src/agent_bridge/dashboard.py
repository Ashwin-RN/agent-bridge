"""The usage dashboard: what the bridge has run, read from its own records only.

A total holds only what the records can vouch for: finished runs whose CLI gave an exact count, or a
per-turn (delta) count. Everything else is listed apart by job id and never added in:

- jobs still queued or running, with any tokens already recorded for their earlier attempts;
- partial counts;
- runs whose CLI was started but left no count (cancelled, timed out, failed, window closed, or a CLI
  that reports none, such as Codex's built-in review);
- runs whose job row has been pruned, so that it cannot be told whether their CLI was started;
- Claude runs that started subagents of their own, whose tokens Claude Code does not report, and Claude
  runs that did not say whether they started any;
- any part of the period from before the bridge's first record.

A run whose CLI was never started is counted, as a run that spent nothing. Live quota readings and Claude
Code's cost estimate are left out: neither is a record the bridge can add up.

The text view is rendered from the same dict that is returned, so the two cannot disagree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

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
OUTCOMES: tuple[str, ...] = ("completed", "failed", "timed_out", "cancelled")
JOB_LIST_LIMIT = 10

PERIOD_RULE = "A run counts in the period its record was written, which is when it finished."
LEFT_OUT: tuple[str, ...] = (
    "Live quota readings: they come from each CLI, not from the bridge's records. get_usage with "
    "include_quota shows them.",
    "Claude Code's cost estimate: it is not a bill, and on a resumed session it repeats the earlier turns.",
)
TOKEN_NOTE = (
    "Token figures are each CLI's own counts, and their kinds are not alike: for claude, input_tokens "
    "includes cache reads and writes; for codex, cached_tokens is part of input_tokens; for agy, "
    "cached_tokens can exceed total_tokens."
)


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


def build_dashboard(
    records: dict[str, Any],
    *,
    window: Window,
    owner_id: str,
    now_ts: float,
) -> dict[str, Any]:
    """Turn the rows from `BridgeStore.dashboard_records` into the dashboard, text view included."""
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
    cannot_tell: dict[str, None] = {}
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
        groups = (
            total,
            by_backend.setdefault(backend, _bucket()),
            by_pool.setdefault(str(row["quota_pool"]), _bucket()),
            by_model.setdefault(model_key, _bucket()),
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
            if not job_on_record:
                cannot_tell[job_id] = None
            elif not row["job_provider_invoked"]:
                never_started[job_id] = None
            else:
                started_no_count[job_id] = status

        if backend == "claude" and (counted or usage_status == "partial"):
            spawned = _metrics(row["provider_metrics_json"]).get("native_subagents")
            if isinstance(spawned, int) and not isinstance(spawned, bool):
                if spawned > 0:
                    subagents[job_id] = subagents.get(job_id, 0) + spawned
            else:
                subagents_unreported[job_id] = None

    window_reaches_now = window.until_ts is None or window.until_ts > now_ts
    running: list[dict[str, Any]] = []
    for job in records["running_jobs"]:
        job_id = str(job["job_id"])
        if not (window_reaches_now or job_id in running_recorded):
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
        "records_begin": _iso(first_ts),
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
            "cannot_tell_if_cli_started": {
                **_id_list(list(cannot_tell)),
                "why": "No count, and the job's row has been pruned (the newest jobs are kept), so it is not "
                "known whether its CLI was started.",
            },
            "claude_subagents": {
                **_id_list(list(subagents)),
                "subagents": sum(subagents.values()),
                "why": "Claude Code reports the main agent's tokens only. Its subagents' tokens are spent "
                "but not counted.",
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


def render_text(d: dict[str, Any]) -> str:
    period = d["period"]
    total = d["totals"]
    offset = datetime.fromisoformat(period["to"]).strftime("%z") if period["to"] else ""
    zone = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local time"
    lines: list[str] = [
        f"agent-bridge usage, {period['name']}: {_local(period['from'])} to {_local(period['to'])} ({zone})",
        "From the bridge's own records. " + PERIOD_RULE,
    ]
    if period["until_is_future"]:
        lines.append("The period runs past now; it is shown up to now.")
    if d["records_begin"] is None:
        lines.append("There are no usage records yet.")
    else:
        lines.append(f"Records begin {_local(d['records_begin'])}.")

    t = total["tokens"]
    lines += [
        "",
        "TOTAL  finished runs; tokens from exact or delta counts only",
        f"  runs {_num(total['runs'])}: completed {_num(total['completed'])}, failed {_num(total['failed'])}, "
        f"timed out {_num(total['timed_out'])}, cancelled {_num(total['cancelled'])}",
        f"  tokens {_num(t['total_tokens'])}  (input {_num(t['input_tokens'])}, output {_num(t['output_tokens'])})"
        f"  time {_duration(total['duration_seconds'])}",
        "",
        "SESSIONS",
    ]
    mine = d["this_session"]
    others = d["other_sessions"]
    started = f", started {_local(mine['started_at'])}" if mine["started_at"] else ""
    lines += _table(
        ["", "runs", "tokens"],
        [
            [f"this session ({mine['owner_id']}{started})", _num(mine["runs"]), _num(mine["tokens"]["total_tokens"])],
            ["other sessions", _num(others["runs"]), _num(others["tokens"]["total_tokens"])],
            ["total", _num(total["runs"]), _num(t["total_tokens"])],
        ],
    )

    lines += ["", "BY BACKEND"]
    lines += _table(
        ["backend", "runs", "completed", "failed", "timed out", "cancelled", "tokens", "time"],
        [
            [
                name,
                _num(b["runs"]),
                _num(b["completed"]),
                _num(b["failed"]),
                _num(b["timed_out"]),
                _num(b["cancelled"]),
                _num(b["tokens"]["total_tokens"]),
                _duration(b["duration_seconds"]),
            ]
            for name, b in d["by_backend"].items()
        ],
    )
    lines += ["", "BY QUOTA POOL"]
    lines += _table(
        ["pool", "runs", "tokens", "time"],
        [
            [name, _num(b["runs"]), _num(b["tokens"]["total_tokens"]), _duration(b["duration_seconds"])]
            for name, b in d["by_quota_pool"].items()
        ],
    )
    lines += ["", "BY MODEL"]
    if d["by_model"]:
        lines += _table(
            ["backend:model", "runs", "tokens"],
            [[name, _num(b["runs"]), _num(b["tokens"]["total_tokens"])] for name, b in d["by_model"].items()],
        )
    else:
        lines.append("  no runs in this period")

    nit = d["not_in_totals"]
    rows: list[list[str]] = []
    running = nit["running_or_queued"]
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
    rows.append(["still queued or running", _num(running["count"]), running_desc or "none"])
    part = nit["partial_counts"]
    rows.append(
        [
            "partial count",
            _num(part["count"]),
            f"{_num(part['reported_total_tokens'])} tokens reported, not added: {_ids(part)}" if part["count"] else "none",
        ]
    )
    lost = nit["cli_started_no_count"]
    lost_desc = ", ".join(f"{s.replace('_', ' ')} {n}" for s, n in sorted(lost["by_status"].items()))
    rows.append(
        ["CLI started, no count", _num(lost["count"]), f"{lost_desc}: {_ids(lost)}" if lost["count"] else "none"]
    )
    unknown = nit["cannot_tell_if_cli_started"]
    rows.append(
        [
            "cannot tell",
            _num(unknown["count"]),
            f"job row pruned, CLI may have run: {_ids(unknown)}" if unknown["count"] else "none",
        ]
    )
    sub = nit["claude_subagents"]
    rows.append(
        [
            "Claude subagents",
            _num(sub["count"]),
            f"{_num(sub['subagents'])} subagents, tokens not reported: {_ids(sub)}" if sub["count"] else "none",
        ]
    )
    unrec = nit["claude_subagents_not_on_record"]
    rows.append(
        [
            "subagents not on record",
            _num(unrec["count"]),
            f"Claude runs that did not say: {_ids(unrec)}" if unrec["count"] else "none",
        ]
    )
    before = nit["before_records_begin"]
    rows.append(
        [
            "before records begin",
            "",
            (f"nothing on record before {_local(before['records_begin'])}" if before["records_begin"] else before["note"])
            if before
            else "the period starts after the first record",
        ]
    )
    label_width = max(len(r[0]) for r in rows)
    count_width = max(len("runs"), *(len(r[1]) for r in rows))
    lines += ["", "NOT IN THE TOTALS", f"  {'':<{label_width}}  {'runs':>{count_width}}"]
    lines += [f"  {label:<{label_width}}  {count:>{count_width}}  {desc}".rstrip() for label, count, desc in rows]

    nothing = d["counted_as_nothing_spent"]
    lines += [
        "",
        "COUNTED AS NOTHING SPENT",
        f"  CLI never started: {_num(nothing['count'])}" + (f"  {_ids(nothing)}" if nothing["count"] else ""),
        "",
        "LEFT OUT: live quota readings and Claude Code's cost estimate (not bridge records).",
        "Token kinds differ by CLI: claude input includes cache reads and writes; codex cached is part of",
        "input; agy cached can exceed its total.",
    ]
    return "\n".join(lines)
