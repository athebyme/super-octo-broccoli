"""
Marketplace Service
Управляет синхронизацией справочников, категорий и характеристик маркетплейсов.
Также позволяет связывать товары и поставщиков с категориями.
"""

import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
import json

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

    @classmethod
    def generate_ai_instruction(cls, charc: dict) -> str:
        """Генерурует умную инструкцию для AI на основе схемы характеристики."""
        name = charc.get('name')
        charc_type = charc.get('charcType')
        unit = charc.get('unitName')
        max_count = charc.get('maxCount', 0)
        required = charc.get('required', False)

        instruction = []
        if required:
            instruction.append("[REQUIRED]")

        if charc_type == 4:
            instruction.append(f"Extract {name} as a strictly NUMERIC value.")
            if unit:
                instruction.append(f"Unit of measurement is {unit}. Convert if necessary.")
        elif charc_type == 1:
            if max_count == 1:
                instruction.append(f"Extract {name} as a single STRING value in an ARRAY.")
            else:
                max_str = f"max {max_count} values" if max_count > 0 else "multiple values allowed"
                instruction.append(f"Extract {name} as an ARRAY of STRINGS ({max_str}).")
        
        return " ".join(instruction)

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

                if required:
                    required_count += 1
                
                charc = MarketplaceCategoryCharacteristic.query.filter_by(
                    category_id=category.id,
                    charc_id=charc_id
                ).first()

                if charc:
                    # Update (Check if required changed = Schema detection!)
                    if not charc.required and required:
                        logger.warning(f"Schema detection: {name} became required for subject {category.subject_id}")
                    
                    charc.name = name
                    charc.charc_type = charc_type
                    charc.required = required
                    charc.unit_name = unit_name
                    charc.max_count = max_count
                    charc.popular = popular
                    charc.updated_at = datetime.utcnow()
                    total_updated += 1
                else:
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
                        ai_instruction=cls.generate_ai_instruction(item)
                    )
                    db.session.add(charc)
                    total_added += 1

            category.characteristics_synced_at = datetime.utcnow()
            category.characteristics_count = len(items)
            category.required_count = required_count
            db.session.commit()

            # Update marketplace aggregates
            marketplace = category.marketplace
            marketplace.total_characteristics = MarketplaceCategoryCharacteristic.query.filter_by(marketplace_id=marketplace.id).count()
            db.session.commit()

            return {
                "success": True,
                "added": total_added,
                "updated": total_updated,
                "total": category.characteristics_count
            }

        except Exception as e:
            logger.error(f"Error syncing characteristics for category {category.id}: {e}")
            return {"success": False, "error": str(e)}

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
        has_errors = False
        error_messages = []
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
            except Exception as e:
                logger.error(f"Failed to fetch {d_type}: {e}")
                results[d_type] = f"Error: {e}"
                has_errors = True
                error_messages.append(f"{d_type}: {e}")
        
        if not has_errors:
            marketplace.directories_synced_at = datetime.utcnow()
            db.session.commit()
            return {"success": True, "results": results}
        else:
            db.session.commit() # save successful ones
            return {"success": False, "error": f"Errors during sync: {'; '.join(error_messages)}", "results": results}
