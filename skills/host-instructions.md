## Subagents on other CLIs (`agent-bridge` MCP)

The `agent-bridge` MCP server delegates work to the other coding CLIs on this machine. Five tools: `run`, `jobs`, `cancel`, `usage`, `backends`.

- Before delegating, load the `bridge-routing` skill: it picks the pool, backend, model and mode.
- Before substantial fan-out, call `usage(quota=True)` and route by `quota.order`. A pool with `priority: "use_before_reset"` gets every job it can do.
- Pass `cwd` on new jobs; leave it out on `resume_from` (`relocate_cwd=True` moves a session on purpose).
- Default to `mode="read_only"`. An unattended edit runs on `codex` or `claude` with `mode="edit"` and `worktree=True`; inspect `worktree_path` before using the work.
- Claude and GPT models through `agy` drain the `agy_claude_gpt` pool fast.
- In Claude Code each `run` shows as its own task row: start no wrapper agent and do not poll. In a host with no task row, collect the job with `jobs(job_ids=[...], wait_seconds=90)`.
- The bridge never commits, pushes or merges: bringing a job's work over is yours to do.
