"""Ozon marketplace adapter."""

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from services.ozon_api_client import (
    OZON_ENDPOINTS,
    OzonAPIError,
    OzonAuthError,
    OzonSellerAPIClient,
)

from .base import MarketplaceAdapter
from .types import ConnectionCheck, MarketplaceCapability, MarketplaceCredentials


class OzonAdapter(MarketplaceAdapter):
    code = "ozon"
    capabilities = frozenset({
        capability.value
        for capability in (
            MarketplaceCapability.CONNECTION_CHECK,
            MarketplaceCapability.REFERENCE_CATEGORIES,
            MarketplaceCapability.REFERENCE_ATTRIBUTES,
            MarketplaceCapability.CATALOG_READ,
            MarketplaceCapability.CATALOG_WRITE,
            MarketplaceCapability.PRICES_READ,
            MarketplaceCapability.PRICES_WRITE,
            MarketplaceCapability.STOCKS_READ,
            MarketplaceCapability.STOCKS_WRITE,
            MarketplaceCapability.WAREHOUSES_READ,
            MarketplaceCapability.ANALYTICS_READ,
        )
    })
    endpoint_versions = {
        endpoint_name: spec.path
        for endpoint_name, spec in OZON_ENDPOINTS.items()
    }

    def __init__(
        self,
        client_factory: Callable[..., OzonSellerAPIClient] = OzonSellerAPIClient,
    ) -> None:
        self._client_factory = client_factory

    def _client(self, credentials: MarketplaceCredentials) -> OzonSellerAPIClient:
        return self._client_factory(credentials)

    def check_connection(
        self,
        credentials: MarketplaceCredentials,
    ) -> ConnectionCheck:
        try:
            response = self._client(credentials).get_roles()
            roles = self._role_names(response)
            return ConnectionCheck(
                ok=True,
                status="connected",
                external_account_id=credentials.external_account_id,
                capabilities=(MarketplaceCapability.CONNECTION_CHECK.value,),
                roles=roles,
                expires_at=self._expires_at(response.get("expires_at")),
            )
        except OzonAuthError as exc:
            return ConnectionCheck(
                ok=False,
                status="invalid",
                external_account_id=credentials.external_account_id,
                provider_request_id=exc.request_id,
                error_code="ozon_auth_error",
                error_message="Ozon отклонил Client-Id или API key",
            )
        except OzonAPIError as exc:
            return ConnectionCheck(
                ok=False,
                status="error",
                external_account_id=credentials.external_account_id,
                provider_request_id=exc.request_id,
                error_code=exc.code,
                error_message=str(exc),
            )
        except (TypeError, ValueError):
            return ConnectionCheck(
                ok=False,
                status="error",
                external_account_id=credentials.external_account_id,
                error_code="ozon_connection_check_failed",
                error_message="Не удалось проверить подключение Ozon",
            )

    @staticmethod
    def _role_names(response: Dict[str, Any]) -> Tuple[str, ...]:
        raw_roles = response.get("roles")
        if raw_roles is None:
            raw_roles = response.get("result")
        if not isinstance(raw_roles, list):
            return ()
        roles = []
        seen = set()
        for item in raw_roles[:200]:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                name = item["name"].strip()
            else:
                continue
            key = name.casefold()
            if name and len(name) <= 200 and key not in seen:
                seen.add(key)
                roles.append(name)
        return tuple(sorted(roles, key=str.casefold))

    @staticmethod
    def _expires_at(value: Any) -> Optional[datetime]:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    def fetch_category_tree(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_description_category_tree(payload)

    def fetch_attribute_schema(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_description_category_attributes(payload)

    def fetch_attribute_values(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_description_category_attribute_values(payload)

    def get_operation_limits(
        self,
        credentials: MarketplaceCredentials,
    ) -> Dict[str, Any]:
        return self._client(credentials).get_product_operation_limits()

    def list_products(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_product_list(payload)

    def get_products(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_product_info_list(payload)

    def get_product_attributes(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_product_attributes(payload)

    def get_product_pictures(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_product_pictures(payload)

    def submit_products(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).submit_products(payload)

    def submit_product_pictures(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).submit_product_pictures(payload)

    def archive_products(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).archive_products(payload)

    def unarchive_products(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).unarchive_products(payload)

    def get_submission(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_product_import_status(payload)

    def read_prices(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_prices(payload)

    def update_prices(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).update_prices(payload)

    def read_stocks(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_stocks(payload)

    def read_stocks_by_warehouse_fbs(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_stocks_by_warehouse_fbs(payload)

    def read_stocks_by_warehouse_fbo(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_stocks_by_warehouse_fbo(payload)

    def update_stocks(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).update_stocks(payload)

    def read_warehouses(
        self,
        credentials: MarketplaceCredentials,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._client(credentials).get_warehouses(payload)

    def read_analytics(
        self,
        credentials: MarketplaceCredentials,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._client(credentials).get_analytics_data(payload)
