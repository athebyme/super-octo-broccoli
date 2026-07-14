"""Compatibility adapter around the existing Wildberries client."""

from typing import Callable

from services.wb_api_client import WildberriesAPIClient

from .base import MarketplaceAdapter
from .types import ConnectionCheck, MarketplaceCapability, MarketplaceCredentials


class LegacyWildberriesAdapter(MarketplaceAdapter):
    code = "wb"
    # The registry exposes only methods already available through this adapter.
    # Existing WB services keep their legacy runtime until each capability is
    # wrapped and parity-tested.
    capabilities = frozenset({MarketplaceCapability.CONNECTION_CHECK.value})
    endpoint_versions = {
        "category_tree": "/content/v2/object/all",
        "category_attributes": "/content/v2/object/charcs/{subject_id}",
        "catalog_write": "/content/v2/cards/upload",
    }

    def __init__(
        self,
        client_factory: Callable[..., WildberriesAPIClient] = WildberriesAPIClient,
    ) -> None:
        self._client_factory = client_factory

    def check_connection(
        self,
        credentials: MarketplaceCredentials,
    ) -> ConnectionCheck:
        try:
            response = self._client_factory(
                api_key=credentials.api_key,
            ).get_subjects_list(limit=1, offset=0)
            if not isinstance(response, dict) or response.get("error") is not False:
                raise ValueError("WB rejected the read-only reference probe")
            if not isinstance(response.get("data"), list):
                raise ValueError("WB returned a malformed reference response")
            return ConnectionCheck(
                ok=True,
                status="connected",
                external_account_id=credentials.external_account_id,
                capabilities=(MarketplaceCapability.CONNECTION_CHECK.value,),
            )
        except Exception:
            # Legacy exceptions may contain upstream response text. Keep the new
            # boundary deliberately generic and free of credentials/provider data.
            return ConnectionCheck(
                ok=False,
                status="error",
                external_account_id=credentials.external_account_id,
                error_code="wb_connection_failed",
                error_message="Не удалось проверить подключение Wildberries",
            )
