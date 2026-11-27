# Backends, models and effort

## Backend names

| `backend` | Runs | Model when you name none | Quota pool |
|---|---|---|---|
| `codex` (also `gpt`, `openai`) | `codex` | `gpt-6.2-sol` at `high` effort | `codex` |
| `neon` (also `agy`, `gemini`, `antigravity`) | `agy` | `neon-high` | `agy_gemini` |
| `agy-claude` (also `agy-sonnet`) | `agy` | `claude-sonnet-high` | `agy_claude_gpt` |
| `agy-opus` | `agy` | `claude-opus-high` | `agy_claude_gpt` |
| `agy-gpt` | `agy` | `gpt-oss` | `agy_claude_gpt` |
| `claude` (also `claude-code`, `anthropic`) | `claude` | `sonnet` at `high` effort | `claude` |

The three `agy-` names are shortcuts for `neon` with a model already chosen. A `model` you pass always wins over the shortcut's own. In results the backend always reads `neon`, `codex` or `claude`.

`agy-claude` and `claude` are different things. `agy-claude` is Claude served by Antigravity, on Antigravity's quota. `claude` is the Claude Code CLI itself, on your Claude plan.

## Model shortcuts on agy

Shortcuts carry no version number. The bridge reads the live list from `agy models` and picks the newest model that fits.

| `model` | Picks |
|---|---|
| `neon`, `neon-low`, `neon-medium`, `neon-high`, `neon-xhigh` | Gemini Neon at that effort. For builds, design and reviews |
| `hydrogen`, `hydrogen-medium`, `hydrogen-high` | Gemini Hydrogen. For reading and exploring a codebase |
| `flash`, `flash-low` | Gemini Flash. For quick, light checks |
| `carbon`, `argon` | Those Gemini families |
| `claude`, `claude-sonnet`, `claude-sonnet-medium`, `claude-sonnet-high` | Claude Sonnet |
| `claude-opus`, `claude-opus-medium`, `claude-opus-high` | Claude Opus |
| `gpt-oss` | GPT-OSS, at `medium` |
| A full id from `list_backends` | That exact model |

Rules worth knowing:

- A family with no effort means `high`, except `gpt-oss`.
- A bare effort such as `model="medium"` means Neon at that effort.
- `claude` alone means Sonnet.
- The pool follows the model: a name with claude, opus, sonnet, haiku, gpt or oss in it counts against `agy_claude_gpt`; everything else on `agy` counts against `agy_gemini`.
- If the effort you asked for does not exist for that family, or a full id has been retired, the bridge picks the nearest newest model and says so in `model_notice`.
- The bridge never moves a job from the Claude and GPT pool to the Gemini pool by itself. If no model in the pool you asked for is available, the job fails. Pass `allow_cross_pool_fallback=True` to allow the move.
- The model list is cached for 15 minutes in the state folder. If `agy models` cannot be reached and there is no cache, only a full model id runs.

## Codex model and effort

- The default is `gpt-6.2-sol` at `high`, whatever `~/.codex/config.toml` says about effort. This keeps a global `xhigh` there from applying to every delegated job.
- If your Codex config names a newer model of the same family, that one is used.
- `model="latest"`, `"default"` or `"auto"` lets Codex choose its own model.
- If Codex rejects the model, the job runs once more with Codex's own default, and `model_notice` says so.
- `effort` on a call overrides the default for that call.

## Effort on agy

For `agy` the effort is part of the model name (`neon-medium`, `claude-opus-high`). You can name that model, or pass the family with `effort`: `backend="agy-opus"` with `effort="medium"` runs Opus at medium. An `effort` you pass wins over the one in the model name.

## Claude as a backend

`backend="claude"` runs `claude -p`, Claude Code's own headless mode, in the job's folder.

- **Models.** `sonnet`, `opus`, `haiku` or `fable` pick the newest model of that family; the CLI resolves the name, and the result's `model` shows what ran. A full model id is passed as it is. `model="default"` leaves the choice to the CLI's own setting.
- **Effort.** `low`, `medium`, `high`, `xhigh`, passed as `effort`. Names in the `agy` style also work: `opus-high` or `claude-sonnet-medium` are read as that model at that effort. An `effort` you pass wins.
- **Default.** `sonnet` at `high`, whatever your own Claude Code default is. Change it with `AGENT_BRIDGE_CLAUDE_MODEL` and `AGENT_BRIDGE_CLAUDE_EFFORT`.
- **Your plan.** A Claude job draws on the same 5 hour and weekly limits as your own Claude Code and claude.ai use. Anthropic's help page says so: signed in with a plan, "the Claude Agent SDK and `claude -p` still draw from your subscription limits". From a Claude Code host, your own native subagents are usually the better way to reach Claude; this backend is for reaching Claude from Codex, Antigravity or another host.
- **Sign-in.** The bridge starts the unmodified `claude` binary, signed in the way you signed it in. It never reads, copies or forwards the login token, and it never passes `--bare`. Each person who installs the bridge signs in to Claude Code themselves, under their own agreement with Anthropic. Anthropic's rules for Claude Code say they do not "prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription", and they forbid tools that "collect, store, or intermediate" those credentials for others. No page names a local tool that starts the binary for you, so read [their page](https://code.claude.com/docs/en/legal-and-compliance) before building anything on this for other people.
- **API keys.** With `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` in the host's environment, Claude Code would bill that API account instead of your plan, without saying so. The bridge leaves both out of a Claude job's environment. Set `AGENT_BRIDGE_CLAUDE_AUTH=inherit` to pass them on; a run billed to a key then says so in `model_notice`.
- **What it loads.** Your `CLAUDE.md` files and settings, so repository rules apply. No MCP servers at all (`--strict-mcp-config`): a Claude subagent cannot reach your connectors, or the bridge.
- **No model fallback.** If the CLI rejects the model, the job fails with the CLI's message. Only a network or server error on a new `read_only` job is retried, once.
