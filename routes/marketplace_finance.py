"""Seller-scoped read-only Ozon finance snapshot UI and API."""

import logging

from services.ozon_read_requests import enqueue_read, read_status

from flask import Blueprint, current_app, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from services.marketplace_accounts import MarketplaceAccountError, MarketplaceAccountService
from services.marketplace_finance import (
    MarketplaceFinanceError,
    MarketplaceFinanceService,
    MarketplaceFinanceValidationError,
)


logger = logging.getLogger("marketplace_finance_routes")
marketplace_finance_bp = Blueprint("marketplace_finance", __name__)


def _feature_enabled() -> bool:
    return bool(current_app.config.get("MARKETPLACE_OZON_ENABLED", False))


def _seller_id() -> int:
    seller = current_user.seller
    if seller is None:
        raise MarketplaceFinanceValidationError("Нет привязки к продавцу")
    return seller.id


def _positive_query(name: str, default=None, *, maximum=None) -> int:
    raw = request.args.get(name)
    if raw is None and default is not None:
        return default
    if (len(request.args.getlist(name)) > 1 or not isinstance(raw, str)
            or not raw.isascii() or not raw.isdecimal() or raw.startswith("0") or len(raw) > 18):
        raise MarketplaceFinanceValidationError(
            f"{name} должен быть положительным целым числом"
        )
    value = int(raw)
    if maximum is not None and value > maximum:
        raise MarketplaceFinanceValidationError(
            f"{name} превышает лимит {maximum}"
        )
    return value


def _validate_query(allowed):
    if set(request.args) - allowed or any(len(request.args.getlist(k)) != 1 for k in request.args):
        raise MarketplaceFinanceValidationError('Неизвестные или повторяющиеся параметры запроса')


def _compact_view():
    value = request.args.get('view')
    if value not in (None, 'compact'):
        raise MarketplaceFinanceValidationError('Неизвестное представление списка')
    return value == 'compact'


def _optional_positive_query(name: str):
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    return _positive_query(name)


def _body(allowed: set) -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise MarketplaceFinanceValidationError("JSON body должен быть объектом")
    if "account_id" in payload or "marketplace" in payload:
        raise MarketplaceFinanceValidationError(
            "Marketplace scope задаётся только query-параметрами"
        )
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise MarketplaceFinanceValidationError(
            "Неизвестные поля: " + ", ".join(unknown)
        )
    return payload


def _strict_bool(value, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise MarketplaceFinanceValidationError(
            f"{field_name} должен быть boolean"
        )
    return value


def _accounts(seller_id: int) -> list:
    return [
        account
        for account in MarketplaceAccountService.list_accounts(
            seller_id=seller_id,
            marketplace_code="ozon",
        )
        if account.is_active
    ]


def _selected_account(seller_id: int, accounts: list):
    raw = request.args.get("account_id")
    if raw is None:
        return accounts[0] if accounts else None
    return MarketplaceAccountService.get_owned_account(
        seller_id=seller_id,
        account_id=_positive_query("account_id"),
        marketplace_code="ozon",
    )


def _known_error(exc):
    return jsonify({
        "error": str(exc),
        "code": getattr(exc, "code", "marketplace_finance_error"),
    }), getattr(exc, "status_code", 400)


@marketplace_finance_bp.get('/marketplaces/finance/changes')
@login_required
def changes_page():
    if not _feature_enabled():
        return jsonify({'error': 'Поддержка Ozon выключена'}), 404
    try:
        _validate_query({'account_id', 'older', 'newer', 'kind', 'page'})
        account = MarketplaceAccountService.get_owned_account(
            seller_id=_seller_id(), account_id=_positive_query('account_id'), marketplace_code='ozon')
        response = make_response(render_template('marketplace_finance_changes.html', account_id=account.id, account_label=account.label))
        response.headers['Cache-Control'] = 'private, no-store'
        return response
    except (MarketplaceFinanceError, MarketplaceAccountError) as exc:
        return _known_error(exc)


def _comparison_response(action):
    if not _feature_enabled():
        return jsonify({'error': 'Поддержка Ozon выключена'}), 404
    try:
        from services.marketplace_finance_comparison import history, compare
        if action == 'history':
            _validate_query({'account_id', 'anchor_id'})
            data = history(seller_id=_seller_id(), account_id=_positive_query('account_id'),
                anchor_id=_positive_query('anchor_id') if 'anchor_id' in request.args else None)
        else:
            _validate_query({'account_id', 'older', 'newer', 'kind', 'page', 'per_page'})
            data = compare(seller_id=_seller_id(), account_id=_positive_query('account_id'),
                older_id=_positive_query('older'), newer_id=_positive_query('newer'),
                kind=request.args.get('kind', ''), page=_positive_query('page', 1, maximum=100_000),
                per_page=_positive_query('per_page', 50, maximum=100))
        response = jsonify({'success': True, 'data': data})
    except (MarketplaceFinanceError, MarketplaceAccountError) as exc:
        response = make_response(_known_error(exc))
    except Exception as exc:
        logger.warning('Ozon finance comparison unavailable: %s', type(exc).__name__)
        response = make_response(jsonify({'error': 'Не удалось сравнить загрузки. Повторите позже.'}), 503)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@marketplace_finance_bp.get('/marketplaces/api/finance/history')
@login_required
def history_api():
    return _comparison_response('history')


@marketplace_finance_bp.get('/marketplaces/api/finance/changes')
@login_required
def changes_api():
    return _comparison_response('changes')


@marketplace_finance_bp.get("/marketplaces/finance")
@login_required
def page():
    if not _feature_enabled():
        return redirect(url_for("finances_page"))
    try:
        seller_id = _seller_id()
        accounts = _accounts(seller_id)
        if not accounts:
            return redirect(url_for("marketplace_accounts.index"))
        selected = _selected_account(seller_id, accounts)
        return render_template(
            "marketplace_finance.html",
            ozon_accounts=[account.to_public_dict() for account in accounts],
            selected_account=selected.to_public_dict(),
        )
    except (MarketplaceFinanceError, MarketplaceAccountError):
        return redirect(url_for("marketplace_accounts.index"))


@marketplace_finance_bp.get("/marketplaces/api/finance")
@login_required
def list_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({'account_id', 'page', 'per_page', 'period', 'category', 'sign', 'type_id', 'search', 'view', 'snapshot_id', 'as_of'})
        data = MarketplaceFinanceService.list_facts(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            page=_positive_query("page", 1, maximum=100_000),
            per_page=_positive_query("per_page", 50, maximum=100),
            period_code=request.args.get("period", "30d"),
            category=request.args.get("category") or None,
            amount_sign=request.args.get("sign") or None,
            type_id=_optional_positive_query("type_id"),
            search=request.args.get("search", ""),
            compact=_compact_view(),
            snapshot_id=_optional_positive_query('snapshot_id'),
            as_of=request.args.get('as_of'),
        )
        return jsonify({"success": True, "data": data})
    except (MarketplaceFinanceError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon finance list failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось загрузить финансы Ozon"}), 500


@marketplace_finance_bp.get("/marketplaces/api/finance/<int:fact_id>")
@login_required
def detail_api(fact_id):
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({'account_id', 'view', 'item_page', 'component_page', 'per_page'})
        compact = _compact_view()
        account_id = _positive_query('account_id')
        item_page = _positive_query('item_page', 1, maximum=100_000)
        component_page = _positive_query('component_page', 1, maximum=100_000)
        per_page = _positive_query('per_page', 50, maximum=100)
        fact = MarketplaceFinanceService.get_fact(
            seller_id=_seller_id(),
            account_id=account_id,
            fact_id=fact_id,
        )
        from services.marketplace_finance_display import fact_detail, fact_previews
        account = MarketplaceFinanceService._owned_account(seller_id=_seller_id(), account_id=account_id)
        data = (fact_detail(fact, account=account, item_page=item_page, component_page=component_page, per_page=per_page)
                if compact else fact_previews([fact], account=account)[0])
        return jsonify({"success": True, "data": data})
    except (MarketplaceFinanceError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon finance detail failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось загрузить начисление Ozon"}), 500


@marketplace_finance_bp.get("/marketplaces/api/finance/export.xlsx")
@login_required
def export_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({'account_id', 'snapshot_id', 'as_of', 'period', 'category', 'sign', 'type_id', 'search'})
        account_id = _positive_query('account_id')
        snapshot_id = _positive_query('snapshot_id')
        from services.marketplace_finance_export import build_workbook, MIME
        payload, filename = build_workbook(
            seller_id=_seller_id(), account_id=account_id, snapshot_id=snapshot_id,
            as_of=request.args.get('as_of'), period_code=request.args.get('period', '30d'),
            category=request.args.get('category') or None, amount_sign=request.args.get('sign') or None,
            type_id=_optional_positive_query('type_id'), search=request.args.get('search', ''),
        )
        response = make_response(payload)
        response.headers.update({
            'Content-Type': MIME, 'Content-Disposition': f'attachment; filename="{filename}"',
            'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Finance-Account-Id': str(account_id), 'X-Finance-Snapshot-Id': str(snapshot_id),
            'X-Finance-As-Of': request.args['as_of'], 'X-Finance-Period': request.args.get('period', '30d'),
        })
        return response
    except (MarketplaceFinanceError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon finance export failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось подготовить файл. Попробуйте сократить период или повторите позже."}), 500


@marketplace_finance_bp.get("/marketplaces/api/finance/sync")
@login_required
def sync_status_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({'account_id', 'period'})
        result = read_status(seller_id=_seller_id(), account_id=_positive_query('account_id'),
                             domain='finance', period_code=request.args.get('period', '30d'))
        return jsonify({"success": True, "data": result})
    except (MarketplaceFinanceError, MarketplaceAccountError) as exc:
        return _known_error(exc)


@marketplace_finance_bp.post("/marketplaces/api/finance/sync")
@login_required
def sync_api():
    if not _feature_enabled():
        return jsonify({"error": "Поддержка Ozon выключена"}), 404
    try:
        _validate_query({'account_id'})
        payload = _body({"period", "force", "max_pages"})
        force = _strict_bool(payload.get("force", False), "force")
        max_pages = payload.get("max_pages", 5)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool):
            raise MarketplaceFinanceValidationError(
                "max_pages должен быть целым числом"
            )
        MarketplaceFinanceService._positive_integer(max_pages, "max_pages", maximum=MarketplaceFinanceService.MAX_PAGES_PER_CALL)
        result = enqueue_read(
            seller_id=_seller_id(),
            account_id=_positive_query("account_id"),
            period_code=payload.get("period", "30d"),
            force=force,
            domain='finance',
        )
        return jsonify({"success": True, "data": result}), (202 if result["active"] else 200)
    except (MarketplaceFinanceError, MarketplaceAccountError) as exc:
        return _known_error(exc)
    except Exception as exc:
        logger.exception("Ozon finance sync failed: %s", type(exc).__name__)
        return jsonify({"error": "Не удалось синхронизировать финансы Ozon"}), 500


def register_marketplace_finance_routes(app):
    app.register_blueprint(marketplace_finance_bp)
