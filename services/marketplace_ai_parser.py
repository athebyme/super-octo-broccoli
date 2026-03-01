"""
Marketplace-aware AI Parsing Pipeline
Generates granular prompts and parses product data specifically tailored
to the target marketplace category schema.

Key features:
- Per-characteristic AI instructions with type/unit/dictionary awareness
- Post-validation of AI responses against the marketplace schema
- Dictionary fuzzy-matching to auto-correct AI output
- Two-pass strategy: bulk parse + targeted re-query for missed required fields
"""
import json
import logging
import re
from typing import Dict, Any, List, Optional, Tuple
from difflib import SequenceMatcher

from models import MarketplaceCategoryCharacteristic, SupplierProduct
from services.ai_service import AITask

logger = logging.getLogger('marketplace_ai_parser')


def _fuzzy_match_dictionary(value: str, dictionary_json: str, threshold: float = 0.75) -> Optional[str]:
    """
    Try to fuzzy-match a value against a dictionary of allowed values.
    Returns the best match if similarity >= threshold, else None.
    """
    if not dictionary_json:
        return None
    try:
        dict_items = json.loads(dictionary_json)
    except (json.JSONDecodeError, TypeError):
        return None

    value_lower = value.strip().lower()
    best_match = None
    best_ratio = 0.0

    for item in dict_items:
        if isinstance(item, dict):
            dict_val = str(item.get('value') or item.get('name', ''))
        else:
            dict_val = str(item)
        if not dict_val:
            continue

        # Exact match (case-insensitive)
        if dict_val.strip().lower() == value_lower:
            return dict_val

        # Fuzzy match
        ratio = SequenceMatcher(None, value_lower, dict_val.strip().lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = dict_val

    if best_ratio >= threshold and best_match:
        return best_match
    return None


class MarketplaceAwareParsingTask(AITask):
    """
    AI parsing task that knows the exact schema of the marketplace category.
    Generates per-field prompts and validates AI output.
    """

    def __init__(self, client, characteristics: List[MarketplaceCategoryCharacteristic], custom_instruction: str = ""):
        super().__init__(client, custom_instruction)
        self.characteristics = characteristics
        # Build lookup for validation
        self._charc_by_name = {c.name: c for c in characteristics if c.is_enabled}

    def get_system_prompt(self) -> str:
        field_blocks = []
        for c in self.characteristics:
            if not c.is_enabled:
                continue

            # Type label
            if c.charc_type == 4:
                type_label = "NUMBER (верни только число, не массив)"
            elif c.charc_type == 1:
                if c.max_count == 1:
                    type_label = 'STRING ARRAY (массив из 1 строки: ["значение"])'
                elif c.max_count > 1:
                    type_label = f'STRING ARRAY (массив строк, макс {c.max_count} элементов)'
                else:
                    type_label = 'STRING ARRAY (массив строк)'
            else:
                type_label = f"TYPE {c.charc_type}"

            block = f'FIELD: "{c.name}"\n'
            block += f'  TYPE: {type_label}\n'
            block += f'  REQUIRED: {"ДА" if c.required else "нет"}\n'

            if c.unit_name:
                block += f'  UNIT: {c.unit_name}\n'

            if c.dictionary_json:
                try:
                    dict_items = json.loads(c.dictionary_json)
                    allowed = []
                    for item in dict_items:
                        if isinstance(item, dict):
                            val = item.get('value') or item.get('name', '')
                        else:
                            val = str(item)
                        if val:
                            allowed.append(str(val))
                    if allowed:
                        if len(allowed) <= 25:
                            block += f'  ALLOWED VALUES: {", ".join(allowed)}\n'
                        else:
                            block += f'  ALLOWED VALUES ({len(allowed)} total, showing first 20): {", ".join(allowed[:20])}...\n'
                        block += '  IMPORTANT: Value MUST exactly match one of the allowed values!\n'
                except (json.JSONDecodeError, TypeError):
                    pass

            if c.ai_instruction:
                block += f'  INSTRUCTION: {c.ai_instruction}\n'

            if c.ai_example_value:
                block += f'  EXAMPLE: {c.ai_example_value}\n'

            field_blocks.append(block)

        sys_prompt = (
            "Ты — эксперт по извлечению характеристик товаров для маркетплейсов.\n"
            "Извлеки характеристики СТРОГО по схеме ниже из данных товара.\n\n"
            "ПРАВИЛА:\n"
            "1. Ответ — ТОЛЬКО валидный JSON объект. Без markdown-блоков (```), без комментариев.\n"
            "2. Ключи JSON = точные имена FIELD ниже.\n"
            "3. Для TYPE=NUMBER верни число (int/float), НЕ строку, НЕ массив.\n"
            "4. Для TYPE=STRING ARRAY верни массив строк: [\"значение\"].\n"
            "5. Если есть ALLOWED VALUES — значение ДОЛЖНО совпадать с одним из них.\n"
            "6. Если REQUIRED поле не удалось найти — лучше пропусти (не ставь null).\n"
            "7. НЕ придумывай характеристики, которых нет в списке.\n"
            "8. Если в тексте единица измерения отличается от UNIT — конвертируй.\n\n"
            "СХЕМА ХАРАКТЕРИСТИК:\n\n"
        )
        sys_prompt += "\n".join(field_blocks)

        return sys_prompt

    def build_user_prompt(self, **kwargs) -> str:
        product_info = kwargs.get('product_info', {})
        original_data = kwargs.get('original_data', {})

        sections = []
        if product_info.get('title'):
            sections.append(f"НАЗВАНИЕ: {product_info['title']}")
        if product_info.get('description'):
            sections.append(f"ОПИСАНИЕ: {product_info['description']}")
        if product_info.get('brand'):
            sections.append(f"БРЕНД: {product_info['brand']}")
        if product_info.get('category') or product_info.get('wb_category'):
            cat = product_info.get('wb_category') or product_info.get('category', '')
            sections.append(f"КАТЕГОРИЯ: {cat}")
        if product_info.get('colors'):
            sections.append(f"ЦВЕТА: {json.dumps(product_info['colors'], ensure_ascii=False)}")
        if product_info.get('materials'):
            sections.append(f"МАТЕРИАЛЫ: {json.dumps(product_info['materials'], ensure_ascii=False)}")
        if product_info.get('sizes'):
            sections.append(f"РАЗМЕРЫ: {json.dumps(product_info['sizes'], ensure_ascii=False)}")
        if product_info.get('dimensions'):
            sections.append(f"ГАБАРИТЫ: {json.dumps(product_info['dimensions'], ensure_ascii=False)}")
        if product_info.get('characteristics'):
            sections.append(f"ХАРАКТЕРИСТИКИ: {json.dumps(product_info['characteristics'], ensure_ascii=False)}")

        if original_data:
            sections.append(f"ИСХОДНЫЕ ДАННЫЕ:\n{json.dumps(original_data, ensure_ascii=False, indent=2)}")

        prompt = "Извлеки характеристики из следующих данных товара:\n\n"
        prompt += "\n\n".join(sections)

        return prompt

    def parse_response(self, response: str) -> Optional[Dict]:
        """Parse AI response and validate each field against the schema."""
        raw_data = self._extract_json(response)
        if raw_data is None:
            return None

        # Post-validate each field
        validated = {}
        validation_log = []

        for field_name, value in raw_data.items():
            charc = self._charc_by_name.get(field_name)
            if not charc:
                validation_log.append(f"'{field_name}': skipped (not in schema)")
                continue

            is_valid, coerced, msg = self._validate_and_coerce(value, charc)
            if is_valid:
                validated[field_name] = coerced
            else:
                validation_log.append(f"'{field_name}': {msg} (original: {value})")
                # Try to salvage — for dictionary fields, fuzzy-match
                if charc.charc_type == 1 and charc.dictionary_json:
                    salvaged = self._try_fuzzy_salvage(value, charc)
                    if salvaged is not None:
                        validated[field_name] = salvaged
                        validation_log.append(f"'{field_name}': fuzzy-matched to {salvaged}")

        if validation_log:
            logger.info(f"AI parse validation: {'; '.join(validation_log)}")

        # Check for missing required fields
        missing_required = []
        for charc in self.characteristics:
            if charc.is_enabled and charc.required and charc.name not in validated:
                missing_required.append(charc.name)

        if missing_required:
            logger.warning(f"Missing required fields: {missing_required}")

        # Attach metadata
        validated['_meta'] = {
            'total_fields': len(self._charc_by_name),
            'filled_fields': len(validated) - 1,  # -1 for _meta itself
            'missing_required': missing_required,
            'validation_issues': validation_log
        }

        return validated

    def _extract_json(self, response: str) -> Optional[Dict]:
        """Extract JSON from AI response, handling markdown blocks."""
        try:
            cleaned = response.strip()
            # Strip markdown code blocks
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            elif cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]

            cleaned = cleaned.strip()
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data
            logger.error("AI response is not a dict")
            return None
        except json.JSONDecodeError:
            # Fallback: try to find JSON object in response
            match = re.search(r'\{[\s\S]*\}', response)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            logger.error(f"Failed to parse AI response as JSON: {response[:300]}")
            return None

    def _validate_and_coerce(
        self, value: Any, charc: MarketplaceCategoryCharacteristic
    ) -> Tuple[bool, Any, Optional[str]]:
        """
        Validate a single value against characteristic schema.
        Returns (is_valid, coerced_value, error_msg).
        """
        if value is None:
            if charc.required:
                return False, None, "Required field is null"
            return True, None, None

        if charc.charc_type == 4:
            # Must be a number
            return self._coerce_to_number(value)

        if charc.charc_type == 1:
            # Must be a string array
            return self._coerce_to_string_array(value, charc)

        if charc.charc_type == 0:
            # Unused type — skip silently
            return True, None, None

        # Unknown type — pass through
        return True, value, None

    def _coerce_to_number(self, value: Any) -> Tuple[bool, Any, Optional[str]]:
        """Coerce value to a number."""
        if isinstance(value, (int, float)):
            return True, value, None

        if isinstance(value, list):
            # AI returned array for a number field — take first
            if value:
                return self._coerce_to_number(value[0])
            return False, None, "Empty array for number field"

        if isinstance(value, str):
            # Extract number from string like "15 см" or "3.5"
            match = re.search(r'([\d]+[.,]?[\d]*)', value.replace(',', '.'))
            if match:
                num_str = match.group(1)
                try:
                    num = float(num_str)
                    return True, int(num) if num == int(num) else num, None
                except ValueError:
                    pass
            return False, value, f"Cannot parse number from '{value}'"

        return False, value, f"Unexpected type {type(value).__name__} for number field"

    def _coerce_to_string_array(
        self, value: Any, charc: MarketplaceCategoryCharacteristic
    ) -> Tuple[bool, Any, Optional[str]]:
        """Coerce value to a string array and validate against dictionary."""
        # Normalize to array
        if isinstance(value, str):
            arr = [value]
        elif isinstance(value, list):
            arr = [str(v) for v in value if v is not None and str(v).strip()]
        elif isinstance(value, (int, float)):
            arr = [str(value)]
        else:
            return False, value, f"Unexpected type {type(value).__name__} for string array"

        if not arr:
            if charc.required:
                return False, [], "Empty array for required field"
            return True, [], None

        # Max count constraint — truncate instead of rejecting
        if charc.max_count > 0 and len(arr) > charc.max_count:
            arr = arr[:charc.max_count]

        # Dictionary validation with fuzzy matching
        if charc.dictionary_json:
            validated_arr = []
            for v in arr:
                matched = _fuzzy_match_dictionary(v, charc.dictionary_json, threshold=0.7)
                if matched:
                    validated_arr.append(matched)
                else:
                    return False, arr, f"Value '{v}' not in dictionary (no fuzzy match found)"
            return True, validated_arr, None

        return True, arr, None

    def _try_fuzzy_salvage(
        self, value: Any, charc: MarketplaceCategoryCharacteristic
    ) -> Optional[List[str]]:
        """Last-resort fuzzy match for failed dictionary values."""
        if not charc.dictionary_json:
            return None

        values = value if isinstance(value, list) else [str(value)]
        result = []
        for v in values:
            matched = _fuzzy_match_dictionary(str(v), charc.dictionary_json, threshold=0.6)
            if matched:
                result.append(matched)
            else:
                return None  # Can't salvage all values
        return result if result else None
