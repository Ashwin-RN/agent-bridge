# Hosts and backends: what is possible

Research only, written on 2026-11-26. Nothing here is built unless the README says so.

Two questions:

1. How can each host show a bridge job as a named agent, the way the Haiku wrapper does in Claude Code?
2. What would it take to add Cursor, Grok and other mainstream CLIs as backends?

How to read the marks:

- **[run]** seen in a real run on this machine while building the Claude backend.
- **[read]** I opened the cited page and found the statement there.
- **[reported]** found by a research job (Gemini, with web access) that cited the page; I did not open it again. Check before building on it.

The CLIs change often. Treat flag names as true on the day of writing.

## 1. Showing a bridge job as a named agent

A bridge job is one MCP tool call. A host shows a tool call as a line in the chat. To see "what is running on which service" at a glance, either the host wraps the call in something it does show by name, or the bridge speaks a part of MCP that the host draws.

### Summary

| | Claude Code | Codex | Antigravity |
|---|---|---|---|
| Subagent with a name chosen per call | Only with agent teams turned on; otherwise the name comes from the agent's definition [reported] | Not by default: it gets a generated nickname [reported] | Yes: `invoke_subagent` takes a role, shown as `<type>:<role>` [read] |
| Subagent on a small model | Yes: `model: haiku` [reported] | Yes: `model` in the agent's TOML file [read] | Yes: `model: flash` [read] |
| Subagent limited to the bridge's tools | Yes: `tools`, `mcpServers` [reported] | No: it inherits the parent's MCP servers [read] | Yes: `tools`, `mcpServers` [read] |
| Where named work is listed | Agent list, `/tasks` | `/subagents` [read] | `/agents` panel with role, state and step [read] |
| Draws MCP progress notifications | Yes, in `/tasks` once the call is in the background [read] | No, logged only [reported] | Yes, per the changelog [reported] |
| MCP tasks (a tool call that answers "working" and is polled) | Yes [read] | No [reported] | No [reported] |
| Runs a slow tool call in the background by itself | Yes, after 2 minutes [read] | No | Per tool, by config: `background: "always"` [read] |
| Default limit on one tool call | None that matters for stdio [reported] | 60 s [read] | 180 s [read] |

### Claude Code

What exists today is the wrapper in the README: one Haiku agent per job, named `<model>: <task>`, which makes the call and polls.

Two things in Claude Code could replace the wrapper:

- **Automatic backgrounding.** "An MCP tool call in the main conversation that is still running after two minutes moves to a background task instead of blocking the session." The task "appears in `/tasks`" and its entry "shows the latest progress the server has reported". Calls made by subagents are not moved. [read: [MCP](https://code.claude.com/docs/en/mcp.md)]
- **MCP tasks.** A server may answer a tool call with a task (`"resultType": "task"`, a `taskId`, a `status`, an optional `statusMessage`) and Claude Code then polls `tasks/get`. "`/tasks` shows the value from your latest `tasks/get` response." [read: same page]

So if the bridge sent progress notifications, a waiting `run` that runs past two minutes would show in `/tasks` with a live line such as "codex: tool: Bash", with no wrapper and no Haiku tokens. If the bridge answered with an MCP task, the job would be a task from the start. Neither gives the entry a name you choose; the wrapper still wins on naming.

Not possible: a per-call name for an ordinary subagent without agent teams [reported: [subagents](https://code.claude.com/docs/en/sub-agents.md)].

### Codex

- **Custom agents.** "To define your own custom agents, add standalone TOML files under `~/.codex/agents/` for personal agents or `.codex/agents/` for project-scoped agents." Each needs `name`, `description` and `developer_instructions`, and may set `model` and `model_reasoning_effort`. [read: [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)]
- **Seeing them.** "Use `/subagents` in the CLI to switch between active agent threads and inspect the ongoing thread." [read: same page]
- **Limits.** Custom agents "inherit the parent's permissions, provider, and MCP servers; a custom agent file can't override those settings". So a wrapper agent in Codex cannot be confined to the bridge's tools. [read: same page]
- **Names.** In the default setup the spawned agent gets a nickname Codex picks. A newer `multi_agent_v2` feature takes a `task_name` per call, lowercase letters, digits and underscores only. [reported: [`multi_agents_spec.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/multi_agents_spec.rs)]
- **No native drawing.** Progress notifications are logged and not shown ([issue 21153](https://github.com/openai/codex/issues/21153)), and MCP tasks are not supported ([issue 48617](https://github.com/openai/codex/issues/48617)). [reported]
- **Timeout.** A tool call is stopped after 60 seconds unless `mcp_servers.<id>.tool_timeout_sec` is set. [read: [config reference](https://learn.chatgpt.com/docs/config-file/config-reference)] A polling wrapper needs it raised; the README's install entry sets 600.
- **Approval.** In `codex exec`, a tool not marked read-only fails with "MCP tool call requires approval, but approval policy is never" unless `default_tools_approval_mode = "approve"` is set for the server. [run]

The closest equivalent: a small-model agent file such as `.codex/agents/bridge-job.toml` whose instructions are the wrapper's (make the call, poll `jobs`, reply with the job's fields only), listed in `/subagents`. The name shown is Codex's, not `<model>: <task>`.

### Antigravity

- **Subagents with a role per call.** "The parent agent calls the `invoke_subagent` tool to spawn a new concurrent session with a dedicated role and initial prompt." [read: [Custom subagents](https://antigravity.google/docs/subagents.md)]
- **Seeing them.** The `/agents` panel "displays a live checklist of all active, completed, killed, or failed background agents" with Identifier, Role, State and Step. [read: same page]
- **Defining one.** A Markdown file with YAML frontmatter in `.agents/agents/` or `~/.gemini/config/agents/`. Fields include `name`, `description`, `tools`, `model` (`inherit`, `flash` or `pro`) and `mcpServers`. [read: same page]
- **Without a wrapper.** A per-tool setting in `mcp_config.json`: `tools.<tool>.background` set to `"always"` "runs the tool's calls as background tasks". [read: [MCP](https://antigravity.google/docs/mcp.md)] Setting it for `run` would list each bridge job as a background task with no subagent at all.
- **Timeout.** 3 minutes per tool call unless `timeoutSeconds` is set. [read: same page]

Antigravity is the host where the wrapper pattern fits best: the role is the per-call name, the model can be Flash, and the agent can be given the bridge's tools only.

### What the bridge could do itself

In order of value for effort:

1. **Send MCP progress notifications** from a waiting `run` or `jobs` call, carrying the status line (now implemented in `mcp_tasks.py`). Claude Code and Antigravity draw them; Codex ignores them harmlessly.
2. **Ship ready-made wrapper agents**: a `.claude/agents/` file, a `.codex/agents/` file and an `.agents/agents/` file with the same instructions, for people who want the named-agent view. Documentation plus three small files.
3. **Answer with MCP tasks** (now implemented in `mcp_tasks.py` for `run` when the client declares `io.modelcontextprotocol/tasks`). Only Claude Code draws them today, and a host that does not support them fails the call ("Unsupported result type 'task'" per the Claude Code page), so the bridge offers it only when the client declares the extension.

## 2. Other CLIs as backends

A backend needs seven things. Claude, added in this change, is the worked example:

| Need | How the Claude backend meets it |
|---|---|
| A run that takes a prompt and exits | `claude -p`, prompt on stdin |
| Machine-readable output with the final reply | `--output-format stream-json`, a final `result` event |
| A session id a later run can continue | In the first event; `--resume <id>` |
| Model and effort | `--model`, `--effort` |
| A read-only mode and an edit mode that never wait for a person | `--permission-mode dontAsk` or `auto`, with `--permission-prompts none` |
| Token usage | In the `result` event |
| Terms that allow a script to drive it on a subscription login | Read the README section; "allowed" is a reading of Anthropic's wording, not a stated permission |

### Summary

| Tool | Official CLI | Headless run | JSON output | Resume | Read-only mode | Token counts | Quota read-out | Verdict |
|---|---|---|---|---|---|---|---|---|
| Cursor CLI (`agent`) | Yes | `agent -p` [read] | `json`, `stream-json` [read] | `--resume` [reported] | `--mode=ask` [reported] | Yes [reported] | Not found | Feasible. Terms unclear on a subscription login |
| Grok Build (`grok`) | Yes [read] | `-p` [reported] | `json`, `streaming-json` [reported] | `-r` [reported] | `--sandbox read-only` [reported] | Yes [reported] | Not found | Likely feasible. Flags not re-checked |
| Gemini CLI (`gemini`) | Yes | `gemini -p` [reported] | `json`, `stream-json` [reported] | `--resume` [reported] | `--approval-mode plan` [reported] | Yes [reported] | Not found | Do not build on its sign-in: see below |
| Copilot CLI (`copilot`) | Yes | `copilot -p` [read] | `--output-format=json`, JSONL [read] | `--resume=<id>` [reported] | No flag; deny tools instead [read] | Premium requests, not tokens [reported] | Not found | Feasible with gaps |
| OpenCode (`opencode`) | Yes | `opencode run` [read] | `--format json` [read] | `--session` [reported] | `--agent plan` [reported] | Yes [reported] | Not found | Feasible. The terms that matter are the model provider's |

None of the five has a documented command that prints the remaining quota. Each would show as `unknown` in `usage(quota=True)`, as the Claude pool does before its first job.

### Cursor CLI

- Headless: "Use print mode (`-p`, `--print`) for non-interactive scripting and automation." [read: [Headless](https://cursor.com/docs/cli/headless)]
- Without `--force`, "anything that runs without approval still runs, and anything that would need approval is rejected". It does not hang. But "print mode writes to files in your workspace even without `--force`", so plain `-p` is not read-only. [read: same page]
- Output: `--output-format json` or `stream-json`. The result is reported to carry `result`, `session_id` and `usage` with input, output and cache tokens. [reported: [output format](https://cursor.com/docs/cli/reference/output-format)]
- Read-only: `--mode=ask` or `--mode=plan`. Effort is part of the model id; there is no separate flag. [reported]
- Sign-in: the docs give `CURSOR_API_KEY` for scripts. [read] Cursor's acceptable-use text is reported to forbid "accessing the Service through automated or non-human means, whether through a bot, script, or otherwise". [reported: [legal](https://cursor.com/legal)] The two sit badly together for a subscription login; read the terms before building.
- Windows: native installer. A hang on the workspace trust prompt is reported, avoided with `--trust`. [reported]

### Grok Build

- xAI publishes it: the repository is [xai-org/grok-build](https://github.com/xai-org/grok-build), "SpaceXAI's coding agent harness and TUI", with prebuilt binaries for macOS, Linux and Windows and a user guide that lists "headless mode, sandboxing" and MCP servers. [read] It is not the community `grok-cli` projects.
- Everything else here is [reported], and the page the research job cited for it (`docs.x.ai/docs/grok-build/headless-and-scripting`) returned 404 when I opened it. The user guide in the repository is the place to check: `-p` for one prompt, `--output-format json` or `streaming-json` with the reply, a session id and token usage, `-r` to resume, `-m` and `--effort`, `--sandbox read-only`, `--always-approve`.
- A run without `--always-approve` is reported to wait for a person and hang. A runner would have to pass it, or a deny-by-default flag if one exists.
- MCP client: yes, `[mcp_servers.<name>]` in `config.toml`, with a `tool_timeout_sec`. [read: [settings](https://docs.x.ai/build/settings)] So Grok Build could also be a host.

### Gemini CLI

- The mechanics are all there [reported: [headless mode](https://geminicli.com/docs/features/headless-mode/)]: `-p`, `stream-json` with a session id and token stats, `--resume`, `--approval-mode plan` or `yolo`, and a denied tool is refused instead of waiting.
- The terms are the blocker. The FAQ says: "Using third-party software, tools, or services to harvest or piggyback on Gemini CLI's OAuth authentication to access our backend services is a direct violation of our applicable terms and policies... the supported and secure method is to use a Vertex AI or Google AI Studio API key." [read: [FAQ](https://geminicli.com/docs/resources/faq)] A bridge that starts the official binary is not harvesting a token, but this is the plainest warning of the five. An API-key backend would be on safe ground; a subscription one would not.
- It also overlaps with what the bridge already does: `agy` reaches the same Gemini models on the Antigravity plan.
- A reported hang when stdin is left open; the bridge already closes stdin for `agy`. [reported]
- The research job also said Google moved the AI Pro and Ultra CLI allowances to Antigravity. The FAQ I read says the opposite ("you automatically have higher limits to Gemini Code Assist and Gemini CLI"). Unconfirmed; left out of the verdict.

### GitHub Copilot CLI

- Headless: "To use the CLI programmatically, include the `-p` or `--prompt` command-line option." [read: [Copilot CLI](https://docs.github.com/en/copilot/concepts/copilot-surfaces/copilot-cli)]
- Output: "`--output-format=FORMAT`: `text` (the default) or `json`. With `json`, the CLI emits JSONL." [read: [programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference)]
- Permissions: `--allow-all-tools`, `--allow-tool='shell(git:*)'`, `--deny-tool='shell(git push)'`. [read: [configuring](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/configure-copilot-cli)] There is no read-only flag; a read-only mode would be built from deny rules for `write` and `shell`.
- Gaps: usage is reported in premium requests, not tokens; no command lists models or quota; `copilot.cmd` on Windows is reported to leave child processes behind when killed. The bridge's Job Object would cover the last one. [reported]
- The two URLs the research job cited for the command reference returned 404; the ones above are the live pages.

### OpenCode

- Headless: `opencode run --format json "List the TODO comments"`. [read: [CLI commands](https://opencode.ai/docs/cli/commands/)] Model: `opencode run --model anthropic/claude-sonnet-5-5`, with effort as a suffix or variant. [read: [models](https://opencode.ai/docs/models/)]
- Reported: JSONL events carry `sessionID`, text parts and token counts; `--session <id>` continues; `--agent plan` is read-only; a run can wait forever on an "ask" permission unless permissions are set to allow in `opencode.json`. [reported]
- OpenCode is a harness, not a subscription. What a job draws on is whichever provider it is pointed at, and that provider's terms apply. Using it to reach Claude on a Claude plan would run into the same Anthropic rule quoted in the README, from the wrong side: it is a third-party client using the subscription login.

### What a new backend costs in this codebase

From adding Claude, the places a backend touches:

| File | What to add |
|---|---|
| `config.py` | The backend and pool names, aliases, defaults, three settings (`*_MODEL`, `*_EFFORT`, `*_BIN`) |
| A runner module | Find the binary, build the command line, parse the output, classify failures. `claude_runner.py` is about 600 lines, half of it shared shape with `CodexRunner` |
| `usage.py` | Turn the CLI's token fields into the bridge's five |
| `jobs.py` | `_runner_for`, whether worktrees are allowed, the quota source |
| `storage.py` | The SQL that guesses a pool when a row has none |
| `quota.py` | A reading for the pool, or "unknown" |
| `server.py` | The health probe in `backends`, the instructions text |
| Tests | A stand-in script for the CLI, and one test per behaviour |

No table changes were needed for Claude and none would be for these: backend and pool are plain text columns.

Before the code, two things per tool, in this order: read its terms for scripted use on the sign-in you mean to use, then run it by hand once in each mode and keep the output. For Claude that hand run found three things no document had: the output carries the plan limits, `auto` mode works headless on a plan, and `auto` allowed a `git push` the prompt asked for.

## Sources

Opened and read:

- https://code.claude.com/docs/en/mcp.md
- https://code.claude.com/docs/en/legal-and-compliance.md
- https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- https://learn.chatgpt.com/docs/agent-configuration/subagents
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://antigravity.google/docs/subagents.md
- https://antigravity.google/docs/mcp.md
- https://cursor.com/docs/cli/headless
- https://github.com/xai-org/grok-build
- https://docs.x.ai/build/settings (search excerpt)
- https://geminicli.com/docs/resources/faq
- https://docs.github.com/en/copilot/concepts/copilot-surfaces/copilot-cli (search excerpt)
- https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference (search excerpt)
- https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/configure-copilot-cli (search excerpt)
- https://opencode.ai/docs/cli/commands/ (search excerpt)
- https://opencode.ai/docs/models/ (search excerpt)

Cited by the research jobs, not opened again:

- https://code.claude.com/docs/en/sub-agents.md, https://code.claude.com/docs/en/env-vars.md, https://code.claude.com/docs/en/agent-teams.md
- https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/multi_agents_spec.rs
- https://github.com/openai/codex/issues/21153, https://github.com/openai/codex/issues/48617
- https://antigravity.google/docs/changelog.md, https://github.com/google-antigravity/antigravity-cli/issues/801
- https://cursor.com/docs/cli/reference/output-format, https://cursor.com/docs/cli/reference/parameters, https://cursor.com/legal
- https://x.ai/legal/terms-of-service
- https://geminicli.com/docs/features/headless-mode/, https://geminicli.com/docs/features/approval-modes
- https://opencode.ai/docs/permissions, https://opencode.ai/docs/agents

Cited by a research job and found dead:

- https://docs.x.ai/docs/grok-build/headless-and-scripting (404)
- https://docs.github.com/en/copilot/copilot-cli/reference/command-reference (404)

The two research jobs ran through the bridge itself, on Gemini Hydrogen: `neon-e33b8cf7` (named agents) and `neon-3454a9c8` (backends).
