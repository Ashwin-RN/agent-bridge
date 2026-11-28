"""`run` as an MCP task row, and progress while a call waits, against the real server and a stand-in CLI."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.extension import ResultClaim
from mcp.client.session import ClientSession
from mcp_types import CallToolResult, Request, Result
from pydantic import ConfigDict, TypeAdapter

from agent_bridge import server as server_mod
from agent_bridge.backends.agy import AGY_REMOTE_TURN_CANCEL_ERROR
from agent_bridge.jobs import JobManager
from agent_bridge.nesting import NOT_NESTED
from agent_bridge.process_control import is_pid_alive
from agent_bridge.server import create_server

from test_replies import _job
from test_surface import STAND_IN_CODEX, StubCodexRunner, _settings

TASKS = "io.modelcontextprotocol/tasks"
JSON_OBJECT = TypeAdapter(dict[str, Any])


class TaskReceipt(Result):
    model_config = ConfigDict(extra="allow")
    result_type: Literal["task"]


class TaskRequest(Request[dict[str, Any], str]):
    name_param = "taskId"


async def _resolve(*_args: Any) -> None:
    pytest.fail("This client handles the task receipt itself")


def _holding_codex(tmp_path: Path) -> Path:
    """A Codex stand-in that writes its pid, starts a thread, then works for a minute."""
    script = tmp_path / "codex_hold.py"
    script.write_text(
        "import json, os, sys, time\n"
        "sys.stdin.read()\n"
        f"open({str(tmp_path / 'pid.txt')!r}, 'w').write(str(os.getpid()))\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'thr-hold'}), flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    return script


def _bridge(tmp_path: Path, script: Path | None = None):
    if script is None:
        script = tmp_path / "codex_stand_in.py"
        script.write_text(STAND_IN_CODEX.strip(), encoding="utf-8")
    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, script))
    mcp, _ = create_server(settings=settings, manager=manager, nesting=NOT_NESTED)
    return mcp, manager


@asynccontextmanager
async def _client(mcp, *, declares: bool):
    extensions = {TASKS: {}} if declares else None
    claims = {TASKS: [ResultClaim(result_type="task", model=TaskReceipt, resolve=_resolve)]} if declares else None
    async with InMemoryTransport(mcp) as (read, write):
        async with ClientSession(read, write, extensions=extensions, result_claims=claims) as client:
            await client.discover()
            yield client


async def _task(client: ClientSession, method: str, job_id: str) -> dict[str, Any]:
    return await client.send_request(TaskRequest(method=method, params={"taskId": job_id}), JSON_OBJECT)


async def _until(client: ClientSession, job_id: str, status: str, timeout: float = 20) -> dict[str, Any]:
    async with asyncio.timeout(timeout):
        while (got := await _task(client, "tasks/get", job_id))["status"] != status:
            await asyncio.sleep(0.1)
    return got


def _jobs_table(manager: JobManager) -> list[tuple]:
    with sqlite3.connect(manager.store.db_path) as conn:
        return conn.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()


async def test_a_host_that_declares_tasks_gets_a_task_row_and_its_last_get_carries_the_reply(
    tmp_path: Path,
) -> None:
    mcp, manager = _bridge(tmp_path)
    async with _client(mcp, declares=True) as client:
        started = time.monotonic()
        # A task row is never collected by waiting, whatever the call asked for.
        receipt = await client.call_tool(
            "run", {"backend": "codex", "prompt": "SLEEP:2 hi", "label": "probe", "wait_seconds": 60},
            allow_claimed=True,
        )
        assert time.monotonic() - started < 5
        assert isinstance(receipt, TaskReceipt)
        fields = receipt.model_dump(by_alias=True)
        job_id = fields["taskId"]
        assert fields["status"] == "working"
        assert fields["statusMessage"].startswith("codex · ")
        assert "probe" in fields["statusMessage"]

        done = await _until(client, job_id, "completed")
        reply = done["result"]["structuredContent"]
        assert done["result"]["isError"] is False
        assert reply["job_id"] == job_id and reply["response"] == "OK:SLEEP:2 hi"
        assert reply["line"] == done["statusMessage"] or reply["line"].startswith("codex · ")
        assert json.loads(done["result"]["content"][0]["text"]) == reply

        # Reading the row writes nothing.
        before = _jobs_table(manager)
        await _task(client, "tasks/get", job_id)
        assert _jobs_table(manager) == before


async def test_codex_fast_stays_on_the_task_row(tmp_path: Path) -> None:
    mcp, manager = _bridge(tmp_path)
    try:
        async with _client(mcp, declares=True) as client:
            receipt = await client.call_tool(
                "run", {"backend": "codex", "prompt": "hi", "effort": "medium", "fast": True},
                allow_claimed=True,
            )
            assert isinstance(receipt, TaskReceipt)
            fields = receipt.model_dump(by_alias=True)
            done = await _until(client, fields["taskId"], "completed")
            assert " · medium · fast · " in fields["statusMessage"]
            assert " · medium · fast · " in done["statusMessage"]
            assert done["result"]["structuredContent"]["line"] == done["statusMessage"]
    finally:
        await manager.shutdown()


async def test_task_row_collects_its_reply_as_a_file(tmp_path: Path) -> None:
    mcp, manager = _bridge(tmp_path)
    async with _client(mcp, declares=True) as client:
        receipt = await client.call_tool(
            "run", {"backend": "codex", "prompt": "file please", "response_file": True}, allow_claimed=True,
        )
        assert isinstance(receipt, TaskReceipt)
        job_id = receipt.model_dump(by_alias=True)["taskId"]
        done = await _until(client, job_id, "completed")
        reply = done["result"]["structuredContent"]
        assert "response_file" in reply
        assert Path(reply["response_file"]) == manager.settings.state_dir / "replies" / f"{job_id}.md"
        assert Path(reply["response_file"]).read_text(encoding="utf-8") == "OK:file please"
        assert reply["response_head"] == "OK:file please" and reply["response_total_chars"] == 14
        assert "response" not in reply
        assert (await _task(client, "tasks/get", job_id))["result"]["structuredContent"] == reply


async def test_fast_failing_task_run_returns_error_detail_and_supports_task_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp, manager = _bridge(tmp_path)

    async def fail_fast(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("missing cli binary")

    monkeypatch.setattr(manager.codex_runner, "run", fail_fast)
    orig_get = manager.get_async

    async def get_after_done(job_id: str) -> Any:
        ev = manager._events.get(job_id)
        if ev is not None:
            await asyncio.wait_for(ev.wait(), timeout=5)
        return await orig_get(job_id)

    monkeypatch.setattr(manager, "get_async", get_after_done)
    async with _client(mcp, declares=True) as client:
        result = await client.call_tool("run", {"backend": "codex", "prompt": "hi"}, allow_claimed=True)
        assert isinstance(result, CallToolResult)
        assert result.is_error is True
        detail = result.structured_content
        assert detail["status"] == "failed"
        assert "missing cli binary" in detail["error"]
        job_id = detail["job_id"]

        got = await _task(client, "tasks/get", job_id)
        assert got["status"] == "completed"
        assert got["result"]["isError"] is True
        assert got["result"]["structuredContent"]["job_id"] == job_id

        cancel_ack = await _task(client, "tasks/cancel", job_id)
        assert cancel_ack["resultType"] == "complete"


async def test_a_host_that_does_not_declare_tasks_gets_the_ordinary_receipt(tmp_path: Path) -> None:
    mcp, manager = _bridge(tmp_path)
    async with _client(mcp, declares=False) as client:
        result = await client.call_tool("run", {"backend": "codex", "prompt": "plain"}, allow_claimed=True)
        assert isinstance(result, CallToolResult) and not result.is_error
        assert result.structured_content["next"].startswith("jobs(job_ids=[")
        job_id = result.structured_content["job_id"]
    await manager.wait_for([job_id], wait_seconds=20)
    await manager.shutdown()


async def test_a_stop_from_the_host_s_task_list_cancels_the_job(tmp_path: Path) -> None:
    mcp, manager = _bridge(tmp_path, _holding_codex(tmp_path))
    pid_file = tmp_path / "pid.txt"
    async with _client(mcp, declares=True) as client:
        receipt = await client.call_tool("run", {"backend": "codex", "prompt": "hold"}, allow_claimed=True)
        job_id = receipt.model_dump(by_alias=True)["taskId"]
        async with asyncio.timeout(15):
            while not pid_file.exists() or not pid_file.read_text():
                await asyncio.sleep(0.05)
        pid = int(pid_file.read_text())
        # Claude Code sends the stop twice without waiting; each is answered at once.
        acks = await asyncio.wait_for(
            asyncio.gather(_task(client, "tasks/cancel", job_id), _task(client, "tasks/cancel", job_id)), timeout=2,
        )
        assert [a["resultType"] for a in acks] == ["complete", "complete"]
        got = await _until(client, job_id, "cancelled")
        assert got["statusMessage"].startswith("codex · ") and "cancelled" in got["statusMessage"]
    stored = manager.store.get_job(job_id)
    assert stored is not None and stored.status == "cancelled" and stored.provider_state == "stopped"
    assert not is_pid_alive(pid)


async def test_cancelled_task_reports_that_the_provider_may_still_be_running(tmp_path: Path) -> None:
    mcp, manager = _bridge(tmp_path)
    job = _job(job_id="neon-1a2b3c4d", backend="neon", owner_id=manager.owner_id, response="", response_chars=0)
    guidance = f"Confirm that this turn has ended, then resume with confirm_prior_job_id={job.job_id!r}."
    manager.store.insert_job(replace(job, status="running", finished_at=None))
    manager.store.finalize_job(replace(
        job, status="cancelled", provider_state="possibly_running", failure_reason="cancelled",
        error=AGY_REMOTE_TURN_CANCEL_ERROR, retry_guidance=guidance,
    ))
    async with _client(mcp, declares=True) as client:
        task = await _task(client, "tasks/get", job.job_id)
        cancelled = await client.call_tool("cancel", {"job_id": job.job_id})
        assert task["status"] == "cancelled"
        assert "may still be running" in task["statusMessage"]
        assert cancelled.structured_content["retry_guidance"] in task["statusMessage"]
        assert task["result"]["isError"] is True
        detail = task["result"]["structuredContent"]
        assert detail == {**cancelled.structured_content, "line": task["statusMessage"]}


async def test_a_waiting_run_sends_its_line_as_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, "PROGRESS_INTERVAL_SECONDS", 0.05)
    mcp, manager = _bridge(tmp_path)
    messages: list[str] = []

    async def on_progress(_value: float, _total: float | None, message: str | None) -> None:
        messages.append(message or "")

    async with _client(mcp, declares=False) as client:
        result = await client.call_tool(
            "run", {"backend": "codex", "prompt": "SLEEP:1.5 hi", "label": "waiting", "wait_seconds": 30},
            progress_callback=on_progress,
        )
        assert result.structured_content["status"] == "completed"
    assert messages and all(m.startswith("codex · ") for m in messages)
    assert any("running" in m and "waiting" in m for m in messages)


async def test_a_waiting_jobs_call_sends_one_line_for_several_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_mod, "PROGRESS_INTERVAL_SECONDS", 0.05)
    mcp, manager = _bridge(tmp_path)
    messages: list[str] = []

    async def on_progress(_value: float, _total: float | None, message: str | None) -> None:
        messages.append(message or "")

    async with _client(mcp, declares=False) as client:
        ids = []
        for prompt in ("SLEEP:1.5 a", "SLEEP:1.5 b"):
            res = await client.call_tool("run", {"backend": "codex", "prompt": prompt})
            ids.append(res.structured_content["job_id"])
        out = await client.call_tool("jobs", {"job_ids": ids, "wait_seconds": 30}, progress_callback=on_progress)
        assert out.structured_content["all_done"] is True
    assert messages and all(m.startswith("2 jobs: codex · ") for m in messages)
