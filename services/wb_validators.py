"""
Валидация данных для WB API согласно swagger документации
"""
import logging
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger('wb_validators')


class WBValidationError(Exception):
    """Ошибка валидации данных для WB API"""
    pass


def validate_card_update(card_data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Валидация данных карточки товара перед отправкой в WB API

    Args:
        card_data: Данные карточки товара

    Returns:
        Tuple[bool, List[str]]: (валидна ли карточка, список ошибок)
    """
    errors = []

    # Обязательные поля
    if 'nmID' not in card_data or not card_data['nmID']:
        errors.append("Поле 'nmID' обязательно")

    if 'vendorCode' not in card_data or not card_data['vendorCode']:
        errors.append("Поле 'vendorCode' обязательно")

    if 'sizes' not in card_data or not isinstance(card_data['sizes'], list):
        errors.append("Поле 'sizes' обязательно и должно быть массивом")

    # Валидация title
    if 'title' in card_data and card_data['title']:
        title_len = len(card_data['title'])
        if title_len > 60:
            errors.append(f"Название товара слишком длинное ({title_len} символов, максимум 60)")

    # Валидация description
    if 'description' in card_data and card_data['description']:
        desc_len = len(card_data['description'])
        if desc_len < 1000:
            logger.warning(f"Описание слишком короткое ({desc_len} символов, минимум 1000)")
        if desc_len > 5000:
            errors.append(f"Описание слишком длинное ({desc_len} символов, максимум 5000)")

    # Валидация dimensions
    if 'dimensions' in card_data and card_data['dimensions']:
        dims = card_data['dimensions']
        if not isinstance(dims, dict):
            errors.append("Поле 'dimensions' должно быть объектом")
        else:
            # Проверяем что все размеры положительные
            for field in ['length', 'width', 'height']:
                if field in dims:
                    value = dims[field]
                    if not isinstance(value, (int, float)) or value <= 0:
                        errors.append(f"Габарит '{field}' должен быть положительным числом")

            # Проверяем вес
            if 'weightBrutto' in dims:
                weight = dims['weightBrutto']
                if not isinstance(weight, (int, float)) or weight <= 0:
                    errors.append("Вес 'weightBrutto' должен быть положительным числом")
                # Проверяем количество знаков после запятой
                weight_str = str(weight)
                if '.' in weight_str:
                    decimal_places = len(weight_str.split('.')[1])
                    if decimal_places > 3:
                        errors.append(f"Вес имеет слишком много знаков после запятой ({decimal_places}, максимум 3)")

    # Валидация characteristics
    if 'characteristics' in card_data and card_data['characteristics']:
        chars = card_data['characteristics']
        if not isinstance(chars, list):
            errors.append("Поле 'characteristics' должно быть массивом")
        else:
            for i, char in enumerate(chars):
                if not isinstance(char, dict):
                    errors.append(f"Характеристика #{i+1} должна быть объектом")
                    continue

                # Обязательные поля характеристики
                if 'id' not in char or not char['id']:
                    errors.append(f"Характеристика #{i+1}: отсутствует 'id'")

                if 'value' not in char:
                    errors.append(f"Характеристика #{i+1}: отсутствует 'value'")
                else:
                    # Проверяем формат value
                    value = char['value']
                    # WB API ожидает массив для большинства характеристик (тип 1),
                    # но числовое значение (int/float) допустимо для charcType=4
                    if isinstance(value, (int, float)):
                        pass  # OK для числовых характеристик (charcType=4)
                    elif not isinstance(value, list):
                        errors.append(
                            f"Характеристика #{i+1} (id={char.get('id')}): "
                            f"'value' должно быть массивом или числом, получено {type(value).__name__}. "
                            f"Используйте clean_characteristics_for_update() перед валидацией."
                        )
                    elif len(value) == 0:
                        logger.warning(f"Характеристика #{i+1} (id={char.get('id')}): пустой массив значений")
                    else:
                        # Проверяем что все элементы - строки или числа
                        for j, item in enumerate(value):
                            if not isinstance(item, (str, int, float)):
                                errors.append(
                                    f"Характеристика #{i+1} (id={char.get('id')}), "
                                    f"элемент #{j+1}: должен быть строкой или числом, "
                                    f"получено {type(item).__name__}"
                                )

    # Валидация sizes
    if 'sizes' in card_data and card_data['sizes']:
        sizes = card_data['sizes']
        if not isinstance(sizes, list):
            errors.append("Поле 'sizes' должно быть массивом")
        elif len(sizes) == 0:
            errors.append("Массив 'sizes' не должен быть пустым")
        else:
            for i, size in enumerate(sizes):
                if not isinstance(size, dict):
                    errors.append(f"Размер #{i+1} должен быть объектом")
                    continue

                # Для безразмерного товара должен быть хотя бы баркод
                if 'skus' not in size or not isinstance(size['skus'], list) or len(size['skus']) == 0:
                    errors.append(f"Размер #{i+1}: отсутствуют баркоды (skus)")

    return len(errors) == 0, errors


def validate_characteristics_value(
    value: Any,
    charc_type: int,
    max_count: int = 0
) -> Tuple[bool, Optional[str]]:
    """
    Валидация значения характеристики согласно её типу

    Args:
        value: Значение характеристики
        charc_type: Тип характеристики (1 - массив строк, 4 - число, 0 - не используется)
        max_count: Максимальное количество значений (0 - не ограничено)

    Returns:
        Tuple[bool, Optional[str]]: (валидно ли значение, сообщение об ошибке)
    """
    if charc_type == 0:
        return False, "Характеристика не используется (charcType=0)"

    elif charc_type == 1:
        # Массив строк
        if not isinstance(value, list):
            return False, "Значение должно быть массивом строк для характеристики типа 1"

        if len(value) == 0:
            return False, "Массив значений не должен быть пустым"

        # Проверяем max_count
        if max_count > 0 and len(value) > max_count:
            return False, f"Слишком много значений ({len(value)}, максимум {max_count})"

        # Проверяем что все элементы строки
        for i, item in enumerate(value):
            if not isinstance(item, str):
                return False, f"Элемент #{i+1} должен быть строкой"

        return True, None

    elif charc_type == 4:
        # Число
        if not isinstance(value, (int, float)):
            return False, "Значение должно быть числом для характеристики типа 4"

        return True, None

    else:
        return False, f"Неизвестный тип характеристики: {charc_type}"


def prepare_card_for_update(
    full_card: Dict[str, Any],
    updates: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Подготовка карточки для обновления в WB API

    Согласно документации WB API, при обновлении нужно отправлять
    ВСЕ поля карточки, включая те, которые не меняются.

    Args:
        full_card: Полная карточка товара из WB API
        updates: Поля которые нужно обновить

    Returns:
        Подготовленная карточка для отправки в API
    """
    # Копируем полную карточку
    prepared = full_card.copy()

    # Применяем обновления
    for key, value in updates.items():
        prepared[key] = value

    # Удаляем поля которые нельзя редактировать через update API
    fields_to_remove = [
        'photos',  # Фото редактируются отдельно
        'video',   # Видео редактируется отдельно
        'tags',    # Теги редактируются отдельно
        'mediaFiles',  # Медиа редактируется отдельно
        'createdAt',
        'updatedAt',
        'nmUUID',
        'imtID',
        'subjectID',
        'subjectName',
        'wholesale',
        'needKiz',
        # ВАЖНО: brand НЕ удаляем — если поле отсутствует, WB обнуляет бренд на карточке.
        # Бренд всегда передаём как есть из карточки (или новый при update_brand).
    ]

    for field in fields_to_remove:
        prepared.pop(field, None)

    # Проверяем обязательные поля
    required_fields = ['nmID', 'vendorCode', 'sizes']
    for field in required_fields:
        if field not in prepared or prepared[field] is None:
            logger.error(f"Отсутствует обязательное поле: {field}")

    # Предупреждение о nm_id=0 — такие карточки вызовут ошибку "Неуникальный баркод"
    nm_id = prepared.get('nmID', 0)
    if not nm_id or nm_id <= 0:
        logger.warning(
            f"⚠️ Карточка с nmID={nm_id} ({prepared.get('vendorCode', '?')}) — "
            f"не привязана к WB, обновление приведет к ошибке баркодов!"
        )

    # Предупреждение о sizes без chrtID — могут вызвать конфликт баркодов
    sizes = prepared.get('sizes', [])
    if sizes:
        sizes_without_chrt = [s for s in sizes if not s.get('chrtID')]
        if sizes_without_chrt:
            logger.warning(
                f"⚠️ Карточка nmID={nm_id}: {len(sizes_without_chrt)}/{len(sizes)} "
                f"размеров без chrtID — WB может создать дубли баркодов"
            )

    # Исправляем некорректные габариты
    if 'dimensions' in prepared and prepared['dimensions']:
        dims = prepared['dimensions']

        # Проверяем вес - если <= 0, удаляем или ставим дефолт
        if 'weightBrutto' in dims:
            try:
                weight = float(dims['weightBrutto'])
                if weight <= 0:
                    logger.warning(f"Invalid weight {weight}, removing from dimensions")
                    dims.pop('weightBrutto', None)
            except (ValueError, TypeError):
                logger.warning(f"Invalid weight value {dims.get('weightBrutto')}, removing")
                dims.pop('weightBrutto', None)

        # Если dimensions пустой после очистки - удаляем его
        if not dims or all(v is None or v == '' for v in dims.values()):
            prepared.pop('dimensions', None)
            logger.info("Removed empty dimensions")

    # КРИТИЧНО: Очищаем характеристики - оборачиваем строки в массивы
    if 'characteristics' in prepared and prepared['characteristics']:
        logger.info(f"🧹 Cleaning {len(prepared['characteristics'])} characteristics before API call")
        prepared['characteristics'] = clean_characteristics_for_update(prepared['characteristics'])

    return prepared


def clean_characteristics_for_update(
    characteristics: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Очистка характеристик для отправки в WB API

    КРИТИЧНО: WB API для характеристик типа 1 (большинство) ожидает массив строк,
    а не просто строку. Эта функция оборачивает строки в массивы.

    Примеры:
        "Россия" -> ["Россия"]
        "123" -> ["123"]
        ["Хлопок", "Эластан"] -> ["Хлопок", "Эластан"] (без изменений)

    Args:
        characteristics: Список характеристик

    Returns:
        Очищенный список характеристик
    """
    cleaned = []
    wrapped_count = 0

    logger.info(f"🧹 Cleaning {len(characteristics)} characteristics for WB API update")

    for i, char in enumerate(characteristics):
        # Оставляем только необходимые поля
        cleaned_char = {
            'id': char.get('id'),
            'value': char.get('value')
        }

        # Пропускаем характеристики без значения
        if cleaned_char['value'] is None or cleaned_char['value'] == '':
            logger.debug(f"  Char #{i+1} (id={cleaned_char['id']}): Skipping (empty value)")
            continue

        # КРИТИЧНО: Если value - строка, оборачиваем в массив
        # WB API ожидает массив для характеристик типа 1
        if isinstance(cleaned_char['value'], str):
            original_value = cleaned_char['value']
            cleaned_char['value'] = [cleaned_char['value']]
            wrapped_count += 1
            logger.debug(f"  Char #{i+1} (id={cleaned_char['id']}): '{original_value}' -> ['{original_value}']")
        elif isinstance(cleaned_char['value'], (int, float)):
            # Числовое значение (charcType=4) — оставляем как есть, WB ожидает число
            logger.debug(f"  Char #{i+1} (id={cleaned_char['id']}): numeric value {cleaned_char['value']} — keeping as number")
        elif isinstance(cleaned_char['value'], list):
            # Уже массив - проверяем что элементы строки
            for j, item in enumerate(cleaned_char['value']):
                if not isinstance(item, str):
                    cleaned_char['value'][j] = str(item)
            logger.debug(f"  Char #{i+1} (id={cleaned_char['id']}): Already a list with {len(cleaned_char['value'])} items")
        else:
            logger.warning(f"  Char #{i+1} (id={cleaned_char['id']}): Unknown type {type(cleaned_char['value']).__name__}, converting to string array")
            cleaned_char['value'] = [str(cleaned_char['value'])]
            wrapped_count += 1

        cleaned.append(cleaned_char)

    logger.info(f"✅ Cleaned {len(cleaned)} characteristics: {wrapped_count} wrapped in arrays, {len(characteristics) - len(cleaned)} skipped")
    return cleaned


def validate_and_log_errors(
    card_data: Dict[str, Any],
    operation: str = "update"
) -> bool:
    """
    Валидация данных и логирование ошибок

    Args:
        card_data: Данные карточки
        operation: Операция (update, create)

    Returns:
        True если валидация прошла успешно
    """
    is_valid, errors = validate_card_update(card_data)

    if not is_valid:
        logger.error(f"❌ Валидация карточки nmID={card_data.get('nmID')} не прошла:")
        for error in errors:
            logger.error(f"  - {error}")
        return False

    logger.info(f"✅ Валидация карточки nmID={card_data.get('nmID')} прошла успешно")
    return True


def prepare_batch_cards_safe(
    products,
    updates_fn,
    client,
    seller_id: int = None,
    log_to_db: bool = True
) -> tuple:
    """
    Безопасная подготовка карточек для batch-обновления.

    Для каждого продукта получает СВЕЖИЕ sizes из WB API (с chrtID),
    чтобы избежать ошибки "Неуникальный баркод" при обновлении.

    Карточки с nm_id=0 или без nm_id пропускаются.

    Args:
        products: Список объектов Product
        updates_fn: Функция (product, full_card) -> dict с обновлениями для карточки.
                    Должна вернуть dict с изменяемыми полями, или None если пропустить.
        client: WildberriesAPIClient
        seller_id: ID продавца для логирования
        log_to_db: Логировать запросы в БД

    Returns:
        (cards_to_update, product_map, skipped_errors)
        - cards_to_update: список подготовленных карточек
        - product_map: dict {nmID: product}
        - skipped_errors: список ошибок для пропущенных товаров
    """
    cards_to_update = []
    product_map = {}
    skipped_errors = []

    # Фильтруем продукты с валидным nm_id
    valid_products = []
    for product in products:
        if not product.nm_id or product.nm_id <= 0:
            skipped_errors.append(
                f"Товар {product.vendor_code}: пропущен (nm_id={product.nm_id}, "
                f"карточка не привязана к WB). Синхронизируйте товары."
            )
            logger.warning(f"⚠️ Skipping product {product.vendor_code}: nm_id={product.nm_id}")
            continue
        valid_products.append(product)

    if not valid_products:
        return cards_to_update, product_map, skipped_errors

    # Получаем свежие sizes из WB API для всех карточек
    nm_ids = [p.nm_id for p in valid_products]
    fresh_sizes_map = client.get_fresh_sizes_map(
        nm_ids,
        log_to_db=log_to_db,
        seller_id=seller_id
    )

    for product in valid_products:
        try:
            full_card = product.to_wb_card_format()
            if not full_card:
                skipped_errors.append(f"Товар {product.vendor_code}: нет данных в БД")
                continue

            # Подменяем sizes на свежие из WB API (с chrtID)
            fresh_sizes = fresh_sizes_map.get(product.nm_id)
            if fresh_sizes:
                full_card['sizes'] = fresh_sizes
            elif not full_card.get('sizes'):
                skipped_errors.append(
                    f"Товар {product.vendor_code}: нет sizes ни в БД, "
                    f"ни в WB API (требуется синхронизация)"
                )
                continue

            # Применяем обновления через пользовательскую функцию
            updates = updates_fn(product, full_card)
            if updates is None:
                continue

            for key, value in updates.items():
                full_card[key] = value

            card_ready = prepare_card_for_update(full_card, {})
            cards_to_update.append(card_ready)
            product_map[product.nm_id] = product

        except Exception as e:
            skipped_errors.append(f"Товар {product.vendor_code}: ошибка подготовки - {str(e)}")
            logger.error(f"Error preparing card {product.vendor_code}: {e}")

    logger.info(
        f"✅ Prepared {len(cards_to_update)} cards for batch update "
        f"({len(skipped_errors)} skipped)"
    )
    return cards_to_update, product_map, skipped_errors
