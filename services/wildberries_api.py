"""
Utility helpers for interacting with the Wildberries supplier API.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import os

import requests


DEFAULT_CARDS_ENDPOINT = "https://suppliers-api.wildberries.ru/content/v2/get/cards/list"
DEFAULT_TIMEOUT = int(os.getenv("WB_API_TIMEOUT", "30"))


class WildberriesAPIError(RuntimeError):
    """Raised when the Wildberries API responds with an error payload or status."""


@dataclass
class CardsQuery:
    api_key: str
    limit: int = 50
    search: Optional[str] = None
    updated_at: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        cursor_limit = max(1, min(self.limit, 1_000))
        payload: Dict[str, Any] = {
            "settings": {
                "cursor": {
                    "limit": cursor_limit,
                }
            }
        }
        filter_block: Dict[str, Any] = {}
        if self.search:
            filter_block["textSearch"] = self.search
        if self.updated_at:
            payload["settings"]["cursor"]["updatedAt"] = self.updated_at
        if filter_block:
            payload["settings"]["filter"] = filter_block
        return payload


def _request_json(url: str, *, api_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    response = requests.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
    try:
        data = response.json()
    except ValueError as exc:  # pragma: no cover - defensive
        raise WildberriesAPIError(
            f"WB API вернул ответ, который не удалось разобрать как JSON (status {response.status_code})."
        ) from exc

    if response.status_code >= 400 or data.get("error"):
        message = data.get("errorText") or response.text
        raise WildberriesAPIError(message.strip() or "Неизвестная ошибка WB API.")

    return data


def list_cards(
    api_key: str,
    *,
    limit: int = 50,
    search: Optional[str] = None,
    updated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fetch a batch of product cards for the authenticated seller.

    :param api_key: WB supplier API token (`Authorization` header)
    :param limit: number of cards to request (1..1000)
    :param search: optional full-text search string
    :param updated_at: optional cursor value for pagination
    """
    if not api_key:
        raise WildberriesAPIError("API токен не передан.")

    query = CardsQuery(api_key=api_key, limit=limit, search=search, updated_at=updated_at)
    payload = query.to_payload()

    endpoint = os.getenv("WB_CONTENT_CARDS_ENDPOINT", DEFAULT_CARDS_ENDPOINT)
    return _request_json(endpoint, api_key=api_key, payload=payload)
