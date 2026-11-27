"""Data models for subagent runs, results, failure taxonomy, and job records."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_bridge.config import BackendName, EffortLevel, ExecutionMode, QuotaPool, classify_quota_pool

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
    confirm_prior_stopped: bool = False
    confirm_prior_job_id: str | None = None
    allow_cross_pool_fallback: bool = False
    prior_quota_pool: QuotaPool | None = None
    prior_model: str | None = None
    resume_notice: str | None = None


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

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def elapsed_seconds(self) -> float:
        if self.duration_seconds is not None:
            return round(self.duration_seconds, 2)
        if self.started_at is not None:
            end = self.finished_at if self.finished_at is not None else time.time()
            return round(max(0.0, end - self.started_at), 2)
        return 0.0

    def to_summary(self) -> dict[str, Any]:
        pool = self.quota_pool or classify_quota_pool(self.backend, self.model or self.requested_model)
        out: dict[str, Any] = {
            "job_id": self.job_id,
            "owner_id": self.owner_id,
            "backend": self.backend,
            "quota_pool": pool,
            "status": self.status,
            "mode": self.mode,
            "model": self.model or self.requested_model or f"{self.backend}-default",
            "cwd": self.cwd,
            "effective_cwd": self.effective_cwd or self.cwd,
            "elapsed_seconds": self.elapsed_seconds(),
            "prompt_preview": self.prompt_preview,
        }
        if self.effort:
            out["effort"] = self.effort
        if self.label:
            out["label"] = self.label
        if self.parent_run:
            out["parent_run"] = self.parent_run
        if self.session_id:
            out["session_id"] = self.session_id
        if self.worktree_path:
            out["worktree_path"] = self.worktree_path
        if self.usage:
            out["total_tokens"] = self.usage.get("total_tokens")
            out["usage_status"] = self.usage_status
        elif self.is_terminal:
            out["usage_status"] = self.usage_status
        if self.model_notice:
            out["model_notice"] = self.model_notice
        if self.last_activity and not self.is_terminal:
            out["last_activity"] = self.last_activity
        if self.failure_reason:
            out["failure_reason"] = self.failure_reason
        if self.provider_state and self.status != "completed":
            out["provider_state"] = self.provider_state
        if self.error:
            out["error"] = self.error
        return out

    def to_detail(self, *, max_response_chars: int | None = None) -> dict[str, Any]:
        response = self.response
        truncated = False
        if max_response_chars is not None and len(response) > max_response_chars:
            response = response[:max_response_chars] + "\n...[truncated]"
            truncated = True

        pool = self.quota_pool or classify_quota_pool(self.backend, self.model or self.requested_model)
        out: dict[str, Any] = {
            "job_id": self.job_id,
            "owner_id": self.owner_id,
            "backend": self.backend,
            "quota_pool": pool,
            "status": self.status,
            "mode": self.mode,
            "model": self.model or self.requested_model or f"{self.backend}-default",
            "cwd": self.cwd,
            "effective_cwd": self.effective_cwd or self.cwd,
            "elapsed_seconds": self.elapsed_seconds(),
        }
        if self.cleanup_seconds is not None and (
            self.cleanup_seconds > 0 or self.status in {"timed_out", "cancelled"}
        ):
            out["cleanup_seconds"] = round(self.cleanup_seconds, 2)
        if self.effort:
            out["effort"] = self.effort
        if self.requested_model and self.requested_model != self.model:
            out["requested_model"] = self.requested_model
        if self.model_notice:
            out["model_notice"] = self.model_notice
        if self.label:
            out["label"] = self.label
        if self.parent_run:
            out["parent_run"] = self.parent_run
        if self.worktree or self.worktree_path:
            if self.worktree:
                out["worktree"] = True
            if self.worktree_path:
                out["worktree_path"] = self.worktree_path
            if self.worktree_branch:
                out["worktree_branch"] = self.worktree_branch
            if self.worktree_commit:
                out["worktree_commit"] = self.worktree_commit
        if self.session_id:
            out["session_id"] = self.session_id
        if self.last_activity and not self.is_terminal:
            out["last_activity"] = self.last_activity
        if self.is_terminal or response:
            out["response"] = response
            if truncated:
                out["response_truncated"] = True
        if self.usage:
            out["usage"] = self.usage
        if self.provider_metrics:
            out["provider_metrics"] = self.provider_metrics
        attempts_list = self.attempts
        if not attempts_list and isinstance(self.provider_metrics, dict):
            raw_att = self.provider_metrics.get("attempts")
            if isinstance(raw_att, list) and len(raw_att) > 1:
                attempts_list = raw_att
        if attempts_list and len(attempts_list) > 1:
            out["attempts"] = attempts_list
        if self.is_terminal:
            out["usage_status"] = self.usage_status
        if self.exit_code is not None and self.exit_code != 0:
            out["exit_code"] = self.exit_code
        if self.failure_reason:
            out["failure_reason"] = self.failure_reason
        if self.provider_state and self.status != "completed":
            out["provider_state"] = self.provider_state
        if self.retry_guidance and self.status != "completed":
            out["retry_guidance"] = self.retry_guidance
        if self.error:
            out["error"] = self.error
        if self.stderr_tail and (self.error or self.status != "completed"):
            out["stderr_tail"] = self.stderr_tail
        return out
