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

from agent_bridge.config import Settings, normalize_backend
from agent_bridge.jobs import JobManager
from agent_bridge.usage import UsagePeriod

SERVER_INSTRUCTIONS = """
`agent-bridge` lets you delegate tasks, reviews, and parallel work to two external CLI subagents
using the user's existing subscriptions:

- `neon` (Antigravity CLI / Gemini Neon, Hydrogen, Flash, etc.) — great for architecture reviews,
  second opinions, large-context codebase analysis, debugging, and independent edits.
  Prefer versionless shortcuts (`neon`, `neon-xhigh`, `neon-medium`, `flash`, `hydrogen-xhigh`)
  so newer model versions are picked up automatically from live `agy models`.
- `codex` (OpenAI Codex CLI / GPT) — great for focused code reviews, targeted implementations,
  test generation, bug fixes, and isolated worktree edits. Omit `model` to always use the user's
  latest Codex subscription default.

Guidelines:
- Always pass `cwd` pointing to the repository or workspace you want the subagent to inspect.
- Default `mode` is `"read_only"`. Only pass `mode="edit"` when the user wants the subagent to modify files.
  For parallel edits with Codex, prefer `worktree=True` to avoid conflicting edits.
- Use `run_agent` for quick blocking tasks; use `start_agent` or `run_parallel` + `check_jobs` for
  long-running work or running Neon and Codex simultaneously.
- Every completed run returns `session_id`, `job_id`, normalized token `usage`, and `session_usage`.
  Pass `job_id` or `session_id` into `resume_from` on a follow-up call to continue that conversation.
- Call `get_usage` anytime to inspect cumulative token and run usage across `session`, `today`, `7d`, `30d`, or `all`.
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
            "Optional model ID or versionless Neon shortcut (e.g. 'neon-xhigh', 'neon-medium', "
            "'flash', 'hydrogen'). Omit for backend default."
        ),
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
        description="Optional extra directories to grant read/write access to.",
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
            "and Codex CLI subagents with dynamic model resolution and usage tracking."
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
        resume_from: str | None = None,
        worktree: bool = False,
        extra_dirs: list[str] | None = None,
        timeout_seconds: int = 300,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Run a Neon (`agy`) or Codex (`codex`) subagent synchronously and return its response.

        Pass `resume_from` with a prior `job_id` or `session_id` to continue an existing conversation.
        Defaults to `mode="read_only"`. Use versionless Neon intents like `'neon-high'`, `'neon-xhigh'`,
        `'neon-medium'`, `'flash'` (or omit `model`) so newer model versions update seamlessly.
        """
        req = job_manager.prepare_request(
            backend=backend,
            prompt=prompt,
            cwd=cwd,
            mode=mode,
            model=model,
            resume_from=resume_from,
            worktree=worktree,
            extra_dirs=extra_dirs,
            timeout_seconds=timeout_seconds,
            label=label,
        )
        record = await job_manager.run(req)
        detail = record.to_detail()
        detail["session_usage"] = job_manager.usage_tracker.session_compact_summary()
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
        resume_from: str | None = None,
        worktree: bool = False,
        extra_dirs: list[str] | None = None,
        timeout_seconds: int = 600,
        label: str | None = None,
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
            resume_from=resume_from,
            worktree=worktree,
            extra_dirs=extra_dirs,
            timeout_seconds=timeout_seconds,
            label=label,
        )
        record = job_manager.start(req)
        return {
            "job_id": record.job_id,
            "backend": record.backend,
            "requested_model": record.requested_model,
            "mode": record.mode,
            "cwd": record.cwd,
            "status": record.status,
            "label": record.label,
            "session_id": record.session_id,
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
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        """Run multiple Neon and/or Codex subagent tasks concurrently in one call.

        Set `wait=True` (default) to wait for all tasks and return all responses together, or
        `wait=False` to start them in the background and return their `job_ids` immediately.
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
                resume_from=spec.resume_from,
                worktree=spec.worktree,
                extra_dirs=spec.extra_dirs,
                timeout_seconds=timeout_seconds,
                label=spec.label,
            )
            for spec in tasks
        ]
        records = await job_manager.run_parallel(
            requests,
            wait=wait,
            wait_seconds=timeout_seconds if wait else 0,
        )
        return {
            "count": len(records),
            "all_done": all(r.is_terminal for r in records),
            "results": [r.to_detail() if wait else r.to_summary() for r in records],
            "session_usage": job_manager.usage_tracker.session_compact_summary(),
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
        limit: int = 20,
    ) -> dict[str, Any]:
        """Inspect background or recent subagent jobs, or wait for specific jobs to complete.

        - If `job_ids` is provided: waits up to `wait_seconds` (capped at 300s) for `'all'` or
          `'any'` of those jobs to complete, then returns full details (`response`, `session_id`, `usage`).
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
                "session_usage": job_manager.usage_tracker.session_compact_summary(),
            }

        summaries = job_manager.list_jobs(limit=limit, status=status, backend=backend)
        return {
            "count": len(summaries),
            "jobs": summaries,
            "session_usage": job_manager.usage_tracker.session_compact_summary(),
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel a running or queued background subagent job and terminate its subprocess tree."""
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
        wait: bool = True,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        """Run a read-only code review using Neon, Codex, or both in parallel.

        - `backends`: defaults to `["neon", "codex"]` for dual-model review.
        - `scope`: `'uncommitted'` (default), `'base'` (with `base="main"`), or `'commit'` (with `commit="<sha>"`).
        - `focus`: optional custom review criteria (e.g. security, concurrency, API compatibility).
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
                    timeout_seconds=timeout_seconds,
                    label=f"codex-review-{scope}",
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
                    timeout_seconds=timeout_seconds,
                    label=f"{backend}-review-{scope}",
                )
            requests.append(req)

        records = await job_manager.run_parallel(
            requests,
            wait=wait,
            wait_seconds=timeout_seconds if wait else 0,
        )
        return {
            "scope": scope,
            "base": base,
            "commit": commit,
            "all_done": all(r.is_terminal for r in records),
            "reviews": [r.to_detail() if wait else r.to_summary() for r in records],
            "session_usage": job_manager.usage_tracker.session_compact_summary(),
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    async def get_usage(
        period: UsagePeriod = "today",
        backend: str | None = None,
        recent_limit: int = 10,
    ) -> dict[str, Any]:
        """Inspect token, run-count, and duration usage across external Neon and Codex subagents.

        - `period`: `'session'` (this MCP run), `'today'` (local calendar day), `'7d'`, `'30d'`, or `'all'`.
        - `backend`: optional filter (`'neon'` or `'codex'`).
        - Returns totals, breakdown `by_backend` and `by_model`, plus the most recent runs.
        """
        norm_backend = normalize_backend(backend) if backend and backend.strip() else None
        return job_manager.usage_tracker.summarize(
            period=period,
            backend=norm_backend,
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
        """Check installed `agy` (Neon) and `codex` CLIs, live models, dynamic shortcuts, and session usage."""
        neon_info: dict[str, Any] = {
            "backend": "neon",
            "cli": "agy",
            "default_intent": cfg.default_neon_model,
        }
        try:
            neon_info["command"] = job_manager.neon_runner.resolve_command()
            neon_info["available"] = True
            neon_info["version"] = await job_manager.neon_runner.get_version()
            models = await job_manager.neon_runner.list_models(refresh=refresh_models)
            neon_info["models"] = models
            neon_info["resolved_shortcuts"] = job_manager.neon_runner.catalog.summarize_latest_aliases()
            default_res = await job_manager.neon_runner.catalog.resolve(
                None, default_intent=cfg.default_neon_model
            )
            neon_info["resolved_default_model"] = default_res.resolved
        except Exception as exc:
            neon_info["available"] = False
            neon_info["error"] = str(exc)

        codex_info: dict[str, Any] = {
            "backend": "codex",
            "cli": "codex",
            "default_model": cfg.default_codex_model or "(Codex CLI subscription default)",
        }
        try:
            codex_info["command"] = job_manager.codex_runner.resolve_command()
            codex_info["available"] = True
            codex_info["version"] = await job_manager.codex_runner.get_version()
        except Exception as exc:
            codex_info["available"] = False
            codex_info["error"] = str(exc)

        return {
            "default_cwd": str(cfg.default_cwd),
            "default_timeout_seconds": cfg.default_timeout_seconds,
            "max_concurrent_jobs": cfg.max_concurrent_jobs,
            "session_usage": job_manager.usage_tracker.session_compact_summary(),
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
