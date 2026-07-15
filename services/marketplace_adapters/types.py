"""Stable DTOs shared by marketplace adapters.

These types deliberately contain no ORM objects. Tenant authorization happens in
the service layer before an adapter receives credentials or external identities.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple


class MarketplaceCapability(str, Enum):
    CONNECTION_CHECK = "connection_check"
    REFERENCE_CATEGORIES = "reference_categories"
    REFERENCE_ATTRIBUTES = "reference_attributes"
    CATALOG_READ = "catalog_read"
    CATALOG_WRITE = "catalog_write"
    PRICES_READ = "prices_read"
    PRICES_WRITE = "prices_write"
    STOCKS_READ = "stocks_read"
    STOCKS_WRITE = "stocks_write"
    WAREHOUSES_READ = "warehouses_read"
    ORDERS_READ = "orders_read"
    ANALYTICS_READ = "analytics_read"
    FINANCE_READ = "finance_read"
    REVIEWS_READ = "reviews_read"
    REVIEWS_WRITE = "reviews_write"
    QUESTIONS_READ = "questions_read"


def _validate_credential_part(
    value: Any,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not minimum <= len(normalized) <= maximum:
        raise ValueError(
            f"{field_name} length must be between {minimum} and {maximum}"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} contains control characters")
    return normalized


@dataclass(frozen=True)
class MarketplaceCredentials:
    """In-memory credentials passed to an adapter for one bounded call."""

    api_key: str = field(repr=False)
    external_account_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "api_key",
            _validate_credential_part(
                self.api_key,
                "api_key",
                minimum=1,
                maximum=2000,
            ),
        )
        if self.external_account_id:
            object.__setattr__(
                self,
                "external_account_id",
                _validate_credential_part(
                    self.external_account_id,
                    "external_account_id",
                    minimum=1,
                    maximum=200,
                ),
            )


@dataclass(frozen=True)
class ConnectionCheck:
    """Sanitized result of a read-only marketplace credential probe."""

    ok: bool
    status: str
    external_account_id: str
    capabilities: Tuple[str, ...] = ()
    roles: Tuple[str, ...] = ()
    expires_at: Optional[datetime] = None
    provider_request_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "external_account_id": self.external_account_id,
            "capabilities": list(self.capabilities),
            "roles": list(self.roles),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "provider_request_id": self.provider_request_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": dict(self.metadata),
        }
