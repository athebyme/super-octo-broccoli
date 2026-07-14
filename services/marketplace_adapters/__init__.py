"""Typed marketplace adapters and the process-local adapter registry."""

from .base import MarketplaceAdapter, MarketplaceAdapterError
from .registry import MarketplaceRegistry, get_marketplace_registry
from .types import ConnectionCheck, MarketplaceCapability, MarketplaceCredentials

__all__ = [
    "ConnectionCheck",
    "MarketplaceAdapter",
    "MarketplaceAdapterError",
    "MarketplaceCapability",
    "MarketplaceCredentials",
    "MarketplaceRegistry",
    "get_marketplace_registry",
]
