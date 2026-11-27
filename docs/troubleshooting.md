# Troubleshooting

Run `agent-bridge doctor` first to check backend CLIs, host registrations, timeouts, headless approval rules, the `bridge-usage` skill, the state database and `AGENT_BRIDGE_PARENT_JOB`.

## Common problems

| What you see | Likely cause and fix |
|---|---|
| The host does not list the bridge's tools | It was registered after the host started. Restart it, or reconnect from `/mcp`. Check the path with `claude mcp get agent-bridge`, `codex mcp list` or `agy mcp list` |
| In `codex exec`: "MCP tool call requires approval, but approval policy is never" | Nobody can approve a tool call there. Set `default_tools_approval_mode = "approve"` for the server in `~/.codex/config.toml` |
| In Codex, a tool call ends after 60 seconds | Codex's default tool timeout. Set `tool_timeout_sec = 600` for the server (or run `agent-bridge install --host codex`) |
| In Antigravity, a tool call is denied or waits for approval | MCP tools are in Ask mode until allowed. Add `"mcp(agent-bridge/*)"` to `permissions.allow` |
| `nested_bridge` | The call came from a subagent that the bridge itself started. That is refused on purpose: see [A bridge inside a bridge](safety.md#a-bridge-inside-a-bridge). If it comes from your own chat, `AGENT_BRIDGE_PARENT_JOB` is set in that host's environment: unset it |
| A tool error that reads only "Error executing tool ..." | A crash inside the bridge, not a refusal. The bridge's log on stderr has the traceback |
| The `claude` backend shows `available: false` and "too old" | Claude Code is older than 2.1.259. Update it |
| "... is a shell shim, which the bridge will not start" | On Windows, `claude` resolved to a `.cmd` file with no script the bridge could run directly. Set `AGENT_BRIDGE_CLAUDE_BIN` to `claude.exe` |
| Every `claude` job fails at once on a managed machine | The bridge passes `--strict-mcp-config`. Claude Code's docs describe setups with a managed MCP file where that flag is not accepted; not tested here |
| A `claude` job fails with `auth_error` | Claude Code is not signed in for that user. Run `claude auth login`, or `claude` and `/login` |
| A `claude` job in `read_only` ends with "denied" in its reply | It tried to write or to run a changing command. That is the mode working. Use `mode="edit"` if you meant it to change things |
| `command_found: false` for a backend | That CLI is not on `PATH`. Install it, or set `AGENT_BRIDGE_AGY_BIN`, `AGENT_BRIDGE_CODEX_BIN` or `AGENT_BRIDGE_CLAUDE_BIN` |
| `auth_error` on Codex or `agy` | Sign in to that CLI in a terminal, then try again. If you use an API-key sign-in, add its variable name to `AGENT_BRIDGE_CHILD_ENV_KEEP` (default `AGENT_BRIDGE_CHILD_ENV=trim` drops credential-named variables) |
| `cli_error`: output ended without its final event | The CLI exited 0 but stopped before its final event (`turn.completed` for Codex, a result event with status for `agy`, or `result` for Claude). Any partial `response` is kept. Read `response` and `stderr_tail`, check `backends`, and inspect `git status`/`git diff` on edit tasks before retrying |
| An `agy` job comes back with one line such as "I'll start by reading the files", or with no reply | `agy` denied an action it could not ask about, most often a shell command, and ended the turn. With no reply the job fails with `permission_error`; with a line of text it still reads `completed`. Resume it and tell it to use its file tools |
| The reply points to a report file instead of holding the answer | `agy` wrote its answer to a file in its own data folder. Open that file, or ask for the answer in the reply itself |
| `timed_out` on a job that was doing fine | The 300 second default. Pass a larger `timeout_seconds` |
| `timed_out` after a few seconds, with `provider_timeout_still_running` and an `error` about quota | The pool is out of quota; `agy` sometimes reports it this way. The `error` text gives the reset time. Use another pool until then |
| `prior_turn_uncertain` on a resume | The last turn on that session may not have stopped. See [Resuming a session](#resuming-a-session) |
| A resume ran on a different model than before | A shortcut backend such as `agy-opus` was passed on the resume. Pass plain `neon` or leave `backend` out |
| `usage(quota=True)` is slow, or a pool shows `priority: "unknown"` | It starts both `codex` and `agy` to read live numbers. Check `unread` for the reason, check that the CLI is signed in, and try again |
| "must be cancelled from that window" | The job belongs to another open host window. Cancel it there |
| `owner_exited` | The host window that ran the job closed. For an edit job, check what it left, then run it again |
| `interactive_fallback` | `agy` wanted an approval. Use Codex with `mode="edit"` and `worktree=True` |
| `agy` jobs fail with `permission_error` on ordinary shell commands | `agy` is older than 1.5.7, or its `settings.json` pins a mode with `agentMode`. Update `agy`, or remove `agentMode`, so a headless run uses its default mode |
| `/bridge-usage` or `$bridge-usage` says the bridge is out of date | The bridge that host runs has no `usage` tool yet. Update it as in [Update an existing install](install.md#update-an-existing-install), then reconnect it in that host |
| The skill does not show up in a host | It is not in a folder that host reads, or the host started before it was copied. See [Install the skills](install.md#install-the-skills), then open a new chat |
| A Codex worktree job says it could not commit (`index.lock`, access denied) | Codex's sandbox refused the write to Git's own folder. The edits are in the worktree: commit them yourself |

## Timeouts and waiting

- `timeout_seconds` is the job's deadline: 300 seconds unless you set it, clamped to 10–3600. The clock starts when the job is queued, so waiting for a slot or for a session uses it up. At the deadline the subprocesses are killed and the job ends `timed_out`.
- `wait_seconds` is only how long the tool call waits (default `0`, max `300`). `run` returns a receipt right away when `wait_seconds=0` or when the wait expires before the job ends; collect the job with the `jobs` call in `next`.
- 300 seconds is tight for real work, and time spent queued comes off the same clock. Give jobs that read large files or do substantial work a larger `timeout_seconds` (see [verification log](verification-log.md#timeouts-and-large-reads)).
- Waits on `run` and `jobs` are automatically capped 10 seconds under the detected host's tool-call limit (and at most 300 seconds). An unrecognised host gets a conservative 20 second cap (`30 - 10`). Claude Code has no practical cap on stdio (and receives `run` as an MCP task row without waiting). A shortened wait whose job has not finished carries `wait_note`. `backends` reports `host` and `wait_cap`.
- `agent-bridge install` sets Codex's `tool_timeout_sec` and Antigravity's `timeoutSeconds` to 600 seconds (up from defaults of 60 s and 180 s).
- If the host cancels a waiting `run`, that job is cancelled too. Background jobs are not cancelled when a waiting `jobs` call is cancelled.
- Each window runs at most 8 jobs at a time (`AGENT_BRIDGE_MAX_CONCURRENT`). More are queued.

## Resuming a session

Pass `resume_from` with a `job_id` or a `session_id`. The new turn continues that conversation.

- Carried over: the backend, the folder or worktree, the model, the effort and `parent_run`.
- Pass the plain backend, `neon`, `codex` or `claude`, or leave `backend` out. A shortcut such as `agy-opus` brings its own default model (`claude-opus-high`) and replaces the one the session was using.
- Leave `cwd` out. A resumed session stays in its recorded folder or worktree; a different `cwd` is ignored unless you also pass `relocate_cwd=True`.
- You may change the model. If that moves the session to another quota pool, `model_notice` says so.
- You cannot change the backend, and `extra_dirs` is refused on a Codex resume.
- Two turns on one session never run at once. The second waits, in any window, and the wait counts against its timeout.

If the earlier turn ended with `provider_state` `possibly_running` or `unknown`, the bridge will not resume by itself: the old turn may still be writing. The job fails with `prior_turn_uncertain`. Check that the old turn is over (for an edit job, look at `git status` and `git diff`), then resume with `confirm_prior_job_id="<that job_id>"`.
