# agent-bridge

agent-bridge lets the AI coding CLI you are sitting in hand work to the other ones on your machine and get the answer back in the same chat.

It is a small local MCP server. Your CLI (the host) calls its tools, the bridge runs the `codex`, `agy` (Antigravity) or `claude` CLI as a subprocess, and the reply comes back as a tool result.

```
you -> host CLI -> agent-bridge -> codex, agy or claude -> reply -> host CLI -> you
```

The host can be Claude Code, Codex, Antigravity or any other MCP client. From Claude Code you reach Codex and Gemini; from Codex or Antigravity you reach Claude the same way.

It uses the CLI subscriptions you already have. There are no API keys, no web app and no background service: the bridge runs only while a host that registered it is open.

What you can do with it:

- Ask a second model for a review, a second opinion or a piece of research.
- Fan out parallel tasks across different models (up to 8 concurrent jobs per window by default).
- Let Codex or Claude edit code in its own Git worktree, away from your checkout.
- Carry on a conversation with an agent you started earlier.
- See how much of each subscription's quota is left before you spend it.
- See everything the bridge has run, by backend, pool and model, for a period you choose, from its own records.

## What you need

| Thing | Why |
|---|---|
| Python 3.12+ and [`uv`](https://docs.astral.sh/uv/) | To install and run the bridge |
| A host (Claude Code, Codex, Antigravity or another stdio MCP client) | The CLI that calls the bridge |
| `codex` CLI, installed and signed in | For the `codex` backend |
| `agy` CLI 1.5.7 or newer, installed and signed in | For Gemini, Claude and GPT models on Antigravity (1.5.7+ lets headless shell commands run in `agy`'s sandbox) |
| `claude` CLI (Claude Code) 2.1.259 or newer, installed and signed in | For the `claude` backend (see [Claude as a backend](docs/backends-and-models.md#claude-as-a-backend)) |
| `git` | Only for worktree edits and code review |

You need at least one of `codex`, `agy` and `claude` besides your host. A missing CLI shows `command_found: false` in `backends` and its jobs fail; everything else works. The same CLI can be the host in one window and a backend in another.

Check what is installed:

```
uv --version
claude --version
codex --version
agy --version
```

## Install

```
git clone https://github.com/Ashwin-RN/agent-bridge.git
cd agent-bridge
uv sync
uv run agent-bridge install
```

`agent-bridge install` finds the host CLIs on your machine, shows a unified diff for each file it would change, asks before each write, saves a timestamped backup and writes atomically. It refuses symlinked configs and JSON it would alter, preserves an existing `env` block and any higher timeout, warns on throwaway interpreter paths, and offers the status line and `bridge-usage` skill.

Restart each host (or reconnect `agent-bridge` in it) once the installer finishes.

- Run `uv run agent-bridge install --print` to inspect every diff and command without writing anything.
- Run `uv run agent-bridge doctor` to check backend versions, host registrations, timeouts, approvals, the skill and the state database.
- See [docs/install.md](docs/install.md) for registering by hand, for what `install` writes per host, for skills (`bridge-usage` and `bridge-routing`), and for hosts beyond Claude Code, Codex and Antigravity.

## Check it works

See that the host has it: `claude mcp get agent-bridge`, `codex mcp list` or `agy mcp list`.

Then ask the host:

> Call backends on agent-bridge and tell me which backends are healthy.

A healthy backend has `command_found`, `healthy` and `available` all `true`, and a `version`. For `agy` the reply also lists the live models and what each shortcut resolves to today. Check that `nesting` reads `{"nested": false}`; `host` and `wait_cap` show the detected host and its tool-call wait cap.

## Your first tasks

You talk to your host in plain words. It picks the tool and the arguments. Naming the bridge and the backend helps.

| You say | The host calls |
|---|---|
| "Ask Codex through agent-bridge whether this function has a race." | `run` (with `wait_seconds`, or collected by `jobs` / a task row) |
| "Start a Gemini agent in the background to map how auth works in this repo, and carry on." | `run`, later `jobs` |
| "Get a review of my uncommitted changes from both Codex and Gemini." | two parallel `run` calls with `review={}` |
| "Have Codex make this refactor in an isolated worktree." | `run` with `mode="edit"`, `worktree=True` |
| "How much quota is left on each pool?" | `usage` with `quota=True` |

## Seeing what is running

- **Task rows in Claude Code.** Claude Code supports MCP tasks, so each `run` appears as a background task row whose summary is the job's `line`. Claude Code polls the bridge and delivers the finished reply when the job ends, with no polling by the model.
- **Status line.** `uv run agent-bridge status --line` reads `bridge.db` read-only and prints one line for a host's status line (nothing when idle); `--lines` prints one line per running job:

```
bridge: codex · requested gpt-6.2-sol · xhigh · running 2m10s · reading jobs.py
```

- **Live watch and snapshots.** `uv run agent-bridge status --watch` redraws a table of running and recently finished jobs from every window of every host until Ctrl+C. `uv run agent-bridge status` prints the table once, and `--json` prints one snapshot as JSON.

A Codex model reads `requested` because the Codex CLI does not report which model ran, so the bridge shows what was asked for (`agy` and `claude` report the model that ran).

## Safety

- The bridge does not sandbox anything itself; it relies on each CLI's own rules.
- Read-only on `agy` is an instruction in the prompt and not a sandbox.
- For Codex and `agy` jobs, `AGENT_BRIDGE_CHILD_ENV=trim` (default) drops environment variables whose names look like credentials.
- The bridge never commits, pushes or merges.
- A subagent's prompt and the files it reads go to that CLI's provider, under your account there.
- Replies are stored in plain text in the state folder (`~/.agent-bridge/bridge.db` and `replies/`).
- Full details on modes, child environment, worktrees and nesting limits are in [docs/safety.md](docs/safety.md).

## Documentation

| Page | Covers |
|---|---|
| [docs/install.md](docs/install.md) | `agent-bridge install`, manual registration, other hosts, skills, short instructions snippet, update and uninstall |
| [docs/backends-and-models.md](docs/backends-and-models.md) | Backend names, `agy` model shortcuts, Codex and Claude defaults and plan rules |
| [docs/tools.md](docs/tools.md) | The five MCP tools, arguments, reply shapes, paging, failure reasons and retries |
| [docs/safety.md](docs/safety.md) | What a subagent can touch, child environment filtering, edits in a Git worktree, and nesting prevention |
| [docs/quota-and-usage.md](docs/quota-and-usage.md) | The four quota pools, reading quota, usage counts and skills |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common problems, timeouts and waiting, and resuming a session |
| [docs/known-limits.md](docs/known-limits.md) | Known limits and unverified paths |
| [docs/internals.md](docs/internals.md) | Key ideas, how it works inside, state on disk, environment variables and development |
| [docs/verification-log.md](docs/verification-log.md) | Dated test runs and measured observations behind the rules |
| [docs/hosts-and-backends.md](docs/hosts-and-backends.md) | Research notes on named-agent UIs and other CLIs as backends |

## License

MIT. See [LICENSE](LICENSE).
