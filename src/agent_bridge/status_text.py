"""The one status line for a bridge job, built in one place.

`codex · requested gpt-6.2-sol · xhigh · running 2m10s · reading jobs.py`

Pure functions over a job mapping (a `jobs` row as a dict, or any mapping with the same keys). No I/O
and no imports from the rest of the package, so the status command, the server's tool replies and
progress notifications can all use it without paying for anything else.

Every value that reaches the line is sanitised first: a job's activity can carry command text from a
subagent, and a label or model name comes from a caller, so none of them may move the cursor, clear
the screen or start a line of their own.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from agent_bridge.backends.registry import get_adapter_optional

SEP = " · "
ELLIPSIS = "…"
ACTIVE_STATUSES: frozenset[str] = frozenset({"queued", "running"})
WINDOW_CLOSED = "window closed"
ACTIVITY_MAX_CHARS = 60
ACTIVITY_STALE_SECONDS = 30.0

# Escape sequences first (CSI with its parameters, OSC up to BEL or ST, then any other two-byte
# escape), so their printable tails go with them; then every remaining control or invisible
# formatting character: C0, DEL, C1 (0x9b is an 8-bit CSI), zero-width and bidi controls, and
# the Unicode line and paragraph separators.
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|\x1b[@-Z\\-_]"
    r"|\x9b[0-?]*[ -/]*[@-~]"
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿]")
_SPACE_RE = re.compile(r"\s+")


def sanitize(value: Any, max_chars: int | None = None) -> str:
    """One printable line: escapes and control characters removed, whitespace collapsed, truncated."""
    if value is None:
        return ""
    text = str(value)
    text = _ANSI_RE.sub(" ", text)
    text = _CONTROL_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    if max_chars is not None:
        text = truncate(text, max_chars)
    return text


def truncate(text: str, width: int) -> str:
    """Cut `text` to at most `width` characters, ending in an ellipsis when anything was cut."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return ELLIPSIS
    return text[: width - 1].rstrip() + ELLIPSIS


def format_duration(seconds: float | None) -> str:
    """`12s`, `2m10s`, `1h03m`."""
    if seconds is None:
        return ""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def model_label(job: Mapping[str, Any]) -> str:
    """The model as the bridge actually knows it: observed, requested, or the CLI's default."""
    backend = sanitize(job.get("backend")).lower()
    adapter = get_adapter_optional(backend)
    reported = sanitize(job.get("model"))
    model = reported or sanitize(job.get("requested_model"))
    if model == f"{backend}-default":
        model = ""
    source = sanitize(job.get("model_source")).lower()
    if source not in {"observed", "requested", "default"}:
        reports_observed = adapter.capabilities.reports_observed_model if adapter is not None else True
        if not reports_observed:
            source = "requested" if model else "default"
        elif reported:
            source = "observed"
        elif model:
            source = "requested"
        else:
            return ""
    if source == "default":
        return adapter.default_model_label if adapter is not None else "default model"
    if not model:
        return ""
    if source == "requested":
        return f"requested {model}"
    return model


def elapsed_seconds(job: Mapping[str, Any], *, now: float) -> float | None:
    """Time since the job started (or was queued); a finished job's own duration."""
    status = str(job.get("status") or "")
    if status not in ACTIVE_STATUSES and status != WINDOW_CLOSED:
        duration = _num(job.get("duration_seconds"))
        if duration is not None:
            return duration
        started, finished = _num(job.get("started_at")), _num(job.get("finished_at"))
        if started is not None and finished is not None:
            return max(0.0, finished - started)
        return None
    since = _num(job.get("started_at"))
    if since is None:
        since = _num(job.get("created_at"))
    if since is None:
        return None
    return max(0.0, now - since)


def status_part(job: Mapping[str, Any], *, now: float, with_word: bool = True) -> str:
    status = sanitize(job.get("display_status") or job.get("status"))
    elapsed = format_duration(elapsed_seconds(job, now=now))
    if not with_word:
        return elapsed or status
    return " ".join(p for p in (status, elapsed) if p)


def activity_part(job: Mapping[str, Any], *, now: float) -> str:
    activity = sanitize(job.get("last_activity"), ACTIVITY_MAX_CHARS)
    if not activity:
        return ""
    at = _num(job.get("last_activity_at"))
    if at is not None and now - at > ACTIVITY_STALE_SECONDS:
        activity += f" ({format_duration(now - at)} ago)"
    return activity


def line_parts(
    job: Mapping[str, Any], *, now: float, long: bool = False, compact: bool = False
) -> list[str]:
    """The line's parts in order, each already sanitised; unknown parts are left out."""
    parts = [sanitize(job.get("backend")), model_label(job)]
    if compact:
        parts.append(status_part(job, now=now, with_word=False))
        return [p for p in parts if p]
    parts += [sanitize(job.get("effort")), status_part(job, now=now), activity_part(job, now=now)]
    if long:
        parts += [sanitize(job.get("label"), 40), sanitize(job.get("quota_pool"))]
    return [p for p in parts if p]


def format_job_line(
    job: Mapping[str, Any],
    *,
    now: float,
    width: int | None = None,
    long: bool = False,
    compact: bool = False,
) -> str:
    """`codex · requested gpt-6.2-sol · xhigh · running 2m10s · reading jobs.py`.

    `long` adds the label and the quota pool; `compact` keeps only backend, model and elapsed time
    (the form used when several jobs share one line). `width` truncates the whole line.
    """
    line = SEP.join(line_parts(job, now=now, long=long, compact=compact))
    return truncate(line, width) if width is not None else line


def clean_prefix(prefix: str) -> str:
    """A caller's prefix, sanitised, keeping one trailing space if it ended in whitespace."""
    clean = sanitize(prefix)
    return clean + " " if clean and prefix[-1:].isspace() else clean


def format_summary_line(
    jobs: list[Mapping[str, Any]], *, now: float, width: int | None = None, prefix: str = ""
) -> str:
    """One line for a host's status line: nothing, one job's full line, or `N jobs: a | b`."""
    prefix = clean_prefix(prefix)
    if not jobs:
        return ""
    if len(jobs) == 1:
        line = prefix + format_job_line(jobs[0], now=now)
    else:
        body = " | ".join(format_job_line(j, now=now, compact=True) for j in jobs)
        line = f"{prefix}{len(jobs)} jobs: {body}"
    return truncate(line, width) if width is not None else line
