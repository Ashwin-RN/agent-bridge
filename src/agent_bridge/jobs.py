"""Async job lifecycle management, concurrency control, usage tracking, and persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from agent_bridge.config import (
    BackendName,
    Settings,
    clamp_timeout,
    normalize_backend,
    normalize_mode,
)
from agent_bridge.models import JobRecord, RunRequest, RunResult
from agent_bridge.runners import CodexRunner, NeonRunner, terminate_process_tree
from agent_bridge.usage import UsageTracker

logger = logging.getLogger("agent_bridge.jobs")


def _preview(prompt: str, max_chars: int = 120) -> str:
    flat = " ".join(prompt.split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1] + "…"


class JobManager:
    """Manages sync and background Neon/Codex subagent jobs."""

    def __init__(
        self,
        settings: Settings,
        *,
        neon_runner: NeonRunner | None = None,
        codex_runner: CodexRunner | None = None,
        usage_tracker: UsageTracker | None = None,
    ) -> None:
        self.settings = settings
        self.neon_runner = neon_runner or NeonRunner(settings)
        self.codex_runner = codex_runner or CodexRunner(settings)
        self.usage_tracker = usage_tracker or UsageTracker(settings.state_dir)

        self._jobs: dict[str, JobRecord] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled_ids: set[str] = set()
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._state_path = settings.state_dir / "jobs.json"

        self._load_state()

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return
            dirty = False
            for entry in raw:
                if not isinstance(entry, dict) or "job_id" not in entry:
                    continue
                rec = JobRecord.from_persisted(entry)
                if not rec.is_terminal:
                    rec.status = "failed"
                    rec.error = "MCP server restarted before job completed."
                    rec.finished_at = time.time()
                    dirty = True
                self._jobs[rec.job_id] = rec
                ev = asyncio.Event()
                ev.set()
                self._events[rec.job_id] = ev
            if dirty:
                self._save_state()
        except Exception as exc:
            logger.warning("Could not load persisted jobs from %s: %s", self._state_path, exc)

    def _save_state(self) -> None:
        try:
            self.settings.state_dir.mkdir(parents=True, exist_ok=True)
            ordered = sorted(
                self._jobs.values(),
                key=lambda rec: max(rec.created_at, rec.finished_at or 0.0),
                reverse=True,
            )[: self.settings.max_stored_jobs]
            keep_ids = {rec.job_id for rec in ordered} | set(self._tasks.keys())
            for stale_id in list(self._jobs.keys()):
                if stale_id not in keep_ids:
                    self._jobs.pop(stale_id, None)
                    self._events.pop(stale_id, None)

            payload = [rec.to_persisted() for rec in ordered]
            tmp_path = self._state_path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.replace(self._state_path)
        except Exception as exc:
            logger.warning("Could not persist job state to %s: %s", self._state_path, exc)

    def _validate_cwd(self, cwd: str | None) -> str:
        path = Path(cwd).expanduser().resolve() if cwd and cwd.strip() else self.settings.default_cwd
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
        resume_from: str | None = None,
        worktree: bool = False,
        extra_dirs: list[str] | None = None,
        timeout_seconds: int | None = None,
        label: str | None = None,
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
            prior = self._jobs.get(token)
            if prior is not None:
                if not prior.session_id:
                    raise ValueError(
                        f"Job {token!r} does not have a resumable session_id yet "
                        f"(status={prior.status})."
                    )
                resolved_session_id = prior.session_id
                inferred_backend = prior.backend
                inferred_cwd = prior.cwd
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
        resolved_cwd = self._validate_cwd(cwd or inferred_cwd)
        resolved_extra_dirs = self._validate_extra_dirs(extra_dirs)
        resolved_timeout = clamp_timeout(timeout_seconds, self.settings.default_timeout_seconds)

        if worktree and resolved_backend != "codex":
            raise ValueError("worktree=True is only supported for the 'codex' backend.")

        requested_model = model.strip() if model and model.strip() else None

        return RunRequest(
            backend=resolved_backend,
            prompt=prompt,
            cwd=resolved_cwd,
            mode=resolved_mode,
            model=requested_model,
            session_id=resolved_session_id,
            worktree=bool(worktree and resolved_mode == "edit"),
            extra_dirs=resolved_extra_dirs,
            timeout_seconds=resolved_timeout,
            label=label.strip() if label and label.strip() else None,
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
            backend=request.backend,
            cwd=request.cwd,
            mode=request.mode,
            requested_model=request.model,
            model=request.model,
            prompt_preview=_preview(preview_source),
            label=request.label,
            worktree=request.worktree,
            requested_session_id=request.session_id,
            session_id=request.session_id,
            status="queued",
            last_activity="queued",
        )
        event = asyncio.Event()
        self._jobs[job_id] = record
        self._events[job_id] = event
        self._save_state()

        task = asyncio.create_task(self._execute(record, request, event), name=f"job-{job_id}")
        self._tasks[job_id] = task
        return record

    def _record_usage(self, record: JobRecord) -> None:
        self.usage_tracker.record(
            job_id=record.job_id,
            backend=record.backend,
            model=record.model or record.requested_model,
            mode=record.mode,
            status=record.status,
            duration_seconds=record.duration_seconds,
            cwd=record.cwd,
            label=record.label,
            usage=record.usage,
        )

    async def _execute(
        self,
        record: JobRecord,
        request: RunRequest,
        done_event: asyncio.Event,
    ) -> None:
        runner = self.neon_runner if request.backend == "neon" else self.codex_runner

        def on_process(proc: asyncio.subprocess.Process) -> None:
            self._processes[record.job_id] = proc

        def on_session(session_id: str) -> None:
            record.session_id = session_id

        def on_activity(activity: str) -> None:
            record.last_activity = activity

        try:
            async with self._semaphore:
                record.status = "running"
                record.started_at = time.time()
                record.last_activity = f"starting {request.backend}"
                result: RunResult = await runner.run(
                    request,
                    on_process=on_process,
                    on_session=on_session,
                    on_activity=on_activity,
                )
        except asyncio.CancelledError:
            self._cancelled_ids.discard(record.job_id)
            record.status = "cancelled"
            record.finished_at = time.time()
            if record.started_at is not None:
                record.duration_seconds = record.finished_at - record.started_at
            record.error = "Job cancelled."
            record.last_activity = "cancelled"
            self._record_usage(record)
            self._save_state()
            done_event.set()
            return
        except Exception as exc:
            logger.exception("Unhandled error running job %s", record.job_id)
            record.status = "failed"
            record.finished_at = time.time()
            if record.started_at is not None:
                record.duration_seconds = record.finished_at - record.started_at
            record.error = f"{type(exc).__name__}: {exc}"
            record.last_activity = "failed"
            self._record_usage(record)
            self._save_state()
            done_event.set()
            return
        finally:
            self._processes.pop(record.job_id, None)
            self._tasks.pop(record.job_id, None)

        if record.job_id in self._cancelled_ids:
            self._cancelled_ids.discard(record.job_id)
            record.status = "cancelled"
            record.error = "Job cancelled."
            record.finished_at = time.time()
            if record.started_at is not None:
                record.duration_seconds = record.finished_at - record.started_at
            record.last_activity = "cancelled"
            self._record_usage(record)
            self._save_state()
            done_event.set()
            return

        record.status = result.status
        record.response = result.response
        if result.session_id:
            record.session_id = result.session_id
        if result.model:
            record.model = result.model
        record.model_notice = result.model_notice
        record.usage = result.usage
        record.exit_code = result.exit_code
        record.error = result.error
        record.stderr_tail = result.stderr_tail
        record.finished_at = time.time()
        record.duration_seconds = (
            result.duration_seconds
            if result.duration_seconds is not None
            else (record.finished_at - (record.started_at or record.created_at))
        )
        record.last_activity = record.status
        self._record_usage(record)
        self._save_state()
        done_event.set()

    async def run(self, request: RunRequest) -> JobRecord:
        record = self.start(request)
        await self._events[record.job_id].wait()
        return record

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
        timeout = (
            wait_seconds
            if wait_seconds is not None
            else max((r.timeout_seconds for r in requests), default=300)
        )
        return await self.wait_for(
            [r.job_id for r in records],
            wait_seconds=timeout,
            wait_mode="all",
        )

    def get(self, job_id: str) -> JobRecord:
        rec = self._jobs.get(job_id)
        if rec is None:
            raise KeyError(f"Unknown job_id: {job_id!r}")
        return rec

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

        pending_events = [
            self._events[jid]
            for jid in job_ids
            if jid in self._events and not self._events[jid].is_set()
        ]
        if not pending_events:
            return records

        waiters = [asyncio.create_task(ev.wait()) for ev in pending_events]
        return_when = asyncio.FIRST_COMPLETED if wait_mode == "any" else asyncio.ALL_COMPLETED
        try:
            _done, still_pending = await asyncio.wait(
                waiters,
                timeout=max(0, wait_seconds),
                return_when=return_when,
            )
            for w in still_pending:
                w.cancel()
        finally:
            for w in waiters:
                if not w.done():
                    w.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

        # Return captured record objects (updated in-place), safe even if aged out of retention.
        return records

    async def cancel(self, job_id: str) -> JobRecord:
        record = self.get(job_id)
        if record.is_terminal:
            return record

        self._cancelled_ids.add(job_id)

        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()

        proc = self._processes.get(job_id)
        if proc is not None:
            await terminate_process_tree(proc)

        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # If task was cancelled before entering `_execute()`, finalize cleanup here.
        if not record.is_terminal:
            self._cancelled_ids.discard(job_id)
            self._processes.pop(job_id, None)
            self._tasks.pop(job_id, None)
            record.status = "cancelled"
            record.error = "Job cancelled."
            record.finished_at = time.time()
            if record.started_at is not None:
                record.duration_seconds = record.finished_at - record.started_at
            record.last_activity = "cancelled"
            self._record_usage(record)
            self._save_state()
            ev = self._events.get(job_id)
            if ev is not None:
                ev.set()

        return record

    def list_jobs(
        self,
        *,
        limit: int = 20,
        status: str | None = None,
        backend: str | None = None,
    ) -> list[dict[str, Any]]:
        norm_backend = normalize_backend(backend) if backend and backend.strip() else None
        norm_status = status.strip().lower() if status and status.strip() else None
        items = sorted(self._jobs.values(), key=lambda r: r.created_at, reverse=True)
        filtered = [
            r
            for r in items
            if (norm_backend is None or r.backend == norm_backend)
            and (norm_status is None or r.status == norm_status)
        ]
        return [r.to_summary() for r in filtered[: max(1, min(limit, 100))]]

    async def shutdown(self) -> None:
        active_ids = list(self._tasks.keys())
        if not active_ids:
            return
        await asyncio.gather(*(self.cancel(jid) for jid in active_ids), return_exceptions=True)
