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
import logging
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('brand_engine')


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

            for mb in MarketplaceBrand.query.all():
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
                result.category_valid = self._check_category(result.marketplace_brand_id, category_id)
            return result

        # Step 2: Alphanumeric exact match
        result = self._match_alphanumeric(raw_brand, marketplace_id)
        if result:
            if category_id and marketplace_id:
                result.category_valid = self._check_category(result.marketplace_brand_id, category_id)
            return result

        # Step 3: Fuzzy match
        result = self._match_fuzzy(normalized, raw_brand, marketplace_id)
        if result:
            if category_id and marketplace_id:
                result.category_valid = self._check_category(result.marketplace_brand_id, category_id)
            return result

        # Step 4: Marketplace API поиск
        if marketplace_client:
            result = self._match_marketplace_api(raw_brand, marketplace_id, marketplace_client)
            if result:
                if category_id and marketplace_id:
                    result.category_valid = self._check_category(result.marketplace_brand_id, category_id)
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
                                marketplace_client) -> Optional[BrandResolution]:
        """Step 4: Поиск через API маркетплейса и сохранение результата."""
        try:
            # Используем validate_brand (WB-совместимый интерфейс)
            api_result = marketplace_client.validate_brand(raw_brand)

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
                    )
                    db.session.add(mp_brand)
                else:
                    if not mp_brand.marketplace_brand_id and mp_ext_id:
                        mp_brand.marketplace_brand_id = mp_ext_id
                    if mp_brand.status == 'pending':
                        mp_brand.status = 'verified'
                        mp_brand.verified_at = datetime.utcnow()

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

    def sync_marketplace_brands_async(self, marketplace_id: int, api_key: str, app=None):
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
                    from services.wb_api_client import WildberriesAPIClient
                    with WildberriesAPIClient(api_key) as wb_client:
                        self.sync_marketplace_brands(marketplace_id, wb_client)
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
        """
        Синхронизация справочника брендов маркетплейса в БД.

        Загружает бренды последовательно по включённым категориям,
        сохраняет в БД батчами по 200 штук.
        """
        from models import db, Brand, BrandAlias, MarketplaceBrand, MarketplaceCategory

        progress = self._sync_progress.get(marketplace_id)
        if not progress:
            progress = {'status': 'running'}
            self._sync_progress[marketplace_id] = progress

        def update_progress(**kwargs):
            progress.update(kwargs)

        logger.info(f"Starting brand sync for marketplace #{marketplace_id}...")
        stats = {'created': 0, 'updated': 0, 'mp_created': 0, 'errors': 0, 'total_fetched': 0}

        # --- Phase 1: Получаем включённые категории ---
        update_progress(phase='categories', message='Загрузка списка категорий...')

        enabled_cats = MarketplaceCategory.query.filter_by(
            marketplace_id=marketplace_id,
            is_enabled=True,
        ).all()

        if not enabled_cats:
            logger.warning(f"No enabled categories for marketplace #{marketplace_id}")
            update_progress(
                status='done', phase='done',
                message='Нет включённых категорий для синхронизации.',
            )
            return stats

        subject_ids = [c.subject_id for c in enabled_cats if c.subject_id]
        total_cats = len(subject_ids)
        update_progress(categories_total=total_cats,
                        message=f'Загрузка брендов из {total_cats} категорий...')
        logger.info(f"Found {total_cats} enabled categories for brand sync")

        # --- Phase 2: Последовательная загрузка брендов ---
        update_progress(phase='fetching')
        all_brands = {}  # ext_id -> name

        for i, subject_id in enumerate(subject_ids):
            try:
                result = marketplace_client.get_brands_by_subject(subject_id)
                data = result.get('data', [])

                # Диагностика первого ответа — записываем в progress
                if i == 0:
                    sample = data[:2] if isinstance(data, list) else str(data)[:300]
                    update_progress(_debug_first_response={
                        'type': type(data).__name__,
                        'len': len(data) if isinstance(data, list) else 'N/A',
                        'keys': list(result.keys()),
                        'sample': sample,
                        'subject_id': subject_id,
                    })

                if isinstance(data, list):
                    for brand_data in data:
                        ext_id = brand_data.get('id')
                        name = brand_data.get('name', '')
                        if ext_id and name:
                            all_brands[ext_id] = name
                else:
                    logger.warning(f"Unexpected data type for subjectId={subject_id}: "
                                   f"{type(data).__name__}: {str(data)[:200]}")
            except Exception as e:
                logger.warning(f"Failed to fetch brands for subjectId={subject_id}: {e}")
                stats['errors'] += 1
                if i == 0:
                    update_progress(_debug_first_response={
                        'error': str(e),
                        'type': type(e).__name__,
                        'subject_id': subject_id,
                    })

            update_progress(
                categories_done=i + 1,
                brands_found=len(all_brands),
                errors=stats['errors'],
                message=f'Категории: {i + 1}/{total_cats}, найдено брендов: {len(all_brands)}'
                        + (f', ошибок: {stats["errors"]}' if stats['errors'] else ''),
            )

            # Пауза между запросами чтобы не перегрузить API
            if i + 1 < total_cats:
                time.sleep(0.15)

        stats['total_fetched'] = len(all_brands)
        logger.info(f"Fetched {len(all_brands)} brands from marketplace #{marketplace_id}")

        # --- Phase 3: Сохранение в БД батчами ---
        update_progress(
            phase='saving',
            brands_total=len(all_brands),
            brands_saved=0,
            message=f'Сохранение {len(all_brands)} брендов...',
        )

        brand_items = list(all_brands.items())
        save_batch_size = 200
        saved = 0

        for batch_start in range(0, len(brand_items), save_batch_size):
            batch = brand_items[batch_start:batch_start + save_batch_size]

            for ext_id, name in batch:
                try:
                    name_norm = normalize_for_comparison(name)

                    # Глобальный бренд
                    brand = Brand.query.filter_by(name_normalized=name_norm).first()
                    if brand:
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

                        existing = BrandAlias.query.filter_by(alias_normalized=name_norm).first()
                        if not existing:
                            alias = BrandAlias(
                                brand_id=brand.id,
                                alias=name,
                                alias_normalized=name_norm,
                                source='marketplace_sync',
                                confidence=1.0,
                            )
                            db.session.add(alias)

                        stats['created'] += 1

                    # Привязка к маркетплейсу
                    mp_brand = MarketplaceBrand.query.filter_by(
                        brand_id=brand.id,
                        marketplace_id=marketplace_id,
                    ).first()

                    if not mp_brand:
                        mp_brand = MarketplaceBrand(
                            brand_id=brand.id,
                            marketplace_id=marketplace_id,
                            marketplace_brand_name=name,
                            marketplace_brand_id=ext_id,
                            status='verified',
                            verified_at=datetime.utcnow(),
                        )
                        db.session.add(mp_brand)
                        stats['mp_created'] += 1
                    else:
                        if not mp_brand.marketplace_brand_id:
                            mp_brand.marketplace_brand_id = ext_id
                        mp_brand.marketplace_brand_name = name
                        if mp_brand.status == 'pending':
                            mp_brand.status = 'verified'
                            mp_brand.verified_at = datetime.utcnow()

                except Exception as e:
                    logger.warning(f"Failed to save brand '{name}': {e}")
                    stats['errors'] += 1

            # Коммитим батч
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.error(f"Failed to commit brand batch: {e}")
                stats['errors'] += len(batch)

            saved += len(batch)
            update_progress(
                brands_saved=saved,
                message=f'Сохранено: {saved}/{len(all_brands)} брендов...',
            )

        self.invalidate_cache()

        update_progress(
            status='done',
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
                                     marketplace_client=None) -> bool:
        """Проверить и сохранить допустимость бренда в категории маркетплейса."""
        from models import db, MarketplaceBrand, BrandCategoryLink

        mp_brand = MarketplaceBrand.query.get(marketplace_brand_id)
        if not mp_brand:
            return False

        link = BrandCategoryLink.query.filter_by(
            marketplace_brand_id=marketplace_brand_id,
            category_id=category_id,
        ).first()

        if link and link.verified_at:
            age = (datetime.utcnow() - link.verified_at).total_seconds()
            if age < 86400:
                return link.is_available

        if not marketplace_client:
            return link.is_available if link else True

        try:
            result = marketplace_client.search_brands(mp_brand.marketplace_brand_name, top=50)
            wb_brands = result.get('data', [])

            brand_alnum = normalize_alphanumeric(mp_brand.marketplace_brand_name)
            is_available = any(
                normalize_alphanumeric(wb.get('name', '')) == brand_alnum
                for wb in wb_brands
            )

            if link:
                link.is_available = is_available
                link.verified_at = datetime.utcnow()
            else:
                link = BrandCategoryLink(
                    marketplace_brand_id=marketplace_brand_id,
                    category_id=category_id,
                    is_available=is_available,
                    verified_at=datetime.utcnow(),
                )
                db.session.add(link)

            db.session.commit()
            return is_available

        except Exception as e:
            logger.warning(f"Category validation failed: {e}")
            return True

    # ------------------------------------------------------------------
    # Brand management
    # ------------------------------------------------------------------

    def merge_brands(self, source_brand_id: int, target_brand_id: int) -> dict:
        """Объединить source бренд в target. Переносит aliases, marketplace_brands, products."""
        from models import db, Brand, BrandAlias, MarketplaceBrand, ImportedProduct, SupplierProduct

        source = Brand.query.get(source_brand_id)
        target = Brand.query.get(target_brand_id)

        if not source or not target:
            raise ValueError("Brand not found")
        if source_brand_id == target_brand_id:
            raise ValueError("Cannot merge brand with itself")

        stats = {'aliases_moved': 0, 'mp_brands_moved': 0,
                 'imported_products_updated': 0, 'supplier_products_updated': 0}

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
                from models import BrandCategoryLink
                for link in BrandCategoryLink.query.filter_by(marketplace_brand_id=mp_brand.id).all():
                    dup = BrandCategoryLink.query.filter_by(
                        marketplace_brand_id=existing.id,
                        category_id=link.category_id,
                    ).first()
                    if dup:
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
                result = marketplace_client.validate_brand(mp_brand.marketplace_brand_name)
                stats['checked'] += 1

                if result.get('valid'):
                    match = result.get('exact_match', {})
                    if match.get('id') and not mp_brand.marketplace_brand_id:
                        mp_brand.marketplace_brand_id = match['id']
                    mp_brand.verified_at = datetime.utcnow()
                    stats['still_valid'] += 1
                else:
                    mp_brand.status = 'needs_review'
                    stats['invalidated'] += 1

                time.sleep(0.2)

            except Exception as e:
                logger.warning(f"Revalidation failed for mp_brand '{mp_brand.marketplace_brand_name}': {e}")
                stats['errors'] += 1

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

        self.invalidate_cache()
        logger.info(f"Marketplace brand revalidation complete: {stats}")
        return stats

    def auto_resolve_pending(self, marketplace_client, marketplace_id: int = None) -> dict:
        """Попытка авто-резолва pending брендов."""
        from models import db, Brand, MarketplaceBrand

        stats = {'checked': 0, 'resolved': 0, 'still_pending': 0, 'errors': 0}

        pending_brands = Brand.query.filter_by(status='pending').limit(50).all()

        for brand in pending_brands:
            try:
                stats['checked'] += 1
                result = marketplace_client.validate_brand(brand.name)

                if result.get('valid') and result.get('exact_match'):
                    match = result['exact_match']
                    mp_name = match.get('name', '')
                    mp_ext_id = match.get('id')

                    if mp_name and normalize_alphanumeric(mp_name) == normalize_alphanumeric(brand.name):
                        brand.name = mp_name
                        brand.name_normalized = normalize_for_comparison(mp_name)
                    brand.status = 'verified'

                    # Создаём MarketplaceBrand
                    if marketplace_id:
                        existing_mp = MarketplaceBrand.query.filter_by(
                            brand_id=brand.id,
                            marketplace_id=marketplace_id,
                        ).first()
                        if not existing_mp:
                            mp_brand = MarketplaceBrand(
                                brand_id=brand.id,
                                marketplace_id=marketplace_id,
                                marketplace_brand_name=mp_name or brand.name,
                                marketplace_brand_id=mp_ext_id,
                                status='verified',
                                verified_at=datetime.utcnow(),
                            )
                            db.session.add(mp_brand)

                    stats['resolved'] += 1
                else:
                    stats['still_pending'] += 1

                time.sleep(0.3)

            except Exception as e:
                logger.warning(f"Auto-resolve failed for brand '{brand.name}': {e}")
                stats['errors'] += 1

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

        self.invalidate_cache()
        logger.info(f"Auto-resolve pending complete: {stats}")
        return stats

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
