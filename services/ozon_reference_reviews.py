"""Exact snapshot approval for anomalous Ozon dictionary shrinkage.

HTTP only reviews local observations. The regular reference worker fetches the
whole official dictionary again before an approved snapshot can replace cache.
"""
from datetime import datetime, timedelta
import json

from sqlalchemy import func
from models import (db, User, AdminAuditLog,
                    OzonReferenceValueReview)
from services.ozon_reference_service import (OzonReferenceService as References,
                                            OzonReferenceValidationError)


class OzonReferenceReviewService:
    MAX_CANDIDATE_BYTES = 8 * 1024 * 1024
    MAX_TOTAL_BYTES = 64 * 1024 * 1024
    APPROVAL_HOURS = 24

    @staticmethod
    def scope(attribute):
        product_type = attribute.product_type
        return References._hash({
            'attribute_id': attribute.id, 'external_id': attribute.external_attribute_id,
            'dictionary_id': attribute.dictionary_id, 'required': attribute.is_required,
            'available': attribute.is_available, 'type_id': product_type.id,
            'external_type_id': product_type.external_type_id,
            'category_id': product_type.category.external_category_id,
            'type_available': product_type.is_available,
            'category_available': product_type.category.is_available,
            'schema': product_type.attributes_schema_hash,
            'baseline_hash': attribute.values_snapshot_hash,
            'baseline_version': attribute.values_version,
        })

    @classmethod
    def current(cls, attribute, review):
        return bool(review and review.scope_hash == cls.scope(attribute))

    @classmethod
    def blocks(cls, attribute):
        review = attribute.value_review
        return bool(cls.current(attribute, review)
                    and review.status in ('pending_review', 'approved'))

    @staticmethod
    def eligible(attribute, now):
        return bool(attribute.is_available and attribute.is_enabled and attribute.dictionary_id
                    and attribute.product_type.is_seller_selectable
                    and References.reference_is_fresh(attribute.product_type, now=now))

    @staticmethod
    def audit(review, action):
        db.session.add(AdminAuditLog(
            admin_user_id=review.approved_by, action=action,
            target_type='ozon_reference', target_id=review.attribute_id,
            details=json.dumps({
                'version': review.version, 'candidate_hash': review.candidate_hash,
                'baseline_hash': review.baseline_hash, 'baseline_version': review.baseline_version,
                'scope_hash': review.scope_hash, 'schema_hash': review.schema_hash,
                'previous_count': review.previous_count, 'candidate_count': review.candidate_count,
            }, sort_keys=True),
        ))

    @classmethod
    def admit_or_stage(cls, attribute, canonical, snapshot_hash, previous_count, now, *, existing):
        """Called under the value lock after a complete, validated provider read."""
        review = db.session.get(OzonReferenceValueReview, attribute.id)
        if (cls.current(attribute, review) and review.status == 'approved'
                and review.candidate_hash == snapshot_hash and review.expires_at > now
                and cls.eligible(attribute, now)):
            actor = db.session.get(User, review.approved_by)
            if actor and actor.is_admin and actor.is_active:
                return review
        storage_lock = References._try_claim('review-store', 0)
        if storage_lock is None:
            raise OzonReferenceValidationError('Ozon dictionary review storage is busy')
        try:
            # Keep display-only prior names with the exact candidate. HTTP
            # preview must not load an entire large active dictionary again.
            stored = {**canonical, 'previous_values': {
                value.external_value_id: value.value
                for value in existing.values() if value.is_available
            }}
            serialized = References._stable_json(stored)
            size = len(serialized.encode('utf-8'))
            total = db.session.query(func.coalesce(func.sum(OzonReferenceValueReview.payload_bytes), 0)).filter(
                OzonReferenceValueReview.attribute_id != attribute.id,
            ).scalar()
            if size > cls.MAX_CANDIDATE_BYTES or total + size > cls.MAX_TOTAL_BYTES:
                raise OzonReferenceValidationError('Ozon dictionary review exceeds the storage safety limit')
            same = (cls.current(attribute, review) and review.candidate_hash == snapshot_hash
                    and review.status == 'pending_review' and review.expires_at > now)
            if not same:
                if review is None:
                    review = OzonReferenceValueReview(attribute_id=attribute.id, version=0)
                    db.session.add(review)
                review.version += 1
                review.product_type_id = attribute.product_type_id
                review.status = 'pending_review'
                review.baseline_hash = attribute.values_snapshot_hash or ''
                review.baseline_version = attribute.values_version or 0
                review.schema_hash = attribute.product_type.attributes_schema_hash or ''
                review.scope_hash = cls.scope(attribute)
                review.candidate_hash = snapshot_hash
                review.candidate_json = serialized
                review.payload_bytes = size
                review.previous_count = previous_count
                review.candidate_count = len(canonical['values'])
                review.observed_at = now
                review.expires_at = now + timedelta(hours=cls.APPROVAL_HOURS)
                review.approved_by = None
                review.approved_at = None
                review.applied_at = None
            # Refresh the bounded preview payload even for an unchanged
            # pending candidate; its approval identity remains exact.
            review.candidate_json = serialized
            review.payload_bytes = size
            # Persist the observation, never any partial active dictionary rows.
            attribute.values_sync_checkpoint = None
            db.session.commit()
            raise OzonReferenceValidationError(
                'Ozon dictionary became smaller; administrator review is required'
            )
        finally:
            References._release_claim(storage_lock)

    @classmethod
    def finish(cls, attribute, approved, now):
        review = approved or db.session.get(OzonReferenceValueReview, attribute.id)
        if review and (approved or review.status in ('pending_review', 'approved')):
            review.status = 'applied' if approved else 'stale'
            review.candidate_json = None
            review.payload_bytes = 0
            if approved:
                review.applied_at = now
                cls.audit(review, 'ozon_reference_shrink_applied')

    @classmethod
    def approve(cls, attribute, payload, actor_id, *, now=None):
        now = now or datetime.utcnow()
        if (not isinstance(payload, dict) or set(payload) != {'version', 'candidate_hash', 'confirm'}
                or payload['confirm'] is not True or type(payload['version']) is not int
                or not isinstance(payload['candidate_hash'], str)):
            raise OzonReferenceValidationError('Подтвердите точную версию просмотренного справочника.')
        lock = References._try_claim('values', attribute.id)
        if lock is None:
            raise OzonReferenceValidationError('Словарь обновляется. Повторите после завершения.')
        schema_lock = References._try_claim('attributes', attribute.product_type_id)
        if schema_lock is None:
            References._release_claim(lock)
            raise OzonReferenceValidationError('Схема обновляется. Повторите после завершения.')
        try:
            db.session.expire_all()
            actor = db.session.get(User, actor_id)
            review = db.session.get(OzonReferenceValueReview, attribute.id)
            if not actor or not actor.is_admin or not actor.is_active:
                raise OzonReferenceValidationError('Подтверждение доступно активному администратору.')
            if (not cls.current(attribute, review) or review.status not in ('pending_review', 'approved')
                    or review.expires_at <= now or review.version != payload['version']
                    or review.candidate_hash != payload['candidate_hash']
                    or not cls.eligible(attribute, now)):
                raise OzonReferenceValidationError('Справочник изменился или устарел. Обновите данные и проверьте новый список.')
            if review.status != 'approved':
                review.status = 'approved'
                review.approved_by = actor.id
                review.approved_at = now
                attribute.values_sync_status = 'pending'
                attribute.values_sync_error = None
                cls.audit(review, 'ozon_reference_shrink_approved')
                db.session.commit()
            return {'success': True, 'status': 'approved'}
        finally:
            References._release_claim(schema_lock)
            References._release_claim(lock)

    @classmethod
    def preview(cls, attribute, *, page=1, mode='removed', now=None):
        now = now or datetime.utcnow()
        review = db.session.get(OzonReferenceValueReview, attribute.id)
        if not review:
            return {'success': True, 'review': None}
        current = cls.current(attribute, review)
        data = {
            'version': review.version, 'status': review.status,
            'candidate_hash': review.candidate_hash,
            'previous_count': review.previous_count, 'candidate_count': review.candidate_count,
            'observed_at': review.observed_at.isoformat() + 'Z',
            'expires_at': review.expires_at.isoformat() + 'Z',
            'can_approve': bool(current and review.status == 'pending_review' and review.expires_at > now
                                and cls.eligible(attribute, now)),
            'current': current, 'mode': mode, 'page': page, 'rows': [], 'total': 0,
        }
        if current and review.candidate_json:
            stored = json.loads(review.candidate_json)
            candidate = stored['values']
            new = {row['external_value_id']: row['value'] for row in candidate}
            old = stored.get('previous_values', {})
            if 'previous_values' not in stored:
                data['can_approve'] = False
            if mode == 'removed':
                rows = [{'id': key, 'value': value} for key, value in old.items() if key not in new]
            elif mode == 'changed':
                rows = [{'id': key, 'value': value, 'previous': old[key]} for key, value in new.items()
                        if key in old and old[key] != value]
            else:
                rows = [{'id': key, 'value': value, 'added': key not in old} for key, value in new.items()]
            rows.sort(key=lambda row: int(row['id']))
            data['total'] = len(rows)
            data['rows'] = rows[(page - 1) * 100:page * 100]
        return {'success': True, 'review': data}
