# agent-bridge

Lightweight local stdio MCP server that connects **Claude Code** (or any MCP client) to **Antigravity (`agy` — Gemini Neon, Hydrogen, Flash)** and **Codex (`codex` — GPT)** subagents over your existing CLI subscriptions.

No API keys. No separate UI or web app. Shared SQLite state across multiple Claude windows, accurate per-turn usage & opt-in live quota checks, dynamic effort-preserving model resolution, and single-owner process tree cleanup.

---

## Quick Setup (For Humans & AI Agents)

If you are an AI agent setting this up after a fresh `git clone` or `git pull`, run these steps in order.

### 1. Check prerequisites

```powershell
# Windows (PowerShell)
uv --version
claude --version
agy --version
codex --version
```

```bash
# macOS / Linux
uv --version && claude --version && (agy --version || true) && (codex --version || true)
```

### 2. Install / update dependencies

From the root of this repository (`<REPO_DIR>`):

```powershell
cd <REPO_DIR>
uv sync
```

*(After any future `git pull`, re-run `uv sync`.)*

### 3. Register with Claude Code (user scope — available in every project)

**Windows (PowerShell):**

```powershell
claude mcp add --scope user agent-bridge -- <REPO_DIR>\.venv\Scripts\python.exe -m agent_bridge
```

Example when cloned at `C:\Users\<you>\agent-bridge`:

```powershell
claude mcp add --scope user agent-bridge -- C:\Users\<you>\agent-bridge\.venv\Scripts\python.exe -m agent_bridge
```

**macOS / Linux:**

```bash
claude mcp add --scope user agent-bridge -- <REPO_DIR>/.venv/bin/python -m agent_bridge
```

*(Use `--scope project` instead of `--scope user` to register for a single repository.)*

### 4. Verify the integration

```powershell
uv run pytest
claude mcp get agent-bridge
```

Inside Claude Code, ask:
> *"Check `list_backends` on agent-bridge and confirm Neon and Codex are healthy."*

---

## Seamless Model Updates & Three Independent Quota Pools

- **Codex (`codex` pool)**:
  - Defaults to **`gpt-6.2-sol` at `high` reasoning effort** as the norm (so global Claude Code sessions don't accidentally inherit `xhigh` from `~/.codex/config.toml`).
  - Automatically rides strictly newer versions in `~/.codex/config.toml` (e.g. `gpt-6.3-sol` > `gpt-6.2-sol`), and if a pinned model is ever retired/rejected, falls back seamlessly to Codex's current default with a `model_notice`.
  - Override per call via `effort="low" | "medium" | "high" | "xhigh"` or `model="latest"`, or configure `AGENT_BRIDGE_CODEX_MODEL` / `AGENT_BRIDGE_CODEX_EFFORT` (`inherit` defers to `~/.codex/config.toml`).
- **Antigravity Gemini Models (`agy_gemini` pool)**:
  - Defaults to `neon-high` and resolves against live `agy models` (cached in `~/.agent-bridge/agy_models_cache.json`).
  - **Preserves requested effort tier by default** (`neon-medium`, `neon-high`, `neon-xhigh`, `hydrogen`, `flash`). Any tier substitution or retired-version upgrade is disclosed in `model_notice`, with automatic catalog refresh & retry on `model_unavailable`.
- **Antigravity Claude & GPT Models (`agy_claude_gpt` pool)**:
  - Antigravity provides a **separate 5-hour + weekly quota bucket** (`Claude and GPT models`) independent from both Gemini Models and your Claude Code / Codex subscriptions.
  - Delegate directly using `backend="agy-claude"` (Sonnet), `backend="agy-opus"` (Opus), or `backend="agy-gpt"` (GPT-OSS), or pass `model="claude-sonnet-high" | "claude-opus-high" | "gpt-oss"` on `backend="neon"`. Versionless shortcuts auto-upgrade as newer Claude/GPT versions appear in `agy models`.

---

## Usage Planning, Pool Tracking & Weekly-Reset Awareness

- **Accurate per-turn & per-pool usage**:
  - Antigravity runs use `agy --output-format stream-json` so per-turn token usage is summed directly from the current turn's `step_update` events (preventing double-counting on resumed sessions), with wall-clock duration measured per invocation.
  - Usage is tracked and aggregated directly in SQLite by both `backend` and `quota_pool` (`agy_gemini`, `agy_claude_gpt`, `codex`), preserving provider-specific cache counters and `usage_coverage` (`exact`, `delta`, `partial`, `missing`).
- **Weekly-reset-aware quota advisory**:
  - `get_usage(compact=True, cwd=..., parent_run=..., include_quota=True)` returns live (briefly cached) quota windows for all 3 pools plus `quota_advisory`:
    - Exact reset countdowns (`resets_in`, `reset_time`, `time_remaining_percent`) and `upcoming_resets` across all involved models.
    - `expiring_weekly_alerts` and `priority: "use_before_reset"` whenever a pool has meaningful unused weekly quota nearing reset so Claude can leverage expiring capacity before it rolls over.
    - `recommended_order` ranking pools across `use_before_reset` -> `high` (including untouched `100%` pools like `agy_claude_gpt`) -> `normal` -> `conserve` -> `avoid`.
  - If a quota probe or parser fails, `get_usage` still succeeds and returns `status: "unknown"` with timestamp and error details.

---

## Reliability & Stuck-Job Handling

- **Multi-window SQLite store (`~/.agent-bridge/bridge.db`)**: Per-job updates with WAL mode, unified OS-PID dead-owner checks, atomic `session_claims`, direct SQL usage aggregation, and automatic one-time import of legacy `jobs.json` / `usage.jsonl` on upgrade. Completed job records are released from memory once persisted to SQLite.
- **Per-process concurrency cap**: `AGENT_BRIDGE_MAX_CONCURRENT` applies **per bridge process** (per Claude window).
- **Atomic per-session serialization**: Turns targeting the same `(backend, session_id)` acquire an atomic SQLite claim (preventing cross-window deadlocks and same-window Turn 1/Turn 2 overlaps), and queued resumes refresh the prior turn's final `effective_cwd` and worktree path right before launch.
- **Pre-assigned worktrees & resume safety**:
  - Codex `worktree=True` requires `mode="edit"`, pre-assigns a dedicated Git worktree and branch (`agent-bridge/<job_id>`) per job under `~/.agent-bridge/worktrees/` before launch, and persists `effective_cwd`, `worktree_path`, `worktree_branch`, and `worktree_commit` so parallel edits never collide.
  - Unsupported resume options (such as `extra_dirs` on Codex resume) are rejected explicitly instead of silently dropped.
  - Native `codex exec review` explicitly passes `-c sandbox_mode="read-only"`.
- **Single overall deadline & verified cleanup**:
  - Queueing, model discovery, worktree provisioning, execution, and any retry share one strict `timeout_seconds` deadline (defaulting to `AGENT_BRIDGE_DEFAULT_TIMEOUT` when omitted).
  - For multi-minute work, use `start_agent`, `run_parallel(wait=False)`, or a shorter `wait_seconds` on `run_agent`, then poll with `check_jobs`. When a wait ends with `queued`/`running`, the job stays active; if a synchronous `run_agent` call is cancelled by the client, its underlying job is cancelled cleanly.
  - Every subprocess (including `agy models`, `git worktree`, and runner processes) cleans up on all exit paths (timeout, cancellation, or internal callback errors) using Windows Job Objects (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) / POSIX process groups, and verifies exit before reporting `provider_state: "stopped"`. Failures return machine-readable `failure_reason`, `provider_state` (`stopped` / `possibly_running` / `unknown`), partial `response` + `session_id`, and `retry_guidance`.

---

## MCP Tools (8)

| Tool | Purpose |
|---|---|
| `run_agent` | Run Neon or Codex synchronously (supports `effort`, `resume_from`, `mode`, `worktree`, `parent_run`, and optional `wait_seconds` separate from `timeout_seconds`) |
| `start_agent` | Launch a subagent in the background and return `job_id` immediately |
| `run_parallel` | Fan out up to 10 tasks concurrently (`wait=True` or background `wait=False`) |
| `check_jobs` | List/filter jobs across windows, fetch results, or wait (`wait_seconds`, `wait_mode="all" \| "any"`) |
| `cancel_job` | Cancel a queued/running job owned by this bridge process and terminate its subprocess tree |
| `review_code` | Read-only code review (`uncommitted`, vs `base`, or `commit`) via Neon, Codex, or both |
| `get_usage` | Per-backend usage, `in_flight` load, coverage, optional `cwd`/`parent_run` scope, `compact=True` view, and opt-in `include_quota=True` |
| `list_backends` | Executable discovery, `--version` health check, live/cached/stale Neon catalog status, Codex local config defaults, and session usage |

---

## Optional `CLAUDE.md` Snippet

Drop this into `~/.claude/CLAUDE.md` (or a project's `CLAUDE.md`):

```markdown
## Multi-Model Subagents (`agent-bridge` MCP)
- Before substantial fan-out, call `get_usage(compact=True, cwd=<repo>, include_quota=True)` to inspect all 3 quota pools (`codex`, `agy_gemini`, `agy_claude_gpt`), upcoming model resets, and `quota_advisory.expiring_weekly_alerts` / `recommended_order`.
- Prioritize any pool flagged `use_before_reset` when its weekly quota is about to reset with unused capacity. Leverage the separate `agy_claude_gpt` pool (`backend="agy-claude"` / `"agy-opus"` / `"agy-gpt"`) when you want extra Claude/GPT capacity without drawing down Codex or Gemini limits.
- Start with one subagent for routine work; use multiple backends when independent perspectives or dual reviews matter; resume the original agent (`resume_from`) when prior context matters.
- Always pass `cwd`. Codex defaults to `gpt-6.2-sol` at `high` effort (override `effort` when you want `low`/`medium`/`xhigh`). Default to `mode="read_only"`; for unattended edits prefer `codex` with `mode="edit"` and `worktree=True`, and inspect the returned `effective_cwd` / `worktree_path`.
- For multi-minute work, use `start_agent` or `run_parallel(wait=False)` and poll with `check_jobs`. Return concise findings inline and reference longer outputs by `job_id`.
```

---

## Configuration (Environment Variables)

| Variable | Default | Description |
|---|---|---|
| `AGENT_BRIDGE_NEON_MODEL` | `neon-high` | Default versionless intent or model ID for `neon` |
| `AGENT_BRIDGE_CODEX_MODEL` | `gpt-6.2-sol` | Default Codex model (automatically rides newer `~/.codex/config.toml` versions in-family; set `auto` to always defer to CLI) |
| `AGENT_BRIDGE_CODEX_EFFORT` | `high` | Default Codex reasoning effort (`low`, `medium`, `high`, `xhigh`, or `inherit` to use `~/.codex/config.toml`) |
| `AGENT_BRIDGE_DEFAULT_TIMEOUT` | `300` | Default overall job deadline in seconds (10–3600) |
| `AGENT_BRIDGE_MAX_CONCURRENT` | `8` | Maximum concurrent subagents **per bridge process** |
| `AGENT_BRIDGE_MAX_STORED_JOBS` | `500` | Maximum terminal jobs retained in `bridge.db` |
| `AGENT_BRIDGE_MODELS_TTL_SECONDS` | `900` | Cache TTL (seconds) for live `agy models` discovery |
| `AGENT_BRIDGE_QUOTA_TTL_SECONDS` | `120` | Cache TTL (seconds) for opt-in `include_quota=True` readings |
| `AGENT_BRIDGE_STATE_DIR` | `~/.agent-bridge` | Directory for `bridge.db` and `agy_models_cache.json` |
| `AGENT_BRIDGE_AGY_BIN` | *(auto)* | Explicit path to `agy` executable |
| `AGENT_BRIDGE_CODEX_BIN` | *(auto)* | Explicit path to `codex` executable |
| `AGENT_BRIDGE_DEFAULT_CWD` | *(server cwd)* | Fallback working directory when `cwd` is omitted |
