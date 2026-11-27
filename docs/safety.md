# Safety

## What a subagent can touch

The bridge does not sandbox anything itself. It asks each CLI to, and the CLI's own rules apply.

| Backend and mode | What the bridge passes | In practice |
|---|---|---|
| `codex`, `read_only` | Sandbox `read-only` | Codex's own read-only sandbox: reading, no writes |
| `codex`, `edit` | Sandbox `workspace-write` on `cwd` and each `extra_dirs` folder | Codex may change files there |
| `codex`, `edit`, `worktree=True` | The same, on the new worktree, plus the repository's `.git` folder | Codex may change files in the worktree. The bridge also offers it the main repository's `.git` folder so that it can commit, but on Windows Codex's sandbox can still refuse the commit (`index.lock`, access denied; see [verification log](verification-log.md#codex-worktree-commit-on-windows)). Expect the edits uncommitted, and commit them yourself |
| `codex`, review with no `focus` | Sandbox `read-only` | Read only |
| `agy`, `read_only` | No mode flag, and an instruction at the top of the prompt: this is a read-only run, change no file, say at the end whether you changed one | Read-only by instruction, not by a sandbox. `agy` is able to write files here, so check `git status` after a run that matters. Shell commands run in `agy`'s own sandbox |
| `agy`, `edit` | No mode flag | `agy`'s default headless mode: it may edit files in `cwd` and `extra_dirs`, and shell commands run in its own sandbox |
| `claude`, `read_only` | Permission mode `dontAsk`, the writing tools removed (`Write`, `Edit`, `NotebookEdit`), and nobody to answer a prompt | It reads and searches, and may run shell commands that only read. It has no tool that writes a file, and a shell command that changes something is denied, unless your own Claude Code settings allow that exact command for good (see [verification log](verification-log.md#claude-modes-and-git-push)). No web access |
| `claude`, `edit` | Permission mode `auto`, and deny rules for `git push` | File edits are accepted. Claude Code's own classifier decides each shell command. There is no sandbox: a command the classifier lets through runs with your rights, network included |
| `claude`, `edit`, `worktree=True` | The same, in the new worktree | Edits land in the worktree, the main checkout stays untouched, and plain forms of `git push` are denied (see [verification log](verification-log.md#claude-modes-and-git-push)) |

Also true in every mode:

- The bridge adds `AGENT_BRIDGE_PARENT_JOB` and `AGENT_BRIDGE_PARENT_OWNER` to every subagent's environment (see [A bridge inside a bridge](#a-bridge-inside-a-bridge)).
- The bridge never commits, pushes or merges. A subagent may commit. A Claude edit job is started with deny rules for the plain forms of `git push`, because its classifier can otherwise allow a push asked for in the prompt (see [verification log](verification-log.md#claude-modes-and-git-push)); a push hidden in a script would not be caught. Codex's sandbox has no network. `agy` has neither rule.
- A Claude subagent gets no MCP servers. Codex and `agy` subagents get whatever MCP servers that CLI has registered, the bridge included if you registered it there.
- Your prompt and the files the subagent reads go to that CLI's provider, under your account there.
- `agy` cannot ask for an approval when run this way. An action that needs one is denied, the turn ends with no reply, and the job fails with `permission_error`.
- The bridge starts `agy` with no `--mode` on purpose. With `--mode plan` or `--mode accept-edits`, every shell command needs an approval that a headless run cannot give (see [verification log](verification-log.md#agy-headless-mode)).
- For unattended edits, prefer Codex or Claude with `mode="edit"` and `worktree=True`. `agy` has no worktree option.

## Child environment

By default (`AGENT_BRIDGE_CHILD_ENV=trim`), Codex and `agy` jobs do not inherit environment variables whose names look like credentials (`KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `CREDENTIAL`, case-insensitive). Dropped and explicitly kept variable names are logged on stderr.

Built-in exemptions kept so local tools work: `SSH_AUTH_SOCK`, `SSH_AGENT_PID`, `GPG_AGENT_INFO`, `GNOME_KEYRING_CONTROL`, `GNOME_KEYRING_PID`, `GCM_CREDENTIAL_STORE`, and `GIT_CONFIG_KEY_<n>`.

What the filter covers and what it does not:

- **Covered:** variable names matching the credential pattern, for Codex and `agy` jobs.
- **Not covered:** credentials embedded inside variable values (such as proxy or package-index URLs), Git configuration, agent sockets, and the bridge's own Git worktree commands.
- **API-key sign-in:** if you sign in to Codex or `agy` with an API key (rather than a subscription login), list that variable's exact name in `AGENT_BRIDGE_CHILD_ENV_KEEP` (comma-separated) or set `AGENT_BRIDGE_CHILD_ENV=inherit`.
- **Claude jobs:** handled separately by `AGENT_BRIDGE_CLAUDE_AUTH` (`plan` by default strips `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`, plus Claude Code's own session variables so the child does not take itself for part of the host's session; `inherit` passes API keys on).

## Edits in a worktree

`worktree=True` works on Codex and Claude, in `edit` mode only.

1. Before the job starts, the bridge makes a worktree from the repository's current `HEAD` commit. Uncommitted changes in your checkout are not in it.
2. It lives at `<state dir>/worktrees/<repo name>-<hash>/<job_id>`, on a new branch `agent-bridge/<job_id>`. If `cwd` was a subfolder, the job runs in the same subfolder of the worktree.
3. The job reply gives `worktree_path`, `worktree_branch` and `effective_cwd`. Inspect `git -C <worktree_path> status` to see whether edits were committed or left uncommitted.
4. A resume of that session goes back to the same worktree.

Getting the work out is yours to do:

```
git -C <worktree_path> status
git -C <worktree_path> diff
git -C <worktree_path> add -A && git -C <worktree_path> commit -m "..."   # if the subagent left changes uncommitted
git merge agent-bridge/<job_id>                                           # from your own checkout
```

The bridge never deletes a worktree or its branch. When you are done:

```
git worktree remove <worktree_path>          # add --force if it still holds uncommitted changes you do not want
git branch -D agent-bridge/<job_id>
```

Refused: a folder that is not a Git repository, a repository with no commits, `worktree=True` on `agy`, and `worktree=True` without `mode="edit"`.

## A bridge inside a bridge

Once the bridge is registered in more than one CLI, a subagent can see the bridge's tools too. Codex started by the bridge would find the bridge among its own MCP servers, and could start a Claude job, which could start another, and so on.

The bridge stops this at the first step. A bridge that finds itself running inside a bridge job refuses `run`:

```
nested_bridge: this agent-bridge was started inside job codex-1a2b3c4d (detected by env). A subagent may
not start further bridge jobs, so `run` is refused. ...
```

`jobs`, `cancel`, `usage` and `backends` still work there, and `backends` shows `nesting`. There is no setting to allow a second level.

How a bridge knows:

- **A marker variable.** Every subagent is started with `AGENT_BRIDGE_PARENT_JOB` set to its job id. A bridge that sees it is nested. `agy` passes its environment on to the MCP servers it starts, so this is what catches it there.
- **The process tree.** Codex passes only a fixed list of variables to its MCP servers. The bridge checks its ancestors for a live owner in its database or a `python -m agent_bridge` command line. The command line also identifies bridges with separate state directories. If the process tree is unavailable, `run` retries the check and proceeds while the answer is unknown.
- **No MCP servers.** A Claude subagent is started with none, so it has no bridge to call.

See the [verification log](verification-log.md#nesting-detection-checks) for what has been tested on Windows versus macOS and Linux.

What it does not cover: the database owner check needs both bridges to use the same state folder, which they do unless you set `AGENT_BRIDGE_STATE_DIR` differently per host. A parent process whose start time cannot be read is not counted as a bridge owner, so that a process id left over from a dead bridge can never lock an ordinary host out; the price is that such a parent is missed. On macOS and Linux the check reads `/proc` or `ps`; those paths have unit tests for their parsing but were not run on a real machine here.
