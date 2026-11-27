"""Antigravity (`agy` / backend `neon`) adapter, subprocess runner, usage mapping, and quota reader."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
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
    from agent_bridge.config import Settings
    from agent_bridge.models import JobStatus, ProviderState, RunRequest, RunResult
    from agent_bridge.runners import (
        OnActivityCallback,
        OnAttemptCallback,
        OnModelCallback,
        OnProviderStartCallback,
        OnSessionCallback,
    )

logger = logging.getLogger("agent_bridge.backends.agy")

AGY_MAX_INLINE_PROMPT_CHARS = 20_000
AGY_READ_ONLY_GUARD = (
    "This is a read-only run. Do not create, edit, move or delete any file, and do not run any command "
    "that changes anything. Put your whole answer in your final reply, and end it with one line saying "
    "whether you changed any file.\n\n"
)
AGY_REMOTE_TURN_CANCEL_ERROR = (
    "Job cancelled. agy had reported that its turn may still be running at the server; stopping the local "
    "CLI does not stop that turn."
)
_AGY_THIRD_PARTY_MODEL_RE = re.compile(
    r"(?:^|-)(claude|opus|sonnet|haiku|gpt|oss)(?:-|$)",
    re.I,
)
_AGY_POOLS: frozenset[str] = frozenset({"agy_gemini", "agy_claude_gpt"})
_AGY_TIMEOUT_PATTERNS = (
    re.compile(r"\bprint\s+timeout\b", re.I),
    re.compile(r"\bstill\s+in\s+progress\b", re.I),
    re.compile(r"\bturn\s+timed\s+out\b", re.I),
)


def _agy_remote_turn_cancel_guidance(job_id: str) -> str:
    return (
        "The session stays blocked, as after a timeout with the same signal. Confirm that this turn has "
        "ended (and inspect git status/diff for edits), then resume with "
        f"confirm_prior_job_id={job_id!r}."
    )


def agy_reports_turn_still_running(exit_code: int | None, text: str) -> bool:
    """Whether `agy` said its turn may still be going on at the server: exit code 3, or words to that effect.

    Stopping the local CLI does not stop that turn, so a job that saw this keeps `possibly_running`.
    """
    return exit_code == 3 or any(p.search(text) for p in _AGY_TIMEOUT_PATTERNS)


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


def sum_agy_step_usages(step_usages: list[dict[str, Any]]) -> dict[str, int] | None:
    """Sum per-step `usage` objects from a single `agy --output-format stream-json` turn."""
    valid = [u for u in step_usages if isinstance(u, dict) and u]
    if not valid:
        return None
    keys = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
    totals = {k: sum(to_int(u, k) for u in valid) for k in keys}
    if totals["total_tokens"] == 0 and (totals["input_tokens"] or totals["output_tokens"]):
        totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    return totals


def compute_agy_usage_delta(
    current_cumulative: dict[str, Any] | None,
    previous_cumulative: dict[str, Any] | None,
) -> dict[str, int] | None:
    """Compute a verified monotonic per-turn delta between cumulative `agy` envelopes."""
    if not isinstance(current_cumulative, dict) or not isinstance(previous_cumulative, dict):
        return None
    keys = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
    delta: dict[str, int] = {}
    for k in keys:
        cur = to_int(current_cumulative, k)
        prev = to_int(previous_cumulative, k)
        if cur < prev:
            return None
        delta[k] = cur - prev
    return delta


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


def normalize_agy_usage(
    raw_usage: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Normalize `agy` token counters while preserving provider-specific metrics."""
    if not raw_usage or not isinstance(raw_usage, dict):
        return None, None
    input_tokens = to_int(raw_usage, "input_tokens")
    output_tokens = to_int(raw_usage, "output_tokens")
    cache_read = to_int(raw_usage, "cache_read_tokens") or to_int(raw_usage, "cached_tokens")
    thinking = to_int(raw_usage, "thinking_tokens") or to_int(raw_usage, "reasoning_tokens")
    total = to_int(raw_usage, "total_tokens") or (input_tokens + output_tokens)
    normalized: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cache_read,
        "reasoning_tokens": thinking,
        "total_tokens": total,
    }
    provider_metrics: dict[str, Any] = {
        "provider": "antigravity",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "thinking_tokens": thinking,
        "total_tokens": total,
    }
    return normalized, provider_metrics


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


def _infer_agy_group_pool(group_name: str, buckets: list[dict[str, Any]]) -> str | None:
    low = group_name.lower()
    if any(tok in low for tok in ("claude", "gpt", "3p", "third")):
        return "agy_claude_gpt"
    for b in buckets:
        if not isinstance(b, dict):
            continue
        bid = str(b.get("id") or "").lower()
        if bid.startswith("3p-"):
            return "agy_claude_gpt"
    if "gemini" in low:
        return "agy_gemini"
    for b in buckets:
        if not isinstance(b, dict):
            continue
        bid = str(b.get("id") or "").lower()
        if bid == "gemini" or bid.startswith("gemini-") or bid.startswith("1p-"):
            return "agy_gemini"
    return None


def _extract_models_from_description(description: str | None) -> list[str]:
    if not description or not isinstance(description, str):
        return []
    m = re.search(r"models\s+within\s+this\s+group\s*:\s*(.+)$", description.strip(), re.I)
    if not m:
        return []
    return [part.strip() for part in m.group(1).split(",") if part.strip()]


def parse_neon_usage_envelope(
    payload: dict[str, Any],
    *,
    fetched_at: str | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Parse `agy --output-format json --print=/usage` envelope (structured JSON + text fallback)."""
    from agent_bridge.quota import _as_number, _iso_now, enrich_quota_reading

    ts = fetched_at or _iso_now()
    groups_out: list[dict[str, Any]] = []

    cmd_data = payload.get("command", {}).get("data") if isinstance(payload.get("command"), dict) else None
    raw_groups = cmd_data.get("groups") if isinstance(cmd_data, dict) else None

    if isinstance(raw_groups, list):
        for g in raw_groups:
            if not isinstance(g, dict):
                continue
            windows: list[dict[str, Any]] = []
            for b in g.get("buckets") or []:
                if not isinstance(b, dict):
                    continue
                rem_frac = _as_number(b.get("remaining_fraction"))
                if rem_frac is None or rem_frac < 0.0 or rem_frac > 1.0:
                    continue
                rem_pct = round(rem_frac * 100.0, 1)
                used_pct = round(max(0.0, 100.0 - rem_pct), 1)
                reset = b.get("reset_time")
                if reset == "1970-01-01T00:00:00Z":
                    reset = None
                windows.append(
                    {
                        "id": b.get("id"),
                        "window": b.get("window") or b.get("name"),
                        "used_percent": used_pct,
                        "remaining_percent": rem_pct,
                        "reset_time": reset,
                    }
                )
            if windows:
                gname = str(g.get("name") or "unknown")
                group_entry: dict[str, Any] = {
                    "name": gname,
                    "pool": _infer_agy_group_pool(gname, windows),
                    "windows": windows,
                }
                models_list = _extract_models_from_description(g.get("description"))
                if models_list:
                    group_entry["models"] = models_list
                groups_out.append(group_entry)

    # Fallback: parse tab-separated response text if command.data had no usable windows.
    if not groups_out and isinstance(payload.get("response"), str):
        by_group: dict[str, list[dict[str, Any]]] = {}
        for line in payload["response"].splitlines():
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            if len(parts) < 3:
                continue
            gname, wname, pct_raw = parts[0], parts[1], parts[2]
            reset = parts[3] if len(parts) >= 4 else None
            if reset == "1970-01-01T00:00:00Z":
                reset = None
            m = re.search(r"(\d+(?:\.\d+)?)%", pct_raw)
            if not m:
                continue
            rem_num = float(m.group(1))
            if rem_num < 0.0 or rem_num > 100.0:
                continue
            rem_pct = round(rem_num, 1)
            used_pct = round(max(0.0, 100.0 - rem_pct), 1)
            by_group.setdefault(gname, []).append(
                {
                    "window": wname,
                    "used_percent": used_pct,
                    "remaining_percent": rem_pct,
                    "reset_time": reset,
                }
            )
        groups_out = [
            {"name": k, "pool": _infer_agy_group_pool(k, v), "windows": v}
            for k, v in by_group.items()
            if v
        ]

    if not groups_out or not any(g.get("windows") for g in groups_out):
        return {
            "backend": "neon",
            "status": "unknown",
            "fetched_at": ts,
            "error": "Could not parse usable quota windows from agy /usage output.",
        }

    return enrich_quota_reading(
        {
            "backend": "neon",
            "status": "ok",
            "fetched_at": ts,
            "groups": groups_out,
        },
        now_utc=now_utc,
    )


async def fetch_neon_quota(agy_cmd: list[str], *, timeout_seconds: float = 15.0) -> dict[str, Any]:
    """Perform a short-lived one-shot `agy --output-format json --print=/usage` probe."""
    import asyncio

    from agent_bridge import process_control
    from agent_bridge.quota import _iso_now

    ts = _iso_now()
    managed = None
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    try:
        proc, managed = await process_control.spawn_managed_process(
            *agy_cmd,
            "--mode",
            "plan",
            "--output-format",
            "json",
            "--print-timeout",
            f"{max(1, int(timeout_seconds))}s",
            "--print=/usage",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=max(0.05, deadline - time.monotonic()),
        )
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise asyncio.TimeoutError("Timed out launching agy /usage probe.")
        stdout_b, stderr_b, _ = await process_control.communicate_managed(proc, managed, timeout=rem)

        stdout_text = stdout_b.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or not stdout_text:
            err = stderr_b.decode("utf-8", errors="replace").strip() or f"agy /usage exited {proc.returncode}"
            return {
                "backend": "neon",
                "status": "unknown",
                "fetched_at": ts,
                "error": err[:400],
            }

        envelope: dict[str, Any] | None = None
        try:
            parsed = json.loads(stdout_text)
            if isinstance(parsed, dict):
                envelope = parsed
        except json.JSONDecodeError:
            for line in reversed(stdout_text.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        parsed = json.loads(line)
                        if isinstance(parsed, dict):
                            envelope = parsed
                            break
                    except json.JSONDecodeError:
                        continue

        if envelope is None:
            return {
                "backend": "neon",
                "status": "unknown",
                "fetched_at": ts,
                "error": "Non-JSON response from agy /usage.",
            }

        return parse_neon_usage_envelope(envelope, fetched_at=ts)
    except TimeoutError as exc:
        msg = str(exc).strip() or f"Timed out reading agy /usage after {timeout_seconds:g}s."
        return {
            "backend": "neon",
            "status": "unknown",
            "fetched_at": ts,
            "error": msg,
        }
    except Exception as exc:
        return {
            "backend": "neon",
            "status": "unknown",
            "fetched_at": ts,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if managed is not None:
            await managed.terminate()


def build_agy_args(
    request: RunRequest,
    *,
    base: list[str],
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
        *base,
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


class NeonRunner:
    """Executes subagent turns via Antigravity CLI (`agy --output-format stream-json --print=...`)."""

    def __init__(self, settings: Settings) -> None:
        from agent_bridge.model_resolver import NeonModelCatalog

        self._settings = settings
        self.catalog = NeonModelCatalog(
            fetcher=self._fetch_live_models,
            cache_path=settings.state_dir / "agy_models_cache.json",
            ttl_seconds=settings.models_cache_ttl_seconds,
        )

    def resolve_command(self) -> list[str]:
        return resolve_agy_command(self._settings.agy_bin)

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
        return {
            "command_found": True,
            "healthy": healthy,
            "available": healthy,
            "command": cmd,
            "version": version,
            "error": err,
        }

    async def _fetch_live_models(self) -> list[dict[str, str]]:
        import asyncio

        from agent_bridge.process_control import communicate_managed, spawn_managed_process

        cmd = [*self.resolve_command(), "models"]
        deadline = time.monotonic() + 20.0
        managed = None
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
        return build_agy_args(
            request,
            base=self.resolve_command(),
            resolved_model=resolved_model,
            prompt_text=prompt_text,
            extra_dirs=extra_dirs,
            remaining_seconds=remaining_seconds,
        )

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
        remaining = deadline - started
        if on_model:
            on_model(resolved_model, "requested" if resolved_model else "default")
        if remaining <= 0.05:
            status, reason, pstate, guidance = runners.classify_failure(
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
        await runners._maybe_await(on_provider_start)

        env = runners.job_env(self._settings, request, cli="agy")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessStartupTimeout(
                    "Job deadline expired before execution started.", process_stopped=True,
                )
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
            status, reason, pstate, guidance = runners.classify_failure(
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
        stderr_tail = runners._tail(stderr_text)
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
            usage, provider_metrics = normalize_agy_usage(step_sum)
            usage_status = "exact" if turn_completed_cleanly else "partial"
        elif env_raw_usage is not None:
            is_first_turn = (not request.session_id) and (num_turns in {None, 1})
            if is_first_turn:
                usage, provider_metrics = normalize_agy_usage(env_raw_usage)
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
                    usage, provider_metrics = normalize_agy_usage(delta)
                    usage_status = "delta"
                else:
                    _, provider_metrics = normalize_agy_usage(env_raw_usage)
                    if provider_metrics is not None:
                        provider_metrics["cumulative_only"] = True
                    usage_status = "partial"
            else:
                # Resumed turn without verified adjacent watermark or interrupted turn: never double-count!
                _, provider_metrics = normalize_agy_usage(env_raw_usage)
                if provider_metrics is not None:
                    provider_metrics["cumulative_only"] = True
                usage_status = "partial"

        if cancelled:
            # The timeout path honours agy's word that its turn goes on at the server; so does a cancel.
            env_error = str(envelope.get("error") or "") if isinstance(envelope, dict) else ""
            remote_running = agy_reports_turn_still_running(proc.returncode, f"{env_error}\n{stderr_text}")
            raise RunCancelled(
                RunResult(
                    status="cancelled",
                    session_id=session_id,
                    usage=usage,
                    provider_metrics=provider_metrics,
                    usage_status=usage_status,
                    provider_state="possibly_running" if remote_running else None,
                )
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
                any(p.search(stderr_text) for p in runners._INTERACTIVE_FALLBACK_PATTERNS)
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
            status, reason, pstate, guidance = runners.classify_failure(
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
                response=env_response or partial_text or runners._plain_stdout_reply(stdout_lines),
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
                error=runners.CLI_OUTPUT_CUT_SHORT_ERROR.format(event="a result event with a status"),
                failure_reason="cli_error",
                provider_state="stopped" if process_stopped else "possibly_running",
                retry_guidance=runners.CLI_OUTPUT_CUT_SHORT_GUIDANCE,
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
            status, reason, pstate, guidance = runners.classify_failure(
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
        status, reason, pstate, guidance = runners.classify_failure(
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
        import asyncio

        from agent_bridge import runners
        from agent_bridge.model_resolver import ModelPoolUnavailableError
        from agent_bridge.models import RunResult

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
                status, reason, pstate, guidance = runners.classify_failure(
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
                and runners._can_auto_retry_attempt(
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
                requested_pool = classify_agy_quota_pool(resolution.resolved or request.model)
                different_model = (
                    refreshed.resolved
                    if (refreshed.resolved is not None and refreshed.resolved != resolution.resolved)
                    else None
                )
                if different_model is not None and not request.allow_cross_pool_fallback:
                    if classify_agy_quota_pool(different_model) != requested_pool:
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
                await runners._maybe_await(on_attempt, runners._make_attempt_summary(1, result, backend="neon"))
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
                return runners._merge_retry_results(result, second, backend="neon")

            # One bounded retry for transient service errors on NEW read-only tasks only.
            if (
                request.mode == "read_only"
                and result.status == "failed"
                and result.failure_reason == "transient_service"
                and runners._can_auto_retry_attempt(
                    request,
                    result,
                    kind="transient",
                    prior_watermark=prior_watermark,
                )
                and (overall_deadline - time.monotonic()) > runners.TRANSIENT_RETRY_MIN_REMAINING_SECONDS
            ):
                # Record Attempt 1 BEFORE retry backoff sleep so cancelling during backoff keeps Attempt 1 usage!
                await runners._maybe_await(on_attempt, runners._make_attempt_summary(1, result, backend="neon"))
                if on_activity:
                    on_activity("transient service error; retrying once")
                await asyncio.sleep(runners.TRANSIENT_RETRY_BACKOFF_SECONDS)
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
                return runners._merge_retry_results(result, second, backend="neon")

            return result
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)


def classify_agy_quota_pool(model: str | None = None) -> str:
    if model and _AGY_THIRD_PARTY_MODEL_RE.search(model.strip()):
        return "agy_claude_gpt"
    return "agy_gemini"


class AgyAdapter(BackendAdapter):
    """Adapter for the Antigravity CLI (`agy`, backend name `neon`)."""

    name = "neon"
    cli_name = "agy"
    aliases = (
        "agy",
        "gemini",
        "antigravity",
        "agy-claude",
        "agy_claude",
        "agy-sonnet",
        "agy_sonnet",
        "agy-opus",
        "agy_opus",
        "agy-gpt",
        "agy_gpt",
    )
    alias_model_hints = {
        "agy-claude": "claude-sonnet-high",
        "agy_claude": "claude-sonnet-high",
        "agy-sonnet": "claude-sonnet-high",
        "agy_sonnet": "claude-sonnet-high",
        "agy-opus": "claude-opus-high",
        "agy_opus": "claude-opus-high",
        "agy-gpt": "gpt-oss",
        "agy_gpt": "gpt-oss",
    }
    dashboard_order = 20
    missing_terminal_event_name = "a result event with a status"
    capabilities = Capabilities(
        resume=True,
        worktree=False,
        builtin_review=False,
        model_catalog=True,
        fast_tier=False,
        extra_dirs=True,
        extra_dirs_on_resume=True,
        reports_observed_model=True,
        model_encodes_effort=True,
        cumulative_session_usage=True,
        tracks_native_subagents=False,
    )
    quota_pools = (
        QuotaPoolSpec(
            pool="agy_gemini",
            backend="neon",
            label="Antigravity Gemini",
            dashboard_label="Antigravity Gemini",
            source_label="Antigravity",
            quota_display_order=20,
        ),
        QuotaPoolSpec(
            pool="agy_claude_gpt",
            backend="neon",
            label="Antigravity Claude+GPT",
            dashboard_label="Antigravity Claude+GPT",
            source_label="Antigravity",
            quota_display_order=30,
        ),
    )

    @property
    def default_model_label(self) -> str:
        return "agy's default model"

    def classify_quota_pool(self, model: str | None = None) -> str:
        return classify_agy_quota_pool(model)

    def defaults(self, settings: Settings) -> BackendDefaults:
        return BackendDefaults(
            model=settings.default_neon_model,
            effort=None,
            timeout_seconds=settings.default_timeout_seconds,
        )

    def resolve_command(self, settings: Settings) -> list[str]:
        return resolve_agy_command(settings.agy_bin)

    def build_command(
        self,
        request: RunRequest,
        *,
        cmd: list[str],
        model: str | None = None,
        effort: str | None = None,
        **kwargs: Any,
    ) -> CommandSpec:
        _ = effort
        prompt_text = kwargs.get("prompt_text", request.prompt)
        extra_dirs = kwargs.get("extra_dirs", list(request.extra_dirs))
        remaining_seconds = float(kwargs.get("remaining_seconds", request.timeout_seconds))
        argv = build_agy_args(
            request,
            base=cmd,
            resolved_model=model,
            prompt_text=prompt_text,
            extra_dirs=extra_dirs,
            remaining_seconds=remaining_seconds,
        )
        return CommandSpec(argv=argv, stdin_text=None)

    def parse_stream(
        self,
        lines: list[str],
        *,
        request: RunRequest | None = None,
    ) -> ParsedStream:
        _ = request
        session_id: str | None = None
        model: str | None = None
        partial_parts: list[str] = []
        step_usages: dict[int, dict[str, Any]] = {}
        result_envelope: dict[str, Any] | None = None
        plain_envelope: dict[str, Any] | None = None

        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            ev_type = obj.get("event")
            if ev_type == "init":
                cid = obj.get("conversation_id")
                if isinstance(cid, str) and cid:
                    session_id = cid
                init_obj = obj.get("init")
                if isinstance(init_obj, dict) and isinstance(init_obj.get("model"), str) and init_obj["model"]:
                    model = init_obj["model"]
            elif ev_type == "step_update":
                su = obj.get("step_update")
                if isinstance(su, dict):
                    if isinstance(su.get("conversation_id"), str) and su["conversation_id"] and not session_id:
                        session_id = su["conversation_id"]
                    if isinstance(su.get("text_delta"), str) and su["text_delta"]:
                        partial_parts.append(su["text_delta"])
                    if su.get("state") == "DONE" and isinstance(su.get("usage"), dict):
                        idx = su.get("step_index")
                        key = int(idx) if isinstance(idx, int) else len(step_usages)
                        step_usages[key] = su["usage"]
            else:
                env = _agy_result_envelope(obj)
                if env is not None:
                    if ev_type == "result":
                        result_envelope = env
                    else:
                        plain_envelope = env
                    cid = env.get("conversation_id")
                    if isinstance(cid, str) and cid:
                        session_id = cid

        envelope = result_envelope or plain_envelope or parse_agy_json_output("".join(lines))
        raw_status = str(envelope.get("status") or "").strip().upper() if isinstance(envelope, dict) else ""
        partial_text = "".join(partial_parts).strip()
        response = (
            str(envelope.get("response", "")).strip() or partial_text
            if isinstance(envelope, dict)
            else partial_text
        )
        usage = sum_agy_step_usages(list(step_usages.values()))
        if usage is None and isinstance(envelope, dict) and _has_valid_agy_token_counters(envelope.get("usage")):
            usage = envelope.get("usage")
        is_error = bool(raw_status and raw_status not in {"SUCCESS", "OK"})
        err_val = envelope.get("error") if isinstance(envelope, dict) else None
        errors = (str(err_val),) if err_val else ()
        return ParsedStream(
            session_id=session_id,
            model=model,
            response=response,
            usage=usage,
            terminal_event_seen=bool(raw_status),
            is_error=is_error,
            errors=errors,
            extra={"envelope": envelope, "raw_status": raw_status},
        )

    def normalize_usage(
        self,
        raw_usage: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return normalize_agy_usage(raw_usage)

    async def probe_backend(
        self,
        runner: Any,
        settings: Settings,
        *,
        refresh_models: bool = False,
    ) -> dict[str, Any]:
        from agent_bridge.model_resolver import ModelPoolUnavailableError

        try:
            neon_health = await runner.check_health()
        except Exception as exc:
            neon_health = unhealthy_payload(exc)
        info: dict[str, Any] = {
            "backend": "neon",
            "cli": "agy",
            "default_intent": settings.default_neon_model,
            **neon_health,
        }
        catalog = runner.catalog
        if neon_health.get("healthy"):
            try:
                models = await runner.list_models(refresh=refresh_models)
            except Exception as exc:
                models = []
                info["catalog_error"] = f"{type(exc).__name__}: {exc}"
            info.update(catalog.status_info())
            info["models"] = models
            info["quota_pools"] = catalog.group_models_by_quota_pool()
            info["resolved_shortcuts"] = catalog.summarize_latest_aliases()
            try:
                default_res = await catalog.resolve(None, default_intent=settings.default_neon_model)
                info["resolved_default_model"] = default_res.resolved
            except ModelPoolUnavailableError as exc:
                info["resolved_default_model"] = None
                info["catalog_error"] = str(exc)
                info["available"] = False
            except Exception as exc:
                info["resolved_default_model"] = None
                info["catalog_error"] = f"{type(exc).__name__}: {exc}"
                info["available"] = False
        else:
            info.update(catalog.status_info())
        return info

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
            return {"backend": "neon", "status": "unknown", "error": str(exc)}
        res = await fetch_neon_quota(cmd)
        res = enrich_quota_reading(res)
        res["cached"] = False
        if save_to_cache and res.get("status") == "ok":
            try:
                await db_call(store.set_cached_quota, self.name, res)
            except Exception:
                pass
        return res

    def extract_quota_windows(
        self,
        reading: dict[str, Any],
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        if not isinstance(reading, dict) or reading.get("status") != "ok":
            return {}, []
        by_pool: dict[str, list[dict[str, Any]]] = {}
        unrecognised: list[str] = []
        for group in reading.get("groups") or []:
            if not isinstance(group, dict):
                continue
            gname = str(group.get("name") or "unnamed")
            gwins = [w for w in group.get("windows") or [] if isinstance(w, dict)]
            explicit_pool = group.get("pool")
            if explicit_pool in _AGY_POOLS:
                resolved_pool: str | None = str(explicit_pool)
            elif explicit_pool is None or explicit_pool == "":
                resolved_pool = _infer_agy_group_pool(gname, gwins)
            else:
                resolved_pool = None
            if resolved_pool in _AGY_POOLS:
                by_pool.setdefault(resolved_pool, []).extend(gwins)
            else:
                unrecognised.append(gname)
        return by_pool, unrecognised

    def format_unrecognised_quota_group(self, group_name: str) -> tuple[str, str, str]:
        return "agy_unknown", f"unrecognised Antigravity group: {group_name}", f"agy_unknown ({group_name})"

    def enrich_quota_reading(
        self,
        reading: dict[str, Any],
        enrich_window: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        if isinstance(reading.get("groups"), list):
            for g in reading["groups"]:
                if not isinstance(g, dict):
                    continue
                wins = g.get("windows") or []
                g["pool"] = g.get("pool") or _infer_agy_group_pool(str(g.get("name") or ""), wins)
                g["windows"] = [enrich_window(w) for w in wins]
        return reading

    def advisory_pools(
        self,
        reading: dict[str, Any],
        *,
        neon_pool_models: dict[str, list[str]] | None = None,
        default_label: str | None = None,
    ) -> list[dict[str, Any]]:
        _ = default_label
        if not isinstance(reading, dict) or reading.get("status") != "ok":
            return []
        agy_grouped: dict[str, dict[str, Any]] = {}
        for group in reading.get("groups") or []:
            if not isinstance(group, dict):
                continue
            pool_name = group.get("pool")
            if pool_name not in _AGY_POOLS:
                continue
            bucket = agy_grouped.setdefault(
                str(pool_name),
                {
                    "label": str(group.get("name") or pool_name),
                    "windows": [],
                    "models": list((neon_pool_models or {}).get(str(pool_name)) or []),
                },
            )
            bucket["windows"].extend(w for w in group.get("windows") or [] if isinstance(w, dict))
            if not bucket["models"] and group.get("models"):
                bucket["models"] = list(group["models"])
        return [
            {
                "pool": pool_name,
                "backend": "neon",
                "label": bucket["label"],
                "windows": bucket["windows"],
                "models": bucket["models"],
                "rate_limit_reached": False,
                "shared_with_own_use": False,
            }
            for pool_name, bucket in agy_grouped.items()
        ]

    def classify_failure(
        self,
        *,
        exit_code: int | None,
        error_text: str | None,
        stderr_text: str | None,
        timed_out_locally: bool = False,
        process_stopped: bool = True,
    ) -> tuple[JobStatus, str, ProviderState, str] | None:
        _ = (timed_out_locally, process_stopped)
        combined = f"{error_text or ''}\n{stderr_text or ''}"
        if agy_reports_turn_still_running(exit_code, combined):
            return (
                "timed_out",
                "provider_timeout_still_running",
                "possibly_running",
                "Antigravity reported that the server-side turn may still be in progress (even after local "
                "process cleanup). Keep any partial response and session_id; confirm the prior turn finished "
                "(and inspect git diff for edits) before resuming.",
            )
        return None

    def remote_turn_cancel_error(self) -> str:
        return AGY_REMOTE_TURN_CANCEL_ERROR

    def remote_turn_cancel_guidance(self, job_id: str) -> str:
        return _agy_remote_turn_cancel_guidance(job_id)

    def create_runner(self, settings: Settings) -> NeonRunner:
        return NeonRunner(settings)
