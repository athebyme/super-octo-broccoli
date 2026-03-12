# -*- coding: utf-8 -*-
"""
Сервис проверки запрещённых брендов по маркетплейсам.

Кэширует список запрещённых брендов в памяти и предоставляет быструю проверку.
"""
import logging
import re
from typing import Dict, List, Optional, Set, Tuple

from models import db, ProhibitedBrand

logger = logging.getLogger(__name__)

# Кэш: marketplace -> set(normalized_brand_names)
_cache: Dict[str, Set[str]] = {}
_cache_loaded = False


def _normalize(name: str) -> str:
    """Нормализует имя бренда для сравнения."""
    if not name:
        return ''
    n = name.strip().lower()
    n = re.sub(r'[^\w\s]', '', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def invalidate_cache():
    """Сбросить кэш запрещённых брендов."""
    global _cache, _cache_loaded
    _cache.clear()
    _cache_loaded = False


def _load_cache():
    """Загрузить запрещённые бренды из БД в кэш."""
    global _cache, _cache_loaded
    _cache.clear()
    try:
        brands = ProhibitedBrand.query.filter_by(is_active=True).all()
        for b in brands:
            mp = b.marketplace.lower()
            if mp not in _cache:
                _cache[mp] = set()
            _cache[mp].add(b.brand_name_normalized)
        _cache_loaded = True
    except Exception as e:
        logger.error(f"Ошибка загрузки запрещённых брендов: {e}")
        _cache_loaded = False


def _ensure_cache():
    """Загрузить кэш, если ещё не загружен."""
    if not _cache_loaded:
        _load_cache()


def is_brand_prohibited(brand_name: str, marketplace: str = None) -> bool:
    """
    Проверяет, запрещён ли бренд.

    Args:
        brand_name: Название бренда
        marketplace: Код маркетплейса ('wb', 'ozon', 'sber').
                     Если None, проверяет по всем маркетплейсам.

    Returns:
        True если бренд запрещён
    """
    if not brand_name:
        return False

    _ensure_cache()
    normalized = _normalize(brand_name)
    if not normalized:
        return False

    if marketplace:
        mp = marketplace.lower()
        # Проверяем и конкретный маркетплейс, и 'all'
        if normalized in _cache.get(mp, set()):
            return True
        if normalized in _cache.get('all', set()):
            return True
        return False
    else:
        # Проверяем по всем маркетплейсам
        for mp_brands in _cache.values():
            if normalized in mp_brands:
                return True
        return False


def get_prohibited_marketplaces(brand_name: str) -> List[str]:
    """
    Возвращает список маркетплейсов, на которых бренд запрещён.

    Args:
        brand_name: Название бренда

    Returns:
        Список кодов маркетплейсов, например ['wb', 'ozon']
    """
    if not brand_name:
        return []

    _ensure_cache()
    normalized = _normalize(brand_name)
    if not normalized:
        return []

    result = []
    for mp, brands in _cache.items():
        if normalized in brands:
            result.append(mp)
    return sorted(result)


def check_brand_for_import(brand_name: str, marketplace: str) -> Tuple[bool, Optional[str]]:
    """
    Проверяет бренд перед импортом.

    Returns:
        (can_import, reason) - True если можно импортировать, иначе причина отказа
    """
    if not brand_name:
        return True, None

    if is_brand_prohibited(brand_name, marketplace):
        mp_names = {
            'wb': 'Wildberries',
            'ozon': 'Ozon',
            'sber': 'СберМегаМаркет',
            'all': 'всех маркетплейсах',
        }
        mp_display = mp_names.get(marketplace, marketplace)
        return False, f'Бренд "{brand_name}" запрещён на {mp_display}'

    return True, None


# ============================================================================
# CRUD
# ============================================================================

def add_prohibited_brand(brand_name: str, marketplace: str, reason: str = None) -> Tuple[bool, str]:
    """Добавить запрещённый бренд."""
    normalized = _normalize(brand_name)
    if not normalized:
        return False, 'Пустое имя бренда'

    existing = ProhibitedBrand.query.filter_by(
        brand_name_normalized=normalized,
        marketplace=marketplace.lower()
    ).first()

    if existing:
        if not existing.is_active:
            existing.is_active = True
            existing.reason = reason
            db.session.commit()
            invalidate_cache()
            return True, f'Бренд "{brand_name}" повторно активирован для {marketplace}'
        return False, f'Бренд "{brand_name}" уже запрещён для {marketplace}'

    pb = ProhibitedBrand(
        brand_name=brand_name.strip(),
        brand_name_normalized=normalized,
        marketplace=marketplace.lower(),
        reason=reason,
        is_active=True,
    )
    db.session.add(pb)
    db.session.commit()
    invalidate_cache()
    return True, f'Бренд "{brand_name}" добавлен в запрещённые для {marketplace}'


def remove_prohibited_brand(brand_id: int) -> Tuple[bool, str]:
    """Удалить запрещённый бренд."""
    pb = ProhibitedBrand.query.get(brand_id)
    if not pb:
        return False, 'Бренд не найден'

    name = pb.brand_name
    db.session.delete(pb)
    db.session.commit()
    invalidate_cache()
    return True, f'Бренд "{name}" удалён из запрещённых'


def bulk_add_brands(brands_text: str, marketplace: str, reason: str = None) -> Tuple[int, int]:
    """
    Массовое добавление брендов (один бренд на строку).

    Returns:
        (added_count, skipped_count)
    """
    added = 0
    skipped = 0

    for line in brands_text.strip().split('\n'):
        brand_name = line.strip().strip('*').strip()
        if not brand_name:
            continue

        normalized = _normalize(brand_name)
        if not normalized:
            skipped += 1
            continue

        existing = ProhibitedBrand.query.filter_by(
            brand_name_normalized=normalized,
            marketplace=marketplace.lower()
        ).first()

        if existing:
            skipped += 1
            continue

        pb = ProhibitedBrand(
            brand_name=brand_name,
            brand_name_normalized=normalized,
            marketplace=marketplace.lower(),
            reason=reason,
            is_active=True,
        )
        db.session.add(pb)
        added += 1

    if added > 0:
        db.session.commit()
        invalidate_cache()

    return added, skipped


def seed_default_brands():
    """Заполнить запрещённые бренды начальными данными (идемпотентно)."""
    # Проверяем, есть ли уже данные
    if ProhibitedBrand.query.first():
        return 0

    OZON_BRANDS = [
        "ART STYLE", "B-VIBE", "BLACKRED", "BRAZZERS", "CANDY BOY", "CANDY GIRL",
        "EGZO", "EROLANTA", "EROMANTICA", "EROTIST", "ESKA", "FLESHNASH", "FLOVETTA",
        "FORTE LOVE POWER", "GANZO", "GLOSSY", "GVIBE", "HOT", "HOT PRODUCTION",
        "INDEEP", "JOS", "JUJU", "JULEJU", "L'EROINA", "LE FRIVOLE", "LE WAND",
        "LELO", "LOVENSE", "MAXUS", "MEGA GLIDE", "MIA-MIA", "MIOOCCHI", "MOJO",
        "MOY TOY", "MY.SIZE", "MiNiMi", "Natural Instinct", "ON", "ORION",
        "PORN HUB TOY", "PRE PARFUMER", "PRIVATE", "QUEEN FAIR", "Qvibry",
        "REBELTS", "ROMP by WOW Tech", "RUF", "SEXUS", "SEXY LIFE", "SHIATSU",
        "SPRING", "STIMUL 8", "SVAKOM", "SVAKOM DESIGN USA LIMITED", "SWISS NAVY",
        "TIME HEAT", "TOM OF FINLAND", "TOREX", "TOYFA", "VIAMAX", "VITALIS",
        "WANAME", "WE-VIBE", "WINYI", "WOMANIZER", "YOU2TOYS", "YOVEE",
        "ЁSKA", "ЛАС ИГРАС", "Молот Тора", "ПИКАНТНЫЕ ШТУЧКИ", "РИА ПАНДА",
        "Товары без упаковки", "ФЛЕШНАШ", "Штучки-дрючки", "ЭЛИВЕРТОРГ",
    ]

    WB_BRANDS = [
        "GVIBE", "HOT", "HOT PRODUCTION", "INDEEP", "JUJU", "JULEJU",
        "LELO", "LOVENSE", "MY.SIZE", "ON", "Prime Products", "RUF",
        "SEXY LIFE", "SHIATSU", "VIAMAX INTERNATIONAL", "VITALIS", "WE-VIBE",
        "Молот Тора", "Товары без упаковки", "ФЛЕШНАШ",
    ]

    SBER_BRANDS = [
        "GVIBE", "HOT", "HOT PRODUCTION", "INDEEP", "JUJU", "JULEJU",
        "LELO", "LOLA GAMES", "LOLA TOYS", "LOVENSE", "MY.SIZE", "ON",
        "PLEASURE LAB", "RUF", "SEXY LIFE", "SHIATSU", "VIAMAX INTERNATIONAL",
        "VITALIS", "WE-VIBE", "Молот Тора", "Товары без упаковки", "ФЛЕШНАШ",
    ]

    count = 0
    for brand_name in OZON_BRANDS:
        pb = ProhibitedBrand(
            brand_name=brand_name,
            brand_name_normalized=_normalize(brand_name),
            marketplace='ozon',
            is_active=True,
        )
        db.session.add(pb)
        count += 1

    for brand_name in WB_BRANDS:
        pb = ProhibitedBrand(
            brand_name=brand_name,
            brand_name_normalized=_normalize(brand_name),
            marketplace='wb',
            is_active=True,
        )
        db.session.add(pb)
        count += 1

    for brand_name in SBER_BRANDS:
        pb = ProhibitedBrand(
            brand_name=brand_name,
            brand_name_normalized=_normalize(brand_name),
            marketplace='sber',
            is_active=True,
        )
        db.session.add(pb)
        count += 1

    db.session.commit()
    invalidate_cache()
    logger.info(f"Заполнены запрещённые бренды: {count} записей")
    return count
