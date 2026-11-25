"""Data models for subagent runs, results, and persisted job records."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_bridge.config import BackendName, ExecutionMode

JobStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
]

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
    session_id: str | None = None
    worktree: bool = False
    extra_dirs: list[str] = field(default_factory=list)
    timeout_seconds: int = 300
    label: str | None = None
    review_scope: Literal["uncommitted", "base", "commit"] | None = None
    review_base: str | None = None
    review_commit: str | None = None


@dataclass(slots=True)
class RunResult:
    status: JobStatus
    response: str = ""
    session_id: str | None = None
    model: str | None = None
    model_notice: str | None = None
    duration_seconds: float | None = None
    usage: dict[str, Any] | None = None
    exit_code: int | None = None
    error: str | None = None
    stderr_tail: str | None = None


@dataclass(slots=True)
class JobRecord:
    job_id: str
    backend: BackendName
    cwd: str
    mode: ExecutionMode
    requested_model: str | None
    model: str | None
    prompt_preview: str
    label: str | None = None
    worktree: bool = False
    requested_session_id: str | None = None
    session_id: str | None = None
    model_notice: str | None = None
    status: JobStatus = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    duration_seconds: float | None = None
    response: str = ""
    usage: dict[str, Any] | None = None
    exit_code: int | None = None
    error: str | None = None
    stderr_tail: str | None = None
    last_activity: str | None = None

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
        out: dict[str, Any] = {
            "job_id": self.job_id,
            "backend": self.backend,
            "status": self.status,
            "mode": self.mode,
            "model": self.model or self.requested_model or f"{self.backend}-default",
            "cwd": self.cwd,
            "elapsed_seconds": self.elapsed_seconds(),
            "prompt_preview": self.prompt_preview,
        }
        if self.label:
            out["label"] = self.label
        if self.session_id:
            out["session_id"] = self.session_id
        if self.usage:
            out["total_tokens"] = self.usage.get("total_tokens")
        if self.model_notice:
            out["model_notice"] = self.model_notice
        if self.last_activity and not self.is_terminal:
            out["last_activity"] = self.last_activity
        if self.error:
            out["error"] = self.error
        return out

    def to_detail(self, *, max_response_chars: int | None = None) -> dict[str, Any]:
        response = self.response
        truncated = False
        if max_response_chars is not None and len(response) > max_response_chars:
            response = response[:max_response_chars] + "\n...[truncated]"
            truncated = True

        out: dict[str, Any] = {
            "job_id": self.job_id,
            "backend": self.backend,
            "status": self.status,
            "mode": self.mode,
            "model": self.model or self.requested_model or f"{self.backend}-default",
            "cwd": self.cwd,
            "elapsed_seconds": self.elapsed_seconds(),
        }
        if self.requested_model and self.requested_model != self.model:
            out["requested_model"] = self.requested_model
        if self.model_notice:
            out["model_notice"] = self.model_notice
        if self.label:
            out["label"] = self.label
        if self.worktree:
            out["worktree"] = True
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
        if self.exit_code is not None and self.exit_code != 0:
            out["exit_code"] = self.exit_code
        if self.error:
            out["error"] = self.error
        if self.stderr_tail and (self.error or self.status != "completed"):
            out["stderr_tail"] = self.stderr_tail
        return out

    def to_persisted(self) -> dict[str, Any]:
        persisted_response = self.response[:200_000] if self.response else ""
        return {
            "job_id": self.job_id,
            "backend": self.backend,
            "cwd": self.cwd,
            "mode": self.mode,
            "requested_model": self.requested_model,
            "model": self.model,
            "model_notice": self.model_notice,
            "prompt_preview": self.prompt_preview,
            "label": self.label,
            "worktree": self.worktree,
            "requested_session_id": self.requested_session_id,
            "session_id": self.session_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "response": persisted_response,
            "usage": self.usage,
            "exit_code": self.exit_code,
            "error": self.error,
            "stderr_tail": self.stderr_tail,
            "last_activity": self.last_activity,
        }

    @classmethod
    def from_persisted(cls, data: dict[str, Any]) -> JobRecord:
        return cls(
            job_id=str(data["job_id"]),
            backend=data["backend"],
            cwd=str(data["cwd"]),
            mode=data.get("mode", "read_only"),
            requested_model=data.get("requested_model"),
            model=data.get("model"),
            model_notice=data.get("model_notice"),
            prompt_preview=str(data.get("prompt_preview", "")),
            label=data.get("label"),
            worktree=bool(data.get("worktree", False)),
            requested_session_id=data.get("requested_session_id"),
            session_id=data.get("session_id"),
            status=data.get("status", "failed"),
            created_at=float(data.get("created_at", time.time())),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            duration_seconds=data.get("duration_seconds"),
            response=str(data.get("response", "")),
            usage=data.get("usage"),
            exit_code=data.get("exit_code"),
            error=data.get("error"),
            stderr_tail=data.get("stderr_tail"),
            last_activity=data.get("last_activity"),
        )
