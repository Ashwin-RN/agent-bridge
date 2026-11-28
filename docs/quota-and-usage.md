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

`usage(quota=True)` starts `codex` and `agy` to read their numbers. That takes about 10 to 15 seconds. Readings are cached in `bridge.db` for 2 minutes (`AGENT_BRIDGE_QUOTA_TTL_SECONDS`) and shared across windows; a cached reading whose reset time has already passed is read again automatically. Every pool is returned in one fixed shape regardless of summary filters.

Top-level fields of `quota`:

| Field | Meaning |
|---|---|
| `read_at` | Oldest ISO timestamp among the readings used, or `null` if none succeeded |
| `pools` | Always all four pools in fixed order: `codex`, `agy_gemini`, `agy_claude_gpt`, `claude` |
| `order` | Usable routable pools, best first. Excludes `claude` and any pool with priority `avoid`, `stale` or `unknown` |
| `unread` | List of `{"pool", "reason"}` for pools with no usable reading (and any unrecognised Antigravity group) |
| `summary` | One-sentence routing summary; names any pool whose read failed |

Each entry in `pools` has:

| Field | Meaning |
|---|---|
| `pool`, `backend`, `label` | Pool name, backend name and display label |
| `five_hour`, `weekly` | Window object, or `null` if the reading did not include that window |
| `priority` | Routing priority (table below) |
| `note` | Short explanation of the pool's state or why it was not read |

Each window object has `left_percent`, `resets_in` (human countdown or `null`), `reset_time` (ISO timestamp or `null`), and `state`:

| `state` | Meaning |
|---|---|
| `ok` | Active window with room left |
| `not_started` | Untouched (`100%` left; timer starts on first use) |
| `low` | 15% or less left |
| `exhausted` | 2% or less left |
| `expiring` | Unused capacity with the window resetting soon |
| `stale` | The reset timestamp has passed |

Pool `priority` values:

| `priority` | Meaning |
|---|---|
| `use_before_reset` | Weekly window resets soon with at least 20% unused (and 5h > 5%). Ranked first in `order` |
| `high` | Untouched, healthy headroom (>= 70% or ahead of pace), or expiring 5h window |
| `normal` | Moderate headroom |
| `conserve` | Running low or burning fast |
| `avoid` | Exhausted or rate limited. Left out of `order` |
| `stale` | A window's reset time has passed. Left out of `order` |
| `unknown` | Could not be read, or no reading yet. Left out of `order` |

The `claude` pool is different. Claude Code has no command that prints the plan limits, so the bridge keeps what the Claude jobs it ran reported:

- Before any Claude job has run, or once every window of that reading has reset, `five_hour` and `weekly` are `null`, `priority` is `"unknown"`, and `note` reads `"no reading: no Claude job has reported one"`. It appears in `unread` without marking the `summary` as a read failure.
- Each window is kept until its own reset passes; an expired 5h window is dropped while a current weekly window stays. A Claude job can report only a subset of windows (see [verification log](verification-log.md#claude-quota-window-reporting)). An unlisted window was not reported and is not known to be empty or full.
- The reading does not see your own Claude use since that job.
- `claude` is never listed in `order` and never marked `use_before_reset`: that allowance is shared with your own Claude Code and claude.ai sessions, so a job uses it only when it names `backend="claude"`.

A quota reading is a snapshot. A few large jobs can use up a 5 hour window that looked nearly full a moment ago.

## Usage

Without `quota=True`, `usage()` reports only what the bridge itself ran: `scope`, `totals`, `by_quota_pool` and `note`. Pass `verbose=True` for the per-pool token breakdown, `usage_coverage`, `attempts` (when retries occurred) and `partial_total_tokens`.

- `exact` and `delta` counts go into the token totals. Runs without a full count are counted in `runs_without_full_count` (and detailed in `view="dashboard"` under `not_in_totals`).
- A job that was retried counts as one run with two attempts.
- Work you do in `codex`, `agy` or `claude` outside the bridge is not in these counts. For `codex` and `agy` it does show in the quota reading.
- For `claude`, `input_tokens` includes cache reads and cache writes, and `cached_tokens` is the cache-read part. `provider_metrics.session_cost_usd_estimate` is Claude Code's own list-price estimate (on a resume it includes earlier turns). For new sessions with native subagents, the bridge sums Claude Code's per-model token counts when available and sets `provider_metrics.usage_scope` to `whole_job`. Resumed sessions use the main agent's per-turn counts. Provider metrics retain the main agent's token figures and report `native_subagents` when Claude Code supplies it, including zero.
- Codex's built-in review (`run` with `review` and no `focus` or `prompt`) reports no token counts, so it shows as 0 tokens.

## Skills

Two skills ship in `skills/`:

- `bridge-usage`: calls `usage(view="dashboard")` and shows its `text` unchanged.
- `bridge-routing`: which backend and model to pick, and how to brief and collect a job.

### The usage dashboard skill

`skills/bridge-usage/SKILL.md` works in Claude Code, Codex and Antigravity. The bridge works out every figure; the skill only picks the period and shows `text` unchanged, so the host's model has nothing to add up or round.

`agent-bridge install` offers to install `bridge-usage` automatically. For manual installation, see [Install the skills](install.md#install-the-skills).

| Host | Type |
|---|---|
| Claude Code | `/bridge-usage`, `/bridge-usage 7d`, `/bridge-usage since 2026-11-20` |
| Codex | `$bridge-usage 7d`, or pick it from `/skills` |
| Antigravity | `/bridge-usage 7d` |

With no period it shows today. `session` shows only the window you are typing in.

Notes on host behaviour (see the [verification log](verification-log.md#usage-dashboard-skill-headless-checks-2026-11-26-and-2026-11-27)):

- `codex exec` runs `usage` without an approval override because the tool is marked read-only.
- In Antigravity, a project-level entry named `agent-bridge` in `<project>/.agents/mcp_config.json` does not replace one with the same name in `~/.gemini/config/mcp_config.json`.
- Because Claude Code passes only the structured part of a tool result to its model while Codex and Antigravity pass the text part, `usage(view="dashboard")` returns one object sent as both.
