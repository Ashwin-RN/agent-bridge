# Install, update and uninstall

## Install with `agent-bridge install`

Clone the repository, install dependencies, and run the installer:

```
git clone https://github.com/Ashwin-RN/agent-bridge.git
cd agent-bridge
uv sync
uv run agent-bridge install
```

What `agent-bridge install` does:

- Detects host CLIs on the machine (a binary on `PATH`, or a host folder or config file; launcher shims such as VS Code's `copilot-shim` do not count on their own).
- Registers directly through a previewed file edit (it never invokes the host's `mcp add` command).
- Shows a unified diff for each file it would change and asks before writing.
- Saves an exclusive timestamped backup (`<file>.bak-<UTC timestamp>`, preserving permissions/ACLs) before changing an existing file, and writes atomically through a temp file in the same folder after checking the original has not changed.
- Merges keys without overwriting: preserves an existing `env` object on the server entry and keeps any higher tool-call timeout already configured. A second run changes nothing.
- Refuses symlinked configs, non-regular files, unverified config paths, and JSON it would alter (comments, trailing commas, duplicate keys, non-finite numbers, or numbers that change on round trip), printing a snippet to paste by hand instead.
- Shows Windows batch launchers (`.cmd`, `.bat`) and VS Code user registration as commands for you to run manually instead of executing them.
- Warns before prompting if the bridge interpreter path sits in a throwaway location (`uvx` cache, linked Git worktree, or temporary folder).

Useful options:

| Option | What it does |
|---|---|
| `--print` | Show the diffs and commands without writing anything |
| `--host NAME` | Set up named hosts only (repeatable). Default: offer each host found on the machine |
| `--scope user\|project` | Write user config (every project, the default) or project config |
| `--project DIR` | Project folder for `--scope project` (default: current folder) |
| `--command CMD` | Command that starts the bridge (default: this Python with `-m agent_bridge`) |
| `--yes`, `-y` | Accept every change without asking |
| `--approve` | Also pre-approve the bridge's tools on hosts that do not need it for headless runs |
| `--no-status-line` | Do not offer the status line |
| `--no-skill` | Do not offer the usage dashboard skill |

After installing, restart each host or reconnect `agent-bridge` in it.

Run `agent-bridge doctor` (or `uv run agent-bridge doctor`) to verify the setup. It only reads: it checks backend CLIs and minimum versions (`agy` 1.5.7+, `claude` 2.1.259+; Windows `.cmd`/`.bat` launchers are printed to run manually rather than executed), each detected host's registration, command path, tool-call time limit, headless approval rule and `bridge-usage` skill, the state folder and `bridge.db`, and whether `AGENT_BRIDGE_PARENT_JOB` is set. Pass `--json` for JSON output.

### What `install` writes for the three tested hosts

For Claude Code, Codex and Antigravity, the config path, format and registration command are verified from real configurations on a machine (`run` in `hosts.py`). The tool-call limit, approval and status-line keys come from each vendor's documentation (`docs`), and the skills path is verified by `run` on Claude Code and Antigravity and by `docs` on Codex.

| Host | Registration file | Tool-call limit | Approval | Status line (user scope) | Usage skill |
|---|---|---|---|---|---|
| Claude Code (`claude-code`) | `mcpServers.agent-bridge` in `~/.claude.json` (`<project>/.mcp.json` for project scope) | None written (`env.MCP_TOOL_TIMEOUT` in `~/.claude/settings.json` defaults to 100,000 s) | With `--approve` only: adds `"mcp__agent-bridge"` to `permissions.allow` in `~/.claude/settings.json` | Sets `statusLine` in `~/.claude/settings.json` (`refreshInterval: 2`); if one is already set, keeps it and prints the line to add to your script | `~/.claude/skills/bridge-usage/SKILL.md` (`<project>/.claude/skills/...`) |
| Codex (`codex`) | `[mcp_servers.agent-bridge]` in `~/.codex/config.toml` (honours `CODEX_HOME`; project scope `<project>/.codex/config.toml`, read only when trusted) | `tool_timeout_sec = 600` (default 60 s; higher value kept) | By default: `default_tools_approval_mode = "approve"` | None | `~/.agents/skills/bridge-usage/SKILL.md` (`<project>/.agents/skills/...`) |
| Antigravity (`antigravity`) | `mcpServers.agent-bridge` in `~/.gemini/config/mcp_config.json` (`<project>/.agents/mcp_config.json` for project scope) | `"timeoutSeconds": 600` (default 180 s; higher value kept) | By default: adds `"mcp(agent-bridge/*)"` to `permissions.allow` in `~/.gemini/antigravity-cli/settings.json` | Sets `statusLine` in `~/.gemini/antigravity-cli/settings.json`; if one is already set, keeps it and prints the line to add to your script | `~/.gemini/config/skills/bridge-usage/SKILL.md` (`<project>/.agents/skills/...`) |

## Registration by hand

The command is the same everywhere: the Python inside the repo's `.venv`, with the arguments `-m agent_bridge`, over stdio. Use the full path. Below, `<python>` stands for `C:\path\to\agent-bridge\.venv\Scripts\python.exe` on Windows and `/path/to/agent-bridge/.venv/bin/python` on macOS and Linux.

### Claude Code

```
claude mcp add --scope user agent-bridge -- <python> -m agent_bridge
```

`--scope user` makes it available in every project. `--scope project` keeps it to one repository. Then restart Claude Code, or run `/mcp` inside it and reconnect `agent-bridge`.

### Codex

```
codex mcp add agent-bridge -- <python> -m agent_bridge
```

Then open `~/.codex/config.toml` and add two lines to the entry that command wrote:

```toml
[mcp_servers.agent-bridge]
command = "<python>"
args = ["-m", "agent_bridge"]
tool_timeout_sec = 600                    # Codex stops a tool call after 60 seconds otherwise
default_tools_approval_mode = "approve"   # needed for `codex exec`; optional in the interactive CLI
```

- `tool_timeout_sec`: a waiting `run` or `jobs` call often takes longer than Codex's 60 second default.
- `default_tools_approval_mode`: in the interactive CLI, Codex asks before each tool that changes something, and you can answer. In `codex exec` nobody can, and tools not marked read-only fail with "MCP tool call requires approval" (see [verification log](verification-log.md#codex-headless-approval)). `"approve"` lets them through without asking.

### Antigravity (`agy` CLI, the app and the IDE)

```
agy mcp add agent-bridge -- <python> -m agent_bridge
```

The `--` is needed because `-m` starts with a dash. That command sets no timeout, so afterwards add `"timeoutSeconds": 600` to the entry, or write the whole entry yourself in `~/.gemini/config/mcp_config.json` (every project) or `<project>/.agents/mcp_config.json` (one project):

```json
{
  "mcpServers": {
    "agent-bridge": {
      "command": "<python>",
      "args": ["-m", "agent_bridge"],
      "timeoutSeconds": 600
    }
  }
}
```

Antigravity asks before each MCP tool call until you allow it. To allow the bridge's tools once and for all, add `"mcp(agent-bridge/*)"` to `permissions.allow` in `~/.gemini/antigravity-cli/settings.json`. A headless `agy -p` run cannot ask, so it needs that rule.

### Any other MCP client

Point it at the same command over stdio. Give tool calls a timeout of several minutes if the client has one.

Open a new chat in the host after registering. A host that was already running keeps its old tool list until it reconnects.

Each registration is separate. Registering the bridge in Codex does not change what Claude Code sees, and the other way round. All of them share one database, so a job started from one host shows up in `jobs` from another.

## Other hosts

`src/agent_bridge/hosts.py` also holds entries for eleven other hosts. None of these eleven has been tested end-to-end on a real machine. Each fact in `hosts.py` carries its provenance:

- `run`: read from a real configuration on the author's machine, or from the CLI's own `--help`.
- `docs`: read from the vendor's documentation page (`install` prints a note saying the entry was written from documentation and not tested on this machine).
- `unverified`: neither (`install` does not write a file or key marked `unverified`; it prints a snippet instead).

| Host (`--host`) | Config file (user / project) | Time-limit key | Provenance (from `hosts.py`) |
|---|---|---|---|
| Cursor (`cursor`) | `~/.cursor/mcp.json` / `<project>/.cursor/mcp.json` | None; CLI fixed at 60 s, not raisable (IDE ~60 min) | Config `docs`; timeout `unverified` (staff forum reply, not docs); approval and skills `docs`; detection `run` (launcher and `~/.cursor` found, no MCP file yet) |
| GitHub Copilot CLI (`copilot-cli`) | `~/.copilot/mcp-config.json` / `<project>/.github/mcp.json` | `timeout` (ms; default 30,000, raised to 600,000) | Config, timeout, register, skills `docs`; approval and status line `docs` (snippets printed, not written); detection `run` (VS Code `copilot-shim` excluded) |
| VS Code (GitHub Copilot) (`vscode-copilot`) | `<vscode_user>/mcp.json` / `<project>/.vscode/mcp.json` | None (no timeout key on docs page) | Project config `docs`; user config `unverified` (docs name user `mcp.json` but not its disk path; snippet and `code --add-mcp` printed); timeout, register, skills `docs`; detection `run` |
| Gemini CLI (`gemini-cli`) | `~/.gemini/settings.json` / `<project>/.gemini/settings.json` | `timeout` (ms; default 600,000 = 10 min, no raise needed) | Config, timeout, approval (`trust: true` with `--approve`), skills `docs`; register `unverified` (no documented `--` separator); detection `unverified` |
| OpenCode (`opencode`) | `<xdg_config>/opencode/opencode.json` / `<project>/opencode.json` | `timeout.execution` (ms; default 12 hours, no raise needed) | Config (V2 `mcp.servers` layout), timeout, register, skills `docs`; detection `unverified`. Note: runs MCP tools through Code Mode by default; untested with the bridge |
| Grok Build (`grok`) | `~/.grok/config.toml` / `<project>/.grok/config.toml` | `tool_timeout_sec` (s; default 6000, no raise needed) | Config, timeout, register, skills `docs`; approval and status line `docs` (snippets printed, not written); detection `unverified` |
| Qwen Code (`qwen`) | `~/.qwen/settings.json` / `<project>/.qwen/settings.json` | `timeout` (ms; default 600,000 = 10 min, no raise needed) | Config, timeout, approval (`trust: true` with `--approve`), skills `docs`; register `unverified` (no documented `--` separator); detection `unverified` |
| Kiro (`kiro`) | `~/.kiro/settings/mcp.json` / `<project>/.kiro/settings/mcp.json` | None (its `timeout` key is connection timeout, not tool-call limit) | Config, timeout, approval (`autoApprove: ["*"]` with `--approve`), skills `docs`; detection `unverified` |
| Zed (`zed`) | `<xdg_config>/zed/settings.json` / `<project>/.zed/settings.json` | None verified (`context_server_timeout` defaults to 60 s; per-server unit contradicted) | Project config `docs`; user config `unverified` (Linux path only in docs, macOS disagrees, Windows inferred; snippet printed); timeout, skills, detection `unverified`. Comments in `settings.json` are never rewritten |
| Roo Code (`roo`) | None documented / `<project>/.roo/mcp.json` | `timeout` (s; default 60, raised to 600) | Config `docs` (no documented disk path for global `mcp_settings.json`; user scope prints snippet); timeout, skills `docs`; detection `unverified` |
| Cline (`cline`) | `~/.cline/data/settings/cline_mcp_settings.json` / none | None verified (set in UI; key and default not on official pages) | Config `docs` (Cline CLI file; IDE extension path not documented); timeout `unverified`; register, skills `docs`; detection `unverified` |

`<vscode_user>` is `%APPDATA%/Code/User` on Windows, `~/Library/Application Support/Code/User` on macOS, and `<xdg_config>/Code/User` on Linux. `<xdg_config>` is `$XDG_CONFIG_HOME` if set, else `~/.config`.

## Install the skills

Two skills ship in `skills/`:

- `bridge-usage`: the quota and usage dashboard.
- `bridge-routing`: which backend and model to pick, and how to brief and collect a job.

`agent-bridge install` offers to install `skills/bridge-usage/SKILL.md` into each host's skills folder unless `--no-skill` is passed. See [Quota and usage](quota-and-usage.md#the-usage-dashboard-skill) for how to run `bridge-usage`.

To install a skill by hand, copy its folder from `skills/` into the skills folder of each host you use. Install `bridge-usage` after updating the bridge: until the bridge in that host has `usage`, the skill says the bridge is out of date and stops.

| Host | Folder for every project | Folder for one project |
|---|---|---|
| Claude Code | `~/.claude/skills/<skill>/` | `<project>/.claude/skills/<skill>/` |
| Codex | `~/.agents/skills/<skill>/` (OpenAI's docs). Codex's own skill installer uses `~/.codex/skills/` | `<project>/.agents/skills/<skill>/` |
| Antigravity | `~/.gemini/config/skills/<skill>/` (the app and the CLI) | `<project>/.agents/skills/<skill>/` |

On Windows, from the cloned folder in PowerShell (example for `bridge-usage`):

```
Copy-Item -Recurse skills\bridge-usage $HOME\.claude\skills\
Copy-Item -Recurse skills\bridge-usage $HOME\.agents\skills\
Copy-Item -Recurse skills\bridge-usage $HOME\.gemini\config\skills\
```

On macOS and Linux:

```
cp -r skills/bridge-usage ~/.claude/skills/
cp -r skills/bridge-usage ~/.agents/skills/
cp -r skills/bridge-usage ~/.gemini/config/skills/
```

Make the parent folder first if it does not exist. A host that was running picks up a new skill in a new chat; in Antigravity, `/skills reload` also works.

## Optional instructions snippet

The server's own instructions already cover the basics of the five tools. If you want a short pointer in your host's global instructions file (`CLAUDE.md` for Claude Code, `AGENTS.md` for Codex and Antigravity), add these lines:

```markdown
- `agent-bridge` MCP is connected for delegating to the other coding CLIs on this machine.
- Call `usage(quota=True)` before a fan-out to see which pools have room.
- Load the `bridge-routing` skill for which backend and model to use.
```

## Seeing jobs in the host

Claude Code shows each `run` as a background task row natively through the MCP tasks extension. `agent-bridge status --line` (configured by `agent-bridge install` on Claude Code and Antigravity) and `agent-bridge status --watch` show running jobs across every host without spending tokens.

For notes on how other hosts draw background work or named wrapper agents, see [docs/hosts-and-backends.md](hosts-and-backends.md).

## Update and uninstall

### Update an existing install

These steps are written so that you can hand them to an agent on the machine: "pull and update agent-bridge as its docs say".

1. In the cloned folder:

   ```
   git pull
   uv sync
   uv run pytest
   ```

   Every test should pass. They spend no quota.

2. Check the CLIs you use and your registrations: run `uv run agent-bridge doctor`, or check by hand that `agy --version` is 1.5.7 or newer, `claude --version` is 2.1.259 or newer, `codex --version` answers, and each is signed in. Update a CLI that is older.

3. Check each host's registration still points at this folder: `claude mcp get agent-bridge`, `codex mcp list`, `agy mcp list`. If one is missing or needs updating (such as Codex's `tool_timeout_sec` or the usage skill), run `uv run agent-bridge install` or edit it as in [Registration by hand](#registration-by-hand).

4. Restart every open host window, or reconnect `agent-bridge` in each one (`/mcp` in Claude Code and in `agy`). A running window keeps the old code and its older final-write rules until then. An agent cannot do this step for the chat it is running in: do it yourself, then open a new chat.

5. In the new chat, confirm the new code is live. Ask the host to run this through the bridge on `backend="neon"`, `model="hydrogen-medium"`, read only, in the cloned folder:

   > Run the shell command `git log --oneline -1` and tell me what it prints, in one line.

   A current install answers with the commit line and ends with a sentence saying no file was changed. An old install comes back with no reply.

6. Optional: install or refresh the skills via `uv run agent-bridge install` or [Install the skills](#install-the-skills), and add the short [Optional instructions snippet](#optional-instructions-snippet) to your host's instructions file.

Nothing in `~/.agent-bridge` needs changing on an update. The database is brought up to date by the bridge itself.

### Uninstall

```
claude mcp remove agent-bridge
codex mcp remove agent-bridge
agy mcp remove agent-bridge
```

Run the line for each host you registered it in. Then remove any worktrees you no longer need (`git worktree remove`, from each repository), and delete `~/.agent-bridge` and the cloned folder.
