"""Configuration, backend/mode/effort normalization, quota pool classification, and Codex config inspection."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BackendName = Literal["neon", "codex"]
QuotaPool = Literal["agy_gemini", "agy_claude_gpt", "codex"]
ExecutionMode = Literal["read_only", "edit"]
EffortLevel = Literal["low", "medium", "high", "xhigh"]

DEFAULT_CODEX_MODEL = "gpt-6.2-sol"
DEFAULT_CODEX_EFFORT: EffortLevel = "high"

BACKEND_ALIASES: dict[str, BackendName] = {
    "neon": "neon",
    "agy": "neon",
    "gemini": "neon",
    "antigravity": "neon",
    "agy-claude": "neon",
    "agy_claude": "neon",
    "agy-sonnet": "neon",
    "agy_sonnet": "neon",
    "agy-opus": "neon",
    "agy_opus": "neon",
    "agy-gpt": "neon",
    "agy_gpt": "neon",
    "codex": "codex",
    "gpt": "codex",
    "openai": "codex",
}

BACKEND_DEFAULT_MODEL_HINTS: dict[str, str] = {
    "agy-claude": "claude-sonnet-high",
    "agy_claude": "claude-sonnet-high",
    "agy-sonnet": "claude-sonnet-high",
    "agy_sonnet": "claude-sonnet-high",
    "agy-opus": "claude-opus-high",
    "agy_opus": "claude-opus-high",
    "agy-gpt": "gpt-oss",
    "agy_gpt": "gpt-oss",
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

QUOTA_POOL_ALIASES: dict[str, QuotaPool] = {
    "agy_gemini": "agy_gemini",
    "agy-gemini": "agy_gemini",
    "gemini": "agy_gemini",
    "neon": "agy_gemini",
    "agy_claude_gpt": "agy_claude_gpt",
    "agy-claude-gpt": "agy_claude_gpt",
    "agy_3p": "agy_claude_gpt",
    "agy-3p": "agy_claude_gpt",
    "3p": "agy_claude_gpt",
    "agy-claude": "agy_claude_gpt",
    "agy-gpt": "agy_claude_gpt",
    "claude_gpt": "agy_claude_gpt",
    "codex": "codex",
}

_AGY_THIRD_PARTY_MODEL_RE = re.compile(
    r"(?:^|-)(claude|opus|sonnet|haiku|gpt|oss)(?:-|$)",
    re.I,
)

MIN_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 3600


def normalize_backend(value: str) -> BackendName:
    key = value.strip().lower()
    if key not in BACKEND_ALIASES:
        valid = ", ".join(sorted(set(BACKEND_ALIASES.values())))
        raise ValueError(
            f"Unknown backend {value!r}. Expected one of: {valid} "
            f"(aliases: agy, gemini, agy-claude, agy-opus, agy-sonnet, agy-gpt, gpt, openai)."
        )
    return BACKEND_ALIASES[key]


def resolve_backend_and_model_hint(
    backend: str,
    model: str | None,
) -> tuple[BackendName, str | None]:
    """Normalize backend while turning pool shortcuts (`agy-claude`, `agy-opus`, `agy-gpt`) into model defaults."""
    key = backend.strip().lower()
    resolved_backend = normalize_backend(backend)
    if model and model.strip():
        return resolved_backend, model.strip()
    hinted = BACKEND_DEFAULT_MODEL_HINTS.get(key)
    return resolved_backend, hinted


def normalize_quota_pool(value: str | None) -> QuotaPool | None:
    if value is None or not value.strip():
        return None
    key = value.strip().lower()
    if key not in QUOTA_POOL_ALIASES:
        raise ValueError(
            f"Unknown quota_pool {value!r}. Expected one of: agy_gemini, agy_claude_gpt, codex."
        )
    return QUOTA_POOL_ALIASES[key]


def classify_quota_pool(backend: str, model: str | None = None) -> QuotaPool:
    """Map `(backend, model)` onto one of the three independent subscription quota pools."""
    if backend == "codex":
        return "codex"
    if model and _AGY_THIRD_PARTY_MODEL_RE.search(model.strip()):
        return "agy_claude_gpt"
    return "agy_gemini"


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


def clamp_timeout(timeout_seconds: int | None, default: int) -> int:
    value = default if timeout_seconds is None else int(timeout_seconds)
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, value))


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

        default_timeout = int(os.environ.get("AGENT_BRIDGE_DEFAULT_TIMEOUT", "300"))
        max_concurrent = max(1, int(os.environ.get("AGENT_BRIDGE_MAX_CONCURRENT", "8")))
        max_stored = max(20, int(os.environ.get("AGENT_BRIDGE_MAX_STORED_JOBS", "500")))
        models_ttl = float(os.environ.get("AGENT_BRIDGE_MODELS_TTL_SECONDS", "900"))
        quota_ttl = float(os.environ.get("AGENT_BRIDGE_QUOTA_TTL_SECONDS", "120"))
        owner_stale = float(os.environ.get("AGENT_BRIDGE_OWNER_STALE_SECONDS", "30"))
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
        )
