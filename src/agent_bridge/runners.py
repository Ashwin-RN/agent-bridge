"""Shared subprocess runner utilities: environment trimming, failure classification, retries, and Git worktrees."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from agent_bridge.backends.registry import get_adapter_optional
from agent_bridge.config import Settings, classify_quota_pool
from agent_bridge.model_resolver import is_model_unavailable_error
from agent_bridge.models import JobStatus, ProviderState, RunRequest, RunResult
from agent_bridge.nesting import credential_env_names, subagent_env
from agent_bridge.process_control import (
    ManagedProcess,
    communicate_managed,
    spawn_managed_process,
)

logger = logging.getLogger("agent_bridge.runners")

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
    """Build a marked subagent environment, filtering by variable name in trim mode.

    The filter logs exclusions and explicit exceptions by name. It does not inspect values such as
    URLs. Backends with custom credential policies may override `build_env` on their adapter.
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


def _plain_stdout_reply(lines: list[str]) -> str:
    return "\n".join(line.rstrip("\r\n") for line in lines if not line.lstrip().startswith("{")).strip()


def _repo_worktree_lock(repo_root_key: str) -> asyncio.Lock:
    lock = _WORKTREE_REPO_LOCKS.get(repo_root_key)
    if lock is None:
        lock = asyncio.Lock()
        _WORKTREE_REPO_LOCKS[repo_root_key] = lock
    return lock


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

    adapter = get_adapter_optional(backend)
    if adapter is not None:
        specific = adapter.classify_failure(
            exit_code=exit_code,
            error_text=error_text,
            stderr_text=stderr_text,
            timed_out_locally=timed_out_locally,
            process_stopped=process_stopped,
        )
        if specific is not None:
            return specific

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
    """Deterministically assign or reuse a per-job Git worktree BEFORE launching the subagent."""
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


_COMPAT_EXPORTS: dict[str, tuple[str, str]] = {
    "AGY_MAX_INLINE_PROMPT_CHARS": ("agent_bridge.backends.agy", "AGY_MAX_INLINE_PROMPT_CHARS"),
    "AGY_READ_ONLY_GUARD": ("agent_bridge.backends.agy", "AGY_READ_ONLY_GUARD"),
    "NeonRunner": ("agent_bridge.backends.agy", "NeonRunner"),
    "agy_reports_turn_still_running": ("agent_bridge.backends.agy", "agy_reports_turn_still_running"),
    "parse_agy_json_output": ("agent_bridge.backends.agy", "parse_agy_json_output"),
    "resolve_agy_command": ("agent_bridge.backends.agy", "resolve_agy_command"),
    "CodexRunner": ("agent_bridge.backends.codex", "CodexRunner"),
    "parse_codex_jsonl_events": ("agent_bridge.backends.codex", "parse_codex_jsonl_events"),
    "read_codex_local_defaults": ("agent_bridge.backends.codex", "read_codex_local_defaults"),
    "resolve_codex_command": ("agent_bridge.backends.codex", "resolve_codex_command"),
}


def __getattr__(name: str) -> Any:
    target = _COMPAT_EXPORTS.get(name)
    if target is not None:
        import importlib

        mod = importlib.import_module(target[0])
        return getattr(mod, target[1])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
