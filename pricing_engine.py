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
) -> Optional[Dict[str, Any]]:
    """
    Рассчитать розничные цены из закупочной по формуле.

    Args:
        purchase_price: закупочная цена (P)
        settings: объект PricingSettings или dict с параметрами
        product_id: ID товара (для детерминированного random)

    Returns:
        dict с полями: final_price, discount_price, price_before_discount, и промежуточные
    """
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
    Q = P * D / 100
    if use_random and product_id:
        Q += _stable_random(product_id, random_min, random_max)
    if max_profit and Q > max_profit:
        Q = max_profit
    if Q < min_profit:
        Q = min_profit

    # Шаг 2: R — цена товара без доставки
    if wb_commission >= 100:
        wb_commission = 99.0
    R = (P + Q + logistics + storage + packaging) * 100 / (100 - wb_commission)

    # Шаг 3: S — стоимость доставки
    S = R / 100 * delivery_pct
    S = max(delivery_min, min(delivery_max, S))

    # Шаг 4: Z — итоговая цена на маркетплейсе
    Z = (P + Q + S + acquiring + extra) * 100 / (100 - wb_commission) * tax_rate

    # Шаг 5: T — скидка SPP в рублях
    T = Z * spp_pct / 100
    T = max(spp_min, min(spp_max, T))

    # Шаг 6: X — цена с учётом SPP (премиум-цена)
    X = Z - T - 1

    # Шаг 7: если скидка съедает больше половины прибыли — премиум-цена невыгодна
    if T > 0.5 * Q:
        X = 0

    # Шаг 8: Y — завышенная цена до скидки
    Y = Z * inflated_mult

    return {
        'purchase_price': round(P, 2),
        'profit_q': round(Q, 2),
        'profit_pct_d': D,
        'base_price_r': round(R, 2),
        'delivery_s': round(S, 2),
        'final_price': round(Z),
        'spp_discount_t': round(T, 2),
        'discount_price': round(X) if X > 0 else 0,
        'price_before_discount': round(Y),
        'wb_commission': wb_commission,
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
            # Пропускаем заголовок
            if not header_skipped:
                try:
                    int(raw_id)
                except ValueError:
                    header_skipped = True
                    continue
                header_skipped = True

            try:
                product_id = int(raw_id)
            except ValueError:
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
                prices[product_id] = {
                    'price': price,
                    'quantity': quantity,
                    'vendor_code': row[1].strip() if len(row) > 1 else '',
                }

        logger.info(f"Загружено {len(prices)} цен из CSV поставщика")
        return prices


def extract_supplier_product_id(external_id_or_vendor_code: str) -> Optional[int]:
    """Извлечь числовой ID поставщика из external_id/vendor_code вида 'id-1841-SELLER'."""
    if not external_id_or_vendor_code:
        return None
    m = re.search(r'id[_-](\d+)', external_id_or_vendor_code)
    if m:
        return int(m.group(1))
    # Попробуем просто число
    m = re.match(r'^(\d+)$', external_id_or_vendor_code.strip())
    if m:
        return int(m.group(1))
    return None
