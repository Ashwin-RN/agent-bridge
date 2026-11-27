"""Backend adapter interface, registry, and built-in CLI adapters."""

from __future__ import annotations

from agent_bridge.backends.base import (
    AdapterRunner,
    BackendAdapter,
    BackendDefaults,
    Capabilities,
    CommandSpec,
    ParsedStream,
    QuotaPoolSpec,
)
from agent_bridge.backends.registry import (
    BackendRegistry,
    build_default_registry,
    get_adapter,
    get_adapter_optional,
    get_default_registry,
    register_adapter,
    unregister_adapter,
)

__all__ = [
    "AdapterRunner",
    "BackendAdapter",
    "BackendDefaults",
    "BackendRegistry",
    "Capabilities",
    "CommandSpec",
    "ParsedStream",
    "QuotaPoolSpec",
    "build_default_registry",
    "get_adapter",
    "get_adapter_optional",
    "get_default_registry",
    "register_adapter",
    "unregister_adapter",
]
