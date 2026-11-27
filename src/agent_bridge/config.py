"""Configuration, backend/mode/effort normalization, and quota pool classification."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent_bridge.backends.claude import (
    CLAUDE_MODEL_ALIASES,
    DEFAULT_CLAUDE_EFFORT,
    DEFAULT_CLAUDE_MODEL,
    split_claude_model_and_effort,
)
from agent_bridge.backends.codex import (
    DEFAULT_CODEX_EFFORT,
    DEFAULT_CODEX_MODEL,
    read_codex_local_defaults,
)
from agent_bridge.backends.registry import get_default_registry

BackendName = str
QuotaPool = str
ExecutionMode = Literal["read_only", "edit"]
EffortLevel = Literal["low", "medium", "high", "xhigh"]

_INHERIT_TOKENS: frozenset[str] = frozenset({"auto", "latest", "default", "inherit", "cli"})

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

EFFORT_ALIASES: dict[str, EffortLevel] = {
    "low": "low",
    "lo": "low",
    "medium": "medium",
    "med": "medium",
    "high": "high",
    "hi": "high",
    "xhigh": "xhigh",
    "xh": "xhigh",
    "extra-high": "xhigh",
    "extrahigh": "xhigh",
    "max": "xhigh",
}

MIN_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 3600


def __getattr__(name: str) -> Any:
    if name == "BACKEND_ALIASES":
        return get_default_registry().aliases_map()
    if name == "BACKEND_DEFAULT_MODEL_HINTS":
        return get_default_registry().alias_model_hints_map()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def normalize_backend(value: str) -> BackendName:
    return get_default_registry().normalize_backend(value)


def resolve_backend_and_model_hint(
    backend: str,
    model: str | None,
) -> tuple[BackendName, str | None]:
    """Normalize backend while turning pool shortcuts (`agy-claude`, `agy-opus`, `agy-gpt`) into model defaults."""
    return get_default_registry().resolve_backend_and_model_hint(backend, model)


def classify_quota_pool(backend: str, model: str | None = None) -> QuotaPool:
    """Map `(backend, model)` onto its subscription quota pool via the backend adapter."""
    return get_default_registry().classify_quota_pool(backend, model)


def normalize_mode(value: str | None) -> ExecutionMode:
    if value is None or not value.strip():
        return "read_only"
    key = value.strip().lower()
    if key not in MODE_ALIASES:
        raise ValueError(
            f"Unknown mode {value!r}. Expected 'read_only' or 'edit'."
        )
    return MODE_ALIASES[key]


def normalize_effort(value: str | None) -> EffortLevel | None:
    if value is None or not value.strip():
        return None
    key = value.strip().lower()
    if key not in EFFORT_ALIASES:
        raise ValueError(
            f"Unknown effort {value!r}. Expected one of: low, medium, high, xhigh."
        )
    return EFFORT_ALIASES[key]


STATE_DB_FILENAME = "bridge.db"


def state_dir_from_env() -> Path:
    """The bridge's state folder: `AGENT_BRIDGE_STATE_DIR` if set, else `~/.agent-bridge`.

    The one rule for every reader of the state folder (the server and `agent-bridge status`).
    It reads no other variable and never raises on a value it does not recognise.
    """
    raw = os.environ.get("AGENT_BRIDGE_STATE_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".agent-bridge"


def clamp_timeout(timeout_seconds: int | None, default: int) -> int:
    value = default if timeout_seconds is None else int(timeout_seconds)
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, value))


@dataclass(frozen=True)
class Settings:
    default_neon_model: str
    default_codex_model: str | None
    default_codex_effort: EffortLevel | None
    default_timeout_seconds: int
    max_concurrent_jobs: int
    max_stored_jobs: int
    models_cache_ttl_seconds: float
    quota_cache_ttl_seconds: float
    owner_stale_seconds: float
    state_dir: Path
    agy_bin: str | None
    codex_bin: str | None
    default_cwd: Path
    default_claude_model: str | None = DEFAULT_CLAUDE_MODEL
    default_claude_effort: EffortLevel | None = DEFAULT_CLAUDE_EFFORT
    claude_bin: str | None = None
    # "plan": leave API credentials out of a Claude job's environment, so it runs on the signed-in plan.
    # "inherit": pass the environment on as it is.
    claude_auth: str = "plan"
    # "trim": leave variables whose names look like credentials out of a Codex or agy job's environment.
    # "inherit": pass the environment on as it is.
    child_env: str = "trim"
    # Exact variable names exempted from trimming, using Windows' uppercase environment keys there.
    child_env_keep: frozenset[str] = frozenset()
    # Optional per-backend settings map for additional adapters.
    backend_options: Mapping[str, Any] | None = None

    @classmethod
    def from_env(cls) -> Settings:
        default_neon_model = (
            os.environ.get("AGENT_BRIDGE_NEON_MODEL", "neon-high").strip() or "neon-high"
        )

        raw_codex_model = os.environ.get("AGENT_BRIDGE_CODEX_MODEL")
        if raw_codex_model is None or not raw_codex_model.strip():
            default_codex_model: str | None = DEFAULT_CODEX_MODEL
        elif raw_codex_model.strip().lower() in {"auto", "latest", "default", "inherit", "cli"}:
            default_codex_model = None
        else:
            default_codex_model = raw_codex_model.strip()

        raw_codex_effort = os.environ.get("AGENT_BRIDGE_CODEX_EFFORT")
        if raw_codex_effort is None or not raw_codex_effort.strip():
            default_codex_effort: EffortLevel | None = DEFAULT_CODEX_EFFORT
        elif raw_codex_effort.strip().lower() in {"inherit", "cli", "default", "auto"}:
            default_codex_effort = None
        else:
            default_codex_effort = normalize_effort(raw_codex_effort)

        raw_claude_model = os.environ.get("AGENT_BRIDGE_CLAUDE_MODEL")
        if raw_claude_model is None or not raw_claude_model.strip():
            default_claude_model: str | None = DEFAULT_CLAUDE_MODEL
        elif raw_claude_model.strip().lower() in _INHERIT_TOKENS:
            default_claude_model = None
        else:
            default_claude_model = raw_claude_model.strip()

        raw_claude_effort = os.environ.get("AGENT_BRIDGE_CLAUDE_EFFORT")
        if raw_claude_effort is None or not raw_claude_effort.strip():
            default_claude_effort: EffortLevel | None = DEFAULT_CLAUDE_EFFORT
        elif raw_claude_effort.strip().lower() in _INHERIT_TOKENS:
            default_claude_effort = None
        else:
            default_claude_effort = normalize_effort(raw_claude_effort)

        raw_claude_auth = os.environ.get("AGENT_BRIDGE_CLAUDE_AUTH", "").strip().lower() or "plan"
        if raw_claude_auth not in {"plan", "inherit"}:
            raise ValueError(
                f"Unknown AGENT_BRIDGE_CLAUDE_AUTH {raw_claude_auth!r}. Expected 'plan' or 'inherit'."
            )

        raw_child_env = os.environ.get("AGENT_BRIDGE_CHILD_ENV", "").strip().lower() or "trim"
        if raw_child_env not in {"trim", "inherit"}:
            raise ValueError(
                f"Unknown AGENT_BRIDGE_CHILD_ENV {raw_child_env!r}. Expected 'trim' or 'inherit'."
            )
        child_env_keep = frozenset(
            name.upper() if os.name == "nt" else name
            for part in os.environ.get("AGENT_BRIDGE_CHILD_ENV_KEEP", "").split(",")
            if (name := part.strip())
        )

        default_timeout = int(os.environ.get("AGENT_BRIDGE_DEFAULT_TIMEOUT", "300"))
        max_concurrent = max(1, int(os.environ.get("AGENT_BRIDGE_MAX_CONCURRENT", "8")))
        max_stored = max(20, int(os.environ.get("AGENT_BRIDGE_MAX_STORED_JOBS", "500")))
        models_ttl = float(os.environ.get("AGENT_BRIDGE_MODELS_TTL_SECONDS", "900"))
        quota_ttl = float(os.environ.get("AGENT_BRIDGE_QUOTA_TTL_SECONDS", "120"))
        owner_stale = float(os.environ.get("AGENT_BRIDGE_OWNER_STALE_SECONDS", "30"))
        state_dir = state_dir_from_env()
        agy_bin = os.environ.get("AGENT_BRIDGE_AGY_BIN", "").strip() or None
        codex_bin = os.environ.get("AGENT_BRIDGE_CODEX_BIN", "").strip() or None
        claude_bin = os.environ.get("AGENT_BRIDGE_CLAUDE_BIN", "").strip() or None
        default_cwd_raw = os.environ.get("AGENT_BRIDGE_DEFAULT_CWD", "").strip()
        default_cwd = (
            Path(default_cwd_raw).expanduser().resolve()
            if default_cwd_raw
            else Path.cwd().resolve()
        )

        return cls(
            default_neon_model=default_neon_model,
            default_codex_model=default_codex_model,
            default_codex_effort=default_codex_effort,
            default_timeout_seconds=clamp_timeout(default_timeout, 300),
            max_concurrent_jobs=max_concurrent,
            max_stored_jobs=max_stored,
            models_cache_ttl_seconds=max(30.0, models_ttl),
            quota_cache_ttl_seconds=max(10.0, quota_ttl),
            owner_stale_seconds=max(5.0, owner_stale),
            state_dir=state_dir,
            agy_bin=agy_bin,
            codex_bin=codex_bin,
            default_cwd=default_cwd,
            default_claude_model=default_claude_model,
            default_claude_effort=default_claude_effort,
            claude_bin=claude_bin,
            claude_auth=raw_claude_auth,
            child_env=raw_child_env,
            child_env_keep=child_env_keep,
        )


__all__ = [
    "BackendName",
    "CLAUDE_MODEL_ALIASES",
    "DEFAULT_CLAUDE_EFFORT",
    "DEFAULT_CLAUDE_MODEL",
    "DEFAULT_CODEX_EFFORT",
    "DEFAULT_CODEX_MODEL",
    "EFFORT_ALIASES",
    "EffortLevel",
    "ExecutionMode",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "MODE_ALIASES",
    "QuotaPool",
    "STATE_DB_FILENAME",
    "Settings",
    "clamp_timeout",
    "classify_quota_pool",
    "normalize_backend",
    "normalize_effort",
    "normalize_mode",
    "read_codex_local_defaults",
    "resolve_backend_and_model_hint",
    "split_claude_model_and_effort",
    "state_dir_from_env",
]
