# agent-bridge: rules for agents

## Prod craft

- **Targeted check:** `uv run pytest tests/<file> -k <name>`. **Full check:** `uv run pytest` (about a minute, no quota).
- **Live bridge:** a checkout that a host has registered is running code. Build in a Git worktree with its own `uv sync`; leave the registered checkout's `.venv` alone.
- **Throwaway state:** any bridge you start by hand gets `AGENT_BRIDGE_STATE_DIR` set to a temp folder. New code changes `~/.agent-bridge/bridge.db` the first time it opens it.
- **Tests start no real CLI:** point `agy_bin`, `codex_bin` and `claude_bin` at a path that does not exist, and play the one under test with a stand-in script.
- **Live checks:** `scripts/live-checks/` runs the real CLIs in a temp state folder and spends a little quota. Run by hand before a release or after a change to a runner, the tool surface or the task rows; its README lists the commands.
- **Schema:** a table change ships as its own migration, add first and tighten later, applied to the live database only on the owner's yes.
- **Proof:** one test per new behaviour, shown red by breaking that behaviour on purpose.
- **Commits:** the plain message only, in the style of `git log`.
- **Ask first, each time:**
  - `git push` in any form, `gh pr create`, `gh pr merge`
  - any write under `~/.agent-bridge/`
  - `claude mcp`, `codex mcp` or `agy mcp` with `add` or `remove`
  - edits to `~/.codex/config.toml`, `~/.claude.json`, `~/.gemini/config/mcp_config.json` or `~/.gemini/antigravity-cli/settings.json`
