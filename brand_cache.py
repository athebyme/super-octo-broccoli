"""
Brand Cache Service - кэширование брендов WB в памяти с fuzzy matching
"""
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple
from difflib import SequenceMatcher

logger = logging.getLogger('brand_cache')


class BrandCache:
    """
    Кэш брендов WB с поддержкой fuzzy matching.

    Загружает все бренды из WB API и хранит их в памяти для быстрого поиска.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.brands: Dict[int, str] = {}  # id -> name
        self.brands_lower: Dict[str, int] = {}  # name.lower() -> id
        self.last_sync: float = 0
        self.sync_interval: int = 3600  # Синхронизация раз в час
        self.is_syncing: bool = False
        self.sync_error: Optional[str] = None
        self._initialized = True

        logger.info("BrandCache initialized")

    def sync_brands(self, wb_client) -> bool:
        """
        Синхронизировать бренды из WB API.

        Загружает все бренды используя разные паттерны поиска.
        """
        if self.is_syncing:
            logger.info("Brand sync already in progress")
            return False

        self.is_syncing = True
        self.sync_error = None

        try:
            logger.info("Starting brand sync from WB API...")
            all_brands = {}

            # Паттерны для поиска - буквы алфавита + цифры
            patterns = list('ABCDEFGHIJKLMNOPQRSTUVWXYZ') + list('АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ') + list('0123456789')

            for pattern in patterns:
                try:
                    result = wb_client.search_brands(pattern, top=100)
                    brands = result.get('data', [])

                    for brand in brands:
                        brand_id = brand.get('id')
                        brand_name = brand.get('name', '')
                        if brand_id and brand_name:
                            all_brands[brand_id] = brand_name

                    # Небольшая пауза между запросами
                    time.sleep(0.1)

                except Exception as e:
                    logger.warning(f"Failed to fetch brands for pattern '{pattern}': {e}")
                    continue

            # Обновляем кэш
            self.brands = all_brands
            self.brands_lower = {name.lower(): id for id, name in all_brands.items()}
            self.last_sync = time.time()

            logger.info(f"Brand sync complete: {len(self.brands)} brands cached")
            return True

        except Exception as e:
            self.sync_error = str(e)
            logger.error(f"Brand sync failed: {e}")
            return False
        finally:
            self.is_syncing = False

    def sync_if_needed(self, wb_client) -> bool:
        """Синхронизировать если кэш устарел"""
        if time.time() - self.last_sync > self.sync_interval or not self.brands:
            return self.sync_brands(wb_client)
        return True

    def sync_async(self, api_key: str):
        """Запустить синхронизацию в фоновом потоке"""
        if self.is_syncing:
            return

        def sync_with_new_client():
            from wb_api_client import WildberriesAPIClient
            with WildberriesAPIClient(api_key) as client:
                self.sync_brands(client)

        thread = threading.Thread(target=sync_with_new_client, daemon=True)
        thread.start()

    def find_exact(self, brand_name: str) -> Optional[Dict]:
        """Найти точное совпадение бренда (регистронезависимо)"""
        brand_lower = brand_name.lower().strip()
        brand_id = self.brands_lower.get(brand_lower)

        if brand_id:
            return {'id': brand_id, 'name': self.brands[brand_id]}
        return None

    def find_fuzzy(self, brand_name: str, threshold: float = 0.7, limit: int = 10) -> List[Tuple[Dict, float]]:
        """
        Найти похожие бренды используя fuzzy matching.

        Args:
            brand_name: Название бренда для поиска
            threshold: Минимальный порог схожести (0-1)
            limit: Максимум результатов

        Returns:
            Список кортежей (brand_dict, similarity_score)
        """
        brand_lower = brand_name.lower().strip()
        results = []

        for brand_id, name in self.brands.items():
            name_lower = name.lower()

            # Быстрая проверка - если начинается с той же буквы или содержит подстроку
            if not (name_lower.startswith(brand_lower[0]) or brand_lower in name_lower or name_lower in brand_lower):
                # Проверяем только первые несколько символов для оптимизации
                if len(brand_lower) > 3 and not name_lower.startswith(brand_lower[:3]):
                    continue

            # Вычисляем схожесть
            similarity = SequenceMatcher(None, brand_lower, name_lower).ratio()

            # Бонус если одна строка содержит другую
            if brand_lower in name_lower or name_lower in brand_lower:
                similarity = min(1.0, similarity + 0.2)

            if similarity >= threshold:
                results.append(({'id': brand_id, 'name': name}, similarity))

        # Сортируем по схожести
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    def match_brand(self, brand_name: str) -> Dict:
        """
        Основной метод для матчинга бренда.

        Returns:
            {
                'status': 'exact' | 'confident' | 'uncertain' | 'not_found',
                'match': {...} or None,
                'confidence': float,
                'suggestions': [...]
            }
        """
        if not brand_name or not brand_name.strip():
            return {
                'status': 'not_found',
                'match': None,
                'confidence': 0,
                'suggestions': []
            }

        brand_name = brand_name.strip()

        # 1. Сначала ищем точное совпадение
        exact = self.find_exact(brand_name)
        if exact:
            return {
                'status': 'exact',
                'match': exact,
                'confidence': 1.0,
                'suggestions': []
            }

        # 2. Ищем fuzzy matches
        fuzzy_results = self.find_fuzzy(brand_name, threshold=0.5, limit=10)

        if not fuzzy_results:
            return {
                'status': 'not_found',
                'match': None,
                'confidence': 0,
                'suggestions': []
            }

        best_match, best_score = fuzzy_results[0]
        suggestions = [r[0] for r in fuzzy_results[1:6]]  # Остальные как предложения

        # 3. Определяем уровень уверенности
        if best_score >= 0.9:
            status = 'confident'
        elif best_score >= 0.7:
            status = 'uncertain'
        else:
            status = 'uncertain'
            suggestions = [r[0] for r in fuzzy_results[:6]]
            best_match = None

        return {
            'status': status,
            'match': best_match,
            'confidence': best_score,
            'suggestions': suggestions
        }

    def get_stats(self) -> Dict:
        """Получить статистику кэша"""
        return {
            'brands_count': len(self.brands),
            'last_sync': self.last_sync,
            'last_sync_ago': time.time() - self.last_sync if self.last_sync else None,
            'is_syncing': self.is_syncing,
            'sync_error': self.sync_error
        }


# Глобальный инстанс
brand_cache = BrandCache()


def get_brand_cache() -> BrandCache:
    """Получить глобальный инстанс кэша брендов"""
    return brand_cache
