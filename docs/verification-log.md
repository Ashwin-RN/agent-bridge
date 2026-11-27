# Verification log

Dated runs and test observations moved out of the main documentation. Each entry records what was checked, when (where a date was recorded), and what happened.

## Usage dashboard skill headless checks (2026-11-26 and 2026-11-27)

On 2026-11-26 the skill was run headless in each host, against a bridge started from a test folder: Claude Code 2.1.332 (`claude -p "/bridge-usage 7d"`), Codex 0.172.0 (`codex exec '$bridge-usage 7d'`) and `agy` 1.5.18 (`agy -p "/bridge-usage 7d"`). Each loaded the skill from the project folder in the install table, called `usage_dashboard` with `period="7d"`, and printed the view exactly as the tool returned it, with nothing added. `codex exec` called it with no approval setting, because the tool is marked read-only.

On 2026-11-27, after the view began to read live quota and the tool was marked open-world as `get_usage` is, it was run headless again, against a bridge started from a worktree with a test state folder. Codex 0.172.0 (`codex exec '$bridge-usage today'`, approval `never`, no approval setting for the server) called the tool and printed the view as returned, bars included. `agy` 1.5.18 (`agy -p`, with the tool allowed for that session through `--settings`) did the same, asked for the tool by name on a test server entry in `<project>/.agents/mcp_config.json`: an entry there named `agent-bridge` did not take the place of the one in `~/.gemini/config/mcp_config.json`, which answered instead. Claude Code was not run headless again.

The same tests found that the hosts hand a tool result to their model differently: Claude Code passes only the structured part, and Codex and Antigravity pass the text. That is why `usage_dashboard` returns one object, which the bridge sends as both.

## Codex headless approval

In a test of `codex exec` without `default_tools_approval_mode = "approve"`, tools not marked read-only failed with `"MCP tool call requires approval"`: `run_agent` failed, while `list_backends` went through.

## Codex worktree commit on Windows

In testing on Windows, Codex's sandbox refused to commit inside a worktree (`index.lock`, access denied) even though the bridge offered it the main repository's `.git` folder. Edits were left uncommitted in the worktree.

## Claude modes and git push

- **`claude`, `read_only`:** in tests, a write by tool and a write by shell were both refused, and the folder stayed clean.
- **`claude`, `edit`, `worktree=True`:** in a test, the file was made in the worktree, the main checkout stayed clean, and `git push origin HEAD` was denied.
- **Classifier and `git push`:** in testing, Claude Code's `auto` mode classifier allowed a `git push` that the prompt asked for; Claude edit jobs are therefore started with deny rules for the plain forms of `git push`.
- **Managed MCP file:** Claude Code's docs describe setups with a managed MCP file where `--strict-mcp-config` is not accepted; this was not tested here.

## agy headless mode

In testing with `--mode plan` or `--mode accept-edits`, every shell command needed an approval that a headless run could not give, so most jobs ended empty. The bridge therefore starts `agy` with no `--mode`.

## Nesting detection checks

All three nesting protections (environment variable on `agy`, process tree on Codex, and no MCP servers on Claude) were checked with real runs on Windows: a Codex job and an `agy` job, each with the bridge registered, were refused, and a Claude job reported no MCP tools.

On macOS and Linux the process-tree check reads `/proc` or `ps`; those paths have unit tests for their parsing, but were not run on a real machine here.

## Claude quota window reporting

In the runs made here, Claude Code reported only the weekly window, which was past 75% used. An unlisted window was simply not reported and is not known to be empty or full.

## Timeouts and large reads

A job that reads a few large files has taken about three and a half minutes. Because queue time shares the same clock, the 300 second default is tight for real work.

## Haiku wrapper agent token cost

In two short test jobs, each Haiku wrapper agent in Claude Code used about 76,000 Haiku tokens, nearly all of it the agent's own start-up text. The cost did not grow with the size of the job's reply. Each further 90 second `check_jobs` check added about 25,000 more tokens on the Claude plan.

## agy Claude Opus quota consumption

In one test, seven Opus reading jobs through `agy` took 45% of the `agy_claude_gpt` weekly quota.
