"""Fast, lightweight stdio MCP server exposing Neon (`agy`), Codex (`codex`) and Claude (`claude`) subagents.

Any MCP client can be the host: Claude Code, Codex, Antigravity or another. Nothing here assumes which.

Five tools: `run` starts one job and answers at once, `jobs` reads, waits for and pages jobs, `cancel`
stops one, `usage` reports what was run and what quota is left, `backends` checks the CLIs.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from agent_bridge.config import EffortLevel, Settings, normalize_backend
from agent_bridge.jobs import ALL_BACKENDS, JobManager
from agent_bridge.mcp_tasks import TASK_CALL, JobTasks, JobView, pump_progress
from agent_bridge.model_resolver import ModelPoolUnavailableError
from agent_bridge.models import DEFAULT_RESPONSE_CHARS, JobRecord
from agent_bridge.nesting import UNKNOWN_NESTING, NestedBridgeError, Nesting, detect_nesting
from agent_bridge.quota import build_quota_view
from agent_bridge.status_text import format_summary_line

UsagePeriodName = Literal["today", "7d", "30d", "all", "session"]

# The longest a call may wait, whatever it asks for. Hosts cut tool calls at 60 to 180 seconds.
MAX_WAIT_SECONDS = 300
# The wait a receipt's `next` call suggests: under the shortest host limit known.
NEXT_WAIT_SECONDS = 50
# How often a waiting call reports progress, and re-reads a job another window runs.
PROGRESS_INTERVAL_SECONDS = 5.0

SERVER_INSTRUCTIONS = """
agent-bridge runs the other AI coding CLIs on this machine as subagent jobs, whichever CLI you \
are: `codex`, `neon` (agy; also `agy-claude`, `agy-opus`, `agy-gpt`), `claude`. `run` starts one \
job and returns at once; `jobs` collects replies, or lists jobs; `cancel` stops a job; `usage` \
shows what ran, and with quota=True what quota is left: call that before a fan-out; `backends` \
checks the CLIs. Default mode is read_only. `completed` only means a job ended: read its \
`response`.

- Fan out by calling `run` once per job, in parallel, routed by the `order` of `usage(quota=True)`.
- Collect a job with the call in its receipt's `next`: `jobs(job_ids=[...], wait_seconds=...)`. A \
long reply comes in pages; call again with the `next_offset` it gives.
- `resume_from` takes a job_id or session_id and inherits cwd, worktree, model, effort and \
parent_run. Pass `cwd` on new jobs only.
- `mode="edit"` lets a job change files; `worktree=True` (codex, claude) gives it its own Git worktree.
- To show the user the quota and what the bridge ran, call `usage(view="dashboard")` and show its \
`text` unchanged.
- A job the bridge started cannot start bridge jobs: there `run` is refused with `nested_bridge`.
""".strip()

NESTED_INSTRUCTIONS = """
agent-bridge is running inside one of its own jobs: you are a subagent that another session started \
through the bridge. `run` is refused here with `nested_bridge`: do the work yourself and put the \
answer in your reply. `jobs`, `cancel`, `usage` and `backends` still work.
""".strip()

RUN_DESCRIPTION = (
    "Start ONE subagent job and return a receipt at once; collect its reply with `jobs`. For several "
    "jobs, call `run` once per job, in parallel. `wait_seconds` (max 300) waits that long first: a job "
    "that ends in time is returned in full, one still running gets a receipt. `review` makes it a "
    "read-only code review by this backend (`prompt` is then optional and added to the focus). "
    "`resume_from` continues a session and inherits its cwd, worktree, model, effort and parent_run."
)
JOBS_DESCRIPTION = (
    "With `job_ids`: each job in full, after waiting up to `wait_seconds` (max 300) for all or any of "
    "them to end; a job still queued or running carries on. `response` is cut to `response_chars` from "
    "`response_offset`; a cut reply gives `next_offset`. `completed` only means the job ended: read "
    "`response`. Without `job_ids`: recent jobs, filtered by `status`, `backend`, `cwd`, `parent_run`."
)
CANCEL_DESCRIPTION = "Stop a queued or running job that this window started, and return it."
USAGE_DESCRIPTION = (
    "What the bridge ran in a period: `period`, or `since`/`until` as a date or ISO time. `quota=True` "
    "adds what is left in each subscription pool and the order to use them in (about 10 s; read it "
    "before a fan-out). `view=\"dashboard\"` returns `text` to show the user unchanged; `verbose` adds "
    "its figures. `backend`, `cwd` and `parent_run` filter the summary view."
)
BACKENDS_DESCRIPTION = (
    "Each CLI's health and version, the backend defaults, agy's model shortcuts, and whether this "
    "bridge runs inside a bridge job. `refresh_models` reads agy's model list again."
)


class Review(BaseModel):
    """A read-only code review of a change."""

    scope: Literal["uncommitted", "base", "commit"] = "uncommitted"
    base: str | None = Field(default=None, description="Branch for scope='base' (default main).")
    commit: str | None = Field(default=None, description="SHA for scope='commit'.")
    focus: str | None = None


def _effective_wait(wait_seconds: int | None) -> int:
    """How long a call may wait, in whole seconds: the one place a wait is bounded, for `run` and `jobs`.

    A cap for the host that is calling belongs here, so that every wait obeys it.
    """
    try:
        asked = int(wait_seconds or 0)
    except (TypeError, ValueError):
        asked = 0
    return max(0, min(asked, MAX_WAIT_SECONDS))


def _next_call(job_id: str) -> str:
    return f"jobs(job_ids=['{job_id}'], wait_seconds={_effective_wait(NEXT_WAIT_SECONDS)})"


def _build_review_prompt(
    scope: Literal["uncommitted", "base", "commit"],
    *,
    base: str | None,
    commit: str | None,
    focus: str | None,
) -> str:
    if scope == "uncommitted":
        scope_text = (
            "Review all uncommitted changes in this repository (staged, unstaged, and untracked files; "
            "inspect with `git status` and `git diff` / `git diff --cached`)."
        )
    elif scope == "base":
        target = base or "main"
        scope_text = (
            f"Review all changes on the current branch compared to `{target}` "
            f"(inspect with `git diff {target}...HEAD` and `git log {target}..HEAD --oneline`)."
        )
    else:
        if not commit:
            raise ValueError("review.commit (a SHA) is required when review.scope='commit'.")
        scope_text = (
            f"Review the changes introduced by commit `{commit}` (inspect with `git show {commit}`)."
        )

    focus_block = (
        f"\n\nAdditional focus instructions:\n{focus.strip()}"
        if focus and focus.strip()
        else ""
    )
    return (
        f"{scope_text}\n\n"
        "Report concrete, high-signal findings ordered by severity (bugs, regressions, security issues, "
        "edge cases, missing tests). Cite exact file paths and line numbers. Skip nitpicks and do not "
        "modify any files."
        f"{focus_block}"
    )


def _review_request(
    review: Review,
    *,
    backend: str | None,
    prompt: str,
    mode: str,
    worktree: bool,
    resume_from: str | None,
    label: str | None,
) -> dict[str, Any]:
    """The request fields that make a job a review. Codex with no focus runs its built-in review."""
    if not backend or not backend.strip():
        raise ValueError("A review needs a backend.")
    if mode != "read_only" or worktree:
        raise ValueError("A review is read-only: leave mode at 'read_only' and worktree off.")
    if resume_from and resume_from.strip():
        raise ValueError("A review starts a new session: resume_from cannot be combined with review.")
    scope = review.scope
    base = (review.base or "main") if scope == "base" else review.base
    if scope == "commit" and not (review.commit and review.commit.strip()):
        raise ValueError("review.commit (a SHA) is required when review.scope='commit'.")
    focus = "\n\n".join(p.strip() for p in (review.focus, prompt) if p and p.strip())
    name = normalize_backend(backend)
    out: dict[str, Any] = {"label": label or f"{name}-review-{scope}"}
    if name == "codex" and not focus:
        out.update(prompt="", review_scope=scope, review_base=base, review_commit=review.commit)
    else:
        out["prompt"] = _build_review_prompt(scope, base=base, commit=review.commit, focus=focus or None)
    return out


_ToolFn = TypeVar("_ToolFn", bound=Callable[..., Awaitable[Any]])


def _reported(fn: _ToolFn) -> _ToolFn:
    """Let a tool's own refusals reach the caller with their text.

    The MCP library passes on the message of a `ToolError` only. Anything else it treats as a crash and
    reports as "Error executing tool <name>", with nothing the calling model could act on. The bridge
    refuses with `ValueError` and `RuntimeError` (an unknown backend, a session that is not safe to
    resume, a nested bridge), so those are turned into `ToolError` here.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except (ValueError, NestedBridgeError) as exc:
            raise ToolError(str(exc) or type(exc).__name__) from exc
        except (LookupError, RuntimeError, OSError) as exc:
            # These may be refusals too (a session that is not safe to resume, a missing worktree), or a
            # fault. The caller gets the text either way; the log keeps the traceback in case it is a fault.
            logging.getLogger("agent_bridge.server").warning(
                "Tool %s failed with %s", fn.__name__, type(exc).__name__, exc_info=True
            )
            raise ToolError(str(exc) or type(exc).__name__) from exc

    return wrapper  # type: ignore[return-value]


def _detect_nesting(job_manager: JobManager) -> Nesting:
    """Try to find out whether this bridge runs inside a bridge job."""
    try:
        return detect_nesting(live_owners=job_manager.store.list_live_owners())
    except Exception as exc:
        logging.getLogger("agent_bridge.server").warning("Nesting check failed: %s", exc)
        marker = detect_nesting(ancestors=())
        return marker if marker.nested else UNKNOWN_NESTING


class _LiveLines:
    """The status line of the jobs a call waits for, read without blocking the event loop.

    A job this window runs is read from memory each time. One that another window runs is read from
    the database in a thread every few seconds, and the last copy is used in between.
    """

    def __init__(self, manager: JobManager, job_ids: list[str]) -> None:
        self._manager = manager
        self._ids = list(dict.fromkeys(job_ids))
        self._last: dict[str, JobRecord] = {}

    def records(self) -> list[JobRecord]:
        out = []
        for job_id in self._ids:
            rec = self._manager.peek_local(job_id)
            if rec is not None:
                # Kept, so that a job this window has just finished and let go of stays on the line.
                self._last[job_id] = rec
            else:
                rec = self._last.get(job_id)
            if rec is not None:
                out.append(rec)
        return out

    def line(self) -> str:
        recs = self.records()
        if len(recs) == 1:
            return recs[0].line()
        return format_summary_line([r.line_fields() for r in recs], now=time.time())

    async def refresh(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for job_id in self._ids:
                if self._manager.peek_local(job_id) is None:
                    try:
                        rec = await asyncio.to_thread(self._manager.peek, job_id)
                    except Exception:
                        rec = None
                    if rec is not None:
                        self._last[job_id] = rec
            try:
                await asyncio.wait_for(stop.wait(), timeout=PROGRESS_INTERVAL_SECONDS)
            except TimeoutError:
                pass


async def _report_while_waiting(ctx: Context, lines: _LiveLines, stop: asyncio.Event) -> None:
    """Send the jobs' line as progress until `stop` is set. Returns at once if the host asked for none."""
    refresher = asyncio.create_task(lines.refresh(stop))
    try:
        await pump_progress(ctx, lines.line, interval=PROGRESS_INTERVAL_SECONDS, stop=stop)
    finally:
        refresher.cancel()


def create_server(
    settings: Settings | None = None,
    manager: JobManager | None = None,
    nesting: Nesting | None = None,
) -> tuple[MCPServer, JobManager]:
    """Create and configure the MCPServer instance and its backing JobManager.

    `nesting` is worked out from the environment and the process tree unless it is passed in.
    """
    cfg = settings or Settings.from_env()
    job_manager = manager or JobManager(cfg)
    nest = nesting if nesting is not None else _detect_nesting(job_manager)
    job_manager.nesting = nest

    def _refuse_if_nested(tool: str) -> None:
        nest = job_manager.nesting
        if nest.nested is None:
            nest = job_manager.nesting = _detect_nesting(job_manager)
        if nest.nested:
            raise NestedBridgeError(nest.refusal(tool))

    def _reply(
        record: JobRecord,
        *,
        response_offset: int = 0,
        response_chars: int = DEFAULT_RESPONSE_CHARS,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """The one full reply about a job: a waited `run`, `jobs`, `cancel` and a finished task row."""
        return record.to_detail(response_offset=response_offset, response_chars=response_chars, verbose=verbose)

    def _start_progress(ctx: Context, job_ids: list[str], stop: asyncio.Event) -> asyncio.Task[None]:
        return asyncio.create_task(_report_while_waiting(ctx, _LiveLines(job_manager, job_ids), stop))

    # --- MCP tasks: a host that declares the extension gets each `run` as a task row ---

    def _job_id_from_result(result: Any) -> str | None:
        data = result.get("structuredContent") if isinstance(result, dict) else getattr(
            result, "structured_content", None
        )
        job_id = data.get("job_id") if isinstance(data, dict) else None
        return job_id if isinstance(job_id, str) else None

    async def _task_view(job_id: str) -> JobView | None:
        record = job_manager.peek_local(job_id)
        if record is None:
            record = await asyncio.to_thread(job_manager.peek, job_id)
        if record is None:
            return None
        updated = record.finished_at or record.last_activity_at or record.started_at or record.created_at
        return JobView(
            status=record.status,
            line=record.line(),
            created_at=record.created_at,
            updated_at=updated,
            detail=_reply(record) if record.is_terminal else None,
        )

    job_tasks = JobTasks({"run"}, _job_id_from_result, _task_view, job_manager.cancel)

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            try:
                await job_tasks.aclose()
            finally:
                await job_manager.shutdown()

    mcp = MCPServer(
        name="agent-bridge",
        title="Agent Bridge (Neon, Codex & Claude Subagents)",
        description=(
            "Delegate tasks and code reviews to Antigravity (Neon), Codex CLI and Claude Code subagents "
            "from any MCP host, with shared SQLite state, dynamic models, and usage/quota visibility."
        ),
        instructions=NESTED_INSTRUCTIONS if nest.nested else SERVER_INSTRUCTIONS,
        lifespan=lifespan,
        extensions=[job_tasks],
    )

    # Not marked read-only: a job may edit, and a review on `agy` is read-only by its prompt, not a sandbox.
    @mcp.tool(
        name="run",
        description=RUN_DESCRIPTION,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    )
    @_reported
    async def run(
        ctx: Context,
        backend: Annotated[
            str | None,
            Field(description="codex, neon (agy), agy-claude, agy-opus, agy-gpt or claude. "
                  "May be left out with resume_from."),
        ] = None,
        prompt: str = "",
        cwd: str | None = None,
        mode: Literal["read_only", "edit"] = "read_only",
        model: Annotated[
            str | None,
            Field(description="Model id or shortcut (neon: neon-high, hydrogen-medium, flash; claude: "
                  "sonnet, opus, haiku, fable). Default: the backend's."),
        ] = None,
        effort: EffortLevel | None = None,
        timeout_seconds: Annotated[int | None, Field(description="The job's own deadline.")] = None,
        wait_seconds: Annotated[int, Field(description="Wait up to this long before answering (max 300).")] = 0,
        resume_from: Annotated[str | None, Field(description="A prior job_id or session_id.")] = None,
        worktree: Annotated[bool, Field(description="codex/claude edit jobs: own Git worktree.")] = False,
        extra_dirs: list[str] | None = None,
        label: str | None = None,
        parent_run: Annotated[str | None, Field(description="Groups jobs for jobs and usage.")] = None,
        review: Review | None = None,
        relocate_cwd: Annotated[bool, Field(description="Move a resumed session to cwd.")] = False,
        confirm_prior_job_id: Annotated[
            str | None,
            Field(description="The prior turn's job_id, once you know it stopped, to resume after it."),
        ] = None,
        allow_cross_pool_fallback: Annotated[
            bool, Field(description="Let agy fall back to its other quota pool.")
        ] = False,
    ) -> dict[str, Any]:
        _refuse_if_nested("run")
        fields: dict[str, Any] = {"prompt": prompt, "label": label}
        if review is not None:
            fields = _review_request(
                review, backend=backend, prompt=prompt, mode=mode, worktree=worktree,
                resume_from=resume_from, label=label,
            )
        req = await job_manager.prepare_request_async(
            backend=backend,
            cwd=cwd,
            mode=mode,
            model=model,
            effort=effort,
            resume_from=resume_from,
            relocate_cwd=relocate_cwd,
            worktree=worktree,
            extra_dirs=extra_dirs,
            timeout_seconds=timeout_seconds,
            parent_run=parent_run,
            confirm_prior_job_id=confirm_prior_job_id,
            allow_cross_pool_fallback=allow_cross_pool_fallback,
            **fields,
        )
        # A host that shows the call as a task row collects the job through the task, never by waiting.
        wait = 0 if TASK_CALL.get() else _effective_wait(wait_seconds)
        stop = asyncio.Event()
        progress: list[asyncio.Task[None]] = []

        def _started(record: JobRecord) -> None:
            if wait > 0:
                progress.append(_start_progress(ctx, [record.job_id], stop))

        try:
            record = await job_manager.run(req, wait_seconds=wait, on_started=_started)
        finally:
            stop.set()
            for task in progress:
                task.cancel()
        if record.is_terminal and wait > 0:
            return _reply(record)
        return record.to_receipt(timeout_seconds=req.timeout_seconds, next_call=_next_call(record.job_id))

    @mcp.tool(
        name="jobs",
        description=JOBS_DESCRIPTION,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @_reported
    async def jobs(
        ctx: Context,
        job_ids: list[str] | None = None,
        wait_seconds: int = 0,
        wait_mode: Literal["all", "any"] = "all",
        response_offset: int = 0,
        response_chars: Annotated[int, Field(description="Max 100000.")] = DEFAULT_RESPONSE_CHARS,
        verbose: Annotated[bool, Field(description="Add provider metrics and attempts.")] = False,
        status: str | None = None,
        backend: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if job_ids:
            if response_offset < 0:
                raise ValueError("response_offset cannot be negative.")
            wait = _effective_wait(wait_seconds)
            missing: list[str] = []
            stop = asyncio.Event()
            progress = _start_progress(ctx, job_ids, stop) if wait > 0 else None
            try:
                records = await job_manager.wait_for(
                    job_ids, wait_seconds=wait, wait_mode=wait_mode, missing=missing,
                )
            finally:
                stop.set()
                if progress is not None:
                    progress.cancel()
            details = {
                r.job_id: _reply(r, response_offset=response_offset, response_chars=response_chars, verbose=verbose)
                for r in records
            }
            return {
                "jobs": [
                    details[jid]
                    if jid in details
                    else {
                        "job_id": jid,
                        "status": "not_found",
                        "error": f"No job {jid!r} was found. It may have been removed from the stored "
                        "history, or the id is mistyped.",
                    }
                    for jid in job_ids
                ],
                "all_done": all(r.is_terminal for r in records),
                "any_done": any(r.is_terminal for r in records),
            }

        summaries = await job_manager.list_jobs_async(
            limit=limit,
            status=status,
            backend=backend,
            cwd=cwd,
            parent_run=parent_run,
        )
        return {"count": len(summaries), "jobs": summaries}

    @mcp.tool(
        name="cancel",
        description=CANCEL_DESCRIPTION,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    )
    @_reported
    async def cancel(job_id: str) -> dict[str, Any]:
        return _reply(await job_manager.cancel(job_id))

    @mcp.tool(
        name="usage",
        description=USAGE_DESCRIPTION,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True),
    )
    @_reported
    async def usage(
        period: UsagePeriodName | None = None,
        since: str | None = None,
        until: str | None = None,
        backend: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
        quota: bool = False,
        view: Literal["summary", "dashboard"] = "summary",
        verbose: bool = False,
    ) -> dict[str, Any]:
        if view == "dashboard":
            if any(v and v.strip() for v in (backend, cwd, parent_run)):
                raise ValueError(
                    "The dashboard covers every job in the period: backend, cwd and parent_run filter "
                    "view='summary' only."
                )
            readings: dict[str, Any] = {}
            dashboard = await job_manager.usage_dashboard(
                period=period, since=since, until=until, readings_out=readings,
            )
            out: dict[str, Any] = {"text": dashboard["text"]}
            if verbose:
                # The dashboard's own quota section is in `text`; `quota` only ever has the one shape.
                out.update({k: v for k, v in dashboard.items() if k not in ("text", "quota")})
            if quota:
                out["quota"] = build_quota_view(readings)
            return out

        summary = await job_manager.get_usage_summary(
            period=period, since=since, until=until, backend=backend, cwd=cwd, parent_run=parent_run,
        )
        out = _usage_summary_reply(summary, verbose=verbose)
        if quota:
            # Every pool, whatever the filters: a pool left unread would read as one that failed.
            out["quota"] = build_quota_view(await job_manager.get_quota(list(ALL_BACKENDS)))
        return out

    @mcp.tool(
        name="backends",
        description=BACKENDS_DESCRIPTION,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @_reported
    async def backends(refresh_models: bool = False) -> dict[str, Any]:
        def _unhealthy(exc: Exception) -> dict[str, Any]:
            return {
                "command_found": False,
                "healthy": False,
                "available": False,
                "command": None,
                "version": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

        async def _probe_neon() -> dict[str, Any]:
            try:
                neon_health = await job_manager.neon_runner.check_health()
            except Exception as exc:
                neon_health = _unhealthy(exc)
            info: dict[str, Any] = {
                "backend": "neon",
                "cli": "agy",
                "default_intent": cfg.default_neon_model,
                **neon_health,
            }
            catalog = job_manager.neon_runner.catalog
            if neon_health.get("healthy"):
                try:
                    models = await job_manager.neon_runner.list_models(refresh=refresh_models)
                except Exception as exc:
                    models = []
                    info["catalog_error"] = f"{type(exc).__name__}: {exc}"
                info.update(catalog.status_info())
                info["models"] = models
                info["quota_pools"] = catalog.group_models_by_quota_pool()
                info["resolved_shortcuts"] = catalog.summarize_latest_aliases()
                try:
                    default_res = await catalog.resolve(None, default_intent=cfg.default_neon_model)
                    info["resolved_default_model"] = default_res.resolved
                except ModelPoolUnavailableError as exc:
                    info["resolved_default_model"] = None
                    info["catalog_error"] = str(exc)
                    info["available"] = False
                except Exception as exc:
                    info["resolved_default_model"] = None
                    info["catalog_error"] = f"{type(exc).__name__}: {exc}"
                    info["available"] = False
            else:
                info.update(catalog.status_info())
            return info

        async def _probe_codex() -> dict[str, Any]:
            try:
                codex_health = await job_manager.codex_runner.check_health()
            except Exception as exc:
                codex_health = _unhealthy(exc)
            return {
                "backend": "codex",
                "cli": "codex",
                "default_model": cfg.default_codex_model,
                "default_effort": cfg.default_codex_effort,
                **codex_health,
            }

        async def _probe_claude() -> dict[str, Any]:
            try:
                claude_health = await job_manager.claude_runner.check_health()
            except Exception as exc:
                claude_health = _unhealthy(exc)
            return {
                "backend": "claude",
                "cli": "claude",
                "quota_pool": "claude",
                "default_model": cfg.default_claude_model,
                "default_effort": cfg.default_claude_effort,
                "model_names": ["sonnet", "opus", "haiku", "fable", "<full model id>"],
                **claude_health,
            }

        neon_info, codex_info, claude_info = await asyncio.gather(
            _probe_neon(), _probe_codex(), _probe_claude()
        )
        return {
            "default_cwd": str(cfg.default_cwd),
            "default_timeout_seconds": cfg.default_timeout_seconds,
            "max_concurrent_jobs_per_process": cfg.max_concurrent_jobs,
            "db_path": str(job_manager.store.db_path),
            "nesting": job_manager.nesting.to_dict(),
            "backends": {"neon": neon_info, "codex": codex_info, "claude": claude_info},
        }

    return mcp, job_manager


_OUTCOMES = ("completed", "failed", "timed_out", "cancelled", "in_flight")


def _compact_bucket(bucket: dict[str, Any], *, verbose: bool) -> dict[str, Any]:
    """One group's figures: runs, outcomes and in-flight jobs that are not zero, time and counted tokens."""
    tokens = bucket.get("tokens") or {}
    coverage = bucket.get("usage_coverage") or {}
    out: dict[str, Any] = {"runs": bucket.get("runs", 0)}
    out.update({k: n for k in _OUTCOMES if (n := bucket.get(k))})
    out["duration_seconds"] = bucket.get("duration_seconds", 0.0)
    out["total_tokens"] = tokens.get("total_tokens", 0)
    uncounted = int(coverage.get("partial") or 0) + int(coverage.get("missing") or 0)
    if uncounted:
        out["runs_without_full_count"] = uncounted
    if verbose:
        out["tokens"] = tokens
        out["usage_coverage"] = coverage
        if bucket.get("attempts", 0) != bucket.get("runs", 0):
            out["attempts"] = bucket.get("attempts", 0)
        if bucket.get("partial_total_tokens"):
            out["partial_total_tokens"] = bucket["partial_total_tokens"]
    return out


def _usage_summary_reply(summary: dict[str, Any], *, verbose: bool) -> dict[str, Any]:
    """The summary view: the period's totals, then each quota pool, each figure once."""
    pools = summary.get("by_quota_pool") or {}
    overview = summary.get("overview") or {}
    totals = {
        **overview,
        "tokens": {
            "total_tokens": sum(int((b.get("tokens") or {}).get("total_tokens") or 0) for b in pools.values()),
        },
    }
    out: dict[str, Any] = {"scope": summary.get("scope", {})}
    out["totals"] = _compact_bucket(totals, verbose=False)
    out["by_quota_pool"] = {name: _compact_bucket(b, verbose=verbose) for name, b in sorted(pools.items())}
    out["note"] = (
        "Tokens are counted for finished runs whose CLI reported an exact or per-turn count; "
        "runs_without_full_count says how many are left out."
    )
    return out


def _configure_stderr_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main() -> None:
    _configure_stderr_logging()
    server, _manager = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
