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

# Подавляем предупреждения о self-signed сертификатах внутри Docker-сети
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class PlatformClient:
    """Клиент для Internal API v1."""

    def __init__(self, config: AgentConfig = None):
        self.cfg = config or AgentConfig
        self.base_url = self.cfg.PLATFORM_URL.rstrip('/')
        self.session = requests.Session()
        # Внутри Docker-сети seller-platform использует self-signed сертификат —
        # верификацию отключаем для inter-service коммуникации.
        self.session.verify = False
        self.session.headers.update({
            'X-Agent-Id': self.cfg.AGENT_ID,
            'X-Agent-Key': self.cfg.AGENT_API_KEY,
            'Content-Type': 'application/json',
        })

    def _url(self, path: str) -> str:
        return f"{self.base_url}/internal/v1{path}"

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Выполняет запрос с retry на сетевые и timeout ошибки."""
        url = self._url(path)
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

    def complete_task(self, task_id: str, result: dict = None) -> dict:
        return self._request('POST', f'/tasks/{task_id}/complete',
                             json={'result': result or {}})

    def fail_task(self, task_id: str, error: str, result: dict = None) -> dict:
        payload = {'error': error}
        if result:
            payload['result'] = result
        return self._request('POST', f'/tasks/{task_id}/fail', json=payload)

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

    def list_products(self, seller_id: int, page: int = 1,
                      per_page: int = 50, status: str = None) -> dict:
        params = f'?page={page}&per_page={per_page}'
        if status:
            params += f'&status={status}'
        return self._request('GET', f'/sellers/{seller_id}/products{params}')

    def get_product(self, seller_id: int, product_id: int) -> dict:
        data = self._request('GET', f'/sellers/{seller_id}/products/{product_id}')
        return data.get('product', {})

    def update_product(self, seller_id: int, product_id: int,
                       updates: dict) -> dict:
        return self._request('PATCH',
                             f'/sellers/{seller_id}/products/{product_id}',
                             json=updates)

    def list_imported_products(self, seller_id: int, page: int = 1,
                               per_page: int = 50) -> dict:
        return self._request(
            'GET',
            f'/sellers/{seller_id}/imported-products?page={page}&per_page={per_page}'
        )

    def get_imported_product(self, product_id: int) -> dict:
        return self._request('GET', f'/imported-products/{product_id}')
