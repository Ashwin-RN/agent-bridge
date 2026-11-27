"""Codex CLI (`codex`) backend adapter, subprocess runner, usage mapping, and quota reader."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import sys
import time
import tomllib
from collections.abc import Awaitable, Callable
from datetime import datetime
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

logger = logging.getLogger("agent_bridge.backends.codex")

DEFAULT_CODEX_MODEL = "gpt-6.2-sol"
DEFAULT_CODEX_EFFORT: EffortLevel = "high"


def read_codex_local_defaults() -> dict[str, str | None]:
    """Read non-secret model/effort/sandbox defaults from `~/.codex/config.toml` if present."""
    codex_home_raw = os.environ.get("CODEX_HOME", "").strip()
    codex_home = Path(codex_home_raw).expanduser() if codex_home_raw else Path.home() / ".codex"
    cfg_path = codex_home / "config.toml"
    out: dict[str, str | None] = {
        "model": None,
        "model_reasoning_effort": None,
        "sandbox_mode": None,
    }
    if not cfg_path.exists():
        return out
    try:
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        for key in out:
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                out[key] = val.strip()
    except Exception:
        pass
    return out


def parse_codex_model_family_version(model_id: str | None) -> tuple[str, tuple[int, ...]] | None:
    """Extract `(family_key, version_tuple)` from Codex model IDs like `gpt-6.2-sol` -> `('gpt-sol', (6, 2))`."""
    if not model_id or not model_id.strip():
        return None
    tokens = [t for t in model_id.strip().lower().split("-") if t]
    ver: list[int] = []
    family_parts: list[str] = []
    for tok in tokens:
        if re.fullmatch(r"\d+(?:\.\d+)*", tok):
            ver.extend(int(piece) for piece in tok.split("."))
        else:
            family_parts.append(tok)
    if not ver or not family_parts:
        return None
    return "-".join(family_parts), tuple(ver)


def is_newer_codex_model_in_same_family(candidate: str | None, baseline: str | None) -> bool:
    """Return True if `candidate` is a strictly newer version of the same Codex model family as `baseline`."""
    p_cand = parse_codex_model_family_version(candidate)
    p_base = parse_codex_model_family_version(baseline)
    if p_cand is None or p_base is None:
        return False
    return p_cand[0] == p_base[0] and p_cand[1] > p_base[1]


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


def normalize_codex_usage(
    raw_usage: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Normalize Codex token counters while preserving provider-specific metrics."""
    if not raw_usage or not isinstance(raw_usage, dict):
        return None, None
    input_tokens = to_int(raw_usage, "input_tokens")
    output_tokens = to_int(raw_usage, "output_tokens")
    cached_input = to_int(raw_usage, "cached_input_tokens") or to_int(raw_usage, "cached_tokens")
    cache_write = to_int(raw_usage, "cache_write_input_tokens") or to_int(raw_usage, "cache_write_tokens")
    reasoning_out = to_int(raw_usage, "reasoning_output_tokens") or to_int(raw_usage, "reasoning_tokens")
    # In OpenAI/Codex usage, cached_input_tokens is a subset of input_tokens and
    # reasoning_output_tokens is a subset of output_tokens.
    total = to_int(raw_usage, "total_tokens") or (input_tokens + output_tokens)

    normalized: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_input,
        "reasoning_tokens": reasoning_out,
        "total_tokens": total,
    }
    if cache_write:
        normalized["cache_write_tokens"] = cache_write

    provider_metrics: dict[str, Any] = {
        "provider": "codex",
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_out,
        "total_tokens": total,
    }
    return normalized, provider_metrics


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


def build_codex_args(
    request: RunRequest,
    *,
    base: list[str],
    effective_model: str | None,
    effective_effort: str | None,
) -> tuple[list[str], bool]:
    """Build Codex argv and return `(argv, use_stdin_prompt)`."""
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
        if request.fast:
            args.extend(["-c", 'service_tier="fast"'])
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
        if request.fast:
            args.extend(["-c", 'service_tier="fast"'])
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
    if request.fast:
        args.extend(["-c", 'service_tier="fast"'])
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


def parse_codex_rate_limits(
    result: dict[str, Any],
    *,
    fetched_at: str | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Parse `codex app-server` `account/rateLimits/read` result into a clean quota summary."""
    from agent_bridge.quota import (
        _as_number,
        _epoch_to_iso,
        _iso_now,
        _window_label,
        enrich_quota_reading,
    )

    ts = fetched_at or _iso_now()
    rl = result.get("rateLimits")
    if not isinstance(rl, dict):
        return {
            "backend": "codex",
            "pool": "codex",
            "status": "unknown",
            "fetched_at": ts,
            "error": "Missing rateLimits object in Codex app-server response.",
        }

    windows: list[dict[str, Any]] = []
    for slot in ("primary", "secondary"):
        bucket = rl.get(slot)
        if not isinstance(bucket, dict):
            continue
        used_num = _as_number(bucket.get("usedPercent"))
        if used_num is None or used_num < 0.0 or used_num > 100.0:
            continue
        used_pct = round(used_num, 1)
        rem_pct = round(max(0.0, 100.0 - used_pct), 1)
        windows.append(
            {
                "slot": slot,
                "window": _window_label(bucket.get("windowDurationMins")),
                "used_percent": used_pct,
                "remaining_percent": rem_pct,
                "reset_time": _epoch_to_iso(bucket.get("resetsAt")),
            }
        )

    if not windows:
        return {
            "backend": "codex",
            "pool": "codex",
            "status": "unknown",
            "fetched_at": ts,
            "plan_type": rl.get("planType"),
            "windows": [],
            "error": "Codex rateLimits contained no usable quota windows.",
        }

    out: dict[str, Any] = {
        "backend": "codex",
        "pool": "codex",
        "status": "ok",
        "fetched_at": ts,
        "plan_type": rl.get("planType"),
        "ordinary_usage_allowed": result.get("ordinaryUsageAllowed"),
        "rate_limit_reached": bool(rl.get("rateLimitReachedType")) or bool(rl.get("spendControlReached")),
        "windows": windows,
    }
    if rl.get("rateLimitReachedType"):
        out["rate_limit_reached_type"] = rl.get("rateLimitReachedType")
    return enrich_quota_reading(out, now_utc=now_utc)


async def fetch_codex_quota(codex_cmd: list[str], *, timeout_seconds: float = 12.0) -> dict[str, Any]:
    """Perform a short-lived one-shot `codex app-server` `account/rateLimits/read` RPC."""
    import asyncio

    from agent_bridge import process_control
    from agent_bridge.quota import _iso_now

    ts = _iso_now()
    managed = None
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    try:
        proc, managed = await process_control.spawn_managed_process(
            *codex_cmd,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=max(0.05, deadline - time.monotonic()),
        )
        assert proc.stdin is not None and proc.stdout is not None

        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "agent-bridge", "version": "0.3.0"},
                    "capabilities": {},
                },
            },
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
        ]
        payload = "".join(json.dumps(m) + "\n" for m in messages).encode("utf-8")
        proc.stdin.write(payload)
        await asyncio.wait_for(proc.stdin.drain(), timeout=max(0.05, deadline - time.monotonic()))

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "backend": "codex",
                    "pool": "codex",
                    "status": "unknown",
                    "fetched_at": ts,
                    "error": f"Timed out reading Codex rate limits after {timeout_seconds:g}s.",
                }
            raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not raw_line:
                break
            text = raw_line.decode("utf-8", errors="replace").strip()
            if not text.startswith("{"):
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == 2:
                if "result" in msg and isinstance(msg["result"], dict):
                    return parse_codex_rate_limits(msg["result"], fetched_at=ts)
                err = msg.get("error", {})
                return {
                    "backend": "codex",
                    "pool": "codex",
                    "status": "unknown",
                    "fetched_at": ts,
                    "error": str(err.get("message") if isinstance(err, dict) else err),
                }

        return {
            "backend": "codex",
            "pool": "codex",
            "status": "unknown",
            "fetched_at": ts,
            "error": "Codex app-server closed before returning rate limits.",
        }
    except TimeoutError:
        return {
            "backend": "codex",
            "pool": "codex",
            "status": "unknown",
            "fetched_at": ts,
            "error": f"Timed out reading Codex rate limits after {timeout_seconds:g}s.",
        }
    except Exception as exc:
        return {
            "backend": "codex",
            "pool": "codex",
            "status": "unknown",
            "fetched_at": ts,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if managed is not None:
            await managed.terminate()


class CodexRunner:
    """Executes subagent turns via Codex CLI (`codex exec` / `resume` / `review`)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve_command(self) -> list[str]:
        return resolve_codex_command(self._settings.codex_bin)

    async def check_health(self) -> dict[str, Any]:
        from agent_bridge.runners import _check_cli_health

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
        return build_codex_args(
            request,
            base=self.resolve_command(),
            effective_model=effective_model,
            effective_effort=effective_effort,
        )

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
        local_defaults = read_codex_local_defaults()
        reported_model = effective_model or local_defaults.get("model") or "codex-default"
        reported_effort = effective_effort or local_defaults.get("model_reasoning_effort")
        if on_model:
            on_model(reported_model, "requested" if effective_model else "default")

        remaining = deadline - started
        if remaining <= 0.05:
            status, reason, pstate, guidance = runners.classify_failure(
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
            wt_info = await runners.prepare_codex_worktree(
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
        await runners._maybe_await(on_provider_start)

        env = runners.job_env(self._settings, request, cli="codex")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessStartupTimeout(
                    "Job deadline expired before execution started.", process_stopped=True,
                )
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
            status, reason, pstate, guidance = runners.classify_failure(
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
        stderr_tail = runners._tail(stderr_text)
        parsed = parse_codex_jsonl_events(stdout_lines)
        session_id = parsed["session_id"] or request.session_id
        response = parsed["response"]
        errors = parsed["errors"]
        if proc.returncode == 0 and not errors and not response:
            response = runners._plain_stdout_reply(stdout_lines)
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

        usage, provider_metrics = normalize_codex_usage(parsed["usage"])
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

        if timed_out_locally:
            status, reason, pstate, guidance = runners.classify_failure(
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
                error=runners.CLI_OUTPUT_CUT_SHORT_ERROR.format(event="turn.completed"),
                failure_reason="cli_error",
                provider_state="stopped" if process_stopped else "possibly_running",
                retry_guidance=runners.CLI_OUTPUT_CUT_SHORT_GUIDANCE,
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
        status, reason, pstate, guidance = runners.classify_failure(
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
        import asyncio

        from agent_bridge import runners

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
            and runners._can_auto_retry_attempt(request, result, kind="model_unavailable")
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
            await runners._maybe_await(on_attempt, runners._make_attempt_summary(1, result, backend="codex"))
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
            return runners._merge_retry_results(result, second, backend="codex")

        # One bounded retry for transient service errors on NEW read-only tasks only (never on uncertain/resumed turns).
        if (
            request.mode == "read_only"
            and result.status == "failed"
            and result.failure_reason == "transient_service"
            and runners._can_auto_retry_attempt(request, result, kind="transient")
            and (overall_deadline - time.monotonic()) > runners.TRANSIENT_RETRY_MIN_REMAINING_SECONDS
        ):
            # Record Attempt 1 BEFORE retry backoff sleep so cancelling during backoff keeps Attempt 1 usage!
            await runners._maybe_await(on_attempt, runners._make_attempt_summary(1, result, backend="codex"))
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
            return runners._merge_retry_results(result, second, backend="codex")

        return result


class CodexAdapter(BackendAdapter):
    """Adapter for the OpenAI Codex CLI (`codex`)."""

    name = "codex"
    cli_name = "codex"
    aliases = ("gpt", "openai")
    dashboard_order = 10
    advisory_order = 10
    default_model_placeholder = "codex-default"
    missing_terminal_event_name = "turn.completed"
    capabilities = Capabilities(
        resume=True,
        worktree=True,
        builtin_review=True,
        model_catalog=False,
        fast_tier=True,
        extra_dirs=True,
        extra_dirs_on_resume=False,
        reports_observed_model=False,
        model_encodes_effort=False,
        cumulative_session_usage=False,
        tracks_native_subagents=False,
    )
    quota_pools = (
        QuotaPoolSpec(
            pool="codex",
            backend="codex",
            label="Codex CLI",
            dashboard_label="Codex",
            advisory_models_fallback=(f"{DEFAULT_CODEX_MODEL} ({DEFAULT_CODEX_EFFORT})",),
            quota_display_order=10,
        ),
    )

    @property
    def default_model_label(self) -> str:
        return "Codex's default model"

    def defaults(self, settings: Settings) -> BackendDefaults:
        return BackendDefaults(
            model=settings.default_codex_model,
            effort=settings.default_codex_effort,
            timeout_seconds=settings.default_timeout_seconds,
        )

    def resolve_model_and_effort(
        self,
        settings: Settings,
        requested_model: str | None,
        requested_effort: str | None,
    ) -> tuple[str | None, str | None]:
        runner = CodexRunner(settings)
        return runner._effective_model(requested_model), runner._effective_effort(requested_effort)

    def extra_dirs_on_resume_error(self) -> str:
        return (
            "extra_dirs is not supported when resuming a Codex session "
            "(`codex exec resume` does not accept --add-dir). Start a new Codex session instead."
        )

    def resolve_command(self, settings: Settings) -> list[str]:
        return resolve_codex_command(settings.codex_bin)

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
        argv, use_stdin = build_codex_args(
            request, base=cmd, effective_model=model, effective_effort=effort,
        )
        return CommandSpec(argv=argv, stdin_text=request.prompt if use_stdin else None)

    def parse_stream(
        self,
        lines: list[str],
        *,
        request: RunRequest | None = None,
    ) -> ParsedStream:
        parsed = parse_codex_jsonl_events(lines)
        response = parsed["response"]
        builtin_review = bool(
            request is not None
            and request.review_scope is not None
            and not request.prompt.strip()
            and response
        )
        return ParsedStream(
            session_id=parsed["session_id"],
            model=None,
            response=response,
            usage=parsed["usage"],
            terminal_event_seen=bool(parsed["turn_completed"] or builtin_review),
            is_error=bool(parsed["errors"]),
            errors=tuple(parsed["errors"]),
        )

    def describe_event(self, event: dict[str, Any]) -> str | None:
        return _describe_codex_event(event)

    def normalize_usage(
        self,
        raw_usage: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return normalize_codex_usage(raw_usage)

    async def probe_backend(
        self,
        runner: Any,
        settings: Settings,
        *,
        refresh_models: bool = False,
    ) -> dict[str, Any]:
        _ = refresh_models
        try:
            codex_health = await runner.check_health()
        except Exception as exc:
            codex_health = unhealthy_payload(exc)
        return {
            "backend": "codex",
            "cli": "codex",
            "default_model": settings.default_codex_model,
            "default_effort": settings.default_codex_effort,
            **codex_health,
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
        from agent_bridge.quota import enrich_quota_reading, has_passed_reset

        cached = await db_call(store.get_cached_quota, self.name, ttl_seconds)
        if cached is not None and not has_passed_reset(cached):
            return enrich_quota_reading(cached)

        try:
            cmd = runner.resolve_command()
        except Exception as exc:
            return {"backend": "codex", "pool": "codex", "status": "unknown", "error": str(exc)}
        res = await fetch_codex_quota(cmd)
        res = enrich_quota_reading(res)
        res["cached"] = False
        if save_to_cache and res.get("status") == "ok":
            try:
                await db_call(store.set_cached_quota, self.name, res)
            except Exception:
                pass
        return res

    def create_runner(self, settings: Settings) -> CodexRunner:
        return CodexRunner(settings)
