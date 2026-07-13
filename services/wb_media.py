# -*- coding: utf-8 -*-
"""Единый резолвер корзин WB CDN + генерация URL фото карточек.

Таблица «vol → basket» у WB конечна и регулярно прирастает новыми корзинами,
а шаг диапазонов плавает — любая захардкоженная формула со временем врёт
(реальный пример: vol 9059 → basket 39, старая таблица давала 21, формула
с шагом 216 — 46). Поэтому:

- проверенные диапазоны — статическая таблица (быстрый путь);
- неизвестный vol — разовая проба CDN (спираль вокруг оценки от ближайшего
  известного vol) с кешированием НАВСЕГДА: маппинг vol→basket неизменяем,
  инвалидация не нужна;
- кеш процессный + персист в SystemSettings('wb_basket_vol_map') — общий
  для всех gunicorn-воркеров и переживает рестарты;
- неудачная проба (нет сети / у карточки нет фото) НЕ кешируется — возвращаем
  оценку и пробуем снова при следующем обращении.

Потокобезопасность: single-flight на vol (двойная конкурентная проба
безвредна — обе запишут одно и то же значение). Потеря записи при
конкурентном read-modify-write персиста между воркерами возможна и
безопасна: vol просто будет пропробован ещё раз.
"""
import json
import logging
import threading

import requests

logger = logging.getLogger(__name__)

# Проверенные диапазоны (подтверждены живыми пробами CDN)
_BASKET_RANGES = [
    (143, '01'), (287, '02'), (431, '03'), (719, '04'),
    (1007, '05'), (1061, '06'), (1115, '07'), (1169, '08'),
    (1313, '09'), (1601, '10'), (1655, '11'), (1919, '12'),
    (2045, '13'), (2189, '14'), (2405, '15'), (2621, '16'),
    (2837, '17'), (3053, '18'), (3269, '19'), (3485, '20'),
    (3701, '21'), (3917, '22'), (4133, '23'), (4349, '24'),
    (4565, '25'),
]
_LAST_STATIC_VOL = _BASKET_RANGES[-1][0]
_LAST_STATIC_BASKET = int(_BASKET_RANGES[-1][1])
# Средний шаг новых корзин (эмпирика 2026): используется только как оценка
_EXTRAPOLATION_STEP = 312

_PERSIST_KEY = 'wb_basket_vol_map'
_PROBE_TIMEOUT = 1.5
_PROBE_SPREAD = (0, 1, -1, 2, -2, 3, -3, 4, -4)

_vol_cache = {}          # vol(int) -> basket(str), только УСПЕШНЫЕ пробы
_persist_loaded = False
_cache_lock = threading.Lock()


def _reset_caches_for_tests():
    global _persist_loaded
    with _cache_lock:
        _vol_cache.clear()
        _persist_loaded = False


def _static_basket(vol: int):
    for max_vol, basket in _BASKET_RANGES:
        if vol <= max_vol:
            return basket
    return None


def _estimate_basket(vol: int) -> int:
    """Оценка корзины: от ближайшего известного vol, иначе — формульная."""
    with _cache_lock:
        known = dict(_vol_cache)
    if known:
        nearest_vol = min(known, key=lambda v: abs(v - vol))
        base = int(known[nearest_vol])
        return max(_LAST_STATIC_BASKET + 1,
                   base + (vol - nearest_vol) // _EXTRAPOLATION_STEP)
    return (_LAST_STATIC_BASKET + 1
            + (vol - _LAST_STATIC_VOL - 1) // _EXTRAPOLATION_STEP)


def _probe_vol(vol: int, nm_id: int):
    """Найти корзину пробой CDN (спираль вокруг оценки). None при неудаче."""
    estimate = _estimate_basket(vol)
    part = nm_id // 1000
    for delta in _PROBE_SPREAD:
        basket = estimate + delta
        if basket <= _LAST_STATIC_BASKET:
            continue
        url = (f"https://basket-{basket:02d}.wbbasket.ru/"
               f"vol{vol}/part{part}/{nm_id}/images/big/1.webp")
        try:
            resp = requests.head(url, timeout=_PROBE_TIMEOUT, allow_redirects=False)
            if resp.status_code == 200:
                logger.info(f"WB basket probe: vol {vol} -> basket {basket:02d}")
                return f"{basket:02d}"
        except requests.RequestException:
            continue
    logger.warning(f"WB basket probe failed for vol {vol} (nm {nm_id})")
    return None


def _load_persisted():
    """Разово подгрузить персистентную карту в процессный кеш."""
    global _persist_loaded
    if _persist_loaded:
        return
    try:
        from models import SystemSettings
        setting = SystemSettings.query.filter_by(key=_PERSIST_KEY).first()
        if setting and setting.value:
            data = json.loads(setting.value)
            with _cache_lock:
                for vol, basket in data.items():
                    _vol_cache.setdefault(int(vol), str(basket))
    except Exception as e:
        logger.debug(f"wb_basket_vol_map load skipped: {e}")
    _persist_loaded = True


def _persist(vol: int, basket: str):
    """Слить {vol: basket} в SystemSettings (merge с текущим содержимым)."""
    try:
        from models import db, SystemSettings
        setting = SystemSettings.query.filter_by(key=_PERSIST_KEY).first()
        current = {}
        if setting and setting.value:
            try:
                current = json.loads(setting.value)
            except (ValueError, TypeError):
                current = {}
        current[str(vol)] = basket
        if not setting:
            setting = SystemSettings(
                key=_PERSIST_KEY, value_type='json',
                description='Кеш маппинга vol->basket для WB CDN (самообучающийся)')
            db.session.add(setting)
        setting.value = json.dumps(current, ensure_ascii=False)
        db.session.commit()
    except Exception as e:
        logger.debug(f"wb_basket_vol_map persist skipped: {e}")


def resolve_basket(nm_id: int) -> str:
    """Номер корзины CDN для nm_id ('01'..'NN')."""
    vol = nm_id // 100000
    static = _static_basket(vol)
    if static is not None:
        return static

    _load_persisted()
    with _cache_lock:
        cached = _vol_cache.get(vol)
    if cached is not None:
        return cached

    probed = _probe_vol(vol, nm_id)
    if probed is not None:
        with _cache_lock:
            _vol_cache[vol] = probed
        _persist(vol, probed)
        return probed

    # Проба не удалась — оценка без кеширования (попробуем снова позже)
    return f"{_estimate_basket(vol):02d}"


def wb_photo_url(nm_id: int, photo_index: int = 1, size: str = 'big') -> str:
    """URL фото карточки WB: basket-NN.wbbasket.ru/volV/partP/nm/images/size/i.webp"""
    if not nm_id:
        return ''
    basket = resolve_basket(nm_id)
    vol = nm_id // 100000
    part = nm_id // 1000
    return (f"https://basket-{int(basket):02d}.wbbasket.ru/"
            f"vol{vol}/part{part}/{nm_id}/images/{size}/{photo_index}.webp")


def wb_basket_base_url(nm_id: int) -> str:
    """Базовый basket-URL карточки (для card.json и прочих ресурсов)."""
    basket = resolve_basket(nm_id)
    vol = nm_id // 100000
    part = nm_id // 1000
    return f"https://basket-{int(basket):02d}.wbbasket.ru/vol{vol}/part{part}/{nm_id}"


def normalize_photo_urls(nm_id, photos, size: str = 'big') -> list:
    """Список фото карточки → строковые URL.

    photos_json у WB-карточек хранит либо URL-строки, либо целочисленные
    индексы фото CDN; индексы разворачиваются через wb_photo_url. Строки
    передаются как есть (абсолютные и относительные URL), остальные типы
    отбрасываются — в WB media/save нельзя отправлять не-URL.
    """
    result = []
    for item in photos or []:
        if isinstance(item, str) and item.strip():
            result.append(item)
        elif isinstance(item, int) and not isinstance(item, bool) and nm_id:
            result.append(wb_photo_url(nm_id, item, size))
    return result
