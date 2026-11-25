"""Async job orchestration with shared SQLite state, session serialization, and quota lookups."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from agent_bridge.config import (
    BackendName,
    Settings,
    clamp_timeout,
    normalize_backend,
    normalize_effort,
    normalize_mode,
)
from agent_bridge.models import JobRecord, RunRequest, RunResult
from agent_bridge.process_control import is_pid_alive
from agent_bridge.quota import fetch_codex_quota, fetch_neon_quota
from agent_bridge.runners import CodexRunner, NeonRunner
from agent_bridge.storage import BridgeStore, UsagePeriod

logger = logging.getLogger("agent_bridge.jobs")


def _preview(prompt: str, max_chars: int = 120) -> str:
    flat = " ".join(prompt.split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1] + "…"


class JobManager:
    """Manages sync and background subagent jobs backed by shared SQLite state."""

    def __init__(
        self,
        settings: Settings,
        *,
        neon_runner: NeonRunner | None = None,
        codex_runner: CodexRunner | None = None,
        store: BridgeStore | None = None,
        owner_id: str | None = None,
        pid: int | None = None,
        is_pid_alive_fn: Callable[[int], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.owner_id = owner_id or f"pid-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._pid = pid if pid is not None else os.getpid()
        self._is_pid_alive = is_pid_alive_fn or is_pid_alive

        self.store = store or BridgeStore(
            settings.state_dir,
            max_stored_jobs=settings.max_stored_jobs,
        )
        self.neon_runner = neon_runner or NeonRunner(settings)
        self.codex_runner = codex_runner or CodexRunner(settings)

        self._local_jobs: dict[str, JobRecord] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._cancelled_ids: set[str] = set()
        self._session_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)

        self.store.register_owner(self.owner_id, self._pid)
        self.store.recover_dead_owner_jobs(
            self.owner_id,
            is_pid_alive_fn=self._is_pid_alive,
            stale_seconds=settings.owner_stale_seconds,
        )

    def _get_session_lock(self, backend: str, session_id: str) -> asyncio.Lock:
        key = (backend, session_id)
        lock = self._session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[key] = lock
        return lock

    def _validate_cwd(self, cwd: str | None) -> str:
        path = (
            Path(cwd).expanduser().resolve()
            if cwd and cwd.strip()
            else self.settings.default_cwd
        )
        if not path.exists():
            raise ValueError(f"Working directory does not exist: {path}")
        if not path.is_dir():
            raise ValueError(f"Working directory is not a directory: {path}")
        return str(path)

    def _validate_extra_dirs(self, extra_dirs: list[str] | None) -> list[str]:
        if not extra_dirs:
            return []
        resolved: list[str] = []
        for raw in extra_dirs:
            if not raw or not raw.strip():
                continue
            path = Path(raw).expanduser().resolve()
            if not path.exists() or not path.is_dir():
                raise ValueError(f"extra_dir does not exist or is not a directory: {path}")
            resolved.append(str(path))
        return resolved

    def prepare_request(
        self,
        *,
        backend: str | None,
        prompt: str,
        cwd: str | None = None,
        mode: str | None = "read_only",
        model: str | None = None,
        effort: str | None = None,
        resume_from: str | None = None,
        worktree: bool = False,
        extra_dirs: list[str] | None = None,
        timeout_seconds: int | None = None,
        label: str | None = None,
        parent_run: str | None = None,
        review_scope: Literal["uncommitted", "base", "commit"] | None = None,
        review_base: str | None = None,
        review_commit: str | None = None,
    ) -> RunRequest:
        if not prompt.strip() and review_scope is None:
            raise ValueError("Prompt must be non-empty.")

        resolved_session_id: str | None = None
        inferred_backend: BackendName | None = None
        inferred_cwd: str | None = None

        if resume_from and resume_from.strip():
            token = resume_from.strip()
            prior = self.store.get_job(token)
            if prior is not None:
                if not prior.session_id:
                    raise ValueError(
                        f"Job {token!r} does not have a resumable session_id yet "
                        f"(status={prior.status})."
                    )
                resolved_session_id = prior.session_id
                inferred_backend = prior.backend
                inferred_cwd = prior.effective_cwd or prior.cwd
            else:
                resolved_session_id = token

        if backend and backend.strip():
            resolved_backend = normalize_backend(backend)
            if inferred_backend and resolved_backend != inferred_backend:
                raise ValueError(
                    f"Job {resume_from!r} belongs to backend {inferred_backend!r}, "
                    f"not {resolved_backend!r}."
                )
        elif inferred_backend is not None:
            resolved_backend = inferred_backend
        else:
            raise ValueError("backend is required ('neon' or 'codex').")

        resolved_mode = normalize_mode(mode)
        resolved_effort = normalize_effort(effort)
        resolved_cwd = self._validate_cwd(cwd or inferred_cwd)
        resolved_extra_dirs = self._validate_extra_dirs(extra_dirs)
        resolved_timeout = clamp_timeout(timeout_seconds, self.settings.default_timeout_seconds)

        if worktree and resolved_backend != "codex":
            raise ValueError("worktree=True is only supported for the 'codex' backend.")
        if worktree and resolved_mode != "edit":
            raise ValueError("worktree=True requires mode='edit'.")
        if resolved_backend == "codex" and resolved_session_id and resolved_extra_dirs:
            raise ValueError(
                "extra_dirs is not supported when resuming a Codex session "
                "(`codex exec resume` does not accept --add-dir). Start a new Codex session instead."
            )

        requested_model = model.strip() if model and model.strip() else None

        return RunRequest(
            backend=resolved_backend,
            prompt=prompt,
            cwd=resolved_cwd,
            mode=resolved_mode,
            model=requested_model,
            effort=resolved_effort,
            session_id=resolved_session_id,
            worktree=bool(worktree),
            extra_dirs=resolved_extra_dirs,
            timeout_seconds=resolved_timeout,
            label=label.strip() if label and label.strip() else None,
            parent_run=parent_run.strip() if parent_run and parent_run.strip() else None,
            review_scope=review_scope,
            review_base=review_base,
            review_commit=review_commit,
        )

    def start(self, request: RunRequest) -> JobRecord:
        job_id = f"{request.backend}-{uuid.uuid4().hex[:8]}"
        preview_source = (
            request.prompt
            if request.prompt.strip()
            else f"[review:{request.review_scope}]"
        )
        record = JobRecord(
            job_id=job_id,
            owner_id=self.owner_id,
            backend=request.backend,
            cwd=request.cwd,
            effective_cwd=request.cwd,
            mode=request.mode,
            requested_model=request.model,
            model=request.model,
            effort=request.effort,
            prompt_preview=_preview(preview_source),
            label=request.label,
            parent_run=request.parent_run,
            worktree=request.worktree,
            requested_session_id=request.session_id,
            session_id=request.session_id,
            status="queued",
            last_activity="queued",
        )
        event = asyncio.Event()
        self._local_jobs[job_id] = record
        self._events[job_id] = event
        self.store.insert_job(record)

        deadline = time.monotonic() + request.timeout_seconds
        task = asyncio.create_task(
            self._execute(record, request, event, deadline),
            name=f"job-{job_id}",
        )
        self._tasks[job_id] = task
        return record

    async def _wait_for_cross_window_session_turn(
        self,
        record: JobRecord,
        session_id: str,
        deadline: float,
    ) -> None:
        """Wait if another live bridge window is currently running a turn on `(backend, session_id)`."""
        while True:
            other = self.store.find_active_session_job(
                record.backend,
                session_id,
                exclude_job_id=record.job_id,
            )
            if other is None:
                return
            if other.owner_id == self.owner_id:
                # Same process serialization is handled by `_session_locks`; don't deadlock on self-queued sibling.
                return
            if not self.store.is_owner_alive(
                other.owner_id,
                is_pid_alive_fn=self._is_pid_alive,
                stale_seconds=self.settings.owner_stale_seconds,
            ):
                self.store.recover_dead_owner_jobs(
                    self.owner_id,
                    is_pid_alive_fn=self._is_pid_alive,
                    stale_seconds=self.settings.owner_stale_seconds,
                )
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                raise asyncio.TimeoutError(
                    f"Timed out waiting for active session {session_id!r} (held by {other.job_id}) to finish."
                )
            record.last_activity = f"waiting for session turn ({other.job_id})"
            self.store.update_job_progress(
                record.job_id,
                owner_id=self.owner_id,
                last_activity=record.last_activity,
            )
            await asyncio.sleep(min(0.25, remaining))

    async def _execute(
        self,
        record: JobRecord,
        request: RunRequest,
        done_event: asyncio.Event,
        deadline: float,
    ) -> None:
        runner = self.neon_runner if request.backend == "neon" else self.codex_runner

        def on_session(session_id: str) -> None:
            if record.session_id != session_id:
                record.session_id = session_id
                self.store.update_job_progress(
                    record.job_id,
                    owner_id=self.owner_id,
                    session_id=session_id,
                )

        def on_model(model: str) -> None:
            if record.model != model:
                record.model = model
                self.store.update_job_progress(
                    record.job_id,
                    owner_id=self.owner_id,
                    model=model,
                )

        def on_activity(activity: str) -> None:
            record.last_activity = activity
            self.store.update_job_progress(
                record.job_id,
                owner_id=self.owner_id,
                last_activity=activity,
            )

        session_lock = (
            self._get_session_lock(request.backend, request.session_id)
            if request.session_id
            else None
        )
        lock_acquired = False
        sem_acquired = False

        try:
            # 1. Serialize turns per session within the single overall deadline.
            if session_lock is not None:
                rem = deadline - time.monotonic()
                if rem <= 0.5:
                    raise asyncio.TimeoutError("Job deadline expired while waiting for session lock.")
                record.last_activity = "waiting for prior turn on session"
                self.store.update_job_progress(
                    record.job_id,
                    owner_id=self.owner_id,
                    last_activity=record.last_activity,
                )
                await asyncio.wait_for(session_lock.acquire(), timeout=rem)
                lock_acquired = True
                await self._wait_for_cross_window_session_turn(
                    record,
                    request.session_id,  # type: ignore[arg-type]
                    deadline,
                )

            # 2. Acquire per-bridge concurrency slot within the same overall deadline.
            rem = deadline - time.monotonic()
            if rem <= 0.5:
                raise asyncio.TimeoutError("Job deadline expired while queued for concurrency slot.")
            await asyncio.wait_for(self._semaphore.acquire(), timeout=rem)
            sem_acquired = True

            record.status = "running"
            record.started_at = time.time()
            record.last_activity = f"starting {request.backend}"
            self.store.update_job_progress(
                record.job_id,
                owner_id=self.owner_id,
                status="running",
                started_at=record.started_at,
                last_activity=record.last_activity,
            )

            prior_watermark = (
                self.store.get_session_watermark(request.backend, request.session_id)
                if request.backend == "neon" and request.session_id
                else None
            )

            result: RunResult = await runner.run(
                request,
                deadline=deadline,
                prior_watermark=prior_watermark,
                on_session=on_session,
                on_activity=on_activity,
                on_model=on_model,
            )
        except asyncio.CancelledError:
            self._cancelled_ids.discard(record.job_id)
            record.status = "cancelled"
            record.failure_reason = "cancelled"
            record.provider_state = "stopped"
            record.error = "Job cancelled."
            record.retry_guidance = (
                "Subprocess tree was terminated. For edit tasks inspect git status/diff before retrying."
            )
            record.finished_at = time.time()
            record.duration_seconds = (
                round(max(0.0, record.finished_at - record.started_at), 2)
                if record.started_at is not None
                else 0.0
            )
            record.last_activity = "cancelled"
            self.store.finalize_job(record)
            done_event.set()
            return
        except asyncio.TimeoutError as exc:
            record.status = "timed_out"
            record.failure_reason = "timeout"
            record.provider_state = "stopped"
            record.error = str(exc) or f"Job timed out after {request.timeout_seconds}s."
            record.retry_guidance = (
                "Job deadline expired while queued or waiting on an active session turn. "
                "Increase timeout_seconds or wait for active work to finish."
            )
            record.finished_at = time.time()
            record.duration_seconds = (
                round(max(0.0, record.finished_at - record.started_at), 2)
                if record.started_at is not None
                else 0.0
            )
            record.last_activity = "timed_out"
            self.store.finalize_job(record)
            done_event.set()
            return
        except Exception as exc:
            logger.exception("Unhandled error running job %s", record.job_id)
            record.status = "failed"
            record.failure_reason = "cli_error"
            record.provider_state = "unknown"
            record.error = f"{type(exc).__name__}: {exc}"
            record.retry_guidance = "Check CLI installation with list_backends and inspect error."
            record.finished_at = time.time()
            record.duration_seconds = (
                round(max(0.0, record.finished_at - record.started_at), 2)
                if record.started_at is not None
                else 0.0
            )
            record.last_activity = "failed"
            self.store.finalize_job(record)
            done_event.set()
            return
        finally:
            if sem_acquired:
                self._semaphore.release()
            if lock_acquired and session_lock is not None:
                session_lock.release()
            self._tasks.pop(record.job_id, None)

        if record.job_id in self._cancelled_ids:
            self._cancelled_ids.discard(record.job_id)
            record.status = "cancelled"
            record.failure_reason = "cancelled"
            record.provider_state = "stopped"
            record.error = "Job cancelled."
            record.retry_guidance = (
                "Subprocess tree was terminated. For edit tasks inspect git status/diff before retrying."
            )
            record.finished_at = time.time()
            record.duration_seconds = (
                round(max(0.0, record.finished_at - record.started_at), 2)
                if record.started_at is not None
                else 0.0
            )
            record.last_activity = "cancelled"
            self.store.finalize_job(record)
            done_event.set()
            return

        record.status = result.status
        record.response = result.response
        if result.session_id:
            record.session_id = result.session_id
        if result.model:
            record.model = result.model
        if result.effort:
            record.effort = result.effort
        record.model_notice = result.model_notice
        if result.effective_cwd:
            record.effective_cwd = result.effective_cwd
        record.worktree_path = result.worktree_path
        record.worktree_branch = result.worktree_branch
        record.worktree_commit = result.worktree_commit
        record.usage = result.usage
        record.provider_metrics = result.provider_metrics
        record.usage_status = result.usage_status
        record.exit_code = result.exit_code
        record.error = result.error
        record.failure_reason = result.failure_reason
        record.provider_state = result.provider_state
        record.retry_guidance = result.retry_guidance
        record.stderr_tail = result.stderr_tail
        record.finished_at = time.time()
        record.duration_seconds = (
            round(result.duration_seconds, 2)
            if result.duration_seconds is not None
            else round(max(0.0, record.finished_at - (record.started_at or record.created_at)), 2)
        )
        record.last_activity = record.status

        self.store.finalize_job(
            record,
            watermark_cumulative=result.cumulative_usage,
            watermark_turns=result.num_turns,
        )
        done_event.set()

    async def run(
        self,
        request: RunRequest,
        *,
        wait_seconds: int | None = None,
    ) -> JobRecord:
        """Run a job and wait for completion (or up to `wait_seconds`).

        If the MCP caller cancels this synchronous wait, the underlying job is cancelled cleanly.
        If `wait_seconds` expires before the job's own deadline, the job stays active and the live
        `JobRecord` (with `job_id`) is returned so the caller can poll via `check_jobs`.
        """
        record = self.start(request)
        ev = self._events[record.job_id]
        try:
            if wait_seconds is None:
                await ev.wait()
            elif wait_seconds > 0:
                try:
                    await asyncio.wait_for(ev.wait(), timeout=wait_seconds)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            await self.cancel(record.job_id)
            raise
        return self.get(record.job_id)

    async def run_parallel(
        self,
        requests: list[RunRequest],
        *,
        wait: bool = True,
        wait_seconds: int | None = None,
    ) -> list[JobRecord]:
        records = [self.start(req) for req in requests]
        if not wait:
            return records
        job_ids = [r.job_id for r in records]
        effective_wait = (
            wait_seconds
            if wait_seconds is not None
            else max((r.timeout_seconds for r in requests), default=self.settings.default_timeout_seconds)
        )
        try:
            return await self.wait_for(job_ids, wait_seconds=effective_wait, wait_mode="all")
        except asyncio.CancelledError:
            await asyncio.gather(*(self.cancel(jid) for jid in job_ids), return_exceptions=True)
            raise

    def get(self, job_id: str) -> JobRecord:
        rec = self.store.get_job(job_id)
        if rec is not None:
            return rec
        local = self._local_jobs.get(job_id)
        if local is not None:
            return local
        raise KeyError(f"Unknown job_id: {job_id!r}")

    async def wait_for(
        self,
        job_ids: list[str],
        *,
        wait_seconds: int = 0,
        wait_mode: Literal["all", "any"] = "all",
    ) -> list[JobRecord]:
        records = [self.get(jid) for jid in job_ids]
        if wait_seconds <= 0:
            return records

        if wait_mode == "any" and any(r.is_terminal for r in records):
            return records
        if wait_mode == "all" and all(r.is_terminal for r in records):
            return records

        deadline = time.monotonic() + max(0, wait_seconds)
        local_pending = [
            jid for jid, rec in zip(job_ids, records)
            if not rec.is_terminal and jid in self._events
        ]
        remote_pending = [
            jid for jid, rec in zip(job_ids, records)
            if not rec.is_terminal and jid not in self._events
        ]

        # Fast path: all pending jobs belong to this process -> pure event wait, zero polling!
        if not remote_pending:
            waiters = [asyncio.create_task(self._events[jid].wait()) for jid in local_pending]
            return_when = asyncio.FIRST_COMPLETED if wait_mode == "any" else asyncio.ALL_COMPLETED
            try:
                _done, still_pending = await asyncio.wait(
                    waiters,
                    timeout=max(0.0, deadline - time.monotonic()),
                    return_when=return_when,
                )
                for w in still_pending:
                    w.cancel()
            finally:
                for w in waiters:
                    if not w.done():
                        w.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
            return [self.store.get_job(jid) or rec for jid, rec in zip(job_ids, records)]

        # Cross-window path: poll SQLite cleanly while checking for remote owner crashes.
        while time.monotonic() < deadline:
            self.store.recover_dead_owner_jobs(
                self.owner_id,
                is_pid_alive_fn=self._is_pid_alive,
                stale_seconds=self.settings.owner_stale_seconds,
            )
            current = [self.store.get_job(jid) or rec for jid, rec in zip(job_ids, records)]
            if wait_mode == "any" and any(r.is_terminal for r in current):
                return current
            if wait_mode == "all" and all(r.is_terminal for r in current):
                return current
            await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

        return [self.store.get_job(jid) or rec for jid, rec in zip(job_ids, records)]

    async def cancel(self, job_id: str) -> JobRecord:
        record = self.get(job_id)
        if record.is_terminal:
            return record

        # Cross-window protection: never corrupt an active job owned by another live bridge process.
        if record.owner_id != self.owner_id:
            if self.store.is_owner_alive(
                record.owner_id,
                is_pid_alive_fn=self._is_pid_alive,
                stale_seconds=self.settings.owner_stale_seconds,
            ):
                raise RuntimeError(
                    f"Job {job_id!r} is owned by another live bridge process ({record.owner_id}) "
                    "and must be cancelled from that window."
                )
            self.store.recover_dead_owner_jobs(
                self.owner_id,
                is_pid_alive_fn=self._is_pid_alive,
                stale_seconds=self.settings.owner_stale_seconds,
            )
            return self.get(job_id)

        local_record = self._local_jobs.get(job_id, record)
        self._cancelled_ids.add(job_id)

        # Single-owner cleanup: cancel the job's task so the runner's ManagedProcess terminates the tree.
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=6.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # If task was cancelled before entering `_execute()`, finalize cleanup here.
        if not local_record.is_terminal:
            self._cancelled_ids.discard(job_id)
            self._tasks.pop(job_id, None)
            local_record.status = "cancelled"
            local_record.failure_reason = "cancelled"
            local_record.provider_state = "stopped"
            local_record.error = "Job cancelled."
            local_record.retry_guidance = (
                "Job was cancelled before execution started."
            )
            local_record.finished_at = time.time()
            local_record.duration_seconds = (
                round(max(0.0, local_record.finished_at - local_record.started_at), 2)
                if local_record.started_at is not None
                else 0.0
            )
            local_record.last_activity = "cancelled"
            self.store.finalize_job(local_record)
            ev = self._events.get(job_id)
            if ev is not None:
                ev.set()

        return self.get(job_id)

    def list_jobs(
        self,
        *,
        limit: int = 20,
        status: str | None = None,
        backend: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
    ) -> list[dict[str, Any]]:
        self.store.recover_dead_owner_jobs(
            self.owner_id,
            is_pid_alive_fn=self._is_pid_alive,
            stale_seconds=self.settings.owner_stale_seconds,
        )
        norm_backend = normalize_backend(backend) if backend and backend.strip() else None
        norm_status = status.strip().lower() if status and status.strip() else None
        norm_cwd = str(Path(cwd).expanduser().resolve()) if cwd and cwd.strip() else None
        norm_parent = parent_run.strip() if parent_run and parent_run.strip() else None
        recs = self.store.list_jobs(
            limit=limit,
            status=norm_status,
            backend=norm_backend,
            cwd=norm_cwd,
            parent_run=norm_parent,
        )
        return [r.to_summary() for r in recs]

    async def get_usage_summary(
        self,
        *,
        period: UsagePeriod = "today",
        backend: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
        include_quota: bool = False,
        refresh_quota: bool = False,
        compact: bool = False,
        recent_limit: int = 10,
    ) -> dict[str, Any]:
        self.store.recover_dead_owner_jobs(
            self.owner_id,
            is_pid_alive_fn=self._is_pid_alive,
            stale_seconds=self.settings.owner_stale_seconds,
        )
        norm_backend = normalize_backend(backend) if backend and backend.strip() else None
        norm_cwd = str(Path(cwd).expanduser().resolve()) if cwd and cwd.strip() else None
        norm_parent = parent_run.strip() if parent_run and parent_run.strip() else None

        summary = self.store.summarize_usage(
            period=period,
            owner_id=self.owner_id,
            backend=norm_backend,
            cwd=norm_cwd,
            parent_run=norm_parent,
            recent_limit=recent_limit,
            compact=compact,
        )

        if include_quota:
            targets = [norm_backend] if norm_backend else ["neon", "codex"]
            summary["quota"] = await self.get_quota(targets, refresh=refresh_quota)

        return summary

    async def get_quota(
        self,
        backends: list[str],
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        async def _probe_one(bname: str) -> tuple[str, dict[str, Any]]:
            if not refresh:
                cached = self.store.get_cached_quota(bname, self.settings.quota_cache_ttl_seconds)
                if cached is not None:
                    return bname, cached

            if bname == "codex":
                try:
                    cmd = self.codex_runner.resolve_command()
                except Exception as exc:
                    return bname, {"backend": "codex", "status": "unknown", "error": str(exc)}
                res = await fetch_codex_quota(cmd)
            else:
                try:
                    cmd = self.neon_runner.resolve_command()
                except Exception as exc:
                    return bname, {"backend": "neon", "status": "unknown", "error": str(exc)}
                res = await fetch_neon_quota(cmd)

            res["cached"] = False
            if res.get("status") == "ok":
                self.store.set_cached_quota(bname, res)
            return bname, res

        pairs = await asyncio.gather(*(_probe_one(b) for b in backends))
        return dict(pairs)

    async def shutdown(self) -> None:
        active_ids = list(self._tasks.keys())
        if active_ids:
            await asyncio.gather(*(self.cancel(jid) for jid in active_ids), return_exceptions=True)
        self.store.mark_owner_exited(self.owner_id)
