"""`agent-bridge install` and `doctor`, on fixture files under tmp_path only.

The fixtures copy the shape of real host files (Codex's config.toml, Antigravity's
mcp_config.json and settings.json, Claude Code's settings.json and .claude.json) with every
personal path and secret replaced. An audit hook fails any test that touches a path under the
real home folder.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_bridge import hosts, install_cmd
from agent_bridge.hosts import Roots, detect_installed, detect_running_host, get_host

REAL_HOME = Path(os.path.expanduser("~")).resolve()


@pytest.fixture(autouse=True)
def _never_the_real_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory, tmp_path: Path
) -> None:
    """Fail on any file access under the real home that is not under pytest's temp folder."""
    base = tmp_path_factory.getbasetemp().resolve()

    def guard(path: Path) -> None:
        resolved = Path(os.path.abspath(path))
        if resolved.is_relative_to(REAL_HOME) and not resolved.is_relative_to(base):
            raise AssertionError(f"test touched a path under the real home: {path}")

    monkeypatch.setattr(install_cmd, "AUDIT", [guard])
    poison = tmp_path / "poison-home"
    for var in ("HOME", "USERPROFILE", "APPDATA", "XDG_CONFIG_HOME", "CODEX_HOME", "AGENT_BRIDGE_STATE_DIR"):
        monkeypatch.setenv(var, str(poison))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: poison))

    def no_real_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"a real host command was started: {argv}")

    monkeypatch.setattr(install_cmd, "_version_runner", no_real_runner)


def test_guard_refuses_a_path_under_the_real_home() -> None:
    with pytest.raises(AssertionError, match="real home"):
        install_cmd._exists(REAL_HOME / ".claude.json")


# ------------------------------------------------------------------ fixtures with real shapes

CODEX_TOML = """\
model = "gpt-6.2-sol"
model_reasoning_effort = "xhigh"
[projects.'c:\\users\\me\\alpha']
trust_level = "trusted"

[projects.'c:\\users\\me\\beta']
trust_level = "trusted"

[windows]
sandbox = "elevated"

[tui]
screen_reader_detection_done = true

# a comment the user wrote
[tui.model_availability_nux]
"gpt-5.5" = 3
gpt-6-astra = 4

[mcp_servers.openaiDeveloperDocs]
url = "https://developers.openai.com/mcp"
"""

AGY_MCP = (
    '{\r\n  "mcpServers": {\r\n    "agent-bridge": {\r\n      "args": [\r\n        "-m",\r\n'
    '        "agent_bridge"\r\n      ],\r\n      "command": "C:\\\\old\\\\python.exe",\r\n'
    '      "disabled": false,\r\n      "timeoutSeconds": 600\r\n    },\r\n    "other": {\r\n'
    '      "command": "npx",\r\n      "args": ["-y", "x"]\r\n    }\r\n  }\r\n}\r\n'
)

AGY_SETTINGS = {
    "model": "neon-high",
    "permissions": {"allow": ['command(python "C:\\\\scratch\\\\inventory.py")', "command(git status)"]},
    "trustedWorkspaces": ["C:/work/a", "C:/work/b"],
}

CLAUDE_SETTINGS = {
    "env": {"CLAUDE_CODE_AUTO_CONNECT_IDE": "false"},
    "model": "opus",
    "statusLine": {
        "type": "command",
        "command": 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:/home/.claude/statusline.ps1"',
    },
    "enabledPlugins": {"example@market": True},
    "theme": "dark",
}

CLAUDE_JSON = {
    "numStartups": 12,
    "projects": {"C:/work/a": {"allowedTools": [], "mcpServers": {"x": {"type": "http", "url": "https://x"}}}},
    "mcpServers": {"spotter": {"type": "http", "url": "https://example.invalid/mcp"}},
    "userID": "0000",
}


def _write(path: Path, data: str | bytes | dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, dict):
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    elif isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_bytes(data.encode("utf-8"))
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def python(tmp_path: Path) -> Path:
    exe = tmp_path / "venv" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    return exe


class Run:
    def __init__(self, home: Path, python: Path) -> None:
        self.home = home
        self.python = python
        self.lines: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def install(self, *args: str, answers: Sequence[str] = (), runner=None, path_lookup=lambda name: None, env=None) -> int:
        self.lines.clear()
        queue = list(answers)

        def ask(prompt: str) -> str:
            self.lines.append(prompt)
            if not queue:
                raise EOFError
            return queue.pop(0)

        return install_cmd.run_install(
            ["--home", str(self.home), "--command", f"{self.python} -m agent_bridge", *args],
            env=env or {},
            path_lookup=path_lookup,
            runner=runner,
            ask=ask,
            out=self.lines.append,
        )


@pytest.fixture
def run(home: Path, python: Path) -> Run:
    return Run(home, python)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# ------------------------------------------------------------------ JSON merges


def test_claude_json_merge_keeps_other_servers_and_keys(run: Run, home: Path, python: Path) -> None:
    _write(home / ".claude.json", CLAUDE_JSON)
    assert run.install("--host", "claude-code", "--yes", "--no-skill", "--no-status-line") == 0
    data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["spotter"] == CLAUDE_JSON["mcpServers"]["spotter"]
    assert data["projects"] == CLAUDE_JSON["projects"]
    assert data["numStartups"] == 12 and data["userID"] == "0000"
    assert data["mcpServers"]["agent-bridge"] == {
        "type": "stdio", "command": str(python), "args": ["-m", "agent_bridge"], "env": {},
    }


def test_json_install_redacts_secrets_from_preview_and_print(run: Run, home: Path) -> None:
    other = {
        "env": {"OPENAI_API_KEY": "sk-test-SECRET", "CUSTOM": "private-env"},
        "headers": {"Authorization": "Bearer abc.SECRET", "X-Custom": "private-header"},
        "clientSecret": "private-client",
        "url": "https://login:private-url@example.test/mcp",
        "args": ["Bearer private-arg"],
    }
    original = json.dumps({"mcpServers": {"other": other}})
    path = _write(home / ".claude.json", original)
    args = ("--host", "claude-code", "--no-skill", "--no-status-line")
    secrets = (
        "sk-test-SECRET", "abc.SECRET", "private-env", "private-header", "private-client",
        "login:private-url", "private-arg",
    )
    for flags, answers in [((), ["n"]), (("--print", "--yes"), [])]:
        assert run.install(*args, *flags, answers=answers) == 0
        assert all(secret not in run.text for secret in secrets), "preview exposed a JSON credential"
        assert '\n+    "agent-bridge": {\n' in run.text
        assert path.read_bytes() == original.encode()
    assert run.install(*args, answers=["yes"]) == 0
    assert all(secret not in run.text for secret in secrets), "confirmed install exposed a JSON credential"
    assert json.loads(path.read_bytes())["mcpServers"]["other"] == other


def test_antigravity_merge_keeps_crlf_unknown_keys_and_other_rules(run: Run, home: Path, python: Path) -> None:
    mcp = _write(home / ".gemini" / "config" / "mcp_config.json", AGY_MCP)
    settings = _write(home / ".gemini" / "antigravity-cli" / "settings.json", AGY_SETTINGS)
    assert run.install("--host", "antigravity", "--yes", "--no-skill", "--no-status-line") == 0
    raw = mcp.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n"), "CRLF line endings must survive"
    entry = json.loads(raw)["mcpServers"]["agent-bridge"]
    assert entry == {"args": ["-m", "agent_bridge"], "command": str(python), "disabled": False, "timeoutSeconds": 600}
    assert json.loads(raw)["mcpServers"]["other"] == {"command": "npx", "args": ["-y", "x"]}
    new_settings = json.loads(settings.read_text(encoding="utf-8"))
    assert new_settings["permissions"]["allow"] == AGY_SETTINGS["permissions"]["allow"] + ["mcp(agent-bridge/*)"]
    assert new_settings["trustedWorkspaces"] == AGY_SETTINGS["trustedWorkspaces"]
    assert "re-indents lines it does not change" in run.text  # the fixture's one-line array


def test_vscode_project_file_uses_servers_and_opencode_uses_one_command_array(
    run: Run, home: Path, python: Path, tmp_path: Path
) -> None:
    project = tmp_path / "repo"
    _write(project / ".vscode" / "mcp.json", {"servers": {"github": {"type": "http", "url": "https://x"}}, "inputs": []})
    assert run.install("--host", "vscode-copilot", "--scope", "project", "--project", str(project), "--yes", "--no-skill") == 0
    vscode = json.loads((project / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    assert vscode["servers"]["github"] == {"type": "http", "url": "https://x"}
    assert vscode["servers"]["agent-bridge"] == {"type": "stdio", "command": str(python), "args": ["-m", "agent_bridge"]}
    assert "mcpServers" not in vscode

    assert run.install("--host", "opencode", "--yes", "--no-skill") == 0
    opencode = json.loads((home / ".config" / "opencode" / "opencode.json").read_text(encoding="utf-8"))
    assert opencode["mcp"]["servers"]["agent-bridge"] == {
        "type": "local", "command": [str(python), "-m", "agent_bridge"],
    }
    assert install_cmd.DOCS_WARNING in run.text


def test_opencode_merge_keeps_other_servers_and_keys(run: Run, home: Path, python: Path) -> None:
    path = _write(home / ".config" / "opencode" / "opencode.json", {
        "theme": "dark",
        "mcp": {"timeout": {"startup": 30000}, "servers": {"context7": {"type": "remote", "url": "https://x"}}},
    })
    assert run.install("--host", "opencode", "--yes", "--no-skill") == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["mcp"]["timeout"] == {"startup": 30000}
    assert data["mcp"]["servers"]["context7"] == {"type": "remote", "url": "https://x"}
    assert data["mcp"]["servers"]["agent-bridge"]["command"] == [str(python), "-m", "agent_bridge"]


def test_zed_project_merge_keeps_other_context_servers_and_keys(run: Run, python: Path, tmp_path: Path) -> None:
    project = tmp_path / "repo"
    path = _write(project / ".zed" / "settings.json", {
        "tab_size": 2,
        "context_servers": {"postgres": {"command": "pg-mcp", "args": [], "env": {"PGHOST": "localhost"}}},
    })
    assert run.install("--host", "zed", "--scope", "project", "--project", str(project), "--yes") == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tab_size"] == 2
    assert data["context_servers"]["postgres"] == {"command": "pg-mcp", "args": [], "env": {"PGHOST": "localhost"}}
    assert data["context_servers"]["agent-bridge"] == {"command": str(python), "args": ["-m", "agent_bridge"], "env": {}}


def test_project_scope_edits_the_project_file_and_never_runs_the_hosts_command(
    run: Run, python: Path, tmp_path: Path
) -> None:
    def must_not_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"ran {argv}: a project-scope mcp add writes into the current folder")

    project = tmp_path / "repo"
    lookup = {"claude": "C:/tools/claude.exe"}.get
    assert run.install(
        "--host", "claude-code", "--scope", "project", "--project", str(project), "--yes", "--no-skill",
        runner=must_not_run, path_lookup=lookup,
    ) == 0
    entry = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["agent-bridge"]
    assert entry["command"] == str(python)


# ------------------------------------------------------------------ TOML


def test_toml_install_redacts_credentials_in_context(run: Run, home: Path) -> None:
    original = (
        '[mcp_servers.agent-bridge]\ncommand = "old"\n'
        'headers = { Custom = "private-header" }\n'
        '"api\\u005fkey" = """\nprivate-multiline\n"""\n'
        'url = "https://login:private-url@example.test"\n'
        'args = ["Bearer private-arg"]\n'
        '[mcp_servers.agent-bridge."env"]\nCustom = "private-env"\n'
    )
    path = _write(home / ".codex" / "config.toml", original)
    assert run.install("--host", "codex", "--print", "--no-skill") == 0
    secrets = ("private-header", "private-multiline", "login:private-url", "private-arg", "private-env")
    assert all(secret not in run.text for secret in secrets), "preview exposed a TOML credential"
    assert path.read_bytes() == original.encode()
    assert run.install("--host", "codex", "--yes", "--no-skill") == 0
    assert all(secret in path.read_text(encoding="utf-8") for secret in secrets if secret != "private-arg")


def test_codex_toml_gains_the_table_and_keeps_every_other_line(run: Run, home: Path, python: Path) -> None:
    config = _write(home / ".codex" / "config.toml", CODEX_TOML)
    assert run.install("--host", "codex", "--yes", "--no-skill") == 0
    text = config.read_text(encoding="utf-8")
    assert text.startswith(CODEX_TOML.rstrip("\n")), "every original line, comments included, stays as it was"
    data = tomllib.loads(text)
    assert data["mcp_servers"]["agent-bridge"] == {
        "command": str(python),
        "args": ["-m", "agent_bridge"],
        "tool_timeout_sec": 600,
        "default_tools_approval_mode": "approve",
    }
    old = tomllib.loads(CODEX_TOML)
    del data["mcp_servers"]["agent-bridge"]
    assert data == old


def test_codex_toml_edits_keys_inside_the_existing_table_and_keeps_its_subtable(run: Run, home: Path, python: Path) -> None:
    existing = CODEX_TOML + (
        "\n[mcp_servers.agent-bridge]\n"
        "command = 'C:\\old\\python.exe'\n"
        'args = ["-m", "agent_bridge"]\n'
        "tool_timeout_sec = 60  # too short\n"
        "\n[mcp_servers.agent-bridge.env]\n"
        'AGENT_BRIDGE_DEFAULT_TIMEOUT = "900"\n'
    )
    config = _write(home / ".codex" / "config.toml", existing)
    assert run.install("--host", "codex", "--yes", "--no-skill") == 0
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    table = data["mcp_servers"]["agent-bridge"]
    assert table["command"] == str(python)
    assert table["tool_timeout_sec"] == 600
    assert table["default_tools_approval_mode"] == "approve"
    assert table["env"] == {"AGENT_BRIDGE_DEFAULT_TIMEOUT": "900"}
    assert "tool_timeout_sec = 600  # too short" in config.read_text(encoding="utf-8"), "a trailing comment is kept"


def test_toml_edit_that_would_change_another_table_is_refused() -> None:
    text = (
        '[notes]\nbody = """\n[mcp_servers.agent-bridge]\nsee above\n"""\n\n'
        "[mcp_servers.agent-bridge]\ncommand = \"old\"\n"
    )
    with pytest.raises(install_cmd.Unsafe, match="another table"):
        install_cmd.plan_toml(text, ("mcp_servers", "agent-bridge"), {"command": "new", "tool_timeout_sec": 600})


def test_toml_that_cannot_be_edited_is_not_written_and_the_snippet_is_printed(run: Run, home: Path) -> None:
    # The entry as an inline table: valid TOML, but not a form the installer edits textually.
    original = CODEX_TOML.replace("[mcp_servers.openaiDeveloperDocs]", "[mcp_servers]\nagent-bridge = { command = \"old\" }\n\n[mcp_servers.openaiDeveloperDocs]")
    config = _write(home / ".codex" / "config.toml", original)
    assert run.install("--host", "codex", "--yes", "--no-skill") == 0
    assert config.read_text(encoding="utf-8") == original
    assert "paste by hand" in run.text and "[mcp_servers.agent-bridge]" in run.text


# ------------------------------------------------------------------ safety


def test_second_run_reports_already_set_and_changes_nothing(run: Run, home: Path) -> None:
    _write(home / ".codex" / "config.toml", CODEX_TOML)
    _write(home / ".gemini" / "config" / "mcp_config.json", AGY_MCP)
    _write(home / ".gemini" / "antigravity-cli" / "settings.json", AGY_SETTINGS)
    _write(home / ".claude" / "settings.json", CLAUDE_SETTINGS)
    _write(home / ".claude.json", CLAUDE_JSON)
    hosts_args = ["--host", "codex", "--host", "antigravity", "--host", "claude-code", "--yes"]
    assert run.install(*hosts_args) == 0
    first = _snapshot(home)
    assert run.install(*hosts_args) == 0
    assert _snapshot(home) == first
    assert "already set" in run.text
    assert "done:" not in run.text


def test_print_shows_diffs_and_writes_nothing(run: Run, home: Path) -> None:
    _write(home / ".codex" / "config.toml", CODEX_TOML)
    _write(home / ".gemini" / "config" / "mcp_config.json", AGY_MCP)
    _write(home / ".gemini" / "antigravity-cli" / "settings.json", AGY_SETTINGS)
    before = _snapshot(home)

    def must_not_run(argv):
        pytest.fail(f"--print started {argv}")

    assert run.install(
        "--host", "codex", "--host", "antigravity", "--print", "--yes",
        runner=must_not_run, path_lookup={"codex": "C:/tools/codex.exe", "agy": "C:/tools/agy.exe"}.get,
    ) == 0
    assert _snapshot(home) == before
    assert "+[mcp_servers.agent-bridge]" in run.text
    assert '+      "mcp(agent-bridge/*)"' in run.text


def test_a_backup_of_the_original_is_kept(run: Run, home: Path) -> None:
    config = _write(home / ".codex" / "config.toml", CODEX_TOML)
    assert run.install("--host", "codex", "--yes", "--no-skill") == 0
    backups = list(config.parent.glob("config.toml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == CODEX_TOML
    assert backups[0].name.endswith("Z")


def test_json_with_comments_is_not_written_and_the_snippet_is_printed(run: Run, home: Path) -> None:
    original = '// my servers\n{\n  "mcpServers": {}\n}\n'
    path = _write(home / ".cursor" / "mcp.json", original)
    assert run.install("--host", "cursor", "--yes", "--no-skill") == 0
    assert path.read_text(encoding="utf-8") == original
    assert not list(path.parent.glob("mcp.json.bak-*"))
    assert "paste by hand" in run.text
    assert '"agent-bridge"' in run.text


def test_an_answer_of_no_or_no_terminal_writes_nothing(run: Run, home: Path) -> None:
    _write(home / ".codex" / "config.toml", CODEX_TOML)
    before = _snapshot(home)
    assert run.install("--host", "codex", answers=["n"]) == 0
    assert _snapshot(home) == before
    assert run.install("--host", "codex") == 0  # no answers: EOF counts as no
    assert _snapshot(home) == before
    assert "declined" in run.text


def test_an_existing_status_line_is_kept_and_the_line_to_add_is_printed(run: Run, home: Path) -> None:
    settings = _write(home / ".claude" / "settings.json", CLAUDE_SETTINGS)
    assert run.install("--host", "claude-code", "--yes", "--no-skill") == 0
    assert json.loads(settings.read_text(encoding="utf-8"))["statusLine"] == CLAUDE_SETTINGS["statusLine"]
    assert "is kept" in run.text
    line = next(l for l in run.lines if l.startswith("& ") and "status --line" in l)
    assert "-m agent_bridge status --line" in line


def test_a_missing_status_line_is_set_with_its_refresh_interval(run: Run, home: Path) -> None:
    settings = _write(home / ".claude" / "settings.json", {"theme": "dark"})
    assert run.install("--host", "claude-code", "--yes", "--no-skill") == 0
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["statusLine"]["type"] == "command"
    assert data["statusLine"]["refreshInterval"] == 2
    assert data["statusLine"]["command"].endswith("-m agent_bridge status --line")


def test_a_host_whose_file_is_unverified_gets_a_snippet_not_a_write(run: Run, home: Path) -> None:
    def must_not_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"ran {argv} for a file that cannot be checked first")

    lookup = {"code": "C:/Code/bin/code.cmd"}.get
    assert run.install("--host", "vscode-copilot", "--yes", "--no-skill", runner=must_not_run, path_lookup=lookup) == 0
    assert not (home / "AppData").exists() and not (home / ".config").exists() and not (home / "Library").exists()
    assert "not verified" in run.text
    assert "code --add-mcp" in run.text


def test_the_skill_is_copied_and_a_changed_one_is_replaced_only_on_yes(run: Run, home: Path) -> None:
    target = home / ".claude" / "skills" / "bridge-usage" / "SKILL.md"
    assert run.install("--host", "claude-code", "--yes", "--no-status-line") == 0
    assert target.read_bytes() == install_cmd.packaged_skill("bridge-usage")
    target.write_text("my own edit\n", encoding="utf-8")
    assert run.install("--host", "claude-code", "--no-status-line", answers=["n"]) == 0
    assert target.read_text(encoding="utf-8") == "my own edit\n"
    assert "-my own edit" in run.text


def test_install_writes_every_skill_and_a_second_run_changes_nothing(run: Run, home: Path) -> None:
    skills = home / ".claude" / "skills"
    assert run.install("--host", "claude-code", "--no-status-line", "--print") == 0
    assert not skills.exists()
    for name in install_cmd.SKILL_NAMES:
        assert f"(the packaged {name}/SKILL.md," in run.text
    assert run.install("--host", "claude-code", "--yes", "--no-status-line") == 0
    for name in install_cmd.SKILL_NAMES:
        assert (skills / name / "SKILL.md").read_bytes() == install_cmd.packaged_skill(name)
    before = _snapshot(home)
    assert run.install("--host", "claude-code", "--yes", "--no-status-line") == 0
    assert _snapshot(home) == before
    assert not [line for line in run.lines if line.startswith(">> ")]


def test_install_offers_each_skill_as_its_own_confirmed_write(home: Path, python: Path) -> None:
    lines: list[str] = []
    asked: list[str] = []

    def ask(prompt: str) -> str:
        header = next(line for line in reversed(lines) if line.startswith(">> "))
        asked.append(header)
        return "y" if "bridge-routing" in header else "n"

    code = install_cmd.run_install(
        ["--home", str(home), "--command", f"{python} -m agent_bridge", "--host", "claude-code", "--no-status-line"],
        env={}, path_lookup=lambda name: None, runner=None, ask=ask, out=lines.append,
    )
    assert code == 0
    skills = home / ".claude" / "skills"
    assert (skills / "bridge-routing" / "SKILL.md").read_bytes() == install_cmd.packaged_skill("bridge-routing")
    assert not (skills / "bridge-usage" / "SKILL.md").exists()
    for name in install_cmd.SKILL_NAMES:
        assert len([h for h in asked if str(skills / name / "SKILL.md") in h]) == 1, name


# ------------------------------------------------------------------ the host's own command


@pytest.mark.parametrize("original", [CODEX_TOML, "mcp_servers = []\n"])
def test_user_registration_uses_confirmed_file_editor(run: Run, home: Path, python: Path, original: str) -> None:
    config = _write(home / ".codex" / "config.toml", original)
    calls: list[list[str]] = []

    def fake(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "unsupported config")

    lookup = {"codex": "C:/tools/codex.exe"}.get
    code = run.install("--host", "codex", "--no-skill", answers=["y"], runner=fake, path_lookup=lookup)
    assert not calls
    assert code == 0
    if original != CODEX_TOML:
        assert config.read_bytes() == original.encode()
        assert "paste by hand" in run.text
        assert not list(config.parent.glob("*.bak-*"))
        return
    assert run.text.index("+[mcp_servers.agent-bridge]") < run.text.index("Apply?") < run.text.index("backup:")
    backup, = config.parent.glob("*.bak-*")
    assert backup.read_bytes() == original.encode()
    table = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["agent-bridge"]
    assert table["command"] == str(python)
    assert table["tool_timeout_sec"] == 600 and table["default_tools_approval_mode"] == "approve"


def test_with_home_and_no_runner_no_host_command_runs(run: Run, home: Path) -> None:
    _write(home / ".codex" / "config.toml", CODEX_TOML)
    lookup = {"codex": "C:/tools/codex.exe"}.get
    assert run.install("--host", "codex", "--yes", "--no-skill", path_lookup=lookup) == 0
    assert "agent-bridge" in tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8"))["mcp_servers"]


# ------------------------------------------------------------------ detection


def test_detect_running_host_by_client_name() -> None:
    assert detect_running_host("gemini-cli-mcp-client", []).name == "gemini-cli"
    assert detect_running_host("opencode", []).name == "opencode"
    assert detect_running_host("grok-shell-agent-bridge", []).name == "grok"
    assert detect_running_host("gemini-cli-mcp-client", ["codex.exe"]).name == "gemini-cli"
    assert detect_running_host("something-else", []) is None


def test_detect_running_host_by_nearest_ancestor_process() -> None:
    assert detect_running_host(None, ["python.exe", "node.exe", "codex.exe", "pwsh.exe"]).name == "codex"
    assert detect_running_host(None, [r"C:\Program Files\agy\agy.exe", "claude.exe"]).name == "antigravity"
    assert detect_running_host("unknown-client", ["/usr/bin/claude"]).name == "claude-code"
    assert detect_running_host(None, ["node", "bash", "python3"]) is None


def test_detect_installed_uses_path_and_markers_and_ignores_a_shim(home: Path) -> None:
    (home / ".copilot").mkdir()  # VS Code makes this folder; it is not the Copilot CLI
    (home / ".grok").mkdir()
    lookup = {
        "claude": "C:/bin/claude.exe",
        "copilot": "C:/Code/bin/copilot-shim/copilot",
    }.get
    found = [h.name for h in detect_installed({}, lookup, roots=Roots.for_home(home), exists=Path.exists)]
    assert found == ["claude-code", "grok"]


def test_every_fact_with_docs_provenance_names_its_page() -> None:
    for host in hosts.HOSTS:
        sources = [host.config_source, host.timeout.source, host.skills_source]
        sources += [s.source for s in (host.approval, host.register, host.status_line) if s is not None]
        for source in sources:
            assert source.kind in ("run", "docs", "unverified")
            if source.kind == "docs":
                assert source.url and source.url.startswith("https://"), host.name


# ------------------------------------------------------------------ doctor


def _versions(table: dict[str, str]):
    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, table[Path(argv[0]).stem], "")

    return runner


BACKENDS = {"codex": "C:/b/codex.exe", "agy": "C:/b/agy.exe", "claude": "C:/b/claude.exe"}
VERSIONS = {"codex": "codex-cli 0.172.0", "agy": "1.5.18", "claude": "2.1.332 (Claude Code)"}


def _doctor(home: Path, env: dict[str, str] | None = None, versions: dict[str, str] = VERSIONS) -> tuple[int, dict]:
    lines: list[str] = []
    code = install_cmd.run_doctor(
        ["--home", str(home), "--json"], env=env or {}, path_lookup=BACKENDS.get, runner=_versions(versions), out=lines.append
    )
    return code, {c["name"]: c for c in json.loads("\n".join(lines))["checks"]}


def _good_home(home: Path, python: Path) -> None:
    _write(home / ".codex" / "config.toml", CODEX_TOML + (
        f"\n[mcp_servers.agent-bridge]\ncommand = '{python}'\nargs = [\"-m\", \"agent_bridge\"]\n"
        "tool_timeout_sec = 600\ndefault_tools_approval_mode = \"approve\"\n"
    ))
    for name in install_cmd.SKILL_NAMES:
        _write(home / ".agents" / "skills" / name / "SKILL.md", install_cmd.packaged_skill(name))
    state = home / ".agent-bridge"
    state.mkdir()
    conn = sqlite3.connect(state / "bridge.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE jobs (id TEXT)")
    conn.commit()
    conn.close()


def test_doctor_passes_a_good_setup_and_opens_the_database_read_only(home: Path, python: Path) -> None:
    _good_home(home, python)
    db = home / ".agent-bridge" / "bridge.db"
    before = db.read_bytes()
    code, checks = _doctor(home)
    assert code == 0
    for name in (
        "Codex: registered", "Codex: command", "Codex: tool-call limit", "Codex: approval",
        "Codex: skill bridge-usage", "Codex: skill bridge-routing", "database", "nesting",
    ):
        assert checks[name]["status"] == "ok", (name, checks[name])
    assert checks["backend agy"]["detail"].startswith("1.5.18")
    # SQLite can create WAL sidecars for a lock-aware read-only connection.
    assert db.read_bytes() == before, "doctor changed the database"


@pytest.mark.parametrize("copy", [None, b"an older copy\n"])
def test_doctor_warns_on_a_missing_or_out_of_date_routing_skill(home: Path, python: Path, copy: bytes | None) -> None:
    _good_home(home, python)
    target = home / ".agents" / "skills" / "bridge-routing" / "SKILL.md"
    if copy is None:
        target.unlink()
    else:
        target.write_bytes(copy)
    code, checks = _doctor(home)
    check = checks["Codex: skill bridge-routing"]
    assert check["status"] == "warn"
    assert check["detail"] == f"{'missing' if copy is None else 'out of date'} at {target}"
    assert check["fix"] == "agent-bridge install --host codex"
    assert checks["Codex: skill bridge-usage"]["status"] == "ok"
    assert code == 0


def test_doctor_warns_on_a_missing_timeout_and_names_the_default(home: Path, python: Path) -> None:
    _write(home / ".codex" / "config.toml", CODEX_TOML + f"\n[mcp_servers.agent-bridge]\ncommand = '{python}'\nargs = []\n")
    code, checks = _doctor(home)
    limit = checks["Codex: tool-call limit"]
    assert limit["status"] == "warn"
    assert "60 s" in limit["detail"]
    assert limit["fix"] == "agent-bridge install --host codex"
    assert checks["Codex: approval"]["status"] == "warn"
    assert code == 0


def test_doctor_warns_on_a_missing_registration_and_fails_on_a_missing_command(home: Path, tmp_path: Path) -> None:
    _write(home / ".codex" / "config.toml", CODEX_TOML)
    _write(home / ".gemini" / "config" / "mcp_config.json", {"mcpServers": {"agent-bridge": {"command": str(tmp_path / "gone.exe"), "args": []}}})
    code, checks = _doctor(home)
    assert checks["Codex: registered"]["status"] == "warn"
    assert checks["Codex: registered"]["fix"] == "agent-bridge install --host codex"
    assert checks["Antigravity: command"]["status"] == "fail"
    assert code == 1


def test_doctor_fails_an_old_backend_and_the_nesting_marker(home: Path) -> None:
    code, checks = _doctor(home, env={"AGENT_BRIDGE_PARENT_JOB": "neon-1"}, versions={**VERSIONS, "agy": "1.5.6"})
    assert checks["backend agy"]["status"] == "fail"
    assert "1.5.7" in checks["backend agy"]["fix"]
    assert checks["backend claude"]["status"] == "ok"
    assert checks["nesting"]["status"] == "fail"
    assert code == 1


def test_same_second_backups_are_exclusive_and_preserve_mode(
    run: Run, home: Path, python: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 27, tzinfo=UTC)

    monkeypatch.setattr(install_cmd, "datetime", FrozenTime)
    config = _write(home / ".codex" / "config.toml", CODEX_TOML)
    config.chmod(0o600)
    mode = stat.S_IMODE(config.stat().st_mode)
    base = ["--host", "codex", "--yes", "--no-skill"]
    assert run.install(*base) == 0
    first_edit = config.read_bytes()
    assert run.install(*base, "--command", f'"{python}" --second') == 0
    backups = list(config.parent.glob("*.bak-*"))
    assert len(backups) == 2
    assert {p.read_bytes() for p in backups} == {CODEX_TOML.encode(), first_edit}
    assert all(stat.S_IMODE(p.stat().st_mode) & 0o444 & ~mode == 0 for p in backups)
    assert stat.S_IMODE(config.stat().st_mode) == mode


@pytest.mark.skipif(sys.platform != "win32", reason="uses Windows ACLs")
def test_windows_backup_keeps_restricted_acl(run: Run, home: Path) -> None:
    config = _write(home / ".codex" / "config.toml", CODEX_TOML)
    subprocess.run(["icacls.exe", str(config), "/inheritance:d"], check=True, capture_output=True)
    assert run.install("--host", "codex", "--yes", "--no-skill") == 0
    backup, = config.parent.glob("*.bak-*")
    for path in (backup, config):
        acl = subprocess.check_output(["icacls.exe", str(path)])
        assert b"(I)" not in acl


def test_replacement_failure_leaves_original_and_cleans_up_temp(
    run: Run, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write(home / ".codex" / "config.toml", CODEX_TOML)
    replace = os.replace
    attempted: list[Path] = []

    def fail_replacing_config(src, dst):
        if Path(dst) != config:
            return replace(src, dst)
        source = Path(src)
        attempted.append(source)
        assert source.parent == config.parent
        assert config.read_bytes() == CODEX_TOML.encode()
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(os, "replace", fail_replacing_config)
    assert run.install("--host", "codex", "--yes", "--no-skill") == 1
    assert len(attempted) == 1 and not attempted[0].exists()
    assert config.read_bytes() == CODEX_TOML.encode()
    assert "simulated replacement failure" in run.text


@pytest.mark.parametrize("dangling", [False, True])
def test_symlink_config_is_refused(run: Run, home: Path, dangling: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    config = home / ".cursor" / "mcp.json"
    config.parent.mkdir()
    target = home / "target.json"
    if not dangling:
        _write(target, '{"mcpServers": {}}')
    try:
        config.symlink_to(target)
    except OSError as exc:
        # Windows can require a privilege unavailable to the test process.
        if getattr(exc, "winerror", None) != 1314:
            raise
        real_lstat, real_readlink = Path.lstat, Path.readlink
        link_stat = os.stat_result((stat.S_IFLNK, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        monkeypatch.setattr(Path, "lstat", lambda p: link_stat if p == config else real_lstat(p))
        monkeypatch.setattr(Path, "readlink", lambda p: target if p == config else real_readlink(p))
    before = _snapshot(home)
    assert run.install("--host", "cursor", "--yes", "--no-skill") == 0
    assert _snapshot(home) == before
    assert "symlink" in run.text and str(config) in run.text and str(target) in run.text
    assert "paste by hand" in run.text


@pytest.mark.parametrize("host,relative_path,root,scope", [
    ("claude-code", ".claude.json", "mcpServers", "user"),
    ("zed", ".zed/settings.json", "context_servers", "project"),
])
def test_reinstall_preserves_server_env(
    run: Run, home: Path, python: Path, tmp_path: Path, host: str, relative_path: str, root: str, scope: str
) -> None:
    project = tmp_path / "project"
    env = {"AGENT_BRIDGE_DEFAULT_TIMEOUT": "900", "EXTRA": "keep"}
    config = _write((home if scope == "user" else project) / relative_path, {
        root: {"agent-bridge": {"command": "old", "env": env}},
    })
    assert run.install("--host", host, "--scope", scope, "--project", str(project), "--yes", "--no-status-line", "--no-skill") == 0
    entry = json.loads(config.read_bytes())[root]["agent-bridge"]
    assert entry["command"] == str(python) and entry["env"] == env


@pytest.mark.skipif(sys.platform != "win32", reason="uses the Windows argument parser")
def test_windows_command_parses_quotes_inside_arguments(run: Run, home: Path) -> None:
    raw = r'"C:\A&B dir\python.exe" -m agent_bridge --tag="two words" "" "a\"b"'
    assert run.install("--host", "cursor", "--yes", "--no-skill", "--command", raw) == 0
    entry = json.loads((home / ".cursor" / "mcp.json").read_bytes())["mcpServers"]["agent-bridge"]
    assert entry["command"] == r"C:\A&B dir\python.exe"
    assert entry["args"] == ["-m", "agent_bridge", "--tag=two words", "", 'a"b']


def test_batch_launcher_is_only_shown(run: Run, home: Path) -> None:
    calls = []

    def fake(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    _write(home / ".gemini" / "config" / "mcp_config.json", '{"mcpServers": {}}')
    assert run.install(
        "--host", "antigravity", "--yes", "--no-skill", "--no-status-line",
        runner=fake, path_lookup={"agy": "C:/tools/agy.CMD"}.get,
    ) == 0
    assert not calls
    assert "agy.CMD" in run.text and "mcp add" in run.text


def test_doctor_shows_batch_version_command_without_running_it(home: Path) -> None:
    lines = []
    calls = []

    def fake(argv):
        calls.append(argv)
        return _versions(VERSIONS)(argv)

    assert install_cmd.run_doctor(
        ["--home", str(home), "--json"], env={"AGENT_BRIDGE_AGY_BIN": "C:/tools/agy.bat"},
        runner=fake, path_lookup=BACKENDS.get, out=lines.append,
    ) == 0
    assert all(not argv[0].endswith(".bat") for argv in calls)
    check = next(row for row in json.loads("\n".join(lines))["checks"] if row["name"] == "backend agy")
    assert check["status"] == "warn"
    assert "agy.bat" in check["fix"] and "--version" in check["fix"]


def test_status_line_quotes_for_the_script_shell(run: Run, home: Path, python: Path) -> None:
    _write(home / ".claude" / "settings.json", CLAUDE_SETTINGS)
    raw = f'"{python}" -m agent_bridge "--tag=$x & two words"'
    assert run.install("--host", "claude-code", "--yes", "--no-skill", "--command", raw) == 0
    line = next(line for line in run.lines if line.startswith("& "))
    assert "'--tag=$x & two words'" in line


@pytest.mark.skipif(sys.platform != "win32", reason="executes the hook under native Windows shells")
@pytest.mark.parametrize("host,settings_path,shell", [
    ("antigravity", ".gemini/antigravity-cli/settings.json", ["cmd.exe", "/d", "/s", "/c"]),
    ("claude-code", ".claude/settings.json", ["powershell.exe", "-NoProfile", "-Command"]),
])
def test_generated_windows_hook_passes_literal_arguments(
    run: Run, home: Path, tmp_path: Path, host: str, settings_path: str, shell: list[str]
) -> None:
    probe = _write(tmp_path / "probe & args.py", "import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    tags = ["--tag=$x&%PATH%!two words", 'a"b', ""]
    command = subprocess.list2cmdline([sys.executable, str(probe), *tags])
    assert run.install("--host", host, "--yes", "--no-skill", "--command", command) == 0
    hook = json.loads((home / settings_path).read_bytes())["statusLine"]["command"]
    output = subprocess.check_output([*shell, hook], text=True, timeout=15)
    assert json.loads(output) == [*tags, "status", "--line"]


@pytest.mark.parametrize("raw,reason", [
    (b'{"value":1e400}', "non-finite"),
    (b'{"value":NaN}', "non-finite"),
    (b'{"value":0.123456789123456789}', "round trip"),
    (b'{"value":1e2}', "round trip"),
    (b'{"value":-0}', "round trip"),
    (b'{"nested":{"value":1,"value":2}}', "duplicate"),
    ('{"mcpServers": {}}'.encode("utf-16"), "UTF-8"),
])
def test_unsafe_json_gets_a_snippet_and_other_files_are_edited(
    run: Run, home: Path, raw: bytes, reason: str
) -> None:
    path = _write(home / ".cursor" / "mcp.json", raw)
    assert run.install("--host", "cursor", "--host", "codex", "--yes", "--no-skill") == 0
    assert path.read_bytes() == raw
    assert not list(path.parent.glob("*.bak-*"))
    assert reason in run.text and "paste by hand" in run.text and '"agent-bridge"' in run.text
    assert (home / ".codex" / "config.toml").exists()


@pytest.mark.parametrize("host,relative_path,key,value", [
    ("codex", ".codex/config.toml", "tool_timeout_sec", 900),
    ("antigravity", ".gemini/config/mcp_config.json", "timeoutSeconds", 900),
    ("copilot-cli", ".copilot/mcp-config.json", "timeout", 900_000),
])
def test_repoint_preserves_higher_timeout(run: Run, home: Path, python: Path, host, relative_path, key, value) -> None:
    raw = (
        f'[mcp_servers.agent-bridge]\ncommand = "old"\n{key} = {value}\n'
        if host == "codex" else {"mcpServers": {"agent-bridge": {"command": "old", key: value}}}
    )
    config = _write(home / relative_path, raw)
    assert run.install("--host", host, "--yes", "--no-status-line", "--no-skill") == 0
    original = config.read_text(encoding="utf-8")
    data = tomllib.loads(original) if host == "codex" else json.loads(original)
    entry = data["mcp_servers" if host == "codex" else "mcpServers"]["agent-bridge"]
    assert entry[key] == value and entry["command"] == str(python)


@pytest.mark.parametrize("location,warning", [
    ("uv/cache/archive-v0/abc", "uvx"),
    ("scratch", "temp"),
    ("linked", "worktree"),
])
def test_install_warns_about_transient_interpreter_before_prompt(
    run: Run, home: Path, tmp_path: Path, location: str, warning: str
) -> None:
    if location == "linked":
        gitdir = tmp_path / "main" / ".git" / "worktrees" / "linked"
        _write(tmp_path / location / ".git", f"gitdir: {gitdir.as_posix()}\n")
        _write(gitdir / "commondir", "../..\n")
    exe = _write(tmp_path / location / ".venv" / "python.exe", b"")
    assert run.install(
        "--host", "codex", "--no-skill", "--command", f'"{exe}"', answers=["n"], env={"TMPDIR": str(tmp_path)},
    ) == 0
    warnings = [line for line in run.lines if line.startswith("warning:") and warning in line]
    assert warnings and run.lines.index(warnings[0]) < next(i for i, line in enumerate(run.lines) if "Apply?" in line)
    assert not (home / ".codex" / "config.toml").exists()


def test_repoint_offers_replacement_for_recognized_stale_hook(run: Run, home: Path, python: Path) -> None:
    _write(home / ".claude.json", {"mcpServers": {"agent-bridge": {"command": "C:/old/python.exe"}}})
    old = '"C:/old/python.exe" -m agent_bridge status --line'
    settings = _write(home / ".claude" / "settings.json", {"statusLine": {"command": old}})
    assert run.install("--host", "claude-code", "--yes", "--no-skill") == 0
    assert json.loads(settings.read_bytes())["statusLine"]["command"] == old
    assert old in run.text and "still uses" in run.text
    assert str(python).replace("\\", "/") in run.text and "status --line" in run.text


def test_doctor_relative_state_dir_uses_read_only_query_guard(
    home: Path, python: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _good_home(home, python)
    monkeypatch.chdir(tmp_path)
    db = home / ".agent-bridge" / "bridge.db"
    connect = sqlite3.connect
    queries: list[str] = []

    def observe(database, *, uri):
        assert uri and database == db.resolve().as_uri() + "?mode=ro"
        conn = connect(database, uri=uri)
        conn.set_trace_callback(queries.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", observe)
    assert install_cmd.run_doctor(
        ["--home", str(home), "--state-dir", str(db.parent.relative_to(tmp_path))],
        env={}, path_lookup=BACKENDS.get, runner=_versions(VERSIONS), out=lambda line: None,
    ) == 0
    assert queries[0].lower().replace(" ", "") == "pragmaquery_only=on"
