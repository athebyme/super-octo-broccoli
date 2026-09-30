# Срок ключа Ozon и безопасная замена — 26.09.2026

Принято **17:33 МСК**. Production image `e81fd556ead03439caf1b1dd77bad675b836ab5ba5524c412371deccf0003e6a`, healthy, 0 restarts; запуск 17:23 МСК, миграции/инициализация завершились около 17:30 МСК. Внешнее хранилище backup отложено по указанию владельца; для проверки используется уже принятая локальная копия.

## Поведение

У продавца заранее не было предупреждения о подтверждённом сроке ключа; старая вкладка могла заменить ключ без проверки просмотренной версии. Новый локальный worker создаёт in-app уведомления за 14/7/1 сутки и при истечении. После перерыва появляется только текущая степень. Dedup переживает restart и удаление/прочтение уведомления; новый ключ или новая подтверждённая дата начинают отдельную серию. Будущая дата не подтверждает доступ, unknown не превращается в бессрочность.

Vue показывает дату в UTC, объяснение и переход к форме точного магазина. Ссылка из уведомления раскрывает настройки и фокусирует summary; явная кнопка — поле нового ключа. Ключ остаётся только в DOM input до отправки и очищается после успеха. История uncertain операций сохраняется; сама замена не отправляет их повторно.

Reconnect требует положительный `expected_version`, повторно проверенный под account lock. Конфликт показывает явное «Перечитать настройки» с сохранением ввода, затем требуется отдельное подтверждение. Это работает и при изменившемся названии магазина в старой вкладке. Обычная форма настроек не позволяет обойти отдельную замену ключа.

## Границы выполнения

- In-app worker: первый tick через 60s, затем 15 минут; discovery cap 100, emit cap 25, budget 5s; SQLite writer timeout 200ms с восстановлением до возврата соединения в pool. Нет decrypt/provider/LLM/Telegram.
- Additive migration: одна таблица journal и два индекса. Scope/FK/constraints проверяются fail-fast, repeat no-op. Старые credentials, catalogue и operation history не обновляются миграцией.
- Образ строится от принятого `f7a81b581899…`, с точной заменой 16 runtime files и проверкой executable entrypoint; зависимости не меняются. Auto-publish остаётся выключенным.
- Проверка реального snapshot проходит в `--network=none`: production volume read-only, staging отдельно, общий source backup-lock сериализует расход диска с daily manager. Удаляются только временные восстановленные DB/WAL/SHM, архивы сохраняются.

## Проверки до деплоя

Точный кандидат `e81fd556ead03439caf1b1dd77bad675b836ab5ba5524c412371deccf0003e6a`: 30 browser scenarios / 175 layouts (общие разделы 14/86, массовое исправление 9/64, credentials 7/25). Реальная Flask auth/CSRF, synthetic SQLite, network none; 0 JS/HTTP/external/provider errors. Новый credential journey проверяет известный/неизвестный срок, четыре степени предупреждений, seller scope, focus, две вкладки с изменением имени, explicit review, CSRF rejection, сохранение uncertain history, обе темы/320–1440 px и эквивалент 200% масштаба.

Restore архива от 13:10 UTC: 13,658,419,200 bytes, SHA/size/full quick_check подтверждены за 448.422s. Миграция на копии принята: ровно три additive объекта, повторный no-op, fingerprints шести protected таблиц неизменны; прежние 21 legacy FK violation сохранены, новых нет. Restore + migration + cleanup заняли 542.88s; удалена только временная raw DB, free вернулся к 20,13 GB. Образ rehearsal `34081197a552…` предшествует финальному исправлению HTTP-конфликта: все schema/startup inputs совпадают с финальным кандидатом. Финальный CI принят: 1435 tests +373 subtests (JUnit: 1808 случаев), 30 browser scenarios/175 layouts, exit 0 за 584.02s. Runtime inputs всех 16 файлов совпали с кандидатом. Deployment начат 17:23 МСК; production acceptance завершена 17:33 МСК.

План: [expiry notices](../design/ozon-credential-expiry-notices.md). Эксплуатация: [runbook](../OZON_PRODUCTION_RUNBOOK.md). Пользовательский сценарий: [руководство](../OZON_USER_GUIDE.md).


## Production acceptance

- **25 страниц / 150 layouts**, 6 фактических категорий, **60/60 фото** каталога, 6 поверхностей цены. Route/JS/HTTP/overflow errors = 0; браузер не отправлял mutations.
- **3 реальные партии / 6 сценариев / 48 layouts**, **13/13 фото**. Preview, фильтры, клавиатурный focus и classic fallback работают; draft versions и operation count неизменны.
- Recovery/health: **3 проверки / 12 layouts** в обеих темах на 320/390/1440 px. Форма открывает точный магазин, focus правильный, password пустой, hidden viewed version совпадает; expiry UI/API/health передают один сохранённый факт.
- Fingerprints четырёх production scopes (accounts, operations, drafts, proposals) совпали до/после. Новый journal создан, строк 0: единственный active Ozon account имеет expiry дальше 14 дней. Реальные expiry/ключи не менялись ради предупреждения; live notification delivery этим пакетом не заявляется. Сценарии истечения и успешной ротации проверены на exact candidate с synthetic credentials и настоящим Flask/CSRF.
- Новый worker дошёл до discovery после запуска: свежий dedicated lock, elected scheduler heartbeat healthy. Это свидетельство выполнения локального tick, а не доставки выдуманного предупреждения. Startup migration выполнилась штатно, новых provider endpoints нет.
- Локальный observer: issues пуст, backup-lock свободен, оба timers active. Free **20,088,909,824 bytes**, required для следующей копии **17,953,640,448 bytes**; ёмкость остаётся конечной и проверяется перед каждым запуском. Архив от 13:10 UTC и более старые копии сохранены.
- Rollback image сохранён: `f7a81b581899cc064397de9faaf3b418218289051ab934a656631d0be5625cdc`. Credentials/env/write flags/rate ledger path не менялись; auto-publish = 0.

Private evidence: `~/.local/share/seller-hub/releases/ozon-20260924/credential-release.json`, `credential-image.json`, `credential-ci/summary.json`, `credential-acceptance-v2`, `credential-rehearsal/migration-rehearsal.json`, production browser/state/heartbeat records. Full CI exit 0; JUnit 1808 уже включает 373 subtests, это не дополнительные 1808 основных тестов.

## Оставшиеся границы

Этот выпуск закрывает предупреждения о сроке и просмотренную версию при замене ключа, а не весь W1/весь запуск Ozon. Не закрыты operator quarantine/audit для недоказуемых исходов, покупательская цена/скидка Ozon, inbox access, реальные package/compliance facts, stock/shipping write pilots и длительный пользовательский пилот. Внешнее backup-хранилище отложено владельцем и не блокирует принятый локальный пакет.

Telegram: общий статус production, проверки, локальные бэкапы и незакрытые ограничения доставлены **2/2 активным подписчикам `/start`**, по одной попытке; rejected/unconfirmed/deferred = 0. Предыдущая неизвестная доставка local-operations milestone не повторялась.
