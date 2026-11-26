"""Shared test setup."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_bridge import nesting
from agent_bridge.nesting import ENV_PARENT_JOB, ENV_PARENT_OWNER


@pytest.fixture(scope="session")
def _external_bridge_pids() -> set[int]:
    return {
        pid for pid, _ in nesting.ancestor_chain() or ()
        if nesting._is_bridge_command(nesting._process_command_line(pid) or [])
    }


@pytest.fixture(autouse=True)
def _not_inside_a_bridge_job(monkeypatch: pytest.MonkeyPatch, _external_bridge_pids: set[int]) -> None:
    """Run every test as if no bridge had started it.

    The suite is often run by a subagent that a bridge started, which carries the nesting marker.
    Without this, every bridge a test starts would take itself for a nested one and refuse jobs.
    Tests of the guard set the marker themselves.
    """
    monkeypatch.delenv(ENV_PARENT_JOB, raising=False)
    monkeypatch.delenv(ENV_PARENT_OWNER, raising=False)
    if _external_bridge_pids:
        command_line = nesting._process_command_line
        monkeypatch.setattr(
            nesting, "_process_command_line", lambda pid: [] if pid in _external_bridge_pids else command_line(pid),
        )


@pytest.fixture
def plain_host_env(tmp_path: Path, _external_bridge_pids: set[int]) -> dict[str, str]:
    """For stdio tests that need a plain host, mask only bridges above the test process."""
    env = dict(os.environ)
    if _external_bridge_pids:
        (tmp_path / "sitecustomize.py").write_text(
            "from agent_bridge import nesting\n"
            "command_line = nesting._process_command_line\n"
            f"external_pids = {_external_bridge_pids!r}\n"
            "nesting._process_command_line = lambda pid: [] if pid in external_pids else command_line(pid)\n",
            encoding="utf-8",
        )
        search_path = [str(tmp_path)]
        if env.get("PYTHONPATH"):
            search_path.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(search_path)
    return env
