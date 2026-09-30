# Рабочий экран заказов, возвратов и отмен

25.09.2026. Образ `sha256:41536969c86dfd699a693c498d0ccb01a8275093e6ec7e8ea0fccc094d3b56bb` развернут 07:03:26 UTC; healthy, 0 restarts; production-проверки завершены 2026-09-25T07:15:32Z. Rollback image: `sha256:cbec4f1cc4f4347c73982e82d3d418bf0966cafb846e2c810da17c5295ddae03`.

Три раздела используют Vue в общей оболочке Seller Hub. Список показывает реальные товары и фотографии, статус, схему и наблюдённые даты; подробности отправления открываются в native dialog с полным постраничным составом и историей наблюдений. Фильтры, страница, открытое отправление и страница состава сохраняются в URL. Возврат и отмена ведут прямо в exact связанное отправление, включая заказы вне текущего периода списка.

Компактный API добавлен к существующим routes без замены legacy response по умолчанию. Список ограничивает preview тремя позициями, полный состав — страницами до 100. Scoped FK повторно проверяются перед ссылкой и фото. Unicode-поиск не меняет identity-сравнения и экранирует `%/_`. Списки не вызывают Ozon. Ноль цены сохраняется, неизвестная валюта явно обозначена; цена позиции не объявляется финальной покупательской ценой.

Общий refresh controller для Vue и legacy analytics/finance получил timeout и восстановление после неизвестного ответа POST. До GET состояния второй POST недоступен; redirect/401/403 останавливают опрос. Success появляется после completed snapshot.

Проверки до выпуска:

- 98 тестов и 29 subtests затронутого fulfillment/read-scheduler/finance/analytics контура прошли; JUnit `/tmp/ozon-fulfillment-regression.xml`.
- Изолированный Chromium: 13 сценариев, 54 проверки layout (1440/390/320, обе темы), 0 JS-ошибок. Проверены загрузка фото, zero/unknown currency, подробности/страницы состава/reload/Escape/focus/Back/Forward, URL поиска/пагинации, пустые состояния, ошибка с сохранением прошлого списка, waiting и завершённая сессия. Синтетическая read-refresh заявка: 1 POST, реальных provider reads/writes нет.
- Backup 06:12:38 UTC: SQLite quick_check и gzip round trip, 13 620 408 320 байт; SHA-256 `043caefdf14382efe2070348418140a790498aad8f19372956c644a3c5e0c5b0`. Архив `/app/data/backups/ozon-20260925-fulfillment-workspace-predeploy.sqlite.gz`.
- Визуальная ревизия исправила неверные имена трёх иконок и синтаксис Vue backdrop handler; итоговый browser-run прошёл после исправления. Full-app проверка затем обнаружила неправильный closing tag двух `<option>`: API возвращал статусы, но browser оставлял только «Все». Исправление проверено реальным выбором схемы/статуса и reload; изолированный сценарий теперь явно проверяет доступные options.

Полная оболочка на query-only копии реальной базы: **8 сценариев / 36 layouts**, обе темы и 1440/390/320, реальные lists/detail/links/status filter, legacy finance/analytics. 0 JS/HTTP errors и 0 provider reads/writes. В окне 30 дней на 25.09 наблюдаются 17 отправлений: 5 cancelled, 7 delivered, 5 delivering; ожидание теста теперь берётся из наблюдённых статусов выбранного периода. Проверка десяти exact изменённых runtime files исключила сторонние изменения из выпуска; базовая миграция пройдена, итоговая сборка отдельно проверена штатным startup runner с уже актуальным journal. Никакой journal не подменялся.

Не закрытые этим выпуском gates: сборка/этикетки/отгрузка FBS и решения по возвратам, ответы покупателям, buyer price/скидка площадки, текущий product-write pilot и stock pilot. Они не подменяются read-only UI. Подготовленная складская заявка №3 остаётся pending review и не отправляется этим выпуском.

## Production acceptance

- 8 реальных read-only сценариев нового экрана, 24 layout/theme cases. В 30-дневном окне: 17 отправлений, 5 возвратов, 6 отмен; 15/15 проверенных фото заказов/возвратов загрузились. Проверены exact detail/reload/Escape, status options/filter/reload, поиск номера и direct linked order из возврата/отмены. В production сейчас 17 отправлений — второй страницы нет; реальная пагинация 121 строк проверена API-тестом, browser-пагинация списка и состава — синтетическим сценарием.
- Общий проход: 25 страниц, 150 layout/theme cases и 12 вариантов commercial form, 6 категорий, 6 ценовых поверхностей; 60/60 фотографий каталога загружены. Route/JS/local HTTP/overflow errors: 0.
- Внешний HTTPS/TLS: login и 13 static assets вернули 200.
- Два CSRF-protected read-refresh сценария: 2 склада, 2 наблюдения stock. Реальных provider writes и новых commercial proposals нет.
- Цена тестового товара в последнем наблюдённом каталоге — 1059, old 1462, min 0. История op7 (uncertain после округления)/op8 (успешный возврат) сохранена, attempt_count по 1. Stock proposal #3 — pending_review, operation_id NULL.
- Singleton scheduler жив и держит exclusive lock. Последний completed catalog run #24: 8719 товаров / 10 страниц / 0 warnings. Flags general/manual/commercial = 1, auto publish = 0, encryption/runtime configuration сохранены.
- После проверок удалена только временная raw QA DB и её WAL/SHM; архив сохранён. Освобождено 13 620 441 088 логических байт, доступно около 16.6 GB.

Доказательства: private release root `~/.local/share/seller-hub/releases/ozon-20260924/fulfillment-{browser,fullapp,production}/`, image/files/diff manifests `fulfillment-*.json`; production `/app/data/ozon_release_reports/20260925-fulfillment/`. Итоговый startup verification container `seller-ozon-fulfillment-verified-migrations-20260925` завершён 0. Raw QA DB после cleanup отсутствует: для следующего репетиционного запуска нужна новая копия или восстановление проверенного архива.
