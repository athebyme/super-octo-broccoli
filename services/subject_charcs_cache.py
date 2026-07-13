# -*- coding: utf-8 -*-
"""Кэш конфигов характеристик категорий WB (таблица wb_subject_charcs_cache).

Хранит нормализованный список [{'name','required'}] на subject_id с TTL 7 дней.
Чтение — без WB; обновление — лениво, во время синка рейтингов.
"""
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger('subject_charcs_cache')

CHARCS_TTL_DAYS = 7


def get_available_charcs(subject_id):
    """Список [{'name','required'}] из кэша; None, если конфига нет/битый."""
    if not subject_id:
        return None
    from models import db, WbSubjectCharcsCache
    row = db.session.get(WbSubjectCharcsCache, int(subject_id))
    if not row or not row.charcs_json:
        return None
    try:
        data = json.loads(row.charcs_json)
    except (ValueError, TypeError):
        return None
    return data or None


def refresh_subject_charcs(wb_client, subject_ids, force=False):
    """Обновить кэш для протухших/отсутствующих subject_id.

    Ошибки WB не пробрасываются (warning в лог) — скоринг деградирует
    в fallback без конфига. Коммитит сессию сама.
    """
    from models import db, WbSubjectCharcsCache
    now = datetime.utcnow()
    cutoff = now - timedelta(days=CHARCS_TTL_DAYS)
    changed = False
    for sid in {int(s) for s in (subject_ids or []) if s}:
        row = db.session.get(WbSubjectCharcsCache, sid)
        if row and not force and row.fetched_at and row.fetched_at > cutoff:
            continue
        try:
            resp = wb_client.get_card_characteristics_config(sid)
        except Exception as e:
            logger.warning('charcs config fetch failed subject_id=%s: %s', sid, e)
            continue
        charcs = [
            {'name': c.get('name'), 'required': bool(c.get('required'))}
            for c in (resp or {}).get('data') or []
            if isinstance(c, dict) and c.get('name')
        ]
        if row is None:
            row = WbSubjectCharcsCache(subject_id=sid)
            db.session.add(row)
        row.charcs_json = json.dumps(charcs, ensure_ascii=False)
        row.fetched_at = now
        changed = True
    if changed:
        db.session.commit()
