"""Explicit marketplace adapter registry.

No module scans or dynamic imports are used: adding a provider is a reviewed code
change and its capabilities remain deterministic.
"""

from threading import Lock
from typing import Dict, Iterable

from .base import MarketplaceAdapter, MarketplaceAdapterError


class MarketplaceRegistry:
    def __init__(self, adapters: Iterable[MarketplaceAdapter] = ()) -> None:
        self._adapters: Dict[str, MarketplaceAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: MarketplaceAdapter) -> None:
        code = str(getattr(adapter, "code", "")).strip().lower()
        if not code:
            raise MarketplaceAdapterError("Marketplace adapter code is required")
        if code in self._adapters:
            raise MarketplaceAdapterError(
                f"Marketplace adapter {code!r} is already registered"
            )
        self._adapters[code] = adapter

    def get(self, code: str) -> MarketplaceAdapter:
        normalized = str(code or "").strip().lower()
        adapter = self._adapters.get(normalized)
        if adapter is None:
            raise MarketplaceAdapterError(
                f"Marketplace adapter {normalized or '<empty>'!r} is unavailable"
            )
        return adapter

    def manifests(self) -> Dict[str, dict]:
        return {
            code: self._adapters[code].public_manifest()
            for code in sorted(self._adapters)
        }


_registry = None
_registry_lock = Lock()


def get_marketplace_registry() -> MarketplaceRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                from .ozon import OzonAdapter
                from .wb import LegacyWildberriesAdapter

                _registry = MarketplaceRegistry((
                    LegacyWildberriesAdapter(),
                    OzonAdapter(),
                ))
    return _registry
