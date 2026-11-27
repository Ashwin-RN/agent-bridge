"""Configuration, backend/mode/effort normalization, quota pool classification, and Codex config inspection."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BackendName = Literal["neon", "codex", "claude"]
QuotaPool = Literal["agy_gemini", "agy_claude_gpt", "codex", "claude"]
ExecutionMode = Literal["read_only", "edit"]
EffortLevel = Literal["low", "medium", "high", "xhigh"]

DEFAULT_CODEX_MODEL = "gpt-6.2-sol"
DEFAULT_CODEX_EFFORT: EffortLevel = "high"
DEFAULT_CLAUDE_MODEL = "sonnet"
DEFAULT_CLAUDE_EFFORT: EffortLevel = "high"

# Names the `claude` CLI resolves to its newest model of that family.
CLAUDE_MODEL_ALIASES: frozenset[str] = frozenset({"sonnet", "opus", "haiku", "fable"})
_INHERIT_TOKENS: frozenset[str] = frozenset({"auto", "latest", "default", "inherit", "cli"})

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
    "claude": "claude",
    "claude-code": "claude",
    "claude_code": "claude",
    "anthropic": "claude",
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
    "claude": "claude",
    "claude-code": "claude",
    "claude_code": "claude",
    "anthropic": "claude",
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
            f"(aliases: agy, gemini, agy-claude, agy-opus, agy-sonnet, agy-gpt, gpt, openai, "
            f"claude-code, anthropic)."
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
            f"Unknown quota_pool {value!r}. Expected one of: agy_gemini, agy_claude_gpt, codex, claude."
        )
    return QUOTA_POOL_ALIASES[key]


def classify_quota_pool(backend: str, model: str | None = None) -> QuotaPool:
    """Map `(backend, model)` onto one of the four independent subscription quota pools."""
    if backend == "codex":
        return "codex"
    # The backend decides first: `sonnet` on the `claude` backend is the Claude plan, not the agy pool.
    if backend == "claude":
        return "claude"
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


def split_claude_model_and_effort(model: str | None) -> tuple[str | None, EffortLevel | None]:
    """Split `opus-high` or `claude-sonnet-medium` into `("opus", "high")`; leave full model ids alone.

    The `claude` CLI takes the family name and the effort as separate flags, while the other backends
    accept `<model>-<effort>` shortcuts. Only a known family name followed by a known effort is split.
    """
    if model is None or not model.strip():
        return None, None
    raw = model.strip()
    key = raw.lower()
    if key.startswith("claude-") and key[len("claude-"):].split("-")[0] in CLAUDE_MODEL_ALIASES:
        key = key[len("claude-"):]
    if key in CLAUDE_MODEL_ALIASES:
        return key, None
    family, _, tail = key.partition("-")
    if family in CLAUDE_MODEL_ALIASES and tail in EFFORT_ALIASES:
        return family, EFFORT_ALIASES[tail]
    return raw, None


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
