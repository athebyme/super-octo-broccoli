# Сокращение времени startup migrations

Статус: **развёрнуто, healthy, 0 restarts, production-приёмка пройдена**.

## Изменение

Последовательные additive migrations используют одно соединение SQLite с commit после каждого шага. Повторная полная проверка FK может использовать реально полученный результат только внутри явного scope, пока совпадают connection identity, local changes, transaction state, schema version и external data version. Проверка managed domain выполняется на каждом шаге. Специальные rebuilds сохраняют исходную отдельную команду и соединение; общий connection не держит транзакцию через subprocess.

Полный backend digest, startup lock, fail-fast и success journal после всего bundle сохранены. SQL-определения миграций, модели, зависимости, UI, API и provider-контракты не менялись. Audited image diff содержит ровно четыре runtime-файла: `docker-entrypoint.sh`, `seller_platform.py`, `migrations/_foreign_key_safety.py`, `migrations/run_scoped_batch.py`. Порядок исходных миграций проверен отдельно.

## Проверки до выпуска

- **114 tests + 5 subtests**: миграции, исторические данные, fresh/repeat, rollback/stop, managed/new orphan rejection, локальная/внешняя запись, DDL, scope cleanup и отдельные rebuild commands.
- На восстановленной базе 13 620 535 296 байт полный baseline guard — **430,993 с**, final candidate — **314,460 с**: на **27,04%** меньше. Последовательные измерения на одном сервере; влияние системного кэша не исключено. Оставшиеся полные scans сохраняются там, где БД менялась.
- До/после совпали 777 объектов схемы, все 21 прежнее legacy FK violation и хеши пяти защищённых таблиц: accounts, operations, commercial proposals, listing snapshots и drafts. Это не очистка старых orphan rows.
- Fresh DB: baseline **27,280 с**, candidate **24,735 с**, одинаковая схема, `quick_check=ok`, 0 FK violations. Повторный обычный guard обоих образов использовал success journal; повтор на большой копии также прошёл без полного bundle.
- Полное приложение на реальном восстановленном снимке: **5 сценариев / 36 layouts**, обе темы, 1440/390/320 px, **1 898 SKU / 19 API-страниц**, точные totals/daily/product values. 0 JS/HTTP/overflow errors и provider calls.

## Развёртывание и восстановление

Production image: `sha256:0632b1e7e6cf3386afe6497cf1119dfb930ae196ced0bf2819c2e74a5656a745`, tag `seller-hub:startup-batch-20260925`. Предыдущий image для отката: `sha256:6282ec860fc2f7e885dc755b52d04f94f7d5fe9536b1b986140c1cd7dbb41954`. Exact image запущен штатным Compose 25.09 в **18:56:55 UTC / 21:56:55 МСК**. Critical settings не менялись, auto-publish выключен.

Сохранён и реально восстановлен проверенный архив от **25.09 08:19:07 UTC**, raw SHA-256 `94b06449b1c19fdc146f33584ebd834e1c6bcd7a28d3d4c079bf5db333b6b878`, round-trip verification и quick_check успешны. На момент выпуска ему **10,63 часа**; новый production backup не создавался. При необходимости восстанавливать именно этот архив данные после его времени будут потеряны; архив не является свежим RPO. Откат совместимого кода предпочтительно выполняется предыдущим образом поверх сохранённой рабочей БД, без ручной пометки migration journal.

После завершения всех процессов rehearsal удалены только принадлежащие проверке временные QA DB/WAL/SHM: свободное место выросло с 1,8 до **15,4 GB**. Архивы, evidence и rollback images сохранены. Cleanup выполнен перед deploy из-за давления на диск; он не заявлен production acceptance. Свежая полная raw-копия вместе с gzip и рабочим запасом места сейчас не помещается, поэтому отдельная задача ёмкости/backup остаётся открытой.

Private evidence: `startup-batch-image-diff.json`, `startup-batch-files.json`, `startup-command-parity.json`, `startup-profile/`, `startup-before.json`, `startup-after-final.json`, `startup-fresh-acceptance.json`, `startup-repeat-guard.json`, `startup-fullapp/report.json`, `startup-cleanup.json`, `startup-deployment.json` в закрытом release-каталоге. Снимки экрана и реальные данные не публикуются в Git.

## Production acceptance

Приёмка завершена **25.09 в 19:04:36 UTC / 22:04 МСК**. Штатный full migration bundle на текущей production базе занял **338.437 секунды**. Success journal соответствует коду и схеме; вручную он не менялся.

- **5 сценариев / 36 layouts**, обе темы, 1440/390/320 px. Все **1899 SKU / 19 страниц API**, дневные значения и **25/25 фото** проверены на текущем закреплённом снимке; переключение метрик, таблица, поиск, страницы/reload и периоды работают.
- Общая браузерная проверка: **25 страниц / 150 layouts**, шесть категорий, **60/60 фото каталога**, шесть представлений цены. 0 route/JS/HTTP/overflow errors и новых provider writes из приёмки.
- Внешние HTTPS/TLS, login и четыре assets — 200, хеши совпали. Scheduler жив и держит singleton lock. Повторно подтверждены healthy и 0 restarts.
- Цена пилотного артикула остаётся 1059; операции #7/#8 сохранили исходы и по одному attempt. Stock proposal #3 остаётся pending_review без operation. Auto-publish выключен.
- После приёмки свободно **15,517,827,072 байт**. Backup archive и rollback image сохранены; новый backup этим не заявлен.

Дополнительное private evidence: `startup-production/`, `startup-external.json`, `startup-verified.json`, `startup-release.json`. Итоговый Telegram-статус доставлен одной попыткой.

## Границы

Ускорение не обещает zero downtime: полный startup на большой базе всё ещё занимает несколько минут. W8 требует дальнейшей эксплуатационной проверки, достаточной дисковой ёмкости, алертов и наблюдения. Buyer price/скидка Ozon остаются unknown; stock/product write pilots требуют своих исходных данных и подтверждений. Семидневный пилот, usability и representative-period финансовая сверка остаются открыты. Этот выпуск не закрывает весь запуск Ozon.


## Последующая точка восстановления

После этого deployment отдельная проверенная команда создала свежий backup от 19:27:50 UTC. Это не ретроактивное изменение backup/RPO на момент выпуска. [Результат и границы](2026-09-25-verified-backup.md).
