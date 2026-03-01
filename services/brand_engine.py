# -*- coding: utf-8 -*-
"""
Brand Engine — централизованный движок валидации и резолва брендов.

Заменяет разрозненную логику (BRAND_CANONICAL dict, BrandCache, прямые вызовы WB API)
единым pipeline: normalize → alias lookup → fuzzy → WB cache → category check.
"""
import logging
import re
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
    wb_brand_id: Optional[int] = None
    confidence: float = 0.0
    suggestions: list = field(default_factory=list)
    category_valid: Optional[bool] = None
    source: str = ''  # alias_exact, alias_fuzzy, wb_cache, wb_api

    def to_dict(self) -> dict:
        return {
            'status': self.status,
            'brand_id': self.brand_id,
            'canonical_name': self.canonical_name,
            'wb_brand_id': self.wb_brand_id,
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

    Работает с БД (Brand, BrandAlias) как source of truth,
    с in-memory кэшем для быстрого lookup.
    """

    def __init__(self, app=None):
        self._app = app
        self._alias_cache: Dict[str, Tuple[int, str, int]] = {}  # norm -> (brand_id, canonical_name, wb_brand_id)
        self._brand_cache: Dict[int, dict] = {}  # brand_id -> {name, wb_brand_id, status}
        self._cache_loaded = False
        self._cache_lock = threading.Lock()
        self._last_cache_load: float = 0
        self._cache_ttl: int = 300  # 5 минут

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
        """Загрузить все aliases и бренды из БД в память."""
        try:
            from models import Brand, BrandAlias

            alias_cache = {}
            brand_cache = {}

            brands = Brand.query.all()
            for b in brands:
                brand_cache[b.id] = {
                    'name': b.name,
                    'name_normalized': b.name_normalized,
                    'wb_brand_id': b.wb_brand_id,
                    'status': b.status,
                }

            aliases = BrandAlias.query.filter_by(is_active=True).all()
            for a in aliases:
                brand_data = brand_cache.get(a.brand_id)
                if brand_data:
                    alias_cache[a.alias_normalized] = (
                        a.brand_id,
                        brand_data['name'],
                        brand_data.get('wb_brand_id'),
                    )

            self._alias_cache = alias_cache
            self._brand_cache = brand_cache
            self._cache_loaded = True
            self._last_cache_load = time.time()
            logger.debug(f"Brand cache loaded: {len(brand_cache)} brands, {len(alias_cache)} aliases")

        except Exception as e:
            logger.warning(f"Failed to load brand cache: {e}")

    def invalidate_cache(self):
        """Принудительно инвалидировать кэш."""
        with self._cache_lock:
            self._cache_loaded = False
            self._alias_cache = {}
            self._brand_cache = {}

    # ------------------------------------------------------------------
    # Resolve pipeline
    # ------------------------------------------------------------------

    def resolve(self, raw_brand: str, subject_id: int = None, wb_client=None) -> BrandResolution:
        """
        Главный метод — резолвит сырой бренд в каноническое имя WB.

        Pipeline:
        1. Нормализация (strip, unicode normalize)
        2. Exact match по BrandAlias.alias_normalized
        3. Fuzzy match по alias кэшу (SequenceMatcher, threshold 0.85)
        4. Поиск через WB API (если wb_client передан)
        5. Если subject_id — проверка допустимости бренда в категории

        Args:
            raw_brand: Сырой бренд от поставщика / AI
            subject_id: ID предмета WB (для проверки категории)
            wb_client: WildberriesAPIClient (опционально, для API валидации)

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
        result = self._match_exact(normalized)
        if result:
            if subject_id:
                result.category_valid = self._check_category(result.brand_id, subject_id)
            return result

        # Step 2: Alphanumeric exact match (LOVETOYS == Love Toys)
        result = self._match_alphanumeric(raw_brand)
        if result:
            if subject_id:
                result.category_valid = self._check_category(result.brand_id, subject_id)
            return result

        # Step 3: Fuzzy match по alias кэшу
        result = self._match_fuzzy(normalized, raw_brand)
        if result:
            if subject_id:
                result.category_valid = self._check_category(result.brand_id, subject_id)
            return result

        # Step 4: WB API поиск (если клиент доступен)
        if wb_client:
            result = self._match_wb_api(raw_brand, wb_client)
            if result:
                if subject_id:
                    result.category_valid = self._check_category(result.brand_id, subject_id)
                return result

        # Step 5: Не найден — создаём pending бренд
        return self._create_unresolved(raw_brand, normalized)

    def _match_exact(self, normalized: str) -> Optional[BrandResolution]:
        """Step 1: Точное совпадение по alias_normalized."""
        cached = self._alias_cache.get(normalized)
        if cached:
            brand_id, canonical_name, wb_brand_id = cached
            return BrandResolution(
                status='exact',
                brand_id=brand_id,
                canonical_name=canonical_name,
                wb_brand_id=wb_brand_id,
                confidence=1.0,
                source='alias_exact',
            )
        return None

    def _match_alphanumeric(self, raw_brand: str) -> Optional[BrandResolution]:
        """Step 2: Совпадение по alphanumeric (без пробелов и спецсимволов)."""
        raw_alnum = normalize_alphanumeric(raw_brand)
        if not raw_alnum or len(raw_alnum) < 2:
            return None

        for alias_norm, (brand_id, canonical_name, wb_brand_id) in self._alias_cache.items():
            alias_alnum = normalize_alphanumeric(alias_norm)
            if alias_alnum == raw_alnum:
                return BrandResolution(
                    status='exact',
                    brand_id=brand_id,
                    canonical_name=canonical_name,
                    wb_brand_id=wb_brand_id,
                    confidence=0.98,
                    source='alias_alphanumeric',
                )
        return None

    def _match_fuzzy(self, normalized: str, raw_brand: str) -> Optional[BrandResolution]:
        """Step 3: Fuzzy match по кэшу aliases."""
        if len(normalized) < 2:
            return None

        raw_alnum = normalize_alphanumeric(raw_brand)
        prefix = normalized[:3] if len(normalized) >= 3 else normalized
        first_char = normalized[0]

        best_score = 0.0
        best_brand_id = None
        best_canonical = None
        best_wb_id = None
        suggestions = []

        for alias_norm, (brand_id, canonical_name, wb_brand_id) in self._alias_cache.items():
            # Оптимизация: проверяем только потенциально похожие
            if not (alias_norm.startswith(first_char) or
                    alias_norm.startswith(prefix) or
                    prefix in alias_norm or
                    normalized in alias_norm or
                    alias_norm in normalized):
                continue

            similarity = SequenceMatcher(None, normalized, alias_norm).ratio()

            # Бонус за containment
            if normalized in alias_norm or alias_norm in normalized:
                similarity = min(1.0, similarity + 0.2)

            # Бонус за общий prefix
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
                best_wb_id = wb_brand_id
            elif similarity >= 0.5:
                suggestions.append({
                    'brand_id': brand_id,
                    'name': canonical_name,
                    'score': similarity,
                })

        suggestions.sort(key=lambda x: x['score'], reverse=True)

        if best_score >= 0.85:
            return BrandResolution(
                status='confident',
                brand_id=best_brand_id,
                canonical_name=best_canonical,
                wb_brand_id=best_wb_id,
                confidence=best_score,
                suggestions=suggestions[:5],
                source='alias_fuzzy',
            )
        elif best_score >= 0.6:
            return BrandResolution(
                status='uncertain',
                brand_id=best_brand_id,
                canonical_name=best_canonical,
                wb_brand_id=best_wb_id,
                confidence=best_score,
                suggestions=suggestions[:8],
                source='alias_fuzzy',
            )
        elif suggestions:
            return BrandResolution(
                status='uncertain',
                confidence=best_score,
                suggestions=suggestions[:8],
                source='alias_fuzzy',
            )

        return None

    def _match_wb_api(self, raw_brand: str, wb_client) -> Optional[BrandResolution]:
        """Step 4: Поиск через WB API и сохранение результата."""
        try:
            api_result = wb_client.validate_brand(raw_brand)

            if api_result.get('valid') and api_result.get('exact_match'):
                match = api_result['exact_match']
                wb_name = match.get('name', '')
                wb_id = match.get('id')

                # Сохраняем найденный бренд в БД
                brand = self._save_brand_from_wb(wb_name, wb_id, raw_brand)
                if brand:
                    return BrandResolution(
                        status='exact',
                        brand_id=brand.id,
                        canonical_name=brand.name,
                        wb_brand_id=wb_id,
                        confidence=0.95,
                        source='wb_api',
                    )

            # Собираем suggestions из API
            suggestions = []
            for s in api_result.get('suggestions', [])[:8]:
                suggestions.append({
                    'name': s.get('name', ''),
                    'wb_brand_id': s.get('id'),
                    'score': 0.5,
                })

            if suggestions:
                return BrandResolution(
                    status='uncertain',
                    confidence=0.4,
                    suggestions=suggestions,
                    source='wb_api',
                )

        except Exception as e:
            logger.warning(f"WB API brand lookup failed for '{raw_brand}': {e}")

        return None

    def _save_brand_from_wb(self, wb_name: str, wb_id: int, raw_alias: str):
        """Сохранить бренд найденный через WB API в БД."""
        try:
            from models import db, Brand, BrandAlias

            name_norm = normalize_for_comparison(wb_name)
            brand = Brand.query.filter_by(name_normalized=name_norm).first()

            if not brand:
                brand = Brand(
                    name=wb_name,
                    name_normalized=name_norm,
                    wb_brand_id=wb_id,
                    status='verified',
                    verified_at=datetime.utcnow(),
                )
                db.session.add(brand)
                db.session.flush()

                # Добавляем каноническое имя как alias
                canon_alias = BrandAlias(
                    brand_id=brand.id,
                    alias=wb_name,
                    alias_normalized=name_norm,
                    source='wb_api',
                    confidence=1.0,
                )
                db.session.add(canon_alias)
            else:
                # Обновляем wb_brand_id если не было
                if not brand.wb_brand_id and wb_id:
                    brand.wb_brand_id = wb_id
                if brand.status == 'pending':
                    brand.status = 'verified'
                    brand.verified_at = datetime.utcnow()

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
            logger.warning(f"Failed to save brand from WB: {e}")
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

            # Проверяем что бренд с таким именем ещё не существует
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

    def _check_category(self, brand_id: int, subject_id: int) -> Optional[bool]:
        """Проверить допустимость бренда в категории WB."""
        if not brand_id or not subject_id:
            return None

        try:
            from models import BrandCategoryLink

            link = BrandCategoryLink.query.filter_by(
                brand_id=brand_id,
                wb_subject_id=subject_id,
            ).first()

            if link:
                return link.is_available
            return None  # Нет данных — не проверялось

        except Exception:
            return None

    # ------------------------------------------------------------------
    # Bulk resolve
    # ------------------------------------------------------------------

    def bulk_resolve(self, items: List[dict], brand_field: str = 'brand',
                     subject_field: str = 'wb_subject_id') -> List[BrandResolution]:
        """
        Пакетный резолв для импорта CSV.

        Args:
            items: Список словарей с данными товаров
            brand_field: Имя поля с брендом
            subject_field: Имя поля с subject_id

        Returns:
            Список BrandResolution, соответствующий items
        """
        self._ensure_cache()

        results = []
        for item in items:
            raw_brand = item.get(brand_field, '')
            subject_id = item.get(subject_field)
            result = self.resolve(raw_brand, subject_id=subject_id)
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # WB sync
    # ------------------------------------------------------------------

    def sync_wb_brands(self, wb_client) -> dict:
        """
        Синхронизация справочника WB в БД.

        Загружает бренды через API и обновляет/создаёт записи в Brand.
        """
        from models import db, Brand, BrandAlias

        logger.info("Starting WB brand sync...")
        stats = {'created': 0, 'updated': 0, 'errors': 0, 'total_fetched': 0}

        # Паттерны для поиска
        patterns = (
            list('ABCDEFGHIJKLMNOPQRSTUVWXYZ') +
            list('АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЭЮЯ') +
            list('0123456789')
        )

        all_brands = {}  # wb_id -> name

        for pattern in patterns:
            try:
                result = wb_client.search_brands(pattern, top=100)
                for brand_data in result.get('data', []):
                    wb_id = brand_data.get('id')
                    wb_name = brand_data.get('name', '')
                    if wb_id and wb_name:
                        all_brands[wb_id] = wb_name
                time.sleep(0.05)
            except Exception as e:
                logger.warning(f"Failed to fetch brands for pattern '{pattern}': {e}")
                stats['errors'] += 1
                continue

        stats['total_fetched'] = len(all_brands)
        logger.info(f"Fetched {len(all_brands)} brands from WB API")

        # Сохраняем в БД
        for wb_id, wb_name in all_brands.items():
            try:
                name_norm = normalize_for_comparison(wb_name)
                brand = Brand.query.filter_by(name_normalized=name_norm).first()

                if brand:
                    if not brand.wb_brand_id:
                        brand.wb_brand_id = wb_id
                    if brand.status == 'pending':
                        brand.status = 'verified'
                        brand.verified_at = datetime.utcnow()
                    brand.updated_at = datetime.utcnow()
                    stats['updated'] += 1
                else:
                    brand = Brand(
                        name=wb_name,
                        name_normalized=name_norm,
                        wb_brand_id=wb_id,
                        status='verified',
                        verified_at=datetime.utcnow(),
                    )
                    db.session.add(brand)
                    db.session.flush()

                    # Добавляем каноническое имя как alias
                    existing = BrandAlias.query.filter_by(alias_normalized=name_norm).first()
                    if not existing:
                        alias = BrandAlias(
                            brand_id=brand.id,
                            alias=wb_name,
                            alias_normalized=name_norm,
                            source='wb_sync',
                            confidence=1.0,
                        )
                        db.session.add(alias)

                    stats['created'] += 1

            except Exception as e:
                logger.warning(f"Failed to save brand '{wb_name}': {e}")
                stats['errors'] += 1

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to commit brand sync: {e}")
            raise

        self.invalidate_cache()
        logger.info(f"WB brand sync complete: {stats}")
        return stats

    # ------------------------------------------------------------------
    # Category validation
    # ------------------------------------------------------------------

    def validate_brand_for_category(self, brand_id: int, subject_id: int, wb_client=None) -> bool:
        """Проверить и сохранить допустимость бренда в категории WB."""
        from models import db, Brand, BrandCategoryLink

        brand = Brand.query.get(brand_id)
        if not brand:
            return False

        # Проверяем кэш
        link = BrandCategoryLink.query.filter_by(
            brand_id=brand_id,
            wb_subject_id=subject_id,
        ).first()

        # Если уже проверено менее 24 часов назад
        if link and link.verified_at:
            age = (datetime.utcnow() - link.verified_at).total_seconds()
            if age < 86400:
                return link.is_available

        # Если нет API клиента — возвращаем кэшированное или None
        if not wb_client:
            return link.is_available if link else True  # Optimistic default

        # Проверяем через API
        try:
            # Ищем бренд в списке брендов для этой категории
            result = wb_client.search_brands(brand.name, top=50)
            wb_brands = result.get('data', [])

            brand_alnum = normalize_alphanumeric(brand.name)
            is_available = False

            for wb_brand in wb_brands:
                wb_alnum = normalize_alphanumeric(wb_brand.get('name', ''))
                if wb_alnum == brand_alnum:
                    is_available = True
                    break

            if link:
                link.is_available = is_available
                link.verified_at = datetime.utcnow()
            else:
                link = BrandCategoryLink(
                    brand_id=brand_id,
                    wb_subject_id=subject_id,
                    is_available=is_available,
                    verified_at=datetime.utcnow(),
                )
                db.session.add(link)

            db.session.commit()
            return is_available

        except Exception as e:
            logger.warning(f"Category validation failed for brand {brand_id}, subject {subject_id}: {e}")
            return True  # Optimistic default on error

    # ------------------------------------------------------------------
    # Brand management
    # ------------------------------------------------------------------

    def merge_brands(self, source_brand_id: int, target_brand_id: int) -> dict:
        """
        Объединить source бренд в target.

        Переносит aliases, обновляет products, удаляет source.
        """
        from models import db, Brand, BrandAlias, BrandCategoryLink, ImportedProduct, SupplierProduct

        source = Brand.query.get(source_brand_id)
        target = Brand.query.get(target_brand_id)

        if not source or not target:
            raise ValueError("Brand not found")

        if source_brand_id == target_brand_id:
            raise ValueError("Cannot merge brand with itself")

        stats = {'aliases_moved': 0, 'imported_products_updated': 0,
                 'supplier_products_updated': 0, 'category_links_moved': 0}

        # Переносим aliases
        for alias in BrandAlias.query.filter_by(brand_id=source_brand_id).all():
            # Проверяем конфликт
            existing = BrandAlias.query.filter_by(
                alias_normalized=alias.alias_normalized,
                brand_id=target_brand_id,
            ).first()
            if existing:
                db.session.delete(alias)
            else:
                alias.brand_id = target_brand_id
                stats['aliases_moved'] += 1

        # Переносим category links
        for link in BrandCategoryLink.query.filter_by(brand_id=source_brand_id).all():
            existing = BrandCategoryLink.query.filter_by(
                brand_id=target_brand_id,
                wb_subject_id=link.wb_subject_id,
            ).first()
            if existing:
                db.session.delete(link)
            else:
                link.brand_id = target_brand_id
                stats['category_links_moved'] += 1

        # Обновляем products
        updated = ImportedProduct.query.filter_by(resolved_brand_id=source_brand_id).update(
            {'resolved_brand_id': target_brand_id}
        )
        stats['imported_products_updated'] = updated

        updated = SupplierProduct.query.filter_by(resolved_brand_id=source_brand_id).update(
            {'resolved_brand_id': target_brand_id}
        )
        stats['supplier_products_updated'] = updated

        # Удаляем source бренд
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

        # Проверяем уникальность
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

    def revalidate_existing(self, wb_client) -> dict:
        """Перепроверка verified брендов — всё ещё существуют в WB?"""
        from models import db, Brand

        stats = {'checked': 0, 'still_valid': 0, 'invalidated': 0, 'errors': 0}

        brands = Brand.query.filter_by(status='verified').all()

        for brand in brands:
            try:
                result = wb_client.validate_brand(brand.name)
                stats['checked'] += 1

                if result.get('valid'):
                    match = result.get('exact_match', {})
                    if match.get('id') and not brand.wb_brand_id:
                        brand.wb_brand_id = match['id']
                    brand.verified_at = datetime.utcnow()
                    stats['still_valid'] += 1
                else:
                    brand.status = 'needs_review'
                    stats['invalidated'] += 1

                time.sleep(0.2)  # Rate limiting

            except Exception as e:
                logger.warning(f"Revalidation failed for brand '{brand.name}': {e}")
                stats['errors'] += 1

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

        self.invalidate_cache()
        logger.info(f"Brand revalidation complete: {stats}")
        return stats

    def auto_resolve_pending(self, wb_client) -> dict:
        """Попытка авто-резолва pending брендов через fuzzy + WB API."""
        from models import db, Brand

        stats = {'checked': 0, 'resolved': 0, 'still_pending': 0, 'errors': 0}

        pending_brands = Brand.query.filter_by(status='pending').limit(50).all()

        for brand in pending_brands:
            try:
                stats['checked'] += 1
                result = wb_client.validate_brand(brand.name)

                if result.get('valid') and result.get('exact_match'):
                    match = result['exact_match']
                    wb_name = match.get('name', '')
                    wb_id = match.get('id')

                    # Обновляем бренд
                    if wb_name and normalize_alphanumeric(wb_name) == normalize_alphanumeric(brand.name):
                        brand.name = wb_name  # Используем каноническое имя из WB
                        brand.name_normalized = normalize_for_comparison(wb_name)
                    brand.wb_brand_id = wb_id
                    brand.status = 'verified'
                    brand.verified_at = datetime.utcnow()
                    stats['resolved'] += 1
                else:
                    stats['still_pending'] += 1

                time.sleep(0.3)  # Rate limiting

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
            from models import Brand, BrandAlias
            from sqlalchemy import func

            total = Brand.query.count()
            by_status = dict(
                Brand.query.with_entities(Brand.status, func.count(Brand.id))
                .group_by(Brand.status).all()
            )
            total_aliases = BrandAlias.query.filter_by(is_active=True).count()

            return {
                'total_brands': total,
                'verified': by_status.get('verified', 0),
                'pending': by_status.get('pending', 0),
                'needs_review': by_status.get('needs_review', 0),
                'rejected': by_status.get('rejected', 0),
                'total_aliases': total_aliases,
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
