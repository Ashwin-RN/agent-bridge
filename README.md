# agent-bridge

agent-bridge lets Claude Code hand work to other coding agents on your machine and get the answer back in the same chat.

It is a small local MCP server. Claude Code calls its tools, the bridge runs the `codex` CLI or the `agy` (Antigravity) CLI as a subprocess, and the reply comes back as a tool result.

```
you -> Claude Code -> agent-bridge -> codex or agy -> reply -> Claude Code -> you
```

It uses the CLI subscriptions you already have. There are no API keys, no web app and no background service: the bridge runs only while Claude Code is open.

What you can do with it:

- Ask a second model for a review, a second opinion or a piece of research.
- Run up to 10 tasks side by side on different models.
- Let Codex edit code in its own Git worktree, away from your checkout.
- Carry on a conversation with an agent you started earlier.
- See how much of each subscription's quota is left before you spend it.

## Contents

- [What you need](#what-you-need)
- [Install](#install)
- [Check it works](#check-it-works)
- [Your first tasks](#your-first-tasks)
- [Key ideas](#key-ideas)
- [Backends, models and effort](#backends-models-and-effort)
- [The eight tools](#the-eight-tools)
- [Reading a result](#reading-a-result)
- [What a subagent can touch](#what-a-subagent-can-touch)
- [Edits in a worktree](#edits-in-a-worktree)
- [Resuming a session](#resuming-a-session)
- [Timeouts and waiting](#timeouts-and-waiting)
- [Quota and usage](#quota-and-usage)
- [Optional CLAUDE.md snippet](#optional-claudemd-snippet)
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
| Claude Code (`claude`) | The client that calls the bridge. Any MCP client that runs a stdio server works too |
| `codex` CLI, installed and signed in | For the `codex` backend |
| `agy` CLI 1.5.7 or newer, installed and signed in | For the Gemini, Claude and GPT models that Antigravity offers. From 1.5.7 a headless run lets shell commands through in `agy`'s own sandbox; the bridge relies on that |
| `git` | Only for worktree edits and code review |

You need at least one of `codex` and `agy`. With only one, the other backend shows `command_found: false` in `list_backends` and its jobs fail; everything else works.

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

2. Register it with Claude Code. Use the full path to the Python inside the repo's `.venv`.

   Windows (PowerShell):

   ```powershell
   claude mcp add --scope user agent-bridge -- C:\path\to\agent-bridge\.venv\Scripts\python.exe -m agent_bridge
   ```

   macOS and Linux:

   ```bash
   claude mcp add --scope user agent-bridge -- /path/to/agent-bridge/.venv/bin/python -m agent_bridge
   ```

   `--scope user` makes it available in every project. `--scope project` keeps it to one repository.

3. Restart Claude Code, or run `/mcp` inside it and reconnect `agent-bridge`.

For another MCP client, point it at the same command: the Python in `.venv`, with the arguments `-m agent_bridge`, over stdio.

## Check it works

```
claude mcp get agent-bridge
```

Then ask Claude Code:

> Call list_backends on agent-bridge and tell me which backends are healthy.

A healthy backend has `command_found`, `healthy` and `available` all `true`, and a `version`. For `agy` the reply also lists the live models and what each shortcut resolves to today.

## Your first tasks

You talk to Claude in plain words. Claude picks the tool and the arguments. Naming the bridge and the backend helps.

| You say | Claude calls |
|---|---|
| "Ask Codex through agent-bridge whether this function has a race." | `run_agent` |
| "Start a Gemini agent in the background to map how auth works in this repo, and carry on." | `start_agent`, later `check_jobs` |
| "Get a review of my uncommitted changes from both Codex and Gemini." | `review_code` |
| "Run these three questions in parallel on agy Opus." | `run_parallel` |
| "Resume that Codex job and ask it to add tests." | `run_agent` with `resume_from` |
| "Have Codex make this refactor in an isolated worktree." | `run_agent` with `mode="edit"`, `worktree=True` |
| "How much quota is left on each pool?" | `get_usage` with `include_quota=True` |

## Key ideas

| Word | Meaning |
|---|---|
| Subagent | One run of `codex` or `agy` that the bridge starts for you |
| Backend | Which CLI runs the job: `codex` or `neon` (the name the bridge uses for `agy`) |
| Model | The model the CLI is asked to use. Shortcuts such as `neon-high` or `claude-opus` pick the newest version for you |
| Effort | How hard the model thinks: `low`, `medium`, `high`, `xhigh`. Higher is slower and uses more quota |
| Quota pool | One subscription allowance. There are three, each with its own 5 hour and weekly limit |
| Job | One task handed to the bridge. It has a `job_id` such as `codex-1a2b3c4d` |
| Session | The conversation a job belongs to, kept by the CLI. A resume adds a turn to it |
| Mode | `read_only` (the default) or `edit` |
| Worktree | A separate Git checkout on its own branch, made for one Codex edit job |
| `parent_run` | A free label you choose to group related jobs, for example `auth-review`. You can filter jobs and usage by it |
| Window | One running Claude Code. Each has its own bridge process; they share one database |

## Backends, models and effort

### Backend names

| `backend` | Runs | Model when you name none | Quota pool |
|---|---|---|---|
| `codex` (also `gpt`, `openai`) | `codex` | `gpt-6.2-sol` at `high` effort | `codex` |
| `neon` (also `agy`, `gemini`, `antigravity`) | `agy` | `neon-high` | `agy_gemini` |
| `agy-claude` (also `agy-sonnet`) | `agy` | `claude-sonnet-high` | `agy_claude_gpt` |
| `agy-opus` | `agy` | `claude-opus-high` | `agy_claude_gpt` |
| `agy-gpt` | `agy` | `gpt-oss` | `agy_claude_gpt` |

The last three are shortcuts for `neon` with a model already chosen. A `model` you pass always wins over the shortcut's own. In results the backend always reads `neon` or `codex`.

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

## The eight tools

Claude fills these in for you. The tables are here so you know what you can ask for.

### run_agent

Runs one job and waits for the reply.

| Argument | Default | What it does |
|---|---|---|
| `backend` | required | See [Backend names](#backend-names). On a resume pass plain `neon` or `codex` |
| `prompt` | required | The task |
| `cwd` | where Claude Code runs | The folder the subagent works in |
| `mode` | `read_only` | `read_only` or `edit` |
| `model` | the backend's default | A shortcut or a full id |
| `effort` | the backend's default | `low`, `medium`, `high`, `xhigh` |
| `timeout_seconds` | 300 | The job's deadline, 10 to 3600 |
| `wait_seconds` | until the job ends | How long this call waits. The job carries on if the wait ends first |
| `resume_from` | none | A `job_id` or `session_id` to continue |
| `relocate_cwd` | `False` | Move a resumed session to another folder on purpose |
| `worktree` | `False` | Codex in `edit` mode only: work in a new Git worktree |
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

The list covers jobs from every Claude Code window. A wait that ends with a job still `queued` or `running` leaves it running: call again.

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

A read-only code review by one backend or by both at once.

| Argument | Default | What it does |
|---|---|---|
| `cwd` | where Claude Code runs | The repository |
| `backends` | `["neon", "codex"]` | Who reviews |
| `scope` | `uncommitted` | `uncommitted`, `base` (the diff against a branch) or `commit` (one commit) |
| `base` | `main` | The branch for `scope="base"` |
| `commit` | none | The commit for `scope="commit"`. Required there |
| `focus` | none | What to look for, in your words |
| `neon_model`, `codex_model` | the defaults | The model for each side |
| `effort` | the default | Applied to the backends chosen |
| `wait`, `timeout_seconds`, `wait_seconds`, `parent_run` | as `run_parallel` | |

Notes:

- With no `focus`, Codex runs its own built-in review (`codex exec review`). With a `focus`, Codex gets a written review prompt instead, as `agy` always does.
- To review with Claude or GPT through `agy`, pass `backends=["neon"]` and set `neon_model`, for example `"claude-opus"`. A name such as `agy-opus` in `backends` only selects `agy`; it does not choose the model here.

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

### list_backends

Shows what the bridge found on this machine: each CLI's path, version and health, the live `agy` model list with what every shortcut resolves to, the Codex defaults, the database path, the default timeout and the concurrency cap.

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

A bad argument (an unknown backend, a folder that does not exist, `worktree=True` without `edit`) is refused as a tool error before any job is made.

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
| `permission_error` | The sandbox refused something, or `agy` denied an action it could not ask about and ended the turn with no reply | Use `edit` or `extra_dirs` if you meant to allow it. For `agy`, resume and tell it to use its file tools, or add an allow rule in `agy`'s `settings.json` |
| `cli_error` | Anything else, a missing CLI included | Read `error` and `stderr_tail`; run `list_backends` |
| `launch_error` | The subprocess could not be started safely | Read `error` |
| `cancelled` | Cancelled | For an edit job, look at `git diff` |
| `prior_turn_uncertain` | An earlier turn on this session may not have stopped | See [Resuming a session](#resuming-a-session) |
| `worktree_missing` | The session's worktree was deleted | Resume with `relocate_cwd=True` and a `cwd`, or with `worktree=True` |
| `owner_exited` | The Claude Code window that owned the job closed | Check what it did, then run it again |

| `provider_state` | Meaning |
|---|---|
| `stopped` | The subprocesses were checked and are gone |
| `possibly_running` | The bridge could not confirm that, or `agy`'s turn may go on at the server |
| `unknown` | The owning window died mid-run |

### What the bridge retries by itself

- A rejected model: once, with another model in the same pool (`agy`) or Codex's default.
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

Also true in every mode:

- The subprocess runs in `cwd` and inherits the environment of Claude Code, variables and all.
- The bridge never commits, pushes or merges.
- Your prompt and the files the subagent reads go to that CLI's provider, under your account there.
- `agy` cannot ask for an approval when run this way. An action that needs one is denied, the turn ends with no reply, and the job fails with `permission_error`.
- The bridge starts `agy` with no `--mode` on purpose. With `--mode plan` or `--mode accept-edits`, every shell command needs an approval, so most jobs ended empty.
- For unattended edits, prefer Codex with `mode="edit"` and `worktree=True`. `agy` has no worktree option.

## Edits in a worktree

`worktree=True` works on Codex in `edit` mode only.

1. Before the job starts, the bridge makes a worktree from the repository's current `HEAD` commit. Uncommitted changes in your checkout are not in it.
2. It lives at `<state dir>/worktrees/<repo name>-<hash>/<job_id>`, on a new branch `agent-bridge/<job_id>`. If `cwd` was a subfolder, the job runs in the same subfolder of the worktree.
3. The result gives `worktree_path`, `worktree_branch` and `worktree_commit`. `worktree_commit` is the commit the worktree stands on; if it is the one it started from, the edits are there but not committed.
4. A resume of that session goes back to the same worktree.

Getting the work out is yours to do:

```
git -C <worktree_path> status
git -C <worktree_path> diff
git -C <worktree_path> add -A && git -C <worktree_path> commit -m "..."   # if Codex left changes uncommitted
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
- Pass the plain backend, `neon` or `codex`. A shortcut such as `agy-opus` brings its own default model (`claude-opus-high`) and replaces the one the session was using.
- Leave `cwd` out. A resumed session stays in its recorded folder or worktree; a different `cwd` is ignored unless you also pass `relocate_cwd=True`.
- You may change the model. If that moves the session to another quota pool, `model_notice` says so.
- You cannot change the backend, and `extra_dirs` is refused on a Codex resume.
- Two turns on one session never run at once. The second waits, in any window, and the wait counts against its timeout.

If the earlier turn ended with `provider_state` `possibly_running` or `unknown`, the bridge will not resume by itself: the old turn may still be writing. The job fails with `prior_turn_uncertain`. Check that the old turn is over (for an edit job, look at `git status` and `git diff`), then resume with `confirm_prior_job_id="<that job_id>"` or `confirm_prior_stopped=True`.

## Timeouts and waiting

- `timeout_seconds` is the job's deadline: 300 seconds unless you set it, never under 10 or over 3600. The clock starts when the job is queued, so waiting for a slot or for a session uses it up. At the deadline the subprocesses are killed and the job ends `timed_out`.
- `wait_seconds` is only how long the tool call waits. When it runs out, you get the job as it stands, `queued` or `running`, and it carries on.
- 300 seconds is tight for real work. A job that reads a few large files has taken about three and a half minutes, and time spent queued comes off the same clock. Give such jobs a larger `timeout_seconds`.
- For anything over a minute or two, use `start_agent` or `run_parallel` with `wait=False`, then `check_jobs`. Claude Code may move a tool call that waits longer than about two minutes to the background; the call still finishes.
- If Claude Code cancels a waiting `run_agent`, `run_parallel` or `review_code`, the jobs it was waiting on are cancelled too. Background jobs are not.
- Each window runs at most 8 jobs at a time. More are queued.

## Quota and usage

### The three pools

| Pool | What counts against it | Where the numbers come from |
|---|---|---|
| `codex` | Every Codex job | Your Codex subscription, read through `codex app-server` |
| `agy_gemini` | Gemini models on `agy` | Antigravity's `/usage`, read through `agy` |
| `agy_claude_gpt` | Claude and GPT-OSS models on `agy` | The same reading |

Each pool has a 5 hour window and a weekly window. The Claude and GPT pool on `agy` is separate from your Claude Code subscription and from Codex.

### Reading the quota

`get_usage` with `include_quota=True` starts both CLIs to ask for their numbers. That can take 10 to 15 seconds. The reading is kept for 2 minutes and shared by every window; `refresh_quota=True` reads again. If a reading fails, that pool shows `status: "unknown"` with the error, and the rest of the reply is still good.

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

`recommended_order` lists the usable pools best first. Pools marked `avoid` or `stale` are left out of it. `upcoming_resets` lists every reset to come, soonest first.

A quota reading is a snapshot. A few large jobs can use up a 5 hour window that looked nearly full a moment ago.

### Usage

Without `include_quota`, `get_usage` reports only what the bridge itself ran: runs, outcomes, time and tokens, by backend and by pool.

- `exact` and `delta` counts go into the token totals. `partial` counts are shown apart, and `missing` means the CLI gave no figure.
- A job that was retried counts as one run with two attempts.
- Work you do in `codex` or `agy` outside the bridge is not in these counts. It does show in the quota reading.
- Codex's built-in review (`review_code` with no `focus`) reports no token counts, so it shows as 0 tokens.

## Optional CLAUDE.md snippet

Claude Code already gets short instructions from the bridge. To make it plan with the quota and keep edits in worktrees without being told each time, add this to `~/.claude/CLAUDE.md` or to a project's `CLAUDE.md`:

```markdown
## Multi-Model Subagents (`agent-bridge` MCP)
- Before substantial fan-out, call `get_usage(compact=True, cwd=<repo>, include_quota=True)` to inspect all 3 quota pools (`codex`, `agy_gemini`, `agy_claude_gpt`), upcoming model resets, and `quota_advisory.expiring_weekly_alerts` / `recommended_order`.
- Prioritize any pool flagged `use_before_reset` when its weekly quota is about to reset with unused capacity. Leverage the separate `agy_claude_gpt` pool (`backend="agy-claude"` / `"agy-opus"` / `"agy-gpt"`) when you want extra Claude/GPT capacity without drawing down Codex or Gemini limits.
- Start with one subagent for routine work: use `hydrogen-medium` / `hydrogen-high` for exploration, and `neon-medium` / `neon-high` for builds and reviews based on task complexity; use multiple backends when independent perspectives or dual reviews matter; resume the original agent (`resume_from`) when prior context matters.
- Pass `cwd` on new jobs; omit `cwd` on `resume_from` so the bridge inherits the session's recorded worktree/cwd (set `relocate_cwd=True` only to intentionally move a resumed session). Resumes inherit prior model intent, effort, and `parent_run` automatically unless overridden.
- Codex defaults to `gpt-6.2-sol` at `high` effort (override `effort` when you want `low`/`medium`/`xhigh`). Default to `mode="read_only"`; for unattended edits prefer `codex` with `mode="edit"` and `worktree=True`, and inspect the returned `effective_cwd` / `worktree_path`.
- For multi-minute work, use `start_agent` or `run_parallel(wait=False)` and poll with `check_jobs`. Treat `status="completed"` as a lifecycle status and still verify the response contains the requested deliverable. Return concise findings inline and reference longer outputs by `job_id`.
```

## Optional: see each job as an agent

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

Settings are environment variables, read once when the bridge starts. Set them when you register the server, then restart Claude Code:

```
claude mcp remove agent-bridge
claude mcp add --scope user agent-bridge -e AGENT_BRIDGE_DEFAULT_TIMEOUT=900 -- /path/to/agent-bridge/.venv/bin/python -m agent_bridge
```

| Variable | Default | What it sets |
|---|---|---|
| `AGENT_BRIDGE_NEON_MODEL` | `neon-high` | The model for `backend="neon"` when none is named |
| `AGENT_BRIDGE_CODEX_MODEL` | `gpt-6.2-sol` | The Codex model. `auto`, `latest`, `default`, `inherit` or `cli` leaves the choice to Codex |
| `AGENT_BRIDGE_CODEX_EFFORT` | `high` | The Codex effort: `low`, `medium`, `high`, `xhigh`. `inherit`, `cli`, `default` or `auto` uses `~/.codex/config.toml`. An unknown value stops the bridge from starting |
| `AGENT_BRIDGE_DEFAULT_TIMEOUT` | `300` | The job deadline in seconds when a call gives none. Kept within 10 to 3600 |
| `AGENT_BRIDGE_MAX_CONCURRENT` | `8` | Jobs running at once in one window. At least 1 |
| `AGENT_BRIDGE_MAX_STORED_JOBS` | `500` | Jobs kept in the database. At least 20 |
| `AGENT_BRIDGE_MODELS_TTL_SECONDS` | `900` | How long the `agy` model list is cached. At least 30 |
| `AGENT_BRIDGE_QUOTA_TTL_SECONDS` | `120` | How long a quota reading is cached. At least 10 |
| `AGENT_BRIDGE_OWNER_STALE_SECONDS` | `30` | How long before a silent window's jobs may be treated as abandoned. At least 5 |
| `AGENT_BRIDGE_STATE_DIR` | `~/.agent-bridge` | Where the database, the model cache and the worktrees live |
| `AGENT_BRIDGE_AGY_BIN` | found on `PATH` | The full path to `agy` |
| `AGENT_BRIDGE_CODEX_BIN` | found on `PATH` | The full path to `codex` |
| `AGENT_BRIDGE_DEFAULT_CWD` | where the bridge starts | The folder used when a call gives no `cwd` |
| `CODEX_HOME` | `~/.codex` | Where the bridge looks for Codex's `config.toml` |

## What is kept on disk

Everything is under `~/.agent-bridge` (or `AGENT_BRIDGE_STATE_DIR`):

| Path | What is in it |
|---|---|
| `bridge.db` | A SQLite database shared by every Claude Code window: jobs, sessions, usage and the last quota reading |
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

2. Check the CLIs: `agy --version` is 1.5.7 or newer, `codex --version` answers, and both are signed in. Update a CLI that is older.

3. Check the registration still points at this folder: `claude mcp get agent-bridge`. If it is missing, register it as in [Install](#install).

4. Restart Claude Code, or reconnect `agent-bridge` from `/mcp`. A running Claude Code keeps the old code until then. An agent cannot do this step for the chat it is running in: do it yourself, then open a new chat.

5. In the new chat, confirm the new code is live. Ask Claude to run this through the bridge on `backend="neon"`, `model="hydrogen-medium"`, read only, in the cloned folder:

   > Run the shell command `git log --oneline -1` and tell me what it prints, in one line.

   A current install answers with the commit line and ends with a sentence saying no file was changed. An old install comes back with no reply.

6. Optional, per project: add the lines from [Optional CLAUDE.md snippet](#optional-claudemd-snippet) and [Optional: see each job as an agent](#optional-see-each-job-as-an-agent) to the project's `CLAUDE.md`, or to a `CLAUDE.local.md` beside it if you want them kept out of Git.

Nothing in `~/.agent-bridge` needs changing on an update. The database is brought up to date by the bridge itself.

### Uninstall

```
claude mcp remove agent-bridge
```

Then remove any worktrees you no longer need (`git worktree remove`, from each repository), and delete `~/.agent-bridge` and the cloned folder.

## Troubleshooting

| What you see | Likely cause and fix |
|---|---|
| Claude Code does not list the bridge's tools | It was registered after Claude Code started. Restart, or reconnect from `/mcp`. Check the path with `claude mcp get agent-bridge` |
| `command_found: false` for a backend | That CLI is not on `PATH`. Install it, or set `AGENT_BRIDGE_AGY_BIN` or `AGENT_BRIDGE_CODEX_BIN` |
| `auth_error` | Sign in to that CLI in a terminal, then try again |
| An `agy` job comes back with one line such as "I'll start by reading the files", or with no reply | `agy` denied an action it could not ask about, most often a shell command, and ended the turn. With no reply the job now fails with `permission_error`; with a line of text it still reads `completed`. Resume it and tell it to use its file tools |
| The reply points to a report file instead of holding the answer | `agy` wrote its answer to a file in its own data folder. Open that file, or ask for the answer in the reply itself |
| `timed_out` on a job that was doing fine | The 300 second default. Pass a larger `timeout_seconds` |
| `timed_out` after a few seconds, with `provider_timeout_still_running` and an `error` about quota | The pool is out of quota; `agy` sometimes reports it this way. The `error` text gives the reset time. Use another pool until then |
| `prior_turn_uncertain` on a resume | The last turn on that session may not have stopped. See [Resuming a session](#resuming-a-session) |
| A resume ran on a different model than before | A shortcut backend such as `agy-opus` was passed on the resume. Pass plain `neon` there |
| `get_usage` with `include_quota` is slow, or a pool shows `unknown` | It starts both CLIs to read the numbers. Try again; check that the CLI is signed in |
| "must be cancelled from that window" | The job belongs to another open Claude Code. Cancel it there |
| `owner_exited` | The window that ran the job closed. For an edit job, check what it left, then run it again |
| `interactive_fallback` | `agy` wanted an approval. Use Codex with `mode="edit"` and `worktree=True` |
| `agy` jobs fail with `permission_error` on ordinary shell commands | `agy` is older than 1.5.7, or its `settings.json` pins a mode with `agentMode`. Update `agy`, or remove `agentMode`, so a headless run uses its default mode |
| A Codex worktree job says it could not commit (`index.lock`, access denied) | Codex's sandbox refused the write to Git's own folder. The edits are in the worktree: commit them yourself |

## How it works inside

- **One process per window.** Each Claude Code starts its own bridge over stdio. All of them share `bridge.db`, opened in WAL mode, so any window can list every job.
- **Sessions take turns.** A turn claims its session in the database before it starts. A second turn on the same session waits for the claim, in any window.
- **One deadline.** Queueing, model lookup, worktree setup, the run and any retry all share the job's single deadline.
- **Cleanup.** On Windows each subprocess is placed in a Job Object before it runs, so the whole process tree dies with the job or with the bridge. On macOS and Linux each subprocess gets its own process group, ended with SIGTERM and then SIGKILL. A job reports `stopped` only after the bridge has checked the tree is gone.
- **Closed windows.** If a window dies, its unfinished jobs are marked `failed` with `owner_exited` the next time another window looks, and the session is held until you confirm that turn is over.
- **Usage counting.** `agy` runs are read from its JSON event stream and counted per turn, so a resumed session is not counted twice. Codex reports its own token counts.
- **Models.** Shortcuts are resolved against the live `agy models` list each time, so a new model version is picked up without a change here.

## Development

```
uv sync
uv run pytest
```

The tests use stand-ins for the CLIs and spend no quota. The code is in `src/agent_bridge/`:

| File | What it holds |
|---|---|
| `server.py` | The eight MCP tools |
| `jobs.py` | Requests, queueing, sessions, waiting and cancelling |
| `runners.py` | The `agy` and `codex` command lines, worktrees, retries |
| `process_control.py` | Starting and stopping subprocess trees |
| `model_resolver.py` | Model shortcuts and the `agy` model list |
| `quota.py`, `usage.py` | Quota readings, advice and token counts |
| `storage.py` | The SQLite database |
| `config.py` | Environment variables and name aliases |
