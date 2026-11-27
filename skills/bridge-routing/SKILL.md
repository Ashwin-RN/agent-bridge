---
name: bridge-routing
description: Use before you delegate work to a subagent through the agent-bridge run tool, to pick its backend, model and mode, and to brief, collect and cancel the job.
---

# Bridge routing

The tools are those of the MCP server named `agent-bridge`. Its own instructions say what each tool does; this adds the choices they leave to you.

## Pool

`order` in `usage(quota=True)` lists quota pools, not backends:

| Pool | Backend |
|---|---|
| `agy_gemini` | `neon` |
| `codex` | `codex` |
| `agy_claude_gpt` | `agy-claude`, `agy-opus`, `agy-gpt` |
| `claude` | `claude`. Never in `order`: it is the user's own plan, shared with their own sessions |

- A `pools` entry with `priority` `use_before_reset` loses its unused weekly quota soon. Give it every job it can do, even where another backend fits a little better.

## Backend and model

| Work | `backend` and `model` |
|---|---|
| Quick, light check | `neon`, `flash` |
| Reading and exploring a codebase | `neon`, `hydrogen-medium`; `hydrogen-high` for a wide sweep |
| Build, design, review | `neon`, `neon-medium`; `neon-high` (its default) when it is hard |
| Focused code review | `codex` with `review` |
| Unattended edit | `codex`, or `claude` (see Edits) |
| More Claude or GPT capacity | `agy-claude`, `agy-opus`, `agy-gpt`: only when `agy_claude_gpt` is `use_before_reset`, or the user's Claude plan is low. They drain that pool fast |
| The user's own Claude plan | `claude`, with `sonnet`, `opus`, `haiku` or `fable` |

## Shape

- Start with one job. Send the same question to several backends only when independent views matter, such as a review whose findings you will act on.
- When the next step needs what a job already read, `resume_from` that job instead of starting a fresh one and briefing it again.

## Edits

- An unattended edit runs on `codex` or `claude` with `mode="edit"` and `worktree=True`. The reply's `worktree_path` and `worktree_branch` say where the work is: inspect the diff there before you use it.
- The bridge never commits, pushes or merges. Bringing the work over is yours to do.

## Brief

- Name the deliverable and its length: "file:line findings, most severe first, under 300 words".
- Each subagent's CLI loads its own global rules file as it always does. Give the task and its context, not standing rules.
- A `claude` job has no MCP servers. Put anything it needs from a connector into the brief.

## Collect

- When the host turned `run` into a background task, not a receipt with `next`, the host hands you the result when the job ends. Leave it: call no `jobs` to poll, and start no wrapper agent to hold the job.
- With `response_file=True` the reply gives `response_file` (a path), `response_head` and `response_total_chars`. Read only the part of the file you need.
- A `completed` job whose reply lacks the deliverable has not done the work: `resume_from` it and ask for the missing part.
- `cancel` each job whose answer you no longer need, such as the rest once `wait_mode="any"` has returned one that answers.
