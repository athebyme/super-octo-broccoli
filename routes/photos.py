# -*- coding: utf-8 -*-
"""
Маршруты для раздачи фотографий поставщиков.

Включает:
- Безопасную раздачу кэшированных фото по хэшу
- Прокси-маршрут для фото товаров из каталога поставщика (SupplierProduct)
"""
import json
import re
import logging
from pathlib import Path
from io import BytesIO

from flask import send_file, abort, Response
from flask_login import login_required

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


def register_photo_routes(app):
    """Регистрирует маршруты раздачи фото в приложении Flask"""

    # ==========================================================================
    # Раздача кэшированного фото по хэшу (перенесено из seller_platform.py)
    # ==========================================================================

    @app.route('/photos/supplier/<supplier_type>/<external_id>/<photo_hash>')
    @login_required
    def serve_supplier_photo(supplier_type, external_id, photo_hash):
        """
        Безопасная раздача кэшированных фото поставщика.
        Только авторизованные пользователи. Не раскрывает оригинальный URL поставщика.
        """
        # Валидация параметров — только безопасные символы
        if not re.match(r'^[a-zA-Z0-9_-]+$', supplier_type):
            abort(404)
        if not re.match(r'^[a-zA-Z0-9_-]+$', external_id):
            abort(404)
        if not re.match(r'^[a-f0-9]+$', photo_hash):
            abort(404)

        cache_base = BASE_DIR / 'data' / 'photo_cache'
        photo_path = cache_base / supplier_type / external_id / f"{photo_hash}.jpg"

        # Path traversal защита
        try:
            photo_path.resolve().relative_to(cache_base.resolve())
        except ValueError:
            abort(404)

        if not photo_path.exists():
            abort(404)

        response = send_file(photo_path, mimetype='image/jpeg', conditional=True)
        response.cache_control.max_age = 86400
        response.cache_control.public = False
        response.cache_control.private = True
        return response

    # ==========================================================================
    # Прокси для фото товаров из каталога поставщика (SupplierProduct)
    # ==========================================================================

    @app.route('/api/photos/supplier-product/<int:supplier_product_id>/<int:photo_idx>')
    @login_required
    def serve_supplier_product_photo(supplier_product_id, photo_idx):
        """
        Прокси для фото товаров из каталога поставщика.
        Отдаёт из кэша или скачивает с сервера поставщика.
        """
        import requests as _requests
        from PIL import Image as _Image
        from models import SupplierProduct
        from services.photo_cache import get_photo_cache

        product = SupplierProduct.query.get_or_404(supplier_product_id)

        if not product.photo_urls_json:
            abort(404)

        try:
            photos = json.loads(product.photo_urls_json)
        except (json.JSONDecodeError, TypeError):
            abort(404)

        if photo_idx < 0 or photo_idx >= len(photos):
            abort(404)

        ph = photos[photo_idx]

        # Определяем URL — поддерживаем и dict, и строку
        if isinstance(ph, dict):
            url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
        elif isinstance(ph, str):
            url = ph
        else:
            abort(404)

        if not url:
            abort(404)

        supplier_type = product.supplier.code if product.supplier else 'unknown'
        external_id = product.external_id or ''
        cache = get_photo_cache()

        # Если уже закэшировано — отдаём из кэша
        if cache.is_cached(supplier_type, external_id, url):
            cache_path = cache.get_cache_path(supplier_type, external_id, url)
            response = send_file(cache_path, mimetype='image/jpeg', conditional=True)
            response.cache_control.max_age = 86400
            response.cache_control.private = True
            return response

        # Скачиваем с поставщика
        auth_cookies = _get_supplier_auth_cookies(product.supplier)

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'image/*,*/*;q=0.8',
        }
        if 'sexoptovik.ru' in url:
            headers['Referer'] = 'https://sexoptovik.ru/admin/'

        # Подготовим fallback URLs
        fallbacks = []
        if isinstance(ph, dict):
            if ph.get('blur') and ph['blur'] != url:
                fallbacks.append(ph['blur'])
            if ph.get('original') and ph['original'] != url:
                fallbacks.append(ph['original'])

        for current_url in [url] + fallbacks:
            try:
                resp = _requests.get(
                    current_url, headers=headers, cookies=auth_cookies,
                    timeout=15, allow_redirects=True
                )
                resp.raise_for_status()

                content_type = resp.headers.get('Content-Type', '')
                if not content_type.startswith('image/') and len(resp.content) < 1024:
                    continue

                img = _Image.open(BytesIO(resp.content))
                output = BytesIO()
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(output, format='JPEG', quality=95)
                image_bytes = output.getvalue()

                # Сохраняем в кэш
                cache.save_to_cache(supplier_type, external_id, url, image_bytes)

                response = Response(image_bytes, mimetype='image/jpeg')
                response.cache_control.max_age = 86400
                response.cache_control.private = True
                return response

            except Exception as e:
                logger.debug(f"[PhotoProxy] Failed {current_url[:60]}: {e}")
                continue

        # Возвращаем placeholder вместо 404
        return _generate_placeholder_image()


def _get_supplier_auth_cookies(supplier) -> dict:
    """Получает cookies авторизации для поставщика (если требуется)"""
    if not supplier:
        return {}

    if supplier.code == 'sexoptovik' and supplier.auth_login and supplier.auth_password:
        try:
            import requests as _requests
            session = _requests.Session()
            login_url = 'https://sexoptovik.ru/admin/login'
            resp = session.post(login_url, data={
                'login': supplier.auth_login,
                'password': supplier.auth_password,
            }, timeout=10, allow_redirects=False)
            if resp.status_code in (200, 302):
                return dict(session.cookies)
        except Exception as e:
            logger.debug(f"[PhotoProxy] Auth failed for {supplier.code}: {e}")

    return {}


def _generate_placeholder_image():
    """Генерирует placeholder изображение (серый квадрат с иконкой)"""
    from PIL import Image as _Image, ImageDraw as _ImageDraw

    img = _Image.new('RGB', (200, 200), '#f3f4f6')
    draw = _ImageDraw.Draw(img)
    # Рисуем простую рамку с крестиком
    draw.line([(80, 80), (120, 120)], fill='#d1d5db', width=2)
    draw.line([(120, 80), (80, 120)], fill='#d1d5db', width=2)
    draw.rectangle([(60, 60), (140, 140)], outline='#d1d5db', width=1)

    output = BytesIO()
    img.save(output, format='JPEG', quality=80)
    output.seek(0)

    response = Response(output.getvalue(), mimetype='image/jpeg')
    response.cache_control.max_age = 300
    response.cache_control.private = True
    return response
