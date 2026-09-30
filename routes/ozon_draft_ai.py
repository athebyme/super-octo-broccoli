"""Seller-scoped local Ozon AI suggestion requests and explicit field review."""
from functools import wraps
import json
import math

from flask import Blueprint, current_app, jsonify, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf.csrf import generate_csrf

from models import db
from services.marketplace_drafts import MarketplaceDraftError
from services.ozon_draft_ai_completion import OzonDraftAICompletionService as Service, DraftAIError
from services.ozon_draft_ai_validation import OzonDraftAIValidationError

bp = Blueprint('ozon_draft_ai', __name__)
API = '/marketplaces/api/drafts'


def _seller(*, require_enabled=False):
    if require_enabled and not current_app.config.get('MARKETPLACE_OZON_ENABLED', False):
        raise DraftAIError('ozon_feature_disabled', 'Черновики Ozon отключены.', 404)
    seller = getattr(current_user, 'seller', None)
    if not seller:
        raise DraftAIError('ai_seller_required', 'Нужен кабинет продавца.', 403)
    return seller.id


def _reply(document, status=200):
    response = jsonify({'success': True, **document, 'csrf_token': generate_csrf()})
    response.status_code = status
    response.headers['Cache-Control'] = 'private, no-store'
    return response


def _guard(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (MarketplaceDraftError, OzonDraftAIValidationError) as exc:
            db.session.rollback()
            response = jsonify({'success': False, 'code': exc.code, 'error': str(exc),
                                'csrf_token': generate_csrf()})
            response.status_code = getattr(exc, 'status_code', 409)
        except Exception as exc:
            db.session.rollback()
            # Neither generated content nor source data belongs in error logs.
            current_app.logger.error('Ozon AI local action failed type=%s', type(exc).__name__)
            response = jsonify({'success': False, 'code': 'ai_action_failed',
                'error': 'Не удалось подтвердить действие. Проверьте сохранённый результат.',
                'csrf_token': generate_csrf()})
            response.status_code = 500
        response.headers['Cache-Control'] = 'private, no-store'
        return response
    return wrapped


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_key')
        value[key] = item
    return value


def _constant(value):
    raise ValueError('nonfinite_json')


def _finite(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('nonfinite_json')
    return result


def _body(allowed):
    if request.args or not request.is_json or (request.content_length or 0) > 65536:
        raise DraftAIError('ai_request_invalid', 'Нужен JSON ограниченного размера без query-параметров.', 400)
    raw = request.stream.read(65537)
    if len(raw) > 65536:
        raise DraftAIError('ai_request_invalid', 'Слишком большой запрос.', 400)
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_finite)
    except (ValueError, RecursionError, UnicodeError):
        raise DraftAIError('ai_request_invalid', 'Некорректный JSON запроса.', 400) from None
    if not isinstance(value, dict) or set(value) - allowed:
        raise DraftAIError('ai_request_invalid', 'Запрос содержит неизвестные поля.', 400)
    return value


def _query_integer(name, *, optional=False):
    values = request.args.getlist(name)
    if not values and optional:
        return None
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit() or len(values[0]) > 19:
        raise DraftAIError('ai_scope_invalid', 'Некорректный идентификатор.', 400)
    value = int(values[0])
    if not 0 < value <= 2**63 - 1:
        raise DraftAIError('ai_scope_invalid', 'Некорректный идентификатор.', 400)
    return value


@bp.route(API + '/ai-completions', methods=['POST'])
@login_required
@_guard
def create():
    seller_id = _seller(require_enabled=True)
    value = _body({'account_id', 'draft_ids', 'expected_versions', 'confirm_generate', 'request_key'})
    if value.get('confirm_generate') is not True:
        raise DraftAIError('ai_confirmation_required', 'Подтвердите дополнение выбранных карточек.', 400)
    run, replayed = Service.accept(seller_id=seller_id, account_id=value.get('account_id'),
        draft_ids=value.get('draft_ids'), expected_versions=value.get('expected_versions'),
        request_key=value.get('request_key'), actor_user_id=current_user.id)
    response = _reply({'run': Service.document(run), 'replayed': replayed}, 202)
    response.headers['Location'] = url_for('ozon_draft_ai.run_page', job_uid=run.job.job_uid)
    return response


@bp.route(API + '/ai-completions/by-request', methods=['GET'])
@login_required
@_guard
def by_request():
    seller_id = _seller()
    if set(request.args) != {'account_id'}:
        raise DraftAIError('ai_scope_invalid', 'Укажите магазин.', 400)
    run = Service.find_by_request(seller_id=seller_id, account_id=_query_integer('account_id'),
                                 request_key=request.headers.get('X-AI-Request-Key'))
    return _reply({'run': Service.document(run)})


@bp.route(API + '/ai-completions/<job_uid>', methods=['GET'])
@login_required
@_guard
def detail(job_uid):
    if request.args:
        raise DraftAIError('ai_request_invalid', 'Неизвестные параметры запроса.', 400)
    return _reply({'run': Service.document(Service.get_run(seller_id=_seller(), job_uid=job_uid))})


@bp.route(API + '/ai-completions/<job_uid>/cancel', methods=['POST'])
@login_required
@_guard
def cancel(job_uid):
    seller_id = _seller()
    _body(set())
    return _reply({'run': Service.document(Service.cancel(seller_id=seller_id, job_uid=job_uid))})


@bp.route(API + '/<int:draft_id>/ai-suggestions', methods=['GET'])
@login_required
@_guard
def suggestions(draft_id):
    seller_id = _seller()
    if set(request.args) - {'item_id'}:
        raise DraftAIError('ai_request_invalid', 'Неизвестные параметры запроса.', 400)
    return _reply(Service.suggestions_document(seller_id=seller_id, draft_id=draft_id,
        actor_user_id=current_user.id, item_id=_query_integer('item_id', optional=True)))


def _review(draft_id, action):
    seller_id = _seller(require_enabled=action == 'apply')
    value = _body({'suggestion_ids', 'expected_version', 'review_token', 'request_key'})
    result = Service.review(seller_id=seller_id, draft_id=draft_id, actor_user_id=current_user.id,
        suggestion_ids=value.get('suggestion_ids'), expected_version=value.get('expected_version'),
        review_token=value.get('review_token'), request_key=value.get('request_key'), action=action)
    return _reply({'review': result})


@bp.route(API + '/<int:draft_id>/ai-suggestions/apply', methods=['POST'])
@login_required
@_guard
def apply(draft_id):
    return _review(draft_id, 'apply')


@bp.route(API + '/<int:draft_id>/ai-suggestions/reject', methods=['POST'])
@login_required
@_guard
def reject(draft_id):
    return _review(draft_id, 'reject')


@bp.route('/marketplaces/drafts/ai-completions/<job_uid>', methods=['GET'])
@login_required
@_guard
def run_page(job_uid):
    run = Service.get_run(seller_id=_seller(), job_uid=job_uid)
    config = {'run': Service.document(run), 'csrf_token': generate_csrf(), 'urls': {
        'status': url_for('ozon_draft_ai.detail', job_uid=job_uid),
        'cancel': url_for('ozon_draft_ai.cancel', job_uid=job_uid),
        'editorBase': '/marketplaces/drafts/', 'history': '/marketplaces/drafts/'}}
    response = current_app.make_response(render_template('ozon_draft_ai_run.html', config=config, run=config['run']))
    response.headers['Cache-Control'] = 'private, no-store'
    return response


def register_ozon_draft_ai_routes(app):
    app.register_blueprint(bp)
