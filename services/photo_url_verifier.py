# -*- coding: utf-8 -*-
"""
Проверка доступности URL фотографий товаров.

Выполняет HEAD-запросы к URL фотографий, чтобы:
- Выявить битые ссылки (404, 403, таймаут)
- Проверить Content-Type (действительно ли это изображение)
- Пометить товары с проблемными фото
"""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)


@dataclass
class PhotoVerificationResult:
    """Результат проверки фото для одного товара."""
    product_id: int
    total_urls: int = 0
    valid_urls: int = 0
    broken_urls: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)  # url -> error


@dataclass
class BatchVerificationResult:
    """Результат пакетной проверки фото."""
    total_products: int = 0
    products_checked: int = 0
    products_with_broken_photos: int = 0
    total_urls_checked: int = 0
    total_broken_urls: int = 0
    duration_seconds: float = 0.0
    details: List[PhotoVerificationResult] = field(default_factory=list)


class PhotoURLVerifier:
    """Проверка доступности URL фотографий."""

    # Допустимые Content-Type для изображений
    IMAGE_CONTENT_TYPES = {
        'image/jpeg', 'image/png', 'image/webp', 'image/gif',
        'image/bmp', 'image/tiff', 'image/svg+xml',
    }

    @classmethod
    def verify_url(
        cls,
        url: str,
        timeout: int = 10,
        auth: Tuple[str, str] = None,
    ) -> Tuple[bool, str]:
        """
        Проверить один URL фотографии через HEAD-запрос.

        Returns:
            (is_valid, error_message)
        """
        if not url or not url.startswith(('http://', 'https://')):
            return False, 'Invalid URL'

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (compatible; PhotoVerifier/1.0)',
            }
            resp = requests.head(
                url,
                timeout=timeout,
                allow_redirects=True,
                headers=headers,
                auth=auth,
            )

            if resp.status_code == 405:
                # HEAD не поддерживается — пробуем GET с range
                resp = requests.get(
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                    headers={**headers, 'Range': 'bytes=0-0'},
                    auth=auth,
                    stream=True,
                )
                resp.close()

            if resp.status_code >= 400:
                return False, f'HTTP {resp.status_code}'

            # Проверяем Content-Type
            content_type = resp.headers.get('Content-Type', '').lower().split(';')[0].strip()
            if content_type and content_type not in cls.IMAGE_CONTENT_TYPES:
                # Некоторые CDN не возвращают Content-Type при HEAD
                if content_type not in ('application/octet-stream', ''):
                    return False, f'Not an image: {content_type}'

            return True, ''

        except requests.Timeout:
            return False, 'Timeout'
        except requests.ConnectionError:
            return False, 'Connection error'
        except Exception as e:
            return False, str(e)[:100]

    @classmethod
    def verify_product_photos(
        cls,
        product,
        auth: Tuple[str, str] = None,
    ) -> PhotoVerificationResult:
        """Проверить все фото одного товара."""
        result = PhotoVerificationResult(product_id=product.id)

        try:
            urls = json.loads(product.photo_urls_json) if product.photo_urls_json else []
        except (json.JSONDecodeError, TypeError):
            urls = []

        result.total_urls = len(urls)
        if not urls:
            return result

        for url in urls:
            is_valid, error = cls.verify_url(url, auth=auth)
            if is_valid:
                result.valid_urls += 1
            else:
                result.broken_urls.append(url)
                result.errors[url] = error

        return result

    @classmethod
    def verify_supplier_photos(
        cls,
        supplier_id: int,
        limit: int = 200,
        max_workers: int = 10,
    ) -> BatchVerificationResult:
        """
        Пакетная проверка фото для поставщика.

        Args:
            supplier_id: ID поставщика
            limit: Макс. количество товаров для проверки
            max_workers: Параллельные потоки
        """
        from models import SupplierProduct, Supplier

        batch_result = BatchVerificationResult()
        start_time = time.time()

        supplier = Supplier.query.get(supplier_id)
        auth = None
        if supplier and supplier.auth_login and supplier.auth_password:
            auth = (supplier.auth_login, supplier.auth_password)

        # Берём товары с фото
        products = (
            SupplierProduct.query
            .filter_by(supplier_id=supplier_id)
            .filter(SupplierProduct.photo_urls_json.isnot(None))
            .filter(SupplierProduct.photo_urls_json != '[]')
            .limit(limit)
            .all()
        )

        batch_result.total_products = len(products)

        def check_product(prod):
            return cls.verify_product_photos(prod, auth=auth)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(check_product, p): p
                for p in products
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    batch_result.products_checked += 1
                    batch_result.total_urls_checked += result.total_urls
                    batch_result.total_broken_urls += len(result.broken_urls)
                    if result.broken_urls:
                        batch_result.products_with_broken_photos += 1
                        batch_result.details.append(result)
                except Exception as e:
                    logger.debug(f"Photo verification error: {e}")

        batch_result.duration_seconds = time.time() - start_time

        logger.info(
            f"Photo verification for supplier {supplier_id}: "
            f"{batch_result.products_checked} products, "
            f"{batch_result.total_urls_checked} URLs, "
            f"{batch_result.total_broken_urls} broken "
            f"({batch_result.duration_seconds:.1f}s)"
        )

        return batch_result
