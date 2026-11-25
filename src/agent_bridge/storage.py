"""Shared SQLite persistence for multi-window jobs, atomic usage ledger, watermarks, and quota cache."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from agent_bridge.models import TERMINAL_STATUSES, JobRecord

logger = logging.getLogger("agent_bridge.storage")

UsagePeriod = Literal["session", "today", "7d", "30d", "all"]

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS owners (
    owner_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    started_at REAL NOT NULL,
    last_heartbeat REAL NOT NULL,
    exited_at REAL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    cwd TEXT NOT NULL,
    effective_cwd TEXT,
    mode TEXT NOT NULL,
    requested_model TEXT,
    model TEXT,
    effort TEXT,
    model_notice TEXT,
    prompt_preview TEXT NOT NULL,
    label TEXT,
    parent_run TEXT,
    worktree INTEGER NOT NULL DEFAULT 0,
    worktree_path TEXT,
    worktree_branch TEXT,
    worktree_commit TEXT,
    requested_session_id TEXT,
    session_id TEXT,
    status TEXT NOT NULL,
    failure_reason TEXT,
    provider_state TEXT,
    retry_guidance TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    duration_seconds REAL,
    response TEXT NOT NULL DEFAULT '',
    usage_json TEXT,
    provider_metrics_json TEXT,
    usage_status TEXT NOT NULL DEFAULT 'missing',
    exit_code INTEGER,
    error TEXT,
    stderr_tail TEXT,
    last_activity TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(backend, session_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner_id);
CREATE INDEX IF NOT EXISTS idx_jobs_cwd ON jobs(cwd);
CREATE INDEX IF NOT EXISTS idx_jobs_parent_run ON jobs(parent_run);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,
    owner_id TEXT NOT NULL,
    ts REAL NOT NULL,
    iso_time TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT,
    effort TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    cwd TEXT NOT NULL,
    parent_run TEXT,
    label TEXT,
    session_id TEXT,
    usage_status TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_tokens INTEGER,
    provider_metrics_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_owner ON usage_events(owner_id);
CREATE INDEX IF NOT EXISTS idx_usage_backend ON usage_events(backend);
CREATE INDEX IF NOT EXISTS idx_usage_cwd ON usage_events(cwd);
CREATE INDEX IF NOT EXISTS idx_usage_parent_run ON usage_events(parent_run);

CREATE TABLE IF NOT EXISTS session_usage_watermarks (
    backend TEXT NOT NULL,
    session_id TEXT NOT NULL,
    cumulative_json TEXT NOT NULL,
    num_turns INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (backend, session_id)
);

CREATE TABLE IF NOT EXISTS quota_cache (
    backend TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);
"""


def _period_cutoff_ts(period: UsagePeriod) -> float | None:
    now = time.time()
    if period in {"all", "session"}:
        return None
    if period == "today":
        local_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return local_midnight.timestamp()
    if period == "7d":
        return now - (7 * 86_400)
    if period == "30d":
        return now - (30 * 86_400)
    raise ValueError(f"Unknown period {period!r}. Expected: session, today, 7d, 30d, all.")


def _loads_opt(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _row_to_job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        owner_id=row["owner_id"],
        backend=row["backend"],
        cwd=row["cwd"],
        effective_cwd=row["effective_cwd"],
        mode=row["mode"],
        requested_model=row["requested_model"],
        model=row["model"],
        effort=row["effort"],
        model_notice=row["model_notice"],
        prompt_preview=row["prompt_preview"],
        label=row["label"],
        parent_run=row["parent_run"],
        worktree=bool(row["worktree"]),
        worktree_path=row["worktree_path"],
        worktree_branch=row["worktree_branch"],
        worktree_commit=row["worktree_commit"],
        requested_session_id=row["requested_session_id"],
        session_id=row["session_id"],
        status=row["status"],
        failure_reason=row["failure_reason"],
        provider_state=row["provider_state"],
        retry_guidance=row["retry_guidance"],
        created_at=float(row["created_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_seconds=row["duration_seconds"],
        response=row["response"] or "",
        usage=_loads_opt(row["usage_json"]),
        provider_metrics=_loads_opt(row["provider_metrics_json"]),
        usage_status=row["usage_status"] or "missing",
        exit_code=row["exit_code"],
        error=row["error"],
        stderr_tail=row["stderr_tail"],
        last_activity=row["last_activity"],
    )


def _insert_usage_event_cur(cur: sqlite3.Cursor, record: JobRecord) -> None:
    usage = record.usage if isinstance(record.usage, dict) else {}
    now_ts = record.finished_at or time.time()
    iso_time = datetime.fromtimestamp(now_ts).astimezone().isoformat(timespec="seconds")

    def _opt_int(key: str) -> int | None:
        v = usage.get(key)
        return int(v) if isinstance(v, (int, float)) else None

    cur.execute(
        """
        INSERT OR REPLACE INTO usage_events (
            job_id, owner_id, ts, iso_time, backend, model, effort, mode, status,
            duration_seconds, cwd, parent_run, label, session_id, usage_status,
            input_tokens, output_tokens, cached_tokens, cache_write_tokens,
            reasoning_tokens, total_tokens, provider_metrics_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.job_id,
            record.owner_id,
            round(now_ts, 3),
            iso_time,
            record.backend,
            record.model or record.requested_model or f"{record.backend}-default",
            record.effort,
            record.mode,
            record.status,
            round(float(record.duration_seconds or 0.0), 2),
            record.cwd,
            record.parent_run,
            record.label,
            record.session_id,
            record.usage_status,
            _opt_int("input_tokens"),
            _opt_int("output_tokens"),
            _opt_int("cached_tokens"),
            _opt_int("cache_write_tokens"),
            _opt_int("reasoning_tokens"),
            _opt_int("total_tokens"),
            json.dumps(record.provider_metrics, separators=(",", ":"))
            if record.provider_metrics
            else None,
        ),
    )


class BridgeStore:
    """Concurrency-safe SQLite store shared across multiple bridge processes."""

    def __init__(self, state_dir: Path, *, max_stored_jobs: int = 500) -> None:
        self.state_dir = state_dir
        self.db_path = state_dir / "bridge.db"
        self.max_stored_jobs = max(20, max_stored_jobs)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA_SQL)
        finally:
            conn.close()

    @contextmanager
    def _tx(self, *, write: bool = True) -> Iterator[sqlite3.Cursor]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            if write:
                cur.execute("BEGIN IMMEDIATE")
            yield cur
            if write:
                conn.execute("COMMIT")
        except Exception:
            if write:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
            raise
        finally:
            conn.close()

    # --- Owner tracking & crash recovery ---

    def register_owner(self, owner_id: str, pid: int) -> None:
        now = time.time()
        with self._tx(write=True) as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO owners (owner_id, pid, started_at, last_heartbeat, exited_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (owner_id, int(pid), now, now),
            )

    def heartbeat_owner(self, owner_id: str) -> None:
        with self._tx(write=True) as cur:
            cur.execute(
                "UPDATE owners SET last_heartbeat = ? WHERE owner_id = ?",
                (time.time(), owner_id),
            )

    def mark_owner_exited(self, owner_id: str) -> None:
        now = time.time()
        with self._tx(write=True) as cur:
            cur.execute(
                "UPDATE owners SET exited_at = ?, last_heartbeat = ? WHERE owner_id = ?",
                (now, now, owner_id),
            )

    def is_owner_alive(
        self,
        owner_id: str,
        *,
        is_pid_alive_fn: Callable[[int], bool],
        stale_seconds: float = 30.0,
    ) -> bool:
        with self._tx(write=False) as cur:
            row = cur.execute(
                "SELECT pid, last_heartbeat, exited_at FROM owners WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
            if row is None:
                return False
            if row["exited_at"] is not None:
                return False
            if is_pid_alive_fn(int(row["pid"])):
                return True
            return (time.time() - float(row["last_heartbeat"])) < stale_seconds

    def recover_dead_owner_jobs(
        self,
        current_owner_id: str,
        *,
        is_pid_alive_fn: Callable[[int], bool],
        stale_seconds: float = 30.0,
    ) -> list[str]:
        """Recover only queued/running jobs whose owning bridge process has actually exited."""
        recovered: list[str] = []
        now = time.time()
        with self._tx(write=True) as cur:
            rows = cur.execute(
                """
                SELECT j.*, o.pid AS owner_pid, o.exited_at AS owner_exited_at,
                       o.last_heartbeat AS owner_heartbeat
                FROM jobs j
                LEFT JOIN owners o ON j.owner_id = o.owner_id
                WHERE j.status IN ('queued', 'running') AND j.owner_id != ?
                """,
                (current_owner_id,),
            ).fetchall()

            for row in rows:
                owner_exited = row["owner_exited_at"] is not None
                owner_pid = row["owner_pid"]
                owner_hb = row["owner_heartbeat"]

                if owner_exited:
                    dead = True
                elif owner_pid is None:
                    dead = True
                elif is_pid_alive_fn(int(owner_pid)):
                    dead = False
                else:
                    # PID is dead; also require heartbeat older than 1s or no active PID
                    dead = (owner_hb is None) or (now - float(owner_hb) >= 0.0)

                if not dead:
                    continue

                rec = _row_to_job(row)
                rec.status = "failed"
                rec.failure_reason = "owner_exited"
                rec.provider_state = "unknown"
                rec.error = (
                    f"Owning bridge process ({rec.owner_id}) exited before job completed."
                )
                rec.retry_guidance = (
                    "Verify whether any provider work or file edits landed before retrying; "
                    "for edit tasks inspect git status/diff first."
                )
                rec.finished_at = now
                if rec.started_at is not None:
                    rec.duration_seconds = round(max(0.0, now - rec.started_at), 2)
                else:
                    rec.duration_seconds = 0.0
                rec.last_activity = "owner_exited"
                rec.usage_status = "missing"

                cur.execute(
                    """
                    UPDATE jobs SET
                        status = ?, failure_reason = ?, provider_state = ?, retry_guidance = ?,
                        error = ?, finished_at = ?, duration_seconds = ?, last_activity = ?,
                        usage_status = ?
                    WHERE job_id = ? AND status IN ('queued', 'running')
                    """,
                    (
                        rec.status,
                        rec.failure_reason,
                        rec.provider_state,
                        rec.retry_guidance,
                        rec.error,
                        rec.finished_at,
                        rec.duration_seconds,
                        rec.last_activity,
                        rec.usage_status,
                        rec.job_id,
                    ),
                )
                if cur.rowcount > 0:
                    _insert_usage_event_cur(cur, rec)
                    if owner_pid is not None and not owner_exited:
                        cur.execute(
                            "UPDATE owners SET exited_at = ? WHERE owner_id = ? AND exited_at IS NULL",
                            (now, rec.owner_id),
                        )
                    recovered.append(rec.job_id)

        return recovered

    # --- Per-job CRUD ---

    def insert_job(self, record: JobRecord) -> None:
        with self._tx(write=True) as cur:
            cur.execute(
                """
                INSERT INTO jobs (
                    job_id, owner_id, backend, cwd, effective_cwd, mode,
                    requested_model, model, effort, model_notice, prompt_preview,
                    label, parent_run, worktree, worktree_path, worktree_branch,
                    worktree_commit, requested_session_id, session_id, status,
                    failure_reason, provider_state, retry_guidance, created_at,
                    started_at, finished_at, duration_seconds, response, usage_json,
                    provider_metrics_json, usage_status, exit_code, error, stderr_tail,
                    last_activity
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    record.job_id,
                    record.owner_id,
                    record.backend,
                    record.cwd,
                    record.effective_cwd or record.cwd,
                    record.mode,
                    record.requested_model,
                    record.model,
                    record.effort,
                    record.model_notice,
                    record.prompt_preview,
                    record.label,
                    record.parent_run,
                    1 if record.worktree else 0,
                    record.worktree_path,
                    record.worktree_branch,
                    record.worktree_commit,
                    record.requested_session_id,
                    record.session_id,
                    record.status,
                    record.failure_reason,
                    record.provider_state,
                    record.retry_guidance,
                    record.created_at,
                    record.started_at,
                    record.finished_at,
                    record.duration_seconds,
                    record.response[:200_000],
                    json.dumps(record.usage, separators=(",", ":")) if record.usage else None,
                    json.dumps(record.provider_metrics, separators=(",", ":"))
                    if record.provider_metrics
                    else None,
                    record.usage_status,
                    record.exit_code,
                    record.error,
                    record.stderr_tail,
                    record.last_activity,
                ),
            )
            cur.execute(
                "UPDATE owners SET last_heartbeat = ? WHERE owner_id = ?",
                (time.time(), record.owner_id),
            )

    def update_job_progress(
        self,
        job_id: str,
        *,
        owner_id: str,
        status: str | None = None,
        started_at: float | None = None,
        session_id: str | None = None,
        model: str | None = None,
        last_activity: str | None = None,
    ) -> None:
        sets: list[str] = []
        vals: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            vals.append(status)
        if started_at is not None:
            sets.append("started_at = ?")
            vals.append(started_at)
        if session_id is not None:
            sets.append("session_id = ?")
            vals.append(session_id)
        if model is not None:
            sets.append("model = ?")
            vals.append(model)
        if last_activity is not None:
            sets.append("last_activity = ?")
            vals.append(last_activity)
        if not sets:
            return
        vals.append(job_id)
        with self._tx(write=True) as cur:
            cur.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ?", vals)
            cur.execute(
                "UPDATE owners SET last_heartbeat = ? WHERE owner_id = ?",
                (time.time(), owner_id),
            )

    def finalize_job(
        self,
        record: JobRecord,
        *,
        watermark_cumulative: dict[str, Any] | None = None,
        watermark_turns: int | None = None,
    ) -> None:
        """Atomically persist terminal job status, usage event, and optional session watermark."""
        with self._tx(write=True) as cur:
            cur.execute(
                """
                UPDATE jobs SET
                    effective_cwd = ?, model = ?, effort = ?, model_notice = ?,
                    worktree_path = ?, worktree_branch = ?, worktree_commit = ?,
                    session_id = ?, status = ?, failure_reason = ?, provider_state = ?,
                    retry_guidance = ?, started_at = ?, finished_at = ?, duration_seconds = ?,
                    response = ?, usage_json = ?, provider_metrics_json = ?, usage_status = ?,
                    exit_code = ?, error = ?, stderr_tail = ?, last_activity = ?
                WHERE job_id = ?
                """,
                (
                    record.effective_cwd or record.cwd,
                    record.model,
                    record.effort,
                    record.model_notice,
                    record.worktree_path,
                    record.worktree_branch,
                    record.worktree_commit,
                    record.session_id,
                    record.status,
                    record.failure_reason,
                    record.provider_state,
                    record.retry_guidance,
                    record.started_at,
                    record.finished_at,
                    record.duration_seconds,
                    record.response[:200_000],
                    json.dumps(record.usage, separators=(",", ":")) if record.usage else None,
                    json.dumps(record.provider_metrics, separators=(",", ":"))
                    if record.provider_metrics
                    else None,
                    record.usage_status,
                    record.exit_code,
                    record.error,
                    record.stderr_tail,
                    record.last_activity,
                    record.job_id,
                ),
            )
            _insert_usage_event_cur(cur, record)

            if (
                watermark_cumulative is not None
                and record.session_id
                and record.backend == "neon"
            ):
                cur.execute(
                    """
                    INSERT OR REPLACE INTO session_usage_watermarks (
                        backend, session_id, cumulative_json, num_turns, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.backend,
                        record.session_id,
                        json.dumps(watermark_cumulative, separators=(",", ":")),
                        int(watermark_turns or 0),
                        time.time(),
                    ),
                )

            cur.execute(
                "UPDATE owners SET last_heartbeat = ? WHERE owner_id = ?",
                (time.time(), record.owner_id),
            )

            # Prune only oldest TERMINAL jobs beyond max_stored_jobs (never prune active jobs).
            cur.execute(
                """
                DELETE FROM jobs
                WHERE status IN ('completed', 'failed', 'cancelled', 'timed_out')
                  AND job_id NOT IN (
                      SELECT job_id FROM jobs
                      ORDER BY MAX(created_at, COALESCE(finished_at, 0)) DESC
                      LIMIT ?
                  )
                """,
                (self.max_stored_jobs,),
            )

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._tx(write=False) as cur:
            row = cur.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return _row_to_job(row) if row is not None else None

    def list_jobs(
        self,
        *,
        limit: int = 20,
        status: str | None = None,
        backend: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
    ) -> list[JobRecord]:
        clauses: list[str] = []
        vals: list[Any] = []
        if status:
            clauses.append("status = ?")
            vals.append(status)
        if backend:
            clauses.append("backend = ?")
            vals.append(backend)
        if cwd:
            clauses.append("cwd = ?")
            vals.append(cwd)
        if parent_run:
            clauses.append("parent_run = ?")
            vals.append(parent_run)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        vals.append(max(1, min(int(limit), 100)))
        with self._tx(write=False) as cur:
            rows = cur.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?",
                vals,
            ).fetchall()
            return [_row_to_job(r) for r in rows]

    def find_active_session_job(
        self,
        backend: str,
        session_id: str,
        *,
        exclude_job_id: str | None = None,
    ) -> JobRecord | None:
        with self._tx(write=False) as cur:
            if exclude_job_id:
                row = cur.execute(
                    """
                    SELECT * FROM jobs
                    WHERE backend = ? AND session_id = ?
                      AND status IN ('queued', 'running')
                      AND job_id != ?
                    ORDER BY created_at ASC LIMIT 1
                    """,
                    (backend, session_id, exclude_job_id),
                ).fetchone()
            else:
                row = cur.execute(
                    """
                    SELECT * FROM jobs
                    WHERE backend = ? AND session_id = ?
                      AND status IN ('queued', 'running')
                    ORDER BY created_at ASC LIMIT 1
                    """,
                    (backend, session_id),
                ).fetchone()
            return _row_to_job(row) if row is not None else None

    def count_in_flight(
        self,
        *,
        backend: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
    ) -> dict[str, int]:
        clauses = ["status IN ('queued', 'running')"]
        vals: list[Any] = []
        if backend:
            clauses.append("backend = ?")
            vals.append(backend)
        if cwd:
            clauses.append("cwd = ?")
            vals.append(cwd)
        if parent_run:
            clauses.append("parent_run = ?")
            vals.append(parent_run)
        where = f"WHERE {' AND '.join(clauses)}"
        with self._tx(write=False) as cur:
            rows = cur.execute(
                f"SELECT backend, COUNT(*) AS cnt FROM jobs {where} GROUP BY backend",
                vals,
            ).fetchall()
            return {str(r["backend"]): int(r["cnt"]) for r in rows}

    def get_session_watermark(self, backend: str, session_id: str) -> dict[str, Any] | None:
        with self._tx(write=False) as cur:
            row = cur.execute(
                """
                SELECT cumulative_json, num_turns, updated_at
                FROM session_usage_watermarks
                WHERE backend = ? AND session_id = ?
                """,
                (backend, session_id),
            ).fetchone()
            if row is None:
                return None
            return {
                "cumulative": _loads_opt(row["cumulative_json"]) or {},
                "num_turns": int(row["num_turns"]),
                "updated_at": float(row["updated_at"]),
            }

    # --- Usage aggregation ---

    def session_compact_summary(self, owner_id: str) -> dict[str, Any]:
        with self._tx(write=False) as cur:
            rows = cur.execute(
                """
                SELECT backend,
                       COUNT(*) AS runs,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                       ROUND(SUM(duration_seconds), 2) AS duration_seconds,
                       SUM(COALESCE(total_tokens, 0)) AS total_tokens,
                       SUM(CASE WHEN usage_status IN ('partial', 'missing') THEN 1 ELSE 0 END) AS incomplete_usage_runs
                FROM usage_events
                WHERE owner_id = ?
                GROUP BY backend
                """,
                (owner_id,),
            ).fetchall()
        out: dict[str, Any] = {}
        for r in rows:
            entry: dict[str, Any] = {
                "runs": int(r["runs"]),
                "completed": int(r["completed"] or 0),
                "duration_seconds": round(float(r["duration_seconds"] or 0.0), 2),
                "total_tokens": int(r["total_tokens"] or 0),
            }
            if int(r["incomplete_usage_runs"] or 0) > 0:
                entry["incomplete_usage_runs"] = int(r["incomplete_usage_runs"])
            out[str(r["backend"])] = entry
        return out

    def summarize_usage(
        self,
        *,
        period: UsagePeriod = "today",
        owner_id: str,
        backend: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
        recent_limit: int = 10,
        compact: bool = False,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        vals: list[Any] = []
        if period == "session":
            clauses.append("owner_id = ?")
            vals.append(owner_id)
        else:
            cutoff = _period_cutoff_ts(period)
            if cutoff is not None:
                clauses.append("ts >= ?")
                vals.append(cutoff)
        if backend:
            clauses.append("backend = ?")
            vals.append(backend)
        if cwd:
            clauses.append("cwd = ?")
            vals.append(cwd)
        if parent_run:
            clauses.append("parent_run = ?")
            vals.append(parent_run)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._tx(write=False) as cur:
            rows = cur.execute(
                f"SELECT * FROM usage_events {where} ORDER BY ts ASC",
                vals,
            ).fetchall()

        in_flight = self.count_in_flight(backend=backend, cwd=cwd, parent_run=parent_run)

        def _empty_backend_bucket(bname: str) -> dict[str, Any]:
            return {
                "runs": 0,
                "completed": 0,
                "failed": 0,
                "timed_out": 0,
                "cancelled": 0,
                "in_flight": in_flight.get(bname, 0),
                "duration_seconds": 0.0,
                "tokens": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                },
                "usage_coverage": {
                    "exact": 0,
                    "delta": 0,
                    "partial": 0,
                    "missing": 0,
                },
            }

        overall_runs = 0
        overall_completed = 0
        overall_failed = 0
        overall_timed_out = 0
        overall_cancelled = 0
        overall_duration = 0.0
        overall_coverage = {"exact": 0, "delta": 0, "partial": 0, "missing": 0}
        by_backend: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}

        # Ensure backends with active in-flight jobs appear even before any job completes.
        for bname, cnt in in_flight.items():
            if cnt > 0:
                by_backend.setdefault(bname, _empty_backend_bucket(bname))

        for r in rows:
            bname = str(r["backend"])
            status = str(r["status"])
            dur = float(r["duration_seconds"] or 0.0)
            ustatus = str(r["usage_status"] or "missing")
            if ustatus not in overall_coverage:
                ustatus = "missing"

            overall_runs += 1
            overall_duration = round(overall_duration + dur, 2)
            overall_coverage[ustatus] += 1
            if status == "completed":
                overall_completed += 1
            elif status == "failed":
                overall_failed += 1
            elif status == "timed_out":
                overall_timed_out += 1
            elif status == "cancelled":
                overall_cancelled += 1

            bb = by_backend.setdefault(bname, _empty_backend_bucket(bname))
            bb["runs"] += 1
            if status in {"completed", "failed", "timed_out", "cancelled"}:
                bb[status] += 1
            bb["duration_seconds"] = round(bb["duration_seconds"] + dur, 2)
            bb["usage_coverage"][ustatus] += 1

            mkey = f"{bname}:{r['model'] or f'{bname}-default'}"
            mb = by_model.setdefault(
                mkey,
                {
                    "runs": 0,
                    "completed": 0,
                    "duration_seconds": 0.0,
                    "total_tokens": 0,
                    "usage_coverage": {"exact": 0, "delta": 0, "partial": 0, "missing": 0},
                },
            )
            mb["runs"] += 1
            if status == "completed":
                mb["completed"] += 1
            mb["duration_seconds"] = round(mb["duration_seconds"] + dur, 2)
            mb["usage_coverage"][ustatus] += 1

            # Only aggregate token counts for exact/delta runs (never inflate totals with cumulative partial blobs).
            if ustatus in {"exact", "delta"}:
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "total_tokens",
                ):
                    val = r[key]
                    if isinstance(val, (int, float)):
                        bb["tokens"][key] += int(val)
                if isinstance(r["total_tokens"], (int, float)):
                    mb["total_tokens"] += int(r["total_tokens"])

        # Add accounting semantics note per backend so parent AI never conflates cache semantics.
        if "neon" in by_backend:
            by_backend["neon"]["token_note"] = (
                "Per-turn Antigravity tokens (agy cache_read_tokens can exceed reported total_tokens; "
                "compare subscriptions via quota rather than raw combined tokens)."
            )
        if "codex" in by_backend:
            by_backend["codex"]["token_note"] = (
                "Per-turn Codex tokens (cached_tokens is a subset of input_tokens; "
                "reasoning_tokens is a subset of output_tokens)."
            )

        scope_info: dict[str, Any] = {"period": period}
        if backend:
            scope_info["backend"] = backend
        if cwd:
            scope_info["cwd"] = cwd
        if parent_run:
            scope_info["parent_run"] = parent_run

        summary: dict[str, Any] = {
            "scope": scope_info,
            "owner_id": owner_id,
            "db_path": str(self.db_path),
            "overview": {
                "runs": overall_runs,
                "completed": overall_completed,
                "failed": overall_failed,
                "timed_out": overall_timed_out,
                "cancelled": overall_cancelled,
                "in_flight": sum(in_flight.values()),
                "duration_seconds": overall_duration,
                "usage_coverage": overall_coverage,
            },
            "by_backend": by_backend,
        }

        if compact:
            # Compact planning view: drop verbose model table and raw recent rows.
            for bb in by_backend.values():
                bb.pop("token_note", None)
            return summary

        summary["by_model"] = by_model

        recent_n = max(0, min(int(recent_limit), 50))
        recent: list[dict[str, Any]] = []
        if recent_n > 0:
            for r in reversed(rows[-recent_n:]):
                item: dict[str, Any] = {
                    "time": r["iso_time"],
                    "job_id": r["job_id"],
                    "backend": r["backend"],
                    "model": r["model"],
                    "status": r["status"],
                    "duration_seconds": r["duration_seconds"],
                    "usage_status": r["usage_status"],
                    "total_tokens": r["total_tokens"],
                }
                if r["effort"]:
                    item["effort"] = r["effort"]
                if r["parent_run"]:
                    item["parent_run"] = r["parent_run"]
                if r["label"]:
                    item["label"] = r["label"]
                recent.append(item)
        summary["recent"] = recent
        return summary

    # --- Shared quota cache ---

    def get_cached_quota(self, backend: str, ttl_seconds: float) -> dict[str, Any] | None:
        with self._tx(write=False) as cur:
            row = cur.execute(
                "SELECT fetched_at, payload_json FROM quota_cache WHERE backend = ?",
                (backend,),
            ).fetchone()
            if row is None:
                return None
            age = time.time() - float(row["fetched_at"])
            if age > ttl_seconds:
                return None
            payload = _loads_opt(row["payload_json"])
            if payload is None:
                return None
            payload["cached"] = True
            payload["cache_age_seconds"] = round(max(0.0, age), 1)
            return payload

    def set_cached_quota(self, backend: str, payload: dict[str, Any]) -> None:
        with self._tx(write=True) as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO quota_cache (backend, fetched_at, payload_json)
                VALUES (?, ?, ?)
                """,
                (backend, time.time(), json.dumps(payload, separators=(",", ":"))),
            )
