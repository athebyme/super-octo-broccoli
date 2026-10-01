"""Local, seller-scoped presentation for the Vue Ozon card editor.

This module never contacts Ozon. Writes keep using MarketplaceDraftService and
the existing publication workflow; reference values are exact cached identities.
"""
from sqlalchemy import or_

from models import MarketplaceAttributeDefinition, MarketplaceAttributeValue
from services.marketplace_drafts import (
    MarketplaceDraftService, MarketplaceDraftError, MarketplaceDraftValidationError,
    MarketplaceDraftConflict, MarketplaceDraftNotFound,
)
from services.marketplace_publications import MarketplacePublicationService
from services.ozon_reference_service import OzonReferenceService
from services.source_photo_display import imported_photo_previews


class MarketplaceDraftEditor:
    @staticmethod
    def card(draft):
        """Compact list presentation; no per-row readiness/provider work."""
        data = draft.to_public_dict()
        content = MarketplaceDraftService._stored_json(draft.content_json, dict)
        media = MarketplaceDraftService._stored_json(draft.media_json, dict)
        source = draft.imported_product
        source = source if source is not None and source.seller_id == draft.seller_id else None
        listing = draft.published_listing
        if not media.get('primary_image') and not media.get('images') and listing is not None and (
            listing.seller_id == draft.seller_id and listing.account_id == draft.account_id
        ):
            media = MarketplaceDraftService._stored_json(listing.media_json, dict)
        images = media.get('images') if isinstance(media.get('images'), list) else []
        image = media.get('primary_image') or next((url for url in images if isinstance(url, str) and url), None)
        title = content.get('name')
        data['title'] = (title if isinstance(title, str) and title else None) or (
            source.title if source is not None else None) or draft.offer_id
        data['primary_image'] = (image if isinstance(image, str) and len(image) <= 2000 else None) or (
            f'/api/photos/imported-product/{source.id}/0' if source is not None else None
        )
        return data

    @classmethod
    def document(cls, *, seller_id, draft_id):
        from services.ozon_write_quarantine import draft_hold, hold_document
        draft = MarketplaceDraftService.get_draft(seller_id=seller_id, draft_id=draft_id)
        data = draft.to_public_dict(detail=True)
        baseline_error = None
        try:
            documents, baseline = MarketplaceDraftService.publication_documents(
                draft, include_baseline_editor_data=True,
            )
        except MarketplaceDraftError as exc:
            documents = MarketplaceDraftService._stored_draft_documents(draft)
            baseline = None
            baseline_error = str(exc)
        definitions = []
        if draft.product_type_id:
            rows = MarketplaceAttributeDefinition.query.filter_by(
                product_type_id=draft.product_type_id,
                marketplace_id=draft.marketplace_id,
            ).order_by(
                MarketplaceAttributeDefinition.is_required.desc(),
                MarketplaceAttributeDefinition.sort_order,
                MarketplaceAttributeDefinition.id,
            ).limit(MarketplaceDraftService.MAX_ATTRIBUTES + 1).all()
            if len(rows) > MarketplaceDraftService.MAX_ATTRIBUTES:
                raise MarketplaceDraftValidationError('Слишком много характеристик в схеме Ozon')
            definitions = [{
                'id': row.external_attribute_id,
                'complex_id': row.attribute_complex_id or '0',
                'name': row.name,
                'description': (row.description or '')[:4000],
                'data_type': row.data_type,
                'required': row.is_required,
                'dictionary': bool(row.dictionary_id),
                'dictionary_count': row.values_count or 0,
                'dictionary_fresh': OzonReferenceService.dictionary_is_fresh(row)
                    if row.dictionary_id else True,
                'max_values': min(row.max_value_count or MarketplaceDraftService.MAX_ATTRIBUTE_VALUES,
                                  MarketplaceDraftService.MAX_ATTRIBUTE_VALUES),
                'collection': row.is_collection,
                'complex_collection': row.complex_is_collection,
                'group': row.group_name,
                'editable': bool(row.is_available and row.is_enabled),
            } for row in rows]
        operations = MarketplacePublicationService.list_for_draft(
            seller_id=seller_id, draft_id=draft.id, limit=20,
        )
        # Keep the persisted validation as history on ``draft``. The editor
        # also needs a current, read-only result so an old ready snapshot cannot
        # make newly missing local fields look publishable. This validator is
        # local-only: it performs no provider, AI, or persistence work.
        current_validation = MarketplaceDraftService._build_validation_result(
            draft
        )
        active = next((op.id for op in operations
                       if op.status in MarketplacePublicationService.ACTIVE_STATUSES), None)
        suggestions = []
        if not draft.product_type_id:
            suggestions = MarketplaceDraftService.suggest_product_types(
                seller_id=seller_id, draft_id=draft.id,
            )
        return {
            'draft': data,
            'linked_category': MarketplaceDraftService.linked_category_state(draft),
            'title': draft.imported_product.title or draft.offer_id,
            'documents': documents,
            'photo_previews': imported_photo_previews(draft.imported_product),
            'baseline_error': baseline_error,
            'baseline_synced_at': baseline.get('synced_at') if baseline else None,
            'baseline_attribute_identities': baseline.get('attribute_identities', []) if baseline else [],
            'preserved_media': baseline.get('preserved_media', {}) if baseline else {},
            'preserved_barcodes': baseline.get('preserved_barcodes', []) if baseline else [],
            'definitions': definitions,
            'suggestions': suggestions,
            'readiness': MarketplaceDraftService.mapping_readiness(
                seller_id=seller_id, draft_id=draft.id,
            ),
            'current_validation': current_validation,
            'operations': [op.to_public_dict(detail=False) for op in operations],
            'active_operation_id': active,
            'write_quarantine': hold_document(draft_hold(draft)),
        }

    @classmethod
    def dictionary(cls, *, seller_id, draft_id, attribute_id, product_type_id, query=''):
        draft = MarketplaceDraftService.get_draft(seller_id=seller_id, draft_id=draft_id)
        if draft.product_type_id != product_type_id:
            raise MarketplaceDraftConflict('Категория изменилась. Обновите редактор перед выбором значения.')
        if not isinstance(query, str) or len(query) > 200:
            raise MarketplaceDraftValidationError('Поиск ограничен 200 символами')
        definition = MarketplaceAttributeDefinition.query.filter_by(
            marketplace_id=draft.marketplace_id, product_type_id=draft.product_type_id,
            external_attribute_id=attribute_id, is_available=True, is_enabled=True,
        ).first()
        if definition is None or not definition.dictionary_id:
            raise MarketplaceDraftNotFound('Справочник характеристики не найден')
        if not OzonReferenceService.dictionary_is_fresh(definition):
            raise MarketplaceDraftConflict('Справочник ещё обновляется. Повторите поиск позже.')
        values = MarketplaceAttributeValue.query.filter_by(
            marketplace_id=draft.marketplace_id, product_type_id=draft.product_type_id,
            attribute_id=definition.id, is_available=True,
        )
        if definition.restriction_value_ids:
            values = values.filter(MarketplaceAttributeValue.external_value_id.in_(
                definition.restriction_value_ids,
            ))
        search = OzonReferenceService.normalize_value(query)
        if search:
            escaped = search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            values = values.filter(or_(
                MarketplaceAttributeValue.value_normalized.like(f'%{escaped}%', escape='\\'),
                MarketplaceAttributeValue.external_value_id.like(f'%{escaped}%', escape='\\'),
            ))
        rows = values.order_by(MarketplaceAttributeValue.value_normalized,
                               MarketplaceAttributeValue.id).limit(31).all()
        return {
            'product_type_id': draft.product_type_id,
            'attribute_id': attribute_id,
            'items': [{'id': row.external_value_id, 'value': row.value} for row in rows[:30]],
            'has_more': len(rows) > 30,
        }
