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
| [`uv`](https://docs.astral.sh/uv/getting-started/installation/) | Installs and runs the bridge. It fetches Python 3.12 itself if the machine has none |
| `git` | To clone the repository; also for worktree edits and code review |
| A host: Claude Code, Codex, Antigravity or another stdio MCP client | The CLI that calls the bridge |
| `codex` CLI, signed in | The `codex` backend |
| `agy` CLI 1.5.7 or newer, signed in | Gemini, Claude and GPT models on Antigravity |
| `claude` CLI (Claude Code) 2.1.259 or newer, signed in | The `claude` backend (see [Claude as a backend](docs/backends-and-models.md#claude-as-a-backend)) |

You need at least one of `codex`, `agy` and `claude` besides your host. A missing CLI shows `command_found: false` in `backends` and its jobs fail; everything else works. The same CLI can be the host in one window and a backend in another.

Developed and tested on Windows 11. macOS and Linux are written for but not yet run: see [known limits](docs/known-limits.md).

## Install

Every step is per machine. Nothing here needs an API key.

1. **Install `uv`** from its [install page](https://docs.astral.sh/uv/getting-started/installation/).

2. **Install each CLI you want, and sign in to it once in a terminal.** Start the CLI and follow its sign-in prompt; a job on a CLI that is not signed in fails with `auth_error`. Then check they are on `PATH`:

   ```
   uv --version
   git --version
   claude --version
   codex --version
   agy --version
   ```

3. **Get the bridge and run its installer:**

   ```
   git clone https://github.com/Ashwin-RN/agent-bridge.git
   cd agent-bridge
   uv sync
   uv run agent-bridge install
   ```

   Keep the folder: the hosts start the bridge from the Python inside its `.venv`.

   `install` finds the host CLIs on the machine, shows a diff for each file it would change and asks before each write. It registers the bridge, raises each host's tool-call limit, offers the status line and the two skills (`bridge-usage`, `bridge-routing`), and backs up every file it changes. `uv run agent-bridge install --print` shows all of it and writes nothing.

4. **Add the suggested lines to your global instructions file.** `install` ends by printing the lines in [`skills/host-instructions.md`](skills/host-instructions.md). Paste them into `CLAUDE.md` (Claude Code) or `AGENTS.md` (Codex, Antigravity) so the host reads the quota and loads `bridge-routing` before it delegates. The installer never edits that file.

5. **Restart every host window**, or reconnect `agent-bridge` in it.

6. **Check the setup:** `uv run agent-bridge doctor`. It only reads, and gives a fix for anything it flags: backend versions, registrations, timeouts, approvals, the skills and the state database.

Registering by hand, what `install` writes per host, and hosts beyond Claude Code, Codex and Antigravity are in [docs/install.md](docs/install.md).

### Update

```
git pull
uv sync
uv run agent-bridge install
```

Then restart every host window together: windows left open keep running the old code. The database in `~/.agent-bridge` is brought up to date by the bridge itself. Details are in [Update an existing install](docs/install.md#update-an-existing-install).

### A second machine

Repeat Install there. What carries over and what does not:

| | |
|---|---|
| Subscription quota | Shared: it belongs to your accounts, so every machine draws on the same windows |
| Job history, replies and quota readings | Per machine, in `~/.agent-bridge` |
| Host registrations, skills, instruction lines | Per machine: `install` sets them up again |
| CLI sign-ins | Per machine |

## Check it works

In a restarted host window, ask:

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
| [docs/install.md](docs/install.md) | `agent-bridge install`, manual registration, other hosts, skills, the suggested instruction lines, update and uninstall |
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
