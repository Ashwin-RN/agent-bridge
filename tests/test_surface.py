"""The five tools: `run` answers at once, `jobs` collects and pages, `usage` gives one quota shape.

Every job here is played by a Codex stand-in; no real CLI is started.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from mcp.types import Implementation

from agent_bridge import host_limits
from agent_bridge import server as server_mod
from agent_bridge.config import Settings
from agent_bridge.jobs import JobManager
from agent_bridge.models import JobRecord
from agent_bridge.nesting import NOT_NESTED
from agent_bridge.runners import CodexRunner
from agent_bridge.server import create_server

# Reads its prompt, then acts on the words in it: `SLEEP:<s>` works that long, `SIZE:<n>` answers with
# n characters, `EMPTY` answers with nothing, `ARGV` answers with its own command line.
STAND_IN_CODEX = r'''
import json, sys, time
if "--version" in sys.argv[1:]:
    print("codex-cli 0.99.0")
    sys.exit(0)
if sys.argv[1:2] != ["exec"]:
    sys.exit(0)  # anything but a job, such as the quota read, ends at once
prompt = "" if "review" in sys.argv[1:] else sys.stdin.read()
words = dict(w.split(":", 1) if ":" in w else (w, "") for w in prompt.split())
print(json.dumps({"type": "thread.started", "thread_id": "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"}), flush=True)
time.sleep(float(words.get("SLEEP") or 0))
if "SIZE" in words:
    text = ("0123456789" * (int(words["SIZE"]) // 10 + 1))[: int(words["SIZE"])]
elif "EMPTY" in words:
    text = ""
elif "ARGV" in words or not prompt:
    text = json.dumps({"argv": sys.argv[1:], "prompt": prompt})
else:
    text = "OK:" + prompt.strip()
print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": text}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1200, "cached_input_tokens": 900,
      "output_tokens": 80, "reasoning_output_tokens": 20}}), flush=True)
'''


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        default_neon_model="neon-high",
        default_codex_model=None,
        default_codex_effort=None,
        default_timeout_seconds=60,
        max_concurrent_jobs=4,
        max_stored_jobs=50,
        models_cache_ttl_seconds=900.0,
        quota_cache_ttl_seconds=120.0,
        owner_stale_seconds=30.0,
        state_dir=tmp_path / "state",
        agy_bin=str(tmp_path / "missing-agy.exe"),
        codex_bin=str(tmp_path / "missing-codex.exe"),
        claude_bin=str(tmp_path / "missing-claude.exe"),
        default_cwd=tmp_path,
    )


class StubCodexRunner(CodexRunner):
    def __init__(self, settings: Settings, script: Path) -> None:
        self._script = script
        super().__init__(settings)

    def resolve_command(self) -> list[str]:
        return [sys.executable, str(self._script)]


def _bridge(tmp_path: Path):
    script = tmp_path / "codex_stand_in.py"
    script.write_text(STAND_IN_CODEX.strip(), encoding="utf-8")
    settings = _settings(tmp_path)
    manager = JobManager(settings, codex_runner=StubCodexRunner(settings, script))
    mcp, _ = create_server(settings=settings, manager=manager, nesting=NOT_NESTED)
    return mcp, manager


def _data(raw: Any) -> dict[str, Any]:
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
        return raw[1]
    if isinstance(raw, dict):
        return raw
    return json.loads(raw.content[0].text)


async def _call(mcp, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return _data(await mcp.call_tool(tool, args))


@pytest.fixture
def host_limit(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    calls: dict[str, list] = {"identify": [], "lookup": [], "cap": []}
    detected = SimpleNamespace(name="test-host")
    event_loop_thread = threading.get_ident()

    def identify(client_name):
        assert threading.get_ident() != event_loop_thread
        calls["identify"].append(client_name)
        return detected

    def wait_cap(host):
        assert threading.get_ident() != event_loop_thread
        calls["lookup"].append(host.name)
        return 1

    def cap_wait(requested, host):
        assert threading.get_ident() != event_loop_thread
        calls["cap"].append((requested, host.name))
        return (1, "Test host limits waits to 1 s.") if requested > 1 else (requested, None)

    monkeypatch.setattr(host_limits, "identify_host", identify)
    monkeypatch.setattr(host_limits, "wait_cap_seconds", wait_cap)
    monkeypatch.setattr(host_limits, "cap_wait", cap_wait)
    return calls


@pytest.fixture
def unknown_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_limits, "identify_host", lambda _client_name: None)
    monkeypatch.setattr(host_limits, "wait_cap_seconds", lambda _host: None)


# --- run and jobs ---


async def test_host_wait_caps_jobs_and_leaves_the_job_running(tmp_path: Path, host_limit: dict) -> None:
    mcp, manager = _bridge(tmp_path)
    async with InMemoryTransport(mcp) as (read, write):
        async with ClientSession(read, write, client_info=Implementation(name="test-client", version="1")) as client:
            await client.initialize()
            receipt = _data(await client.call_tool("run", {"backend": "codex", "prompt": "SLEEP:10 hold"}))
            job_id = receipt["job_id"]
            started = time.monotonic()
            async with asyncio.timeout(3):
                out = _data(await client.call_tool("jobs", {"job_ids": [job_id, job_id], "wait_seconds": 30}))
            assert 0.9 <= time.monotonic() - started < 3
            assert out["wait_note"] == "Test host limits waits to 1 s."
            assert json.dumps(out).count('"wait_note"') == 1
            assert out["all_done"] is False and all(job["status"] == "running" for job in out["jobs"])
            assert (await manager.get_async(job_id)).status == "running"

            immediate = _data(await client.call_tool("jobs", {"job_ids": [job_id]}))
            assert "wait_note" not in immediate and "wait_note" not in receipt
            second = _data(await client.call_tool("jobs", {"job_ids": [job_id], "wait_seconds": 2}))
            assert second["wait_note"] == out["wait_note"]
            backends = _data(await client.call_tool("backends", {}))
            assert (backends["host"], backends["wait_cap"]) == ("test-host", 1)
            assert host_limit == {
                "identify": ["test-client"], "lookup": ["test-host"], "cap": [(30, "test-host")],
            }


async def test_host_wait_caps_run_and_recommends_safe_next(tmp_path: Path, host_limit: dict) -> None:
    mcp, manager = _bridge(tmp_path)
    try:
        started = time.monotonic()
        async with asyncio.timeout(3):
            receipt = await _call(mcp, "run", {
                "backend": "codex", "prompt": "SLEEP:10 hold", "wait_seconds": 30, "timeout_seconds": 44,
            })
        assert 0.9 <= time.monotonic() - started < 3
        assert receipt["wait_note"] == "Test host limits waits to 1 s."
        assert receipt["status"] == "running" and receipt["timeout_seconds"] == 44
        assert receipt["next"] == f"jobs(job_ids=['{receipt['job_id']}'], wait_seconds=1)"
        assert (await manager.get_async(receipt["job_id"])).status == "running"
        assert host_limit["cap"] == [(30, "test-host")]
    finally:
        await manager.shutdown()


async def test_a_capped_wait_that_saw_the_job_end_carries_no_wait_note(tmp_path: Path, host_limit: dict) -> None:
    """The note says the job is still running: a job that ended inside the shortened wait must not carry it."""
    mcp, manager = _bridge(tmp_path)
    try:
        waited = await _call(mcp, "run", {"backend": "codex", "prompt": "hello", "wait_seconds": 30})
        assert waited["status"] == "completed" and "wait_note" not in waited
        collected = await _call(mcp, "jobs", {"job_ids": [waited["job_id"]], "wait_seconds": 30})
        assert collected["all_done"] is True and "wait_note" not in collected
    finally:
        await manager.shutdown()


async def test_run_returns_a_small_receipt_at_once_and_jobs_collects_the_reply(
    tmp_path: Path, unknown_host: None,
) -> None:
    mcp, manager = _bridge(tmp_path)
    try:
        started = time.monotonic()
        receipt = await _call(mcp, "run", {"backend": "codex", "prompt": "SLEEP:3 hello", "label": "greet"})
        assert time.monotonic() - started < 2.5  # it did not wait for the 3-second job
        # `model` is there once the CLI's start has named one, which may or may not have happened yet.
        assert [k for k in receipt if k != "model"] == ["line", "job_id", "status", "backend", "timeout_seconds", "next"]
        assert receipt["status"] in ("queued", "running") and receipt["backend"] == "codex"
        assert receipt["line"].startswith("codex · ") and "greet" in receipt["line"]
        assert receipt["timeout_seconds"] == 60
        assert receipt["next"] == f"jobs(job_ids=['{receipt['job_id']}'], wait_seconds=50)"

        out = await _call(mcp, "jobs", {"job_ids": [receipt["job_id"]], "wait_seconds": 30})
        [job] = out["jobs"]
        assert (out["all_done"], out["any_done"]) == (True, True)
        assert job["status"] == "completed" and job["response"] == "OK:SLEEP:3 hello"
        assert job["line"].startswith("codex · ") and " · completed " in job["line"]
    finally:
        await manager.shutdown()


async def test_run_with_wait_seconds_returns_a_finished_job_in_full_and_a_running_one_as_a_receipt(
    tmp_path: Path,
) -> None:
    mcp, manager = _bridge(tmp_path)
    try:
        done = await _call(mcp, "run", {"backend": "codex", "prompt": "quick", "wait_seconds": 30})
        assert done["status"] == "completed" and done["response"] == "OK:quick"
        assert "next" not in done

        still = await _call(mcp, "run", {"backend": "codex", "prompt": "SLEEP:5 slow", "wait_seconds": 1})
        assert still["status"] == "running" and "next" in still and "response" not in still
        await manager.cancel(still["job_id"])
    finally:
        await manager.shutdown()


async def test_every_wait_goes_through_one_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown_host: None,
) -> None:
    mcp, manager = _bridge(tmp_path)
    seen = []

    async def observe_wait(_job_ids, *, wait_seconds, **_kwargs):
        seen.append(wait_seconds)
        return []

    monkeypatch.setattr(manager, "wait_for", observe_wait)
    try:
        for asked in (0, None, -5, 42, 10_000):
            args: dict[str, Any] = {"job_ids": ["codex-00000001"]}
            if asked is not None:
                args["wait_seconds"] = asked
            await _call(mcp, "jobs", args)
        assert seen == [0, 0, 0, 42, 300]
    finally:
        await manager.shutdown()


async def test_jobs_waits_no_longer_than_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown_host: None,
) -> None:
    mcp, manager = _bridge(tmp_path)
    monkeypatch.setattr(server_mod, "MAX_WAIT_SECONDS", 1)
    try:
        receipt = await _call(mcp, "run", {"backend": "codex", "prompt": "SLEEP:6 hold"})
        started = time.monotonic()
        out = await _call(mcp, "jobs", {"job_ids": [receipt["job_id"]], "wait_seconds": 300})
        assert time.monotonic() - started < 4
        assert out["jobs"][0]["status"] == "running" and out["all_done"] is False
        await manager.cancel(receipt["job_id"])
    finally:
        await manager.shutdown()


async def test_jobs_refuses_a_negative_response_offset(tmp_path: Path) -> None:
    mcp, manager = _bridge(tmp_path)
    try:
        done = await _call(mcp, "run", {"backend": "codex", "prompt": "paged", "wait_seconds": 30})
        with pytest.raises(Exception, match="response_offset cannot be negative"):
            await mcp.call_tool("jobs", {"job_ids": [done["job_id"]], "response_offset": -1})
    finally:
        await manager.shutdown()


async def test_jobs_list_is_short(tmp_path: Path) -> None:
    mcp, manager = _bridge(tmp_path)
    try:
        await _call(mcp, "run", {"backend": "codex", "prompt": "listed", "wait_seconds": 30})
        out = await _call(mcp, "jobs", {})
        assert out["count"] == 1
        assert list(out["jobs"][0]) == ["line", "job_id", "status", "prompt_preview"]
    finally:
        await manager.shutdown()


@pytest.mark.parametrize("form", ["new", "resume", "review"])
async def test_codex_fast_cli_override(tmp_path: Path, form: str) -> None:
    mcp, manager = _bridge(tmp_path)
    args: dict[str, Any] = {"backend": "codex", "prompt": "ARGV", "wait_seconds": 30}
    try:
        if form == "resume":
            first = await _call(mcp, "run", args)
            args["resume_from"] = first["job_id"]
        elif form == "review":
            args.update(prompt="", review={"scope": "uncommitted"})
        reply = await _call(mcp, "run", {**args, "fast": True})
        assert reply["status"] == "completed"
        argv = json.loads(reply["response"])["argv"]
        assert argv[:2] == ["exec", "--json" if form == "new" else form]
        tier = argv.index('service_tier="fast"')
        assert argv[tier - 1] == "-c"
    finally:
        await manager.shutdown()


async def test_codex_fast_is_opt_in_on_each_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_bridge.runners.read_codex_local_defaults", lambda: {})
    mcp, manager = _bridge(tmp_path)
    args = {"backend": "codex", "prompt": "ARGV", "wait_seconds": 30}
    try:
        fast = await _call(mcp, "run", {**args, "fast": True})
        assert 'service_tier="fast"' in json.loads(fast["response"])["argv"]
        for resume in ({"resume_from": fast["job_id"]}, {}):
            reply = await _call(mcp, "run", {**args, **resume})
            assert reply["status"] == "completed"
            argv = json.loads(reply["response"])["argv"]
            assert all(not arg.startswith("service_tier=") for arg in argv)
            assert argv.count("-c") == bool(resume)  # resume still overrides sandbox_mode
    finally:
        await manager.shutdown()


async def test_codex_fast_line_follows_effort(tmp_path: Path) -> None:
    mcp, manager = _bridge(tmp_path)
    try:
        receipt = await _call(mcp, "run", {"backend": "codex", "prompt": "hi", "effort": "medium", "fast": True})
        [done] = (await _call(mcp, "jobs", {"job_ids": [receipt["job_id"]], "wait_seconds": 30}))["jobs"]
        [summary] = (await _call(mcp, "jobs", {}))["jobs"]
        assert done["status"] == "completed"
        for reply in (receipt, done, summary):
            assert " · medium · fast · " in reply["line"]
    finally:
        await manager.shutdown()


# --- review ---


async def test_a_codex_review_with_no_focus_is_codex_s_own_review_and_any_focus_makes_it_a_prompt(
    tmp_path: Path,
) -> None:
    mcp, manager = _bridge(tmp_path)
    try:
        builtin = await _call(mcp, "run", {"backend": "gpt", "review": {"scope": "base"}, "wait_seconds": 30})
        seen = json.loads(builtin["response"])
        assert seen["argv"][:2] == ["exec", "review"] and seen["argv"][-1] != "--uncommitted"
        assert seen["argv"][seen["argv"].index("--base") + 1] == "main"
        assert builtin["line"].endswith("codex-review-base · codex")
        assert builtin["mode"] == "read_only"

        focused = await _call(
            mcp, "run",
            {"backend": "codex", "prompt": "ARGV", "review": {"scope": "commit", "commit": "abc123"}, "wait_seconds": 30},
        )
        seen = json.loads(focused["response"])
        assert "review" not in seen["argv"]
        assert "Review the changes introduced by commit `abc123`" in seen["prompt"]
        assert "ARGV" in seen["prompt"]  # the prompt is added to the review's focus
    finally:
        await manager.shutdown()


@pytest.mark.parametrize(
    "args,refusal",
    [
        ({"mode": "edit"}, "A review is read-only"),
        ({"resume_from": "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"}, "resume_from cannot be combined"),
        ({"review": {"scope": "commit"}}, "review.commit (a SHA) is required"),
    ],
)
async def test_a_review_that_would_not_be_one_is_refused(tmp_path: Path, args: dict, refusal: str) -> None:
    mcp, manager = _bridge(tmp_path)
    try:
        with pytest.raises(Exception, match=refusal.replace("(", r"\(").replace(")", r"\)")):
            await mcp.call_tool("run", {"backend": "codex", "review": {"scope": "uncommitted"}, **args})
        assert manager.store.list_jobs(limit=5) == []
    finally:
        await manager.shutdown()


# --- usage and backends ---


@pytest.mark.parametrize("empty", [False, True])
async def test_agy_fast_warns_and_keeps_other_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty: bool,
) -> None:
    mcp, manager = _bridge(tmp_path)
    script = tmp_path / "agy_stand_in.py"
    script.write_text(
        "import json, sys\n"
        "if sys.argv[1:2] == ['models']:\n"
        "    print('gemini-4-neon-high\\tGemini 4 Neon (High)')\n"
        "    sys.exit(0)\n"
        "argv = sys.argv[1:]\n"
        "assert not any(arg.startswith('service_tier=') for arg in argv)\n"
        f"response = '' if {empty!r} else json.dumps(argv)\n"
        "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'response': response}}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(manager.neon_runner, "resolve_command", lambda: [sys.executable, str(script)])
    if not empty:
        def fail_export(*_args: Any) -> None:
            raise OSError("test export failure")

        monkeypatch.setattr(server_mod, "_write_reply_file", fail_export)
    try:
        reply = await _call(mcp, "run", {
            "backend": "neon", "prompt": "hi", "fast": True, "wait_seconds": 30, "response_file": True,
        })
        assert reply["status"] == "completed"
        if not empty:
            assert "--model" in json.loads(reply["response"])
        assert "fast" not in reply["line"].split(" · ")
        assert "neon has no fast tier" in reply["warning"]
        assert ("empty reply" if empty else "Could not write reply file") in reply["warning"]
        assert len(reply["warning"].splitlines()) == 1
    finally:
        await manager.shutdown()


def _finish(manager: JobManager, job_id: str, *, backend: str, pool: str, tokens: int, finished: float,
            parent_run: str | None = None) -> None:
    rec = JobRecord(
        job_id=job_id, owner_id=manager.owner_id, backend=backend, cwd=str(manager.settings.default_cwd),  # type: ignore[arg-type]
        mode="read_only", requested_model=None, model=None, prompt_preview="x", quota_pool=pool,  # type: ignore[arg-type]
        parent_run=parent_run, status="running", started_at=finished - 2, created_at=finished - 2,
    )
    manager.store.insert_job(rec)
    rec.status, rec.finished_at, rec.duration_seconds = "completed", finished, 2.0
    rec.usage, rec.usage_status = {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens}, "exact"
    manager.store.finalize_job(rec)


async def test_usage_summary_gives_each_figure_once_and_honours_since_until_and_filters(tmp_path: Path) -> None:
    mcp, manager = _bridge(tmp_path)
    now = time.time()
    try:
        _finish(manager, "codex-00000001", backend="codex", pool="codex", tokens=100, finished=now - 5)
        _finish(manager, "neon-00000002", backend="neon", pool="agy_gemini", tokens=40, finished=now - 3,
                parent_run="wf")
        out = await _call(mcp, "usage", {"period": "all"})
        assert set(out) == {"scope", "totals", "by_quota_pool", "note"}
        assert out["totals"] == {"runs": 2, "completed": 2, "duration_seconds": 4.0, "total_tokens": 140}
        assert out["by_quota_pool"]["codex"] == {"runs": 1, "completed": 1, "duration_seconds": 2.0, "total_tokens": 100}
        assert "by_backend" not in out and "owner_id" not in json.dumps(out)

        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 4))
        assert (await _call(mcp, "usage", {"since": since}))["totals"]["total_tokens"] == 40
        assert (await _call(mcp, "usage", {"period": "all", "parent_run": "wf"}))["totals"]["runs"] == 1
        assert (await _call(mcp, "usage", {"period": "all", "backend": "codex"}))["totals"]["total_tokens"] == 100
        with pytest.raises(Exception, match="either period or since/until"):
            await mcp.call_tool("usage", {"period": "7d", "since": since})
        with pytest.raises(Exception, match="filter view='summary' only"):
            await mcp.call_tool("usage", {"view": "dashboard", "backend": "codex"})
    finally:
        await manager.shutdown()


async def test_usage_with_quota_adds_the_one_quota_shape_for_every_pool(tmp_path: Path) -> None:
    mcp, manager = _bridge(tmp_path)
    try:
        out = await _call(mcp, "usage", {"period": "today", "backend": "codex", "quota": True})
        quota = out["quota"]
        assert set(quota) == {"read_at", "pools", "order", "unread", "summary"}
        # Every pool once, whatever the filter, and no second copy of the readings anywhere.
        assert [p["pool"] for p in quota["pools"]] == ["codex", "agy_gemini", "agy_claude_gpt", "claude"]
        assert "quota_advisory" not in out and "upcoming_resets" not in json.dumps(out)
        # Each pool was asked for, though none can be read here: none is missing for want of a reading.
        reasons = {u["pool"]: u["reason"] for u in quota["unread"]}
        assert set(reasons) == {"codex", "agy_gemini", "agy_claude_gpt", "claude"}
        assert "no reading provided" not in reasons.values()
        assert reasons["claude"] == "no reading: no Claude job has reported one"
    finally:
        await manager.shutdown()


def test_the_usage_skill_asks_for_the_dashboard_view_and_says_when_the_bridge_is_out_of_date() -> None:
    skill = (Path(__file__).parents[1] / "skills" / "bridge-usage" / "SKILL.md").read_text(encoding="utf-8")
    assert 'Call the `usage` tool of the MCP server named `agent-bridge` with `view="dashboard"`' in skill
    assert "`text` field" in skill
    assert "neither a `usage` tool nor a `usage_dashboard` tool" in skill and "out of date" in skill


async def test_backends_reports_the_clis_and_no_quota_or_window_totals(tmp_path: Path) -> None:
    mcp, manager = _bridge(tmp_path)
    try:
        out = await _call(mcp, "backends", {})
        assert set(out) == {
            "default_cwd", "default_timeout_seconds", "max_concurrent_jobs_per_process", "db_path", "nesting",
            "backends", "host", "wait_cap",
        }
        assert out["backends"]["codex"]["healthy"] is True and "0.99.0" in out["backends"]["codex"]["version"]
    finally:
        await manager.shutdown()
