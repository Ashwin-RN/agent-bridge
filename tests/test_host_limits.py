"""Runtime host limits with fixture configs and simulated process trees."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from agent_bridge import host_limits, install_cmd, nesting
from agent_bridge.hosts import UNVERIFIED, Roots, get_host

REAL_HOME = Path.home()


@pytest.fixture(autouse=True)
def protect_real_home(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    base = tmp_path_factory.getbasetemp().resolve()

    def guard(path: Path) -> None:
        path = Path(os.path.abspath(path))
        if path.is_relative_to(REAL_HOME) and not path.is_relative_to(base):
            raise AssertionError(f"test attempted to access the real home: {path}")

    monkeypatch.setattr(install_cmd, "AUDIT", [guard])
    host_limits.clear_limit_cache()
    yield
    host_limits.clear_limit_cache()


@pytest.fixture
def roots(tmp_path: Path) -> Roots:
    return Roots.for_home(tmp_path / "home")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def codex_config(roots: Roots, setting: str = "", *, project: bool = False) -> None:
    folder = roots.project / ".codex" if project else roots.codex_home
    write(folder / "config.toml", '[mcp_servers.agent-bridge]\ncommand = "fixture"\n' + setting)


@pytest.mark.parametrize(
    ("client", "expected"),
    [("claude-code", "claude-code"), ("gemini-cli-mcp-client", "gemini-cli"), ("opencode", "opencode")],
)
def test_client(client: str, expected: str) -> None:
    # Red when ancestor detection takes precedence over the client name.
    assert host_limits.identify_host(client, ("codex",)) is get_host(expected)


def test_ancestor(monkeypatch: pytest.MonkeyPatch) -> None:
    # Red when the ancestor fallback is removed.
    monkeypatch.setattr(host_limits, "ancestor_process_names", lambda: ("node", "agy", "codex"))
    assert host_limits.identify_host("unknown-client") is get_host("antigravity")


def test_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Red when an explicitly empty ancestor list triggers a real ancestry lookup.
    monkeypatch.setattr(host_limits, "ancestor_process_names", lambda: ("codex",))
    assert host_limits.identify_host("unknown-client", ()) is None
    assert host_limits.identify_host(None, ("node", "python")) is None


def test_ancestor_names(monkeypatch: pytest.MonkeyPatch) -> None:
    # Red when executable normalization is removed.
    command_lines = {7: [r"C:\Tools\AgY.EXE"], 6: None, 5: ["/usr/bin/Codex", "--version"]}
    monkeypatch.setattr(nesting, "ancestor_chain", lambda: [(pid, None) for pid in command_lines])
    monkeypatch.setattr(nesting, "_process_command_line", command_lines.__getitem__)
    assert host_limits.ancestor_process_names() == ("agy", "codex")


def test_unreadable_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    # Red when an ancestry reader failure is propagated.
    def unreadable() -> None:
        raise PermissionError("process tree is unavailable")

    monkeypatch.setattr(nesting, "ancestor_chain", unreadable)
    assert host_limits.ancestor_process_names() == ()


@pytest.mark.parametrize(
    ("setting", "expected"), [("tool_timeout_sec = 247.5\n", 247.5), ("", 60)], ids=["set", "unset"],
)
def test_codex(roots: Roots, setting: str, expected: float) -> None:
    # Red for a configured override when parsing is skipped, and for an unset key when fallback is removed.
    codex_config(roots, setting)
    assert host_limits.tool_call_limit_seconds(get_host("codex"), roots) == expected


@pytest.mark.parametrize(("timeout", "expected"), [(425, 425), (None, 180)], ids=["set", "unset"])
def test_antigravity(roots: Roots, timeout: int | None, expected: float) -> None:
    # Red for a configured override when parsing is skipped, and for an unset key when fallback is removed.
    entry: dict[str, str | int] = {"command": "fixture"}
    if timeout is not None:
        entry["timeoutSeconds"] = timeout
    write(roots.home / ".gemini/config/mcp_config.json", json.dumps({"mcpServers": {"agent-bridge": entry}}))
    assert host_limits.tool_call_limit_seconds(get_host("antigravity"), roots) == expected


def test_project_config(roots: Roots) -> None:
    # Red when project lookup is removed or when it takes precedence over a user registration.
    host = get_host("codex")
    codex_config(roots, "tool_timeout_sec = 200\n", project=True)
    assert host_limits.tool_call_limit_seconds(host, roots) == 200
    codex_config(roots, "tool_timeout_sec = 300\n")
    host_limits.clear_limit_cache()
    assert host_limits.tool_call_limit_seconds(host, roots) == 300


@pytest.mark.parametrize("kind", ["malformed", "unreadable", "missing", "nonfinite"])
def test_bad_config(roots: Roots, kind: str) -> None:
    # Red when parse/I/O failures propagate, a missing file loses its fallback, or infinity is accepted.
    path = roots.codex_home / "config.toml"
    if kind == "malformed":
        write(path, "[not valid TOML")
    elif kind == "unreadable":
        path.mkdir(parents=True)
    elif kind == "nonfinite":
        codex_config(roots, "tool_timeout_sec = inf\n")
    assert host_limits.tool_call_limit_seconds(get_host("codex"), roots) == 60


def test_unverified(roots: Roots) -> None:
    # Red when an unverified timeout override is accepted.
    codex_config(roots, "tool_timeout_sec = 300\n")
    codex = get_host("codex")
    host = replace(codex, timeout=replace(codex.timeout, source=UNVERIFIED))
    assert host_limits.tool_call_limit_seconds(host, roots) == 60


def test_claude_uncapped(roots: Roots) -> None:
    # Red when Claude's table default is used as a finite limit.
    host = get_host("claude-code")
    assert host_limits.tool_call_limit_seconds(host, roots) is None
    assert host_limits.wait_cap_seconds(host, roots) is None
    assert host_limits.cap_wait(120, host, roots) == (120, None)


@pytest.mark.parametrize(("limit", "cap"), [(60.9, 50), (12, 5)], ids=["margin", "floor"])
def test_cap(roots: Roots, limit: float, cap: int) -> None:
    # Red when the margin or the minimum is removed.
    codex_config(roots, f"tool_timeout_sec = {limit}\n")
    assert host_limits.wait_cap_seconds(get_host("codex"), roots) == cap


def test_wait_under(roots: Roots) -> None:
    # Red when the cap is also used for shorter waits.
    assert host_limits.cap_wait(25, get_host("codex"), roots) == (25, None)


def test_wait_over(roots: Roots) -> None:
    # Red when a longer wait is left unchanged or the install guidance is missing.
    assert host_limits.cap_wait(120, get_host("codex"), roots) == (
        50,
        "Codex stops a tool call after 60 s on this machine, so this call waited 50 s and the job is still running"
        " (run `agent-bridge install --host codex` to raise the limit).",
    )


def test_wait_unknown(roots: Roots) -> None:
    # Red when an unknown host receives a limit.
    assert host_limits.wait_cap_seconds(None, roots) is None
    assert host_limits.cap_wait(120, None, roots) == (120, None)


def test_fixed_hint(roots: Roots) -> None:
    # Red when the message omits that Cursor's timeout is fixed.
    wait, note = host_limits.cap_wait(120, get_host("cursor"), roots)
    assert wait == 50
    assert note is not None and "this limit is fixed" in note


def test_manual_hint(roots: Roots) -> None:
    # Red when install is recommended for a value it cannot raise further.
    codex_config(roots, "tool_timeout_sec = 600\n")
    wait, note = host_limits.cap_wait(650, get_host("codex"), roots)
    assert wait == 590
    assert note is not None and "increase `tool_timeout_sec`" in note


def test_cache(roots: Roots, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Red when fresh cache entries are bypassed, shared across roots, never expire, or cannot be cleared.
    now = 100.0
    monkeypatch.setattr(host_limits, "monotonic", lambda: now)
    reads: list[Path] = []
    install_cmd.AUDIT.append(reads.append)
    path = roots.codex_home / "config.toml"
    host = get_host("codex")
    assert host_limits.tool_call_limit_seconds(host, roots) == 60
    codex_config(roots, "tool_timeout_sec = 200\n")
    now += 59
    assert host_limits.tool_call_limit_seconds(host, roots) == 60
    assert reads.count(path) == 1

    other_roots = Roots.for_home(tmp_path / "other-home")
    codex_config(other_roots, "tool_timeout_sec = 300\n")
    assert host_limits.tool_call_limit_seconds(host, other_roots) == 300

    now += 1
    assert host_limits.tool_call_limit_seconds(host, roots) == 200
    codex_config(roots, "tool_timeout_sec = 400\n")
    assert host_limits.tool_call_limit_seconds(host, roots) == 200
    host_limits.clear_limit_cache()
    assert host_limits.tool_call_limit_seconds(host, roots) == 400
    assert reads.count(path) == 3


def test_home_guard() -> None:
    # Red when the guard allows a real-home path. The audit probe performs no file read.
    with pytest.raises(AssertionError, match="real home"):
        install_cmd._audit(REAL_HOME / ".codex/config.toml")
