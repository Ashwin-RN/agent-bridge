"""What the bridge knows about each host CLI: one table of facts, data rather than code.

A host is a program that runs this bridge as an MCP server: Claude Code, Codex, Antigravity and
the others below. For each one the table says how to find it, where its MCP configuration lives
and in which shape, which keys raise its tool-call time limit and pre-approve the bridge's tools,
whether it has an official `mcp add` command, a status-line hook and a skills folder, and how to
recognise it at run time.

Every fact carries its provenance:

- ``run``: read from this machine's real configuration, or from the CLI's own `--help`;
- ``docs``: read on the vendor page named by ``url``;
- ``unverified``: neither. The installer does not write a fact marked so; it prints the snippet.

Paths are templates over a handful of roots (``{home}``, ``{appdata}``, ``{xdg_config}``,
``{codex_home}``, ``{vscode_user}``, ``{project}``). `Roots` resolves them, and every path the
installer touches comes from it, so a test or a dry run with another home never sees the real one.

Two pure functions sit on top: `detect_installed` (which hosts are on this machine) and
`detect_running_host` (which host started this server, from its MCP client name or its ancestor
processes; the server can use it to keep waits under that host's limit).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SERVER_NAME = "agent-bridge"
WANTED_TIMEOUT_SECONDS = 600

Provenance = Literal["run", "docs", "unverified"]


@dataclass(frozen=True)
class Source:
    """Where a fact came from."""

    kind: Provenance
    url: str | None = None
    note: str = ""

    def describe(self) -> str:
        text = self.kind if not self.url else f"{self.kind} {self.url}"
        return f"{text} ({self.note})" if self.note else text


RUN = Source("run")
UNVERIFIED = Source("unverified")


def docs(url: str, note: str = "") -> Source:
    return Source("docs", url, note)


@dataclass(frozen=True)
class Timeout:
    """How a host limits one tool call, and how to raise the limit to what the bridge wants.

    ``key`` is a dotted path inside the server entry, or inside ``file`` when one is given (Claude
    Code's limit is an environment variable in its settings file). ``scale`` is the number of the
    key's units in one second. ``default_seconds`` is the limit when the key is unset. A host
    whose default already meets the bridge's wish needs nothing written.
    """

    key: str | None
    default_seconds: float | None
    raisable: bool = True
    scale: int = 1
    as_string: bool = False
    file: str | None = None
    source: Source = UNVERIFIED

    def wanted_value(self) -> int | str:
        value = WANTED_TIMEOUT_SECONDS * self.scale
        return str(value) if self.as_string else value

    def needs_raising(self) -> bool:
        return self.default_seconds is None or self.default_seconds < WANTED_TIMEOUT_SECONDS


@dataclass(frozen=True)
class Approval:
    """How a host pre-approves the bridge's tools.

    ``kind`` is ``entry`` (a key inside the server entry set to ``value``) or ``list`` (``value``
    added to the list at ``key`` in ``file``). ``by_default`` says whether `install` sets it
    without being asked (it does where the README already told people to); ``headless_needs``
    says whether a run with nobody to answer prompts fails without it, which `doctor` checks.
    ``writable`` is false where the rule lives somewhere the installer does not edit; it then
    prints the snippet.
    """

    kind: Literal["entry", "list"]
    key: str
    value: Any
    file: str | None = None
    file_format: Literal["json", "toml"] = "json"
    by_default: bool = False
    headless_needs: bool = False
    writable: bool = True
    snippet: str = ""
    source: Source = UNVERIFIED


@dataclass(frozen=True)
class Register:
    """The host's own command that writes the registration. ``{scope}`` and ``{cmd}`` expand."""

    argv: tuple[str, ...]
    scopes: tuple[str, ...] = ("user",)
    source: Source = UNVERIFIED


@dataclass(frozen=True)
class StatusLine:
    """A hook that runs a command and shows what it prints. ``value`` holds ``{status_cmd}``."""

    file: str
    key: str
    value: Mapping[str, Any]
    writable: bool = True
    snippet: str = ""
    source: Source = UNVERIFIED


@dataclass(frozen=True)
class Host:
    name: str
    label: str
    binaries: tuple[str, ...]
    markers: tuple[str, ...]
    format: str
    user_config: str | None
    project_config: str | None
    entry: Mapping[str, Any]
    config_source: Source
    timeout: Timeout
    approval: Approval | None = None
    register: Register | None = None
    status_line: StatusLine | None = None
    skills_user: str | None = None
    skills_project: str | None = None
    skills_source: Source = UNVERIFIED
    process_names: tuple[str, ...] = ()
    client_names: tuple[str, ...] = ()
    client_prefixes: tuple[str, ...] = ()
    detect_source: Source = UNVERIFIED
    user_config_source: Source | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def tool_limit_seconds(self) -> float | None:
        """The limit on one tool call when nothing is configured: what a waiting call must stay under."""
        return self.timeout.default_seconds

    def config_path_source(self, scope: str) -> Source:
        if scope == "user" and self.user_config_source is not None:
            return self.user_config_source
        return self.config_source


# Server entries. "{command}" is the executable, "{args}" its argument list, "{argv}" both as one list.
_STDIO = {"command": "{command}", "args": "{args}"}

CLAUDE_DOCS = "https://code.claude.com/docs/en/mcp.md"
CODEX_DOCS = "https://learn.chatgpt.com/docs/config-file/config-reference"
CODEX_MCP_DOCS = "https://learn.chatgpt.com/docs/extend/mcp?surface=cli"
AGY_DOCS = "https://antigravity.google/docs/mcp"
CURSOR_DOCS = "https://cursor.com/docs/context/mcp"
COPILOT_REF = "https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference"
COPILOT_ADD = "https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers"
COPILOT_DIR = "https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference"
VSCODE_DOCS = "https://code.visualstudio.com/docs/copilot/reference/mcp-configuration"
GEMINI_DOCS = "https://geminicli.com/docs/tools/mcp-server/"
OPENCODE_DOCS = "https://opencode.ai/docs/mcp-servers/"
GROK_DOCS = (
    "https://raw.githubusercontent.com/xai-org/grok-build/main/crates/codegen/xai-grok-pager/docs/"
    "user-guide/07-mcp-servers.md"
)
QWEN_DOCS = "https://raw.githubusercontent.com/QwenLM/qwen-code/main/docs/users/features/mcp.md"
KIRO_DOCS = "https://kiro.dev/docs/mcp/configuration/"
ROO_DOCS = "https://roocodeinc.github.io/Roo-Code/features/mcp/using-mcp-in-roo"
CLINE_DOCS = "https://docs.cline.bot/mcp/configuring-mcp-servers"
GROK_GUIDE ="https://github.com/xai-org/grok-build/tree/main/crates/codegen/xai-grok-pager/docs/user-guide"

HOSTS: tuple[Host, ...] = (
    Host(
        name="claude-code",
        label="Claude Code",
        binaries=("claude",),
        markers=("{home}/.claude",),
        format="json-mcpServers",
        user_config="{home}/.claude.json",
        project_config="{project}/.mcp.json",
        entry={"type": "stdio", "command": "{command}", "args": "{args}", "env": {}},
        config_source=RUN,
        timeout=Timeout(
            key="env.MCP_TOOL_TIMEOUT",
            file="{home}/.claude/settings.json",
            default_seconds=100_000,
            scale=1000,
            as_string=True,
            source=docs(
                "https://code.claude.com/docs/en/env-vars.md",
                "MCP_TOOL_TIMEOUT, milliseconds, default 100000000; stdio servers have no per-request timer",
            ),
        ),
        approval=Approval(
            kind="list",
            key="permissions.allow",
            value="mcp__agent-bridge",
            file="{home}/.claude/settings.json",
            source=docs("https://code.claude.com/docs/en/permissions.md"),
        ),
        register=Register(
            argv=("claude", "mcp", "add", "--scope", "{scope}", SERVER_NAME, "--", "{cmd}"),
            scopes=("user", "project"),
            source=RUN,
        ),
        status_line=StatusLine(
            file="{home}/.claude/settings.json",
            key="statusLine",
            value={"type": "command", "command": "{status_cmd}", "refreshInterval": 2},
            source=docs("https://code.claude.com/docs/en/statusline.md", "refreshInterval in seconds, minimum 1"),
        ),
        skills_user="{home}/.claude/skills",
        skills_project="{project}/.claude/skills",
        skills_source=RUN,
        process_names=("claude",),
        detect_source=RUN,
    ),
    Host(
        name="codex",
        label="Codex",
        binaries=("codex",),
        markers=("{codex_home}",),
        format="toml-mcp_servers",
        user_config="{codex_home}/config.toml",
        project_config="{project}/.codex/config.toml",
        entry=_STDIO,
        config_source=RUN,
        timeout=Timeout(
            key="tool_timeout_sec",
            default_seconds=60,
            source=docs(CODEX_DOCS, "Override the default 60s per-tool timeout for an MCP server"),
        ),
        approval=Approval(
            kind="entry",
            key="default_tools_approval_mode",
            value="approve",
            by_default=True,
            headless_needs=True,
            source=docs(CODEX_MCP_DOCS, "Supported values are auto, prompt, writes, and approve"),
        ),
        register=Register(
            argv=("codex", "mcp", "add", SERVER_NAME, "--", "{cmd}"),
            source=RUN,
        ),
        skills_user="{home}/.agents/skills",
        skills_project="{project}/.agents/skills",
        skills_source=docs("https://learn.chatgpt.com/docs/build-skills"),
        process_names=("codex",),
        detect_source=RUN,
        notes=("Codex reads a project's .codex/config.toml only when the project is trusted.",),
    ),
    Host(
        name="antigravity",
        label="Antigravity",
        binaries=("agy",),
        markers=("{home}/.gemini/antigravity-cli", "{home}/.gemini/config"),
        format="antigravity-mcp_config",
        user_config="{home}/.gemini/config/mcp_config.json",
        project_config="{project}/.agents/mcp_config.json",
        entry=_STDIO,
        config_source=RUN,
        timeout=Timeout(
            key="timeoutSeconds",
            default_seconds=180,
            source=docs(AGY_DOCS, "for each tool call; the 180 s default is from a page summary"),
        ),
        approval=Approval(
            kind="list",
            key="permissions.allow",
            value="mcp(agent-bridge/*)",
            file="{home}/.gemini/antigravity-cli/settings.json",
            by_default=True,
            headless_needs=True,
            source=docs("https://antigravity.google/docs/permissions"),
        ),
        register=Register(
            argv=("agy", "mcp", "add", SERVER_NAME, "--", "{cmd}"),
            source=RUN,
        ),
        status_line=StatusLine(
            file="{home}/.gemini/antigravity-cli/settings.json",
            key="statusLine",
            value={"type": "command", "command": "{status_cmd}"},
            source=docs("https://antigravity.google/docs/cli/statusline", "runs on each state change"),
        ),
        skills_user="{home}/.gemini/config/skills",
        skills_project="{project}/.agents/skills",
        skills_source=RUN,
        process_names=("agy", "antigravity"),
        detect_source=RUN,
    ),
    Host(
        name="cursor",
        label="Cursor",
        binaries=("cursor", "agent", "cursor-agent"),
        markers=("{home}/.cursor",),
        format="json-mcpServers",
        user_config="{home}/.cursor/mcp.json",
        project_config="{project}/.cursor/mcp.json",
        entry={"type": "stdio", "command": "{command}", "args": "{args}"},
        config_source=docs(CURSOR_DOCS),
        timeout=Timeout(
            key=None,
            default_seconds=60,
            raisable=False,
            source=Source(
                "unverified",
                "https://forum.cursor.com/t/agent-acp-mcp-tools-call-times-out-at-60s-with-no-way-to-configure-it/163925",
                "staff forum reply, not documentation: CLI fixed at 60 s, IDE about 60 min",
            ),
        ),
        approval=Approval(
            kind="list",
            key="permissions.allow",
            value="Mcp(agent-bridge:*)",
            file="{home}/.cursor/cli-config.json",
            source=docs("https://cursor.com/docs/cli/reference/permissions", "Cursor CLI only"),
        ),
        skills_user="{home}/.cursor/skills",
        skills_project="{project}/.cursor/skills",
        skills_source=docs("https://cursor.com/docs/skills"),
        process_names=("cursor", "cursor-agent"),
        detect_source=Source("run", note="the cursor launcher and ~/.cursor were found here; no MCP file yet"),
        notes=(
            "Cursor's CLI stops a tool call after 60 s and cannot be raised: use start_agent and short check_jobs waits.",
        ),
    ),
    Host(
        name="copilot-cli",
        label="GitHub Copilot CLI",
        binaries=("copilot",),
        markers=("{home}/.copilot/mcp-config.json",),
        format="json-mcpServers",
        user_config="{home}/.copilot/mcp-config.json",
        project_config="{project}/.mcp.json",
        entry={"type": "local", "command": "{command}", "args": "{args}", "tools": ["*"]},
        config_source=docs(COPILOT_ADD, "type, command, args and tools are required"),
        timeout=Timeout(
            key="timeout",
            default_seconds=30,
            scale=1000,
            source=docs(COPILOT_REF, "Timeout in milliseconds for tool discovery and tool calls. Default: 30000"),
        ),
        approval=Approval(
            kind="entry",
            key="",
            value=None,
            writable=False,
            snippet="copilot --allow-tool='agent-bridge'",
            source=docs(COPILOT_REF, "permission patterns in the format Kind(argument), passed as --allow-tool"),
        ),
        register=Register(
            argv=("copilot", "mcp", "add", SERVER_NAME, "--", "{cmd}"),
            source=docs(COPILOT_ADD),
        ),
        status_line=StatusLine(
            file="{home}/.copilot/settings.json",
            key="statusLine",
            value={"command": "{status_cmd}"},
            writable=False,
            snippet='"statusLine": {"command": "<status command>"}',
            source=docs(COPILOT_DIR, "the payload and the other keys are not documented"),
        ),
        skills_user="{home}/.copilot/skills",
        skills_project="{project}/.github/skills",
        skills_source=docs(COPILOT_DIR),
        process_names=("copilot",),
        detect_source=Source("run", note="a VS Code shim named copilot is not the CLI"),
    ),
    Host(
        name="vscode-copilot",
        label="VS Code (GitHub Copilot)",
        binaries=("code",),
        markers=("{vscode_user}",),
        format="vscode-servers",
        user_config="{vscode_user}/mcp.json",
        project_config="{project}/.vscode/mcp.json",
        entry={"type": "stdio", "command": "{command}", "args": "{args}"},
        config_source=docs(VSCODE_DOCS),
        user_config_source=Source(
            "unverified", VSCODE_DOCS, "the docs name the user mcp.json but not where it is on disk"
        ),
        timeout=Timeout(key=None, default_seconds=None, raisable=False, source=docs(VSCODE_DOCS, "no timeout key")),
        register=Register(
            argv=("code", "--add-mcp", "{vscode_json}"),
            source=docs(VSCODE_DOCS),
        ),
        skills_user="{home}/.copilot/skills",
        skills_project="{project}/.github/skills",
        skills_source=docs("https://code.visualstudio.com/docs/agent-customization/agent-skills"),
        process_names=("code",),
        detect_source=RUN,
    ),
    Host(
        name="gemini-cli",
        label="Gemini CLI",
        binaries=("gemini",),
        markers=("{home}/.gemini/settings.json",),
        format="json-mcpServers",
        user_config="{home}/.gemini/settings.json",
        project_config="{project}/.gemini/settings.json",
        entry=_STDIO,
        config_source=docs(GEMINI_DOCS),
        timeout=Timeout(
            key="timeout",
            default_seconds=600,
            scale=1000,
            source=docs(GEMINI_DOCS, "Request timeout in milliseconds (default: 600,000ms = 10 minutes)"),
        ),
        approval=Approval(
            kind="entry",
            key="trust",
            value=True,
            source=docs(GEMINI_DOCS, "When true, bypasses all tool call confirmations for this server"),
        ),
        register=Register(
            argv=("gemini", "mcp", "add", "-s", "{scope}", SERVER_NAME, "{cmd}"),
            scopes=("user", "project"),
            source=docs(GEMINI_DOCS, "the default scope is project"),
        ),
        skills_user="{home}/.gemini/skills",
        skills_project="{project}/.gemini/skills",
        skills_source=docs("https://geminicli.com/docs/cli/skills/"),
        process_names=("gemini",),
        client_names=("gemini-cli-mcp-client",),
        detect_source=Source(
            "unverified",
            "https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/tools/mcp-client.ts",
            "client name read from the source, not the docs; ~/.gemini is shared with Antigravity",
        ),
        notes=("~/.gemini/settings.json is Gemini CLI's; Antigravity uses ~/.gemini/config and ~/.gemini/antigravity-cli.",),
    ),
    Host(
        name="opencode",
        label="OpenCode",
        binaries=("opencode",),
        markers=("{xdg_config}/opencode",),
        format="opencode-mcp",
        user_config="{xdg_config}/opencode/opencode.json",
        project_config="{project}/opencode.json",
        entry={"type": "local", "command": "{argv}"},
        config_source=docs(OPENCODE_DOCS, "V2 layout: servers under mcp.servers, command is one array"),
        timeout=Timeout(
            key="timeout.execution",
            default_seconds=12 * 3600,
            scale=1000,
            source=docs(OPENCODE_DOCS, "execution: 12 hours, positive integer milliseconds"),
        ),
        register=Register(
            argv=("opencode", "mcp", "add", SERVER_NAME, "--global", "--", "{cmd}"),
            source=docs(OPENCODE_DOCS, "writes the project config unless --global"),
        ),
        skills_user="{xdg_config}/opencode/skills",
        skills_project="{project}/.opencode/skills",
        skills_source=docs("https://opencode.ai/docs/skills/"),
        process_names=("opencode",),
        client_names=("opencode",),
        detect_source=Source("unverified", note="client name from the design notes, not from a page opened here"),
        notes=("OpenCode runs MCP tools through Code Mode by default; untested with the bridge.",),
    ),
    Host(
        name="grok",
        label="Grok Build",
        binaries=("grok",),
        markers=("{home}/.grok",),
        format="toml-mcp_servers",
        user_config="{home}/.grok/config.toml",
        project_config="{project}/.grok/config.toml",
        entry=_STDIO,
        config_source=docs(GROK_DOCS),
        timeout=Timeout(
            key="tool_timeout_sec",
            default_seconds=6000,
            source=docs(GROK_DOCS, "Per-tool-call timeout fallback, seconds (default: 6000)"),
        ),
        approval=Approval(
            kind="list",
            key="permission.allow",
            value="MCPTool(agent-bridge__*)",
            file="{home}/.grok/config.toml",
            file_format="toml",
            writable=False,
            snippet='[permission]\nallow = ["MCPTool(agent-bridge__*)"]',
            source=docs(GROK_GUIDE + "/22-permissions-and-safety.md"),
        ),
        register=Register(
            argv=("grok", "mcp", "add", "--scope", "{scope}", SERVER_NAME, "--", "{cmd}"),
            scopes=("user", "project"),
            source=docs(GROK_DOCS),
        ),
        status_line=StatusLine(
            file="{home}/.grok/config.toml",
            key="ui.status_line",
            value={"type": "command", "command": "{status_cmd}"},
            writable=False,
            snippet='[ui.status_line]\ntype = "command"\ncommand = "<status command>"',
            source=docs(GROK_GUIDE + "/25-status-line.md"),
        ),
        skills_user="{home}/.grok/skills",
        skills_project="{project}/.grok/skills",
        skills_source=docs(GROK_GUIDE + "/08-skills.md"),
        process_names=("grok", "xai-grok-pager"),
        client_prefixes=("grok-shell-",),
        detect_source=Source("unverified", note="client name prefix from the design notes, not from a page opened here"),
    ),
    Host(
        name="qwen",
        label="Qwen Code",
        binaries=("qwen",),
        markers=("{home}/.qwen",),
        format="json-mcpServers",
        user_config="{home}/.qwen/settings.json",
        project_config="{project}/.qwen/settings.json",
        entry=_STDIO,
        config_source=docs(QWEN_DOCS),
        timeout=Timeout(
            key="timeout",
            default_seconds=600,
            scale=1000,
            source=docs(QWEN_DOCS, "(default: 600,000ms = 10 minutes)"),
        ),
        approval=Approval(
            kind="entry",
            key="trust",
            value=True,
            source=docs(QWEN_DOCS, "bypasses tool call confirmations for this server in a trusted workspace"),
        ),
        register=Register(
            argv=("qwen", "mcp", "add", "-s", "{scope}", SERVER_NAME, "{cmd}"),
            scopes=("user", "project"),
            source=docs(QWEN_DOCS),
        ),
        skills_user="{home}/.qwen/skills",
        skills_project="{project}/.qwen/skills",
        skills_source=docs("https://qwenlm.github.io/qwen-code-docs/en/users/configuration/settings/"),
        process_names=("qwen",),
    ),
    Host(
        name="kiro",
        label="Kiro",
        binaries=("kiro-cli", "kiro"),
        markers=("{home}/.kiro",),
        format="json-mcpServers",
        user_config="{home}/.kiro/settings/mcp.json",
        project_config="{project}/.kiro/settings/mcp.json",
        entry=_STDIO,
        config_source=docs(KIRO_DOCS),
        timeout=Timeout(
            key="timeout",
            default_seconds=None,
            scale=1000,
            source=docs(KIRO_DOCS, "milliseconds, Kiro CLI only; the default is not documented"),
        ),
        approval=Approval(
            kind="entry",
            key="autoApprove",
            value=["*"],
            source=docs(KIRO_DOCS, 'use "*" to auto-approve all tools'),
        ),
        skills_user="{home}/.kiro/skills",
        skills_project="{project}/.kiro/skills",
        skills_source=docs("https://kiro.dev/docs/skills/"),
        process_names=("kiro", "kiro-cli"),
    ),
    Host(
        name="zed",
        label="Zed",
        binaries=("zed",),
        markers=("{xdg_config}/zed",),
        format="zed-context_servers",
        user_config="{xdg_config}/zed/settings.json",
        project_config="{project}/.zed/settings.json",
        entry={"command": "{command}", "args": "{args}", "env": {}},
        config_source=docs("https://zed.dev/docs/reference/all-settings"),
        user_config_source=Source(
            "unverified",
            "https://zed.dev/docs/remote-development",
            "the docs give ~/.config/zed/settings.json on Linux only and disagree for macOS; Windows inferred",
        ),
        timeout=Timeout(
            key=None,
            default_seconds=60,
            source=Source(
                "unverified",
                "https://raw.githubusercontent.com/zed-industries/zed/main/assets/settings/default.json",
                "context_server_timeout defaults to 60 s; the per-server timeout's unit is contradicted",
            ),
        ),
        skills_source=UNVERIFIED,
        process_names=("zed",),
        notes=("Zed's settings.json usually holds comments, which the installer never rewrites.",),
    ),
    Host(
        name="roo",
        label="Roo Code",
        binaries=(),
        markers=("{home}/.roo",),
        format="json-mcpServers",
        user_config=None,
        project_config="{project}/.roo/mcp.json",
        entry=_STDIO,
        config_source=docs(ROO_DOCS, "the global mcp_settings.json has no documented path on disk"),
        timeout=Timeout(
            key="timeout",
            default_seconds=60,
            source=docs(ROO_DOCS, "Per-server timeout override in seconds (1-3600)... default of 60 seconds"),
        ),
        skills_user="{home}/.roo/skills",
        skills_project="{project}/.roo/skills",
        skills_source=docs("https://roocodeinc.github.io/Roo-Code/features/skills"),
    ),
    Host(
        name="cline",
        label="Cline",
        binaries=("cline",),
        markers=("{home}/.cline",),
        format="json-mcpServers",
        user_config="{home}/.cline/data/settings/cline_mcp_settings.json",
        project_config=None,
        entry=_STDIO,
        config_source=docs(CLINE_DOCS, "the Cline CLI's file; the IDE extension's is not documented on disk"),
        timeout=Timeout(
            key=None,
            default_seconds=60,
            source=Source("unverified", CLINE_DOCS, "set in the UI; key and default not on the official pages"),
        ),
        register=Register(
            argv=("cline", "mcp", "add", SERVER_NAME, "--yes", "--", "{cmd}"),
            source=docs(CLINE_DOCS),
        ),
        skills_user="{home}/.cline/skills",
        skills_project="{project}/.cline/skills",
        skills_source=docs("https://docs.cline.bot/customization/skills.md"),
        process_names=("cline",),
    ),
)

HOSTS_BY_NAME: dict[str, Host] = {host.name: host for host in HOSTS}


def get_host(name: str) -> Host:
    try:
        return HOSTS_BY_NAME[name]
    except KeyError:
        known = ", ".join(HOSTS_BY_NAME)
        raise KeyError(f"unknown host {name!r}; known hosts: {known}") from None


@dataclass(frozen=True)
class Roots:
    """Where the templates' roots are. Built from a home folder, or from the environment."""

    home: Path
    appdata: Path
    xdg_config: Path
    codex_home: Path
    vscode_user: Path
    project: Path

    @classmethod
    def for_home(cls, home: Path, project: Path | None = None, platform: str | None = None) -> Roots:
        """Every root under ``home``, whatever the environment says. For tests and dry runs."""
        platform = platform or sys.platform
        appdata = home / "AppData" / "Roaming"
        xdg = home / ".config"
        return cls(
            home=home,
            appdata=appdata,
            xdg_config=xdg,
            codex_home=home / ".codex",
            vscode_user=_vscode_user(platform, home, appdata, xdg),
            project=project or home / "project",
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str], project: Path | None = None, platform: str | None = None) -> Roots:
        platform = platform or sys.platform
        home_raw = env.get("USERPROFILE") if platform == "win32" else env.get("HOME")
        home = Path(home_raw or env.get("HOME") or Path.home())
        appdata = Path(env["APPDATA"]) if env.get("APPDATA") else home / "AppData" / "Roaming"
        xdg = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
        codex_home = Path(env["CODEX_HOME"]).expanduser() if env.get("CODEX_HOME") else home / ".codex"
        return cls(
            home=home,
            appdata=appdata,
            xdg_config=xdg,
            codex_home=codex_home,
            vscode_user=_vscode_user(platform, home, appdata, xdg),
            project=project or Path.cwd(),
        )

    def path(self, template: str) -> Path:
        return Path(
            template.format(
                home=self.home,
                appdata=self.appdata,
                xdg_config=self.xdg_config,
                codex_home=self.codex_home,
                vscode_user=self.vscode_user,
                project=self.project,
            )
        )


def _vscode_user(platform: str, home: Path, appdata: Path, xdg: Path) -> Path:
    if platform == "win32":
        return appdata / "Code" / "User"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Code" / "User"
    return xdg / "Code" / "User"


def is_shim(path: str) -> bool:
    """A launcher that only stands in for a missing CLI, like VS Code's `copilot-shim`."""
    return "shim" in Path(path).parent.name.lower()


def detect_installed(
    env: Mapping[str, str],
    path_lookup: Callable[[str], str | None],
    *,
    roots: Roots | None = None,
    exists: Callable[[Path], bool] = Path.exists,
) -> list[Host]:
    """The hosts that look installed: one of their binaries on PATH, or one of their markers present.

    A marker is a folder or file only that host makes (``~/.claude``, ``~/.copilot/mcp-config.json``).

    A binary that is only a shim (VS Code's `copilot-shim`) does not count on its own.
    """
    roots = roots or Roots.from_env(env)
    found: list[Host] = []
    for host in HOSTS:
        on_path = any((hit := path_lookup(name)) and not is_shim(hit) for name in host.binaries)
        has_marker = any(exists(roots.path(template)) for template in host.markers)
        if on_path or has_marker:
            found.append(host)
    return found


def _process_key(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def detect_running_host(client_name: str | None, ancestor_process_names: Sequence[str]) -> Host | None:
    """The host that started this server, or None.

    The MCP ``clientInfo.name`` decides when it is one the table knows. Otherwise the nearest
    ancestor process whose name is a host's process name decides. Names like ``node`` or
    ``python`` that many hosts share are never in the table, so they never decide.
    """
    if client_name:
        wanted = client_name.strip().lower()
        for host in HOSTS:
            if wanted in (name.lower() for name in host.client_names):
                return host
            if any(wanted.startswith(prefix.lower()) for prefix in host.client_prefixes):
                return host
    for process in ancestor_process_names:
        key = _process_key(process)
        for host in HOSTS:
            if key in host.process_names:
                return host
    return None


def provenance_rows() -> list[tuple[str, str, str, str, str]]:
    """host, user config path, format, timeout key, provenance of the config facts. For docs and review."""
    rows = []
    for host in HOSTS:
        rows.append(
            (
                host.name,
                host.user_config or "-",
                host.format,
                host.timeout.key or "(none)",
                host.config_path_source("user").describe(),
            )
        )
    return rows


def environ() -> dict[str, str]:
    return dict(os.environ)
