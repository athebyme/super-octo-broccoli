# Аналитика Ozon: согласованный снимок и Vue

Статус: **развёрнуто, healthy, 0 restarts, production-приёмка пройдена**.

## Результат

«Аналитика заказов» показывает сумму заказанных товаров, количество единиц, среднюю сумму на единицу, дневную динамику и товары с реальными фотографиями. Сумма заказанных товаров не объявляется прибылью, начислениями или банковской выплатой. Данные просмотров и корзины отсутствуют и не заменяются нулями.

Vue использует существующую тему и локальный SVG вместо внешнего Chart.js. График переключается между суммой и единицами; точные значения доступны через нативный выбор дня и таблицу. Пропуски остаются пропусками. Поиск, сортировка, страницы, выбранный показатель/день и снимок сохраняются в URL и истории браузера. Доля товара считается от всего периода, а не от текущего поиска или страницы.

Новый `GET /marketplaces/api/analytics/workspace` читает один завершённый current-contract снимок exact seller/account/marketplace. Totals, дни и SKU page читаются одной SQLite READ transaction с бюджетом 5 секунд. Связанные названия, артикулы и фотографии требуют собственного listing; противоречивый FK не выбирает случайную карточку. Старые analytics readers также закрыты от foreign listing metadata и несогласованного marketplace scope.

Деньги и количества передаются точными decimal strings, Vue использует BigInt для подписей и отношений. Missing, invalid и explicit zero различаются. SQL группирует/сортирует/пагинирует до 100 SKU, UI — 25; literal Unicode-поиск не выполняет wildcard-подстановку. При ошибке показана предыдущая выборка с предупреждением. Исчезнувший закреплённый snapshot не подменяется latest; пользователь открывает актуальные данные явно. После завершения наблюдаемой refresh-заявки свежий снимок открывается без повторного POST.

## Проверки кода

**88 tests + 11 subtests passed**: workspace, legacy analytics, quality, routes, core contract, Vue, общий refresh, durable read requests и scheduler. Проверены scope/foreign child/listing, согласованность FK, текущая версия контракта, точные дроби/ноль/unknown, ошибочные даты и числовой диапазон, стабильная пагинация без N+1, буквальный Unicode-поиск и pin через новый snapshot/смену суток. Компиляция Python, Node syntax и `git diff --check` прошли.

Отдельная конкурентная WAL-проверка удаляет snapshot/facts другим connection после чтения parent. Текущий READ transaction возвращает исходные согласованные итоги, дни и товары; новый connection уже видит удаление.

Offline Chromium использует реальные routes, Vue controller и template с синтетическими наблюдениями: **16 сценариев / 72 layouts**, 1440/768/390/320, обе темы, дополнительно клавиатура и масштаб 200%. Поиск/reset/Back/reload, переключение метрики/дня/таблицы, pin при initial completed, old period, missing pin, поздний ответ, ошибка/retry, потерянный refresh POST, фото/retry и завершение сессии прошли. 0 JS errors/overflow/provider calls; один синтетический refresh POST, без повторной отправки после потери ответа.

Синтетический performance-прогон: 20 001 SKU / 40 000 добавленных metric facts; первая страница, страница 800, Unicode-поиск и сортировка со 100 строками заняли 0.172–0.252 секунды на запрос. Это локальное измерение SQL/workspace на синтетической базе, не production SLA.

## Сборка и восстановление

Production image: `sha256:6282ec860fc2f7e885dc755b52d04f94f7d5fe9536b1b986140c1cd7dbb41954`, tag `seller-hub:ozon-analytics-20260925`. Base/rollback: `sha256:6e7c579014975181f455074639063a904c4c7086c2ec58d26afb8b3f621a8f47`. Whole-image hash audit подтвердил ровно шесть runtime-файлов: два analytics services, insights routes, Vue JS, CSS и template. Models/schema/migrations/dependencies/provider manifest не меняются; secret files исключены.

Для rehearsal используется проверенный архив финансового выпуска от **25.09.2026 08:19:07 UTC**, а не новый backup production. Raw SQLite: 13 620 535 296 байт, SHA-256 `94b06449b1c19fdc146f33584ebd834e1c6bcd7a28d3d4c079bf5db333b6b878`; gzip: 1 781 326 056 байт. Восстановление выполняется в отдельную owned QA-копию с SHA/size/quick_check и резервом 2 GiB. Обычный migration guard не обходится. Сам выпуск не меняет схему и откатывается предыдущим образом; старый архив не объявляется точкой восстановления текущих коммерческих данных.

## Проверка на рабочей копии

Архив восстановлен с совпадением SHA/размера и `quick_check=ok`; 398,8 секунды, после восстановления свободно 2 563 252 224 байта. Обычный migration runner завершился exit 0 и записал подтверждённый bundle.

Полное Flask-приложение в `--network none`, без scheduler и provider calls, прошло **5 сценариев / 36 layouts**. Все **1 898 SKU / 19 API-страниц** и 30 дневных строк сверены с исходными metric facts того же snapshot; totals совпали с сохранёнными точными значениями. Проверены метрика/дневная таблица, вторая страница/reload, SKU-поиск/пустой результат/reset, переход 30d↔7d с честным состоянием отсутствия данных. 0 JS/HTTP/overflow errors, 0 mutations/provider calls. Скриншоты реальной копии остаются private.

Сборка запущена в production **25.09.2026 11:23:15 UTC / 14:23 МСК**. Critical environment и flags сохранены. Штатный startup завершён, приёмка подтверждена в **11:33:21 UTC / 14:33 МСК**. Healthy и 0 restarts повторно проверены перед финализацией.

## Production acceptance

- **5 сценариев / 36 layouts**, 1440/390/320, обе темы. Все **1,899 SKU / 19 страниц API** и 30 дневных строк сверены с исходными наблюдениями exact snapshot. **25/25 доступных фото** товарной страницы загружены. Переключение метрик/дня/таблицы, страница/reload, поиск/reset и разные периоды прошли. 0 JS/HTTP/overflow errors и новых provider writes.
- Общий проход: **25 страниц / 150 layouts**, шесть категорий, **60/60 фото каталога**, шесть ценовых представлений. Ошибок маршрутов, JS, локального HTTP и верстки нет; browser mutations запрещены.
- Внешние HTTPS/TLS, login и четыре asset: 200, hashes совпали. Singleton scheduler жив и держит exclusive lock.
- Price operations #7/#8 сохранили прежние исходы и по одному attempt; наблюдённая seller price остаётся 1059. Stock proposal #3 — pending_review, operation_id=NULL. Auto-publish выключен; текущие critical settings сохранены.
- После окончания проверок удалены только owned временные QA DB/WAL/SHM. Архив, manifests и rollback image сохранены; свободно **16,181,964,800 байт**. Приёмка не создаёт новый backup production и не повторяет ценовой пилот.

Private evidence: `analytics-image-diff.json`, `analytics-files.json`, `analytics-restore.json`, `analytics-browser/report.json`, `analytics-capacity.json`, `analytics-fullapp/report.json`, `analytics-production/`, `analytics-external.json`, `analytics-verified.json`, `analytics-release.json`. Реальные скриншоты и наблюдения остаются вне Git. Доставка стартового Telegram-статуса не подтвердилась; автоматический retry не выполнялся. Итоговый Telegram-статус о проверенном результате доставлен успешно; это отдельное сообщение после приёмки, не повтор стартового.

## Границы

Это интерфейс наблюдённого спроса, не бухгалтерская сверка с кабинетом Ozon за репрезентативный период. P&L, поздние корректировки, product/stock pilots и остальные A+B gates остаются открытыми. Buyer price и скидка площадки неизвестны; price-details probes не повторяются. Ценовой пилот завершён ранее, новая запись для этой приёмки не требуется. Stock proposal #3 остаётся без одобрения.
