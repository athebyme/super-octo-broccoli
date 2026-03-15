# -*- coding: utf-8 -*-
"""
Агент размеров — нормализация размеров и габаритов.

Задачи:
  - normalize_single:  нормализовать размеры одного товара
  - normalize_batch:   пакетная нормализация
  - fill_size_grid:    заполнить размерную сетку
"""
import json
import logging

from ..base_agent import BaseAgent

logger = logging.getLogger(__name__)


class SizeNormalizerAgent(BaseAgent):
    agent_name = 'size-normalizer'
    max_iterations = 15

    system_prompt = """Ты — эксперт по размерам и габаритам товаров для Wildberries.

Твои задачи:
- Парсить строки размеров из данных поставщика (например "42-44 RU", "L/XL", "27.5 см")
- Конвертировать единицы измерения (см↔мм, EU↔RU↔US, г↔кг)
- Нормализовать в формат WB
- Заполнять размерные сетки для одежды/обуви

ФОРМАТ РАЗМЕРОВ WB:
Размеры хранятся в поле sizes как JSON-объект:
{
  "simple_sizes": ["S", "M", "L", "XL"]
}

Для безразмерных товаров (духи, крема, игрушки и т.д.) — sizes должен быть:
{
  "simple_sizes": []
}

ГАБАРИТЫ (dimensions) сохраняются в характеристиках товара, НЕ в sizes:
- Длина, Ширина, Высота — в сантиметрах (charc_type=4, числовое значение)
- Вес — в граммах

КРИТИЧЕСКИЕ ПРАВИЛА:
1. ОБЯЗАТЕЛЬНО вызови get_category_characteristics(subject_id=...) — узнай какие характеристики размеров есть в категории
2. Если у категории нет характеристик размеров — товар безразмерный
3. Для одежды размеры это: "S", "M", "L", "XL" (международные) или "42", "44", "46" (RU)
4. Для обуви: "36", "37", "38" и т.д.
5. Для безразмерных товаров — запиши {"simple_sizes": []}
6. Габариты из описания (длина, ширина, диаметр) сохраняй в characteristics, НЕ в sizes
7. Для импортированных товаров ВСЕГДА используй update_imported_product
8. НЕ выдумывай размеры — извлекай из описания/характеристик товара

Таблица конвертации одежды (женская):
  XS=40-42, S=42-44, M=44-46, L=46-48, XL=48-50, XXL=50-52

Таблица конвертации обуви:
  EU 36=RU 35=23см, EU 37=RU 36=23.5см, EU 38=RU 37=24см

Результат: JSON с нормализованными размерами."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'normalize_single')
        seller_id = task.get('seller_id')

        if task_type == 'normalize_single':
            product_id = input_data.get('product_id')
            imported_product_id = input_data.get('imported_product_id')

            target_id = imported_product_id or product_id
            get_cmd = f"get_imported_product(product_id={target_id})" if imported_product_id else f"get_product(seller_id={seller_id}, product_id={product_id})"

            return (
                f"Нормализуй размеры товара для WB.\n"
                f"{'Imported Product' if imported_product_id else 'Product'} ID: {target_id}\n\n"
                f"Шаги:\n"
                f"1. {get_cmd} — получи данные товара\n"
                f"2. get_category_characteristics(subject_id=<wb_subject_id>) — узнай какие характеристики размеров поддерживает категория\n"
                f"3. Проанализируй описание товара на наличие размеров и габаритов\n"
                f"4. Определи:\n"
                f"   - Размеры (одежда/обувь): нормализуй в WB формат\n"
                f"   - Габариты (длина, ширина, высота, вес): выдели из описания\n"
                f"5. update_imported_product(product_id={target_id},\n"
                f'     sizes=\'{{"simple_sizes": ["S", "M"]}}\' или \'{{"simple_sizes": []}}\' для безразмерных,\n'
                f'     characteristics=\'{{"Длина": 15, "Ширина": 5}}\' — габариты как характеристики\n'
                f"   )\n\n"
                f"ВАЖНО:\n"
                f"- sizes = размерная сетка (S/M/L или 42/44/46)\n"
                f"- characteristics = габариты и физические параметры (длина, вес, диаметр)\n"
                f"- Названия characteristics должны ТОЧНО совпадать с get_category_characteristics\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product для сохранения.\n"
                f"Верни JSON: {{sizes: {{simple_sizes: [...]}}, characteristics: {{...}}, is_sized: bool}}"
            )

        elif task_type == 'fill_size_grid':
            return (
                f"Заполни размерную сетку для товаров.\n"
                f"Seller ID: {seller_id}\n"
                f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n\n"
                f"Построй полную размерную сетку с конвертацией RU/EU/US.\n"
                f"Верни JSON: {{grid: [{{ru_size, eu_size, us_size, measurements: {{}}}}]}}"
            )

        elif task_type == 'normalize_batch':
            product_ids = input_data.get('product_ids', [])

            if len(product_ids) == 1:
                return self.build_task_prompt({
                    **task,
                    'task_type': 'normalize_single',
                    'input_data': json.dumps({
                        'imported_product_id': product_ids[0],
                        'seller_id': seller_id,
                    }),
                })

            if product_ids:
                count = len(product_ids)
                products_brief = self._prefetch_products_brief(product_ids)
                if products_brief:
                    products_text = json.dumps(products_brief, ensure_ascii=False, indent=2)
                    return (
                        f"Нормализация размеров для {count} товаров.\n"
                        f"Данные товаров уже загружены:\n{products_text}\n\n"
                        f"ОПТИМИЗАЦИЯ: данные уже загружены выше. ЗАПРЕЩЕНО вызывать get_imported_product.\n\n"
                        f"Алгоритм:\n"
                        f"1. Сгруппируй товары по категории (wb_subject_id)\n"
                        f"2. Для каждой категории: get_category_characteristics(subject_id=...) — ОДИН раз\n"
                        f"3. Для каждого товара:\n"
                        f"   - Определи размеры из описания/названия\n"
                        f'   - update_imported_product(product_id=ID, sizes=\'{{"simple_sizes": [...]}}\', characteristics=...)\n\n'
                        f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                        f"Верни JSON: {{processed: число, results: [{{product_id, sizes, is_sized}}]}}"
                    )

                ids_str = ', '.join(str(i) for i in product_ids[:20])
                return (
                    f"Нормализация размеров для {count} выбранных товаров.\n"
                    f"Seller ID: {seller_id}\n"
                    f"Product IDs: {ids_str}\n\n"
                    f"ВАЖНО: Обрабатывай ТОЛЬКО перечисленные товары.\n\n"
                    f"1. Для каждого ID: get_imported_product(product_id=ID)\n"
                    f"2. get_category_characteristics — узнай формат размеров\n"
                    f"3. Нормализуй и update_imported_product\n\n"
                    f"Верни JSON: {{processed: число, results: [{{product_id, sizes, is_sized}}]}}"
                )

        return (
            f"Задача нормализации размеров.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Нормализуй размеры и верни результат в JSON."
        )

    def _prefetch_products_brief(self, product_ids: list) -> list:
        """Предзагрузка кратких данных товаров для встраивания в промпт."""
        try:
            return self.platform.get_imported_products_brief(product_ids)
        except Exception as e:
            logger.warning(f"Failed to prefetch products brief: {e}")
            return []
