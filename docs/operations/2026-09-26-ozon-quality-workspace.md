# 26.09.2026: рабочий список качества Ozon

Статус: image `b2fda91b09480edef36c95998f9bbf26425cd85d8f487c74fd91bc8196109e20` развёрнут в **13:06 МСК**, принят в **13:22 МСК**: healthy, 0 restarts. Rollback image — предыдущий принятый finance comparison `97cfb9c9f4e9ece0c350bf3d2d3ad0bb43f54fbc7fc2f833b3337f061bcd82bf`. Полный запуск Ozon остаётся незавершённым по перечисленным ниже внешним gates.

## Что изменилось

«Качество карточек» перенесено на Vue 3. Рабочий список показывает фото, артикул, дату и последнюю оценку, причины, ещё не проверенные карточки и изменение данных после оценки. Поиск понимает русский регистр; фильтры/page/detail сохраняются в URL и browser history. Выбор до 200 точных карточек переживает страницы/reload и передаётся в существующий AI context без автоматического вызова модели.

Деталь — native dialog с Escape/focus, составляющими оценки, объяснимыми причинами, подписанными метриками и ссылками к карточке/аналитике. JSON из пользовательского интерфейса убран. Балл/среднее названы сохранённой оценкой Seller Hub; никакой подмены официальной оценкой Ozon, readiness, прогнозом денег или разрешением отправки. Unknown и ноль различаются; поддержанные метрики требуют exact completed current-contract analytics и валидного fingerprint.

GET нового workspace и legacy quality GET ничего не пересчитывают. Явная кнопка ставит durable `ozon_quality_recompute` job; worker обходит bounded keyset по 200 товаров раз в 10 секунд. Job и общий scorer имеют process-shared locks, восстановление после restart, deadline 24h и отдельное наблюдение прогресса. Удаление/архивирование не смещает cursor; исходный total не объявлен immutable. Новые после старта ID обрабатываются следующим/плановым проходом. Generic status скрывает cursor и не закрывает quality job по старому 30-минутному timeout. Provider/image/LLM/credentials I/O в этом worker отсутствуют, price/stock/drafts не изменяются.

Десять runtime-файлов; новые таблицы, миграции, provider endpoints и зависимости не добавлены. `models.py` изменён только в публичной сериализации BackgroundJob. `AGENTS.md`, дизайн и user guide обновлены. Старый bounded explicit recompute POST остаётся совместимым; общий writer lock защищает его и плановый scorer.

## Доказательства до deploy

- **130 tests passed / 42.43s**: queue/HTTP/read model/Vue, existing quality/analytics routes/service/workspace, account health/heartbeat/catalog scheduler и startup migration guards. Проверены scope/strict query, GET без пересчёта, фактический ноль/unknown/unassessed, literal Unicode search и SQL-пагинация.
- Queue: дедупликация, занятый живой lock, feature flag, expiry, сохранение checkpoint при имитации process exit после commit оценок, повтор только локального batch без удвоения progress, удаление/архивация и новые ID. Необработанная ошибка не становится completed.
- Финальный образ на network-none стенде: **12 сценариев / 72 layouts**, 1440/768/390/320 px, обе темы. **25/25 фото**. Detail/deep URL/back/reload, поиск, выбор/пагинация, unknown metrics, keyboard close, empty/offline/foreign response/budget/session expiry, настоящий CSRF POST в локальную очередь, потерянный POST и восстановление после reload; completed не сбрасывает прежний список без явного открытия новых оценок. Один local queue POST; provider reads/writes/LLM — 0.
- Desktop light и mobile dark screenshots просмотрены. После визуальной критики дополнительные фильтры свёрнуты, служебная ссылка повторной загрузки фото появляется только при ошибке, контраст primary link в тёмной теме исправлен. Полная стендовая приёмка повторена на финальном image после этих изменений.
- Прямой read нового сервиса с реальной volume `:ro`, `network=none`: **8111 активных карточек**, 8111 сохранённых проверок, 1529 changed, первая страница 25/25 с фото, detail доступен. Workspace **0.835s**, включая старт контейнера/imports **3.33s**; никаких provider calls/основных DB writes. Это наблюдение до deployment, не неизменная статистика магазина.

## Backup и deployment

Переиспользован проверенный snapshot **26.09 09:06:17 UTC**, возраст перед deployment **1.01 часа**, меньше принятого лимита 6 часов. Этот архив физически восстановлен и прошёл SHA/size/full SQLite quick_check в предыдущем выпуске; receipt не выдаётся за новый backup или новый isolated app restore. Схема не меняется. Архив: `seller-platform-20260926T091826Z-e37005959d72.sqlite.gz`, raw SHA `672ee23ee02fb18b07420b7acf4a07c2afed03adb0ba60360ef997ed4a369efd`. Перед переключением свободно 17 782 059 008 bytes. Старые archives/original media сохранены.

Critical environment и flags сохранены, auto-publish=0. Startup guard выполняется полностью. QA-контейнер остановлен при deployment. Startup guard и production-приёмка завершены; доказательства ниже.

## Production-приёмка

- **4 сценария / 16 layouts**, две темы, 1440/768/390/320 px: exact scoped API, полная сводка 8111 карточек, две непересекающиеся страницы, реальные фото **25/25**, detail/Escape, поиск артикула и reload. GET-only браузерный проход не создаёт provider/LLM вызовов.
- Общая регрессия: **25 страниц / 150 layouts**, **6 категорий**, **60/60 фото**, **6 ценовых представлений**. Route/JS/local HTTP/overflow errors — 0. Публичные TLS/login и SHA четырёх assets подтверждены; все 10 runtime-файлов совпадают с проверенным образом.
- Реальная кнопка через UI/CSRF создала **ровно одну локальную заявку**. Браузер закрыт после enqueue; singleton worker завершил **8111 из исходных 8111 карточек за 406.5 с**, затем повторное открытие показало завершение. Generic job GET не выдаёт private cursor. Это не restart drill: процесс не перезапускался; аварийное восстановление checkpoint проверено отдельным unit test.
- После завершения scheduler healthy; наблюдённое продвижение heartbeat от первой проверки — **600.5 с**, контейнер healthy, **0 restarts**. История двух price attempts и ожидающая stock proposal сохранены; восстановленная цена 1059 подтверждена до queue pilot. Queue не изменяет commercial/publication поля.
- Отдельный отрицательный CSRF test после основного прогона: **1 passed / 2.02s**, запрос без токена отклонён до создания job. Всего доказано **130 + 1 тест**, это два receipt, не повторный запуск всей suite. `py_compile` и `git diff --check` пройдены.
- Приватные доказательства: `quality-release.json`, `quality-production-*.json`, `quality-cli-after-pilot.json` в release-каталоге; сводка без credentials — `output/ozon-ui/rehearsal-summary.json`. QA-контейнер остановлен. Финальное время приёмки относится к health/queue/hash-проверке; browser-проход завершён раньше, в 13:14 МСК.

## Ограничения запуска

Этот выпуск не закрывает buyer price/скидку Ozon, доступ к inbox и проверенные reply/shipping writes, stock pilot, подтверждённые package/compliance facts для публикации, внешний бухгалтерский отчёт (владелец предоставит позже), 429 finance postings, off-host/key/media recovery, RPO/RTO, внешний монитор и длительный пользовательский пилот. Повторных Ozon probes или marketplace writes в рамках этого пакета нет.
