"""Seller-owned, local AI suggestions. This module never publishes to Ozon.

Admission and review are short SQL transactions. Source/schema validation and a
shared physical-call ledger are mandatory before any background model attempt.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import re
import uuid

from flask import current_app
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, OperationalError

from models import (
    db, BackgroundJob, MarketplaceProductDraft, SellerMarketplaceAccount, AIParsingAttempt,
    OzonDraftCompletionRun as Run, OzonDraftCompletionItem as Item,
    OzonDraftCompletionSuggestion as Suggestion, OzonDraftCompletionReview as Review,
)
from services.marketplace_drafts import MarketplaceDraftService, MarketplaceDraftError
from services.ozon_draft_ai_validation import OzonDraftAIValidation as Validator, OzonDraftAIValidationError
from services.ai_parsing_budget import seller_run_call_limit

PROFILE_VERSION = 'ozon_source_flash_v1'
ACTIVE = ('pending', 'running', 'cancelling')
KEY = re.compile(r'[A-Za-z0-9_-]{24,128}\Z')
MAX_ITEMS = 200
MAX_SUGGESTIONS = 200_000
AI_REJECTION_SAFE_PREFIX = 'ai_rejection:'
AI_REJECTION_UNCLASSIFIED = 'unclassified'
# Exact fixed codes Validator.validate_result may append to rejection results.
VALIDATOR_REJECTION_CODES = frozenset({
    'invalid_suggestion_shape', 'invalid_suggestion_identity',
    'complex_group_requires_manual_review', 'attribute_outside_sealed_schema',
    'invalid_complex_group', 'attribute_values_limit', 'attribute_not_collection',
    'missing_or_excessive_evidence', 'invalid_evidence_fields',
    'invalid_evidence_quote', 'invalid_evidence_path', 'evidence_path_not_found',
    'invalid_evidence_index', 'evidence_must_point_to_scalar', 'nonfinite_evidence',
    'quote_not_in_source', 'source_field_mismatch', 'invalid_attribute_value',
    'dictionary_value_not_exact', 'unexpected_dictionary_value_id',
    'value_not_grounded',
})


def _rejection_reason_counts(run_id, item_count):
    """Bounded derived item counts; unknown/legacy codes never become keys."""
    rows = db.session.query(Item.safe_code).filter_by(
        run_id=run_id,
    ).limit(MAX_ITEMS + 1).all()
    if item_count > MAX_ITEMS or len(rows) != item_count:
        return None
    counts = {}
    for (safe_code,) in rows:
        if safe_code == 'ai_fields_rejected':
            reason = AI_REJECTION_UNCLASSIFIED
        elif isinstance(safe_code, str) and safe_code.startswith(AI_REJECTION_SAFE_PREFIX):
            candidate = safe_code[len(AI_REJECTION_SAFE_PREFIX):]
            reason = candidate if candidate in VALIDATOR_REJECTION_CODES else AI_REJECTION_UNCLASSIFIED
        else:
            continue
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _seller_safe_code(safe_code):
    """Keep detailed validator diagnostics in storage/summary, not item UI codes."""
    if isinstance(safe_code, str) and safe_code.startswith(AI_REJECTION_SAFE_PREFIX):
        return 'ai_fields_rejected'
    return safe_code


class DraftAIError(MarketplaceDraftError):
    def __init__(self, code, message, status_code=409):
        super().__init__(message)
        self.code, self.status_code = code, status_code


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_dump(value).encode('utf-8')).hexdigest()


def _key(value):
    if not isinstance(value, str) or not KEY.fullmatch(value):
        raise DraftAIError('ai_request_key_invalid', 'Нужен сохранённый ключ действия.', 400)
    return hashlib.sha256(value.encode('ascii')).hexdigest()


def _positive(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def _ids(values, limit=MAX_ITEMS):
    if (not isinstance(values, list) or not 1 <= len(values) <= limit
            or not all(_positive(x) for x in values) or len(set(values)) != len(values)):
        raise DraftAIError('ai_selection_invalid', 'Выберите от 1 до 200 разных карточек.', 400)
    return values


@contextmanager
def _writer():
    """No provider I/O is permitted inside this bounded SQLite writer claim."""
    session = db.session()
    if session.new or session.dirty or session.deleted:
        raise DraftAIError('ai_local_conflict', 'Завершите текущее изменение и повторите просмотр.')
    raw = session.connection().connection.driver_connection
    if db.engine.dialect.name != 'sqlite' or raw.in_transaction:
        raise DraftAIError('ai_local_conflict', 'Повторите действие после текущего изменения.')
    previous = raw.execute('PRAGMA busy_timeout').fetchone()[0]
    try:
        raw.execute('PRAGMA busy_timeout=200')
        session.execute(text('BEGIN IMMEDIATE'))
        raw.execute(f'PRAGMA busy_timeout={int(previous)}')
        session.expire_all()
        yield session
        session.commit()
    except OperationalError:
        raw.execute(f'PRAGMA busy_timeout={int(previous)}')
        session.rollback()
        raise DraftAIError('ai_local_busy', 'Сейчас сохраняется другое изменение; повторите позже.', 503) from None
    except Exception:
        session.rollback()
        raise



def _account(seller_id, account_id):
    if not _positive(seller_id) or not _positive(account_id):
        raise DraftAIError('ai_scope_invalid', 'Магазин не найден.', 404)
    account = MarketplaceDraftService._owned_account(seller_id=seller_id, account_id=account_id)
    if not account.is_active:
        raise DraftAIError('ai_account_inactive', 'Магазин отключён; проверьте его настройки.')
    return account


def _owned_drafts(seller_id, account_id, ids):
    rows = MarketplaceProductDraft.query.filter(
        MarketplaceProductDraft.seller_id == seller_id,
        MarketplaceProductDraft.account_id == account_id,
        MarketplaceProductDraft.id.in_(ids),
    ).all()
    if len(rows) != len(ids):
        raise DraftAIError('ai_draft_not_found', 'Часть карточек больше недоступна в этом магазине.', 404)
    by_id = {row.id: row for row in rows}
    return [by_id[x] for x in ids]


def resolve_config(seller_id):
    """An explicit native profile; never route a native key through OpenRouter."""
    from services.ai_service import AIConfig, AIProvider
    try:
        # Preserve the platform's configured credential policy: central when
        # present, otherwise this exact seller's profile. Never override the
        # provider while carrying a key obtained for another provider.
        config = AIConfig.for_seller(seller_id)
    except (KeyError, TypeError, ValueError):
        config = None
    if config is None or config.provider != AIProvider.DEEPSEEK or not config.api_key:
        raise DraftAIError('ai_native_profile_missing', 'Для дополнения карточек настройте нативный DeepSeek Flash.')
    base = config.api_base_url
    if base not in ('https://api.deepseek.com', 'https://api.deepseek.com/',
                    'https://api.deepseek.com/v1', 'https://api.deepseek.com/v1/'):
        raise DraftAIError('ai_native_profile_missing', 'Проверьте адрес нативной платформы DeepSeek.')
    return AIConfig(provider=AIProvider.DEEPSEEK, api_key=config.api_key,
        api_base_url='https://api.deepseek.com/v1', model='deepseek-flash',
        task_profile='seller_draft_completion_flash', seller_id=seller_id,
        max_retries=1, parse_retries=1, timeout=60, max_tokens=8000,
        proxy_enabled=False, log_payloads=False)


class OzonDraftAICompletionService:
    @staticmethod
    def get_run(*, seller_id, job_uid):
        run = Run.query.join(BackgroundJob, BackgroundJob.id == Run.job_id).filter(
            Run.seller_id == seller_id, BackgroundJob.job_uid == job_uid).first()
        if not run:
            raise DraftAIError('ai_run_not_found', 'Запуск дополнения не найден.', 404)
        return run

    @staticmethod
    def find_by_request(*, seller_id, account_id, request_key):
        _account(seller_id, account_id)
        run = Run.query.filter_by(seller_id=seller_id, account_id=account_id,
                                 request_key_hash=_key(request_key)).first()
        if not run:
            raise DraftAIError('ai_run_not_found', 'Запуск по этому ключу пока не найден.', 404)
        return run

    @classmethod
    def accept(cls, *, seller_id, account_id, draft_ids, expected_versions, request_key, actor_user_id):
        _account(seller_id, account_id)
        ids = _ids(draft_ids)
        if (not isinstance(expected_versions, dict)
                or set(expected_versions) != {str(x) for x in ids}
                or not all(_positive(x) for x in expected_versions.values())):
            raise DraftAIError('ai_versions_required', 'Просмотрите текущие версии всех выбранных карточек.')
        key_hash = _key(request_key)
        fingerprint = _hash({'seller_id': seller_id, 'account_id': account_id, 'draft_ids': ids,
                             'versions': expected_versions, 'profile': PROFILE_VERSION})
        existing = Run.query.filter_by(seller_id=seller_id, request_key_hash=key_hash).first()
        if existing:
            if existing.account_id != account_id or existing.request_fingerprint != fingerprint:
                raise DraftAIError('ai_request_key_conflict', 'Этот ключ уже относится к другому действию.')
            return existing, True
        resolve_config(seller_id)  # Validate locally, without provider I/O.
        drafts = _owned_drafts(seller_id, account_id, ids)
        seals = {}
        for draft in drafts:
            if draft.version != expected_versions[str(draft.id)]:
                raise DraftAIError('ai_draft_changed', 'Карточка изменилась; просмотрите её ещё раз.')
            try:
                seals[draft.id] = Validator.capture(draft)
            except OzonDraftAIValidationError as exc:
                seals[draft.id] = exc
        now = datetime.utcnow()
        try:
            with _writer() as session:
                _account(seller_id, account_id)
                existing = Run.query.filter_by(seller_id=seller_id, request_key_hash=key_hash).first()
                if existing:
                    if existing.account_id != account_id or existing.request_fingerprint != fingerprint:
                        raise DraftAIError('ai_request_key_conflict', 'Этот ключ уже относится к другому действию.')
                    return existing, True
                if (Run.query.filter(Run.status.in_(ACTIVE)).count() >= 20
                        or Run.query.filter(Run.seller_id == seller_id, Run.status.in_(ACTIVE)).count() >= 2
                        or Suggestion.query.count() >= MAX_SUGGESTIONS):
                    raise DraftAIError('ai_capacity_full', 'Дождитесь завершения текущих партий дополнения.', 429)
                if Item.query.filter(Item.seller_id == seller_id, Item.account_id == account_id,
                        Item.draft_id.in_(ids), Item.status.in_(('pending', 'reserved'))).first():
                    raise DraftAIError('ai_draft_already_running', 'Для части карточек уже выполняется дополнение.')
                drafts = _owned_drafts(seller_id, account_id, ids)
                if any(d.version != expected_versions[str(d.id)] for d in drafts):
                    raise DraftAIError('ai_draft_changed', 'Карточки изменились; повторите просмотр.')
                job = BackgroundJob(job_uid='ozon-ai-' + uuid.uuid4().hex, seller_id=seller_id,
                    job_type='ozon_draft_completion', status='running', total=len(ids),
                    processed=0, succeeded=0, failed_count=0)
                session.add(job); session.flush()
                run = Run(job_id=job.id, seller_id=seller_id, account_id=account_id,
                    actor_user_id=actor_user_id, request_key_hash=key_hash, request_fingerprint=fingerprint,
                    profile_version=PROFILE_VERSION, model='deepseek-flash', status='pending',
                    next_due_at=now, item_count=len(ids), max_calls=seller_run_call_limit(len(ids)),
                    requested_calls=0, created_at=now, updated_at=now)
                session.add(run); session.flush()
                for ordinal, draft in enumerate(drafts, 1):
                    seal = seals[draft.id]
                    good = isinstance(seal, dict)
                    item = Item(run_id=run.id, ordinal=ordinal, seller_id=seller_id, account_id=account_id,
                        draft_id=draft.id, imported_product_id=draft.imported_product_id,
                        product_type_id=draft.product_type_id, expected_draft_version=draft.version,
                        status='pending' if good else 'needs_input', next_due_at=now if good else None,
                        attempt_count=0, safe_code=None if good else seal.code,
                        created_at=now, updated_at=now, completed_at=None if good else now)
                    if good:
                        for name in ('source_kind', 'source_product_id', 'source_hash', 'type_schema_hash',
                                     'dictionary_hash', 'filled_slots_hash'):
                            setattr(item, name, seal[name])
                    session.add(item)
                job.set_progress({'version': 1, 'run_id': run.id, 'mode': 'draft_suggestions'})
                cls._refresh(run, flush=True)
            return run, False
        except IntegrityError:
            db.session.rollback()
            existing = Run.query.filter_by(seller_id=seller_id, request_key_hash=key_hash).first()
            if existing and existing.account_id == account_id and existing.request_fingerprint == fingerprint:
                return existing, True
            raise DraftAIError('ai_local_conflict', 'Карточки уже приняты другим действием; обновите список.') from None

    @staticmethod
    def _refresh(run, *, flush=False):
        if flush:
            db.session.flush()
        counts = dict(db.session.query(Item.status, func.count(Item.id)).filter_by(run_id=run.id).group_by(Item.status))
        rejection_reasons = _rejection_reason_counts(run.id, run.item_count)
        active = counts.get('pending', 0) + counts.get('reserved', 0)
        now = datetime.utcnow()
        if not active:
            run.status = 'cancelled' if run.status in ('cancelling', 'cancelled') else 'completed'
            run.completed_at = run.completed_at or now
            run.next_due_at = None
        job = db.session.get(BackgroundJob, run.job_id)
        job.processed = run.item_count - active
        job.succeeded = counts.get('proposed', 0) + counts.get('no_evidence', 0)
        job.failed_count = sum(counts.get(x, 0) for x in ('failed', 'needs_input', 'stale', 'unknown_response'))
        job.status = 'running' if active else 'completed'
        job.set_result({'mode': 'draft_suggestions', 'status': run.status,
                        'counts': counts, 'active': active, 'total': run.item_count,
                        'rejection_reasons': rejection_reasons})
        job.updated_at = now
        run.updated_at = now

    @staticmethod
    def document(run):
        job = db.session.get(BackgroundJob, run.job_id)
        rows = Item.query.filter_by(run_id=run.id).order_by(Item.ordinal).limit(MAX_ITEMS + 1).all()
        if len(rows) > MAX_ITEMS:
            raise DraftAIError('ai_run_invalid', 'Состав запуска повреждён.')
        titles = dict(db.session.query(MarketplaceProductDraft.id, MarketplaceProductDraft.content_json).filter(
            MarketplaceProductDraft.seller_id == run.seller_id,
            MarketplaceProductDraft.account_id == run.account_id,
            MarketplaceProductDraft.id.in_([r.draft_id for r in rows])))
        items = []
        for item in rows:
            try:
                title = json.loads(titles.get(item.draft_id) or '{}').get('name')
                title = title[:300] if isinstance(title, str) and title.strip() else f'Карточка № {item.draft_id}'
            except (ValueError, TypeError, AttributeError):
                title = None
            items.append({'id': item.id, 'draft_id': item.draft_id,
                'expected_version': item.expected_draft_version, 'title': title,
                'status': item.status, 'code': _seller_safe_code(item.safe_code),
                'next_due_at': item.next_due_at.isoformat() + 'Z' if item.next_due_at else None})
        summary = job.get_result()
        if isinstance(summary, dict):
            summary = {**summary,
                       'rejection_reasons': _rejection_reason_counts(run.id, run.item_count)}
        return {'job_uid': job.job_uid, 'account_id': run.account_id, 'mode': 'draft_suggestions',
            'status': run.status, 'model': run.model, 'total': run.item_count,
            'physical_calls': AIParsingAttempt.query.filter_by(lane='seller_draft_completion', run_uid=job.job_uid).count(),
            'max_calls': run.max_calls,
            'items': items, 'summary': summary,
            'created_at': run.created_at.isoformat() + 'Z'}

    @classmethod
    def cancel(cls, *, seller_id, job_uid):
        run_id = cls.get_run(seller_id=seller_id, job_uid=job_uid).id
        with _writer():
            run = db.session.get(Run, run_id)
            if run.status in ACTIVE:
                run.status = 'cancelling'
                now = datetime.utcnow()
                Item.query.filter(Item.run_id == run.id, Item.status.in_(('pending', 'reserved'))).update(
                    {'status': 'cancelled', 'completed_at': now, 'updated_at': now,
                     'next_due_at': None}, synchronize_session=False)
                db.session.expire_all()
                run = db.session.get(Run, run_id)
                cls._refresh(run)
        return run

    @staticmethod
    def _suggestion(row):
        return {'id': row.id, 'attribute_id': str(row.attribute_id),
                'complex_id': str(row.complex_id), 'group_ordinal': row.group_ordinal,
                'values': json.loads(row.values_json), 'evidence': json.loads(row.evidence_json),
                'provenance_code': row.provenance_code, 'status': row.status}

    @staticmethod
    def _signer():
        return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='ozon-ai-review-v1')

    @classmethod
    def _review_state(cls, *, seller_id, draft_id, actor_user_id, item_id=None):
        draft = MarketplaceDraftService.get_draft(seller_id=seller_id, draft_id=draft_id)
        _account(seller_id, draft.account_id)
        query = Item.query.filter_by(seller_id=seller_id, account_id=draft.account_id, draft_id=draft.id)
        if item_id is not None:
            if not _positive(item_id):
                raise DraftAIError('ai_item_not_found', 'Предложение не найдено.', 404)
            query = query.filter_by(id=item_id)
        item = query.order_by(Item.id.desc()).first()
        if item_id is not None and not item:
            raise DraftAIError('ai_item_not_found', 'Предложение не найдено.', 404)
        rows = Suggestion.query.filter_by(item_id=item.id).order_by(Suggestion.id).limit(201).all() if item else []
        if len(rows) > 200:
            raise DraftAIError('ai_suggestion_limit', 'Состав предложений требует отдельной проверки.')
        applicable = False
        code = None
        seal = None
        if item and item.status == 'proposed' and any(r.status == 'proposed' for r in rows):
            try:
                seal = Validator.capture(draft)
                same_source = (draft.imported_product_id == item.imported_product_id
                    and draft.product_type_id == item.product_type_id
                    and all(seal[name] == getattr(item, name) for name in
                            ('source_kind', 'source_product_id', 'source_hash', 'type_schema_hash', 'dictionary_hash')))
                # A partial apply permits a NEW review of the resulting exact
                # version; arbitrary manual edits never silently rebase proposals.
                last_apply = max((r.applied_draft_version or 0 for r in rows if r.status == 'accepted'), default=0)
                version_ok = draft.version in (item.expected_draft_version, last_apply)
                filled_ok = seal['filled_slots_hash'] == (item.reviewed_filled_slots_hash or item.filled_slots_hash)
                applicable = same_source and version_ok and filled_ok
                if not applicable:
                    code = 'ai_suggestions_stale'
            except OzonDraftAIValidationError as exc:
                code = exc.code
        elif item:
            code = _seller_safe_code(item.safe_code)
        canonical = [cls._suggestion(r) for r in rows]
        claims = {'seller_id': seller_id, 'account_id': draft.account_id, 'draft_id': draft.id,
                  'actor_user_id': actor_user_id, 'item_id': item.id if item else None,
                  'version': draft.version, 'suggestions_hash': _hash(canonical),
                  'seal_hash': _hash({k: seal[k] for k in ('source_hash', 'type_schema_hash',
                      'dictionary_hash', 'filled_slots_hash')}) if seal else None}
        return draft, item, rows, canonical, applicable, code, claims

    @classmethod
    def suggestions_document(cls, *, seller_id, draft_id, actor_user_id, item_id=None):
        from models import MarketplaceAttributeDefinition
        draft, item, rows, canonical, applicable, code, claims = cls._review_state(
            seller_id=seller_id, draft_id=draft_id, actor_user_id=actor_user_id, item_id=item_id)
        names = dict(db.session.query(MarketplaceAttributeDefinition.external_attribute_id,
                    MarketplaceAttributeDefinition.name).filter_by(product_type_id=draft.product_type_id)) if item else {}
        for value in canonical:
            value['name'] = names.get(value['attribute_id']) or 'Характеристика ' + value['attribute_id']
            value['label'] = ', '.join(str(v.get('value', '')) for v in value['values'])
            value['applicable'] = applicable and value['status'] == 'proposed'
        return {'draft_id': draft.id, 'account_id': draft.account_id, 'version': draft.version,
                'item': {'id': item.id, 'status': item.status, 'code': code,
                         'expected_version': item.expected_draft_version,
                         'run_uid': item.run.job.job_uid} if item else None,
                'suggestions': canonical,
                'review_token': cls._signer().dumps(claims) if any(r.status == 'proposed' for r in rows) else None,
                'code': code}

    @classmethod
    def review(cls, *, seller_id, draft_id, actor_user_id, suggestion_ids,
               expected_version, review_token, request_key, action='apply'):
        if action not in ('apply', 'reject') or not _positive(expected_version):
            raise DraftAIError('ai_review_invalid', 'Повторите просмотр предложений.', 400)
        ids = sorted(_ids(suggestion_ids))
        key_hash = _key(request_key)
        fingerprint = _hash({'seller_id': seller_id, 'draft_id': draft_id, 'actor': actor_user_id,
            'suggestions': ids, 'version': expected_version, 'action': action})

        def replay():
            previous = Review.query.filter_by(seller_id=seller_id, request_key_hash=key_hash).first()
            if previous and previous.request_fingerprint != fingerprint:
                raise DraftAIError('ai_review_key_conflict', 'Этот ключ уже относится к другому изменению.')
            return previous

        previous = replay()
        if previous:
            return cls.review_document(previous, replayed=True)
        if not isinstance(review_token, str) or len(review_token) > 8192:
            raise DraftAIError('ai_review_required', 'Откройте свежий просмотр предложений.')
        try:
            signed = cls._signer().loads(review_token, max_age=1800)
        except (BadSignature, SignatureExpired):
            raise DraftAIError('ai_review_expired', 'Просмотр устарел; откройте предложения ещё раз.') from None
        if (not isinstance(signed, dict) or signed.get('seller_id') != seller_id
                or signed.get('draft_id') != draft_id or signed.get('actor_user_id') != actor_user_id
                or signed.get('version') != expected_version or not _positive(signed.get('item_id'))):
            raise DraftAIError('ai_review_required', 'Просмотр не совпадает с выбранной карточкой.')
        with _writer() as session:
            previous = replay()
            if previous:
                return cls.review_document(previous, replayed=True)
            draft, item, rows, canonical, applicable, code, claims = cls._review_state(
                seller_id=seller_id, draft_id=draft_id, actor_user_id=actor_user_id,
                item_id=signed['item_id'])
            if signed != claims or (action == 'apply' and not applicable):
                raise DraftAIError(code or 'ai_review_stale', 'Карточка или предложения изменились; просмотрите их снова.')
            selected = [r for r in rows if r.id in ids and r.status == 'proposed']
            if len(selected) != len(ids):
                raise DraftAIError('ai_suggestion_conflict', 'Часть выбранных предложений уже изменилась.')
            groups = {(r.complex_id, r.group_ordinal) for r in selected if str(r.complex_id) != '0'}
            if any(r.status == 'proposed' and (r.complex_id, r.group_ordinal) in groups and r.id not in ids for r in rows):
                raise DraftAIError('ai_complex_group_incomplete', 'Выберите все поля связанной группы характеристик.')
            if action == 'apply':
                MarketplaceDraftService._assert_no_active_publication(draft)
            before_version = draft.version
            if action == 'apply':
                patch = Validator.merge_selected(draft, [cls._suggestion(r) for r in selected])
                draft = MarketplaceDraftService._update_draft_core(
                    seller_id=seller_id, draft_id=draft.id, expected_version=expected_version,
                    patch=patch, corrected_by_user_id=actor_user_id, commit=False)
                item.reviewed_filled_slots_hash = Validator.filled_slots_hash(draft)
            now = datetime.utcnow()
            for row in selected:
                row.status = 'accepted' if action == 'apply' else 'rejected'
                row.reviewer_user_id = actor_user_id
                row.reviewed_at = now
                row.updated_at = now
                row.applied_draft_version = draft.version if action == 'apply' else None
            review = Review(seller_id=seller_id, account_id=draft.account_id, draft_id=draft.id,
                item_id=item.id, actor_user_id=actor_user_id, request_key_hash=key_hash,
                request_fingerprint=fingerprint, action=action, version_before=before_version,
                version_after=draft.version if action == 'apply' else None,
                selected_ids_json=_dump(ids), created_at=now)
            session.add(review); session.flush()
        return cls.review_document(review, replayed=False)

    @staticmethod
    def review_document(review, *, replayed):
        return {'review_id': review.id, 'draft_id': review.draft_id, 'action': review.action,
                'version_before': review.version_before, 'version_after': review.version_after,
                'suggestion_ids': json.loads(review.selected_ids_json), 'replayed': replayed,
                'requires_validation': review.action == 'apply'}
