"""Detect the MCP host and keep blocking calls within its tool-call timeout."""

from __future__ import annotations

import math
import ntpath
import os
from collections.abc import Sequence
from threading import Lock
from time import monotonic

from agent_bridge import nesting
from agent_bridge.hosts import WANTED_TIMEOUT_SECONDS, Host, Roots, detect_running_host
from agent_bridge.install_cmd import Unsafe, config_path, configured_timeout_seconds, registered_entry, usable

_CACHE_SECONDS = 60
_UNKNOWN_LIMIT_SECONDS = 30  # The shortest documented host default in the table.
_LAUNCHER_PROCESSES = frozenset({"agent-bridge", "node", "python", "python3", "pythonw", "uv", "uvx"})
_CONFIG_ERRORS = (Unsafe, OSError, ValueError, OverflowError, RuntimeError, KeyError)
_limit_cache: dict[tuple[str, Roots], tuple[float, float | None]] = {}
_cache_lock = Lock()


def _executable_name(name: str) -> str:
    return ntpath.splitext(ntpath.basename(name))[0].lower()


def ancestor_process_names() -> tuple[str, ...]:
    """Return normalized executable names up to the first unreadable ancestor."""
    try:
        names = []
        for pid, _ in nesting.ancestor_chain() or ():
            argv = nesting.process_command_line(pid)
            if not argv or not argv[0]:
                break
            names.append(_executable_name(argv[0]))
        return tuple(names)
    except Exception:
        return ()


def identify_host(client_name: str | None, ancestors: Sequence[str] | None = None) -> Host | None:
    """Use the MCP client name first, then check who launched the server."""
    host = detect_running_host(client_name, ())
    if host is not None:
        return host
    processes = ancestor_process_names() if ancestors is None else ancestors
    for process in processes:
        # A shell or unrelated program between the server and a host breaks the evidence.
        if _executable_name(process) not in _LAUNCHER_PROCESSES:
            return detect_running_host(None, (process,))
    return None


def _limit_or_default(limit: float | None, default: float | None) -> float | None:
    return limit if limit is not None and math.isfinite(limit) and limit > 0 else default


def _read_configured_limit(host: Host, roots: Roots, default: float | None) -> float | None:
    if host.timeout.file is not None:
        try:
            limit = configured_timeout_seconds(host, None, roots)
        except _CONFIG_ERRORS:
            return default
        return _limit_or_default(limit, default)

    limits: list[float] = []
    # Either registration could be active; each uses the host default if it has no override.
    for scope in ("user", "project"):
        try:
            path, reason = config_path(host, roots, scope)
            if path is None or reason is not None:
                continue
            entry = registered_entry(host, path)
            if entry is None:
                continue
            limit = configured_timeout_seconds(host, entry, roots)
        except _CONFIG_ERRORS:
            continue
        effective = _limit_or_default(limit, default)
        if effective is not None:
            limits.append(effective)
    return min(limits, default=default)


def clear_limit_cache() -> None:
    """Forget all cached host limits."""
    with _cache_lock:
        _limit_cache.clear()


def tool_call_limit_seconds(host: Host, roots: Roots | None = None) -> float | None:
    """Read the host's effective tool limit, caching defaults and overrides for 60 seconds."""
    if host.name == "claude-code":
        # The table's 100,000-second default has no practical effect on stdio tool waits.
        return None
    default = host.tool_limit_seconds()
    if host.timeout.key is None or not usable(host.timeout.source):
        return default
    if roots is None:
        try:
            roots = Roots.from_env(os.environ)
        except (OSError, RuntimeError, ValueError):
            return default

    key = (host.name, roots)
    with _cache_lock:
        cached = _limit_cache.get(key)
        if cached is not None and monotonic() < cached[0]:
            return cached[1]
        limit = _read_configured_limit(host, roots, default)
        _limit_cache[key] = (monotonic() + _CACHE_SECONDS, limit)
        return limit


def _wait_cap(limit: float) -> int:
    return max(0, int(limit - 10))


def wait_cap_seconds(host: Host | None, roots: Roots | None = None) -> int | None:
    """Leave a ten-second margin and skip waiting if it uses up the limit."""
    limit = tool_call_limit_seconds(host, roots) if host is not None else _UNKNOWN_LIMIT_SECONDS
    return _wait_cap(limit) if limit is not None else None


def _raise_hint(host: Host, limit: float) -> str:
    spec = host.timeout
    if not spec.raisable:
        return " (this limit is fixed)"
    if spec.key is None or not usable(spec.source):
        return ""
    if spec.needs_raising() and limit < WANTED_TIMEOUT_SECONDS and usable(host.config_path_source("user")):
        return f" (run `agent-bridge install --host {host.name}` to raise the limit)"
    return f" (increase `{spec.key}` in {host.label}'s configuration to raise the limit)"


def cap_wait(requested: int, host: Host | None, roots: Roots | None = None) -> tuple[int, str | None]:
    """Return the wait to use and a reply sentence if it was shortened."""
    limit = tool_call_limit_seconds(host, roots) if host is not None else _UNKNOWN_LIMIT_SECONDS
    if limit is None:
        return requested, None
    cap = _wait_cap(limit)
    if requested <= cap:
        return requested, None
    if host is None:
        return cap, f"This call used a conservative {cap} s wait for an unknown MCP host, and the job is still running."
    hint = _raise_hint(host, limit)
    if cap == 0:
        return 0, f"{host.label}'s {limit:g} s tool-call limit is too short to wait, so the job keeps running{hint}."
    return cap, (
        f"{host.label} stops a tool call after {limit:g} s on this machine, so this call waited {cap} s"
        f" and the job is still running{hint}."
    )
