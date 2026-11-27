"""The command line: no argument must keep meaning "start the server"."""

from __future__ import annotations

import pytest

from agent_bridge import cli


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr("agent_bridge.server.main", lambda: calls.append("serve"))
    return calls


def test_no_argument_starts_the_server(served: list[str]) -> None:
    assert cli.main([]) == 0
    assert served == ["serve"]


def test_serve_starts_the_server(served: list[str]) -> None:
    assert cli.main(["serve"]) == 0
    assert served == ["serve"]


def test_unknown_command_is_refused_and_starts_nothing(
    served: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["bogus"]) == 2
    captured = capsys.readouterr()
    assert served == []
    assert captured.out == ""
    assert "unknown command 'bogus'" in captured.err


def test_help_prints_usage_and_starts_nothing(
    served: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--help"]) == 0
    assert served == []
    assert "usage: agent-bridge" in capsys.readouterr().out


def test_status_command_is_dispatched_with_its_arguments(
    served: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("agent_bridge.status_cmd.run", lambda argv: seen.append(argv) or 0)
    assert cli.main(["status", "--line"]) == 0
    assert seen == [["--line"]]
    assert served == []
