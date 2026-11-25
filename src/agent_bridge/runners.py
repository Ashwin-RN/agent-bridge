"""Subprocess runners for Antigravity (`agy`) and Codex (`codex`) CLIs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_bridge.config import Settings, read_codex_local_defaults
from agent_bridge.model_resolver import (
    DEFAULT_INTENT_TOKENS,
    NeonModelCatalog,
    is_model_unavailable_error,
)
from agent_bridge.models import JobStatus, ProviderState, RunRequest, RunResult, UsageStatus
from agent_bridge.process_control import ManagedProcess, subprocess_creation_kwargs
from agent_bridge.usage import (
    compute_agy_usage_delta,
    normalize_usage,
    sum_agy_step_usages,
)

logger = logging.getLogger("agent_bridge.runners")

AGY_MAX_INLINE_PROMPT_CHARS = 20_000
STDERR_TAIL_MAX_CHARS = 4_000
TRANSIENT_RETRY_BACKOFF_SECONDS = 2.0
TRANSIENT_RETRY_MIN_REMAINING_SECONDS = 12.0

OnSessionCallback = Callable[[str], None]
OnActivityCallback = Callable[[str], None]
OnModelCallback = Callable[[str], None]

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


def _tail(text: str, limit: int = STDERR_TAIL_MAX_CHARS) -> str | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return "...[truncated]\n" + cleaned[-limit:]


def classify_failure(
    *,
    backend: str,
    exit_code: int | None,
    error_text: str | None,
    stderr_text: str | None,
    timed_out_locally: bool = False,
    agy_permission_fallback: bool = False,
) -> tuple[JobStatus, str, ProviderState, str]:
    """Classify a non-success outcome into `(status, failure_reason, provider_state, retry_guidance)`."""
    combined = f"{error_text or ''}\n{stderr_text or ''}"

    if timed_out_locally:
        return (
            "timed_out",
            "timeout",
            "stopped",
            "Local bridge deadline elapsed and the subprocess tree was terminated. "
            "Preserve any partial response and session_id; inspect git diff/worktree before retrying edits.",
        )

    if backend == "neon" and (
        exit_code == 3 or any(p.search(combined) for p in _AGY_TIMEOUT_PATTERNS)
    ):
        return (
            "timed_out",
            "provider_timeout_still_running",
            "possibly_running",
            "Antigravity print timeout elapsed while the server-side turn may still be in progress. "
            "Keep the partial response and session_id; confirm the prior turn finished (and inspect git diff "
            "for edits) before resuming.",
        )

    if any(p.search(combined) for p in _AUTH_PATTERNS):
        return (
            "failed",
            "auth_error",
            "stopped",
            "CLI authentication failed. Re-authenticate the underlying CLI (`agy` or `codex login`) and retry.",
        )

    if any(p.search(combined) for p in _QUOTA_PATTERNS):
        return (
            "failed",
            "quota_exceeded",
            "stopped",
            "Subscription quota or rate limit reached. Call get_usage(include_quota=True) to check reset "
            "times or delegate to the other backend.",
        )

    if is_model_unavailable_error(combined):
        return (
            "failed",
            "model_unavailable",
            "stopped",
            "Requested model was rejected by the CLI. Refresh models via list_backends(refresh_models=True) "
            "or use a versionless shortcut.",
        )

    if any(p.search(combined) for p in _TRANSIENT_PATTERNS):
        return (
            "failed",
            "transient_service",
            "stopped",
            "Transient provider/network error. Safe to retry a read-only task; for edit tasks reconcile "
            "git status/diff before retrying.",
        )

    if agy_permission_fallback or any(p.search(combined) for p in _INTERACTIVE_FALLBACK_PATTERNS):
        return (
            "failed",
            "interactive_fallback",
            "stopped",
            "Antigravity fell back to interactive permission prompts in headless mode. For unattended edits "
            "use backend='codex' with mode='edit' (and worktree=True), or keep Neon read-only.",
        )

    if any(p.search(combined) for p in _PERMISSION_PATTERNS):
        return (
            "failed",
            "permission_error",
            "stopped",
            "Subagent lacked permission for an attempted action. Retry with mode='edit' (or extra_dirs) "
            "if writes outside read-only scope were intended.",
        )

    return (
        "failed",
        "cli_error",
        "stopped",
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


def parse_agy_json_output(stdout_text: str) -> dict[str, Any] | None:
    """Parse single-envelope `agy --output-format json` stdout, tolerating leading non-JSON lines."""
    stripped = stdout_text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def parse_codex_jsonl_events(lines: list[str]) -> dict[str, Any]:
    """Parse Codex `--json` JSONL events into session_id, response, usage, and errors."""
    session_id: str | None = None
    messages: list[str] = []
    usage: dict[str, Any] | None = None
    errors: list[str] = []

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
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = raw_usage
        elif etype in {"error", "turn.failed"}:
            msg = event.get("message")
            if not msg and isinstance(event.get("error"), dict):
                msg = event["error"].get("message")
            if isinstance(msg, str) and msg.strip():
                errors.append(msg.strip())

    return {
        "session_id": session_id,
        "response": "\n\n".join(messages).strip(),
        "usage": usage,
        "errors": errors,
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


async def _list_git_worktrees(cwd: str) -> list[dict[str, str | None]]:
    git_bin = shutil.which("git")
    if not git_bin:
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            git_bin,
            "worktree",
            "list",
            "--porcelain",
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            return []
        return _parse_git_worktree_porcelain(stdout_b.decode("utf-8", errors="replace"))
    except Exception:
        return []


def _norm_path_key(path_str: str | None) -> str:
    if not path_str:
        return ""
    try:
        return os.path.normcase(str(Path(path_str).resolve()))
    except Exception:
        return os.path.normcase(path_str)


async def _check_cli_health(cmd: list[str]) -> tuple[bool, str | None, str | None]:
    """Run `<cmd> --version` and return `(healthy, version_line, error_msg)`."""
    managed: ManagedProcess | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_creation_kwargs(),
        )
        managed = ManagedProcess(proc)
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        managed.close()
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
        managed: ManagedProcess | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **subprocess_creation_kwargs(),
            )
            managed = ManagedProcess(proc)
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
            managed.close()
        except Exception:
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

    def build_args(
        self,
        request: RunRequest,
        *,
        resolved_model: str | None,
        prompt_text: str,
        extra_dirs: list[str],
        remaining_seconds: int,
    ) -> list[str]:
        args: list[str] = [
            *self.resolve_command(),
            "--output-format",
            "stream-json",
            "--mode",
            "plan" if request.mode == "read_only" else "accept-edits",
            "--print-timeout",
            f"{max(5, remaining_seconds)}s",
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
    ) -> RunResult:
        started = time.monotonic()
        remaining = deadline - started
        if remaining <= 0.5:
            status, reason, pstate, guidance = classify_failure(
                backend="neon",
                exit_code=None,
                error_text="Job deadline expired before execution started.",
                stderr_text=None,
                timed_out_locally=True,
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
            remaining_seconds=int(remaining),
        )
        if on_model and resolved_model:
            on_model(resolved_model)
        if on_activity:
            on_activity(f"running agy ({resolved_model or 'agy-default'})")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=request.cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_creation_kwargs(),
        )
        managed = ManagedProcess(proc)

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
                            on_model(im)
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
            elif ev_type == "result":
                res_obj = obj.get("result")
                if isinstance(res_obj, dict):
                    result_envelope = res_obj
                    cid = res_obj.get("conversation_id")
                    if isinstance(cid, str) and cid:
                        session_id = cid
                        if on_session:
                            on_session(cid)
            elif "conversation_id" in obj and ("response" in obj or "status" in obj):
                # Plain single-JSON envelope fallback (e.g. non-stream output or test stubs).
                plain_envelope = obj
                cid = obj.get("conversation_id")
                if isinstance(cid, str) and cid:
                    session_id = cid
                    if on_session:
                        on_session(cid)

        async def _read_stdout() -> None:
            assert proc.stdout is not None
            buf = bytearray()
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
            if buf:
                _handle_line(bytes(buf).decode("utf-8", errors="replace"))

        async def _read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(8192)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        timed_out_locally = False
        try:
            # Give agy's own --print-timeout a slight cushion (5s) so it can emit exit code 3 + session_id cleanly,
            # while still enforcing our hard local deadline.
            wait_budget = max(1.0, deadline - time.monotonic())
            await asyncio.wait_for(
                asyncio.gather(_read_stdout(), _read_stderr(), proc.wait()),
                timeout=wait_budget + 5.0,
            )
            managed.close()
        except asyncio.TimeoutError:
            timed_out_locally = True
            await managed.terminate()
        except asyncio.CancelledError:
            await managed.terminate()
            raise

        # Always measure wall-clock duration per invocation (never cumulative agy duration!).
        elapsed = round(max(0.0, time.monotonic() - started), 2)
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        stderr_tail = _tail(stderr_text)
        partial_text = "".join(partial_text_parts).strip()

        envelope = result_envelope or plain_envelope
        if envelope is None:
            envelope = parse_agy_json_output("".join(stdout_lines))

        # Determine per-turn usage accurately without double-counting prior resumed turns.
        step_sum = sum_agy_step_usages(list(step_usages_by_index.values()))
        env_raw_usage = (
            envelope.get("usage")
            if isinstance(envelope, dict) and isinstance(envelope.get("usage"), dict)
            else None
        )
        num_turns = (
            int(envelope["num_turns"])
            if isinstance(envelope, dict) and isinstance(envelope.get("num_turns"), int)
            else None
        )

        usage: dict[str, Any] | None = None
        provider_metrics: dict[str, Any] | None = None
        usage_status: UsageStatus = "missing"

        if step_sum is not None:
            usage, provider_metrics = normalize_usage("neon", step_sum)
            usage_status = "exact"
        elif env_raw_usage is not None:
            is_first_turn = (not request.session_id) and (num_turns in {None, 1})
            if is_first_turn:
                usage, provider_metrics = normalize_usage("neon", env_raw_usage)
                usage_status = "exact"
            elif (
                prior_watermark is not None
                and isinstance(prior_watermark.get("cumulative"), dict)
                and (
                    num_turns is None
                    or int(prior_watermark.get("num_turns", 0)) + 1 == num_turns
                )
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
                # Resumed turn with cumulative envelope and no verified watermark: never double-count!
                _, provider_metrics = normalize_usage("neon", env_raw_usage)
                if provider_metrics is not None:
                    provider_metrics["cumulative_only"] = True
                usage_status = "partial"

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

        if timed_out_locally:
            status, reason, pstate, guidance = classify_failure(
                backend="neon",
                exit_code=proc.returncode,
                error_text=f"Neon subagent timed out after {request.timeout_seconds}s.",
                stderr_text=stderr_text,
                timed_out_locally=True,
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
                usage=usage,
                provider_metrics=provider_metrics,
                usage_status=usage_status,
                cumulative_usage=env_raw_usage,
                num_turns=num_turns,
                exit_code=proc.returncode,
                error=f"Neon subagent timed out after {request.timeout_seconds}s.",
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
                stderr_tail=stderr_tail,
            )

        if envelope is not None:
            cid = envelope.get("conversation_id")
            if isinstance(cid, str) and cid:
                session_id = cid
            raw_status = str(envelope.get("status", "")).upper()
            response_text = str(envelope.get("response", "")).strip() or partial_text

            if proc.returncode == 0 and raw_status in {"", "SUCCESS", "OK"}:
                return RunResult(
                    status="completed",
                    response=response_text,
                    session_id=session_id,
                    model=actual_model,
                    effort=request.effort,
                    model_notice=combined_notice,
                    effective_cwd=request.cwd,
                    duration_seconds=elapsed,
                    usage=usage,
                    provider_metrics=provider_metrics,
                    usage_status=usage_status,
                    cumulative_usage=env_raw_usage,
                    num_turns=num_turns,
                    exit_code=0,
                    provider_state="stopped",
                    stderr_tail=stderr_tail,
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
                usage=usage,
                provider_metrics=provider_metrics,
                usage_status=usage_status,
                cumulative_usage=env_raw_usage,
                num_turns=num_turns,
                exit_code=proc.returncode,
                error=err_msg,
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
                stderr_tail=stderr_tail,
            )

        plain_text = partial_text or "".join(stdout_lines).strip()
        if proc.returncode == 0:
            return RunResult(
                status="completed",
                response=plain_text,
                session_id=session_id,
                model=actual_model,
                effort=request.effort,
                model_notice=combined_notice,
                effective_cwd=request.cwd,
                duration_seconds=elapsed,
                usage=usage,
                provider_metrics=provider_metrics,
                usage_status=usage_status,
                exit_code=0,
                provider_state="stopped",
                stderr_tail=stderr_tail,
            )

        err_msg = stderr_tail or plain_text or f"agy exited with code {proc.returncode}"
        status, reason, pstate, guidance = classify_failure(
            backend="neon",
            exit_code=proc.returncode,
            error_text=err_msg,
            stderr_text=stderr_text,
            agy_permission_fallback=agy_perm_fallback,
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
            usage=usage,
            provider_metrics=provider_metrics,
            usage_status=usage_status,
            exit_code=proc.returncode,
            error=err_msg,
            failure_reason=reason,
            provider_state=pstate,
            retry_guidance=guidance,
            stderr_tail=stderr_tail,
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

            lookup_budget = max(2.0, min(20.0, overall_deadline - time.monotonic()))
            resolution = await self.catalog.resolve(
                request.model,
                default_intent=self._settings.default_neon_model,
                effort_override=request.effort,
                timeout_seconds=lookup_budget,
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
            )

            # Seamless self-healing retry if selected model was retired/rejected.
            if (
                result.status == "failed"
                and result.failure_reason == "model_unavailable"
                and resolution.resolved is not None
                and (overall_deadline - time.monotonic()) > 8.0
            ):
                logger.info(
                    "agy rejected model %r; refreshing catalog and retrying within deadline.",
                    resolution.resolved,
                )
                refresh_budget = max(2.0, min(15.0, overall_deadline - time.monotonic()))
                refreshed = await self.catalog.resolve(
                    request.model,
                    default_intent=self._settings.default_neon_model,
                    effort_override=request.effort,
                    force_refresh=True,
                    timeout_seconds=refresh_budget,
                )
                retry_model = (
                    refreshed.resolved
                    if refreshed.resolved != resolution.resolved
                    else None
                )
                notice = (
                    f"Model {resolution.resolved!r} was unavailable; seamlessly refreshed and retried with "
                    f"{retry_model or 'agy default'}."
                )
                return await self._run_once(
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
                )

            # One bounded retry for transient service errors on read-only tasks only.
            if (
                request.mode == "read_only"
                and result.status == "failed"
                and result.failure_reason == "transient_service"
                and (overall_deadline - time.monotonic()) > TRANSIENT_RETRY_MIN_REMAINING_SECONDS
            ):
                if on_activity:
                    on_activity("transient service error; retrying once")
                await asyncio.sleep(TRANSIENT_RETRY_BACKOFF_SECONDS)
                return await self._run_once(
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
                )

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

    def _effective_model(self, requested: str | None) -> str | None:
        raw = (requested or "").strip()
        if raw.lower() in DEFAULT_INTENT_TOKENS:
            return self._settings.default_codex_model
        return raw

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
            if request.worktree and request.mode == "edit":
                args.append("--worktree")
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
        if request.worktree and request.mode == "edit":
            args.append("--worktree")
        for directory in request.extra_dirs:
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
    ) -> RunResult:
        started = time.monotonic()
        local_defaults = read_codex_local_defaults()
        reported_model = effective_model or local_defaults.get("model") or "codex-default"
        reported_effort = effective_effort or local_defaults.get("model_reasoning_effort")

        remaining = deadline - started
        if remaining <= 0.5:
            status, reason, pstate, guidance = classify_failure(
                backend="codex",
                exit_code=None,
                error_text="Job deadline expired before execution started.",
                stderr_text=None,
                timed_out_locally=True,
            )
            return RunResult(
                status=status,
                session_id=request.session_id,
                model=reported_model,
                effort=reported_effort,
                model_notice=model_notice,
                effective_cwd=request.cwd,
                duration_seconds=0.0,
                usage_status="missing",
                error="Job deadline expired before execution started.",
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
            )

        worktrees_before: set[str] = set()
        if request.worktree:
            before_list = await _list_git_worktrees(request.cwd)
            worktrees_before = {_norm_path_key(e.get("path")) for e in before_list}

        cmd, use_stdin_prompt = self.build_args(
            request,
            effective_model=effective_model,
            effective_effort=effective_effort,
        )
        if on_model:
            on_model(reported_model)
        if on_activity:
            on_activity(f"running codex ({reported_model})")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=request.cwd,
            stdin=asyncio.subprocess.PIPE if use_stdin_prompt else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_creation_kwargs(),
        )
        managed = ManagedProcess(proc)

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
            if buf:
                _handle_stdout_line(bytes(buf).decode("utf-8", errors="replace"))

        async def _read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(8192)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        timed_out_locally = False
        try:
            wait_budget = max(1.0, deadline - time.monotonic())
            await asyncio.wait_for(
                asyncio.gather(_write_stdin(), _read_stdout(), _read_stderr(), proc.wait()),
                timeout=wait_budget,
            )
            managed.close()
        except asyncio.TimeoutError:
            timed_out_locally = True
            await managed.terminate()
        except asyncio.CancelledError:
            await managed.terminate()
            raise

        elapsed = round(max(0.0, time.monotonic() - started), 2)
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        stderr_tail = _tail(stderr_text)
        parsed = parse_codex_jsonl_events(stdout_lines)
        session_id = parsed["session_id"] or request.session_id
        response = parsed["response"]
        usage, provider_metrics = normalize_usage("codex", parsed["usage"])
        usage_status: UsageStatus = "exact" if usage is not None else "missing"
        errors = parsed["errors"]

        # Detect any newly created Git worktree so Claude gets exact checkout path/branch/commit.
        effective_cwd = request.cwd
        wt_path: str | None = None
        wt_branch: str | None = None
        wt_commit: str | None = None
        if request.worktree:
            after_list = await _list_git_worktrees(request.cwd)
            new_wts = [
                e for e in after_list if _norm_path_key(e.get("path")) not in worktrees_before
            ]
            if new_wts:
                chosen = new_wts[-1]
                wt_path = chosen.get("path")
                wt_branch = chosen.get("branch")
                wt_commit = chosen.get("commit")
                if wt_path:
                    effective_cwd = wt_path

        if timed_out_locally:
            status, reason, pstate, guidance = classify_failure(
                backend="codex",
                exit_code=proc.returncode,
                error_text=f"Codex subagent timed out after {request.timeout_seconds}s.",
                stderr_text=stderr_text,
                timed_out_locally=True,
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
                usage=usage,
                provider_metrics=provider_metrics,
                usage_status=usage_status,
                exit_code=proc.returncode,
                error=f"Codex subagent timed out after {request.timeout_seconds}s.",
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
                stderr_tail=stderr_tail,
            )

        if proc.returncode == 0 and not errors:
            if not response:
                non_json = "\n".join(
                    ln.strip()
                    for ln in stdout_lines
                    if ln.strip() and not ln.strip().startswith("{")
                ).strip()
                response = non_json
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
                usage=usage,
                provider_metrics=provider_metrics,
                usage_status=usage_status,
                exit_code=0,
                provider_state="stopped",
                stderr_tail=stderr_tail,
            )

        err_msg = "; ".join(errors) if errors else (
            stderr_tail or f"codex exited with code {proc.returncode}"
        )
        status, reason, pstate, guidance = classify_failure(
            backend="codex",
            exit_code=proc.returncode,
            error_text=err_msg,
            stderr_text=stderr_text,
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
            usage=usage,
            provider_metrics=provider_metrics,
            usage_status=usage_status,
            exit_code=proc.returncode,
            error=err_msg,
            failure_reason=reason,
            provider_state=pstate,
            retry_guidance=guidance,
            stderr_tail=stderr_tail,
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
    ) -> RunResult:
        _ = prior_watermark  # Unused for Codex (emits exact per-turn usage natively).
        overall_deadline = deadline if deadline is not None else (time.monotonic() + request.timeout_seconds)
        effective_model = self._effective_model(request.model)
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
        )

        # Seamless fallback if an explicit/pinned Codex model is rejected/retired.
        if (
            result.status == "failed"
            and result.failure_reason == "model_unavailable"
            and effective_model is not None
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
            return await self._run_once(
                request,
                effective_model=None,
                effective_effort=effective_effort,
                model_notice=notice,
                deadline=overall_deadline,
                on_session=on_session,
                on_activity=on_activity,
                on_model=on_model,
            )

        # One bounded retry for transient service errors on read-only tasks only.
        if (
            request.mode == "read_only"
            and result.status == "failed"
            and result.failure_reason == "transient_service"
            and (overall_deadline - time.monotonic()) > TRANSIENT_RETRY_MIN_REMAINING_SECONDS
        ):
            if on_activity:
                on_activity("transient service error; retrying once")
            await asyncio.sleep(TRANSIENT_RETRY_BACKOFF_SECONDS)
            return await self._run_once(
                request,
                effective_model=effective_model,
                effective_effort=effective_effort,
                model_notice=None,
                deadline=overall_deadline,
                on_session=on_session,
                on_activity=on_activity,
                on_model=on_model,
            )

        return result
