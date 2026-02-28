"""
Marketplace-aware AI Parsing Pipeline
Generates granular prompts and parses product data specifically tailored to the target marketplace category schema.
"""
import json
import logging
import re
from typing import Dict, Any, List

from models import MarketplaceCategoryCharacteristic, SupplierProduct
from services.ai_service import AITask

logger = logging.getLogger('marketplace_ai_parser')

class MarketplaceAwareParsingTask(AITask):
    """
    AI parsing task that knows the exact schema of the marketplace category.
    """

    def __init__(self, client, characteristics: List[MarketplaceCategoryCharacteristic], custom_instruction: str = ""):
        super().__init__(client, custom_instruction)
        self.characteristics = characteristics

    def get_system_prompt(self) -> str:
        instructions = []
        for c in self.characteristics:
            if not c.is_enabled:
                continue
            
            line = f'FIELD: "{c.name}" | TYPE: {"number" if c.charc_type == 4 else "string array"} | REQUIRED: {"yes" if c.required else "no"}'
            if c.unit_name:
                line += f' | UNIT: {c.unit_name}'
            if c.dictionary_json:
                try: 
                    dict_items = json.loads(c.dictionary_json)[:20]
                    examples = [str(item.get('name') or item.get('value')) for item in dict_items]
                    line += f' | ALLOWED VALUES: {", ".join(examples)}'
                except Exception:
                    pass
            if c.ai_instruction:
                line += f'\n  INSTRUCTION: {c.ai_instruction}'
            
            instructions.append(line)
            
        sys_prompt = "You are an expert product data extractor for e-commerce. You must extract characteristics exactly as specified below for a product.\n"
        sys_prompt += "Return the result STRICTLY as a valid JSON object where keys are exactly the FIELD names requested. Do NOT use markdown code blocks (```json). Return ONLY the raw JSON.\n\n"
        sys_prompt += "\n\n".join(instructions)
        sys_prompt += "\n\nCRITICAL: Do not invent properties not listed above. Strictly respect the TYPE and ALLOWED VALUES constraints. If a REQUIRED field cannot be found, omit it instead of guessing."
        
        return sys_prompt

    def build_user_prompt(self, **kwargs) -> str:
        product_info = kwargs.get('product_info', {})
        original_data = kwargs.get('original_data', {})
        
        prompt = "Extract the requested characteristics from the following product data:\n\n"
        prompt += f"--- PRODUCT TITLE ---\n{product_info.get('title', 'N/A')}\n\n"
        prompt += f"--- DESCRIPTION ---\n{product_info.get('description', 'N/A')}\n\n"
        prompt += f"--- BRAND ---\n{product_info.get('brand', 'N/A')}\n\n"
        prompt += f"--- RAW DATA ---\n{json.dumps(original_data, ensure_ascii=False, indent=2)}\n\n"
        
        return prompt

    def parse_response(self, response: str) -> Any:
        try:
            cleaned = response.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            
            cleaned = cleaned.strip()
            data = json.loads(cleaned)
            if not isinstance(data, dict):
                logger.error("AI response is not a dict.")
                return None
            return data
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI response as JSON: {e}\nResponse: {response[:200]}")
            fallback_match = re.search(r'\{(.*)\}', response, re.DOTALL)
            if fallback_match:
                try:
                    return json.loads("{" + fallback_match.group(1) + "}")
                except:
                    pass
            return None
        except Exception as e:
            logger.error(f"Error parsing AI response: {e}")
            return None
