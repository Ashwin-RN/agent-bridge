"""Data models for subagent runs, results, failure taxonomy, and job records."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_bridge.config import BackendName, EffortLevel, ExecutionMode, QuotaPool, classify_quota_pool
from agent_bridge.status_text import SEP, format_job_line

# A job reply holds this much of the reply text unless the caller asks for more, up to the maximum.
DEFAULT_RESPONSE_CHARS = 20_000
MAX_RESPONSE_CHARS = 100_000

JobStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
]

ProviderState = Literal["stopped", "possibly_running", "unknown"]
UsageStatus = Literal["exact", "delta", "partial", "missing"]

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "timed_out"}
)


@dataclass(slots=True)
class RunRequest:
    backend: BackendName
    prompt: str
    cwd: str
    mode: ExecutionMode = "read_only"
    model: str | None = None
    effort: EffortLevel | None = None
    session_id: str | None = None
    resume_from_job_id: str | None = None
    explicit_cwd: bool = False
    relocate_cwd: bool = False
    worktree: bool = False
    worktree_path: str | None = None
    worktree_branch: str | None = None
    worktree_commit: str | None = None
    git_common_dir: str | None = None
    extra_dirs: list[str] = field(default_factory=list)
    timeout_seconds: int = 300
    label: str | None = None
    parent_run: str | None = None
    review_scope: Literal["uncommitted", "base", "commit"] | None = None
    review_base: str | None = None
    review_commit: str | None = None
    job_id: str | None = None
    owner_id: str | None = None
    confirm_prior_job_id: str | None = None
    allow_cross_pool_fallback: bool = False
    prior_quota_pool: QuotaPool | None = None
    prior_model: str | None = None
    resume_notice: str | None = None
    fast: bool = False


@dataclass(slots=True)
class RunResult:
    status: JobStatus
    response: str = ""
    session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    model_notice: str | None = None
    effective_cwd: str | None = None
    worktree_path: str | None = None
    worktree_branch: str | None = None
    worktree_commit: str | None = None
    duration_seconds: float | None = None
    cleanup_seconds: float | None = None
    usage: dict[str, Any] | None = None
    provider_metrics: dict[str, Any] | None = None
    usage_status: UsageStatus = "missing"
    cumulative_usage: dict[str, Any] | None = None
    num_turns: int | None = None
    observed_num_turns: int | None = None
    observed_turn_progress: bool = False
    exit_code: int | None = None
    error: str | None = None
    failure_reason: str | None = None
    provider_state: ProviderState | None = None
    retry_guidance: str | None = None
    stderr_tail: str | None = None
    provider_invoked: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)


class RunCancelled(asyncio.CancelledError):
    """A cancelled runner with the output it had already counted."""

    def __init__(self, result: RunResult) -> None:
        self.result = result
        super().__init__()


@dataclass(slots=True)
class JobRecord:
    job_id: str
    owner_id: str
    backend: BackendName
    cwd: str
    mode: ExecutionMode
    requested_model: str | None
    model: str | None
    prompt_preview: str
    quota_pool: QuotaPool | None = None
    effective_cwd: str | None = None
    effort: str | None = None
    label: str | None = None
    parent_run: str | None = None
    worktree: bool = False
    worktree_path: str | None = None
    worktree_branch: str | None = None
    worktree_commit: str | None = None
    requested_session_id: str | None = None
    session_id: str | None = None
    model_notice: str | None = None
    status: JobStatus = "queued"
    failure_reason: str | None = None
    provider_state: ProviderState | None = None
    retry_guidance: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    duration_seconds: float | None = None
    cleanup_seconds: float | None = None
    response: str = ""
    usage: dict[str, Any] | None = None
    provider_metrics: dict[str, Any] | None = None
    usage_status: UsageStatus = "missing"
    exit_code: int | None = None
    error: str | None = None
    stderr_tail: str | None = None
    last_activity: str | None = None
    provider_invoked: bool = False
    attempts: list[dict[str, Any]] | None = None
    last_activity_at: float | None = None
    model_source: Literal["observed", "requested", "default"] | None = None
    response_chars: int | None = None
    fast: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    # --- Replies to a host ---
    #
    # Every reply about a job opens with its status line, then carries only what the host can act on.
    # Nothing that is null, nothing about the window rather than the job, and the reply text in pages.

    def model_value(self) -> str | None:
        """The model name to show, or None when the bridge only knows the CLI's default."""
        model = self.model or self.requested_model
        return None if not model or model == f"{self.backend}-default" else model

    def pool(self) -> QuotaPool:
        return self.quota_pool or classify_quota_pool(self.backend, self.model or self.requested_model)

    def line_fields(self) -> dict[str, Any]:
        """What the status line is built from. A finished job's activity is its status, so it is left out."""
        effort = self.effort
        if self.fast and self.backend == "codex":
            effort = f"{effort}{SEP}fast" if effort else "fast"
        return {
            "backend": self.backend,
            "model": self.model,
            "requested_model": self.requested_model,
            "model_source": self.model_source,
            "effort": effort,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "last_activity": None if self.is_terminal else self.last_activity,
            "last_activity_at": self.last_activity_at,
            "label": self.label,
            "quota_pool": self.pool(),
        }

    def line(self, *, now: float | None = None) -> str:
        return format_job_line(self.line_fields(), now=time.time() if now is None else now, long=True)

    def model_source_value(self) -> str:
        if self.model_source:
            return self.model_source
        if self.model_value() is None:
            return "default"
        return "requested" if self.backend == "codex" or not self.model else "observed"

    def to_summary(self, *, now: float | None = None) -> dict[str, Any]:
        """One entry of a job list: the line, the ids, and why it failed when it did."""
        out: dict[str, Any] = {
            "line": self.line(now=now),
            "job_id": self.job_id,
            "status": self.status,
            "prompt_preview": self.prompt_preview,
        }
        if self.is_terminal and self.status != "completed" and self.failure_reason:
            out["failure_reason"] = self.failure_reason
        return out

    def to_receipt(self, *, timeout_seconds: int, next_call: str, now: float | None = None) -> dict[str, Any]:
        """The reply to a job started in the background: what it is, and the call that collects it."""
        out: dict[str, Any] = {
            "line": self.line(now=now),
            "job_id": self.job_id,
            "status": self.status,
            "backend": self.backend,
        }
        if (model := self.model_value()) is not None:
            out["model"] = model
        out["timeout_seconds"] = timeout_seconds
        out["next"] = next_call
        return out

    def to_detail(
        self,
        *,
        response_offset: int = 0,
        response_chars: int = DEFAULT_RESPONSE_CHARS,
        response_file: str | None = None,
        verbose: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """The full reply about one job, with paged text or a preview of the exported text."""
        out: dict[str, Any] = {
            "line": self.line(now=now),
            "job_id": self.job_id,
            "status": self.status,
            "backend": self.backend,
        }
        if (model := self.model_value()) is not None:
            out["model"] = model
        out["model_source"] = self.model_source_value()
        if self.requested_model and self.model and self.requested_model != self.model:
            out["requested_model"] = self.requested_model
        if self.effort:
            out["effort"] = self.effort
        out["quota_pool"] = self.pool()
        out["mode"] = self.mode
        for key in ("session_id", "parent_run", "model_notice", "worktree_path", "worktree_branch"):
            if value := getattr(self, key):
                out[key] = value
        if self.effective_cwd and self.effective_cwd != self.cwd:
            out["effective_cwd"] = self.effective_cwd
        if response_file is not None:
            out.update(
                response_file=response_file,
                response_head=self.response[:400],
                response_total_chars=len(self.response),
            )
        elif self.is_terminal or self.response:
            out.update(self._response_page(response_offset, response_chars))
        if self.usage:
            out["usage"] = self.usage if verbose else {
                k: v for k in _REPLY_USAGE_KEYS if (v := self.usage.get(k)) not in (None, 0)
            }
        if self.is_terminal and self.usage_status != "exact":
            out["usage_status"] = self.usage_status
        if self.is_terminal and self.status != "completed":
            out.update(self._failure_fields())
        if verbose:
            if self.provider_metrics:
                out["provider_metrics"] = self.provider_metrics
            attempts = self.attempts
            if not attempts and isinstance(self.provider_metrics, dict):
                attempts = self.provider_metrics.get("attempts")
            if isinstance(attempts, list) and len(attempts) > 1:
                out["attempts"] = attempts
        return out

    def _response_page(self, offset: int, chars: int) -> dict[str, Any]:
        stored = self.response
        kept = len(stored)
        total = max(self.response_chars or 0, kept)
        offset = max(0, offset)
        chars = max(1, min(int(chars), MAX_RESPONSE_CHARS))
        piece = stored[offset : offset + chars]
        end = offset + len(piece)
        out: dict[str, Any] = {"response": piece}
        if offset:
            out["response_offset"] = offset
        if end < kept:
            out.update(
                response_truncated=True,
                response_total_chars=total,
                next_offset=end,
                response_note=(
                    f"This is characters {offset} to {end} of {total}. Call jobs(job_ids=['{self.job_id}'], "
                    f"response_offset={end}) for the next part."
                ),
            )
        elif total > kept:
            out.update(
                response_truncated=True,
                response_total_chars=total,
                response_note=(
                    f"The bridge kept only the first {kept} of {total} characters of this reply; "
                    "the rest was not kept."
                ),
            )
        if self.status == "completed" and not stored.strip() and total == kept:
            out["warning"] = (
                "The job completed with an empty reply: the subagent returned no text. Check what it did "
                "(git status and diff for an edit job) before relying on it, or run it again."
            )
        return out

    def _failure_fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in ("failure_reason", "provider_state", "retry_guidance", "error"):
            if value := getattr(self, key):
                out[key] = value
        if self.exit_code not in (None, 0):
            out["exit_code"] = self.exit_code
        if self.cleanup_seconds is not None:
            out["cleanup_seconds"] = round(self.cleanup_seconds, 2)
        if self.stderr_tail:
            out["stderr_tail"] = self.stderr_tail
        return out


# A job reply carries the total only; `verbose` gives the CLI's full count by kind.
_REPLY_USAGE_KEYS: tuple[str, ...] = ("total_tokens",)
