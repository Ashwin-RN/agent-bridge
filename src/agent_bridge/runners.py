"""Subprocess runners for Antigravity (`agy`) and Codex (`codex`) CLIs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_bridge.config import Settings
from agent_bridge.model_resolver import (
    DEFAULT_INTENT_TOKENS,
    NeonModelCatalog,
    is_model_unavailable_error,
)
from agent_bridge.models import RunRequest, RunResult
from agent_bridge.usage import normalize_usage

logger = logging.getLogger("agent_bridge.runners")

# Keep agy CLI argument comfortably below Windows CreateProcessW 32,767-char limit.
AGY_MAX_INLINE_PROMPT_CHARS = 20_000
STDERR_TAIL_MAX_CHARS = 4_000

OnProcessCallback = Callable[[asyncio.subprocess.Process], None]
OnSessionCallback = Callable[[str], None]
OnActivityCallback = Callable[[str], None]


def _tail(text: str, limit: int = STDERR_TAIL_MAX_CHARS) -> str | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return "...[truncated]\n" + cleaned[-limit:]


def _subprocess_creation_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess and any child processes cleanly across platforms."""
    if proc.returncode is not None:
        return

    if sys.platform == "win32":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5.0)
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            try:
                proc.terminate()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except Exception:
        pass


def resolve_agy_command(configured: str | None = None) -> list[str]:
    """Locate the `agy` executable without going through a shell."""
    if configured:
        return [configured]

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
        return [configured]

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
    """Parse `agy --output-format json` stdout, tolerating leading non-JSON lines."""
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


class NeonRunner:
    """Executes subagent turns via Antigravity CLI (`agy --print`) with live model resolution."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.catalog = NeonModelCatalog(
            fetcher=self._fetch_live_models,
            cache_path=settings.state_dir / "agy_models_cache.json",
            ttl_seconds=settings.models_cache_ttl_seconds,
        )

    def resolve_command(self) -> list[str]:
        return resolve_agy_command(self._settings.agy_bin)

    async def get_version(self) -> str | None:
        try:
            cmd = [*self.resolve_command(), "--version"]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            text = (stdout or stderr).decode("utf-8", errors="replace").strip()
            return text.splitlines()[0] if text else None
        except Exception:
            return None

    async def _fetch_live_models(self) -> list[dict[str, str]]:
        cmd = [*self.resolve_command(), "models"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
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
    ) -> list[str]:
        args: list[str] = [
            *self.resolve_command(),
            "--output-format",
            "json",
            "--mode",
            "plan" if request.mode == "read_only" else "auto",
            "--print-timeout",
            f"{request.timeout_seconds}s",
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
        on_process: OnProcessCallback | None,
        on_session: OnSessionCallback | None,
        on_activity: OnActivityCallback | None,
    ) -> RunResult:
        started = time.monotonic()
        cmd = self.build_args(
            request,
            resolved_model=resolved_model,
            prompt_text=prompt_text,
            extra_dirs=extra_dirs,
        )
        if on_activity:
            on_activity(f"running agy ({resolved_model or 'agy-default'})")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=request.cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_subprocess_creation_kwargs(),
        )
        if on_process:
            on_process(proc)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=request.timeout_seconds + 15,
            )
        except asyncio.TimeoutError:
            await terminate_process_tree(proc)
            elapsed = time.monotonic() - started
            return RunResult(
                status="timed_out",
                session_id=request.session_id,
                model=resolved_model,
                model_notice=model_notice,
                duration_seconds=elapsed,
                error=f"Neon subagent timed out after {request.timeout_seconds}s.",
            )
        except asyncio.CancelledError:
            await terminate_process_tree(proc)
            raise

        elapsed = time.monotonic() - started
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        stderr_tail = _tail(stderr_text)
        payload = parse_agy_json_output(stdout_text)

        if payload is not None:
            conv_id = payload.get("conversation_id") or request.session_id
            if isinstance(conv_id, str) and conv_id and on_session:
                on_session(conv_id)

            raw_status = str(payload.get("status", "")).upper()
            response_text = str(payload.get("response", "")).strip()
            raw_usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
            usage = normalize_usage("neon", raw_usage)
            reported_duration = payload.get("duration_seconds")
            duration = (
                float(reported_duration)
                if isinstance(reported_duration, (int, float))
                else elapsed
            )

            if proc.returncode == 0 and raw_status in {"", "SUCCESS", "OK"}:
                return RunResult(
                    status="completed",
                    response=response_text,
                    session_id=conv_id if isinstance(conv_id, str) else None,
                    model=resolved_model,
                    model_notice=model_notice,
                    duration_seconds=duration,
                    usage=usage,
                    exit_code=proc.returncode,
                    stderr_tail=stderr_tail,
                )

            err_msg = (
                payload.get("error")
                or response_text
                or stderr_tail
                or f"agy exited with status {raw_status or proc.returncode}"
            )
            return RunResult(
                status="failed",
                response=response_text,
                session_id=conv_id if isinstance(conv_id, str) else None,
                model=resolved_model,
                model_notice=model_notice,
                duration_seconds=duration,
                usage=usage,
                exit_code=proc.returncode,
                error=str(err_msg),
                stderr_tail=stderr_tail,
            )

        if proc.returncode == 0:
            return RunResult(
                status="completed",
                response=stdout_text.strip(),
                session_id=request.session_id,
                model=resolved_model,
                model_notice=model_notice,
                duration_seconds=elapsed,
                exit_code=0,
                stderr_tail=stderr_tail,
            )

        return RunResult(
            status="failed",
            response=stdout_text.strip(),
            session_id=request.session_id,
            model=resolved_model,
            model_notice=model_notice,
            duration_seconds=elapsed,
            exit_code=proc.returncode,
            error=stderr_tail or stdout_text.strip() or f"agy exited with code {proc.returncode}",
            stderr_tail=stderr_tail,
        )

    async def run(
        self,
        request: RunRequest,
        *,
        on_process: OnProcessCallback | None = None,
        on_session: OnSessionCallback | None = None,
        on_activity: OnActivityCallback | None = None,
    ) -> RunResult:
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

            resolution = await self.catalog.resolve(
                request.model,
                default_intent=self._settings.default_neon_model,
            )
            result = await self._run_once(
                request,
                resolved_model=resolution.resolved,
                model_notice=resolution.notice,
                prompt_text=prompt_text,
                extra_dirs=extra_dirs,
                on_process=on_process,
                on_session=on_session,
                on_activity=on_activity,
            )

            # Seamless self-healing retry if the selected model was rejected/retired.
            if (
                result.status == "failed"
                and resolution.resolved is not None
                and is_model_unavailable_error(f"{result.error or ''}\n{result.stderr_tail or ''}")
            ):
                logger.info(
                    "agy rejected model %r; refreshing catalog and retrying seamlessly.",
                    resolution.resolved,
                )
                refreshed = await self.catalog.resolve(
                    request.model,
                    default_intent=self._settings.default_neon_model,
                    force_refresh=True,
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
                    on_process=on_process,
                    on_session=on_session,
                    on_activity=on_activity,
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

    async def get_version(self) -> str | None:
        try:
            cmd = [*self.resolve_command(), "--version"]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            text = (stdout or stderr).decode("utf-8", errors="replace").strip()
            return text.splitlines()[0] if text else None
        except Exception:
            return None

    def _effective_model(self, requested: str | None) -> str | None:
        raw = (requested or "").strip()
        if raw.lower() in DEFAULT_INTENT_TOKENS:
            return self._settings.default_codex_model
        return raw

    def build_args(
        self,
        request: RunRequest,
        *,
        effective_model: str | None,
    ) -> tuple[list[str], bool]:
        """Build Codex argv and return `(argv, use_stdin_prompt)`."""
        base = self.resolve_command()
        sandbox = "read-only" if request.mode == "read_only" else "workspace-write"

        if request.review_scope is not None and not request.prompt.strip():
            args = [*base, "exec", "review", "--json", "--skip-git-repo-check"]
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
        model_notice: str | None,
        on_process: OnProcessCallback | None,
        on_session: OnSessionCallback | None,
        on_activity: OnActivityCallback | None,
    ) -> RunResult:
        started = time.monotonic()
        cmd, use_stdin_prompt = self.build_args(request, effective_model=effective_model)
        if on_activity:
            on_activity(f"running codex ({effective_model or 'codex-default'})")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=request.cwd,
            stdin=asyncio.subprocess.PIPE if use_stdin_prompt else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_subprocess_creation_kwargs(),
        )
        if on_process:
            on_process(proc)

        if use_stdin_prompt and proc.stdin is not None:
            prompt_bytes = request.prompt.encode("utf-8")
        else:
            prompt_bytes = None

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
            # Stream in chunks and split manually so JSONL events >64 KiB never hit StreamReader limits.
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

        try:
            await asyncio.wait_for(
                asyncio.gather(_write_stdin(), _read_stdout(), _read_stderr(), proc.wait()),
                timeout=request.timeout_seconds,
            )
        except asyncio.TimeoutError:
            await terminate_process_tree(proc)
            elapsed = time.monotonic() - started
            parsed = parse_codex_jsonl_events(stdout_lines)
            return RunResult(
                status="timed_out",
                response=parsed["response"],
                session_id=parsed["session_id"] or request.session_id,
                model=effective_model or "codex-default",
                model_notice=model_notice,
                duration_seconds=elapsed,
                usage=normalize_usage("codex", parsed["usage"]),
                error=f"Codex subagent timed out after {request.timeout_seconds}s.",
                stderr_tail=_tail(b"".join(stderr_chunks).decode("utf-8", errors="replace")),
            )
        except asyncio.CancelledError:
            await terminate_process_tree(proc)
            raise

        elapsed = time.monotonic() - started
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        stderr_tail = _tail(stderr_text)
        parsed = parse_codex_jsonl_events(stdout_lines)
        session_id = parsed["session_id"] or request.session_id
        response = parsed["response"]
        usage = normalize_usage("codex", parsed["usage"])
        errors = parsed["errors"]

        if proc.returncode == 0 and not errors:
            if not response:
                non_json = "\n".join(
                    ln.strip() for ln in stdout_lines if ln.strip() and not ln.strip().startswith("{")
                ).strip()
                response = non_json
            return RunResult(
                status="completed",
                response=response,
                session_id=session_id,
                model=effective_model or "codex-default",
                model_notice=model_notice,
                duration_seconds=elapsed,
                usage=usage,
                exit_code=0,
                stderr_tail=stderr_tail,
            )

        err_msg = "; ".join(errors) if errors else (
            stderr_tail or f"codex exited with code {proc.returncode}"
        )
        return RunResult(
            status="failed",
            response=response,
            session_id=session_id,
            model=effective_model or "codex-default",
            model_notice=model_notice,
            duration_seconds=elapsed,
            usage=usage,
            exit_code=proc.returncode,
            error=err_msg,
            stderr_tail=stderr_tail,
        )

    async def run(
        self,
        request: RunRequest,
        *,
        on_process: OnProcessCallback | None = None,
        on_session: OnSessionCallback | None = None,
        on_activity: OnActivityCallback | None = None,
    ) -> RunResult:
        effective_model = self._effective_model(request.model)
        result = await self._run_once(
            request,
            effective_model=effective_model,
            model_notice=None,
            on_process=on_process,
            on_session=on_session,
            on_activity=on_activity,
        )

        # Seamless fallback if an explicit/pinned Codex model is retired or unsupported.
        if (
            result.status == "failed"
            and effective_model is not None
            and is_model_unavailable_error(f"{result.error or ''}\n{result.stderr_tail or ''}")
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
                model_notice=notice,
                on_process=on_process,
                on_session=on_session,
                on_activity=on_activity,
            )

        return result
