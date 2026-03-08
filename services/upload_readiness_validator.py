# -*- coding: utf-8 -*-
"""
Сервис валидации готовности товара к загрузке на WB.

Интегрируется в UI "Мои товары" для показа проблем перед загрузкой.
Проверяет: цены, фото, характеристики, баркоды, бренд, категорию.
"""
import json
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


class UploadIssue:
    """Проблема, найденная при валидации."""
    ERROR = 'error'
    WARNING = 'warning'
    INFO = 'info'

    def __init__(self, level: str, field: str, message: str, fix_hint: str = ''):
        self.level = level
        self.field = field
        self.message = message
        self.fix_hint = fix_hint

    def to_dict(self):
        d = {'level': self.level, 'field': self.field, 'message': self.message}
        if self.fix_hint:
            d['fix_hint'] = self.fix_hint
        return d


def validate_product_upload_readiness(imported_product, seller=None) -> Dict[str, Any]:
    """
    Полная проверка готовности ImportedProduct к загрузке на WB.

    Возвращает:
        {
            'is_ready': bool,       # True если нет ОШИБОК (error)
            'can_push': bool,       # True если готов к загрузке на WB
            'issues': [...],        # Список проблем
            'errors_count': int,
            'warnings_count': int,
            'checks': {             # Детали по каждой проверке
                'price': {'status': 'ok'|'error'|'warning', 'details': '...'},
                'photos': {...},
                'characteristics': {...},
                'barcodes': {...},
                'brand': {...},
                'title': {...},
                'description': {...},
                'category': {...},
            }
        }
    """
    issues: List[UploadIssue] = []
    checks = {}

    # === ЦЕНА ===
    checks['price'] = _check_price(imported_product, issues)

    # === ФОТО ===
    checks['photos'] = _check_photos(imported_product, issues)

    # === ХАРАКТЕРИСТИКИ ===
    checks['characteristics'] = _check_characteristics(imported_product, issues)

    # === БАРКОДЫ ===
    checks['barcodes'] = _check_barcodes(imported_product, issues)

    # === БРЕНД ===
    checks['brand'] = _check_brand(imported_product, issues)

    # === НАЗВАНИЕ ===
    checks['title'] = _check_title(imported_product, issues)

    # === ОПИСАНИЕ ===
    checks['description'] = _check_description(imported_product, issues)

    # === КАТЕГОРИЯ ===
    checks['category'] = _check_category(imported_product, issues)

    errors = [i for i in issues if i.level == UploadIssue.ERROR]
    warnings = [i for i in issues if i.level == UploadIssue.WARNING]

    has_api_key = False
    if seller:
        has_api_key = seller.has_valid_api_key() if hasattr(seller, 'has_valid_api_key') else bool(seller.wb_api_key)

    return {
        'is_ready': len(errors) == 0,
        'can_push': len(errors) == 0 and has_api_key,
        'issues': [i.to_dict() for i in issues],
        'errors_count': len(errors),
        'warnings_count': len(warnings),
        'checks': checks,
    }


def _check_price(product, issues: List[UploadIssue]) -> Dict:
    """Проверка готовности цены."""
    from models import PricingSettings

    if not product.supplier_price or product.supplier_price <= 0:
        issues.append(UploadIssue(
            UploadIssue.ERROR, 'price',
            f'Нет закупочной цены (supplier_price = {product.supplier_price})',
            'Убедитесь что CSV-файл цен от поставщика загружен и привязан к товару'
        ))
        return {'status': 'error', 'supplier_price': product.supplier_price,
                'calculated_price': None, 'details': 'supplier_price отсутствует'}

    pricing = PricingSettings.query.filter_by(seller_id=product.seller_id).first()

    if not pricing or not pricing.is_enabled:
        if product.calculated_price and product.calculated_price > 0:
            return {
                'status': 'ok',
                'supplier_price': product.supplier_price,
                'calculated_price': product.calculated_price,
                'price_before_discount': product.calculated_price_before_discount,
                'details': 'Используется предрассчитанная цена (формула отключена)'
            }
        else:
            issues.append(UploadIssue(
                UploadIssue.ERROR, 'price',
                'Формула ценообразования не настроена и нет предрассчитанной цены',
                'Настройте PricingSettings или рассчитайте цены заранее'
            ))
            return {'status': 'error', 'supplier_price': product.supplier_price,
                    'calculated_price': None, 'details': 'Нет формулы и нет calculated_price'}

    # Пробуем рассчитать
    try:
        from services.pricing_engine import calculate_price
        result = calculate_price(
            purchase_price=product.supplier_price,
            settings=pricing,
            product_id=product.id
        )
        if result and result.get('final_price', 0) > 0:
            return {
                'status': 'ok',
                'supplier_price': product.supplier_price,
                'final_price': result['final_price'],
                'price_before_discount': result['price_before_discount'],
                'discount_pct': max(0, min(99, int((1 - result['final_price'] / result['price_before_discount']) * 100))) if result['price_before_discount'] > 0 else 0,
                'details': 'Цена рассчитана по формуле'
            }
        else:
            issues.append(UploadIssue(
                UploadIssue.ERROR, 'price',
                f'Формула вернула нулевую цену для supplier_price={product.supplier_price}',
                'Проверьте диапазоны цен в PricingSettings'
            ))
            return {'status': 'error', 'supplier_price': product.supplier_price,
                    'details': 'Формула вернула None или 0'}
    except Exception as e:
        issues.append(UploadIssue(
            UploadIssue.ERROR, 'price',
            f'Ошибка расчёта цены: {e}',
        ))
        return {'status': 'error', 'supplier_price': product.supplier_price,
                'details': f'Ошибка: {e}'}


def _check_photos(product, issues: List[UploadIssue]) -> Dict:
    """Проверка фотографий."""
    photo_urls = []
    if product.photo_urls:
        try:
            photo_urls = json.loads(product.photo_urls)
        except (json.JSONDecodeError, TypeError):
            issues.append(UploadIssue(
                UploadIssue.ERROR, 'photos',
                'photo_urls содержит невалидный JSON'
            ))
            return {'status': 'error', 'count': 0, 'details': 'Невалидный JSON'}

    if not photo_urls:
        issues.append(UploadIssue(
            UploadIssue.ERROR, 'photos',
            'Нет фотографий — карточка WB будет без изображений',
            'Убедитесь что фото загружены в данные товара'
        ))
        return {'status': 'error', 'count': 0, 'details': 'photo_urls пуст'}

    # Ключевая проверка: привязка к SupplierProduct
    if not product.supplier_product_id:
        issues.append(UploadIssue(
            UploadIssue.ERROR, 'photos',
            f'Есть {len(photo_urls)} URL фото, но нет привязки к SupplierProduct '
            f'(supplier_product_id = None). Фото НЕ попадут на WB!',
            'Привяжите ImportedProduct к SupplierProduct через supplier_product_id'
        ))
        return {
            'status': 'error',
            'count': len(photo_urls),
            'supplier_product_id': None,
            'details': 'supplier_product_id = None → generate_public_photo_urls() вернёт []'
        }

    # Проверяем SupplierProduct
    from models import SupplierProduct
    sp = SupplierProduct.query.get(product.supplier_product_id)
    if not sp:
        issues.append(UploadIssue(
            UploadIssue.ERROR, 'photos',
            f'supplier_product_id={product.supplier_product_id}, но запись SupplierProduct не найдена'
        ))
        return {'status': 'error', 'count': len(photo_urls), 'details': 'SupplierProduct не найден'}

    if not sp.photo_urls_json:
        issues.append(UploadIssue(
            UploadIssue.ERROR, 'photos',
            f'SupplierProduct#{sp.id} существует, но photo_urls_json пуст'
        ))
        return {'status': 'error', 'count': len(photo_urls), 'details': 'SP photo_urls_json пуст'}

    # Считаем валидные URL
    try:
        sp_photos = json.loads(sp.photo_urls_json)
        valid = 0
        for ph in sp_photos:
            if isinstance(ph, dict):
                url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
            elif isinstance(ph, str):
                url = ph
            else:
                url = None
            if url and url.startswith('http'):
                valid += 1

        if valid == 0:
            issues.append(UploadIssue(
                UploadIssue.WARNING, 'photos',
                f'SupplierProduct#{sp.id} имеет {len(sp_photos)} записей фото, но ни одного валидного URL'
            ))
            return {'status': 'warning', 'count': len(sp_photos), 'valid_urls': 0}

        # Проверяем PUBLIC_BASE_URL
        try:
            from flask import current_app
            public_base = current_app.config.get('PUBLIC_BASE_URL', '')
            if not public_base:
                issues.append(UploadIssue(
                    UploadIssue.WARNING, 'photos',
                    f'{valid} фото доступны, но PUBLIC_BASE_URL не задан — '
                    f'будет использован file upload (медленнее)',
                ))
        except RuntimeError:
            pass  # Нет app context

        return {'status': 'ok', 'count': valid, 'supplier_product_id': sp.id,
                'details': f'{valid} фото готовы к загрузке'}
    except Exception as e:
        issues.append(UploadIssue(UploadIssue.WARNING, 'photos', f'Ошибка проверки фото SP: {e}'))
        return {'status': 'warning', 'count': len(photo_urls), 'details': str(e)}


def _check_characteristics(product, issues: List[UploadIssue]) -> Dict:
    """Проверка характеристик."""
    if not product.wb_subject_id:
        issues.append(UploadIssue(
            UploadIssue.ERROR, 'characteristics',
            'Нет wb_subject_id — характеристики не будут построены',
            'Задайте категорию WB для товара'
        ))
        return {'status': 'error', 'count': 0, 'details': 'Нет wb_subject_id'}

    # Собираем все источники характеристик
    sources = {}
    total_chars = 0

    # 1. Основной JSON
    if product.characteristics:
        try:
            raw = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
            if isinstance(raw, dict):
                filled = {k: v for k, v in raw.items() if not k.startswith('_') and v}
                if filled:
                    sources['characteristics_json'] = len(filled)
                    total_chars += len(filled)
        except Exception:
            pass

    # 2. Отдельные поля
    field_count = 0
    if product.colors:
        try:
            if json.loads(product.colors):
                field_count += 1
        except Exception:
            pass
    if product.materials:
        try:
            if json.loads(product.materials):
                field_count += 1
        except Exception:
            pass
    if product.gender:
        field_count += 1
    if product.country:
        field_count += 1
    if field_count:
        sources['fields'] = field_count
        total_chars += field_count

    # 3. AI данные
    ai_count = 0
    for attr in ['ai_colors', 'ai_materials', 'ai_dimensions', 'ai_attributes']:
        val = getattr(product, attr, None)
        if val:
            try:
                if json.loads(val) if isinstance(val, str) else val:
                    ai_count += 1
            except Exception:
                pass
    if product.ai_gender:
        ai_count += 1
    if product.ai_country:
        ai_count += 1
    if ai_count:
        sources['ai'] = ai_count
        total_chars += ai_count

    if total_chars == 0:
        issues.append(UploadIssue(
            UploadIssue.ERROR, 'characteristics',
            'Нет характеристик ни в одном источнике (JSON, поля, AI)',
            'Заполните характеристики товара или запустите AI-анализ'
        ))
        return {'status': 'error', 'count': 0, 'sources': {}, 'details': 'Пустые характеристики'}

    if total_chars < 3:
        issues.append(UploadIssue(
            UploadIssue.WARNING, 'characteristics',
            f'Мало характеристик ({total_chars}). WB может отклонить карточку.',
        ))
        return {'status': 'warning', 'count': total_chars, 'sources': sources,
                'details': f'{total_chars} характеристик из {sources}'}

    return {'status': 'ok', 'count': total_chars, 'sources': sources,
            'details': f'{total_chars} характеристик из {sources}'}


def _check_barcodes(product, issues: List[UploadIssue]) -> Dict:
    """Проверка баркодов."""
    if not product.barcodes:
        issues.append(UploadIssue(
            UploadIssue.WARNING, 'barcodes',
            'Нет баркодов — карточка может быть отклонена WB',
            'Добавьте EAN-13 баркоды'
        ))
        return {'status': 'warning', 'count': 0}

    try:
        barcodes = json.loads(product.barcodes)
        if not barcodes:
            issues.append(UploadIssue(UploadIssue.WARNING, 'barcodes', 'Массив баркодов пуст'))
            return {'status': 'warning', 'count': 0}
        return {'status': 'ok', 'count': len(barcodes)}
    except Exception:
        issues.append(UploadIssue(UploadIssue.WARNING, 'barcodes', 'Невалидный JSON в barcodes'))
        return {'status': 'warning', 'count': 0}


def _check_brand(product, issues: List[UploadIssue]) -> Dict:
    """Проверка бренда."""
    if not product.brand:
        issues.append(UploadIssue(
            UploadIssue.ERROR, 'brand',
            'Бренд не указан — будет использован "NoName", WB может отклонить',
            'Укажите бренд товара'
        ))
        return {'status': 'error', 'brand': None}

    if not product.resolved_brand_id:
        issues.append(UploadIssue(
            UploadIssue.WARNING, 'brand',
            f'Бренд "{product.brand}" не подтверждён в реестре WB',
            'Проверьте и зарегистрируйте бренд в личном кабинете WB'
        ))
        return {'status': 'warning', 'brand': product.brand, 'resolved': False}

    return {'status': 'ok', 'brand': product.brand, 'resolved': True}


def _check_title(product, issues: List[UploadIssue]) -> Dict:
    """Проверка названия."""
    if not product.title:
        issues.append(UploadIssue(UploadIssue.ERROR, 'title', 'Название не указано'))
        return {'status': 'error', 'length': 0}

    length = len(product.title)
    if length > 60:
        issues.append(UploadIssue(
            UploadIssue.WARNING, 'title',
            f'Название {length} символов (WB лимит 60) — будет обрезано'
        ))
        return {'status': 'warning', 'length': length, 'max': 60}

    return {'status': 'ok', 'length': length}


def _check_description(product, issues: List[UploadIssue]) -> Dict:
    """Проверка описания."""
    desc = product.description or product.title or ''
    length = len(desc)

    if length < 10:
        issues.append(UploadIssue(
            UploadIssue.WARNING, 'description',
            f'Описание слишком короткое ({length} символов)'
        ))
        return {'status': 'warning', 'length': length}

    if length < 1000:
        issues.append(UploadIssue(
            UploadIssue.INFO, 'description',
            f'Описание {length} символов (WB рекомендует ≥ 1000)'
        ))

    return {'status': 'ok', 'length': length}


def _check_category(product, issues: List[UploadIssue]) -> Dict:
    """Проверка категории."""
    if not product.wb_subject_id:
        issues.append(UploadIssue(
            UploadIssue.ERROR, 'category',
            'Категория WB не определена',
            'Определите wb_subject_id через маппинг категорий'
        ))
        return {'status': 'error', 'subject_id': None}

    return {
        'status': 'ok',
        'subject_id': product.wb_subject_id,
        'name': product.mapped_wb_category or ''
    }
