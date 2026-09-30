"""A seller-facing observation surface; GET never refreshes Ozon or changes jobs."""
from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from services.marketplace_accounts import MarketplaceAccountError, MarketplaceAccountService
from services.ozon_account_health import HealthError, HealthNotFound, observe_account

bp=Blueprint('ozon_account_health',__name__)


@bp.after_request
def no_store(response):
    response.headers['Cache-Control']='private, no-store'
    return response


def scope():
    seller=getattr(current_user,'seller',None)
    raw=request.args.get('account_id','')
    if not seller:
        raise HealthNotFound('Нет привязки к продавцу.')
    if set(request.args)!={'account_id'} or len(request.args.getlist('account_id'))!=1 or not raw.isascii() or not raw.isdecimal() or raw.startswith('0') or len(raw)>18:
        error=HealthError('Выберите магазин в настройках подключения.');error.status_code=400;error.code='invalid_account_scope';raise error
    return seller.id,int(raw)


def error_response(error):
    return jsonify({'success':False,'error':str(error),'code':error.code}),error.status_code


@bp.get('/marketplaces/status')
@login_required
def page():
    try:
        seller_id,account_id=scope()
        account=MarketplaceAccountService.get_owned_account(seller_id=seller_id,account_id=account_id,marketplace_code='ozon')
        return render_template('ozon_account_health.html',account_id=account.id,account_label=account.label)
    except (HealthError,MarketplaceAccountError) as error:
        return error_response(error)


@bp.get('/marketplaces/api/status')
@login_required
def api():
    try:
        seller_id,account_id=scope()
        return jsonify({'success':True,'data':observe_account(seller_id=seller_id,account_id=account_id,config=current_app.config)})
    except HealthError as error:
        return error_response(error)
    except Exception as error:
        current_app.logger.warning('Ozon account health unavailable: %s',type(error).__name__)
        return error_response(HealthError('Не удалось проверить состояние. Сохранённые данные доступны в разделах магазина.'))


def register_ozon_account_health_routes(app):
    app.register_blueprint(bp)
