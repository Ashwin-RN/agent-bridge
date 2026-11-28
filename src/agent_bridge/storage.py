"""Shared SQLite persistence for multi-window jobs, atomic session claims, SQL usage aggregation, and quota cache."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from agent_bridge.config import classify_quota_pool
from agent_bridge.models import TERMINAL_STATUSES, JobRecord

logger = logging.getLogger("agent_bridge.storage")

UsagePeriod = Literal["session", "today", "7d", "30d", "all"]

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS owners (
        owner_id TEXT PRIMARY KEY,
        pid INTEGER NOT NULL,
        started_at REAL NOT NULL,
        last_heartbeat REAL NOT NULL,
        exited_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        backend TEXT NOT NULL,
        quota_pool TEXT,
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
        cleanup_seconds REAL,
        response TEXT NOT NULL DEFAULT '',
        usage_json TEXT,
        provider_metrics_json TEXT,
        usage_status TEXT NOT NULL DEFAULT 'missing',
        exit_code INTEGER,
        error TEXT,
        stderr_tail TEXT,
        last_activity TEXT,
        provider_invoked INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(backend, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_cwd ON jobs(cwd)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_parent_run ON jobs(parent_run)",
    """
    CREATE TABLE IF NOT EXISTS session_claims (
        backend TEXT NOT NULL,
        session_id TEXT NOT NULL,
        job_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        claimed_at REAL NOT NULL,
        PRIMARY KEY (backend, session_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_session_claims_job ON session_claims(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_session_claims_owner ON session_claims(owner_id)",
    """
    CREATE TABLE IF NOT EXISTS usage_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL DEFAULT 1,
        is_final_attempt INTEGER NOT NULL DEFAULT 1,
        owner_id TEXT NOT NULL,
        ts REAL NOT NULL,
        iso_time TEXT NOT NULL,
        backend TEXT NOT NULL,
        quota_pool TEXT,
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
        provider_metrics_json TEXT,
        UNIQUE(job_id, attempt_number)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_usage_owner ON usage_events(owner_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_backend ON usage_events(backend)",
    "CREATE INDEX IF NOT EXISTS idx_usage_cwd ON usage_events(cwd)",
    "CREATE INDEX IF NOT EXISTS idx_usage_parent_run ON usage_events(parent_run)",
    """
    CREATE TABLE IF NOT EXISTS session_usage_watermarks (
        backend TEXT NOT NULL,
        session_id TEXT NOT NULL,
        cumulative_json TEXT NOT NULL,
        num_turns INTEGER NOT NULL DEFAULT 0,
        last_job_id TEXT,
        updated_at REAL NOT NULL,
        PRIMARY KEY (backend, session_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_safety_state (
        backend TEXT NOT NULL,
        session_id TEXT NOT NULL,
        blocking_job_id TEXT NOT NULL,
        provider_state TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (backend, session_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quota_cache (
        backend TEXT PRIMARY KEY,
        fetched_at REAL NOT NULL,
        payload_json TEXT NOT NULL
    )
    """,
)


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


def _is_owner_row_alive(
    pid: int | None,
    exited_at: float | None,
    process_identity: str | None = None,
    *,
    is_pid_alive_fn: Callable[[int], bool],
    process_identity_fn: Callable[[int], str | None] | None = None,
) -> bool:
    """Single canonical liveness predicate used by status checks, cancellation, recovery, and session claims.

    An owner is live while its row has no exit time, its pid is alive, and that pid is still the process
    that registered: the identity read from the kernel now equals the one recorded, exactly (see
    `nesting.process_identity`; nothing in it follows the wall clock). Where either is unknown (a row
    from an older bridge, a platform with no such identity, no reader given, or one that cannot be read
    now) the pid check decides alone, as before.
    """
    if exited_at is not None or pid is None:
        return False
    if not is_pid_alive_fn(int(pid)):
        return False
    if process_identity is None or process_identity_fn is None:
        return True
    current = process_identity_fn(int(pid))
    if current is None:
        return True
    return current == process_identity


class SessionHeldError(RuntimeError):
    """A job's CLI reported a session that another job, not yet ended, holds. Nothing was written."""

    def __init__(self, *, backend: str, session_id: str, holder_job_id: str) -> None:
        self.backend = backend
        self.session_id = session_id
        self.holder_job_id = holder_job_id
        super().__init__(
            f"Session {session_id!r} on {backend!r} is held by job {holder_job_id!r}, which has not ended."
        )


FinalizeOutcome = Literal["written", "kept", "missing"]


def is_sqlite_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _row_to_job(row: sqlite3.Row) -> JobRecord:
    keys = row.keys()
    raw_pool = row["quota_pool"] if "quota_pool" in keys else None
    pool = raw_pool or classify_quota_pool(row["backend"], row["model"] or row["requested_model"])
    provider_metrics = _loads_opt(row["provider_metrics_json"])
    attempts_val = None
    if isinstance(provider_metrics, dict) and isinstance(provider_metrics.get("attempts"), list):
        attempts_val = provider_metrics["attempts"]
    return JobRecord(
        job_id=row["job_id"],
        owner_id=row["owner_id"],
        backend=row["backend"],
        quota_pool=pool,
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
        cleanup_seconds=row["cleanup_seconds"] if "cleanup_seconds" in keys else None,
        response=row["response"] or "",
        usage=_loads_opt(row["usage_json"]),
        provider_metrics=provider_metrics,
        usage_status=row["usage_status"] or "missing",
        exit_code=row["exit_code"],
        error=row["error"],
        stderr_tail=row["stderr_tail"],
        last_activity=row["last_activity"],
        last_activity_at=row["last_activity_at"] if "last_activity_at" in keys else None,
        model_source=row["model_source"] if "model_source" in keys else None,
        response_chars=row["response_chars"] if "response_chars" in keys else None,
        provider_invoked=bool(row["provider_invoked"]) if "provider_invoked" in keys else (row["started_at"] is not None),
        attempts=attempts_val,
    )


def _insert_usage_event_row_cur(
    cur: sqlite3.Cursor,
    *,
    job_id: str,
    attempt_number: int = 1,
    is_final_attempt: bool = True,
    owner_id: str,
    now_ts: float,
    backend: str,
    quota_pool: str,
    model_name: str,
    effort: str | None,
    mode: str,
    status: str,
    duration_seconds: float,
    cwd: str,
    parent_run: str | None,
    label: str | None,
    session_id: str | None,
    usage_status: str,
    usage: dict[str, Any] | None,
    provider_metrics: dict[str, Any] | None,
    keep_existing: bool = False,
) -> None:
    u = usage if isinstance(usage, dict) else {}
    iso_time = datetime.fromtimestamp(now_ts).astimezone().isoformat(timespec="seconds")

    def _opt_int(key: str) -> int | None:
        v = u.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    conflict = "ON CONFLICT(job_id, attempt_number) DO NOTHING" if keep_existing else ""
    cur.execute(
        f"""
        INSERT OR REPLACE INTO usage_events (
            job_id, attempt_number, is_final_attempt, owner_id, ts, iso_time, backend, quota_pool,
            model, effort, mode, status, duration_seconds, cwd, parent_run, label, session_id,
            usage_status, input_tokens, output_tokens, cached_tokens, cache_write_tokens,
            reasoning_tokens, total_tokens, provider_metrics_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        {conflict}
        """,
        (
            job_id,
            max(1, int(attempt_number)),
            1 if is_final_attempt else 0,
            owner_id,
            round(now_ts, 3),
            iso_time,
            backend,
            quota_pool,
            model_name,
            effort,
            mode,
            status,
            round(float(duration_seconds or 0.0), 2),
            cwd,
            parent_run,
            label,
            session_id,
            usage_status,
            _opt_int("input_tokens"),
            _opt_int("output_tokens"),
            _opt_int("cached_tokens"),
            _opt_int("cache_write_tokens"),
            _opt_int("reasoning_tokens"),
            _opt_int("total_tokens"),
            json.dumps(provider_metrics, separators=(",", ":")) if provider_metrics else None,
        ),
    )


def _insert_usage_event_cur(
    cur: sqlite3.Cursor,
    record: JobRecord,
    *,
    attempts: list[dict[str, Any]] | None = None,
    keep_existing: bool = False,
) -> None:
    if keep_existing and cur.execute(
        "SELECT 1 FROM usage_events WHERE job_id = ? AND is_final_attempt = 1", (record.job_id,),
    ).fetchone():
        return  # This pruned job already has its final ledger.

    now_ts = record.finished_at or time.time()
    att_list = attempts if attempts and len(attempts) >= 1 else None
    if att_list is None and record.attempts and len(record.attempts) >= 1:
        att_list = record.attempts

    if att_list is not None and len(att_list) > 1:
        if not keep_existing:
            # Clear any stale attempt rows beyond len(att_list) before writing the final ledger.
            cur.execute("DELETE FROM usage_events WHERE job_id = ?", (record.job_id,))
        total_att = len(att_list)
        for idx, att in enumerate(att_list, start=1):
            is_last = idx == total_att
            att_model = att.get("model") or record.model or record.requested_model or f"{record.backend}-default"
            att_pool = att.get("quota_pool") or classify_quota_pool(record.backend, att_model)
            att_status = str(att.get("status") or (record.status if is_last else "failed"))
            att_dur = float(att.get("duration_seconds") or 0.0)
            att_ustatus = str(att.get("usage_status") or "missing")
            att_usage = att.get("usage") if isinstance(att.get("usage"), dict) else None
            att_pm = att.get("provider_metrics") if isinstance(att.get("provider_metrics"), dict) else None
            _insert_usage_event_row_cur(
                cur,
                job_id=record.job_id,
                attempt_number=int(att.get("attempt") or idx),
                is_final_attempt=is_last,
                owner_id=record.owner_id,
                now_ts=now_ts - ((total_att - idx) * 0.001),
                backend=record.backend,
                quota_pool=str(att_pool),
                model_name=str(att_model),
                effort=att.get("effort") or record.effort,
                mode=record.mode,
                status=att_status,
                duration_seconds=att_dur,
                cwd=record.cwd,
                parent_run=record.parent_run,
                label=record.label,
                session_id=record.session_id,
                usage_status=att_ustatus,
                usage=att_usage,
                provider_metrics=att_pm,
                keep_existing=keep_existing,
            )
        return

    # If a prior non-final attempt was already recorded (e.g. cancelled during retry backoff!),
    # keep those prior attempt rows intact and append the final terminal attempt cleanly.
    existing_prior = cur.execute(
        "SELECT MAX(attempt_number) AS max_att FROM usage_events WHERE job_id = ? AND is_final_attempt = 0",
        (record.job_id,),
    ).fetchone()
    prior_max = int(existing_prior["max_att"] or 0) if existing_prior is not None else 0
    if prior_max > 0:
        final_att_num = prior_max + 1
        final_dur = 0.0
        final_usage = None
        final_ustatus = "missing"
    else:
        final_att_num = 1
        final_dur = float(record.duration_seconds or 0.0)
        final_usage = record.usage
        final_ustatus = record.usage_status

    model_name = record.model or record.requested_model or f"{record.backend}-default"
    quota_pool = record.quota_pool or classify_quota_pool(record.backend, model_name)
    _insert_usage_event_row_cur(
        cur,
        job_id=record.job_id,
        attempt_number=final_att_num,
        is_final_attempt=True,
        owner_id=record.owner_id,
        now_ts=now_ts,
        backend=record.backend,
        quota_pool=str(quota_pool),
        model_name=model_name,
        effort=record.effort,
        mode=record.mode,
        status=record.status,
        duration_seconds=final_dur,
        cwd=record.cwd,
        parent_run=record.parent_run,
        label=record.label,
        session_id=record.session_id,
        usage_status=final_ustatus,
        usage=final_usage,
        provider_metrics=record.provider_metrics,
        keep_existing=keep_existing,
    )


def _recover_job_row_cur(cur: sqlite3.Cursor, row: sqlite3.Row, now: float) -> bool:
    """Mark one dead-owner job failed, record usage, release its session claim, and invalidate watermarks only if launched."""
    rec = _row_to_job(row)
    was_queued_never_started = (
        (not rec.provider_invoked)
        and (rec.started_at is None)
        and (str(row["status"]) == "queued")
    )
    rec.status = "failed"
    rec.failure_reason = "owner_exited"
    rec.provider_state = "stopped" if was_queued_never_started else "unknown"
    rec.error = f"Owning bridge process ({rec.owner_id}) exited before job completed."
    rec.retry_guidance = (
        "Owning bridge process exited while this job was still queued (never launched); safe to retry."
        if was_queued_never_started
        else "Verify whether any provider work or file edits landed before retrying; "
        "for edit tasks inspect git status/diff first."
    )
    rec.finished_at = now
    if rec.started_at is not None:
        rec.duration_seconds = round(max(0.0, now - rec.started_at), 2)
    else:
        rec.duration_seconds = 0.0
    rec.last_activity = "owner_exited"
    rec.last_activity_at = now
    rec.usage_status = "missing"

    cur.execute(
        """
        UPDATE jobs SET
            status = ?, failure_reason = ?, provider_state = ?, retry_guidance = ?,
            error = ?, finished_at = ?, duration_seconds = ?, last_activity = ?, last_activity_at = ?,
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
            rec.last_activity_at,
            rec.usage_status,
            rec.job_id,
        ),
    )
    changed = cur.rowcount > 0
    cur.execute("DELETE FROM session_claims WHERE job_id = ?", (rec.job_id,))
    if changed:
        _insert_usage_event_cur(cur, rec)
        # Only invalidate Antigravity's usage checkpoint when the recovered job had actually invoked the provider!
        if rec.provider_invoked and rec.backend == "neon" and rec.session_id:
            cur.execute(
                "DELETE FROM session_usage_watermarks WHERE backend = ? AND session_id = ?",
                (rec.backend, rec.session_id),
            )
        # Persist durable unresolved provider state per session (independent of job-history pruning).
        if (rec.provider_invoked or not was_queued_never_started) and rec.session_id and rec.provider_state in {
            "possibly_running",
            "unknown",
        }:
            cur.execute(
                """
                INSERT OR REPLACE INTO session_safety_state (
                    backend, session_id, blocking_job_id, provider_state, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (rec.backend, rec.session_id, rec.job_id, rec.provider_state, now),
            )
    return changed


def _live_session_holder_cur(
    cur: sqlite3.Cursor,
    backend: str,
    session_id: str,
    *,
    job_id: str,
    owner_id: str,
    now: float,
    is_pid_alive_fn: Callable[[int], bool] | None,
    process_identity_fn: Callable[[int], str | None] | None = None,
) -> str | None:
    """The job, other than `job_id`, that holds `(backend, session_id)` and is live, or None.

    The one claim decision, for a turn that asks for a session and for one whose CLI reveals it. A claim
    whose job has ended is dropped. A claim whose job's window has died is released by recovering that
    job (failed, owner_exited; which leaves the session blocked as `unknown` if the turn had started).
    Without a pid check (`is_pid_alive_fn=None`) any holder that has not ended counts as live.
    """
    claim = cur.execute(
        "SELECT job_id FROM session_claims WHERE backend = ? AND session_id = ?",
        (backend, session_id),
    ).fetchone()
    if claim is None or str(claim["job_id"]) == job_id:
        return None
    holder_id = str(claim["job_id"])
    holder_row = cur.execute(
        """
        SELECT j.*, o.pid AS owner_pid, o.exited_at AS owner_exited_at,
               o.process_identity AS owner_process_identity
        FROM jobs j
        LEFT JOIN owners o ON j.owner_id = o.owner_id
        WHERE j.job_id = ?
        """,
        (holder_id,),
    ).fetchone()
    if holder_row is None or str(holder_row["status"]) in TERMINAL_STATUSES:
        cur.execute(
            "DELETE FROM session_claims WHERE backend = ? AND session_id = ?",
            (backend, session_id),
        )
        return None
    if is_pid_alive_fn is None:
        return holder_id
    holder_alive = str(holder_row["owner_id"]) == owner_id or _is_owner_row_alive(
        holder_row["owner_pid"],
        holder_row["owner_exited_at"],
        holder_row["owner_process_identity"],
        is_pid_alive_fn=is_pid_alive_fn,
        process_identity_fn=process_identity_fn,
    )
    if holder_alive:
        return holder_id
    # Dead owner held the claim: recover its job atomically, which releases the claim.
    _recover_job_row_cur(cur, holder_row, now)
    if holder_row["owner_pid"] is not None and holder_row["owner_exited_at"] is None:
        cur.execute(
            "UPDATE owners SET exited_at = ? WHERE owner_id = ? AND exited_at IS NULL",
            (now, str(holder_row["owner_id"])),
        )
    return None


def session_held_error_text(session_id: str, holder_job_id: str) -> str:
    return (
        f"The CLI reported session {session_id!r}, which job {holder_job_id!r} holds and has not ended. Two "
        "turns must not run on one session at once, so this job was stopped, and that session is not "
        "recorded on it."
    )


def session_held_guidance(session_id: str, holder_job_id: str) -> str:
    return (
        f"Wait for job {holder_job_id!r} to end, then pass resume_from={session_id!r} if this turn was meant "
        "to continue that session. For edit tasks inspect git status/diff first."
    )


def _reconcile_session_safety_cur(
    cur: sqlite3.Cursor,
    backend: str,
    session_id: str,
    *,
    persist: bool,
) -> tuple[str, str] | None:
    """Return active `(blocking_job_id, provider_state)` for `(backend, session_id)`.

    Checks durable `session_safety_state` AND reconciles against the latest terminal invoked row in
    `jobs` so mixed-version older writers (which don't populate `session_safety_state`) can never
    bypass the uncertain-prior-turn resume gate.
    """
    safety = cur.execute(
        """
        SELECT blocking_job_id, provider_state, updated_at
        FROM session_safety_state
        WHERE backend = ? AND session_id = ?
        """,
        (backend, session_id),
    ).fetchone()

    latest = cur.execute(
        """
        SELECT rowid AS rid, job_id, provider_state, COALESCE(finished_at, created_at) AS ts
        FROM jobs
        WHERE backend = ? AND session_id = ?
          AND status IN ('completed', 'failed', 'cancelled', 'timed_out')
          AND (
              provider_invoked = 1
              OR started_at IS NOT NULL
              OR provider_state IN ('possibly_running', 'unknown')
          )
        ORDER BY COALESCE(finished_at, created_at) DESC, rowid DESC
        LIMIT 1
        """,
        (backend, session_id),
    ).fetchone()

    if latest is not None and str(latest["provider_state"]) in {"possibly_running", "unknown"}:
        latest_id = str(latest["job_id"])
        latest_state = str(latest["provider_state"])
        latest_ts = float(latest["ts"] or time.time())

        if safety is None:
            if persist:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO session_safety_state (
                        backend, session_id, blocking_job_id, provider_state, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (backend, session_id, latest_id, latest_state, latest_ts),
                )
            return latest_id, latest_state

        safety_id = str(safety["blocking_job_id"])
        safety_state = str(safety["provider_state"])
        if safety_id == latest_id:
            return safety_id, safety_state

        safety_job = cur.execute(
            "SELECT 1 FROM jobs WHERE job_id = ?",
            (safety_id,),
        ).fetchone()
        if safety_job is not None or latest_ts >= float(safety["updated_at"]):
            if persist:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO session_safety_state (
                        backend, session_id, blocking_job_id, provider_state, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (backend, session_id, latest_id, latest_state, latest_ts),
                )
            return latest_id, latest_state
        return safety_id, safety_state

    if safety is not None:
        return str(safety["blocking_job_id"]), str(safety["provider_state"])
    return None


class BridgeStore:
    """Concurrency-safe SQLite store shared across multiple bridge processes."""

    def __init__(self, state_dir: Path, *, max_stored_jobs: int = 500) -> None:
        self.state_dir = state_dir
        self.db_path = state_dir / "bridge.db"
        self.max_stored_jobs = max(20, max_stored_jobs)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self, *, busy_timeout_ms: int = 1000) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=max(1.0, busy_timeout_ms / 1000.0),
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={max(50, int(busy_timeout_ms))}")
        for attempt in range(6):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if is_sqlite_lock_error(exc) and attempt < 5:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        # Execute ALL DDL checks, column migrations, and backfills inside ONE atomic BEGIN IMMEDIATE
        # transaction so concurrent windows upgrading an older DB never race on ALTER TABLE or partial backfill.
        with self._tx(write=True) as cur:
            for stmt in _SCHEMA_STATEMENTS:
                cur.execute(stmt)

            wm_cols = {
                str(r["name"])
                for r in cur.execute("PRAGMA table_info(session_usage_watermarks)").fetchall()
            }
            if "last_job_id" not in wm_cols:
                cur.execute("ALTER TABLE session_usage_watermarks ADD COLUMN last_job_id TEXT")

            job_cols = {
                str(r["name"])
                for r in cur.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "quota_pool" not in job_cols:
                cur.execute("ALTER TABLE jobs ADD COLUMN quota_pool TEXT")
            if "provider_invoked" not in job_cols:
                cur.execute("ALTER TABLE jobs ADD COLUMN provider_invoked INTEGER NOT NULL DEFAULT 0")
                cur.execute("UPDATE jobs SET provider_invoked = 1 WHERE started_at IS NOT NULL")
            if "cleanup_seconds" not in job_cols:
                cur.execute("ALTER TABLE jobs ADD COLUMN cleanup_seconds REAL")
            if "last_activity_at" not in job_cols:
                cur.execute("ALTER TABLE jobs ADD COLUMN last_activity_at REAL")
            if "model_source" not in job_cols:
                cur.execute("ALTER TABLE jobs ADD COLUMN model_source TEXT")
            if "response_chars" not in job_cols:
                cur.execute("ALTER TABLE jobs ADD COLUMN response_chars INTEGER")

            # Always backfill any NULL quota_pool rows atomically (recovers interrupted prior migrations too).
            if cur.execute("SELECT 1 FROM jobs WHERE quota_pool IS NULL LIMIT 1").fetchone() is not None:
                cur.execute(
                    """
                    UPDATE jobs SET quota_pool = CASE
                        WHEN backend = 'codex' THEN 'codex'
                        WHEN backend = 'claude' THEN 'claude'
                        WHEN LOWER(COALESCE(model, requested_model, '')) LIKE '%claude%'
                          OR LOWER(COALESCE(model, requested_model, '')) LIKE '%gpt%'
                          OR LOWER(COALESCE(model, requested_model, '')) LIKE '%opus%'
                          OR LOWER(COALESCE(model, requested_model, '')) LIKE '%sonnet%'
                          OR LOWER(COALESCE(model, requested_model, '')) LIKE '%haiku%'
                          OR LOWER(COALESCE(model, requested_model, '')) LIKE '%oss%'
                        THEN 'agy_claude_gpt'
                        ELSE 'agy_gemini'
                    END
                    WHERE quota_pool IS NULL
                    """
                )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_quota_pool ON jobs(quota_pool)")

            usage_cols = {
                str(r["name"])
                for r in cur.execute("PRAGMA table_info(usage_events)").fetchall()
            }
            if "quota_pool" not in usage_cols:
                cur.execute("ALTER TABLE usage_events ADD COLUMN quota_pool TEXT")

            # Upgrade older `usage_events` tables that had single-column `job_id TEXT UNIQUE`
            # into `UNIQUE(job_id, attempt_number)` atomically inside this BEGIN IMMEDIATE tx.
            usage_ddl_row = cur.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'usage_events'"
            ).fetchone()
            usage_ddl = str(usage_ddl_row["sql"] or "") if usage_ddl_row is not None else ""
            if "attempt_number" not in usage_cols or "job_id TEXT UNIQUE" in usage_ddl:
                cur.execute(
                    """
                    CREATE TABLE usage_events_migrated (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        attempt_number INTEGER NOT NULL DEFAULT 1,
                        is_final_attempt INTEGER NOT NULL DEFAULT 1,
                        owner_id TEXT NOT NULL,
                        ts REAL NOT NULL,
                        iso_time TEXT NOT NULL,
                        backend TEXT NOT NULL,
                        quota_pool TEXT,
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
                        provider_metrics_json TEXT,
                        UNIQUE(job_id, attempt_number)
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT OR REPLACE INTO usage_events_migrated (
                        job_id, attempt_number, is_final_attempt, owner_id, ts, iso_time, backend,
                        quota_pool, model, effort, mode, status, duration_seconds, cwd, parent_run,
                        label, session_id, usage_status, input_tokens, output_tokens, cached_tokens,
                        cache_write_tokens, reasoning_tokens, total_tokens, provider_metrics_json
                    )
                    SELECT
                        CASE
                            WHEN INSTR(u.job_id, '#attempt-') > 0
                            THEN SUBSTR(u.job_id, 1, INSTR(u.job_id, '#attempt-') - 1)
                            ELSE u.job_id
                        END,
                        CASE
                            WHEN INSTR(u.job_id, '#attempt-') > 0
                            THEN COALESCE(CAST(SUBSTR(u.job_id, INSTR(u.job_id, '#attempt-') + 9) AS INTEGER), 1)
                            ELSE 1 + COALESCE((
                                SELECT MAX(CAST(SUBSTR(u2.job_id, INSTR(u2.job_id, '#attempt-') + 9) AS INTEGER))
                                FROM usage_events u2
                                WHERE u2.job_id LIKE u.job_id || '#attempt-%'
                            ), 0)
                        END,
                        CASE WHEN INSTR(u.job_id, '#attempt-') > 0 THEN 0 ELSE 1 END,
                        u.owner_id, u.ts, u.iso_time, u.backend, u.quota_pool, u.model, u.effort, u.mode,
                        u.status, u.duration_seconds, u.cwd, u.parent_run, u.label, u.session_id,
                        u.usage_status, u.input_tokens, u.output_tokens, u.cached_tokens,
                        u.cache_write_tokens, u.reasoning_tokens, u.total_tokens, u.provider_metrics_json
                    FROM usage_events u
                    ORDER BY u.id ASC
                    """
                )
                cur.execute("DROP TABLE usage_events")
                cur.execute("ALTER TABLE usage_events_migrated RENAME TO usage_events")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_owner ON usage_events(owner_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_backend ON usage_events(backend)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_cwd ON usage_events(cwd)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_parent_run ON usage_events(parent_run)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_job_attempt ON usage_events(job_id, attempt_number)")

            if cur.execute("SELECT 1 FROM usage_events WHERE quota_pool IS NULL LIMIT 1").fetchone() is not None:
                cur.execute(
                    """
                    UPDATE usage_events SET quota_pool = CASE
                        WHEN backend = 'codex' THEN 'codex'
                        WHEN backend = 'claude' THEN 'claude'
                        WHEN LOWER(COALESCE(model, '')) LIKE '%claude%'
                          OR LOWER(COALESCE(model, '')) LIKE '%gpt%'
                          OR LOWER(COALESCE(model, '')) LIKE '%opus%'
                          OR LOWER(COALESCE(model, '')) LIKE '%sonnet%'
                          OR LOWER(COALESCE(model, '')) LIKE '%haiku%'
                          OR LOWER(COALESCE(model, '')) LIKE '%oss%'
                        THEN 'agy_claude_gpt'
                        ELSE 'agy_gemini'
                    END
                    WHERE quota_pool IS NULL
                    """
                )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_quota_pool ON usage_events(quota_pool)")

            # Always reconcile any latest-uncertain terminal rows in `jobs` into durable
            # `session_safety_state` (covers initial upgrade AND mixed-version older writers).
            uncertain_rows = cur.execute(
                """
                SELECT j.backend, j.session_id, j.job_id, j.provider_state,
                       COALESCE(j.finished_at, j.created_at) AS ts
                FROM jobs j
                WHERE j.session_id IS NOT NULL
                  AND j.status IN ('completed', 'failed', 'cancelled', 'timed_out')
                  AND j.provider_state IN ('possibly_running', 'unknown')
                  AND (
                      j.provider_invoked = 1
                      OR j.started_at IS NOT NULL
                      OR j.provider_state IN ('possibly_running', 'unknown')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs j2
                      WHERE j2.backend = j.backend
                        AND j2.session_id = j.session_id
                        AND j2.status IN ('completed', 'failed', 'cancelled', 'timed_out')
                        AND (
                            j2.provider_invoked = 1
                            OR j2.started_at IS NOT NULL
                            OR j2.provider_state IN ('possibly_running', 'unknown')
                        )
                        AND (
                            COALESCE(j2.finished_at, j2.created_at) > COALESCE(j.finished_at, j.created_at)
                            OR (
                                COALESCE(j2.finished_at, j2.created_at) = COALESCE(j.finished_at, j.created_at)
                                AND j2.rowid > j.rowid
                            )
                        )
                  )
                """
            ).fetchall()
            now = time.time()
            for urow in uncertain_rows:
                u_backend = str(urow["backend"])
                u_sid = str(urow["session_id"])
                u_jid = str(urow["job_id"])
                u_state = str(urow["provider_state"])
                u_ts = float(urow["ts"] or now)
                existing = cur.execute(
                    """
                    SELECT blocking_job_id, updated_at
                    FROM session_safety_state
                    WHERE backend = ? AND session_id = ?
                    """,
                    (u_backend, u_sid),
                ).fetchone()
                if existing is None:
                    cur.execute(
                        """
                        INSERT INTO session_safety_state (
                            backend, session_id, blocking_job_id, provider_state, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (u_backend, u_sid, u_jid, u_state, u_ts),
                    )
                elif str(existing["blocking_job_id"]) != u_jid:
                    ex_job = cur.execute(
                        "SELECT 1 FROM jobs WHERE job_id = ?",
                        (str(existing["blocking_job_id"]),),
                    ).fetchone()
                    if ex_job is not None or u_ts >= float(existing["updated_at"]):
                        cur.execute(
                            """
                            INSERT OR REPLACE INTO session_safety_state (
                                backend, session_id, blocking_job_id, provider_state, updated_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (u_backend, u_sid, u_jid, u_state, u_ts),
                        )
            cur.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('session_safety_backfilled', ?)",
                (str(now),),
            )

            self._migrate_owner_process_identity_cur(cur)

        self._import_legacy_files_if_needed()

    @staticmethod
    def _migrate_owner_process_identity_cur(cur: sqlite3.Cursor) -> None:
        """Migration step: `owners.process_identity`, a token naming the owning process itself.

        Additive and nullable. A row without it, written by an older bridge or on a platform with no
        stable identity, is "unknown": its liveness is the pid check alone, as before.
        """
        owner_cols = {str(r["name"]) for r in cur.execute("PRAGMA table_info(owners)").fetchall()}
        if "process_identity" not in owner_cols:
            cur.execute("ALTER TABLE owners ADD COLUMN process_identity TEXT")

    def _import_legacy_files_if_needed(self) -> None:
        """One-time, idempotent import of v1 `jobs.json` and `usage.jsonl` into SQLite."""
        legacy_jobs_path = self.state_dir / "jobs.json"
        legacy_usage_path = self.state_dir / "usage.jsonl"
        if not legacy_jobs_path.exists() and not legacy_usage_path.exists():
            return

        with self._tx(write=True) as cur:
            imported_keys = {
                str(r["key"])
                for r in cur.execute(
                    "SELECT key FROM meta WHERE key IN ('legacy_jobs_imported', 'legacy_usage_imported')"
                ).fetchall()
            }

            if legacy_jobs_path.exists() and "legacy_jobs_imported" not in imported_keys:
                self._import_legacy_jobs_cur(cur, legacy_jobs_path)
                cur.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('legacy_jobs_imported', ?)",
                    (str(time.time()),),
                )

            if legacy_usage_path.exists() and "legacy_usage_imported" not in imported_keys:
                self._import_legacy_usage_cur(cur, legacy_usage_path)
                cur.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('legacy_usage_imported', ?)",
                    (str(time.time()),),
                )

    def _import_legacy_jobs_cur(self, cur: sqlite3.Cursor, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read legacy jobs file %s: %s", path, exc)
            return

        entries = raw.get("jobs") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return

        now = time.time()
        imported = 0
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("job_id"):
                continue
            backend = str(entry.get("backend") or "")
            if backend not in {"neon", "codex"}:
                continue
            cwd = str(entry.get("cwd") or "")
            if not cwd:
                continue

            status = str(entry.get("status") or "failed")
            failure_reason = entry.get("failure_reason")
            provider_state = entry.get("provider_state")
            error = entry.get("error")
            finished_at = entry.get("finished_at")
            if status not in TERMINAL_STATUSES:
                status = "failed"
                failure_reason = failure_reason or "owner_exited"
                provider_state = provider_state or "unknown"
                error = error or "Legacy job was not terminal before upgrade to SQLite."
                finished_at = finished_at or now

            usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else None
            usage_status = entry.get("usage_status")
            if not usage_status:
                if usage is None:
                    usage_status = "missing"
                elif backend == "neon" and entry.get("requested_session_id"):
                    # Legacy resumed Neon turns may have stored cumulative counts; never trust as exact.
                    usage_status = "partial"
                elif status == "completed":
                    usage_status = "exact"
                else:
                    usage_status = "partial"

            quota_pool = entry.get("quota_pool") or classify_quota_pool(
                backend,
                entry.get("model") or entry.get("requested_model"),
            )
            cur.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    job_id, owner_id, backend, quota_pool, cwd, effective_cwd, mode,
                    requested_model, model, effort, model_notice, prompt_preview,
                    label, parent_run, worktree, worktree_path, worktree_branch,
                    worktree_commit, requested_session_id, session_id, status,
                    failure_reason, provider_state, retry_guidance, created_at,
                    started_at, finished_at, duration_seconds, response, usage_json,
                    provider_metrics_json, usage_status, exit_code, error, stderr_tail,
                    last_activity, provider_invoked
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    str(entry["job_id"]),
                    str(entry.get("owner_id") or "legacy-json"),
                    backend,
                    str(quota_pool),
                    cwd,
                    str(entry.get("effective_cwd") or cwd),
                    str(entry.get("mode") or "read_only"),
                    entry.get("requested_model"),
                    entry.get("model"),
                    entry.get("effort"),
                    entry.get("model_notice"),
                    str(entry.get("prompt_preview") or ""),
                    entry.get("label"),
                    entry.get("parent_run"),
                    1 if entry.get("worktree") else 0,
                    entry.get("worktree_path"),
                    entry.get("worktree_branch"),
                    entry.get("worktree_commit"),
                    entry.get("requested_session_id"),
                    entry.get("session_id"),
                    status,
                    failure_reason,
                    provider_state,
                    entry.get("retry_guidance"),
                    float(entry.get("created_at") or now),
                    entry.get("started_at"),
                    finished_at,
                    entry.get("duration_seconds"),
                    str(entry.get("response") or "")[:200_000],
                    json.dumps(usage, separators=(",", ":")) if usage else None,
                    None,
                    str(usage_status),
                    entry.get("exit_code"),
                    error,
                    entry.get("stderr_tail"),
                    entry.get("last_activity") or status,
                    1 if (entry.get("started_at") is not None or status == "completed") else 0,
                ),
            )
            if cur.rowcount > 0:
                imported += 1
        if imported:
            logger.info("Imported %d legacy jobs from %s into %s", imported, path, self.db_path)

    def _import_legacy_usage_cur(self, cur: sqlite3.Cursor, path: Path) -> None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            logger.warning("Could not read legacy usage file %s: %s", path, exc)
            return

        imported = 0
        for idx, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            backend = str(entry.get("backend") or "")
            if backend not in {"neon", "codex"}:
                continue
            job_id = str(entry.get("job_id") or f"legacy-usage-{idx}")
            ts = float(entry.get("ts") or time.time())
            iso_time = str(
                entry.get("iso_time")
                or datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
            )
            status = str(entry.get("status") or "completed")
            usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}

            def _opt_int(key: str) -> int | None:
                v = usage.get(key)
                return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

            has_numeric_usage = any(
                _opt_int(k) is not None
                for k in ("input_tokens", "output_tokens", "total_tokens")
            )
            explicit_ustatus = entry.get("usage_status")
            if isinstance(explicit_ustatus, str) and explicit_ustatus in {"exact", "delta", "partial", "missing"}:
                usage_status = explicit_ustatus
            elif not has_numeric_usage:
                usage_status = "missing"
            elif backend == "neon":
                # Legacy v1 `usage.jsonl` lacked resume metadata and stored cumulative Antigravity blobs on
                # resumed turns; mark ambiguous legacy Neon totals as `partial` so they never inflate totals.
                usage_status = "partial"
            elif status == "completed":
                usage_status = "exact"
            else:
                usage_status = "partial"

            session_key = entry.get("session_key")
            owner_id = f"legacy-{session_key}" if session_key else "legacy-jsonl"
            model_name = str(entry.get("model") or f"{backend}-default")
            quota_pool = str(entry.get("quota_pool") or classify_quota_pool(backend, model_name))
            cur.execute(
                """
                INSERT OR IGNORE INTO usage_events (
                    job_id, owner_id, ts, iso_time, backend, quota_pool, model, effort, mode, status,
                    duration_seconds, cwd, parent_run, label, session_id, usage_status,
                    input_tokens, output_tokens, cached_tokens, cache_write_tokens,
                    reasoning_tokens, total_tokens, provider_metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    owner_id,
                    round(ts, 3),
                    iso_time,
                    backend,
                    quota_pool,
                    model_name,
                    entry.get("effort"),
                    str(entry.get("mode") or "read_only"),
                    status,
                    round(float(entry.get("duration_seconds") or 0.0), 2),
                    str(entry.get("cwd") or ""),
                    entry.get("parent_run"),
                    entry.get("label"),
                    entry.get("session_id"),
                    usage_status,
                    _opt_int("input_tokens"),
                    _opt_int("output_tokens"),
                    _opt_int("cached_tokens"),
                    _opt_int("cache_write_tokens"),
                    _opt_int("reasoning_tokens"),
                    _opt_int("total_tokens"),
                    None,
                ),
            )
            if cur.rowcount > 0:
                imported += 1
        if imported:
            logger.info("Imported %d legacy usage events from %s into %s", imported, path, self.db_path)

    @contextmanager
    def _tx(
        self,
        *,
        write: bool = True,
        lock_timeout: float = 5.0,
    ) -> Iterator[sqlite3.Cursor]:
        busy_ms = max(50, min(1000, int(lock_timeout * 1000)))
        conn = self._connect(busy_timeout_ms=busy_ms)
        try:
            cur = conn.cursor()
            if write:
                deadline = time.monotonic() + max(0.05, lock_timeout)
                attempt = 0
                while True:
                    try:
                        cur.execute("BEGIN IMMEDIATE")
                        break
                    except sqlite3.OperationalError as exc:
                        if is_sqlite_lock_error(exc) and time.monotonic() < deadline:
                            attempt += 1
                            time.sleep(min(0.15, 0.02 * attempt, max(0.0, deadline - time.monotonic())))
                            continue
                        raise
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

    def register_owner(self, owner_id: str, pid: int, *, process_identity: str | None = None) -> None:
        """Record a live bridge process. `process_identity` names that process, None when unknown."""
        now = time.time()
        with self._tx(write=True) as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO owners (
                    owner_id, pid, started_at, last_heartbeat, exited_at, process_identity
                ) VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (owner_id, int(pid), now, now, process_identity),
            )

    def mark_owner_exited(self, owner_id: str) -> None:
        now = time.time()
        with self._tx(write=True) as cur:
            cur.execute(
                "UPDATE owners SET exited_at = ?, last_heartbeat = ? WHERE owner_id = ?",
                (now, now, owner_id),
            )
            cur.execute("DELETE FROM session_claims WHERE owner_id = ?", (owner_id,))

    def list_live_owners(self) -> list[tuple[str, int, float]]:
        """`(owner_id, pid, started_at)` for every bridge process not marked as exited."""
        with self._tx(write=False) as cur:
            rows = cur.execute(
                "SELECT owner_id, pid, started_at FROM owners WHERE exited_at IS NULL"
            ).fetchall()
        return [(str(r["owner_id"]), int(r["pid"]), float(r["started_at"])) for r in rows]

    def is_owner_alive(
        self,
        owner_id: str,
        *,
        is_pid_alive_fn: Callable[[int], bool],
        process_identity_fn: Callable[[int], str | None] | None = None,
    ) -> bool:
        with self._tx(write=False) as cur:
            row = cur.execute(
                "SELECT pid, exited_at, process_identity FROM owners WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
            if row is None:
                return False
            return _is_owner_row_alive(
                row["pid"],
                row["exited_at"],
                row["process_identity"],
                is_pid_alive_fn=is_pid_alive_fn,
                process_identity_fn=process_identity_fn,
            )

    def recover_dead_owner_jobs(
        self,
        current_owner_id: str,
        *,
        is_pid_alive_fn: Callable[[int], bool],
        process_identity_fn: Callable[[int], str | None] | None = None,
    ) -> list[str]:
        """Recover only queued/running jobs whose owning bridge process has actually exited."""
        recovered: list[str] = []
        now = time.time()
        with self._tx(write=True) as cur:
            rows = cur.execute(
                """
                SELECT j.*, o.pid AS owner_pid, o.exited_at AS owner_exited_at,
                       o.process_identity AS owner_process_identity
                FROM jobs j
                LEFT JOIN owners o ON j.owner_id = o.owner_id
                WHERE j.status IN ('queued', 'running') AND j.owner_id != ?
                """,
                (current_owner_id,),
            ).fetchall()

            dead_owners_to_mark: set[str] = set()
            for row in rows:
                alive = _is_owner_row_alive(
                    row["owner_pid"],
                    row["owner_exited_at"],
                    row["owner_process_identity"],
                    is_pid_alive_fn=is_pid_alive_fn,
                    process_identity_fn=process_identity_fn,
                )
                if alive:
                    continue
                if _recover_job_row_cur(cur, row, now):
                    recovered.append(str(row["job_id"]))
                if row["owner_pid"] is not None and row["owner_exited_at"] is None:
                    dead_owners_to_mark.add(str(row["owner_id"]))

            for dead_oid in dead_owners_to_mark:
                cur.execute(
                    "UPDATE owners SET exited_at = ? WHERE owner_id = ? AND exited_at IS NULL",
                    (now, dead_oid),
                )

            # Prune any dangling session claims referencing terminal or missing jobs.
            cur.execute(
                """
                DELETE FROM session_claims
                WHERE job_id NOT IN (
                    SELECT job_id FROM jobs WHERE status IN ('queued', 'running')
                )
                """
            )

        return recovered

    # --- Atomic per-session claims ---

    def get_session_safety_block(
        self,
        backend: str,
        session_id: str,
    ) -> dict[str, str] | None:
        """Return durable unresolved provider state for `(backend, session_id)` (reconciling older writers too)."""
        with self._tx(write=False) as cur:
            block = _reconcile_session_safety_cur(cur, backend, session_id, persist=False)
            if block is None:
                return None
            return {
                "blocking_job_id": block[0],
                "provider_state": block[1],
            }

    def try_claim_session(
        self,
        backend: str,
        session_id: str,
        job_id: str,
        owner_id: str,
        *,
        is_pid_alive_fn: Callable[[int], bool],
        process_identity_fn: Callable[[int], str | None] | None = None,
        confirm_prior_job_id: str | None = None,
    ) -> tuple[Literal["claimed", "busy", "blocked"], str | None, str | None]:
        """Atomically claim `(backend, session_id)` for `job_id`.

        Returns:
        - `("claimed", None, None)` if claimed by `job_id`
        - `("busy", holder_job_id, None)` if another live job currently holds the turn
        - `("blocked", blocking_job_id, prior_provider_state)` if the session has unresolved
          uncertain provider state that has not been confirmed for that exact `blocking_job_id`.
        """
        now = time.time()
        with self._tx(write=True) as cur:
            claim = cur.execute(
                "SELECT job_id FROM session_claims WHERE backend = ? AND session_id = ?",
                (backend, session_id),
            ).fetchone()
            if claim is not None and str(claim["job_id"]) == job_id:
                return "claimed", None, None
            holder_id = _live_session_holder_cur(
                cur,
                backend,
                session_id,
                job_id=job_id,
                owner_id=owner_id,
                now=now,
                is_pid_alive_fn=is_pid_alive_fn,
                process_identity_fn=process_identity_fn,
            )
            if holder_id is not None:
                return "busy", holder_id, None

            # Defensive check: ensure no active RUNNING job on `(backend, session_id)` lacks a claim row.
            running_rows = cur.execute(
                """
                SELECT j.*, o.pid AS owner_pid, o.exited_at AS owner_exited_at,
                       o.process_identity AS owner_process_identity
                FROM jobs j
                LEFT JOIN owners o ON j.owner_id = o.owner_id
                WHERE j.backend = ? AND j.session_id = ?
                  AND j.status = 'running' AND j.job_id != ?
                ORDER BY j.created_at ASC
                """,
                (backend, session_id, job_id),
            ).fetchall()

            for rrow in running_rows:
                running_alive = (
                    str(rrow["owner_id"]) == owner_id
                    or _is_owner_row_alive(
                        rrow["owner_pid"],
                        rrow["owner_exited_at"],
                        rrow["owner_process_identity"],
                        is_pid_alive_fn=is_pid_alive_fn,
                        process_identity_fn=process_identity_fn,
                    )
                )
                if running_alive:
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO session_claims (
                            backend, session_id, job_id, owner_id, claimed_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (backend, session_id, str(rrow["job_id"]), str(rrow["owner_id"]), now),
                    )
                    return "busy", str(rrow["job_id"]), None
                _recover_job_row_cur(cur, rrow, now)
                if rrow["owner_pid"] is not None and rrow["owner_exited_at"] is None:
                    cur.execute(
                        "UPDATE owners SET exited_at = ? WHERE owner_id = ? AND exited_at IS NULL",
                        (now, str(rrow["owner_id"])),
                    )

            # Durable safety gate (independent of jobs pruning AND reconciled against older writers!):
            safety = _reconcile_session_safety_cur(cur, backend, session_id, persist=True)
            if safety is not None:
                blocking_id, blocking_state = safety
                if blocking_id != job_id and confirm_prior_job_id != blocking_id:
                    return "blocked", blocking_id, blocking_state

            cur.execute(
                """
                INSERT OR REPLACE INTO session_claims (
                    backend, session_id, job_id, owner_id, claimed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (backend, session_id, job_id, owner_id, now),
            )
            cur.execute(
                "UPDATE owners SET last_heartbeat = ? WHERE owner_id = ?",
                (now, owner_id),
            )
            return "claimed", None, None

    def release_session_claims(self, job_id: str) -> None:
        with self._tx(write=True) as cur:
            cur.execute("DELETE FROM session_claims WHERE job_id = ?", (job_id,))

    def get_latest_session_context(
        self,
        backend: str,
        session_id: str,
        *,
        resume_from_job_id: str | None = None,
        exclude_job_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return latest committed cwd, worktree, model intent, effort, and parent_run from an executed turn."""
        with self._tx(write=False) as cur:
            if exclude_job_id:
                rows = cur.execute(
                    """
                    SELECT cwd, effective_cwd, worktree_path, worktree_branch, worktree_commit,
                           requested_model, model, quota_pool, effort, parent_run
                    FROM jobs
                    WHERE backend = ? AND session_id = ?
                      AND provider_invoked = 1
                      AND status IN ('completed', 'failed', 'cancelled', 'timed_out')
                      AND job_id != ?
                    ORDER BY COALESCE(finished_at, created_at) DESC, rowid DESC
                    LIMIT 10
                    """,
                    (backend, session_id, exclude_job_id),
                ).fetchall()
            else:
                rows = cur.execute(
                    """
                    SELECT cwd, effective_cwd, worktree_path, worktree_branch, worktree_commit,
                           requested_model, model, quota_pool, effort, parent_run
                    FROM jobs
                    WHERE backend = ? AND session_id = ?
                      AND provider_invoked = 1
                      AND status IN ('completed', 'failed', 'cancelled', 'timed_out')
                    ORDER BY COALESCE(finished_at, created_at) DESC, rowid DESC
                    LIMIT 10
                    """,
                    (backend, session_id),
                ).fetchall()

            chosen: sqlite3.Row | None = rows[0] if rows else None

            if chosen is None and resume_from_job_id:
                chosen = cur.execute(
                    """
                    SELECT cwd, effective_cwd, worktree_path, worktree_branch, worktree_commit,
                           requested_model, model, quota_pool, effort, parent_run
                    FROM jobs
                    WHERE job_id = ?
                    """,
                    (resume_from_job_id,),
                ).fetchone()

            if chosen is None:
                return None
            return {
                "cwd": chosen["cwd"],
                "effective_cwd": chosen["effective_cwd"] or chosen["cwd"],
                "worktree_path": chosen["worktree_path"],
                "worktree_branch": chosen["worktree_branch"],
                "worktree_commit": chosen["worktree_commit"],
                "requested_model": chosen["requested_model"],
                "model": chosen["model"],
                "quota_pool": chosen["quota_pool"] or classify_quota_pool(
                    backend,
                    chosen["model"] or chosen["requested_model"],
                ),
                "effort": chosen["effort"],
                "parent_run": chosen["parent_run"],
            }

    # --- Per-job CRUD ---

    def insert_job(self, record: JobRecord) -> None:
        pool = record.quota_pool or classify_quota_pool(
            record.backend,
            record.model or record.requested_model,
        )
        record.quota_pool = pool
        if record.last_activity is not None and record.last_activity_at is None:
            record.last_activity_at = time.time()
        with self._tx(write=True) as cur:
            cur.execute(
                """
                INSERT INTO jobs (
                    job_id, owner_id, backend, quota_pool, cwd, effective_cwd, mode,
                    requested_model, model, effort, model_notice, prompt_preview,
                    label, parent_run, worktree, worktree_path, worktree_branch,
                    worktree_commit, requested_session_id, session_id, status,
                    failure_reason, provider_state, retry_guidance, created_at,
                    started_at, finished_at, duration_seconds, response, usage_json,
                    provider_metrics_json, usage_status, exit_code, error, stderr_tail,
                    last_activity, provider_invoked, last_activity_at, model_source
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    record.job_id,
                    record.owner_id,
                    record.backend,
                    pool,
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
                    1 if record.provider_invoked else 0,
                    record.last_activity_at,
                    record.model_source,
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
        backend: str | None = None,
        status: str | None = None,
        started_at: float | None = None,
        session_id: str | None = None,
        model: str | None = None,
        model_source: str | None = None,
        quota_pool: str | None = None,
        effective_cwd: str | None = None,
        worktree_path: str | None = None,
        worktree_branch: str | None = None,
        worktree_commit: str | None = None,
        last_activity: str | None = None,
        last_activity_at: float | None = None,
        provider_invoked: bool | None = None,
        lock_timeout: float = 2.0,
        is_pid_alive_fn: Callable[[int], bool] | None = None,
        process_identity_fn: Callable[[int], str | None] | None = None,
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
        if model is not None or model_source is not None:
            sets.append("model_source = ?")
            vals.append(model_source)
        if quota_pool is not None:
            sets.append("quota_pool = ?")
            vals.append(quota_pool)
        if effective_cwd is not None:
            sets.append("effective_cwd = ?")
            vals.append(effective_cwd)
        if worktree_path is not None:
            sets.append("worktree_path = ?")
            vals.append(worktree_path)
        if worktree_branch is not None:
            sets.append("worktree_branch = ?")
            vals.append(worktree_branch)
        if worktree_commit is not None:
            sets.append("worktree_commit = ?")
            vals.append(worktree_commit)
        if last_activity is not None:
            sets.extend(("last_activity = ?", "last_activity_at = ?"))
            vals.extend((last_activity, last_activity_at if last_activity_at is not None else time.time()))
        if provider_invoked is not None:
            sets.append("provider_invoked = ?")
            vals.append(1 if provider_invoked else 0)
        if not sets:
            return
        vals.append(job_id)
        now = time.time()
        with self._tx(write=True, lock_timeout=lock_timeout) as cur:
            if session_id is not None:
                # A session another job holds and has not ended is refused before anything is written:
                # the error rolls this transaction back, and the caller stops its run.
                claim_backend = backend
                if not claim_backend:
                    brow = cur.execute("SELECT backend FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                    claim_backend = str(brow["backend"]) if brow is not None else None
                if claim_backend:
                    # The claim decision a resume gets: a holder whose window has died is recovered, not
                    # obeyed. With no pid check given, any holder that has not ended counts as live.
                    holder = _live_session_holder_cur(
                        cur,
                        claim_backend,
                        session_id,
                        job_id=job_id,
                        owner_id=owner_id,
                        now=now,
                        is_pid_alive_fn=is_pid_alive_fn,
                        process_identity_fn=process_identity_fn,
                    )
                    if holder is not None:
                        raise SessionHeldError(backend=claim_backend, session_id=session_id, holder_job_id=holder)
            cur.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ? "
                "AND status NOT IN ('completed', 'failed', 'cancelled', 'timed_out')",
                vals,
            )
            # Atomically claim a newly discovered session_id in the SAME transaction that publishes it!
            # Only claim if the job row is still non-terminal (`cur.rowcount > 0`).
            if session_id is not None and cur.rowcount > 0:
                resolved_backend = backend
                if not resolved_backend:
                    row = cur.execute(
                        "SELECT backend, status FROM jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    if row is not None and str(row["status"]) not in TERMINAL_STATUSES:
                        resolved_backend = str(row["backend"])
                if resolved_backend:
                    existing = cur.execute(
                        "SELECT job_id FROM session_claims WHERE backend = ? AND session_id = ?",
                        (resolved_backend, session_id),
                    ).fetchone()
                    if existing is None:
                        cur.execute(
                            """
                            INSERT INTO session_claims (
                                backend, session_id, job_id, owner_id, claimed_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (resolved_backend, session_id, job_id, owner_id, now),
                        )
                    else:
                        ex_job = cur.execute(
                            "SELECT status FROM jobs WHERE job_id = ?",
                            (str(existing["job_id"]),),
                        ).fetchone()
                        if ex_job is None or str(ex_job["status"]) in TERMINAL_STATUSES:
                            cur.execute(
                                """
                                INSERT OR REPLACE INTO session_claims (
                                    backend, session_id, job_id, owner_id, claimed_at
                                ) VALUES (?, ?, ?, ?, ?)
                                """,
                                (resolved_backend, session_id, job_id, owner_id, now),
                            )
            cur.execute(
                "UPDATE owners SET last_heartbeat = ? WHERE owner_id = ?",
                (now, owner_id),
            )

    def finalize_job(
        self,
        record: JobRecord,
        *,
        watermark_cumulative: dict[str, Any] | None = None,
        watermark_turns: int | None = None,
        touched_provider: bool | None = None,
        attempts: list[dict[str, Any]] | None = None,
        lock_timeout: float = 5.0,
        is_pid_alive_fn: Callable[[int], bool] | None = None,
        process_identity_fn: Callable[[int], str | None] | None = None,
    ) -> FinalizeOutcome:
        """Atomically persist terminal status, per-attempt usage, claim release, and safety state.

        Only the write that ends an active row stores a result and touches session state. Returns
        "written" for that; "kept" when the row had already ended, whatever this record says, and it is
        then left exactly as it is, usage, watermark and safety state included; or "missing" when there
        is no row (pruned), in which case the run's usage is recorded and its own claim released, and
        no session state is touched.
        """
        shared_record = record
        record = replace(record)
        now = time.time()
        pool = record.quota_pool or classify_quota_pool(
            record.backend,
            record.model or record.requested_model,
        )
        record.quota_pool = pool
        invoked = (
            bool(touched_provider)
            if touched_provider is not None
            else (record.provider_invoked or record.started_at is not None)
        )
        record.provider_invoked = invoked
        att_list = attempts if attempts and len(attempts) > 1 else record.attempts
        if att_list and len(att_list) > 1:
            record.attempts = att_list
            pm = dict(record.provider_metrics) if isinstance(record.provider_metrics, dict) else {}
            pm["attempts"] = att_list
            record.provider_metrics = pm
        with self._tx(write=True, lock_timeout=lock_timeout) as cur:
            current = cur.execute(
                "SELECT status, failure_reason FROM jobs WHERE job_id = ?", (record.job_id,)
            ).fetchone()
            if current is not None and str(current["status"]) in TERMINAL_STATUSES:
                # A late writer never overwrites a result that is already stored, even with the same
                # status: its reply, usage or checkpoint may be older than the stored ones. A retried
                # write of the stored result itself lands here too, which is as it should be.
                same = (str(current["status"]), current["failure_reason"]) == (record.status, record.failure_reason)
                (logger.info if same else logger.warning)(
                    "Job %s had already ended %s (%s); a later %s (%s) result is not written over it.",
                    record.job_id,
                    current["status"],
                    current["failure_reason"],
                    record.status,
                    record.failure_reason,
                )
                cur.execute("DELETE FROM session_claims WHERE job_id = ?", (record.job_id,))
                return "kept"
            if current is None:
                # No active row is being ended, so no session state is touched; the run's usage still goes
                # in the ledger, and its own claim is released.
                logger.warning(
                    "Job %s has no row to store its result in; only its usage is recorded.", record.job_id
                )
                _insert_usage_event_cur(cur, record, attempts=att_list, keep_existing=True)
                cur.execute("DELETE FROM session_claims WHERE job_id = ?", (record.job_id,))
                return "missing"

            # A session the CLI revealed (not the one this turn asked for and was let into) that another live
            # job holds: two turns ran on one session, which is a failure, whatever the run said. The row
            # keeps the session it had, and that session's watermark and safety state are left alone. This
            # is the same decision as while the job ran, for when that check had not finished in time.
            revealed = bool(record.session_id) and record.session_id != record.requested_session_id
            if revealed:
                holder = _live_session_holder_cur(
                    cur,
                    record.backend,
                    str(record.session_id),
                    job_id=record.job_id,
                    owner_id=record.owner_id,
                    now=now,
                    is_pid_alive_fn=is_pid_alive_fn,
                    process_identity_fn=process_identity_fn,
                )
                if holder is not None:
                    logger.warning(
                        "Job %s reported session %s, which job %s holds; it is stored failed, without that session.",
                        record.job_id,
                        record.session_id,
                        holder,
                    )
                    ended = f"{record.status}" + (f", {record.failure_reason}" if record.failure_reason else "")
                    held_sid = str(record.session_id)
                    record.status = "failed"
                    record.failure_reason = "cli_error"
                    record.error = f"{session_held_error_text(held_sid, holder)} The run itself had ended {ended}."
                    record.retry_guidance = session_held_guidance(held_sid, holder)
                    row_sid = cur.execute(
                        "SELECT session_id FROM jobs WHERE job_id = ?", (record.job_id,)
                    ).fetchone()
                    record.session_id = row_sid["session_id"] if row_sid is not None else None

            record.last_activity = record.status
            cur.execute(
                """
                UPDATE jobs SET
                    quota_pool = ?, effective_cwd = ?, model = ?, effort = ?, model_notice = ?,
                    worktree_path = ?, worktree_branch = ?, worktree_commit = ?,
                    session_id = ?, status = ?, failure_reason = ?, provider_state = ?,
                    retry_guidance = ?, started_at = ?, finished_at = ?, duration_seconds = ?,
                    cleanup_seconds = ?, response = ?, usage_json = ?, provider_metrics_json = ?,
                    usage_status = ?, exit_code = ?, error = ?, stderr_tail = ?, last_activity = ?,
                    provider_invoked = ?, last_activity_at = ?, model_source = ?, response_chars = ?
                WHERE job_id = ?
                """,
                (
                    pool,
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
                    record.cleanup_seconds,
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
                    1 if invoked else 0,
                    (record.last_activity_at if record.last_activity_at is not None else now)
                    if record.last_activity is not None else None,
                    record.model_source,
                    len(record.response),
                    record.job_id,
                ),
            )
            _insert_usage_event_cur(cur, record, attempts=att_list)

            # Watermark hygiene for Neon sessions: ONLY touch the checkpoint if the provider was actually
            # invoked! Pre-launch cancellations/timeouts never executed on the session and must preserve
            # the prior turn's valid checkpoint.
            if invoked and record.backend == "neon" and record.session_id:
                if (
                    record.status == "completed"
                    and watermark_cumulative is not None
                    and isinstance(watermark_turns, int)
                    and not isinstance(watermark_turns, bool)
                    and watermark_turns >= 1
                ):
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO session_usage_watermarks (
                            backend, session_id, cumulative_json, num_turns, last_job_id, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.backend,
                            record.session_id,
                            json.dumps(watermark_cumulative, separators=(",", ":")),
                            int(watermark_turns),
                            record.job_id,
                            now,
                        ),
                    )
                else:
                    cur.execute(
                        "DELETE FROM session_usage_watermarks WHERE backend = ? AND session_id = ?",
                        (record.backend, record.session_id),
                    )

            # Durable session safety state (survives job-history pruning!):
            # Record uncertain provider state per session; clear only once an invoked turn cleanly stops.
            if invoked and record.session_id:
                if record.provider_state in {"possibly_running", "unknown"}:
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO session_safety_state (
                            backend, session_id, blocking_job_id, provider_state, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            record.backend,
                            record.session_id,
                            record.job_id,
                            str(record.provider_state),
                            record.finished_at or now,
                        ),
                    )
                elif record.session_id == record.requested_session_id:
                    # The turn was let into this session past its block (confirmed), and has stopped.
                    cur.execute(
                        "DELETE FROM session_safety_state WHERE backend = ? AND session_id = ?",
                        (record.backend, record.session_id),
                    )
                else:
                    # A session the CLI revealed never passed its block, so another job's block stays (such as
                    # the `unknown` one a recovered holder leaves); only this job's own is cleared.
                    cur.execute(
                        "DELETE FROM session_safety_state WHERE backend = ? AND session_id = ? AND blocking_job_id = ?",
                        (record.backend, record.session_id, record.job_id),
                    )

            # Atomically release any session claim held by this job right as terminal state commits.
            cur.execute("DELETE FROM session_claims WHERE job_id = ?", (record.job_id,))

            cur.execute(
                "UPDATE owners SET last_heartbeat = ? WHERE owner_id = ?",
                (now, record.owner_id),
            )

            # Keep the newest max_stored_jobs FINISHED jobs. Queued and running jobs are never pruned, and do
            # not count against the cap, so a burst of active jobs cannot push finished results out.
            cur.execute(
                """
                DELETE FROM jobs
                WHERE status IN ('completed', 'failed', 'cancelled', 'timed_out')
                  AND job_id != ?
                  AND job_id NOT IN (
                      SELECT job_id FROM jobs
                      WHERE status IN ('completed', 'failed', 'cancelled', 'timed_out')
                      ORDER BY MAX(created_at, COALESCE(finished_at, 0)) DESC
                      LIMIT ?
                  )
                """,
                (record.job_id, self.max_stored_jobs),
            )

        shared_record.quota_pool = record.quota_pool
        shared_record.provider_invoked = record.provider_invoked
        shared_record.attempts = record.attempts
        shared_record.provider_metrics = record.provider_metrics
        shared_record.status = record.status
        shared_record.failure_reason = record.failure_reason
        shared_record.error = record.error
        shared_record.retry_guidance = record.retry_guidance
        shared_record.session_id = record.session_id
        shared_record.last_activity = record.last_activity
        return "written"

    def delete_aborted_queued_job(self, job_id: str, *, lock_timeout: float = 2.0) -> bool:
        """Remove a queued job row whose submission was cancelled before execution started."""
        with self._tx(write=True, lock_timeout=lock_timeout) as cur:
            cur.execute("DELETE FROM session_claims WHERE job_id = ?", (job_id,))
            cur.execute(
                """
                DELETE FROM jobs
                WHERE job_id = ?
                  AND status = 'queued'
                  AND started_at IS NULL
                  AND provider_invoked = 0
                """,
                (job_id,),
            )
            return cur.rowcount > 0

    def record_attempt_usage(
        self,
        record: JobRecord,
        attempt: dict[str, Any],
        *,
        attempt_number: int,
        is_final: bool = False,
        lock_timeout: float = 2.0,
    ) -> None:
        """Persist one executed attempt's usage/duration immediately before retry backoff or next attempt."""
        now_ts = time.time()
        att_model = attempt.get("model") or record.model or record.requested_model or f"{record.backend}-default"
        att_pool = attempt.get("quota_pool") or classify_quota_pool(record.backend, att_model)
        with self._tx(write=True, lock_timeout=lock_timeout) as cur:
            _insert_usage_event_row_cur(
                cur,
                job_id=record.job_id,
                attempt_number=attempt_number,
                is_final_attempt=is_final,
                owner_id=record.owner_id,
                now_ts=now_ts,
                backend=record.backend,
                quota_pool=str(att_pool),
                model_name=str(att_model),
                effort=attempt.get("effort") or record.effort,
                mode=record.mode,
                status=str(attempt.get("status") or "failed"),
                duration_seconds=float(attempt.get("duration_seconds") or 0.0),
                cwd=record.cwd,
                parent_run=record.parent_run,
                label=record.label,
                session_id=record.session_id,
                usage_status=str(attempt.get("usage_status") or "missing"),
                usage=attempt.get("usage") if isinstance(attempt.get("usage"), dict) else None,
                provider_metrics=attempt.get("provider_metrics")
                if isinstance(attempt.get("provider_metrics"), dict)
                else None,
            )

    def get_job(self, job_id: str) -> JobRecord | None:
        return self.peek_job(job_id)

    def peek_job(self, job_id: str) -> JobRecord | None:
        """Read the committed row through a connection opened read-only."""
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
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
        """Return the currently claimed or running job for `(backend, session_id)`, if any."""
        with self._tx(write=False) as cur:
            claim = cur.execute(
                "SELECT job_id FROM session_claims WHERE backend = ? AND session_id = ?",
                (backend, session_id),
            ).fetchone()
            if claim is not None and str(claim["job_id"]) != (exclude_job_id or ""):
                row = cur.execute(
                    "SELECT * FROM jobs WHERE job_id = ? AND status IN ('queued', 'running')",
                    (str(claim["job_id"]),),
                ).fetchone()
                if row is not None:
                    return _row_to_job(row)

            if exclude_job_id:
                row = cur.execute(
                    """
                    SELECT * FROM jobs
                    WHERE backend = ? AND session_id = ?
                      AND status = 'running'
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
                      AND status = 'running'
                    ORDER BY created_at ASC LIMIT 1
                    """,
                    (backend, session_id),
                ).fetchone()
            return _row_to_job(row) if row is not None else None

    @staticmethod
    def _count_in_flight_cur(
        cur: sqlite3.Cursor,
        *,
        backend: str | None = None,
        quota_pool: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
    ) -> tuple[dict[str, int], dict[str, int]]:
        clauses = ["status IN ('queued', 'running')"]
        vals: list[Any] = []
        if backend:
            clauses.append("backend = ?")
            vals.append(backend)
        if quota_pool:
            clauses.append("COALESCE(quota_pool, CASE WHEN backend = 'codex' THEN 'codex' WHEN backend = 'claude' THEN 'claude' ELSE 'agy_gemini' END) = ?")
            vals.append(quota_pool)
        if cwd:
            clauses.append("cwd = ?")
            vals.append(cwd)
        if parent_run:
            clauses.append("parent_run = ?")
            vals.append(parent_run)
        where = f"WHERE {' AND '.join(clauses)}"
        rows = cur.execute(
            f"""
            SELECT backend,
                   COALESCE(quota_pool, CASE WHEN backend = 'codex' THEN 'codex' WHEN backend = 'claude' THEN 'claude' ELSE 'agy_gemini' END) AS pool,
                   COUNT(*) AS cnt
            FROM jobs
            {where}
            GROUP BY backend, pool
            """,
            vals,
        ).fetchall()
        by_backend: dict[str, int] = {}
        by_pool: dict[str, int] = {}
        for r in rows:
            b = str(r["backend"])
            p = str(r["pool"])
            c = int(r["cnt"])
            by_backend[b] = by_backend.get(b, 0) + c
            by_pool[p] = by_pool.get(p, 0) + c
        return by_backend, by_pool

    def count_in_flight(
        self,
        *,
        backend: str | None = None,
        quota_pool: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
    ) -> dict[str, int]:
        with self._tx(write=False) as cur:
            by_backend, _ = self._count_in_flight_cur(
                cur,
                backend=backend,
                quota_pool=quota_pool,
                cwd=cwd,
                parent_run=parent_run,
            )
            return by_backend

    def get_session_watermark(self, backend: str, session_id: str) -> dict[str, Any] | None:
        with self._tx(write=False) as cur:
            row = cur.execute(
                """
                SELECT cumulative_json, num_turns, last_job_id, updated_at
                FROM session_usage_watermarks
                WHERE backend = ? AND session_id = ?
                """,
                (backend, session_id),
            ).fetchone()
            if row is None:
                return None
            cumulative = _loads_opt(row["cumulative_json"])
            if cumulative is None or row["num_turns"] is None or int(row["num_turns"]) < 1:
                return None
            return {
                "cumulative": cumulative,
                "num_turns": int(row["num_turns"]),
                "last_job_id": row["last_job_id"],
                "updated_at": float(row["updated_at"]),
            }

    # --- Usage aggregation (direct SQL GROUP BY) ---

    def summarize_usage(
        self,
        *,
        period: UsagePeriod = "today",
        owner_id: str,
        backend: str | None = None,
        quota_pool: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
        since_ts: float | None = None,
        until_ts: float | None = None,
    ) -> dict[str, Any]:
        """Runs, outcomes, time and counted tokens over the usage records, per backend and per quota pool.

        `since_ts` (inclusive) and `until_ts` (exclusive) bound the records by when they were written,
        on top of `period`.
        """
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
        if since_ts is not None:
            clauses.append("ts >= ?")
            vals.append(since_ts)
        if until_ts is not None:
            clauses.append("ts < ?")
            vals.append(until_ts)
        if backend:
            clauses.append("backend = ?")
            vals.append(backend)
        if quota_pool:
            clauses.append(
                "COALESCE(quota_pool, CASE WHEN backend = 'codex' THEN 'codex' WHEN backend = 'claude' THEN 'claude' ELSE 'agy_gemini' END) = ?"
            )
            vals.append(quota_pool)
        if cwd:
            clauses.append("cwd = ?")
            vals.append(cwd)
        if parent_run:
            clauses.append("parent_run = ?")
            vals.append(parent_run)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        pool_expr = "COALESCE(quota_pool, CASE WHEN backend = 'codex' THEN 'codex' WHEN backend = 'claude' THEN 'claude' ELSE 'agy_gemini' END)"

        with self._tx(write=False) as cur:
            in_flight, in_flight_by_pool = self._count_in_flight_cur(
                cur,
                backend=backend,
                quota_pool=quota_pool,
                cwd=cwd,
                parent_run=parent_run,
            )
            backend_rows = cur.execute(
                f"""
                SELECT
                    backend,
                    COUNT(DISTINCT job_id) AS runs,
                    COUNT(*) AS attempts,
                    SUM(CASE WHEN is_final_attempt = 1 AND status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN is_final_attempt = 1 AND status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN is_final_attempt = 1 AND status = 'timed_out' THEN 1 ELSE 0 END) AS timed_out,
                    SUM(CASE WHEN is_final_attempt = 1 AND status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                    ROUND(COALESCE(SUM(duration_seconds), 0.0), 2) AS duration_seconds,
                    SUM(CASE WHEN usage_status = 'exact' THEN 1 ELSE 0 END) AS cov_exact,
                    SUM(CASE WHEN usage_status = 'delta' THEN 1 ELSE 0 END) AS cov_delta,
                    SUM(CASE WHEN usage_status = 'partial' THEN 1 ELSE 0 END) AS cov_partial,
                    SUM(CASE WHEN usage_status NOT IN ('exact', 'delta', 'partial') THEN 1 ELSE 0 END) AS cov_missing,
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(input_tokens, 0) ELSE 0 END) AS input_tokens,
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(output_tokens, 0) ELSE 0 END) AS output_tokens,
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(cached_tokens, 0) ELSE 0 END) AS cached_tokens,
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(cache_write_tokens, 0) ELSE 0 END) AS cache_write_tokens,
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(reasoning_tokens, 0) ELSE 0 END) AS reasoning_tokens,
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(total_tokens, 0) ELSE 0 END) AS total_tokens,
                    SUM(CASE WHEN usage_status = 'partial' THEN COALESCE(total_tokens, 0) ELSE 0 END) AS partial_total_tokens
                FROM usage_events
                {where}
                GROUP BY backend
                """,
                vals,
            ).fetchall()

            pool_rows = cur.execute(
                f"""
                SELECT
                    {pool_expr} AS pool,
                    COUNT(DISTINCT job_id) AS runs,
                    COUNT(*) AS attempts,
                    SUM(CASE WHEN is_final_attempt = 1 AND status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN is_final_attempt = 1 AND status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN is_final_attempt = 1 AND status = 'timed_out' THEN 1 ELSE 0 END) AS timed_out,
                    SUM(CASE WHEN is_final_attempt = 1 AND status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                    ROUND(COALESCE(SUM(duration_seconds), 0.0), 2) AS duration_seconds,
                    SUM(CASE WHEN usage_status = 'exact' THEN 1 ELSE 0 END) AS cov_exact,
                    SUM(CASE WHEN usage_status = 'delta' THEN 1 ELSE 0 END) AS cov_delta,
                    SUM(CASE WHEN usage_status = 'partial' THEN 1 ELSE 0 END) AS cov_partial,
                    SUM(CASE WHEN usage_status NOT IN ('exact', 'delta', 'partial') THEN 1 ELSE 0 END) AS cov_missing,
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(input_tokens, 0) ELSE 0 END) AS input_tokens,
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(output_tokens, 0) ELSE 0 END) AS output_tokens,
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(cached_tokens, 0) ELSE 0 END) AS cached_tokens,
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(cache_write_tokens, 0) ELSE 0 END) AS cache_write_tokens,
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(reasoning_tokens, 0) ELSE 0 END) AS reasoning_tokens,
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(total_tokens, 0) ELSE 0 END) AS total_tokens,
                    SUM(CASE WHEN usage_status = 'partial' THEN COALESCE(total_tokens, 0) ELSE 0 END) AS partial_total_tokens
                FROM usage_events
                {where}
                GROUP BY pool
                """,
                vals,
            ).fetchall()

        def _empty_bucket(in_flight_cnt: int) -> dict[str, Any]:
            return {
                "runs": 0,
                "attempts": 0,
                "completed": 0,
                "failed": 0,
                "timed_out": 0,
                "cancelled": 0,
                "in_flight": in_flight_cnt,
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

        def _row_to_bucket(r: sqlite3.Row, in_flight_cnt: int) -> dict[str, Any]:
            bucket: dict[str, Any] = {
                "runs": int(r["runs"] or 0),
                "attempts": int(r["attempts"] or 0),
                "completed": int(r["completed"] or 0),
                "failed": int(r["failed"] or 0),
                "timed_out": int(r["timed_out"] or 0),
                "cancelled": int(r["cancelled"] or 0),
                "in_flight": in_flight_cnt,
                "duration_seconds": round(float(r["duration_seconds"] or 0.0), 2),
                "tokens": {
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cached_tokens": int(r["cached_tokens"] or 0),
                    "cache_write_tokens": int(r["cache_write_tokens"] or 0),
                    "reasoning_tokens": int(r["reasoning_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                },
                "usage_coverage": {
                    "exact": int(r["cov_exact"] or 0),
                    "delta": int(r["cov_delta"] or 0),
                    "partial": int(r["cov_partial"] or 0),
                    "missing": int(r["cov_missing"] or 0),
                },
            }
            partial_total = int(r["partial_total_tokens"] or 0)
            if partial_total > 0:
                bucket["partial_total_tokens"] = partial_total
            return bucket

        overall_runs = 0
        overall_attempts = 0
        overall_completed = 0
        overall_failed = 0
        overall_timed_out = 0
        overall_cancelled = 0
        overall_duration = 0.0
        overall_coverage = {"exact": 0, "delta": 0, "partial": 0, "missing": 0}
        by_backend: dict[str, dict[str, Any]] = {}
        by_quota_pool: dict[str, dict[str, Any]] = {}

        for bname, cnt in in_flight.items():
            if cnt > 0:
                by_backend.setdefault(bname, _empty_bucket(cnt))
        for pname, cnt in in_flight_by_pool.items():
            if cnt > 0:
                by_quota_pool.setdefault(pname, _empty_bucket(cnt))

        for r in backend_rows:
            bname = str(r["backend"])
            bucket = _row_to_bucket(r, in_flight.get(bname, 0))
            by_backend[bname] = bucket

            overall_runs += bucket["runs"]
            overall_attempts += bucket["attempts"]
            overall_completed += bucket["completed"]
            overall_failed += bucket["failed"]
            overall_timed_out += bucket["timed_out"]
            overall_cancelled += bucket["cancelled"]
            overall_duration = round(overall_duration + bucket["duration_seconds"], 2)
            for k in overall_coverage:
                overall_coverage[k] += bucket["usage_coverage"][k]

        for pr in pool_rows:
            pname = str(pr["pool"])
            by_quota_pool[pname] = _row_to_bucket(pr, in_flight_by_pool.get(pname, 0))

        scope_info: dict[str, Any] = {"period": period}
        if backend:
            scope_info["backend"] = backend
        if quota_pool:
            scope_info["quota_pool"] = quota_pool
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
                "attempts": overall_attempts,
                "completed": overall_completed,
                "failed": overall_failed,
                "timed_out": overall_timed_out,
                "cancelled": overall_cancelled,
                "in_flight": sum(in_flight.values()),
                "duration_seconds": overall_duration,
                "usage_coverage": overall_coverage,
            },
            "by_backend": by_backend,
            "by_quota_pool": by_quota_pool,
        }

        return summary

    def dashboard_records(
        self,
        *,
        since_ts: float | None,
        until_ts: float | None,
        session_owner_id: str,
        only_session: bool,
        is_pid_alive_fn: Callable[[int], bool],
        process_identity_fn: Callable[[int], str | None] | None = None,
    ) -> dict[str, Any]:
        """Every record the usage dashboard reads, from one snapshot so that its figures agree. It writes nothing.

        Each usage row comes with what its job row says, when the job row is still kept: whether the job
        is still running, whether its CLI was started, and why it failed. Pruned job rows read as NULL.
        Each queued or running job says whether the window that owns it is still alive, judged by the same
        check that `recover_dead_owner_jobs` uses, but without marking anything.
        """
        clauses: list[str] = []
        vals: list[Any] = []
        if since_ts is not None:
            clauses.append("u.ts >= ?")
            vals.append(since_ts)
        if until_ts is not None:
            clauses.append("u.ts < ?")
            vals.append(until_ts)
        if only_session:
            clauses.append("u.owner_id = ?")
            vals.append(session_owner_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        running_where = "WHERE j.status IN ('queued', 'running')" + (" AND j.owner_id = ?" if only_session else "")
        running_vals: list[Any] = [session_owner_id] if only_session else []

        with self._tx(write=False) as cur:
            # One read transaction: in WAL mode every SELECT below sees the same committed state.
            cur.execute("BEGIN")
            usage_rows = cur.execute(
                f"""
                SELECT u.job_id, u.attempt_number, u.is_final_attempt, u.owner_id, u.ts, u.backend,
                       COALESCE(u.quota_pool, CASE WHEN u.backend = 'codex' THEN 'codex'
                                                   WHEN u.backend = 'claude' THEN 'claude'
                                                   ELSE 'agy_gemini' END) AS quota_pool,
                       COALESCE(NULLIF(u.model, ''), u.backend || '-default') AS model,
                       u.status, u.duration_seconds, u.usage_status,
                       u.input_tokens, u.output_tokens, u.cached_tokens, u.cache_write_tokens,
                       u.reasoning_tokens, u.total_tokens, u.provider_metrics_json,
                       j.job_id AS job_row_id, j.status AS job_status,
                       j.provider_invoked AS job_provider_invoked, j.failure_reason AS job_failure_reason,
                       j.provider_state AS job_provider_state
                FROM usage_events u
                LEFT JOIN jobs j ON j.job_id = u.job_id
                {where}
                ORDER BY u.ts ASC, u.id ASC
                """,
                vals,
            ).fetchall()
            running_rows = cur.execute(
                f"""
                SELECT j.job_id, j.owner_id, j.backend, j.quota_pool, j.model, j.requested_model, j.status,
                       j.created_at, j.started_at, j.last_activity, j.provider_invoked,
                       o.pid AS owner_pid, o.exited_at AS owner_exited_at,
                       o.process_identity AS owner_process_identity
                FROM jobs j
                LEFT JOIN owners o ON o.owner_id = j.owner_id
                {running_where}
                ORDER BY j.created_at ASC
                """,
                running_vals,
            ).fetchall()
            first = cur.execute("SELECT MIN(ts) AS first_ts FROM usage_events").fetchone()
            owner = cur.execute(
                "SELECT started_at FROM owners WHERE owner_id = ?",
                (session_owner_id,),
            ).fetchone()
            cur.execute("COMMIT")

        running_jobs: list[dict[str, Any]] = []
        for r in running_rows:
            job = dict(r)
            pid = job.pop("owner_pid")
            exited_at = job.pop("owner_exited_at")
            process_identity = job.pop("owner_process_identity")
            # The calling window is alive by definition. Any other: the check recovery uses, read only.
            job["owner_alive"] = job["owner_id"] == session_owner_id or _is_owner_row_alive(
                pid,
                exited_at,
                process_identity,
                is_pid_alive_fn=is_pid_alive_fn,
                process_identity_fn=process_identity_fn,
            )
            running_jobs.append(job)

        return {
            "usage_rows": [dict(r) for r in usage_rows],
            "running_jobs": running_jobs,
            "first_record_ts": float(first["first_ts"]) if first is not None and first["first_ts"] is not None else None,
            "session_started_at": float(owner["started_at"]) if owner is not None else None,
        }

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
