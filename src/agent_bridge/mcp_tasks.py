"""Expose bridge jobs through the modern MCP tasks extension."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import Context, require_client_extension
from mcp.shared.exceptions import MCPError
from mcp_types import CLIENT_CAPABILITIES_META_KEY, INVALID_PARAMS, CallToolRequestParams, ErrorData, RequestParams
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

__all__ = ["JobTasks", "JobView", "TASK_CALL", "pump_progress"]

logger = logging.getLogger(__name__)
TASK_CALL: ContextVar[bool] = ContextVar("TASK_CALL", default=False)
_ACTIVE = ("queued", "running")


@dataclass(frozen=True)
class JobView:
    """A read-only job snapshot. Terminal jobs carry their full tool reply in detail."""

    status: Literal["queued", "running", "completed", "failed", "timed_out", "cancelled"]
    line: str
    created_at: float
    updated_at: float
    detail: dict[str, Any] | None = None


class _TaskIdParams(RequestParams):
    task_id: str


def _utc_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _task_fields(job_id: str, view: JobView) -> dict[str, Any]:
    if view.status in _ACTIVE:
        status = "working"
    elif view.status == "cancelled":
        status = "cancelled"
    else:
        status = "completed"
    return {
        "taskId": job_id,
        "status": status,
        "statusMessage": view.line,
        "createdAt": _utc_timestamp(view.created_at),
        "lastUpdatedAt": _utc_timestamp(view.updated_at),
        "ttlMs": 3_600_000,
        "pollIntervalMs": 2_000,
    }


class JobTasks(Extension):
    identifier = "io.modelcontextprotocol/tasks"

    def __init__(
        self,
        tool_names: Iterable[str],
        job_id_from_result: Callable[[HandlerResult], str | None],
        view: Callable[[str], JobView | None | Awaitable[JobView | None]],
        cancel: Callable[[str], Awaitable[object]],
    ) -> None:
        self._tool_names = frozenset(tool_names)
        self._job_id_from_result = job_id_from_result
        self._view = view
        self._cancel = cancel
        self._cancel_tasks: dict[str, asyncio.Task[None]] = {}

    def methods(self) -> Sequence[MethodBinding]:
        versions = frozenset(MODERN_PROTOCOL_VERSIONS)
        return (
            MethodBinding("tasks/get", _TaskIdParams, self._tasks_get, versions),
            MethodBinding("tasks/cancel", _TaskIdParams, self._tasks_cancel, versions),
        )

    async def intercept_tool_call(
        self,
        params: CallToolRequestParams,
        ctx: ServerRequestContext,
        call_next: CallNext,
    ) -> HandlerResult:
        if params.name not in self._tool_names or ctx.protocol_version not in MODERN_PROTOCOL_VERSIONS:
            return await call_next(ctx)

        request_caps = (params.meta or ctx.meta or {}).get(CLIENT_CAPABILITIES_META_KEY)
        request_extensions = request_caps.get("extensions") if isinstance(request_caps, Mapping) else None
        session_caps = ctx.session.client_capabilities
        if (
            not isinstance(request_extensions, Mapping)
            or self.identifier not in request_extensions
            or session_caps is None
            or self.identifier not in (session_caps.extensions or {})
        ):
            return await call_next(ctx)

        token = TASK_CALL.set(True)
        try:
            result = await call_next(ctx)
        finally:
            TASK_CALL.reset(token)

        if isinstance(result, ErrorData):
            return result
        if isinstance(result, dict):
            if result.get("isError") or result.get("resultType", "complete") != "complete":
                return result
        elif getattr(result, "is_error", False) or getattr(result, "result_type", "complete") != "complete":
            return result

        job_id = self._job_id_from_result(result)
        if job_id is None:
            return result
        view = await self._get_view(job_id)
        if view is None:
            return result
        return {"resultType": "task", **_task_fields(job_id, view)}

    async def _get_view(self, job_id: str) -> JobView | None:
        view = self._view(job_id)
        return await view if inspect.isawaitable(view) else view

    async def _tasks_get(self, ctx: ServerRequestContext, params: _TaskIdParams) -> dict[str, Any]:
        require_client_extension(ctx, self.identifier)
        view = await self._get_view(params.task_id)
        if view is None:
            raise MCPError(code=INVALID_PARAMS, message="Failed to retrieve task: Task not found")
        response = {"resultType": "complete", **_task_fields(params.task_id, view)}
        if response["status"] == "completed":
            response["result"] = {
                "content": [{"type": "text", "text": json.dumps(view.detail, ensure_ascii=False, separators=(",", ":"))}],
                "structuredContent": view.detail,
                "isError": view.status != "completed",
            }
        return response

    async def _tasks_cancel(self, ctx: ServerRequestContext, params: _TaskIdParams) -> dict[str, Any]:
        require_client_extension(ctx, self.identifier)
        job_id = params.task_id
        view = await self._get_view(job_id)
        if view is None:
            raise MCPError(code=INVALID_PARAMS, message="Failed to cancel task: Task not found")
        if job_id not in self._cancel_tasks and view.status in _ACTIVE:
            task = asyncio.create_task(self._cancel_job(job_id))
            self._cancel_tasks[job_id] = task
            task.add_done_callback(lambda _: self._cancel_tasks.pop(job_id, None))
        return {"resultType": "complete"}

    async def _cancel_job(self, job_id: str) -> None:
        try:
            await self._cancel(job_id)
        except Exception:
            logger.exception("Failed to cancel job %s", job_id)

    async def aclose(self) -> None:
        """Drain cancellation work during server shutdown."""
        while self._cancel_tasks:
            await asyncio.gather(*self._cancel_tasks.values(), return_exceptions=True)


async def pump_progress(
    ctx: Context,
    get_line: Callable[[], str],
    *,
    interval: float = 5.0,
    stop: asyncio.Event,
) -> None:
    """Report changes and a 30-second heartbeat while an ordinary tool call waits."""
    try:
        meta = ctx.request_context.meta or {}
    except ValueError:
        return  # No active request.
    if meta.get("progress_token") is None or stop.is_set():
        return

    async def report() -> None:
        last_line = None
        last_sent = 0.0
        progress = 0
        while not stop.is_set():
            await asyncio.sleep(interval)
            if stop.is_set():
                return
            try:
                line = get_line()
                now = monotonic()
                if line == last_line and now - last_sent < 30.0:
                    continue
                progress += 1
                await ctx.report_progress(progress, message=line)
                last_line, last_sent = line, now
            except Exception:
                logger.debug("Could not report job progress", exc_info=True)

    reporter = asyncio.create_task(report())
    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((reporter, stopper), return_when=asyncio.FIRST_COMPLETED)
    finally:
        reporter.cancel()
        stopper.cancel()
        await asyncio.gather(reporter, stopper, return_exceptions=True)
