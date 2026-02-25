#!/usr/bin/env python3
"""
Диагностика проблем с AI-кнопками и фотографиями.
Запуск: docker exec seller-platform python /app/diagnose.py
"""
import sqlite3
import os

DB_PATH = os.environ.get('DIAGNOSE_DB', '/app/data/seller_platform.db')

SEP = "=" * 70
SEP2 = "-" * 70

def mask(val, show=4):
    """Маскировать чувствительные данные, оставить первые N символов"""
    if not val:
        return None
    s = str(val)
    if len(s) <= show:
        return "***"
    return s[:show] + "..." + f"[{len(s)} chars]"

def yn(val):
    return "✅ ДА" if val else "❌ НЕТ"

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row
cur = con.cursor()

print(SEP)
print("ДИАГНОСТИКА: AI КНОПКИ И ФОТОГРАФИИ")
print(f"База данных: {DB_PATH}")
print(SEP)

# ─── 1. Список продавцов и их AI-настройки ────────────────────────────────────
print("\n[1] ПРОДАВЦЫ И НАСТРОЙКИ AI\n" + SEP2)

cur.execute("""
    SELECT
        s.id          AS seller_id,
        s.company_name,
        u.username,
        u.email,
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
        -- Присутствие записи
        CASE WHEN ais.id IS NULL THEN 0 ELSE 1 END AS has_settings
    FROM sellers s
    JOIN users u ON u.id = s.user_id
    LEFT JOIN auto_import_settings ais ON ais.seller_id = s.id
    ORDER BY s.id
""")

rows = cur.fetchall()

if not rows:
    print("⚠️  Продавцы не найдены!")
else:
    for r in rows:
        has_api_key = bool(r['ai_api_key'])
        has_oauth = bool(r['ai_client_id']) and bool(r['ai_client_secret'])

        # Симулируем проверку из auto_import_ai_update (строка 1219)
        ai_enabled_update_page = (
            r['has_settings'] and
            bool(r['ai_enabled']) and
            bool(r['ai_api_key'])
        )
        # Симулируем проверку из auto_import_product_detail (строка 590)
        has_ai_key_detail_page = bool(
            r['has_settings'] and (r['ai_api_key'] or (r['ai_client_id'] and r['ai_client_secret']))
        )
        # Симулируем проверку из auto_import_ai_process_single (строка 1261) - эндпоинт
        endpoint_ok = bool(
            r['has_settings'] and r['ai_enabled'] and r['ai_api_key']
        )

        print(f"Продавец #{r['seller_id']} — {r['company_name']} (@{r['username']})")
        print(f"  Запись auto_import_settings:   {yn(r['has_settings'])}")
        print(f"  ai_enabled (DB):               {yn(r['ai_enabled'])}")
        print(f"  ai_provider:                   {r['ai_provider'] or '(не указан)'}")
        print(f"  ai_model:                      {r['ai_model'] or '(не указан)'}")
        print(f"  ai_api_key:                    {'✅ ' + mask(r['ai_api_key']) if has_api_key else '❌ ПУСТО'}")
        print(f"  ai_client_id (OAuth):          {'✅ ' + mask(r['ai_client_id']) if r['ai_client_id'] else '❌ ПУСТО'}")
        print(f"  ai_client_secret (OAuth):      {'✅ ' + mask(r['ai_client_secret']) if r['ai_client_secret'] else '❌ ПУСТО'}")
        print(f"  sexoptovik_login:              {r['sexoptovik_login'] or '❌ ПУСТО'}")
        print(f"  sexoptovik_password:           {'✅ задан' if r['sexoptovik_password'] else '❌ ПУСТО'}")
        print()
        print(f"  ┌── РЕЗУЛЬТАТЫ ПРОВЕРОК ──────────────────────────────────")
        print(f"  │ ai_enabled (страница AI-обновления):  {yn(ai_enabled_update_page)}")
        print(f"  │   → кнопка Запустить будет {'активна' if ai_enabled_update_page else 'СЕРОЙ (показан баннер «AI не настроен»)'}")
        print(f"  │ has_ai_key (деталь товара):           {yn(has_ai_key_detail_page)}")
        print(f"  │ Эндпоинт /ai-process примет запрос:  {yn(endpoint_ok)}")
        if has_oauth and not has_api_key:
            print(f"  │ ⚠️  ПРОБЛЕМА: Cloud.ru OAuth настроен, но ai_api_key ПУСТ!")
            print(f"  │    Страница AI-обновления показывает 'AI не настроен'")
            print(f"  │    Эндпоинт /ai-process вернёт 400 ошибку")
        print(f"  └─────────────────────────────────────────────────────────")
        print()

# ─── 2. Кол-во товаров по статусам для каждого продавца ───────────────────────
print("\n[2] ТОВАРЫ ПО СТАТУСАМ (страница AI-обновления показывает только pending/validated/failed)\n" + SEP2)

cur.execute("""
    SELECT
        s.id AS seller_id,
        s.company_name,
        ip.import_status,
        COUNT(*) AS cnt
    FROM imported_products ip
    JOIN sellers s ON s.id = ip.seller_id
    GROUP BY s.id, ip.import_status
    ORDER BY s.id, ip.import_status
""")

status_rows = cur.fetchall()

if not status_rows:
    print("⚠️  Импортированных товаров нет ни у одного продавца")
else:
    from collections import defaultdict
    by_seller = defaultdict(lambda: defaultdict(int))
    names = {}
    for r in status_rows:
        by_seller[r['seller_id']][r['import_status']] = r['cnt']
        names[r['seller_id']] = r['company_name']

    for sid, statuses in by_seller.items():
        total = sum(statuses.values())
        visible_on_ai_page = sum(v for k, v in statuses.items() if k in ('pending', 'validated', 'failed'))
        print(f"Продавец #{sid} — {names[sid]}")
        for status, cnt in sorted(statuses.items()):
            marker = "👁" if status in ('pending', 'validated', 'failed') else " "
            print(f"  {marker} {status:<20} {cnt}")
        print(f"  → Видно на странице AI-обновления: {visible_on_ai_page} товаров", end="")
        if visible_on_ai_page == 0:
            print("  ⚠️  СПИСОК ПУСТ — кнопка «Запустить» будет вечно серой!")
        else:
            print()
        print()

# ─── 3. Sexoptovik fallback — кто предоставит credentials ─────────────────────
print("\n[3] SEXOPTOVIK FALLBACK — кто предоставит credentials другим\n" + SEP2)

cur.execute("""
    SELECT s.id, s.company_name, ais.sexoptovik_login, ais.sexoptovik_password
    FROM auto_import_settings ais
    JOIN sellers s ON s.id = ais.seller_id
    WHERE ais.sexoptovik_login IS NOT NULL
      AND ais.sexoptovik_password IS NOT NULL
    ORDER BY ais.id
""")

fallback_creds = cur.fetchall()

if not fallback_creds:
    print("❌ НЕТ НИ ОДНОГО продавца с sexoptovik credentials!")
    print("   Фотографии с sexoptovik.ru не загрузятся ни у кого.")
else:
    print(f"Найдено продавцов с credentials: {len(fallback_creds)}")
    first = fallback_creds[0]
    print(f"\nФallback (первый в очереди):")
    print(f"  Продавец: #{first['id']} {first['company_name']}")
    print(f"  Логин:    {first['sexoptovik_login']}")
    print(f"  Пароль:   ✅ задан")
    if len(fallback_creds) > 1:
        print(f"\nОстальные (не используются как fallback):")
        for r in fallback_creds[1:]:
            print(f"  #{r['id']} {r['company_name']} — логин: {r['sexoptovik_login']}")
    print()
    print("ℹ️  Когда у продавца нет своих sexoptovik credentials,")
    print(f"   используются credentials продавца #{first['id']} ({first['company_name']})")

# ─── 4. Итог ───────────────────────────────────────────────────────────────────
print("\n" + SEP)
print("ИТОГ — что проверить:")
print(SEP)
print("1. Если у проблемного продавца ai_client_id/secret есть, а ai_api_key ПУСТ")
print("   → это и есть баг: страница AI-обновления и эндпоинт требуют ai_api_key")
print()
print("2. Если у проблемного продавца 0 товаров pending/validated/failed")
print("   → кнопка «Запустить» вечно серая, это нормальное поведение")
print()
print("3. Если фотки не грузятся — смотри раздел [3]:")
print("   нет credentials в fallback = фото с sexoptovik недоступны")
print(SEP)

con.close()
