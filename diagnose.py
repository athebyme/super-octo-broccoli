#!/usr/bin/env python3
"""
Диагностика проблем с AI-кнопками и фотографиями.
Запуск: docker exec seller-platform python /app/diagnose.py
"""
import sqlite3
import os
import json
import sys
import hashlib
import requests

DB_PATH = os.environ.get('DIAGNOSE_DB', '/app/data/seller_platform.db')
CLOUDRU_API_URL = "https://foundation-models.api.cloud.ru/v1/chat/completions"
CLOUDRU_IAM_URL = "https://iam.api.cloud.ru/api/v1/auth/token"

SEP  = "=" * 70
SEP2 = "-" * 70

def mask(val, show=4):
    if not val:
        return None
    s = str(val)
    if len(s) <= show:
        return "***"
    return s[:show] + "..." + f"[{len(s)} chars]"

def tail4(val):
    """Последние 4 символа (для сравнения паролей без раскрытия)"""
    if not val:
        return "(пусто)"
    return "..." + str(val)[-4:]

def yn(val):
    return "✅ ДА" if val else "❌ НЕТ"

def test_cloudru_key(api_key: str, model: str = "openai/gpt-oss-120b") -> tuple:
    """
    Проверяет API-ключ Cloud.ru минимальным запросом.
    Возвращает (ok: bool, detail: str)
    """
    try:
        # Определяем тип ключа и заголовок
        if ':' in api_key and '.' not in api_key:
            # Ключ доступа - нужен IAM обмен
            parts = api_key.split(':', 1)
            key_id, secret = parts[0], parts[1]
            r = requests.post(
                CLOUDRU_IAM_URL,
                json={"keyId": key_id, "secret": secret},
                timeout=10
            )
            if r.status_code != 200:
                return False, f"IAM обмен токена провалился: HTTP {r.status_code} — {r.text[:200]}"
            token = r.json().get('token')
            if not token:
                return False, f"IAM ответил 200, но token отсутствует: {r.text[:200]}"
            auth_header = f"Bearer {token}"
        else:
            # Прямой API-ключ
            auth_header = f"Bearer {api_key}"

        # Минимальный тестовый запрос
        r = requests.post(
            CLOUDRU_API_URL,
            headers={
                "Authorization": auth_header,
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
            },
            timeout=15,
        )
        if r.status_code == 200:
            return True, f"HTTP 200 OK"
        elif r.status_code == 401:
            return False, f"HTTP 401 — ключ невалиден или истёк"
        elif r.status_code == 403:
            return False, f"HTTP 403 — нет прав на модель {model}"
        else:
            return False, f"HTTP {r.status_code} — {r.text[:200]}"
    except requests.Timeout:
        return False, "Таймаут соединения (15с) — возможно недоступен снаружи Docker"
    except Exception as e:
        return False, f"Ошибка: {e}"


con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row
cur = con.cursor()

print(SEP)
print("ДИАГНОСТИКА: AI КНОПКИ И ФОТОГРАФИИ")
print(f"База данных: {DB_PATH}")
print(SEP)

# ─── 1. Список продавцов и настройки AI ───────────────────────────────────────
print("\n[1] ПРОДАВЦЫ И НАСТРОЙКИ AI\n" + SEP2)

cur.execute("""
    SELECT
        s.id          AS seller_id,
        s.company_name,
        u.username,
        -- AI settings
        ais.ai_enabled,
        ais.ai_provider,
        ais.ai_api_key,
        ais.ai_client_id,
        ais.ai_client_secret,
        ais.ai_model,
        -- Sexoptovik
        ais.sexoptovik_login,
        ais.sexoptovik_password,
        CASE WHEN ais.id IS NULL THEN 0 ELSE 1 END AS has_settings
    FROM sellers s
    JOIN users u ON u.id = s.user_id
    LEFT JOIN auto_import_settings ais ON ais.seller_id = s.id
    ORDER BY s.id
""")

rows = cur.fetchall()

for r in rows:
    has_api_key = bool(r['ai_api_key'])
    has_oauth   = bool(r['ai_client_id']) and bool(r['ai_client_secret'])

    ai_enabled_update_page = (r['has_settings'] and bool(r['ai_enabled']) and bool(r['ai_api_key']))
    has_ai_key_detail_page = bool(r['has_settings'] and (r['ai_api_key'] or (r['ai_client_id'] and r['ai_client_secret'])))
    endpoint_ok            = bool(r['has_settings'] and r['ai_enabled'] and r['ai_api_key'])

    print(f"Продавец #{r['seller_id']} — {r['company_name']} (@{r['username']})")
    print(f"  ai_enabled:    {yn(r['ai_enabled'])}   ai_provider: {r['ai_provider'] or '—'}   model: {r['ai_model'] or '—'}")
    print(f"  ai_api_key:    {'✅ ' + mask(r['ai_api_key']) if has_api_key else '❌ ПУСТО'}")
    print(f"  client_id:     {'✅ ' + mask(r['ai_client_id']) if r['ai_client_id'] else '❌ ПУСТО'}")
    print(f"  client_secret: {'✅ задан' if r['ai_client_secret'] else '❌ ПУСТО'}")
    print(f"  sexoptovik:    логин={r['sexoptovik_login'] or '❌'}  пароль_хвост={tail4(r['sexoptovik_password'])}")
    print()
    print(f"  Страница AI-обновления (runAIBtn):   {yn(ai_enabled_update_page)}")
    print(f"  Деталь товара (has_ai_key):          {yn(has_ai_key_detail_page)}")
    print(f"  Эндпоинт /ai-process примет запрос: {yn(endpoint_ok)}")

    if has_oauth and not has_api_key:
        print(f"  ⚠️  КОНФИГ-БАГ: Cloud.ru OAuth задан, но ai_api_key ПУСТ")
        print(f"     Страница /ai-update и /ai-process требуют api_key — они сломаны!")

    # Тест реального подключения к Cloud.ru
    if r['ai_api_key']:
        model = r['ai_model'] or 'openai/gpt-oss-120b'
        print(f"\n  Тест Cloud.ru API ({model})...", end=' ', flush=True)
        ok, detail = test_cloudru_key(r['ai_api_key'], model)
        if ok:
            print(f"✅ {detail}")
        else:
            print(f"❌ {detail}")
    else:
        print(f"\n  Тест Cloud.ru API: пропущен (нет ключа)")

    print()

# ─── 2. Товары по статусам ─────────────────────────────────────────────────────
print("\n[2] ТОВАРЫ ПО СТАТУСАМ\n" + SEP2)
print("(страница AI-обновления показывает только pending / validated / failed)\n")

cur.execute("""
    SELECT s.id AS seller_id, s.company_name, ip.import_status, COUNT(*) AS cnt
    FROM imported_products ip
    JOIN sellers s ON s.id = ip.seller_id
    GROUP BY s.id, ip.import_status
    ORDER BY s.id, ip.import_status
""")
status_rows = cur.fetchall()

if not status_rows:
    print("⚠️  Нет импортированных товаров")
else:
    from collections import defaultdict
    by_seller = defaultdict(lambda: defaultdict(int))
    names = {}
    for r in status_rows:
        by_seller[r['seller_id']][r['import_status']] = r['cnt']
        names[r['seller_id']] = r['company_name']

    for sid, statuses in by_seller.items():
        visible = sum(v for k, v in statuses.items() if k in ('pending', 'validated', 'failed'))
        print(f"Продавец #{sid} — {names[sid]}")
        for st, cnt in sorted(statuses.items()):
            m = "👁" if st in ('pending', 'validated', 'failed') else " "
            print(f"  {m} {st:<20} {cnt}")
        print(f"  → На странице AI-обновления: {visible} товаров", end="")
        print("  ⚠️  СПИСОК ПУСТ — кнопка «Запустить» вечно серая!" if visible == 0 else "")
        print()

# ─── 3. Битый JSON в товарах (ломает рендер шаблона) ─────────────────────────
print("\n[3] БИТЫЙ JSON В ТОВАРАХ (ломает рендер страницы)\n" + SEP2)

cur.execute("""
    SELECT seller_id, id, photo_urls, characteristics, sizes, colors, materials
    FROM imported_products
""")

bad = []
for r in cur.fetchall():
    fields = {
        'photo_urls': r['photo_urls'],
        'characteristics': r['characteristics'],
        'sizes': r['sizes'],
        'colors': r['colors'],
        'materials': r['materials'],
    }
    for fname, val in fields.items():
        if val:
            try:
                json.loads(val)
            except (json.JSONDecodeError, TypeError):
                bad.append((r['seller_id'], r['id'], fname, str(val)[:60]))

if not bad:
    print("✅ Битых JSON-полей не найдено — шаблоны рендерятся корректно")
else:
    print(f"❌ Найдено {len(bad)} товаров с битым JSON:")
    by_seller_bad = defaultdict(list)
    for sid, pid, fname, sample in bad:
        by_seller_bad[sid].append((pid, fname, sample))
    for sid, items in by_seller_bad.items():
        print(f"\n  Продавец #{sid}: {len(items)} проблемных записей")
        for pid, fname, sample in items[:5]:
            print(f"    product_id={pid}  поле={fname}  значение: {sample!r}")
        if len(items) > 5:
            print(f"    ... и ещё {len(items) - 5}")
    print()
    print("  ⚠️  Битый JSON в photo_urls ломает строки таблицы на AI-обновлении")
    print("     Если страница не рендерится → кнопки не появляются вообще")

# ─── 4. Итог ───────────────────────────────────────────────────────────────────
print("\n" + SEP)
print("ИТОГ")
print(SEP)
print("Смотри разделы выше. Ключевые точки:")
print("1. Тест Cloud.ru API [1] — если ❌, ключ протух → нужно обновить в настройках")
print("2. Битый JSON [3]        — если ❌, шаблон не рендерится → JS не грузится → кнопки мертвы")
print("3. Товары на странице [2]— если 0, кнопка «Запустить» вечно серая (нужно выбрать операции И товары)")
print("4. Хвосты паролей sexoptovik в [1] — если они разные, у одного тенанта неверный пароль")
print(SEP)

con.close()
