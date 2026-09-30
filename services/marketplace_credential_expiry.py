"""One local expiry projection; a future date never certifies API access."""
from datetime import datetime, timedelta, timezone
import math

WINDOWS = ((1, 3), (7, 2), (14, 1))


def expiry_notice(expires_at, *, active=True, now=None):
    now = now or datetime.utcnow()
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    result = {'state': 'unknown', 'stage': 0, 'needs_attention': False,
              'expires_at': None, 'days_remaining': None,
              'label': 'Срок ключа не подтверждён',
              'message': 'Дата окончания не получена. Это не означает бессрочный доступ.',
              'hint': 'Дата окончания не получена. Это не означает бессрочный доступ.'}
    if not active:
        return {**result, 'state': 'inactive', 'label': 'Ключ не используется',
                'message': 'Подключите магазин, чтобы проверить доступ.',
                'hint': 'Подключите магазин, чтобы проверить доступ.'}
    if not isinstance(expires_at, datetime):
        return result
    if expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
    remaining = expires_at - now
    stage = 4 if remaining <= timedelta(0) else next(
        (rank for days, rank in WINDOWS if remaining <= timedelta(days=days)), 0)
    date = expires_at.strftime('%d.%m.%Y %H:%M UTC')
    message = (f'Указанный срок закончился {date}. Замените ключ; товары и история сохранены.'
               if stage == 4 else
               f'Указанный срок — {date}. Замените ключ заранее, чтобы обновления не остановились.'
               if stage else f'Указанный срок — {date}. Доступ и права проверяются отдельно.')
    return {**result, 'state': 'expired' if stage == 4 else 'expiring' if stage else 'scheduled',
            'stage': stage, 'needs_attention': stage > 0,
            'expires_at': expires_at.isoformat() + 'Z',
            'days_remaining': max(0, math.ceil(remaining.total_seconds() / 86400)),
            'label': {4: 'Срок ключа истёк', 3: 'До окончания срока не больше суток',
                      2: 'До окончания срока не больше 7 дней',
                      1: 'До окончания срока не больше 14 дней',
                      0: 'Срок ключа указан' }[stage],
            'message': message,
            'hint': ('Замените ключ; сохранённые товары и история доступны.' if stage == 4 else
                     'Замените ключ заранее, чтобы обновления не остановились.' if stage else
                     'Указанная дата не подтверждает доступ и права ключа.')}
