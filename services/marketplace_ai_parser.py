"""
Marketplace-aware AI Parsing Pipeline
Generates granular prompts and parses product data specifically tailored
to the target marketplace category schema.

Key features:
- Per-characteristic AI instructions with type/unit/dictionary awareness
- Post-validation of AI responses against the marketplace schema
- Dictionary fuzzy-matching to auto-correct AI output
- Two-pass strategy: bulk parse + targeted re-query for missed required fields
- Few-shot examples from validated products of the same category
- AI response caching via content hash
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

    def __init__(self, client, characteristics: List[MarketplaceCategoryCharacteristic],
                 custom_instruction: str = "", category_id: int = None):
        super().__init__(client, custom_instruction)
        self.characteristics = characteristics
        self.category_id = category_id
        # Build lookup for validation
        self._charc_by_name = {c.name: c for c in characteristics if c.is_enabled}

    def get_system_prompt(self, target_fields: List[str] = None) -> str:
        field_blocks = []
        for c in self.characteristics:
            if not c.is_enabled:
                continue

            # Если указаны целевые поля (2-й проход) — показываем только их
            if target_fields and c.name not in target_fields:
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

        is_retry = bool(target_fields)
        if is_retry:
            sys_prompt = (
                "Ты — эксперт по извлечению характеристик товаров для маркетплейсов.\n"
                "ВАЖНО: Нижеперечисленные поля НЕ БЫЛИ заполнены при первом проходе.\n"
                "Ты ДОЛЖЕН заполнить их, используя ВСЕ доступные данные и экспертное знание.\n\n"
                "СТРАТЕГИЯ:\n"
                "- Извлеки из текста описания, названия, характеристик.\n"
                "- Если точного значения нет — ВЫВЕДИ ЛОГИЧЕСКИ из контекста:\n"
                "  • Вес — оцени по типу товара и материалу (силикон ~100-300г, пластик ~50-150г).\n"
                "  • Ширина — оцени по диаметру или типу изделия.\n"
                "  • Особенности — определи по материалу, форме, описанию.\n"
                "- Не оставляй поля пустыми если можно разумно предположить значение.\n\n"
                "ПРАВИЛА:\n"
                "1. Ответ — ТОЛЬКО валидный JSON объект.\n"
                "2. Ключи JSON = точные имена FIELD ниже.\n"
                "3. Для TYPE=NUMBER верни число (int/float).\n"
                "4. Для TYPE=STRING ARRAY верни массив строк.\n"
                "5. Если есть ALLOWED VALUES — значение ДОЛЖНО совпадать с одним из них.\n\n"
                "ПОЛЯ ДЛЯ ЗАПОЛНЕНИЯ:\n\n"
            )
        else:
            sys_prompt = (
                "Ты — эксперт по извлечению характеристик товаров для маркетплейсов.\n"
                "Извлеки характеристики по схеме ниже из данных товара.\n\n"
                "ПРАВИЛА:\n"
                "1. Ответ — ТОЛЬКО валидный JSON объект. Без markdown-блоков (```), без комментариев.\n"
                "2. Ключи JSON = точные имена FIELD ниже.\n"
                "3. Для TYPE=NUMBER верни число (int/float), НЕ строку, НЕ массив.\n"
                "4. Для TYPE=STRING ARRAY верни массив строк: [\"значение\"].\n"
                "5. Если есть ALLOWED VALUES — значение ДОЛЖНО совпадать с одним из них.\n"
                "6. Если значение можно ЛОГИЧЕСКИ ВЫВЕСТИ из контекста — выведи.\n"
                "   Например: из описания видно материал, из размеров — ширину, из названия — особенности.\n"
                "   Используй своё экспертное знание о товарах этой категории.\n"
                "7. Ключи JSON = только имена FIELD из схемы, не добавляй своих.\n"
                "8. Если в тексте единица измерения отличается от UNIT — конвертируй.\n"
                "9. Старайся заполнить МАКСИМУМ полей. Пустые поля = плохо заполненная карточка.\n"
                "   Для числовых полей (размеры, вес) — используй здравый смысл если точное значение неизвестно,\n"
                "   но можно оценить по типу товара, материалу, аналогичным изделиям.\n\n"
                "СХЕМА ХАРАКТЕРИСТИК:\n\n"
            )

        sys_prompt += "\n".join(field_blocks)

        # Добавляем few-shot примеры из уже валидированных товаров
        if not is_retry:
            examples = self._get_validated_examples(limit=2)
            if examples:
                sys_prompt += "\n\nПРИМЕРЫ ПРАВИЛЬНОГО ЗАПОЛНЕНИЯ:\n"
                for ex in examples:
                    sys_prompt += f"\nВход: {ex['title']}\n"
                    sys_prompt += f"Выход: {json.dumps(ex['parsed'], ensure_ascii=False)}\n"

        return sys_prompt

    def _get_validated_examples(self, limit: int = 2) -> List[Dict]:
        """
        Получить примеры из уже успешно валидированных товаров той же категории.
        Используется для few-shot prompting.
        """
        if not self.category_id:
            return []

        try:
            examples = []
            products = SupplierProduct.query.filter(
                SupplierProduct.wb_subject_id == self.category_id,
                SupplierProduct.marketplace_validation_status == 'valid',
                SupplierProduct.ai_marketplace_json.isnot(None),
            ).order_by(SupplierProduct.ai_parsed_at.desc()).limit(limit).all()

            for p in products:
                try:
                    parsed = json.loads(p.ai_marketplace_json)
                    # Убираем мета-данные из примера
                    parsed.pop('_meta', None)
                    if parsed and p.title:
                        examples.append({
                            'title': p.title[:100],
                            'parsed': parsed,
                        })
                except (json.JSONDecodeError, TypeError):
                    continue

            return examples
        except Exception as e:
            logger.debug(f"Could not load few-shot examples: {e}")
            return []

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

    # ------------------------------------------------------------------
    # Two-pass execution with caching
    # ------------------------------------------------------------------

    def execute_two_pass(self, **kwargs) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Двухпроходное выполнение AI-парсинга:

        Проход 1: Полный парсинг всех характеристик.
        Проход 2: Точечный re-query только для пропущенных обязательных полей.

        Также поддерживает кэширование через AIParsingCache.
        """
        product = kwargs.get('product')
        product_info = kwargs.get('product_info', {})

        # Проверяем кэш
        if product:
            from services.ai_parsing_cache import AIParsingCache
            product_data = product.get_all_data_for_parsing() if hasattr(product, 'get_all_data_for_parsing') else product_info
            if AIParsingCache.is_cache_valid(product, product_data):
                cached = AIParsingCache.get_cached(product)
                if cached:
                    logger.info(f"AI parse cache hit for product {product.external_id}")
                    return True, cached, None

        # Проход 1: Полный парсинг
        success, result, error = self.execute(**kwargs)
        if not success or result is None:
            return success, result, error

        meta = result.get('_meta', {})
        missing_required = meta.get('missing_required', [])

        # Проход 2: Точечный retry для пропущенных обязательных полей
        if missing_required:
            logger.info(
                f"Two-pass: {len(missing_required)} required fields missing, "
                f"starting pass 2: {missing_required}"
            )
            retry_result = self._execute_targeted_retry(missing_required, **kwargs)

            if retry_result:
                # Мержим результаты
                for field_name, value in retry_result.items():
                    if field_name != '_meta' and field_name not in result:
                        result[field_name] = value

                # Пересчитываем метаданные
                still_missing = [
                    f for f in missing_required if f not in result
                ]
                result['_meta'] = {
                    'total_fields': meta.get('total_fields', 0),
                    'filled_fields': len([k for k in result if k != '_meta']),
                    'missing_required': still_missing,
                    'validation_issues': meta.get('validation_issues', []),
                    'two_pass_recovered': [f for f in missing_required if f in result],
                }

                logger.info(
                    f"Two-pass result: recovered {len(missing_required) - len(still_missing)}"
                    f"/{len(missing_required)} fields"
                )

        # Сохраняем в кэш
        if product:
            from services.ai_parsing_cache import AIParsingCache
            product_data = product.get_all_data_for_parsing() if hasattr(product, 'get_all_data_for_parsing') else product_info
            model_name = getattr(self.client, 'model', '')
            AIParsingCache.save_to_cache(product, product_data, result, model_used=model_name)

        return True, result, None

    def _execute_targeted_retry(
        self, missing_fields: List[str], **kwargs
    ) -> Optional[Dict]:
        """
        Второй проход: запрос AI только для конкретных пропущенных полей.
        Использует более агрессивный промпт.
        """
        try:
            system_prompt = self.get_system_prompt(target_fields=missing_fields)
            user_prompt = self.build_user_prompt(**kwargs)

            # Добавляем контекст о том, что это повторная попытка
            user_prompt += (
                f"\n\nВАЖНО: При предыдущей попытке не удалось извлечь "
                f"следующие ОБЯЗАТЕЛЬНЫЕ поля: {', '.join(missing_fields)}. "
                f"Ты ДОЛЖЕН заполнить каждое из них. "
                f"Если точного значения нет в тексте — выведи логически: "
                f"оцени по типу товара, материалу, аналогичным изделиям. "
                f"Незаполненное поле = карточка не пройдёт модерацию маркетплейса."
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            response = self.client.chat_completion(messages)
            if not response:
                return None

            return self.parse_response(response)

        except Exception as e:
            logger.error(f"Targeted retry failed: {e}")
            return None
