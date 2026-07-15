# -*- coding: utf-8 -*-
"""
HTTP-клиент для Internal API платформы.

Обеспечивает:
- Аутентификацию через X-Agent-Id / X-Agent-Key
- Heartbeat
- Получение и обновление задач
- Логирование шагов (thinking / action / result)
- Доступ к данным продавцов и товаров
"""
import time
import logging
from typing import Optional

import requests
import urllib3

from .config import AgentConfig

logger = logging.getLogger(__name__)


class ReferenceDataUnavailableError(RuntimeError):
    """Raised locally when an agent would otherwise use stale WB metadata."""

    def __init__(self, resource: str, payload: dict):
        self.resource = resource
        self.payload = payload if isinstance(payload, dict) else {}
        self.reference_status = self.payload.get('reference_status') or {}
        message = (
            self.payload.get('warning')
            or self.reference_status.get('error')
            or f'WB reference data is unavailable: {resource}'
        )
        super().__init__(str(message)[:500])


def require_usable_reference(payload: dict, resource: str) -> dict:
    """Validate the typed reference contract before an agent uses its data."""
    status = payload.get('reference_status') if isinstance(payload, dict) else None
    if not isinstance(status, dict) or status.get('usable') is not True:
        raise ReferenceDataUnavailableError(resource, payload)
    return payload


def _validated_product_ids(product_ids, limit: int = 200) -> list[int]:
    """Return the complete typed selection or fail instead of clipping it."""
    if not isinstance(product_ids, (list, tuple)) or not product_ids:
        raise ValueError('product_ids must be a non-empty list')
    if len(product_ids) > limit:
        raise ValueError(f'Maximum {limit} product_ids per request')
    result = []
    seen = set()
    for index, raw in enumerate(product_ids):
        if isinstance(raw, bool):
            raise ValueError(f'product_ids[{index}] must be a positive integer')
        if isinstance(raw, int):
            product_id = raw
        elif isinstance(raw, str) and raw.strip().isdigit() and int(raw) > 0:
            product_id = int(raw)
        else:
            raise ValueError(f'product_ids[{index}] must be a positive integer')
        if product_id <= 0:
            raise ValueError(f'product_ids[{index}] must be a positive integer')
        if product_id in seen:
            raise ValueError(f'Duplicate product_id: {product_id}')
        result.append(product_id)
        seen.add(product_id)
    return result


def _validated_marketplace_listing_ids(listing_ids, limit: int = 200) -> list[int]:
    """Marketplace IDs are already canonical JSON integers; never coerce them."""
    if not isinstance(listing_ids, (list, tuple)) or not listing_ids:
        raise ValueError('listing_ids must be a non-empty list')
    if len(listing_ids) > limit:
        raise ValueError(f'Maximum {limit} listing_ids per request')
    result = []
    seen = set()
    for index, raw in enumerate(listing_ids):
        if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
            raise ValueError(f'listing_ids[{index}] must be a positive integer')
        if raw in seen:
            raise ValueError(f'Duplicate listing_id: {raw}')
        result.append(raw)
        seen.add(raw)
    return result


class PlatformClient:
    """Клиент для Internal API v1."""

    def __init__(self, config: AgentConfig = None):
        self.cfg = config or AgentConfig
        self.base_url = self.cfg.PLATFORM_URL.rstrip('/')
        self.session = requests.Session()

        # TLS верификация: отключена по умолчанию для Docker inter-service,
        # управляется через PLATFORM_SKIP_TLS_VERIFY (0 — включить верификацию).
        skip_tls = bool(self.cfg.PLATFORM_SKIP_TLS_VERIFY)
        self.session.verify = not skip_tls
        if skip_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self.session.headers.update({
            'X-Agent-Id': self.cfg.AGENT_ID,
            'X-Agent-Key': self.cfg.AGENT_API_KEY,
            'Content-Type': 'application/json',
        })
        self._current_task_id: Optional[str] = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}/internal/v1{path}"

    def set_task_id(self, task_id: Optional[str]):
        """Устанавливает текущий task_id для передачи в X-Task-Id заголовке."""
        self._current_task_id = task_id

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Выполняет запрос с retry на сетевые и timeout ошибки."""
        url = self._url(path)
        if self._current_task_id:
            headers = kwargs.pop('headers', {})
            headers['X-Task-Id'] = self._current_task_id
            kwargs['headers'] = headers
        last_error = None
        for attempt in range(4):
            try:
                resp = self.session.request(method, url, timeout=90, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ReadTimeout,
                    requests.exceptions.Timeout) as e:
                last_error = e
                wait = 2 ** (attempt + 1)
                logger.warning(f"Request error (attempt {attempt+1}/4), retry in {wait}s: {e}")
                time.sleep(wait)
            except requests.exceptions.HTTPError as e:
                logger.error(f"HTTP error {resp.status_code}: {resp.text}")
                raise
        raise ConnectionError(f"Failed after 4 attempts: {last_error}")

    # ── Heartbeat ──────────────────────────────────────────────────

    def heartbeat(self, status: str = 'online', error: str = None) -> dict:
        payload = {'status': status}
        if error:
            payload['error'] = error
        return self._request('POST', '/heartbeat', json=payload)

    # ── Задачи ─────────────────────────────────────────────────────

    def poll_tasks(self, limit: int = 5) -> list:
        """Получает очередь задач."""
        data = self._request('GET', f'/tasks/poll?limit={limit}')
        return data.get('tasks', [])

    def start_task(self, task_id: str) -> dict:
        return self._request('POST', f'/tasks/{task_id}/start')

    def update_progress(self, task_id: str, completed_steps: int,
                        current_step_label: str = None,
                        total_steps: int = None) -> dict:
        payload = {'completed_steps': completed_steps}
        if current_step_label:
            payload['current_step_label'] = current_step_label
        if total_steps is not None:
            payload['total_steps'] = total_steps
        return self._request('POST', f'/tasks/{task_id}/progress', json=payload)

    def update_checkpoint(self, task_id: str, checkpoint: dict) -> dict:
        return self._request(
            'POST', f'/tasks/{task_id}/checkpoint',
            json={'checkpoint': checkpoint or {}},
        )

    def complete_task(self, task_id: str, result: dict = None) -> dict:
        return self._request('POST', f'/tasks/{task_id}/complete',
                             json={'result': result or {}})

    def fail_task(self, task_id: str, error: str, result: dict = None) -> dict:
        payload = {'error': error}
        if result:
            payload['result'] = result
        return self._request('POST', f'/tasks/{task_id}/fail', json=payload)

    def get_task_ai_config(self, task_id: str) -> dict:
        """Получает seller AI profile для принадлежащей агенту задачи."""
        data = self._request('GET', f'/tasks/{task_id}/ai-config')
        return data.get('ai_config', {})

    # ── Шаги ───────────────────────────────────────────────────────

    def log_step(self, task_id: str, step_type: str, title: str,
                 detail: str = None, duration_ms: int = None,
                 metadata: dict = None) -> dict:
        """Логирует шаг выполнения задачи."""
        payload = {
            'step_type': step_type,
            'title': title,
        }
        if detail:
            payload['detail'] = detail
        if duration_ms is not None:
            payload['duration_ms'] = duration_ms
        if metadata:
            payload['metadata'] = metadata
        return self._request('POST', f'/tasks/{task_id}/steps', json=payload)

    def log_thinking(self, task_id: str, title: str, detail: str = None,
                     duration_ms: int = None) -> dict:
        return self.log_step(task_id, 'thinking', title, detail, duration_ms)

    def log_action(self, task_id: str, title: str, detail: str = None,
                   duration_ms: int = None) -> dict:
        return self.log_step(task_id, 'action', title, detail, duration_ms)

    def log_decision(self, task_id: str, title: str, detail: str = None,
                     duration_ms: int = None) -> dict:
        return self.log_step(task_id, 'decision', title, detail, duration_ms)

    def log_result(self, task_id: str, title: str, detail: str = None,
                   duration_ms: int = None) -> dict:
        return self.log_step(task_id, 'result', title, detail, duration_ms)

    def log_error(self, task_id: str, title: str, detail: str = None,
                  duration_ms: int = None) -> dict:
        return self.log_step(task_id, 'error', title, detail, duration_ms)

    # ── Данные продавцов ───────────────────────────────────────────

    def get_seller(self, seller_id: int) -> dict:
        data = self._request('GET', f'/sellers/{seller_id}')
        return data.get('seller', {})

    def get_product_defaults(self, seller_id: int,
                             subject_id: int = None) -> dict:
        params = {'subject_id': subject_id} if subject_id is not None else None
        return self._request(
            'GET', f'/sellers/{seller_id}/product-defaults', params=params,
        )

    def get_api_connection_status(self, seller_id: int) -> dict:
        return self._request(
            'GET', f'/sellers/{seller_id}/api-connection-status',
        )

    def get_api_logs(self, seller_id: int, limit: int = 20) -> dict:
        return self._request(
            'GET', f'/sellers/{seller_id}/api-logs',
            params={'limit': min(max(int(limit), 1), 50)},
        )

    def search_knowledge(self, seller_id: int, query: str, limit: int = 6,
                         max_chars: int = 6000) -> dict:
        """Retrieve bounded, cited unstructured guidance for the current task."""
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 500:
            raise ValueError('query must contain 2..500 characters')
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError('limit must be an integer')
        if not isinstance(max_chars, int) or isinstance(max_chars, bool):
            raise ValueError('max_chars must be an integer')
        return self._request(
            'POST', f'/sellers/{seller_id}/knowledge/search',
            json={
                'query': query.strip(),
                'limit': min(max(limit, 1), 8),
                'max_chars': min(max(max_chars, 500), 6000),
            },
        )

    def resolve_supplier(self, seller_id: int, query: str) -> dict:
        return self._request(
            'GET', f'/sellers/{seller_id}/suppliers/resolve',
            params={'q': query},
        )

    def get_ready_supplier_candidates(self, seller_id: int, supplier_id: int,
                                      limit: int = 60) -> dict:
        return self._request(
            'GET',
            f'/sellers/{seller_id}/suppliers/{supplier_id}/ready-candidates',
            params={'limit': min(max(int(limit), 1), 100)},
        )

    def audit_supplier_imported_products(self, seller_id: int, supplier_id: int,
                                         focus_limit: int = 100) -> dict:
        return self._request(
            'GET',
            f'/sellers/{seller_id}/suppliers/{supplier_id}/imported-audit',
            params={'focus_limit': min(max(int(focus_limit), 1), 200)},
        )

    def get_products_content_brief(self, seller_id: int, entity_kind: str,
                                   product_ids: list[int]) -> dict:
        product_ids = _validated_product_ids(product_ids)
        return self._request(
            'POST', f'/sellers/{seller_id}/products/content-brief',
            json={
                'entity_kind': entity_kind,
                'product_ids': product_ids,
            },
        )

    def audit_product_batch(self, seller_id: int, entity_kind: str,
                            product_ids: list[int], focus_limit: int = 100) -> dict:
        product_ids = _validated_product_ids(product_ids)
        return self._request(
            'POST', f'/sellers/{seller_id}/products/audit-batch',
            json={
                'entity_kind': entity_kind,
                'product_ids': product_ids,
                'focus_limit': min(max(int(focus_limit), 1), 200),
            },
        )

    def get_marketplace_listing_brief(
        self,
        seller_id: int,
        marketplace_code: str,
        account_id: int,
        listing_ids: list[int],
        focus_limit: int = 100,
    ) -> dict:
        listing_ids = _validated_marketplace_listing_ids(listing_ids)
        if not isinstance(marketplace_code, str) or not marketplace_code.strip():
            raise ValueError('marketplace_code is required')
        if not isinstance(account_id, int) or isinstance(account_id, bool) or account_id <= 0:
            raise ValueError('account_id must be a positive integer')
        if (
            not isinstance(focus_limit, int)
            or isinstance(focus_limit, bool)
            or not 1 <= focus_limit <= 200
        ):
            raise ValueError('focus_limit must be an integer from 1 to 200')
        return self._request(
            'POST', f'/sellers/{seller_id}/marketplace-listings/brief',
            json={
                'marketplace_code': marketplace_code.strip().lower(),
                'account_id': account_id,
                'listing_ids': listing_ids,
                'focus_limit': focus_limit,
            },
        )

    def list_products(self, seller_id: int, page: int = 1,
                      per_page: int = 50, status: str = None) -> dict:
        params = f'?page={page}&per_page={per_page}'
        if status:
            params += f'&status={status}'
        return self._request('GET', f'/sellers/{seller_id}/products{params}')

    def get_product(self, seller_id: int, product_id: int) -> dict:
        data = self._request('GET', f'/sellers/{seller_id}/products/{product_id}')
        return data.get('product', {})

    def query_products(self, seller_id: int, **filters) -> dict:
        allowed = {
            key: value for key, value in filters.items()
            if key in {'active', 'stock_state', 'quality_max', 'limit'} and value is not None
        }
        return self._request(
            'GET', f'/sellers/{seller_id}/products/query', params=allowed,
        )

    def get_card_quality_brief(self, seller_id: int, product_ids=None,
                               reason: str = None, limit: int = 30) -> dict:
        """Качество карточек: причины, impact, воронка (read-only, до 50)."""
        params = {'limit': min(int(limit or 30), 50)}
        if reason:
            params['reason'] = reason
        payload = {}
        if product_ids:
            payload['product_ids'] = _validated_product_ids(product_ids, 50)
        return self._request(
            'POST', f'/sellers/{seller_id}/products/quality-brief',
            params=params, json=payload)

    def update_product(self, seller_id: int, product_id: int,
                       updates: dict) -> dict:
        return self._request('PATCH',
                             f'/sellers/{seller_id}/products/{product_id}',
                             json=updates)

    def batch_update_products(self, seller_id: int,
                              updates: list[dict]) -> dict:
        """Update main WB cards in server-sized chunks without dropping items."""
        if not isinstance(updates, list):
            raise ValueError('updates must be a list')
        if not updates:
            return {
                'ok': True, 'updated': 0, 'unchanged': 0,
                'failed': 0, 'results': [],
            }
        product_ids = []
        for index, item in enumerate(updates):
            if not isinstance(item, dict):
                raise ValueError(f'updates[{index}] must be an object')
            product_ids.append(item.get('product_id'))
        _validated_product_ids(product_ids, limit=max(len(product_ids), 1))

        all_results = []
        totals = {'updated': 0, 'unchanged': 0, 'failed': 0}
        for index in range(0, len(updates), 50):
            response = self._request(
                'PATCH', f'/sellers/{seller_id}/products/batch',
                json={'updates': updates[index:index + 50]},
            )
            all_results.extend(response.get('results') or [])
            for key in totals:
                totals[key] += int(response.get(key) or 0)
        return {
            'ok': totals['failed'] == 0,
            **totals,
            'results': all_results,
        }

    def list_imported_products(self, seller_id: int, page: int = 1,
                               per_page: int = 50) -> dict:
        return self._request(
            'GET',
            f'/sellers/{seller_id}/imported-products?page={page}&per_page={per_page}'
        )

    def query_imported_products(self, seller_id: int, **filters) -> dict:
        allowed = {
            key: value for key, value in filters.items()
            if key in {
                'price_min', 'price_max', 'quantity_min', 'quantity_max',
                'stock_state', 'missing_field', 'import_status', 'published',
                'vendor_code', 'limit',
            } and value is not None
        }
        return self._request(
            'GET', f'/sellers/{seller_id}/imported-products/query', params=allowed,
        )

    def get_imported_product(self, product_id: int) -> dict:
        return self._request('GET', f'/imported-products/{product_id}')

    def update_imported_product(self, product_id: int, updates: dict) -> dict:
        return self._request('PATCH', f'/imported-products/{product_id}',
                             json=updates)

    def get_imported_products_brief(self, product_ids: list,
                                    include_description: bool = False) -> list:
        """Получает краткие данные товаров пакетно (только id, title, brand, category)."""
        data = self._request('POST', '/imported-products/brief',
                             json={
                                 'product_ids': product_ids,
                                 'include_description': bool(include_description),
                             })
        return data.get('products', [])

    def batch_update_imported_products(self, updates: list[dict]) -> dict:
        """Пакетное обновление товаров. Каждый элемент: {product_id: int, ...поля}.

        Автоматически разбивает на пачки по 50 (лимит API).
        Возвращает: {updated: int, failed: int, results: [{product_id, status, error?}]}.
        """
        all_results = []
        total_updated = 0
        total_failed = 0
        for i in range(0, len(updates), 50):
            batch = updates[i:i + 50]
            resp = self._request('PATCH', '/imported-products/batch',
                                 json={'updates': batch})
            all_results.extend(resp.get('results', []))
            total_updated += resp.get('updated', 0)
            total_failed += resp.get('failed', 0)
        return {
            'updated': total_updated,
            'failed': total_failed,
            'results': all_results,
        }

    # ── Справочник категорий ──────────────────────────────────────

    def search_categories(self, query: str, limit: int = 20) -> dict:
        """Поиск категорий WB с типизированным reference_status."""
        return self._request('GET', '/categories/search',
                             params={'q': query, 'limit': limit})

    def search_categories_batch(
        self, queries: list[str], limit: int = 20,
    ) -> dict:
        """Search typed category scopes in one request and verify exact order."""
        if not isinstance(queries, list) or not 1 <= len(queries) <= 200:
            raise ValueError('queries must contain 1..200 entries')
        prepared = []
        seen = set()
        for index, raw_query in enumerate(queries):
            if not isinstance(raw_query, str):
                raise ValueError(f'queries[{index}] must be a string')
            query = raw_query.strip()
            if not 2 <= len(query) <= 300:
                raise ValueError(f'queries[{index}] must contain 2..300 chars')
            normalized = query.casefold()
            if normalized in seen:
                raise ValueError(f'Duplicate query: {query}')
            seen.add(normalized)
            prepared.append(query)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ValueError('limit must be an integer from 1 to 50')

        payload = self._request(
            'POST', '/categories/search-batch',
            json={'queries': prepared, 'limit': limit},
        )
        results = payload.get('results') if isinstance(payload, dict) else None
        if (
            not isinstance(results, list)
            or len(results) != len(prepared)
            or payload.get('count') != len(results)
            or not isinstance(payload.get('reference_status'), dict)
        ):
            raise ValueError('Invalid category search batch response')
        for expected_query, result in zip(prepared, results):
            if (
                not isinstance(result, dict)
                or result.get('query') != expected_query
                or not isinstance(result.get('categories'), list)
                or result.get('count') != len(result['categories'])
                or not isinstance(result.get('reference_status'), dict)
            ):
                raise ValueError(
                    'Category search batch response does not match request order',
                )
        if {item['query'] for item in results} != set(prepared):
            raise ValueError('Category search batch query set does not match request')
        return payload

    # ── Характеристики категории ─────────────────────────────────

    def get_category_characteristics(self, subject_id: int,
                                      required_only: bool = False) -> dict:
        """Получает схему WB с типизированным reference_status."""
        params = f'?required_only=true' if required_only else ''
        return self._request(
            'GET', f'/categories/{subject_id}/characteristics{params}'
        )

    def get_category_characteristics_batch(
        self, subject_ids: list[int], required_only: bool = False,
    ) -> dict:
        """Load category schemas in one request and verify exact IDs/order."""
        if not isinstance(subject_ids, list) or not 1 <= len(subject_ids) <= 200:
            raise ValueError('subject_ids must contain 1..200 entries')
        prepared = []
        seen = set()
        for index, subject_id in enumerate(subject_ids):
            if (
                not isinstance(subject_id, int)
                or isinstance(subject_id, bool)
                or subject_id <= 0
            ):
                raise ValueError(
                    f'subject_ids[{index}] must be a positive integer',
                )
            if subject_id in seen:
                raise ValueError(f'Duplicate subject_id: {subject_id}')
            seen.add(subject_id)
            prepared.append(subject_id)
        if not isinstance(required_only, bool):
            raise ValueError('required_only must be a boolean')

        payload = self._request(
            'POST', '/categories/characteristics-batch',
            json={
                'subject_ids': prepared,
                'required_only': required_only,
            },
        )
        results = payload.get('results') if isinstance(payload, dict) else None
        if (
            not isinstance(results, list)
            or len(results) != len(prepared)
            or payload.get('count') != len(results)
        ):
            raise ValueError('Invalid characteristic schema batch response')
        for expected_id, result in zip(prepared, results):
            if (
                not isinstance(result, dict)
                or result.get('subject_id') != expected_id
                or not isinstance(result.get('characteristics'), list)
                or result.get('count') != len(result['characteristics'])
                or not isinstance(result.get('reference_status'), dict)
            ):
                raise ValueError(
                    'Characteristic schema batch response does not match request order',
                )
        if {item['subject_id'] for item in results} != set(prepared):
            raise ValueError(
                'Characteristic schema batch subject set does not match request',
            )
        return payload

    # ── Справочники ────────────────────────────────────────────────

    def get_directory(self, directory_type: str, query: str = None,
                      limit: int = 50) -> dict:
        """Получает справочник WB с reference_status."""
        q = {'limit': limit}
        if query:
            q['q'] = query
        return self._request('GET', f'/directories/{directory_type}', params=q)

    # ── Запрещённые слова ──────────────────────────────────────────

    def get_prohibited_words(self, seller_id: int = None,
                              query: str = None) -> dict:
        """Получает список стоп-слов."""
        q = {}
        if seller_id:
            q['seller_id'] = seller_id
        if query:
            q['q'] = query
        return self._request('GET', '/prohibited-words', params=q)

    def check_prohibited_words(self, text: str,
                                seller_id: int = None) -> dict:
        """Проверяет текст на стоп-слова."""
        payload = {'text': text}
        if seller_id:
            payload['seller_id'] = seller_id
        return self._request('POST', '/prohibited-words/check', json=payload)

    def check_prohibited_words_batch(self, items: list[dict],
                                     seller_id: int) -> dict:
        """Проверяет все тексты, разбивая их по лимиту internal API."""
        if not isinstance(items, list):
            raise ValueError('items must be a list')
        if not items:
            return {'results': [], 'count': 0}
        results = []
        for index in range(0, len(items), 50):
            response = self._request(
                'POST', '/prohibited-words/check-batch',
                json={
                    'seller_id': seller_id,
                    'items': items[index:index + 50],
                },
            )
            results.extend(response.get('results') or [])
        return {'results': results, 'count': len(results)}

    # ── Бренды ─────────────────────────────────────────────────────

    def preflight_brand_categories(self, category_ids: list[int]) -> dict:
        """Check brand-reference freshness for typed WB category scopes."""
        if not isinstance(category_ids, list) or not 1 <= len(category_ids) <= 100:
            raise ValueError('category_ids must contain 1..100 entries')
        payload = self._request(
            'POST', '/brands/preflight', json={'category_ids': category_ids},
        )
        results = payload.get('results') if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != len(set(category_ids)):
            raise ValueError('Invalid brand preflight response')
        expected = {int(category_id) for category_id in category_ids}
        actual = {
            int(item.get('category_id'))
            for item in results if isinstance(item, dict)
        }
        if actual != expected:
            raise ValueError('Brand preflight category IDs do not match request')
        return payload

    def validate_brand(self, brand_name: str,
                       category_id: int = None) -> dict:
        """Проверяет бренд и возвращает плоский typed result без HTTP envelope."""
        query = {'brand': brand_name}
        if category_id:
            query['category_id'] = category_id
        payload = self._request('GET', '/brands/validate', params=query)
        result = payload.get('result') if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise ValueError('Invalid brand validation response')
        require_usable_reference(payload, 'wb_brands')
        result = dict(result)
        result['reference_status'] = payload['reference_status']
        return result

    def validate_brands(self, items: list[dict]) -> list[dict]:
        """Validate a typed brand batch in one internal API request."""
        if not isinstance(items, list) or not items or len(items) > 100:
            raise ValueError('items must contain 1..100 entries')
        payload = self._request(
            'POST', '/brands/validate-batch', json={'items': items},
        )
        results = payload.get('results') if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != len(items):
            raise ValueError('Invalid batch brand validation response')
        expected_ids = {int(item['product_id']) for item in items}
        result_ids = {
            int(item.get('product_id'))
            for item in results if isinstance(item, dict)
        }
        if result_ids != expected_ids:
            raise ValueError('Batch brand validation IDs do not match request')
        return results

    # ── Настройки ценообразования ──────────────────────────────────

    def get_pricing_settings(self, seller_id: int) -> dict:
        """Получает формулы и коэффициенты ценообразования."""
        return self._request('GET', f'/sellers/{seller_id}/pricing')

    # ── Задачи (для оркестратора) ────────────────────────────────

    # ── Конфигурация LLM из платформы ────────────────────────────

    def get_llm_config(self) -> dict:
        """Получает LLM-конфигурацию из платформы (SystemSettings).

        Возвращает dict с env-ключами: {LLM_PROVIDER: 'openrouter', ...}.
        Пустой dict если платформа недоступна.
        """
        try:
            data = self._request('GET', '/config/llm')
            return data.get('config', {})
        except Exception as e:
            logger.debug(f"Failed to fetch LLM config from platform: {e}")
            return {}

    # ── Подзадачи оркестратора ─────────────────────────────────────

    def create_subtask(self, agent_name: str, task_type: str,
                       seller_id: int, title: str,
                       input_data: dict = None,
                       parent_task_id: str = None) -> dict:
        """Создаёт подзадачу для другого агента."""
        return self._request('POST', '/tasks/create', json={
            'agent_name': agent_name,
            'task_type': task_type,
            'seller_id': seller_id,
            'title': title,
            'input_data': input_data or {},
            'parent_task_id': parent_task_id,
        })

    def get_task_status(self, task_id: str) -> dict:
        """Получает статус задачи."""
        if self._current_task_id and self._current_task_id != task_id:
            return self._request(
                'GET',
                f'/tasks/{self._current_task_id}/subtasks/{task_id}',
            )
        return self._request('GET', f'/tasks/{task_id}')
