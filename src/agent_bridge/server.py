"""Fast, lightweight stdio MCP server exposing Neon (`agy`), Codex (`codex`) and Claude (`claude`) subagents.

Any MCP client can be the host: Claude Code, Codex, Antigravity or another. Nothing here assumes which.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from agent_bridge.config import EffortLevel, Settings, normalize_backend
from agent_bridge.jobs import JobManager
from agent_bridge.model_resolver import ModelPoolUnavailableError
from agent_bridge.nesting import UNKNOWN_NESTING, NestedBridgeError, Nesting, detect_nesting
from agent_bridge.storage import UsagePeriod

DashboardPeriod = Literal["today", "7d", "30d", "all", "session"]

SERVER_INSTRUCTIONS = """
`agent-bridge` lets you delegate tasks, reviews, and parallel work to other AI coding CLIs on this
machine, whichever CLI you are yourself. It covers FOUR independent subscription quota pools over
three local CLIs:

1. `codex` (OpenAI Codex CLI — `codex` pool) — defaults to `gpt-6.2-sol` at `high` reasoning
   effort (seamlessly rides newer versions / falls back if retired). Override per call with `effort`
   (`low` | `medium` | `high` | `xhigh`). Great for focused code reviews, targeted edits, test
   generation, and isolated pre-assigned Git worktree edits (`mode="edit"`, `worktree=True`).
2. `neon` / `agy` Gemini Models (`agy_gemini` pool) — versionless shortcuts auto-upgrade while
   preserving effort tier. Use **Hydrogen** (`hydrogen-medium` / `hydrogen-high`) for codebase
   exploration and research, and **Neon** (`neon-medium` / `neon-high`) for builds, architecture,
   and reviews based on task complexity (`flash` for quick lightweight checks).
3. `agy-claude` / `agy-opus` / `agy-gpt` (`agy_claude_gpt` pool inside `agy`) — SEPARATE 5h +
   weekly quota pool from both Gemini Models and the Claude plan below. Use
   `backend="agy-claude"` (Sonnet), `backend="agy-opus"` (Opus), or `backend="agy-gpt"` (GPT-OSS),
   or pass `model="claude-sonnet-high" | "claude-opus-high" | "gpt-oss"` on `backend="neon"`.
4. `claude` (Claude Code CLI — `claude` pool) — the user's own Claude plan, the same limits their
   Claude Code and claude.ai use draw on. Defaults to `sonnet` at `high`; pass `model="opus"`,
   `"haiku"`, `"fable"` or a full model id. `read_only` runs cannot write or run changing commands;
   `mode="edit"` accepts edits and supports `worktree=True`. Claude subagents get no MCP servers.

If you are one of these CLIs yourself, your own native subagents are usually the better way to reach
your own models; use the bridge for the others.

Smart Quota & Weekly-Reset Routing:
- Before substantial fan-out or heavy tasks, call `get_usage(compact=True, cwd=..., include_quota=True)`
  to check all pools (`by_quota_pool`), exact reset countdowns of all involved models
  (`quota_advisory.upcoming_resets`), and `quota_advisory.expiring_weekly_alerts` / `recommended_order`.
  The `claude` pool has no live read-out: its reading is what the last Claude job reported.
- If a pool has an `expiring_weekly_alerts` entry (`priority="use_before_reset"`), prioritize routing
  eligible work there so unused weekly quota is leveraged before it resets!
- Pass `cwd` on new jobs; omit `cwd` on `resume_from` so the bridge inherits the session's recorded
  working directory / worktree (pass `relocate_cwd=True` only to intentionally move a resumed session).
- Default `mode` is `"read_only"`. For multi-minute work prefer `start_agent` or
  `run_parallel(wait=False)` (or a bounded `wait_seconds`), then poll via `check_jobs`.
- Turns on the same `session_id` serialize atomically across windows via SQLite; pass `job_id` or
  `session_id` into `resume_from` to continue a conversation (model intent, effort, worktree, and
  `parent_run` are inherited automatically unless overridden).
- Treat `status="completed"` as a lifecycle status and still verify that `response` contains the
  requested deliverable.
- To show the user what is left of each subscription and what the bridge has run, call
  `usage_dashboard` and show its `text` unchanged.
- A subagent started by the bridge may not start bridge jobs itself: there the job-starting tools
  fail with `nested_bridge`.
""".strip()

NESTED_INSTRUCTIONS = """
`agent-bridge` is running INSIDE one of its own jobs: you are a subagent that another session started
through the bridge. `run_agent`, `start_agent`, `run_parallel` and `review_code` are refused here with
`nested_bridge`. Do the work yourself and put the answer in your reply. `check_jobs`, `get_usage`,
`usage_dashboard` and `list_backends` still work.
""".strip()


class ParallelTaskSpec(BaseModel):
    backend: str = Field(
        description="Subagent backend: 'neon' (or 'agy'/'gemini'), 'codex' (or 'gpt') or 'claude'."
    )
    prompt: str = Field(description="Instructions or question for the subagent.")
    cwd: str | None = Field(
        default=None,
        description="Optional per-task working directory override (falls back to top-level cwd).",
    )
    mode: Literal["read_only", "edit"] | None = Field(
        default=None,
        description="Optional per-task mode override ('read_only' or 'edit').",
    )
    model: str | None = Field(
        default=None,
        description=(
            "Optional model ID or versionless shortcut (Neon: 'neon-medium', 'neon-high', 'flash'; "
            "Claude: 'sonnet', 'opus', 'haiku', 'fable'). Omit for backend default."
        ),
    )
    effort: EffortLevel | None = Field(
        default=None,
        description="Optional reasoning effort override ('low', 'medium', 'high', 'xhigh').",
    )
    resume_from: str | None = Field(
        default=None,
        description="Optional prior job_id or session_id to continue.",
    )
    relocate_cwd: bool = Field(
        default=False,
        description="Set True to intentionally move a resumed session out of its recorded worktree/cwd.",
    )
    worktree: bool = Field(
        default=False,
        description="Codex or Claude edit mode only: run in an isolated managed Git worktree.",
    )
    extra_dirs: list[str] | None = Field(
        default=None,
        description="Optional extra directories to grant read/write access to (new sessions only on Codex).",
    )
    label: str | None = Field(
        default=None,
        description="Short human-readable label for tracking this task.",
    )
    confirm_prior_stopped: bool = Field(
        default=False,
        description=(
            "Set True only after confirming the current blocking prior turn on the same session "
            "has finished before resuming (bound to the current blocking job_id at request time)."
        ),
    )
    confirm_prior_job_id: str | None = Field(
        default=None,
        description="Explicit prior blocking job_id confirmed as stopped before resuming.",
    )
    allow_cross_pool_fallback: bool = Field(
        default=False,
        description="Opt in to falling back across Antigravity quota pools (e.g. Claude/GPT -> Gemini).",
    )


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
            raise ValueError("commit SHA is required when scope='commit'.")
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

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await job_manager.shutdown()

    mcp = MCPServer(
        name="agent-bridge",
        title="Agent Bridge (Neon, Codex & Claude Subagents)",
        description=(
            "Delegate tasks, parallel work, and code reviews to Antigravity (Neon), Codex CLI "
            "and Claude Code subagents from any MCP host, with shared SQLite state, dynamic models, "
            "and usage/quota visibility."
        ),
        instructions=NESTED_INSTRUCTIONS if nest.nested else SERVER_INSTRUCTIONS,
        lifespan=lifespan,
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    @_reported
    async def run_agent(
        backend: str,
        prompt: str,
        cwd: str | None = None,
        mode: Literal["read_only", "edit"] = "read_only",
        model: str | None = None,
        effort: EffortLevel | None = None,
        resume_from: str | None = None,
        relocate_cwd: bool = False,
        worktree: bool = False,
        extra_dirs: list[str] | None = None,
        timeout_seconds: int | None = None,
        wait_seconds: int | None = None,
        label: str | None = None,
        parent_run: str | None = None,
        confirm_prior_stopped: bool = False,
        confirm_prior_job_id: str | None = None,
        allow_cross_pool_fallback: bool = False,
    ) -> dict[str, Any]:
        """Run a Neon (`agy`), Codex (`codex`) or Claude (`claude`) subagent synchronously and return its response.

        - `timeout_seconds`: overall job deadline (defaults to `AGENT_BRIDGE_DEFAULT_TIMEOUT`).
        - `wait_seconds`: optional shorter MCP wait window; if it expires while the job is still
          running, returns the live `job_id` so you can poll via `check_jobs` without stopping the job.
        - `effort`: optional reasoning effort override (`low`, `medium`, `high`, `xhigh`).
        - `resume_from`: prior `job_id` or `session_id` to continue (inherits worktree/cwd, model intent,
          effort, and `parent_run` unless overridden; turns on the same session serialize).
        - `relocate_cwd`: set `True` only to intentionally move a resumed session out of its recorded worktree.
        - `confirm_prior_job_id` / `confirm_prior_stopped`: confirm the specific prior blocking turn
          has stopped before resuming a session whose prior turn ended `possibly_running` / `unknown`.
        - `allow_cross_pool_fallback`: opt in (`True`) to allow falling back across Antigravity quota pools.
        """
        _refuse_if_nested("run_agent")
        req = await job_manager.prepare_request_async(
            backend=backend,
            prompt=prompt,
            cwd=cwd,
            mode=mode,
            model=model,
            effort=effort,
            resume_from=resume_from,
            relocate_cwd=relocate_cwd,
            worktree=worktree,
            extra_dirs=extra_dirs,
            timeout_seconds=timeout_seconds,
            label=label,
            parent_run=parent_run,
            confirm_prior_stopped=confirm_prior_stopped,
            confirm_prior_job_id=confirm_prior_job_id,
            allow_cross_pool_fallback=allow_cross_pool_fallback,
        )
        record = await job_manager.run(req, wait_seconds=wait_seconds)
        detail = record.to_detail()
        detail["session_usage"] = await job_manager.session_compact_summary_async()
        return detail

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    @_reported
    async def start_agent(
        backend: str,
        prompt: str,
        cwd: str | None = None,
        mode: Literal["read_only", "edit"] = "read_only",
        model: str | None = None,
        effort: EffortLevel | None = None,
        resume_from: str | None = None,
        relocate_cwd: bool = False,
        worktree: bool = False,
        extra_dirs: list[str] | None = None,
        timeout_seconds: int | None = None,
        label: str | None = None,
        parent_run: str | None = None,
        confirm_prior_stopped: bool = False,
        confirm_prior_job_id: str | None = None,
        allow_cross_pool_fallback: bool = False,
    ) -> dict[str, Any]:
        """Launch a Neon, Codex or Claude subagent in the background and immediately return its `job_id`.

        Use `check_jobs(job_ids=[job_id], wait_seconds=60)` to collect results or monitor progress.
        """
        _refuse_if_nested("start_agent")
        req = await job_manager.prepare_request_async(
            backend=backend,
            prompt=prompt,
            cwd=cwd,
            mode=mode,
            model=model,
            effort=effort,
            resume_from=resume_from,
            relocate_cwd=relocate_cwd,
            worktree=worktree,
            extra_dirs=extra_dirs,
            timeout_seconds=timeout_seconds,
            label=label,
            parent_run=parent_run,
            confirm_prior_stopped=confirm_prior_stopped,
            confirm_prior_job_id=confirm_prior_job_id,
            allow_cross_pool_fallback=allow_cross_pool_fallback,
        )
        record = await job_manager.start_async(req)
        return {
            "job_id": record.job_id,
            "owner_id": record.owner_id,
            "backend": record.backend,
            "requested_model": record.requested_model,
            "effort": record.effort,
            "mode": record.mode,
            "cwd": record.cwd,
            "status": record.status,
            "label": record.label,
            "parent_run": record.parent_run,
            "session_id": record.session_id,
            "timeout_seconds": req.timeout_seconds,
            "hint": f"Call check_jobs(job_ids=['{record.job_id}'], wait_seconds=60) to wait for results.",
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    @_reported
    async def run_parallel(
        tasks: list[ParallelTaskSpec],
        cwd: str | None = None,
        mode: Literal["read_only", "edit"] = "read_only",
        wait: bool = True,
        timeout_seconds: int | None = None,
        wait_seconds: int | None = None,
        parent_run: str | None = None,
    ) -> dict[str, Any]:
        """Run multiple Neon, Codex and/or Claude subagent tasks concurrently in one call.

        Set `wait=True` (default) to wait for all tasks (up to `wait_seconds` or `timeout_seconds`),
        or `wait=False` to start them in the background and return their `job_ids` immediately.
        """
        _refuse_if_nested("run_parallel")
        if not tasks:
            raise ValueError("tasks must contain at least one task.")
        if len(tasks) > 10:
            raise ValueError("At most 10 parallel tasks may be launched in a single call.")

        requests = [
            await job_manager.prepare_request_async(
                backend=spec.backend,
                prompt=spec.prompt,
                cwd=spec.cwd or cwd,
                mode=spec.mode or mode,
                model=spec.model,
                effort=spec.effort,
                resume_from=spec.resume_from,
                relocate_cwd=spec.relocate_cwd,
                worktree=spec.worktree,
                extra_dirs=spec.extra_dirs,
                timeout_seconds=timeout_seconds,
                label=spec.label,
                parent_run=parent_run,
                confirm_prior_stopped=spec.confirm_prior_stopped,
                confirm_prior_job_id=spec.confirm_prior_job_id,
                allow_cross_pool_fallback=spec.allow_cross_pool_fallback,
            )
            for spec in tasks
        ]
        effective_wait = wait_seconds if wait_seconds is not None else (
            requests[0].timeout_seconds if wait else 0
        )
        records = await job_manager.run_parallel(
            requests,
            wait=wait,
            wait_seconds=effective_wait,
        )
        return {
            "count": len(records),
            "all_done": all(r.is_terminal for r in records),
            "results": [r.to_detail() if wait else r.to_summary() for r in records],
            "session_usage": await job_manager.session_compact_summary_async(),
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    @_reported
    async def check_jobs(
        job_ids: list[str] | None = None,
        wait_seconds: int = 0,
        wait_mode: Literal["all", "any"] = "all",
        status: str | None = None,
        backend: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Inspect background or recent subagent jobs across windows, or wait for specific jobs.

        - If `job_ids` is provided: waits up to `wait_seconds` (capped at 300s) for `'all'` or
          `'any'` of those jobs to reach a terminal state, then returns full details. A wait ending
          with `'queued'` or `'running'` leaves the job active.
        - If `job_ids` is omitted: returns a compact summary list of recent jobs.
        """
        if job_ids:
            bounded_wait = max(0, min(int(wait_seconds), 300))
            records = await job_manager.wait_for(
                job_ids,
                wait_seconds=bounded_wait,
                wait_mode=wait_mode,
            )
            return {
                "all_done": all(r.is_terminal for r in records),
                "any_done": any(r.is_terminal for r in records),
                "jobs": [r.to_detail() for r in records],
                "session_usage": await job_manager.session_compact_summary_async(),
            }

        summaries = await job_manager.list_jobs_async(
            limit=limit,
            status=status,
            backend=backend,
            cwd=cwd,
            parent_run=parent_run,
        )
        return {
            "count": len(summaries),
            "jobs": summaries,
            "session_usage": await job_manager.session_compact_summary_async(),
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    @_reported
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel a running or queued background subagent job owned by this bridge process."""
        record = await job_manager.cancel(job_id)
        return record.to_detail()

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    @_reported
    async def review_code(
        cwd: str | None = None,
        backends: list[str] | None = None,
        scope: Literal["uncommitted", "base", "commit"] = "uncommitted",
        base: str | None = None,
        commit: str | None = None,
        focus: str | None = None,
        neon_model: str | None = None,
        codex_model: str | None = None,
        effort: EffortLevel | None = None,
        wait: bool = True,
        timeout_seconds: int | None = None,
        wait_seconds: int | None = None,
        parent_run: str | None = None,
        claude_model: str | None = None,
    ) -> dict[str, Any]:
        """Run a read-only code review using Neon, Codex, Claude, or several in parallel.

        - `backends`: defaults to `["neon", "codex"]` for dual-model review; add `"claude"` to include it.
        - `scope`: `'uncommitted'` (default), `'base'` (with `base="main"`), or `'commit'` (with `commit="<sha>"`).
        - `effort`: optional reasoning effort override applied to selected backends.
        """
        _refuse_if_nested("review_code")
        models_by_backend = {"neon": neon_model, "codex": codex_model, "claude": claude_model}
        selected = backends if backends else ["neon", "codex"]
        normalized_backends = []
        for b in selected:
            nb = normalize_backend(b)
            if nb not in normalized_backends:
                normalized_backends.append(nb)

        if scope == "base" and not base:
            base = "main"
        if scope == "commit" and not commit:
            raise ValueError("commit SHA is required when scope='commit'.")

        prompt_for_custom = _build_review_prompt(scope, base=base, commit=commit, focus=focus)
        requests = []
        for backend in normalized_backends:
            if backend == "codex" and not (focus and focus.strip()):
                req = await job_manager.prepare_request_async(
                    backend="codex",
                    prompt="",
                    cwd=cwd,
                    mode="read_only",
                    model=codex_model,
                    effort=effort,
                    timeout_seconds=timeout_seconds,
                    label=f"codex-review-{scope}",
                    parent_run=parent_run,
                    review_scope=scope,
                    review_base=base,
                    review_commit=commit,
                )
            else:
                req = await job_manager.prepare_request_async(
                    backend=backend,
                    prompt=prompt_for_custom,
                    cwd=cwd,
                    mode="read_only",
                    model=models_by_backend.get(backend),
                    effort=effort,
                    timeout_seconds=timeout_seconds,
                    label=f"{backend}-review-{scope}",
                    parent_run=parent_run,
                )
            requests.append(req)

        effective_wait = wait_seconds if wait_seconds is not None else (
            requests[0].timeout_seconds if wait else 0
        )
        records = await job_manager.run_parallel(
            requests,
            wait=wait,
            wait_seconds=effective_wait,
        )
        return {
            "scope": scope,
            "base": base,
            "commit": commit,
            "all_done": all(r.is_terminal for r in records),
            "reviews": [r.to_detail() if wait else r.to_summary() for r in records],
            "session_usage": await job_manager.session_compact_summary_async(),
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    @_reported
    async def get_usage(
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
        """Inspect usage across all quota pools, in-flight load, coverage, and weekly-reset-aware quota.

        - `period`: `'session'`, `'today'`, `'7d'`, `'30d'`, or `'all'`.
        - `quota_pool`: optional pool filter (`'agy_gemini'`, `'agy_claude_gpt'`, `'codex'`, or `'claude'`).
        - `cwd` / `parent_run`: optional filters for project-scoped or workflow-scoped planning.
        - `compact=True`: concise planning view (per-backend & per-quota-pool runs, in-flight, duration, coverage).
        - `include_quota=True`: opt-in cached live quota lookup + `quota_advisory` with weekly-reset alerts,
          upcoming reset times across all involved models, and smart pool `recommended_order`.
        """
        return await job_manager.get_usage_summary(
            period=period,
            backend=backend,
            quota_pool=quota_pool,
            cwd=cwd,
            parent_run=parent_run,
            include_quota=include_quota,
            refresh_quota=refresh_quota,
            compact=compact,
            recent_limit=recent_limit,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    @_reported
    async def usage_dashboard(
        period: DashboardPeriod | None = None,
        since: str | None = None,
        until: str | None = None,
        quota: bool = True,
    ) -> dict[str, Any]:
        """What is left of each subscription, then what the bridge has run in a period.

        - `period`: `'today'` (the default), `'7d'`, `'30d'`, `'all'`, or `'session'` (this window only).
        - `since` / `until`: instead of `period`, a local date (`2026-11-25`) or an ISO time. A date alone as
          `until` takes in that whole day.
        - `quota`: `False` leaves the quota section out and starts no CLI.

        `quota` lists every pool with the 5h and weekly windows its reading holds: percent left, the
        reset, and when it was read.
        The readings come from each CLI, as `get_usage(include_quota=True)` reads them, and take about
        10 seconds; a pool that could not be read says why. The period does not change them.

        The rest is from the bridge's own records. Totals hold only finished runs with exact or delta
        counts. Everything the records cannot vouch for (running jobs, partial counts, runs that left no
        count, Claude subagents, time before the first record) is listed apart under `not_in_totals` and
        never added in. The tool writes nothing. Show `text` to the user as it is: every figure in it is
        already worked out.
        """
        return await job_manager.usage_dashboard(period=period, since=since, until=until, quota=quota)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    @_reported
    async def list_backends(
        refresh_models: bool = False,
        include_quota: bool = False,
    ) -> dict[str, Any]:
        """Check CLI discovery, health, live/cached agy pools (Gemini + Claude/GPT), Codex and Claude defaults, and optional quota."""
        async def _probe_neon() -> dict[str, Any]:
            try:
                neon_health = await job_manager.neon_runner.check_health()
            except Exception as exc:
                neon_health = {
                    "command_found": False,
                    "healthy": False,
                    "available": False,
                    "command": None,
                    "version": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            info: dict[str, Any] = {
                "backend": "neon",
                "cli": "agy",
                "default_intent": cfg.default_neon_model,
                **neon_health,
            }
            if neon_health.get("healthy"):
                try:
                    models = await job_manager.neon_runner.list_models(refresh=refresh_models)
                except Exception as exc:
                    models = []
                    info["catalog_error"] = f"{type(exc).__name__}: {exc}"
                info.update(job_manager.neon_runner.catalog.status_info())
                info["models"] = models
                info["quota_pools"] = job_manager.neon_runner.catalog.group_models_by_quota_pool()
                info["resolved_shortcuts"] = job_manager.neon_runner.catalog.summarize_latest_aliases()
                try:
                    default_res = await job_manager.neon_runner.catalog.resolve(
                        None,
                        default_intent=cfg.default_neon_model,
                    )
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
                info.update(job_manager.neon_runner.catalog.status_info())
            return info

        async def _probe_codex() -> dict[str, Any]:
            try:
                codex_health = await job_manager.codex_runner.check_health()
            except Exception as exc:
                codex_health = {
                    "command_found": False,
                    "healthy": False,
                    "available": False,
                    "command": None,
                    "version": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            return {
                "backend": "codex",
                "cli": "codex",
                "default_model": cfg.default_codex_model,
                "default_effort": cfg.default_codex_effort,
                "configured_model_override": cfg.default_codex_model,
                "configured_effort_override": cfg.default_codex_effort,
                **codex_health,
            }

        async def _probe_claude() -> dict[str, Any]:
            try:
                claude_health = await job_manager.claude_runner.check_health()
            except Exception as exc:
                claude_health = {
                    "command_found": False,
                    "healthy": False,
                    "available": False,
                    "command": None,
                    "version": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
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

        out: dict[str, Any] = {
            "owner_id": job_manager.owner_id,
            "default_cwd": str(cfg.default_cwd),
            "default_timeout_seconds": cfg.default_timeout_seconds,
            "max_concurrent_jobs_per_process": cfg.max_concurrent_jobs,
            "db_path": str(job_manager.store.db_path),
            "session_usage": await job_manager.session_compact_summary_async(),
            "nesting": job_manager.nesting.to_dict(),
            "backends": {
                "neon": neon_info,
                "codex": codex_info,
                "claude": claude_info,
            },
        }
        if include_quota:
            quota = await job_manager.get_quota(["neon", "codex", "claude"], refresh=refresh_models)
            out["quota"] = quota
            out["quota_advisory"] = job_manager._build_quota_advisory(quota)
        return out

    return mcp, job_manager


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
