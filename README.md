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
- Run up to 10 tasks side by side on different models.
- Let Codex or Claude edit code in its own Git worktree, away from your checkout.
- Carry on a conversation with an agent you started earlier.
- See how much of each subscription's quota is left before you spend it.
- See everything the bridge has run, by backend, pool and model, for a period you choose, from its own records.

## Contents

- [What you need](#what-you-need)
- [Install](#install)
- [Check it works](#check-it-works)
- [Your first tasks](#your-first-tasks)
- [Key ideas](#key-ideas)
- [Backends, models and effort](#backends-models-and-effort)
- [The nine tools](#the-nine-tools)
- [Reading a result](#reading-a-result)
- [What a subagent can touch](#what-a-subagent-can-touch)
- [Edits in a worktree](#edits-in-a-worktree)
- [Resuming a session](#resuming-a-session)
- [Timeouts and waiting](#timeouts-and-waiting)
- [Quota and usage](#quota-and-usage)
- [The usage dashboard skill](#the-usage-dashboard-skill)
- [A bridge inside a bridge](#a-bridge-inside-a-bridge)
- [Optional instructions snippet](#optional-instructions-snippet)
- [Optional: see each job as an agent](#optional-see-each-job-as-an-agent)
- [Configuration](#configuration)
- [What is kept on disk](#what-is-kept-on-disk)
- [Update and uninstall](#update-and-uninstall)
- [Troubleshooting](#troubleshooting)
- [How it works inside](#how-it-works-inside)
- [Development](#development)

## What you need

| Thing | Why |
|---|---|
| Python 3.12 or newer and [`uv`](https://docs.astral.sh/uv/) | To install and run the bridge |
| A host: Claude Code, Codex, Antigravity or another MCP client | The CLI that calls the bridge. It needs to run a stdio MCP server, nothing more |
| `codex` CLI, installed and signed in | For the `codex` backend |
| `agy` CLI 1.5.7 or newer, installed and signed in | For the Gemini, Claude and GPT models that Antigravity offers. From 1.5.7 a headless run lets shell commands through in `agy`'s own sandbox; the bridge relies on that |
| `claude` CLI (Claude Code) 2.1.259 or newer, installed and signed in | For the `claude` backend. Older versions lack a flag the bridge passes, and show `available: false`. See [Claude as a backend](#claude-as-a-backend) for how it uses your plan |
| `git` | Only for worktree edits and code review |

You need at least one of `codex`, `agy` and `claude` besides your host. A CLI that is missing shows `command_found: false` in `list_backends` and its jobs fail; everything else works. The same CLI can be the host in one window and a backend in another.

Check what is installed:

```
uv --version
claude --version
codex --version
agy --version
```

## Install

1. Clone and install.

   ```
   git clone https://github.com/Ashwin-RN/agent-bridge.git
   cd agent-bridge
   uv sync
   ```

2. Register it with each CLI you want to use as a host. The command is the same everywhere: the Python inside the repo's `.venv`, with the arguments `-m agent_bridge`, over stdio. Use the full path. Below, `<python>` stands for `C:\path\to\agent-bridge\.venv\Scripts\python.exe` on Windows and `/path/to/agent-bridge/.venv/bin/python` on macOS and Linux.

   **Claude Code**

   ```
   claude mcp add --scope user agent-bridge -- <python> -m agent_bridge
   ```

   `--scope user` makes it available in every project. `--scope project` keeps it to one repository. Then restart Claude Code, or run `/mcp` inside it and reconnect `agent-bridge`.

   **Codex**

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

   - `tool_timeout_sec`: a `run_agent` or a waiting `check_jobs` often takes longer than Codex's 60 second default.
   - `default_tools_approval_mode`: in the interactive CLI, Codex asks before each tool that changes something, and you can answer. In `codex exec` nobody can, and the tools that are not marked read-only fail with "MCP tool call requires approval": `run_agent` did in a test, while `list_backends` went through. `"approve"` lets them through without asking.

   **Antigravity (`agy` CLI, the app and the IDE)**

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

   **Any other MCP client**

   Point it at the same command over stdio. Give tool calls a timeout of several minutes if the client has one.

3. Open a new chat in the host. A host that was already running keeps its old tool list until it reconnects.

Each registration is separate. Registering the bridge in Codex does not change what Claude Code sees, and the other way round. All of them share one database, so a job started from one host shows up in `check_jobs` from another.

## Check it works

See that the host has it: `claude mcp get agent-bridge`, `codex mcp list` or `agy mcp list`.

Then ask the host:

> Call list_backends on agent-bridge and tell me which backends are healthy.

A healthy backend has `command_found`, `healthy` and `available` all `true`, and a `version`. For `agy` the reply also lists the live models and what each shortcut resolves to today. `nesting` should read `{"nested": false}`.

## Your first tasks

You talk to your host in plain words. It picks the tool and the arguments. Naming the bridge and the backend helps.

| You say | The host calls |
|---|---|
| "Ask Codex through agent-bridge whether this function has a race." | `run_agent` |
| "Start a Gemini agent in the background to map how auth works in this repo, and carry on." | `start_agent`, later `check_jobs` |
| "Get a review of my uncommitted changes from both Codex and Gemini." | `review_code` |
| "Run these three questions in parallel on agy Opus." | `run_parallel` |
| "Resume that Codex job and ask it to add tests." | `run_agent` with `resume_from` |
| "Have Codex make this refactor in an isolated worktree." | `run_agent` with `mode="edit"`, `worktree=True` |
| From Codex or Antigravity: "Ask Claude through agent-bridge to review this plan." | `run_agent` with `backend="claude"` |
| "How much quota is left on each pool?" | `get_usage` with `include_quota=True` |
| `/bridge-usage 7d` (Claude Code, Antigravity) or `$bridge-usage 7d` (Codex), once the skill is installed | `usage_dashboard` with `period="7d"` |

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

## Backends, models and effort

### Backend names

| `backend` | Runs | Model when you name none | Quota pool |
|---|---|---|---|
| `codex` (also `gpt`, `openai`) | `codex` | `gpt-6.2-sol` at `high` effort | `codex` |
| `neon` (also `agy`, `gemini`, `antigravity`) | `agy` | `neon-high` | `agy_gemini` |
| `agy-claude` (also `agy-sonnet`) | `agy` | `claude-sonnet-high` | `agy_claude_gpt` |
| `agy-opus` | `agy` | `claude-opus-high` | `agy_claude_gpt` |
| `agy-gpt` | `agy` | `gpt-oss` | `agy_claude_gpt` |
| `claude` (also `claude-code`, `anthropic`) | `claude` | `sonnet` at `high` effort | `claude` |

The three `agy-` names are shortcuts for `neon` with a model already chosen. A `model` you pass always wins over the shortcut's own. In results the backend always reads `neon`, `codex` or `claude`.

`agy-claude` and `claude` are different things. `agy-claude` is Claude served by Antigravity, on Antigravity's quota. `claude` is the Claude Code CLI itself, on your Claude plan.

### Model shortcuts on agy

Shortcuts carry no version number. The bridge reads the live list from `agy models` and picks the newest model that fits.

| `model` | Picks |
|---|---|
| `neon`, `neon-low`, `neon-medium`, `neon-high`, `neon-xhigh` | Gemini Neon at that effort. For builds, design and reviews |
| `hydrogen`, `hydrogen-medium`, `hydrogen-high` | Gemini Hydrogen. For reading and exploring a codebase |
| `flash`, `flash-low` | Gemini Flash. For quick, light checks |
| `carbon`, `argon` | Those Gemini families |
| `claude`, `claude-sonnet`, `claude-sonnet-medium`, `claude-sonnet-high` | Claude Sonnet |
| `claude-opus`, `claude-opus-medium`, `claude-opus-high` | Claude Opus |
| `gpt-oss` | GPT-OSS, at `medium` |
| A full id from `list_backends` | That exact model |

Rules worth knowing:

- A family with no effort means `high`, except `gpt-oss`.
- A bare effort such as `model="medium"` means Neon at that effort.
- `claude` alone means Sonnet.
- The pool follows the model: a name with claude, opus, sonnet, haiku, gpt or oss in it counts against `agy_claude_gpt`; everything else on `agy` counts against `agy_gemini`.
- If the effort you asked for does not exist for that family, or a full id has been retired, the bridge picks the nearest newest model and says so in `model_notice`.
- The bridge never moves a job from the Claude and GPT pool to the Gemini pool by itself. If no model in the pool you asked for is available, the job fails. Pass `allow_cross_pool_fallback=True` to allow the move.
- The model list is cached for 15 minutes in the state folder. If `agy models` cannot be reached and there is no cache, only a full model id runs.

### Codex model and effort

- The default is `gpt-6.2-sol` at `high`, whatever `~/.codex/config.toml` says about effort. This keeps a global `xhigh` there from applying to every delegated job.
- If your Codex config names a newer model of the same family, that one is used.
- `model="latest"`, `"default"` or `"auto"` lets Codex choose its own model.
- If Codex rejects the model, the job runs once more with Codex's own default, and `model_notice` says so.
- `effort` on a call overrides the default for that call.

### Effort on agy

For `agy` the effort is part of the model name (`neon-medium`, `claude-opus-high`). You can name that model, or pass the family with `effort`: `backend="agy-opus"` with `effort="medium"` runs Opus at medium. An `effort` you pass wins over the one in the model name.

### Claude as a backend

`backend="claude"` runs `claude -p`, Claude Code's own headless mode, in the job's folder.

- **Models.** `sonnet`, `opus`, `haiku` or `fable` pick the newest model of that family; the CLI resolves the name, and the result's `model` shows what ran. A full model id is passed as it is. `model="default"` leaves the choice to the CLI's own setting.
- **Effort.** `low`, `medium`, `high`, `xhigh`, passed as `effort`. Names in the `agy` style also work: `opus-high` or `claude-sonnet-medium` are read as that model at that effort. An `effort` you pass wins.
- **Default.** `sonnet` at `high`, whatever your own Claude Code default is. Change it with `AGENT_BRIDGE_CLAUDE_MODEL` and `AGENT_BRIDGE_CLAUDE_EFFORT`.
- **Your plan.** A Claude job draws on the same 5 hour and weekly limits as your own Claude Code and claude.ai use. Anthropic's help page says so: signed in with a plan, "the Claude Agent SDK and `claude -p` still draw from your subscription limits". From a Claude Code host, your own native subagents are usually the better way to reach Claude; this backend is for reaching Claude from Codex, Antigravity or another host.
- **Sign-in.** The bridge starts the unmodified `claude` binary, signed in the way you signed it in. It never reads, copies or forwards the login token, and it never passes `--bare`. Each person who installs the bridge signs in to Claude Code themselves, under their own agreement with Anthropic. Anthropic's rules for Claude Code say they do not "prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription", and they forbid tools that "collect, store, or intermediate" those credentials for others. No page names a local tool that starts the binary for you, so read [their page](https://code.claude.com/docs/en/legal-and-compliance) before building anything on this for other people.
- **API keys.** With `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` in the host's environment, Claude Code would bill that API account instead of your plan, without saying so. The bridge leaves both out of a Claude job's environment. Set `AGENT_BRIDGE_CLAUDE_AUTH=inherit` to pass them on; a run billed to a key then says so in `model_notice`.
- **What it loads.** Your `CLAUDE.md` files and settings, so repository rules apply. No MCP servers at all (`--strict-mcp-config`): a Claude subagent cannot reach your connectors, or the bridge.
- **No model fallback.** If the CLI rejects the model, the job fails with the CLI's message. Only a network or server error on a new `read_only` job is retried, once.

## The nine tools

Your host fills these in for you. The tables are here so you know what you can ask for.

### run_agent

Runs one job and waits for the reply.

| Argument | Default | What it does |
|---|---|---|
| `backend` | required | See [Backend names](#backend-names). On a resume pass plain `neon`, `codex` or `claude`, or leave it out |
| `prompt` | required | The task |
| `cwd` | where the host started the bridge | The folder the subagent works in. Pass it: not every host starts the bridge in your project folder |
| `mode` | `read_only` | `read_only` or `edit` |
| `model` | the backend's default | A shortcut or a full id |
| `effort` | the backend's default | `low`, `medium`, `high`, `xhigh` |
| `timeout_seconds` | 300 | The job's deadline, 10 to 3600 |
| `wait_seconds` | until the job ends | How long this call waits. The job carries on if the wait ends first |
| `resume_from` | none | A `job_id` or `session_id` to continue |
| `relocate_cwd` | `False` | Move a resumed session to another folder on purpose |
| `worktree` | `False` | Codex or Claude in `edit` mode only: work in a new Git worktree |
| `extra_dirs` | none | More folders the subagent may use. Not accepted on a Codex resume |
| `label` | none | A short name shown in job lists |
| `parent_run` | none | A group label |
| `confirm_prior_stopped`, `confirm_prior_job_id` | off | See [Resuming a session](#resuming-a-session) |
| `allow_cross_pool_fallback` | `False` | Let an `agy` job fall back to a model in the other `agy` pool |

Example call:

```json
{
  "backend": "agy-claude",
  "cwd": "/path/to/repo",
  "prompt": "In one sentence, what does pyproject.toml say this package is?"
}
```

Example result, shortened:

```json
{
  "job_id": "neon-1a2b3c4d",
  "backend": "neon",
  "quota_pool": "agy_claude_gpt",
  "status": "completed",
  "mode": "read_only",
  "model": "claude-sonnet-5-5-high",
  "requested_model": "claude-sonnet-high",
  "cwd": "/path/to/repo",
  "effective_cwd": "/path/to/repo",
  "elapsed_seconds": 14.47,
  "session_id": "0f1e2d3c-4b5a-6978-8695-a4b3c2d1e0f9",
  "response": "It is a lightweight local stdio MCP server that ...",
  "usage": {"input_tokens": 17900, "output_tokens": 907, "cached_tokens": 9100, "reasoning_tokens": 0, "total_tokens": 18807},
  "usage_status": "exact"
}
```

### start_agent

The same as `run_agent`, but it returns at once with a `job_id` and the job runs in the background. It takes every `run_agent` argument except `wait_seconds`. Collect the reply with `check_jobs`.

```json
{"job_id": "neon-5e6f7a8b", "backend": "neon", "status": "queued", "mode": "read_only", "timeout_seconds": 300,
 "hint": "Call check_jobs(job_ids=['neon-5e6f7a8b'], wait_seconds=60) to wait for results."}
```

### run_parallel

Starts 1 to 10 jobs in one call.

| Argument | Default | What it does |
|---|---|---|
| `tasks` | required | A list. Each task needs `backend` and `prompt`, and may set `cwd`, `mode`, `model`, `effort`, `resume_from`, `relocate_cwd`, `worktree`, `extra_dirs`, `label`, the two confirm flags and `allow_cross_pool_fallback` |
| `cwd`, `mode` | as `run_agent` | Used by any task that does not set its own |
| `wait` | `True` | `True` waits for all tasks. `False` returns the `job_ids` at once |
| `timeout_seconds` | 300 | One deadline applied to every task |
| `wait_seconds` | the timeout | How long the call waits |
| `parent_run` | none | One group label for every task |

Every task is checked before any starts, so one bad task stops the whole call. Once running, a failed task does not stop the others. The reply has `count`, `all_done` and `results`, one entry per task in order.

```json
{
  "cwd": "/path/to/repo",
  "wait": false,
  "timeout_seconds": 1800,
  "parent_run": "auth-scope",
  "tasks": [
    {"backend": "agy-opus", "model": "claude-opus-medium", "label": "read-server", "prompt": "..."},
    {"backend": "codex", "effort": "low", "label": "read-tests", "prompt": "..."}
  ]
}
```

### check_jobs

Lists recent jobs, or waits for named ones.

| Argument | Default | What it does |
|---|---|---|
| `job_ids` | none | With ids: wait for these jobs and return each in full. Without: return a short list of recent jobs |
| `wait_seconds` | 0 | How long to wait, at most 300 |
| `wait_mode` | `all` | `all` waits for every job, `any` returns when the first one ends |
| `status`, `backend`, `cwd`, `parent_run`, `limit` | none, `limit` 20 | Filters for the list. Ignored when `job_ids` is given |

The list covers jobs from every window of every host. A wait that ends with a job still `queued` or `running` leaves it running: call again.

### cancel_job

Stops one job by `job_id` and kills its subprocesses.

- It cancels jobs started from this window.
- A job owned by another live window must be cancelled from that window.
- A job whose window has closed is marked `failed` with `owner_exited` instead.
- A job that already ended is returned unchanged.

```json
{"job_id": "neon-5e6f7a8b", "status": "cancelled", "failure_reason": "cancelled", "provider_state": "stopped",
 "error": "Job cancelled.",
 "retry_guidance": "Subprocess tree was verified stopped. For edit tasks inspect git status/diff before retrying."}
```

### review_code

A read-only code review by one backend or by several at once.

| Argument | Default | What it does |
|---|---|---|
| `cwd` | where the host started the bridge | The repository |
| `backends` | `["neon", "codex"]` | Who reviews. Add `"claude"` to include it |
| `scope` | `uncommitted` | `uncommitted`, `base` (the diff against a branch) or `commit` (one commit) |
| `base` | `main` | The branch for `scope="base"` |
| `commit` | none | The commit for `scope="commit"`. Required there |
| `focus` | none | What to look for, in your words |
| `neon_model`, `codex_model`, `claude_model` | the defaults | The model for each side |
| `effort` | the default | Applied to the backends chosen |
| `wait`, `timeout_seconds`, `wait_seconds`, `parent_run` | as `run_parallel` | |

Notes:

- With no `focus`, Codex runs its own built-in review (`codex exec review`). With a `focus`, Codex gets a written review prompt instead, as `agy` always does.
- To review with Claude or GPT through `agy`, pass `backends=["neon"]` and set `neon_model`, for example `"claude-opus"`. A name such as `agy-opus` in `backends` only selects `agy`; it does not choose the model here.
- To review with Claude Code itself, pass `backends=["claude"]`. It gets the written review prompt and runs `read_only`.

The reply has `scope`, `base`, `commit`, `all_done` and `reviews`, one result per backend.

### get_usage

What the bridge has run, and optionally how much quota is left.

| Argument | Default | What it does |
|---|---|---|
| `period` | `today` | `session` (this window only), `today`, `7d`, `30d`, `all`. All but `session` count every window |
| `backend`, `quota_pool`, `cwd`, `parent_run` | none | Filters |
| `compact` | `False` | A short planning view: no per-model table and no recent list |
| `include_quota` | `False` | Also read the live quota of each pool. See [Quota and usage](#quota-and-usage) |
| `refresh_quota` | `False` | Skip the cache and read the quota again |
| `recent_limit` | 10 | How many recent runs to list |

### usage_dashboard

A readable account of everything the bridge has run in a period, across every backend, quota pool and model, read from the bridge's own records and nothing else. It only reads: it writes nothing to the database. It also answers in a bridge that runs inside a bridge job. The [usage dashboard skill](#the-usage-dashboard-skill) is the easy way to call it.

| Argument | Default | What it does |
|---|---|---|
| `period` | `today` | `today` (since local midnight), `7d`, `30d`, `all`, or `session` (this window only) |
| `since`, `until` | none | Instead of `period`: a local date such as `2026-11-25`, or an ISO time such as `2026-11-25T14:30`. A date alone as `until` takes in that whole day. Giving both `period` and one of these is refused |

What the reply holds:

- `text`: the whole view as fixed-width text, ready to show as it is. It is rendered from the same figures as the rest of the reply.
- `covers` and `not_covered`: it covers jobs run through the bridge only. Not in it: the host session's own tokens, a host's own subagents, and CLI runs made outside the bridge. The view says so in its third line.
- `totals`: runs, their outcomes, time and tokens.
- `this_session` and `other_sessions`: the same figures split between the calling window and every other window. The two add up to `totals`.
- `by_backend`, `by_quota_pool`, `by_model`: each adds up to `totals`. Every backend and pool is listed, with zeros where nothing ran.
- `not_in_totals`: what the records cannot vouch for, by job id. None of it is added in.
- `counted_as_nothing_spent`: runs whose CLI was never started.

Rules the figures follow:

- A run counts in the period its record was written, which is when it finished. A retried run counts once, under the model its last attempt ran on. Each attempt's tokens go to the model that attempt ran on.
- Totals hold tokens only from finished runs with an `exact` or `delta` count.
- Listed apart under `not_in_totals`, and never added in:

  | Entry | What it is |
  |---|---|
  | `running_or_queued` | Jobs not finished yet, in windows that are still open, with any tokens already recorded for an earlier attempt |
  | `partial_counts` | A count for a turn that did not finish cleanly. It is shown, but it may be short |
  | `cli_started_no_count` | The CLI was started but left no count: cancelled, timed out, failed, or a CLI that reports none, such as Codex's built-in review |
  | `cannot_tell` | Runs the records cannot speak for, each with a `reason`: `window_closed` (the window that ran the job closed before it finished, whether or not a later window has marked it `owner_exited` yet), `job_row_pruned` (no count, and the job's row has been pruned; the newest 500 are kept unless `AGENT_BRIDGE_MAX_STORED_JOBS` says otherwise), or `end_not_confirmed` (no count, and the bridge could not confirm whether the CLI ran or stopped) |
  | `claude_subagents` | Claude runs that started subagents whose tokens are outside the reported count, including resumed sessions |
  | `claude_subagents_not_on_record` | Claude runs that did not record whether they started subagents: written before the bridge kept that count, or Claude Code did not report it |
  | `before_records_begin` | Set when the period reaches back before the bridge's first record. That stretch is not covered, which is not the same as zero |

- A job whose row still says queued or running, but whose window has closed, is found by reading the owners table. Unlike `get_usage`, the dashboard does not mark it failed; it lists it under `cannot_tell` as `window_closed`.
- Left out on purpose: live quota readings, which come from each CLI (`get_usage` with `include_quota` shows them), and Claude Code's cost estimate, which is not a bill and repeats earlier turns on a resume.

### list_backends

Shows what the bridge found on this machine: each CLI's path, version and health, the live `agy` model list with what every shortcut resolves to, the Codex and Claude defaults, the database path, the default timeout, the concurrency cap, and `nesting` (whether this bridge is itself running inside a bridge job).

| Argument | Default | What it does |
|---|---|---|
| `refresh_models` | `False` | Read `agy models` again instead of using the cache |
| `include_quota` | `False` | Add the quota reading and advice |

## Reading a result

### Status

| `status` | Meaning |
|---|---|
| `queued` | Waiting for a free slot, or for an earlier turn on the same session |
| `running` | The CLI is working |
| `completed` | The CLI exited cleanly |
| `failed` | It did not. Read `failure_reason` and `error` |
| `timed_out` | The deadline passed |
| `cancelled` | You cancelled it |

`completed` means the process ended well, not that the job did what you asked. A job can complete with a short or empty `response`. Always read the reply.

### Fields

| Field | Meaning |
|---|---|
| `response` | The subagent's final message |
| `session_id` | Keep it, or the `job_id`, to resume |
| `model`, `requested_model`, `model_notice` | What ran, what you asked for, and why they differ if they do |
| `cwd`, `effective_cwd` | The folder asked for and the folder used. They differ for a worktree job |
| `worktree_path`, `worktree_branch`, `worktree_commit` | Where a worktree job's edits are |
| `usage`, `usage_status` | Tokens for this turn, and how sure the count is (`exact`, `delta`, `partial`, `missing`) |
| `failure_reason`, `provider_state`, `retry_guidance`, `error`, `stderr_tail` | Present when the job did not complete |
| `last_activity` | What a running job is doing |

A bad argument (an unknown backend, a folder that does not exist, `worktree=True` without `edit`) is refused as a tool error before any job is made. The error carries the reason in words, for example `Unknown backend 'bogus'. Expected one of: ...`. A bridge that runs inside a bridge job refuses the job-starting tools the same way, with `nested_bridge`: see [A bridge inside a bridge](#a-bridge-inside-a-bridge).

### When a job fails

| `failure_reason` | Meaning | What to do |
|---|---|---|
| `timeout` | The bridge's deadline passed, queue time included | Raise `timeout_seconds`, or run it in the background |
| `provider_timeout_still_running` | `agy` reports that its turn may still be running on the server | Read `error` for the real cause, wait, then resume with a confirm flag |
| `quota_exceeded` | The pool is out of quota | Check `get_usage`, use another pool |
| `auth_error` | The CLI is not signed in | Sign in to that CLI again |
| `model_unavailable` | The model was rejected, or the pool has no model to offer | `list_backends` with `refresh_models=True`, or name another model |
| `transient_service` | A network or server error | Try again. For an edit job, look at `git diff` first |
| `interactive_fallback` | `agy` wanted an approval that nobody can give | Use Codex in `edit` mode with a worktree |
| `permission_error` | The sandbox refused something, or `agy` or `claude` was denied an action it could not ask about and ended the turn with no reply | Use `edit` or `extra_dirs` if you meant to allow it. For `agy`, resume and tell it to use its file tools, or add an allow rule in `agy`'s `settings.json` |
| `cli_error` | Anything else, a missing CLI included | Read `error` and `stderr_tail`; run `list_backends` |
| `launch_error` | The subprocess could not be started safely | Read `error` |
| `cancelled` | Cancelled | For an edit job, look at `git diff` |
| `prior_turn_uncertain` | An earlier turn on this session may not have stopped | See [Resuming a session](#resuming-a-session) |
| `worktree_missing` | The session's worktree was deleted | Resume with `relocate_cwd=True` and a `cwd`, or with `worktree=True` |
| `owner_exited` | The host window that owned the job closed | Check what it did, then run it again |

| `provider_state` | Meaning |
|---|---|
| `stopped` | The subprocesses were checked and are gone |
| `possibly_running` | The bridge could not confirm that, or `agy`'s turn may go on at the server |
| `unknown` | The owning window died mid-run |

### What the bridge retries by itself

- A rejected model: once, with another model in the same pool (`agy`) or Codex's default. Not on `claude`.
- A network or server error: once, only for a new `read_only` job that had made no progress.
- Nothing else. No retry on a timeout, on quota, on sign-in, or on any `edit` job.

## What a subagent can touch

The bridge does not sandbox anything itself. It asks each CLI to, and the CLI's own rules apply.

| Backend and mode | What the bridge passes | In practice |
|---|---|---|
| `codex`, `read_only` | Sandbox `read-only` | Codex's own read-only sandbox: reading, no writes |
| `codex`, `edit` | Sandbox `workspace-write` on `cwd` and each `extra_dirs` folder | Codex may change files there |
| `codex`, `edit`, `worktree=True` | The same, on the new worktree, plus the repository's `.git` folder | Codex may change files in the worktree. The bridge also offers it the main repository's `.git` folder so that it can commit, but on Windows Codex's sandbox still refused the commit in testing (`index.lock`, access denied). Expect the edits uncommitted, and commit them yourself |
| `codex`, review with no `focus` | Sandbox `read-only` | Read only |
| `agy`, `read_only` | No mode flag, and an instruction at the top of the prompt: this is a read-only run, change no file, say at the end whether you changed one | Read-only by instruction, not by a sandbox. `agy` is able to write files here, so check `git status` after a run that matters. Shell commands run in `agy`'s own sandbox |
| `agy`, `edit` | No mode flag | `agy`'s default headless mode: it may edit files in `cwd` and `extra_dirs`, and shell commands run in its own sandbox |
| `claude`, `read_only` | Permission mode `dontAsk`, the writing tools removed (`Write`, `Edit`, `NotebookEdit`), and nobody to answer a prompt | It reads and searches, and may run shell commands that only read. It has no tool that writes a file, and a shell command that changes something is denied, unless your own Claude Code settings allow that exact command for good. In tests a write by tool and a write by shell were both refused, and the folder stayed clean. No web access |
| `claude`, `edit` | Permission mode `auto`, and deny rules for `git push` | File edits are accepted. Claude Code's own classifier decides each shell command. There is no sandbox: a command the classifier lets through runs with your rights, network included |
| `claude`, `edit`, `worktree=True` | The same, in the new worktree | Edits land in the worktree. In a test the file was made there, the main checkout stayed clean, and `git push origin HEAD` was denied |

Also true in every mode:

- The subprocess runs in `cwd` and inherits the environment of the host, variables and all. The bridge adds `AGENT_BRIDGE_PARENT_JOB` and `AGENT_BRIDGE_PARENT_OWNER` (see [A bridge inside a bridge](#a-bridge-inside-a-bridge)). For `claude` it removes the variables Claude Code sets for its own running session, so that the child does not take itself for part of the host's session.
- The bridge never commits, pushes or merges. A subagent may commit. A Claude edit job is started with deny rules for the plain forms of `git push`, because its classifier allowed a push that the prompt asked for; a push hidden in a script would not be caught. Codex's sandbox has no network. `agy` has neither rule.
- A Claude subagent gets no MCP servers. Codex and `agy` subagents get whatever MCP servers that CLI has registered, the bridge included if you registered it there.
- Your prompt and the files the subagent reads go to that CLI's provider, under your account there.
- `agy` cannot ask for an approval when run this way. An action that needs one is denied, the turn ends with no reply, and the job fails with `permission_error`.
- The bridge starts `agy` with no `--mode` on purpose. With `--mode plan` or `--mode accept-edits`, every shell command needs an approval, so most jobs ended empty.
- For unattended edits, prefer Codex or Claude with `mode="edit"` and `worktree=True`. `agy` has no worktree option.

## Edits in a worktree

`worktree=True` works on Codex and Claude, in `edit` mode only.

1. Before the job starts, the bridge makes a worktree from the repository's current `HEAD` commit. Uncommitted changes in your checkout are not in it.
2. It lives at `<state dir>/worktrees/<repo name>-<hash>/<job_id>`, on a new branch `agent-bridge/<job_id>`. If `cwd` was a subfolder, the job runs in the same subfolder of the worktree.
3. The result gives `worktree_path`, `worktree_branch` and `worktree_commit`. `worktree_commit` is the commit the worktree stands on; if it is the one it started from, the edits are there but not committed.
4. A resume of that session goes back to the same worktree.

Getting the work out is yours to do:

```
git -C <worktree_path> status
git -C <worktree_path> diff
git -C <worktree_path> add -A && git -C <worktree_path> commit -m "..."   # if the subagent left changes uncommitted
git merge agent-bridge/<job_id>                                           # from your own checkout
```

The bridge never deletes a worktree or its branch. When you are done:

```
git worktree remove <worktree_path>          # add --force if it still holds uncommitted changes you do not want
git branch -D agent-bridge/<job_id>
```

Refused: a folder that is not a Git repository, a repository with no commits, `worktree=True` on `agy`, and `worktree=True` without `mode="edit"`.

## Resuming a session

Pass `resume_from` with a `job_id` or a `session_id`. The new turn continues that conversation.

- Carried over: the backend, the folder or worktree, the model, the effort and `parent_run`.
- Pass the plain backend, `neon`, `codex` or `claude`, or leave `backend` out. A shortcut such as `agy-opus` brings its own default model (`claude-opus-high`) and replaces the one the session was using.
- Leave `cwd` out. A resumed session stays in its recorded folder or worktree; a different `cwd` is ignored unless you also pass `relocate_cwd=True`.
- You may change the model. If that moves the session to another quota pool, `model_notice` says so.
- You cannot change the backend, and `extra_dirs` is refused on a Codex resume.
- Two turns on one session never run at once. The second waits, in any window, and the wait counts against its timeout.

If the earlier turn ended with `provider_state` `possibly_running` or `unknown`, the bridge will not resume by itself: the old turn may still be writing. The job fails with `prior_turn_uncertain`. Check that the old turn is over (for an edit job, look at `git status` and `git diff`), then resume with `confirm_prior_job_id="<that job_id>"` or `confirm_prior_stopped=True`.

## Timeouts and waiting

- `timeout_seconds` is the job's deadline: 300 seconds unless you set it, never under 10 or over 3600. The clock starts when the job is queued, so waiting for a slot or for a session uses it up. At the deadline the subprocesses are killed and the job ends `timed_out`.
- `wait_seconds` is only how long the tool call waits. When it runs out, you get the job as it stands, `queued` or `running`, and it carries on.
- 300 seconds is tight for real work. A job that reads a few large files has taken about three and a half minutes, and time spent queued comes off the same clock. Give such jobs a larger `timeout_seconds`.
- For anything over a minute or two, use `start_agent` or `run_parallel` with `wait=False`, then `check_jobs`.
- Each host has its own limit on how long one tool call may take, apart from the bridge's deadline. Claude Code has none to speak of, and may move a call that waits longer than about two minutes to the background; the call still finishes. Codex stops a call after 60 seconds unless `tool_timeout_sec` is set for the server. Antigravity stops it after 3 minutes unless `timeoutSeconds` is set. The [Install](#install) entries above set both to 10 minutes. Keep `wait_seconds` under the host's limit.
- If the host cancels a waiting `run_agent`, `run_parallel` or `review_code`, the jobs it was waiting on are cancelled too. Background jobs are not.
- Each window runs at most 8 jobs at a time. More are queued.

## Quota and usage

### The four pools

| Pool | What counts against it | Where the numbers come from |
|---|---|---|
| `codex` | Every Codex job | Your Codex subscription, read through `codex app-server` |
| `agy_gemini` | Gemini models on `agy` | Antigravity's `/usage`, read through `agy` |
| `agy_claude_gpt` | Claude and GPT-OSS models on `agy` | The same reading |
| `claude` | Every job on the `claude` backend | Your Claude plan. Not read live: see below |

Each pool has a 5 hour window and a weekly window. The Claude and GPT pool on `agy` is separate from your Claude plan and from Codex.

### Reading the quota

`get_usage` with `include_quota=True` starts `codex` and `agy` to ask for their numbers. That can take 10 to 15 seconds. The reading is kept for 2 minutes and shared by every window; `refresh_quota=True` reads again. If a reading fails, that pool shows `status: "unknown"` with the error, and the rest of the reply is still good.

The `claude` pool is different. Claude Code has no command that prints the plan limits, so the bridge keeps what the last Claude job's output carried:

- Before any Claude job has run, or once that reading's reset time has passed, the pool shows `status: "unknown"` with a note.
- After a job, it shows the windows that job reported, with `source: "last_claude_job"`. In the runs made here Claude Code reported only the weekly window, which was past 75% used. A window that is not listed was not reported; it is not known to be empty or full.
- The reading does not see your own Claude use since that job. `refresh_quota` does not change it.
- The advice never marks this pool `use_before_reset`. What is left of the plan before its reset is also what your own Claude sessions run on.

Each window comes back with `used_percent`, `remaining_percent`, `reset_time`, `resets_in` and a `signal`:

| `signal` | Meaning |
|---|---|
| `idle_full` | Untouched. The timer starts on first use |
| `on_track` | Nothing to note |
| `ahead_of_pace` | More quota left than time left in the window |
| `burning_fast` | Being used faster than the window allows |
| `low` | 15% or less left |
| `exhausted` | 2% or less left |
| `expiring_soon_use_it` | A good share is unused and the window resets soon |
| `stale_reset` | The reset time has passed. Read again with `refresh_quota=True` |

`quota_advisory` turns that into advice for each pool:

| `priority` | Meaning |
|---|---|
| `use_before_reset` | The weekly window resets soon with quota unused. Send work here first. Also listed in `expiring_weekly_alerts` |
| `high` | Plenty of room |
| `normal` | Some room |
| `conserve` | Running low or burning fast |
| `avoid` | Exhausted or rate limited |
| `stale` | The reading is out of date |

`recommended_order` lists the usable pools best first. Pools marked `avoid` or `stale` are left out of it. So is the `claude` pool, always: it is the allowance your own Claude sessions run on, so a job uses it only when it names the `claude` backend. `upcoming_resets` lists every reset to come, soonest first.

A quota reading is a snapshot. A few large jobs can use up a 5 hour window that looked nearly full a moment ago.

### Usage

Without `include_quota`, `get_usage` reports only what the bridge itself ran: runs, outcomes, time and tokens, by backend and by pool.

- `exact` and `delta` counts go into the token totals. `partial` counts are shown apart, and `missing` means the CLI gave no figure.
- A job that was retried counts as one run with two attempts.
- Work you do in `codex`, `agy` or `claude` outside the bridge is not in these counts. For `codex` and `agy` it does show in the quota reading.
- For `claude`, `input_tokens` includes cache reads and cache writes, and `cached_tokens` is the cache-read part. `provider_metrics.session_cost_usd_estimate` is Claude Code's own list-price estimate. On a resume the estimate includes earlier turns. For new sessions with native subagents, the bridge sums Claude Code's per-model token counts when available and sets `provider_metrics.usage_scope` to `whole_job`. Resumed sessions use the main agent's per-turn counts. Provider metrics retain the main agent's token figures and report the number of native subagents in `native_subagents` when Claude Code supplies it, including zero.
- Codex's built-in review (`review_code` with no `focus`) reports no token counts, so it shows as 0 tokens.

## The usage dashboard skill

`skills/bridge-usage/SKILL.md` is a skill that calls [`usage_dashboard`](#usage_dashboard) and shows its `text` unchanged. The same file works in Claude Code, Codex and Antigravity. The bridge works out every figure; the skill only picks the period and shows the answer, so the host's model has nothing to add up or round.

### Install it

Copy the `bridge-usage` folder into the skills folder of each host you use. Install it after updating the bridge: until the bridge in that host has `usage_dashboard`, the skill says the bridge is out of date and stops.

| Host | Folder for every project | Folder for one project |
|---|---|---|
| Claude Code | `~/.claude/skills/bridge-usage/` | `<project>/.claude/skills/bridge-usage/` |
| Codex | `~/.agents/skills/bridge-usage/` (OpenAI's docs). Codex's own skill installer uses `~/.codex/skills/` | `<project>/.agents/skills/bridge-usage/` |
| Antigravity | `~/.gemini/config/skills/bridge-usage/` (the app and the CLI) | `<project>/.agents/skills/bridge-usage/` |

On Windows, from the cloned folder in PowerShell:

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

### Use it

| Host | Type |
|---|---|
| Claude Code | `/bridge-usage`, `/bridge-usage 7d`, `/bridge-usage since 2026-11-20` |
| Codex | `$bridge-usage 7d`, or pick it from `/skills` |
| Antigravity | `/bridge-usage 7d` |

With no period it shows today. `session` shows only the window you are typing in.

### What was checked

On 2026-11-26 the skill was run headless in each host, against a bridge started from a test folder: Claude Code 2.1.332 (`claude -p "/bridge-usage 7d"`), Codex 0.172.0 (`codex exec '$bridge-usage 7d'`) and `agy` 1.5.18 (`agy -p "/bridge-usage 7d"`). Each loaded the skill from the project folder in the table, called the tool with `period="7d"`, and printed the view exactly as the tool returned it, with nothing added. `codex exec` called it with no approval setting, because the tool is marked read-only. The same tests found that the hosts hand a tool result to their model differently: Claude Code passes only the structured part, and Codex and Antigravity pass the text. That is why `usage_dashboard` returns one object, which the bridge sends as both.

## A bridge inside a bridge

Once the bridge is registered in more than one CLI, a subagent can see the bridge's tools too. Codex started by the bridge would find the bridge among its own MCP servers, and could start a Claude job, which could start another, and so on.

The bridge stops this at the first step. A bridge that finds itself running inside a bridge job refuses `run_agent`, `start_agent`, `run_parallel` and `review_code`:

```
nested_bridge: this agent-bridge was started inside job codex-1a2b3c4d (detected by env). A subagent may
not start further bridge jobs, so `run_agent` is refused. ...
```

`check_jobs`, `get_usage`, `usage_dashboard` and `list_backends` still work there, and `list_backends` shows `nesting`. There is no setting to allow a second level.

How a bridge knows:

- **A marker variable.** Every subagent is started with `AGENT_BRIDGE_PARENT_JOB` set to its job id. A bridge that sees it is nested. `agy` passes its environment on to the MCP servers it starts, so this is what catches it there.
- **The process tree.** Codex passes only a fixed list of variables to its MCP servers. The bridge checks its ancestors for a live owner in its database or a `python -m agent_bridge` command line. The command line also identifies bridges with separate state directories. If the process tree is unavailable, job-starting calls retry the check and proceed while the answer is unknown.
- **No MCP servers.** A Claude subagent is started with none, so it has no bridge to call.

All three were checked with real runs on Windows: a Codex job and an `agy` job, each with the bridge registered, were refused, and a Claude job reported no MCP tools.

What it does not cover: the process-tree check needs both bridges to use the same state folder, which they do unless you set `AGENT_BRIDGE_STATE_DIR` differently per host. A parent process whose start time cannot be read is not counted as a bridge, so that a process id left over from a dead bridge can never lock an ordinary host out; the price is that such a parent is missed. On macOS and Linux the check reads `/proc` or `ps`; those paths have unit tests for their parsing but were not run on a real machine here.

## Optional instructions snippet

Each host already gets short instructions from the bridge. To make it plan with the quota and keep edits in worktrees without being told each time, add this to the instructions file your host reads: `AGENTS.md` for Codex and Antigravity, `CLAUDE.md` for Claude Code, in your home folder for every project or in one project.

```markdown
## Multi-Model Subagents (`agent-bridge` MCP)
- Before substantial fan-out, call `get_usage(compact=True, cwd=<repo>, include_quota=True)` to inspect the quota pools (`codex`, `agy_gemini`, `agy_claude_gpt`, `claude`), upcoming model resets, and `quota_advisory.expiring_weekly_alerts` / `recommended_order`.
- Prioritize any pool flagged `use_before_reset` when its weekly quota is about to reset with unused capacity. Leverage the separate `agy_claude_gpt` pool (`backend="agy-claude"` / `"agy-opus"` / `"agy-gpt"`) when you want extra Claude/GPT capacity without drawing down Codex or Gemini limits.
- Start with one subagent for routine work: use `hydrogen-medium` / `hydrogen-high` for exploration, and `neon-medium` / `neon-high` for builds and reviews based on task complexity; use multiple backends when independent perspectives or dual reviews matter; resume the original agent (`resume_from`) when prior context matters.
- Pass `cwd` on new jobs; omit `cwd` on `resume_from` so the bridge inherits the session's recorded worktree/cwd (set `relocate_cwd=True` only to intentionally move a resumed session). Resumes inherit prior model intent, effort, and `parent_run` automatically unless overridden.
- Codex defaults to `gpt-6.2-sol` at `high` effort (override `effort` when you want `low`/`medium`/`xhigh`). Default to `mode="read_only"`; for unattended edits prefer `codex` with `mode="edit"` and `worktree=True`, and inspect the returned `effective_cwd` / `worktree_path`.
- `backend="claude"` runs Claude Code on the user's own Claude plan (`sonnet` at `high` unless told otherwise). Use it from a host that is not Claude; in Claude Code use native subagents instead.
- For multi-minute work, use `start_agent` or `run_parallel(wait=False)` and poll with `check_jobs`. Treat `status="completed"` as a lifecycle status and still verify the response contains the requested deliverable. Return concise findings inline and reference longer outputs by `job_id`.
```

## Optional: see each job as an agent

This part is about Claude Code as the host. What the same idea looks like in Codex and Antigravity, and what each host can show without a wrapper, is in [docs/hosts-and-backends.md](docs/hosts-and-backends.md).

A bridge job is one tool call, so in Claude Code it shows as a line in the chat and nowhere else. If you want to see what is running on which service at a glance, have Claude start one small Claude agent per bridge job and let that agent hold the job. Each one then appears in Claude Code's agent list under its own name, such as `neon-high: review auth` or `codex low: add tests`, and you can open it to see the job id, the model and the status.

How it is done:

- One Haiku agent per job, named `<model>: <task>`.
- The agent makes the bridge call, waits with `check_jobs` in steps of about 90 seconds, and replies with a few lines only: `job_id`, `status`, `model`, `elapsed_seconds`, `total_tokens`, `failure_reason`, and whether the reply is empty.
- It does not pass the job's reply along. Claude reads the reply from the bridge by `job_id`, so nothing is reworded on the way.
- Several of these agents can run at once. They share the one bridge process and its concurrency cap.

What it costs: in two short test jobs each Haiku agent used about 76,000 Haiku tokens, nearly all of it the agent's own start-up text. The cost does not grow with the size of the job's reply. Each further 90 second check adds about 25,000 more. It falls on your Claude plan, not on the bridge's pools.

Limits: the name is fixed when the agent starts, so it shows no progress; the agent leaves the list when the job ends. Stopping the agent does not always stop the job: cancel the job by its id as well.

To make it the habit, add this beside the snippet above:

```markdown
- Routing: quick checks go to `hydrogen-medium`. Claude models through `agy` use that pool's quota fast (seven Opus reading jobs took 45% of a week in one test): keep them for when that pool is about to reset with quota unused, or when your Claude plan is running low.
- Make each `agent-bridge` job visible: start one Haiku agent per job, named `<model>: <task>` (for example `neon-high: review auth`). It only makes the bridge call, waits with `check_jobs(wait_seconds=90)` until the job ends, and replies with `job_id`, `status`, `model`, `elapsed_seconds`, `total_tokens`, `failure_reason` and whether `response` is empty. It never relays or rewords the response: read that yourself with `check_jobs(job_ids=[...])`. If you stop the agent, cancel its job by id too.
```

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
| `AGENT_BRIDGE_DEFAULT_TIMEOUT` | `300` | The job deadline in seconds when a call gives none. Kept within 10 to 3600 |
| `AGENT_BRIDGE_MAX_CONCURRENT` | `8` | Jobs running at once in one window. At least 1 |
| `AGENT_BRIDGE_MAX_STORED_JOBS` | `500` | Jobs kept in the database. At least 20 |
| `AGENT_BRIDGE_MODELS_TTL_SECONDS` | `900` | How long the `agy` model list is cached. At least 30 |
| `AGENT_BRIDGE_QUOTA_TTL_SECONDS` | `120` | How long a quota reading is cached. At least 10 |
| `AGENT_BRIDGE_OWNER_STALE_SECONDS` | `30` | How long before a silent window's jobs may be treated as abandoned. At least 5 |
| `AGENT_BRIDGE_STATE_DIR` | `~/.agent-bridge` | Where the database, the model cache and the worktrees live |
| `AGENT_BRIDGE_AGY_BIN` | found on `PATH` | The full path to `agy` |
| `AGENT_BRIDGE_CODEX_BIN` | found on `PATH` | The full path to `codex` |
| `AGENT_BRIDGE_CLAUDE_BIN` | found on `PATH` | The full path to `claude` |
| `AGENT_BRIDGE_DEFAULT_CWD` | where the bridge starts | The folder used when a call gives no `cwd` |
| `AGENT_BRIDGE_PARENT_JOB`, `AGENT_BRIDGE_PARENT_OWNER` | unset | Set by the bridge for its subagents. Do not set them yourself: a bridge that sees the first one refuses to start jobs |
| `CODEX_HOME` | `~/.codex` | Where the bridge looks for Codex's `config.toml` |

## What is kept on disk

Everything is under `~/.agent-bridge` (or `AGENT_BRIDGE_STATE_DIR`):

| Path | What is in it |
|---|---|
| `bridge.db` | A SQLite database shared by every window of every host: jobs, sessions, usage and the last quota reading |
| `agy_models_cache.json` | The last model list from `agy` |
| `worktrees/` | Worktrees made for Codex edit jobs. Never deleted by the bridge |

What to know about the database:

- Each job's full reply is stored in plain text (up to 200,000 characters), with the first 120 characters of its prompt, any error text, the folder paths and the token counts. The full prompt is not stored.
- The newest 500 jobs are kept; older finished ones are deleted. Usage counts are kept for good.
- Nothing is sent anywhere by the bridge itself. The CLIs keep their own histories in their own folders.
- Do not share `bridge.db` if your replies hold anything private.

## Update and uninstall

### Update an existing install

These steps are written so that you can hand them to an agent on the machine: "pull and update agent-bridge as its README says".

1. In the cloned folder:

   ```
   git pull
   uv sync
   uv run pytest
   ```

   Every test should pass. They spend no quota.

2. Check the CLIs you use: `agy --version` is 1.5.7 or newer, `codex --version` and `claude --version` answer, and each is signed in. Update a CLI that is older.

3. Check each host's registration still points at this folder: `claude mcp get agent-bridge`, `codex mcp list`, `agy mcp list`. If one is missing, register it as in [Install](#install). For Codex, check the entry has `tool_timeout_sec`.

4. Restart each host, or reconnect `agent-bridge` in it (`/mcp` in Claude Code and in `agy`). A running host keeps the old code until then. An agent cannot do this step for the chat it is running in: do it yourself, then open a new chat.

5. In the new chat, confirm the new code is live. Ask the host to run this through the bridge on `backend="neon"`, `model="hydrogen-medium"`, read only, in the cloned folder:

   > Run the shell command `git log --oneline -1` and tell me what it prints, in one line.

   A current install answers with the commit line and ends with a sentence saying no file was changed. An old install comes back with no reply.

6. Optional: install or refresh the usage dashboard skill, as in [The usage dashboard skill](#the-usage-dashboard-skill).

7. Optional, per project: add the lines from [Optional instructions snippet](#optional-instructions-snippet) to the project's `AGENTS.md` or `CLAUDE.md`. For Claude Code, [Optional: see each job as an agent](#optional-see-each-job-as-an-agent) too; a `CLAUDE.local.md` beside `CLAUDE.md` keeps them out of Git.

Nothing in `~/.agent-bridge` needs changing on an update. The database is brought up to date by the bridge itself.

### Uninstall

```
claude mcp remove agent-bridge
codex mcp remove agent-bridge
agy mcp remove agent-bridge
```

Run the line for each host you registered it in. Then remove any worktrees you no longer need (`git worktree remove`, from each repository), and delete `~/.agent-bridge` and the cloned folder.

## Troubleshooting

| What you see | Likely cause and fix |
|---|---|
| The host does not list the bridge's tools | It was registered after the host started. Restart it, or reconnect from `/mcp`. Check the path with `claude mcp get agent-bridge`, `codex mcp list` or `agy mcp list` |
| In `codex exec`: "MCP tool call requires approval, but approval policy is never" | Nobody can approve a tool call there. Set `default_tools_approval_mode = "approve"` for the server in `~/.codex/config.toml` |
| In Codex, a tool call ends after 60 seconds | Codex's default tool timeout. Set `tool_timeout_sec = 600` for the server |
| In Antigravity, a tool call is denied or waits for approval | MCP tools are in Ask mode until allowed. Add `"mcp(agent-bridge/*)"` to `permissions.allow` |
| `nested_bridge` | The call came from a subagent that the bridge itself started. That is refused on purpose: see [A bridge inside a bridge](#a-bridge-inside-a-bridge). If it comes from your own chat, `AGENT_BRIDGE_PARENT_JOB` is set in that host's environment: unset it |
| A tool error that reads only "Error executing tool ..." | A crash inside the bridge, not a refusal. The bridge's log on stderr has the traceback |
| The `claude` backend shows `available: false` and "too old" | Claude Code is older than 2.1.259. Update it |
| "... is a shell shim, which the bridge will not start" | On Windows, `claude` resolved to a `.cmd` file with no script the bridge could run directly. Set `AGENT_BRIDGE_CLAUDE_BIN` to `claude.exe` |
| Every `claude` job fails at once on a managed machine | The bridge passes `--strict-mcp-config`. Claude Code's docs describe setups with a managed MCP file where that flag is not accepted; not tested here |
| A `claude` job fails with `auth_error` | Claude Code is not signed in for that user. Run `claude auth login`, or `claude` and `/login` |
| A `claude` job in `read_only` ends with "denied" in its reply | It tried to write or to run a changing command. That is the mode working. Use `mode="edit"` if you meant it to change things |
| `command_found: false` for a backend | That CLI is not on `PATH`. Install it, or set `AGENT_BRIDGE_AGY_BIN` or `AGENT_BRIDGE_CODEX_BIN` |
| `auth_error` | Sign in to that CLI in a terminal, then try again |
| An `agy` job comes back with one line such as "I'll start by reading the files", or with no reply | `agy` denied an action it could not ask about, most often a shell command, and ended the turn. With no reply the job now fails with `permission_error`; with a line of text it still reads `completed`. Resume it and tell it to use its file tools |
| The reply points to a report file instead of holding the answer | `agy` wrote its answer to a file in its own data folder. Open that file, or ask for the answer in the reply itself |
| `timed_out` on a job that was doing fine | The 300 second default. Pass a larger `timeout_seconds` |
| `timed_out` after a few seconds, with `provider_timeout_still_running` and an `error` about quota | The pool is out of quota; `agy` sometimes reports it this way. The `error` text gives the reset time. Use another pool until then |
| `prior_turn_uncertain` on a resume | The last turn on that session may not have stopped. See [Resuming a session](#resuming-a-session) |
| A resume ran on a different model than before | A shortcut backend such as `agy-opus` was passed on the resume. Pass plain `neon` there |
| `get_usage` with `include_quota` is slow, or a pool shows `unknown` | It starts both CLIs to read the numbers. Try again; check that the CLI is signed in |
| "must be cancelled from that window" | The job belongs to another open host window. Cancel it there |
| `owner_exited` | The host window that ran the job closed. For an edit job, check what it left, then run it again |
| `interactive_fallback` | `agy` wanted an approval. Use Codex with `mode="edit"` and `worktree=True` |
| `agy` jobs fail with `permission_error` on ordinary shell commands | `agy` is older than 1.5.7, or its `settings.json` pins a mode with `agentMode`. Update `agy`, or remove `agentMode`, so a headless run uses its default mode |
| `/bridge-usage` or `$bridge-usage` says the bridge is out of date | The bridge that host runs has no `usage_dashboard` yet. Update it as in [Update an existing install](#update-an-existing-install), then reconnect it in that host |
| The skill does not show up in a host | It is not in a folder that host reads, or the host started before it was copied. See [Install it](#install-it), then open a new chat |
| A Codex worktree job says it could not commit (`index.lock`, access denied) | Codex's sandbox refused the write to Git's own folder. The edits are in the worktree: commit them yourself |

## How it works inside

- **One process per window.** Each host window starts its own bridge over stdio. All of them, whichever CLI started them, share `bridge.db`, opened in WAL mode, so any window can list every job.
- **No second level.** A bridge started inside a bridge job refuses to start jobs. It is found by a marker variable or by a bridge process among its parents.
- **Sessions take turns.** A turn claims its session in the database before it starts. A second turn on the same session waits for the claim, in any window.
- **One deadline.** Queueing, model lookup, worktree setup, the run and any retry all share the job's single deadline.
- **Cleanup.** On Windows each subprocess is placed in a Job Object before it runs, so the whole process tree dies with the job or with the bridge. On macOS and Linux each subprocess gets its own process group, ended with SIGTERM and then SIGKILL. A job reports `stopped` only after the bridge has checked the tree is gone.
- **Closed windows.** If a window dies, its unfinished jobs are marked `failed` with `owner_exited` the next time another window looks, and the session is held until you confirm that turn is over.
- **Usage counting.** `agy` runs are read from its JSON event stream and counted per turn, so a resumed session is not counted twice. Codex and Claude Code report their own token counts for each run.
- **Models.** Shortcuts are resolved against the live `agy models` list each time, so a new model version is picked up without a change here.

## Development

```
uv sync
uv run pytest
```

The tests use stand-ins for the CLIs, a throwaway state folder, and spend no quota. The checks an agent should run, and the actions it must ask about first, are in [AGENTS.md](AGENTS.md). The code is in `src/agent_bridge/`:

| File | What it holds |
|---|---|
| `server.py` | The nine MCP tools |
| `dashboard.py` | The usage dashboard: its figures and its text view |
| `jobs.py` | Requests, queueing, sessions, waiting and cancelling |
| `runners.py` | The `agy` and `codex` command lines, worktrees, retries |
| `claude_runner.py` | The `claude -p` command line and its output |
| `nesting.py` | Finding out that a bridge runs inside a bridge job |
| `process_control.py` | Starting and stopping subprocess trees |
| `model_resolver.py` | Model shortcuts and the `agy` model list |
| `quota.py`, `usage.py` | Quota readings, advice and token counts |
| `storage.py` | The SQLite database |
| `config.py` | Environment variables and name aliases |
