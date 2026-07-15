"""Strict transport for the current Ozon Seller API endpoint families.

Ozon uses POST for both reads and writes. This client therefore classifies
retry behavior per endpoint instead of treating every POST equally. Read-only
POST requests may retry bounded transient failures; writes never retry an
ambiguous response automatically.
"""

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import urljoin
import re
import time

import requests
from requests.adapters import HTTPAdapter

from services.marketplace_adapters.types import MarketplaceCredentials


OZON_API_BASE_URL = "https://api-seller.ozon.ru"


@dataclass(frozen=True)
class OzonEndpointSpec:
    path: str
    capability: str
    retry_class: str  # read | write


OZON_ENDPOINTS: Mapping[str, OzonEndpointSpec] = MappingProxyType({
    "roles": OzonEndpointSpec(
        "/v1/roles", "connection_check", "read"
    ),
    "seller_info": OzonEndpointSpec(
        "/v1/seller/info", "connection_check", "read"
    ),
    "product_operation_limits": OzonEndpointSpec(
        "/v4/product/info/limit", "catalog_read", "read"
    ),
    "product_list": OzonEndpointSpec(
        "/v3/product/list", "catalog_read", "read"
    ),
    "product_info_list": OzonEndpointSpec(
        "/v3/product/info/list", "catalog_read", "read"
    ),
    "product_attributes": OzonEndpointSpec(
        "/v4/product/info/attributes", "catalog_read", "read"
    ),
    "product_pictures_info": OzonEndpointSpec(
        "/v2/product/pictures/info", "catalog_read", "read"
    ),
    "description_category_tree": OzonEndpointSpec(
        "/v1/description-category/tree", "reference_categories", "read"
    ),
    "description_category_attributes": OzonEndpointSpec(
        "/v1/description-category/attribute", "reference_attributes", "read"
    ),
    "description_category_attribute_values": OzonEndpointSpec(
        "/v1/description-category/attribute/values",
        "reference_attributes",
        "read",
    ),
    "description_category_attribute_values_search": OzonEndpointSpec(
        "/v1/description-category/attribute/values/search",
        "reference_attributes",
        "read",
    ),
    "product_import": OzonEndpointSpec(
        "/v3/product/import", "catalog_write", "write"
    ),
    "product_import_status": OzonEndpointSpec(
        "/v1/product/import/info", "catalog_read", "read"
    ),
    "product_pictures_import": OzonEndpointSpec(
        "/v1/product/pictures/import", "catalog_write", "write"
    ),
    "product_archive": OzonEndpointSpec(
        "/v1/product/archive", "catalog_write", "write"
    ),
    "product_unarchive": OzonEndpointSpec(
        "/v1/product/unarchive", "catalog_write", "write"
    ),
    "product_prices": OzonEndpointSpec(
        "/v5/product/info/prices", "prices_read", "read"
    ),
    "product_prices_update": OzonEndpointSpec(
        "/v1/product/import/prices", "prices_write", "write"
    ),
    "product_stocks": OzonEndpointSpec(
        "/v4/product/info/stocks", "stocks_read", "read"
    ),
    "product_stocks_by_warehouse_fbs": OzonEndpointSpec(
        "/v2/product/info/stocks-by-warehouse/fbs", "stocks_read", "read"
    ),
    "product_stocks_by_warehouse_fbo": OzonEndpointSpec(
        "/v1/product/info/stocks-by-warehouse/fbo", "stocks_read", "read"
    ),
    "product_stocks_update": OzonEndpointSpec(
        "/v2/products/stocks", "stocks_write", "write"
    ),
    "warehouses": OzonEndpointSpec(
        "/v2/warehouse/list", "warehouses_read", "read"
    ),
    "analytics_data": OzonEndpointSpec(
        "/v1/analytics/data", "analytics_read", "read"
    ),
    "posting_fbs_list": OzonEndpointSpec(
        "/v4/posting/fbs/list", "orders_read", "read"
    ),
    "posting_fbo_list": OzonEndpointSpec(
        "/v3/posting/fbo/list", "orders_read", "read"
    ),
    "returns_list": OzonEndpointSpec(
        "/v1/returns/list", "orders_read", "read"
    ),
    "returns_rfbs_list": OzonEndpointSpec(
        "/v2/returns/rfbs/list", "orders_read", "read"
    ),
    "conditional_cancellation_list": OzonEndpointSpec(
        "/v2/conditional-cancellation/list", "orders_read", "read"
    ),
    "finance_accrual_by_day": OzonEndpointSpec(
        "/v1/finance/accrual/by-day", "finance_read", "read"
    ),
    "finance_accrual_types": OzonEndpointSpec(
        "/v1/finance/accrual/types", "finance_read", "read"
    ),
    "finance_accrual_postings": OzonEndpointSpec(
        "/v1/finance/accrual/postings", "finance_read", "read"
    ),
})


class OzonAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "ozon_api_error",
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
        request_id: Optional[str] = None,
        retriable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after
        self.request_id = request_id
        self.retriable = retriable


class OzonAuthError(OzonAPIError):
    pass


class OzonRateLimitError(OzonAPIError):
    pass


class OzonProtocolError(OzonAPIError):
    pass


class OzonAmbiguousWriteError(OzonAPIError):
    """The provider may have accepted a write; reconcile before any retry."""


class OzonSellerAPIClient:
    BASE_URL = OZON_API_BASE_URL
    REQUEST_ID_HEADERS = (
        "X-Ozon-Trace-Id",
        "X-Ozon-Request-Id",
        "X-Request-Id",
    )
    _SAFE_ERROR_CODE = re.compile(r"[^A-Za-z0-9_.:-]+")

    def __init__(
        self,
        credentials: MarketplaceCredentials,
        *,
        session: Optional[requests.Session] = None,
        timeout: Tuple[float, float] = (5.0, 30.0),
        read_retries: int = 2,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if not credentials.external_account_id:
            raise ValueError("Ozon Client-Id is required")
        if (
            not isinstance(read_retries, int)
            or isinstance(read_retries, bool)
            or not 0 <= read_retries <= 5
        ):
            raise ValueError("read_retries must be an integer between 0 and 5")
        if (
            not isinstance(timeout, tuple)
            or len(timeout) != 2
            or any(not isinstance(value, (int, float)) or value <= 0 for value in timeout)
        ):
            raise ValueError("timeout must be a positive (connect, read) tuple")

        self._credentials = credentials
        self.timeout = (float(timeout[0]), float(timeout[1]))
        self.read_retries = read_retries
        self._sleep = sleep_fn
        self.session = session or self._create_session()
        self.session.headers.update({
            "Client-Id": credentials.external_account_id,
            "Api-Key": credentials.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    @staticmethod
    def _create_session() -> requests.Session:
        session = requests.Session()
        # HTTPAdapter is used only for connection pooling. urllib3 retries stay
        # disabled because retry safety is decided from the endpoint manifest.
        adapter = HTTPAdapter(max_retries=0, pool_connections=10, pool_maxsize=20)
        session.mount("https://", adapter)
        return session

    def _redact(self, value: Any, *, maximum: int = 500) -> str:
        text = str(value or "")
        for secret in (
            self._credentials.api_key,
            self._credentials.external_account_id,
        ):
            if secret:
                text = text.replace(secret, "[redacted]")
        text = " ".join(text.replace("\x00", " ").split())
        return text[:maximum]

    @classmethod
    def _request_id(cls, response: Any) -> Optional[str]:
        headers = getattr(response, "headers", {}) or {}
        for header in cls.REQUEST_ID_HEADERS:
            value = headers.get(header)
            if value:
                return str(value)[:200]
        return None

    @staticmethod
    def _retry_after(response: Any) -> Optional[float]:
        raw = (getattr(response, "headers", {}) or {}).get("Retry-After")
        if raw in (None, ""):
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(raw))
                now = parsedate_to_datetime(
                    (getattr(response, "headers", {}) or {}).get("Date", "")
                )
                return max(0.0, (retry_at - now).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def _response_json(self, response: Any) -> Optional[Dict[str, Any]]:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _provider_error(
        self,
        response: Any,
        *,
        default_code: str,
        default_message: str,
        retriable: bool = False,
    ) -> OzonAPIError:
        payload = self._response_json(response) or {}
        raw_code = payload.get("code") or payload.get("error") or default_code
        code = self._SAFE_ERROR_CODE.sub("_", str(raw_code))[:100] or default_code
        raw_message = payload.get("message") or payload.get("error_message")
        message = self._redact(raw_message or default_message)
        return OzonAPIError(
            message,
            code=code,
            status_code=getattr(response, "status_code", None),
            retry_after=self._retry_after(response),
            request_id=self._request_id(response),
            retriable=retriable,
        )

    @staticmethod
    def _validate_payload(payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Ozon request payload must be an object")
        forbidden = {"api_key", "api-key", "client_id", "client-id"}
        if any(str(key).strip().lower() in forbidden for key in payload):
            raise ValueError("Ozon credentials must be sent only as headers")
        return payload

    def request(self, endpoint_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        spec = OZON_ENDPOINTS.get(endpoint_name)
        if spec is None:
            raise ValueError(f"Unsupported Ozon endpoint: {endpoint_name}")
        payload = self._validate_payload(payload)
        max_attempts = self.read_retries + 1 if spec.retry_class == "read" else 1

        for attempt in range(max_attempts):
            try:
                response = self.session.request(
                    "POST",
                    urljoin(self.BASE_URL, spec.path),
                    json=payload,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except requests.RequestException:
                if spec.retry_class == "read" and attempt + 1 < max_attempts:
                    self._sleep(min(2 ** attempt, 5))
                    continue
                if spec.retry_class == "write":
                    raise OzonAmbiguousWriteError(
                        "Результат операции Ozon неизвестен; требуется сверка перед повтором",
                        code="ozon_ambiguous_transport",
                        retriable=False,
                    ) from None
                raise OzonAPIError(
                    "Ozon временно недоступен",
                    code="ozon_transport_error",
                    retriable=True,
                ) from None

            status_code = int(getattr(response, "status_code", 0) or 0)
            request_id = self._request_id(response)
            retry_after = self._retry_after(response)

            if status_code in (401, 403):
                error = self._provider_error(
                    response,
                    default_code="ozon_auth_error",
                    default_message="Ozon отклонил Client-Id или API key",
                )
                raise OzonAuthError(
                    str(error),
                    code=error.code,
                    status_code=status_code,
                    request_id=request_id,
                )

            if status_code == 429:
                if spec.retry_class == "read" and attempt + 1 < max_attempts:
                    self._sleep(min(retry_after if retry_after is not None else 2 ** attempt, 30))
                    continue
                raise OzonRateLimitError(
                    "Ozon временно ограничил частоту запросов",
                    code="ozon_rate_limited",
                    status_code=status_code,
                    retry_after=retry_after,
                    request_id=request_id,
                    retriable=spec.retry_class == "read",
                )

            if status_code >= 500 or status_code <= 0:
                if spec.retry_class == "read" and attempt + 1 < max_attempts:
                    self._sleep(min(2 ** attempt, 5))
                    continue
                if spec.retry_class == "write":
                    raise OzonAmbiguousWriteError(
                        "Результат операции Ozon неизвестен; требуется сверка перед повтором",
                        code="ozon_ambiguous_provider_error",
                        status_code=status_code or None,
                        request_id=request_id,
                    )
                raise self._provider_error(
                    response,
                    default_code="ozon_server_error",
                    default_message="Ozon временно недоступен",
                    retriable=True,
                )

            if not 200 <= status_code < 300:
                raise self._provider_error(
                    response,
                    default_code="ozon_request_rejected",
                    default_message="Ozon отклонил запрос",
                )

            result = self._response_json(response)
            if result is None:
                if spec.retry_class == "write":
                    raise OzonAmbiguousWriteError(
                        "Ozon принял запрос, но вернул некорректный ответ; требуется сверка",
                        code="ozon_ambiguous_response",
                        status_code=status_code,
                        request_id=request_id,
                    )
                raise OzonProtocolError(
                    "Ozon вернул некорректный JSON-объект",
                    code="ozon_invalid_response",
                    status_code=status_code,
                    request_id=request_id,
                )
            return result

        raise AssertionError("unreachable Ozon request state")

    def get_product_operation_limits(self) -> Dict[str, Any]:
        return self.request("product_operation_limits", {})

    def get_analytics_data(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("analytics_data", payload)

    def get_fbs_postings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("posting_fbs_list", payload)

    def get_fbo_postings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("posting_fbo_list", payload)

    def get_returns(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("returns_list", payload)

    def get_rfbs_returns(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("returns_rfbs_list", payload)

    def get_conditional_cancellations(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.request("conditional_cancellation_list", payload)

    def get_finance_accrual_by_day(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.request("finance_accrual_by_day", payload)

    def get_finance_accrual_types(
        self,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.request("finance_accrual_types", payload or {})

    def get_finance_accrual_postings(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.request("finance_accrual_postings", payload)

    def get_product_list(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_list", payload)

    def get_product_info_list(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_info_list", payload)

    def get_product_attributes(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_attributes", payload)

    def get_product_pictures(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_pictures_info", payload)

    def get_roles(self) -> Dict[str, Any]:
        return self.request("roles", {})

    def get_seller_info(self) -> Dict[str, Any]:
        return self.request("seller_info", {})

    def get_description_category_tree(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.request("description_category_tree", payload)

    def get_description_category_attributes(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.request("description_category_attributes", payload)

    def get_description_category_attribute_values(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.request("description_category_attribute_values", payload)

    def search_description_category_attribute_values(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.request(
            "description_category_attribute_values_search",
            payload,
        )

    def submit_products(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_import", payload)

    def submit_product_pictures(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_pictures_import", payload)

    def archive_products(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_archive", payload)

    def unarchive_products(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_unarchive", payload)

    def get_product_import_status(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_import_status", payload)

    def get_prices(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_prices", payload)

    def update_prices(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_prices_update", payload)

    def get_stocks(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_stocks", payload)

    def get_stocks_by_warehouse_fbs(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.request("product_stocks_by_warehouse_fbs", payload)

    def get_stocks_by_warehouse_fbo(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.request("product_stocks_by_warehouse_fbo", payload)

    def update_stocks(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("product_stocks_update", payload)

    def get_warehouses(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.request("warehouses", payload or {})
