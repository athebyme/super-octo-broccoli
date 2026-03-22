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
    Product, Seller, PricingSettings
)
from services.pricing_engine import (
    SupplierPriceLoader, calculate_price, extract_supplier_product_id,
    DEFAULT_PRICE_RANGES,
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

    @staticmethod
    def _clean_size_text(size_str: str) -> str:
        """
        Очищает строку размера от текстовых описаний.
        'универсальный (42-46)' -> '42-46'
        'универсальный' -> ''
        '42-44' -> '42-44'
        'One Size' -> ''
        """
        if not size_str:
            return ''

        # Убираем кавычки
        cleaned = size_str.strip().strip('"\'«»')

        # Если есть числовой диапазон в скобках — извлекаем его
        # "универсальный (42-46)" -> "42-46"
        paren_match = re.search(r'\((\d[\d\s,\-]+\d)\)', cleaned)
        if paren_match:
            cleaned = paren_match.group(1).strip()

        # Убираем текстовые слова-описания размеров (не числовые)
        non_numeric_labels = [
            'универсальный', 'универсальная', 'универсальное', 'универсальн',
            'one size', 'one-size', 'onesize', 'os', 'uni', 'free size',
            'безразмерный', 'безразмерная', 'стандартный', 'стандарт',
        ]
        cleaned_lower = cleaned.lower().strip()
        for label in non_numeric_labels:
            if cleaned_lower == label:
                return ''
            # Убираем label из строки если он часть строки
            cleaned = re.sub(re.escape(label), '', cleaned, flags=re.IGNORECASE).strip()

        # Убираем скобки, оставшиеся после очистки
        cleaned = re.sub(r'[()[\]]', '', cleaned).strip()

        # Убираем кавычки ещё раз после очистки
        cleaned = cleaned.strip('"\'«»').strip()

        return cleaned

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

        # Нормализуем: убираем текстовые описания типа "универсальный", "one size" и т.д.
        # Оставляем только числовые размеры
        sizes_cleaned = self._clean_size_text(sizes_raw)
        sizes_lower = sizes_cleaned.lower()

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
            if re.search(r'\d{2}-\d{2}', sizes_cleaned):  # 42-44 или 46-48
                # Извлекаем только числовой диапазон (например, "42-46" из "универсальный (42-46)")
                range_match = re.search(r'(\d{2}-\d{2})', sizes_cleaned)
                if range_match:
                    range_str = range_match.group(1)
                    parts = range_str.split('-')
                    result['simple_sizes'] = [p.strip() for p in parts if p.strip()]
                else:
                    parts = sizes_cleaned.split('-')
                    result['simple_sizes'] = [p.strip() for p in parts if p.strip()]
            elif ',' in sizes_cleaned:
                result['simple_sizes'] = [s.strip() for s in sizes_cleaned.split(',') if s.strip()]
            elif sizes_cleaned.strip():
                result['simple_sizes'] = [sizes_cleaned.strip()]

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

        # Извлекаем числовой ID из external_id
        # Поддерживаемые форматы:
        #   "id-12345-код" → 12345
        #   "0T-00000877"  → 00000877 (sex-opt формат)
        #   "12345"        → 12345
        match = re.search(r'id-(\d+)', product_id)
        if match:
            numeric_id = match.group(1)
        else:
            # Пытаемся извлечь числовую часть после дефиса (sex-opt: "0T-00000877" → "00000877")
            match_sexopt = re.search(r'[A-Za-z]+-(\d+)', product_id)
            if match_sexopt:
                numeric_id = match_sexopt.group(1)
            else:
                numeric_id = product_id

        for num in photo_nums:
            # Формируем все варианты URL
            # ВАЖНО: sexoptovik первый - он используется по умолчанию
            # Новый формат с /admin/_project/ требует авторизации (используем SexoptovikAuth)
            photo_obj = {
                'sexoptovik': f"https://sexoptovik.ru/admin/_project/user_images/prods_res/{numeric_id}/{numeric_id}_{num}_1200.jpg",
                'blur': f"https://x-story.ru/mp/_project/img_sx0_1200/{numeric_id}_{num}_1200.jpg",
                'original': f"https://x-story.ru/mp/_project/img_sx_1200/{numeric_id}_{num}_1200.jpg"
            }
            photos.append(photo_obj)

        return photos


class CategoryMapper:
    """
    Маппер категорий из внешних источников в категории WB
    Использует точный маппинг из wb_categories_mapping.py
    Поддерживает AI для улучшения определения
    """

    def __init__(self, ai_service=None, ai_confidence_threshold: float = 0.7):
        # Импортируем точный маппинг категорий WB
        from services.wb_categories_mapping import get_best_category_match
        self.get_best_match = get_best_category_match
        self.ai_service = ai_service
        self.ai_confidence_threshold = ai_confidence_threshold

    def set_ai_service(self, ai_service, confidence_threshold: float = 0.7):
        """Устанавливает AI сервис для определения категорий"""
        self.ai_service = ai_service
        self.ai_confidence_threshold = confidence_threshold

    def map_category(self, source_category: str, source_type: str = 'sexoptovik',
                    general_category: str = '', all_categories: List[str] = None,
                    product_title: str = '', external_id: str = None,
                    brand: str = '', description: str = '',
                    use_ai: bool = True) -> Tuple[Optional[int], Optional[str], float]:
        """
        Определяет категорию WB для товара

        Args:
            source_category: Основная категория из источника
            source_type: Тип источника
            general_category: Общая категория
            all_categories: Все категории товара
            product_title: Название товара (для анализа ключевых слов)
            external_id: ID товара из внешнего источника (для ручных исправлений)
            brand: Бренд товара
            description: Описание товара
            use_ai: Использовать ли AI для определения

        Returns:
            Tuple[subject_id, subject_name, confidence]
        """
        if not source_category and not product_title:
            return None, None, 0.0

        # Сначала проверяем БД (пользовательские переопределения через CategoryMapping)
        mapping = CategoryMapping.query.filter_by(
            source_category=source_category,
            source_type=source_type
        ).order_by(CategoryMapping.priority.desc()).first()

        if mapping:
            return mapping.wb_subject_id, mapping.wb_subject_name, mapping.confidence_score

        # Используем обычный алгоритм маппинга
        subject_id, subject_name, confidence = self.get_best_match(
            csv_category=source_category,
            product_title=product_title,
            all_categories=all_categories,
            external_id=external_id,
            source_type=source_type
        )

        # Если уверенность низкая и AI доступен - пробуем AI
        if use_ai and self.ai_service and confidence < self.ai_confidence_threshold:
            logger.info(f"🤖 Низкая уверенность маппинга ({confidence:.2f}), пробуем AI...")

            ai_cat_id, ai_cat_name, ai_confidence, ai_reasoning = self.ai_service.detect_category(
                product_title=product_title,
                source_category=source_category,
                all_categories=all_categories,
                brand=brand,
                description=description
            )

            if ai_cat_id and ai_confidence > confidence:
                logger.info(f"🤖 AI определил категорию: {ai_cat_name} (ID: {ai_cat_id}) "
                           f"с уверенностью {ai_confidence:.2f}")
                logger.info(f"🤖 Причина: {ai_reasoning}")
                return ai_cat_id, ai_cat_name, ai_confidence

        return subject_id, subject_name, confidence


class SexoptovikAuth:
    """
    Авторизация на sexoptovik.ru для доступа к фотографиям
    """

    _session_cookies = {}  # Кеш cookies для каждого логина
    _sessions = {}  # Кеш сессий requests

    @classmethod
    def get_auth_cookies(cls, login: str, password: str, force_refresh: bool = False) -> Optional[dict]:
        """
        Авторизуется на sexoptovik.ru и возвращает cookies

        Args:
            login: Логин от sexoptovik.ru
            password: Пароль от sexoptovik.ru
            force_refresh: Принудительно обновить авторизацию

        Returns:
            dict с cookies или None если авторизация не удалась
        """
        # Проверяем кеш
        cache_key = f"{login}:{password}"
        if cache_key in cls._session_cookies and not force_refresh:
            logger.debug(f"Используем кешированные cookies для {login}")
            return cls._session_cookies[cache_key]

        try:
            logger.info(f"🔐 Начало авторизации на sexoptovik.ru для пользователя: {login}")

            # Создаем или переиспользуем сессию
            if cache_key not in cls._sessions:
                cls._sessions[cache_key] = requests.Session()
            session = cls._sessions[cache_key]

            # Полные заголовки браузера
            base_headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0'
            }

            # Сначала загружаем главную страницу для получения сессии
            logger.info(f"📄 Загрузка главной страницы для инициализации сессии...")
            main_response = session.get('https://sexoptovik.ru/', headers=base_headers, timeout=30)
            main_response.raise_for_status()
            logger.debug(f"🍪 Cookies после главной страницы: {len(session.cookies)} шт.")

            # Загружаем страницу логина
            login_page_url = 'https://sexoptovik.ru/login_page.php'
            base_headers['Referer'] = 'https://sexoptovik.ru/'
            base_headers['Sec-Fetch-Site'] = 'same-origin'

            logger.info(f"📄 Загрузка страницы логина...")
            get_response = session.get(login_page_url, headers=base_headers, timeout=30)
            get_response.raise_for_status()
            logger.info(f"✅ Страница логина загружена, статус: {get_response.status_code}")
            logger.debug(f"🍪 Cookies после GET: {len(session.cookies)} шт.")

            # Извлекаем скрытые поля формы (CSRF токен и т.д.)
            hidden_fields = {}
            try:
                from html.parser import HTMLParser

                class FormParser(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.hidden_inputs = {}

                    def handle_starttag(self, tag, attrs):
                        if tag == 'input':
                            attrs_dict = dict(attrs)
                            if attrs_dict.get('type') == 'hidden':
                                name = attrs_dict.get('name')
                                value = attrs_dict.get('value', '')
                                if name:
                                    self.hidden_inputs[name] = value

                parser = FormParser()
                parser.feed(get_response.text)
                hidden_fields = parser.hidden_inputs
                if hidden_fields:
                    logger.info(f"🔍 Найдены скрытые поля формы: {list(hidden_fields.keys())}")
            except Exception as e:
                logger.warning(f"⚠️  Не удалось распарсить скрытые поля: {e}")

            # POST запрос на авторизацию
            auth_data = {
                'client_login': login,
                'client_password': password,
                'submit': 'Войти',
                **hidden_fields  # Добавляем скрытые поля
            }

            post_headers = base_headers.copy()
            post_headers['Content-Type'] = 'application/x-www-form-urlencoded'
            post_headers['Referer'] = login_page_url
            post_headers['Origin'] = 'https://sexoptovik.ru'
            post_headers['Sec-Fetch-Site'] = 'same-origin'

            logger.info(f"📤 Отправка данных авторизации: login={login}")
            logger.info(f"POST данные: {list(auth_data.keys())}")
            response = session.post(login_page_url, data=auth_data, headers=post_headers, timeout=30, allow_redirects=True)
            logger.info(f"📥 Ответ получен, статус: {response.status_code}")
            logger.info(f"🔗 Final URL: {response.url}")
            logger.debug(f"🍪 Cookies после POST: {len(session.cookies)} шт.")

            response.raise_for_status()

            # Получаем cookies из сессии
            cookies_dict = session.cookies.get_dict()

            # Проверяем, что получили cookies авторизации
            if 'PHPSESSID' in cookies_dict:
                logger.info(f"✅ Успешная авторизация для {login}")
                logger.info(f"Полученные cookies: {list(cookies_dict.keys())}")

                # Проверяем, что это именно авторизованная сессия
                if 'admin_pretends_as' in cookies_dict:
                    logger.info(f"✅ Подтверждена авторизованная сессия (admin_pretends_as={cookies_dict['admin_pretends_as']})")

                # Дополнительная проверка - пробуем загрузить тестовую страницу админки
                test_result = cls._verify_auth(session, base_headers)
                if test_result:
                    cls._session_cookies[cache_key] = cookies_dict
                    return cookies_dict
                else:
                    logger.warning(f"⚠️  Cookies получены, но доступ к админке не подтвержден")
                    # Всё равно возвращаем cookies - возможно хватит для фото
                    cls._session_cookies[cache_key] = cookies_dict
                    return cookies_dict
            else:
                # Логируем содержимое ответа для отладки
                logger.error(f"❌ Авторизация не удалась для {login} - нет PHPSESSID")
                logger.error(f"Полученные cookies: {cookies_dict}")
                logger.error(f"Статус код: {response.status_code}")

                # Проверяем, есть ли на странице сообщение об ошибке
                response_lower = response.text.lower()
                if 'неверн' in response_lower or 'error' in response_lower or 'ошибка' in response_lower:
                    logger.error(f"⚠️  На странице обнаружено сообщение об ошибке авторизации")

                return None

        except Exception as e:
            import traceback
            logger.error(f"❌ Критическая ошибка авторизации на sexoptovik.ru: {e}")
            logger.error(f"Traceback:\n{traceback.format_exc()}")
            return None

    @classmethod
    def _verify_auth(cls, session: requests.Session, headers: dict) -> bool:
        """Проверяет, что авторизация действительно работает"""
        try:
            # Пробуем зайти на страницу админки
            verify_url = 'https://sexoptovik.ru/admin/'
            response = session.get(verify_url, headers=headers, timeout=10, allow_redirects=False)

            # Если редирект на login - авторизация не работает
            if response.status_code in [301, 302, 303, 307, 308]:
                location = response.headers.get('Location', '')
                if 'login' in location.lower():
                    logger.warning(f"⚠️  Редирект на страницу логина: {location}")
                    return False

            # Код 200 - авторизация работает
            if response.status_code == 200:
                return True

            return False
        except Exception as e:
            logger.warning(f"⚠️  Ошибка проверки авторизации: {e}")
            return False

    @classmethod
    def clear_cache(cls, login: str = None):
        """Очистить кеш cookies и сессий"""
        if login:
            # Удаляем cookies и сессии для конкретного логина
            keys_to_delete = [key for key in cls._session_cookies.keys() if key.startswith(f"{login}:")]
            for key in keys_to_delete:
                if key in cls._session_cookies:
                    del cls._session_cookies[key]
                if key in cls._sessions:
                    try:
                        cls._sessions[key].close()
                    except:
                        pass
                    del cls._sessions[key]
        else:
            # Очищаем весь кеш
            cls._session_cookies.clear()
            for session in cls._sessions.values():
                try:
                    session.close()
                except:
                    pass
            cls._sessions.clear()


class ImageProcessor:
    """
    Обработчик изображений товаров
    """

    # Сессия для переиспользования соединений
    _session = None

    @classmethod
    def _get_session(cls) -> requests.Session:
        """Возвращает или создает requests сессию"""
        if cls._session is None:
            cls._session = requests.Session()
        return cls._session

    @classmethod
    def reset_session(cls):
        """Сбрасывает сессию (при ошибках авторизации)"""
        if cls._session:
            cls._session.close()
        cls._session = None

    @staticmethod
    def download_and_process_image(url: str, target_size: Tuple[int, int] = (1200, 1200),
                                   background_color: str = 'white',
                                   auth_cookies: Optional[dict] = None,
                                   fallback_urls: Optional[List[str]] = None,
                                   retry_count: int = 1) -> Optional[BytesIO]:
        """
        Скачивает и обрабатывает изображение (быстро, без долгих retry)

        Args:
            url: URL изображения
            target_size: Целевой размер (ширина, высота)
            background_color: Цвет фона для дорисовки
            auth_cookies: Cookies для авторизации (для sexoptovik)
            fallback_urls: Альтернативные URL если основной не работает
            retry_count: Количество попыток для каждого URL (по умолчанию 1)

        Returns:
            BytesIO с обработанным изображением или None
        """
        # Собираем все URL для попыток (основной + fallbacks)
        urls_to_try = [url]
        if fallback_urls:
            urls_to_try.extend(fallback_urls)

        for current_url in urls_to_try:
            try:
                result = ImageProcessor._download_single_image(
                    current_url, target_size, background_color, auth_cookies
                )
                if result:
                    return result
            except Exception as e:
                # Логируем кратко, без спама
                logger.debug(f"Фото недоступно: {current_url[:60]}...")

                # При ошибке авторизации - сбрасываем кеш cookies и пробуем следующий URL
                if 'Content-Type=text/html' in str(e) or '401' in str(e) or '403' in str(e):
                    SexoptovikAuth.clear_cache()
                    ImageProcessor.reset_session()

        # Не спамим error логами - просто возвращаем None
        return None

    @staticmethod
    def _download_single_image(url: str, target_size: Tuple[int, int],
                               background_color: str,
                               auth_cookies: Optional[dict]) -> Optional[BytesIO]:
        """
        Скачивает одно изображение (внутренний метод)
        """
        # Заголовки для обхода защиты от hotlinking
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        }

        # Добавляем Referer в зависимости от домена
        if 'sexoptovik.ru' in url:
            headers['Referer'] = 'https://sexoptovik.ru/admin/'
            headers['Sec-Fetch-Dest'] = 'image'
            headers['Sec-Fetch-Mode'] = 'no-cors'
            headers['Sec-Fetch-Site'] = 'same-origin'
        elif 'x-story.ru' in url:
            headers['Referer'] = 'https://x-story.ru/'

        session = ImageProcessor._get_session()

        # Если переданы cookies авторизации - используем их
        response = session.get(
            url,
            headers=headers,
            cookies=auth_cookies,
            timeout=10,  # Уменьшен с 30 до 10 секунд
            allow_redirects=True
        )
        response.raise_for_status()

        # Проверяем, что получили изображение, а не HTML/текст
        content_type = response.headers.get('Content-Type', '')

        # Проверяем на редирект на страницу логина
        if response.url != url and 'login' in response.url.lower():
            raise Exception(f"Редирект на страницу авторизации: {response.url}")

        if not content_type.startswith('image/'):
            # Проверяем содержимое - если это HTML с формой логина
            content_preview = response.content[:500].decode('utf-8', errors='ignore').lower()
            if '<html' in content_preview or '<form' in content_preview or 'login' in content_preview:
                raise Exception(f"URL {url} вернул HTML (возможно требуется авторизация): Content-Type={content_type}")
            # Иногда сервер возвращает неправильный Content-Type, но содержимое - картинка
            logger.warning(f"URL {url} имеет неожиданный Content-Type={content_type}, пробуем распарсить как изображение")

        # Проверяем минимальный размер (картинка должна быть больше 1KB)
        if len(response.content) < 1024:
            raise Exception(f"Слишком маленький ответ ({len(response.content)} bytes), вероятно это не изображение")

        img = Image.open(BytesIO(response.content))

        # Проверяем размер
        if img.size == target_size:
            # Уже нужный размер
            output = BytesIO()
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(output, format='JPEG', quality=95)
            output.seek(0)
            return output

        # Нужно изменить размер с сохранением пропорций
        img_resized = ImageProcessor._resize_with_padding(img, target_size, background_color)

        output = BytesIO()
        img_resized.save(output, format='JPEG', quality=95)
        output.seek(0)
        return output

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
        self.validator = ProductValidator()

        # Инициализируем AI сервис если включен
        self.ai_service = None
        if settings.ai_enabled and settings.ai_api_key:
            try:
                from services.ai_service import get_ai_service, AIConfig
                self.ai_service = get_ai_service(settings)
                if self.ai_service:
                    logger.info(f"🤖 AI сервис инициализирован: провайдер={settings.ai_provider}, модель={settings.ai_model}")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось инициализировать AI сервис: {e}")

        # Инициализируем маппер категорий с AI
        ai_threshold = settings.ai_category_confidence_threshold if hasattr(settings, 'ai_category_confidence_threshold') else 0.7
        self.category_mapper = CategoryMapper(
            ai_service=self.ai_service if settings.ai_use_for_categories else None,
            ai_confidence_threshold=ai_threshold
        )

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

            # Загружаем цены поставщика (если настроено)
            supplier_prices = self._load_supplier_prices()

            # Обрабатываем каждый товар
            imported_count = 0
            skipped_count = 0
            failed_count = 0

            for product_data in products:
                # Подставляем цену поставщика если есть
                self._attach_supplier_price(product_data, supplier_prices)
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

    def _load_supplier_prices(self) -> Dict[int, Dict]:
        """Загрузить цены поставщика из отдельного CSV если настроено."""
        pricing = PricingSettings.query.filter_by(seller_id=self.seller.id).first()
        if not pricing or not pricing.is_enabled or not pricing.supplier_price_url:
            return {}

        try:
            loader = SupplierPriceLoader(
                price_url=pricing.supplier_price_url,
                inf_url=pricing.supplier_price_inf_url,
            )
            prices = loader.load_prices()
            pricing.last_price_sync_at = datetime.utcnow()
            db.session.commit()
            logger.info(f"Загружено {len(prices)} цен поставщика")
            return prices
        except Exception as e:
            logger.warning(f"Не удалось загрузить цены поставщика: {e}")
            return {}

    def _attach_supplier_price(self, product_data: Dict, supplier_prices: Dict[str, Dict]):
        """Подставить цену поставщика и рассчитать розничную цену."""
        if not supplier_prices:
            return

        ext_id = product_data.get('external_id', '')
        if ext_id and ext_id in supplier_prices:
            product_data['supplier_price'] = supplier_prices[ext_id]['price']
            product_data['supplier_quantity'] = supplier_prices[ext_id].get('quantity', 0)
        else:
            product_data['supplier_price'] = None
            product_data['supplier_quantity'] = 0

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

            # Формируем артикул по шаблону из настроек
            from services.pricing_engine import generate_vendor_code
            from models import Supplier as _Supplier

            _supplier_obj = None
            if self.settings.csv_source_type:
                _supplier_obj = _Supplier.query.filter_by(code=self.settings.csv_source_type).first()

            generated_vendor_code = generate_vendor_code(
                pattern=self.settings.vendor_code_pattern,
                supplier_code=self.settings.supplier_code,
                external_id=external_id,
                external_vendor_code=product_data.get('external_vendor_code', ''),
                supplier=_supplier_obj,
            )

            # ПРОВЕРКА ДУБЛИКАТОВ: проверяем, есть ли уже товар с таким артикулом в WB
            existing_product = Product.query.filter_by(
                seller_id=self.seller.id,
                vendor_code=generated_vendor_code
            ).first()

            if existing_product:
                logger.info(f"⚠️  Товар {external_id} уже существует в WB (nm_id={existing_product.nm_id}), пропускаем")
                # Обновляем ImportedProduct с пометкой о дубликате
                imported_product = ImportedProduct.query.filter_by(
                    seller_id=self.seller.id,
                    external_id=external_id,
                    source_type=self.settings.csv_source_type
                ).first()

                if imported_product:
                    imported_product.import_status = 'imported'
                    imported_product.product_id = existing_product.id
                    imported_product.import_error = None
                    db.session.commit()
                else:
                    # Создаем запись с пометкой об уже импортированном товаре
                    imported_product = ImportedProduct(
                        seller_id=self.seller.id,
                        external_id=external_id,
                        source_type=self.settings.csv_source_type,
                        import_status='imported',
                        product_id=existing_product.id,
                        title=product_data.get('title', ''),
                        brand=product_data.get('brand', '')
                    )
                    db.session.add(imported_product)
                    db.session.commit()

                return 'skipped'

            # Генерируем описание заранее для использования в AI
            description = self._generate_description(product_data)

            # Используем AI для парсинга размеров если включено
            if self.ai_service and self.settings.ai_use_for_sizes:
                sizes_raw = product_data.get('sizes', {}).get('raw', '')
                if sizes_raw:
                    success, ai_sizes, error = self.ai_service.parse_sizes(
                        sizes_text=sizes_raw,
                        product_title=product_data.get('title', ''),
                        description=description
                    )
                    if success and ai_sizes.get('characteristics'):
                        logger.info(f"🤖 AI распарсил размеры: {ai_sizes['characteristics']}")
                        # Добавляем AI-распарсенные характеристики к sizes
                        if isinstance(product_data['sizes'], dict):
                            product_data['sizes']['ai_characteristics'] = ai_sizes['characteristics']
                            product_data['sizes']['ai_confidence'] = ai_sizes.get('confidence', 0.5)

            # Определяем категорию WB (с учетом ручных исправлений и AI)
            subject_id, subject_name, confidence = self.category_mapper.map_category(
                product_data['category'],
                self.settings.csv_source_type,
                product_data.get('general_category', ''),
                product_data.get('all_categories', []),
                product_data.get('title', ''),
                external_id=product_data.get('external_id'),
                brand=product_data.get('brand', ''),
                description=description,
                use_ai=self.settings.ai_use_for_categories if self.ai_service else False
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
            # ВАЖНО: 'completed' тоже считается импортированным (привязка к существующей карточке WB)
            was_already_imported = False
            if imported_product:
                was_already_imported = (imported_product.import_status in ('imported', 'completed'))
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

            # Сохраняем цену поставщика, кол-во и рассчитываем розничные цены
            sp = product_data.get('supplier_price')
            sq = product_data.get('supplier_quantity')
            imported_product.supplier_quantity = sq if sq is not None else 0
            if sp and sp > 0:
                imported_product.supplier_price = sp
                pricing = PricingSettings.query.filter_by(
                    seller_id=self.seller.id
                ).first()
                if pricing and pricing.is_enabled:
                    supplier_pid = extract_supplier_product_id(external_id)
                    result = calculate_price(sp, pricing, product_id=supplier_pid or 0)
                    if result:
                        imported_product.calculated_price = result['final_price']
                        imported_product.calculated_discount_price = result['discount_price']
                        imported_product.calculated_price_before_discount = result['price_before_discount']

            # Сохраняем оригинальные данные поставщика (до AI модификаций)
            # Это позволит восстановить данные если AI что-то потеряет
            original_data = {
                'title': product_data.get('title', ''),
                'description': product_data.get('description', ''),
                'category': product_data.get('category', ''),
                'brand': product_data.get('brand', ''),
                'colors': product_data.get('colors', []),
                'sizes': product_data.get('sizes', {}),
                'materials': product_data.get('materials', []),
                'characteristics': product_data.get('characteristics', {}),
                'country': product_data.get('country', ''),
                'gender': product_data.get('gender', ''),
            }
            imported_product.original_data = json.dumps(original_data, ensure_ascii=False)

            # Используем уже сгенерированное описание
            imported_product.description = description

            # === Проверка дублей по баркоду ===
            # Если товар с таким же баркодом уже импортирован (под другим артикулом),
            # считаем текущий товар дублем и не даём повторно загрузить на WB
            if not was_already_imported and product_data.get('barcodes'):
                duplicate_nm_id = self._find_duplicate_by_barcode(
                    imported_product, product_data['barcodes']
                )
                if duplicate_nm_id:
                    was_already_imported = True
                    imported_product.wb_nm_id = duplicate_nm_id
                    imported_product.import_status = 'imported'
                    imported_product.imported_at = datetime.utcnow()
                    imported_product.import_error = (
                        f'Дубль: баркод совпадает с существующей карточкой nmID={duplicate_nm_id}'
                    )
                    logger.info(
                        f"Товар {external_id} — дубль по баркоду, "
                        f"привязан к nmID={duplicate_nm_id}"
                    )

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

    def _find_duplicate_by_barcode(self, imported_product, barcodes: list) -> int:
        """
        Ищет дубли по баркоду среди уже импортированных товаров и существующих Product записей.
        Предотвращает создание дубликатов на WB когда тот же товар загружается под другим артикулом.

        Returns: nmID существующей карточки WB если найден дубль, иначе 0
        """
        barcode_set = set(str(b) for b in barcodes if b)
        if not barcode_set:
            return 0

        product_id = getattr(imported_product, 'id', None)

        # 1. Проверяем ImportedProduct с imported/completed статусом
        try:
            query = ImportedProduct.query.filter(
                ImportedProduct.seller_id == self.seller.id,
                ImportedProduct.import_status.in_(('imported', 'completed')),
                ImportedProduct.wb_nm_id.isnot(None),
                ImportedProduct.barcodes.isnot(None),
            )
            if product_id:
                query = query.filter(ImportedProduct.id != product_id)

            for cp in query.all():
                try:
                    cp_barcodes = set(str(b) for b in json.loads(cp.barcodes) if b)
                    if barcode_set & cp_barcodes:
                        return cp.wb_nm_id
                except (json.JSONDecodeError, TypeError):
                    continue
        except Exception as e:
            logger.warning(f"Ошибка поиска дубля по баркоду (ImportedProduct): {e}")

        # 2. Проверяем Product записи (sizes_json содержит skus с баркодами)
        try:
            products = Product.query.filter(
                Product.seller_id == self.seller.id,
                Product.sizes_json.isnot(None),
            ).all()

            for p in products:
                try:
                    sizes = json.loads(p.sizes_json)
                    for size_entry in (sizes if isinstance(sizes, list) else []):
                        skus = set(str(s) for s in size_entry.get('skus', []) if s)
                        if barcode_set & skus:
                            return p.nm_id
                except (json.JSONDecodeError, TypeError):
                    continue
        except Exception as e:
            logger.warning(f"Ошибка поиска дубля по баркоду (Product): {e}")

        return 0

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
