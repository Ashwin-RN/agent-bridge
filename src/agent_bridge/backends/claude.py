"""Claude Code (`claude -p`) backend adapter, subprocess runner, usage mapping, and quota helpers."""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_bridge.backends.base import (
    BackendAdapter,
    BackendDefaults,
    Capabilities,
    CommandSpec,
    ParsedStream,
    QuotaPoolSpec,
    to_int,
    unhealthy_payload,
)

if TYPE_CHECKING:
    from agent_bridge.config import EffortLevel, Settings
    from agent_bridge.models import RunRequest, RunResult
    from agent_bridge.runners import (
        OnActivityCallback,
        OnAttemptCallback,
        OnModelCallback,
        OnProviderStartCallback,
        OnSessionCallback,
    )

logger = logging.getLogger("agent_bridge.backends.claude")

DEFAULT_CLAUDE_MODEL = "sonnet"
DEFAULT_CLAUDE_EFFORT: EffortLevel = "high"

# Names the `claude` CLI resolves to its newest model of that family.
CLAUDE_MODEL_ALIASES: frozenset[str] = frozenset({"sonnet", "opus", "haiku", "fable"})

# `--permission-prompts none` needs this version. An older CLI passes `--version` and then fails every job.
CLAUDE_MIN_VERSION: tuple[int, int, int] = (2, 1, 259)

# `dontAsk` denies whatever would need an approval: writes and commands that change things.
# Reads, searches and read-only shell commands still run.
CLAUDE_READ_ONLY_PERMISSION_MODE = "dontAsk"
# `dontAsk` still honours the allow rules in the user's own settings, and a user may well have allowed
# `Edit` there. So a read-only run does not get the writing tools at all: a bare name removes the tool.
# A shell command the user has allowed for good in their settings does still run.
CLAUDE_READ_ONLY_REMOVED_TOOLS: tuple[str, ...] = ("Write", "Edit", "NotebookEdit")
# Credentials that make `claude` bill an API account instead of the plan it is signed in to.
CLAUDE_API_CREDENTIAL_ENV: tuple[str, ...] = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# `auto` accepts file edits and lets Claude Code's own classifier decide each shell command.
CLAUDE_EDIT_PERMISSION_MODE = "auto"
# The classifier allows a push that the prompt asked for, and a subagent has no sandbox to stop the
# network. These rules deny the plain forms of a push outright. They are a best effort: a push hidden
# behind a script or an alias is not caught.
CLAUDE_EDIT_DENY_RULES: tuple[str, ...] = (
    "Bash(git push)",
    "Bash(git push *)",
    "Bash(git -C * push)",
    "Bash(git -C * push *)",
    "PowerShell(git push)",
    "PowerShell(git push *)",
    "PowerShell(git -C * push)",
    "PowerShell(git -C * push *)",
)

_CLAUDE_LIMIT_RE = re.compile(r"\b(hit|reached)\s+your\s+(\w+\s+)?limit\b|\bout\s+of\s+extra\s+usage\b", re.I)

CLAUDE_QUOTA_NOTE = (
    "Claude Code has no command that prints the plan limits. This pool's reading is what the Claude "
    "jobs the bridge ran reported: each window is kept until its own reset passes, and the pool is "
    "unknown when none is left. Claude jobs draw on the same plan limits as your own Claude Code and "
    "claude.ai use."
)
CLAUDE_NO_READING = "Claude Code prints none; it shows after a Claude job runs through the bridge"
CLAUDE_SHARED_NOTE = (
    "Claude plan: as its bridge jobs reported it, at the read time or later. Your own Claude Code and "
    "claude.ai use draw on it too."
)
_CLAUDE_WINDOW_LABELS = {"five_hour": "5h", "seven_day": "weekly"}


def split_claude_model_and_effort(model: str | None) -> tuple[str | None, EffortLevel | None]:
    """Split `opus-high` or `claude-sonnet-medium` into `("opus", "high")`; leave full model ids alone.

    The `claude` CLI takes the family name and the effort as separate flags, while the other backends
    accept `<model>-<effort>` shortcuts. Only a known family name followed by a known effort is split.
    """
    from agent_bridge.config import EFFORT_ALIASES

    if model is None or not model.strip():
        return None, None
    raw = model.strip()
    key = raw.lower()
    if key.startswith("claude-") and key[len("claude-"):].split("-")[0] in CLAUDE_MODEL_ALIASES:
        key = key[len("claude-"):]
    if key in CLAUDE_MODEL_ALIASES:
        return key, None
    family, _, tail = key.partition("-")
    if family in CLAUDE_MODEL_ALIASES and tail in EFFORT_ALIASES:
        return family, EFFORT_ALIASES[tail]
    return raw, None


def _without_shell_shim(path: Path) -> list[str]:
    """Return a command for `path` that Windows can start without a shell, or raise.

    An npm install leaves a `.cmd` shim. Windows starts a batch file through `cmd.exe`, which would read
    the job's own arguments (a model name, a session id) as shell text. So the script behind the shim is
    run with `node` instead, and a shim that cannot be resolved that way is refused.
    """
    if sys.platform != "win32" or path.suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return [str(path)]
    cli_js = path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    node_bin = shutil.which("node")
    if cli_js.exists() and node_bin:
        return [node_bin, str(cli_js)]
    raise FileNotFoundError(
        f"{str(path)!r} is a shell shim, which the bridge will not start. Set AGENT_BRIDGE_CLAUDE_BIN to "
        "claude.exe (the native install puts it in ~/.local/bin)."
    )


def resolve_claude_command(configured: str | None = None) -> list[str]:
    """Locate the `claude` executable without going through a shell."""
    if configured:
        cand = Path(configured).expanduser()
        if cand.exists():
            return _without_shell_shim(cand)
        found_cfg = shutil.which(configured)
        if found_cfg:
            return _without_shell_shim(Path(found_cfg))
        raise FileNotFoundError(
            f"Configured AGENT_BRIDGE_CLAUDE_BIN does not exist: {configured!r}"
        )

    native = Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    found = shutil.which("claude")
    if not found:
        if native.exists():
            return [str(native)]
        raise FileNotFoundError(
            "Could not find `claude` CLI in PATH. Install Claude Code or set AGENT_BRIDGE_CLAUDE_BIN."
        )
    try:
        return _without_shell_shim(Path(found))
    except FileNotFoundError:
        if native.exists():
            return [str(native)]
        raise


def parse_claude_version(text: str | None) -> tuple[int, ...] | None:
    """`2.1.332 (Claude Code)` -> `(2, 1, 332)`."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(g) for g in match.groups()) if match else None


def normalize_claude_usage(
    raw_usage: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Normalize Claude Code token counters while preserving provider-specific metrics."""
    if not raw_usage or not isinstance(raw_usage, dict):
        return None, None
    # Claude Code reports uncached input, cache reads and cache writes as three separate counters.
    # They are added up here so that, as for Codex, `cached_tokens` is a subset of `input_tokens`.
    uncached = to_int(raw_usage, "input_tokens")
    output_tokens = to_int(raw_usage, "output_tokens")
    cache_read = to_int(raw_usage, "cache_read_input_tokens")
    cache_write = to_int(raw_usage, "cache_creation_input_tokens")
    details = raw_usage.get("output_tokens_details")
    thinking = to_int(details, "thinking_tokens") if isinstance(details, dict) else 0
    all_input = uncached + cache_read + cache_write
    total = all_input + output_tokens
    normalized: dict[str, Any] = {
        "input_tokens": all_input,
        "output_tokens": output_tokens,
        "cached_tokens": cache_read,
        "reasoning_tokens": thinking,
        "total_tokens": total,
    }
    if cache_write:
        normalized["cache_write_tokens"] = cache_write
    provider_metrics: dict[str, Any] = {
        "provider": "claude",
        "input_tokens": uncached,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking,
        "total_tokens": total,
    }
    return normalized, provider_metrics


def claude_quota_from_rate_limit(
    info: Any,
    *,
    fetched_at: str | None = None,
) -> dict[str, Any] | None:
    """Turn the `rate_limit_info` of a `claude -p` stream into a quota reading. None if it holds no window."""
    from agent_bridge.quota import _as_number, _epoch_to_iso, _iso_now

    if not isinstance(info, dict):
        return None

    raw_windows: dict[str, Any] = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        raw_windows.update({str(k): v for k, v in unified.items() if isinstance(v, dict)})
    kind = info.get("rateLimitType")
    if isinstance(kind, str) and kind and kind not in raw_windows:
        raw_windows[kind] = info

    windows: list[dict[str, Any]] = []
    for key, raw in raw_windows.items():
        used = _as_number(raw.get("utilization"))
        if used is None or used < 0.0:
            continue
        used_pct = round(min(100.0, used * 100.0), 1)
        windows.append(
            {
                "id": key,
                "window": _CLAUDE_WINDOW_LABELS.get(key, key),
                "used_percent": used_pct,
                "remaining_percent": round(max(0.0, 100.0 - used_pct), 1),
                "reset_time": _epoch_to_iso(raw.get("resetsAt")),
            }
        )
    if not windows:
        return None

    return {
        "backend": "claude",
        "pool": "claude",
        "status": "ok",
        "source": "last_claude_job",
        "fetched_at": fetched_at or _iso_now(),
        "rate_limit_reached": info.get("status") == "rejected",
        "windows": windows,
        "note": CLAUDE_QUOTA_NOTE,
    }


def merge_claude_quota(
    previous: dict[str, Any] | None,
    new: dict[str, Any],
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Keep the windows an older reading had and the new one does not mention, while they are still current.

    A Claude job reports only the window that is closest to its limit, so one job's reading is partial.
    A window that is kept carries the time it was read, which is older than the merged reading's.
    """
    from agent_bridge.quota import _parse_iso_utc

    now = now_utc or datetime.now(timezone.utc)
    merged = {k: v for k, v in new.items() if k not in {"cached", "cache_age_seconds"}}
    seen = {str(w.get("id")) for w in merged.get("windows") or []}
    kept: list[dict[str, Any]] = []
    if isinstance(previous, dict) and previous.get("status") == "ok":
        for w in previous.get("windows") or []:
            if not isinstance(w, dict) or str(w.get("id")) in seen:
                continue
            reset_dt = _parse_iso_utc(w.get("reset_time"))
            if reset_dt is None or (reset_dt - now).total_seconds() <= 0:
                continue
            carried = {
                k: w[k] for k in ("id", "window", "used_percent", "remaining_percent", "reset_time") if k in w
            }
            carried["fetched_at"] = w.get("fetched_at") or previous.get("fetched_at")
            kept.append(carried)
    merged["windows"] = [*(merged.get("windows") or []), *kept]
    return merged


def parse_claude_stream_events(lines: list[str]) -> dict[str, Any]:
    """Reduce `claude -p --output-format stream-json` output to what the bridge records."""
    session_id: str | None = None
    model: str | None = None
    api_key_source: str | None = None
    last_text = ""
    result_event: dict[str, Any] | None = None
    rate_limit: dict[str, Any] | None = None

    for raw in lines:
        stripped = raw.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            session_id = sid
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            if isinstance(event.get("model"), str) and event["model"]:
                model = event["model"]
            if isinstance(event.get("apiKeySource"), str) and event["apiKeySource"]:
                api_key_source = event["apiKeySource"]
        elif etype == "assistant":
            if event.get("parent_tool_use_id"):
                continue  # text written by a subagent of the subagent, not the reply
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                parts = [
                    c["text"]
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)
                ]
                if parts:
                    last_text = "\n\n".join(parts)
        elif etype == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, dict):
                rate_limit = info
        elif etype == "result":
            result_event = event

    errors: list[str] = []
    usage: dict[str, Any] | None = None
    model_usage: dict[str, Any] | None = None
    is_error = False
    response = last_text
    denials: list[str] = []
    num_turns: int | None = None
    cost_usd: float | None = None
    subtype: str | None = None
    # None when Claude Code did not report it, which is not the same as 0.
    native_subagents: int | None = None

    if result_event is not None:
        subtype = result_event.get("subtype") if isinstance(result_event.get("subtype"), str) else None
        is_error = bool(result_event.get("is_error")) or (subtype is not None and subtype != "success")
        raw_usage = result_event.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) and raw_usage else None
        raw_model_usage = result_event.get("modelUsage")
        model_usage = raw_model_usage if isinstance(raw_model_usage, dict) else None
        result_text = result_event.get("result")
        if isinstance(result_text, str) and result_text.strip():
            if is_error:
                errors.append(result_text.strip())
            else:
                response = result_text
        # Some failures carry their text only in an `errors` list.
        listed = result_event.get("errors")
        if is_error and isinstance(listed, list):
            for item in listed:
                text = item if isinstance(item, str) else (item.get("message") if isinstance(item, dict) else None)
                if isinstance(text, str) and text.strip() and text.strip() not in errors:
                    errors.append(text.strip())
        api_status = result_event.get("api_error_status")
        if api_status not in (None, "", 0):
            errors.append(f"API error {api_status}")
        stats = result_event.get("subagent_stats")
        spawned = stats.get("spawned") if isinstance(stats, dict) else None
        if isinstance(spawned, int) and not isinstance(spawned, bool):
            native_subagents = spawned
        if is_error and not errors:
            errors.append(f"claude ended with {subtype or 'an error'}")
        raw_denials = result_event.get("permission_denials")
        if isinstance(raw_denials, list):
            denials = [
                str(d.get("tool_name"))
                for d in raw_denials
                if isinstance(d, dict) and d.get("tool_name")
            ]
        turns = result_event.get("num_turns")
        if isinstance(turns, int) and not isinstance(turns, bool):
            num_turns = turns
        cost = result_event.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            cost_usd = float(cost)

    if isinstance(rate_limit, dict) and rate_limit.get("status") == "rejected" and is_error:
        errors.append(f"usage limit reached ({rate_limit.get('rateLimitType') or 'plan limit'})")

    return {
        "session_id": session_id,
        "model": model,
        "response": response.strip(),
        "usage": usage,
        "model_usage": model_usage,
        "result_seen": result_event is not None,
        "is_error": is_error,
        "errors": errors,
        "rate_limit": rate_limit,
        "permission_denials": denials,
        "num_turns": num_turns,
        "cost_usd": cost_usd,
        "subtype": subtype,
        "api_key_source": api_key_source,
        "native_subagents": native_subagents,
    }


def _sum_model_usage(model_usage: dict[str, Any] | None) -> dict[str, int] | None:
    """Return whole-job counters only when every model reports its token counts."""
    if not model_usage:
        return None
    fields = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "cacheReadInputTokens": "cache_read_input_tokens",
        "cacheCreationInputTokens": "cache_creation_input_tokens",
    }
    totals = dict.fromkeys(fields.values(), 0)
    for counters in model_usage.values():
        if not isinstance(counters, dict):
            return None
        for field, name in fields.items():
            count = counters.get(field)
            if type(count) is not int or count < 0:
                return None
            totals[name] += count
    return totals


def _describe_claude_event(event: dict[str, Any]) -> str | None:
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        return "claude session started"
    if etype == "assistant" and not event.get("parent_tool_use_id"):
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name"):
                    return f"tool: {c['name']}"
            if any(isinstance(c, dict) and c.get("type") == "text" for c in content):
                return "writing reply"
    if etype == "result":
        return "finishing"
    return None


def build_claude_args(
    request: RunRequest,
    *,
    base: list[str],
    effective_model: str | None,
    effective_effort: str | None,
) -> list[str]:
    """Build the `claude` argv. The prompt always goes in on stdin."""
    args = [
        *base,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        # No MCP servers at all: the subagent cannot reach the user's connectors, or this bridge.
        "--strict-mcp-config",
        # Nobody is there to answer a prompt, so whatever would ask is denied.
        "--permission-prompts",
        "none",
        "--permission-mode",
        CLAUDE_READ_ONLY_PERMISSION_MODE if request.mode == "read_only" else CLAUDE_EDIT_PERMISSION_MODE,
    ]
    if request.mode == "read_only":
        args.extend(["--disallowedTools", *CLAUDE_READ_ONLY_REMOVED_TOOLS])
    else:
        args.extend(["--disallowedTools", *CLAUDE_EDIT_DENY_RULES])
    if effective_model:
        args.extend(["--model", effective_model])
    if effective_effort:
        args.extend(["--effort", effective_effort])
    if request.session_id:
        args.extend(["--resume", request.session_id])
    for directory in request.extra_dirs:
        args.extend(["--add-dir", directory])
    return args


class ClaudeRunner:
    """Executes subagent turns via the Claude Code CLI in print mode."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve_command(self) -> list[str]:
        return resolve_claude_command(self._settings.claude_bin)

    async def check_health(self) -> dict[str, Any]:
        from agent_bridge.runners import _check_cli_health

        try:
            cmd = self.resolve_command()
        except Exception as exc:
            return {
                "command_found": False,
                "healthy": False,
                "available": False,
                "command": None,
                "version": None,
                "error": str(exc),
            }
        healthy, version, err = await _check_cli_health(cmd)
        available = healthy
        parsed = parse_claude_version(version)
        if healthy and parsed is not None and parsed < CLAUDE_MIN_VERSION:
            available = False
            err = (
                f"Claude Code {'.'.join(map(str, parsed))} is too old for the bridge: it needs "
                f"{'.'.join(map(str, CLAUDE_MIN_VERSION))} or newer (for --permission-prompts). Update it."
            )
        return {
            "command_found": True,
            "healthy": healthy,
            "available": available,
            "command": cmd,
            "version": version,
            "auth": self._settings.claude_auth,
            "error": err,
        }

    def effective_model_and_effort(
        self,
        requested_model: str | None,
        requested_effort: str | None,
    ) -> tuple[str | None, str | None]:
        """Resolve the model and effort to pass. None means: leave it to the CLI's own default."""
        raw = (requested_model or "").strip()
        effort_from_name: str | None = None
        if raw.lower() in {"default", "latest", "auto", "inherit", "cli"}:
            model: str | None = None
        elif raw:
            model, effort_from_name = split_claude_model_and_effort(raw)
        else:
            model = self._settings.default_claude_model

        if requested_effort and requested_effort.strip():
            effort: str | None = requested_effort.strip().lower()
        elif effort_from_name:
            effort = effort_from_name
        else:
            effort = self._settings.default_claude_effort
        return model, effort

    def build_args(
        self,
        request: RunRequest,
        *,
        effective_model: str | None,
        effective_effort: str | None,
    ) -> list[str]:
        """Build the `claude` argv. The prompt always goes in on stdin."""
        return build_claude_args(
            request,
            base=self.resolve_command(),
            effective_model=effective_model,
            effective_effort=effective_effort,
        )

    def build_env(self, request: RunRequest) -> dict[str, str]:
        from agent_bridge.nesting import CLAUDE_SESSION_ENV, subagent_env

        drop: tuple[str, ...] = CLAUDE_SESSION_ENV
        if self._settings.claude_auth != "inherit":
            # With one of these set, `claude` bills that API account even when it is signed in to a plan.
            # The job is filed under the plan's pool, so the variable is left out unless asked for.
            drop = (*drop, *CLAUDE_API_CREDENTIAL_ENV)
        return subagent_env(job_id=request.job_id, owner_id=request.owner_id, drop=drop)

    async def _run_once(
        self,
        request: RunRequest,
        *,
        effective_model: str | None,
        effective_effort: str | None,
        model_notice: str | None,
        deadline: float,
        on_session: OnSessionCallback | None,
        on_activity: OnActivityCallback | None,
        on_model: OnModelCallback | None,
        on_provider_start: OnProviderStartCallback | None = None,
    ) -> RunResult:
        import asyncio

        from agent_bridge import runners
        from agent_bridge.models import RunCancelled, RunResult, UsageStatus
        from agent_bridge.process_control import (
            ProcessContainmentError,
            ProcessStartupTimeout,
            run_managed_process_io,
            spawn_managed_process,
        )

        started = time.monotonic()
        reported_model = effective_model or "claude-default"
        reported_effort = effective_effort
        if on_model:
            on_model(reported_model, "requested" if effective_model else "default")

        def _early(
            *,
            error: str,
            reason: str,
            state: str,
            guidance: str,
            status: str = "failed",
            provider_invoked: bool = False,
        ) -> RunResult:
            return RunResult(
                status=status,  # type: ignore[arg-type]
                session_id=request.session_id,
                model=reported_model,
                effort=reported_effort,
                model_notice=model_notice,
                effective_cwd=request.cwd,
                worktree_path=request.worktree_path,
                worktree_branch=request.worktree_branch,
                worktree_commit=request.worktree_commit,
                duration_seconds=round(max(0.0, time.monotonic() - started), 2),
                usage_status="missing",
                error=error,
                failure_reason=reason,
                provider_state=state,  # type: ignore[arg-type]
                retry_guidance=guidance,
                provider_invoked=provider_invoked,
            )

        if deadline - started <= 0.05:
            status, reason, pstate, guidance = runners.classify_failure(
                backend="claude",
                exit_code=None,
                error_text="Job deadline expired before execution started.",
                stderr_text=None,
                timed_out_locally=True,
                process_stopped=True,
            )
            return _early(
                error="Job deadline expired before execution started.",
                reason=reason,
                state=pstate,
                guidance=guidance,
                status=status,
            )

        # Fallback worktree provisioning when ClaudeRunner.run() is called without a JobManager.
        if request.worktree and not request.worktree_path:
            wt_job_id = request.job_id or f"claude-{int(time.time() * 1000)}"
            wt_info = await runners.prepare_job_worktree(
                job_id=wt_job_id,
                cwd=request.cwd,
                state_dir=self._settings.state_dir,
                deadline=deadline,
            )
            request.cwd = str(wt_info["effective_cwd"] or request.cwd)
            request.worktree_path = wt_info["worktree_path"]
            request.worktree_branch = wt_info["worktree_branch"]
            request.worktree_commit = wt_info["worktree_commit"]
            request.git_common_dir = wt_info["git_common_dir"]

        cmd = self.build_args(
            request,
            effective_model=effective_model,
            effective_effort=effective_effort,
        )
        if on_activity:
            on_activity(f"running claude ({reported_model})")

        # Commit launch intent in SQLite BEFORE spawning the provider subprocess.
        await runners._maybe_await(on_provider_start)

        env = self.build_env(request)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessStartupTimeout(
                    "Job deadline expired before execution started.", process_stopped=True,
                )
            proc, managed = await spawn_managed_process(
                *cmd,
                cwd=request.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                timeout=remaining,
                env=env,
            )
        except asyncio.TimeoutError as exc:
            process_stopped = isinstance(exc, ProcessStartupTimeout) and exc.process_stopped
            message = f"Claude subagent timed out during startup after {request.timeout_seconds}s."
            status, reason, pstate, guidance = runners.classify_failure(
                backend="claude",
                exit_code=None,
                error_text=message,
                stderr_text=None,
                timed_out_locally=True,
                process_stopped=process_stopped,
            )
            return _early(
                error=message,
                reason=reason,
                state=pstate,
                guidance=guidance,
                status=status,
                provider_invoked=not process_stopped,
            )
        except ProcessContainmentError as exc:
            return _early(
                error=str(exc),
                reason="launch_error",
                state="possibly_running" if exc.process_still_alive else "stopped",
                guidance=(
                    "Windows process containment failed before launching Claude; worker was terminated "
                    "while still suspended. Inspect host Job Object permissions/environment and retry."
                ),
            )

        prompt_bytes = request.prompt.encode("utf-8")
        stdout_lines: list[str] = []
        stderr_chunks: list[bytes] = []

        async def _write_stdin() -> None:
            if proc.stdin is None:
                return
            try:
                proc.stdin.write(prompt_bytes)
                await proc.stdin.drain()
            except Exception as exc:
                logger.warning("Failed writing prompt to claude stdin: %s", exc)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        def _handle_stdout_line(line: str) -> None:
            nonlocal reported_model
            stdout_lines.append(line)
            stripped = line.strip()
            if not stripped.startswith("{"):
                return
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                return
            if not isinstance(event, dict):
                return
            if event.get("type") == "system" and event.get("subtype") == "init":
                sid = event.get("session_id")
                if isinstance(sid, str) and sid and on_session:
                    on_session(sid)
                live_model = event.get("model")
                if isinstance(live_model, str) and live_model:
                    reported_model = live_model
                    if on_model:
                        on_model(live_model, "observed")
            if on_activity:
                activity = _describe_claude_event(event)
                if activity:
                    on_activity(activity)

        async def _read_stdout() -> None:
            assert proc.stdout is not None
            buf = bytearray()
            try:
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    while True:
                        nl_idx = buf.find(b"\n")
                        if nl_idx == -1:
                            break
                        raw_line = bytes(buf[: nl_idx + 1])
                        del buf[: nl_idx + 1]
                        _handle_stdout_line(raw_line.decode("utf-8", errors="replace"))
            except asyncio.CancelledError:
                if buf:
                    tail = bytes(buf)
                    buf.clear()
                    _handle_stdout_line(tail.decode("utf-8", errors="replace"))
                raise
            if buf:
                _handle_stdout_line(bytes(buf).decode("utf-8", errors="replace"))

        async def _read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(8192)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        cancelled = False
        timed_out_locally = False
        process_stopped = True
        try:
            wait_budget = deadline - time.monotonic()
            if wait_budget <= 0:
                timed_out_locally = True
                managed.work_stopped_at = time.monotonic()
                process_stopped = await managed.terminate()
            else:
                process_stopped = await run_managed_process_io(
                    proc,
                    managed,
                    read_stdout=_read_stdout,
                    read_stderr=_read_stderr,
                    write_stdin=_write_stdin,
                    timeout=wait_budget,
                )
        except asyncio.TimeoutError:
            timed_out_locally = True
            process_stopped = managed.verified_stopped and (not managed.is_alive())
        except asyncio.CancelledError:
            cancelled = True

        work_end = managed.work_stopped_at if managed.work_stopped_at is not None else time.monotonic()
        elapsed = round(max(0.0, work_end - started), 2)
        cleanup_secs = round(managed.last_cleanup_seconds, 2) if managed.last_cleanup_seconds > 0 else (
            0.0 if timed_out_locally else None
        )
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        stderr_tail = runners._tail(stderr_text)
        parsed = parse_claude_stream_events(stdout_lines)
        session_id = parsed["session_id"] or request.session_id
        response: str = parsed["response"]
        errors: list[str] = parsed["errors"]
        if parsed["model"]:
            reported_model = parsed["model"]
        if (not timed_out_locally) and proc.returncode == 0 and not parsed["result_seen"] and not errors:
            # A run that ends well always ends with a `result` event. Without one the output was cut
            # short, whatever the exit code says. Any partial reply is kept on the failed job.
            errors = ["claude ended without a result event; its output was cut short"]
        turn_completed_cleanly = (
            (not timed_out_locally)
            and (proc.returncode == 0 or cancelled)
            and parsed["result_seen"]
            and (not parsed["is_error"])
            and (not errors)
        )
        key_source = parsed["api_key_source"]
        if key_source and key_source != "none":
            billing_notice = (
                f"This run was billed to an API key ({key_source}), not to the Claude plan the `claude` "
                "pool stands for."
            )
            model_notice = f"{model_notice} {billing_notice}" if model_notice else billing_notice

        usage, provider_metrics = normalize_claude_usage(parsed["usage"])
        extras: dict[str, Any] = {}
        if parsed["cost_usd"] is not None:
            # Claude Code works this out itself from list prices. It is not a bill. On a resumed
            # session it covers the earlier turns too, unlike the token counts.
            extras["session_cost_usd_estimate"] = parsed["cost_usd"]
        if parsed["num_turns"] is not None:
            extras["num_turns"] = parsed["num_turns"]
        if parsed["native_subagents"] is not None:
            # Kept at 0 too, so that a run without the key reads "not reported" rather than "none".
            extras["native_subagents"] = parsed["native_subagents"]
            if parsed["native_subagents"] > 0:
                extras["usage_scope"] = "main_agent"
                # modelUsage may span earlier turns when resuming. Keep the per-turn usage then.
                if not request.session_id and (whole_job := _sum_model_usage(parsed["model_usage"])) is not None:
                    usage, _ = normalize_claude_usage(whole_job)
                    extras["usage_scope"] = "whole_job"
        usage_status: UsageStatus = "missing"
        if usage is not None:
            usage_status = "exact" if turn_completed_cleanly else "partial"
        if key_source and key_source != "none":
            extras["api_key_source"] = key_source
        if parsed["permission_denials"]:
            extras["permission_denials"] = parsed["permission_denials"]
        if parsed["rate_limit"]:
            extras["rate_limit"] = parsed["rate_limit"]
        if extras:
            provider_metrics = {**(provider_metrics or {"provider": "claude"}), **extras}

        effective_cwd = request.cwd
        wt_path = request.worktree_path
        wt_branch = request.worktree_branch
        wt_commit = request.worktree_commit
        if not cancelled and not timed_out_locally and wt_path and Path(wt_path).is_dir():
            if deadline - time.monotonic() > 0.05:
                try:
                    new_branch, new_commit, _ = await runners._inspect_git_checkout(
                        wt_path, timeout_seconds=5.0, deadline=deadline, include_common_dir=False,
                    )
                except asyncio.CancelledError:
                    cancelled = True
                else:
                    wt_branch = new_branch or wt_branch
                    wt_commit = new_commit or wt_commit

        if cancelled:
            raise RunCancelled(
                RunResult(
                    status="cancelled",
                    session_id=session_id,
                    usage=usage,
                    provider_metrics=provider_metrics,
                    usage_status=usage_status,
                )
            )

        def _final(**fields: Any) -> RunResult:
            return RunResult(
                response=response,
                session_id=session_id,
                model=reported_model,
                effort=reported_effort,
                model_notice=model_notice,
                effective_cwd=effective_cwd,
                worktree_path=wt_path,
                worktree_branch=wt_branch,
                worktree_commit=wt_commit,
                duration_seconds=elapsed,
                cleanup_seconds=cleanup_secs,
                usage=usage,
                provider_metrics=provider_metrics,
                usage_status=usage_status,
                stderr_tail=stderr_tail,
                provider_invoked=True,
                **fields,
            )

        if timed_out_locally:
            message = f"Claude subagent timed out after {request.timeout_seconds}s."
            status, reason, pstate, guidance = runners.classify_failure(
                backend="claude",
                exit_code=proc.returncode,
                error_text=message,
                stderr_text=stderr_text,
                timed_out_locally=True,
                process_stopped=process_stopped,
            )
            return _final(
                status=status,
                exit_code=proc.returncode,
                error=message,
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
            )

        if turn_completed_cleanly and not response and parsed["permission_denials"]:
            # Everything it tried was denied and it said nothing: a failure, not an empty success.
            denied = ", ".join(sorted(set(parsed["permission_denials"])))
            return _final(
                status="failed",
                exit_code=proc.returncode,
                error=f"Claude was denied {denied} and ended the turn with no reply.",
                failure_reason="permission_error",
                provider_state="stopped" if process_stopped else "possibly_running",
                retry_guidance=(
                    "The subagent lacked permission for what it attempted. Retry with mode='edit' if "
                    "writes were intended, or ask for the answer without changing anything."
                ),
            )

        if turn_completed_cleanly:
            return _final(
                status="completed",
                exit_code=0,
                provider_state="stopped" if process_stopped else "possibly_running",
            )

        err_msg = "; ".join(errors) if errors else (
            stderr_tail or f"claude exited with code {proc.returncode}"
        )
        classify_text = err_msg
        if _CLAUDE_LIMIT_RE.search(f"{err_msg}\n{stderr_text}"):
            classify_text = f"{err_msg} (usage limit reached)"
        status, reason, pstate, guidance = runners.classify_failure(
            backend="claude",
            exit_code=proc.returncode,
            error_text=classify_text,
            stderr_text=stderr_text,
            process_stopped=process_stopped,
        )
        return _final(
            status=status,
            exit_code=proc.returncode,
            error=err_msg,
            failure_reason=reason,
            provider_state=pstate,
            retry_guidance=guidance,
        )

    async def run(
        self,
        request: RunRequest,
        *,
        deadline: float | None = None,
        prior_watermark: dict[str, Any] | None = None,
        on_session: OnSessionCallback | None = None,
        on_activity: OnActivityCallback | None = None,
        on_model: OnModelCallback | None = None,
        on_provider_start: OnProviderStartCallback | None = None,
        on_attempt: OnAttemptCallback | None = None,
    ) -> RunResult:
        import asyncio

        from agent_bridge import runners

        _ = prior_watermark  # Unused: each `claude -p` run reports the usage of that run alone.
        overall_deadline = deadline if deadline is not None else (time.monotonic() + request.timeout_seconds)
        effective_model, effective_effort = self.effective_model_and_effort(request.model, request.effort)

        result = await self._run_once(
            request,
            effective_model=effective_model,
            effective_effort=effective_effort,
            model_notice=None,
            deadline=overall_deadline,
            on_session=on_session,
            on_activity=on_activity,
            on_model=on_model,
            on_provider_start=on_provider_start,
        )

        # One bounded retry for transient service errors on NEW read-only tasks only.
        if (
            request.mode == "read_only"
            and result.status == "failed"
            and result.failure_reason == "transient_service"
            and runners._can_auto_retry_attempt(request, result, kind="transient")
            and (overall_deadline - time.monotonic()) > runners.TRANSIENT_RETRY_MIN_REMAINING_SECONDS
        ):
            # Record Attempt 1 BEFORE the backoff so that cancelling during it keeps Attempt 1's usage.
            await runners._maybe_await(on_attempt, runners._make_attempt_summary(1, result, backend="claude"))
            if on_activity:
                on_activity("transient service error; retrying once")
            await asyncio.sleep(runners.TRANSIENT_RETRY_BACKOFF_SECONDS)
            second = await self._run_once(
                request,
                effective_model=effective_model,
                effective_effort=effective_effort,
                model_notice=None,
                deadline=overall_deadline,
                on_session=on_session,
                on_activity=on_activity,
                on_model=on_model,
                on_provider_start=on_provider_start,
            )
            return runners._merge_retry_results(result, second, backend="claude")

        return result


class ClaudeAdapter(BackendAdapter):
    """Adapter for the Claude Code CLI (`claude -p`)."""

    name = "claude"
    cli_name = "claude"
    aliases = ("claude-code", "claude_code", "anthropic")
    dashboard_order = 30
    advisory_order = 20
    missing_terminal_event_name = "result"
    capabilities = Capabilities(
        resume=True,
        worktree=True,
        builtin_review=False,
        model_catalog=False,
        fast_tier=False,
        extra_dirs=True,
        extra_dirs_on_resume=True,
        reports_observed_model=True,
        model_encodes_effort=False,
        cumulative_session_usage=False,
        tracks_native_subagents=True,
    )
    quota_pools = (
        QuotaPoolSpec(
            pool="claude",
            backend="claude",
            label="Claude Code",
            dashboard_label="Claude plan",
            shared_with_own_use=True,
            no_reading_reason=CLAUDE_NO_READING,
            quota_view_no_reading="no reading: no Claude job has reported one",
            shared_note=CLAUDE_SHARED_NOTE,
            shared_note_suffix="shared with your own Claude sessions",
            shared_advisory_note=(
                "It is the same allowance the user's own Claude sessions use: "
                "spend it only when the task calls for Claude."
            ),
            default_source="last_claude_job",
            advisory_models_fallback=(f"{DEFAULT_CLAUDE_MODEL} ({DEFAULT_CLAUDE_EFFORT})",),
            quota_display_order=40,
        ),
    )

    @property
    def default_model_label(self) -> str:
        return "Claude's default model"

    def defaults(self, settings: Settings) -> BackendDefaults:
        return BackendDefaults(
            model=settings.default_claude_model,
            effort=settings.default_claude_effort,
            timeout_seconds=settings.default_timeout_seconds,
        )

    def resolve_model_and_effort(
        self,
        settings: Settings,
        requested_model: str | None,
        requested_effort: str | None,
    ) -> tuple[str | None, str | None]:
        return ClaudeRunner(settings).effective_model_and_effort(requested_model, requested_effort)

    def resolve_command(self, settings: Settings) -> list[str]:
        return resolve_claude_command(settings.claude_bin)

    def build_command(
        self,
        request: RunRequest,
        *,
        cmd: list[str],
        model: str | None = None,
        effort: str | None = None,
        **kwargs: Any,
    ) -> CommandSpec:
        _ = kwargs
        argv = build_claude_args(
            request, base=cmd, effective_model=model, effective_effort=effort,
        )
        return CommandSpec(argv=argv, stdin_text=request.prompt)

    def parse_stream(
        self,
        lines: list[str],
        *,
        request: RunRequest | None = None,
    ) -> ParsedStream:
        _ = request
        parsed = parse_claude_stream_events(lines)
        return ParsedStream(
            session_id=parsed["session_id"],
            model=parsed["model"],
            response=parsed["response"],
            usage=parsed["usage"],
            terminal_event_seen=bool(parsed["result_seen"]),
            is_error=bool(parsed["is_error"]),
            errors=tuple(parsed["errors"]),
            extra=parsed,
        )

    def describe_event(self, event: dict[str, Any]) -> str | None:
        return _describe_claude_event(event)

    def normalize_usage(
        self,
        raw_usage: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return normalize_claude_usage(raw_usage)

    async def probe_backend(
        self,
        runner: Any,
        settings: Settings,
        *,
        refresh_models: bool = False,
    ) -> dict[str, Any]:
        _ = refresh_models
        try:
            claude_health = await runner.check_health()
        except Exception as exc:
            claude_health = unhealthy_payload(exc)
        return {
            "backend": "claude",
            "cli": "claude",
            "quota_pool": "claude",
            "default_model": settings.default_claude_model,
            "default_effort": settings.default_claude_effort,
            "model_names": ["sonnet", "opus", "haiku", "fable", "<full model id>"],
            **claude_health,
        }

    async def read_quota(
        self,
        runner: Any,
        *,
        store: Any,
        db_call: Callable[..., Awaitable[Any]],
        ttl_seconds: float,
        save_to_cache: bool = True,
    ) -> dict[str, Any] | None:
        from agent_bridge.quota import enrich_quota_reading, without_passed_windows

        _ = (runner, ttl_seconds, save_to_cache)
        # Claude Code has no command that prints the plan limits. The reading is whatever the
        # Claude jobs' output carried, however old, each window until its own reset passes.
        last = without_passed_windows(
            await db_call(store.get_cached_quota, self.name, float("inf"))
        )
        if last is None:
            return {
                "backend": "claude",
                "pool": "claude",
                "status": "unknown",
                "source": "last_claude_job",
                "note": CLAUDE_QUOTA_NOTE,
            }
        return enrich_quota_reading(last)

    def extract_job_quota(self, provider_metrics: dict[str, Any] | None) -> dict[str, Any] | None:
        info = provider_metrics.get("rate_limit") if isinstance(provider_metrics, dict) else None
        return claude_quota_from_rate_limit(info)

    def merge_cached_quota(
        self,
        previous: dict[str, Any] | None,
        new: dict[str, Any],
    ) -> dict[str, Any]:
        return merge_claude_quota(previous, new)

    def create_runner(self, settings: Settings) -> ClaudeRunner:
        return ClaudeRunner(settings)
