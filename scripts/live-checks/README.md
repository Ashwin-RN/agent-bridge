# Live checks

`uv run pytest` starts no real CLI. These scripts do: they run this checkout's bridge against the real `codex`, `agy` and `claude` CLIs, so they spend a little quota and need each CLI installed and signed in. Run them by hand before a release or after a change to a runner, the tool surface or the task rows.

Each one starts the bridge with `AGENT_BRIDGE_STATE_DIR` set to a new temp folder, so the real `~/.agent-bridge` is never read or written. Each prints `PASS` or `FAIL` and exits 1 on a failure.

Run from the checkout:

| Command | What it proves | Spends |
|---|---|---|
| `uv run python scripts/live-checks/smoke_backends.py` | One read-only job on each backend completes and its reply is the word in a note file. Name backends to run fewer: `smoke_backends.py codex neon`. | A tiny job on Codex (effort low), Gemini Flash and Claude Haiku |
| `uv run python scripts/live-checks/smoke_resume.py` | A second turn with `resume_from` on each backend keeps the session and recalls a word from the first turn. Name backends to run fewer. | Two tiny jobs on Codex (effort low), Gemini Flash and Claude Haiku |
| `uv run python scripts/live-checks/smoke_review.py` | Codex's own review of an uncommitted change, through `run` with `review`, completes and names the changed line. | A small Codex review at effort low |
| `uv run python scripts/live-checks/task_rows_claude_code.py` | Claude Code is given a bridge job as a background task: the bridge answers `run` with a task, Claude Code polls it with `tasks/get`, and the result reaches the model. Prints the client name seen on the wire. | A short Claude Haiku session and one tiny Codex job |

`tee.py` copies every line between a host and a stdio server to a log file; `task_rows_claude_code.py` uses it and prints where the log is.

What these do not cover: the task row as drawn in an interactive Claude Code window, hosts other than Claude Code, and Linux or macOS (they have only been run on Windows).
