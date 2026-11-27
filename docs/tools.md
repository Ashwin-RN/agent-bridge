# The five tools

Your host fills these in for you. The tables are here so you know what you can ask for.

## Updating from the nine-tool surface

The server now exposes five tools with no aliases.

| Old call | New |
|---|---|
| `run_agent(...)` | `run(..., wait_seconds=N)` |
| `start_agent(...)` | `run(...)` |
| `run_parallel` with 3 tasks | 3 `run` calls made in parallel |
| `review_code(backends=[a, b], focus=f)` | one `run` per backend, each with `review={"focus": f}` |
| `check_jobs(...)` | `jobs(...)` |
| `cancel_job(id)` | `cancel(id)` |
| `get_usage(compact=True, include_quota=True)` | `usage(quota=True)` |
| `usage_dashboard(period)` | `usage(view="dashboard", period=...)` |
| `list_backends(include_quota=True)` | `backends()` plus `usage(quota=True)` |

## run

Starts one subagent job and answers at once with a small receipt whose `next` field is the `jobs` call that collects it. To fan out, call `run` once per job in parallel.

With `wait_seconds` (max 300), `run` waits up to that long first: a job that finishes in time returns its full job detail, and one still active returns a receipt. Waits are capped under the host's tool-call limit; a shortened wait whose job has not ended carries `wait_note`.

A host that supports the MCP tasks extension (`io.modelcontextprotocol/tasks`, such as Claude Code) receives each `run` as a background task row instead of waiting, shows the job's `line` as its status message, and delivers the finished job reply when the job ends with no polling by the model.

| Argument | Default | What it does |
|---|---|---|
| `backend` | `None` | `codex`, `neon` (`agy`), `agy-claude`, `agy-opus`, `agy-gpt` or `claude`. Required on new jobs; may be left out with `resume_from`. See [Backend names](backends-and-models.md#backend-names) |
| `prompt` | `""` | The task. Required unless `review` is set; with `review`, optional and appended to `review.focus` |
| `cwd` | `None` | Working folder (defaults to `AGENT_BRIDGE_DEFAULT_CWD` or where the bridge started). Pass on new jobs; leave out on `resume_from` unless `relocate_cwd=True` |
| `mode` | `"read_only"` | `"read_only"` or `"edit"` |
| `model` | `None` | Model id or shortcut (for `neon`: e.g. `neon-high`, `hydrogen-medium`, `flash`; for `claude`: `sonnet`, `opus`, `haiku`, `fable`). Defaults to the backend's |
| `effort` | `None` | `"low"`, `"medium"`, `"high"` or `"xhigh"`. Defaults to the backend's |
| `timeout_seconds` | `None` | The job's own deadline in seconds, clamped to 10–3600 (defaults to `AGENT_BRIDGE_DEFAULT_TIMEOUT`, 300) |
| `wait_seconds` | `0` | Wait up to this many seconds before answering (max 300, also capped by the host's limit). Ignored on MCP task calls |
| `response_file` | `False` | Save a finished non-empty reply to `<state_dir>/replies/<job_id>.md` and return a preview instead of `response`. Forwarded into `next` on a receipt |
| `resume_from` | `None` | A prior `job_id` or `session_id` to continue. Inherits `cwd`, worktree, `model`, `effort` and `parent_run` |
| `worktree` | `False` | For `codex` and `claude` in `edit` mode: give the job its own Git worktree |
| `extra_dirs` | `None` | Additional folders the subagent may use. Refused on a Codex resume |
| `label` | `None` | Short name shown in `line` and status tables |
| `parent_run` | `None` | Group label for filtering in `jobs` and `usage` |
| `review` | `None` | Read-only code review object: `scope` (`"uncommitted"`, `"base"` or `"commit"`, default `"uncommitted"`), `base` (branch for `"base"`, default `"main"`), `commit` (SHA, required for `"commit"`), `focus` (optional focus text). Must keep `mode="read_only"`, `worktree=False` and `resume_from=None`. Codex with no `focus` and empty `prompt` runs `codex exec review`; any focus or prompt runs a review prompt, as `neon` and `claude` always do |
| `relocate_cwd` | `False` | Move a resumed session to `cwd` on purpose |
| `confirm_prior_job_id` | `None` | The prior turn's `job_id`, once you have confirmed it stopped, to resume an uncertain session |
| `allow_cross_pool_fallback` | `False` | Allow an `agy` job to fall back to its other quota pool |

Returns a receipt when the job is still queued or running (or the full job detail when `wait_seconds > 0` and the job finishes in time):

```json
{
  "line": "codex · requested gpt-6.2-sol · high · queued 0s · queued · review auth · codex",
  "job_id": "codex-1a2b3c4d",
  "status": "queued",
  "backend": "codex",
  "model": "gpt-6.2-sol",
  "timeout_seconds": 300,
  "next": "jobs(job_ids=['codex-1a2b3c4d'], wait_seconds=50)"
}
```

`model` is omitted on a receipt when the bridge only knows the CLI's default.

Refused as a tool error before any job is created when:

- The bridge is running inside one of its own jobs (`nested_bridge`).
- Required inputs are missing or invalid (empty `prompt` without `review`, missing `backend` on a new job, unknown `backend`/`mode`/`effort`, non-existent `cwd` or `extra_dirs`, unknown `job_id` in `resume_from`, or a prior job with no `session_id` yet).
- Options conflict (`worktree=True` on `neon` or without `mode="edit"`, `extra_dirs` on a Codex resume, `review` combined with `mode="edit"`, `worktree=True`, `resume_from`, or `scope="commit"` without `commit`, or a resumed session's recorded worktree is missing without `relocate_cwd=True` or `worktree=True`).

## jobs

With `job_ids`: waits up to `wait_seconds` for all or any of those jobs to end and returns each in full. A job still `queued` or `running` carries on. Without `job_ids`: lists recent jobs from every window of every host.

| Argument | Default | What it does |
|---|---|---|
| `job_ids` | `None` | Job ids to wait for and return in full. Omit to list recent jobs |
| `wait_seconds` | `0` | Wait up to this long (max 300, also capped under the host's tool-call limit). Used only with `job_ids` |
| `wait_mode` | `"all"` | `"all"` waits until every found job ends; `"any"` returns when the first ends |
| `response_offset` | `0` | Character offset into the stored `response` (must be `>= 0`) |
| `response_chars` | `20000` | Characters to return per page (clamped to 1–100000) |
| `response_file` | `False` | Write each finished non-empty reply to `<state_dir>/replies/<job_id>.md` and return `response_file`, `response_head` and `response_total_chars` |
| `verbose` | `False` | Add full token breakdown, `provider_metrics` and multi-attempt `attempts` |
| `status` | `None` | List-mode filter: `queued`, `running`, `completed`, `failed`, `cancelled` or `timed_out` |
| `backend` | `None` | List-mode filter by backend |
| `cwd` | `None` | List-mode filter by working folder |
| `parent_run` | `None` | List-mode filter by `parent_run` |
| `limit` | `20` | Maximum rows returned in list mode |

With `job_ids`, returns `jobs`, `all_done`, `any_done`, and `wait_note` (only when the wait was shortened and the wait condition was not met). An unknown id appears in `jobs` with `status: "not_found"` and an `error`.

```json
{
  "jobs": [
    {
      "line": "codex · requested gpt-6.2-sol · high · completed 1m12s · review auth · codex",
      "job_id": "codex-1a2b3c4d",
      "status": "completed",
      "backend": "codex",
      "model": "gpt-6.2-sol",
      "model_source": "requested",
      "effort": "high",
      "quota_pool": "codex",
      "mode": "read_only",
      "session_id": "019a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
      "response": "No race found in ...",
      "usage": {"total_tokens": 3692345}
    }
  ],
  "all_done": true,
  "any_done": true
}
```

Without `job_ids`, returns `{"count": N, "jobs": [...]}` where each entry has `line`, `job_id`, `status`, `prompt_preview`, and (on non-completed terminal jobs) `failure_reason`.

Refused as a tool error when `response_offset < 0` or a list-mode `status` or `backend` filter is unknown.

## cancel

Stops a queued or running job that this window started, terminates its subprocess tree, and returns its job detail.

| Argument | Default | What it does |
|---|---|---|
| `job_id` | required | The job to stop |

- A job owned by this window is stopped and returned as `cancelled`. If `agy` had already reported that its server-side turn may still be running, `provider_state` stays `"possibly_running"` and resuming that session requires `confirm_prior_job_id`.
- A job whose owning window has closed is marked `failed` with `owner_exited` and returned.
- A job that already ended is returned unchanged.

```json
{
  "line": "codex · requested gpt-6.2-sol · high · cancelled 4s · codex",
  "job_id": "codex-1a2b3c4d",
  "status": "cancelled",
  "backend": "codex",
  "model": "gpt-6.2-sol",
  "model_source": "requested",
  "effort": "high",
  "quota_pool": "codex",
  "mode": "read_only",
  "response": "",
  "usage_status": "missing",
  "failure_reason": "cancelled",
  "provider_state": "stopped",
  "retry_guidance": "Subprocess tree was verified stopped. For edit tasks inspect git status/diff before retrying.",
  "error": "Job cancelled."
}
```

Refused as a tool error when `job_id` does not exist or belongs to another live bridge window ("must be cancelled from that window").

## usage

Reports what the bridge ran in a period, and optionally what subscription quota is left across every pool.

| Argument | Default | What it does |
|---|---|---|
| `period` | `None` | `"today"` (default when `since`/`until` are omitted), `"7d"`, `"30d"`, `"all"` or `"session"` (this window only) |
| `since` | `None` | Local date (`YYYY-MM-DD`) or ISO time (`YYYY-MM-DDTHH:MM`). Cannot be combined with `period` |
| `until` | `None` | End bound. A date alone includes that whole day. Cannot be combined with `period` |
| `backend` | `None` | Filter `view="summary"` by backend |
| `cwd` | `None` | Filter `view="summary"` by working folder |
| `parent_run` | `None` | Filter `view="summary"` by `parent_run` |
| `quota` | `False` | Add `quota` with all four pools in one shape and their routing `order` (about 10 s when read live; call before a fan-out) |
| `view` | `"summary"` | `"summary"` returns compact period totals and `by_quota_pool`; `"dashboard"` returns `text` ready to show unchanged (reads without writing to the database or quota cache) |
| `verbose` | `False` | On `"summary"`, adds per-pool `tokens`, `usage_coverage`, `attempts` (when different from `runs`) and `partial_total_tokens`. On `"dashboard"`, adds the dashboard's structured figures (without duplicating `quota`) |

Example reply for `usage(period="today", quota=True)`:

```json
{
  "scope": {"period": "today", "from": "2026-11-27T00:00:00+05:30"},
  "totals": {"runs": 2, "completed": 2, "duration_seconds": 48.5, "total_tokens": 140200},
  "by_quota_pool": {
    "codex": {"runs": 2, "completed": 2, "duration_seconds": 48.5, "total_tokens": 140200}
  },
  "note": "Tokens are counted for finished runs whose CLI reported an exact or per-turn count; runs_without_full_count says how many are left out.",
  "quota": {
    "read_at": "2026-11-27T21:40:00+00:00",
    "pools": [
      {
        "pool": "codex",
        "backend": "codex",
        "label": "Codex CLI",
        "five_hour": {"left_percent": 71, "resets_in": "2h 16m", "reset_time": "2026-11-28T00:00:00+00:00", "state": "ok"},
        "weekly": {"left_percent": 30, "resets_in": "1d 3h", "reset_time": "2026-11-29T00:44:00+00:00", "state": "expiring"},
        "priority": "use_before_reset",
        "note": "30% of the week unused, resets in 1d 3h"
      }
    ],
    "order": ["codex", "agy_claude_gpt", "agy_gemini"],
    "unread": [],
    "summary": "Use codex first (30% weekly unused, resets in 1d 3h)."
  }
}
```

With `view="dashboard"`, the reply is `{"text": "..."}` (plus the dashboard figures when `verbose=True`, and `quota` when `quota=True`). The dashboard opens with all four pools' 5h and weekly windows, followed by runs, outcomes, time and tokens by backend, pool and model from the bridge's own records. Totals include only finished runs with an `exact` or `delta` count; everything else is listed apart under `not_in_totals`:

| Entry in `not_in_totals` | What it is |
|---|---|
| `running_or_queued` | Jobs still active in open windows, with any tokens already recorded for earlier attempts |
| `partial_counts` | A count for a turn that did not finish cleanly; may be short |
| `cli_started_no_count` | The CLI started but left no count (cancelled, timed out, failed, or Codex's built-in review) |
| `cannot_tell` | Runs the records cannot speak for: `window_closed`, `job_row_pruned`, or `end_not_confirmed` |
| `claude_subagents` | Claude runs whose reported count covers only the main agent while native subagents ran |
| `claude_subagents_not_on_record` | Claude runs that did not record whether native subagents ran |
| `before_records_begin` | Set when the period reaches before the bridge's first record |

See [Quota and usage](quota-and-usage.md) for the full `quota` shape and pool rules.

Refused as a tool error when:

- Both `period` and `since`/`until` are passed.
- `period` is unknown, `since` or `until` is not a valid date/ISO time, or `since >= until`.
- `backend`, `cwd` or `parent_run` is passed with `view="dashboard"`.
- `backend` is unknown.

## backends

Checks each CLI's health and version, reports backend defaults, `agy`'s live models and shortcuts, the detected `host` and its `wait_cap` in seconds, and whether the bridge is nested.

| Argument | Default | What it does |
|---|---|---|
| `refresh_models` | `False` | Read `agy models` again instead of using the 15-minute cache |

```json
{
  "host": "claude-code",
  "wait_cap": null,
  "default_cwd": "C:\\work\\repo",
  "default_timeout_seconds": 300,
  "max_concurrent_jobs_per_process": 8,
  "db_path": "C:\\Users\\me\\.agent-bridge\\bridge.db",
  "nesting": {"nested": false},
  "backends": {
    "neon": {"backend": "neon", "cli": "agy", "default_intent": "neon-high", "command_found": true, "healthy": true, "available": true, "version": "1.5.18"},
    "codex": {"backend": "codex", "cli": "codex", "default_model": "gpt-6.2-sol", "default_effort": "high", "command_found": true, "healthy": true, "available": true, "version": "codex-cli 0.172.0"},
    "claude": {"backend": "claude", "cli": "claude", "quota_pool": "claude", "default_model": "sonnet", "default_effort": "high", "command_found": true, "healthy": true, "available": true, "version": "2.1.332 (Claude Code)"}
  }
}
```

A missing or broken CLI does not fail the tool call; its entry sets `command_found`, `healthy` or `available` to `false` with an `error`.

# Reading a job reply

## Status

| `status` | Meaning |
|---|---|
| `queued` | Waiting for a concurrency slot or for an earlier turn on the same session |
| `running` | The CLI is working |
| `completed` | The CLI exited cleanly and emitted its final event |
| `failed` | It did not. Read `failure_reason` and `error` |
| `timed_out` | The job's deadline passed |
| `cancelled` | The job was cancelled |

`completed` only means the process ended cleanly, not that the job did what you asked. A job that completes with an empty reply carries a `warning`. Always read `response`.

## Job detail fields

Null fields are omitted.

| Field | Meaning |
|---|---|
| `line` | One-line summary: backend, model, effort, status and duration, current activity (while running), label, pool |
| `job_id`, `status`, `backend`, `mode`, `quota_pool` | Job identity and mode |
| `model`, `model_source`, `requested_model`, `model_notice` | Model name (omitted when only the CLI's default is known), how it is known (`observed`, `requested` or `default`), the requested name when it differs, and any fallback or billing notice |
| `effort`, `session_id`, `parent_run` | Effort level, resumable session id, and group label |
| `effective_cwd` | Included only when different from the requested `cwd` (such as inside a worktree) |
| `worktree_path`, `worktree_branch` | Present on worktree jobs |
| `response` | Paged reply text (default up to 20,000 chars, max 100,000). Omitted when `response_file=True` writes a file |
| `response_offset`, `response_truncated`, `response_total_chars`, `next_offset`, `response_note` | Present when a reply is paged or was cut at the 200,000-character storage cap |
| `response_file`, `response_head`, `response_total_chars` | Returned instead of `response` when `response_file=True` wrote the full stored reply to `<state_dir>/replies/<job_id>.md`; `response_head` is the first 400 characters |
| `warning` | Present on an empty completed reply, or when `response_file=True` fell back to a paged reply |
| `usage`, `usage_status` | `{"total_tokens": N}` by default (full token breakdown with `verbose=True`). `usage_status` is omitted when `"exact"` and shown for `"delta"`, `"partial"` or `"missing"` |
| `failure_reason`, `provider_state`, `retry_guidance`, `error`, `exit_code`, `cleanup_seconds`, `stderr_tail` | Present on non-completed terminal jobs |
| `provider_metrics`, `attempts` | Included only with `verbose=True` (`attempts` when more than one attempt ran) |

## Paging and reply files

Replies are stored up to 200,000 characters in `bridge.db` and returned in pages of `response_chars` (default 20,000, max 100,000) starting at `response_offset`. When more text remains, the reply sets `response_truncated: true`, `response_total_chars`, `next_offset`, and a `response_note` with the next `jobs` call. If the CLI produced more than 200,000 characters, the final page notes that only the first 200,000 were kept.

Pass `response_file=True` on `run` or `jobs` to keep a long reply out of the conversation. On a finished job with a non-empty reply, the bridge writes the full stored reply atomically to `<state_dir>/replies/<job_id>.md` and returns `response_file`, `response_head` (first 400 characters) and `response_total_chars`. If the file cannot be written, it falls back to the normal paged reply with a single-line `warning`. Running jobs and empty replies write no file.

## Failure reasons

| `failure_reason` | Meaning | What to do |
|---|---|---|
| `timeout` | The job's deadline passed, queue and session wait included | Raise `timeout_seconds` |
| `provider_timeout_still_running` | `agy` reported its server-side turn may still be running | Read `error` (sometimes quota), wait until it stops, then resume with `confirm_prior_job_id` |
| `quota_exceeded` | The pool is out of quota or rate limited | Call `usage(quota=True)` and use another pool |
| `auth_error` | The CLI is not signed in | Sign in to that CLI again (or add an API key variable to `AGENT_BRIDGE_CHILD_ENV_KEEP` if using key auth) |
| `model_unavailable` | The model was rejected, or the pool has no model to offer | Call `backends(refresh_models=True)` or pick another model |
| `transient_service` | A network or server error | Retry a read-only task; inspect `git status`/`git diff` first on an edit task |
| `interactive_fallback` | `agy` fell back to interactive approval prompts | Use `codex` with `mode="edit"` and `worktree=True` |
| `permission_error` | The sandbox denied an action, or `agy`/`claude` was denied an action and ended with no reply | Use `mode="edit"` or `extra_dirs` if writes were intended, or tell the agent to use file tools |
| `cli_error` | Any other CLI failure, missing CLI, a session held by another job, or a stream that ended without its final event | Read `error` and `stderr_tail`, check `backends`, and inspect `git diff` on edit tasks |
| `launch_error` | The subprocess could not be started safely | Read `error` |
| `cancelled` | The job was cancelled | Inspect `git status`/`git diff` on edit tasks |
| `prior_turn_uncertain` | An earlier turn on this session ended with `possibly_running` or `unknown` | Confirm that turn ended and pass `confirm_prior_job_id` |
| `worktree_missing` | The resumed session's recorded worktree no longer exists | Resume with `relocate_cwd=True` and `cwd`, or with `worktree=True` and `mode="edit"` |
| `owner_exited` | The host window that started the job closed before it finished | Inspect what it left, then run again |

A stream that exits 0 without its final event fails with `failure_reason: "cli_error"`, keeps any partial `response`, and is never auto-retried. The exact `error` text is:

- Codex (outside built-in plain-text review): `the CLI's output ended without its final event (turn.completed), so the run may have been cut short; any partial reply is kept`
- `agy`: `the CLI's output ended without its final event (a result event with a status), so the run may have been cut short; any partial reply is kept`
- Claude: `claude ended without a result event; its output was cut short`

| `provider_state` | Meaning |
|---|---|
| `stopped` | Subprocess tree was verified gone |
| `possibly_running` | Termination could not be verified, or `agy` reported the server-side turn may still be running |
| `unknown` | The owning window exited mid-run |

## What the bridge retries by itself

- A rejected model: once, with another model in the same pool (`agy`) or Codex's default. Not on `claude`.
- A `transient_service` error: once, only for a new `read_only` job that made no progress.
- Nothing else: no auto-retry on timeouts, quota, auth, cut-short streams, resumed sessions, or any `edit` job.
