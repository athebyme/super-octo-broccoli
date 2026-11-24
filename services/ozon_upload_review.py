"""Read-only review of exact draft versions; never prepares or sends cards."""
from datetime import datetime
import math

from flask import current_app
from models import (
    BackgroundJob, MarketplaceOperation, MarketplaceProductDraft,
    OzonBulkUploadItem, OzonBulkUploadRun,
)
from services.marketplace_draft_editor import MarketplaceDraftEditor
from services.marketplace_drafts import MarketplaceDraftService, MarketplaceDraftError
from services.marketplace_publications import MarketplacePublicationService
from services.ozon_bulk_upload import (
    OzonBulkUploadValidationError, OzonBulkUploadNotFound,
)
from services.ozon_write_quarantine import draft_hold
from services.ozon_catalog_operation_fence import (
    active_catalog_listing_operation,
)


class OzonUploadReviewService:
    MAX_ITEMS = 200
    PAGE_SIZE = 20
    COLOR_ATTRIBUTE_ID = "10096"

    @classmethod
    def _prepared_color(cls, documents, validation, *, product_type_id):
        """Return one validator-backed color label from this draft's exact docs."""
        if type(documents) is not dict or type(validation) is not dict:
            return None
        schema = validation.get("schema")
        if (type(schema) is not dict
                or schema.get("product_type_id") != product_type_id
                or schema.get("fresh") is not True):
            return None
        errors = validation.get("errors")
        attributes = documents.get("attributes")
        complex_groups = documents.get("complex_attributes")
        if type(errors) is not list or type(attributes) is not list or type(complex_groups) is not list:
            return None
        if any(type(item) is not dict for item in attributes):
            return None

        # The same attribute ID in a complex group or a malformed group makes
        # its flat publication scope ambiguous, so do not surface a label.
        for group in complex_groups:
            if type(group) is not dict or type(group.get("attributes")) is not list:
                return None
            if any(type(item) is not dict for item in group["attributes"]):
                return None
            if any(item.get("attribute_id") == cls.COLOR_ATTRIBUTE_ID
                   for item in group["attributes"]):
                return None

        matches = [
            (index, item) for index, item in enumerate(attributes)
            if item.get("attribute_id") == cls.COLOR_ATTRIBUTE_ID
        ]
        if len(matches) != 1:
            return None
        index, color = matches[0]
        if color.get("complex_id", "0") != "0":
            return None

        # The normal validator checks these exact output values against the
        # current type schema and dictionary. Suppress the label for any
        # validator error scoped to this attribute.
        color_path = f"attributes[{index}]"
        for error in errors:
            if type(error) is not dict:
                return None
            field = error.get("field")
            if (field == color_path or field == f"attributes.{cls.COLOR_ATTRIBUTE_ID}"
                    or (type(field) is str and field.startswith(color_path + "."))):
                return None

        values = color.get("values")
        if type(values) is not list or not 1 <= len(values) <= 8:
            return None
        labels = []
        total_chars = 0
        for value in values:
            if type(value) is not dict:
                return None
            label = value.get("value")
            dictionary_value_id = value.get("dictionary_value_id")
            if (type(label) is not str or not label or label != label.strip()
                    or len(label) > 120 or type(dictionary_value_id) is not str
                    or not dictionary_value_id or len(dictionary_value_id) > 100):
                return None
            total_chars += len(label)
            if total_chars > 240:
                return None
            labels.append(label)
        return ", ".join(labels)

    @staticmethod
    def _automatic_attribute_cleanup(draft):
        """Expose only the version-current, bounded cleanup note."""
        if draft.validation_status not in {"valid", "invalid"} or draft.validated_at is None:
            return None
        stored = MarketplaceDraftService._stored_json(
            draft.validation_result_json, dict,
        )
        note = stored.get("automatic_attribute_cleanup")
        if not isinstance(note, dict):
            return None
        attribute_name = note.get("attribute_name")
        before_count = note.get("before_count")
        max_value_count = note.get("max_value_count")
        if (
            note.get("attribute_id") != "4543"
            or note.get("complex_id") != "0"
            or note.get("reason") != "unchanged_optional_attribute_exceeds_schema_max"
            or note.get("action") != "omit_entire_attribute"
            or not isinstance(attribute_name, str)
            or not attribute_name.strip()
            or len(attribute_name) > 500
            or type(before_count) is not int
            or type(max_value_count) is not int
            or max_value_count < 1
            or before_count <= max_value_count
        ):
            return None
        return {
            "attribute_id": "4543",
            "complex_id": "0",
            "attribute_name": attribute_name.strip(),
            "reason": "unchanged_optional_attribute_exceeds_schema_max",
            "before_count": before_count,
            "max_value_count": max_value_count,
            "action": "omit_entire_attribute",
        }

    @classmethod
    def document(cls, *, seller_id, account_id, draft_ids, page=1,
                 parent_prepare_job_uid=None, result_job_uid=None):
        if parent_prepare_job_uid is not None and result_job_uid is not None:
            raise OzonBulkUploadValidationError(
                "Укажите только один исходный запуск"
            )
        if (not isinstance(draft_ids, list) or not 1 <= len(draft_ids) <= cls.MAX_ITEMS
                or any(type(pk) is not int or not 0 < pk <= 2**63-1 for pk in draft_ids)
                or len(set(draft_ids)) != len(draft_ids)):
            raise OzonBulkUploadValidationError("Выберите от 1 до 200 разных черновиков")
        pages = math.ceil(len(draft_ids) / cls.PAGE_SIZE)
        if type(page) is not int or not 1 <= page <= pages:
            raise OzonBulkUploadValidationError("Недоступная страница просмотра")

        # Validate the complete selection before disclosing even the first page.
        # Projection queries avoid loading all 200 potentially large documents.
        owned = MarketplaceProductDraft.query.with_entities(
            MarketplaceProductDraft.id, MarketplaceProductDraft.imported_product_id,
        ).filter_by(seller_id=seller_id, account_id=account_id).filter(
            MarketplaceProductDraft.id.in_(draft_ids),
        ).all()
        if {row.id for row in owned} != set(draft_ids):
            raise OzonBulkUploadNotFound("Выбранные черновики недоступны в этом кабинете")

        if result_job_uid is not None:
            from services.ozon_bulk_upload import OzonBulkUploadService
            OzonBulkUploadService.validate_review_recovery_scope(
                seller_id=seller_id,
                account_id=account_id,
                result_job_uid=result_job_uid,
                requested_draft_ids=draft_ids,
            )

        if parent_prepare_job_uid is not None:
            parent = OzonBulkUploadRun.query.join(
                BackgroundJob, BackgroundJob.id == OzonBulkUploadRun.job_id,
            ).filter(
                BackgroundJob.job_uid == parent_prepare_job_uid,
                OzonBulkUploadRun.seller_id == seller_id,
                OzonBulkUploadRun.account_id == account_id,
                OzonBulkUploadRun.mode == "source_prepare",
            ).first()
            if parent is None:
                raise OzonBulkUploadNotFound("Запуск подготовки недоступен")
            parent_pairs = set(OzonBulkUploadItem.query.with_entities(
                OzonBulkUploadItem.draft_id, OzonBulkUploadItem.imported_product_id,
            ).filter_by(run_id=parent.id).filter(
                OzonBulkUploadItem.draft_id.in_(draft_ids),
            ).all())
            if any((row.id, row.imported_product_id) not in parent_pairs for row in owned):
                raise OzonBulkUploadNotFound("Черновик не принадлежит выбранному запуску")

        page_ids = draft_ids[(page-1)*cls.PAGE_SIZE:page*cls.PAGE_SIZE]
        operations = MarketplaceOperation.query.with_entities(
            MarketplaceOperation.draft_id, MarketplaceOperation.id,
        ).filter(
            MarketplaceOperation.seller_id == seller_id,
            MarketplaceOperation.account_id == account_id,
            MarketplaceOperation.draft_id.in_(page_ids),
            MarketplaceOperation.status.in_(MarketplacePublicationService.ACTIVE_STATUSES),
        ).order_by(MarketplaceOperation.id).all()
        active = dict(operations)
        enabled = bool(current_app.config.get("MARKETPLACE_OZON_ENABLED", False)
                       and current_app.config.get("MARKETPLACE_OZON_PUBLICATION_ENABLED", False))
        items = []
        account_label = None
        for pk in page_ids:
            try:
                draft = MarketplaceDraftService.get_draft(seller_id=seller_id, draft_id=pk)
                if draft.account_id != account_id:
                    raise OzonBulkUploadNotFound("Кабинет черновика изменился")
                # The pure validator rebuilds source/reference/content facts at
                # read time. Persisted ready is never treated as fresh evidence.
                validation = MarketplaceDraftService._build_validation_result(draft)
                card = MarketplaceDraftEditor.card(draft)
                blocked = list(validation.get("errors") or [])
                hold = draft_hold(draft)
                try:
                    documents, baseline = MarketplaceDraftService.publication_documents(
                        draft, include_price_context=True,
                    )
                except MarketplaceDraftError:
                    documents = MarketplaceDraftService._stored_draft_documents(draft)
                    baseline = None
            except MarketplaceDraftError as exc:
                raise OzonBulkUploadNotFound("Черновик недоступен для просмотра") from exc
            if draft.status != "ready":
                blocked.append({"code": "draft_review_required", "field": "draft",
                                "message": "Сохраните и проверьте черновик перед отправкой"})
            if not enabled:
                blocked.append({"code": "publication_disabled", "field": "account",
                                "message": "Отправка карточек Ozon сейчас выключена"})
            active_operation_id = active.get(pk)
            if active_operation_id is None and draft.published_listing_id is not None:
                listing_operation = active_catalog_listing_operation(
                    seller_id=draft.seller_id,
                    marketplace_id=draft.marketplace_id,
                    account_id=draft.account_id,
                    listing_id=draft.published_listing_id,
                )
                if listing_operation is not None:
                    active_operation_id = listing_operation.id
            if active_operation_id is not None:
                blocked.append({"code": "already_in_progress", "field": "draft",
                                "message": "По этому товару Ozon уже выполняется операция; сначала сверьте её результат"})
            if hold is not None:
                blocked.append({"code": "write_quarantined", "field": "draft",
                                "message": "Сначала разберите незавершённую запись этой карточки"})
            account_label = draft.account.label
            media = documents.get("media") or {}
            photos = [media.get("primary_image"), *(media.get("images") or [])]
            item = {
                "draft_id": pk, "version": draft.version,
                "imported_product_id": draft.imported_product_id,
                "title": card["title"], "primary_image": card["primary_image"],
                "offer_id": draft.offer_id,
                "action": "update" if draft.published_listing_id else "create",
                "product_type_id": draft.product_type_id,
                "type_name": draft.product_type.name if draft.product_type else None,
                "external_category_id": draft.external_category_id,
                "external_type_id": draft.external_type_id,
                "automatic_attribute_cleanup": cls._automatic_attribute_cleanup(draft),
                "selectable": not blocked,
                "errors": blocked, "warnings": validation.get("warnings", []),
                "checked_at": validation["validated_at"],
                "schema": validation["schema"],
                "active_operation_id": active_operation_id,
                "commercial": {key: (documents.get("commercial") or {}).get(key)
                               for key in ("price", "old_price", "vat", "currency_code")},
                "price_context": MarketplaceDraftEditor.price_context(documents, baseline),
                "dimensions": {key: (documents.get("dimensions") or {}).get(key)
                               for key in ("width", "height", "depth", "dimension_unit", "weight", "weight_unit")},
                "photos_count": len({value for value in photos if isinstance(value, str) and value}),
            }
            prepared_color = cls._prepared_color(
                documents,
                validation,
                product_type_id=draft.product_type_id,
            )
            if prepared_color is not None:
                item["color"] = prepared_color
            items.append(item)
        document = {
            "account_id": account_id, "account_label": account_label,
            "draft_ids": draft_ids, "parent_prepare_job_uid": parent_prepare_job_uid,
            "items": items, "publication_enabled": enabled,
            "checked_at": datetime.utcnow().isoformat(),
            "pagination": {"page": page, "per_page": cls.PAGE_SIZE,
                           "pages": pages, "total": len(draft_ids)},
        }
        if result_job_uid is not None:
            document["result_job_uid"] = result_job_uid
        return document
