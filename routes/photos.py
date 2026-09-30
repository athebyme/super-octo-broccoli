# -*- coding: utf-8 -*-
"""
Маршруты для раздачи фотографий поставщиков.

Включает:
- Безопасную раздачу кэшированных фото по хэшу
- Прокси-маршрут для фото товаров из каталога поставщика (SupplierProduct)
- Публичный маршрут для раздачи фото в WB (без авторизации, с подписанным токеном)
"""
import json
import re
import hmac
import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from io import BytesIO
from types import SimpleNamespace

from flask import send_file, abort, Response, request, url_for
from flask_login import current_user, login_required
from services.source_photo_display import photo_entry_urls as _photo_entry_urls

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

def _bounded_env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


# Публичные signed URL иногда обязаны прогреть cache синхронно для WB/Ozon.
# Это единственный внешний fetch в web process и он отделён bulkhead-ом:
# cache-miss из seller UI всегда уходит в background queue.
PHOTO_PUBLIC_FETCH_CONCURRENCY = _bounded_env_int(
    'PHOTO_PUBLIC_FETCH_CONCURRENCY', 2, 1, 4,
)
_photo_public_fetch_slots = threading.BoundedSemaphore(
    PHOTO_PUBLIC_FETCH_CONCURRENCY,
)

# TTL-кэш auth-cookies поставщика: без него каждый промах фото-кэша делает
# отдельный логин-POST на сайт поставщика, а страница на 30+ фото — шторм
# логинов, за который поставщик троттлит наш IP.
AUTH_COOKIE_TTL_OK = 1800
AUTH_COOKIE_TTL_FAIL = 120
_auth_cookie_cache = {}  # supplier.code -> (cookies_dict, monotonic_expires_at)
_auth_cookie_lock = threading.Lock()


def _supplier_auth_cookies_provider(supplier):
    """Снимок credential fields для вызова только внутри photo worker."""
    if (
        not supplier
        or supplier.code != 'sexoptovik'
        or not supplier.auth_login
        or not supplier.auth_password
    ):
        return None

    supplier_code = supplier.code
    login = supplier.auth_login
    password = supplier.auth_password

    def _provider():
        return _get_supplier_auth_cookies(SimpleNamespace(
            code=supplier_code,
            auth_login=login,
            auth_password=password,
        ))

    return _provider


def _pending_ui_photo_response(queued):
    """Немедленный cache-miss response; внешний I/O уже вынесен из request."""
    if request.args.get('deferred') == '1':
        response = Response(status=202, mimetype='image/jpeg')
    else:
        response = _generate_placeholder_image()
    response.cache_control.no_store = True
    response.cache_control.max_age = 0
    response.headers['Retry-After'] = '2'
    response.headers['X-Photo-Cache'] = 'pending'
    response.headers['X-Photo-Queue'] = 'queued' if queued else 'pending'
    return response


def _warm_public_photo(
    cache,
    supplier_type,
    external_id,
    url,
    fallbacks,
    supplier,
):
    """Bounded sync warm для signed marketplace URL; UI сюда не попадает."""
    if not _photo_public_fetch_slots.acquire(blocking=False):
        return 'busy'
    try:
        auth_cookies = _get_supplier_auth_cookies(supplier)
        if cache.download_now(
            supplier_type,
            external_id,
            url,
            auth_cookies=auth_cookies,
            fallback_urls=fallbacks,
        ):
            return 'ready'
        return 'failed'
    finally:
        _photo_public_fetch_slots.release()


def _sign_photo_token(secret_key: str, sp_id: int, photo_idx: int) -> str:
    """Подписывает параметры фото HMAC-токеном."""
    msg = f'{sp_id}:{photo_idx}'
    sig = hmac.new(secret_key.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]
    return sig


def _sign_imported_photo_token(secret_key: str, ip_id: int, photo_idx: int) -> str:
    """Подписывает параметры фото ImportedProduct HMAC-токеном."""
    msg = f'ip:{ip_id}:{photo_idx}'
    sig = hmac.new(secret_key.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]
    return sig


def _build_public_photo_url(sp_id: int, photo_idx: int) -> str:
    """
    Строит публичный URL для фото, доступный извне (для WB media/save).

    Если задан PUBLIC_BASE_URL — формирует абсолютный URL на его основе.
    Иначе — через url_for(_external=True) (будет localhost — не работает для WB).
    """
    from flask import current_app
    secret = current_app.config['SECRET_KEY']
    sig = _sign_photo_token(secret, sp_id, photo_idx)

    public_base = current_app.config.get('PUBLIC_BASE_URL', '').rstrip('/')
    if public_base:
        return f"{public_base}/photos/public/{sp_id}/{photo_idx}.jpg?sig={sig}"

    # Fallback: url_for (может генерить localhost)
    return url_for('serve_public_photo', sp=sp_id, idx=photo_idx, sig=sig, _external=True)


def generate_public_photo_url(supplier_product_id: int, photo_idx: int) -> str:
    """
    Генерирует публичный URL для фото товара поставщика.
    Используется для отображения в превью WB и для загрузки фото в WB.
    """
    return _build_public_photo_url(supplier_product_id, photo_idx)


def generate_public_photo_urls(imported_product) -> list:
    """
    Генерирует список публичных URL для всех фото ImportedProduct.
    Если есть связь с SupplierProduct — использует её.
    Если нет — генерирует URL через imported-product маршрут.
    """
    import json as _json

    photo_urls = []
    if imported_product.photo_urls:
        try:
            photo_urls = _json.loads(imported_product.photo_urls)
        except Exception:
            return []

    if not photo_urls:
        return []

    # Предпочитаем supplier_product_id для эффективного кэширования
    sp_id = imported_product.supplier_product_id
    if sp_id:
        return [_build_public_photo_url(sp_id, idx) for idx in range(len(photo_urls))]

    # Фолбэк: генерируем URL через imported-product маршрут
    return [_build_imported_photo_url(imported_product.id, idx) for idx in range(len(photo_urls))]


def _build_imported_photo_url(ip_id: int, photo_idx: int) -> str:
    """
    Строит публичный URL для фото ImportedProduct (без SupplierProduct).
    Используется как фолбэк, когда supplier_product_id = None.
    """
    from flask import current_app
    secret = current_app.config['SECRET_KEY']
    sig = _sign_imported_photo_token(secret, ip_id, photo_idx)

    public_base = current_app.config.get('PUBLIC_BASE_URL', '').rstrip('/')
    if public_base:
        return f"{public_base}/photos/imported/{ip_id}/{photo_idx}.jpg?sig={sig}"

    return url_for('serve_imported_public_photo', ip=ip_id, idx=photo_idx, sig=sig, _external=True)


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
        Cache hit отдаётся сразу; cache miss только ставится в background queue.
        """
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

        url, fallbacks = _photo_entry_urls(photos[photo_idx])
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

        queued = cache.queue_download(
            supplier_type=supplier_type,
            external_id=external_id,
            url=url,
            fallback_urls=fallbacks,
            auth_cookies_provider=_supplier_auth_cookies_provider(
                product.supplier,
            ),
        )
        return _pending_ui_photo_response(queued)

    # ==========================================================================
    # Прокси для фото ImportedProduct (через связь с SupplierProduct)
    # ==========================================================================

    @app.route('/api/photos/imported-product/<int:product_id>/<int:photo_idx>')
    @login_required
    def serve_imported_product_photo(product_id, photo_idx):
        """
        Прокси для фото импортированных товаров продавца.
        Без redirect переиспользует SupplierProduct cache key; cache miss
        немедленно уходит в bounded background queue.
        """
        from models import ImportedProduct, SupplierProduct, db
        from services.photo_cache import get_photo_cache

        seller = getattr(current_user, 'seller', None)
        if not seller:
            abort(404)
        product = ImportedProduct.query.filter_by(
            id=product_id,
            seller_id=seller.id,
        ).first_or_404()
        url = None
        fallbacks = []
        source_supplier = None
        supplier_type = 'imported'
        external_id = str(product.external_id or product.id)

        # Используем точную supplier projection напрямую, без второго HTTP 302.
        if product.supplier_product_id:
            supplier_product = db.session.get(
                SupplierProduct, product.supplier_product_id,
            )
            if supplier_product and supplier_product.photo_urls_json:
                try:
                    supplier_photos = json.loads(
                        supplier_product.photo_urls_json,
                    )
                except (json.JSONDecodeError, TypeError):
                    supplier_photos = []
                if 0 <= photo_idx < len(supplier_photos):
                    url, fallbacks = _photo_entry_urls(
                        supplier_photos[photo_idx],
                    )
                    if url:
                        source_supplier = supplier_product.supplier
                        supplier_type = (
                            source_supplier.code
                            if source_supplier else 'unknown'
                        )
                        external_id = supplier_product.external_id or ''

        # Legacy rows без usable exact supplier photo используют свой snapshot.
        if not url:
            if not product.photo_urls:
                abort(404)
            try:
                imported_photos = json.loads(product.photo_urls)
            except (json.JSONDecodeError, TypeError):
                abort(404)
            if photo_idx < 0 or photo_idx >= len(imported_photos):
                abort(404)
            url, fallbacks = _photo_entry_urls(imported_photos[photo_idx])
            source_supplier = product.supplier

        if not url:
            abort(404)

        cache = get_photo_cache()
        if cache.is_cached(supplier_type, external_id, url):
            cache_path = cache.get_cache_path(supplier_type, external_id, url)
            response = send_file(
                cache_path, mimetype='image/jpeg', conditional=True,
            )
            response.cache_control.max_age = 86400
            response.cache_control.private = True
            return response

        queued = cache.queue_download(
            supplier_type=supplier_type,
            external_id=external_id,
            url=url,
            fallback_urls=fallbacks,
            auth_cookies_provider=_supplier_auth_cookies_provider(
                source_supplier,
            ),
        )
        return _pending_ui_photo_response(queued)

    # ==========================================================================
    # API управления скачиванием фото
    # ==========================================================================

    @app.route('/api/photos/download-all/<int:supplier_id>', methods=['POST'])
    @login_required
    def api_photos_download_all(supplier_id):
        """
        Запускает массовое фоновое скачивание всех фото поставщика.
        Фото, которые уже есть в кэше, пропускаются.
        """
        from services.photo_cache import bulk_download_supplier_photos
        try:
            result = bulk_download_supplier_photos(supplier_id)
            return {
                'success': True,
                'total_photos': result['total_photos'],
                'already_cached': result['already_cached'],
                'queued': result['queued'],
                'errors': result['errors']
            }
        except Exception as e:
            logger.error(f"Ошибка запуска массового скачивания фото: {e}")
            return {'success': False, 'error': str(e)}, 500

    @app.route('/api/photos/download-status/<int:supplier_id>')
    @login_required
    def api_photos_download_status(supplier_id):
        """
        Возвращает прогресс скачивания фото для поставщика.
        """
        from services.photo_cache import get_photo_cache
        try:
            cache = get_photo_cache()
            progress = cache.get_download_progress(supplier_id)
            return {
                'success': True,
                **progress
            }
        except Exception as e:
            logger.error(f"Ошибка получения прогресса: {e}")
            return {'success': False, 'error': str(e)}, 500

    @app.route('/api/photos/cache-stats')
    @login_required
    def api_photos_cache_stats():
        """
        Общая статистика кэша фотографий.
        """
        from services.photo_cache import get_photo_cache
        try:
            cache = get_photo_cache()
            stats = cache.get_stats()
            return {
                'success': True,
                **stats
            }
        except Exception as e:
            logger.error(f"Ошибка получения статистики: {e}")
            return {'success': False, 'error': str(e)}, 500

    # ==========================================================================
    # Публичный маршрут для раздачи фото (для WB и превью без авторизации)
    # ==========================================================================

    @app.route('/photos/public/<int:sp>/<int:idx>.jpg')
    def serve_public_photo(sp, idx):
        """
        Публичный маршрут для раздачи фото по supplier_product_id + idx.
        НЕ требует авторизации — для WB и внешних сервисов.
        Защищён HMAC-подписью в query parameter sig.
        URL: /photos/public/{supplier_product_id}/{photo_idx}.jpg?sig=HMAC
        """
        from flask import request as _req
        from services.photo_cache import get_photo_cache

        # Проверяем подпись
        sig = _req.args.get('sig', '')
        expected = _sign_photo_token(app.config['SECRET_KEY'], sp, idx)
        if not hmac.compare_digest(sig, expected):
            abort(403)

        from models import SupplierProduct
        product = SupplierProduct.query.get(sp)
        if not product or not product.photo_urls_json:
            abort(404)

        try:
            photos = json.loads(product.photo_urls_json)
        except (json.JSONDecodeError, TypeError):
            abort(404)

        if idx < 0 or idx >= len(photos):
            abort(404)

        supplier_type = product.supplier.code if product.supplier else 'unknown'
        external_id = product.external_id or ''
        url, fallbacks = _photo_entry_urls(photos[idx])
        if not url:
            abort(404)

        cache = get_photo_cache()

        # Из кэша
        if cache.is_cached(supplier_type, external_id, url):
            cache_path = cache.get_cache_path(supplier_type, external_id, url)
            response = send_file(cache_path, mimetype='image/jpeg', conditional=True)
            response.cache_control.max_age = 86400
            response.cache_control.public = True
            return response

        warm_status = _warm_public_photo(
            cache,
            supplier_type,
            external_id,
            url,
            fallbacks,
            product.supplier,
        )
        if warm_status == 'busy':
            response = Response('photo fetch busy', status=503)
            response.cache_control.no_store = True
            response.headers['Retry-After'] = '2'
            return response
        if warm_status != 'ready':
            abort(502)

        cache_path = cache.get_cache_path(supplier_type, external_id, url)
        response = send_file(cache_path, mimetype='image/jpeg', conditional=True)
        response.cache_control.max_age = 86400
        response.cache_control.public = True
        return response

    # ==========================================================================
    # Публичный маршрут для фото ImportedProduct (без SupplierProduct)
    # ==========================================================================

    @app.route('/photos/imported/<int:ip>/<int:idx>.jpg')
    def serve_imported_public_photo(ip, idx):
        """
        Публичный маршрут для фото ImportedProduct напрямую.
        Используется как фолбэк, когда нет привязки к SupplierProduct.
        НЕ требует авторизации — для WB и внешних сервисов.
        Защищён HMAC-подписью.
        """
        from flask import request as _req

        sig = _req.args.get('sig', '')
        expected = _sign_imported_photo_token(app.config['SECRET_KEY'], ip, idx)
        if not hmac.compare_digest(sig, expected):
            abort(403)

        from models import ImportedProduct as _IP
        product = _IP.query.get(ip)
        if not product or not product.photo_urls:
            abort(404)

        try:
            photos = json.loads(product.photo_urls)
        except (json.JSONDecodeError, TypeError):
            abort(404)

        if idx < 0 or idx >= len(photos):
            abort(404)

        url, fallbacks = _photo_entry_urls(photos[idx])
        if not url:
            abort(404)

        # Пробуем отдать из кэша (если товар привязан к поставщику)
        from services.photo_cache import get_photo_cache
        cache = get_photo_cache()
        cache_supplier_type = 'imported'
        cache_external_id = str(product.external_id or product.id)

        if cache.is_cached(cache_supplier_type, cache_external_id, url):
            cache_path = cache.get_cache_path(cache_supplier_type, cache_external_id, url)
            response = send_file(cache_path, mimetype='image/jpeg', conditional=True)
            response.cache_control.max_age = 86400
            response.cache_control.public = True
            return response

        warm_status = _warm_public_photo(
            cache,
            cache_supplier_type,
            cache_external_id,
            url,
            fallbacks,
            product.supplier,
        )
        if warm_status == 'busy':
            response = Response('photo fetch busy', status=503)
            response.cache_control.no_store = True
            response.headers['Retry-After'] = '2'
            return response
        if warm_status != 'ready':
            abort(502)

        cache_path = cache.get_cache_path(
            cache_supplier_type, cache_external_id, url,
        )
        response = send_file(cache_path, mimetype='image/jpeg', conditional=True)
        response.cache_control.max_age = 86400
        response.cache_control.public = True
        return response


def _get_supplier_auth_cookies(supplier) -> dict:
    """
    Получает cookies авторизации для поставщика (если требуется).

    Результат кэшируется в памяти процесса: успешная сессия переиспользуется
    AUTH_COOKIE_TTL_OK секунд, неудачный логин не повторяется чаще, чем раз в
    AUTH_COOKIE_TTL_FAIL секунд. Логин выполняется под lock, чтобы параллельные
    фото-запросы не устраивали шторм логинов на сайт поставщика.
    """
    if not supplier:
        return {}

    if supplier.code != 'sexoptovik' or not supplier.auth_login or not supplier.auth_password:
        return {}

    with _auth_cookie_lock:
        cached = _auth_cookie_cache.get(supplier.code)
        if cached and cached[1] > time.monotonic():
            return dict(cached[0])

        cookies = {}
        ttl = AUTH_COOKIE_TTL_FAIL
        try:
            import requests as _requests
            session = _requests.Session()
            login_url = 'https://sexoptovik.ru/admin/login'
            resp = session.post(login_url, data={
                'login': supplier.auth_login,
                'password': supplier.auth_password,
            }, timeout=10, allow_redirects=False)
            if resp.status_code in (200, 302):
                cookies = dict(session.cookies)
                if cookies:
                    ttl = AUTH_COOKIE_TTL_OK
        except Exception as e:
            logger.debug(f"[PhotoProxy] Auth failed for {supplier.code}: {e}")

        _auth_cookie_cache[supplier.code] = (cookies, time.monotonic() + ttl)
        return dict(cookies)


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


def register_content_photo_routes(app):
    """Роут для раздачи кэшированных фото контент-фабрики."""

    @app.route('/content-photos/<int:nm_id>/<int:index>.jpg')
    def serve_content_photo(nm_id, index):
        """
        Отдаёт закэшированное фото товара для контент-фабрики.
        Без авторизации — чтобы VK/Telegram publisher мог скачать.
        """
        from services.content_photo_cache import get_cached_photo_path

        if index < 1 or index > 20:
            abort(404)

        photo_path = get_cached_photo_path(nm_id, index)
        if not photo_path.exists():
            abort(404)

        response = send_file(str(photo_path), mimetype='image/jpeg', conditional=True)
        response.cache_control.max_age = 86400
        response.cache_control.public = True
        return response
