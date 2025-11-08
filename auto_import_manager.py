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

            # Комплект
            bundle_raw = row[11].strip() if len(row) > 11 else ''
            bundle_items = [b.strip() for b in bundle_raw.split(',') if b.strip()]

            # Коды фотографий
            photo_codes_raw = row[13].strip() if len(row) > 13 else ''
            photo_urls = self._parse_photo_codes(external_id, photo_codes_raw)

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

    def _parse_photo_codes(self, product_id: str, photo_codes: str) -> List[str]:
        """
        Формирует URLs фотографий

        Формат фотографий:
        - Без цензуры: http://sexoptovik.ru/_project/user_images/prods_res/{id}/{id}_{номер}_{1200}.jpg
        - С цензурой (блюр): https://x-story.ru/mp/_project/img_sx0_1200/{id}_{номер}_1200.jpg

        В CSV указаны только номера фотографий через запятую
        """
        if not photo_codes or not product_id:
            return []

        urls = []
        photo_nums = [p.strip() for p in photo_codes.split(',') if p.strip()]

        # Извлекаем числовой ID из external_id (формат: id-12345-код)
        match = re.search(r'id-(\d+)', product_id)
        if not match:
            # Пытаемся использовать сам product_id как числовой
            numeric_id = product_id
        else:
            numeric_id = match.group(1)

        for num in photo_nums:
            # Формируем URL (блюр версия по умолчанию)
            # Блюр: https://x-story.ru/mp/_project/img_sx0_1200/{id}_{номер}_1200.jpg
            blur_url = f"https://x-story.ru/mp/_project/img_sx0_1200/{numeric_id}_{num}_1200.jpg"
            urls.append(blur_url)

        return urls


class CategoryMapper:
    """
    Маппер категорий из внешних источников в категории WB
    """

    def __init__(self):
        # Предустановленные маппинги для категории интим-товаров
        self.predefined_mappings = {
            'sexoptovik': {
                'Вибраторы': {'subject_id': 5994, 'subject_name': 'Вибраторы', 'confidence': 1.0},
                'Фаллоимитаторы': {'subject_id': 5995, 'subject_name': 'Фаллоимитаторы', 'confidence': 1.0},
                'Фаллосы': {'subject_id': 5995, 'subject_name': 'Фаллоимитаторы', 'confidence': 1.0},
                'Белье эротическое': {'subject_id': 3, 'subject_name': 'Белье', 'confidence': 0.8},
                'Белье': {'subject_id': 3, 'subject_name': 'Белье', 'confidence': 0.9},
                'Костюмы эротические': {'subject_id': 6007, 'subject_name': 'Эротические костюмы', 'confidence': 1.0},
                'Костюмы': {'subject_id': 6007, 'subject_name': 'Эротические костюмы', 'confidence': 0.8},
                'Игрушки для взрослых': {'subject_id': 5993, 'subject_name': 'Игрушки для взрослых', 'confidence': 0.9},
                'Массажеры': {'subject_id': 469, 'subject_name': 'Массажеры', 'confidence': 0.7},
                'Лубриканты': {'subject_id': 6003, 'subject_name': 'Лубриканты', 'confidence': 1.0},
                'Смазки': {'subject_id': 6003, 'subject_name': 'Лубриканты', 'confidence': 1.0},
                'Смазка': {'subject_id': 6003, 'subject_name': 'Лубриканты', 'confidence': 1.0},
                'Гели': {'subject_id': 6003, 'subject_name': 'Лубриканты', 'confidence': 0.9},
                'Наручники': {'subject_id': 5998, 'subject_name': 'Наручники и фиксаторы', 'confidence': 1.0},
                'Фиксаторы': {'subject_id': 5998, 'subject_name': 'Наручники и фиксаторы', 'confidence': 1.0},
                'Маски': {'subject_id': 6000, 'subject_name': 'Маски и повязки', 'confidence': 0.8},
                'Повязки': {'subject_id': 6000, 'subject_name': 'Маски и повязки', 'confidence': 0.9},
                'Кляпы': {'subject_id': 6000, 'subject_name': 'Маски и повязки', 'confidence': 0.9},
                'Стимуляторы': {'subject_id': 5996, 'subject_name': 'Стимуляторы', 'confidence': 0.9},
                'Анальные игрушки': {'subject_id': 5997, 'subject_name': 'Анальные игрушки', 'confidence': 1.0},
                'Анальные стимуляторы': {'subject_id': 5997, 'subject_name': 'Анальные игрушки', 'confidence': 1.0},
                'Анальные пробки': {'subject_id': 5997, 'subject_name': 'Анальные игрушки', 'confidence': 1.0},
                'Анальные': {'subject_id': 5997, 'subject_name': 'Анальные игрушки', 'confidence': 0.8},
                'Вакуумные помпы': {'subject_id': 5999, 'subject_name': 'Вакуумные помпы', 'confidence': 1.0},
                'Помпы': {'subject_id': 5999, 'subject_name': 'Вакуумные помпы', 'confidence': 0.9},
                'Кольца': {'subject_id': 6001, 'subject_name': 'Эрекционные кольца', 'confidence': 0.9},
                'Эрекционные кольца': {'subject_id': 6001, 'subject_name': 'Эрекционные кольца', 'confidence': 1.0},
                'Секс-наборы': {'subject_id': 6002, 'subject_name': 'Наборы', 'confidence': 1.0},
                'Наборы': {'subject_id': 6002, 'subject_name': 'Наборы', 'confidence': 0.9},
                'Мастурбаторы': {'subject_id': 6004, 'subject_name': 'Мастурбаторы', 'confidence': 1.0},
                'Секс-куклы': {'subject_id': 6005, 'subject_name': 'Секс-куклы', 'confidence': 1.0},
                'Вагинальные шарики': {'subject_id': 6006, 'subject_name': 'Вагинальные шарики', 'confidence': 1.0},
                'Шарики': {'subject_id': 6006, 'subject_name': 'Вагинальные шарики', 'confidence': 0.7},
                'БДСМ': {'subject_id': 6008, 'subject_name': 'БДСМ аксессуары', 'confidence': 0.9},
                'Плетки': {'subject_id': 6008, 'subject_name': 'БДСМ аксессуары', 'confidence': 1.0},
                'Стеки': {'subject_id': 6008, 'subject_name': 'БДСМ аксессуары', 'confidence': 1.0},
                'Ошейники': {'subject_id': 6008, 'subject_name': 'БДСМ аксессуары', 'confidence': 1.0},
            }
        }

        # Ключевые слова для определения категорий по названию товара
        self.keywords_mapping = {
            'вибратор': {'subject_id': 5994, 'subject_name': 'Вибраторы', 'confidence': 0.9},
            'фаллоимитатор': {'subject_id': 5995, 'subject_name': 'Фаллоимитаторы', 'confidence': 0.95},
            'фаллос': {'subject_id': 5995, 'subject_name': 'Фаллоимитаторы', 'confidence': 0.9},
            'анальн': {'subject_id': 5997, 'subject_name': 'Анальные игрушки', 'confidence': 0.85},
            'пробк': {'subject_id': 5997, 'subject_name': 'Анальные игрушки', 'confidence': 0.85},
            'мастурбатор': {'subject_id': 6004, 'subject_name': 'Мастурбаторы', 'confidence': 0.95},
            'смазк': {'subject_id': 6003, 'subject_name': 'Лубриканты', 'confidence': 0.9},
            'лубрикант': {'subject_id': 6003, 'subject_name': 'Лубриканты', 'confidence': 0.95},
            'гель': {'subject_id': 6003, 'subject_name': 'Лубриканты', 'confidence': 0.8},
            'набор': {'subject_id': 6002, 'subject_name': 'Наборы', 'confidence': 0.7},
            'костюм': {'subject_id': 6007, 'subject_name': 'Эротические костюмы', 'confidence': 0.8},
            'белье': {'subject_id': 3, 'subject_name': 'Белье', 'confidence': 0.8},
            'кольцо': {'subject_id': 6001, 'subject_name': 'Эрекционные кольца', 'confidence': 0.85},
            'помп': {'subject_id': 5999, 'subject_name': 'Вакуумные помпы', 'confidence': 0.9},
            'наручник': {'subject_id': 5998, 'subject_name': 'Наручники и фиксаторы', 'confidence': 0.9},
            'фиксатор': {'subject_id': 5998, 'subject_name': 'Наручники и фиксаторы', 'confidence': 0.9},
            'маска': {'subject_id': 6000, 'subject_name': 'Маски и повязки', 'confidence': 0.85},
            'кляп': {'subject_id': 6000, 'subject_name': 'Маски и повязки', 'confidence': 0.9},
            'плет': {'subject_id': 6008, 'subject_name': 'БДСМ аксессуары', 'confidence': 0.9},
            'стек': {'subject_id': 6008, 'subject_name': 'БДСМ аксессуары', 'confidence': 0.9},
            'ошейник': {'subject_id': 6008, 'subject_name': 'БДСМ аксессуары', 'confidence': 0.9},
            'шарик': {'subject_id': 6006, 'subject_name': 'Вагинальные шарики', 'confidence': 0.75},
            'бдсм': {'subject_id': 6008, 'subject_name': 'БДСМ аксессуары', 'confidence': 0.9},
        }

    def map_category(self, source_category: str, source_type: str = 'sexoptovik',
                    general_category: str = '', all_categories: List[str] = None,
                    product_title: str = '') -> Tuple[Optional[int], Optional[str], float]:
        """
        Определяет категорию WB для товара

        Args:
            source_category: Основная категория из источника
            source_type: Тип источника
            general_category: Общая категория
            all_categories: Все категории товара
            product_title: Название товара (для анализа ключевых слов)

        Returns:
            Tuple[subject_id, subject_name, confidence]
        """
        if not source_category:
            return None, None, 0.0

        # Сначала проверяем БД
        mapping = CategoryMapping.query.filter_by(
            source_category=source_category,
            source_type=source_type
        ).order_by(CategoryMapping.priority.desc()).first()

        if mapping:
            return mapping.wb_subject_id, mapping.wb_subject_name, mapping.confidence_score

        # Проверяем предустановленные маппинги
        if source_type in self.predefined_mappings:
            category_mappings = self.predefined_mappings[source_type]

            # Точное совпадение
            if source_category in category_mappings:
                mapping_data = category_mappings[source_category]
                return mapping_data['subject_id'], mapping_data['subject_name'], mapping_data['confidence']

            # Частичное совпадение (нечеткий поиск)
            source_lower = source_category.lower()
            best_match = None
            best_confidence = 0.0

            for cat_key, cat_data in category_mappings.items():
                cat_lower = cat_key.lower()
                if source_lower in cat_lower or cat_lower in source_lower:
                    # Вычисляем уверенность на основе длины совпадения
                    overlap = len(set(source_lower.split()) & set(cat_lower.split()))
                    total = len(set(source_lower.split()) | set(cat_lower.split()))
                    confidence = (overlap / total) * cat_data['confidence'] if total > 0 else 0.0

                    if confidence > best_confidence:
                        best_confidence = confidence
                        best_match = (cat_data['subject_id'], cat_data['subject_name'], confidence)

            if best_match and best_confidence > 0.5:
                return best_match

        # Пытаемся определить по ключевым словам в названии товара
        if product_title:
            title_lower = product_title.lower()
            keyword_match = None
            keyword_confidence = 0.0

            for keyword, cat_data in self.keywords_mapping.items():
                if keyword in title_lower:
                    if cat_data['confidence'] > keyword_confidence:
                        keyword_confidence = cat_data['confidence']
                        keyword_match = (cat_data['subject_id'], cat_data['subject_name'], cat_data['confidence'])

            if keyword_match and keyword_confidence > 0.7:
                return keyword_match

        # Если не нашли - используем общую категорию или дефолт
        if general_category and general_category != source_category:
            return self.map_category(general_category, source_type, '', all_categories, product_title)

        # Дефолтная категория - "Товары для взрослых"
        logger.warning(f"Не удалось определить категорию для '{source_category}', используем дефолт")
        return 5993, 'Игрушки для взрослых', 0.3


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
            response = requests.get(url, timeout=30)
            response.raise_for_status()

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

            # Проверяем, не импортирован ли уже
            if self.settings.import_only_new:
                existing = ImportedProduct.query.filter_by(
                    seller_id=self.seller.id,
                    external_id=external_id,
                    source_type=self.settings.csv_source_type
                ).first()

                if existing and existing.import_status == 'imported':
                    logger.debug(f"Товар {external_id} уже импортирован, пропускаем")
                    return 'skipped'

            # Определяем категорию WB
            subject_id, subject_name, confidence = self.category_mapper.map_category(
                product_data['category'],
                self.settings.csv_source_type,
                product_data.get('general_category', ''),
                product_data.get('all_categories', []),
                product_data.get('title', '')
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

            if not imported_product:
                imported_product = ImportedProduct(
                    seller_id=self.seller.id,
                    external_id=external_id,
                    source_type=self.settings.csv_source_type
                )

            # Заполняем данные
            imported_product.external_vendor_code = product_data['external_vendor_code']
            imported_product.title = product_data['title']
            imported_product.category = product_data['category']
            imported_product.mapped_wb_category = subject_name
            imported_product.wb_subject_id = subject_id
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

            if is_valid:
                imported_product.import_status = 'validated'
                imported_product.validation_errors = None
            else:
                imported_product.import_status = 'failed'
                imported_product.validation_errors = json.dumps(errors, ensure_ascii=False)

            db.session.add(imported_product)
            db.session.commit()

            if is_valid:
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
            sizes_str = ', '.join(product_data['sizes'])
            parts.append(f"Размер: {sizes_str}")

        if product_data.get('features'):
            parts.append(f"\nОсобенности: {product_data['features']}")

        if product_data.get('bundle_items'):
            bundle_str = ', '.join(product_data['bundle_items'])
            parts.append(f"\nВ комплекте: {bundle_str}")

        if product_data.get('batteries'):
            parts.append(f"\nБатарейки: {product_data['batteries']}")

        return '\n'.join(parts)
