"""Async job orchestration with atomic SQLite session claims, pre-assigned worktrees, and bounded RAM."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from agent_bridge.config import (
    BackendName,
    Settings,
    clamp_timeout,
    classify_quota_pool,
    normalize_backend,
    normalize_effort,
    normalize_mode,
    normalize_quota_pool,
    read_codex_local_defaults,
    resolve_backend_and_model_hint,
)
from agent_bridge.claude_runner import ClaudeRunner
from agent_bridge.dashboard import build_dashboard, resolve_window
from agent_bridge.models import JobRecord, RunCancelled, RunRequest, RunResult
from agent_bridge.nesting import NOT_NESTED, NestedBridgeError, Nesting
from agent_bridge.process_control import (
    CURRENT_PROCESS_TRACKER,
    ProcessContainmentError,
    ProcessTracker,
    is_pid_alive,
)
from agent_bridge.quota import (
    CLAUDE_QUOTA_NOTE,
    build_quota_advisory,
    claude_quota_from_rate_limit,
    enrich_quota_reading,
    fetch_codex_quota,
    fetch_neon_quota,
    has_passed_reset,
    merge_claude_quota,
)
from agent_bridge.runners import CodexRunner, NeonRunner, prepare_job_worktree

ALL_BACKENDS: tuple[str, ...] = ("neon", "codex", "claude")
# Backends whose edit jobs can run in a Git worktree the bridge makes for them.
WORKTREE_BACKENDS: frozenset[str] = frozenset({"codex", "claude"})
from agent_bridge.storage import BridgeStore, UsagePeriod, is_sqlite_lock_error

logger = logging.getLogger("agent_bridge.jobs")


def _is_path_within(candidate: str, root: str) -> bool:
    try:
        Path(candidate).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


class SessionResumeBlockedError(RuntimeError):
    """Raised when automatic resume is blocked because the prior turn's provider state is uncertain."""

    def __init__(self, *, session_id: str, prior_job_id: str, prior_provider_state: str) -> None:
        self.session_id = session_id
        self.prior_job_id = prior_job_id
        self.prior_provider_state = prior_provider_state
        super().__init__(
            f"Automatic resume blocked on session {session_id!r}: prior turn {prior_job_id!r} "
            f"ended with provider_state={prior_provider_state!r}. Confirm that exact turn has ended "
            f"(and inspect git diff/worktree for edits), then retry with confirm_prior_job_id={prior_job_id!r} "
            "(or confirm_prior_stopped=True)."
        )


class WorktreeMissingError(RuntimeError):
    """Raised when a resumed session's recorded Git worktree no longer exists."""

    def __init__(self, *, session_id: str, worktree_path: str) -> None:
        self.session_id = session_id
        self.worktree_path = worktree_path
        super().__init__(
            f"Recorded worktree for session {session_id!r} no longer exists: {worktree_path!r}. "
            "Pass relocate_cwd=True (with cwd) to intentionally move this session to another directory, "
            "or pass worktree=True (with mode='edit') to create a new worktree."
        )


def _sum_attempt_usage(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Aggregate available token counters across prior executed attempts."""
    keys = (
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
    )
    totals: dict[str, int] = {}
    for att in attempts:
        u = att.get("usage")
        if not isinstance(u, dict):
            continue
        for k in keys:
            v = u.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                totals[k] = totals.get(k, 0) + int(v)
    if not totals:
        return None
    if "total_tokens" not in totals and ("input_tokens" in totals or "output_tokens" in totals):
        totals["total_tokens"] = totals.get("input_tokens", 0) + totals.get("output_tokens", 0)
    return totals


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
        claude_runner: ClaudeRunner | None = None,
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
        self.claude_runner = claude_runner or ClaudeRunner(settings)
        # Set by the server at startup when this bridge runs inside a bridge job. Nothing starts then.
        self.nesting: Nesting = NOT_NESTED

        # In-memory state is tracked ONLY for active (or uncommitted terminal) jobs and released on commit.
        self._local_jobs: dict[str, JobRecord] = {}
        self._terminal_uncommitted: dict[str, JobRecord] = {}
        self._finalizing_records: dict[str, JobRecord] = {}
        self._uncommitted_session_ids: set[str] = set()
        self._pending_finalizations: dict[str, asyncio.Task[Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._trackers: dict[str, ProcessTracker] = {}
        self._cancelled_ids: set[str] = set()
        self._session_waiters: dict[tuple[str, str], set[asyncio.Event]] = {}
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._finalize_retry_seconds: float = 15.0

        self.store.register_owner(self.owner_id, self._pid)
        self.store.recover_dead_owner_jobs(
            self.owner_id,
            is_pid_alive_fn=self._is_pid_alive,
            stale_seconds=settings.owner_stale_seconds,
        )

    def _runner_for(self, backend: str) -> NeonRunner | CodexRunner | ClaudeRunner:
        if backend == "neon":
            return self.neon_runner
        if backend == "codex":
            return self.codex_runner
        if backend == "claude":
            return self.claude_runner
        raise ValueError(f"No runner for backend {backend!r}.")

    async def _db(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a SQLite store call off the asyncio event loop so lock waits never stall MCP traffic."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _visible_record(self, rec: JobRecord) -> JobRecord:
        """Hide a newly discovered session_id from callers until SQLite commits its claim/terminal row."""
        if rec.job_id in self._uncommitted_session_ids and rec.session_id is not None:
            return replace(rec, session_id=None)
        return rec

    def _cleanup_local_job(self, job_id: str) -> None:
        """Release completed job records and handles from RAM once committed to SQLite."""
        if job_id in self._terminal_uncommitted or job_id in self._finalizing_records:
            return
        self._local_jobs.pop(job_id, None)
        self._events.pop(job_id, None)
        self._tasks.pop(job_id, None)
        self._trackers.pop(job_id, None)
        self._cancelled_ids.discard(job_id)
        self._uncommitted_session_ids.discard(job_id)

    def _notify_session_waiters(self, backend: str, session_id: str | None) -> None:
        if not session_id:
            return
        waiters = self._session_waiters.get((backend, session_id))
        if not waiters:
            return
        for ev in list(waiters):
            ev.set()

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
        relocate_cwd: bool = False,
        worktree: bool = False,
        extra_dirs: list[str] | None = None,
        timeout_seconds: int | None = None,
        label: str | None = None,
        parent_run: str | None = None,
        review_scope: Literal["uncommitted", "base", "commit"] | None = None,
        review_base: str | None = None,
        review_commit: str | None = None,
        confirm_prior_stopped: bool = False,
        confirm_prior_job_id: str | None = None,
        allow_cross_pool_fallback: bool = False,
    ) -> RunRequest:
        if not prompt.strip() and review_scope is None:
            raise ValueError("Prompt must be non-empty.")

        resolved_session_id: str | None = None
        resume_from_job_id: str | None = None
        inferred_backend: BackendName | None = None
        inferred_cwd: str | None = None
        inferred_worktree_path: str | None = None
        inferred_worktree_branch: str | None = None
        inferred_worktree_commit: str | None = None
        inferred_model: str | None = None
        inferred_effort: str | None = None
        inferred_parent_run: str | None = None
        prior_quota_pool: Any = None
        prior_model_label: str | None = None

        if resume_from and resume_from.strip():
            token = resume_from.strip()
            uncommitted_prior = self._terminal_uncommitted.get(token)
            prior = (
                self._visible_record(uncommitted_prior)
                if uncommitted_prior is not None
                else (self._local_jobs.get(token) or self.store.get_job(token))
            )
            if prior is not None:
                if not prior.session_id:
                    raise ValueError(
                        f"Job {token!r} does not have a resumable session_id yet "
                        f"(status={prior.status})."
                    )
                resume_from_job_id = prior.job_id
                resolved_session_id = prior.session_id
                inferred_backend = prior.backend
                inferred_cwd = prior.effective_cwd or prior.cwd
                inferred_worktree_path = prior.worktree_path
                inferred_worktree_branch = prior.worktree_branch
                inferred_worktree_commit = prior.worktree_commit
                inferred_model = prior.requested_model or prior.model
                inferred_effort = prior.effort
                inferred_parent_run = prior.parent_run
                prior_model_label = prior.model or prior.requested_model
                prior_quota_pool = prior.quota_pool or classify_quota_pool(
                    prior.backend,
                    prior_model_label,
                )
            else:
                resolved_session_id = token

        hinted_model: str | None = model.strip() if model and model.strip() else None
        if backend and backend.strip():
            resolved_backend, hinted_model = resolve_backend_and_model_hint(backend, model)
            if inferred_backend and resolved_backend != inferred_backend:
                raise ValueError(
                    f"Job {resume_from!r} belongs to backend {inferred_backend!r}, "
                    f"not {resolved_backend!r}."
                )
        elif inferred_backend is not None:
            resolved_backend = inferred_backend
        else:
            raise ValueError(
                "backend is required ('neon', 'agy-claude', 'agy-opus', 'agy-gpt', 'codex', or 'claude')."
            )

        # Also inherit latest executed session context when resuming by raw session_id (or if newer turn exists).
        if resolved_session_id:
            latest_ctx = self.store.get_latest_session_context(
                resolved_backend,
                resolved_session_id,
                resume_from_job_id=resume_from_job_id,
            )
            if latest_ctx is not None:
                if not inferred_worktree_path and latest_ctx.get("worktree_path"):
                    inferred_worktree_path = latest_ctx["worktree_path"]
                    inferred_worktree_branch = latest_ctx.get("worktree_branch")
                    inferred_worktree_commit = latest_ctx.get("worktree_commit")
                if not inferred_cwd:
                    inferred_cwd = latest_ctx.get("effective_cwd") or latest_ctx.get("cwd")
                if not inferred_model:
                    inferred_model = latest_ctx.get("requested_model") or latest_ctx.get("model")
                if not inferred_effort:
                    inferred_effort = latest_ctx.get("effort")
                if not inferred_parent_run:
                    inferred_parent_run = latest_ctx.get("parent_run")
                if not prior_quota_pool:
                    prior_model_label = latest_ctx.get("model") or latest_ctx.get("requested_model")
                    prior_quota_pool = latest_ctx.get("quota_pool")

        resolved_mode = normalize_mode(mode)
        if worktree and resolved_backend not in WORKTREE_BACKENDS:
            raise ValueError("worktree=True is only supported for the 'codex' and 'claude' backends.")
        if worktree and resolved_mode != "edit":
            raise ValueError("worktree=True requires mode='edit'.")

        explicit_effort = bool(effort and effort.strip())
        # Don't let an inherited effort from a prior turn override an explicitly supplied Neon model tier (e.g. `neon-low`).
        should_inherit_effort = (
            (not explicit_effort)
            and (inferred_effort is not None)
            and (resolved_backend != "neon" or hinted_model is None)
        )
        resolved_effort = normalize_effort(effort if explicit_effort else (inferred_effort if should_inherit_effort else None))
        explicit_cwd = bool(cwd and cwd.strip())

        # Preserve a session's recorded worktree on resume unless `relocate_cwd=True` is explicitly set!
        # If the recorded worktree was removed, FAIL CLOSED unless `relocate_cwd=True` or `worktree=True`.
        preserved_worktree_path: str | None = None
        preserved_worktree_branch: str | None = None
        preserved_worktree_commit: str | None = None
        if resolved_session_id and inferred_worktree_path:
            if Path(inferred_worktree_path).is_dir():
                if not relocate_cwd:
                    preserved_worktree_path = str(Path(inferred_worktree_path).resolve())
                    preserved_worktree_branch = inferred_worktree_branch
                    preserved_worktree_commit = inferred_worktree_commit
                    if explicit_cwd and _is_path_within(cwd or "", preserved_worktree_path):
                        resolved_cwd = self._validate_cwd(cwd)
                    elif inferred_cwd and Path(inferred_cwd).is_dir() and _is_path_within(
                        inferred_cwd, preserved_worktree_path
                    ):
                        resolved_cwd = self._validate_cwd(inferred_cwd)
                    else:
                        resolved_cwd = self._validate_cwd(preserved_worktree_path)
                else:
                    resolved_cwd = self._validate_cwd(cwd if explicit_cwd else inferred_cwd)
                    if _is_path_within(resolved_cwd, inferred_worktree_path):
                        preserved_worktree_path = str(Path(inferred_worktree_path).resolve())
                        preserved_worktree_branch = inferred_worktree_branch
                        preserved_worktree_commit = inferred_worktree_commit
            else:
                if not relocate_cwd and not worktree:
                    raise ValueError(
                        f"Recorded worktree for session {resolved_session_id!r} no longer exists: "
                        f"{inferred_worktree_path!r}. Pass relocate_cwd=True (with cwd) to intentionally "
                        "move this session to another directory, or pass worktree=True (with mode='edit') "
                        "to create a new worktree."
                    )
                if worktree:
                    fallback_cwd = (
                        cwd
                        if explicit_cwd
                        else (inferred_cwd if inferred_cwd and Path(inferred_cwd).is_dir() else None)
                    )
                    resolved_cwd = self._validate_cwd(fallback_cwd)
                else:
                    resolved_cwd = self._validate_cwd(cwd if explicit_cwd else inferred_cwd)
        else:
            resolved_cwd = self._validate_cwd(cwd if explicit_cwd else inferred_cwd)

        resolved_extra_dirs = self._validate_extra_dirs(extra_dirs)
        resolved_timeout = clamp_timeout(timeout_seconds, self.settings.default_timeout_seconds)

        if resolved_backend == "codex" and resolved_session_id and resolved_extra_dirs:
            raise ValueError(
                "extra_dirs is not supported when resuming a Codex session "
                "(`codex exec resume` does not accept --add-dir). Start a new Codex session instead."
            )

        # Inherit prior model intent & parent_run on resume unless caller explicitly overrode them.
        requested_model = hinted_model if hinted_model is not None else inferred_model
        resolved_parent_run = (
            parent_run.strip()
            if parent_run and parent_run.strip()
            else (inferred_parent_run.strip() if isinstance(inferred_parent_run, str) and inferred_parent_run.strip() else None)
        )

        resume_notice: str | None = None
        if resolved_session_id and prior_quota_pool and requested_model:
            requested_pool = classify_quota_pool(resolved_backend, requested_model)
            if requested_pool != prior_quota_pool:
                resume_notice = (
                    f"Quota pool changed on resume: prior turn used {prior_quota_pool!r} "
                    f"({prior_model_label or 'unknown'}), this turn requested {requested_pool!r} "
                    f"({requested_model})."
                )

        # Bind confirmation to a specific prior turn so a pre-confirmed or stale boolean cannot bypass a newer uncertain turn!
        bound_confirm_job_id: str | None = None
        if confirm_prior_job_id and confirm_prior_job_id.strip():
            bound_confirm_job_id = confirm_prior_job_id.strip()
        elif confirm_prior_stopped and resolved_session_id:
            active_block = self.store.get_session_safety_block(resolved_backend, resolved_session_id)
            if active_block is not None:
                bound_confirm_job_id = active_block["blocking_job_id"]
            elif resume_from_job_id is not None:
                bound_confirm_job_id = resume_from_job_id

        return RunRequest(
            backend=resolved_backend,
            prompt=prompt,
            cwd=resolved_cwd,
            mode=resolved_mode,
            model=requested_model,
            effort=resolved_effort,
            session_id=resolved_session_id,
            resume_from_job_id=resume_from_job_id,
            explicit_cwd=explicit_cwd,
            relocate_cwd=bool(relocate_cwd),
            worktree=bool(worktree),
            worktree_path=preserved_worktree_path,
            worktree_branch=preserved_worktree_branch,
            worktree_commit=preserved_worktree_commit,
            extra_dirs=resolved_extra_dirs,
            timeout_seconds=resolved_timeout,
            label=label.strip() if label and label.strip() else None,
            parent_run=resolved_parent_run,
            review_scope=review_scope,
            review_base=review_base,
            review_commit=review_commit,
            confirm_prior_stopped=bool(confirm_prior_stopped or bound_confirm_job_id),
            confirm_prior_job_id=bound_confirm_job_id,
            allow_cross_pool_fallback=bool(allow_cross_pool_fallback),
            prior_quota_pool=prior_quota_pool,
            prior_model=prior_model_label,
            resume_notice=resume_notice,
        )

    async def prepare_request_async(self, **kwargs: Any) -> RunRequest:
        return await self._db(self.prepare_request, **kwargs)

    def _build_and_schedule_job(self, request: RunRequest, *, persist_sync: bool) -> JobRecord:
        if self.nesting.nested:
            raise NestedBridgeError(self.nesting.refusal("start a job"))
        job_id = f"{request.backend}-{uuid.uuid4().hex[:8]}"
        request.job_id = job_id
        request.owner_id = self.owner_id
        preview_source = (
            request.prompt
            if request.prompt.strip()
            else f"[review:{request.review_scope}]"
        )
        record = JobRecord(
            job_id=job_id,
            owner_id=self.owner_id,
            backend=request.backend,
            quota_pool=classify_quota_pool(request.backend, request.model),
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
            worktree_path=request.worktree_path,
            worktree_branch=request.worktree_branch,
            worktree_commit=request.worktree_commit,
            requested_session_id=request.session_id,
            session_id=request.session_id,
            status="queued",
            last_activity="queued",
        )
        event = asyncio.Event()
        tracker = ProcessTracker()
        self._local_jobs[job_id] = record
        self._events[job_id] = event
        self._trackers[job_id] = tracker
        if persist_sync:
            self.store.insert_job(record)
        return record

    def _launch_job_task(self, record: JobRecord, request: RunRequest) -> None:
        event = self._events[record.job_id]
        tracker = self._trackers[record.job_id]
        deadline = time.monotonic() + request.timeout_seconds
        task = asyncio.create_task(
            self._execute(record, request, event, tracker, deadline),
            name=f"job-{record.job_id}",
        )
        self._tasks[record.job_id] = task

    def start(self, request: RunRequest) -> JobRecord:
        record = self._build_and_schedule_job(request, persist_sync=True)
        self._launch_job_task(record, request)
        return record

    async def start_async(self, request: RunRequest) -> JobRecord:
        record = self._build_and_schedule_job(request, persist_sync=False)
        insert_task = asyncio.create_task(
            self._db(self.store.insert_job, record),
            name=f"insert-{record.job_id}",
        )
        try:
            await asyncio.shield(insert_task)
        except BaseException:
            # Remove from local visibility immediately and clean up any row that commits in the worker thread!
            self._local_jobs.pop(record.job_id, None)

            async def _cleanup_aborted_insert() -> None:
                try:
                    inserted = False
                    try:
                        await insert_task
                        inserted = True
                    except BaseException:
                        inserted = False
                    if inserted:
                        delay = 0.05
                        dl = time.monotonic() + 10.0
                        while True:
                            try:
                                await self._db(
                                    self.store.delete_aborted_queued_job,
                                    record.job_id,
                                    lock_timeout=1.0,
                                )
                                break
                            except Exception as exc:
                                if is_sqlite_lock_error(exc) and time.monotonic() < dl:
                                    await asyncio.sleep(delay)
                                    delay = min(0.3, delay * 1.5)
                                    continue
                                logger.warning(
                                    "Failed cleaning up aborted queued job %s: %s",
                                    record.job_id,
                                    exc,
                                )
                                break
                finally:
                    self._pending_finalizations.pop(record.job_id, None)
                    self._cleanup_local_job(record.job_id)

            cleanup_task = asyncio.create_task(
                _cleanup_aborted_insert(),
                name=f"cleanup-insert-{record.job_id}",
            )
            self._pending_finalizations[record.job_id] = cleanup_task
            try:
                await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=2.0)
            except BaseException:
                pass
            raise
        self._launch_job_task(record, request)
        return record

    async def _acquire_session_claim(
        self,
        record: JobRecord,
        session_id: str,
        deadline: float,
        *,
        confirm_prior_job_id: str | None = None,
    ) -> None:
        """Atomically claim `(record.backend, session_id)` in SQLite off the event loop within `deadline`."""
        key = (record.backend, session_id)
        last_holder: str | None = None
        while True:
            # Also check local in-memory running jobs so a queued resume never races ahead of an
            # in-flight off-loop `on_session` SQLite write from a same-window Turn 1!
            local_holder: str | None = None
            for other in list(self._local_jobs.values()):
                if (
                    other.job_id != record.job_id
                    and other.backend == record.backend
                    and other.session_id == session_id
                    and not other.is_terminal
                    and (other.provider_invoked or other.status == "running")
                ):
                    local_holder = other.job_id
                    break

            if local_holder is not None:
                outcome, other_job_id, prior_state = "busy", local_holder, None
            else:
                try:
                    outcome, other_job_id, prior_state = await self._db(
                        self.store.try_claim_session,
                        record.backend,
                        session_id,
                        record.job_id,
                        self.owner_id,
                        is_pid_alive_fn=self._is_pid_alive,
                        confirm_prior_job_id=confirm_prior_job_id,
                    )
                except Exception as exc:
                    if is_sqlite_lock_error(exc):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0.05:
                            raise asyncio.TimeoutError(
                                f"Timed out waiting for SQLite lock while claiming session {session_id!r}."
                            ) from exc
                        await asyncio.sleep(min(0.15, remaining))
                        continue
                    raise
            if outcome == "claimed":
                return
            if outcome == "blocked":
                raise SessionResumeBlockedError(
                    session_id=session_id,
                    prior_job_id=other_job_id or "unknown",
                    prior_provider_state=prior_state or "unknown",
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                raise asyncio.TimeoutError(
                    f"Timed out waiting for active session {session_id!r} (held by {other_job_id}) to finish."
                )

            if other_job_id != last_holder:
                last_holder = other_job_id
                record.last_activity = f"waiting for session turn ({other_job_id})"
                try:
                    await self._db(
                        self.store.update_job_progress,
                        record.job_id,
                        owner_id=self.owner_id,
                        last_activity=record.last_activity,
                        lock_timeout=0.5,
                    )
                except Exception:
                    pass

            waiter = asyncio.Event()
            bucket = self._session_waiters.setdefault(key, set())
            bucket.add(waiter)
            try:
                await asyncio.wait_for(waiter.wait(), timeout=min(0.12, remaining))
            except asyncio.TimeoutError:
                pass
            finally:
                bucket.discard(waiter)
                if not bucket:
                    self._session_waiters.pop(key, None)

    async def _refresh_resumed_context_async(self, record: JobRecord, request: RunRequest) -> None:
        """Refresh `effective_cwd` and worktree metadata from the finished prior turn before launch."""
        if not request.session_id or request.relocate_cwd:
            return
        ctx = await self._db(
            self.store.get_latest_session_context,
            request.backend,
            request.session_id,
            resume_from_job_id=request.resume_from_job_id,
            exclude_job_id=record.job_id,
        )
        if ctx is None:
            if request.worktree_path and not Path(request.worktree_path).is_dir():
                if request.worktree:
                    request.worktree_path = None
                    request.worktree_branch = None
                    request.worktree_commit = None
                    record.worktree_path = None
                    record.worktree_branch = None
                    record.worktree_commit = None
                else:
                    raise WorktreeMissingError(
                        session_id=request.session_id,
                        worktree_path=request.worktree_path,
                    )
            return

        wt_path = ctx.get("worktree_path") or request.worktree_path
        if wt_path:
            if Path(wt_path).is_dir():
                wt_resolved = str(Path(wt_path).resolve())
                request.worktree_path = wt_resolved
                request.worktree_branch = ctx.get("worktree_branch") or request.worktree_branch
                request.worktree_commit = ctx.get("worktree_commit") or request.worktree_commit
                record.worktree_path = request.worktree_path
                record.worktree_branch = request.worktree_branch
                record.worktree_commit = request.worktree_commit
                # Always keep resumed turn inside the session's recorded worktree unless relocate_cwd=True!
                if not _is_path_within(request.cwd, wt_resolved):
                    cand = ctx.get("effective_cwd") or wt_resolved
                    target = cand if (cand and Path(cand).is_dir() and _is_path_within(cand, wt_resolved)) else wt_resolved
                    resolved = str(Path(target).resolve())
                    request.cwd = resolved
                    record.effective_cwd = resolved
            else:
                if not request.worktree:
                    raise WorktreeMissingError(
                        session_id=request.session_id,
                        worktree_path=str(wt_path),
                    )
                # Caller explicitly opted into creating a new worktree (`worktree=True`).
                request.worktree_path = None
                request.worktree_branch = None
                request.worktree_commit = None
                record.worktree_path = None
                record.worktree_branch = None
                record.worktree_commit = None
                if not Path(request.cwd).is_dir():
                    fallback = ctx.get("cwd")
                    if fallback and Path(fallback).is_dir():
                        resolved = str(Path(fallback).resolve())
                        request.cwd = resolved
                        record.effective_cwd = resolved
        elif not request.explicit_cwd:
            candidate_cwd = ctx.get("effective_cwd") or ctx.get("cwd")
            if candidate_cwd and Path(candidate_cwd).is_dir():
                resolved = str(Path(candidate_cwd).resolve())
                request.cwd = resolved
                record.effective_cwd = resolved

        if not request.model:
            prior_m = ctx.get("requested_model") or ctx.get("model")
            if prior_m:
                request.model = str(prior_m)
                record.requested_model = request.model
                record.model = request.model
                record.quota_pool = classify_quota_pool(record.backend, request.model)
        if not request.prior_quota_pool and ctx.get("quota_pool"):
            request.prior_quota_pool = ctx["quota_pool"]
            request.prior_model = ctx.get("model") or ctx.get("requested_model")

        try:
            await self._db(
                self.store.update_job_progress,
                record.job_id,
                owner_id=self.owner_id,
                effective_cwd=record.effective_cwd,
                worktree_path=record.worktree_path,
                worktree_branch=record.worktree_branch,
                worktree_commit=record.worktree_commit,
                lock_timeout=1.5,
            )
        except Exception as exc:
            logger.debug("Deferred progress refresh for %s: %s", record.job_id, exc)

    async def _commit_required_progress(
        self,
        job_id: str,
        *,
        deadline: float,
        description: str,
        **fields: Any,
    ) -> None:
        """Commit safety-critical job progress to SQLite, retrying on lock contention until `deadline`."""
        delay = 0.05
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                raise asyncio.TimeoutError(
                    f"Timed out waiting for SQLite lock while {description}."
                )
            lock_t = min(0.5, max(0.05, remaining - 0.02))
            try:
                await self._db(
                    self.store.update_job_progress,
                    job_id,
                    owner_id=self.owner_id,
                    lock_timeout=lock_t,
                    **fields,
                )
                return
            except Exception as exc:
                if is_sqlite_lock_error(exc):
                    rem2 = deadline - time.monotonic()
                    if rem2 <= 0.05:
                        raise asyncio.TimeoutError(
                            f"Timed out waiting for SQLite lock while {description}."
                        ) from exc
                    await asyncio.sleep(min(delay, rem2))
                    delay = min(0.25, delay * 1.5)
                    continue
                raise

    async def _finalize_job_durable(
        self,
        record: JobRecord,
        *,
        request_session_id: str | None = None,
        watermark_cumulative: dict[str, Any] | None = None,
        watermark_turns: int | None = None,
        touched_provider: bool | None = None,
        attempts: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Persist terminal job state idempotently off the event loop, retaining result & claim until committed."""
        # Mark finalizing immediately so `cancel()` and `shutdown()` wait for this commit instead of cancelling it!
        self._finalizing_records[record.job_id] = record

        async def _try_commit_once(lock_timeout: float) -> tuple[bool, bool]:
            """Return `(committed, retryable)`."""
            try:
                await self._db(
                    self.store.finalize_job,
                    record,
                    watermark_cumulative=watermark_cumulative,
                    watermark_turns=watermark_turns,
                    touched_provider=touched_provider,
                    attempts=attempts,
                    lock_timeout=lock_timeout,
                )
                return True, False
            except Exception as exc:
                if is_sqlite_lock_error(exc):
                    return False, True
                logger.warning("Unexpected error finalizing job %s: %s", record.job_id, exc)
                return False, False

        async def _commit_loop() -> bool:
            delay = 0.08
            first_attempt = True
            try:
                while True:
                    committed, retryable = await _try_commit_once(
                        lock_timeout=0.35 if first_attempt else 0.8
                    )
                    if committed:
                        self._terminal_uncommitted.pop(record.job_id, None)
                        self._uncommitted_session_ids.discard(record.job_id)
                        self._finalizing_records.pop(record.job_id, None)
                        self._notify_session_waiters(record.backend, request_session_id)
                        if record.session_id and record.session_id != request_session_id:
                            self._notify_session_waiters(record.backend, record.session_id)
                        task_ref = self._tasks.get(record.job_id)
                        if task_ref is None or task_ref.done():
                            self._cleanup_local_job(record.job_id)
                        return True
                    if not retryable:
                        return False
                    # Lock contention detected: expose full terminal result in RAM while holding session_claims!
                    self._terminal_uncommitted[record.job_id] = record
                    self._local_jobs[record.job_id] = record
                    first_attempt = False
                    await asyncio.sleep(delay)
                    delay = min(0.4, delay * 1.5)
            finally:
                if self._pending_finalizations.get(record.job_id) is asyncio.current_task():
                    self._pending_finalizations.pop(record.job_id, None)

        fin_task = self._pending_finalizations.get(record.job_id)
        if fin_task is None or fin_task.done():
            fin_task = asyncio.create_task(_commit_loop(), name=f"finalize-{record.job_id}")
            self._pending_finalizations[record.job_id] = fin_task

        # Fast-path wait: if SQLite is uncontended, finishes immediately without exposing `_terminal_uncommitted`.
        done, _ = await asyncio.wait({fin_task}, timeout=0.35)
        if fin_task in done:
            return bool(fin_task.result()) if not fin_task.cancelled() and fin_task.exception() is None else False

        # Delayed write or lock contention: expose terminal result in RAM while continuing to wait (uncancelled!).
        self._terminal_uncommitted[record.job_id] = record
        self._local_jobs[record.job_id] = record
        done2, _ = await asyncio.wait({fin_task}, timeout=max(0.2, self._finalize_retry_seconds))
        if fin_task in done2:
            return bool(fin_task.result()) if not fin_task.cancelled() and fin_task.exception() is None else False
        return False

    async def _execute(
        self,
        record: JobRecord,
        request: RunRequest,
        done_event: asyncio.Event,
        tracker: ProcessTracker,
        deadline: float,
    ) -> None:
        runner = self._runner_for(request.backend)
        progress_pending: list[dict[str, Any]] = []
        progress_flusher: asyncio.Task[None] | None = None
        fatal_progress_exc: BaseException | None = None
        active_runner_task: asyncio.Task[RunResult] | None = None
        committed_session_ids: set[str] = {request.session_id} if request.session_id else set()
        pending_session_ids: set[str] = set()
        session_claim_tasks: list[asyncio.Task[None]] = []
        last_observed_session_id: str | None = request.session_id
        recorded_attempts: list[dict[str, Any]] = []

        async def _flush_progress_loop() -> None:
            nonlocal progress_flusher, fatal_progress_exc
            try:
                while progress_pending:
                    merged: dict[str, Any] = {}
                    lock_t = 0.4
                    while progress_pending:
                        item = progress_pending.pop(0)
                        lt = float(item.pop("_lock_timeout", 0.4))
                        lock_t = max(lock_t, lt)
                        merged.update(item)
                    if not merged:
                        continue
                    try:
                        await self._db(
                            self.store.update_job_progress,
                            record.job_id,
                            owner_id=self.owner_id,
                            lock_timeout=lock_t,
                            **merged,
                        )
                    except Exception as exc:
                        if is_sqlite_lock_error(exc):
                            logger.debug("Skipping locked progress write for %s: %s", record.job_id, exc)
                            continue
                        # Non-lock storage failure (e.g., disk/schema/injected error): abort runner so child dies!
                        fatal_progress_exc = exc
                        if active_runner_task is not None and not active_runner_task.done():
                            active_runner_task.cancel()
                        return
            finally:
                progress_flusher = None

        def _schedule_progress(*, lock_timeout: float = 0.4, **fields: Any) -> None:
            nonlocal progress_flusher
            fields["_lock_timeout"] = lock_timeout
            progress_pending.append(fields)
            if progress_flusher is None or progress_flusher.done():
                progress_flusher = asyncio.create_task(_flush_progress_loop())

        async def _drain_progress(*, cancel_session_claims: bool = False) -> None:
            if session_claim_tasks:
                pending_claims = [t for t in session_claim_tasks if not t.done()]
                if pending_claims:
                    if cancel_session_claims:
                        for t in pending_claims:
                            t.cancel()
                        await asyncio.gather(*pending_claims, return_exceptions=True)
                    else:
                        try:
                            await asyncio.wait_for(
                                asyncio.gather(*pending_claims, return_exceptions=True),
                                timeout=0.3,
                            )
                        except Exception:
                            for t in pending_claims:
                                if not t.done():
                                    t.cancel()
                            await asyncio.gather(*pending_claims, return_exceptions=True)
            if progress_flusher is not None and not progress_flusher.done():
                try:
                    await asyncio.wait_for(asyncio.shield(progress_flusher), timeout=0.5)
                except Exception:
                    pass

        def on_session(session_id: str) -> None:
            nonlocal last_observed_session_id, fatal_progress_exc
            last_observed_session_id = session_id
            if session_id in committed_session_ids or session_id in pending_session_ids:
                return
            pending_session_ids.add(session_id)

            async def _commit_and_publish_session(sid: str) -> None:
                nonlocal fatal_progress_exc
                try:
                    await self._commit_required_progress(
                        record.job_id,
                        deadline=deadline,
                        description=f"claiming session {sid!r}",
                        backend=record.backend,
                        session_id=sid,
                    )
                except asyncio.CancelledError:
                    return
                except asyncio.TimeoutError:
                    return
                except Exception as exc:
                    fatal_progress_exc = exc
                    if active_runner_task is not None and not active_runner_task.done():
                        active_runner_task.cancel()
                    return
                finally:
                    pending_session_ids.discard(sid)
                committed_session_ids.add(sid)
                if not record.is_terminal:
                    record.session_id = sid

            session_claim_tasks.append(
                asyncio.create_task(
                    _commit_and_publish_session(session_id),
                    name=f"claim-session-{record.job_id}",
                )
            )

        def on_model(model: str) -> None:
            if record.model != model:
                record.model = model
                record.quota_pool = classify_quota_pool(record.backend, model)
                _schedule_progress(lock_timeout=0.5, model=model, quota_pool=record.quota_pool)

        def on_activity(activity: str) -> None:
            record.last_activity = activity
            _schedule_progress(lock_timeout=0.3, last_activity=activity)

        async def on_provider_start() -> None:
            if not record.provider_invoked:
                await self._commit_required_progress(
                    record.job_id,
                    deadline=deadline,
                    description=f"recording launch intent for {record.job_id}",
                    status="running",
                    provider_invoked=True,
                )
                record.provider_invoked = True

        async def on_attempt(attempt_summary: dict[str, Any]) -> None:
            recorded_attempts.append(attempt_summary)
            record.attempts = list(recorded_attempts)
            try:
                await self._db(
                    self.store.record_attempt_usage,
                    record,
                    attempt_summary,
                    attempt_number=int(attempt_summary.get("attempt") or len(recorded_attempts)),
                    is_final=False,
                    lock_timeout=1.5,
                )
            except Exception as exc:
                if is_sqlite_lock_error(exc):
                    logger.debug(
                        "Deferring intermediate attempt usage write for %s: %s",
                        record.job_id,
                        exc,
                    )
                else:
                    logger.warning(
                        "Failed recording intermediate attempt usage for %s: %s",
                        record.job_id,
                        exc,
                    )

        def _prepare_session_for_finalization(candidate_sid: str | None) -> None:
            sid = candidate_sid or last_observed_session_id
            if sid:
                record.session_id = sid
                if sid not in committed_session_ids:
                    self._uncommitted_session_ids.add(record.job_id)

        def _keep_cancelled_usage(result: RunResult | None) -> None:
            _prepare_session_for_finalization(result.session_id if result else None)
            if result is None:
                return
            if recorded_attempts and result.attempts:
                last_attempt = result.attempts[-1]
                if last_attempt.get("usage") is not None:
                    record.usage = last_attempt["usage"]
                    record.usage_status = last_attempt["usage_status"]
                    record.provider_metrics = last_attempt.get("provider_metrics")
            elif result.usage is not None:
                record.usage = result.usage
                record.usage_status = result.usage_status
                record.provider_metrics = result.provider_metrics

        def _attach_interrupted_attempts() -> list[dict[str, Any]] | None:
            if not recorded_attempts:
                return None
            prior_dur = sum(float(a.get("duration_seconds") or 0.0) for a in recorded_attempts)
            wall_dur = float(record.duration_seconds or 0.0)
            total_dur = round(max(wall_dur, prior_dur), 2)
            record.duration_seconds = total_dur
            rem_dur = round(max(0.0, total_dur - prior_dur), 2)
            final_att: dict[str, Any] = {
                "attempt": len(recorded_attempts) + 1,
                "status": record.status,
                "model": record.model or record.requested_model or f"{record.backend}-default",
                "quota_pool": record.quota_pool
                or classify_quota_pool(record.backend, record.model or record.requested_model),
                "duration_seconds": rem_dur,
                "usage_status": record.usage_status,
            }
            if record.usage is not None:
                final_att["usage"] = record.usage
            if record.provider_metrics is not None:
                final_att["provider_metrics"] = record.provider_metrics
            if record.effort:
                final_att["effort"] = record.effort
            if record.failure_reason:
                final_att["failure_reason"] = record.failure_reason
            if record.error:
                final_att["error"] = record.error
            combined = [*recorded_attempts, final_att]
            record.attempts = combined
            agg_usage = _sum_attempt_usage(combined)
            if agg_usage is not None:
                record.usage = agg_usage
                record.usage_status = "partial"
            return combined

        def on_worktree_planned(planned: dict[str, str | None]) -> None:
            wt_p = planned.get("worktree_path")
            wt_b = planned.get("worktree_branch")
            wt_c = planned.get("worktree_commit")
            eff = planned.get("effective_cwd")
            if wt_p:
                record.worktree_path = wt_p
                request.worktree_path = wt_p
            if wt_b:
                record.worktree_branch = wt_b
                request.worktree_branch = wt_b
            if wt_c:
                record.worktree_commit = wt_c
                request.worktree_commit = wt_c
            if planned.get("git_common_dir"):
                request.git_common_dir = planned["git_common_dir"]
            _schedule_progress(
                lock_timeout=1.5,
                worktree_path=record.worktree_path,
                worktree_branch=record.worktree_branch,
                worktree_commit=record.worktree_commit,
                last_activity=f"creating git worktree ({wt_b or wt_p or eff})",
            )

        acquired_sem: asyncio.Semaphore | None = None
        result: RunResult | None = None
        finalized_committed = False
        tracker_token = CURRENT_PROCESS_TRACKER.set(tracker)

        try:
            # 1. Atomically claim the session in SQLite (works across windows AND same window with no deadlock).
            if request.session_id:
                await self._acquire_session_claim(
                    record,
                    request.session_id,
                    deadline,
                    confirm_prior_job_id=request.confirm_prior_job_id,
                )
                # Prior turn has now finalized: refresh its final effective_cwd / worktree path!
                await self._refresh_resumed_context_async(record, request)

            # 2. Acquire per-bridge concurrency slot within the same overall deadline.
            rem = deadline - time.monotonic()
            if rem <= 0.05:
                raise asyncio.TimeoutError("Job deadline expired while queued for concurrency slot.")
            sem_ref = self._semaphore
            await asyncio.wait_for(sem_ref.acquire(), timeout=rem)
            acquired_sem = sem_ref

            started_ts = time.time()
            starting_activity = f"starting {request.backend}"
            await self._commit_required_progress(
                record.job_id,
                deadline=deadline,
                description=f"marking job {record.job_id} running",
                status="running",
                started_at=started_ts,
                last_activity=starting_activity,
            )
            record.status = "running"
            record.started_at = started_ts
            record.last_activity = starting_activity

            # 3. Pre-assign a dedicated Git worktree BEFORE launch and persist planned path before invoking Git.
            if request.backend in WORKTREE_BACKENDS and request.worktree:
                record.last_activity = "preparing git worktree"
                _schedule_progress(lock_timeout=0.5, last_activity=record.last_activity)
                wt_info = await prepare_job_worktree(
                    job_id=record.job_id,
                    cwd=request.cwd,
                    state_dir=self.settings.state_dir,
                    existing_worktree_path=request.worktree_path if not request.relocate_cwd else None,
                    existing_worktree_branch=request.worktree_branch if not request.relocate_cwd else None,
                    existing_worktree_commit=request.worktree_commit if not request.relocate_cwd else None,
                    deadline=deadline,
                    on_worktree_planned=on_worktree_planned,
                )
                request.cwd = str(wt_info["effective_cwd"] or request.cwd)
                request.worktree_path = wt_info["worktree_path"]
                request.worktree_branch = wt_info["worktree_branch"]
                request.worktree_commit = wt_info["worktree_commit"]
                request.git_common_dir = wt_info["git_common_dir"]

                record.effective_cwd = request.cwd
                record.worktree_path = request.worktree_path
                record.worktree_branch = request.worktree_branch
                record.worktree_commit = request.worktree_commit
                try:
                    await self._db(
                        self.store.update_job_progress,
                        record.job_id,
                        owner_id=self.owner_id,
                        effective_cwd=record.effective_cwd,
                        worktree_path=record.worktree_path,
                        worktree_branch=record.worktree_branch,
                        worktree_commit=record.worktree_commit,
                        last_activity=f"worktree ready ({record.worktree_branch or record.worktree_path})",
                        lock_timeout=1.5,
                    )
                except Exception:
                    pass

            prior_watermark = (
                await self._db(self.store.get_session_watermark, request.backend, request.session_id)
                if request.backend == "neon" and request.session_id
                else None
            )

            active_runner_task = asyncio.create_task(
                runner.run(
                    request,
                    deadline=deadline,
                    prior_watermark=prior_watermark,
                    on_session=on_session,
                    on_activity=on_activity,
                    on_model=on_model,
                    on_provider_start=on_provider_start,
                    on_attempt=on_attempt,
                )
            )
            try:
                result = await active_runner_task
            except asyncio.CancelledError:
                if fatal_progress_exc is not None:
                    raise fatal_progress_exc from None
                raise
            finally:
                if not active_runner_task.done():
                    active_runner_task.cancel()
                    await asyncio.gather(active_runner_task, return_exceptions=True)
                active_runner_task = None
            await _drain_progress()
            if fatal_progress_exc is not None:
                raise fatal_progress_exc
        except asyncio.CancelledError as exc:
            self._cancelled_ids.discard(record.job_id)
            work_stop_wall = time.time()
            c0 = time.monotonic()
            stopped = await tracker.terminate_all_and_verify()
            cleanup_secs = round(max(time.monotonic() - c0, tracker.total_cleanup_seconds), 2)
            await _drain_progress(cancel_session_claims=True)
            _keep_cancelled_usage(exc.result if isinstance(exc, RunCancelled) else result)
            record.status = "cancelled"
            record.failure_reason = "cancelled"
            record.provider_state = "stopped" if stopped else "possibly_running"
            record.error = "Job cancelled."
            record.retry_guidance = (
                "Subprocess tree was verified stopped. For edit tasks inspect git status/diff before retrying."
                if stopped
                else "Job was cancelled, but child process termination could not be verified. "
                "Verify no child process remains and inspect git status/diff before retrying."
            )
            record.finished_at = time.time()
            record.duration_seconds = (
                round(max(0.0, work_stop_wall - record.started_at), 2)
                if record.started_at is not None
                else 0.0
            )
            record.cleanup_seconds = cleanup_secs
            record.last_activity = "cancelled"
            final_attempts = _attach_interrupted_attempts()
            finalized_committed = await self._finalize_job_durable(
                record,
                request_session_id=request.session_id,
                touched_provider=record.provider_invoked,
                attempts=final_attempts,
            )
            return
        except SessionResumeBlockedError as exc:
            await _drain_progress(cancel_session_claims=True)
            record.status = "failed"
            record.failure_reason = "prior_turn_uncertain"
            record.provider_state = "stopped"
            record.error = str(exc)
            record.retry_guidance = (
                f"Prior turn {exc.prior_job_id!r} ended with provider_state={exc.prior_provider_state!r}. "
                "Confirm that exact prior turn has stopped (and inspect git status/diff for edits), "
                f"then retry with confirm_prior_job_id={exc.prior_job_id!r} (or confirm_prior_stopped=True)."
            )
            record.finished_at = time.time()
            record.duration_seconds = 0.0
            record.last_activity = "prior_turn_uncertain"
            finalized_committed = await self._finalize_job_durable(
                record,
                request_session_id=request.session_id,
                touched_provider=False,
            )
            return
        except WorktreeMissingError as exc:
            await _drain_progress(cancel_session_claims=True)
            record.status = "failed"
            record.failure_reason = "worktree_missing"
            record.provider_state = "stopped"
            record.error = str(exc)
            record.retry_guidance = (
                "Recorded Git worktree is missing. Inspect `git worktree list`, then retry with "
                "relocate_cwd=True (and cwd) to intentionally move the session or worktree=True "
                "(with mode='edit') to create a new worktree."
            )
            record.finished_at = time.time()
            record.duration_seconds = 0.0
            record.last_activity = "worktree_missing"
            finalized_committed = await self._finalize_job_durable(
                record,
                request_session_id=request.session_id,
                touched_provider=False,
            )
            return
        except asyncio.TimeoutError as exc:
            work_stop_wall = time.time()
            c0 = time.monotonic()
            stopped = await tracker.terminate_all_and_verify()
            cleanup_secs = round(max(time.monotonic() - c0, tracker.total_cleanup_seconds), 2)
            await _drain_progress(cancel_session_claims=True)
            _prepare_session_for_finalization(None)
            record.status = "timed_out"
            record.failure_reason = "timeout"
            record.provider_state = "stopped" if stopped else "possibly_running"
            record.error = str(exc) or f"Job timed out after {request.timeout_seconds}s."
            record.retry_guidance = (
                "Job deadline expired while queued or waiting on an active session turn or storage lock. "
                "Increase timeout_seconds or wait for active work to finish."
            )
            record.finished_at = time.time()
            record.duration_seconds = (
                round(max(0.0, work_stop_wall - record.started_at), 2)
                if record.started_at is not None
                else 0.0
            )
            record.cleanup_seconds = cleanup_secs
            record.last_activity = "timed_out"
            final_attempts = _attach_interrupted_attempts()
            finalized_committed = await self._finalize_job_durable(
                record,
                request_session_id=request.session_id,
                touched_provider=record.provider_invoked,
                attempts=final_attempts,
            )
            return
        except Exception as exc:
            logger.exception("Unhandled error running job %s", record.job_id)
            work_stop_wall = time.time()
            c0 = time.monotonic()
            stopped = await tracker.terminate_all_and_verify()
            cleanup_secs = round(max(time.monotonic() - c0, tracker.total_cleanup_seconds), 2)
            await _drain_progress(cancel_session_claims=True)
            _prepare_session_for_finalization(None)
            record.status = "failed"
            record.failure_reason = (
                "launch_error" if isinstance(exc, ProcessContainmentError) else "cli_error"
            )
            record.provider_state = "stopped" if stopped else "possibly_running"
            record.error = f"{type(exc).__name__}: {exc}"
            record.retry_guidance = (
                "Check CLI installation with list_backends and inspect error."
                if stopped
                else "Subprocess cleanup could not be verified after an internal error; inspect running "
                "processes and repository state before retrying."
            )
            record.finished_at = time.time()
            record.duration_seconds = (
                round(max(0.0, work_stop_wall - record.started_at), 2)
                if record.started_at is not None
                else 0.0
            )
            record.cleanup_seconds = cleanup_secs if cleanup_secs > 0 else None
            record.last_activity = "failed"
            final_attempts = _attach_interrupted_attempts()
            finalized_committed = await self._finalize_job_durable(
                record,
                request_session_id=request.session_id,
                touched_provider=record.provider_invoked,
                attempts=final_attempts,
            )
            return
        else:
            if (
                not recorded_attempts
                and not result.provider_invoked
                and result.status in {"failed", "timed_out"}
                and result.failure_reason in {"launch_error", "timeout", "model_unavailable", "cli_not_found"}
            ):
                record.provider_invoked = False
            else:
                record.provider_invoked = (
                    record.provider_invoked
                    or result.provider_invoked
                    or (result.status == "completed")
                )
            if record.job_id in self._cancelled_ids:
                self._cancelled_ids.discard(record.job_id)
                work_stop_wall = time.time()
                c0 = time.monotonic()
                stopped = await tracker.terminate_all_and_verify()
                cleanup_secs = round(max(time.monotonic() - c0, tracker.total_cleanup_seconds), 2)
                await _drain_progress(cancel_session_claims=True)
                _keep_cancelled_usage(result)
                record.status = "cancelled"
                record.failure_reason = "cancelled"
                record.provider_state = "stopped" if stopped else "possibly_running"
                record.error = "Job cancelled."
                record.retry_guidance = (
                    "Subprocess tree was verified stopped. For edit tasks inspect git status/diff before retrying."
                    if stopped
                    else "Subprocess termination could not be verified after cancellation."
                )
                record.finished_at = time.time()
                record.duration_seconds = (
                    round(max(0.0, work_stop_wall - record.started_at), 2)
                    if record.started_at is not None
                    else 0.0
                )
                record.cleanup_seconds = cleanup_secs
                record.last_activity = "cancelled"
                final_attempts = _attach_interrupted_attempts()
                finalized_committed = await self._finalize_job_durable(
                    record,
                    request_session_id=request.session_id,
                    touched_provider=record.provider_invoked,
                    attempts=final_attempts,
                )
                return

            await _drain_progress(cancel_session_claims=True)
            record.status = result.status
            record.response = result.response
            _prepare_session_for_finalization(result.session_id)
            if result.model:
                record.model = result.model
            record.quota_pool = classify_quota_pool(
                record.backend,
                record.model or record.requested_model,
            )
            if result.effort:
                record.effort = result.effort

            # Surface any quota-pool change on resume explicitly alongside model notices!
            notice_parts: list[str] = []
            if request.resume_notice:
                notice_parts.append(request.resume_notice)
            elif (
                request.session_id
                and request.prior_quota_pool
                and record.quota_pool
                and record.quota_pool != request.prior_quota_pool
            ):
                notice_parts.append(
                    f"Quota pool changed on resume: prior turn used {request.prior_quota_pool!r} "
                    f"({request.prior_model or 'unknown'}), this turn ran on {record.quota_pool!r} "
                    f"({record.model or record.requested_model or 'unknown'})."
                )
            if result.model_notice:
                notice_parts.append(result.model_notice)
            record.model_notice = " ".join(notice_parts) if notice_parts else None

            if result.effective_cwd:
                record.effective_cwd = result.effective_cwd
            if result.worktree_path:
                record.worktree_path = result.worktree_path
            if result.worktree_branch:
                record.worktree_branch = result.worktree_branch
            if result.worktree_commit:
                record.worktree_commit = result.worktree_commit
            record.usage = result.usage
            record.provider_metrics = result.provider_metrics
            record.usage_status = result.usage_status
            record.attempts = result.attempts if len(result.attempts) > 1 else None
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
            record.cleanup_seconds = (
                round(result.cleanup_seconds, 2)
                if result.cleanup_seconds is not None
                else (round(tracker.total_cleanup_seconds, 2) if tracker.total_cleanup_seconds > 0 else None)
            )
            record.last_activity = record.status

            finalized_committed = await self._finalize_job_durable(
                record,
                request_session_id=request.session_id,
                watermark_cumulative=result.cumulative_usage,
                watermark_turns=result.num_turns,
                touched_provider=record.provider_invoked,
                attempts=result.attempts if len(result.attempts) > 1 else None,
            )
            # Only after the job's terminal row is committed: a quota reading is a nicety, and nothing
            # that happens while storing it (a lock, a cancel) may stand between a finished job and its row.
            if finalized_committed and record.backend == "claude":
                await self._remember_claude_rate_limit(result.provider_metrics)
        finally:
            CURRENT_PROCESS_TRACKER.reset(tracker_token)
            if tracker.has_alive_processes():
                tracker.terminate_all_sync()
            if acquired_sem is not None:
                acquired_sem.release()
            # Only release session_claims and drop local state once SQLite terminal write has committed!
            if finalized_committed:
                try:
                    await self._db(self.store.release_session_claims, record.job_id)
                except Exception:
                    pass
                self._notify_session_waiters(record.backend, request.session_id)
                if record.session_id and record.session_id != request.session_id:
                    self._notify_session_waiters(record.backend, record.session_id)
                done_event.set()
                self._cleanup_local_job(record.job_id)
            else:
                # Signal waiter with the in-memory terminal record while background committer retries.
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
        record = await self.start_async(request)
        ev = self._events.get(record.job_id)
        try:
            if ev is not None:
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
        return await self.get_async(record.job_id)

    async def run_parallel(
        self,
        requests: list[RunRequest],
        *,
        wait: bool = True,
        wait_seconds: int | None = None,
    ) -> list[JobRecord]:
        records = [await self.start_async(req) for req in requests]
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
        uncommitted = self._terminal_uncommitted.get(job_id)
        if uncommitted is not None:
            return self._visible_record(uncommitted)
        rec = self.store.get_job(job_id)
        if rec is not None:
            if not rec.is_terminal:
                local = self._local_jobs.get(job_id)
                if local is not None and local.owner_id == self.owner_id:
                    return self._visible_record(local)
                if rec.owner_id != self.owner_id:
                    if not self.store.is_owner_alive(
                        rec.owner_id,
                        is_pid_alive_fn=self._is_pid_alive,
                        stale_seconds=self.settings.owner_stale_seconds,
                    ):
                        self.store.recover_dead_owner_jobs(
                            self.owner_id,
                            is_pid_alive_fn=self._is_pid_alive,
                            stale_seconds=self.settings.owner_stale_seconds,
                        )
                        refreshed = self.store.get_job(job_id)
                        if refreshed is not None:
                            return refreshed
            return rec
        local = self._local_jobs.get(job_id)
        if local is not None:
            return self._visible_record(local)
        raise KeyError(f"Unknown job_id: {job_id!r}")

    async def get_async(self, job_id: str) -> JobRecord:
        uncommitted = self._terminal_uncommitted.get(job_id)
        if uncommitted is not None:
            return self._visible_record(uncommitted)
        return await self._db(self.get, job_id)

    async def wait_for(
        self,
        job_ids: list[str],
        *,
        wait_seconds: int = 0,
        wait_mode: Literal["all", "any"] = "all",
    ) -> list[JobRecord]:
        records = [await self.get_async(jid) for jid in job_ids]
        if wait_seconds <= 0:
            return records

        if wait_mode == "any" and any(r.is_terminal for r in records):
            return records
        if wait_mode == "all" and all(r.is_terminal for r in records):
            return records

        deadline = time.monotonic() + max(0, wait_seconds)

        # Re-check any non-terminal job whose local event was already cleaned up on completion.
        local_events: list[asyncio.Event] = []
        remote_pending: list[str] = []
        for idx, (jid, rec) in enumerate(zip(job_ids, records)):
            if rec.is_terminal:
                continue
            ev = self._events.get(jid)
            if ev is not None:
                local_events.append(ev)
            else:
                uncommitted = self._terminal_uncommitted.get(jid)
                latest = (
                    self._visible_record(uncommitted)
                    if uncommitted is not None
                    else await self._db(self.store.get_job, jid)
                )
                if latest is not None:
                    records[idx] = latest
                    if latest.is_terminal:
                        continue
                remote_pending.append(jid)

        if wait_mode == "any" and any(r.is_terminal for r in records):
            return records
        if wait_mode == "all" and all(r.is_terminal for r in records):
            return records

        # Fast path: all pending jobs belong to this process -> pure event wait, zero polling!
        if not remote_pending and local_events:
            waiters = [asyncio.create_task(ev.wait()) for ev in local_events]
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
            return [
                self._visible_record(self._terminal_uncommitted[jid])
                if jid in self._terminal_uncommitted
                else ((await self._db(self.store.get_job, jid)) or rec)
                for jid, rec in zip(job_ids, records)
            ]

        # Cross-window path: poll SQLite off the event loop while checking for remote owner crashes.
        while time.monotonic() < deadline:
            try:
                await self._db(
                    self.store.recover_dead_owner_jobs,
                    self.owner_id,
                    is_pid_alive_fn=self._is_pid_alive,
                    stale_seconds=self.settings.owner_stale_seconds,
                )
            except Exception:
                pass
            current = [
                self._visible_record(self._terminal_uncommitted[jid])
                if jid in self._terminal_uncommitted
                else ((await self._db(self.store.get_job, jid)) or rec)
                for jid, rec in zip(job_ids, records)
            ]
            if wait_mode == "any" and any(r.is_terminal for r in current):
                return current
            if wait_mode == "all" and all(r.is_terminal for r in current):
                return current
            await asyncio.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

        return [
            self._visible_record(self._terminal_uncommitted[jid])
            if jid in self._terminal_uncommitted
            else ((await self._db(self.store.get_job, jid)) or rec)
            for jid, rec in zip(job_ids, records)
        ]

    async def cancel(self, job_id: str) -> JobRecord:
        # Never cancel a job that has already reached terminal state and is committing its terminal write!
        if job_id in self._finalizing_records:
            fin_task = self._pending_finalizations.get(job_id)
            if fin_task is not None and not fin_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(fin_task), timeout=2.0)
                except Exception:
                    pass
            fin_rec = self._terminal_uncommitted.get(job_id) or self._finalizing_records.get(job_id)
            if fin_rec is not None and fin_rec.is_terminal:
                return self._visible_record(fin_rec)
            return await self.get_async(job_id)

        record = await self.get_async(job_id)
        if record.is_terminal:
            return record

        # Cross-window protection using the same unified dead-owner check as status/recovery.
        if record.owner_id != self.owner_id:
            if await self._db(
                self.store.is_owner_alive,
                record.owner_id,
                is_pid_alive_fn=self._is_pid_alive,
                stale_seconds=self.settings.owner_stale_seconds,
            ):
                raise RuntimeError(
                    f"Job {job_id!r} is owned by another live bridge process ({record.owner_id}) "
                    "and must be cancelled from that window."
                )
            await self._db(
                self.store.recover_dead_owner_jobs,
                self.owner_id,
                is_pid_alive_fn=self._is_pid_alive,
                stale_seconds=self.settings.owner_stale_seconds,
            )
            return await self.get_async(job_id)

        local_record = self._local_jobs.get(job_id, record)
        tracker = self._trackers.get(job_id)
        ev = self._events.get(job_id)
        self._cancelled_ids.add(job_id)

        # Single-owner cleanup: cancel the job's task so its ManagedProcess terminates the tree.
        task = self._tasks.get(job_id)
        task_finished = task is None or task.done()
        if task is not None and not task.done() and job_id not in self._finalizing_records:
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=6.0)
                task_finished = True
            except asyncio.CancelledError:
                task_finished = task.done()
            except asyncio.TimeoutError:
                task_finished = task.done()

        if job_id in self._finalizing_records:
            fin_rec = self._terminal_uncommitted.get(job_id) or self._finalizing_records.get(job_id)
            if fin_rec is not None and fin_rec.is_terminal:
                return self._visible_record(fin_rec)

        uncommitted = self._terminal_uncommitted.get(job_id)
        if uncommitted is not None and uncommitted.is_terminal:
            return self._visible_record(uncommitted)
        latest = await self._db(self.store.get_job, job_id)
        if latest is not None and latest.is_terminal:
            return latest

        # Task was cancelled before entering `_execute()` or hung during cleanup: verify stop explicitly!
        work_stop_wall = time.time()
        c0 = time.monotonic()
        stopped = True
        if tracker is not None:
            stopped = await tracker.terminate_all_and_verify()
        cleanup_secs = round(
            max(time.monotonic() - c0, tracker.total_cleanup_seconds if tracker else 0.0),
            2,
        )
        verified_stopped = stopped and task_finished

        if not local_record.is_terminal:
            self._cancelled_ids.discard(job_id)
            local_record.status = "cancelled"
            local_record.failure_reason = "cancelled"
            local_record.provider_state = "stopped" if verified_stopped else "possibly_running"
            local_record.error = "Job cancelled."
            local_record.retry_guidance = (
                "Job was cancelled and verified stopped."
                if verified_stopped
                else "Job cancellation requested, but process cleanup did not finish within timeout. "
                "Verify no child process remains and inspect git status/diff before retrying."
            )
            local_record.finished_at = time.time()
            local_record.duration_seconds = (
                round(max(0.0, work_stop_wall - local_record.started_at), 2)
                if local_record.started_at is not None
                else 0.0
            )
            local_record.cleanup_seconds = cleanup_secs
            local_record.last_activity = "cancelled"
            committed = await self._finalize_job_durable(
                local_record,
                touched_provider=local_record.provider_invoked,
            )
            if ev is not None:
                ev.set()
            if task_finished and committed:
                self._cleanup_local_job(job_id)

        return await self.get_async(job_id)

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
        merged: list[JobRecord] = [
            self._visible_record(self._terminal_uncommitted[r.job_id])
            if r.job_id in self._terminal_uncommitted
            else r
            for r in recs
        ]
        return [r.to_summary() for r in merged]

    async def list_jobs_async(
        self,
        *,
        limit: int = 20,
        status: str | None = None,
        backend: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            await self._db(
                self.store.recover_dead_owner_jobs,
                self.owner_id,
                is_pid_alive_fn=self._is_pid_alive,
                stale_seconds=self.settings.owner_stale_seconds,
            )
        except Exception:
            pass
        norm_backend = normalize_backend(backend) if backend and backend.strip() else None
        norm_status = status.strip().lower() if status and status.strip() else None
        norm_cwd = str(Path(cwd).expanduser().resolve()) if cwd and cwd.strip() else None
        norm_parent = parent_run.strip() if parent_run and parent_run.strip() else None
        recs = await self._db(
            self.store.list_jobs,
            limit=limit,
            status=norm_status,
            backend=norm_backend,
            cwd=norm_cwd,
            parent_run=norm_parent,
        )
        merged: list[JobRecord] = [
            self._visible_record(self._terminal_uncommitted[r.job_id])
            if r.job_id in self._terminal_uncommitted
            else r
            for r in recs
        ]
        return [r.to_summary() for r in merged]

    async def session_compact_summary_async(self) -> dict[str, Any]:
        return await self._db(self.store.session_compact_summary, self.owner_id)

    async def get_usage_summary(
        self,
        *,
        period: UsagePeriod = "today",
        backend: str | None = None,
        quota_pool: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
        include_quota: bool = False,
        refresh_quota: bool = False,
        compact: bool = False,
        recent_limit: int = 10,
    ) -> dict[str, Any]:
        try:
            await self._db(
                self.store.recover_dead_owner_jobs,
                self.owner_id,
                is_pid_alive_fn=self._is_pid_alive,
                stale_seconds=self.settings.owner_stale_seconds,
            )
        except Exception:
            pass
        norm_backend = normalize_backend(backend) if backend and backend.strip() else None
        norm_pool = normalize_quota_pool(quota_pool)
        norm_cwd = str(Path(cwd).expanduser().resolve()) if cwd and cwd.strip() else None
        norm_parent = parent_run.strip() if parent_run and parent_run.strip() else None

        summary = await self._db(
            self.store.summarize_usage,
            period=period,
            owner_id=self.owner_id,
            backend=norm_backend,
            quota_pool=norm_pool,
            cwd=norm_cwd,
            parent_run=norm_parent,
            recent_limit=recent_limit,
            compact=compact,
        )

        if include_quota:
            if norm_backend:
                targets = [norm_backend]
            elif norm_pool == "codex":
                targets = ["codex"]
            elif norm_pool == "claude":
                targets = ["claude"]
            elif norm_pool in {"agy_gemini", "agy_claude_gpt"}:
                targets = ["neon"]
            else:
                targets = list(ALL_BACKENDS)
            quota = await self.get_quota(targets, refresh=refresh_quota)
            summary["quota"] = quota
            summary["quota_advisory"] = self._build_quota_advisory(quota)

        return summary

    async def usage_dashboard(
        self,
        *,
        period: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Everything the bridge has run in a period, from its records only; see `dashboard.py`.

        It reads and never writes. A job left running by a window that has closed is not recovered here,
        as `get_usage` would: the dashboard lists it under "cannot tell" instead.
        """
        now_ts = time.time()
        window = resolve_window(period, since, until, now_ts=now_ts)
        records = await self._db(
            self.store.dashboard_records,
            since_ts=window.since_ts,
            until_ts=window.until_ts,
            session_owner_id=self.owner_id,
            only_session=window.only_session,
            is_pid_alive_fn=self._is_pid_alive,
        )
        return build_dashboard(records, window=window, owner_id=self.owner_id, now_ts=now_ts)

    def _build_quota_advisory(self, quota: dict[str, Any]) -> dict[str, Any]:
        local_defaults = read_codex_local_defaults()
        eff_codex_model = (
            self.codex_runner._effective_model(None, local_defaults=local_defaults)
            or local_defaults.get("model")
            or "codex-default"
        )
        eff_codex_effort = (
            self.codex_runner._effective_effort(None)
            or local_defaults.get("model_reasoning_effort")
            or "default"
        )
        claude_model, claude_effort = self.claude_runner.effective_model_and_effort(None, None)
        return build_quota_advisory(
            quota,
            neon_pool_models=self.neon_runner.catalog.group_models_by_quota_pool(),
            codex_default_label=f"{eff_codex_model} (effort={eff_codex_effort})",
            claude_default_label=f"{claude_model or 'claude-default'} (effort={claude_effort or 'default'})",
        )

    async def _remember_claude_rate_limit(self, provider_metrics: dict[str, Any] | None) -> None:
        """Keep the plan limits a Claude job reported, as the only quota reading there is for that pool."""
        try:
            info = provider_metrics.get("rate_limit") if isinstance(provider_metrics, dict) else None
            reading = claude_quota_from_rate_limit(info)
            if reading is None:
                return
            previous = await self._db(self.store.get_cached_quota, "claude", float("inf"))
            await self._db(self.store.set_cached_quota, "claude", merge_claude_quota(previous, reading))
        except Exception as exc:
            logger.debug("Could not store Claude rate limit reading: %s", exc)

    async def get_quota(
        self,
        backends: list[str],
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        async def _probe_one(bname: str) -> tuple[str, dict[str, Any]]:
            if bname == "claude":
                # Claude Code has no command that prints the plan limits. The reading is whatever the
                # last Claude job's output carried, however old, until its reset time passes.
                last = await self._db(self.store.get_cached_quota, "claude", float("inf"))
                if last is None or has_passed_reset(last):
                    return bname, {
                        "backend": "claude",
                        "pool": "claude",
                        "status": "unknown",
                        "source": "last_claude_job",
                        "note": CLAUDE_QUOTA_NOTE,
                    }
                return bname, enrich_quota_reading(last)

            if not refresh:
                cached = await self._db(
                    self.store.get_cached_quota,
                    bname,
                    self.settings.quota_cache_ttl_seconds,
                )
                if cached is not None and not has_passed_reset(cached):
                    return bname, enrich_quota_reading(cached)

            if bname == "codex":
                try:
                    cmd = self.codex_runner.resolve_command()
                except Exception as exc:
                    return bname, {"backend": "codex", "pool": "codex", "status": "unknown", "error": str(exc)}
                res = await fetch_codex_quota(cmd)
            else:
                try:
                    cmd = self.neon_runner.resolve_command()
                except Exception as exc:
                    return bname, {"backend": "neon", "status": "unknown", "error": str(exc)}
                res = await fetch_neon_quota(cmd)

            res = enrich_quota_reading(res)
            res["cached"] = False
            if res.get("status") == "ok":
                try:
                    await self._db(self.store.set_cached_quota, bname, res)
                except Exception:
                    pass
            return bname, res

        pairs = await asyncio.gather(*(_probe_one(b) for b in backends))
        return dict(pairs)

    async def shutdown(self, *, flush_timeout: float = 20.0) -> None:
        # 1. Cancel ONLY active jobs that have NOT already reached terminal finalization.
        non_finalizing = [
            jid for jid in list(self._tasks.keys()) if jid not in self._finalizing_records
        ]
        if non_finalizing:
            await asyncio.gather(
                *(self.cancel(jid) for jid in non_finalizing),
                return_exceptions=True,
            )

        # 2. Wait for BOTH in-progress finalizing job tasks AND background finalization/cleanup tasks!
        dl = time.monotonic() + max(0.5, flush_timeout)
        while True:
            wait_tasks = [
                t
                for t in (*self._tasks.values(), *self._pending_finalizations.values())
                if not t.done()
            ]
            if not wait_tasks:
                break
            rem = dl - time.monotonic()
            if rem <= 0:
                for t in wait_tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*wait_tasks, return_exceptions=True)
                break
            await asyncio.wait(wait_tasks, timeout=rem)

        # 3. Only mark owner exited & clear claims once all terminal writes have committed!
        if not self._terminal_uncommitted and not self._finalizing_records:
            try:
                await self._db(self.store.mark_owner_exited, self.owner_id)
            except Exception:
                pass
        else:
            logger.warning(
                "Shutdown flush timeout reached with %d uncommitted terminal job(s); skipping mark_owner_exited.",
                len(self._terminal_uncommitted) or len(self._finalizing_records),
            )
