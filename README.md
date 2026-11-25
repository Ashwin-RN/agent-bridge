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

## Seamless Model Updates & Effort Control

- **Neon (`agy`)**:
  - Defaults to the versionless intent `neon-high` and resolves against live `agy models` (cached in `~/.agent-bridge/agy_models_cache.json`).
  - **Preserves requested effort tier by default**: e.g. `neon-medium` picks the newest version offering `medium`. If a requested tier is unavailable in that family, `agent-bridge` substitutes the nearest available tier and always discloses it in `model_notice`.
  - Retired versioned model IDs auto-upgrade to the newest matching family/tier, and if `agy` rejects a cached model at runtime, `agent-bridge` force-refreshes `agy models` and retries once automatically within the job's overall deadline.
- **Codex (`codex`)**:
  - Omits `-m` by default so Codex inherits your subscription's latest default model (shown in `list_backends` under `local_config_defaults`).
  - Pass `effort="low" | "medium" | "high" | "xhigh"` (or set `AGENT_BRIDGE_CODEX_EFFORT`) to override `~/.codex/config.toml`'s `model_reasoning_effort` per call.
  - If an explicit model ID is rejected, `agent-bridge` falls back once to Codex's default model and attaches a `model_notice`.

---

## Usage Planning & Opt-In Live Quota

- **Accurate per-turn usage**:
  - Antigravity runs use `agy --output-format stream-json` so per-turn token usage is summed directly from the current turn's `step_update` events (preventing double-counting on resumed sessions), with wall-clock duration measured per invocation.
  - Provider-specific cache metrics (`cache_read_tokens` vs `cached_input_tokens`) are preserved separately in `provider_metrics`, and `get_usage` reports `usage_coverage` (`exact`, `delta`, `partial`, `missing`) per backend without conflating cross-provider token totals.
- **Planning & live quota**:
  - `get_usage(compact=True, cwd=..., parent_run=..., include_quota=True)` gives a concise planning view with per-backend runs, active `in_flight` jobs across windows, recent duration, usage coverage, and cached live subscription quota (`codex app-server` `account/rateLimits/read` + `agy --print=/usage`).
  - If a quota probe or parser fails, `get_usage` still succeeds and returns `status: "unknown"` with timestamp and error details.

---

## Reliability & Stuck-Job Handling

- **Multi-window SQLite store (`~/.agent-bridge/bridge.db`)**: Per-job updates with WAL mode and owner PID tracking prevent multiple Claude Code windows from overwriting each other's job history or misclassifying live jobs as failed. Terminal job status, usage events, and session watermarks commit in a single SQLite transaction.
- **Per-process concurrency cap**: `AGENT_BRIDGE_MAX_CONCURRENT` applies **per bridge process** (per Claude window).
- **Per-session serialization**: Concurrent turns targeting the same `(backend, session_id)` automatically serialize within the job's overall deadline.
- **Worktree & resume safety**:
  - Codex `worktree=True` requires `mode="edit"` and reports `effective_cwd`, `worktree_path`, `worktree_branch`, and `worktree_commit`.
  - Unsupported resume options (such as `extra_dirs` on Codex resume) are rejected explicitly instead of silently dropped.
  - Native `codex exec review` explicitly passes `-c sandbox_mode="read-only"`.
- **Single overall deadline & separate wait window**:
  - Queueing, model discovery, execution, and any retry share one `timeout_seconds` deadline (defaulting to `AGENT_BRIDGE_DEFAULT_TIMEOUT` when omitted).
  - For multi-minute work, use `start_agent`, `run_parallel(wait=False)`, or a shorter `wait_seconds` on `run_agent`, then poll with `check_jobs`. When a wait ends with `queued`/`running`, the job stays active; if a synchronous `run_agent` call is cancelled by the client, its underlying job is cancelled cleanly.
  - Failures return machine-readable `failure_reason`, `provider_state` (`stopped` / `possibly_running` / `unknown`), partial `response` + `session_id`, and `retry_guidance`. Subprocess cleanup has a single owner using Windows Job Objects (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) on Windows and process groups on POSIX.

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
- Before substantial fan-out, call `get_usage(compact=True, cwd=<repo>, include_quota=True)` to check load and subscription quota.
- Start with one subagent for routine work; use both `neon` and `codex` when independent perspectives or dual reviews matter; resume the original agent (`resume_from`) when prior context matters.
- Always pass `cwd`. Default to `mode="read_only"`; for unattended edits prefer `codex` with `mode="edit"` and `worktree=True`, and inspect the returned `effective_cwd` / `worktree_path`. Pass `effort` explicitly when you want to cap reasoning cost.
- For multi-minute work, use `start_agent` or `run_parallel(wait=False)` and poll with `check_jobs`. Return concise findings inline and reference longer outputs by `job_id`.
```

---

## Configuration (Environment Variables)

| Variable | Default | Description |
|---|---|---|
| `AGENT_BRIDGE_NEON_MODEL` | `neon-high` | Default versionless intent or model ID for `neon` |
| `AGENT_BRIDGE_CODEX_MODEL` | *(unset)* | Optional Codex `-m` override (unset = Codex CLI default) |
| `AGENT_BRIDGE_CODEX_EFFORT` | *(unset)* | Optional default Codex reasoning effort (`low`, `medium`, `high`, `xhigh`) |
| `AGENT_BRIDGE_DEFAULT_TIMEOUT` | `300` | Default overall job deadline in seconds (10–3600) |
| `AGENT_BRIDGE_MAX_CONCURRENT` | `8` | Maximum concurrent subagents **per bridge process** |
| `AGENT_BRIDGE_MAX_STORED_JOBS` | `500` | Maximum terminal jobs retained in `bridge.db` |
| `AGENT_BRIDGE_MODELS_TTL_SECONDS` | `900` | Cache TTL (seconds) for live `agy models` discovery |
| `AGENT_BRIDGE_QUOTA_TTL_SECONDS` | `120` | Cache TTL (seconds) for opt-in `include_quota=True` readings |
| `AGENT_BRIDGE_STATE_DIR` | `~/.agent-bridge` | Directory for `bridge.db` and `agy_models_cache.json` |
| `AGENT_BRIDGE_AGY_BIN` | *(auto)* | Explicit path to `agy` executable |
| `AGENT_BRIDGE_CODEX_BIN` | *(auto)* | Explicit path to `codex` executable |
| `AGENT_BRIDGE_DEFAULT_CWD` | *(server cwd)* | Fallback working directory when `cwd` is omitted |
