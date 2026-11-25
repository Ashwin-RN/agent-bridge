# agent-bridge

Lightweight local stdio MCP server that connects **Claude Code** (or any MCP client) to **Antigravity (`agy` — Gemini Neon, Hydrogen, Flash)** and **Codex (`codex` — GPT)** subagents over your existing CLI subscriptions.

No API keys. No separate UI or web app. One lightweight integration across all subscriptions, with dynamic model auto-upgrading and parent-visible usage tracking.

---

## Quick Setup (For Humans & AI Agents)

If you are an AI agent setting this up after a fresh `git clone` or `git pull`, run the steps below in order.

### 1. Check prerequisites

Verify `uv`, `claude`, and at least one of `agy` / `codex` are installed and authenticated:

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

*(After any future `git pull`, re-run `uv sync` to update dependencies cleanly.)*

### 3. Register with Claude Code (user scope — available in every project)

**Windows (PowerShell):**

```powershell
claude mcp add --scope user agent-bridge -- <REPO_DIR>\.venv\Scripts\python.exe -m agent_bridge
```

Example when cloned at `C:\Users\ashwi\agent-bridge`:

```powershell
claude mcp add --scope user agent-bridge -- C:\Users\ashwi\agent-bridge\.venv\Scripts\python.exe -m agent_bridge
```

**macOS / Linux:**

```bash
claude mcp add --scope user agent-bridge -- <REPO_DIR>/.venv/bin/python -m agent_bridge
```

*(To register for only one repository instead of globally, change `--scope user` to `--scope project`.)*

### 4. Verify the integration

```powershell
# Run automated unit & stdio MCP protocol tests
uv run pytest

# Confirm Claude Code sees and connects to agent-bridge
claude mcp get agent-bridge
```

Inside Claude Code, ask:
> *"Check `list_backends` on agent-bridge and confirm Neon and Codex are ready."*

---

## Seamless Model Updates (No Hardcoded Version Breakage)

`agent-bridge` is designed so newer model releases work automatically without editing code or breaking calls:

- **Neon (`agy`)**:
  - Defaults to the versionless intent `neon-high` (not a hardcoded version number).
  - Queries live `agy models` (cached in `~/.agent-bridge/agy_models_cache.json`, TTL 15 min) and automatically selects the **highest numeric version** matching the requested family and effort tier (e.g. `neon`, `neon-xhigh`, `neon-high`, `neon-medium`, `neon-low`, `hydrogen`, `hydrogen-xhigh`, `carbon`, `argon`, `flash`, `flash-low`).
  - When a newer generation rolls out (e.g. `gemini-4-neon-high` → `gemini-5-neon-high`), versionless shortcuts pick it up automatically, and even retired exact IDs are auto-upgraded to the latest matching family/tier with a transparent `model_notice`.
  - If `agy` ever rejects a cached model at runtime, `agent-bridge` force-refreshes `agy models` and retries once automatically (falling back to `agy`'s own default if needed).
- **Codex (`codex`)**:
  - Omits `-m` by default so Codex always uses your subscription's latest default model.
  - If an explicit pinned model ID is ever rejected or retired by Codex CLI, `agent-bridge` automatically retries once using Codex's current default model and attaches a `model_notice`.

---

## Parent-Visible Usage Tracking

Every tool call gives the parent AI (Claude) immediate visibility into external subagent consumption:

- **Per-run `usage`**: normalized across both CLIs into `{input_tokens, output_tokens, cached_tokens, reasoning_tokens, total_tokens}`.
- **Inline `session_usage`**: compact current-session totals by backend (`neon` vs `codex`) included on `run_agent`, `run_parallel`, `review_code`, `check_jobs`, and `list_backends`.
- **Persistent ledger (`~/.agent-bridge/usage.jsonl`)**: append-only JSONL history queried via `get_usage(period="session" | "today" | "7d" | "30d" | "all", backend=...)`, broken down by totals, backend, model, and recent runs.

---

## MCP Tools

| Tool | Purpose |
|---|---|
| `run_agent` | Run a Neon (`agy`) or Codex (`codex`) subagent synchronously; supports `resume_from` (`job_id` or `session_id`), `mode="read_only" \| "edit"`, `model`, and Codex `worktree=True` |
| `start_agent` | Start a subagent in the background and return a `job_id` immediately |
| `run_parallel` | Fan out up to 10 tasks across Neon and/or Codex concurrently (`wait=True` or background `wait=False`) |
| `check_jobs` | List recent jobs, fetch results, or wait (`wait_seconds`, `wait_mode="all" \| "any"`) without busy-polling |
| `cancel_job` | Cancel a running job and terminate its subprocess tree cleanly |
| `review_code` | Run a read-only code review (`uncommitted`, vs `base` branch, or `commit`) via Neon, Codex, or both in parallel |
| `get_usage` | Inspect cumulative token, run-count, and duration usage (`session`, `today`, `7d`, `30d`, `all`) by backend and model |
| `list_backends` | Verify CLI availability, versions, live `agy` models, dynamic shortcuts, and session usage |

---

## Optional `CLAUDE.md` Snippet

Drop this into `~/.claude/CLAUDE.md` (or a project's `CLAUDE.md`) so Claude naturally delegates when helpful:

```markdown
## Multi-Model Subagents (`agent-bridge` MCP)
- Use `agent-bridge` to delegate reviews, second opinions, or parallel work to `neon` (Gemini Neon via `agy`) and `codex` (GPT via `codex`).
- Always pass the current repo path as `cwd`. Keep `mode="read_only"` unless I ask for edits; use `worktree=True` for parallel Codex edits.
- Prefer versionless Neon shortcuts (`neon-medium`, `neon-high`, `neon-xhigh`, `flash`) and omit `model` for Codex so latest models are used automatically.
- Use `review_code` for dual-model reviews, `run_parallel` or `start_agent` + `check_jobs` for concurrent tasks, `resume_from` for follow-ups, and `get_usage` when I ask about usage across subscriptions.
```

---

## Configuration (Environment Variables)

All settings are optional; pass via `-e KEY=VALUE` on `claude mcp add` if you want to override defaults:

| Variable | Default | Description |
|---|---|---|
| `AGENT_BRIDGE_NEON_MODEL` | `neon-high` | Default versionless intent or model ID for `neon` |
| `AGENT_BRIDGE_CODEX_MODEL` | *(unset)* | Optional Codex `-m` override (unset = Codex CLI default) |
| `AGENT_BRIDGE_DEFAULT_TIMEOUT` | `300` | Default per-job timeout in seconds (10–3600) |
| `AGENT_BRIDGE_MAX_CONCURRENT` | `8` | Maximum simultaneous subagent subprocesses |
| `AGENT_BRIDGE_MODELS_TTL_SECONDS` | `900` | Cache TTL (seconds) for live `agy models` discovery |
| `AGENT_BRIDGE_STATE_DIR` | `~/.agent-bridge` | Directory for `jobs.json`, `usage.jsonl`, and `agy_models_cache.json` |
| `AGENT_BRIDGE_AGY_BIN` | *(auto)* | Explicit path to `agy` executable |
| `AGENT_BRIDGE_CODEX_BIN` | *(auto)* | Explicit path to `codex` executable |
| `AGENT_BRIDGE_DEFAULT_CWD` | *(server cwd)* | Fallback working directory when `cwd` is omitted |

---

## Architecture Notes

- Pure Python 3.12+ with only `mcp>=2.0.0` (stdio transport) as a runtime dependency.
- All logs go strictly to `stderr`; `stdout` is reserved exclusively for MCP JSON-RPC frames.
- On Windows, `codex` resolves directly to the native `codex.exe` binary (or `node codex.js`), bypassing `.cmd` shell quoting/length limits, and prompts are piped via stdin (`-`).
- Cancelling or timing out a job terminates the full subprocess tree cleanly (`taskkill /T /F` on Windows, process groups on POSIX).
