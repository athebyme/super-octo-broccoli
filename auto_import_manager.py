# -*- coding: utf-8 -*-
"""
Менеджер автоимпорта товаров из внешних источников
"""
import csv
import re
import json
import requests
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from io import StringIO, BytesIO
from PIL import Image
import logging

from models import (
    db, AutoImportSettings, ImportedProduct, CategoryMapping,
    Product, Seller
)

logger = logging.getLogger(__name__)


class SizeParser:
    """
    Интеллектуальный парсер размеров товаров
    """

    def __init__(self):
        # Паттерны для извлечения размеров
        self.dimension_patterns = {
            'length': r'(?:общ\.|общая|общий)?\s*(?:длин[аы]|дл\.)\s*(?:проник[а-я]*\.)?\s*(\d+(?:[.,]\d+)?)\s*(?:см|мм|м)?',
            'diameter': r'(?:макс\.|максимальн[а-я]*\.)?\s*диаметр\s*(?:при\s+расширении|шариков)?\s*(\d+(?:[.,]\d+)?)\s*(?:см|мм)?',
            'width': r'(?:макс\.|максимальн[а-я]*\.)?\s*ширин[аы]\s*(\d+(?:[.,]\d+)?)\s*(?:см|мм)?',
            'depth': r'глубин[аы]\s*(?:проник[а-я]*\.?)?\s*(\d+(?:[.,]\d+)?)\s*(?:см|мм)?',
            'weight': r'вес\s*(\d+(?:[.,]\d+)?)\s*(?:г|кг|гр)?',
            'volume': r'(?:объ[её]м|мл)\s*(\d+(?:[.,]\d+)?)\s*(?:мл|л)?',
        }

    def parse(self, sizes_raw: str) -> Dict[str, any]:
        """
        Парсит строку размеров и возвращает структурированные данные

        Returns:
            {
                'raw': 'исходная строка',
                'dimensions': {
                    'length': [значение1, значение2, ...],
                    'diameter': [значение1, ...],
                    'weight': значение,
                    ...
                },
                'simple_sizes': ['S', 'M', 'L'] или ['42', '44'] для одежды
            }
        """
        if not sizes_raw:
            return {'raw': '', 'dimensions': {}, 'simple_sizes': []}

        result = {
            'raw': sizes_raw,
            'dimensions': {},
            'simple_sizes': []
        }

        sizes_lower = sizes_raw.lower()

        # Извлекаем размерности
        for dim_type, pattern in self.dimension_patterns.items():
            matches = re.findall(pattern, sizes_lower, re.IGNORECASE)
            if matches:
                # Конвертируем в float, заменяя запятую на точку
                values = [float(m.replace(',', '.')) for m in matches if m]
                if values:
                    result['dimensions'][dim_type] = values

        # Если не нашли размерности, пробуем определить как простые размеры
        if not result['dimensions']:
            # Размеры одежды (42-44, S-M-L и т.д.)
            if re.search(r'\d{2}-\d{2}', sizes_raw):  # 42-44 или 46-48
                # Для одежды/белья размеры через тире - это отдельные размеры, не диапазон
                # "46-48" -> ["46", "48"], а НЕ ["46", "47", "48"]
                parts = sizes_raw.split('-')
                result['simple_sizes'] = [p.strip() for p in parts if p.strip()]
            elif ',' in sizes_raw:
                result['simple_sizes'] = [s.strip() for s in sizes_raw.split(',') if s.strip()]
            else:
                result['simple_sizes'] = [sizes_raw.strip()]

        return result

    def format_for_wb(self, parsed_sizes: Dict, wb_category_id: int) -> Dict[str, str]:
        """
        Форматирует размеры для конкретной категории WB

        Returns:
            {'characteristic_name': 'value', ...}
        """
        wb_characteristics = {}
        dimensions = parsed_sizes.get('dimensions', {})

        # Маппинг характеристик по категориям
        # Для интим-товаров обычно есть: длина, диаметр, вес
        if dimensions.get('length'):
            # Берем максимальную длину если их несколько
            length = max(dimensions['length'])
            wb_characteristics['Длина'] = f"{length:.1f} см"

        if dimensions.get('diameter'):
            # Берем максимальный диаметр
            diameter = max(dimensions['diameter'])
            wb_characteristics['Диаметр'] = f"{diameter:.1f} см"

        if dimensions.get('width'):
            width = max(dimensions['width'])
            wb_characteristics['Ширина'] = f"{width:.1f} см"

        if dimensions.get('depth'):
            depth = max(dimensions['depth'])
            wb_characteristics['Глубина'] = f"{depth:.1f} см"

        if dimensions.get('weight'):
            weight = dimensions['weight'][0]
            wb_characteristics['Вес'] = f"{weight:.0f} г"

        if dimensions.get('volume'):
            volume = dimensions['volume'][0]
            wb_characteristics['Объем'] = f"{volume:.0f} мл"

        # Для одежды
        if parsed_sizes.get('simple_sizes'):
            wb_characteristics['Размер'] = ', '.join(parsed_sizes['simple_sizes'])

        return wb_characteristics


class CSVProductParser:
    """
    Парсер CSV файлов с товарами

    Формат CSV (sexoptovik):
    1 - id товара (формат: id-<id>-<код продавца>)
    2 - артикул поставщика (модель товара)
    3 - название товара
    4 - категория товара (через # разные категории)
    5 - бренд
    6 - страна производства
    7 - общая категория товара
    8 - особенность товара
    9 - пол
    10 - цвет (если несколько - через запятую)
    11 - размеры
    12 - комплект (каждая вещь через запятую)
    13 - пустая колонка
    14 - коды фотографий
    15 - баркод (может быть несколько через запятую)
    16 - материал товара
    17 - батарейки (если нужны) + входят/не входят
    """

    def __init__(self, source_type: str = 'sexoptovik', delimiter: str = ';'):
        self.source_type = source_type
        self.delimiter = delimiter
        self.size_parser = SizeParser()

    def parse_csv_file(self, csv_content: str) -> List[Dict]:
        """
        Парсит CSV файл и возвращает список товаров

        Args:
            csv_content: Содержимое CSV файла

        Returns:
            Список словарей с данными товаров
        """
        products = []
        csv_file = StringIO(csv_content)
        reader = csv.reader(csv_file, delimiter=self.delimiter, quotechar='"')

        for row_num, row in enumerate(reader, 1):
            try:
                if len(row) < 15:
                    logger.warning(f"Строка {row_num}: недостаточно колонок ({len(row)})")
                    continue

                product = self._parse_row(row, row_num)
                if product:
                    products.append(product)
            except Exception as e:
                logger.error(f"Ошибка парсинга строки {row_num}: {e}")
                continue

        logger.info(f"Распарсено {len(products)} товаров из CSV")
        return products

    def _parse_row(self, row: List[str], row_num: int) -> Optional[Dict]:
        """Парсит одну строку CSV"""
        try:
            # Извлекаем базовые поля
            external_id = row[0].strip() if len(row) > 0 else ''
            vendor_code = row[1].strip() if len(row) > 1 else ''
            title = row[2].strip() if len(row) > 2 else ''

            # Категории (могут быть указаны через #)
            categories_raw = row[3].strip() if len(row) > 3 else ''
            categories = [c.strip() for c in categories_raw.split('#') if c.strip()]
            main_category = categories[0] if categories else ''

            # Остальные поля
            brand = row[4].strip() if len(row) > 4 else ''
            country = row[5].strip() if len(row) > 5 else ''
            general_category = row[6].strip() if len(row) > 6 else ''
            features = row[7].strip() if len(row) > 7 else ''
            gender = row[8].strip() if len(row) > 8 else ''

            # Цвета (через запятую)
            colors_raw = row[9].strip() if len(row) > 9 else ''
            colors = [c.strip() for c in colors_raw.split(',') if c.strip()]

            # Размеры
            sizes_raw = row[10].strip() if len(row) > 10 else ''
            sizes = self._parse_sizes(sizes_raw)
            logger.info(f"  РАЗМЕРЫ: '{sizes_raw}' → {sizes}")

            # Комплект
            bundle_raw = row[11].strip() if len(row) > 11 else ''
            bundle_items = [b.strip() for b in bundle_raw.split(',') if b.strip()]

            # Коды фотографий
            photo_codes_raw = row[13].strip() if len(row) > 13 else ''
            photo_urls = self._parse_photo_codes(external_id, photo_codes_raw)
            logger.info(f"  ФОТО: коды='{photo_codes_raw}' external_id='{external_id}' → {len(photo_urls)} URLs")
            if photo_urls:
                logger.info(f"  Первое фото: {photo_urls[0]}")

            # Баркоды (разделены через #)
            barcodes_raw = row[14].strip() if len(row) > 14 else ''
            barcodes = [b.strip() for b in barcodes_raw.split('#') if b.strip()]

            # Материалы
            materials_raw = row[15].strip() if len(row) > 15 else ''
            materials = [m.strip() for m in materials_raw.split(',') if m.strip()]

            # Батарейки
            batteries_raw = row[16].strip() if len(row) > 16 else ''

            # Формируем данные товара
            product_data = {
                'external_id': external_id,
                'external_vendor_code': vendor_code,
                'title': title,
                'category': main_category,
                'all_categories': categories,
                'general_category': general_category,
                'brand': brand,
                'country': country,
                'features': features,
                'gender': gender,
                'colors': colors,
                'sizes': sizes,
                'bundle_items': bundle_items,
                'photo_urls': photo_urls,
                'barcodes': barcodes,
                'materials': materials,
                'batteries': batteries_raw,
                'row_num': row_num
            }

            return product_data

        except Exception as e:
            logger.error(f"Ошибка обработки строки {row_num}: {e}")
            return None

    def _parse_sizes(self, sizes_raw: str) -> Dict:
        """
        Парсит размеры из строки с использованием умного парсера

        Returns:
            Словарь с структурированными данными о размерах
        """
        return self.size_parser.parse(sizes_raw)

    def _parse_photo_codes(self, product_id: str, photo_codes: str) -> List[Dict[str, str]]:
        """
        Формирует URLs фотографий

        Формат фотографий:
        - Без цензуры (sexoptovik): https://sexoptovik.ru/admin/_project/user_images/prods_res/{id}/{id}_{номер}_1200.jpg
        - С цензурой (блюр): https://x-story.ru/mp/_project/img_sx0_1200/{id}_{номер}_1200.jpg
        - Без цензуры (x-story): https://x-story.ru/mp/_project/img_sx_1200/{id}_{номер}_1200.jpg

        В CSV номера фотографий могут быть через запятую или пробелы

        По умолчанию используется sexoptovik (без цензуры).
        Если в настройках включена цензура - будет использоваться blur (x-story).

        Returns:
            List[Dict]: [{'sexoptovik': url, 'blur': url, 'original': url}, ...]
        """
        if not photo_codes or not product_id:
            return []

        photos = []
        # Определяем разделитель: запятая или пробелы
        if ',' in photo_codes:
            photo_nums = [p.strip() for p in photo_codes.split(',') if p.strip()]
        else:
            # Разделяем по пробелам (один или несколько)
            photo_nums = [p.strip() for p in photo_codes.split() if p.strip()]

        # Извлекаем числовой ID из external_id (формат: id-12345-код)
        match = re.search(r'id-(\d+)', product_id)
        if not match:
            # Пытаемся использовать сам product_id как числовой
            numeric_id = product_id
        else:
            numeric_id = match.group(1)

        for num in photo_nums:
            # Формируем все варианты URL
            # ВАЖНО: sexoptovik первый - он используется по умолчанию
            # ПРОБЛЕМА: /admin/_project/ требует авторизации и недоступен извне
            # TODO: Уточнить у пользователя публичный URL для фотографий
            photo_obj = {
                'sexoptovik': f"http://sexoptovik.ru/project/user_images/prods_res/{numeric_id}/{numeric_id}_{num}_1200.jpg",
                'blur': f"https://x-story.ru/mp/_project/img_sx0_1200/{numeric_id}_{num}_1200.jpg",
                'original': f"https://x-story.ru/mp/_project/img_sx_1200/{numeric_id}_{num}_1200.jpg"
            }
            photos.append(photo_obj)

        return photos


class CategoryMapper:
    """
    Маппер категорий из внешних источников в категории WB
    Использует точный маппинг из wb_categories_mapping.py
    """

    def __init__(self):
        # Импортируем точный маппинг категорий WB
        from wb_categories_mapping import get_best_category_match
        self.get_best_match = get_best_category_match

    def map_category(self, source_category: str, source_type: str = 'sexoptovik',
                    general_category: str = '', all_categories: List[str] = None,
                    product_title: str = '', external_id: str = None) -> Tuple[Optional[int], Optional[str], float]:
        """
        Определяет категорию WB для товара

        Args:
            source_category: Основная категория из источника
            source_type: Тип источника
            general_category: Общая категория
            all_categories: Все категории товара
            product_title: Название товара (для анализа ключевых слов)
            external_id: ID товара из внешнего источника (для ручных исправлений)

        Returns:
            Tuple[subject_id, subject_name, confidence]
        """
        if not source_category:
            return None, None, 0.0

        # Сначала проверяем БД (пользовательские переопределения через CategoryMapping)
        mapping = CategoryMapping.query.filter_by(
            source_category=source_category,
            source_type=source_type
        ).order_by(CategoryMapping.priority.desc()).first()

        if mapping:
            return mapping.wb_subject_id, mapping.wb_subject_name, mapping.confidence_score

        # Используем новый точный алгоритм (включая проверку ручных исправлений через ProductCategoryCorrection)
        subject_id, subject_name, confidence = self.get_best_match(
            csv_category=source_category,
            product_title=product_title,
            all_categories=all_categories,
            external_id=external_id,
            source_type=source_type
        )

        return subject_id, subject_name, confidence


class ImageProcessor:
    """
    Обработчик изображений товаров
    """

    @staticmethod
    def download_and_process_image(url: str, target_size: Tuple[int, int] = (1200, 1200),
                                   background_color: str = 'white') -> Optional[BytesIO]:
        """
        Скачивает и обрабатывает изображение

        Args:
            url: URL изображения
            target_size: Целевой размер (ширина, высота)
            background_color: Цвет фона для дорисовки

        Returns:
            BytesIO с обработанным изображением или None
        """
        try:
            # Заголовки для обхода защиты от hotlinking
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://sexoptovik.ru/',
                'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Sec-Fetch-Dest': 'image',
                'Sec-Fetch-Mode': 'no-cors',
                'Sec-Fetch-Site': 'same-origin'
            }

            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()

            # Проверяем, что получили изображение, а не HTML/текст
            content_type = response.headers.get('Content-Type', '')
            if not content_type.startswith('image/'):
                # Возможно, сервер вернул ошибку в виде HTML
                logger.warning(f"URL {url} вернул не изображение: Content-Type={content_type}")
                # Пробуем все равно распарсить

            img = Image.open(BytesIO(response.content))

            # Проверяем размер
            if img.size == target_size:
                # Уже нужный размер
                output = BytesIO()
                img.save(output, format='JPEG', quality=95)
                output.seek(0)
                return output

            # Нужно изменить размер с сохранением пропорций
            img_resized = ImageProcessor._resize_with_padding(img, target_size, background_color)

            output = BytesIO()
            img_resized.save(output, format='JPEG', quality=95)
            output.seek(0)
            return output

        except Exception as e:
            logger.error(f"Ошибка обработки изображения {url}: {e}")
            return None

    @staticmethod
    def _resize_with_padding(img: Image.Image, target_size: Tuple[int, int],
                            background_color: str = 'white') -> Image.Image:
        """
        Изменяет размер изображения с добавлением паддинга

        Args:
            img: Исходное изображение
            target_size: Целевой размер (ширина, высота)
            background_color: Цвет фона

        Returns:
            Изображение с новым размером
        """
        # Конвертируем в RGB если нужно
        if img.mode != 'RGB':
            img = img.convert('RGB')

        # Вычисляем коэффициент масштабирования
        img_width, img_height = img.size
        target_width, target_height = target_size

        ratio = min(target_width / img_width, target_height / img_height)

        # Новый размер с сохранением пропорций
        new_width = int(img_width * ratio)
        new_height = int(img_height * ratio)

        # Изменяем размер
        img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        # Создаем новое изображение с паддингом
        new_img = Image.new('RGB', target_size, background_color)

        # Вычисляем позицию для центрирования
        paste_x = (target_width - new_width) // 2
        paste_y = (target_height - new_height) // 2

        # Вставляем изображение
        new_img.paste(img_resized, (paste_x, paste_y))

        return new_img

    @staticmethod
    def check_image_url(url: str) -> bool:
        """Проверяет доступность изображения"""
        try:
            response = requests.head(url, timeout=5)
            return response.status_code == 200
        except:
            return False


class ProductValidator:
    """
    Валидатор товаров перед импортом в WB
    """

    @staticmethod
    def validate_product(product_data: Dict) -> Tuple[bool, List[str]]:
        """
        Валидирует товар перед импортом

        Args:
            product_data: Данные товара

        Returns:
            Tuple[is_valid, errors]
        """
        errors = []

        # Обязательные поля
        if not product_data.get('title'):
            errors.append("Отсутствует название товара")
        elif len(product_data['title']) < 3:
            errors.append("Название товара слишком короткое (минимум 3 символа)")

        if not product_data.get('external_vendor_code'):
            errors.append("Отсутствует артикул товара")

        if not product_data.get('category'):
            errors.append("Не определена категория товара")

        if not product_data.get('brand'):
            errors.append("Отсутствует бренд")

        # Фотографии
        if not product_data.get('photo_urls') or len(product_data['photo_urls']) == 0:
            errors.append("Отсутствуют фотографии товара")
        elif len(product_data['photo_urls']) > 30:
            errors.append(f"Слишком много фотографий ({len(product_data['photo_urls'])}), максимум 30")

        # Баркоды
        if not product_data.get('barcodes') or len(product_data['barcodes']) == 0:
            errors.append("Отсутствуют баркоды товара")

        # Размеры (должен быть хотя бы один)
        if not product_data.get('sizes') or len(product_data['sizes']) == 0:
            # Добавляем дефолтный размер
            product_data['sizes'] = ['One Size']

        # Цвета
        if not product_data.get('colors') or len(product_data['colors']) == 0:
            # Добавляем дефолтный цвет
            product_data['colors'] = ['Разноцветный']

        # Характеристики WB
        if not product_data.get('wb_subject_id'):
            errors.append("Не определена категория WB (subject_id)")

        is_valid = len(errors) == 0
        return is_valid, errors


class AutoImportManager:
    """
    Главный менеджер автоимпорта товаров
    """

    def __init__(self, seller: Seller, settings: AutoImportSettings):
        self.seller = seller
        self.settings = settings
        delimiter = settings.csv_delimiter if settings.csv_delimiter else ';'
        self.parser = CSVProductParser(settings.csv_source_type, delimiter)
        self.category_mapper = CategoryMapper()
        self.validator = ProductValidator()

    def run_import(self) -> Dict:
        """
        Запускает процесс импорта

        Returns:
            Статистика импорта
        """
        start_time = datetime.utcnow()

        try:
            # Обновляем статус
            self.settings.last_import_status = 'running'
            db.session.commit()

            # Скачиваем CSV
            logger.info(f"Скачивание CSV из {self.settings.csv_source_url}")
            csv_content = self._download_csv()

            # Парсим CSV
            logger.info("Парсинг CSV файла")
            products = self.parser.parse_csv_file(csv_content)

            self.settings.total_products_found = len(products)
            db.session.commit()

            # Обрабатываем каждый товар
            imported_count = 0
            skipped_count = 0
            failed_count = 0

            for product_data in products:
                result = self._process_product(product_data)
                if result == 'imported':
                    imported_count += 1
                elif result == 'skipped':
                    skipped_count += 1
                elif result == 'failed':
                    failed_count += 1

            # Обновляем статистику
            end_time = datetime.utcnow()
            duration = (end_time - start_time).total_seconds()

            self.settings.last_import_at = end_time
            self.settings.last_import_status = 'success'
            self.settings.last_import_duration = duration
            self.settings.products_imported = imported_count
            self.settings.products_skipped = skipped_count
            self.settings.products_failed = failed_count
            db.session.commit()

            stats = {
                'success': True,
                'total_found': len(products),
                'imported': imported_count,
                'skipped': skipped_count,
                'failed': failed_count,
                'duration': duration
            }

            logger.info(f"Импорт завершен: {stats}")
            return stats

        except Exception as e:
            logger.error(f"Ошибка импорта: {e}", exc_info=True)

            self.settings.last_import_status = 'failed'
            self.settings.last_import_error = str(e)
            db.session.commit()

            return {
                'success': False,
                'error': str(e)
            }

    def _download_csv(self) -> str:
        """Скачивает CSV файл"""
        response = requests.get(self.settings.csv_source_url, timeout=60)
        response.raise_for_status()

        # Определяем кодировку
        # Для sexoptovik используется cp1251 (windows-1251)
        if self.settings.csv_source_type == 'sexoptovik':
            encoding = 'cp1251'
        elif 'charset' in response.headers.get('content-type', ''):
            encoding = response.encoding
        else:
            # Пробуем определить автоматически
            try:
                return response.content.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    return response.content.decode('cp1251')
                except UnicodeDecodeError:
                    return response.content.decode('latin-1')

        return response.content.decode(encoding, errors='replace')

    def _process_product(self, product_data: Dict) -> str:
        """
        Обрабатывает один товар

        Returns:
            'imported', 'skipped' или 'failed'
        """
        try:
            external_id = product_data['external_id']

            # Определяем категорию WB (с учетом ручных исправлений)
            subject_id, subject_name, confidence = self.category_mapper.map_category(
                product_data['category'],
                self.settings.csv_source_type,
                product_data.get('general_category', ''),
                product_data.get('all_categories', []),
                product_data.get('title', ''),
                external_id=product_data.get('external_id')
            )

            # Подробное логирование для отладки категорий
            logger.info(f"📦 КАТЕГОРИЯ | Товар: {product_data.get('title', '')[:50]}...")
            logger.info(f"   CSV категория: {product_data['category']}")
            if product_data.get('all_categories'):
                logger.info(f"   Все категории CSV: {' > '.join(product_data.get('all_categories', []))}")
            logger.info(f"   ➜ WB категория: {subject_name} (ID: {subject_id}) | Уверенность: {confidence:.2f}")
            logger.info("-" * 80)

            product_data['wb_subject_id'] = subject_id
            product_data['wb_subject_name'] = subject_name
            product_data['category_confidence'] = confidence

            # Валидируем товар
            is_valid, errors = self.validator.validate_product(product_data)

            # Создаем или обновляем запись ImportedProduct
            imported_product = ImportedProduct.query.filter_by(
                seller_id=self.seller.id,
                external_id=external_id,
                source_type=self.settings.csv_source_type
            ).first()

            # Запоминаем, был ли товар уже импортирован ранее
            was_already_imported = False
            if imported_product:
                was_already_imported = (imported_product.import_status == 'imported')
                if was_already_imported:
                    logger.info(f"Товар {external_id} уже был импортирован на WB ранее, обновляем данные")
            else:
                imported_product = ImportedProduct(
                    seller_id=self.seller.id,
                    external_id=external_id,
                    source_type=self.settings.csv_source_type
                )

            # Заполняем данные (обновляем всегда, даже если товар уже импортирован)
            imported_product.external_vendor_code = product_data['external_vendor_code']
            imported_product.title = product_data['title']
            imported_product.category = product_data['category']
            imported_product.all_categories = json.dumps(product_data.get('all_categories', []), ensure_ascii=False)
            imported_product.mapped_wb_category = subject_name
            imported_product.wb_subject_id = subject_id
            imported_product.category_confidence = confidence
            imported_product.brand = product_data['brand']
            imported_product.country = product_data['country']
            imported_product.gender = product_data['gender']
            imported_product.colors = json.dumps(product_data['colors'], ensure_ascii=False)
            imported_product.sizes = json.dumps(product_data['sizes'], ensure_ascii=False)
            imported_product.materials = json.dumps(product_data['materials'], ensure_ascii=False)
            imported_product.photo_urls = json.dumps(product_data['photo_urls'], ensure_ascii=False)
            imported_product.barcodes = json.dumps(product_data['barcodes'], ensure_ascii=False)

            # Формируем описание
            description = self._generate_description(product_data)
            imported_product.description = description

            # ВАЖНО: Если товар уже был импортирован на WB, НЕ меняем статус обратно на 'validated'
            # Это предотвратит повторный импорт того же товара
            if not was_already_imported:
                if is_valid:
                    imported_product.import_status = 'validated'
                    imported_product.validation_errors = None
                else:
                    imported_product.import_status = 'failed'
                    imported_product.validation_errors = json.dumps(errors, ensure_ascii=False)
            else:
                # Товар уже импортирован - оставляем статус 'imported', но обновляем данные
                # Это позволит видеть актуальную информацию из CSV
                logger.info(f"Товар {external_id} сохраняет статус 'imported', данные обновлены")

            db.session.add(imported_product)
            db.session.commit()

            if was_already_imported:
                # Товар уже был импортирован - считаем его пропущенным, а не импортированным заново
                logger.info(f"Товар {external_id} уже импортирован, пропускаем")
                return 'skipped'
            elif is_valid:
                logger.info(f"Товар {external_id} успешно обработан и готов к импорту")
                return 'imported'
            else:
                logger.warning(f"Товар {external_id} не прошел валидацию: {errors}")
                return 'failed'

        except Exception as e:
            logger.error(f"Ошибка обработки товара {product_data.get('external_id')}: {e}", exc_info=True)
            return 'failed'

    def _generate_description(self, product_data: Dict) -> str:
        """Генерирует описание товара"""
        parts = []

        if product_data.get('title'):
            parts.append(f"**{product_data['title']}**\n")

        if product_data.get('brand'):
            parts.append(f"Бренд: {product_data['brand']}")

        if product_data.get('country'):
            parts.append(f"Страна производства: {product_data['country']}")

        if product_data.get('materials'):
            materials_str = ', '.join(product_data['materials'])
            parts.append(f"Материал: {materials_str}")

        if product_data.get('colors'):
            colors_str = ', '.join(product_data['colors'])
            parts.append(f"Цвет: {colors_str}")

        if product_data.get('sizes'):
            # Размеры - это структурированный объект, не список
            sizes_data = product_data['sizes']
            size_parts = []

            # Используем raw строку если есть
            if sizes_data.get('raw'):
                size_parts.append(sizes_data['raw'])
            # Или собираем из simple_sizes
            elif sizes_data.get('simple_sizes'):
                size_parts.append(', '.join(str(s) for s in sizes_data['simple_sizes']))
            # Или собираем из dimensions
            elif sizes_data.get('dimensions'):
                dims = sizes_data['dimensions']
                dim_strs = []
                if dims.get('length'):
                    dim_strs.append(f"длина {', '.join(str(v) for v in dims['length'])} см")
                if dims.get('diameter'):
                    dim_strs.append(f"диаметр {', '.join(str(v) for v in dims['diameter'])} см")
                if dims.get('width'):
                    dim_strs.append(f"ширина {', '.join(str(v) for v in dims['width'])} см")
                if dims.get('weight'):
                    dim_strs.append(f"вес {', '.join(str(v) for v in dims['weight'])} г")
                if dims.get('volume'):
                    dim_strs.append(f"объём {', '.join(str(v) for v in dims['volume'])} мл")
                if dim_strs:
                    size_parts.append(', '.join(dim_strs))

            if size_parts:
                parts.append(f"Размер: {'; '.join(size_parts)}")

        if product_data.get('features'):
            parts.append(f"\nОсобенности: {product_data['features']}")

        if product_data.get('bundle_items'):
            bundle_str = ', '.join(product_data['bundle_items'])
            parts.append(f"\nВ комплекте: {bundle_str}")

        if product_data.get('batteries'):
            parts.append(f"\nБатарейки: {product_data['batteries']}")

        return '\n'.join(parts)
