"""
Marketplace Service
Управляет синхронизацией справочников, категорий и характеристик маркетплейсов.
Также позволяет связывать товары и поставщиков с категориями.
"""

import logging
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, List, Any, Optional
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time

from sqlalchemy import case, func, or_

from models import (
    db, Marketplace, MarketplaceCategory, MarketplaceCategoryCharacteristic,
    MarketplaceDirectory, MarketplaceConnection, SupplierProduct
)
from services.wb_api_client import WildberriesAPIClient

logger = logging.getLogger('marketplace_service')


def _try_reference_sync_claim(scope: str, identifier: Any):
    lock_dir = os.path.join(tempfile.gettempdir(), 'seller-hub-reference-locks')
    os.makedirs(lock_dir, mode=0o700, exist_ok=True)
    lock_path = os.path.join(lock_dir, f'{scope}-{int(identifier)}.lock')
    lock_file = open(lock_path, 'a+', encoding='ascii')
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def _release_reference_sync_claim(lock_file) -> None:
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _reference_sync_guard(scope: str, identifier_name: str):
    """Serialize one reference refresh scope across local worker processes."""
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            identifier = kwargs.get(identifier_name)
            if identifier is None and len(args) > 1:
                identifier = args[1]
            try:
                lock_file = _try_reference_sync_claim(scope, identifier)
            except (OSError, TypeError, ValueError):
                logger.exception('Could not acquire %s reference sync claim', scope)
                return {
                    'success': False,
                    'error': 'Reference sync claim is unavailable',
                }
            if lock_file is None:
                return {
                    'success': False,
                    'skipped': True,
                    'error': 'Reference sync is already running',
                    'reason': 'Reference sync is already running',
                    'added': 0,
                    'updated': 0,
                    'selected': 0,
                    'synced': 0,
                    'failed': 0,
                    'errors': [],
                    'results': {},
                }
            try:
                return func(*args, **kwargs)
            finally:
                _release_reference_sync_claim(lock_file)
        return wrapped
    return decorator


class MarketplaceService:

    CATEGORY_PAGE_SIZE = 1000
    MAX_CATEGORY_PAGES = 100
    CATEGORY_SHRINK_GUARD_MIN = 100
    CATEGORY_SHRINK_GUARD_RATIO = 0.75
    CHARACTERISTIC_SHRINK_GUARD_MIN = 8
    CHARACTERISTIC_SHRINK_GUARD_RATIO = 0.5
    DIRECTORY_SHRINK_GUARD_MIN = 4
    DIRECTORY_SHRINK_GUARD_RATIO = 0.5
    REFERENCE_STALE_HOURS = 48
    SCHEMA_REFRESH_AFTER_HOURS = 30
    DEFAULT_STALE_SCHEMA_BATCH = 50
    MAX_STALE_SCHEMA_BATCH = 200
    CHARACTERISTIC_REQUEST_INTERVAL_SECONDS = 0.65
    MAX_CHARACTERISTIC_ALLOWLIST_VALUES = 2000
    MAX_CHARACTERISTIC_ALLOWLIST_VALUE_LENGTH = 300
    TNVED_CHARACTERISTIC_NAMES = frozenset({
        'тнвэд', 'тнвэд код', 'тн вэд', 'тн вэд код',
    })
    GLOBAL_DIRECTORY_CHARACTERISTIC_NAMES = frozenset({
        'цвет', 'цвет товара', 'пол', 'пол товара',
        'страна производства', 'сезон', 'ставка ндс',
    })

    @staticmethod
    def _stable_json(value: Any) -> str:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        )

    @classmethod
    def _payload_hash(cls, value: Any) -> str:
        return hashlib.sha256(cls._stable_json(value).encode('utf-8')).hexdigest()

    @staticmethod
    def _normalized_characteristic_name(value: Any) -> str:
        text = str(value or '').strip().casefold().replace('ё', 'е')
        text = re.sub(r'[^\w\s]+', ' ', text, flags=re.UNICODE)
        return re.sub(r'\s+', ' ', text).strip()

    @classmethod
    def _is_tnved_characteristic(cls, value: Any) -> bool:
        return cls._normalized_characteristic_name(value) in cls.TNVED_CHARACTERISTIC_NAMES

    @classmethod
    def _dictionary_hash(cls, dictionary_json: Optional[str]) -> Optional[str]:
        if not dictionary_json:
            return None
        try:
            return cls._payload_hash(json.loads(dictionary_json))
        except (json.JSONDecodeError, TypeError):
            return hashlib.sha256(dictionary_json.encode('utf-8')).hexdigest()

    @classmethod
    def _normalize_tnved_snapshot(cls, items: List[Any]) -> List[Dict[str, Any]]:
        """Validate the category-scoped TNVED response and keep KIZ metadata."""
        normalized = []
        seen = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f'tnved[{index}] must be an object')
            code = item.get('tnved')
            is_kiz = item.get('isKiz')
            if not isinstance(code, str) or not code.strip():
                raise ValueError(f'tnved[{index}].tnved must be a non-empty string')
            code = code.strip()
            if not re.fullmatch(r'\d{4,20}', code):
                raise ValueError(f'tnved[{index}].tnved has an invalid format')
            if not isinstance(is_kiz, bool):
                raise ValueError(f'tnved[{index}].isKiz must be a boolean')
            if code in seen:
                raise ValueError('tnved snapshot contains a duplicate code')
            seen.add(code)
            normalized.append({'value': code, 'isKiz': is_kiz})
        return sorted(normalized, key=lambda item: item['value'])

    @staticmethod
    def _error_text(exc: Exception) -> str:
        return str(exc)[:2000]

    @classmethod
    def _wb_data_list(cls, response: Any, reference_name: str) -> List[Any]:
        """Validate the common WB Content API response envelope."""
        if not isinstance(response, dict):
            raise ValueError(f'WB {reference_name} response is not an object')

        error_flag = response.get('error')
        error_text = response.get('errorText')
        additional_errors = response.get('additionalErrors')
        if (
            error_flag is not False
            or error_text not in (None, '')
            or additional_errors not in (None, '', [], {})
        ):
            raise ValueError(f'WB {reference_name} response reports an error')

        items = response.get('data')
        if not isinstance(items, list):
            raise ValueError(f'WB {reference_name} response has no typed data list')
        return items

    @staticmethod
    def _is_typed_integer(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @classmethod
    def _require_integer(
        cls,
        value: Any,
        field_name: str,
        *,
        minimum: int,
    ) -> int:
        """Accept only JSON integers; never truncate bool, float or strings."""
        if not cls._is_typed_integer(value) or value < minimum:
            qualifier = 'positive ' if minimum == 1 else 'non-negative '
            raise ValueError(
                f'WB {field_name} is not a {qualifier}integer'
            )
        return value

    @staticmethod
    def _require_boolean(value: Any, field_name: str) -> bool:
        if not isinstance(value, bool):
            raise ValueError(f'WB {field_name} is not a boolean')
        return value

    @classmethod
    def _normalize_directory_snapshot(
        cls,
        directory_type: str,
        items: List[Any],
    ) -> List[Any]:
        """Validate and normalize the documented shape of a WB directory."""
        normalized: List[Any] = []
        seen = set()
        seen_names = set()

        for index, item in enumerate(items):
            if directory_type == 'colors':
                if not isinstance(item, dict):
                    raise ValueError(f'colors[{index}] must be an object')
                name = item.get('name')
                parent_name = item.get('parentName')
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(f'colors[{index}].name must be a non-empty string')
                if parent_name is not None and not isinstance(parent_name, str):
                    raise ValueError(
                        f'colors[{index}].parentName must be a string or null'
                    )
                key = name.strip().casefold()
                normalized_item = dict(item)
                normalized_item['name'] = name.strip()
                normalized_item['parentName'] = (
                    parent_name.strip() if isinstance(parent_name, str) else ''
                )
            elif directory_type == 'countries':
                if not isinstance(item, dict):
                    raise ValueError(f'countries[{index}] must be an object')
                external_id = item.get('id')
                name = item.get('name')
                full_name = item.get('fullName')
                if not cls._is_typed_integer(external_id) or external_id <= 0:
                    raise ValueError(f'countries[{index}].id must be a positive integer')
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(
                        f'countries[{index}].name must be a non-empty string'
                    )
                if not isinstance(full_name, str) or not full_name.strip():
                    raise ValueError(
                        f'countries[{index}].fullName must be a non-empty string'
                    )
                key = external_id
                name_key = name.strip().casefold()
                if name_key in seen_names:
                    raise ValueError(
                        f'countries snapshot duplicated name {name.strip()!r}'
                    )
                seen_names.add(name_key)
                normalized_item = dict(item)
                normalized_item['name'] = name.strip()
                normalized_item['fullName'] = full_name.strip()
            elif directory_type in {'kinds', 'seasons', 'vat'}:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(
                        f'{directory_type}[{index}] must be a non-empty string'
                    )
                normalized_item = item.strip()
                key = normalized_item.casefold()
            else:
                raise ValueError(f'Unsupported directory type: {directory_type}')

            if key in seen:
                raise ValueError(
                    f'{directory_type} snapshot contains a duplicate item'
                )
            seen.add(key)
            normalized.append(normalized_item)

        return normalized

    @classmethod
    def characteristic_allowlist_values(
        cls, dictionary_json: Optional[str],
    ) -> List[str]:
        """Read canonical values from the supported category dictionary shape."""
        if not dictionary_json:
            return []
        try:
            raw = json.loads(dictionary_json)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(raw, list):
            return []

        values = []
        seen = set()
        for item in raw:
            if isinstance(item, dict):
                value = item.get('name') or item.get('value')
                if value is None and isinstance(item.get('id'), str):
                    value = item['id']
            else:
                value = item
            value = str(value).strip() if value is not None else ''
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                values.append(value)
        return values

    @classmethod
    def _normalize_characteristic_allowlist(cls, values: Any) -> List[str]:
        if not isinstance(values, list):
            raise ValueError('values должен быть массивом строк')
        if len(values) > cls.MAX_CHARACTERISTIC_ALLOWLIST_VALUES:
            raise ValueError(
                'Слишком много значений: максимум '
                f'{cls.MAX_CHARACTERISTIC_ALLOWLIST_VALUES}'
            )

        normalized = []
        seen = set()
        for index, value in enumerate(values):
            if not isinstance(value, str):
                raise ValueError(f'values[{index}] должен быть строкой')
            value = value.strip()
            if not value:
                continue
            if any(ord(character) < 32 for character in value):
                raise ValueError(
                    f'values[{index}] содержит недопустимые управляющие символы'
                )
            if len(value) > cls.MAX_CHARACTERISTIC_ALLOWLIST_VALUE_LENGTH:
                raise ValueError(
                    f'values[{index}] длиннее '
                    f'{cls.MAX_CHARACTERISTIC_ALLOWLIST_VALUE_LENGTH} символов'
                )
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(value)
        return normalized

    @classmethod
    def _category_characteristics_schema_hash(cls, category_id: int) -> str:
        """Hash the effective available schema stored for one WB category."""
        characteristics = MarketplaceCategoryCharacteristic.query.filter_by(
            category_id=category_id,
            is_available=True,
        ).order_by(
            MarketplaceCategoryCharacteristic.charc_id.asc(),
        ).all()
        payload = []
        for charc in characteristics:
            dictionary_json = charc.dictionary_json
            if dictionary_json:
                try:
                    dictionary_json = cls._stable_json(json.loads(dictionary_json))
                except (json.JSONDecodeError, TypeError):
                    pass
            payload.append({
                'charc_id': int(charc.charc_id),
                'name': charc.name,
                'charc_type': int(charc.charc_type or 0),
                'required': bool(charc.required),
                'unit_name': charc.unit_name,
                'max_count': int(charc.max_count or 0),
                'popular': bool(charc.popular),
                'has_filter': bool(charc.has_filter),
                'is_variable': bool(charc.is_variable),
                'dictionary_json': dictionary_json,
                'dictionary_source': getattr(charc, 'dictionary_source', None) or (
                    'admin' if dictionary_json else 'none'
                ),
            })
        return cls._payload_hash(payload)

    @staticmethod
    def get_wb_client(marketplace_id: int) -> Optional[WildberriesAPIClient]:
        marketplace = Marketplace.query.get(marketplace_id)
        if not marketplace or not marketplace.api_key:
            return None
        return WildberriesAPIClient(api_key=marketplace.api_key)

    # =========================================================================
    # CATEGORY SYNC
    # =========================================================================

    @classmethod
    @_reference_sync_guard('categories', 'marketplace_id')
    def sync_categories(
        cls,
        marketplace_id: int,
        *,
        client: Optional[WildberriesAPIClient] = None,
        now: Optional[datetime] = None,
        sleep_fn=time.sleep,
    ) -> Dict[str, Any]:
        """
        Полная синхронизация иерархии категорий (предметов) с WB API.
        """
        marketplace = Marketplace.query.get(marketplace_id)
        if not marketplace or marketplace.code != 'wb':
            return {"success": False, "error": "Invalid or unsupported marketplace"}

        client = client or cls.get_wb_client(marketplace_id)
        if not client:
            return {"success": False, "error": "API key not configured"}

        marketplace.categories_sync_status = 'running'
        marketplace.categories_sync_error = None
        db.session.commit()

        try:
            synced_at = now or datetime.utcnow()
            offset = 0
            limit = cls.CATEGORY_PAGE_SIZE
            snapshot: Dict[int, Dict[str, Any]] = {}
            previous_full_page_hash = None

            for page_number in range(cls.MAX_CATEGORY_PAGES):
                if page_number:
                    sleep_fn(cls.CHARACTERISTIC_REQUEST_INTERVAL_SECONDS)
                response = client.get_subjects_list(limit=limit, offset=offset)
                items = cls._wb_data_list(response, 'categories')
                if not items:
                    if not snapshot:
                        raise ValueError('WB returned an empty category snapshot')
                    break

                normalized_page = []
                for item in items:
                    if not isinstance(item, dict) or item.get('subjectID') is None:
                        raise ValueError('WB category snapshot contains an invalid item')
                    subject_id = cls._require_integer(
                        item['subjectID'], 'category subjectID', minimum=1,
                    )
                    subject_name = item.get('subjectName')
                    if not isinstance(subject_name, str) or not subject_name.strip():
                        raise ValueError('WB category snapshot contains an empty subjectName')
                    parent_id = item.get('parentID')
                    if parent_id is not None:
                        parent_id = cls._require_integer(
                            parent_id, 'category parentID', minimum=1,
                        )

                    availability_flags = {}
                    for flag_name in ('isEnabled', 'isVisible', 'disabled'):
                        if flag_name in item:
                            availability_flags[flag_name] = cls._require_boolean(
                                item[flag_name], f'category {flag_name}',
                            )

                    is_available = not (
                        availability_flags.get('isEnabled') is False
                        or availability_flags.get('isVisible') is False
                        or availability_flags.get('disabled') is True
                    )
                    normalized = {
                        'subject_id': subject_id,
                        'subject_name': subject_name.strip(),
                        'parent_id': parent_id,
                        'parent_name': item.get('parentName'),
                        'is_available': is_available,
                    }
                    if subject_id in snapshot:
                        raise ValueError(
                            f'WB category snapshot duplicated subjectID {subject_id}'
                        )
                    normalized_page.append(normalized)
                    snapshot[subject_id] = normalized

                if len(items) == limit:
                    page_hash = cls._payload_hash(normalized_page)
                    if page_hash == previous_full_page_hash:
                        raise ValueError('WB category pagination repeated a full page')
                    previous_full_page_hash = page_hash

                if len(items) < limit:
                    break
                offset += limit
            else:
                raise ValueError('WB category pagination exceeded the safety limit')

            # Mutate only after a complete, non-empty upstream traversal.
            existing = {
                int(category.subject_id): category
                for category in MarketplaceCategory.query.filter_by(
                    marketplace_id=marketplace.id,
                ).all()
            }
            previous_available = sum(
                1 for category in existing.values() if category.is_available
            )
            snapshot_available = sum(
                1 for item in snapshot.values() if item['is_available']
            )
            if (
                previous_available >= cls.CATEGORY_SHRINK_GUARD_MIN
                and snapshot_available
                < previous_available * cls.CATEGORY_SHRINK_GUARD_RATIO
            ):
                raise ValueError(
                    'WB category snapshot shrank anomalously '
                    f'({previous_available} -> {snapshot_available}); cache preserved'
                )
            total_added = 0
            total_updated = 0
            total_unavailable = 0

            for subject_id, item in snapshot.items():
                category = existing.get(subject_id)
                if category is None:
                    category = MarketplaceCategory(
                        marketplace_id=marketplace.id,
                        subject_id=subject_id,
                        subject_name=item['subject_name'],
                        parent_id=item['parent_id'],
                        parent_name=item['parent_name'],
                        is_enabled=False,
                        is_available=item['is_available'],
                        last_seen_at=synced_at,
                    )
                    db.session.add(category)
                    total_added += 1
                    continue

                changed = any((
                    category.subject_name != item['subject_name'],
                    category.parent_id != item['parent_id'],
                    category.parent_name != item['parent_name'],
                    bool(category.is_available) != item['is_available'],
                ))
                category.subject_name = item['subject_name']
                category.parent_id = item['parent_id']
                category.parent_name = item['parent_name']
                category.is_available = item['is_available']
                category.last_seen_at = synced_at
                if changed:
                    category.updated_at = synced_at
                    total_updated += 1

            for subject_id, category in existing.items():
                if subject_id in snapshot:
                    continue
                if category.is_available:
                    category.is_available = False
                    category.updated_at = synced_at
                    total_updated += 1
                total_unavailable += 1

            marketplace.categories_synced_at = synced_at
            marketplace.categories_sync_status = 'success'
            marketplace.categories_sync_error = None
            if total_added or total_updated:
                marketplace.categories_version = (marketplace.categories_version or 0) + 1
            marketplace.total_categories = sum(
                1 for item in snapshot.values() if item['is_available']
            )
            db.session.commit()

            return {
                "success": True,
                "added": total_added,
                "updated": total_updated,
                "unavailable": total_unavailable,
                "total": marketplace.total_categories,
                "version": marketplace.categories_version,
            }

        except Exception as e:
            logger.error(f"Error syncing categories: {e}")
            db.session.rollback()
            marketplace = Marketplace.query.get(marketplace_id)
            marketplace.categories_sync_status = 'failed'
            marketplace.categories_sync_error = cls._error_text(e)
            db.session.commit()
            return {"success": False, "error": str(e)}

    # =========================================================================
    # CHARACTERISTIC SYNC
    # =========================================================================

    @classmethod
    @_reference_sync_guard('characteristics-category', 'category_id')
    def sync_category_characteristics(
        cls,
        category_id: int,
        *,
        client: Optional[WildberriesAPIClient] = None,
        now: Optional[datetime] = None,
        sleep_fn=time.sleep,
    ) -> Dict[str, Any]:
        """Синхронизация характеристик для одной категории."""
        category = MarketplaceCategory.query.get(category_id)
        if not category:
            return {"success": False, "error": "Category not found"}

        if not category.is_available:
            return {"success": False, "error": "Category is unavailable upstream"}

        client = client or cls.get_wb_client(category.marketplace_id)
        if not client:
            return {"success": False, "error": "API key not configured"}

        category.characteristics_sync_status = 'running'
        category.characteristics_sync_error = None
        db.session.commit()

        try:
            synced_at = now or datetime.utcnow()
            response = client.get_card_characteristics_config(category.subject_id)
            items = cls._wb_data_list(response, 'characteristics')
            if not items:
                raise ValueError('WB returned an empty characteristics snapshot')

            normalized_items: Dict[int, Dict[str, Any]] = {}
            for item in items:
                if not isinstance(item, dict) or item.get('charcID') is None:
                    raise ValueError('WB characteristics snapshot contains an invalid item')
                charc_id = cls._require_integer(
                    item['charcID'], 'characteristic charcID', minimum=1,
                )
                if charc_id in normalized_items:
                    raise ValueError(
                        f'WB characteristics snapshot duplicated charcID {charc_id}'
                    )
                subject_id = cls._require_integer(
                    item.get('subjectID'),
                    'characteristic subjectID',
                    minimum=1,
                )
                if subject_id != category.subject_id:
                    raise ValueError(
                        'WB characteristic subjectID does not match the requested category'
                    )
                subject_name = item.get('subjectName')
                if not isinstance(subject_name, str) or not subject_name.strip():
                    raise ValueError('WB characteristic has no typed subjectName')
                name = item.get('name')
                if not isinstance(name, str) or not name.strip():
                    raise ValueError('WB characteristic has no name')
                charc_type = cls._require_integer(
                    item.get('charcType'),
                    'characteristic charcType',
                    minimum=0,
                )
                max_count = cls._require_integer(
                    item.get('maxCount'),
                    'characteristic maxCount',
                    minimum=0,
                )
                for boolean_field in ('required', 'popular'):
                    cls._require_boolean(
                        item.get(boolean_field),
                        f'characteristic {boolean_field}',
                    )
                for boolean_field in (
                    'hasFilter', 'isVariable', 'existNamedField',
                ):
                    if boolean_field not in item:
                        continue
                    cls._require_boolean(
                        item[boolean_field],
                        f'characteristic {boolean_field}',
                    )
                unit_name = item.get('unitName')
                if unit_name is not None and not isinstance(unit_name, str):
                    raise ValueError('WB characteristic unitName has an invalid type')
                dictionary = item.get('dictionary')
                if dictionary is not None and not isinstance(dictionary, (list, dict)):
                    raise ValueError('WB characteristic dictionary has an invalid type')
                if isinstance(dictionary, list):
                    dictionary = sorted(dictionary, key=cls._stable_json)
                dictionary_json = (
                    cls._stable_json(dictionary) if dictionary is not None else None
                )
                dictionary_values = cls.characteristic_allowlist_values(dictionary_json)
                if dictionary not in (None, [], {}) and not dictionary_values:
                    raise ValueError(
                        f'WB characteristic {charc_id} has an unusable dictionary'
                    )
                normalized_items[charc_id] = {
                    'charc_id': charc_id,
                    'name': name.strip(),
                    'charc_type': charc_type,
                    'required': item['required'],
                    'unit_name': unit_name.strip() if unit_name else None,
                    'max_count': max_count,
                    'popular': item['popular'],
                    'has_filter': item.get('hasFilter', False),
                    'is_variable': item.get('isVariable', False),
                    'dictionary_json': dictionary_json if dictionary_values else None,
                    'dictionary_source': 'wb_schema' if dictionary_values else 'none',
                    'dictionary_synced_at': synced_at if dictionary_values else None,
                    'dictionary_hash': cls._dictionary_hash(
                        dictionary_json if dictionary_values else None
                    ),
                }

            tnved_characteristics = [
                item for item in normalized_items.values()
                if cls._is_tnved_characteristic(item['name'])
            ]
            if tnved_characteristics:
                sleep_fn(cls.CHARACTERISTIC_REQUEST_INTERVAL_SECONDS)
                tnved_response = client.get_directory_tnved(category.subject_id)
                tnved_items = cls._wb_data_list(tnved_response, 'tnved directory')
                if not tnved_items:
                    raise ValueError(
                        f'WB returned an empty TNVED directory for subjectID={category.subject_id}'
                    )
                normalized_tnved = cls._normalize_tnved_snapshot(tnved_items)
                tnved_json = cls._stable_json(normalized_tnved)
                tnved_hash = cls._dictionary_hash(tnved_json)
                for item in tnved_characteristics:
                    item['dictionary_json'] = tnved_json
                    item['dictionary_source'] = 'wb_directory'
                    item['dictionary_synced_at'] = synced_at
                    item['dictionary_hash'] = tnved_hash

            existing = {
                int(charc.charc_id): charc
                for charc in MarketplaceCategoryCharacteristic.query.filter_by(
                    category_id=category.id,
                ).all()
            }
            previous_available = sum(
                1 for characteristic in existing.values()
                if characteristic.is_available
            )
            if (
                previous_available >= cls.CHARACTERISTIC_SHRINK_GUARD_MIN
                and len(normalized_items)
                < previous_available * cls.CHARACTERISTIC_SHRINK_GUARD_RATIO
            ):
                raise ValueError(
                    'WB characteristics snapshot shrank anomalously '
                    f'({previous_available} -> {len(normalized_items)}); '
                    'cache preserved'
                )

            total_added = 0
            total_updated = 0
            schema_changes = []

            schema_fields = (
                'name', 'charc_type', 'required', 'unit_name', 'max_count',
                'popular', 'has_filter', 'is_variable', 'dictionary_json',
                'dictionary_source', 'dictionary_hash',
            )
            for charc_id, item in normalized_items.items():
                charc = existing.get(charc_id)
                if charc:
                    # WB's characteristic schema commonly omits dictionary values
                    # or sends an explicit empty array. Preserve a non-empty
                    # admin allowlist until WB supplies a non-empty replacement.
                    existing_source = getattr(charc, 'dictionary_source', None) or (
                        'admin' if cls.characteristic_allowlist_values(charc.dictionary_json)
                        else 'none'
                    )
                    if (
                        existing_source == 'none'
                        and cls.characteristic_allowlist_values(charc.dictionary_json)
                    ):
                        existing_source = 'admin'
                    if (
                        not cls.characteristic_allowlist_values(
                            item['dictionary_json'])
                        and cls.characteristic_allowlist_values(charc.dictionary_json)
                        and cls._normalized_characteristic_name(item['name'])
                        not in cls.GLOBAL_DIRECTORY_CHARACTERISTIC_NAMES
                    ):
                        item = dict(item)
                        item['dictionary_json'] = charc.dictionary_json
                        item['dictionary_source'] = existing_source
                        item['dictionary_synced_at'] = getattr(
                            charc, 'dictionary_synced_at', None,
                        )
                        item['dictionary_hash'] = getattr(
                            charc, 'dictionary_hash', None,
                        ) or cls._dictionary_hash(charc.dictionary_json)
                    instruction_is_generated = cls._instruction_is_generated(charc)
                    dictionary_changed = any(
                        getattr(charc, field, None) != item[field]
                        for field in (
                            'dictionary_json', 'dictionary_source', 'dictionary_hash',
                        )
                    )
                    changed_fields = [
                        field for field in schema_fields
                        if getattr(charc, field) != item[field]
                    ]
                    was_unavailable = not bool(charc.is_available)
                    if changed_fields or was_unavailable:
                        schema_changes.append(
                            f"'{item['name']}': {', '.join(changed_fields) or 'available'}"
                        )
                        total_updated += 1
                    for field in schema_fields:
                        setattr(charc, field, item[field])
                    if item['dictionary_source'] in {'wb_schema', 'wb_directory'}:
                        charc.dictionary_synced_at = synced_at
                    elif item['dictionary_source'] == 'none':
                        charc.dictionary_synced_at = None
                    if dictionary_changed:
                        charc.dictionary_version = (
                            getattr(charc, 'dictionary_version', 0) or 0
                        ) + 1
                    # WB requirements take precedence over an old admin parsing
                    # preference. A characteristic that became required must be
                    # visible to every agent and publication validator again.
                    if item['required']:
                        charc.is_enabled = True
                    charc.is_available = True
                    charc.last_seen_at = synced_at
                    if changed_fields:
                        charc.updated_at = synced_at
                    if instruction_is_generated:
                        charc.ai_instruction = cls.generate_ai_instruction(
                            name=item['name'],
                            charc_type=item['charc_type'],
                            unit_name=item['unit_name'],
                            max_count=item['max_count'],
                            required=item['required'],
                            dictionary_json=item['dictionary_json'],
                        )
                        charc.ai_instruction_source = 'generated'
                    elif charc.ai_instruction_source == 'legacy':
                        charc.ai_instruction_source = 'custom'
                else:
                    ai_instruction = cls.generate_ai_instruction(
                        name=item['name'],
                        charc_type=item['charc_type'],
                        unit_name=item['unit_name'],
                        max_count=item['max_count'],
                        required=item['required'],
                        dictionary_json=item['dictionary_json'],
                    )
                    charc = MarketplaceCategoryCharacteristic(
                        category_id=category.id,
                        marketplace_id=category.marketplace_id,
                        charc_id=charc_id,
                        name=item['name'],
                        charc_type=item['charc_type'],
                        required=item['required'],
                        unit_name=item['unit_name'],
                        max_count=item['max_count'],
                        popular=item['popular'],
                        has_filter=item['has_filter'],
                        is_variable=item['is_variable'],
                        dictionary_json=item['dictionary_json'],
                        dictionary_source=item['dictionary_source'],
                        dictionary_synced_at=item['dictionary_synced_at'],
                        dictionary_hash=item['dictionary_hash'],
                        dictionary_version=1 if item['dictionary_json'] else 0,
                        ai_instruction=ai_instruction,
                        ai_instruction_source='generated',
                        is_available=True,
                        last_seen_at=synced_at,
                    )
                    db.session.add(charc)
                    total_added += 1

            for charc_id, charc in existing.items():
                if charc_id in normalized_items:
                    continue
                if charc.is_available:
                    charc.is_available = False
                    charc.updated_at = synced_at
                    total_updated += 1
                    schema_changes.append(f"'{charc.name}': unavailable")

            if schema_changes:
                logger.warning(
                    f"Schema changes detected for subject {category.subject_id}: "
                    + "; ".join(schema_changes)
                )

            db.session.flush()
            schema_hash = cls._category_characteristics_schema_hash(category.id)
            schema_changed = bool(
                total_added
                or total_updated
                or category.characteristics_schema_hash != schema_hash
            )
            category.characteristics_synced_at = synced_at
            category.characteristics_sync_status = 'success'
            category.characteristics_sync_error = None
            category.characteristics_schema_hash = schema_hash
            if schema_changed:
                category.characteristics_version = (
                    category.characteristics_version or 0
                ) + 1
            category.characteristics_count = len(normalized_items)
            category.required_count = sum(
                1 for item in normalized_items.values() if item['required']
            )

            # Update marketplace aggregates (unique charc_id only — same characteristic can exist in many categories)
            marketplace = category.marketplace
            marketplace.total_characteristics = db.session.query(
                func.count(func.distinct(MarketplaceCategoryCharacteristic.charc_id))
            ).join(
                MarketplaceCategory,
                MarketplaceCategory.id == MarketplaceCategoryCharacteristic.category_id,
            ).filter(
                MarketplaceCategoryCharacteristic.marketplace_id == marketplace.id,
                MarketplaceCategoryCharacteristic.is_available.is_(True),
                MarketplaceCategory.is_available.is_(True),
            ).scalar() or 0
            db.session.commit()

            return {
                "success": True,
                "added": total_added,
                "updated": total_updated,
                "total": category.characteristics_count,
                "schema_changes": schema_changes[:50],
                "schema_version": category.characteristics_version,
                "schema_hash": schema_hash,
            }

        except Exception as e:
            logger.error(f"Error syncing characteristics for category {category.id}: {e}")
            db.session.rollback()
            category = MarketplaceCategory.query.get(category_id)
            category.characteristics_sync_status = 'failed'
            category.characteristics_sync_error = cls._error_text(e)
            db.session.commit()
            return {"success": False, "error": str(e)}

    @classmethod
    def save_characteristic_allowlist(
        cls,
        characteristic_id: int,
        values: Any,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Validate and persist a category-scoped manual WB allowlist."""
        charc = MarketplaceCategoryCharacteristic.query.get(characteristic_id)
        if charc is None:
            raise LookupError('Характеристика не найдена')
        if charc.charc_type != 1:
            raise ValueError('Словарь допустим только для строковой характеристики')
        category = charc.category
        marketplace = category.marketplace if category else None
        if not marketplace or marketplace.code != 'wb':
            raise ValueError('Ручной словарь поддерживается только для WB')
        if not category.is_available or not charc.is_available:
            raise ValueError('Нельзя изменить словарь недоступной характеристики')
        normalized_name = cls._normalized_characteristic_name(charc.name)
        if (
            normalized_name in cls.GLOBAL_DIRECTORY_CHARACTERISTIC_NAMES
            or cls._is_tnved_characteristic(charc.name)
            or getattr(charc, 'dictionary_source', None) in {
                'wb_schema', 'wb_directory',
            }
        ):
            raise ValueError(
                'Официальный словарь WB обновляется автоматически и не может '
                'быть заменён ручным списком'
            )

        normalized = cls._normalize_characteristic_allowlist(values)
        dictionary_json = (
            cls._stable_json([{'value': value} for value in normalized])
            if normalized else None
        )
        dictionary_source = 'admin' if dictionary_json else 'none'
        dictionary_hash = cls._dictionary_hash(dictionary_json)
        changed = any((
            charc.dictionary_json != dictionary_json,
            (getattr(charc, 'dictionary_source', None) or 'none') != dictionary_source,
            getattr(charc, 'dictionary_hash', None) != dictionary_hash,
        ))
        if not changed:
            schema_hash = cls._category_characteristics_schema_hash(category.id)
            hash_changed = category.characteristics_schema_hash != schema_hash
            if hash_changed:
                changed_at = now or datetime.utcnow()
                category.characteristics_schema_hash = schema_hash
                category.characteristics_version = (
                    category.characteristics_version or 0
                ) + 1
                category.updated_at = changed_at
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    raise
            return {
                'success': True,
                'changed': False,
                'dictionary_values': normalized,
                'dictionary_source': dictionary_source,
                'dictionary_synced_at': (
                    charc.dictionary_synced_at.isoformat()
                    if getattr(charc, 'dictionary_synced_at', None) else None
                ),
                'dictionary_version': getattr(charc, 'dictionary_version', 0) or 0,
                'count': len(normalized),
                'schema_version': category.characteristics_version,
                'schema_hash': schema_hash,
            }

        instruction_is_generated = cls._instruction_is_generated(charc)
        changed_at = now or datetime.utcnow()
        charc.dictionary_json = dictionary_json
        charc.dictionary_source = dictionary_source
        charc.dictionary_hash = dictionary_hash
        charc.dictionary_synced_at = changed_at
        charc.dictionary_version = (
            getattr(charc, 'dictionary_version', 0) or 0
        ) + 1
        charc.updated_at = changed_at
        if instruction_is_generated:
            charc.ai_instruction = cls.generate_ai_instruction(
                name=charc.name,
                charc_type=charc.charc_type,
                unit_name=charc.unit_name,
                max_count=charc.max_count,
                required=charc.required,
                dictionary_json=dictionary_json,
            )
            charc.ai_instruction_source = 'generated'

        category.characteristics_version = (
            category.characteristics_version or 0
        ) + 1
        category.updated_at = changed_at
        try:
            db.session.flush()
            schema_hash = cls._category_characteristics_schema_hash(category.id)
            category.characteristics_schema_hash = schema_hash
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        return {
            'success': True,
            'changed': True,
            'dictionary_values': normalized,
            'dictionary_source': dictionary_source,
            'dictionary_synced_at': changed_at.isoformat(),
            'dictionary_version': charc.dictionary_version,
            'count': len(normalized),
            'schema_version': category.characteristics_version,
            'schema_hash': schema_hash,
        }

    @classmethod
    def _instruction_is_generated(
        cls, charc: MarketplaceCategoryCharacteristic,
    ) -> bool:
        source = charc.ai_instruction_source or 'legacy'
        if source == 'generated' or not charc.ai_instruction:
            return True
        if source == 'custom':
            return False

        generated = cls.generate_ai_instruction(
            name=charc.name,
            charc_type=charc.charc_type,
            unit_name=charc.unit_name,
            max_count=charc.max_count,
            required=charc.required,
            dictionary_json=charc.dictionary_json,
        )
        legacy_generated = generated.replace(
            'Если значение не найдено — не выдумывай его; оставь поле для ручной проверки.',
            'Если значение не найдено — попробуй оценить по типу товара.',
        )
        return charc.ai_instruction in (generated, legacy_generated)

    @classmethod
    @_reference_sync_guard('characteristics-sweep', 'marketplace_id')
    def sync_stale_characteristics(
        cls,
        marketplace_id: int,
        *,
        limit: int = DEFAULT_STALE_SCHEMA_BATCH,
        stale_after_hours: int = SCHEMA_REFRESH_AFTER_HOURS,
        client: Optional[WildberriesAPIClient] = None,
        now: Optional[datetime] = None,
        sleep_fn=time.sleep,
    ) -> Dict[str, Any]:
        """Refresh a bounded batch of enabled stale schemas, oldest first."""
        limit = max(1, min(int(limit), cls.MAX_STALE_SCHEMA_BATCH))
        current_time = now or datetime.utcnow()
        cutoff = current_time - timedelta(hours=max(1, stale_after_hours))
        client = client or cls.get_wb_client(marketplace_id)
        if not client:
            return {"success": False, "error": "API key not configured", "synced": 0}

        refresh_priority = case(
            (MarketplaceCategory.characteristics_sync_status == 'success', 0),
            (MarketplaceCategory.characteristics_sync_status.is_(None), 1),
            else_=2,
        )
        categories = MarketplaceCategory.query.filter(
            MarketplaceCategory.marketplace_id == marketplace_id,
            MarketplaceCategory.is_enabled.is_(True),
            MarketplaceCategory.is_available.is_(True),
            or_(
                MarketplaceCategory.characteristics_synced_at.is_(None),
                MarketplaceCategory.characteristics_synced_at < cutoff,
                MarketplaceCategory.characteristics_sync_status.is_(None),
                MarketplaceCategory.characteristics_sync_status != 'success',
            ),
        ).order_by(
            refresh_priority.asc(),
            MarketplaceCategory.characteristics_synced_at.asc(),
            MarketplaceCategory.updated_at.asc(),
            MarketplaceCategory.id.asc(),
        ).limit(limit).all()

        synced = 0
        skipped_categories = 0
        errors = []
        for index, category in enumerate(categories):
            if index:
                sleep_fn(cls.CHARACTERISTIC_REQUEST_INTERVAL_SECONDS)
            result = cls.sync_category_characteristics(
                category.id, client=client, now=current_time,
                sleep_fn=sleep_fn,
            )
            if result.get('skipped'):
                skipped_categories += 1
            elif result.get('success'):
                synced += 1
            else:
                errors.append({
                    'category_id': category.id,
                    'subject_id': category.subject_id,
                    'error': result.get('error', 'unknown error'),
                })

        return {
            'success': not errors,
            'selected': len(categories),
            'synced': synced,
            'skipped_categories': skipped_categories,
            'failed': len(errors),
            'errors': errors[:10],
            'limit': limit,
        }

    @classmethod
    def ensure_wb_references_current(
        cls,
        subject_ids: List[int],
        *,
        client: WildberriesAPIClient,
        now: Optional[datetime] = None,
        refresh_after_hours: int = SCHEMA_REFRESH_AFTER_HOURS,
        sleep_fn=time.sleep,
    ) -> Dict[str, Any]:
        """Refresh every reference needed by a typed import batch once.

        This is the shared preflight for AI preparation and WB writes. It
        prevents the importer from reading a fresh live schema while the final
        validator reads a different/stale admin cache.
        """
        if not isinstance(subject_ids, (list, tuple, set)):
            return {'success': False, 'error': 'subject_ids must be an array'}
        normalized_subjects = []
        seen = set()
        for raw in subject_ids:
            if not cls._is_typed_integer(raw) or raw <= 0:
                return {'success': False, 'error': 'subject_ids must contain positive integers'}
            if raw in seen:
                continue
            seen.add(raw)
            normalized_subjects.append(raw)
        if not normalized_subjects:
            return {'success': False, 'error': 'No WB subject IDs supplied'}
        if len(normalized_subjects) > cls.MAX_STALE_SCHEMA_BATCH:
            return {
                'success': False,
                'error': f'At most {cls.MAX_STALE_SCHEMA_BATCH} WB categories per preflight',
            }
        if client is None:
            return {'success': False, 'error': 'WB API client is not configured'}

        marketplace = Marketplace.query.filter_by(code='wb').first()
        if not marketplace:
            return {'success': False, 'error': 'Маркетплейс WB не настроен в админке'}
        current_time = now or datetime.utcnow()
        cutoff = current_time - timedelta(hours=max(1, int(refresh_after_hours)))
        refreshed = {'categories': False, 'directories': False, 'schemas': []}
        errors = []

        existing_subjects = {
            row[0] for row in db.session.query(MarketplaceCategory.subject_id).filter(
                MarketplaceCategory.marketplace_id == marketplace.id,
                MarketplaceCategory.subject_id.in_(normalized_subjects),
            ).all()
        }
        categories_stale = bool(
            marketplace.categories_sync_status != 'success'
            or not marketplace.categories_synced_at
            or marketplace.categories_synced_at < cutoff
            or existing_subjects != set(normalized_subjects)
        )
        if categories_stale:
            result = cls.sync_categories(
                marketplace.id, client=client, now=current_time, sleep_fn=sleep_fn,
            )
            if result.get('skipped'):
                return {
                    'success': False,
                    'error': 'Справочник категорий WB уже обновляется; повторите пакет позже',
                    'refreshed': refreshed,
                }
            if not result.get('success'):
                return {
                    'success': False,
                    'error': f'Не удалось обновить категории WB: {result.get("error")}',
                    'refreshed': refreshed,
                }
            refreshed['categories'] = bool(result.get('success'))
            db.session.expire_all()
            marketplace = Marketplace.query.get(marketplace.id)

        directories_stale = bool(
            marketplace.directories_sync_status != 'success'
            or not marketplace.directories_synced_at
            or marketplace.directories_synced_at < cutoff
        )
        if directories_stale:
            result = cls.sync_directories(
                marketplace.id, client=client, now=current_time, sleep_fn=sleep_fn,
            )
            if result.get('skipped'):
                return {
                    'success': False,
                    'error': 'Справочники WB уже обновляются; повторите пакет позже',
                    'refreshed': refreshed,
                }
            if not result.get('success'):
                return {
                    'success': False,
                    'error': f'Не удалось обновить справочники WB: {result.get("error")}',
                    'refreshed': refreshed,
                }
            refreshed['directories'] = bool(result.get('success'))
            db.session.expire_all()
            marketplace = Marketplace.query.get(marketplace.id)
            if marketplace.directories_sync_status != 'success':
                return {
                    'success': False,
                    'error': (
                        'Не все обязательные справочники WB обновились: '
                        f'{marketplace.directories_sync_error or "неизвестная ошибка"}'
                    ),
                    'refreshed': refreshed,
                }

        categories = MarketplaceCategory.query.filter(
            MarketplaceCategory.marketplace_id == marketplace.id,
            MarketplaceCategory.subject_id.in_(normalized_subjects),
        ).all()
        by_subject = {int(category.subject_id): category for category in categories}
        for index, subject_id in enumerate(normalized_subjects):
            category = by_subject.get(subject_id)
            if not category:
                errors.append(f'subjectID={subject_id}: категория отсутствует в актуальном WB-кэше')
                continue
            if not category.is_available:
                errors.append(f'subjectID={subject_id}: категория больше недоступна в WB')
                continue
            if not category.is_enabled:
                errors.append(f'subjectID={subject_id}: категория отключена в админке')
                continue
            schema_stale = bool(
                category.characteristics_sync_status != 'success'
                or not category.characteristics_synced_at
                or category.characteristics_synced_at < cutoff
            )
            if schema_stale:
                if index and refreshed['schemas']:
                    sleep_fn(cls.CHARACTERISTIC_REQUEST_INTERVAL_SECONDS)
                result = cls.sync_category_characteristics(
                    category.id, client=client, now=current_time, sleep_fn=sleep_fn,
                )
                if result.get('success'):
                    refreshed['schemas'].append(subject_id)
                elif result.get('skipped'):
                    errors.append(
                        f'subjectID={subject_id}: схема уже обновляется; повторите пакет позже'
                    )
                else:
                    errors.append(
                        f'subjectID={subject_id}: {result.get("error", "ошибка синхронизации")}'
                    )

        return {
            'success': not errors,
            'subjects': normalized_subjects,
            'refreshed': refreshed,
            'errors': errors[:20],
            'error': '; '.join(errors[:5]) if errors else None,
        }

    @classmethod
    def get_cached_characteristics_snapshot(cls, subject_id: int) -> List[Dict[str, Any]]:
        """Return the exact admin-cache schema in the WB response shape."""
        if not cls._is_typed_integer(subject_id) or subject_id <= 0:
            raise ValueError('subject_id must be a positive integer')
        marketplace = Marketplace.query.filter_by(code='wb').first()
        category = MarketplaceCategory.query.filter_by(
            marketplace_id=marketplace.id if marketplace else -1,
            subject_id=subject_id,
            is_available=True,
        ).first()
        if not marketplace or not category:
            raise ValueError(f'Категория WB subject_id={subject_id} отсутствует в кэше')
        rows = MarketplaceCategoryCharacteristic.query.filter_by(
            marketplace_id=marketplace.id,
            category_id=category.id,
            is_available=True,
        ).order_by(MarketplaceCategoryCharacteristic.display_order.asc(),
                   MarketplaceCategoryCharacteristic.charc_id.asc()).all()
        rows = [row for row in rows if row.is_enabled or row.required]
        if not rows:
            raise ValueError(f'Схема WB subject_id={subject_id} пуста')
        snapshot = []
        for row in rows:
            dictionary = None
            if row.dictionary_json:
                try:
                    parsed = json.loads(row.dictionary_json)
                    dictionary = parsed if isinstance(parsed, list) else None
                except (json.JSONDecodeError, TypeError):
                    dictionary = None
            snapshot.append({
                'charcID': int(row.charc_id),
                'subjectID': int(category.subject_id),
                'subjectName': category.subject_name or str(category.subject_id),
                'name': row.name,
                'charcType': int(row.charc_type or 0),
                'required': bool(row.required),
                'unitName': row.unit_name,
                'maxCount': int(row.max_count or 0),
                'popular': bool(row.popular),
                'hasFilter': bool(row.has_filter),
                'isVariable': bool(row.is_variable),
                'dictionary': dictionary,
                'dictionarySource': row.dictionary_source or (
                    'admin' if dictionary else 'none'
                ),
                'dictionaryVersion': int(row.dictionary_version or 0),
            })
        return snapshot

    # =========================================================================
    # AI INSTRUCTION GENERATION
    # =========================================================================

    @classmethod
    def generate_ai_instruction(
        cls,
        name: str,
        charc_type: int,
        unit_name: Optional[str],
        max_count: int,
        required: bool,
        dictionary_json: Optional[str] = None
    ) -> str:
        """
        Генерирует умную, гранулярную инструкцию для AI на основе схемы характеристики.

        Учитывает:
        - charcType (0=не используется, 1=строка/массив строк, 4=число)
        - unitName (единица измерения)
        - maxCount (макс. кол-во значений)
        - required (обязательность)
        - dictionary (допустимые значения)
        """
        parts = []

        if required:
            parts.append("[ОБЯЗАТЕЛЬНОЕ ПОЛЕ]")

        if charc_type == 0:
            parts.append(f'Характеристика "{name}" не используется в текущей версии API. Пропустить.')
            return " ".join(parts)

        if charc_type == 4:
            # Числовой тип
            parts.append(f'Извлечь "{name}" — верни ТОЛЬКО ЧИСЛО (int или float).')
            parts.append("НЕ массив, НЕ строку — именно число.")
            if unit_name:
                parts.append(f'Единица измерения: {unit_name}.')
                parts.append(f'Если в тексте указаны другие единицы — конвертируй в {unit_name}.')
                # Common conversions
                unit_lower = unit_name.lower()
                if unit_lower in ('см', 'сантиметр'):
                    parts.append('Пример: "150 мм" -> 15, "0.5 м" -> 50.')
                elif unit_lower in ('г', 'грамм'):
                    parts.append('Пример: "1.5 кг" -> 1500, "500 мг" -> 0.5.')
                elif unit_lower in ('кг', 'килограмм'):
                    parts.append('Пример: "500 г" -> 0.5, "1500 г" -> 1.5.')
                elif unit_lower in ('мл', 'миллилитр'):
                    parts.append('Пример: "1 л" -> 1000, "0.5 л" -> 500.')
            parts.append(f'Пример ответа: "{name}": 15')
            if required:
                parts.append(
                    "Если значение не найдено — не выдумывай его; "
                    "оставь поле для ручной проверки."
                )

        elif charc_type == 1:
            # Строковый тип / массив строк
            if max_count == 1:
                parts.append(f'Извлечь "{name}" — верни массив из ОДНОЙ строки.')
                parts.append(f'Пример: "{name}": ["значение"]')
            elif max_count > 1:
                parts.append(f'Извлечь "{name}" — верни массив строк (максимум {max_count} значений).')
                parts.append(f'Пример: "{name}": ["знач1", "знач2"]')
            else:
                parts.append(f'Извлечь "{name}" — верни массив строк (количество не ограничено).')

            if unit_name:
                parts.append(f'Единица измерения: {unit_name} (включи в значение если уместно).')

            # Dictionary constraints — the key feature
            if dictionary_json:
                try:
                    dict_items = json.loads(dictionary_json)
                    if dict_items:
                        allowed = []
                        for item in dict_items:
                            if isinstance(item, dict):
                                val = item.get('value') or item.get('name', '')
                            else:
                                val = str(item)
                            if val:
                                allowed.append(str(val))

                        if len(allowed) <= 30:
                            parts.append(
                                f'ДОПУСТИМЫЕ ЗНАЧЕНИЯ (СТРОГО одно из): {", ".join(allowed)}.'
                            )
                        else:
                            # Show first 25 + count
                            sample = ", ".join(allowed[:25])
                            parts.append(
                                f'ДОПУСТИМЫЕ ЗНАЧЕНИЯ ({len(allowed)} шт., первые 25): {sample}...'
                            )
                        parts.append(
                            "Значение ДОЛЖНО точно совпадать с одним из допустимых. "
                            "Приведи регистр к формату из словаря."
                        )
                except (json.JSONDecodeError, TypeError):
                    pass

        return " ".join(parts)

    # =========================================================================
    # DIRECTORY SYNC
    # =========================================================================

    @classmethod
    @_reference_sync_guard('directories', 'marketplace_id')
    def sync_directories(
        cls,
        marketplace_id: int,
        *,
        client: Optional[WildberriesAPIClient] = None,
        now: Optional[datetime] = None,
        sleep_fn=time.sleep,
    ) -> Dict[str, Any]:
        """Синхронизация базовых справочников."""
        marketplace = Marketplace.query.get(marketplace_id)
        if not marketplace or marketplace.code != 'wb':
            return {"success": False, "error": "Invalid marketplace"}

        client = client or cls.get_wb_client(marketplace_id)
        if not client:
            return {"success": False, "error": "API key not configured"}

        marketplace.directories_sync_status = 'running'
        marketplace.directories_sync_error = None
        db.session.commit()
        synced_at = now or datetime.utcnow()

        # Define API fetchers
        dirs_to_fetch = {
            'colors': client.get_directory_colors,
            'countries': client.get_directory_countries,
            'kinds': client.get_directory_kinds,
            'seasons': client.get_directory_seasons,
            'vat': client.get_directory_vat,
        }

        results = {}
        succeeded = 0
        failed = 0
        errors = []
        changed = 0

        for index, (d_type, fetcher) in enumerate(dirs_to_fetch.items()):
            if index:
                sleep_fn(cls.CHARACTERISTIC_REQUEST_INTERVAL_SECONDS)
            directory = MarketplaceDirectory.query.filter_by(
                marketplace_id=marketplace_id,
                directory_type=d_type,
            ).first()
            try:
                res = fetcher()
                items = cls._wb_data_list(res, f'{d_type} directory')
                if not items:
                    raise ValueError('empty upstream directory snapshot')
                normalized_items = cls._normalize_directory_snapshot(d_type, items)
                previous_count = int(directory.items_count or 0) if directory else 0
                if (
                    previous_count >= cls.DIRECTORY_SHRINK_GUARD_MIN
                    and len(normalized_items)
                    < previous_count * cls.DIRECTORY_SHRINK_GUARD_RATIO
                ):
                    raise ValueError(
                        f'{d_type} directory snapshot shrank anomalously '
                        f'({previous_count} -> {len(normalized_items)}); '
                        'cache preserved'
                    )
                data_json = cls._stable_json(normalized_items)
                canonical_items = sorted(normalized_items, key=cls._stable_json)
                data_hash = cls._payload_hash(canonical_items)

                if directory:
                    if directory.data_hash != data_hash:
                        directory.version = (directory.version or 0) + 1
                        changed += 1
                    directory.data_json = data_json
                    directory.data_hash = data_hash
                    directory.synced_at = synced_at
                    directory.items_count = len(normalized_items)
                    directory.sync_status = 'success'
                    directory.sync_error = None
                else:
                    directory = MarketplaceDirectory(
                        marketplace_id=marketplace_id,
                        directory_type=d_type,
                        data_json=data_json,
                        data_hash=data_hash,
                        version=1,
                        synced_at=synced_at,
                        items_count=len(normalized_items),
                        sync_status='success',
                    )
                    db.session.add(directory)
                    changed += 1

                results[d_type] = len(normalized_items)
                succeeded += 1
            except Exception as e:
                logger.error(f"Failed to fetch directory '{d_type}': {e}")
                if directory is None:
                    directory = MarketplaceDirectory(
                        marketplace_id=marketplace_id,
                        directory_type=d_type,
                        data_json='[]',
                        items_count=0,
                        version=0,
                    )
                    db.session.add(directory)
                directory.sync_status = 'failed'
                directory.sync_error = cls._error_text(e)
                results[d_type] = f"Error: {e}"
                errors.append(f"{d_type}: {e}")
                failed += 1

        if changed:
            marketplace.directories_version = (
                marketplace.directories_version or 0
            ) + 1
        if failed == 0:
            marketplace.directories_synced_at = synced_at
            marketplace.directories_sync_status = 'success'
            marketplace.directories_sync_error = None
        elif succeeded:
            marketplace.directories_sync_status = 'partial'
            marketplace.directories_sync_error = '; '.join(errors)[:2000]
        else:
            marketplace.directories_sync_status = 'failed'
            marketplace.directories_sync_error = '; '.join(errors)[:2000]
        db.session.commit()

        if failed == len(dirs_to_fetch):
            return {
                "success": False,
                "error": f"All directories failed: {'; '.join(errors)}",
                "results": results
            }

        if failed > 0:
            return {
                "success": True,
                "warning": f"{failed}/{len(dirs_to_fetch)} directories failed",
                "errors": errors,
                "results": results
            }

        return {
            "success": True,
            "results": results,
            "version": marketplace.directories_version,
        }

    # =========================================================================
    # ENABLED CATEGORIES FOR AI PROMPT
    # =========================================================================

    @classmethod
    def get_enabled_categories_for_prompt(cls, marketplace_id: int) -> str:
        """
        Формирует текстовый блок со списком включённых категорий
        для вставки в AI-промпт. Группирует по parent_name.

        Возвращает пустую строку если нет включённых категорий.
        """
        categories = MarketplaceCategory.query.filter_by(
            marketplace_id=marketplace_id,
            is_enabled=True,
            is_available=True,
        ).order_by(
            MarketplaceCategory.parent_name,
            MarketplaceCategory.subject_name
        ).all()

        if not categories:
            return ""

        lines = []
        lines.append("ДОСТУПНЫЕ КАТЕГОРИИ МАРКЕТПЛЕЙСА (wb_subject):")
        lines.append("Выбери ОДНУ наиболее подходящую категорию из списка ниже.")
        lines.append("Значение wb_subject ДОЛЖНО точно совпадать с одним из предметов.")
        lines.append("")

        current_parent = None
        # Страховка: если включено слишком много категорий, обрезаем.
        # Обычно 50-200, но на всякий случай лимит 1000 (~30к символов).
        MAX_CATEGORIES_IN_PROMPT = 1000
        truncated = len(categories) > MAX_CATEGORIES_IN_PROMPT
        cats_for_prompt = categories[:MAX_CATEGORIES_IN_PROMPT]

        for cat in cats_for_prompt:
            parent = cat.parent_name or 'Другое'
            if parent != current_parent:
                current_parent = parent
                lines.append(f"  [{parent}]")
            lines.append(f"    - {cat.subject_name} (ID: {cat.subject_id})")

        lines.append("")
        if truncated:
            lines.append(
                f"Показано {MAX_CATEGORIES_IN_PROMPT} из {len(categories)} категорий. "
                f"Если ни одна не подходит — укажи наиболее близкую."
            )
        else:
            lines.append(f"Всего доступно {len(categories)} категорий.")

        return "\n".join(lines)

    @classmethod
    def get_enabled_categories_list(cls, marketplace_id: int) -> List[Dict[str, Any]]:
        """
        Возвращает список включённых категорий в виде простых dict-ов
        для использования в AI-задачах или API.
        """
        categories = MarketplaceCategory.query.filter_by(
            marketplace_id=marketplace_id,
            is_enabled=True,
            is_available=True,
        ).order_by(MarketplaceCategory.subject_name).all()

        return [
            {
                "subject_id": c.subject_id,
                "subject_name": c.subject_name,
                "parent_name": c.parent_name,
            }
            for c in categories
        ]
