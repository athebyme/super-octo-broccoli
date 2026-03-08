# -*- coding: utf-8 -*-
"""
Диагностический скрипт для проверки готовности товаров из "Мои товары" к загрузке на WB.

Проверяет все аспекты:
- Цены: supplier_price, PricingSettings, calculated_price
- Фото: photo_urls, supplier_product_id привязка, PUBLIC_BASE_URL
- Характеристики: characteristics JSON, wb_subject_id, маппинг имён
- Баркоды, размеры, бренд, описание

Запуск:
    python scripts/validate_upload_readiness.py [--seller-id N] [--product-id N] [--fix]
"""
import sys
import os
import json
import argparse
import logging

# Добавляем корень проекта в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


class UploadReadinessValidator:
    """Валидатор готовности товаров к загрузке на WB."""

    def __init__(self, app):
        self.app = app
        self.issues = []
        self.stats = {
            'total': 0,
            'price_ok': 0,
            'price_missing': 0,
            'price_no_supplier_price': 0,
            'price_no_settings': 0,
            'price_no_calculated': 0,
            'photos_ok': 0,
            'photos_no_urls': 0,
            'photos_no_sp_link': 0,
            'photos_no_public_base': 0,
            'chars_ok': 0,
            'chars_empty': 0,
            'chars_no_subject': 0,
            'chars_partial': 0,
            'barcodes_ok': 0,
            'barcodes_missing': 0,
            'brand_ok': 0,
            'brand_missing': 0,
            'brand_unresolved': 0,
            'title_ok': 0,
            'title_missing': 0,
            'title_too_long': 0,
            'description_ok': 0,
            'description_missing': 0,
            'description_too_short': 0,
            'category_ok': 0,
            'category_missing': 0,
            'fully_ready': 0,
        }

    def validate_all(self, seller_id=None, product_id=None, verbose=True):
        """Запускает полную проверку всех товаров."""
        from models import ImportedProduct, Seller, PricingSettings

        with self.app.app_context():
            # Получаем товары
            query = ImportedProduct.query
            if product_id:
                query = query.filter_by(id=product_id)
            elif seller_id:
                query = query.filter_by(seller_id=seller_id)

            products = query.all()
            self.stats['total'] = len(products)

            if not products:
                print("Нет товаров для проверки")
                return

            print(f"\n{'='*80}")
            print(f"ДИАГНОСТИКА ГОТОВНОСТИ К ЗАГРУЗКЕ НА WB")
            print(f"Товаров для проверки: {len(products)}")
            print(f"{'='*80}\n")

            # Проверяем глобальные настройки
            self._check_global_settings(seller_id or (products[0].seller_id if products else None))

            for product in products:
                issues = self._validate_product(product, verbose)
                if not issues:
                    self.stats['fully_ready'] += 1

            self._print_summary()

    def _check_global_settings(self, seller_id):
        """Проверяет глобальные настройки продавца."""
        from models import Seller, PricingSettings
        from flask import current_app

        print("--- ГЛОБАЛЬНЫЕ НАСТРОЙКИ ---\n")

        if seller_id:
            seller = Seller.query.get(seller_id)
            if seller:
                # API ключ
                has_key = seller.has_valid_api_key() if hasattr(seller, 'has_valid_api_key') else bool(seller.wb_api_key)
                status = "OK" if has_key else "ОТСУТСТВУЕТ"
                print(f"  WB API ключ: {status}")

                # Ценообразование
                pricing = PricingSettings.query.filter_by(seller_id=seller_id).first()
                if pricing and pricing.is_enabled:
                    print(f"  Ценообразование: ВКЛЮЧЕНО")
                    print(f"    Комиссия WB: {pricing.wb_commission_pct}%")
                    print(f"    Налоговая ставка: {pricing.tax_rate}")
                    print(f"    Наценочный множитель (Y): {pricing.inflated_multiplier}")
                elif pricing:
                    print(f"  Ценообразование: ОТКЛЮЧЕНО (есть настройки, но is_enabled=False)")
                else:
                    print(f"  Ценообразование: НЕ НАСТРОЕНО (нет PricingSettings)")

        # PUBLIC_BASE_URL
        public_base = current_app.config.get('PUBLIC_BASE_URL', '')
        if public_base:
            print(f"  PUBLIC_BASE_URL: {public_base}")
        else:
            print(f"  PUBLIC_BASE_URL: НЕ ЗАДАН (фото через URL-based upload невозможны)")

        print()

    def _validate_product(self, product, verbose=False):
        """Валидирует один товар. Возвращает список проблем."""
        issues = []

        # === ЦЕНА ===
        price_issues = self._validate_price(product)
        issues.extend(price_issues)

        # === ФОТО ===
        photo_issues = self._validate_photos(product)
        issues.extend(photo_issues)

        # === ХАРАКТЕРИСТИКИ ===
        char_issues = self._validate_characteristics(product)
        issues.extend(char_issues)

        # === БАРКОДЫ ===
        barcode_issues = self._validate_barcodes(product)
        issues.extend(barcode_issues)

        # === БРЕНД ===
        brand_issues = self._validate_brand(product)
        issues.extend(brand_issues)

        # === НАЗВАНИЕ ===
        title_issues = self._validate_title(product)
        issues.extend(title_issues)

        # === ОПИСАНИЕ ===
        desc_issues = self._validate_description(product)
        issues.extend(desc_issues)

        # === КАТЕГОРИЯ ===
        cat_issues = self._validate_category(product)
        issues.extend(cat_issues)

        if verbose and issues:
            self._print_product_issues(product, issues)

        return issues

    def _validate_price(self, product):
        """Проверка цены."""
        issues = []

        if not product.supplier_price or product.supplier_price <= 0:
            issues.append(('ОШИБКА', 'ЦЕНА', f'supplier_price пуст или ≤ 0 (значение: {product.supplier_price})'))
            self.stats['price_no_supplier_price'] += 1
        else:
            # Проверяем есть ли рассчитанная цена
            from models import PricingSettings
            pricing = PricingSettings.query.filter_by(seller_id=product.seller_id).first()

            if not pricing or not pricing.is_enabled:
                if not product.calculated_price:
                    issues.append(('ОШИБКА', 'ЦЕНА', 'PricingSettings не настроены И calculated_price пуст — цена НЕ будет отправлена'))
                    self.stats['price_no_settings'] += 1
                else:
                    # Есть calculated_price, но нет формулы — будет использован фолбэк
                    self.stats['price_ok'] += 1
            else:
                # Есть настройки — проверяем что формула даст результат
                from services.pricing_engine import calculate_price
                try:
                    result = calculate_price(
                        purchase_price=product.supplier_price,
                        settings=pricing,
                        product_id=product.id
                    )
                    if result:
                        self.stats['price_ok'] += 1
                        if result['final_price'] <= 0:
                            issues.append(('ПРЕДУПР', 'ЦЕНА', f'Рассчитанная цена ≤ 0: Z={result["final_price"]}'))
                    else:
                        issues.append(('ОШИБКА', 'ЦЕНА', f'calculate_price вернул None для supplier_price={product.supplier_price}'))
                        self.stats['price_no_calculated'] += 1
                except Exception as e:
                    issues.append(('ОШИБКА', 'ЦЕНА', f'Ошибка расчёта цены: {e}'))
                    self.stats['price_no_calculated'] += 1

        if not issues:
            self.stats['price_ok'] += 1
        else:
            self.stats['price_missing'] += 1

        return issues

    def _validate_photos(self, product):
        """Проверка фотографий."""
        issues = []
        photo_urls = []

        if product.photo_urls:
            try:
                photo_urls = json.loads(product.photo_urls)
            except (json.JSONDecodeError, TypeError):
                issues.append(('ОШИБКА', 'ФОТО', f'photo_urls не является валидным JSON: {product.photo_urls[:100]}'))

        if not photo_urls:
            issues.append(('ОШИБКА', 'ФОТО', 'photo_urls пуст — фотографии НЕ будут загружены'))
            self.stats['photos_no_urls'] += 1
            return issues

        # Проверяем привязку к SupplierProduct
        if not product.supplier_product_id:
            issues.append(('ОШИБКА', 'ФОТО',
                f'supplier_product_id = None — generate_public_photo_urls() вернёт [] '
                f'(есть {len(photo_urls)} URL в photo_urls, но они НЕ попадут на WB)'))
            self.stats['photos_no_sp_link'] += 1
        else:
            # Проверяем что SupplierProduct реально существует и имеет фото
            from models import SupplierProduct
            sp = SupplierProduct.query.get(product.supplier_product_id)
            if not sp:
                issues.append(('ОШИБКА', 'ФОТО', f'supplier_product_id={product.supplier_product_id} но SupplierProduct не найден в БД'))
                self.stats['photos_no_sp_link'] += 1
            elif not sp.photo_urls_json:
                issues.append(('ОШИБКА', 'ФОТО',
                    f'SupplierProduct#{sp.id} существует, но photo_urls_json пуст'))
                self.stats['photos_no_sp_link'] += 1
            else:
                # Проверяем что URL фото валидные
                try:
                    sp_photos = json.loads(sp.photo_urls_json)
                    valid_count = 0
                    for ph in sp_photos:
                        if isinstance(ph, dict):
                            url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
                        elif isinstance(ph, str):
                            url = ph
                        else:
                            url = None
                        if url and (url.startswith('http://') or url.startswith('https://')):
                            valid_count += 1

                    if valid_count == 0:
                        issues.append(('ОШИБКА', 'ФОТО', f'SupplierProduct#{sp.id} имеет {len(sp_photos)} записей, но ни одного валидного URL'))
                    else:
                        self.stats['photos_ok'] += 1
                except Exception:
                    issues.append(('ОШИБКА', 'ФОТО', f'Не удалось распарсить photo_urls_json SupplierProduct#{sp.id}'))

        return issues

    def _validate_characteristics(self, product):
        """Проверка характеристик."""
        issues = []

        # 1. wb_subject_id
        if not product.wb_subject_id:
            issues.append(('ОШИБКА', 'ХАРАКТ', 'wb_subject_id не задан — характеристики НЕ будут построены'))
            self.stats['chars_no_subject'] += 1
            return issues

        # 2. Собираем все доступные характеристики
        chars_dict = {}
        chars_source = 'нет данных'

        if product.characteristics:
            try:
                raw = json.loads(product.characteristics) if isinstance(product.characteristics, str) else product.characteristics
                if isinstance(raw, dict):
                    chars_dict = {k: v for k, v in raw.items() if not k.startswith('_') and v}
                    if chars_dict:
                        chars_source = f'characteristics JSON ({len(chars_dict)} полей)'
            except Exception:
                issues.append(('ПРЕДУПР', 'ХАРАКТ', 'characteristics не парсится как JSON'))

        # Из отдельных полей
        field_chars = {}
        if product.colors:
            try:
                colors = json.loads(product.colors)
                if colors:
                    field_chars['Цвет'] = colors
            except Exception:
                pass
        if product.materials:
            try:
                materials = json.loads(product.materials)
                if materials:
                    field_chars['Материал'] = materials
            except Exception:
                pass
        if product.gender:
            field_chars['Пол'] = product.gender
        if product.country:
            field_chars['Страна производства'] = product.country

        # AI характеристики
        ai_chars = {}
        for field in ['ai_colors', 'ai_materials', 'ai_dimensions', 'ai_attributes']:
            val = getattr(product, field, None)
            if val:
                try:
                    parsed = json.loads(val) if isinstance(val, str) else val
                    if parsed:
                        ai_chars[field] = parsed
                except Exception:
                    pass
        if product.ai_gender:
            ai_chars['ai_gender'] = product.ai_gender
        if product.ai_country:
            ai_chars['ai_country'] = product.ai_country

        total_sources = len(chars_dict) + len(field_chars) + len(ai_chars)

        if total_sources == 0:
            issues.append(('ОШИБКА', 'ХАРАКТ',
                'Нет данных ни в characteristics JSON, ни в отдельных полях, ни в AI полях'))
            self.stats['chars_empty'] += 1
        else:
            # Проверяем полноту: обязательные поля для WB
            all_chars = {}
            all_chars.update(chars_dict)
            for k, v in field_chars.items():
                if k not in all_chars:
                    all_chars[k] = v

            required_char_names = ['Цвет', 'Страна производства', 'Пол']
            missing_required = []
            for name in required_char_names:
                found = False
                for k in all_chars:
                    if name.lower() in k.lower():
                        found = True
                        break
                if not found:
                    # Проверяем AI
                    if name == 'Цвет' and ('ai_colors' in ai_chars and ai_chars['ai_colors']):
                        found = True
                    elif name == 'Страна производства' and product.ai_country:
                        found = True
                    elif name == 'Пол' and product.ai_gender:
                        found = True

                if not found:
                    missing_required.append(name)

            if missing_required:
                issues.append(('ПРЕДУПР', 'ХАРАКТ',
                    f'Отсутствуют часто обязательные характеристики: {", ".join(missing_required)}'))
                self.stats['chars_partial'] += 1
            else:
                self.stats['chars_ok'] += 1

            # Инфо о доступных данных
            if not issues or all(i[0] == 'ПРЕДУПР' for i in issues):
                details = []
                if chars_dict:
                    details.append(f'JSON: {len(chars_dict)}')
                if field_chars:
                    details.append(f'поля: {len(field_chars)}')
                if ai_chars:
                    details.append(f'AI: {len(ai_chars)}')

        return issues

    def _validate_barcodes(self, product):
        """Проверка баркодов."""
        issues = []
        if product.barcodes:
            try:
                barcodes = json.loads(product.barcodes)
                if not barcodes:
                    issues.append(('ПРЕДУПР', 'БАРКОД', 'Массив баркодов пуст'))
                    self.stats['barcodes_missing'] += 1
                else:
                    self.stats['barcodes_ok'] += 1
            except Exception:
                issues.append(('ПРЕДУПР', 'БАРКОД', 'barcodes не является валидным JSON'))
                self.stats['barcodes_missing'] += 1
        else:
            issues.append(('ПРЕДУПР', 'БАРКОД', 'Нет баркодов — WB может не принять карточку'))
            self.stats['barcodes_missing'] += 1
        return issues

    def _validate_brand(self, product):
        """Проверка бренда."""
        issues = []
        if not product.brand:
            issues.append(('ОШИБКА', 'БРЕНД', 'Бренд не указан — будет использован "NoName"'))
            self.stats['brand_missing'] += 1
        elif not product.resolved_brand_id:
            issues.append(('ПРЕДУПР', 'БРЕНД', f'Бренд "{product.brand}" не подтверждён в реестре WB'))
            self.stats['brand_unresolved'] += 1
        else:
            self.stats['brand_ok'] += 1
        return issues

    def _validate_title(self, product):
        """Проверка названия."""
        issues = []
        if not product.title:
            issues.append(('ОШИБКА', 'НАЗВ', 'Название не указано'))
            self.stats['title_missing'] += 1
        elif len(product.title) > 60:
            issues.append(('ПРЕДУПР', 'НАЗВ', f'Название {len(product.title)} символов (WB лимит 60) — будет обрезано'))
            self.stats['title_too_long'] += 1
        else:
            self.stats['title_ok'] += 1
        return issues

    def _validate_description(self, product):
        """Проверка описания."""
        issues = []
        desc = product.description or product.title or ''
        if not desc or len(desc) < 10:
            issues.append(('ПРЕДУПР', 'ОПИСАНИЕ', f'Описание слишком короткое ({len(desc)} сим.)'))
            self.stats['description_too_short'] += 1
        elif len(desc) < 1000:
            issues.append(('ПРЕДУПР', 'ОПИСАНИЕ', f'Описание {len(desc)} сим. (рекомендация WB ≥ 1000)'))
            self.stats['description_ok'] += 1
        else:
            self.stats['description_ok'] += 1
        return issues

    def _validate_category(self, product):
        """Проверка категории WB."""
        issues = []
        if not product.wb_subject_id:
            issues.append(('ОШИБКА', 'КАТЕГОРИЯ', 'wb_subject_id не задан'))
            self.stats['category_missing'] += 1
        else:
            self.stats['category_ok'] += 1
        return issues

    def _print_product_issues(self, product, issues):
        """Печать проблем одного товара."""
        errors = [i for i in issues if i[0] == 'ОШИБКА']
        warnings = [i for i in issues if i[0] == 'ПРЕДУПР']

        title_preview = (product.title or 'Без названия')[:50]
        print(f"--- Товар #{product.id} | {product.external_id} | {title_preview}")
        print(f"    Статус: {product.import_status} | Категория WB: {product.wb_subject_id} | "
              f"supplier_price: {product.supplier_price} | supplier_product_id: {product.supplier_product_id}")

        for level, area, msg in issues:
            marker = "!!!" if level == 'ОШИБКА' else "(?)"
            print(f"    {marker} [{area}] {msg}")
        print()

    def _print_summary(self):
        """Печать итоговой сводки."""
        s = self.stats
        print(f"\n{'='*80}")
        print(f"ИТОГОВАЯ СВОДКА")
        print(f"{'='*80}")
        print(f"Всего товаров: {s['total']}")
        print(f"Полностью готовы к загрузке: {s['fully_ready']} / {s['total']}")
        print()

        print("ЦЕНА:")
        print(f"  OK: {s['price_ok']}")
        print(f"  Нет supplier_price: {s['price_no_supplier_price']}")
        print(f"  Нет PricingSettings и calculated_price: {s['price_no_settings']}")
        print(f"  Ошибка расчёта: {s['price_no_calculated']}")
        print()

        print("ФОТО:")
        print(f"  OK: {s['photos_ok']}")
        print(f"  Нет photo_urls: {s['photos_no_urls']}")
        print(f"  Нет supplier_product_id привязки: {s['photos_no_sp_link']}")
        print()

        print("ХАРАКТЕРИСТИКИ:")
        print(f"  OK: {s['chars_ok']}")
        print(f"  Пустые: {s['chars_empty']}")
        print(f"  Нет wb_subject_id: {s['chars_no_subject']}")
        print(f"  Частичные (не хватает обязательных): {s['chars_partial']}")
        print()

        print("БАРКОДЫ:")
        print(f"  OK: {s['barcodes_ok']}")
        print(f"  Отсутствуют: {s['barcodes_missing']}")
        print()

        print("БРЕНД:")
        print(f"  OK: {s['brand_ok']}")
        print(f"  Не указан: {s['brand_missing']}")
        print(f"  Не подтверждён: {s['brand_unresolved']}")
        print()

        print("КАТЕГОРИЯ WB:")
        print(f"  OK: {s['category_ok']}")
        print(f"  Не задана: {s['category_missing']}")
        print()

        # Рекомендации
        print(f"{'='*80}")
        print(f"РЕКОМЕНДАЦИИ")
        print(f"{'='*80}")

        if s['price_no_supplier_price'] > 0:
            print(f"  1. Заполнить supplier_price у {s['price_no_supplier_price']} товаров")
            print(f"     -> Убедитесь что CSV с ценами от поставщика загружен и привязан")

        if s['price_no_settings'] > 0:
            print(f"  2. Настроить PricingSettings для продавца ИЛИ предрассчитать calculated_price")
            print(f"     -> Без формулы и без calculated_price цена будет = 0 (WB отклонит)")

        if s['photos_no_sp_link'] > 0:
            print(f"  3. У {s['photos_no_sp_link']} товаров нет привязки supplier_product_id")
            print(f"     -> generate_public_photo_urls() вернёт [] -> фото НЕ загрузятся")
            print(f"     -> Нужно привязать ImportedProduct.supplier_product_id к SupplierProduct")

        if s['photos_no_urls'] > 0:
            print(f"  4. У {s['photos_no_urls']} товаров пустые photo_urls")

        if s['chars_empty'] > 0:
            print(f"  5. У {s['chars_empty']} товаров нет характеристик ни в одном источнике")
            print(f"     -> Нужно заполнить characteristics JSON или отдельные поля")

        if s['chars_no_subject'] > 0:
            print(f"  6. У {s['chars_no_subject']} товаров не задан wb_subject_id")
            print(f"     -> Без категории WB характеристики не будут построены")

        print()


def main():
    parser = argparse.ArgumentParser(description='Валидация готовности товаров к загрузке на WB')
    parser.add_argument('--seller-id', type=int, help='ID продавца')
    parser.add_argument('--product-id', type=int, help='ID конкретного товара')
    parser.add_argument('--quiet', action='store_true', help='Показать только итоги')
    args = parser.parse_args()

    from app import create_app
    app = create_app()

    validator = UploadReadinessValidator(app)
    validator.validate_all(
        seller_id=args.seller_id,
        product_id=args.product_id,
        verbose=not args.quiet
    )


if __name__ == '__main__':
    main()
