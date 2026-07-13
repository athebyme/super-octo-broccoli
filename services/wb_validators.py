"""
Валидация данных для WB API согласно swagger документации
"""
import hashlib
import json
import logging
import copy
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger('wb_validators')


class WBValidationError(Exception):
    """Ошибка валидации данных для WB API"""
    pass


# Внутренний контекст для batch safety boundary. Эти поля удаляются клиентом
# перед HTTP и никогда не являются частью WB wire-contract.
WB_SUBJECT_CONTEXT_KEY = '_wb_subjectID'
WB_CHARACTERISTICS_CHANGED_KEY = '_wb_characteristics_changed'
WB_PREPARED_CONTEXT_KEY = '_wb_prepared_context'
WB_SOURCE_CONTEXT_KEY = '_wb_fetched_source_context'


class _WBFetchedSourceContext:
    """Opaque proof that the API client fetched this base card from WB."""

    __slots__ = ('nm_id', 'subject_id', 'characteristics_hash', '_token')

    def __init__(self, nm_id, subject_id, characteristics_hash, token):
        self.nm_id = nm_id
        self.subject_id = subject_id
        self.characteristics_hash = characteristics_hash
        self._token = token


class _WBPreparedContext:
    """Opaque in-process context; JSON callers cannot forge this marker."""

    __slots__ = (
        'nm_id', 'subject_id', 'changed_ids', 'removed_ids',
        'characteristics_hash', '_token',
    )

    def __init__(
        self,
        nm_id,
        subject_id,
        changed_ids,
        removed_ids,
        characteristics_hash,
        token,
    ):
        self.nm_id = nm_id
        self.subject_id = subject_id
        self.changed_ids = tuple(changed_ids)
        self.removed_ids = tuple(removed_ids)
        self.characteristics_hash = characteristics_hash
        self._token = token


_WB_PREPARED_CONTEXT_TOKEN = object()
_WB_FETCHED_SOURCE_CONTEXT_TOKEN = object()


def _characteristics_fingerprint(characteristics):
    encoded = json.dumps(
        characteristics,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _strict_characteristic_ids(characteristics, source_label):
    """Return IDs without allowing normalization to hide malformed entries."""
    if not isinstance(characteristics, list):
        raise WBValidationError(
            f'{source_label}: characteristics должен быть массивом'
        )
    result = set()
    for index, item in enumerate(characteristics):
        if not isinstance(item, dict):
            raise WBValidationError(
                f'{source_label}: характеристика #{index + 1} должна быть объектом'
            )
        try:
            if isinstance(item.get('id'), bool):
                raise ValueError
            charc_id = int(item.get('id'))
        except (TypeError, ValueError):
            raise WBValidationError(
                f'{source_label}: характеристика #{index + 1} не содержит '
                'числовой id'
            )
        if charc_id in result:
            raise WBValidationError(
                f'{source_label}: характеристика id={charc_id} продублирована'
            )
        if 'value' not in item:
            raise WBValidationError(
                f'{source_label}: характеристика id={charc_id} не содержит value'
            )
        result.add(charc_id)
    return result


def _make_wb_prepared_context(
    nm_id,
    subject_id,
    changed_ids,
    characteristics,
    removed_ids=(),
):
    return _WBPreparedContext(
        nm_id,
        subject_id,
        changed_ids,
        removed_ids,
        _characteristics_fingerprint(characteristics),
        _WB_PREPARED_CONTEXT_TOKEN,
    )


def _mark_wb_card_as_fetched(card):
    """Attach an in-process source receipt to a card returned by WB client."""
    if not isinstance(card, dict):
        raise WBValidationError('Свежая WB-карточка должна быть объектом')
    if not isinstance(card.get('characteristics'), list):
        raise WBValidationError(
            'Свежая WB-карточка не содержит массив characteristics'
        )
    card[WB_SOURCE_CONTEXT_KEY] = _WBFetchedSourceContext(
        card.get('nmID'),
        card.get('subjectID'),
        _characteristics_fingerprint(card.get('characteristics')),
        _WB_FETCHED_SOURCE_CONTEXT_TOKEN,
    )
    return card


def _read_wb_fetched_source_context(value, card):
    if (
        not isinstance(value, _WBFetchedSourceContext)
        or value._token is not _WB_FETCHED_SOURCE_CONTEXT_TOKEN
    ):
        raise WBValidationError('Недостоверный источник WB-карточки')
    if str(value.nm_id) != str(card.get('nmID')):
        raise WBValidationError('Источник относится к другой WB-карточке')
    if str(value.subject_id) != str(card.get('subjectID')):
        raise WBValidationError('subjectID изменён после получения WB-карточки')
    if value.characteristics_hash != _characteristics_fingerprint(
        card.get('characteristics')
    ):
        raise WBValidationError(
            'Характеристики изменены до безопасной подготовки WB-карточки'
        )
    if value.subject_id is None:
        raise WBValidationError(
            'Свежая WB-карточка не содержит typed subjectID'
        )
    return value.subject_id


def _read_wb_prepared_context(value, characteristics, nm_id):
    if (
        not isinstance(value, _WBPreparedContext)
        or value._token is not _WB_PREPARED_CONTEXT_TOKEN
    ):
        raise WBValidationError('Недостоверный внутренний контекст WB-карточки')
    if str(value.nm_id) != str(nm_id):
        raise WBValidationError(
            'Внутренний контекст относится к другой WB-карточке'
        )
    if value.characteristics_hash != _characteristics_fingerprint(characteristics):
        raise WBValidationError(
            'Характеристики изменены после безопасной подготовки карточки'
        )
    return (
        value.subject_id,
        list(value.changed_ids),
        list(value.removed_ids),
    )


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
    if 'characteristics' in card_data:
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


def validate_create_cards_payload(cards: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """
    Валидация payload для POST /content/v2/cards/upload.

    Официальная структура: [{subjectID, variants: [{vendorCode, dimensions,
    characteristics, sizes, ...}]}]. dimensions должен быть объектом, а
    weightBrutto — только внутри dimensions и в килограммах.
    """
    errors: List[str] = []

    if not isinstance(cards, list) or not cards:
        return False, ["Payload создания карточек должен быть непустым массивом"]

    for card_idx, card in enumerate(cards):
        prefix = f"card[{card_idx}]"
        if not isinstance(card, dict):
            errors.append(f"{prefix}: должен быть объектом")
            continue

        if not card.get('subjectID'):
            errors.append(f"{prefix}.subjectID обязателен")

        variants = card.get('variants')
        if not isinstance(variants, list) or not variants:
            errors.append(f"{prefix}.variants должен быть непустым массивом")
            continue

        for variant_idx, variant in enumerate(variants):
            v_prefix = f"{prefix}.variants[{variant_idx}]"
            if not isinstance(variant, dict):
                errors.append(f"{v_prefix}: должен быть объектом")
                continue

            if not variant.get('vendorCode'):
                errors.append(f"{v_prefix}.vendorCode обязателен")
            if not variant.get('brand'):
                errors.append(f"{v_prefix}.brand обязателен")
            if not variant.get('title'):
                errors.append(f"{v_prefix}.title обязателен")
            elif len(str(variant.get('title'))) > 60:
                errors.append(f"{v_prefix}.title длиннее 60 символов")

            dims = variant.get('dimensions')
            if not isinstance(dims, dict):
                errors.append(f"{v_prefix}.dimensions должен быть объектом")
            else:
                for field in ('length', 'width', 'height'):
                    value = dims.get(field)
                    if not isinstance(value, (int, float)) or value <= 0:
                        errors.append(f"{v_prefix}.dimensions.{field} должен быть положительным числом")
                weight = dims.get('weightBrutto')
                if not isinstance(weight, (int, float)) or weight <= 0:
                    errors.append(f"{v_prefix}.dimensions.weightBrutto должен быть положительным числом в кг")

            chars = variant.get('characteristics', [])
            if chars and not isinstance(chars, list):
                errors.append(f"{v_prefix}.characteristics должен быть массивом")
            elif isinstance(chars, list):
                for char_idx, char in enumerate(chars):
                    if not isinstance(char, dict):
                        errors.append(f"{v_prefix}.characteristics[{char_idx}] должен быть объектом")
                        continue
                    if not char.get('id'):
                        errors.append(f"{v_prefix}.characteristics[{char_idx}].id обязателен")
                    if 'value' not in char:
                        errors.append(f"{v_prefix}.characteristics[{char_idx}].value обязателен")

            sizes = variant.get('sizes')
            if not isinstance(sizes, list) or not sizes:
                errors.append(f"{v_prefix}.sizes должен быть непустым массивом")
            else:
                for size_idx, size in enumerate(sizes):
                    if not isinstance(size, dict):
                        errors.append(f"{v_prefix}.sizes[{size_idx}] должен быть объектом")
                        continue
                    skus = size.get('skus')
                    if not isinstance(skus, list) or not any(skus):
                        errors.append(f"{v_prefix}.sizes[{size_idx}].skus должен содержать баркод")

    return len(errors) == 0, errors


def prepare_create_cards_for_wb(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Нормализовать create payload и удалить legacy packed-weight chars."""
    from services.wb_content_payload import normalize_create_cards_payload

    normalized = normalize_create_cards_payload(cards)
    is_valid, errors = validate_create_cards_payload(normalized)
    if not is_valid:
        raise WBValidationError('; '.join(errors))
    return normalized


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
    if not isinstance(full_card, dict) or not isinstance(updates, dict):
        raise WBValidationError('full_card и updates должны быть объектами')

    immutable_update_keys = {
        'nmID', 'subjectID',
        WB_SOURCE_CONTEXT_KEY, WB_PREPARED_CONTEXT_KEY,
        WB_SUBJECT_CONTEXT_KEY, WB_CHARACTERISTICS_CHANGED_KEY,
    }
    forbidden_updates = immutable_update_keys.intersection(updates)
    if forbidden_updates:
        raise WBValidationError(
            'Нельзя изменять identity/context поля WB-карточки: '
            + ', '.join(sorted(forbidden_updates))
        )

    legacy_context_keys = (
        WB_SUBJECT_CONTEXT_KEY,
        WB_CHARACTERISTICS_CHANGED_KEY,
    )
    if any(key in full_card for key in legacy_context_keys):
        raise WBValidationError(
            'Legacy WB context markers are reserved and cannot be supplied'
        )

    opaque_context = full_card.get(WB_PREPARED_CONTEXT_KEY)
    if opaque_context is not None:
        prepared_subject, characteristics_changed_ids, characteristics_removed_ids = (
            _read_wb_prepared_context(
                opaque_context,
                full_card.get('characteristics'),
                full_card.get('nmID'),
            )
        )
    else:
        source_context = full_card.get(WB_SOURCE_CONTEXT_KEY)
        if source_context is None:
            raise WBValidationError(
                'Для batch/full update требуется карточка, свежая из WB API'
            )
        prepared_subject = _read_wb_fetched_source_context(
            source_context, full_card)
        characteristics_changed_ids = []
        characteristics_removed_ids = []
    subject_context = full_card.get('subjectID') or prepared_subject
    if (
        full_card.get('subjectID') is not None
        and prepared_subject is not None
        and str(full_card.get('subjectID')) != str(prepared_subject)
    ):
        raise WBValidationError(
            'WB subjectID не совпадает с безопасно подготовленным контекстом'
        )
    source_characteristic_ids = _strict_characteristic_ids(
        full_card.get('characteristics'),
        'Свежая WB-карточка',
    )

    # Копируем полную карточку
    prepared = full_card.copy()
    prepared.pop(WB_PREPARED_CONTEXT_KEY, None)
    prepared.pop(WB_SOURCE_CONTEXT_KEY, None)

    # Legacy Product мог хранить {name: value}. Нельзя позволять wire-
    # normalizer молча превратить такой объект в [], поэтому сначала точно
    # маппим его по category schema/admin dictionaries. При отсутствии
    # контекста или невалидном значении обновление блокируется.
    if 'characteristics' in prepared and not isinstance(
        prepared['characteristics'], list
    ):
        legacy_characteristics = prepared['characteristics']
        if legacy_characteristics == {}:
            prepared['characteristics'] = []
        elif isinstance(legacy_characteristics, dict):
            if subject_context is None:
                raise WBValidationError(
                    'subjectID обязателен для преобразования legacy characteristics'
                )
            from services.marketplace_validator import build_wb_characteristic_patch
            prepared['characteristics'] = build_wb_characteristic_patch(
                subject_context,
                legacy_characteristics,
            )
        else:
            raise WBValidationError(
                "Поле полной карточки 'characteristics' должно быть массивом"
            )

    # Применяем обновления. characteristics — patch по charc_id: WB требует
    # отправить полный массив, поэтому нельзя стирать остальные значения.
    for key, value in updates.items():
        if key == 'dimensions' and isinstance(value, dict) and isinstance(prepared.get('dimensions'), dict):
            merged_dimensions = dict(prepared.get('dimensions') or {})
            merged_dimensions.update(value)
            prepared[key] = merged_dimensions
        elif key == 'characteristics':
            if value == {}:
                value = []
            if not isinstance(value, list):
                raise WBValidationError(
                    "Поле 'characteristics' должно быть массивом; "
                    'null и объект не могут заменить весь список'
                )
            from services.marketplace_validator import merge_wb_characteristics
            prepared[key] = merge_wb_characteristics(
                prepared.get('characteristics'), value)
            new_changed_ids = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                try:
                    new_changed_ids.append(int(item.get('id')))
                except (TypeError, ValueError):
                    continue
            characteristics_changed_ids = list(dict.fromkeys(
                characteristics_changed_ids + new_changed_ids
            ))
        else:
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
        'isSwatchTryOn',
        # ВАЖНО: brand НЕ удаляем — если поле отсутствует, WB обнуляет бренд на карточке.
        # Бренд всегда передаём как есть из карточки (или новый при update_brand).
    ]

    for field in fields_to_remove:
        prepared.pop(field, None)

    from services.wb_content_payload import normalize_update_card_payload

    prepared = normalize_update_card_payload(prepared)

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

    final_characteristic_ids = _strict_characteristic_ids(
        prepared.get('characteristics'),
        'Подготовленная WB-карточка',
    )
    expected_characteristic_ids = (
        source_characteristic_ids | set(characteristics_changed_ids)
    ) - set(characteristics_removed_ids)
    lost_characteristic_ids = (
        expected_characteristic_ids - final_characteristic_ids
    )
    if lost_characteristic_ids:
        # WB moved this legacy field to dimensions.weightBrutto. Its removal is
        # an explicit, signed migration rather than an invisible side effect.
        from services.wb_content_payload import PACKED_WEIGHT_CHARC_IDS
        deprecated_removed_ids = (
            lost_characteristic_ids & PACKED_WEIGHT_CHARC_IDS
        )
        unsafe_removed_ids = lost_characteristic_ids - deprecated_removed_ids
        if unsafe_removed_ids:
            raise WBValidationError(
                'Нормализация молча удалила характеристики WB: '
                + ', '.join(str(value) for value in sorted(unsafe_removed_ids))
            )
        characteristics_removed_ids = list(dict.fromkeys(
            characteristics_removed_ids + sorted(deprecated_removed_ids)
        ))
        characteristics_changed_ids = list(dict.fromkeys(
            characteristics_changed_ids + sorted(deprecated_removed_ids)
        ))

    # Контекст подписывает уже финальный wire-shape: cleanup выше может удалить
    # технические значения или превратить scalar в list.
    if subject_context is not None:
        prepared[WB_PREPARED_CONTEXT_KEY] = _make_wb_prepared_context(
            prepared.get('nmID'),
            subject_context,
            characteristics_changed_ids,
            prepared.get('characteristics'),
            characteristics_removed_ids,
        )

    return prepared


def prepare_card_for_characteristic_rollback(
    full_card: Dict[str, Any],
    restored_characteristics: List[Dict[str, Any]],
    removed_characteristic_ids: List[int],
) -> Dict[str, Any]:
    """Prepare a fresh WB card while explicitly restoring/removing charc IDs."""
    prepared = prepare_card_for_update(
        full_card,
        {'characteristics': restored_characteristics},
    )
    context = prepared.pop(WB_PREPARED_CONTEXT_KEY, None)
    if context is None:
        raise WBValidationError(
            'subjectID обязателен для безопасного отката характеристик'
        )
    subject_id, changed_ids, previous_removed_ids = _read_wb_prepared_context(
        context,
        prepared.get('characteristics'),
        prepared.get('nmID'),
    )

    try:
        removed_ids = {
            int(value)
            for value in list(previous_removed_ids) + list(removed_characteristic_ids)
        }
    except (TypeError, ValueError) as exc:
        raise WBValidationError(
            'Некорректный ID удаляемой характеристики'
        ) from exc

    restored_ids = {
        int(item.get('id'))
        for item in restored_characteristics
        if isinstance(item, dict) and item.get('id') is not None
    }
    removed_ids.difference_update(restored_ids)

    characteristics = prepared.get('characteristics')
    if not isinstance(characteristics, list):
        raise WBValidationError('Характеристики WB должны быть массивом')
    prepared['characteristics'] = [
        item for item in characteristics
        if not (
            isinstance(item, dict)
            and str(item.get('id', '')).isdigit()
            and int(item['id']) in removed_ids
        )
    ]
    all_changed_ids = list(dict.fromkeys(
        list(changed_ids) + sorted(removed_ids)
    ))
    prepared[WB_PREPARED_CONTEXT_KEY] = _make_wb_prepared_context(
        prepared.get('nmID'),
        subject_id,
        all_changed_ids,
        prepared.get('characteristics'),
        sorted(removed_ids),
    )
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
    numeric_count = 0

    from services.wb_content_payload import (
        coerce_numeric_characteristic_value,
        is_numeric_characteristic,
    )

    logger.info(f"🧹 Cleaning {len(characteristics)} characteristics for WB API update")

    for i, char in enumerate(characteristics):
        if not isinstance(char, dict):
            logger.debug(f"  Char #{i+1}: Skipping non-dict characteristic")
            continue

        # Оставляем только необходимые поля
        cleaned_char = {
            'id': char.get('id'),
            'value': char.get('value')
        }

        # Пропускаем характеристики без значения
        if cleaned_char['value'] is None or cleaned_char['value'] == '':
            logger.debug(f"  Char #{i+1} (id={cleaned_char['id']}): Skipping (empty value)")
            continue

        if is_numeric_characteristic(char):
            numeric_value = coerce_numeric_characteristic_value(cleaned_char['value'])
            if numeric_value is None:
                logger.warning(
                    f"  Char #{i+1} (id={cleaned_char['id']}): Numeric value "
                    f"{cleaned_char['value']!r} is not parseable, skipping"
                )
                continue
            cleaned_char['value'] = numeric_value
            numeric_count += 1
            logger.debug(
                f"  Char #{i+1} (id={cleaned_char['id']}): numeric characteristic -> {numeric_value}"
            )
            cleaned.append(cleaned_char)
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

    logger.info(
        f"✅ Cleaned {len(cleaned)} characteristics: {wrapped_count} wrapped in arrays, "
        f"{numeric_count} numeric, {len(characteristics) - len(cleaned)} skipped"
    )
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
    log_to_db: bool = True,
    fresh_cards_out: Optional[Dict[int, Dict[str, Any]]] = None,
) -> tuple:
    """
    Безопасная подготовка карточек для batch-обновления.

    Для каждого продукта получает целиком СВЕЖУЮ карточку WB. Это сохраняет
    актуальные характеристики и sizes с chrtID при full-replacement update.

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
    characteristic_validation_cache = {}

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

    # Получаем целиком свежие карточки WB. Локальная копия может отставать и
    # не должна повторно отправляться как full replacement при batch update.
    nm_ids = [p.nm_id for p in valid_products]
    fresh_cards_map = client.fetch_cards_by_nm_ids(
        nm_ids,
        log_to_db=log_to_db,
        seller_id=seller_id
    )

    for product in valid_products:
        try:
            full_card = fresh_cards_map.get(product.nm_id)
            if not full_card:
                skipped_errors.append(
                    f"Товар {product.vendor_code}: свежая карточка не найдена в WB"
                )
                continue
            if not full_card.get('sizes'):
                skipped_errors.append(
                    f"Товар {product.vendor_code}: в свежей карточке WB нет sizes"
                )
                continue

            if fresh_cards_out is not None:
                fresh_cards_out[product.nm_id] = copy.deepcopy(full_card)

            # Применяем обновления через пользовательскую функцию
            updates = updates_fn(product, full_card)
            if updates is None:
                continue

            if 'characteristics' in updates:
                from services.marketplace_validator import (
                    build_wb_characteristic_patch,
                )
                subject_id = (
                    full_card.get('subjectID')
                    or getattr(product, 'subject_id', None)
                )
                updates = dict(updates)
                updates['characteristics'] = build_wb_characteristic_patch(
                    subject_id,
                    updates['characteristics'],
                    validation_cache=characteristic_validation_cache,
                )

            card_ready = prepare_card_for_update(full_card, updates)
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
