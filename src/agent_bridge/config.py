"""Configuration, backend normalization, and defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BackendName = Literal["neon", "codex"]
ExecutionMode = Literal["read_only", "edit"]

BACKEND_ALIASES: dict[str, BackendName] = {
    "neon": "neon",
    "agy": "neon",
    "gemini": "neon",
    "antigravity": "neon",
    "codex": "codex",
    "gpt": "codex",
    "openai": "codex",
}

MODE_ALIASES: dict[str, ExecutionMode] = {
    "read_only": "read_only",
    "readonly": "read_only",
    "read-only": "read_only",
    "plan": "read_only",
    "edit": "edit",
    "write": "edit",
    "workspace_write": "edit",
    "workspace-write": "edit",
    "auto": "edit",
}

MIN_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 3600


def normalize_backend(value: str) -> BackendName:
    key = value.strip().lower()
    if key not in BACKEND_ALIASES:
        valid = ", ".join(sorted(set(BACKEND_ALIASES.values())))
        raise ValueError(
            f"Unknown backend {value!r}. Expected one of: {valid} "
            f"(aliases: agy, gemini, gpt, openai)."
        )
    return BACKEND_ALIASES[key]


def normalize_mode(value: str | None) -> ExecutionMode:
    if value is None or not value.strip():
        return "read_only"
    key = value.strip().lower()
    if key not in MODE_ALIASES:
        raise ValueError(
            f"Unknown mode {value!r}. Expected 'read_only' or 'edit'."
        )
    return MODE_ALIASES[key]


def clamp_timeout(timeout_seconds: int | None, default: int) -> int:
    value = default if timeout_seconds is None else int(timeout_seconds)
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, value))


@dataclass(frozen=True)
class Settings:
    # Versionless intent by default ("neon-high"), resolved live against `agy models`.
    default_neon_model: str
    default_codex_model: str | None
    default_timeout_seconds: int
    max_concurrent_jobs: int
    max_stored_jobs: int
    models_cache_ttl_seconds: float
    state_dir: Path
    agy_bin: str | None
    codex_bin: str | None
    default_cwd: Path

    @classmethod
    def from_env(cls) -> Settings:
        default_neon_model = os.environ.get(
            "AGENT_BRIDGE_NEON_MODEL", "neon-high"
        ).strip() or "neon-high"
        default_codex_model = os.environ.get("AGENT_BRIDGE_CODEX_MODEL", "").strip() or None
        default_timeout = int(os.environ.get("AGENT_BRIDGE_DEFAULT_TIMEOUT", "300"))
        max_concurrent = max(1, int(os.environ.get("AGENT_BRIDGE_MAX_CONCURRENT", "8")))
        max_stored = max(10, int(os.environ.get("AGENT_BRIDGE_MAX_STORED_JOBS", "200")))
        models_ttl = float(os.environ.get("AGENT_BRIDGE_MODELS_TTL_SECONDS", "900"))
        state_dir_raw = os.environ.get("AGENT_BRIDGE_STATE_DIR", "").strip()
        state_dir = (
            Path(state_dir_raw).expanduser()
            if state_dir_raw
            else Path.home() / ".agent-bridge"
        )
        agy_bin = os.environ.get("AGENT_BRIDGE_AGY_BIN", "").strip() or None
        codex_bin = os.environ.get("AGENT_BRIDGE_CODEX_BIN", "").strip() or None
        default_cwd_raw = os.environ.get("AGENT_BRIDGE_DEFAULT_CWD", "").strip()
        default_cwd = (
            Path(default_cwd_raw).expanduser().resolve()
            if default_cwd_raw
            else Path.cwd().resolve()
        )

        return cls(
            default_neon_model=default_neon_model,
            default_codex_model=default_codex_model,
            default_timeout_seconds=clamp_timeout(default_timeout, 300),
            max_concurrent_jobs=max_concurrent,
            max_stored_jobs=max_stored,
            models_cache_ttl_seconds=max(30.0, models_ttl),
            state_dir=state_dir,
            agy_bin=agy_bin,
            codex_bin=codex_bin,
            default_cwd=default_cwd,
        )
