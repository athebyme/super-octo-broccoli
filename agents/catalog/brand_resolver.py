# -*- coding: utf-8 -*-
"""
Агент брендов — распознавание, нормализация, валидация брендов.
"""
import json
import logging

from ..base_agent import BaseAgent

logger = logging.getLogger(__name__)


class BrandResolverAgent(BaseAgent):
    agent_name = 'brand-resolver'
    max_iterations = 12

    system_prompt = """Ты — эксперт по брендам на маркетплейсе Wildberries.

Твои задачи:
- Распознавать бренды из названий товаров поставщика
- Нормализовать написание (транслитерация, исправление ошибок)
- Проверять наличие бренда в реестре WB через validate_brand
- Подбирать корректное написание для карточки

КРИТИЧЕСКИЕ ПРАВИЛА:
- ЗАПРЕЩЕНО угадывать написание бренда! ОБЯЗАТЕЛЬНО используй validate_brand(brand_name=...)
  чтобы проверить бренд по реальному реестру WB
- ОБЯЗАТЕЛЬНО передавай category_id=<wb_subject_id> в validate_brand!
  Бренд может быть зарегистрирован в WB, но НЕДОСТУПЕН в конкретной категории товара.
  Без category_id проверка бессмысленна — WB отклонит карточку.
- validate_brand вернёт: точное совпадение, каноническое написание или похожие варианты
- Если validate_brand вернул category_available=false — бренд недоступен в этой категории
- Если бренд не найден — используй "Нет бренда"
- Для импортированных товаров ВСЕГДА используй update_imported_product (НЕ update_product)
- Не вызывай get_imported_products если ID товаров уже известны
- Не повторяй вызовы — каждый инструмент вызывай ровно 1 раз на товар
- Сразу после нормализации бренда — сохрани через update_imported_product

Алгоритм работы:
1. Получить товар (get_imported_product)
2. Извлечь бренд из названия/описания
3. validate_brand(brand_name=<бренд>, category_id=<wb_subject_id>) — ОБЯЗАТЕЛЬНО
4. Использовать каноническое написание из результата validate_brand
5. Сохранить (update_imported_product)

Результат: JSON с нормализованными брендами."""

    def execute_task(self, task: dict) -> dict:
        """Автоматически разбивает большие батчи на чанки.

        Перед запуском проверяет наличие category (wb_subject_id) у товаров —
        без категории валидация бренда бессмысленна на любом маркетплейсе.
        """
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'resolve_single')

        if task_type == 'resolve_single':
            product_ids = []
            pid = input_data.get('imported_product_id') or input_data.get('product_id')
            if pid:
                product_ids = [pid]
        elif task_type in ('resolve_batch',):
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )
        else:
            product_ids = []

        # Проверяем наличие категории у товаров перед запуском LLM
        if product_ids:
            product_ids, skipped = self._filter_products_without_category(product_ids)
            if not product_ids:
                return {
                    'error': 'no_category',
                    'message': (
                        'Ни у одного товара нет категории (wb_subject_id). '
                        'Сначала запустите category-mapper для определения категорий.'
                    ),
                    'skipped': skipped,
                }
            if skipped:
                logger.warning(
                    f"Brand resolver: {len(skipped)} products skipped (no category), "
                    f"{len(product_ids)} products will be processed"
                )
                # Обновляем input_data с отфильтрованными ID
                if 'product_ids' in input_data:
                    input_data['product_ids'] = product_ids
                elif 'imported_product_ids' in input_data:
                    input_data['imported_product_ids'] = product_ids
                task = {**task, 'input_data': json.dumps(input_data)}

        if task_type in ('resolve_batch',) and len(product_ids) > self.max_batch_size:
            return self._run_chunked_batch(task, product_ids)
        return self._execute_react(task)

    def _filter_products_without_category(self, product_ids: list) -> tuple:
        """Разделяет товары на имеющие и не имеющие категорию.

        Returns:
            (valid_ids, skipped_info): список ID с категорией и инфо о пропущенных
        """
        try:
            products = self.platform.get_imported_products_brief(product_ids)
        except Exception as e:
            logger.warning(f"Cannot prefetch products for category check: {e}")
            return product_ids, []  # не блокируем если API недоступен

        valid = []
        skipped = []
        for p in products:
            if p.get('wb_subject_id'):
                valid.append(p['id'])
            else:
                skipped.append({
                    'product_id': p['id'],
                    'title': p.get('title', '')[:80],
                    'reason': 'no_category (wb_subject_id is empty)',
                })

        # Товары, которые не нашлись в ответе — пропускаем тоже
        found_ids = {p['id'] for p in products}
        for pid in product_ids:
            if pid not in found_ids:
                skipped.append({
                    'product_id': pid,
                    'reason': 'product_not_found',
                })

        return valid, skipped

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)
        task_type = task.get('task_type', 'resolve_single')
        seller_id = task.get('seller_id')

        if task_type == 'resolve_single':
            product_id = input_data.get('product_id')
            imported_product_id = input_data.get('imported_product_id')

            if imported_product_id:
                return (
                    f"Определи и нормализуй бренд импортированного товара.\n"
                    f"Imported Product ID: {imported_product_id}\n\n"
                    f"Шаги:\n"
                    f"1. get_imported_product(product_id={imported_product_id})\n"
                    f"2. Извлеки бренд из названия/описания товара\n"
                    f"3. validate_brand(brand_name=<бренд>, category_id=<wb_subject_id из данных товара>) — "
                    f"ОБЯЗАТЕЛЬНО передай category_id! Без него WB отклонит карточку\n"
                    f"4. Используй каноническое написание из результата validate_brand\n"
                    f"5. update_imported_product(product_id={imported_product_id}, brand=<каноническое написание>)\n\n"
                    f"ЗАПРЕЩЕНО угадывать бренд — используй ТОЛЬКО результат validate_brand.\n"
                    f"ОБЯЗАТЕЛЬНО передавай category_id=wb_subject_id в validate_brand!\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для сохранения.\n"
                    f"Верни JSON: {{original_brand, normalized_brand, "
                    f"confidence, wb_registered: bool}}"
                )

            if product_id:
                return (
                    f"Определи и нормализуй бренд товара.\n"
                    f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                    f"1. Получи данные товара через get_product\n"
                    f"2. Определи бренд из названия/описания\n"
                    f"3. Нормализуй написание\n"
                    f"4. Обнови товар через update_product\n\n"
                    f"Верни JSON: {{original_brand, normalized_brand, "
                    f"confidence, wb_registered: bool}}"
                )

            return f"Ошибка: не указан product_id или imported_product_id."

        elif task_type == 'resolve_batch':
            product_ids = (
                input_data.get('product_ids')
                or input_data.get('imported_product_ids')
                or []
            )

            # 1 товар → делегируем в single
            if len(product_ids) == 1:
                return self.build_task_prompt({
                    **task,
                    'task_type': 'resolve_single',
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
                        f"Пакетная нормализация брендов для {count} товаров.\n"
                        f"Данные товаров уже загружены:\n{products_text}\n\n"
                        f"ОПТИМИЗАЦИЯ: данные уже загружены выше. ЗАПРЕЩЕНО вызывать get_imported_product.\n\n"
                        f"Для каждого товара:\n"
                        f"1. validate_brand(brand_name=<бренд из данных>, category_id=<wb_subject_id из данных товара>) — "
                        f"ОБЯЗАТЕЛЬНО передай category_id! Бренд может быть в WB, но недоступен в категории\n"
                        f"2. update_imported_product(product_id=ID, brand=<каноническое написание из validate_brand>)\n\n"
                        f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                        f"Верни JSON: {{total, updated, skipped, saved: число, "
                        f"results: [{{product_id, original, normalized}}]}}"
                    )

                ids_str = ', '.join(str(i) for i in product_ids[:20])
                return (
                    f"Пакетная нормализация брендов для {count} товаров.\n"
                    f"Product IDs: [{ids_str}]\n\n"
                    f"ЗАПРЕЩЕНО вызывать get_imported_products.\n\n"
                    f"Для каждого ID:\n"
                    f"1. get_imported_product(product_id=ID)\n"
                    f"2. validate_brand(brand_name=<бренд>, category_id=<wb_subject_id из данных товара>) — "
                    f"ОБЯЗАТЕЛЬНО с category_id!\n"
                    f"3. update_imported_product(product_id=ID, brand=...)\n\n"
                    f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                    f"Верни JSON: {{total, updated, skipped, saved: число, "
                    f"results: [{{product_id, original, normalized}}]}}"
                )

            limit = input_data.get('limit', 10)
            return (
                f"Пакетная нормализация брендов.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: обработай максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Для каждого товара нормализуй бренд\n"
                f"3. Для каждого: update_imported_product(product_id=ID, brand=...)\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n"
                f"ОБЯЗАТЕЛЬНО вызови update_imported_product для КАЖДОГО товара.\n\n"
                f"Верни JSON: {{total, updated, skipped, saved: число, "
                f"results: [{{product_id, original, normalized}}]}}"
            )

        elif task_type == 'audit_brands':
            limit = input_data.get('limit', 10)
            return (
                f"Аудит брендов.\n"
                f"Seller ID: {seller_id}\n"
                f"Лимит: проверь максимум {limit} товаров.\n\n"
                f"1. get_imported_products(seller_id={seller_id}, page=1, per_page={limit}) — ОДИН раз\n"
                f"2. Проверь бренды на корректность\n"
                f"3. Найди потенциальные проблемы\n\n"
                f"ЗАПРЕЩЕНО вызывать get_imported_products повторно.\n\n"
                f"Верни JSON: {{total, correct, issues: [{{product_id, brand, issue}}]}}"
            )

        return (
            f"Задача по брендам.\n"
            f"Seller ID: {seller_id}\n"
            f"Данные: {json.dumps(input_data, ensure_ascii=False)}\n"
            f"Нормализуй бренды, сохрани через update_imported_product и верни результат в JSON."
        )

    def _prefetch_products_brief(self, product_ids: list) -> list:
        """Предзагрузка кратких данных товаров для встраивания в промпт."""
        try:
            return self.platform.get_imported_products_brief(product_ids)
        except Exception as e:
            logger.warning(f"Failed to prefetch products brief: {e}")
            return []
