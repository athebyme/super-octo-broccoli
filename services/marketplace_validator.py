"""
Marketplace Validator
Обеспечивает валидацию данных товаров в соответствии со схемой API маркетплейса.
"""
import json
import logging
from typing import Dict, Any, Tuple, List, Optional
from models import MarketplaceCategoryCharacteristic, SupplierProduct

logger = logging.getLogger('marketplace_validator')

class MarketplaceValidator:
    """Validates product data against marketplace characteristic schema"""

    @classmethod
    def validate_product_for_marketplace(
        cls, product: SupplierProduct, marketplace_id: int
    ) -> Dict[str, Any]:
        """
        Validates ALL marketplace-specific fields for a product based on mapped category.
        """
        if not product.wb_subject_id:
            return {"valid": False, "status": "invalid", "errors": ["No marketplace category mapped"], "fill_pct": 0.0}

        # 1. Get the requested characteristics schema
        schema_charcs = MarketplaceCategoryCharacteristic.query.filter(
            MarketplaceCategoryCharacteristic.marketplace_id == marketplace_id,
            MarketplaceCategoryCharacteristic.category.has(subject_id=product.wb_subject_id)
        ).all()

        if not schema_charcs:
            return {"valid": False, "status": "invalid", "errors": ["Schema not synced for this category"], "fill_pct": 0.0}

        # 2. Extract parsed product features
        parsed_data = product.get_ai_parsed_data()
        
        validation_results = []
        errors = []
        filled_count = 0
        required_count = 0

        for charc in schema_charcs:
            value = parsed_data.get(charc.name)
            is_valid, err_msg, coerced_val = cls.validate_single_characteristic(value, charc)
            
            if is_valid and value is not None and value != "" and value != []:
                filled_count += 1
                
            if not is_valid and charc.required:
                errors.append(f"{charc.name}: {err_msg}")
                
            validation_results.append({
                "id": charc.charc_id,
                "name": charc.name,
                "value": coerced_val,
                "type": charc.charc_type,
                "valid": is_valid,
                "error": err_msg,
                "required": charc.required
            })

        total_characteristics = len(schema_charcs)
        fill_pct = (filled_count / total_characteristics * 100) if total_characteristics > 0 else 100.0
        
        status = "valid"
        if errors:
            status = "invalid"
            
        result = {
            "subject_id": product.wb_subject_id,
            "characteristics": validation_results,
            "validation_status": status,
            "validation_errors": errors,
            "fill_percentage": round(fill_pct, 2)
        }
        
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
        if value is None or value == "" or value == []:
            if charc.required:
                return False, "Required field is missing", None
            return True, None, None

        if charc.charc_type == 4: # Number
            try:
                if isinstance(value, str):
                    import re
                    val = re.sub(r'[^\d.,-]', '', str(value).replace(',', '.'))
                    num_val = float(val) if '.' in val else int(val)
                else:
                    num_val = float(value) if isinstance(value, float) else int(value)
                return True, None, num_val
            except Exception:
                return False, f"Value must be a number", value
                
        elif charc.charc_type == 1: # String Array
            arr_val = value if isinstance(value, list) else [str(value)]
            arr_val = [str(v) for v in arr_val if v]
            
            # Max count constraint
            if charc.max_count > 0 and len(arr_val) > charc.max_count:
                return False, f"Maximum {charc.max_count} values allowed", arr_val

            # Dictionary constraint
            if charc.dictionary_json:
                allowed_values = []
                try:
                    dict_items = json.loads(charc.dictionary_json)
                    allowed_values = [str(item.get('name') or item.get('value')).strip().lower() for item in dict_items]
                except Exception:
                    pass
                
                if allowed_values:
                    validated_arr = []
                    for v in arr_val:
                        vl = str(v).strip().lower()
                        if vl in allowed_values:
                            validated_arr.append(v)
                        else:
                            return False, f"Value '{v}' is not in the allowed dictionary", arr_val
                    return True, None, validated_arr
            
            return True, None, arr_val

        elif charc.charc_type == 0: # String
            str_val = str(value) if not isinstance(value, list) else str(value[0]) if value else ""
            if not str_val:
                if charc.required:
                    return False, "Required field is missing", None
                return True, None, None
            return True, None, str_val

        return True, None, value

        return True, None, value
