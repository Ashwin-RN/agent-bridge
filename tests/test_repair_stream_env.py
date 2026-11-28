"""Regression coverage for stable process identity, incomplete streams and child environments."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_bridge.config import BackendName, Settings
from agent_bridge.models import RunRequest
from agent_bridge.runners import CLI_OUTPUT_CUT_SHORT_ERROR, CodexRunner, NeonRunner, job_env


BOOT_ID = "e7eeead9-616e-43b2-b6f7-88aa20288b36"


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_BRIDGE_DEFAULT_CWD", str(tmp_path))
    for cli in ("AGY", "CODEX", "CLAUDE"):
        monkeypatch.setenv(f"AGENT_BRIDGE_{cli}_BIN", str(tmp_path / f"missing-{cli}"))
    monkeypatch.setenv("AGENT_BRIDGE_CHILD_ENV", "trim")


@pytest.fixture
def stand_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_config: None,
) -> Callable[[BackendName, str], CodexRunner | NeonRunner]:
    def make_runner(backend: BackendName, stdout: str) -> CodexRunner | NeonRunner:
        script = tmp_path / "fake_cli.py"
        script.write_text(
            "import sys\n"
            "if sys.argv[1:2] == ['models']:\n"
            "    print('gemini-4-neon-high\\tGemini 4 Neon (High)')\n"
            "else:\n"
            f"    sys.stdout.write({stdout!r})\n",
            encoding="utf-8",
        )
        settings = Settings.from_env()
        runner = CodexRunner(settings) if backend == "codex" else NeonRunner(settings)
        monkeypatch.setattr(runner, "resolve_command", lambda: [sys.executable, str(script)])
        return runner

    return make_runner


@pytest.mark.parametrize(
    ("backend", "event"), [("codex", "turn.completed"), ("neon", "a result event with a status")],
)
async def test_incomplete_stream_keeps_plain_text_reply(tmp_path: Path, stand_in, backend: BackendName, event: str) -> None:
    reply = "a reply without a terminal event\n\n    indented text"
    runner = stand_in(backend, reply + "\n")
    result = await runner.run(RunRequest(backend=backend, cwd=str(tmp_path), prompt="go"))
    assert result.status == "failed"
    assert result.error == CLI_OUTPUT_CUT_SHORT_ERROR.format(event=event)
    assert result.response == reply


async def test_agy_ignores_unrelated_status_json(tmp_path: Path, stand_in) -> None:
    runner = stand_in("neon", '{"status":"OK"}\n')
    result = await runner.run(RunRequest(backend="neon", cwd=str(tmp_path), prompt="go"))
    assert result.status == "failed"
    assert result.error == CLI_OUTPUT_CUT_SHORT_ERROR.format(event="a result event with a status")


@pytest.mark.parametrize("stdout", ["", '{"type":"thread.started","thread_id":"review-thread"}\n'])
async def test_codex_review_without_terminal_event_requires_a_reply(tmp_path: Path, stand_in, stdout: str) -> None:
    runner = stand_in("codex", stdout)
    result = await runner.run(RunRequest(backend="codex", cwd=str(tmp_path), prompt="", review_scope="uncommitted"))
    assert result.status == "failed"
    assert result.error == CLI_OUTPUT_CUT_SHORT_ERROR.format(event="turn.completed")


def test_child_env_keep_uses_exact_names_from_settings_and_logs_only_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, isolated_config: None,
) -> None:
    monkeypatch.setenv("AGENT_BRIDGE_CHILD_ENV_KEEP", " OPENAI_API_KEY , NPM_TOKEN , GIT_SSL_* ,,")
    values = {
        "OPENAI_API_KEY": "fake-openai-keep-value",
        "NPM_TOKEN": "fake-npm-keep-value",
        "GIT_SSL_KEY": "fake-git-drop-value",
        "GEMINI_API_KEY": "fake-gemini-drop-value",
        "HTTPS_PROXY": "https://fake-user:fake-proxy-value@example.test",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    monkeypatch.setenv("AGENT_BRIDGE_CHILD_ENV_KEEP", "GEMINI_API_KEY")
    request = RunRequest(backend="codex", cwd=str(tmp_path), prompt="go")
    with caplog.at_level(logging.INFO, logger="agent_bridge.runners"):
        env = job_env(settings, request, cli="codex")
    for name in ("OPENAI_API_KEY", "NPM_TOKEN", "HTTPS_PROXY"):
        assert env[name] == values[name]
    assert "GIT_SSL_KEY" not in env and "GEMINI_API_KEY" not in env
    kept = next(message for message in caplog.messages if message.startswith("Kept in"))
    dropped = next(message for message in caplog.messages if message.startswith("Left out of"))
    assert "OPENAI_API_KEY" in kept and "NPM_TOKEN" in kept
    assert "GIT_SSL_KEY" in dropped and "GEMINI_API_KEY" in dropped
    assert all(value not in caplog.text for value in values.values())


def test_empty_child_env_keep_leaves_no_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_config: None,
) -> None:
    monkeypatch.setenv("AGENT_BRIDGE_CHILD_ENV_KEEP", "")
    monkeypatch.setenv("CODEX_API_KEY", "fake-codex-drop-value")
    settings = Settings.from_env()
    request = RunRequest(backend="codex", cwd=str(tmp_path), prompt="go")
    assert settings.child_env_keep == frozenset()
    assert "CODEX_API_KEY" not in job_env(settings, request, cli="codex")
