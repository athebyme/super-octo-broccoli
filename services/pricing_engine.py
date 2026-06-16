# -*- coding: utf-8 -*-
"""
Модуль расчёта розничных цен из закупочных.

Поддерживает формулу ценообразования с конфигурируемыми константами
и таблицей наценок по диапазонам закупочной цены.
"""
import csv
import hashlib
import io
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger('pricing_engine')

# Таблица наценок по умолчанию (из скриншотов)
DEFAULT_PRICE_RANGES = [
    {"from": 1, "to": 100, "a": 35, "b": 0, "c": 0, "d": 38},
    {"from": 101, "to": 200, "a": 34, "b": 0, "c": 0, "d": 37},
    {"from": 201, "to": 400, "a": 33, "b": 0, "c": 0, "d": 34},
    {"from": 401, "to": 700, "a": 32, "b": 0, "c": 0, "d": 29},
    {"from": 701, "to": 1000, "a": 31, "b": 0, "c": 0, "d": 26},
    {"from": 1001, "to": 1300, "a": 30, "b": 0, "c": 0, "d": 25},
    {"from": 1301, "to": 1600, "a": 29, "b": 0, "c": 0, "d": 25},
    {"from": 1601, "to": 1900, "a": 28, "b": 0, "c": 0, "d": 25},
    {"from": 1901, "to": 2300, "a": 27, "b": 0, "c": 0, "d": 24},
    {"from": 2301, "to": 2700, "a": 26, "b": 0, "c": 0, "d": 22},
    {"from": 2701, "to": 3000, "a": 25, "b": 0, "c": 0, "d": 22},
    {"from": 3001, "to": 4000, "a": 24, "b": 0, "c": 0, "d": 21},
    {"from": 4001, "to": 5000, "a": 23, "b": 0, "c": 0, "d": 21},
    {"from": 5001, "to": 6000, "a": 22, "b": 0, "c": 0, "d": 21},
    {"from": 6001, "to": 7000, "a": 21, "b": 0, "c": 0, "d": 21},
    {"from": 7001, "to": 8000, "a": 20, "b": 0, "c": 0, "d": 20},
    {"from": 8001, "to": 9000, "a": 19, "b": 0, "c": 0, "d": 20},
    {"from": 9001, "to": 10000, "a": 18, "b": 0, "c": 0, "d": 19},
    {"from": 10001, "to": 12000, "a": 17, "b": 0, "c": 0, "d": 18},
    {"from": 12001, "to": 14000, "a": 16, "b": 0, "c": 0, "d": 17},
    {"from": 14001, "to": 16000, "a": 15, "b": 0, "c": 0, "d": 16},
    {"from": 16001, "to": 18000, "a": 14, "b": 0, "c": 0, "d": 15},
    {"from": 18001, "to": 20000, "a": 13, "b": 0, "c": 0, "d": 14},
    {"from": 20001, "to": 99999, "a": 12, "b": 0, "c": 0, "d": 13},
]


def _stable_random(product_id: int, min_val: int, max_val: int) -> int:
    """Детерминированный 'случайный' разброс на основе ID товара."""
    h = int(hashlib.md5(str(product_id).encode()).hexdigest(), 16)
    return min_val + (h % (max_val - min_val + 1))


def get_range_values(purchase_price: float, ranges: List[Dict]) -> Optional[Dict]:
    """Найти строку таблицы наценок по закупочной цене."""
    for r in ranges:
        if r['from'] <= purchase_price <= r['to']:
            return r
    if ranges:
        return ranges[-1]
    return None


def calculate_price(
    purchase_price: float,
    settings,
    product_id: int = 0,
    force_random: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Рассчитать розничные цены из закупочной по формуле.

    Args:
        purchase_price: закупочная цена (P)
        settings: объект PricingSettings или dict с параметрами
        product_id: ID товара (для детерминированного random)
        force_random: True → использовать настоящий random (для калькулятора)

    Returns:
        dict с полями: final_price, discount_price, price_before_discount,
        и все промежуточные шаги для прозрачности расчёта
    """
    import random as _random

    if purchase_price <= 0:
        return None

    # Получаем параметры из settings (поддерживаем и ORM-объект, и dict)
    def _get(attr, default=0):
        if isinstance(settings, dict):
            return settings.get(attr, default)
        return getattr(settings, attr, default)

    ranges_raw = _get('price_ranges', None)
    if isinstance(ranges_raw, str):
        try:
            ranges = json.loads(ranges_raw)
        except (json.JSONDecodeError, TypeError):
            ranges = DEFAULT_PRICE_RANGES
    elif isinstance(ranges_raw, list):
        ranges = ranges_raw
    else:
        ranges = DEFAULT_PRICE_RANGES

    range_row = get_range_values(purchase_price, ranges)
    if not range_row:
        return None

    profit_col = _get('profit_column', 'd').lower()
    D = range_row.get(profit_col, range_row.get('d', 20))

    P = purchase_price
    wb_commission = _get('wb_commission_pct', 40.0)
    tax_rate = _get('tax_rate', 1.13)
    logistics = _get('logistics_cost', 55.0)
    storage = _get('storage_cost', 0.0)
    packaging = _get('packaging_cost', 20.0)
    acquiring = _get('acquiring_cost', 25.0)
    extra = _get('extra_cost', 20.0)
    delivery_pct = _get('delivery_pct', 5.0)
    delivery_min = _get('delivery_min', 55.0)
    delivery_max = _get('delivery_max', 205.0)
    min_profit = _get('min_profit', 30.0)
    max_profit = _get('max_profit', None)
    use_random = _get('use_random', False)
    random_min = _get('random_min', 1)
    random_max = _get('random_max', 10)
    spp_pct = _get('spp_pct', 5.0)
    spp_min = _get('spp_min', 20.0)
    spp_max = _get('spp_max', 500.0)
    inflated_mult = _get('inflated_multiplier', 1.55)

    # Шаг 1: Q — желаемая чистая прибыль (руб.)
    Q_base = P * D / 100
    random_addon = 0
    if use_random:
        if force_random:
            random_addon = _random.randint(random_min, random_max)
        elif product_id:
            random_addon = _stable_random(product_id, random_min, random_max)
    Q = Q_base + random_addon
    Q_before_clamp = Q
    if max_profit and Q > max_profit:
        Q = max_profit
    if Q < min_profit:
        Q = min_profit

    # Шаг 2: R — цена товара без доставки
    if wb_commission >= 100:
        wb_commission = 99.0
    R_numerator = P + Q + logistics + storage + packaging
    R = R_numerator * 100 / (100 - wb_commission)

    # Шаг 3: S — стоимость доставки
    S_raw = R / 100 * delivery_pct
    S = max(delivery_min, min(delivery_max, S_raw))

    # Шаг 4: Z — итоговая цена на маркетплейсе
    Z_numerator = P + Q + S + acquiring + extra
    Z_before_tax = Z_numerator * 100 / (100 - wb_commission)
    Z = Z_before_tax * tax_rate

    # Шаг 5: T — скидка SPP в рублях
    T_raw = Z * spp_pct / 100
    T = max(spp_min, min(spp_max, T_raw))

    # Шаг 6: X — цена с учётом SPP (премиум-цена)
    X = Z - T - 1
    spp_unprofitable = T > 0.5 * Q

    # Шаг 7: если скидка съедает больше половины прибыли — премиум-цена невыгодна
    if spp_unprofitable:
        X = 0

    # Шаг 8: Y — завышенная цена до скидки
    Y = Z * inflated_mult

    return {
        # Итоговые цены
        'purchase_price': round(P, 2),
        'final_price': round(Z),
        'discount_price': round(X) if X > 0 else 0,
        'price_before_discount': round(Y),
        # Промежуточные значения (для прозрачности)
        'profit_pct_d': D,
        'profit_q_base': round(Q_base, 2),
        'random_addon': random_addon,
        'profit_q_before_clamp': round(Q_before_clamp, 2),
        'profit_q': round(Q, 2),
        'profit_clamped': Q != Q_before_clamp,
        'base_price_r': round(R, 2),
        'r_components': f'{round(P,2)} + {round(Q,2)} + {round(logistics,2)} + {round(storage,2)} + {round(packaging,2)} = {round(R_numerator,2)}',
        'delivery_s_raw': round(S_raw, 2),
        'delivery_s': round(S, 2),
        'delivery_clamped': S != S_raw,
        'z_components': f'{round(P,2)} + {round(Q,2)} + {round(S,2)} + {round(acquiring,2)} + {round(extra,2)} = {round(Z_numerator,2)}',
        'z_before_tax': round(Z_before_tax, 2),
        'z_final': round(Z, 2),
        'spp_t_raw': round(T_raw, 2),
        'spp_discount_t': round(T, 2),
        'spp_clamped': T != T_raw,
        'spp_unprofitable': spp_unprofitable,
        'inflated_multiplier': inflated_mult,
        # Коэффициенты (для отображения)
        'wb_commission': wb_commission,
        'tax_rate': tax_rate,
        'logistics_cost': logistics,
        'storage_cost': storage,
        'packaging_cost': packaging,
        'acquiring_cost': acquiring,
        'extra_cost': extra,
        'delivery_pct': delivery_pct,
        'delivery_min': delivery_min,
        'delivery_max': delivery_max,
        'min_profit': min_profit,
        'max_profit': max_profit,
        'use_random': use_random,
        'random_min': random_min,
        'random_max': random_max,
        'spp_pct': spp_pct,
        'range_from': range_row.get('from'),
        'range_to': range_row.get('to'),
    }


# ======================= Загрузчик цен поставщика =======================

class SupplierPriceLoader:
    """Загрузка и парсинг CSV с ценами поставщика (sexoptovik)."""

    def __init__(self, price_url: str, inf_url: str = None, timeout: int = 60):
        self.price_url = price_url
        self.inf_url = inf_url
        self.timeout = timeout

    def check_update(self, last_hash: str = None) -> Tuple[bool, str]:
        """
        Проверить, обновился ли файл цен.
        Возвращает (has_update, new_hash).
        """
        if self.inf_url:
            try:
                resp = requests.get(self.inf_url, timeout=30)
                resp.raise_for_status()
                content = resp.content
                new_hash = hashlib.md5(content).hexdigest()
                return (new_hash != last_hash, new_hash)
            except Exception as e:
                logger.warning(f"Не удалось проверить INF файл: {e}")
        return (True, last_hash or '')

    def load_prices(self) -> Dict[int, Dict[str, Any]]:
        """
        Загрузить CSV с ценами и вернуть словарь {product_id: {price, quantity}}.
        """
        try:
            resp = requests.get(self.price_url, timeout=self.timeout)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Ошибка загрузки CSV цен: {e}")
            raise

        # Пробуем cp1251 (sexoptovik), потом utf-8
        for encoding in ('cp1251', 'utf-8', 'latin-1'):
            try:
                text = resp.content.decode(encoding, errors='replace')
                break
            except UnicodeDecodeError:
                continue
        else:
            text = resp.content.decode('latin-1', errors='replace')

        prices = {}
        reader = csv.reader(io.StringIO(text), delimiter=';')
        header_skipped = False

        for row in reader:
            if len(row) < 4:
                continue
            raw_id = row[0].strip()
            # Пропускаем заголовок — если не содержит числового ID
            if not header_skipped:
                if extract_supplier_product_id(raw_id) is None:
                    header_skipped = True
                    continue
                header_skipped = True

            if not raw_id:
                continue

            try:
                price = float(row[2].strip()) if row[2].strip() else 0
            except ValueError:
                price = 0

            try:
                quantity = int(row[3].strip()) if row[3].strip() else 0
            except ValueError:
                quantity = 0

            if price > 0:
                # Ключ — сырой ID из CSV (строка), совпадает с external_id товара
                prices[raw_id] = {
                    'price': price,
                    'quantity': quantity,
                    'vendor_code': row[1].strip() if len(row) > 1 else '',
                }

        logger.info(f"Загружено {len(prices)} цен из CSV поставщика")
        return prices


def extract_supplier_product_id(external_id_or_vendor_code: str) -> Optional[int]:
    """Извлечь числовой ID поставщика из external_id/vendor_code.

    Поддерживаемые форматы:
        'id-1841-SELLER' → 1841
        '0T-00000877'    → 877 (sex-opt формат)
        '12345'          → 12345
    """
    if not external_id_or_vendor_code:
        return None
    # Формат: id-12345 или id_12345
    m = re.search(r'id[_-](\d+)', external_id_or_vendor_code)
    if m:
        return int(m.group(1))
    # Формат sex-opt: 0T-00000877 (буквы-цифры)
    m = re.search(r'[A-Za-z]+-(\d+)', external_id_or_vendor_code)
    if m:
        return int(m.group(1))
    # Просто число
    m = re.match(r'^(\d+)$', external_id_or_vendor_code.strip())
    if m:
        return int(m.group(1))
    return None


def extract_product_id_for_vendor_code(external_id: str, supplier=None) -> str:
    """Извлечь product_id из external_id для подстановки в шаблон артикула.

    Использует supplier.external_id_pattern (regex с группой 'product_id')
    если задан, иначе — стандартную цепочку регексов.

    Возвращает строку (не int), чтобы сохранять ведущие нули.
    """
    if not external_id:
        return ''

    # Если у поставщика задан кастомный regex — используем его
    if supplier and getattr(supplier, 'external_id_pattern', None):
        try:
            m = re.search(supplier.external_id_pattern, external_id)
            if m:
                # Пробуем именованную группу product_id, иначе первую группу
                try:
                    return m.group('product_id')
                except IndexError:
                    return m.group(1)
        except re.error:
            pass  # Невалидный regex — fallback на стандартную логику

    # Стандартная логика: id-12345-...
    m = re.search(r'id-(\d+)', external_id)
    if m:
        return m.group(1)

    # Формат с буквенным префиксом: "0T-00003031" → "00003031"
    m = re.search(r'[A-Za-z]+-(\d+)', external_id)
    if m:
        return m.group(1)

    # Просто число
    m = re.search(r'(\d+)', external_id)
    if m:
        return m.group(1)

    return external_id


# ---------------------------------------------------------------------------
# Централизованная генерация артикула (vendor code)
# ---------------------------------------------------------------------------

_DEFAULT_VENDOR_CODE_PATTERN = 'id-{product_id}-{supplier_code}'


def resolve_vendor_code_settings(seller_id: int, supplier_id: int = None):
    """Определить шаблон артикула и supplier_code с правильным приоритетом.

    Приоритет: SellerSupplier → AutoImportSettings → default.
    Возвращает (pattern, supplier_code, supplier_obj).
    """
    from models import SellerSupplier, Supplier, Seller

    pattern = None
    sup_code = None
    supplier_obj = None

    if supplier_id:
        ss = SellerSupplier.query.filter_by(
            seller_id=seller_id,
            supplier_id=supplier_id,
            is_active=True,
        ).first()
        if ss:
            pattern = ss.vendor_code_pattern
            sup_code = ss.supplier_code
        supplier_obj = Supplier.query.get(supplier_id)

    if not pattern:
        seller = Seller.query.get(seller_id)
        if seller:
            settings = seller.auto_import_settings
            if settings and settings.vendor_code_pattern:
                pattern = settings.vendor_code_pattern
                if not sup_code:
                    sup_code = settings.supplier_code

    return pattern, sup_code, supplier_obj


def generate_vendor_code(
    pattern: str = None,
    supplier_code: str = None,
    external_id: str = None,
    external_vendor_code: str = None,
    supplier=None,
    fallback_id=None,
    fallback_seller_id=None,
) -> str:
    """Сгенерировать артикул (vendorCode) по шаблону.

    Переменные шаблона:
      {product_id}          — извлечённый из external_id по regex поставщика
      {supplier_code}       — код продавца (SellerSupplier.supplier_code)
      {external_vendor_code} — артикул из CSV поставщика
      {external_id}         — полный external_id товара

    Если pattern не задан:
      - и есть fallback_id/fallback_seller_id → 'id-<fallback_id>-<fallback_seller_id>'
      - иначе → 'id-{product_id}-{supplier_code}' (default шаблон)
    """
    ext_id = str(external_id or '')

    if not pattern:
        if fallback_id is not None and fallback_seller_id is not None:
            return f"id-{fallback_id}-{fallback_seller_id}"
        pattern = _DEFAULT_VENDOR_CODE_PATTERN

    product_id_val = extract_product_id_for_vendor_code(ext_id, supplier)
    ext_vc_val = str(external_vendor_code or '')

    # Поставщик 'andrey' (sex-opt.ru): подставляемые из CSV значения переводим
    # в латинские строчные буквы. Кириллица из external_id ("УТ-00008335")
    # визуально транслитерируется в латиницу ("yt-00008335"), литералы
    # паттерна и {supplier_code} не трогаем.
    if supplier is not None and getattr(supplier, 'code', None) == 'andrey':
        product_id_val = _andrey_normalize(product_id_val)
        ext_vc_val = _andrey_normalize(ext_vc_val)
        ext_id = _andrey_normalize(ext_id)

    result = pattern.replace('{product_id}', product_id_val)
    result = result.replace('{supplier_code}', supplier_code or '')
    result = result.replace('{external_vendor_code}', ext_vc_val)
    result = result.replace('{external_id}', ext_id)

    return result


# Визуальная Cyrillic → Latin транслитерация для артикулов поставщика 'andrey'.
# Только похожие по начертанию буквы — артикулы в CSV у этого поставщика
# содержат именно такие префиксы ('УТ', '0T' и т.п.).
_ANDREY_CYR_TO_LAT = {
    'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H',
    'О': 'O', 'Р': 'P', 'С': 'C', 'Т': 'T', 'Х': 'X', 'У': 'Y',
    'а': 'a', 'в': 'b', 'е': 'e', 'к': 'k', 'м': 'm', 'н': 'h',
    'о': 'o', 'р': 'p', 'с': 'c', 'т': 't', 'х': 'x', 'у': 'y',
}


def _andrey_normalize(value: str) -> str:
    """Транслитерация Cyrillic→Latin (визуальная) + lowercase."""
    if not value:
        return value
    return ''.join(_ANDREY_CYR_TO_LAT.get(ch, ch) for ch in value).lower()
