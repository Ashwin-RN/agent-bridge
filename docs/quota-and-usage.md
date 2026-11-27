# Quota and usage

## The four pools

| Pool | What counts against it | Where the numbers come from |
|---|---|---|
| `codex` | Every Codex job | Your Codex subscription, read through `codex app-server` |
| `agy_gemini` | Gemini models on `agy` | Antigravity's `/usage`, read through `agy` |
| `agy_claude_gpt` | Claude and GPT-OSS models on `agy` | The same reading |
| `claude` | Every job on the `claude` backend | Your Claude plan. Not read live: see below |

Each pool has a 5 hour window and a weekly window. The Claude and GPT pool on `agy` is separate from your Claude plan and from Codex.

## Reading the quota

`get_usage` with `include_quota=True` starts `codex` and `agy` to ask for their numbers. That can take 10 to 15 seconds. The reading is kept for 2 minutes and shared by every window; `refresh_quota=True` reads again. If a reading fails, that pool shows `status: "unknown"` with the error, and the rest of the reply is still good.

The `claude` pool is different. Claude Code has no command that prints the plan limits, so the bridge keeps what the last Claude job's output carried:

- Before any Claude job has run, or once every window of that reading has reset, the pool shows `status: "unknown"` with a note. A window whose reset has passed is dropped on its own; the others stay.
- After a job, it shows the windows that job reported, with `source: "last_claude_job"`. A Claude job can report only a subset of windows (see [verification log](verification-log.md#claude-quota-window-reporting)). A window that is not listed was not reported; it is not known to be empty or full.
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

## Usage

Without `include_quota`, `get_usage` reports only what the bridge itself ran: runs, outcomes, time and tokens, by backend and by pool.

- `exact` and `delta` counts go into the token totals. `partial` counts are shown apart, and `missing` means the CLI gave no figure.
- A job that was retried counts as one run with two attempts.
- Work you do in `codex`, `agy` or `claude` outside the bridge is not in these counts. For `codex` and `agy` it does show in the quota reading.
- For `claude`, `input_tokens` includes cache reads and cache writes, and `cached_tokens` is the cache-read part. `provider_metrics.session_cost_usd_estimate` is Claude Code's own list-price estimate. On a resume the estimate includes earlier turns. For new sessions with native subagents, the bridge sums Claude Code's per-model token counts when available and sets `provider_metrics.usage_scope` to `whole_job`. Resumed sessions use the main agent's per-turn counts. Provider metrics retain the main agent's token figures and report the number of native subagents in `native_subagents` when Claude Code supplies it, including zero.
- Codex's built-in review (`review_code` with no `focus`) reports no token counts, so it shows as 0 tokens.

## The usage dashboard skill

`skills/bridge-usage/SKILL.md` is a skill that calls [`usage_dashboard`](tools.md#usage_dashboard) and shows its `text` unchanged. The same file works in Claude Code, Codex and Antigravity. The bridge works out every figure; the skill only picks the period and shows the answer, so the host's model has nothing to add up or round.

`agent-bridge install` offers to install the skill automatically. For manual installation, see [Install the usage dashboard skill](install.md#install-the-usage-dashboard-skill).

### Use it

| Host | Type |
|---|---|
| Claude Code | `/bridge-usage`, `/bridge-usage 7d`, `/bridge-usage since 2026-11-20` |
| Codex | `$bridge-usage 7d`, or pick it from `/skills` |
| Antigravity | `/bridge-usage 7d` |

With no period it shows today. `session` shows only the window you are typing in.

Notes on host behaviour (see the [verification log](verification-log.md#usage-dashboard-skill-headless-checks-2026-11-26-and-2026-11-27)):

- `codex exec` runs `usage_dashboard` without an approval override because the tool is marked read-only.
- In Antigravity, a project-level entry named `agent-bridge` in `<project>/.agents/mcp_config.json` does not replace one with the same name in `~/.gemini/config/mcp_config.json`.
- Because Claude Code passes only the structured part of a tool result to its model while Codex and Antigravity pass the text part, `usage_dashboard` returns one object sent as both.
