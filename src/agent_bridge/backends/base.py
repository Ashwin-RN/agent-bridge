"""Backend adapter interface, capability descriptors, quota pool metadata, and generic runner."""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_bridge.config import EffortLevel, Settings
    from agent_bridge.models import JobStatus, ProviderState, RunRequest, RunResult
    from agent_bridge.runners import (
        OnActivityCallback,
        OnAttemptCallback,
        OnModelCallback,
        OnProviderStartCallback,
        OnSessionCallback,
    )

logger = logging.getLogger("agent_bridge.backends.base")


def to_int(raw: Mapping[str, Any], key: str) -> int:
    val = raw.get(key)
    return int(val) if isinstance(val, (int, float)) and not isinstance(val, bool) else 0


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Feature and behavioural flags a backend adapter advertises to core."""

    resume: bool = True
    worktree: bool = False
    builtin_review: bool = False
    model_catalog: bool = False
    fast_tier: bool = False
    extra_dirs: bool = True
    extra_dirs_on_resume: bool = True
    # Behavioural flags that keep core free of backend-name branches.
    reports_observed_model: bool = True
    model_encodes_effort: bool = False
    cumulative_session_usage: bool = False
    tracks_native_subagents: bool = False


@dataclass(frozen=True, slots=True)
class QuotaPoolSpec:
    """One subscription quota pool that a backend draws on and reports in quota/dashboard views."""

    pool: str
    backend: str
    label: str
    dashboard_label: str
    shared_with_own_use: bool = False
    source_label: str | None = None
    no_reading_reason: str | None = None
    quota_view_no_reading: str | None = None
    shared_note: str | None = None
    shared_note_suffix: str = "shared with your own sessions"
    shared_advisory_note: str | None = None
    default_source: str | None = None
    advisory_models_fallback: tuple[str, ...] = ()
    quota_display_order: int = 100


@dataclass(frozen=True, slots=True)
class BackendDefaults:
    """Default model, effort, and optional timeout for a backend."""

    model: str | None = None
    effort: EffortLevel | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Subprocess invocation produced by an adapter for one turn."""

    argv: list[str]
    stdin_text: str | None = None
    env: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ParsedStream:
    """Reduced output from a backend CLI's stdout stream."""

    session_id: str | None = None
    model: str | None = None
    response: str = ""
    usage: dict[str, Any] | None = None
    terminal_event_seen: bool = False
    is_error: bool = False
    errors: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


def unhealthy_payload(exc: Exception) -> dict[str, Any]:
    return {
        "command_found": False,
        "healthy": False,
        "available": False,
        "command": None,
        "version": None,
        "error": f"{type(exc).__name__}: {exc}",
    }


class BackendAdapter(ABC):
    """Single source of truth for everything specific to one CLI backend."""

    name: str
    cli_name: str
    aliases: tuple[str, ...] = ()
    alias_model_hints: Mapping[str, str] = {}
    capabilities: Capabilities = Capabilities()
    quota_pools: tuple[QuotaPoolSpec, ...] = ()
    dashboard_order: int = 100
    missing_terminal_event_name: str = "a terminal event"

    @property
    def default_model_label(self) -> str:
        return f"{self.name}'s default model"

    # --- Aliases and quota pools ---

    def all_alias_keys(self) -> tuple[str, ...]:
        """Every lowercase name/alias accepted for this backend, canonical name first."""
        keys: list[str] = [self.name.lower()]
        for alias in self.aliases:
            low = alias.strip().lower()
            if low and low not in keys:
                keys.append(low)
        return tuple(keys)

    def resolve_alias_model_hint(self, alias: str, model: str | None) -> str | None:
        if model and model.strip():
            return model.strip()
        return self.alias_model_hints.get(alias.strip().lower())

    def classify_quota_pool(self, model: str | None = None) -> str:
        if self.quota_pools:
            return self.quota_pools[0].pool
        return self.name

    def quota_pool_for_alias(self, alias: str) -> str:
        return self.classify_quota_pool(self.alias_model_hints.get(alias.strip().lower()))

    @property
    def has_quota_readout(self) -> bool:
        """Whether this adapter overrides `read_quota` to report live or cached quota readings."""
        return type(self).read_quota is not BackendAdapter.read_quota

    def effective_quota_pools(self) -> tuple[QuotaPoolSpec, ...]:
        """Quota pools this backend draws on: declared specs, or a default pool named after the backend."""
        no_readout = f"{self.name} has no quota read-out."
        if not self.quota_pools:
            return (
                QuotaPoolSpec(
                    pool=self.name,
                    backend=self.name,
                    label=self.name,
                    dashboard_label=self.name,
                    no_reading_reason=no_readout,
                    quota_view_no_reading=no_readout,
                    quota_display_order=100,
                ),
            )
        if self.has_quota_readout:
            return self.quota_pools
        return tuple(
            replace(
                spec,
                no_reading_reason=spec.no_reading_reason or no_readout,
                quota_view_no_reading=spec.quota_view_no_reading or no_readout,
            )
            for spec in self.quota_pools
        )

    # --- Defaults and request validation ---

    def defaults(self, settings: Settings) -> BackendDefaults:
        return BackendDefaults(timeout_seconds=settings.default_timeout_seconds)

    def resolve_model_and_effort(
        self,
        settings: Settings,
        requested_model: str | None,
        requested_effort: str | None,
    ) -> tuple[str | None, str | None]:
        defs = self.defaults(settings)
        raw = (requested_model or "").strip()
        if raw.lower() in {"default", "latest", "auto", "inherit", "cli"}:
            model: str | None = None
        elif raw:
            model = raw
        else:
            model = defs.model
        effort = (
            requested_effort.strip().lower()
            if requested_effort and requested_effort.strip()
            else defs.effort
        )
        return model, effort

    def extra_dirs_on_resume_error(self) -> str:
        return f"extra_dirs is not supported when resuming a {self.name} session."

    # --- Binary discovery and health ---

    @abstractmethod
    def resolve_command(self, settings: Settings) -> list[str]:
        """Return argv prefix locating the CLI without going through a shell."""

    async def check_health(self, runner: Any) -> dict[str, Any]:
        from agent_bridge.runners import _check_cli_health

        try:
            cmd = runner.resolve_command()
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

    async def probe_backend(
        self,
        runner: Any,
        settings: Settings,
        *,
        refresh_models: bool = False,
    ) -> dict[str, Any]:
        _ = refresh_models
        try:
            health = await runner.check_health()
        except Exception as exc:
            health = unhealthy_payload(exc)
        defs = self.defaults(settings)
        return {
            "backend": self.name,
            "cli": self.cli_name,
            "default_model": defs.model,
            "default_effort": defs.effort,
            **health,
        }

    # --- Command building, stream reduction, and usage mapping ---

    @abstractmethod
    def build_command(
        self,
        request: RunRequest,
        *,
        cmd: list[str],
        model: str | None = None,
        effort: str | None = None,
        **kwargs: Any,
    ) -> CommandSpec:
        """Build the CLI argv (and optional stdin/env) for a new run, resume, or review."""

    @abstractmethod
    def parse_stream(
        self,
        lines: list[str],
        *,
        request: RunRequest | None = None,
    ) -> ParsedStream:
        """Reduce stdout lines into a ParsedStream, marking `terminal_event_seen`."""

    def describe_event(self, event: dict[str, Any]) -> str | None:
        return None

    def build_env(self, settings: Settings, request: RunRequest) -> dict[str, str]:
        from agent_bridge.runners import job_env

        return job_env(settings, request, cli=self.cli_name)

    def normalize_usage(
        self,
        raw_usage: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Map raw backend usage figures to `(normalized_usage, provider_metrics)`."""
        if not raw_usage or not isinstance(raw_usage, dict):
            return None, None
        input_tokens = to_int(raw_usage, "input_tokens")
        output_tokens = to_int(raw_usage, "output_tokens")
        cached_tokens = to_int(raw_usage, "cached_tokens")
        cache_write = to_int(raw_usage, "cache_write_tokens")
        reasoning = to_int(raw_usage, "reasoning_tokens")
        total = to_int(raw_usage, "total_tokens") or (input_tokens + output_tokens)
        normalized: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning,
            "total_tokens": total,
        }
        if cache_write:
            normalized["cache_write_tokens"] = cache_write
        provider_metrics: dict[str, Any] = {
            "provider": self.name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning,
            "total_tokens": total,
        }
        if cache_write:
            provider_metrics["cache_write_tokens"] = cache_write
        return normalized, provider_metrics

    # --- Optional quota reader & pool extraction ---

    async def read_quota(
        self,
        runner: Any,
        *,
        store: Any,
        db_call: Callable[..., Awaitable[Any]],
        ttl_seconds: float,
        save_to_cache: bool = True,
    ) -> dict[str, Any] | None:
        _ = (runner, store, db_call, ttl_seconds, save_to_cache)
        return None

    def extract_job_quota(self, provider_metrics: dict[str, Any] | None) -> dict[str, Any] | None:
        _ = provider_metrics
        return None

    def merge_cached_quota(
        self,
        previous: dict[str, Any] | None,
        new: dict[str, Any],
    ) -> dict[str, Any]:
        _ = previous
        return new

    def extract_quota_windows(
        self,
        reading: dict[str, Any],
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        """Extract `(windows_by_pool, unrecognised_groups)` from a status=='ok' reading."""
        if not isinstance(reading, dict) or reading.get("status") != "ok":
            return {}, []
        if not self.quota_pools:
            return {}, []
        windows = [w for w in reading.get("windows") or [] if isinstance(w, dict)]
        return {self.quota_pools[0].pool: windows}, []

    def format_unrecognised_quota_group(self, group_name: str) -> tuple[str, str, str]:
        """Return `(pool_key, reason, failed_label)` for an unrecognised quota group."""
        key = f"{self.name}_unknown"
        return key, f"unrecognised {self.name} group: {group_name}", f"{key} ({group_name})"

    def enrich_quota_reading(
        self,
        reading: dict[str, Any],
        enrich_window: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        if isinstance(reading.get("windows"), list):
            if self.quota_pools:
                reading["pool"] = reading.get("pool") or self.quota_pools[0].pool
            reading["windows"] = [enrich_window(w) for w in reading["windows"]]
        return reading

    def advisory_pools(
        self,
        reading: dict[str, Any],
        *,
        neon_pool_models: dict[str, list[str]] | None = None,
        default_label: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build pool descriptor dicts consumed by `quota.build_quota_advisory`."""
        _ = neon_pool_models
        if not isinstance(reading, dict) or reading.get("status") != "ok" or not self.quota_pools:
            return []
        spec = self.quota_pools[0]
        models = [default_label] if default_label else list(spec.advisory_models_fallback)
        return [
            {
                "pool": spec.pool,
                "backend": self.name,
                "label": spec.label,
                "windows": reading.get("windows") or [],
                "models": models,
                "rate_limit_reached": bool(reading.get("rate_limit_reached")),
                "shared_with_own_use": spec.shared_with_own_use,
            }
        ]

    # --- Failure and cancellation hooks ---

    def classify_failure(
        self,
        *,
        exit_code: int | None,
        error_text: str | None,
        stderr_text: str | None,
        timed_out_locally: bool = False,
        process_stopped: bool = True,
    ) -> tuple[JobStatus, str, ProviderState, str] | None:
        _ = (exit_code, error_text, stderr_text, timed_out_locally, process_stopped)
        return None

    def remote_turn_cancel_error(self) -> str:
        return "Job cancelled while the remote turn may still be running."

    def remote_turn_cancel_guidance(self, job_id: str) -> str:
        return (
            "The session stays blocked until confirmed. Confirm that this turn has ended "
            f"(and inspect git status/diff for edits), then resume with confirm_prior_job_id={job_id!r}."
        )

    # --- Runner creation ---

    def create_runner(self, settings: Settings) -> Any:
        return AdapterRunner(self, settings)


class AdapterRunner:
    """Default subprocess runner for any `BackendAdapter`."""

    def __init__(self, adapter: BackendAdapter, settings: Settings) -> None:
        self.adapter = adapter
        self._settings = settings

    def resolve_command(self) -> list[str]:
        return self.adapter.resolve_command(self._settings)

    async def check_health(self) -> dict[str, Any]:
        return await self.adapter.check_health(self)

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

        from agent_bridge.models import RunCancelled, RunResult, UsageStatus
        from agent_bridge.process_control import (
            ProcessContainmentError,
            ProcessStartupTimeout,
            run_managed_process_io,
            spawn_managed_process,
        )
        from agent_bridge.runners import (
            CLI_OUTPUT_CUT_SHORT_ERROR,
            CLI_OUTPUT_CUT_SHORT_GUIDANCE,
            _inspect_git_checkout,
            _maybe_await,
            _tail,
            classify_failure,
            prepare_job_worktree,
        )

        _ = (prior_watermark, on_attempt)
        started = time.monotonic()
        overall_deadline = deadline if deadline is not None else (started + request.timeout_seconds)
        effective_model, effective_effort = self.adapter.resolve_model_and_effort(
            self._settings, request.model, request.effort,
        )
        reported_model = effective_model or f"{self.adapter.name}-default"
        reported_effort = effective_effort
        if on_model:
            on_model(reported_model, "requested" if effective_model else "default")

        if overall_deadline - started <= 0.05:
            status, reason, pstate, guidance = classify_failure(
                backend=self.adapter.name,
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
                provider_invoked=False,
            )

        if self.adapter.capabilities.worktree and request.worktree and not request.worktree_path:
            wt_job_id = request.job_id or f"{self.adapter.name}-{int(time.time() * 1000)}"
            wt_info = await prepare_job_worktree(
                job_id=wt_job_id,
                cwd=request.cwd,
                state_dir=self._settings.state_dir,
                deadline=overall_deadline,
            )
            request.cwd = str(wt_info["effective_cwd"] or request.cwd)
            request.worktree_path = wt_info["worktree_path"]
            request.worktree_branch = wt_info["worktree_branch"]
            request.worktree_commit = wt_info["worktree_commit"]
            request.git_common_dir = wt_info["git_common_dir"]

        cmd = self.resolve_command()
        spec = self.adapter.build_command(
            request, cmd=cmd, model=effective_model, effort=effective_effort,
        )
        if on_activity:
            on_activity(f"running {self.adapter.cli_name} ({reported_model})")

        await _maybe_await(on_provider_start)

        env = spec.env if spec.env is not None else self.adapter.build_env(self._settings, request)
        try:
            remaining = overall_deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessStartupTimeout(
                    "Job deadline expired before execution started.", process_stopped=True,
                )
            proc, managed = await spawn_managed_process(
                *spec.argv,
                cwd=request.cwd,
                stdin=asyncio.subprocess.PIPE if spec.stdin_text is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                timeout=remaining,
                env=env,
            )
        except asyncio.TimeoutError as exc:
            process_stopped = isinstance(exc, ProcessStartupTimeout) and exc.process_stopped
            message = f"{self.adapter.name} subagent timed out during startup after {request.timeout_seconds}s."
            status, reason, pstate, guidance = classify_failure(
                backend=self.adapter.name,
                exit_code=None,
                error_text=message,
                stderr_text=None,
                timed_out_locally=True,
                process_stopped=process_stopped,
            )
            return RunResult(
                status=status,
                session_id=request.session_id,
                model=reported_model,
                effort=reported_effort,
                effective_cwd=request.cwd,
                worktree_path=request.worktree_path,
                worktree_branch=request.worktree_branch,
                worktree_commit=request.worktree_commit,
                duration_seconds=round(max(0.0, time.monotonic() - started), 2),
                usage_status="missing",
                error=message,
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
                    f"Windows process containment failed before launching {self.adapter.cli_name}; "
                    "worker was terminated while still suspended."
                ),
                provider_invoked=False,
            )

        stdin_bytes = spec.stdin_text.encode("utf-8") if spec.stdin_text is not None else None
        stdout_lines: list[str] = []
        stderr_chunks: list[bytes] = []

        async def _write_stdin() -> None:
            if stdin_bytes is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(stdin_bytes)
                await proc.stdin.drain()
            except Exception as exc:
                logger.warning("Failed writing prompt to %s stdin: %s", self.adapter.cli_name, exc)
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
            sid = event.get("session_id")
            if isinstance(sid, str) and sid and on_session:
                on_session(sid)
            live_model = event.get("model")
            if (
                self.adapter.capabilities.reports_observed_model
                and isinstance(live_model, str)
                and live_model
            ):
                reported_model = live_model
                if on_model:
                    on_model(live_model, "observed")
            if on_activity:
                activity = self.adapter.describe_event(event)
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
            wait_budget = overall_deadline - time.monotonic()
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
                    write_stdin=_write_stdin if stdin_bytes is not None else None,
                    timeout=wait_budget,
                )
        except asyncio.TimeoutError:
            timed_out_locally = True
            process_stopped = managed.verified_stopped and (not managed.is_alive())
        except asyncio.CancelledError:
            cancelled = True

        work_end = managed.work_stopped_at if managed.work_stopped_at is not None else time.monotonic()
        elapsed = round(max(0.0, work_end - started), 2)
        cleanup_secs = (
            round(managed.last_cleanup_seconds, 2)
            if managed.last_cleanup_seconds > 0
            else (0.0 if timed_out_locally else None)
        )
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        stderr_tail = _tail(stderr_text)
        parsed = self.adapter.parse_stream(stdout_lines, request=request)
        session_id = parsed.session_id or request.session_id
        if parsed.model:
            reported_model = parsed.model
        errors = list(parsed.errors)
        final_event_missing = (
            (not timed_out_locally)
            and (not cancelled)
            and proc.returncode == 0
            and (not parsed.is_error)
            and (not errors)
            and (not parsed.terminal_event_seen)
        )
        turn_completed_cleanly = (
            (not timed_out_locally)
            and (proc.returncode == 0 or cancelled)
            and parsed.terminal_event_seen
            and (not parsed.is_error)
            and (not errors)
        )

        usage, provider_metrics = self.adapter.normalize_usage(parsed.usage)
        usage_status: UsageStatus = "missing"
        if usage is not None:
            usage_status = "exact" if turn_completed_cleanly else "partial"

        wt_path = request.worktree_path
        wt_branch = request.worktree_branch
        wt_commit = request.worktree_commit
        if not cancelled and not timed_out_locally and wt_path and Path(wt_path).is_dir():
            if overall_deadline - time.monotonic() > 0.05:
                try:
                    new_branch, new_commit, _ = await _inspect_git_checkout(
                        wt_path, timeout_seconds=5.0, deadline=overall_deadline, include_common_dir=False,
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
            message = f"{self.adapter.name} subagent timed out after {request.timeout_seconds}s."
            status, reason, pstate, guidance = classify_failure(
                backend=self.adapter.name,
                exit_code=proc.returncode,
                error_text=message,
                stderr_text=stderr_text,
                timed_out_locally=True,
                process_stopped=process_stopped,
            )
            return RunResult(
                status=status,
                response=parsed.response,
                session_id=session_id,
                model=reported_model,
                effort=reported_effort,
                effective_cwd=request.cwd,
                worktree_path=wt_path,
                worktree_branch=wt_branch,
                worktree_commit=wt_commit,
                duration_seconds=elapsed,
                cleanup_seconds=cleanup_secs,
                usage=usage,
                provider_metrics=provider_metrics,
                usage_status=usage_status,
                exit_code=proc.returncode,
                error=message,
                failure_reason=reason,
                provider_state=pstate,
                retry_guidance=guidance,
                stderr_tail=stderr_tail,
                provider_invoked=True,
            )

        if final_event_missing:
            return RunResult(
                status="failed",
                response=parsed.response,
                session_id=session_id,
                model=reported_model,
                effort=reported_effort,
                effective_cwd=request.cwd,
                worktree_path=wt_path,
                worktree_branch=wt_branch,
                worktree_commit=wt_commit,
                duration_seconds=elapsed,
                cleanup_seconds=cleanup_secs,
                usage=usage,
                provider_metrics=provider_metrics,
                usage_status=usage_status,
                exit_code=0,
                error=CLI_OUTPUT_CUT_SHORT_ERROR.format(event=self.adapter.missing_terminal_event_name),
                failure_reason="cli_error",
                provider_state="stopped" if process_stopped else "possibly_running",
                retry_guidance=CLI_OUTPUT_CUT_SHORT_GUIDANCE,
                stderr_tail=stderr_tail,
                provider_invoked=True,
            )

        if turn_completed_cleanly:
            return RunResult(
                status="completed",
                response=parsed.response,
                session_id=session_id,
                model=reported_model,
                effort=reported_effort,
                effective_cwd=request.cwd,
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
            stderr_tail or f"{self.adapter.cli_name} exited with code {proc.returncode}"
        )
        status, reason, pstate, guidance = classify_failure(
            backend=self.adapter.name,
            exit_code=proc.returncode,
            error_text=err_msg,
            stderr_text=stderr_text,
            process_stopped=process_stopped,
        )
        return RunResult(
            status=status,
            response=parsed.response,
            session_id=session_id,
            model=reported_model,
            effort=reported_effort,
            effective_cwd=request.cwd,
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
