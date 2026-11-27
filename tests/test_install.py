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
import subprocess
import tomllib
from collections.abc import Sequence
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

    monkeypatch.setattr(install_cmd, "_real_runner", no_real_runner)
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

    def install(self, *args: str, answers: Sequence[str] = (), runner=None, path_lookup=lambda name: None) -> int:
        self.lines.clear()
        queue = list(answers)

        def ask(prompt: str) -> str:
            self.lines.append(prompt)
            if not queue:
                raise EOFError
            return queue.pop(0)

        return install_cmd.run_install(
            ["--home", str(self.home), "--command", f"{self.python} -m agent_bridge", *args],
            env={},
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
    assert run.install("--host", "codex", "--host", "antigravity", "--print", "--yes") == 0
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
    assert target.read_bytes() == install_cmd.packaged_skill()
    target.write_text("my own edit\n", encoding="utf-8")
    assert run.install("--host", "claude-code", "--no-status-line", answers=["n"]) == 0
    assert target.read_text(encoding="utf-8") == "my own edit\n"
    assert "-my own edit" in run.text


# ------------------------------------------------------------------ the host's own command


def test_the_hosts_own_command_registers_then_the_file_gets_the_rest(run: Run, home: Path, python: Path) -> None:
    config = _write(home / ".codex" / "config.toml", CODEX_TOML)
    calls: list[list[str]] = []

    def fake(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        with config.open("a", encoding="utf-8") as fh:
            fh.write(f"\n[mcp_servers.agent-bridge]\ncommand = '{python}'\nargs = [\"-m\", \"agent_bridge\"]\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    lookup = {"codex": "C:/tools/codex.exe"}.get
    assert run.install("--host", "codex", "--yes", "--no-skill", runner=fake, path_lookup=lookup) == 0
    assert calls == [["C:/tools/codex.exe", "mcp", "add", "agent-bridge", "--", str(python), "-m", "agent_bridge"]]
    table = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["agent-bridge"]
    assert table["tool_timeout_sec"] == 600 and table["default_tools_approval_mode"] == "approve"


def test_with_home_and_no_runner_no_host_command_runs(run: Run, home: Path) -> None:
    _write(home / ".codex" / "config.toml", CODEX_TOML)
    lookup = {"codex": "C:/tools/codex.exe"}.get
    # _real_runner raises in this module; a run that reached it would fail here.
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
    skill = install_cmd.packaged_skill()
    _write(home / ".agents" / "skills" / "bridge-usage" / "SKILL.md", skill)
    state = home / ".agent-bridge"
    state.mkdir()
    conn = sqlite3.connect(state / "bridge.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE jobs (id TEXT)")
    conn.commit()
    conn.close()


def test_doctor_passes_a_good_setup_and_opens_the_database_read_only(home: Path, python: Path) -> None:
    _good_home(home, python)
    before = _snapshot(home / ".agent-bridge")
    code, checks = _doctor(home)
    assert code == 0
    for name in ("Codex: registered", "Codex: command", "Codex: tool-call limit", "Codex: approval", "Codex: skill", "database", "nesting"):
        assert checks[name]["status"] == "ok", (name, checks[name])
    assert checks["backend agy"]["detail"].startswith("1.5.18")
    assert _snapshot(home / ".agent-bridge") == before, "doctor changed the state folder"


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
