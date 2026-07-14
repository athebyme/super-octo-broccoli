"""Marketplace adapter boundary."""

from abc import ABC, abstractmethod
from typing import Any, Dict, FrozenSet, Mapping

from .types import ConnectionCheck, MarketplaceCredentials


class MarketplaceAdapterError(RuntimeError):
    """A marketplace adapter is missing or does not support a capability."""


class MarketplaceAdapter(ABC):
    """Provider-specific behavior behind a marketplace-neutral contract.

    Adapters never authorize sellers and never receive SQLAlchemy models.
    """

    code: str
    capabilities: FrozenSet[str]
    endpoint_versions: Mapping[str, str]

    @abstractmethod
    def check_connection(
        self,
        credentials: MarketplaceCredentials,
    ) -> ConnectionCheck:
        """Perform one bounded, read-only credential check."""

    def require_capability(self, capability: str) -> None:
        if capability not in self.capabilities:
            raise MarketplaceAdapterError(
                f"Marketplace {self.code!r} does not support {capability!r}"
            )

    def public_manifest(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "capabilities": sorted(self.capabilities),
            "endpoint_versions": dict(self.endpoint_versions),
        }
