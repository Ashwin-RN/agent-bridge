"""Fast, lightweight stdio MCP server exposing Neon (`agy`) and Codex (`codex`) subagents."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from agent_bridge.config import EffortLevel, Settings, normalize_backend
from agent_bridge.jobs import JobManager
from agent_bridge.storage import UsagePeriod

SERVER_INSTRUCTIONS = """
`agent-bridge` lets you delegate tasks, reviews, and parallel work to two external CLI subagents
using the user's existing subscriptions:

- `neon` (Antigravity CLI / Gemini Neon, Hydrogen, Flash) — great for architecture reviews,
  second opinions, large-context codebase analysis, and debugging.
  Prefer versionless shortcuts (`neon-medium`, `neon-high`, `neon-xhigh`, `flash`) so newer
  model versions are picked up automatically while preserving your requested effort tier.
- `codex` (OpenAI Codex CLI / GPT) — great for focused code reviews, targeted implementations,
  test generation, bug fixes, and isolated worktree edits. Omit `model` for the subscription default,
  and pass `effort` (`low` | `medium` | `high` | `xhigh`) when you want to override local Codex effort.

Guidelines:
- Always pass `cwd` pointing to the repository or workspace you want the subagent to inspect.
- Default `mode` is `"read_only"`. Only pass `mode="edit"` when the user wants file edits;
  for Codex edits prefer `worktree=True` and inspect the returned `effective_cwd` / `worktree_path`.
- For multi-minute work, prefer `start_agent` or `run_parallel(wait=False)` (or pass a bounded
  `wait_seconds` separate from the overall `timeout_seconds` deadline), then follow up with `check_jobs`.
- Turns on the same `session_id` serialize automatically; pass `job_id` or `session_id` into
  `resume_from` to continue a conversation.
- Call `get_usage(compact=True, cwd=..., include_quota=True)` before heavy fan-out to check
  in-flight load, recent usage, and live subscription quota.
""".strip()


class ParallelTaskSpec(BaseModel):
    backend: str = Field(
        description="Subagent backend: 'neon' (or 'agy'/'gemini') or 'codex' (or 'gpt')."
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
            "Optional model ID or versionless Neon shortcut (e.g. 'neon-medium', 'neon-high', "
            "'neon-xhigh', 'flash'). Omit for backend default."
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
    worktree: bool = Field(
        default=False,
        description="Codex edit mode only: run in an isolated managed Git worktree.",
    )
    extra_dirs: list[str] | None = Field(
        default=None,
        description="Optional extra directories to grant read/write access to (new sessions only on Codex).",
    )
    label: str | None = Field(
        default=None,
        description="Short human-readable label for tracking this task.",
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


def create_server(
    settings: Settings | None = None,
    manager: JobManager | None = None,
) -> tuple[MCPServer, JobManager]:
    """Create and configure the MCPServer instance and its backing JobManager."""
    cfg = settings or Settings.from_env()
    job_manager = manager or JobManager(cfg)

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await job_manager.shutdown()

    mcp = MCPServer(
        name="agent-bridge",
        title="Agent Bridge (Neon & Codex Subagents)",
        description=(
            "Delegate tasks, parallel work, and code reviews to Antigravity (Neon) "
            "and Codex CLI subagents with shared SQLite state, dynamic models, and usage/quota visibility."
        ),
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def run_agent(
        backend: str,
        prompt: str,
        cwd: str | None = None,
        mode: Literal["read_only", "edit"] = "read_only",
        model: str | None = None,
        effort: EffortLevel | None = None,
        resume_from: str | None = None,
        worktree: bool = False,
        extra_dirs: list[str] | None = None,
        timeout_seconds: int | None = None,
        wait_seconds: int | None = None,
        label: str | None = None,
        parent_run: str | None = None,
    ) -> dict[str, Any]:
        """Run a Neon (`agy`) or Codex (`codex`) subagent synchronously and return its response.

        - `timeout_seconds`: overall job deadline (defaults to `AGENT_BRIDGE_DEFAULT_TIMEOUT`).
        - `wait_seconds`: optional shorter MCP wait window; if it expires while the job is still
          running, returns the live `job_id` so you can poll via `check_jobs` without stopping the job.
        - `effort`: optional reasoning effort override (`low`, `medium`, `high`, `xhigh`).
        - `resume_from`: prior `job_id` or `session_id` to continue (turns on the same session serialize).
        """
        req = job_manager.prepare_request(
            backend=backend,
            prompt=prompt,
            cwd=cwd,
            mode=mode,
            model=model,
            effort=effort,
            resume_from=resume_from,
            worktree=worktree,
            extra_dirs=extra_dirs,
            timeout_seconds=timeout_seconds,
            label=label,
            parent_run=parent_run,
        )
        record = await job_manager.run(req, wait_seconds=wait_seconds)
        detail = record.to_detail()
        detail["session_usage"] = job_manager.store.session_compact_summary(job_manager.owner_id)
        return detail

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def start_agent(
        backend: str,
        prompt: str,
        cwd: str | None = None,
        mode: Literal["read_only", "edit"] = "read_only",
        model: str | None = None,
        effort: EffortLevel | None = None,
        resume_from: str | None = None,
        worktree: bool = False,
        extra_dirs: list[str] | None = None,
        timeout_seconds: int | None = None,
        label: str | None = None,
        parent_run: str | None = None,
    ) -> dict[str, Any]:
        """Launch a Neon or Codex subagent in the background and immediately return its `job_id`.

        Use `check_jobs(job_ids=[job_id], wait_seconds=60)` to collect results or monitor progress.
        """
        req = job_manager.prepare_request(
            backend=backend,
            prompt=prompt,
            cwd=cwd,
            mode=mode,
            model=model,
            effort=effort,
            resume_from=resume_from,
            worktree=worktree,
            extra_dirs=extra_dirs,
            timeout_seconds=timeout_seconds,
            label=label,
            parent_run=parent_run,
        )
        record = job_manager.start(req)
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
    async def run_parallel(
        tasks: list[ParallelTaskSpec],
        cwd: str | None = None,
        mode: Literal["read_only", "edit"] = "read_only",
        wait: bool = True,
        timeout_seconds: int | None = None,
        wait_seconds: int | None = None,
        parent_run: str | None = None,
    ) -> dict[str, Any]:
        """Run multiple Neon and/or Codex subagent tasks concurrently in one call.

        Set `wait=True` (default) to wait for all tasks (up to `wait_seconds` or `timeout_seconds`),
        or `wait=False` to start them in the background and return their `job_ids` immediately.
        """
        if not tasks:
            raise ValueError("tasks must contain at least one task.")
        if len(tasks) > 10:
            raise ValueError("At most 10 parallel tasks may be launched in a single call.")

        requests = [
            job_manager.prepare_request(
                backend=spec.backend,
                prompt=spec.prompt,
                cwd=spec.cwd or cwd,
                mode=spec.mode or mode,
                model=spec.model,
                effort=spec.effort,
                resume_from=spec.resume_from,
                worktree=spec.worktree,
                extra_dirs=spec.extra_dirs,
                timeout_seconds=timeout_seconds,
                label=spec.label,
                parent_run=parent_run,
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
            "session_usage": job_manager.store.session_compact_summary(job_manager.owner_id),
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
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
                "session_usage": job_manager.store.session_compact_summary(job_manager.owner_id),
            }

        summaries = job_manager.list_jobs(
            limit=limit,
            status=status,
            backend=backend,
            cwd=cwd,
            parent_run=parent_run,
        )
        return {
            "count": len(summaries),
            "jobs": summaries,
            "session_usage": job_manager.store.session_compact_summary(job_manager.owner_id),
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
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
    ) -> dict[str, Any]:
        """Run a read-only code review using Neon, Codex, or both in parallel.

        - `backends`: defaults to `["neon", "codex"]` for dual-model review.
        - `scope`: `'uncommitted'` (default), `'base'` (with `base="main"`), or `'commit'` (with `commit="<sha>"`).
        - `effort`: optional reasoning effort override applied to selected backends.
        """
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
                req = job_manager.prepare_request(
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
                req = job_manager.prepare_request(
                    backend=backend,
                    prompt=prompt_for_custom,
                    cwd=cwd,
                    mode="read_only",
                    model=neon_model if backend == "neon" else codex_model,
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
            "session_usage": job_manager.store.session_compact_summary(job_manager.owner_id),
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_usage(
        period: UsagePeriod = "today",
        backend: str | None = None,
        cwd: str | None = None,
        parent_run: str | None = None,
        include_quota: bool = False,
        refresh_quota: bool = False,
        compact: bool = False,
        recent_limit: int = 10,
    ) -> dict[str, Any]:
        """Inspect usage, in-flight load, coverage, and optional live subscription quota.

        - `period`: `'session'`, `'today'`, `'7d'`, `'30d'`, or `'all'`.
        - `cwd` / `parent_run`: optional filters for project-scoped or workflow-scoped planning.
        - `compact=True`: concise planning view (per-backend runs, in-flight jobs, duration, coverage).
        - `include_quota=True`: opt-in cached live quota lookup (Codex rate limits + Antigravity `/usage`).
        """
        return await job_manager.get_usage_summary(
            period=period,
            backend=backend,
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
            openWorldHint=False,
        )
    )
    async def list_backends(refresh_models: bool = False) -> dict[str, Any]:
        """Check CLI discovery, executable health, live/cached Neon models, Codex defaults, and usage."""
        neon_health = await job_manager.neon_runner.check_health()
        neon_info: dict[str, Any] = {
            "backend": "neon",
            "cli": "agy",
            "default_intent": cfg.default_neon_model,
            **neon_health,
        }
        if neon_health["healthy"]:
            models = await job_manager.neon_runner.list_models(refresh=refresh_models)
            neon_info.update(job_manager.neon_runner.catalog.status_info())
            neon_info["models"] = models
            neon_info["resolved_shortcuts"] = (
                job_manager.neon_runner.catalog.summarize_latest_aliases()
            )
            default_res = await job_manager.neon_runner.catalog.resolve(
                None, default_intent=cfg.default_neon_model
            )
            neon_info["resolved_default_model"] = default_res.resolved
        else:
            neon_info.update(job_manager.neon_runner.catalog.status_info())

        codex_health = await job_manager.codex_runner.check_health()
        codex_info: dict[str, Any] = {
            "backend": "codex",
            "cli": "codex",
            "configured_model_override": cfg.default_codex_model,
            "configured_effort_override": cfg.default_codex_effort,
            **codex_health,
        }

        return {
            "owner_id": job_manager.owner_id,
            "default_cwd": str(cfg.default_cwd),
            "default_timeout_seconds": cfg.default_timeout_seconds,
            "max_concurrent_jobs_per_process": cfg.max_concurrent_jobs,
            "db_path": str(job_manager.store.db_path),
            "session_usage": job_manager.store.session_compact_summary(job_manager.owner_id),
            "backends": {
                "neon": neon_info,
                "codex": codex_info,
            },
        }

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
