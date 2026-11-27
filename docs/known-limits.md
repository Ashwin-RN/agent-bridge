# Known limits

- **Process identity across platforms and Linux time namespaces.** Process identity for a bridge window is compared exactly on Windows and Linux and falls back to the process id elsewhere. On Linux, windows in different time namespaces that share one state folder can read different start ticks and disagree on whether another window is alive.
- **Shutdown lock wait.** At shutdown, a database held locked by another process can make the last write wait longer than the flush budget.
- **Restart every window after an update.** After updating agent-bridge, restart or reconnect every open host window. A window still running older code keeps the older, weaker rules for final writes.
- **Mid-run session ids and dead-window blocks.** A session id that a CLI reveals mid-run is not checked against a resume block left behind by a dead window's unfinished remote turn.
- **Child-environment filter checks names, not values.** `AGENT_BRIDGE_CHILD_ENV=trim` removes variables whose names look like credentials, not values. Credentials inside proxy or index URLs, Git configuration, agent sockets, and the bridge's own Git worktree commands are not filtered.
- **TOML mixed line endings.** When `agent-bridge install` edits a TOML file, it normalises mixed line endings in that file.
- **SQLite side files on read-only status.** `agent-bridge status` opens `bridge.db` read-only (`mode=ro`), but SQLite may still create `-shm` and `-wal` side files beside it.
- **Codex model reporting.** The Codex CLI does not report which model ran, so its model always reads `requested` (or Codex's default model).
- **Untested hosts and platforms.** The five tools have been driven live from Claude Code and from Codex. Antigravity as a host has not been run on the five-tool surface, and every other host in the table is written from its documentation and has not been run. A host the bridge cannot identify waits at most 20 seconds per call. Linux and macOS paths have not been run on real machines.
- **Claude Code task rows.** Task rows have been proven in Claude Code only, through a headless session (`scripts/live-checks/task_rows_claude_code.py`). How the row is drawn in the interactive window was not checked.
