"""
Marketplace Service
Управляет синхронизацией справочников, категорий и характеристик маркетплейсов.
Также позволяет связывать товары и поставщиков с категориями.
"""

import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
import json

from sqlalchemy import func

from models import (
    db, Marketplace, MarketplaceCategory, MarketplaceCategoryCharacteristic,
    MarketplaceDirectory, MarketplaceConnection, SupplierProduct
)
from services.wb_api_client import WildberriesAPIClient

logger = logging.getLogger('marketplace_service')


class MarketplaceService:

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
    def sync_categories(cls, marketplace_id: int) -> Dict[str, Any]:
        """
        Полная синхронизация иерархии категорий (предметов) с WB API.
        """
        marketplace = Marketplace.query.get(marketplace_id)
        if not marketplace or marketplace.code != 'wb':
            return {"success": False, "error": "Invalid or unsupported marketplace"}

        client = cls.get_wb_client(marketplace_id)
        if not client:
            return {"success": False, "error": "API key not configured"}

        marketplace.categories_sync_status = 'running'
        db.session.commit()

        try:
            offset = 0
            limit = 1000
            total_added = 0
            total_updated = 0

            while True:
                response = client.get_subjects_list(limit=limit, offset=offset)
                items = response.get('data', [])
                if not items:
                    break

                for item in items:
                    subject_id = item.get('subjectID')
                    subject_name = item.get('subjectName')
                    parent_id = item.get('parentID')
                    parent_name = item.get('parentName')

                    if not subject_id:
                        continue

                    category = MarketplaceCategory.query.filter_by(
                        marketplace_id=marketplace.id,
                        subject_id=subject_id
                    ).first()

                    if category:
                        category.subject_name = subject_name
                        category.parent_id = parent_id
                        category.parent_name = parent_name
                        category.updated_at = datetime.utcnow()
                        total_updated += 1
                    else:
                        category = MarketplaceCategory(
                            marketplace_id=marketplace.id,
                            subject_id=subject_id,
                            subject_name=subject_name,
                            parent_id=parent_id,
                            parent_name=parent_name,
                            is_enabled=False
                        )
                        db.session.add(category)
                        total_added += 1

                db.session.commit()

                if len(items) < limit:
                    break
                offset += limit

            marketplace.categories_synced_at = datetime.utcnow()
            marketplace.categories_sync_status = 'success'
            marketplace.total_categories = MarketplaceCategory.query.filter_by(marketplace_id=marketplace_id).count()
            db.session.commit()

            return {
                "success": True,
                "added": total_added,
                "updated": total_updated,
                "total": marketplace.total_categories
            }

        except Exception as e:
            logger.error(f"Error syncing categories: {e}")
            marketplace.categories_sync_status = 'failed'
            marketplace.total_categories = MarketplaceCategory.query.filter_by(marketplace_id=marketplace_id).count()
            db.session.commit()
            return {"success": False, "error": str(e)}

    # =========================================================================
    # CHARACTERISTIC SYNC
    # =========================================================================

    @classmethod
    def sync_category_characteristics(cls, category_id: int) -> Dict[str, Any]:
        """Синхронизация характеристик для одной категории."""
        category = MarketplaceCategory.query.get(category_id)
        if not category:
            return {"success": False, "error": "Category not found"}

        client = cls.get_wb_client(category.marketplace_id)
        if not client:
            return {"success": False, "error": "API key not configured"}

        try:
            response = client.get_card_characteristics_config(category.subject_id)
            items = response.get('data', [])

            total_added = 0
            total_updated = 0
            required_count = 0
            schema_changes = []

            for item in items:
                charc_id = item.get('charcID')
                if not charc_id:
                    continue

                name = item.get('name')
                charc_type = item.get('charcType', 0)
                required = item.get('required', False)
                unit_name = item.get('unitName')
                max_count = item.get('maxCount', 0)
                popular = item.get('popular', False)
                dictionary = item.get('dictionary')

                if required:
                    required_count += 1

                charc = MarketplaceCategoryCharacteristic.query.filter_by(
                    category_id=category.id,
                    charc_id=charc_id
                ).first()

                # Serialize dictionary
                dict_json = None
                if dictionary:
                    try:
                        dict_json = json.dumps(dictionary, ensure_ascii=False)
                    except Exception:
                        dict_json = None

                if charc:
                    # Detect schema changes
                    if charc.required != required:
                        schema_changes.append(
                            f"'{name}' required: {charc.required} -> {required}"
                        )
                    if charc.charc_type != charc_type:
                        schema_changes.append(
                            f"'{name}' type: {charc.charc_type} -> {charc_type}"
                        )

                    charc.name = name
                    charc.charc_type = charc_type
                    charc.required = required
                    charc.unit_name = unit_name
                    charc.max_count = max_count
                    charc.popular = popular
                    charc.dictionary_json = dict_json
                    charc.updated_at = datetime.utcnow()
                    # Regenerate AI instruction on schema update
                    charc.ai_instruction = cls.generate_ai_instruction(
                        name=name,
                        charc_type=charc_type,
                        unit_name=unit_name,
                        max_count=max_count,
                        required=required,
                        dictionary_json=dict_json
                    )
                    total_updated += 1
                else:
                    ai_instruction = cls.generate_ai_instruction(
                        name=name,
                        charc_type=charc_type,
                        unit_name=unit_name,
                        max_count=max_count,
                        required=required,
                        dictionary_json=dict_json
                    )
                    charc = MarketplaceCategoryCharacteristic(
                        category_id=category.id,
                        marketplace_id=category.marketplace_id,
                        charc_id=charc_id,
                        name=name,
                        charc_type=charc_type,
                        required=required,
                        unit_name=unit_name,
                        max_count=max_count,
                        popular=popular,
                        dictionary_json=dict_json,
                        ai_instruction=ai_instruction
                    )
                    db.session.add(charc)
                    total_added += 1

            if schema_changes:
                logger.warning(
                    f"Schema changes detected for subject {category.subject_id}: "
                    + "; ".join(schema_changes)
                )

            category.characteristics_synced_at = datetime.utcnow()
            category.characteristics_count = len(items)
            category.required_count = required_count
            db.session.commit()

            # Update marketplace aggregates (unique charc_id only — same characteristic can exist in many categories)
            marketplace = category.marketplace
            marketplace.total_characteristics = db.session.query(
                func.count(func.distinct(MarketplaceCategoryCharacteristic.charc_id))
            ).filter_by(marketplace_id=marketplace.id).scalar() or 0
            db.session.commit()

            return {
                "success": True,
                "added": total_added,
                "updated": total_updated,
                "total": category.characteristics_count,
                "schema_changes": schema_changes
            }

        except Exception as e:
            logger.error(f"Error syncing characteristics for category {category.id}: {e}")
            return {"success": False, "error": str(e)}

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
                parts.append("Если значение не найдено — попробуй оценить по типу товара.")

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
    def sync_directories(cls, marketplace_id: int) -> Dict[str, Any]:
        """Синхронизация базовых справочников."""
        marketplace = Marketplace.query.get(marketplace_id)
        if not marketplace or marketplace.code != 'wb':
            return {"success": False, "error": "Invalid marketplace"}

        client = cls.get_wb_client(marketplace_id)
        if not client:
            return {"success": False, "error": "API key not configured"}

        # Define API fetchers
        dirs_to_fetch = {
            'colors': client.get_directory_colors,
            'countries': client.get_directory_countries,
            'kinds': client.get_directory_kinds,
            'seasons': client.get_directory_seasons,
            'vat': client.get_directory_vat,
            'tnved': client.get_directory_tnved,
        }

        results = {}
        succeeded = 0
        failed = 0
        errors = []

        for d_type, fetcher in dirs_to_fetch.items():
            try:
                res = fetcher()
                items = res.get('data', [])

                directory = MarketplaceDirectory.query.filter_by(
                    marketplace_id=marketplace_id,
                    directory_type=d_type
                ).first()

                if directory:
                    directory.data_json = json.dumps(items, ensure_ascii=False)
                    directory.synced_at = datetime.utcnow()
                    directory.items_count = len(items)
                else:
                    directory = MarketplaceDirectory(
                        marketplace_id=marketplace_id,
                        directory_type=d_type,
                        data_json=json.dumps(items, ensure_ascii=False),
                        synced_at=datetime.utcnow(),
                        items_count=len(items)
                    )
                    db.session.add(directory)

                results[d_type] = len(items)
                succeeded += 1
            except Exception as e:
                logger.error(f"Failed to fetch directory '{d_type}': {e}")
                results[d_type] = f"Error: {e}"
                errors.append(f"{d_type}: {e}")
                failed += 1

        # Only mark as synced if at least one directory succeeded
        if succeeded > 0:
            marketplace.directories_synced_at = datetime.utcnow()
            db.session.commit()

        if failed == len(dirs_to_fetch):
            return {
                "success": False,
                "error": f"All directories failed: {'; '.join(errors)}",
                "results": results
            }

        if failed > 0:
            db.session.commit()
            return {
                "success": True,
                "warning": f"{failed}/{len(dirs_to_fetch)} directories failed",
                "errors": errors,
                "results": results
            }

        db.session.commit()
        return {"success": True, "results": results}

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
            is_enabled=True
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
        for cat in categories:
            parent = cat.parent_name or 'Другое'
            if parent != current_parent:
                current_parent = parent
                lines.append(f"  [{parent}]")
            lines.append(f"    - {cat.subject_name} (ID: {cat.subject_id})")

        lines.append("")
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
            is_enabled=True
        ).order_by(MarketplaceCategory.subject_name).all()

        return [
            {
                "subject_id": c.subject_id,
                "subject_name": c.subject_name,
                "parent_name": c.parent_name,
            }
            for c in categories
        ]
