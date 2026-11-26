"""Shared test setup."""

from __future__ import annotations

import pytest

from agent_bridge.nesting import ENV_PARENT_JOB, ENV_PARENT_OWNER


@pytest.fixture(autouse=True)
def _not_inside_a_bridge_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test as if no bridge had started it.

    The suite is often run by a subagent that a bridge started, which carries the nesting marker.
    Without this, every bridge a test starts would take itself for a nested one and refuse jobs.
    Tests of the guard set the marker themselves.
    """
    monkeypatch.delenv(ENV_PARENT_JOB, raising=False)
    monkeypatch.delenv(ENV_PARENT_OWNER, raising=False)
