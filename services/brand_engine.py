# -*- coding: utf-8 -*-
"""
Brand Engine — централизованный движок валидации и резолва брендов.

Мультимаркетплейс-архитектура:
- Brand — глобальная сущность (LELO как концепция)
- MarketplaceBrand — привязка к площадке (LELO на WB, Lelo на Ozon)
- BrandAlias — варианты написания (маппят на Brand)
- BrandCategoryLink — допустимость на площадке в категории

Pipeline: normalize → alias lookup → fuzzy → marketplace API → category check.
"""
import json
import logging
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

logger = logging.getLogger('brand_engine')


def _is_sqlite_write_contention(error: Exception) -> bool:
    """Recognize SQLite BUSY/LOCKED failures without matching domain errors."""
    if not isinstance(error, (sqlite3.OperationalError, SQLAlchemyOperationalError)):
        return False
    messages = [str(error)]
    original = getattr(error, 'orig', None)
    if original is not None:
        messages.append(str(original))
    normalized = ' '.join(messages).lower()
    return any(message in normalized for message in (
        'database is locked',
        'database table is locked',
        'database schema is locked',
    ))


@dataclass
class BrandResolution:
    """Результат резолва бренда."""
    status: str  # 'exact', 'confident', 'uncertain', 'unresolved'
    brand_id: Optional[int] = None
    canonical_name: Optional[str] = None
    marketplace_brand_id: Optional[int] = None  # ID в MarketplaceBrand
    marketplace_brand_ext_id: Optional[int] = None  # ID бренда на площадке (wb_brand_id и т.д.)
    marketplace_brand_name: Optional[str] = None  # Имя бренда на площадке
    confidence: float = 0.0
    suggestions: list = field(default_factory=list)
    category_valid: Optional[bool] = None
    source: str = ''  # alias_exact, alias_fuzzy, marketplace_api

    def to_dict(self) -> dict:
        return {
            'status': self.status,
            'brand_id': self.brand_id,
            'canonical_name': self.canonical_name,
            'marketplace_brand_id': self.marketplace_brand_id,
            'marketplace_brand_ext_id': self.marketplace_brand_ext_id,
            'marketplace_brand_name': self.marketplace_brand_name,
            'confidence': self.confidence,
            'suggestions': self.suggestions[:10],
            'category_valid': self.category_valid,
            'source': self.source,
        }


def normalize_for_comparison(text: str) -> str:
    """Нормализация строки для сравнения: lowercase, без спецсимволов, unicode NFC."""
    if not text:
        return ''
    text = unicodedata.normalize('NFC', text)
    text = ' '.join(text.lower().strip().split())
    return text


def normalize_alphanumeric(text: str) -> str:
    """Только буквы и цифры, lowercase — для fuzzy сравнений."""
    if not text:
        return ''
    return ''.join(c.lower() for c in text if c.isalnum())


class BrandEngine:
    """
    Центральный движок валидации и резолва брендов.

    Работает с БД (Brand, BrandAlias, MarketplaceBrand) как source of truth,
    с in-memory кэшем для быстрого lookup.
    """

    def __init__(self, app=None):
        self._app = app
        # alias_normalized -> (brand_id, canonical_name)
        self._alias_cache: Dict[str, Tuple[int, str]] = {}
        # brand_id -> {name, status}
        self._brand_cache: Dict[int, dict] = {}
        # (brand_id, marketplace_id) -> {mp_brand_id, mp_ext_id, mp_name, status}
        self._mp_brand_cache: Dict[Tuple[int, int], dict] = {}
        self._cache_loaded = False
        self._cache_lock = threading.Lock()
        self._last_cache_load: float = 0
        self._cache_ttl: int = 300  # 5 минут
        # Progress tracking for async sync
        self._sync_progress: Dict[int, dict] = {}  # marketplace_id -> progress info
        self._sync_lock_guard = threading.Lock()
        self._sync_locks: Dict[int, threading.Lock] = {}

    def init_app(self, app):
        self._app = app

    def _get_app(self):
        if self._app:
            return self._app
        try:
            from flask import current_app
            return current_app._get_current_object()
        except RuntimeError:
            return None

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def _ensure_cache(self):
        """Загрузить кэш из БД если устарел."""
        now = time.time()
        if self._cache_loaded and (now - self._last_cache_load) < self._cache_ttl:
            return

        with self._cache_lock:
            if self._cache_loaded and (now - self._last_cache_load) < self._cache_ttl:
                return
            self._load_cache()

    def _load_cache(self):
        """Загрузить все aliases, бренды и маркетплейс-привязки из БД."""
        try:
            from models import Brand, BrandAlias, MarketplaceBrand

            alias_cache = {}
            brand_cache = {}
            mp_brand_cache = {}

            for b in Brand.query.all():
                brand_cache[b.id] = {
                    'name': b.name,
                    'name_normalized': b.name_normalized,
                    'status': b.status,
                }

            for a in BrandAlias.query.filter_by(is_active=True).all():
                if a.brand_id in brand_cache:
                    alias_cache[a.alias_normalized] = (
                        a.brand_id,
                        brand_cache[a.brand_id]['name'],
                    )

            for mb in MarketplaceBrand.query.filter_by(
                is_available=True,
                status='verified',
            ).all():
                mp_brand_cache[(mb.brand_id, mb.marketplace_id)] = {
                    'id': mb.id,
                    'marketplace_brand_name': mb.marketplace_brand_name,
                    'marketplace_brand_id': mb.marketplace_brand_id,
                    'status': mb.status,
                }

            self._alias_cache = alias_cache
            self._brand_cache = brand_cache
            self._mp_brand_cache = mp_brand_cache
            self._cache_loaded = True
            self._last_cache_load = time.time()
            logger.debug(f"Brand cache loaded: {len(brand_cache)} brands, {len(alias_cache)} aliases, {len(mp_brand_cache)} marketplace links")

        except Exception as e:
            logger.warning(f"Failed to load brand cache: {e}")

    def invalidate_cache(self):
        """Принудительно инвалидировать кэш."""
        with self._cache_lock:
            self._cache_loaded = False
            self._alias_cache = {}
            self._brand_cache = {}
            self._mp_brand_cache = {}

    # ------------------------------------------------------------------
    # Resolve pipeline
    # ------------------------------------------------------------------

    def resolve(self, raw_brand: str, marketplace_id: int = None,
                category_id: int = None, marketplace_client=None) -> BrandResolution:
        """
        Главный метод — резолвит сырой бренд.

        Pipeline:
        1. Нормализация (strip, unicode normalize)
        2. Exact match по BrandAlias.alias_normalized
        3. Alphanumeric exact match (LOVETOYS == Love Toys)
        4. Fuzzy match по alias кэшу (SequenceMatcher, threshold 0.85)
        5. Поиск через API маркетплейса (если marketplace_client передан)
        6. Если category_id + marketplace_id — проверка допустимости бренда в категории

        Args:
            raw_brand: Сырой бренд от поставщика / AI
            marketplace_id: ID маркетплейса (для marketplace-specific данных)
            category_id: ID категории на маркетплейсе (subjectId для WB)
            marketplace_client: API клиент маркетплейса (WildberriesAPIClient и т.д.)

        Returns:
            BrandResolution с результатом
        """
        if not raw_brand or not raw_brand.strip():
            return BrandResolution(status='unresolved', confidence=0.0)

        raw_brand = raw_brand.strip()
        normalized = normalize_for_comparison(raw_brand)
        if not normalized:
            return BrandResolution(status='unresolved', confidence=0.0)

        self._ensure_cache()

        # Step 1: Exact match по alias
        result = self._match_exact(normalized, marketplace_id)
        if result:
            if category_id and marketplace_id:
                self._apply_category_scope(result, category_id)
            return result

        # Step 2: Alphanumeric exact match
        result = self._match_alphanumeric(raw_brand, marketplace_id)
        if result:
            if category_id and marketplace_id:
                self._apply_category_scope(result, category_id)
            return result

        # Step 3: Fuzzy match
        result = self._match_fuzzy(normalized, raw_brand, marketplace_id)
        if result:
            if category_id and marketplace_id:
                self._apply_category_scope(result, category_id)
            return result

        # Step 4: Marketplace API поиск
        if marketplace_client:
            result = self._match_marketplace_api(
                raw_brand,
                marketplace_id,
                marketplace_client,
                category_id=category_id,
            )
            if result:
                if category_id and marketplace_id:
                    self._apply_category_scope(result, category_id)
                return result

        # Step 5: Не найден — создаём pending бренд
        return self._create_unresolved(raw_brand, normalized)

    def _enrich_with_marketplace(self, brand_id: int, canonical_name: str,
                                  marketplace_id: Optional[int], base_result: dict) -> BrandResolution:
        """Обогатить результат данными MarketplaceBrand если marketplace_id указан."""
        result = BrandResolution(
            brand_id=brand_id,
            canonical_name=canonical_name,
            **base_result,
        )

        if marketplace_id and brand_id:
            mp_data = self._mp_brand_cache.get((brand_id, marketplace_id))
            if mp_data:
                result.marketplace_brand_id = mp_data['id']
                result.marketplace_brand_ext_id = mp_data['marketplace_brand_id']
                result.marketplace_brand_name = mp_data['marketplace_brand_name']

        return result

    def _match_exact(self, normalized: str, marketplace_id: int = None) -> Optional[BrandResolution]:
        """Step 1: Точное совпадение по alias_normalized."""
        cached = self._alias_cache.get(normalized)
        if cached:
            brand_id, canonical_name = cached
            return self._enrich_with_marketplace(brand_id, canonical_name, marketplace_id, {
                'status': 'exact',
                'confidence': 1.0,
                'source': 'alias_exact',
            })
        return None

    def _match_alphanumeric(self, raw_brand: str, marketplace_id: int = None) -> Optional[BrandResolution]:
        """Step 2: Совпадение по alphanumeric."""
        raw_alnum = normalize_alphanumeric(raw_brand)
        if not raw_alnum or len(raw_alnum) < 2:
            return None

        for alias_norm, (brand_id, canonical_name) in self._alias_cache.items():
            alias_alnum = normalize_alphanumeric(alias_norm)
            if alias_alnum == raw_alnum:
                return self._enrich_with_marketplace(brand_id, canonical_name, marketplace_id, {
                    'status': 'exact',
                    'confidence': 0.98,
                    'source': 'alias_alphanumeric',
                })
        return None

    def _match_fuzzy(self, normalized: str, raw_brand: str,
                     marketplace_id: int = None) -> Optional[BrandResolution]:
        """Step 3: Fuzzy match по кэшу aliases."""
        if len(normalized) < 2:
            return None

        prefix = normalized[:3] if len(normalized) >= 3 else normalized
        first_char = normalized[0]

        best_score = 0.0
        best_brand_id = None
        best_canonical = None
        suggestions = []

        for alias_norm, (brand_id, canonical_name) in self._alias_cache.items():
            if not (alias_norm.startswith(first_char) or
                    alias_norm.startswith(prefix) or
                    prefix in alias_norm or
                    normalized in alias_norm or
                    alias_norm in normalized):
                continue

            similarity = SequenceMatcher(None, normalized, alias_norm).ratio()

            if normalized in alias_norm or alias_norm in normalized:
                similarity = min(1.0, similarity + 0.2)
            if alias_norm.startswith(prefix):
                similarity = min(1.0, similarity + 0.1)

            if similarity > best_score:
                if best_score >= 0.5:
                    suggestions.append({
                        'brand_id': best_brand_id,
                        'name': best_canonical,
                        'score': best_score,
                    })
                best_score = similarity
                best_brand_id = brand_id
                best_canonical = canonical_name
            elif similarity >= 0.5:
                suggestions.append({
                    'brand_id': brand_id,
                    'name': canonical_name,
                    'score': similarity,
                })

        suggestions.sort(key=lambda x: x['score'], reverse=True)

        if best_score >= 0.85:
            return self._enrich_with_marketplace(best_brand_id, best_canonical, marketplace_id, {
                'status': 'confident',
                'confidence': best_score,
                'suggestions': suggestions[:5],
                'source': 'alias_fuzzy',
            })
        elif best_score >= 0.6:
            return self._enrich_with_marketplace(best_brand_id, best_canonical, marketplace_id, {
                'status': 'uncertain',
                'confidence': best_score,
                'suggestions': suggestions[:8],
                'source': 'alias_fuzzy',
            })
        elif suggestions:
            return BrandResolution(
                status='uncertain',
                confidence=best_score,
                suggestions=suggestions[:8],
                source='alias_fuzzy',
            )

        return None

    def _match_marketplace_api(self, raw_brand: str, marketplace_id: int,
                                marketplace_client,
                                category_id: int = None) -> Optional[BrandResolution]:
        """Step 4: Поиск через API маркетплейса и сохранение результата."""
        try:
            # Используем validate_brand (WB-совместимый интерфейс)
            api_result = marketplace_client.validate_brand(
                raw_brand,
                subject_id=category_id,
            )

            if api_result.get('valid') and api_result.get('exact_match'):
                match = api_result['exact_match']
                mp_name = match.get('name', '')
                mp_ext_id = match.get('id')

                brand = self._save_brand_from_marketplace(
                    mp_name, mp_ext_id, raw_brand, marketplace_id
                )
                if brand:
                    mp_data = self._mp_brand_cache.get((brand.id, marketplace_id)) if marketplace_id else None
                    return BrandResolution(
                        status='exact',
                        brand_id=brand.id,
                        canonical_name=brand.name,
                        marketplace_brand_id=mp_data['id'] if mp_data else None,
                        marketplace_brand_ext_id=mp_ext_id,
                        marketplace_brand_name=mp_name,
                        confidence=0.95,
                        source='marketplace_api',
                    )

            suggestions = []
            for s in api_result.get('suggestions', [])[:8]:
                suggestions.append({
                    'name': s.get('name', ''),
                    'marketplace_ext_id': s.get('id'),
                    'score': 0.5,
                })

            if suggestions:
                return BrandResolution(
                    status='uncertain',
                    confidence=0.4,
                    suggestions=suggestions,
                    source='marketplace_api',
                )

        except Exception as e:
            logger.warning(f"Marketplace API brand lookup failed for '{raw_brand}': {e}")

        return None

    def _save_brand_from_marketplace(self, mp_name: str, mp_ext_id: int,
                                      raw_alias: str, marketplace_id: int):
        """Сохранить бренд найденный через API маркетплейса."""
        try:
            from models import db, Brand, BrandAlias, MarketplaceBrand

            name_norm = normalize_for_comparison(mp_name)
            brand = Brand.query.filter_by(name_normalized=name_norm).first()

            if not brand:
                brand = Brand(
                    name=mp_name,
                    name_normalized=name_norm,
                    status='verified',
                )
                db.session.add(brand)
                db.session.flush()

                canon_alias = BrandAlias(
                    brand_id=brand.id,
                    alias=mp_name,
                    alias_normalized=name_norm,
                    source='marketplace_api',
                    confidence=1.0,
                )
                db.session.add(canon_alias)
            elif brand.status == 'pending':
                brand.status = 'verified'

            # Создаём/обновляем MarketplaceBrand
            if marketplace_id:
                mp_brand = MarketplaceBrand.query.filter_by(
                    brand_id=brand.id,
                    marketplace_id=marketplace_id,
                ).first()

                if not mp_brand:
                    mp_brand = MarketplaceBrand(
                        brand_id=brand.id,
                        marketplace_id=marketplace_id,
                        marketplace_brand_name=mp_name,
                        marketplace_brand_id=mp_ext_id,
                        status='verified',
                        verified_at=datetime.utcnow(),
                        is_available=True,
                        last_seen_at=datetime.utcnow(),
                    )
                    db.session.add(mp_brand)
                else:
                    if not mp_brand.marketplace_brand_id and mp_ext_id:
                        mp_brand.marketplace_brand_id = mp_ext_id
                    if mp_brand.status == 'pending':
                        mp_brand.status = 'verified'
                        mp_brand.verified_at = datetime.utcnow()
                    mp_brand.is_available = mp_brand.status == 'verified'
                    mp_brand.last_seen_at = datetime.utcnow()

            # Добавляем raw_alias если отличается
            alias_norm = normalize_for_comparison(raw_alias)
            if alias_norm and alias_norm != name_norm:
                existing_alias = BrandAlias.query.filter_by(alias_normalized=alias_norm).first()
                if not existing_alias:
                    new_alias = BrandAlias(
                        brand_id=brand.id,
                        alias=raw_alias.strip(),
                        alias_normalized=alias_norm,
                        source='auto_matched',
                        confidence=0.95,
                    )
                    db.session.add(new_alias)

            db.session.commit()
            self.invalidate_cache()
            return brand

        except Exception as e:
            logger.warning(f"Failed to save brand from marketplace: {e}")
            try:
                from models import db
                db.session.rollback()
            except Exception:
                pass
            return None

    def _create_unresolved(self, raw_brand: str, normalized: str) -> BrandResolution:
        """Создать pending бренд для неразрешённого имени."""
        try:
            from models import db, Brand, BrandAlias

            brand = Brand.query.filter_by(name_normalized=normalized).first()
            if not brand:
                brand = Brand(
                    name=raw_brand.strip(),
                    name_normalized=normalized,
                    status='pending',
                )
                db.session.add(brand)
                db.session.flush()

                alias = BrandAlias(
                    brand_id=brand.id,
                    alias=raw_brand.strip(),
                    alias_normalized=normalized,
                    source='supplier_csv',
                    confidence=0.5,
                )
                db.session.add(alias)
                db.session.commit()
                self.invalidate_cache()

                logger.info(f"Created pending brand: '{raw_brand}' (id={brand.id})")

            return BrandResolution(
                status='unresolved',
                brand_id=brand.id,
                canonical_name=brand.name,
                confidence=0.0,
                source='new_pending',
            )

        except Exception as e:
            logger.warning(f"Failed to create pending brand for '{raw_brand}': {e}")
            try:
                from models import db
                db.session.rollback()
            except Exception:
                pass
            return BrandResolution(status='unresolved', confidence=0.0)

    def _check_category(self, marketplace_brand_id: int, category_id: int) -> Optional[bool]:
        """Проверить допустимость бренда в категории маркетплейса."""
        if not marketplace_brand_id or not category_id:
            return None

        try:
            from models import BrandCategoryLink

            link = BrandCategoryLink.query.filter_by(
                marketplace_brand_id=marketplace_brand_id,
                category_id=category_id,
            ).first()

            if link:
                return link.is_available
            return None

        except Exception:
            return None

    def _apply_category_scope(
        self, result: BrandResolution, category_id: int,
    ) -> None:
        """Apply exact category availability and provider identity in-place."""
        result.category_valid = None
        # A primary MarketplaceBrand ID is not proof for a category-scoped WB
        # identity. Clear it unless the exact link below supplies one.
        result.marketplace_brand_ext_id = None
        if not result.marketplace_brand_id or not category_id:
            return
        try:
            from models import BrandCategoryLink

            link = BrandCategoryLink.query.filter_by(
                marketplace_brand_id=result.marketplace_brand_id,
                category_id=category_id,
            ).first()
            if link is None:
                return
            result.category_valid = link.is_available
            result.marketplace_brand_ext_id = (
                link.marketplace_external_brand_id
            )
        except Exception:
            return

    # ------------------------------------------------------------------
    # Marketplace-specific helpers
    # ------------------------------------------------------------------

    def get_marketplace_brand(self, brand_id: int, marketplace_id: int) -> Optional[dict]:
        """Получить данные бренда для конкретного маркетплейса."""
        self._ensure_cache()
        return self._mp_brand_cache.get((brand_id, marketplace_id))

    def ensure_marketplace_brand(self, brand_id: int, marketplace_id: int,
                                  marketplace_name: str = None, marketplace_ext_id: int = None) -> Optional[int]:
        """Создать MarketplaceBrand если не существует. Вернуть ID."""
        from models import db, Brand, MarketplaceBrand

        brand = Brand.query.get(brand_id)
        if not brand:
            return None

        mp_brand = MarketplaceBrand.query.filter_by(
            brand_id=brand_id,
            marketplace_id=marketplace_id,
        ).first()

        if mp_brand:
            if marketplace_ext_id and not mp_brand.marketplace_brand_id:
                mp_brand.marketplace_brand_id = marketplace_ext_id
                db.session.commit()
            return mp_brand.id

        mp_brand = MarketplaceBrand(
            brand_id=brand_id,
            marketplace_id=marketplace_id,
            marketplace_brand_name=marketplace_name or brand.name,
            marketplace_brand_id=marketplace_ext_id,
            status='pending',
        )
        db.session.add(mp_brand)
        db.session.commit()
        self.invalidate_cache()
        return mp_brand.id

    # ------------------------------------------------------------------
    # Bulk resolve
    # ------------------------------------------------------------------

    def bulk_resolve(self, items: List[dict], brand_field: str = 'brand',
                     marketplace_id: int = None,
                     category_field: str = 'wb_subject_id') -> List[BrandResolution]:
        """Пакетный резолв для импорта CSV."""
        self._ensure_cache()

        results = []
        for item in items:
            raw_brand = item.get(brand_field, '')
            category_id = item.get(category_field)
            result = self.resolve(raw_brand, marketplace_id=marketplace_id, category_id=category_id)
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # Sync (marketplace-aware)
    # ------------------------------------------------------------------

    def get_sync_progress(self, marketplace_id: int) -> Optional[dict]:
        """Получить текущий прогресс синхронизации брендов."""
        return self._sync_progress.get(marketplace_id)

    def sync_marketplace_brands_async(self, marketplace_id: int, app=None):
        """
        Запуск синхронизации брендов в фоновом потоке.

        Сразу возвращает управление, прогресс можно отслеживать через get_sync_progress().
        app — Flask-приложение (передаётся из роута для гарантированного app_context).
        """
        if marketplace_id in self._sync_progress and self._sync_progress[marketplace_id].get('status') == 'running':
            logger.warning(f"Brand sync already running for marketplace #{marketplace_id}")
            return False

        if not app:
            app = self._get_app()
        if not app:
            logger.error("Brand sync: no Flask app available")
            return False

        self._sync_progress[marketplace_id] = {
            'status': 'running',
            'phase': 'starting',
            'categories_done': 0,
            'categories_total': 0,
            'brands_found': 0,
            'brands_saved': 0,
            'brands_total': 0,
            'errors': 0,
            'started_at': datetime.utcnow().isoformat(),
            'message': 'Запуск синхронизации...',
        }

        def run_sync():
            try:
                with app.app_context():
                    from services.marketplace_service import MarketplaceService
                    wb_client = MarketplaceService.get_wb_client(marketplace_id)
                    if not wb_client:
                        raise RuntimeError('Marketplace API key is not configured')
                    try:
                        result = self.sync_marketplace_brands(
                            marketplace_id, wb_client,
                        )
                        if result.get('skipped'):
                            self._sync_progress[marketplace_id].update({
                                'status': 'done',
                                'phase': 'done',
                                'message': (
                                    'Синхронизация уже выполняется другим процессом.'
                                ),
                            })
                    finally:
                        close = getattr(wb_client, 'close', None)
                        if callable(close):
                            close()
            except Exception as e:
                logger.error(f"Brand sync background task failed: {e}", exc_info=True)
                self._sync_progress[marketplace_id].update({
                    'status': 'error',
                    'message': f'Ошибка: {e}',
                })

        thread = threading.Thread(target=run_sync, daemon=True)
        thread.start()
        return True

    def sync_marketplace_brands(self, marketplace_id: int, marketplace_client) -> dict:
        """Serialize syncs and always leave a durable terminal status."""
        with self._sync_lock_guard:
            sync_lock = self._sync_locks.setdefault(
                marketplace_id, threading.Lock(),
            )
        if not sync_lock.acquire(blocking=False):
            return {'skipped': True, 'reason': 'brand_sync_already_running'}
        lock_fd = None
        try:
            # Gunicorn workers have separate Python locks. A process-shared
            # advisory lock prevents admin/manual sync from overlapping the
            # singleton scheduler in the same container.
            import fcntl
            import os
            lock_path = os.environ.get(
                'BRAND_SYNC_LOCK_FILE',
                '/tmp/seller-platform-brand-sync.lock',
            )
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(lock_fd)
                lock_fd = None
                return {
                    'skipped': True,
                    'reason': 'brand_sync_already_running',
                }
            return self._sync_marketplace_brands_guarded(
                marketplace_id, marketplace_client,
            )
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            sync_lock.release()

    def _sync_marketplace_brands_guarded(
        self, marketplace_id: int, marketplace_client,
    ) -> dict:
        try:
            return self._sync_marketplace_brands(marketplace_id, marketplace_client)
        except Exception as error:
            logger.error(
                'Brand sync for marketplace #%s failed unexpectedly: %s',
                marketplace_id, error, exc_info=True,
            )
            try:
                from models import db, Marketplace
                db.session.rollback()
                marketplace = Marketplace.query.get(marketplace_id)
                if marketplace:
                    marketplace.brands_sync_status = (
                        'partial'
                        if marketplace.brands_synced_at
                        or marketplace.brands_sync_checkpoint
                        else 'failed'
                    )
                    marketplace.brands_sync_error = (
                        f'{type(error).__name__}: brand reference sync failed'
                    )
                    db.session.commit()
            except Exception:
                logger.exception('Could not persist failed brand sync status')
            progress = self._sync_progress.setdefault(marketplace_id, {})
            progress.update({
                'status': 'error',
                'phase': 'done',
                'message': 'Синхронизация справочника брендов завершилась ошибкой.',
            })
            return {
                'errors': 1,
                'error': f'{type(error).__name__}: brand reference sync failed',
            }

    def _sync_marketplace_brands(self, marketplace_id: int, marketplace_client) -> dict:
        """
        Синхронизация справочника брендов маркетплейса в БД.

        Загружает бренды последовательно по включённым категориям,
        сохраняет в БД короткими батчами по 50 штук.
        """
        from models import (
            db, Brand, BrandAlias, Marketplace, MarketplaceBrand,
            MarketplaceCategory, BrandCategoryLink,
        )

        marketplace = Marketplace.query.get(marketplace_id)
        if not marketplace:
            return {'errors': 1, 'error': 'Marketplace not found'}
        sync_started_at = datetime.utcnow()
        marketplace.brands_sync_status = 'running'
        marketplace.brands_sync_error = None
        db.session.commit()

        progress = self._sync_progress.get(marketplace_id)
        if not progress:
            progress = {'status': 'running'}
            self._sync_progress[marketplace_id] = progress

        def update_progress(**kwargs):
            progress.update(kwargs)

        logger.info(f"Starting brand sync for marketplace #{marketplace_id}...")
        stats = {
            'created': 0, 'updated': 0, 'mp_created': 0, 'mp_updated': 0,
            'aliases_created': 0, 'aliases_reactivated': 0,
            'category_links_created': 0, 'category_links_updated': 0,
            'category_links_removed': 0, 'errors': 0, 'total_fetched': 0,
            'categories_completed_this_run': 0,
            'category_identity_conflicts': 0,
            'category_identities_quarantined': 0,
            'db_contention': 0,
        }

        # --- Phase 1: Получаем включённые категории ---
        update_progress(phase='categories', message='Загрузка списка категорий...')

        enabled_cats = MarketplaceCategory.query.filter_by(
            marketplace_id=marketplace_id,
            is_enabled=True,
            is_available=True,
        ).order_by(MarketplaceCategory.subject_id.asc()).all()

        if not enabled_cats:
            logger.warning(f"No enabled categories for marketplace #{marketplace_id}")
            update_progress(
                status='done', phase='done',
                message='Нет включённых категорий для синхронизации.',
            )
            marketplace.brands_sync_status = 'failed'
            marketplace.brands_sync_error = 'No enabled available categories'
            db.session.commit()
            return stats

        subject_ids = sorted({
            int(c.subject_id) for c in enabled_cats if c.subject_id
        })
        total_cats = len(subject_ids)
        logger.info(f"Found {total_cats} enabled categories for brand sync")

        checkpoint = None
        try:
            checkpoint = json.loads(marketplace.brands_sync_checkpoint or 'null')
        except (TypeError, ValueError):
            checkpoint = None
        if not isinstance(checkpoint, dict) or checkpoint.get('subject_ids') != subject_ids:
            checkpoint = {
                'subject_ids': subject_ids,
                'next_index': 0,
                'started_at': sync_started_at.isoformat(),
            }
        try:
            next_index = int(checkpoint.get('next_index') or 0)
        except (TypeError, ValueError):
            next_index = 0
        if next_index < 0 or next_index >= total_cats:
            next_index = 0
            checkpoint['started_at'] = sync_started_at.isoformat()
        remaining_subject_ids = subject_ids[next_index:]

        # --- Phase 2: Загрузка брендов по категориям ---
        # WB API: GET /api/content/v1/brands использует subjectId + next cursor.
        update_progress(
            phase='fetching',
            categories_done=next_index,
            categories_total=total_cats,
            message=(
                f'Загрузка брендов: продолжение с категории '
                f'{next_index + 1}/{total_cats}...'
            ),
        )

        all_brands = {}  # ext_id -> name from trusted category snapshots
        brand_subject_ids = {}  # ext_id -> set[subject_id]
        subject_brand_ids = {}  # subject_id -> set[ext_id]
        trusted_subject_ids = []
        fetch_complete = False
        fetch_errors = []

        def on_progress(done, total, brands_count):
            update_progress(
                categories_done=next_index + done,
                categories_total=total_cats,
                brands_found=brands_count,
                message=(
                    f'Категории: {next_index + done}/{total_cats}, '
                    f'найдено брендов: {brands_count}'
                ),
            )

        try:
            result = marketplace_client.fetch_all_brands(
                subject_ids=remaining_subject_ids,
                top=5000,
                progress_callback=on_progress,
            )
            data = result.get('data', [])
            fetch_complete = result.get('complete') is True
            fetch_errors = result.get('errors') or []
            subject_snapshots = result.get('subject_brands')
            completed_subject_ids = result.get('completed_subject_ids')
            if not isinstance(subject_snapshots, dict) or not isinstance(
                completed_subject_ids, list,
            ):
                fetch_errors.append({
                    'code': 'invalid_subject_snapshot_contract',
                    'error': 'WB brand result has no category membership',
                })
                subject_snapshots = {}
                completed_subject_ids = []

            for expected_subject_id, raw_subject_id in zip(
                remaining_subject_ids, completed_subject_ids,
            ):
                try:
                    subject_id = int(raw_subject_id)
                except (TypeError, ValueError):
                    break
                if subject_id != expected_subject_id:
                    fetch_errors.append({
                        'subject_id': expected_subject_id,
                        'code': 'non_contiguous_subject_snapshot',
                    })
                    break
                snapshot = (
                    subject_snapshots.get(subject_id)
                    if subject_id in subject_snapshots
                    else subject_snapshots.get(str(subject_id))
                )
                if not isinstance(snapshot, list) or not snapshot:
                    fetch_errors.append({
                        'subject_id': subject_id,
                        'code': 'empty_subject_snapshot',
                        'error': 'Empty WB brand snapshot was not applied',
                    })
                    break

                candidate_snapshot = []
                normalized_name_ids = {}
                for brand_data in snapshot:
                    try:
                        ext_id = brand_data.get('id')
                    except AttributeError:
                        candidate_snapshot = []
                        break
                    name = str(brand_data.get('name') or '').strip()
                    if type(ext_id) is not int or ext_id <= 0 or not name:
                        candidate_snapshot = []
                        break
                    name_norm = normalize_for_comparison(name)
                    if not name_norm:
                        candidate_snapshot = []
                        break
                    normalized_name_ids.setdefault(name_norm, set()).add(ext_id)
                    previous_name = all_brands.get(ext_id)
                    if previous_name and previous_name != name:
                        candidate_snapshot = []
                        break
                    candidate_snapshot.append((ext_id, name, name_norm))
                if not candidate_snapshot:
                    fetch_errors.append({
                        'subject_id': subject_id,
                        'code': 'invalid_subject_snapshot',
                    })
                    break

                # WB can expose the same normalized name under several IDs in
                # one category. A card write sends the name, so selecting one
                # of those IDs would manufacture evidence. Quarantine only
                # those identities; the rest of the complete category remains
                # usable and existing links for ambiguous IDs are disabled.
                ambiguous_names = {
                    name_norm
                    for name_norm, external_ids in normalized_name_ids.items()
                    if len(external_ids) > 1
                }
                if ambiguous_names:
                    quarantined_count = sum(
                        1 for _, _, name_norm in candidate_snapshot
                        if name_norm in ambiguous_names
                    )
                    stats['category_identity_conflicts'] += len(ambiguous_names)
                    stats['category_identities_quarantined'] += quarantined_count
                    logger.warning(
                        'Quarantined %s ambiguous WB brand identities in '
                        'subjectId=%s (%s normalized-name conflicts)',
                        quarantined_count,
                        subject_id,
                        len(ambiguous_names),
                    )

                parsed_snapshot = [
                    (ext_id, name)
                    for ext_id, name, name_norm in candidate_snapshot
                    if name_norm not in ambiguous_names
                ]
                if not parsed_snapshot:
                    fetch_errors.append({
                        'subject_id': subject_id,
                        'code': 'subject_snapshot_only_has_ambiguous_identities',
                    })
                    break
                parsed_brand_ids = {
                    ext_id for ext_id, _ in parsed_snapshot
                }
                for ext_id, name in parsed_snapshot:
                    all_brands[ext_id] = name
                    brand_subject_ids.setdefault(ext_id, set()).add(subject_id)
                subject_brand_ids[subject_id] = parsed_brand_ids
                trusted_subject_ids.append(subject_id)

            stats['errors'] += len(fetch_errors)

            # Диагностика
            sample = []
            if isinstance(data, list) and data:
                sample = [{'id': b.get('id'), 'name': b.get('name')} for b in data[:3]]
            debug_info = {
                'method': 'fetch_all_brands (per-category cursor pagination)',
                'total_brands': len(data) if isinstance(data, list) else 'N/A',
                'categories_completed': len(trusted_subject_ids),
                'sample': sample,
            }
            if hasattr(marketplace_client, '_fetch_debug') and marketplace_client._fetch_debug:
                debug_info['first_request'] = marketplace_client._fetch_debug
            update_progress(_debug_first_response=debug_info)

        except Exception as e:
            logger.error(f"Failed to fetch brands: {e}", exc_info=True)
            stats['errors'] += 1
            update_progress(_debug_first_response={
                'error': str(e),
                'type': type(e).__name__,
            })

        stats['total_fetched'] = len(all_brands)
        stats['categories_completed_this_run'] = len(trusted_subject_ids)
        logger.info(
            "Fetched %s unique brands from %s complete categories",
            len(all_brands), len(trusted_subject_ids),
        )

        # Never merge a partial or empty upstream sweep into the last-good
        # registry. This keeps usable bindings and freshness internally
        # consistent while the next scheduled run retries the snapshot.
        if not trusted_subject_ids or not all_brands:
            marketplace.brands_sync_status = (
                'partial'
                if not fetch_complete and marketplace.brands_synced_at
                else 'failed'
            )
            marketplace.brands_sync_error = 'Brand sweep returned no usable data'
            if fetch_errors:
                marketplace.brands_sync_error += f'; {str(fetch_errors[:3])[:1800]}'
            marketplace.brands_sync_checkpoint = (
                json.dumps(checkpoint, ensure_ascii=True, separators=(',', ':'))
                if marketplace.brands_sync_status == 'partial' else None
            )
            db.session.commit()
            update_progress(
                status=(
                    'error' if marketplace.brands_sync_status == 'failed'
                    else 'done'
                ),
                phase='done',
                message=(
                    f'Справочник не обновлён: найдено {stats["total_fetched"]}, '
                    f'ошибок {stats["errors"]}'
                ),
                stats=stats,
            )
            return stats

        # --- Phase 3: Сохранение в БД батчами ---
        update_progress(
            phase='saving',
            brands_total=len(all_brands),
            brands_saved=0,
            message=f'Сохранение {len(all_brands)} брендов...',
        )

        brand_items = list(all_brands.items())
        # Keep each SQLite writer window short enough for interactive writes.
        save_batch_size = 50
        saved = 0

        # Prefetch the working set once. The old path performed up to four
        # SELECTs for every upstream brand and dominated large syncs.
        marketplace_bindings = MarketplaceBrand.query.filter_by(
            marketplace_id=marketplace_id,
        ).all()
        bindings_by_external_id = {}
        bindings_by_id = {
            binding.id: binding for binding in marketplace_bindings
        }

        def register_external_identity(external_id, binding):
            identity = str(int(external_id))
            existing = bindings_by_external_id.get(identity)
            if existing is not None and existing.id != binding.id:
                raise ValueError(
                    'marketplace brand external identity is bound to '
                    'multiple canonical brands'
                )
            bindings_by_external_id[identity] = binding

        for binding in marketplace_bindings:
            if binding.marketplace_brand_id is not None:
                register_external_identity(
                    binding.marketplace_brand_id, binding,
                )
        bindings_by_brand_id = {
            binding.brand_id: binding for binding in marketplace_bindings
        }
        category_names = {
            int(category.subject_id): category.subject_name
            for category in enabled_cats if category.subject_id
        }
        existing_links = BrandCategoryLink.query.join(
            MarketplaceBrand,
            MarketplaceBrand.id == BrandCategoryLink.marketplace_brand_id,
        ).filter(
            MarketplaceBrand.marketplace_id == marketplace_id,
            BrandCategoryLink.category_id.in_(trusted_subject_ids),
        ).all()
        links_by_key = {
            (link.marketplace_brand_id, int(link.category_id)): link
            for link in existing_links
        }

        # Alternative WB IDs are exact identities too. Load only identities
        # present in this bounded upstream batch so a rename of an alternative
        # category ID still resolves to the existing canonical brand.
        current_external_ids = list(all_brands)
        for chunk_start in range(0, len(current_external_ids), 500):
            chunk = current_external_ids[chunk_start:chunk_start + 500]
            identity_links = BrandCategoryLink.query.join(
                MarketplaceBrand,
                MarketplaceBrand.id == BrandCategoryLink.marketplace_brand_id,
            ).filter(
                MarketplaceBrand.marketplace_id == marketplace_id,
                BrandCategoryLink.marketplace_external_brand_id.in_(chunk),
            ).all()
            for identity_link in identity_links:
                binding = bindings_by_id.get(identity_link.marketplace_brand_id)
                if binding is not None:
                    register_external_identity(
                        identity_link.marketplace_external_brand_id, binding,
                    )

        def upsert_category_links(mp_brand, external_id):
            exact_external_id = int(external_id)
            for subject_id in brand_subject_ids.get(external_id, ()):
                key = (mp_brand.id, subject_id)
                link = links_by_key.get(key)
                desired_available = mp_brand.status == 'verified'
                category_name = category_names.get(subject_id)
                if not link:
                    link = BrandCategoryLink(
                        marketplace_brand_id=mp_brand.id,
                        category_id=subject_id,
                        marketplace_external_brand_id=exact_external_id,
                        category_name=category_name,
                        is_available=desired_available,
                        verified_at=sync_started_at,
                    )
                    db.session.add(link)
                    links_by_key[key] = link
                    stats['category_links_created'] += 1
                else:
                    if (
                        link.marketplace_external_brand_id is not None
                        and int(link.marketplace_external_brand_id)
                        != exact_external_id
                    ):
                        raise ValueError(
                            'category brand identity is ambiguous for one '
                            'canonical brand'
                        )
                    changed = (
                        link.is_available != desired_available
                        or link.category_name != category_name
                        or link.marketplace_external_brand_id
                        != exact_external_id
                    )
                    link.is_available = desired_available
                    link.marketplace_external_brand_id = exact_external_id
                    link.category_name = category_name
                    link.verified_at = sync_started_at
                    if changed:
                        stats['category_links_updated'] += 1

        normalized_names = {
            normalize_for_comparison(name) for _, name in brand_items
        }
        brands_by_normalized_name = {}
        aliases_by_normalized_name = {}
        save_error_baseline = stats['errors']
        normalized_names_list = list(normalized_names)
        for chunk_start in range(0, len(normalized_names_list), 500):
            chunk = normalized_names_list[chunk_start:chunk_start + 500]
            for brand in Brand.query.filter(
                Brand.name_normalized.in_(chunk),
            ).all():
                brands_by_normalized_name[brand.name_normalized] = brand
            aliases_by_normalized_name.update({
                alias.alias_normalized: alias
                for alias in BrandAlias.query.filter(
                    BrandAlias.alias_normalized.in_(chunk),
                ).all()
            })
        brands_by_id = {
            brand.id: brand for brand in brands_by_normalized_name.values()
        }
        alias_brand_ids = {
            alias.brand_id for alias in aliases_by_normalized_name.values()
        }
        missing_brand_ids = alias_brand_ids.difference(brands_by_id)
        if missing_brand_ids:
            brands_by_id.update({
                brand.id: brand for brand in Brand.query.filter(
                    Brand.id.in_(missing_brand_ids),
                ).all()
            })

        def ensure_marketplace_alias(brand_id, name, name_norm):
            """Make an upstream canonical name exact-resolvable without takeover."""
            named_brand = brands_by_normalized_name.get(name_norm)
            if named_brand is not None and named_brand.id != brand_id:
                raise ValueError(
                    f'canonical brand name conflict for {name!r}'
                )

            alias = aliases_by_normalized_name.get(name_norm)
            if alias is None:
                alias = BrandAlias(
                    brand_id=brand_id,
                    alias=name,
                    alias_normalized=name_norm,
                    source='marketplace_sync',
                    confidence=1.0,
                )
                db.session.add(alias)
                aliases_by_normalized_name[name_norm] = alias
                stats['aliases_created'] += 1
                return

            if alias.brand_id != brand_id:
                raise ValueError(
                    f'canonical brand alias conflict for {name!r}'
                )
            if alias.is_active:
                return
            if alias.source != 'marketplace_sync':
                raise ValueError(
                    f'inactive managed brand alias blocks {name!r}'
                )
            alias.is_active = True
            stats['aliases_reactivated'] += 1

        def drop_rolled_back_cache_entries():
            """Убирает из локальных кэшей объекты, откатившиеся вместе с
            savepoint: их запись в БД не состоялась, и следующие бренды
            батча не должны ссылаться на transient-объекты."""
            def alive(obj):
                return obj in db.session

            for cache in (
                brands_by_normalized_name,
                brands_by_id,
                aliases_by_normalized_name,
                bindings_by_id,
                bindings_by_brand_id,
                bindings_by_external_id,
                links_by_key,
            ):
                stale_keys = [
                    key for key, obj in cache.items() if not alive(obj)
                ]
                for key in stale_keys:
                    del cache[key]
            marketplace_bindings[:] = [
                binding for binding in marketplace_bindings if alive(binding)
            ]

        def commit_preserving_prefetched_state():
            """End the SQLite transaction without expiring the read caches.

            A normal SQLAlchemy commit expires every prefetched ORM row.  The
            next item would then issue a SELECT before its UPDATE, recreating
            a WAL read snapshot that can fail to upgrade with
            SQLITE_BUSY_SNAPSHOT after any concurrent writer commits.
            """
            session = db.session()
            previous = session.expire_on_commit
            session.expire_on_commit = False
            try:
                session.commit()
            finally:
                session.expire_on_commit = previous

        # All queries needed by the apply phase are complete.  End that read
        # transaction before the first write while retaining the loaded ORM
        # state.  This prevents a hours-old WAL snapshot from being upgraded
        # to a writer after another background task has committed.
        commit_preserving_prefetched_state()

        mutation_stat_keys = (
            'created', 'updated', 'mp_created', 'mp_updated',
            'aliases_created', 'aliases_reactivated',
            'category_links_created', 'category_links_updated',
            'category_links_removed',
        )
        stop_saving = False

        for batch_start in range(0, len(brand_items), save_batch_size):
            batch = brand_items[batch_start:batch_start + save_batch_size]
            batch_stats_before = {
                key: stats[key] for key in mutation_stat_keys
            }

            # Make the outer transaction a writer before opening per-row
            # savepoints.  Without an explicit BEGIN, SQLite can treat the
            # first SAVEPOINT as the outer transaction and RELEASE commits it;
            # a prior read snapshot can also fail its later write upgrade with
            # SQLITE_BUSY_SNAPSHOT.  IMMEDIATE either claims the single writer
            # slot now or lets this background run defer as one unit.
            try:
                db.session.execute(db.text('BEGIN IMMEDIATE'))
            except Exception as e:
                db.session.rollback()
                stats['errors'] += 1
                if _is_sqlite_write_contention(e):
                    stats['db_contention'] += 1
                    logger.warning(
                        'Brand sync deferred before batch apply because the '
                        'SQLite writer is busy; checkpoint was not advanced',
                    )
                else:
                    logger.error('Failed to begin brand sync write batch: %s', e)
                stop_saving = True
                break

            for ext_id, name in batch:
                item_stats_before = {
                    key: stats[key] for key in mutation_stat_keys
                }
                try:
                    name_norm = normalize_for_comparison(name)

                    # Savepoint изолирует row-local ошибку одного
                    # бренда (например, constraint violation), чтобы
                    # сессия осталась рабочей для остального батча.
                    # SQLite BUSY/LOCKED ниже обрабатывается как ошибка
                    # всей транзакции, а не одной строки.
                    with db.session.begin_nested():
                        # External WB ID is stable across a rename. Prefer
                        # it over normalized display name so a rename
                        # updates, not duplicates.
                        mp_brand = bindings_by_external_id.get(str(ext_id))
                        if mp_brand:
                            ensure_marketplace_alias(
                                mp_brand.brand_id, name, name_norm,
                            )
                            previous_status = mp_brand.status
                            if mp_brand.status == 'pending':
                                mp_brand.status = 'verified'
                                mp_brand.verified_at = sync_started_at
                            desired_available = mp_brand.status == 'verified'
                            if (
                                mp_brand.marketplace_brand_name != name
                                or previous_status != mp_brand.status
                                or mp_brand.is_available != desired_available
                            ):
                                stats['mp_updated'] += 1
                            mp_brand.marketplace_brand_name = name
                            mp_brand.last_seen_at = sync_started_at
                            mp_brand.is_available = desired_available
                            upsert_category_links(mp_brand, ext_id)
                            continue

                        # Глобальный бренд
                        existing_alias = aliases_by_normalized_name.get(name_norm)
                        brand = brands_by_normalized_name.get(name_norm)
                        if brand is None and existing_alias is not None:
                            if not existing_alias.is_active:
                                raise ValueError(
                                    f'inactive managed brand alias blocks {name!r}'
                                )
                            brand = brands_by_id.get(existing_alias.brand_id)
                            if brand is None:
                                raise ValueError(
                                    f'canonical brand alias target is missing for {name!r}'
                                )
                        if brand:
                            ensure_marketplace_alias(brand.id, name, name_norm)
                            if brand.status == 'pending':
                                brand.status = 'verified'
                            brand.updated_at = datetime.utcnow()
                            stats['updated'] += 1
                        else:
                            brand = Brand(
                                name=name,
                                name_normalized=name_norm,
                                status='verified',
                            )
                            db.session.add(brand)
                            db.session.flush()
                            brands_by_normalized_name[name_norm] = brand
                            brands_by_id[brand.id] = brand

                            stats['created'] += 1

                            ensure_marketplace_alias(brand.id, name, name_norm)

                        # Привязка к маркетплейсу
                        mp_brand = bindings_by_brand_id.get(brand.id)

                        if not mp_brand:
                            mp_brand = MarketplaceBrand(
                                brand_id=brand.id,
                                marketplace_id=marketplace_id,
                                marketplace_brand_name=name,
                                marketplace_brand_id=ext_id,
                                status='verified',
                                verified_at=sync_started_at,
                                is_available=True,
                                last_seen_at=sync_started_at,
                            )
                            db.session.add(mp_brand)
                            db.session.flush()
                            bindings_by_brand_id[brand.id] = mp_brand
                            bindings_by_id[mp_brand.id] = mp_brand
                            register_external_identity(ext_id, mp_brand)
                            marketplace_bindings.append(mp_brand)
                            stats['mp_created'] += 1
                        else:
                            previous_status = mp_brand.status
                            if mp_brand.status == 'pending':
                                mp_brand.status = 'verified'
                                mp_brand.verified_at = sync_started_at
                            desired_available = mp_brand.status == 'verified'
                            changed = (
                                not mp_brand.marketplace_brand_id
                                or mp_brand.marketplace_brand_name != name
                                or previous_status != mp_brand.status
                                or mp_brand.is_available != desired_available
                            )
                            if not mp_brand.marketplace_brand_id:
                                mp_brand.marketplace_brand_id = ext_id
                            register_external_identity(ext_id, mp_brand)
                            mp_brand.marketplace_brand_name = name
                            mp_brand.last_seen_at = sync_started_at
                            mp_brand.is_available = desired_available
                            if changed:
                                stats['mp_updated'] += 1

                        upsert_category_links(mp_brand, ext_id)

                except Exception as e:
                    for key, value in item_stats_before.items():
                        stats[key] = value
                    if _is_sqlite_write_contention(e):
                        # BUSY/LOCKED is transaction-wide infrastructure
                        # contention, not malformed data in one brand.  A
                        # savepoint cannot make a stale WAL snapshot writable.
                        # Continuing here previously performed an O(N) cache
                        # sweep and emitted a traceback for every remaining
                        # brand, saturating the web process for hours.
                        db.session.rollback()
                        for key, value in batch_stats_before.items():
                            stats[key] = value
                        stats['errors'] += 1
                        stats['db_contention'] += 1
                        stop_saving = True
                        logger.warning(
                            'Brand sync deferred after SQLite write contention; '
                            'checkpoint was not advanced',
                        )
                        break
                    logger.warning(f"Failed to save brand '{name}': {e}")
                    stats['errors'] += 1
                    drop_rolled_back_cache_entries()

            if stop_saving:
                break

            # Коммитим батч
            try:
                commit_preserving_prefetched_state()
            except Exception as e:
                db.session.rollback()
                for key, value in batch_stats_before.items():
                    stats[key] = value
                stats['errors'] += 1
                if _is_sqlite_write_contention(e):
                    stats['db_contention'] += 1
                    logger.warning(
                        'Brand sync batch commit deferred after SQLite write '
                        'contention; checkpoint was not advanced',
                    )
                else:
                    logger.error(f"Failed to commit brand batch: {e}")
                stop_saving = True
                break

            saved += len(batch)
            update_progress(
                brands_saved=saved,
                message=f'Сохранено: {saved}/{len(all_brands)} брендов...',
            )

        save_succeeded = stats['errors'] == save_error_baseline
        if save_succeeded:
            for link in existing_links:
                upstream_ids = subject_brand_ids.get(int(link.category_id), set())
                external_id = link.marketplace_external_brand_id
                if external_id not in upstream_ids:
                    if link.is_available:
                        stats['category_links_removed'] += 1
                    link.is_available = False
                    link.verified_at = sync_started_at

        checkpoint_next_index = (
            next_index + len(trusted_subject_ids)
            if save_succeeded else next_index
        )
        run_complete = (
            save_succeeded
            and fetch_complete
            and not fetch_errors
            and checkpoint_next_index == total_cats
        )
        if run_complete:
            available_binding_ids = {
                row[0] for row in db.session.query(
                    BrandCategoryLink.marketplace_brand_id,
                ).join(
                    MarketplaceBrand,
                    MarketplaceBrand.id == BrandCategoryLink.marketplace_brand_id,
                ).filter(
                    MarketplaceBrand.marketplace_id == marketplace_id,
                    BrandCategoryLink.category_id.in_(subject_ids),
                    BrandCategoryLink.is_available.is_(True),
                ).all()
            }
            for binding in marketplace_bindings:
                desired_available = (
                    binding.status == 'verified'
                    and binding.id in available_binding_ids
                )
                if binding.is_available != desired_available:
                    binding.is_available = desired_available
                    stats['mp_updated'] += 1

        reference_changed = (
            stats['mp_created'] + stats['mp_updated']
            + stats['aliases_created'] + stats['aliases_reactivated']
            + stats['category_links_created']
            + stats['category_links_updated']
            + stats['category_links_removed']
        )
        if reference_changed:
            marketplace.brands_version = (marketplace.brands_version or 0) + 1
        if run_complete:
            marketplace.brands_sync_status = 'success'
            marketplace.brands_sync_error = None
            marketplace.brands_synced_at = sync_started_at
            marketplace.brands_sync_checkpoint = None
        else:
            checkpoint['next_index'] = checkpoint_next_index
            marketplace.brands_sync_checkpoint = json.dumps(
                checkpoint, ensure_ascii=True, separators=(',', ':'),
            )
            marketplace.brands_sync_status = 'partial'
            marketplace.brands_sync_error = (
                f'Brand sweep checkpoint {checkpoint_next_index}/{total_cats}; '
                f'errors={stats["errors"]}'
            )
            if stats['db_contention']:
                marketplace.brands_sync_error += '; deferred=sqlite_write_contention'
        db.session.commit()
        self.invalidate_cache()

        progress_status = (
            'error' if marketplace.brands_sync_status == 'failed' else 'done'
        )
        update_progress(
            status=progress_status,
            phase='done',
            message=f'Готово: найдено {stats["total_fetched"]}, '
                    f'создано {stats["created"]}, обновлено {stats["updated"]}, '
                    f'ошибок {stats["errors"]}',
            stats=stats,
        )
        logger.info(f"Brand sync for marketplace #{marketplace_id} complete: {stats}")
        return stats

    # Backward-compatible alias
    def sync_wb_brands(self, wb_client) -> dict:
        """Обратная совместимость: синхронизация WB брендов."""
        from models import Marketplace
        wb = Marketplace.query.filter_by(code='wb').first()
        if wb:
            return self.sync_marketplace_brands(wb.id, wb_client)
        return {'error': 'WB marketplace not found'}

    # ------------------------------------------------------------------
    # Category validation (marketplace-aware)
    # ------------------------------------------------------------------

    def validate_brand_for_category(self, marketplace_brand_id: int, category_id: int,
                                     marketplace_client=None) -> Optional[bool]:
        """Проверить и сохранить допустимость бренда в категории маркетплейса."""
        from models import db, MarketplaceBrand, BrandCategoryLink

        mp_brand = MarketplaceBrand.query.get(marketplace_brand_id)
        if not mp_brand:
            return False
        if mp_brand.status != 'verified':
            return False

        link = BrandCategoryLink.query.filter_by(
            marketplace_brand_id=marketplace_brand_id,
            category_id=category_id,
        ).first()

        if link and link.verified_at:
            age = (datetime.utcnow() - link.verified_at).total_seconds()
            if age < 86400 and (
                not link.is_available
                or link.marketplace_external_brand_id is not None
            ):
                return link.is_available

        if not marketplace_client:
            return None

        try:
            result = marketplace_client.search_brands(
                mp_brand.marketplace_brand_name,
                top=50,
                subject_id=category_id,
            )
            wb_brands = result.get('data', [])

            brand_alnum = normalize_alphanumeric(mp_brand.marketplace_brand_name)
            exact_ids = set()
            invalid_exact_identity = False
            for wb_brand in wb_brands:
                if normalize_alphanumeric(wb_brand.get('name', '')) != brand_alnum:
                    continue
                try:
                    exact_id = wb_brand.get('id')
                except AttributeError:
                    invalid_exact_identity = True
                    continue
                if type(exact_id) is not int or exact_id <= 0:
                    invalid_exact_identity = True
                    continue
                exact_ids.add(exact_id)

            # A same-category name resolving to multiple provider IDs is not
            # an exact identity and must not overwrite last-good evidence.
            if invalid_exact_identity or len(exact_ids) > 1:
                return None
            is_available = len(exact_ids) == 1
            if not is_available and result.get('complete') is not True:
                return None

            exact_external_id = next(iter(exact_ids), None)
            if (
                is_available
                and link
                and link.marketplace_external_brand_id is not None
                and int(link.marketplace_external_brand_id)
                != exact_external_id
            ):
                return None

            if link:
                link.is_available = is_available
                link.verified_at = datetime.utcnow()
                if is_available:
                    link.marketplace_external_brand_id = exact_external_id
            else:
                link = BrandCategoryLink(
                    marketplace_brand_id=marketplace_brand_id,
                    category_id=category_id,
                    marketplace_external_brand_id=exact_external_id,
                    is_available=is_available,
                    verified_at=datetime.utcnow(),
                )
                db.session.add(link)

            if is_available and mp_brand.marketplace_brand_id is None:
                mp_brand.marketplace_brand_id = exact_external_id

            db.session.commit()
            return is_available

        except Exception as e:
            logger.warning(f"Category validation failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Brand management
    # ------------------------------------------------------------------

    def merge_brands(self, source_brand_id: int, target_brand_id: int) -> dict:
        """Объединить source бренд в target. Переносит aliases, marketplace_brands, products."""
        from models import (
            db, Brand, BrandAlias, BrandCategoryLink, MarketplaceBrand,
            ImportedProduct, SupplierProduct,
        )

        source = Brand.query.get(source_brand_id)
        target = Brand.query.get(target_brand_id)

        if not source or not target:
            raise ValueError("Brand not found")
        if source_brand_id == target_brand_id:
            raise ValueError("Cannot merge brand with itself")

        stats = {'aliases_moved': 0, 'mp_brands_moved': 0,
                 'imported_products_updated': 0, 'supplier_products_updated': 0}

        # Preflight before mutating aliases/products. Two different exact IDs
        # in one marketplace category cannot be collapsed into one safe link.
        target_bindings = {
            binding.marketplace_id: binding
            for binding in MarketplaceBrand.query.filter_by(
                brand_id=target_brand_id,
            ).all()
        }
        for source_binding in MarketplaceBrand.query.filter_by(
            brand_id=source_brand_id,
        ).all():
            target_binding = target_bindings.get(source_binding.marketplace_id)
            if target_binding is None:
                continue
            target_links = {
                int(link.category_id): link
                for link in BrandCategoryLink.query.filter_by(
                    marketplace_brand_id=target_binding.id,
                ).all()
            }
            for source_link in BrandCategoryLink.query.filter_by(
                marketplace_brand_id=source_binding.id,
            ).all():
                target_link = target_links.get(int(source_link.category_id))
                if (
                    target_link
                    and source_link.marketplace_external_brand_id is not None
                    and target_link.marketplace_external_brand_id is not None
                    and int(source_link.marketplace_external_brand_id)
                    != int(target_link.marketplace_external_brand_id)
                ):
                    raise ValueError(
                        'Cannot merge brands with conflicting exact '
                        'marketplace category identities'
                    )

        # Переносим aliases
        for alias in BrandAlias.query.filter_by(brand_id=source_brand_id).all():
            existing = BrandAlias.query.filter_by(
                alias_normalized=alias.alias_normalized,
                brand_id=target_brand_id,
            ).first()
            if existing:
                db.session.delete(alias)
            else:
                alias.brand_id = target_brand_id
                stats['aliases_moved'] += 1

        # Переносим marketplace_brands
        for mp_brand in MarketplaceBrand.query.filter_by(brand_id=source_brand_id).all():
            existing = MarketplaceBrand.query.filter_by(
                brand_id=target_brand_id,
                marketplace_id=mp_brand.marketplace_id,
            ).first()
            if existing:
                # Переносим category_links на существующий
                for link in BrandCategoryLink.query.filter_by(marketplace_brand_id=mp_brand.id).all():
                    dup = BrandCategoryLink.query.filter_by(
                        marketplace_brand_id=existing.id,
                        category_id=link.category_id,
                    ).first()
                    if dup:
                        if (
                            dup.marketplace_external_brand_id is None
                            and link.marketplace_external_brand_id is not None
                        ):
                            dup.marketplace_external_brand_id = (
                                link.marketplace_external_brand_id
                            )
                        db.session.delete(link)
                    else:
                        link.marketplace_brand_id = existing.id
                db.session.delete(mp_brand)
            else:
                mp_brand.brand_id = target_brand_id
                stats['mp_brands_moved'] += 1

        # Обновляем products
        stats['imported_products_updated'] = ImportedProduct.query.filter_by(
            resolved_brand_id=source_brand_id
        ).update({'resolved_brand_id': target_brand_id})

        stats['supplier_products_updated'] = SupplierProduct.query.filter_by(
            resolved_brand_id=source_brand_id
        ).update({'resolved_brand_id': target_brand_id})

        db.session.delete(source)
        db.session.commit()

        self.invalidate_cache()
        logger.info(f"Merged brand #{source_brand_id} into #{target_brand_id}: {stats}")
        return stats

    def add_alias(self, brand_id: int, alias: str, source: str = 'manual',
                  confidence: float = 1.0, supplier_id: int = None) -> Optional[dict]:
        """Добавить alias к бренду."""
        from models import db, Brand, BrandAlias

        brand = Brand.query.get(brand_id)
        if not brand:
            return None

        alias_norm = normalize_for_comparison(alias)
        if not alias_norm:
            return None

        existing = BrandAlias.query.filter_by(alias_normalized=alias_norm).first()
        if existing:
            return None

        new_alias = BrandAlias(
            brand_id=brand_id,
            alias=alias.strip(),
            alias_normalized=alias_norm,
            source=source,
            confidence=confidence,
            supplier_id=supplier_id,
        )
        db.session.add(new_alias)
        db.session.commit()

        self.invalidate_cache()
        return new_alias.to_dict()

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    def revalidate_marketplace_brands(self, marketplace_id: int, marketplace_client) -> dict:
        """Перепроверка verified брендов на маркетплейсе."""
        from models import db, MarketplaceBrand

        stats = {'checked': 0, 'still_valid': 0, 'invalidated': 0, 'errors': 0}

        mp_brands = MarketplaceBrand.query.filter_by(
            marketplace_id=marketplace_id,
            status='verified',
        ).all()

        for mp_brand in mp_brands:
            try:
                stats['checked'] += 1
                links = mp_brand.category_links.all()
                if not links:
                    stats['errors'] += 1
                    continue

                any_valid = False
                all_conclusive = True
                matched_brand = None
                for link in links:
                    result = marketplace_client.validate_brand(
                        mp_brand.marketplace_brand_name,
                        subject_id=link.category_id,
                    )
                    if result.get('valid') and result.get('exact_match'):
                        try:
                            matched_external_id = result['exact_match'].get('id')
                        except AttributeError:
                            all_conclusive = False
                            continue
                        if type(matched_external_id) is not int or (
                            matched_external_id <= 0
                        ) or (
                            link.marketplace_external_brand_id is not None
                            and int(link.marketplace_external_brand_id)
                            != matched_external_id
                        ):
                            all_conclusive = False
                            continue
                        any_valid = True
                        matched_brand = result['exact_match']
                        link.is_available = True
                        link.marketplace_external_brand_id = matched_external_id
                        link.verified_at = datetime.utcnow()
                    elif result.get('complete') is True:
                        link.is_available = False
                        link.verified_at = datetime.utcnow()
                    else:
                        all_conclusive = False

                if any_valid:
                    if matched_brand.get('id') and not mp_brand.marketplace_brand_id:
                        mp_brand.marketplace_brand_id = matched_brand['id']
                    mp_brand.status = 'verified'
                    mp_brand.verified_at = datetime.utcnow()
                    mp_brand.is_available = True
                    mp_brand.last_seen_at = datetime.utcnow()
                    stats['still_valid'] += 1
                elif all_conclusive:
                    mp_brand.status = 'needs_review'
                    mp_brand.is_available = False
                    stats['invalidated'] += 1
                else:
                    stats['errors'] += 1

                # Коммит на каждый бренд: без него первый же autoflush в
                # следующей итерации открывал write-транзакцию, которая жила
                # через network validate_brand + sleep до конца ВСЕХ брендов,
                # и параллельные писатели падали с «database is locked».
                db.session.commit()
                time.sleep(0.2)

            except Exception as e:
                logger.warning(f"Revalidation failed for mp_brand '{mp_brand.marketplace_brand_name}': {e}")
                stats['errors'] += 1
                db.session.rollback()

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

        self.invalidate_cache()
        logger.info(f"Marketplace brand revalidation complete: {stats}")
        return stats

    def auto_resolve_pending(self, marketplace_client, marketplace_id: int = None) -> dict:
        """Legacy hook; the complete registry sync resolves exact pending names."""
        from models import Brand

        pending_count = Brand.query.filter_by(status='pending').limit(50).count()
        logger.info(
            'Pending brand auto-resolve skipped for %s rows: WB validation '
            'requires a typed subject_id',
            pending_count,
        )
        return {
            'checked': 0,
            'resolved': 0,
            'still_pending': pending_count,
            'errors': 0,
            'skipped': pending_count,
            'reason': 'category_scope_required',
        }

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Статистика для dashboard."""
        try:
            from models import Brand, BrandAlias, MarketplaceBrand
            from sqlalchemy import func

            total = Brand.query.count()
            by_status = dict(
                Brand.query.with_entities(Brand.status, func.count(Brand.id))
                .group_by(Brand.status).all()
            )
            total_aliases = BrandAlias.query.filter_by(is_active=True).count()
            total_mp_brands = MarketplaceBrand.query.count()

            return {
                'total_brands': total,
                'verified': by_status.get('verified', 0),
                'pending': by_status.get('pending', 0),
                'needs_review': by_status.get('needs_review', 0),
                'rejected': by_status.get('rejected', 0),
                'total_aliases': total_aliases,
                'total_marketplace_brands': total_mp_brands,
                'cache_loaded': self._cache_loaded,
                'cache_size': len(self._alias_cache),
            }
        except Exception as e:
            logger.warning(f"Failed to get brand stats: {e}")
            return {}


# Глобальный инстанс
_brand_engine = None


def get_brand_engine(app=None) -> BrandEngine:
    """Получить глобальный инстанс BrandEngine."""
    global _brand_engine
    if _brand_engine is None:
        _brand_engine = BrandEngine(app=app)
    return _brand_engine
