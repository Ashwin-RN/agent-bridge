"""`agent-bridge status`: what the bridge is running now, read from `bridge.db` without writing to it.

It runs every second or two from a host's status line, so it stays small and it never writes:

- the database is opened with SQLite's `mode=ro`, one short-lived connection per snapshot;
- it never imports the bridge's store, job manager or server (they migrate the schema, register an
  owner and recover jobs), nor the MCP SDK;
- it reads `PRAGMA table_info(jobs)` and selects only the columns that exist, so an older or a newer
  database both work;
- a job whose row says queued or running but whose owning window has gone is shown as
  `window closed`; nothing is marked or repaired.

Modes: a table (default), `--line` for a one-line status line, `--lines` for one line per job,
`--watch` to redraw the table, `--json` for one snapshot as data.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_bridge.config import STATE_DB_FILENAME, state_dir_from_env
from agent_bridge.status_text import (
    ACTIVE_STATUSES,
    WINDOW_CLOSED,
    activity_part,
    clean_prefix,
    elapsed_seconds,
    format_job_line,
    format_summary_line,
    model_label,
    sanitize,
    status_part,
    truncate,
)

DEFAULT_RECENT_MINUTES = 10.0
DEFAULT_WIDTH = 120
DEFAULT_FINISHED_LIMIT = 20
LOCK_WAIT_SECONDS = 2.0

# Columns the command reads when they exist. `last_activity_at` and `model_source` arrive in a later
# schema; until then they are simply absent.
_WANTED_COLUMNS: tuple[str, ...] = (
    "job_id",
    "owner_id",
    "backend",
    "quota_pool",
    "cwd",
    "effective_cwd",
    "mode",
    "requested_model",
    "model",
    "model_source",
    "effort",
    "label",
    "parent_run",
    "status",
    "failure_reason",
    "created_at",
    "started_at",
    "finished_at",
    "duration_seconds",
    "last_activity",
    "last_activity_at",
)


class SnapshotError(Exception):
    """The database exists but could not be read read-only."""


# --- Reading ------------------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """The same answer as `process_control.is_pid_alive`, without importing asyncio for it."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _norm_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def read_snapshot(
    db_path: Path,
    *,
    now: float,
    recent_seconds: float,
    include_all: bool = False,
    cwd: str | None = None,
    parent_run: str | None = None,
    finished_limit: int = DEFAULT_FINISHED_LIMIT,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> dict[str, Any] | None:
    """`{"active": [...], "finished": [...]}`, or None when there is no database.

    Raises SnapshotError when the file exists but cannot be read read-only. Never writes.
    """
    if not db_path.is_file():
        return None
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=LOCK_WAIT_SECONDS, isolation_level=None)
    except sqlite3.Error as exc:
        raise SnapshotError(str(exc)) from exc
    try:
        try:
            # A read-only connection never checkpoints; say so anyway, and refuse any write.
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        except (AttributeError, sqlite3.Error):
            pass
        conn.execute("PRAGMA query_only = 1")
        conn.execute("BEGIN")  # one read transaction: jobs and owners from the same moment
        tables = {
            str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "jobs" not in tables:
            conn.execute("COMMIT")
            return {"active": [], "finished": []}
        present = {str(r[1]) for r in conn.execute("PRAGMA table_info(jobs)")}
        cols = [c for c in _WANTED_COLUMNS if c in present]
        if "status" not in present:
            raise SnapshotError("the jobs table has no status column")
        select = [f"j.{c}" for c in cols]
        join = ""
        has_owners = "owners" in tables and "owner_id" in present
        if has_owners:
            owner_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(owners)")}
            has_owners = {"owner_id", "pid"} <= owner_cols
            if has_owners:
                select.append("o.owner_id AS _o_id")
                select.append("o.pid AS _o_pid")
                select.append(
                    "o.exited_at AS _o_exited" if "exited_at" in owner_cols else "NULL AS _o_exited"
                )
                join = "LEFT JOIN owners o ON o.owner_id = j.owner_id"

        clauses: list[str] = []
        params: list[Any] = []
        if not include_all:
            finished_col = "finished_at" if "finished_at" in present else (
                "created_at" if "created_at" in present else None
            )
            if recent_seconds > 0 and finished_col:
                clauses.append(f"(j.status IN ('queued', 'running') OR j.{finished_col} >= ?)")
                params.append(now - recent_seconds)
            else:
                clauses.append("j.status IN ('queued', 'running')")
        if parent_run is not None:
            if "parent_run" not in present:
                conn.execute("COMMIT")
                return {"active": [], "finished": []}
            clauses.append("j.parent_run = ?")
            params.append(parent_run)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "ORDER BY j.created_at ASC, j.rowid ASC" if "created_at" in present else "ORDER BY j.rowid ASC"
        rows = conn.execute(
            f"SELECT {', '.join(select)} FROM jobs j {join} {where} {order}", params
        ).fetchall()
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        raise SnapshotError(str(exc)) from exc
    finally:
        conn.close()

    want_cwd = _norm_path(cwd) if cwd else None
    alive_cache: dict[int, bool] = {}
    active: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    names = [*cols, *(["_o_id", "_o_pid", "_o_exited"] if has_owners else [])]
    for row in rows:
        job = dict(zip(names, row))
        if want_cwd is not None:
            paths = {_norm_path(str(p)) for p in (job.get("cwd"), job.get("effective_cwd")) if p}
            if want_cwd not in paths:
                continue
        owner_id, owner_pid, owner_exited = (
            job.pop("_o_id", None), job.pop("_o_pid", None), job.pop("_o_exited", None)
        )
        status = str(job.get("status") or "")
        job["display_status"] = status
        job["window_closed"] = False
        if status in ACTIVE_STATUSES:
            if has_owners:
                alive = owner_id is not None and owner_exited is None and owner_pid is not None
                if alive:
                    pid = int(owner_pid)
                    if pid not in alive_cache:
                        alive_cache[pid] = pid_alive(pid)
                    alive = alive_cache[pid]
                if not alive:
                    job["display_status"] = WINDOW_CLOSED
                    job["window_closed"] = True
            active.append(job)
        else:
            finished.append(job)
    finished.sort(key=lambda j: float(j.get("finished_at") or j.get("created_at") or 0), reverse=True)
    # The cap is never silent: the total travels with the rows.
    return {"active": active, "finished": finished[: max(0, finished_limit)], "finished_total": len(finished)}


# --- Rendering ----------------------------------------------------------------------------------

_TABLE_COLUMNS: tuple[tuple[str, int | None], ...] = (
    ("JOB", None),
    ("BACKEND", None),
    ("MODEL", 32),
    ("EFFORT", None),
    ("STATUS", None),
    ("ACTIVITY", 44),
    ("LABEL", 24),
    ("POOL", None),
)


def _table_cells(job: Mapping[str, Any], *, now: float) -> list[str]:
    return [
        sanitize(job.get("job_id")),
        sanitize(job.get("backend")),
        model_label(job),
        sanitize(job.get("effort")),
        status_part(job, now=now),
        activity_part(job, now=now),
        sanitize(job.get("label")),
        sanitize(job.get("quota_pool")),
    ]


def _render_rows(jobs: Sequence[Mapping[str, Any]], *, now: float, width: int | None) -> list[str]:
    rows = [[truncate(c, cap) if cap else c for c, (_, cap) in zip(_table_cells(j, now=now), _TABLE_COLUMNS)] for j in jobs]
    header = [name for name, _ in _TABLE_COLUMNS]
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]
    out = []
    for cells in [header, *rows]:
        line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
        out.append(truncate(line, width) if width else line)
    return out


def render_table(
    snapshot: Mapping[str, Any],
    *,
    now: float,
    width: int | None,
    recent_minutes: float,
    include_all: bool,
) -> str:
    lines: list[str] = []
    active = snapshot["active"]
    if active:
        lines.append(f"Running ({len(active)})")
        lines += _render_rows(active, now=now, width=width)
    else:
        lines.append("Nothing running.")
    finished = snapshot["finished"]
    if include_all or recent_minutes > 0:
        title = "Finished" if include_all else f"Finished in the last {_minutes(recent_minutes)}"
        lines.append("")
        if finished:
            total = int(snapshot.get("finished_total", len(finished)))
            count = f"{len(finished)} of {total}, --limit to show more" if total > len(finished) else str(total)
            lines.append(f"{title} ({count})")
            lines += _render_rows(finished, now=now, width=width)
        else:
            lines.append(f"{title}: none.")
    return "\n".join(lines)


def _minutes(value: float) -> str:
    n = int(value) if float(value).is_integer() else value
    return f"{n} minute" if n == 1 else f"{n} minutes"


def _job_json(job: Mapping[str, Any], *, now: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in job.items():
        out[key] = sanitize(value) if isinstance(value, str) else value
    out["model_display"] = model_label(job)
    elapsed = elapsed_seconds(job, now=now)
    out["elapsed_seconds"] = round(elapsed, 1) if elapsed is not None else None
    out["line"] = format_job_line(job, now=now)
    return out


def render_json(
    snapshot: Mapping[str, Any] | None, *, db_path: Path, now: float
) -> str:
    import json

    payload: dict[str, Any] = {
        "database": str(db_path),
        "exists": snapshot is not None,
        "now": round(now, 3),
        "active": [_job_json(j, now=now) for j in (snapshot or {}).get("active", [])],
        "finished": [_job_json(j, now=now) for j in (snapshot or {}).get("finished", [])],
        "finished_total": int((snapshot or {}).get("finished_total", 0)),
    }
    if snapshot is None:
        payload["message"] = _missing_sentence(db_path)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _running(snapshot: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Jobs that are really running: a closed window's job is not, so it never pins a status line."""
    return [j for j in (snapshot or {}).get("active", []) if not j.get("window_closed")]


def _missing_sentence(db_path: Path) -> str:
    return f"No bridge database at {db_path}: no job has run with this state folder yet."


# --- Command ------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-bridge status",
        description="What the bridge is running now, read from bridge.db without writing to it.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--line", action="store_true", help="one line for a host's status line; nothing when idle")
    mode.add_argument("--lines", action="store_true", help="one line per running job")
    mode.add_argument("--json", action="store_true", help="one snapshot as JSON")
    mode.add_argument(
        "--watch", nargs="?", type=float, const=2.0, default=None, metavar="SECONDS",
        help="redraw the table every SECONDS (default 2) until Ctrl+C",
    )
    p.add_argument("--recent", type=float, default=DEFAULT_RECENT_MINUTES, metavar="MINUTES",
                   help="also list jobs finished in the last MINUTES (default 10; 0 hides them)")
    p.add_argument("--all", action="store_true", help="include every job, whatever its status or age")
    p.add_argument("--limit", type=int, default=DEFAULT_FINISHED_LIMIT, metavar="N",
                   help="at most N finished jobs (default 20)")
    p.add_argument("--cwd", metavar="PATH", help="only jobs started in PATH")
    p.add_argument("--parent-run", metavar="NAME", help="only jobs with this parent_run")
    p.add_argument("--width", type=int, metavar="N", help="cut lines to N characters (default COLUMNS, else 120)")
    p.add_argument("--prefix", default="bridge: ", metavar="TEXT", help="text before --line/--lines (default 'bridge: ')")
    p.add_argument("--state-dir", metavar="PATH", help="the bridge's state folder (default AGENT_BRIDGE_STATE_DIR, else ~/.agent-bridge)")
    return p


def _line_width(explicit: int | None) -> int:
    if explicit is not None:
        return max(1, explicit)
    try:
        return max(1, int(os.environ.get("COLUMNS", "")))
    except ValueError:
        return DEFAULT_WIDTH


def _table_width(explicit: int | None) -> int | None:
    if explicit is not None:
        return max(1, explicit)
    if os.environ.get("COLUMNS", "").strip().isdigit():
        return max(1, int(os.environ["COLUMNS"]))
    if sys.stdout.isatty():
        import shutil

        return shutil.get_terminal_size().columns
    return None


def _utf8_stdout() -> None:
    """A status-line host reads stdout as UTF-8; Windows would otherwise write the ANSI code page."""
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        else:
            sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass


def _enable_vt() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def _snapshot(args: argparse.Namespace, db_path: Path, now: float) -> dict[str, Any] | None:
    return read_snapshot(
        db_path,
        now=now,
        recent_seconds=max(0.0, args.recent) * 60.0,
        include_all=args.all,
        cwd=args.cwd,
        parent_run=args.parent_run,
        finished_limit=args.limit,
    )


def _error_sentence(db_path: Path, exc: Exception) -> str:
    return f"agent-bridge status: cannot read {db_path} read-only ({exc}); nothing was changed."


def _once(args: argparse.Namespace, db_path: Path) -> int:
    now = time.time()
    try:
        snap = _snapshot(args, db_path, now)
    except SnapshotError as exc:
        print(_error_sentence(db_path, exc), file=sys.stderr)
        return 1
    if args.line:
        line = format_summary_line(_running(snap), now=now, width=_line_width(args.width), prefix=args.prefix)
        if line:
            print(line)
        return 0
    if args.lines:
        width = _line_width(args.width)
        prefix = clean_prefix(args.prefix)
        for job in _running(snap):
            print(truncate(prefix + format_job_line(job, now=now), width))
        return 0
    if args.json:
        print(render_json(snap, db_path=db_path, now=now))
        return 0
    if snap is None:
        print(_missing_sentence(db_path))
        return 0
    print(render_table(snap, now=now, width=_table_width(args.width), recent_minutes=args.recent, include_all=args.all))
    return 0


def _watch(args: argparse.Namespace, db_path: Path) -> int:
    interval = max(0.2, float(args.watch))
    tty = sys.stdout.isatty()
    if tty:
        _enable_vt()
    try:
        while True:
            now = time.time()
            try:
                snap = _snapshot(args, db_path, now)
                body = (
                    _missing_sentence(db_path)
                    if snap is None
                    else render_table(snap, now=now, width=_table_width(args.width),
                                      recent_minutes=args.recent, include_all=args.all)
                )
            except SnapshotError as exc:
                body = _error_sentence(db_path, exc)
            stamp = time.strftime("%H:%M:%S", time.localtime(now))
            header = f"agent-bridge status · {stamp} · every {interval:g}s · Ctrl+C to stop"
            if tty:
                sys.stdout.write("\x1b[H\x1b[2J" + header + "\n\n" + body + "\n")
            else:
                sys.stdout.write(header + "\n" + body + "\n\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def run(argv: list[str]) -> int:
    _utf8_stdout()
    args = _parser().parse_args(argv)
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else state_dir_from_env()
    db_path = state_dir / STATE_DB_FILENAME
    try:
        if args.watch is not None:
            return _watch(args, db_path)
        return _once(args, db_path)
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - silence the flush at exit
        except OSError:
            pass
        return 0
