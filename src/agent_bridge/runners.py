"""Subprocess runners for Antigravity (`agy`) and Codex (`codex`) CLIs."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from agent_bridge.config import Settings, classify_quota_pool, read_codex_local_defaults
from agent_bridge.model_resolver import (
    DEFAULT_INTENT_TOKENS,
    ModelPoolUnavailableError,
    NeonModelCatalog,
    is_model_unavailable_error,
    is_newer_codex_model_in_same_family,
)
from agent_bridge.models import JobStatus, ProviderState, RunCancelled, RunRequest, RunResult, UsageStatus
from agent_bridge.nesting import credential_env_names, subagent_env
from agent_bridge.process_control import (
    ManagedProcess,
    ProcessContainmentError,
    ProcessStartupTimeout,
    communicate_managed,
    run_managed_process_io,
    spawn_managed_process,
)
from agent_bridge.usage import (
    compute_agy_usage_delta,
    normalize_usage,
    sum_agy_step_usages,
)

logger = logging.getLogger("agent_bridge.runners")

AGY_MAX_INLINE_PROMPT_CHARS = 20_000
AGY_READ_ONLY_GUARD = (
    "This is a read-only run. Do not create, edit, move or delete any file, and do not run any command "
    "that changes anything. Put your whole answer in your final reply, and end it with one line saying "
    "whether you changed any file.\n\n"
)
# A run that exits 0 but whose output stops before the CLI's final event was cut short (a crash, a
# killed pipe, or a CLI upgrade that changed the stream). It is a failure, as `claude` already treats it.
CLI_OUTPUT_CUT_SHORT_ERROR = (
    "the CLI's output ended without its final event ({event}), so the run may have been cut short; "
    "any partial reply is kept"
)
CLI_OUTPUT_CUT_SHORT_GUIDANCE = (
    "The CLI exited cleanly, but its output stopped before its final event. Read response and "
    "stderr_tail, check the CLI's version with backends, and for edit tasks inspect git status/diff "
    "before retrying."
)
STDERR_TAIL_MAX_CHARS = 4_000
TRANSIENT_RETRY_BACKOFF_SECONDS = 2.0
TRANSIENT_RETRY_MIN_REMAINING_SECONDS = 12.0

OnSessionCallback = Callable[[str], None]
OnActivityCallback = Callable[[str], None]
ModelSource = Literal["observed", "requested", "default"]
OnModelCallback = Callable[[str | None, ModelSource], None]
OnProviderStartCallback = Callable[[], Awaitable[None] | None]
OnAttemptCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
OnWorktreePlannedCallback = Callable[[dict[str, str | None]], None]


def job_env(settings: Settings, request: RunRequest, *, cli: str) -> dict[str, str]:
    """Build a marked Codex or agy environment, filtering by variable name in trim mode.

    The filter logs exclusions and explicit exceptions by name. It does not inspect values such as
    URLs. Claude jobs have their own environment handling in `ClaudeRunner`.
    """
    candidates = credential_env_names(os.environ) if settings.child_env != "inherit" else []
    kept = [name for name in candidates if name in settings.child_env_keep]
    dropped = [name for name in candidates if name not in settings.child_env_keep]
    if kept:
        logger.info(
            "Kept in %s job %s's environment via AGENT_BRIDGE_CHILD_ENV_KEEP: %s.",
            cli, request.job_id, ", ".join(kept),
        )
    if dropped:
        logger.info(
            "Left out of %s job %s's environment, as their names look like credentials: %s. "
            "AGENT_BRIDGE_CHILD_ENV_KEEP exempts named variables; AGENT_BRIDGE_CHILD_ENV=inherit passes them all on.",
            cli,
            request.job_id,
            ", ".join(dropped),
        )
    return subagent_env(job_id=request.job_id, owner_id=request.owner_id, drop=dropped)


async def _maybe_await(cb: Callable[..., Any] | None, *args: Any) -> None:
    if cb is None:
        return
    res = cb(*args)
    if inspect.isawaitable(res):
        await res


def _has_valid_agy_token_counters(raw_usage: Any) -> bool:
    """Return True only when an `agy` usage dict contains real, non-zero numeric token counters."""
    if not isinstance(raw_usage, dict) or not raw_usage:
        return False
    vals: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"):
        v = raw_usage.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            return False
        vals[key] = int(v)
    if not vals:
        return False
    total = vals.get("total_tokens", 0)
    io_sum = vals.get("input_tokens", 0) + vals.get("output_tokens", 0)
    return total > 0 or io_sum > 0


def _is_valid_agy_cumulative_checkpoint(
    raw_usage: dict[str, Any] | None,
    prior_watermark: dict[str, Any] | None,
) -> bool:
    """Verify `raw_usage` is a non-empty, non-zero, monotonic cumulative checkpoint."""
    if not _has_valid_agy_token_counters(raw_usage):
        return False
    assert isinstance(raw_usage, dict)
    if prior_watermark is not None and isinstance(prior_watermark.get("cumulative"), dict):
        if compute_agy_usage_delta(raw_usage, prior_watermark["cumulative"]) is None:
            return False
    return True

_WORKTREE_REPO_LOCKS: dict[str, asyncio.Lock] = {}

_QUOTA_PATTERNS = (
    re.compile(r"\b(quota|rate\s*limit|usage\s*limit|limit\s*reached|too\s+many\s+requests|429)\b", re.I),
    re.compile(r"\bexhausted\b.*\b(quota|limit|capacity)\b", re.I),
)
_AUTH_PATTERNS = (
    re.compile(r"\b(not\s+logged\s+in|unauthorized|authentication\s+failed|login\s+required|token\s+expired|401)\b", re.I),
    re.compile(r"\binvalid\s+(api\s+key|credentials|token)\b", re.I),
)
_INTERACTIVE_FALLBACK_PATTERNS = (
    re.compile(r"--mode\s+\S+\s+is\s+not\s+available", re.I),
    re.compile(r"\bcontinuing\s+in\s+the\s+default\s+mode\b", re.I),
    re.compile(r"\brequires?\s+interactive\s+(approval|confirmation|input)\b", re.I),
)
_PERMISSION_PATTERNS = (
    re.compile(r"\b(permission\s+denied|approval\s+required|read-only\s+file\s*system|sandbox\s+denied)\b", re.I),
)
_TRANSIENT_PATTERNS = (
    re.compile(r"\b(502|503|504|bad\s+gateway|service\s+unavailable|temporarily\s+unavailable|overloaded)\b", re.I),
    re.compile(r"\b(connection\s+reset|econnreset|etimedout|network\s+error|upstream\s+timeout)\b", re.I),
)
_AGY_TIMEOUT_PATTERNS = (
    re.compile(r"\bprint\s+timeout\b", re.I),
    re.compile(r"\bstill\s+in\s+progress\b", re.I),
    re.compile(r"\bturn\s+timed\s+out\b", re.I),
)


def _make_attempt_summary(attempt_num: int, res: RunResult, *, backend: str) -> dict[str, Any]:
    return {
        "attempt": attempt_num,
        "backend": backend,
        "model": res.model,
        "quota_pool": classify_quota_pool(backend, res.model),
        "effort": res.effort,
        "status": res.status,
        "failure_reason": res.failure_reason,
        "provider_state": res.provider_state,
        "duration_seconds": round(float(res.duration_seconds or 0.0), 2),
        "cleanup_seconds": round(float(res.cleanup_seconds or 0.0), 2) if res.cleanup_seconds else 0.0,
        "usage": res.usage,
        "usage_status": res.usage_status,
        "provider_metrics": res.provider_metrics,
        "exit_code": res.exit_code,
        "error": res.error,
    }


def _sum_usage_dicts(first: dict[str, Any] | None, second: dict[str, Any] | None) -> dict[str, Any] | None:
    if first is None:
        return dict(second) if isinstance(second, dict) else None
    if second is None:
        return dict(first)
    merged: dict[str, Any] = {}
    keys = set(first.keys()) | set(second.keys())
    for k in keys:
        v1 = first.get(k)
        v2 = second.get(k)
        n1 = int(v1) if isinstance(v1, (int, float)) and not isinstance(v1, bool) else None
        n2 = int(v2) if isinstance(v2, (int, float)) and not isinstance(v2, bool) else None
        if n1 is not None and n2 is not None:
            merged[k] = n1 + n2
        elif n1 is not None:
            merged[k] = n1
        elif n2 is not None:
            merged[k] = n2
    return merged or None


def _can_auto_retry_attempt(
    request: RunRequest,
    result: RunResult,
    *,
    kind: str,
    prior_watermark: dict[str, Any] | None = None,
) -> bool:
    """Only retry after verifying the prior attempt stopped AND did not advance a resumed session."""
    if result.provider_state != "stopped":
        return False

    obs_turns = result.observed_num_turns if result.observed_num_turns is not None else result.num_turns
    prior_turns = (
        int(prior_watermark["num_turns"])
        if (
            prior_watermark is not None
            and isinstance(prior_watermark.get("num_turns"), int)
            and not isinstance(prior_watermark.get("num_turns"), bool)
        )
        else None
    )
    turns_advanced = bool(
        obs_turns is not None
        and (
            (prior_turns is not None and obs_turns > prior_turns)
            or (request.session_id is not None and prior_turns is None and obs_turns > 1)
        )
    )
    has_turn_progress = bool(
        result.observed_turn_progress
        or turns_advanced
        or (result.response and result.response.strip())
        or result.usage is not None
    )

    if request.session_id is not None:
        # On a resumed session, a transient network/service error may have already been applied
        # server-side, and any turn progress or turn-counter advance means the session may have mutated.
        if kind == "transient":
            return False
        if has_turn_progress:
            return False
        # On a resumed edit with an observed turn count but no baseline watermark, we cannot prove
        # the turn counter did not advance -> fail closed instead of re-running an edit!
        if request.mode == "edit" and obs_turns is not None and prior_turns is None:
            return False
        return True

    if kind == "model_unavailable" and has_turn_progress:
        return False
    return True


def _merge_retry_results(
    first: RunResult,
    second: RunResult,
    *,
    backend: str,
) -> RunResult:
    """Preserve per-attempt duration, cleanup time, and usage (including partial usage) across retries."""
    attempts = [
        _make_attempt_summary(1, first, backend=backend),
        _make_attempt_summary(2, second, backend=backend),
    ]
    second.attempts = attempts
    second.duration_seconds = round(
        float(first.duration_seconds or 0.0) + float(second.duration_seconds or 0.0),
        2,
    )
    if first.cleanup_seconds is not None or second.cleanup_seconds is not None:
        second.cleanup_seconds = round(
            float(first.cleanup_seconds or 0.0) + float(second.cleanup_seconds or 0.0),
            2,
        )
    if first.usage is not None:
        second.usage = _sum_usage_dicts(first.usage, second.usage)
        if first.usage_status in {"partial", "missing"} or second.usage_status == "partial":
            second.usage_status = "partial"
    elif first.failure_reason == "transient_service" and second.usage_status == "exact":
        # Attempt 1 was a transient failure with unknown/missing usage; keep Attempt 2's usage
        # on the final result while preserving Attempt 1 as a separate missing-usage row in SQLite.
        pass
    pm = dict(second.provider_metrics) if isinstance(second.provider_metrics, dict) else {}
    pm["attempts"] = attempts
    second.provider_metrics = pm
    return second


def _tail(text: str, limit: int = STDERR_TAIL_MAX_CHARS) -> str | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return "...[truncated]\n" + cleaned[-limit:]


def _repo_worktree_lock(repo_root_key: str) -> asyncio.Lock:
    lock = _WORKTREE_REPO_LOCKS.get(repo_root_key)
    if lock is None:
        lock = asyncio.Lock()
        _WORKTREE_REPO_LOCKS[repo_root_key] = lock
    return lock


def agy_reports_turn_still_running(exit_code: int | None, text: str) -> bool:
    """Whether `agy` said its turn may still be going on at the server: exit code 3, or words to that effect.

    Stopping the local CLI does not stop that turn, so a job that saw this keeps `possibly_running`.
    """
    return exit_code == 3 or any(p.search(text) for p in _AGY_TIMEOUT_PATTERNS)


def classify_failure(
    *,
    backend: str,
    exit_code: int | None,
    error_text: str | None,
    stderr_text: str | None,
    timed_out_locally: bool = False,
    agy_permission_fallback: bool = False,
    process_stopped: bool = True,
) -> tuple[JobStatus, str, ProviderState, str]:
    """Classify a non-success outcome into `(status, failure_reason, provider_state, retry_guidance)`."""
    combined = f"{error_text or ''}\n{stderr_text or ''}"
    local_state: ProviderState = "stopped" if process_stopped else "possibly_running"

    # Preserve Antigravity's remote in-progress signal EVEN when our local deadline also fired
    # and the local CLI process was verified stopped (killing the CLI does not stop the remote turn!).
    if backend == "neon" and agy_reports_turn_still_running(exit_code, combined):
        return (
            "timed_out",
            "provider_timeout_still_running",
            "possibly_running",
            "Antigravity reported that the server-side turn may still be in progress (even after local "
            "process cleanup). Keep any partial response and session_id; confirm the prior turn finished "
            "(and inspect git diff for edits) before resuming.",
        )

    if timed_out_locally:
        if process_stopped:
            guidance = (
                "Local bridge deadline elapsed and the subprocess tree was verified stopped. "
                "Preserve any partial response and session_id; inspect git diff/worktree before retrying edits."
            )
        else:
            guidance = (
                "Local bridge deadline elapsed, but subprocess termination could not be verified. "
                "Verify whether any child process is still running and inspect git diff/worktree before retrying."
            )
        return ("timed_out", "timeout", local_state, guidance)

    if any(p.search(combined) for p in _AUTH_PATTERNS):
        return (
            "failed",
            "auth_error",
            local_state,
            "CLI authentication failed. Re-authenticate the underlying CLI (`agy`, `codex login` or "
            "`claude auth login`) and retry.",
        )

    if any(p.search(combined) for p in _QUOTA_PATTERNS):
        return (
            "failed",
            "quota_exceeded",
            local_state,
            "Subscription quota or rate limit reached. Call usage(quota=True) to check reset "
            "times or delegate to another backend.",
        )

    if is_model_unavailable_error(combined):
        return (
            "failed",
            "model_unavailable",
            local_state,
            "Requested model was rejected by the CLI. Refresh models via backends(refresh_models=True) "
            "or use a versionless shortcut.",
        )

    if any(p.search(combined) for p in _TRANSIENT_PATTERNS):
        return (
            "failed",
            "transient_service",
            local_state,
            "Transient provider/network error. Safe to retry a read-only task; for edit tasks reconcile "
            "git status/diff before retrying.",
        )

    if agy_permission_fallback or any(p.search(combined) for p in _INTERACTIVE_FALLBACK_PATTERNS):
        return (
            "failed",
            "interactive_fallback",
            local_state,
            "Antigravity fell back to interactive permission prompts in headless mode. For unattended edits "
            "use backend='codex' with mode='edit' (and worktree=True), or keep Neon read-only.",
        )

    if any(p.search(combined) for p in _PERMISSION_PATTERNS):
        return (
            "failed",
            "permission_error",
            local_state,
            "Subagent lacked permission for an attempted action. Retry with mode='edit' (or extra_dirs) "
            "if writes outside read-only scope were intended.",
        )

    return (
        "failed",
        "cli_error",
        local_state,
        "Inspect error and stderr_tail; for edit tasks verify repository state before retrying.",
    )


def resolve_agy_command(configured: str | None = None) -> list[str]:
    """Locate the `agy` executable without going through a shell."""
    if configured:
        cand = Path(configured).expanduser()
        if cand.exists():
            return [str(cand)]
        found_cfg = shutil.which(configured)
        if found_cfg:
            return [found_cfg]
        raise FileNotFoundError(
            f"Configured AGENT_BRIDGE_AGY_BIN does not exist: {configured!r}"
        )

    found = shutil.which("agy")
    if found:
        return [found]

    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidate = Path(local_appdata) / "agy" / "bin" / "agy.exe"
            if candidate.exists():
                return [str(candidate)]

    raise FileNotFoundError(
        "Could not find `agy` CLI in PATH. Install Antigravity CLI or set AGENT_BRIDGE_AGY_BIN."
    )


def _find_windows_native_codex(npm_bin_dir: Path) -> Path | None:
    machine = platform.machine().lower()
    if "arm" in machine or "aarch64" in machine:
        pkg_names = ("codex-win32-arm64", "codex-win32-x64")
        triples = ("aarch64-pc-windows-msvc", "x86_64-pc-windows-msvc")
    else:
        pkg_names = ("codex-win32-x64", "codex-win32-arm64")
        triples = ("x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc")

    codex_pkg = npm_bin_dir / "node_modules" / "@openai" / "codex"
    for pkg_name, triple in zip(pkg_names, triples):
        candidates = [
            codex_pkg / "node_modules" / "@openai" / pkg_name / "vendor" / triple / "bin" / "codex.exe",
            npm_bin_dir / "node_modules" / "@openai" / pkg_name / "vendor" / triple / "bin" / "codex.exe",
            codex_pkg / "vendor" / triple / "bin" / "codex.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
    return None


def resolve_codex_command(configured: str | None = None) -> list[str]:
    """Locate the Codex CLI, bypassing Windows `.cmd` shims when possible."""
    if configured:
        cand = Path(configured).expanduser()
        if cand.exists():
            return [str(cand)]
        found_cfg = shutil.which(configured)
        if found_cfg:
            return [found_cfg]
        raise FileNotFoundError(
            f"Configured AGENT_BRIDGE_CODEX_BIN does not exist: {configured!r}"
        )

    found = shutil.which("codex")
    if not found:
        raise FileNotFoundError(
            "Could not find `codex` CLI in PATH. Install `@openai/codex` or set AGENT_BRIDGE_CODEX_BIN."
        )

    found_path = Path(found)
    if sys.platform == "win32" and found_path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        npm_bin_dir = found_path.parent
        native_exe = _find_windows_native_codex(npm_bin_dir)
        if native_exe is not None:
            return [str(native_exe)]

        codex_js = npm_bin_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node_bin = shutil.which("node")
        if codex_js.exists() and node_bin:
            return [node_bin, str(codex_js)]

    return [str(found_path)]


def _agy_result_envelope(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    event = obj.get("event")
    result = obj.get("result")
    if event == "result" and isinstance(result, dict):
        return result
    cid = obj.get("conversation_id")
    has_response = "response" in obj
    has_status = "status" in obj
    if event in (None, "result") and (
        (has_response and has_status) or (isinstance(cid, str) and cid and (has_response or has_status))
    ):
        return obj
    return None


def parse_agy_json_output(stdout_text: str) -> dict[str, Any] | None:
    """Parse single-envelope `agy --output-format json` stdout, tolerating leading non-JSON lines."""
    stripped = stdout_text.strip()
    if not stripped:
        return None
    try:
        parsed = _agy_result_envelope(json.loads(stripped))
        if parsed is not None:
            return parsed
    except json.JSONDecodeError:
        pass

    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            parsed = _agy_result_envelope(json.loads(line))
            if parsed is not None:
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _plain_stdout_reply(lines: list[str]) -> str:
    return "\n".join(line.rstrip("\r\n") for line in lines if not line.lstrip().startswith("{")).strip()


def parse_codex_jsonl_events(lines: list[str]) -> dict[str, Any]:
    """Parse Codex `--json` JSONL events into session_id, response, usage, and errors.

    `turn_completed` says whether the stream reached its final `turn.completed` event. A `turn.failed`
    with no message is still an error.
    """
    session_id: str | None = None
    messages: list[str] = []
    usage: dict[str, Any] | None = None
    errors: list[str] = []
    turn_completed = False

    for raw in lines:
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        etype = event.get("type")
        if etype == "thread.started":
            tid = event.get("thread_id")
            if isinstance(tid, str) and tid:
                session_id = tid
        elif etype == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    messages.append(text.strip())
        elif etype == "turn.completed":
            turn_completed = True
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = raw_usage
        elif etype in {"error", "turn.failed"}:
            msg = event.get("message")
            if not msg and isinstance(event.get("error"), dict):
                msg = event["error"].get("message")
            if isinstance(msg, str) and msg.strip():
                errors.append(msg.strip())
            elif etype == "turn.failed":
                errors.append("codex reported that the turn failed, and gave no message")

    return {
        "session_id": session_id,
        "response": "\n\n".join(messages).strip(),
        "usage": usage,
        "errors": errors,
        "turn_completed": turn_completed,
    }


def _describe_codex_event(event: dict[str, Any]) -> str | None:
    etype = event.get("type")
    if etype == "thread.started":
        return "session started"
    if etype == "turn.started":
        return "thinking"
    if etype == "item.started":
        item = event.get("item")
        if isinstance(item, dict):
            itype = item.get("type", "step")
            if itype == "command_execution":
                cmd = str(item.get("command", ""))[:80]
                return f"running command: {cmd}" if cmd else "running command"
            return f"working ({itype})"
    if etype == "item.completed":
        item = event.get("item")
        if isinstance(item, dict):
            itype = item.get("type", "step")
            if itype == "agent_message":
                return "drafted response"
            return f"completed {itype}"
    if etype == "turn.completed":
        return "turn completed"
    return None


def _parse_git_worktree_porcelain(output: str) -> list[dict[str, str | None]]:
    entries: list[dict[str, str | None]] = []
    current: dict[str, str | None] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current.get("path"):
                entries.append(current)
            current = {}
            continue
        if line.startswith("worktree "):
            if current.get("path"):
                entries.append(current)
            current = {"path": line[len("worktree "):].strip(), "commit": None, "branch": None}
        elif line.startswith("HEAD "):
            current["commit"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            if ref.startswith("refs/heads/"):
                ref = ref[len("refs/heads/"):]
            current["branch"] = ref
    if current.get("path"):
        entries.append(current)
    return entries


async def _run_git(
    args: list[str],
    *,
    cwd: str,
    timeout_seconds: float = 15.0,
) -> tuple[int, str, str]:
    """Run a git subprocess with ManagedProcess tracking and a single deadline covering startup + IO."""
    git_bin = shutil.which("git")
    if not git_bin:
        raise RuntimeError("`git` executable not found in PATH.")
    deadline = time.monotonic() + max(0.01, timeout_seconds)
    managed: ManagedProcess | None = None
    try:
        spawn_rem = deadline - time.monotonic()
        if spawn_rem <= 0:
            raise asyncio.TimeoutError("Git probe deadline expired before spawn.")
        proc, managed = await spawn_managed_process(
            git_bin,
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=spawn_rem,
        )
        io_rem = deadline - time.monotonic()
        if io_rem <= 0:
            raise asyncio.TimeoutError("Git probe deadline expired during startup.")
        stdout_b, stderr_b, _ = await communicate_managed(
            proc,
            managed,
            timeout=io_rem,
        )
        return (
            int(proc.returncode or 0),
            stdout_b.decode("utf-8", errors="replace").strip(),
            stderr_b.decode("utf-8", errors="replace").strip(),
        )
    except BaseException:
        if managed is not None:
            await managed.terminate()
        raise


async def _inspect_git_checkout(
    checkout_dir: str,
    *,
    timeout_seconds: float = 5.0,
    deadline: float | None = None,
    include_common_dir: bool = True,
) -> tuple[str | None, str | None, str | None]:
    """Return `(branch, head_commit, git_common_dir)` while strictly obeying remaining `deadline`."""
    inspect_deadline = time.monotonic() + max(0.0, timeout_seconds)
    if deadline is not None:
        inspect_deadline = min(inspect_deadline, deadline)

    def _rem() -> float:
        return inspect_deadline - time.monotonic()

    branch: str | None = None
    commit: str | None = None
    common_dir: str | None = None

    rem = _rem()
    if rem <= 0.02:
        return branch, commit, common_dir
    try:
        rc_c, out_c, _ = await _run_git(
            ["rev-parse", "HEAD"],
            cwd=checkout_dir,
            timeout_seconds=rem,
        )
        if rc_c == 0 and out_c:
            commit = out_c.splitlines()[0].strip()
    except Exception:
        pass

    rem = _rem()
    if rem <= 0.02:
        return branch, commit, common_dir
    try:
        rc_b, out_b, _ = await _run_git(
            ["branch", "--show-current"],
            cwd=checkout_dir,
            timeout_seconds=rem,
        )
        if rc_b == 0 and out_b:
            branch = out_b.splitlines()[0].strip() or None
    except Exception:
        pass

    if not include_common_dir:
        return branch, commit, common_dir

    rem = _rem()
    if rem <= 0.02:
        return branch, commit, common_dir
    try:
        rc_g, out_g, _ = await _run_git(
            ["rev-parse", "--git-common-dir"],
            cwd=checkout_dir,
            timeout_seconds=rem,
        )
        if rc_g == 0 and out_g:
            raw_g = out_g.splitlines()[0].strip()
            gpath = Path(raw_g)
            if not gpath.is_absolute():
                gpath = (Path(checkout_dir) / gpath).resolve()
            else:
                gpath = gpath.resolve()
            common_dir = str(gpath)
    except Exception:
        pass
    return branch, commit, common_dir


async def prepare_job_worktree(
    *,
    job_id: str,
    cwd: str,
    state_dir: Path,
    existing_worktree_path: str | None = None,
    existing_worktree_branch: str | None = None,
    existing_worktree_commit: str | None = None,
    deadline: float | None = None,
    on_worktree_planned: OnWorktreePlannedCallback | None = None,
) -> dict[str, str | None]:
    """Deterministically assign or reuse a per-job Git worktree BEFORE launching the subagent.

    Used by every backend that supports `worktree=True` (Codex and Claude).

    Assigning a dedicated worktree per `job_id` eliminates parallel worktree detection races,
    applies `deadline` to lock acquisition and every Git probe, and notifies `on_worktree_planned`
    before invoking Git so any cancellation mid-checkout still reports the checkout path left behind.
    """
    def _budget(cap: float = 15.0) -> float:
        if deadline is None:
            return cap
        rem = deadline - time.monotonic()
        if rem <= 0.05:
            raise asyncio.TimeoutError("Job deadline expired before Git worktree could be created.")
        return min(cap, rem)

    # If resuming inside an existing managed worktree, reuse it deterministically without exceeding deadline.
    if existing_worktree_path and Path(existing_worktree_path).is_dir():
        wt_resolved = Path(existing_worktree_path).resolve()
        cwd_resolved = Path(cwd).resolve()
        try:
            cwd_resolved.relative_to(wt_resolved)
            effective_cwd = str(cwd_resolved)
        except ValueError:
            effective_cwd = str(wt_resolved)

        branch = existing_worktree_branch
        commit = existing_worktree_commit
        common_dir: str | None = None
        if deadline is None or (deadline - time.monotonic()) > 0.05:
            inspected_branch, inspected_commit, inspected_common = await _inspect_git_checkout(
                str(wt_resolved),
                timeout_seconds=5.0,
                deadline=deadline,
                include_common_dir=True,
            )
            if inspected_branch:
                branch = inspected_branch
            if inspected_commit:
                commit = inspected_commit
            if inspected_common:
                common_dir = inspected_common
        return {
            "effective_cwd": effective_cwd,
            "worktree_path": str(wt_resolved),
            "worktree_branch": branch,
            "worktree_commit": commit,
            "git_common_dir": common_dir,
        }

    rc_top, out_top, err_top = await _run_git(
        ["rev-parse", "--show-toplevel"],
        cwd=cwd,
        timeout_seconds=_budget(10.0),
    )
    if rc_top != 0 or not out_top:
        raise RuntimeError(
            f"Cannot create Git worktree for {cwd!r}: not a Git repository ({err_top or 'rev-parse failed'})."
        )
    repo_root = Path(out_top.splitlines()[0].strip()).resolve()

    rc_head, out_head, err_head = await _run_git(
        ["rev-parse", "HEAD"],
        cwd=str(repo_root),
        timeout_seconds=_budget(10.0),
    )
    if rc_head != 0 or not out_head:
        raise RuntimeError(
            f"Cannot create Git worktree for {repo_root}: repository has no HEAD commit ({err_head or 'empty repo'})."
        )
    head_commit = out_head.splitlines()[0].strip()

    rel_prefix = ""
    try:
        rel_path = Path(cwd).resolve().relative_to(repo_root)
        if str(rel_path) not in {"", "."}:
            rel_prefix = str(rel_path)
    except ValueError:
        rel_prefix = ""

    rc_common, out_common, _ = await _run_git(
        ["rev-parse", "--git-common-dir"],
        cwd=str(repo_root),
        timeout_seconds=_budget(10.0),
    )
    git_common_dir: str | None = None
    if rc_common == 0 and out_common:
        raw_c = Path(out_common.splitlines()[0].strip())
        git_common_dir = str(raw_c.resolve() if raw_c.is_absolute() else (repo_root / raw_c).resolve())

    repo_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", repo_root.name).strip("-") or "repo"
    repo_hash = hashlib.sha256(os.path.normcase(str(repo_root)).encode("utf-8")).hexdigest()[:8]
    wt_root = (state_dir / "worktrees" / f"{repo_slug}-{repo_hash}" / job_id).resolve()
    wt_branch = f"agent-bridge/{job_id}"
    wt_root.parent.mkdir(parents=True, exist_ok=True)

    lock = _repo_worktree_lock(os.path.normcase(str(repo_root)))
    lock_wait = _budget(20.0)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=lock_wait)
    except asyncio.TimeoutError as exc:
        raise asyncio.TimeoutError(
            "Job deadline expired while waiting for Git worktree lock."
        ) from exc

    try:
        # Verify remaining budget after acquiring lock, then persist planned path BEFORE invoking Git!
        _budget(20.0)
        planned_effective = (wt_root / rel_prefix).resolve() if rel_prefix else wt_root
        if on_worktree_planned is not None:
            on_worktree_planned(
                {
                    "effective_cwd": str(planned_effective),
                    "worktree_path": str(wt_root),
                    "worktree_branch": wt_branch,
                    "worktree_commit": head_commit,
                    "git_common_dir": git_common_dir,
                }
            )

        last_err = ""
        for attempt in range(4):
            rc_add, _out_add, err_add = await _run_git(
                ["worktree", "add", "-b", wt_branch, str(wt_root), head_commit],
                cwd=str(repo_root),
                timeout_seconds=_budget(20.0),
            )
            if rc_add == 0:
                break
            last_err = err_add or f"git worktree add exited {rc_add}"
            if attempt < 3 and re.search(r"\.lock|index\.lock|another git process", last_err, re.I):
                await asyncio.sleep(min(0.25 * (attempt + 1), _budget(2.0)))
                continue
            raise RuntimeError(f"Failed to create Git worktree at {wt_root}: {last_err}")
    finally:
        lock.release()

    effective_path = (wt_root / rel_prefix).resolve() if rel_prefix else wt_root
    if not effective_path.is_dir():
        effective_path = wt_root

    return {
        "effective_cwd": str(effective_path),
        "worktree_path": str(wt_root),
        "worktree_branch": wt_branch,
        "worktree_commit": head_commit,
        "git_common_dir": git_common_dir,
    }


# The name this had while only Codex could use a worktree.
prepare_codex_worktree = prepare_job_worktree


async def _check_cli_health(cmd: list[str]) -> tuple[bool, str | None, str | None]:
    """Run `<cmd> --version` and return `(healthy, version_line, error_msg)`."""
    deadline = time.monotonic() + 10.0
    managed: ManagedProcess | None = None
    try:
        proc, managed = await spawn_managed_process(
            *cmd,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=max(0.1, deadline - time.monotonic()),
        )
        io_rem = deadline - time.monotonic()
        if io_rem <= 0:
            raise asyncio.TimeoutError("--version deadline expired during startup.")
        stdout_b, stderr_b, _ = await communicate_managed(proc, managed, timeout=io_rem)
        out = stdout_b.decode("utf-8", errors="replace").strip()
        err = stderr_b.decode("utf-8", errors="replace").strip()
        text = out or err
        version = text.splitlines()[0] if text else None
        if proc.returncode == 0 and version:
            return True, version, None
        return False, version, err or out or f"--version exited with code {proc.returncode}"
    except Exception as exc:
        if managed is not None:
            await managed.terminate()
        return False, None, f"{type(exc).__name__}: {exc}"
    except BaseException:
        if managed is not None:
            await managed.terminate()
        raise


class NeonRunner:
    """Executes subagent turns via Antigravity CLI (`agy --output-format stream-json --print=...`)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.catalog = NeonModelCatalog(
            fetcher=self._fetch_live_models,
            cache_path=settings.state_dir / "agy_models_cache.json",
            ttl_seconds=settings.models_cache_ttl_seconds,
        )

    def resolve_command(self) -> list[str]:
        return resolve_agy_command(self._settings.agy_bin)

    async def check_health(self) -> dict[str, Any]:
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
        return {
            "command_found": True,
            "healthy": healthy,
            "available": healthy,
            "command": cmd,
            "version": version,
            "error": err,
        }

    async def _fetch_live_models(self) -> list[dict[str, str]]:
        cmd = [*self.resolve_command(), "models"]
        deadline = time.monotonic() + 20.0
        managed: ManagedProcess | None = None
        try:
            proc, managed = await spawn_managed_process(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                timeout=max(0.1, deadline - time.monotonic()),
            )
            io_rem = deadline - time.monotonic()
            if io_rem <= 0:
                raise asyncio.TimeoutError("agy models deadline expired during startup.")
            stdout, _stderr, _ = await communicate_managed(proc, managed, timeout=io_rem)
        except BaseException:
            if managed is not None:
                await managed.terminate()
            raise

        models: list[dict[str, str]] = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or "\t" not in line:
                continue
            model_id, label = line.split("\t", 1)
            models.append({"id": model_id.strip(), "label": label.strip()})
        return models

    async def list_models(self, *, refresh: bool = False) -> list[dict[str, str]]:
        return await self.catalog.get_models(refresh=refresh)

    # Put before every read_only prompt. A subclass may blank it.
    read_only_guard: str = AGY_READ_ONLY_GUARD

    def build_args(
        self,
        request: RunRequest,
        *,
        resolved_model: str | None,
        prompt_text: str,
        extra_dirs: list[str],
        remaining_seconds: float,
    ) -> list[str]:
        # Keep agy's own --print-timeout strictly within our hard job deadline (no extra cushion!).
        if remaining_seconds >= 3.0:
            print_timeout_sec = max(1, int(remaining_seconds) - 1)
        else:
            print_timeout_sec = max(1, int(remaining_seconds))
        args: list[str] = [
            *self.resolve_command(),
            "--output-format",
            "stream-json",
            # No --mode: headless agy then runs in its own default (auto where available), where shell
            # commands run in its sandbox. `--mode plan` and `--mode accept-edits` make every shell command
            # need an approval nobody can give headless, and the denial ends the turn with an empty reply.
            # A read_only job is kept read-only by AGY_READ_ONLY_GUARD in the prompt, not by a flag.
            "--print-timeout",
            f"{print_timeout_sec}s",
        ]
        if resolved_model:
            args.extend(["--model", resolved_model])
        if request.session_id:
            args.extend(["--resume", request.session_id])
        for directory in extra_dirs:
            args.extend(["--add-dir", directory])
        args.append(f"--print={prompt_text}")
        return args

    async def _run_once(
        self,
        request: RunRequest,
        *,
        resolved_model: str | None,
        model_notice: str | None,
        prompt_text: str,
        extra_dirs: list[str],
        deadline: float,
        prior_watermark: dict[str, Any] | None,
        on_session: OnSessionCallback | None,
        on_activity: OnActivityCallback | None,
        on_model: OnModelCallback | None,
        on_provider_start: OnProviderStartCallback | None = None,
    ) -> RunResult:
        started = time.monotonic()
        remaining = deadline - started
        if on_model:
            on_model(resolved_model, "requested" if resolved_model else "default")
        if remaining <= 0.05:
            status, reason, pstate, guidance = classify_failure(
                backend="neon",
                exit_code=None,
                error_text="Job deadline expired before execution started.",
                stderr_text=None,
                timed_out_locally=True,
                process_stopped=True,
            )
            return RunResult(
                status=status,
                session_id=request.session_id,
                model=resolved_model,
                effort=request.effort,
                model_notice=model_notice,
                effective_cwd=request.cwd,
                duration_seconds=0.0,
                usage_status="missing",
                error="Job deadline expired before execution started.",
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
            )

        cmd = self.build_args(
            request,
            resolved_model=resolved_model,
            prompt_text=prompt_text,
            extra_dirs=extra_dirs,
            remaining_seconds=remaining,
        )
        if on_activity:
            on_activity(f"running agy ({resolved_model or 'agy-default'})")

        # Commit launch intent in SQLite BEFORE spawning the provider subprocess!
        await _maybe_await(on_provider_start)

        env = job_env(self._settings, request, cli="agy")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessStartupTimeout("Job deadline expired before execution started.", process_stopped=True)
            proc, managed = await spawn_managed_process(
                *cmd,
                cwd=request.cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                timeout=remaining,
                env=env,
            )
        except asyncio.TimeoutError as exc:
            process_stopped = isinstance(exc, ProcessStartupTimeout) and exc.process_stopped
            status, reason, pstate, guidance = classify_failure(
                backend="neon",
                exit_code=None,
                error_text=f"Neon subagent timed out during startup after {request.timeout_seconds}s.",
                stderr_text=None,
                timed_out_locally=True,
                process_stopped=process_stopped,
            )
            return RunResult(
                status=status,
                session_id=request.session_id,
                model=resolved_model,
                effort=request.effort,
                model_notice=model_notice,
                effective_cwd=request.cwd,
                duration_seconds=round(max(0.0, time.monotonic() - started), 2),
                usage_status="missing",
                error=f"Neon subagent timed out during startup after {request.timeout_seconds}s.",
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
                provider_invoked=not process_stopped,
            )
        except ProcessContainmentError as exc:
            return RunResult(
                status="failed",
                session_id=request.session_id,
                model=resolved_model,
                effort=request.effort,
                model_notice=model_notice,
                effective_cwd=request.cwd,
                duration_seconds=round(max(0.0, time.monotonic() - started), 2),
                usage_status="missing",
                error=str(exc),
                failure_reason="launch_error",
                provider_state="possibly_running" if exc.process_still_alive else "stopped",
                retry_guidance=(
                    "Windows process containment failed before launching agy; worker was terminated "
                    "while still suspended. Inspect host Job Object permissions/environment and retry."
                ),
                provider_invoked=False,
            )

        stdout_lines: list[str] = []
        stderr_chunks: list[bytes] = []
        session_id: str | None = request.session_id
        actual_model: str | None = resolved_model
        init_permission_mode: str | None = None
        step_usages_by_index: dict[int, dict[str, Any]] = {}
        partial_text_parts: list[str] = []
        result_envelope: dict[str, Any] | None = None
        plain_envelope: dict[str, Any] | None = None

        def _handle_line(line: str) -> None:
            nonlocal session_id, actual_model, init_permission_mode, result_envelope, plain_envelope
            stdout_lines.append(line)
            stripped = line.strip()
            if not stripped.startswith("{"):
                return
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                return
            if not isinstance(obj, dict):
                return

            ev_type = obj.get("event")
            if ev_type == "init":
                cid = obj.get("conversation_id")
                if isinstance(cid, str) and cid:
                    session_id = cid
                    if on_session:
                        on_session(cid)
                init_obj = obj.get("init")
                if isinstance(init_obj, dict):
                    im = init_obj.get("model")
                    if isinstance(im, str) and im:
                        actual_model = im
                        if on_model:
                            on_model(im, "observed")
                    pm = init_obj.get("permission_mode")
                    if isinstance(pm, str) and pm:
                        init_permission_mode = pm
                if on_activity:
                    on_activity("session initialized")
            elif ev_type == "step_update":
                su = obj.get("step_update")
                if isinstance(su, dict):
                    cid = su.get("conversation_id")
                    if isinstance(cid, str) and cid and not session_id:
                        session_id = cid
                        if on_session:
                            on_session(cid)
                    delta = su.get("text_delta")
                    if isinstance(delta, str) and delta:
                        partial_text_parts.append(delta)
                    st = su.get("step_type")
                    state = su.get("state")
                    if on_activity and isinstance(st, str):
                        on_activity(f"{st} ({state or 'active'})")
                    if state == "DONE" and isinstance(su.get("usage"), dict):
                        idx = su.get("step_index")
                        key = int(idx) if isinstance(idx, int) else len(step_usages_by_index)
                        step_usages_by_index[key] = su["usage"]
            else:
                envelope = _agy_result_envelope(obj)
                if envelope is None:
                    return
                if ev_type == "result":
                    result_envelope = envelope
                else:
                    plain_envelope = envelope
                cid = envelope.get("conversation_id")
                if isinstance(cid, str) and cid:
                    session_id = cid
                    if on_session:
                        on_session(cid)

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
                        _handle_line(raw_line.decode("utf-8", errors="replace"))
            except asyncio.CancelledError:
                if buf:
                    tail = bytes(buf)
                    buf.clear()
                    _handle_line(tail.decode("utf-8", errors="replace"))
                raise
            if buf:
                _handle_line(bytes(buf).decode("utf-8", errors="replace"))

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
                    timeout=wait_budget,
                )
        except asyncio.TimeoutError:
            timed_out_locally = True
            process_stopped = managed.verified_stopped and (not managed.is_alive())
        except asyncio.CancelledError:
            cancelled = True

        # Measure work duration separately from process-tree cleanup time!
        work_end = managed.work_stopped_at if managed.work_stopped_at is not None else time.monotonic()
        elapsed = round(max(0.0, work_end - started), 2)
        cleanup_secs = round(managed.last_cleanup_seconds, 2) if managed.last_cleanup_seconds > 0 else (
            0.0 if timed_out_locally else None
        )
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        stderr_tail = _tail(stderr_text)
        partial_text = "".join(partial_text_parts).strip()

        envelope = result_envelope or plain_envelope
        if envelope is None:
            envelope = parse_agy_json_output("".join(stdout_lines))

        raw_status = str(envelope.get("status") or "").strip().upper() if isinstance(envelope, dict) else ""
        # A clean exit without a recognized result envelope carrying its status is an incomplete stream.
        final_event_missing = (
            (not timed_out_locally) and (not cancelled) and proc.returncode == 0 and not raw_status
        )
        turn_completed_cleanly = (
            (not timed_out_locally)
            and (
                (not cancelled and proc.returncode == 0 and raw_status in {"SUCCESS", "OK"})
                or (cancelled and raw_status in {"SUCCESS", "OK"})
            )
        )

        # Determine per-turn usage accurately without double-counting or labeling interrupted turns exact.
        step_sum = sum_agy_step_usages(list(step_usages_by_index.values()))
        raw_env_candidate = (
            envelope.get("usage")
            if isinstance(envelope, dict) and isinstance(envelope.get("usage"), dict)
            else None
        )
        env_raw_usage = (
            raw_env_candidate
            if _has_valid_agy_token_counters(raw_env_candidate)
            else None
        )
        raw_turns = envelope.get("num_turns") if isinstance(envelope, dict) else None
        num_turns = (
            int(raw_turns)
            if isinstance(raw_turns, int) and not isinstance(raw_turns, bool) and raw_turns >= 1
            else None
        )

        usage: dict[str, Any] | None = None
        provider_metrics: dict[str, Any] | None = None
        usage_status: UsageStatus = "missing"

        if step_sum is not None:
            usage, provider_metrics = normalize_usage("neon", step_sum)
            usage_status = "exact" if turn_completed_cleanly else "partial"
        elif env_raw_usage is not None:
            is_first_turn = (not request.session_id) and (num_turns in {None, 1})
            if is_first_turn:
                usage, provider_metrics = normalize_usage("neon", env_raw_usage)
                usage_status = "exact" if turn_completed_cleanly else "partial"
            elif (
                turn_completed_cleanly
                and prior_watermark is not None
                and isinstance(prior_watermark.get("cumulative"), dict)
                and num_turns is not None
                and isinstance(prior_watermark.get("num_turns"), int)
                and int(prior_watermark["num_turns"]) >= 1
                and int(prior_watermark["num_turns"]) + 1 == num_turns
            ):
                delta = compute_agy_usage_delta(env_raw_usage, prior_watermark["cumulative"])
                if delta is not None:
                    usage, provider_metrics = normalize_usage("neon", delta)
                    usage_status = "delta"
                else:
                    _, provider_metrics = normalize_usage("neon", env_raw_usage)
                    if provider_metrics is not None:
                        provider_metrics["cumulative_only"] = True
                    usage_status = "partial"
            else:
                # Resumed turn without verified adjacent watermark or interrupted turn: never double-count!
                _, provider_metrics = normalize_usage("neon", env_raw_usage)
                if provider_metrics is not None:
                    provider_metrics["cumulative_only"] = True
                usage_status = "partial"

        if cancelled:
            # The timeout path honours agy's word that its turn goes on at the server; so does a cancel.
            env_error = str(envelope.get("error") or "") if isinstance(envelope, dict) else ""
            remote_running = agy_reports_turn_still_running(proc.returncode, f"{env_error}\n{stderr_text}")
            raise RunCancelled(
                RunResult(status="cancelled", session_id=session_id, usage=usage,
                          provider_metrics=provider_metrics, usage_status=usage_status,
                          provider_state="possibly_running" if remote_running else None)
            )

        # Only publish a new cumulative checkpoint when this turn completed cleanly with valid,
        # non-zero, monotonic counters and an explicit turn count.
        published_cumulative = (
            env_raw_usage
            if (
                turn_completed_cleanly
                and num_turns is not None
                and _is_valid_agy_cumulative_checkpoint(env_raw_usage, prior_watermark)
            )
            else None
        )
        published_turns = num_turns if published_cumulative is not None else None

        agy_perm_fallback = bool(
            request.mode == "edit"
            and (
                any(p.search(stderr_text) for p in _INTERACTIVE_FALLBACK_PATTERNS)
                or (
                    init_permission_mode == "request-review"
                    and bool(
                        re.search(
                            r"\b(permission|approval|interactive|request-review|declined|rejected)\b",
                            f"{stderr_text}\n{partial_text}",
                            re.I,
                        )
                    )
                )
            )
        )

        final_notice_parts: list[str] = []
        if model_notice:
            final_notice_parts.append(model_notice)
        if request.mode == "edit" and init_permission_mode == "request-review":
            final_notice_parts.append(
                "Antigravity headless session initialized with permission_mode='request-review'; "
                "non-edit tool prompts may require interactive approval (for unattended full edits prefer Codex)."
            )
        combined_notice = " ".join(final_notice_parts) if final_notice_parts else None

        observed_progress = bool(step_usages_by_index or partial_text_parts)
        if timed_out_locally:
            status, reason, pstate, guidance = classify_failure(
                backend="neon",
                exit_code=proc.returncode,
                error_text=f"Neon subagent timed out after {request.timeout_seconds}s.",
                stderr_text=stderr_text,
                timed_out_locally=True,
                process_stopped=process_stopped,
            )
            return RunResult(
                status=status,
                response=partial_text,
                session_id=session_id,
                model=actual_model,
                effort=request.effort,
                model_notice=combined_notice,
                effective_cwd=request.cwd,
                duration_seconds=elapsed,
                cleanup_seconds=cleanup_secs,
                usage=usage,
                provider_metrics=provider_metrics,
                usage_status=usage_status,
                cumulative_usage=None,
                num_turns=None,
                observed_num_turns=num_turns,
                observed_turn_progress=observed_progress,
                exit_code=proc.returncode,
                error=f"Neon subagent timed out after {request.timeout_seconds}s.",
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
                stderr_tail=stderr_tail,
                provider_invoked=True,
            )

        if final_event_missing:
            # Set directly, not through classify_failure, which would read the reply's own prose.
            env_response = str(envelope.get("response") or "").strip() if isinstance(envelope, dict) else ""
            return RunResult(
                status="failed",
                response=env_response or partial_text or _plain_stdout_reply(stdout_lines),
                session_id=session_id,
                model=actual_model,
                effort=request.effort,
                model_notice=combined_notice,
                effective_cwd=request.cwd,
                duration_seconds=elapsed,
                cleanup_seconds=cleanup_secs,
                usage=usage,
                provider_metrics=provider_metrics,
                usage_status=usage_status,
                cumulative_usage=None,
                num_turns=None,
                observed_num_turns=num_turns,
                observed_turn_progress=observed_progress,
                exit_code=0,
                error=CLI_OUTPUT_CUT_SHORT_ERROR.format(event="a result event with a status"),
                failure_reason="cli_error",
                provider_state="stopped" if process_stopped else "possibly_running",
                retry_guidance=CLI_OUTPUT_CUT_SHORT_GUIDANCE,
                stderr_tail=stderr_tail,
                provider_invoked=True,
            )

        if envelope is not None:
            cid = envelope.get("conversation_id")
            if isinstance(cid, str) and cid:
                session_id = cid
            response_text = str(envelope.get("response", "")).strip() or partial_text

            # Headless agy cannot ask for an approval: it denies the action, ends the turn with no reply,
            # and still exits 0 with status SUCCESS. That is not a completed job.
            denied_actions = envelope.get("denied_actions")
            if turn_completed_cleanly and not response_text and isinstance(denied_actions, list) and denied_actions:
                denied_names = ", ".join(
                    sorted(
                        {
                            str(d.get("action") or d.get("display_name") or "unknown")
                            for d in denied_actions
                            if isinstance(d, dict)
                        }
                    )
                ) or "unknown"
                return RunResult(
                    status="failed",
                    response="",
                    session_id=session_id,
                    model=actual_model,
                    effort=request.effort,
                    model_notice=combined_notice,
                    effective_cwd=request.cwd,
                    duration_seconds=elapsed,
                    cleanup_seconds=cleanup_secs,
                    usage=usage,
                    provider_metrics=provider_metrics,
                    usage_status=usage_status,
                    cumulative_usage=published_cumulative,
                    num_turns=published_turns,
                    observed_num_turns=num_turns,
                    observed_turn_progress=observed_progress,
                    exit_code=0,
                    error=(
                        f"agy denied an action it could not ask about in headless mode ({denied_names}) "
                        "and ended the turn with no reply."
                    ),
                    failure_reason="permission_error",
                    provider_state="stopped" if process_stopped else "possibly_running",
                    retry_guidance=(
                        "Resume the session and tell the subagent to use its file tools instead of the denied "
                        "action, or add an allow rule for it in agy's settings.json."
                    ),
                    stderr_tail=stderr_tail,
                    provider_invoked=True,
                )

            if turn_completed_cleanly:
                return RunResult(
                    status="completed",
                    response=response_text,
                    session_id=session_id,
                    model=actual_model,
                    effort=request.effort,
                    model_notice=combined_notice,
                    effective_cwd=request.cwd,
                    duration_seconds=elapsed,
                    cleanup_seconds=cleanup_secs,
                    usage=usage,
                    provider_metrics=provider_metrics,
                    usage_status=usage_status,
                    cumulative_usage=published_cumulative,
                    num_turns=published_turns,
                    observed_num_turns=num_turns,
                    observed_turn_progress=observed_progress,
                    exit_code=0,
                    provider_state="stopped" if process_stopped else "possibly_running",
                    stderr_tail=stderr_tail,
                    provider_invoked=True,
                )

            err_msg = str(
                envelope.get("error")
                or response_text
                or stderr_tail
                or f"agy exited with status {raw_status or proc.returncode}"
            )
            status, reason, pstate, guidance = classify_failure(
                backend="neon",
                exit_code=proc.returncode,
                error_text=err_msg,
                stderr_text=stderr_text,
                agy_permission_fallback=agy_perm_fallback,
                process_stopped=process_stopped,
            )
            return RunResult(
                status=status,
                response=response_text,
                session_id=session_id,
                model=actual_model,
                effort=request.effort,
                model_notice=combined_notice,
                effective_cwd=request.cwd,
                duration_seconds=elapsed,
                cleanup_seconds=cleanup_secs,
                usage=usage,
                provider_metrics=provider_metrics,
                usage_status=usage_status,
                cumulative_usage=None,
                num_turns=None,
                observed_num_turns=num_turns,
                observed_turn_progress=observed_progress,
                exit_code=proc.returncode,
                error=err_msg,
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
                stderr_tail=stderr_tail,
                provider_invoked=True,
            )

        plain_text = partial_text or "".join(stdout_lines).strip()
        if turn_completed_cleanly:
            return RunResult(
                status="completed",
                response=plain_text,
                session_id=session_id,
                model=actual_model,
                effort=request.effort,
                model_notice=combined_notice,
                effective_cwd=request.cwd,
                duration_seconds=elapsed,
                cleanup_seconds=cleanup_secs,
                usage=usage,
                provider_metrics=provider_metrics,
                usage_status=usage_status,
                cumulative_usage=None,
                num_turns=None,
                observed_num_turns=num_turns,
                observed_turn_progress=observed_progress,
                exit_code=0,
                provider_state="stopped" if process_stopped else "possibly_running",
                stderr_tail=stderr_tail,
                provider_invoked=True,
            )

        err_msg = stderr_tail or plain_text or f"agy exited with code {proc.returncode}"
        status, reason, pstate, guidance = classify_failure(
            backend="neon",
            exit_code=proc.returncode,
            error_text=err_msg,
            stderr_text=stderr_text,
            agy_permission_fallback=agy_perm_fallback,
            process_stopped=process_stopped,
        )
        return RunResult(
            status=status,
            response=partial_text,
            session_id=session_id,
            model=actual_model,
            effort=request.effort,
            model_notice=combined_notice,
            effective_cwd=request.cwd,
            duration_seconds=elapsed,
            cleanup_seconds=cleanup_secs,
            usage=usage,
            provider_metrics=provider_metrics,
            usage_status=usage_status,
            cumulative_usage=None,
            num_turns=None,
            observed_num_turns=num_turns,
            observed_turn_progress=observed_progress,
            exit_code=proc.returncode,
            error=err_msg,
            failure_reason=reason,
            provider_state=pstate,
            retry_guidance=guidance,
            stderr_tail=stderr_tail,
            provider_invoked=True,
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
        overall_deadline = deadline if deadline is not None else (time.monotonic() + request.timeout_seconds)
        temp_dir: str | None = None
        extra_dirs = list(request.extra_dirs)
        prompt_text = request.prompt

        try:
            if len(prompt_text) > AGY_MAX_INLINE_PROMPT_CHARS:
                temp_dir = tempfile.mkdtemp(prefix="agent-bridge-prompt-")
                prompt_file = Path(temp_dir) / "prompt.md"
                prompt_file.write_text(prompt_text, encoding="utf-8")
                extra_dirs.append(temp_dir)
                prompt_text = (
                    f"Read the file {prompt_file} completely and follow all "
                    "instructions inside it."
                )

            if request.mode == "read_only" and self.read_only_guard:
                prompt_text = self.read_only_guard + prompt_text

            rem_before_lookup = overall_deadline - time.monotonic()
            if rem_before_lookup <= 0.05:
                status, reason, pstate, guidance = classify_failure(
                    backend="neon",
                    exit_code=None,
                    error_text="Job deadline expired before model discovery.",
                    stderr_text=None,
                    timed_out_locally=True,
                    process_stopped=True,
                )
                return RunResult(
                    status=status,
                    session_id=request.session_id,
                    model=request.model,
                    effort=request.effort,
                    effective_cwd=request.cwd,
                    duration_seconds=0.0,
                    usage_status="missing",
                    error="Job deadline expired before model discovery.",
                    failure_reason=reason,
                    provider_state=pstate,
                    retry_guidance=guidance,
                )

            lookup_budget = min(20.0, rem_before_lookup)
            try:
                resolution = await self.catalog.resolve(
                    request.model,
                    default_intent=self._settings.default_neon_model,
                    effort_override=request.effort,
                    timeout_seconds=lookup_budget,
                    allow_cross_pool_fallback=request.allow_cross_pool_fallback,
                )
            except ModelPoolUnavailableError as exc:
                return RunResult(
                    status="failed",
                    session_id=request.session_id,
                    model=request.model,
                    effort=request.effort,
                    effective_cwd=request.cwd,
                    duration_seconds=0.0,
                    usage_status="missing",
                    error=str(exc),
                    failure_reason="model_unavailable",
                    provider_state="stopped",
                    retry_guidance=(
                        "Refresh models via backends(refresh_models=True), select a model in an "
                        "available quota pool, or pass allow_cross_pool_fallback=True."
                    ),
                    provider_invoked=False,
                )

            result = await self._run_once(
                request,
                resolved_model=resolution.resolved,
                model_notice=resolution.notice,
                prompt_text=prompt_text,
                extra_dirs=extra_dirs,
                deadline=overall_deadline,
                prior_watermark=prior_watermark,
                on_session=on_session,
                on_activity=on_activity,
                on_model=on_model,
                on_provider_start=on_provider_start,
            )

            # Seamless self-healing retry ONLY if Attempt 1 is verified stopped and didn't advance a session.
            if (
                result.status == "failed"
                and result.failure_reason == "model_unavailable"
                and resolution.resolved is not None
                and _can_auto_retry_attempt(
                    request,
                    result,
                    kind="model_unavailable",
                    prior_watermark=prior_watermark,
                )
                and (overall_deadline - time.monotonic()) > 8.0
            ):
                logger.info(
                    "agy rejected model %r; refreshing catalog and retrying within deadline.",
                    resolution.resolved,
                )
                refresh_budget = min(15.0, max(0.5, overall_deadline - time.monotonic()))
                try:
                    refreshed = await self.catalog.resolve(
                        request.model,
                        default_intent=self._settings.default_neon_model,
                        effort_override=request.effort,
                        force_refresh=True,
                        timeout_seconds=refresh_budget,
                        allow_cross_pool_fallback=request.allow_cross_pool_fallback,
                    )
                except ModelPoolUnavailableError as exc:
                    if on_model and request.model and request.model != result.model:
                        on_model(request.model, "requested")
                    return RunResult(
                        status="failed",
                        session_id=result.session_id or request.session_id,
                        model=request.model,
                        effort=request.effort,
                        effective_cwd=request.cwd,
                        duration_seconds=result.duration_seconds,
                        cleanup_seconds=result.cleanup_seconds,
                        usage=result.usage,
                        provider_metrics=result.provider_metrics,
                        usage_status=result.usage_status,
                        error=str(exc),
                        failure_reason="model_unavailable",
                        provider_state="stopped",
                        retry_guidance=(
                            "Refresh models via backends(refresh_models=True), select a model in an "
                            "available quota pool, or pass allow_cross_pool_fallback=True."
                        ),
                        provider_invoked=result.provider_invoked,
                    )
                requested_pool = classify_quota_pool("neon", resolution.resolved or request.model)
                different_model = (
                    refreshed.resolved
                    if (refreshed.resolved is not None and refreshed.resolved != resolution.resolved)
                    else None
                )
                if different_model is not None and not request.allow_cross_pool_fallback:
                    if classify_quota_pool("neon", different_model) != requested_pool:
                        different_model = None

                if different_model is None and not request.allow_cross_pool_fallback:
                    if on_model and resolution.resolved and resolution.resolved != result.model:
                        on_model(resolution.resolved, "requested")
                    return RunResult(
                        status="failed",
                        session_id=result.session_id or request.session_id,
                        model=resolution.resolved,
                        effort=request.effort,
                        effective_cwd=request.cwd,
                        duration_seconds=result.duration_seconds,
                        cleanup_seconds=result.cleanup_seconds,
                        usage=result.usage,
                        provider_metrics=result.provider_metrics,
                        usage_status=result.usage_status,
                        exit_code=result.exit_code,
                        error=(
                            f"Model {resolution.resolved!r} was rejected by agy and refreshing the catalog "
                            f"found no different eligible model in quota pool {requested_pool!r} "
                            "(refusing to drop --model and risk spending another quota pool)."
                        ),
                        failure_reason="model_unavailable",
                        provider_state=result.provider_state,
                        retry_guidance=(
                            "Refresh models via backends(refresh_models=True), select another model in "
                            f"quota pool {requested_pool!r}, or pass allow_cross_pool_fallback=True."
                        ),
                        stderr_tail=result.stderr_tail,
                        provider_invoked=result.provider_invoked,
                    )

                retry_model = different_model
                notice = (
                    f"Model {resolution.resolved!r} was unavailable; seamlessly refreshed and retried with "
                    f"{retry_model or 'agy default'}."
                )
                await _maybe_await(on_attempt, _make_attempt_summary(1, result, backend="neon"))
                second = await self._run_once(
                    request,
                    resolved_model=retry_model,
                    model_notice=notice,
                    prompt_text=prompt_text,
                    extra_dirs=extra_dirs,
                    deadline=overall_deadline,
                    prior_watermark=prior_watermark,
                    on_session=on_session,
                    on_activity=on_activity,
                    on_model=on_model,
                    on_provider_start=on_provider_start,
                )
                return _merge_retry_results(result, second, backend="neon")

            # One bounded retry for transient service errors on NEW read-only tasks only (never on uncertain/resumed turns).
            if (
                request.mode == "read_only"
                and result.status == "failed"
                and result.failure_reason == "transient_service"
                and _can_auto_retry_attempt(
                    request,
                    result,
                    kind="transient",
                    prior_watermark=prior_watermark,
                )
                and (overall_deadline - time.monotonic()) > TRANSIENT_RETRY_MIN_REMAINING_SECONDS
            ):
                # Record Attempt 1 BEFORE retry backoff sleep so cancelling during backoff keeps Attempt 1 usage!
                await _maybe_await(on_attempt, _make_attempt_summary(1, result, backend="neon"))
                if on_activity:
                    on_activity("transient service error; retrying once")
                await asyncio.sleep(TRANSIENT_RETRY_BACKOFF_SECONDS)
                second = await self._run_once(
                    request,
                    resolved_model=resolution.resolved,
                    model_notice=resolution.notice,
                    prompt_text=prompt_text,
                    extra_dirs=extra_dirs,
                    deadline=overall_deadline,
                    prior_watermark=prior_watermark,
                    on_session=on_session,
                    on_activity=on_activity,
                    on_model=on_model,
                    on_provider_start=on_provider_start,
                )
                return _merge_retry_results(result, second, backend="neon")

            return result
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)


class CodexRunner:
    """Executes subagent turns via Codex CLI (`codex exec` / `resume` / `review`)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve_command(self) -> list[str]:
        return resolve_codex_command(self._settings.codex_bin)

    async def check_health(self) -> dict[str, Any]:
        local_defaults = read_codex_local_defaults()
        try:
            cmd = self.resolve_command()
        except Exception as exc:
            return {
                "command_found": False,
                "healthy": False,
                "available": False,
                "command": None,
                "version": None,
                "local_config_defaults": local_defaults,
                "error": str(exc),
            }
        healthy, version, err = await _check_cli_health(cmd)
        return {
            "command_found": True,
            "healthy": healthy,
            "available": healthy,
            "command": cmd,
            "version": version,
            "local_config_defaults": local_defaults,
            "error": err,
        }

    def _effective_model(
        self,
        requested: str | None,
        local_defaults: dict[str, str | None] | None = None,
    ) -> str | None:
        raw = (requested or "").strip()
        # Explicit "auto" / "latest" / "default" lets Codex CLI choose its own latest default.
        if raw.lower() in {"default", "latest", "auto"}:
            return None
        if raw:
            return raw

        configured = self._settings.default_codex_model
        if not configured:
            return None
        # If local ~/.codex/config.toml has already moved to a strictly newer version in the same family
        # (e.g. gpt-6.3-sol > gpt-6.2-sol), ride the newer version seamlessly!
        defaults = local_defaults if local_defaults is not None else read_codex_local_defaults()
        if is_newer_codex_model_in_same_family(defaults.get("model"), configured):
            return None
        return configured

    def _effective_effort(self, requested: str | None) -> str | None:
        if requested and requested.strip():
            return requested.strip().lower()
        return self._settings.default_codex_effort

    def build_args(
        self,
        request: RunRequest,
        *,
        effective_model: str | None,
        effective_effort: str | None,
    ) -> tuple[list[str], bool]:
        """Build Codex argv and return `(argv, use_stdin_prompt)`."""
        base = self.resolve_command()
        sandbox = "read-only" if request.mode == "read_only" else "workspace-write"

        if request.review_scope is not None and not request.prompt.strip():
            args = [
                *base,
                "exec",
                "review",
                "--json",
                "--skip-git-repo-check",
                "-c",
                'sandbox_mode="read-only"',
            ]
            if effective_effort:
                args.extend(["-c", f'model_reasoning_effort="{effective_effort}"'])
            if request.review_scope == "uncommitted":
                args.append("--uncommitted")
            elif request.review_scope == "base" and request.review_base:
                args.extend(["--base", request.review_base])
            elif request.review_scope == "commit" and request.review_commit:
                args.extend(["--commit", request.review_commit])
            if effective_model:
                args.extend(["-m", effective_model])
            return args, False

        if request.session_id:
            args = [
                *base,
                "exec",
                "resume",
                "--json",
                "--skip-git-repo-check",
                "-c",
                f'sandbox_mode="{sandbox}"',
            ]
            if effective_effort:
                args.extend(["-c", f'model_reasoning_effort="{effective_effort}"'])
            if effective_model:
                args.extend(["-m", effective_model])
            args.extend([request.session_id, "-"])
            return args, True

        args = [
            *base,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-s",
            sandbox,
            "-C",
            request.cwd,
        ]
        if effective_effort:
            args.extend(["-c", f'model_reasoning_effort="{effective_effort}"'])
        if effective_model:
            args.extend(["-m", effective_model])

        extra_dirs = list(request.extra_dirs)
        if request.worktree_path and request.git_common_dir:
            norm_existing = {os.path.normcase(d) for d in extra_dirs}
            if os.path.normcase(request.git_common_dir) not in norm_existing:
                extra_dirs.append(request.git_common_dir)
        for directory in extra_dirs:
            args.extend(["--add-dir", directory])
        args.append("-")
        return args, True

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
        started = time.monotonic()
        local_defaults = read_codex_local_defaults()
        reported_model = effective_model or local_defaults.get("model") or "codex-default"
        reported_effort = effective_effort or local_defaults.get("model_reasoning_effort")
        if on_model:
            on_model(reported_model, "requested" if effective_model else "default")

        remaining = deadline - started
        if remaining <= 0.05:
            status, reason, pstate, guidance = classify_failure(
                backend="codex",
                exit_code=None,
                error_text="Job deadline expired before execution started.",
                stderr_text=None,
                timed_out_locally=True,
                process_stopped=True,
            )
            return RunResult(
                status=status,
                session_id=request.session_id,
                model=reported_model,
                effort=reported_effort,
                model_notice=model_notice,
                effective_cwd=request.cwd,
                worktree_path=request.worktree_path,
                worktree_branch=request.worktree_branch,
                worktree_commit=request.worktree_commit,
                duration_seconds=0.0,
                usage_status="missing",
                error="Job deadline expired before execution started.",
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
            )

        # Fallback worktree provisioning when CodexRunner.run() is invoked directly without JobManager.
        if request.worktree and not request.worktree_path:
            wt_job_id = request.job_id or f"codex-{int(time.time() * 1000)}"
            wt_info = await prepare_codex_worktree(
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

        cmd, use_stdin_prompt = self.build_args(
            request,
            effective_model=effective_model,
            effective_effort=effective_effort,
        )
        if on_activity:
            on_activity(f"running codex ({reported_model})")

        # Commit launch intent in SQLite BEFORE spawning the provider subprocess!
        await _maybe_await(on_provider_start)

        env = job_env(self._settings, request, cli="codex")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessStartupTimeout("Job deadline expired before execution started.", process_stopped=True)
            proc, managed = await spawn_managed_process(
                *cmd,
                cwd=request.cwd,
                stdin=asyncio.subprocess.PIPE if use_stdin_prompt else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                timeout=remaining,
                env=env,
            )
        except asyncio.TimeoutError as exc:
            process_stopped = isinstance(exc, ProcessStartupTimeout) and exc.process_stopped
            status, reason, pstate, guidance = classify_failure(
                backend="codex",
                exit_code=None,
                error_text=f"Codex subagent timed out during startup after {request.timeout_seconds}s.",
                stderr_text=None,
                timed_out_locally=True,
                process_stopped=process_stopped,
            )
            return RunResult(
                status=status,
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
                error=f"Codex subagent timed out during startup after {request.timeout_seconds}s.",
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
                provider_invoked=not process_stopped,
            )
        except ProcessContainmentError as exc:
            return RunResult(
                status="failed",
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
                error=str(exc),
                failure_reason="launch_error",
                provider_state="possibly_running" if exc.process_still_alive else "stopped",
                retry_guidance=(
                    "Windows process containment failed before launching Codex; worker was terminated "
                    "while still suspended. Inspect host Job Object permissions/environment and retry."
                ),
                provider_invoked=False,
            )

        prompt_bytes = request.prompt.encode("utf-8") if use_stdin_prompt else None
        stdout_lines: list[str] = []
        stderr_chunks: list[bytes] = []

        async def _write_stdin() -> None:
            if prompt_bytes is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(prompt_bytes)
                await proc.stdin.drain()
            except Exception as exc:
                logger.warning("Failed writing prompt to codex stdin: %s", exc)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        def _handle_stdout_line(line: str) -> None:
            stdout_lines.append(line)
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    event = None
                if isinstance(event, dict):
                    if event.get("type") == "thread.started":
                        tid = event.get("thread_id")
                        if isinstance(tid, str) and tid and on_session:
                            on_session(tid)
                    if on_activity:
                        activity = _describe_codex_event(event)
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
                    write_stdin=_write_stdin if prompt_bytes is not None else None,
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
        stderr_tail = _tail(stderr_text)
        parsed = parse_codex_jsonl_events(stdout_lines)
        session_id = parsed["session_id"] or request.session_id
        response = parsed["response"]
        errors = parsed["errors"]
        if proc.returncode == 0 and not errors and not response:
            response = _plain_stdout_reply(stdout_lines)
        # `codex exec review` can finish with a reply in plain text and no turn.completed event.
        builtin_review = request.review_scope is not None and not request.prompt.strip() and bool(response)
        final_event_missing = (
            (not timed_out_locally)
            and (not cancelled)
            and proc.returncode == 0
            and (not errors)
            and (not parsed["turn_completed"])
            and (not builtin_review)
        )
        # Codex emits the usage on turn.completed, so a count from that event is final.
        turn_completed_cleanly = (
            (not timed_out_locally)
            and (proc.returncode == 0 or cancelled)
            and (not errors)
            and (not final_event_missing)
        )

        usage, provider_metrics = normalize_usage("codex", parsed["usage"])
        if usage is not None:
            usage_status: UsageStatus = "exact" if turn_completed_cleanly else "partial"
        else:
            usage_status = "missing"

        # Refresh commit/branch metadata on the pre-assigned worktree ONLY if budget remains!
        effective_cwd = request.cwd
        wt_path = request.worktree_path
        wt_branch = request.worktree_branch
        wt_commit = request.worktree_commit
        if not cancelled and not timed_out_locally and wt_path and Path(wt_path).is_dir():
            if deadline - time.monotonic() > 0.05:
                try:
                    new_branch, new_commit, _ = await _inspect_git_checkout(
                        wt_path, timeout_seconds=5.0, deadline=deadline, include_common_dir=False,
                    )
                except asyncio.CancelledError:
                    cancelled = True
                else:
                    wt_branch = new_branch or wt_branch
                    wt_commit = new_commit or wt_commit

        if cancelled:
            raise RunCancelled(
                RunResult(status="cancelled", session_id=session_id, usage=usage,
                          provider_metrics=provider_metrics, usage_status=usage_status)
            )

        if timed_out_locally:
            status, reason, pstate, guidance = classify_failure(
                backend="codex",
                exit_code=proc.returncode,
                error_text=f"Codex subagent timed out after {request.timeout_seconds}s.",
                stderr_text=stderr_text,
                timed_out_locally=True,
                process_stopped=process_stopped,
            )
            return RunResult(
                status=status,
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
                exit_code=proc.returncode,
                error=f"Codex subagent timed out after {request.timeout_seconds}s.",
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
                stderr_tail=stderr_tail,
                provider_invoked=True,
            )

        if final_event_missing:
            # Set directly, not through classify_failure: stderr prose must not turn this into a
            # transient error that is retried by itself.
            return RunResult(
                status="failed",
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
                exit_code=0,
                error=CLI_OUTPUT_CUT_SHORT_ERROR.format(event="turn.completed"),
                failure_reason="cli_error",
                provider_state="stopped" if process_stopped else "possibly_running",
                retry_guidance=CLI_OUTPUT_CUT_SHORT_GUIDANCE,
                stderr_tail=stderr_tail,
                provider_invoked=True,
            )

        if turn_completed_cleanly:
            return RunResult(
                status="completed",
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
                exit_code=0,
                provider_state="stopped" if process_stopped else "possibly_running",
                stderr_tail=stderr_tail,
                provider_invoked=True,
            )

        err_msg = "; ".join(errors) if errors else (
            stderr_tail or f"codex exited with code {proc.returncode}"
        )
        status, reason, pstate, guidance = classify_failure(
            backend="codex",
            exit_code=proc.returncode,
            error_text=err_msg,
            stderr_text=stderr_text,
            process_stopped=process_stopped,
        )
        return RunResult(
            status=status,
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
            exit_code=proc.returncode,
            error=err_msg,
            failure_reason=reason,
            provider_state=pstate,
            retry_guidance=guidance,
            stderr_tail=stderr_tail,
            provider_invoked=True,
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
        _ = prior_watermark  # Unused for Codex (emits exact per-turn usage natively).
        overall_deadline = deadline if deadline is not None else (time.monotonic() + request.timeout_seconds)
        local_defaults = read_codex_local_defaults()
        effective_model = self._effective_model(request.model, local_defaults=local_defaults)
        effective_effort = self._effective_effort(request.effort)

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

        # Seamless fallback ONLY if Attempt 1 is verified stopped and did not advance a resumed session.
        if (
            result.status == "failed"
            and result.failure_reason == "model_unavailable"
            and effective_model is not None
            and _can_auto_retry_attempt(request, result, kind="model_unavailable")
            and (overall_deadline - time.monotonic()) > 8.0
        ):
            logger.info(
                "codex rejected model %r; seamlessly retrying with Codex CLI default.",
                effective_model,
            )
            notice = (
                f"Requested Codex model {effective_model!r} was unavailable; "
                "seamlessly fell back to Codex's current default model."
            )
            await _maybe_await(on_attempt, _make_attempt_summary(1, result, backend="codex"))
            second = await self._run_once(
                request,
                effective_model=None,
                effective_effort=effective_effort,
                model_notice=notice,
                deadline=overall_deadline,
                on_session=on_session,
                on_activity=on_activity,
                on_model=on_model,
                on_provider_start=on_provider_start,
            )
            return _merge_retry_results(result, second, backend="codex")

        # One bounded retry for transient service errors on NEW read-only tasks only (never on uncertain/resumed turns).
        if (
            request.mode == "read_only"
            and result.status == "failed"
            and result.failure_reason == "transient_service"
            and _can_auto_retry_attempt(request, result, kind="transient")
            and (overall_deadline - time.monotonic()) > TRANSIENT_RETRY_MIN_REMAINING_SECONDS
        ):
            # Record Attempt 1 BEFORE retry backoff sleep so cancelling during backoff keeps Attempt 1 usage!
            await _maybe_await(on_attempt, _make_attempt_summary(1, result, backend="codex"))
            if on_activity:
                on_activity("transient service error; retrying once")
            await asyncio.sleep(TRANSIENT_RETRY_BACKOFF_SECONDS)
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
            return _merge_retry_results(result, second, backend="codex")

        return result
