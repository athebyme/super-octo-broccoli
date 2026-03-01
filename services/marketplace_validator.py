"""
Marketplace Validator
Обеспечивает валидацию данных товаров в соответствии со схемой API маркетплейса.
Проверяет типы, обязательность, maxCount, словарные ограничения.
"""
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Dict, Any, Tuple, List, Optional

from models import MarketplaceCategoryCharacteristic, MarketplaceCategory, SupplierProduct

logger = logging.getLogger('marketplace_validator')


class MarketplaceValidator:
    """Validates product data against marketplace characteristic schema"""

    @classmethod
    def validate_product_for_marketplace(
        cls, product: SupplierProduct, marketplace_id: int
    ) -> Dict[str, Any]:
        """
        Validates ALL marketplace-specific fields for a product based on mapped category.
        Stores validation results in product.marketplace_fields_json.
        """
        if not product.wb_subject_id:
            return {
                "valid": False, "status": "invalid",
                "errors": ["No marketplace category mapped"],
                "fill_pct": 0.0
            }

        # Get category + characteristics schema
        category = MarketplaceCategory.query.filter_by(
            marketplace_id=marketplace_id,
            subject_id=product.wb_subject_id
        ).first()

        if not category:
            return {
                "valid": False, "status": "invalid",
                "errors": ["Category not found in marketplace"],
                "fill_pct": 0.0
            }

        schema_charcs = MarketplaceCategoryCharacteristic.query.filter_by(
            category_id=category.id,
            is_enabled=True
        ).all()

        if not schema_charcs:
            return {
                "valid": False, "status": "invalid",
                "errors": ["Schema not synced for this category"],
                "fill_pct": 0.0
            }

        # Get parsed data (from AI or manual)
        parsed_data = product.get_ai_parsed_data()
        # Also try marketplace-specific data
        mp_data = product.get_ai_marketplace_data()
        # Merge: marketplace data takes priority
        merged_data = {**parsed_data, **mp_data}

        validation_results = []
        errors = []
        total_enabled = len(schema_charcs)
        filled_count = 0
        required_filled = 0
        required_total = 0

        for charc in schema_charcs:
            if charc.charc_type == 0:
                # Unused type — skip from counts
                total_enabled -= 1
                continue

            if charc.required:
                required_total += 1

            value = merged_data.get(charc.name)
            is_valid, err_msg, coerced_val = cls.validate_single_characteristic(value, charc)

            has_value = (
                coerced_val is not None
                and coerced_val != ''
                and coerced_val != []
            )

            if is_valid and has_value:
                filled_count += 1
                if charc.required:
                    required_filled += 1

            if not is_valid and charc.required:
                errors.append(f"{charc.name}: {err_msg}")

            validation_results.append({
                "id": charc.charc_id,
                "name": charc.name,
                "value": coerced_val,
                "type": charc.charc_type,
                "unit": charc.unit_name,
                "valid": is_valid,
                "error": err_msg,
                "required": charc.required,
                "has_value": has_value
            })

        # Fill percentage based on ALL enabled characteristics (not just required)
        fill_pct = (filled_count / total_enabled * 100) if total_enabled > 0 else 0.0

        # Status logic
        if errors:
            status = "invalid"
        elif required_total > 0 and required_filled < required_total:
            status = "partial"
        elif filled_count < total_enabled:
            status = "partial"
        else:
            status = "valid"

        result = {
            "subject_id": product.wb_subject_id,
            "category_name": category.subject_name,
            "characteristics": validation_results,
            "validation_status": status,
            "validation_errors": errors,
            "fill_percentage": round(fill_pct, 2),
            "stats": {
                "total_enabled": total_enabled,
                "filled": filled_count,
                "required_total": required_total,
                "required_filled": required_filled
            }
        }

        # Persist to product
        product.marketplace_fields_json = json.dumps(result, ensure_ascii=False)
        product.marketplace_validation_status = status
        product.marketplace_fill_pct = fill_pct

        return result

    @classmethod
    def validate_single_characteristic(
        cls, value: Any, charc: MarketplaceCategoryCharacteristic
    ) -> Tuple[bool, Optional[str], Any]:
        """
        Validate and optionally coerce a single characteristic value.
        Returns (is_valid, error_msg, coerced_value)
        """
        # Empty check
        if value is None or value == "" or value == []:
            if charc.required:
                return False, "Обязательное поле не заполнено", None
            return True, None, None

        # charcType=0: unused
        if charc.charc_type == 0:
            return True, None, None

        # charcType=4: number
        if charc.charc_type == 4:
            return cls._validate_number(value, charc)

        # charcType=1: string array
        if charc.charc_type == 1:
            return cls._validate_string_array(value, charc)

        # Unknown type — pass through with warning
        logger.warning(f"Unknown charc_type={charc.charc_type} for '{charc.name}'")
        return True, None, value

    @classmethod
    def _validate_number(
        cls, value: Any, charc: MarketplaceCategoryCharacteristic
    ) -> Tuple[bool, Optional[str], Any]:
        """Validate and coerce to number."""
        # Already a number
        if isinstance(value, (int, float)):
            return True, None, value

        # Array — extract first element
        if isinstance(value, list):
            if not value:
                if charc.required:
                    return False, "Пустой массив для числового поля", None
                return True, None, None
            return cls._validate_number(value[0], charc)

        # String — parse number
        if isinstance(value, str):
            cleaned = value.replace(',', '.').strip()
            # Remove units: "15 см" -> "15", "3.5 г" -> "3.5"
            match = re.search(r'([-]?[\d]+[.]?[\d]*)', cleaned)
            if match:
                try:
                    num = float(match.group(1))
                    return True, None, int(num) if num == int(num) else num
                except ValueError:
                    pass
            return False, f"Не удалось извлечь число из '{value}'", value

        return False, f"Неожиданный тип {type(value).__name__} для числового поля", value

    @classmethod
    def _validate_string_array(
        cls, value: Any, charc: MarketplaceCategoryCharacteristic
    ) -> Tuple[bool, Optional[str], Any]:
        """Validate and coerce to string array with dictionary matching."""
        # Normalize to array
        if isinstance(value, str):
            arr = [value]
        elif isinstance(value, list):
            arr = [str(v).strip() for v in value if v is not None and str(v).strip()]
        elif isinstance(value, (int, float)):
            arr = [str(value)]
        else:
            return False, f"Неожиданный тип {type(value).__name__}", value

        if not arr:
            if charc.required:
                return False, "Пустой массив для обязательного поля", []
            return True, None, []

        # Max count constraint — truncate
        if charc.max_count > 0 and len(arr) > charc.max_count:
            arr = arr[:charc.max_count]

        # Dictionary validation with fuzzy matching
        if charc.dictionary_json:
            try:
                dict_items = json.loads(charc.dictionary_json)
            except (json.JSONDecodeError, TypeError):
                dict_items = None

            if dict_items:
                allowed_map = {}  # lowercase -> original
                for item in dict_items:
                    if isinstance(item, dict):
                        val = str(item.get('value') or item.get('name', '')).strip()
                    else:
                        val = str(item).strip()
                    if val:
                        allowed_map[val.lower()] = val

                validated_arr = []
                for v in arr:
                    v_lower = v.strip().lower()

                    # Exact match (case-insensitive)
                    if v_lower in allowed_map:
                        validated_arr.append(allowed_map[v_lower])
                        continue

                    # Fuzzy match
                    best_match = None
                    best_ratio = 0.0
                    for allowed_lower, allowed_original in allowed_map.items():
                        ratio = SequenceMatcher(None, v_lower, allowed_lower).ratio()
                        if ratio > best_ratio:
                            best_ratio = ratio
                            best_match = allowed_original

                    if best_ratio >= 0.75 and best_match:
                        validated_arr.append(best_match)
                        logger.info(
                            f"Dictionary fuzzy match for '{charc.name}': "
                            f"'{v}' -> '{best_match}' (ratio={best_ratio:.2f})"
                        )
                    else:
                        return (
                            False,
                            f"Значение '{v}' не найдено в справочнике "
                            f"(ближайшее: '{best_match}', совпадение: {best_ratio:.0%})",
                            arr
                        )

                return True, None, validated_arr

        return True, None, arr

    @classmethod
    def validate_characteristic_live(
        cls, value: Any, charc_id: int, marketplace_id: int
    ) -> Dict[str, Any]:
        """
        Live validation of a single characteristic value.
        Used by the frontend for instant feedback.
        """
        charc = MarketplaceCategoryCharacteristic.query.filter_by(
            marketplace_id=marketplace_id,
            charc_id=charc_id
        ).first()

        if not charc:
            return {"valid": False, "error": "Characteristic not found"}

        is_valid, err_msg, coerced = cls.validate_single_characteristic(value, charc)
        return {
            "valid": is_valid,
            "error": err_msg,
            "coerced_value": coerced,
            "charc_name": charc.name,
            "charc_type": charc.charc_type
        }
