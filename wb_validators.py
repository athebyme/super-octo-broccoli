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
    ]

    for field in fields_to_remove:
        prepared.pop(field, None)

    # Проверяем обязательные поля
    required_fields = ['nmID', 'vendorCode', 'sizes']
    for field in required_fields:
        if field not in prepared or prepared[field] is None:
            logger.error(f"Отсутствует обязательное поле: {field}")

    return prepared


def clean_characteristics_for_update(
    characteristics: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Очистка характеристик для отправки в WB API

    Args:
        characteristics: Список характеристик

    Returns:
        Очищенный список характеристик
    """
    cleaned = []

    logger.info(f"🧹 Cleaning {len(characteristics)} characteristics for update")

    for i, char in enumerate(characteristics):
        # Оставляем только необходимые поля
        cleaned_char = {
            'id': char.get('id'),
            'value': char.get('value')
        }

        logger.debug(f"  Char #{i+1}: id={cleaned_char['id']}, value={cleaned_char['value']} (type: {type(cleaned_char['value']).__name__})")

        # Пропускаем характеристики без значения
        if cleaned_char['value'] is None or cleaned_char['value'] == '':
            logger.debug(f"  Char #{i+1}: Skipping (empty value)")
            continue

        # Если value - строка, оборачиваем в массив для типа 1
        if isinstance(cleaned_char['value'], str):
            logger.debug(f"  Char #{i+1}: Wrapping string '{cleaned_char['value']}' in array")
            cleaned_char['value'] = [cleaned_char['value']]
        elif isinstance(cleaned_char['value'], list):
            logger.debug(f"  Char #{i+1}: Already a list with {len(cleaned_char['value'])} items")

        cleaned.append(cleaned_char)

    logger.info(f"✅ Cleaned {len(cleaned)} characteristics (skipped {len(characteristics) - len(cleaned)})")
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
