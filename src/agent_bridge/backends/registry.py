"""Central backend adapter registry: lookup by name or alias, quota pools, and job-id shape."""

from __future__ import annotations

import re

from agent_bridge.backends.base import BackendAdapter, QuotaPoolSpec

_SAFE_JOB_ID_RE = re.compile(r"([a-z][a-z0-9_-]*)-[0-9a-f]{8}")


class BackendRegistry:
    """Ordered registry of backend adapters, indexed by canonical name and aliases."""

    def __init__(self) -> None:
        self._adapters: dict[str, BackendAdapter] = {}
        self._aliases: dict[str, str] = {}

    def register(self, adapter: BackendAdapter, *, replace: bool = False) -> None:
        canonical = adapter.name.strip().lower()
        if not canonical or not re.fullmatch(r"[a-z][a-z0-9_-]*", canonical):
            raise ValueError(f"Invalid backend name {adapter.name!r}.")
        if canonical in self._adapters and not replace:
            raise ValueError(f"Backend {canonical!r} is already registered.")
        if canonical in self._adapters:
            self.unregister(canonical)
        alias_keys = adapter.all_alias_keys()
        for key in alias_keys:
            owner = self._aliases.get(key)
            if owner is not None and owner != canonical:
                raise ValueError(
                    f"Alias {key!r} for backend {canonical!r} conflicts with backend {owner!r}."
                )
        self._adapters[canonical] = adapter
        for key in alias_keys:
            self._aliases[key] = canonical

    def unregister(self, name: str) -> BackendAdapter | None:
        canonical = name.strip().lower()
        adapter = self._adapters.pop(canonical, None)
        if adapter is None:
            return None
        for key in [k for k, v in self._aliases.items() if v == canonical]:
            self._aliases.pop(key, None)
        return adapter

    def adapters(self) -> tuple[BackendAdapter, ...]:
        return tuple(self._adapters.values())

    def backend_names(self) -> tuple[str, ...]:
        return tuple(self._adapters.keys())

    def dashboard_backends(self) -> tuple[BackendAdapter, ...]:
        return tuple(
            sorted(
                self._adapters.values(),
                key=lambda adapter: adapter.dashboard_order,
            )
        )

    def quota_pools(self) -> tuple[QuotaPoolSpec, ...]:
        specs: list[QuotaPoolSpec] = []
        for adapter in self._adapters.values():
            specs.extend(adapter.quota_pools)
        specs.sort(key=lambda spec: spec.quota_display_order)
        return tuple(specs)

    def get_optional(self, value: str | None) -> BackendAdapter | None:
        if not value or not value.strip():
            return None
        canonical = self._aliases.get(value.strip().lower())
        if canonical is None:
            return None
        return self._adapters.get(canonical)

    def get(self, value: str) -> BackendAdapter:
        adapter = self.get_optional(value)
        if adapter is not None:
            return adapter
        valid = ", ".join(sorted(self._adapters.keys()))
        non_canonical = [
            alias
            for adapter in self._adapters.values()
            for alias in adapter.aliases
            if "_" not in alias
        ]
        alias_note = f" (aliases: {', '.join(non_canonical)})" if non_canonical else ""
        raise ValueError(f"Unknown backend {value!r}. Expected one of: {valid}{alias_note}.")

    def normalize_backend(self, value: str) -> str:
        return self.get(value).name

    def resolve_backend_and_model_hint(
        self,
        backend: str,
        model: str | None,
    ) -> tuple[str, str | None]:
        adapter = self.get(backend)
        hinted = adapter.resolve_alias_model_hint(backend, model)
        return adapter.name, hinted

    def classify_quota_pool(self, backend: str, model: str | None = None) -> str:
        adapter = self.get_optional(backend)
        if adapter is not None:
            return adapter.classify_quota_pool(model)
        return backend

    def aliases_map(self) -> dict[str, str]:
        return dict(self._aliases)

    def alias_model_hints_map(self) -> dict[str, str]:
        hints: dict[str, str] = {}
        for adapter in self._adapters.values():
            for alias, hint in adapter.alias_model_hints.items():
                hints[alias] = hint
        return hints

    def is_job_id(self, token: str) -> bool:
        match = _SAFE_JOB_ID_RE.fullmatch(token)
        if match is None:
            return False
        return match.group(1) in self._adapters


def build_default_registry() -> BackendRegistry:
    from agent_bridge.backends.agy import AgyAdapter
    from agent_bridge.backends.claude import ClaudeAdapter
    from agent_bridge.backends.codex import CodexAdapter

    registry = BackendRegistry()
    registry.register(AgyAdapter())
    registry.register(CodexAdapter())
    registry.register(ClaudeAdapter())
    return registry


_DEFAULT_REGISTRY: BackendRegistry | None = None


def get_default_registry() -> BackendRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY


def register_adapter(adapter: BackendAdapter, *, replace: bool = False) -> None:
    get_default_registry().register(adapter, replace=replace)


def unregister_adapter(name: str) -> BackendAdapter | None:
    return get_default_registry().unregister(name)


def get_adapter(value: str) -> BackendAdapter:
    return get_default_registry().get(value)


def get_adapter_optional(value: str | None) -> BackendAdapter | None:
    return get_default_registry().get_optional(value)
