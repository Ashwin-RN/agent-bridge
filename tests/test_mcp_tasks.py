from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.extension import ResultClaim
from mcp.client.session import ClientSession
from mcp.server.extension import Extension
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    SERVER_INFO_META_KEY,
    CallToolResult,
    ErrorData,
    Request,
    Result,
    TextContent,
)
from pydantic import ConfigDict, TypeAdapter

from agent_bridge import mcp_tasks
from agent_bridge.mcp_tasks import TASK_CALL, JobTasks, JobView, pump_progress

TASKS = "io.modelcontextprotocol/tasks"
JOB_ID = "job-123"
LINE_HEAD = "codex · requested gpt-6.2-sol · xhigh · review auth"
RUNNING = JobView("running", f"{LINE_HEAD} · running 2m10s · reading jobs.py", 0, 130.5)
DETAIL = {"failure_reason": "backend", "error": "réponse indisponible"}
DETAIL_TEXT = '{"failure_reason":"backend","error":"réponse indisponible"}'
JSON_OBJECT = TypeAdapter(dict[str, Any])


class TaskReceipt(Result):
    model_config = ConfigDict(extra="allow")
    result_type: Literal["task"]


class TaskRequest(Request[dict[str, Any], str]):
    name_param = "taskId"


class WireErrors(Extension):
    identifier = "com.example/wire-errors"

    async def intercept_tool_call(self, params, ctx, call_next):
        result = await call_next(ctx)
        mode = (params.arguments or {}).get("mode")
        if mode == "dict-error":
            return {**result.model_dump(by_alias=True), "isError": True}
        if mode == "rpc-result":
            return ErrorData(code=-32602, message="invalid job")
        return result


async def _resolve(*_args):
    pytest.fail("This client handles the task receipt itself")


class ToyBridge:
    def __init__(self, *, selected=True, skew=None, async_view=False):
        self.jobs = {JOB_ID: RUNNING}
        self.calls = []
        self.extractions = []
        self.cancel = AsyncMock()

        def view(job_id):
            return self.jobs.get(job_id)

        async def read_view(job_id):
            await asyncio.sleep(0)
            return view(job_id)

        def extract(result):
            self.extractions.append(TASK_CALL.get())
            return (result.structured_content or {}).get("job_id")

        async def skew_capabilities(ctx, call_next):
            if ctx.method == "tools/call":
                if skew == "request":
                    meta = dict(ctx.params["_meta"])
                    meta[CLIENT_CAPABILITIES_META_KEY] = {}
                    ctx = replace(ctx, params={**ctx.params, "_meta": meta}, meta=meta)
                elif skew == "session":
                    ctx = replace(ctx, session=SimpleNamespace(client_capabilities=None))
                elif skew == "version":
                    ctx = replace(ctx, protocol_version="2025-11-25")
            return await call_next(ctx)

        self.extension = JobTasks(
            {"run_job"} if selected else set(), extract, read_view if async_view else view, self.cancel,
        )
        self.server = MCPServer(
            "toy", extensions=[self.extension, WireErrors()], middleware=[skew_capabilities],
        )

        @self.server.tool()
        async def run_job(ctx: Context, mode: str = "receipt") -> CallToolResult:
            self.calls.append(TASK_CALL.get())
            if mode == "raised":
                raise ValueError("invalid job")
            if mode == "rpc":
                raise MCPError(code=-32602, message="invalid job")
            if mode == "progress":
                await self.progress(ctx)
            detail = {} if mode == "no-id" else {"job_id": JOB_ID}
            return CallToolResult(
                content=[TextContent(type="text", text=mode)],
                structured_content=detail,
                is_error=mode == "refused",
            )

    @asynccontextmanager
    async def connect(self, *, declares=True, legacy=False):
        extensions = {TASKS: {}} if declares else None
        claims = {TASKS: [ResultClaim(result_type="task", model=TaskReceipt, resolve=_resolve)]} if declares else None
        try:
            async with InMemoryTransport(self.server) as (read, write):
                async with ClientSession(read, write, extensions=extensions, result_claims=claims) as client:
                    if legacy:
                        await client.initialize()
                    else:
                        await client.discover()
                    yield client
        finally:
            await self.extension.aclose()


async def task_request(client, method, job_id=JOB_ID):
    return await client.send_request(TaskRequest(method=method, params={"taskId": job_id}), JSON_OBJECT)


def task_fields(status, line):
    return {
        "taskId": JOB_ID,
        "status": status,
        "statusMessage": line,
        "createdAt": "1970-01-01T00:00:00Z",
        "lastUpdatedAt": "1970-01-01T00:02:10.500000Z",
        "ttlMs": 3_600_000,
        "pollIntervalMs": 2_000,
        "_meta": {SERVER_INFO_META_KEY: {"name": "toy", "version": ""}},
    }


async def test_task_receipt():
    bridge = ToyBridge(async_view=True)
    async with bridge.connect() as client:
        result = await client.call_tool("run_job", allow_claimed=True)
    assert isinstance(result, TaskReceipt)
    assert result.model_dump(by_alias=True) == {"resultType": "task", **task_fields("working", RUNNING.line)}
    assert bridge.calls == [True]
    assert bridge.extractions == [False]


@pytest.mark.parametrize("case", ["undeclared", "unselected", "legacy", "request", "session", "version"])
async def test_plain_receipt(case):
    bridge = ToyBridge(selected=case != "unselected", skew=case)
    async with bridge.connect(declares=case != "undeclared", legacy=case == "legacy") as client:
        result = await client.call_tool("run_job", allow_claimed=True)
    assert isinstance(result, CallToolResult)
    assert result.structured_content == {"job_id": JOB_ID}
    assert result.result_type == "complete"
    assert bridge.calls == [False]
    assert bridge.extractions == []


@pytest.mark.parametrize("mode", ["raised", "refused", "rpc", "dict-error", "rpc-result", "no-id", "unreadable"])
async def test_no_task(mode):
    bridge = ToyBridge()
    if mode == "unreadable":
        bridge.jobs.clear()
    async with bridge.connect() as client:
        if mode in ("rpc", "rpc-result"):
            with pytest.raises(MCPError, match="invalid job") as exc:
                await client.call_tool("run_job", {"mode": mode})
            assert exc.value.code == -32602
        else:
            result = await client.call_tool("run_job", {"mode": mode}, allow_claimed=True)
            assert isinstance(result, CallToolResult)
            assert result.is_error == (mode in ("raised", "refused", "dict-error"))
            assert result.content[0].text == ("Error executing tool run_job" if mode == "raised" else mode)
    assert bridge.calls == [True]
    assert bridge.extractions == ([False] if mode in ("no-id", "unreadable") else [])


@pytest.mark.parametrize("status,wire_status,is_error", [
    ("queued", "working", None),
    ("running", "working", None),
    ("completed", "completed", False),
    ("failed", "completed", True),
    ("timed_out", "completed", True),
    ("cancelled", "cancelled", None),
])
async def test_task_status(status, wire_status, is_error):
    bridge = ToyBridge()
    line = f"{LINE_HEAD} · {status}"
    bridge.jobs[JOB_ID] = replace(
        RUNNING, status=status, line=line, detail=DETAIL if status not in ("queued", "running") else None,
    )
    expected = {"resultType": "complete", **task_fields(wire_status, line)}
    if is_error is not None:
        expected["result"] = {
            "content": [{"type": "text", "text": DETAIL_TEXT}],
            "structuredContent": DETAIL,
            "isError": is_error,
        }
    async with bridge.connect() as client:
        assert await task_request(client, "tasks/get") == expected


@pytest.mark.parametrize("method,verb", [("tasks/get", "retrieve"), ("tasks/cancel", "cancel")])
async def test_unknown_task(method, verb):
    bridge = ToyBridge()
    async with bridge.connect() as client:
        with pytest.raises(MCPError, match=f"^Failed to {verb} task: Task not found$") as exc:
            await task_request(client, method, "missing")
        assert exc.value.code == -32602
    bridge.cancel.assert_not_awaited()


@pytest.mark.parametrize("method", ["tasks/get", "tasks/cancel"])
async def test_task_methods_require_declaration(method):
    bridge = ToyBridge()
    async with bridge.connect(declares=False) as client:
        with pytest.raises(MCPError) as exc:
            await task_request(client, method)
        assert exc.value.code == -32021
        assert exc.value.error.data == {"requiredCapabilities": {"extensions": {TASKS: {}}}}
    bridge.cancel.assert_not_awaited()


async def test_cancel_once():
    bridge = ToyBridge(async_view=True)

    async def cancel(job_id):
        await asyncio.sleep(2)
        bridge.jobs[job_id] = replace(RUNNING, status="cancelled", detail={})

    bridge.cancel.side_effect = cancel
    async with bridge.connect() as client:
        ack = {"resultType": "complete", "_meta": {SERVER_INFO_META_KEY: {"name": "toy", "version": ""}}}
        replies = await asyncio.wait_for(asyncio.gather(
            task_request(client, "tasks/cancel"), task_request(client, "tasks/cancel"),
        ), timeout=1)
        assert replies == [ack, ack]
        assert bridge.jobs[JOB_ID].status == "running"
        await bridge.extension.aclose()
        assert bridge.jobs[JOB_ID].status == "cancelled"
        assert await task_request(client, "tasks/cancel") == ack
    bridge.cancel.assert_awaited_once_with(JOB_ID)


async def test_cancel_failure(caplog):
    bridge = ToyBridge()
    bridge.cancel.side_effect = [RuntimeError("cancel unavailable"), None]
    async with bridge.connect() as client:
        assert (await task_request(client, "tasks/cancel"))["resultType"] == "complete"
        await bridge.extension.aclose()
        assert "cancel unavailable" in caplog.text
        assert (await task_request(client, "tasks/cancel"))["resultType"] == "complete"
    assert bridge.cancel.await_count == 2


async def test_discovery():
    bridge = ToyBridge()
    async with bridge.connect(declares=False) as client:
        assert client.protocol_version == "2026-07-28"
        assert client.discover_result.capabilities.extensions[TASKS] == {}


@pytest.mark.parametrize("token", [True, False], ids=["token", "no-token"])
async def test_progress_wire(token):
    bridge = ToyBridge()
    stop = asyncio.Event()
    get_line = Mock(return_value=RUNNING.line)
    notifications = []

    async def progress(ctx):
        if token:
            await pump_progress(ctx, get_line, interval=0.01, stop=stop)
        else:
            # A tokenless pump returns without the tool asking it to stop.
            await asyncio.wait_for(pump_progress(ctx, get_line, stop=stop), timeout=0.5)

    async def on_progress(value, total, message):
        notifications.append((value, total, message))
        stop.set()

    bridge.progress = progress
    async with bridge.connect(declares=False) as client:
        result = await asyncio.wait_for(client.call_tool(
            "run_job", {"mode": "progress"}, progress_callback=on_progress if token else None,
        ), timeout=1)
        assert not result.is_error
    assert notifications == ([(1, None, RUNNING.line)] if token else [])
    assert get_line.call_count == int(token)


async def test_progress_heartbeat(monkeypatch):
    times = iter([0, 5, 29.9, 30, 35, 35])
    monkeypatch.setattr(mcp_tasks, "monotonic", lambda: next(times))
    stop = asyncio.Event()
    ctx = SimpleNamespace(request_context=SimpleNamespace(meta={"progress_token": 0}), report_progress=AsyncMock())
    lines = iter(["starting", "starting", "starting", "starting", "done"])

    def get_line():
        line = next(lines, None)
        if line is None:
            stop.set()
            return "done"
        return line

    await asyncio.wait_for(pump_progress(ctx, get_line, interval=0, stop=stop), timeout=1)
    assert [(call.args[0], call.kwargs["message"]) for call in ctx.report_progress.await_args_list] == [
        (1, "starting"), (2, "starting"), (3, "done"),
    ]


async def test_progress_stops_during_send():
    stop = asyncio.Event()

    async def report(*args, **kwargs):
        stop.set()
        await asyncio.Future()

    ctx = SimpleNamespace(request_context=SimpleNamespace(meta={"progress_token": "p"}), report_progress=report)
    await asyncio.wait_for(pump_progress(ctx, lambda: "working", interval=0, stop=stop), timeout=0.5)


async def test_progress_recovers():
    stop = asyncio.Event()
    get_line = Mock(side_effect=[ValueError("read failed"), "working", "working"])

    async def report(progress, *, message):
        if progress == 1:
            raise OSError("send failed")
        stop.set()

    send = AsyncMock(side_effect=report)
    ctx = SimpleNamespace(request_context=SimpleNamespace(meta={"progress_token": "p"}), report_progress=send)
    await asyncio.wait_for(pump_progress(ctx, get_line, interval=0, stop=stop), timeout=0.5)
    assert [call.args[0] for call in send.await_args_list] == [1, 2]
