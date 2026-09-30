"""Small, credential-free account audit written in the caller's transaction."""
import json

from models import db, MarketplaceAccountEvent, Seller, User

ACTIONS = {
    'connected': 'Магазин подключён',
    'key_replaced': 'Ключ заменён',
    'settings_changed': 'Настройки сохранены',
    'default_changed': 'Основной магазин изменён',
    'disconnected': 'Магазин отключён',
}
FIELDS = {'label': 'Название магазина', 'default_vat': 'НДС новых карточек',
          'is_default': 'Основной магазин', 'is_active': 'Подключение'}
VATS = {'0': 'Без НДС', '0.05': '5%', '0.07': '7%', '0.1': '10%', '0.10': '10%',
        '0.2': '20%', '0.20': '20%', '0.22': '22%'}


def validate_actor(seller_id, actor_user_id):
    # None is an explicitly unrecorded internal caller, never an invented user.
    if actor_user_id is None:
        return None
    from services.marketplace_accounts import MarketplaceAccountValidationError
    if type(actor_user_id) is not int or actor_user_id <= 0:
        raise MarketplaceAccountValidationError('Не удалось подтвердить инициатора изменения.')
    owner = db.session.get(Seller, seller_id)
    actor = db.session.get(User, actor_user_id)
    if owner is None or actor is None or owner.user_id != actor.id or not actor.is_active:
        raise MarketplaceAccountValidationError('Инициатор изменения не относится к этому продавцу.')
    return actor.id


def snapshot(account):
    return {'version': account.version or 0, 'credential_version': account.credential_version or 0,
            'label': account.label if isinstance(account.label, str) and len(account.label) <= 120 else None,
            'default_vat': account.public_settings['default_vat'],
            'is_default': bool(account.is_default), 'is_active': bool(account.is_active)}


def append_event(account, before, action, actor_user_id):
    """No commit, decrypt, provider call or arbitrary metadata acceptance."""
    if action not in ACTIONS:
        raise ValueError('unknown_account_event')
    actor_user_id = validate_actor(account.seller_id, actor_user_id)
    after = snapshot(account)
    if after['version'] <= before.get('version', 0):
        raise ValueError('account_event_version_not_advanced')
    changes = {field: {'before': before.get(field), 'after': after[field]}
               for field in FIELDS if before.get(field) != after[field]}
    db.session.add(MarketplaceAccountEvent(
        seller_id=account.seller_id, marketplace_id=account.marketplace_id, account_id=account.id,
        actor_user_id=actor_user_id, action=action,
        account_version_before=before.get('version', 0), account_version_after=after['version'],
        credential_version_before=before.get('credential_version', 0),
        credential_version_after=after['credential_version'],
        changes_json=json.dumps(changes, ensure_ascii=False, separators=(',', ':')),
    ))


def _display(field, value):
    if field == 'label':
        return value if isinstance(value, str) and len(value) <= 120 else 'Не указано'
    if field == 'default_vat':
        return VATS.get(value, 'Не задан') if isinstance(value, str) else 'Не задан'
    if type(value) is not bool:
        return 'Не указано'
    if field == 'is_active':
        return 'Подключён' if value else 'Отключён'
    return 'Да' if value else 'Нет'


def history_page(*, seller_id, account_id, viewer_user_id, before_id=None):
    from services.marketplace_accounts import MarketplaceAccountService, MarketplaceAccountValidationError
    account = MarketplaceAccountService.get_owned_account(
        seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
    if before_id is not None and (type(before_id) is not int or not 0 < before_id <= 2**63-1):
        raise MarketplaceAccountValidationError('Некорректная страница истории.')
    query = MarketplaceAccountEvent.query.filter_by(seller_id=seller_id,
        marketplace_id=account.marketplace_id, account_id=account.id)
    if before_id is not None:
        query = query.filter(MarketplaceAccountEvent.id < before_id)
    rows = query.order_by(MarketplaceAccountEvent.id.desc()).limit(31).all()
    items = []
    for row in rows[:30]:
        try:
            changes = json.loads(row.changes_json) if isinstance(row.changes_json, str) and len(row.changes_json) <= 8192 else {}
        except (ValueError, TypeError):
            changes = {}
        if not isinstance(changes, dict):
            changes = {}
        details = []
        for field, label in FIELDS.items():
            change = changes.get(field)
            if isinstance(change, dict) and set(change) == {'before', 'after'}:
                details.append({'field': field, 'label': label,
                    'before': _display(field, change['before']), 'after': _display(field, change['after'])})
        items.append({'id': row.id, 'action': row.action if row.action in ACTIONS else 'unknown',
            'title': ACTIONS.get(row.action, 'Изменение магазина'),
            'actor': 'Инициатор не записан' if row.actor_user_id is None else 'Вы' if row.actor_user_id == viewer_user_id else 'Другой пользователь',
            'created_at': row.created_at.isoformat() + 'Z', 'changes': details,
            'hint': 'Ключ сохранён. Доступ проверяется отдельно.' if row.action in {'connected','key_replaced'} else
                    'Каталог и история сохранены.' if row.action == 'disconnected' else ''})
    return {'account_id': account.id, 'marketplace_code': 'ozon', 'items': items,
            'next_before_id': rows[29].id if len(rows) > 30 else None,
            'history_scope': 'changes_recorded_after_feature_release'}
