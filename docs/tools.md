<!-- TOOLS: this page describes the tool surface before the five-tool change; it is rewritten in the final pass -->

# The nine tools

Your host fills these in for you. The tables are here so you know what you can ask for.

## run_agent

Runs one job and waits for the reply.

| Argument | Default | What it does |
|---|---|---|
| `backend` | required | See [Backend names](backends-and-models.md#backend-names). On a resume pass plain `neon`, `codex` or `claude`, or leave it out |
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
| `confirm_prior_stopped`, `confirm_prior_job_id` | off | See [Resuming a session](troubleshooting.md#resuming-a-session) |
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

## start_agent

The same as `run_agent`, but it returns at once with a `job_id` and the job runs in the background. It takes every `run_agent` argument except `wait_seconds`. Collect the reply with `check_jobs`.

```json
{"job_id": "neon-5e6f7a8b", "backend": "neon", "status": "queued", "mode": "read_only", "timeout_seconds": 300,
 "hint": "Call check_jobs(job_ids=['neon-5e6f7a8b'], wait_seconds=60) to wait for results."}
```

## run_parallel

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

## check_jobs

Lists recent jobs, or waits for named ones.

| Argument | Default | What it does |
|---|---|---|
| `job_ids` | none | With ids: wait for these jobs and return each in full. Without: return a short list of recent jobs |
| `wait_seconds` | 0 | How long to wait, at most 300 |
| `wait_mode` | `all` | `all` waits for every job, `any` returns when the first one ends |
| `status`, `backend`, `cwd`, `parent_run`, `limit` | none, `limit` 20 | Filters for the list. Ignored when `job_ids` is given |

The list covers jobs from every window of every host. A wait that ends with a job still `queued` or `running` leaves it running: call again.

## cancel_job

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

## review_code

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

## get_usage

What the bridge has run, and optionally how much quota is left.

| Argument | Default | What it does |
|---|---|---|
| `period` | `today` | `session` (this window only), `today`, `7d`, `30d`, `all`. All but `session` count every window |
| `backend`, `quota_pool`, `cwd`, `parent_run` | none | Filters |
| `compact` | `False` | A short planning view: no per-model table and no recent list |
| `include_quota` | `False` | Also read the live quota of each pool. See [Quota and usage](quota-and-usage.md) |
| `refresh_quota` | `False` | Skip the cache and read the quota again |
| `recent_limit` | 10 | How many recent runs to list |

## usage_dashboard

What is left of each subscription, then what the bridge has run in a period.

The view opens with the quota left in every pool: Codex, Antigravity's two pools and the Claude plan, each with the 5h and weekly windows its reading holds, as a bar with the percent left, its reset and the time it was read. A window the reading does not hold says `no reading`; a Claude job can report one window only. Those readings come from each CLI, as [`get_usage`](#get_usage) with `include_quota` reads them, and take about 10 seconds. Below it is everything the bridge has run in the period, by quota pool and model, read from the bridge's own records and nothing else.

It only reads: it writes nothing to the database, and a quota reading it takes is not put in the cache. It also answers in a bridge that runs inside a bridge job. The [usage dashboard skill](quota-and-usage.md#the-usage-dashboard-skill) is the easy way to call it.

| Argument | Default | What it does |
|---|---|---|
| `period` | `today` | `today` (since local midnight), `7d`, `30d`, `all`, or `session` (this window only) |
| `since`, `until` | none | Instead of `period`: a local date such as `2026-11-25`, or an ISO time such as `2026-11-25T14:30`. A date alone as `until` takes in that whole day. Giving both `period` and one of these is refused |

What the reply holds:

- `text`: the whole view as fixed-width text, ready to show as it is. It is rendered from the same figures as the rest of the reply. A caveat is shown only when there is something under it; the rest of the reply always holds every entry.
- `quota`: what is left in every pool, in a fixed order: `codex`, `agy_gemini`, `agy_claude_gpt`, `claude`. A pool that was read has `five_hour` and `weekly` (each with `remaining_percent`, `resets_in`, `reset_time`, `read_at` and a `state` of `ok`, `not_started`, `exhausted` or `reset_passed`; `None` when the CLI reported no such window), `as_of` and `read_at` (when it was read: the time of its oldest window), `source` (`live`, `cached`, or `last_claude_job`) and `priority`. A pool that could not be read, or whose reading holds neither window, has status `unknown` and a `reason`; one backend failing does not hide the others. `notes` holds the lines worth acting on, such as a week about to reset with quota unused. The period does not change this section.
- `covers` and `not_covered`: it covers jobs run through the bridge only. Not in it: the host session's own tokens, a host's own subagents, and CLI runs made outside the bridge. The view says so in its footer.
- `totals`: runs, their outcomes, time and tokens.
- `this_session` and `other_sessions`: the same figures split between the calling window and every other window. The two add up to `totals`.
- `by_backend`, `by_quota_pool`, `by_model`: each adds up to `totals`. Every backend and pool is listed, with zeros where nothing ran. Each model carries its `quota_pool`.
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
- The quota readings are not bridge records, and no figure from them is added to a total. The Claude plan has no live read-out: its reading is what the Claude jobs run through the bridge reported. Each window is kept until its own reset passes and carries the time it was read, so the week stays on show after the 5h window has reset. The pool says `no reading` when no window is left.
- Left out on purpose: Claude Code's cost estimate, which is not a bill and repeats earlier turns on a resume.

## list_backends

Shows what the bridge found on this machine: each CLI's path, version and health, the live `agy` model list with what every shortcut resolves to, the Codex and Claude defaults, the database path, the default timeout, the concurrency cap, and `nesting` (whether this bridge is itself running inside a bridge job).

| Argument | Default | What it does |
|---|---|---|
| `refresh_models` | `False` | Read `agy models` again instead of using the cache |
| `include_quota` | `False` | Add the quota reading and advice |

# Reading a result

## Status

| `status` | Meaning |
|---|---|
| `queued` | Waiting for a free slot, or for an earlier turn on the same session |
| `running` | The CLI is working |
| `completed` | The CLI exited cleanly |
| `failed` | It did not. Read `failure_reason` and `error` |
| `timed_out` | The deadline passed |
| `cancelled` | You cancelled it |

`completed` means the process ended well, not that the job did what you asked. A job can complete with a short or empty `response`. Always read the reply.

## Fields

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

A bad argument (an unknown backend, a folder that does not exist, `worktree=True` without `edit`) is refused as a tool error before any job is made. The error carries the reason in words, for example `Unknown backend 'bogus'. Expected one of: ...`. A bridge that runs inside a bridge job refuses the job-starting tools the same way, with `nested_bridge`: see [A bridge inside a bridge](safety.md#a-bridge-inside-a-bridge).

## When a job fails

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
| `prior_turn_uncertain` | An earlier turn on this session may not have stopped | See [Resuming a session](troubleshooting.md#resuming-a-session) |
| `worktree_missing` | The session's worktree was deleted | Resume with `relocate_cwd=True` and a `cwd`, or with `worktree=True` |
| `owner_exited` | The host window that owned the job closed | Check what it did, then run it again |

| `provider_state` | Meaning |
|---|---|
| `stopped` | The subprocesses were checked and are gone |
| `possibly_running` | The bridge could not confirm that, or `agy`'s turn may go on at the server |
| `unknown` | The owning window died mid-run |

## What the bridge retries by itself

- A rejected model: once, with another model in the same pool (`agy`) or Codex's default. Not on `claude`.
- A network or server error: once, only for a new `read_only` job that had made no progress.
- Nothing else. No retry on a timeout, on quota, on sign-in, or on any `edit` job.
