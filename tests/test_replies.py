"""Replies a host cannot misread: the line first, nothing about the window, the reply text in pages."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_bridge.models import JobRecord
from agent_bridge.storage import BridgeStore

NOW = 1_800_000_000.0


def _job(**overrides: Any) -> JobRecord:
    base: dict[str, Any] = dict(
        job_id="codex-1a2b3c4d",
        owner_id="pid-123-abcdef",
        backend="codex",
        cwd="C:/work/project",
        effective_cwd="C:/work/project",
        mode="read_only",
        requested_model="gpt-6.2-sol",
        model="gpt-6.2-sol",
        model_source="requested",
        effort="high",
        quota_pool="codex",
        label="review auth",
        prompt_preview="review the auth module",
        session_id="019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
        status="completed",
        created_at=NOW - 75,
        started_at=NOW - 74,
        finished_at=NOW - 2,
        duration_seconds=72.0,
        response="x" * 1000,
        response_chars=1000,
        usage={"input_tokens": 3_680_000, "output_tokens": 12_345, "cached_tokens": 3_420_000,
               "reasoning_tokens": 4_100, "total_tokens": 3_692_345},
        usage_status="exact",
        provider_metrics={"provider": "codex", "input_tokens": 3_680_000},
        last_activity="completed",
        last_activity_at=NOW - 2,
    )
    base.update(overrides)
    return JobRecord(**base)


def test_a_job_reply_opens_with_its_line_and_leaves_out_what_is_not_the_job_s() -> None:
    reply = _job().to_detail(now=NOW)
    assert list(reply)[:2] == ["line", "job_id"]
    assert reply["line"] == "codex · requested gpt-6.2-sol · high · completed 1m12s · review auth · codex"
    assert list(reply) == [
        "line", "job_id", "status", "backend", "model", "model_source", "effort", "quota_pool", "mode",
        "session_id", "response", "usage",
    ]
    assert reply["usage"] == {"total_tokens": 3_692_345}
    for absent in ("owner_id", "session_usage", "effective_cwd", "provider_metrics", "attempts", "error"):
        assert absent not in reply
    assert None not in reply.values()

    # Where the job ran when it is not where it was sent; the CLI's full count and metrics on request.
    moved = _job(effective_cwd="C:/work/project-wt", attempts=[{"attempt": 1}, {"attempt": 2}]).to_detail(
        now=NOW, verbose=True,
    )
    assert moved["effective_cwd"] == "C:/work/project-wt"
    assert moved["usage"]["cached_tokens"] == 3_420_000
    assert moved["provider_metrics"]["provider"] == "codex" and len(moved["attempts"]) == 2


def test_failure_fields_come_only_with_a_job_that_did_not_complete() -> None:
    failed = _job(status="failed", failure_reason="cli_error", error="boom", retry_guidance="look",
                  provider_state="stopped", exit_code=2, stderr_tail="trace", usage_status="partial")
    reply = failed.to_detail(now=NOW)
    for key, value in {"failure_reason": "cli_error", "error": "boom", "retry_guidance": "look",
                       "provider_state": "stopped", "exit_code": 2, "stderr_tail": "trace",
                       "usage_status": "partial"}.items():
        assert reply[key] == value
    # A completed job keeps an `error` it may carry to itself.
    assert "error" not in _job(error="a warning from the CLI").to_detail(now=NOW)


def test_a_long_reply_pages_to_its_end_and_each_cut_says_how_to_go_on() -> None:
    text = "".join(f"line {i:05d}\n" for i in range(5000))  # 55,000 characters
    job = _job(response=text, response_chars=len(text))
    pieces, offset, pages = [], 0, 0
    while True:
        page = job.to_detail(response_offset=offset, response_chars=20_000, now=NOW)
        pieces.append(page["response"])
        pages += 1
        if "next_offset" not in page:
            assert "response_truncated" not in page
            break
        assert page["response_truncated"] is True and page["response_total_chars"] == len(text)
        assert f"response_offset={page['next_offset']}" in page["response_note"]
        assert "jobs(job_ids=['codex-1a2b3c4d']" in page["response_note"]
        offset = page["next_offset"]
    assert "".join(pieces) == text and pages == 3
    assert page["response_offset"] == 40_000

    # The default is 20,000, and no page is longer than 100,000 whatever is asked for.
    assert len(job.to_detail(now=NOW)["response"]) == 20_000
    big = _job(response="z" * 150_000, response_chars=150_000).to_detail(response_chars=10**9, now=NOW)
    assert len(big["response"]) == 100_000 and big["next_offset"] == 100_000


def test_a_reply_cut_when_it_was_stored_says_the_rest_was_not_kept(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "state")
    store.register_owner("pid-123-abcdef", 123)
    full = "y" * 250_000
    running = _job(status="running", response="", response_chars=None, finished_at=None, duration_seconds=None)
    store.insert_job(running)
    store.finalize_job(replace(running, status="completed", response=full, response_chars=len(full),
                               finished_at=NOW, duration_seconds=1.0))
    stored = store.peek_job("codex-1a2b3c4d")
    assert stored is not None and len(stored.response) == 200_000 and stored.response_chars == 250_000

    last = stored.to_detail(response_offset=180_000, response_chars=100_000, now=NOW)
    assert len(last["response"]) == 20_000 and "next_offset" not in last
    assert last["response_truncated"] is True and last["response_total_chars"] == 250_000
    assert "kept only the first 200000 of 250000" in last["response_note"]
    assert "not kept" in last["response_note"]


def test_a_job_that_completed_with_an_empty_reply_carries_a_warning() -> None:
    assert "empty reply" in _job(response="  \n", response_chars=3).to_detail(now=NOW)["warning"]
    assert "warning" not in _job().to_detail(now=NOW)
    assert "warning" not in _job(status="failed", response="").to_detail(now=NOW)


def test_a_background_receipt_is_small() -> None:
    receipt = _job(status="queued", started_at=None, finished_at=None, duration_seconds=None,
                   last_activity="queued", response="", usage=None).to_receipt(
        timeout_seconds=300, next_call="jobs(job_ids=['codex-1a2b3c4d'], wait_seconds=50)", now=NOW,
    )
    assert receipt == {
        "line": "codex · requested gpt-6.2-sol · high · queued 1m15s · queued · review auth · codex",
        "job_id": "codex-1a2b3c4d",
        "status": "queued",
        "backend": "codex",
        "model": "gpt-6.2-sol",
        "timeout_seconds": 300,
        "next": "jobs(job_ids=['codex-1a2b3c4d'], wait_seconds=50)",
    }
    # A model the bridge only knows as the CLI's default is left out, not named "codex-default".
    plain = _job(requested_model=None, model="codex-default", model_source="default").to_receipt(
        timeout_seconds=300, next_call="", now=NOW,
    )
    assert "model" not in plain and "Codex's default model" in plain["line"]
