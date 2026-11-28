# Internals, configuration and development

## Key ideas

| Word | Meaning |
|---|---|
| Host | The CLI you are talking to, which calls the bridge: Claude Code, Codex, Antigravity or another MCP client |
| Subagent | One run of `codex`, `agy` or `claude` that the bridge starts for you |
| Backend | Which CLI runs the job: `codex`, `neon` (the name the bridge uses for `agy`) or `claude` |
| Model | The model the CLI is asked to use. Shortcuts such as `neon-high` or `claude-opus` pick the newest version for you |
| Effort | How hard the model thinks: `low`, `medium`, `high`, `xhigh`. Higher is slower and uses more quota |
| Quota pool | One subscription allowance. There are four, each with its own 5 hour and weekly limit |
| Job | One task handed to the bridge. It has a `job_id` such as `codex-1a2b3c4d` |
| Session | The conversation a job belongs to, kept by the CLI. A resume adds a turn to it |
| Mode | `read_only` (the default) or `edit` |
| Worktree | A separate Git checkout on its own branch, made for one Codex or Claude edit job |
| `parent_run` | A free label you choose to group related jobs, for example `auth-review`. You can filter jobs and usage by it |
| Window | One running host. Each has its own bridge process; they share one database |

## How it works inside

- **One process per window.** Each host window starts its own bridge over stdio. All of them, whichever CLI started them, share `bridge.db`, opened in WAL mode, so any window can list every job.
- **Task rows and progress.** Hosts that declare the MCP tasks extension (`io.modelcontextprotocol/tasks`, such as Claude Code) receive each `run` as a task row polled via `tasks/get` and cancelled via `tasks/cancel`. Other hosts receive receipts and, while a `run` or `jobs` call waits, MCP progress notifications carrying the job status line.
- **Host wait capping.** The server identifies its host from MCP `clientInfo.name` or its ancestor process, reads that host's configured or default tool-call limit, and caps `wait_seconds` 10 seconds below it (or 20 seconds for an unrecognised host).
- **No second level.** A bridge started inside a bridge job refuses `run`. It is found by a marker variable or by a bridge process among its parents.
- **Sessions take turns.** A turn claims its session in the database before it starts. A second turn on the same session waits for the claim, in any window.
- **One deadline.** Queueing, model lookup, worktree setup, the run and any retry all share the job's single deadline.
- **Cleanup.** On Windows each subprocess is placed in a Job Object before it runs, so the whole process tree dies with the job or with the bridge. On macOS and Linux each subprocess gets its own process group, ended with SIGTERM and then SIGKILL. A job reports `stopped` only after the bridge has checked the tree is gone.
- **Closed windows.** If a window dies, its unfinished jobs are marked `failed` with `owner_exited` the next time another window looks, and the session is held until you confirm that turn is over.
- **Usage counting.** `agy` runs are read from its JSON event stream and counted per turn, so a resumed session is not counted twice. Codex and Claude Code report their own token counts for each run.
- **Models.** Shortcuts are resolved against the live `agy models` list each time, so a new model version is picked up without a change here.
- **CLI subcommands.** With no argument, or with `serve`, `agent-bridge` starts the stdio MCP server. `status`, `install` and `doctor` never start the server or open the job manager; `status` opens `bridge.db` read-only (`mode=ro`) and never writes to it.

## What is kept on disk

Everything is under `~/.agent-bridge` (or `AGENT_BRIDGE_STATE_DIR`):

| Path | What is in it |
|---|---|
| `bridge.db` | A SQLite database shared by every window of every host: jobs, sessions, usage and the last quota reading |
| `agy_models_cache.json` | The last model list from `agy` |
| `replies/` | Full replies written as `<job_id>.md` when a call passes `response_file=True` |
| `worktrees/` | Worktrees made for Codex and Claude edit jobs. Never deleted by the bridge |

What to know about the database:

- Each job's full reply is stored in plain text (up to 200,000 characters), with the first 120 characters of its prompt, any error text, the folder paths and the token counts. The full prompt is not stored.
- The newest 500 jobs are kept; older finished ones are deleted. Usage counts are kept for good.
- Nothing is sent anywhere by the bridge itself. The CLIs keep their own histories in their own folders.
- Do not share `bridge.db` or `replies/` if your replies hold anything private.

## Configuration

Settings are environment variables, read once when the bridge starts. Set them where you register the server, then open a new chat in that host. Each host's registration has its own:

```
# Claude Code
claude mcp remove agent-bridge
claude mcp add --scope user agent-bridge -e AGENT_BRIDGE_DEFAULT_TIMEOUT=900 -- /path/to/agent-bridge/.venv/bin/python -m agent_bridge

# Codex: in ~/.codex/config.toml
[mcp_servers.agent-bridge.env]
AGENT_BRIDGE_DEFAULT_TIMEOUT = "900"

# Antigravity: in mcp_config.json, inside the server's entry
"env": {"AGENT_BRIDGE_DEFAULT_TIMEOUT": "900"}
```

| Variable | Default | What it sets |
|---|---|---|
| `AGENT_BRIDGE_NEON_MODEL` | `neon-high` | The model for `backend="neon"` when none is named |
| `AGENT_BRIDGE_CODEX_MODEL` | `gpt-6.2-sol` | The Codex model. `auto`, `latest`, `default`, `inherit` or `cli` leaves the choice to Codex |
| `AGENT_BRIDGE_CODEX_EFFORT` | `high` | The Codex effort: `low`, `medium`, `high`, `xhigh`. `inherit`, `cli`, `default` or `auto` uses `~/.codex/config.toml`. An unknown value stops the bridge from starting |
| `AGENT_BRIDGE_CLAUDE_MODEL` | `sonnet` | The model for `backend="claude"` when none is named. `auto`, `latest`, `default`, `inherit` or `cli` leaves the choice to Claude Code |
| `AGENT_BRIDGE_CLAUDE_EFFORT` | `high` | The Claude effort: `low`, `medium`, `high`, `xhigh`. `inherit`, `cli`, `default` or `auto` leaves it to Claude Code. An unknown value stops the bridge from starting |
| `AGENT_BRIDGE_CLAUDE_AUTH` | `plan` | `plan` leaves `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` out of a Claude job, so it runs on the plan `claude` is signed in to. `inherit` passes them on. An unknown value stops the bridge from starting |
| `AGENT_BRIDGE_CHILD_ENV` | `trim` | `trim` drops credential-named variables from Codex and `agy` job environments. `inherit` passes the environment through unchanged. An unknown value stops the bridge from starting. See [Child environment](safety.md#child-environment) |
| `AGENT_BRIDGE_CHILD_ENV_KEEP` | unset | Comma-separated exact variable names exempted from `AGENT_BRIDGE_CHILD_ENV=trim` (matched case-insensitively on Windows). Use for API-key sign-ins to Codex or `agy` |
| `AGENT_BRIDGE_DEFAULT_TIMEOUT` | `300` | The job deadline in seconds when a call gives none. Kept within 10 to 3600 |
| `AGENT_BRIDGE_MAX_CONCURRENT` | `8` | Jobs running at once in one window. At least 1 |
| `AGENT_BRIDGE_MAX_STORED_JOBS` | `500` | Jobs kept in the database. At least 20 |
| `AGENT_BRIDGE_MODELS_TTL_SECONDS` | `900` | How long the `agy` model list is cached. At least 30 |
| `AGENT_BRIDGE_QUOTA_TTL_SECONDS` | `120` | How long a quota reading is cached. At least 10 |
| `AGENT_BRIDGE_OWNER_STALE_SECONDS` | `30` | How long before a silent window's jobs may be treated as abandoned. At least 5 |
| `AGENT_BRIDGE_STATE_DIR` | `~/.agent-bridge` | Where the database, model cache, reply files and worktrees live |
| `AGENT_BRIDGE_AGY_BIN` | found on `PATH` | The full path to `agy` |
| `AGENT_BRIDGE_CODEX_BIN` | found on `PATH` | The full path to `codex` |
| `AGENT_BRIDGE_CLAUDE_BIN` | found on `PATH` | The full path to `claude` |
| `AGENT_BRIDGE_DEFAULT_CWD` | where the bridge starts | The folder used when a call gives no `cwd` |
| `AGENT_BRIDGE_PARENT_JOB`, `AGENT_BRIDGE_PARENT_OWNER` | unset | Set by the bridge for its subagents. Do not set them yourself: a bridge that sees the first one refuses to start jobs |
| `CODEX_HOME` | `~/.codex` | Where the bridge looks for Codex's `config.toml` |

## Development

```
uv sync
uv run pytest
```

The test suite uses stand-ins for the CLIs, a throwaway state folder, and spends no quota. For checks against the real `codex`, `agy` and `claude` CLIs (spending a little quota in a temp state folder), see [scripts/live-checks/README.md](../scripts/live-checks/README.md). The checks an agent should run, and the actions it must ask about first, are in [AGENTS.md](../AGENTS.md). The code is in `src/agent_bridge/`:

| File | What it holds |
|---|---|
| `cli.py` | Command-line dispatcher (`serve`, `status`, `install`, `doctor`) |
| `install_cmd.py` | `agent-bridge install` and `agent-bridge doctor` |
| `hosts.py` | Host CLI table, config formats, timeout/approval specs and provenance |
| `host_limits.py` | Host identification and tool-call wait capping |
| `mcp_tasks.py` | MCP tasks extension (`io.modelcontextprotocol/tasks`) and progress notifications |
| `status_cmd.py`, `status_text.py` | `agent-bridge status` read-only snapshots and one-line formatting |
| `server.py` | The five MCP tools |
| `dashboard.py` | The usage dashboard: its figures and its text view |
| `jobs.py` | Requests, queueing, sessions, waiting and cancelling |
| `runners.py` | The `agy` and `codex` command lines, worktrees, retries |
| `claude_runner.py` | The `claude -p` command line and its output |
| `nesting.py` | Nesting detection, process identity, and child-environment filtering |
| `process_control.py` | Starting and stopping subprocess trees |
| `model_resolver.py` | Model shortcuts and the `agy` model list |
| `quota.py`, `usage.py` | Quota readings, unified quota view, advice and token counts |
| `storage.py` | The SQLite database |
| `config.py` | Environment variables and name aliases |
