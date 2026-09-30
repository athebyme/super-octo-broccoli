# 26.09.2026: повторяемая регрессия Ozon и возврат после входа

Статус: runtime image `9d0bf9fd8fe54b2688d183324a85f5a77d843a3dbb6c5377e93dfffe38bbb86f` развёрнут в **14:18 МСК**, принят в **14:26 МСК**: healthy, 0 restarts. Единая локальная команда завершилась с exit 0; **1357 tests + 373 subtests**, **14 browser scenarios / 86 layouts**, без skipped/errors. Hosted GitHub CI не запускался; полный запуск Ozon остаётся открытым.

## Проверки из репозитория

Команда `bash scripts/check_ozon_release.sh /absolute/path/to/new-report-directory` собирает отдельный образ и проверяет все `test_ozon_*`, `test_marketplace_*` и связанные guards, затем полный браузерный сценарий. Node/Chromium обязательны; skipped, timeout, пустой набор и ошибка экспорта артефактов не считаются успехом. Runtime requirements не меняются; отдельный `requirements-ci.txt` добавляет pytest.

Контейнер работает от UID 1000, `--network=none`, без production volumes, `.env`, портов и Docker socket. Синтетическая база создаётся заново. Настоящий Chromium общается с локальным HTTP Flask/Werkzeug: вход, cookies, redirects, CSRF, handlers, services и ORM настоящие. Public JS/fonts имеют фиксированный test bundle с source/SHA/license; изображение — собственный SVG. Неизвестный внешний URL, provider attempt или JS error закрывают проверку ошибкой. Это не проверка текущего CDN, production Gunicorn/TLS или живого Seller API.

Browser покрывает вход с неверным/верным паролем и возвратом, 36 фото, шесть категорий, ценовые факты в плитке/таблице, Unicode-поиск/URL/reload, детали, tenant denial, сохранение черновика с CSRF, publication gate, качество/выбор/пагинацию/dialog, offline и чужой scope, потерянный POST с единственной durable job, завершение после закрытия браузера, 11 рабочих разделов нового продавца, истечение сессии и четыре unsafe login destinations. На точном runtime-кандидате: **14 сценариев / 86 layouts**, обе темы, 1440/390/320 px, без JS/local HTTP/provider ошибок.

Намеренный `throw` в catalog JS подтвердил negative drill: process exit 1, failed JSON, зарегистрированная JS-ошибка и `failure.png`. Отдельная проверка shell wrapper с synthetic Docker double подтвердила ненулевой код и при ошибке тестов, и при ошибке копирования evidence. Настоящий negative drill не изменял приложение/данные.

Первый полный запуск: **1355 passed / 2 failed / 373 subtests**, 433.86s. Оба падения исследованы: hardcoded `venv/bin/python` заменён на текущий `sys.executable`; scheduler assertion проверяет следующий tick в пределах 60s от сохранённого окончания работы, а не неявное выполнение всей операции за 1s. Контракт не ослаблен. Docker context теперь исключает bytecode/cache рекурсивно, чтобы не переносить compiled tests и host paths.

Workflow `.github/workflows/ozon-regression.yml`: PR, push main/master, dispatch, pinned actions, `contents: read`, без secrets/deploy; синтетические evidence хранятся 7 дней. Workflow подготовлен в рабочей копии. Непубликованный workflow и локальный Docker success не объявляются hosted CI или обязательным branch gate.

## Финальный локальный результат

Неизменяемый повторный запуск `bash scripts/check_ozon_release.sh /tmp/ozon-release-check-20260926-accepted` завершился **exit 0**. CI image `766e093eef4ef4c6dbde39339c68f06118fe51b4ade4d7726328c625ba325892`: фактически подтверждены network none, user check, отсутствие mounts и cap-drop ALL. **1357 passed / 373 subtests / 200 existing deprecation warnings**, 0 failed/errors/skipped. JUnit содержит 1730 cases, включая subtests; это не дополнительные 1730 тестов. Browser: **14 сценариев / 86 layouts**, JS/HTTP/external/provider errors=0. Общее время двух stages внутри контейнера **497.91s**, включая запуск процессов; browser 55.03s.

Предшествующий полный прогон также прошёл contract/browser stages, но shell вернул 2: его файл был отредактирован во время ожидания дочернего процесса, и Bash продолжил чтение со старого offset. Он не выдан за успешный запуск команды. Повтор выполнен без изменений выполняемого script, с сохранёнными отчётами и нормальной очисткой контейнера. Ошибка копирования evidence отдельно проверена synthetic Docker double и возвращает failure.

## Исправление runtime

Форма входа теряла `next` при POST, хотя сервер уже поддерживал возврат. Теперь действие формы сохраняет ссылку, включая повтор после неверного пароля. Server-owned local URL gate отклоняет browser-special backslash, encoded separator и control characters. Обычный локальный путь с фильтрами и account context сохраняется.

Образ отличается от предыдущего принятого ровно двумя runtime-файлами: `templates/login.html`, `services/url_security.py`. Отдельные unit tests и реальные CSRF login requests подтверждают локальный возврат и отказ от внешнего redirect. Схема, миграции, provider endpoints и write flags не меняются.

## Deployment и резервная копия

Переключение выполнено в 14:18 МСК. Переиспользован проверенный snapshot 26.09 09:06:17 UTC, возраст 2.2 часа при разрешённом пределе 6 часов. Этот точный gzip уже физически восстановлен в отдельный файл, проверен SHA/size/full SQLite quick_check; archive/manifest и размер повторно подтверждены перед переключением. Это не новый backup и не новый isolated application restore. Свободно 15 495 684 096 bytes. Предыдущий image `b2fda91b09480…` сохранён для rollback.

Critical environment сравнен до переключения, credentials/flags не менялись, auto-publish=0. Startup guard завершился полностью, production-приёмка подтверждена ниже. Нового provider I/O или queue pilot этот выпуск не требует.

## Production-приёмка

- **25 страниц / 150 layouts**, обе темы; шесть категорий, **60/60 реальных фото**, шесть ценовых представлений. Route/JS/local HTTP/overflow errors — 0. Browser mutations — 0.
- Отдельный quality workspace: **4 сценария / 16 layouts**, **25/25 фото**; saved facts, scoped detail, фильтры/reload и keyboard close.
- Публичный HTTPS с проверкой TLS: login 200, form action сохраняет exact account/page `next`; четыре runtime assets совпадают по SHA. Два изменённых runtime-файла в контейнере совпадают с проверенным candidate/worktree.
- Scheduler healthy, фактически наблюдённое продвижение heartbeat **120.53s**. Прежние price attempts остаются по одному: округление/conflict сохранено в истории, восстановленная seller price 1059. Pending stock proposal сохранена. Новых provider writes/live API probes нет.
- Приёмка текущего runtime image отдельно от CI image. Кандидат прошёл тот же offline full-app browser fixture с неизменными production runtime files; mounted только public test harness и пустой каталог synthetic evidence. Production-проверки выполнены на реальных Gunicorn/TLS и сохранённых данных.
- Приватные receipts `login-return-release.json`, `login-return-production-*.json`, `login-return-external.json`; aggregate summary — `output/ozon-ui/rehearsal-summary.json`. Окончательная приёмка 11:26:55 UTC; browser/external verification завершена 11:26:28 UTC.

## Сообщение подписчикам

После приёмки отправлен общий статус production, проверок и открытых ограничений всем активным `/start` подписчикам. Telegram подтвердил **2/2 доставки**, rejected/unconfirmed/deferred=0; receiver active/running, NRestarts=0. Это одна итоговая контрольная точка, автоматического повтора нет.

## Границы

Этот пакет не закрывает buyer price/скидку Ozon, inbox grants/reply, shipping/stock pilots, реальные package/compliance facts, внешний финансовый отчёт, postings 429, внешний аварийный монитор, off-host/key/media recovery и длительный пользовательский пилот. Новых Ozon probes/writes не требуется. Telegram-подписки и общий статус уже приняты отдельно: [отчёт](2026-09-26-deploy-telegram-subscribers.md).
