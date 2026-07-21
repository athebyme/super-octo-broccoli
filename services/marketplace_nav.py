"""Глобальный UI-контекст каналов маркетплейсов (shell-слой).

Регистрирует Jinja-глобальную функцию `mp_nav()`: включён ли Ozon и список
активных Ozon-кабинетов текущего продавца. Из неё макрос `channel_bar`
(templates/macros/components.html) строит переключатель каналов на
страницах-концептах (качество, цены, аналитика, финансы, отзывы...).

Именно глобальная функция, а не context processor: переменные context
processor не видны внутри макросов, импортированных без `with context`,
а глобалы Jinja-среды видны всегда — channel_bar работает при любом
способе импорта.

Инварианты:
- один SELECT на HTTP-запрос (кэш в flask.g), только для авторизованного seller;
- наружу уходят только id/label/is_default — никаких credentials и статусов
  подключения;
- при выключенном MARKETPLACE_OZON_ENABLED или любой ошибке чтения shell
  остаётся рабочим: ozon_enabled=False/accounts=[] и channel bar просто
  не рендерится.
"""

from flask import g, has_request_context, request, session
from flask_login import current_user

_EMPTY = {'ozon_enabled': False, 'accounts': [], 'last_account_id': None}

_LAST_ACCOUNT_SESSION_KEY = 'mp_last_ozon_account_id'


def _load_ozon_accounts(seller_id):
    from services.marketplace_accounts import MarketplaceAccountService

    accounts = MarketplaceAccountService.list_accounts(
        seller_id=seller_id,
        marketplace_code='ozon',
    )
    return [
        {
            'id': account.id,
            'label': account.label,
            'is_default': bool(account.is_default),
        }
        for account in accounts
        if account.is_active
    ]


def prime_ozon_accounts_cache(accounts):
    """Роут с уже загруженными Ozon-кабинетами seller-а может отдать их mp_nav,
    чтобы channel bar не выполнял второй идентичный SELECT в том же запросе."""
    if not has_request_context():
        return
    g._mp_nav_ozon_accounts = [
        {
            'id': account.id,
            'label': account.label,
            'is_default': bool(account.is_default),
        }
        for account in accounts
        if account.is_active
    ]


def register_marketplace_nav(app):
    @app.before_request
    def _remember_ozon_account():
        """Запоминает последний явно выбранный Ozon-кабинет.

        Контекст кабинета «липнет» между разделами: ссылки сайдбара на
        Ozon-страницы получают account_id последнего просмотренного кабинета,
        чтобы переход «Финансы кабинета B → Заказы» не уводил молча на
        кабинет по умолчанию.
        """
        if not app.config.get('MARKETPLACE_OZON_ENABLED'):
            return
        if not request.path.startswith('/marketplaces'):
            return
        raw = request.args.get('account_id')
        if raw and isinstance(raw, str) and raw.isdigit():
            account_id = int(raw)
            if session.get(_LAST_ACCOUNT_SESSION_KEY) != account_id:
                session[_LAST_ACCOUNT_SESSION_KEY] = account_id

    def mp_nav():
        if not app.config.get('MARKETPLACE_OZON_ENABLED'):
            return dict(_EMPTY)
        if not has_request_context():
            return dict(_EMPTY)
        if not getattr(current_user, 'is_authenticated', False):
            return dict(_EMPTY)
        seller = getattr(current_user, 'seller', None)
        if seller is None:
            return dict(_EMPTY)

        accounts = getattr(g, '_mp_nav_ozon_accounts', None)
        if accounts is None:
            try:
                accounts = _load_ozon_accounts(seller.id)
            except Exception:  # shell не должен ронять рендер любой страницы
                app.logger.warning(
                    'mp_nav: не удалось загрузить Ozon-кабинеты', exc_info=True
                )
                accounts = []
            g._mp_nav_ozon_accounts = accounts

        # Липкий кабинет валиден только среди активных кабинетов текущего
        # seller — чужое/устаревшее значение из session не всплывает в UI.
        last_account_id = session.get(_LAST_ACCOUNT_SESSION_KEY)
        if not any(acc['id'] == last_account_id for acc in accounts):
            last_account_id = None

        return {
            'ozon_enabled': True,
            'accounts': accounts,
            'last_account_id': last_account_id,
        }

    app.jinja_env.globals['mp_nav'] = mp_nav
