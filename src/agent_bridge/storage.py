"""Shared SQLite persistence for multi-window jobs, atomic session claims, SQL usage aggregation, and quota cache."""

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

from agent_bridge.config import classify_quota_pool
from agent_bridge.models import TERMINAL_STATUSES, JobRecord

logger = logging.getLogger("agent_bridge.storage")

UsagePeriod = Literal["session", "today", "7d", "30d", "all"]

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

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

CREATE TABLE IF NOT EXISTS session_claims (
    backend TEXT NOT NULL,
    session_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    PRIMARY KEY (backend, session_id)
);

CREATE INDEX IF NOT EXISTS idx_session_claims_job ON session_claims(job_id);
CREATE INDEX IF NOT EXISTS idx_session_claims_owner ON session_claims(owner_id);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,
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
    last_job_id TEXT,
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


def _is_owner_row_alive(
    pid: int | None,
    exited_at: float | None,
    *,
    is_pid_alive_fn: Callable[[int], bool],
) -> bool:
    """Single canonical liveness predicate used by status checks, cancellation, recovery, and session claims."""
    if exited_at is not None or pid is None:
        return False
    return bool(is_pid_alive_fn(int(pid)))


def _row_to_job(row: sqlite3.Row) -> JobRecord:
    keys = row.keys()
    raw_pool = row["quota_pool"] if "quota_pool" in keys else None
    pool = raw_pool or classify_quota_pool(row["backend"], row["model"] or row["requested_model"])
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
    model_name = record.model or record.requested_model or f"{record.backend}-default"
    quota_pool = record.quota_pool or classify_quota_pool(record.backend, model_name)

    def _opt_int(key: str) -> int | None:
        v = usage.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    cur.execute(
        """
        INSERT OR REPLACE INTO usage_events (
            job_id, owner_id, ts, iso_time, backend, quota_pool, model, effort, mode, status,
            duration_seconds, cwd, parent_run, label, session_id, usage_status,
            input_tokens, output_tokens, cached_tokens, cache_write_tokens,
            reasoning_tokens, total_tokens, provider_metrics_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.job_id,
            record.owner_id,
            round(now_ts, 3),
            iso_time,
            record.backend,
            quota_pool,
            model_name,
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


def _recover_job_row_cur(cur: sqlite3.Cursor, row: sqlite3.Row, now: float) -> bool:
    """Mark one dead-owner job failed, record usage, release its session claim, and invalidate watermarks."""
    rec = _row_to_job(row)
    rec.status = "failed"
    rec.failure_reason = "owner_exited"
    rec.provider_state = "unknown"
    rec.error = f"Owning bridge process ({rec.owner_id}) exited before job completed."
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
    changed = cur.rowcount > 0
    cur.execute("DELETE FROM session_claims WHERE job_id = ?", (rec.job_id,))
    if changed:
        _insert_usage_event_cur(cur, rec)
        if rec.backend == "neon" and rec.session_id:
            cur.execute(
                "DELETE FROM session_usage_watermarks WHERE backend = ? AND session_id = ?",
                (rec.backend, rec.session_id),
            )
    return changed


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
            # Lightweight idempotent column migrations for existing databases.
            wm_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(session_usage_watermarks)").fetchall()
            }
            if "last_job_id" not in wm_cols:
                conn.execute("ALTER TABLE session_usage_watermarks ADD COLUMN last_job_id TEXT")

            job_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "quota_pool" not in job_cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN quota_pool TEXT")
                conn.execute(
                    """
                    UPDATE jobs SET quota_pool = CASE
                        WHEN backend = 'codex' THEN 'codex'
                        WHEN LOWER(COALESCE(model, requested_model, '')) LIKE '%claude%'
                          OR LOWER(COALESCE(model, requested_model, '')) LIKE '%gpt%'
                          OR LOWER(COALESCE(model, requested_model, '')) LIKE '%opus%'
                          OR LOWER(COALESCE(model, requested_model, '')) LIKE '%sonnet%'
                        THEN 'agy_claude_gpt'
                        ELSE 'agy_gemini'
                    END
                    WHERE quota_pool IS NULL
                    """
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_quota_pool ON jobs(quota_pool)")

            usage_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(usage_events)").fetchall()
            }
            if "quota_pool" not in usage_cols:
                conn.execute("ALTER TABLE usage_events ADD COLUMN quota_pool TEXT")
                conn.execute(
                    """
                    UPDATE usage_events SET quota_pool = CASE
                        WHEN backend = 'codex' THEN 'codex'
                        WHEN LOWER(COALESCE(model, '')) LIKE '%claude%'
                          OR LOWER(COALESCE(model, '')) LIKE '%gpt%'
                          OR LOWER(COALESCE(model, '')) LIKE '%opus%'
                          OR LOWER(COALESCE(model, '')) LIKE '%sonnet%'
                        THEN 'agy_claude_gpt'
                        ELSE 'agy_gemini'
                    END
                    WHERE quota_pool IS NULL
                    """
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_quota_pool ON usage_events(quota_pool)")
        finally:
            conn.close()

        self._import_legacy_files_if_needed()

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
                    last_activity
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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

            if not usage:
                usage_status = "missing"
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
            cur.execute("DELETE FROM session_claims WHERE owner_id = ?", (owner_id,))

    def is_owner_alive(
        self,
        owner_id: str,
        *,
        is_pid_alive_fn: Callable[[int], bool],
        stale_seconds: float = 30.0,
    ) -> bool:
        _ = stale_seconds  # Kept for API compatibility; liveness uses the unified OS PID check.
        with self._tx(write=False) as cur:
            row = cur.execute(
                "SELECT pid, exited_at FROM owners WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
            if row is None:
                return False
            return _is_owner_row_alive(
                row["pid"],
                row["exited_at"],
                is_pid_alive_fn=is_pid_alive_fn,
            )

    def recover_dead_owner_jobs(
        self,
        current_owner_id: str,
        *,
        is_pid_alive_fn: Callable[[int], bool],
        stale_seconds: float = 30.0,
    ) -> list[str]:
        """Recover only queued/running jobs whose owning bridge process has actually exited."""
        _ = stale_seconds
        recovered: list[str] = []
        now = time.time()
        with self._tx(write=True) as cur:
            rows = cur.execute(
                """
                SELECT j.*, o.pid AS owner_pid, o.exited_at AS owner_exited_at
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
                    is_pid_alive_fn=is_pid_alive_fn,
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

    def try_claim_session(
        self,
        backend: str,
        session_id: str,
        job_id: str,
        owner_id: str,
        *,
        is_pid_alive_fn: Callable[[int], bool],
    ) -> tuple[bool, str | None]:
        """Atomically claim `(backend, session_id)` for `job_id`.

        Returns `(True, None)` if claimed by `job_id`, or `(False, holder_job_id)` if another
        live job currently holds the session turn. Never deadlocks queued siblings.
        """
        now = time.time()
        with self._tx(write=True) as cur:
            claim = cur.execute(
                """
                SELECT job_id, owner_id FROM session_claims
                WHERE backend = ? AND session_id = ?
                """,
                (backend, session_id),
            ).fetchone()

            if claim is not None:
                holder_id = str(claim["job_id"])
                if holder_id == job_id:
                    return True, None

                holder_row = cur.execute(
                    """
                    SELECT j.*, o.pid AS owner_pid, o.exited_at AS owner_exited_at
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
                else:
                    holder_alive = (
                        str(holder_row["owner_id"]) == owner_id
                        or _is_owner_row_alive(
                            holder_row["owner_pid"],
                            holder_row["owner_exited_at"],
                            is_pid_alive_fn=is_pid_alive_fn,
                        )
                    )
                    if holder_alive:
                        return False, holder_id
                    # Dead owner held the claim: recover its job atomically and take over.
                    _recover_job_row_cur(cur, holder_row, now)
                    if holder_row["owner_pid"] is not None and holder_row["owner_exited_at"] is None:
                        cur.execute(
                            "UPDATE owners SET exited_at = ? WHERE owner_id = ? AND exited_at IS NULL",
                            (now, str(holder_row["owner_id"])),
                        )

            # Defensive check: ensure no active RUNNING job on `(backend, session_id)` lacks a claim row.
            running_rows = cur.execute(
                """
                SELECT j.*, o.pid AS owner_pid, o.exited_at AS owner_exited_at
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
                        is_pid_alive_fn=is_pid_alive_fn,
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
                    return False, str(rrow["job_id"])
                _recover_job_row_cur(cur, rrow, now)
                if rrow["owner_pid"] is not None and rrow["owner_exited_at"] is None:
                    cur.execute(
                        "UPDATE owners SET exited_at = ? WHERE owner_id = ? AND exited_at IS NULL",
                        (now, str(rrow["owner_id"])),
                    )

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
            return True, None

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
        """Return the latest committed working directory & worktree metadata for `(backend, session_id)`."""
        with self._tx(write=False) as cur:
            if exclude_job_id:
                row = cur.execute(
                    """
                    SELECT cwd, effective_cwd, worktree_path, worktree_branch, worktree_commit
                    FROM jobs
                    WHERE backend = ? AND session_id = ?
                      AND status IN ('completed', 'failed', 'cancelled', 'timed_out')
                      AND job_id != ?
                    ORDER BY COALESCE(finished_at, created_at) DESC
                    LIMIT 1
                    """,
                    (backend, session_id, exclude_job_id),
                ).fetchone()
            else:
                row = cur.execute(
                    """
                    SELECT cwd, effective_cwd, worktree_path, worktree_branch, worktree_commit
                    FROM jobs
                    WHERE backend = ? AND session_id = ?
                      AND status IN ('completed', 'failed', 'cancelled', 'timed_out')
                    ORDER BY COALESCE(finished_at, created_at) DESC
                    LIMIT 1
                    """,
                    (backend, session_id),
                ).fetchone()

            if row is None and resume_from_job_id:
                row = cur.execute(
                    """
                    SELECT cwd, effective_cwd, worktree_path, worktree_branch, worktree_commit
                    FROM jobs
                    WHERE job_id = ?
                    """,
                    (resume_from_job_id,),
                ).fetchone()

            if row is None:
                return None
            return {
                "cwd": row["cwd"],
                "effective_cwd": row["effective_cwd"] or row["cwd"],
                "worktree_path": row["worktree_path"],
                "worktree_branch": row["worktree_branch"],
                "worktree_commit": row["worktree_commit"],
            }

    # --- Per-job CRUD ---

    def insert_job(self, record: JobRecord) -> None:
        pool = record.quota_pool or classify_quota_pool(
            record.backend,
            record.model or record.requested_model,
        )
        record.quota_pool = pool
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
                    last_activity
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
        quota_pool: str | None = None,
        effective_cwd: str | None = None,
        worktree_path: str | None = None,
        worktree_branch: str | None = None,
        worktree_commit: str | None = None,
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
            sets.append("last_activity = ?")
            vals.append(last_activity)
        if not sets:
            return
        vals.append(job_id)
        now = time.time()
        with self._tx(write=True) as cur:
            cur.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ?", vals)
            # Atomically claim a newly discovered session_id in the SAME transaction that publishes it!
            if session_id is not None:
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
    ) -> None:
        """Atomically persist terminal status, usage event, session claim release, and watermark update/invalidation."""
        now = time.time()
        pool = record.quota_pool or classify_quota_pool(
            record.backend,
            record.model or record.requested_model,
        )
        record.quota_pool = pool
        with self._tx(write=True) as cur:
            cur.execute(
                """
                UPDATE jobs SET
                    quota_pool = ?, effective_cwd = ?, model = ?, effort = ?, model_notice = ?,
                    worktree_path = ?, worktree_branch = ?, worktree_commit = ?,
                    session_id = ?, status = ?, failure_reason = ?, provider_state = ?,
                    retry_guidance = ?, started_at = ?, finished_at = ?, duration_seconds = ?,
                    response = ?, usage_json = ?, provider_metrics_json = ?, usage_status = ?,
                    exit_code = ?, error = ?, stderr_tail = ?, last_activity = ?
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

            # Watermark hygiene for Neon sessions: update ONLY on a clean completed turn with
            # both cumulative usage and an integer turn count; otherwise INVALIDATE so a future
            # turn can never compute a bogus delta across a missing envelope or interrupted turn!
            if record.backend == "neon" and record.session_id:
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

            # Atomically release any session claim held by this job right as terminal state commits.
            cur.execute("DELETE FROM session_claims WHERE job_id = ?", (record.job_id,))

            cur.execute(
                "UPDATE owners SET last_heartbeat = ? WHERE owner_id = ?",
                (now, record.owner_id),
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
            clauses.append("COALESCE(quota_pool, CASE WHEN backend = 'codex' THEN 'codex' ELSE 'agy_gemini' END) = ?")
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
                   COALESCE(quota_pool, CASE WHEN backend = 'codex' THEN 'codex' ELSE 'agy_gemini' END) AS pool,
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

    def session_compact_summary(self, owner_id: str) -> dict[str, Any]:
        with self._tx(write=False) as cur:
            rows = cur.execute(
                """
                SELECT backend,
                       COUNT(*) AS runs,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                       ROUND(COALESCE(SUM(duration_seconds), 0.0), 2) AS duration_seconds,
                       SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(total_tokens, 0) ELSE 0 END) AS total_tokens,
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
        quota_pool: str | None = None,
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
        if quota_pool:
            clauses.append(
                "COALESCE(quota_pool, CASE WHEN backend = 'codex' THEN 'codex' ELSE 'agy_gemini' END) = ?"
            )
            vals.append(quota_pool)
        if cwd:
            clauses.append("cwd = ?")
            vals.append(cwd)
        if parent_run:
            clauses.append("parent_run = ?")
            vals.append(parent_run)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        recent_n = max(0, min(int(recent_limit), 50))
        pool_expr = "COALESCE(quota_pool, CASE WHEN backend = 'codex' THEN 'codex' ELSE 'agy_gemini' END)"

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
                    COUNT(*) AS runs,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status = 'timed_out' THEN 1 ELSE 0 END) AS timed_out,
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
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
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(total_tokens, 0) ELSE 0 END) AS total_tokens
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
                    COUNT(*) AS runs,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status = 'timed_out' THEN 1 ELSE 0 END) AS timed_out,
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
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
                    SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(total_tokens, 0) ELSE 0 END) AS total_tokens
                FROM usage_events
                {where}
                GROUP BY pool
                """,
                vals,
            ).fetchall()

            model_rows: list[sqlite3.Row] = []
            recent_rows: list[sqlite3.Row] = []
            if not compact:
                model_rows = cur.execute(
                    f"""
                    SELECT
                        backend,
                        COALESCE(NULLIF(model, ''), backend || '-default') AS model_name,
                        COUNT(*) AS runs,
                        SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                        ROUND(COALESCE(SUM(duration_seconds), 0.0), 2) AS duration_seconds,
                        SUM(CASE WHEN usage_status IN ('exact', 'delta') THEN COALESCE(total_tokens, 0) ELSE 0 END) AS total_tokens,
                        SUM(CASE WHEN usage_status = 'exact' THEN 1 ELSE 0 END) AS cov_exact,
                        SUM(CASE WHEN usage_status = 'delta' THEN 1 ELSE 0 END) AS cov_delta,
                        SUM(CASE WHEN usage_status = 'partial' THEN 1 ELSE 0 END) AS cov_partial,
                        SUM(CASE WHEN usage_status NOT IN ('exact', 'delta', 'partial') THEN 1 ELSE 0 END) AS cov_missing
                    FROM usage_events
                    {where}
                    GROUP BY backend, model_name
                    """,
                    vals,
                ).fetchall()

                if recent_n > 0:
                    recent_rows = cur.execute(
                        f"""
                        SELECT iso_time, job_id, backend, {pool_expr} AS quota_pool, model,
                               status, duration_seconds, usage_status, total_tokens,
                               effort, parent_run, label
                        FROM usage_events
                        {where}
                        ORDER BY ts DESC, id DESC
                        LIMIT ?
                        """,
                        [*vals, recent_n],
                    ).fetchall()

        def _empty_bucket(in_flight_cnt: int) -> dict[str, Any]:
            return {
                "runs": 0,
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
            return {
                "runs": int(r["runs"] or 0),
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

        overall_runs = 0
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

        if compact:
            return summary

        if "neon" in by_backend:
            by_backend["neon"]["token_note"] = (
                "Per-turn Antigravity tokens across agy_gemini and agy_claude_gpt pools "
                "(agy cache_read_tokens can exceed reported total_tokens; compare pools via quota)."
            )
        if "codex" in by_backend:
            by_backend["codex"]["token_note"] = (
                "Per-turn Codex tokens (cached_tokens is a subset of input_tokens; "
                "reasoning_tokens is a subset of output_tokens)."
            )

        by_model: dict[str, dict[str, Any]] = {}
        for mr in model_rows:
            mkey = f"{mr['backend']}:{mr['model_name']}"
            by_model[mkey] = {
                "runs": int(mr["runs"] or 0),
                "completed": int(mr["completed"] or 0),
                "duration_seconds": round(float(mr["duration_seconds"] or 0.0), 2),
                "total_tokens": int(mr["total_tokens"] or 0),
                "usage_coverage": {
                    "exact": int(mr["cov_exact"] or 0),
                    "delta": int(mr["cov_delta"] or 0),
                    "partial": int(mr["cov_partial"] or 0),
                    "missing": int(mr["cov_missing"] or 0),
                },
            }
        summary["by_model"] = by_model

        recent: list[dict[str, Any]] = []
        for rr in recent_rows:
            item: dict[str, Any] = {
                "time": rr["iso_time"],
                "job_id": rr["job_id"],
                "backend": rr["backend"],
                "quota_pool": rr["quota_pool"],
                "model": rr["model"],
                "status": rr["status"],
                "duration_seconds": rr["duration_seconds"],
                "usage_status": rr["usage_status"],
                "total_tokens": rr["total_tokens"],
            }
            if rr["effort"]:
                item["effort"] = rr["effort"]
            if rr["parent_run"]:
                item["parent_run"] = rr["parent_run"]
            if rr["label"]:
                item["label"] = rr["label"]
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
