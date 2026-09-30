# Финансы Ozon: журнал начислений на Vue

Статус: **развёрнуто, healthy, production acceptance пройден**. Образ `sha256:9fb48834bec48be57a488ad06f899be2e3ed027253df2b309cd9d2c79b908fd7`, старт 25.09.2026 в 08:56:44 UTC / 11:56 МСК. Предыдущий образ `sha256:95d6af011230286937c7385c53a52248711fdfe0c1bf82cd53020cddc7b3d34f` сохранён. Проверка завершена в **08:59 UTC / 11:59 МСК**, 0 restarts.

## Поведение

Журнал на Vue показывает положительные, отрицательные и итоговые начисления отдельно по валютам, фильтруется по номеру/SKU, категории, знаку и типу услуги. Расшифровка открывает товары, фотографии точных связанных карточек, отправление, отдельные страницы компонентов и источник с временем наблюдения. До трёх preview-товаров/компонентов на строку; полный состав доступен по страницам. Native dialog поддерживает Escape, Back/Forward, reload и возврат фокуса. Светлая и тёмная темы используют существующие токены Seller Hub.

Суммы берутся только из top-level `accruals[].total_amount`. Компоненты поясняют сумму, не прибавляются повторно и не обязаны складываться в неё. Фильтр типа выбирает целое начисление. Это не банковская выплата и не P&L. Decimal string не преобразуется в JS Number; ноль, знак и до четырёх десятичных знаков сохраняются. Отсутствующая валюта не подменяется RUB.

Первое чтение закрепляет completed snapshot и точные даты периода в URL/API (`snapshot_id`, `as_of`). Новая фоновая загрузка и смена дня не меняют суммы посреди просмотра. Смена периода, действие открытия последних данных или завершение наблюдаемой активной refresh-заявки снимают закрепление; начальный статус старой completed заявки этого не делает. Недоступный снимок возвращает 404 с отдельным действием восстановления, без незаметной подмены. Неудачный фильтр сохраняет явно обозначенную предыдущую выборку.

API ограничивает список, дочерние preview и страницы detail; чужие child/listing/posting FK не раскрывают названия, изображения или ссылки. Буквальные `%`/`_`, strict query validation, abort/revision/session/timeout guards проверены. HTTP refresh по-прежнему только ставит durable read-заявку, новые provider endpoints/writes не добавлены.

## Проверки до выкладки

- **87 tests + 29 subtests**: finance service/routes/contracts, compact previews, scoped children/FK, постоянное число SQL запросов, 121 компонент и последняя страница, signed totals без double count, strict query, snapshot/date pinning через смену дня, race/timeout/session, общий refresh и fulfillment routes.
- Offline Chromium: **15 сценариев, 54 layouts** (1440/390/320, light/dark), 0 JS errors/overflow, 0 реальных provider calls. Включены новая версия между страницами, initial completed refresh, active completion, pruned snapshot recovery независимые страницы состава/услуг и completed GET после потерянного POST без второй заявки.
- Полное Flask-приложение на свежей изолированной копии базы: **8 сценариев, 24 layouts**, **71 начисление**, 0 JS/local HTTP errors/overflow/mutations/provider calls. Все страницы пройдены ровно один раз с тем же snapshot; валютные totals независимо пересчитаны из top-level фактов. Реальные фильтры, составы, exact links, deep link/reload/Escape проверены. После финальной правки восстановления повторно пройден этот же стенд на окончательном образе.
- Один отдельный реальный read `/v1/finance/accrual/by-day` за **24.09.2026**: 1 факт, полный день, ID-набор, суммы/валюта/категория/позиции/компоненты совпали с локальным snapshot #463, net RUB −1.1400. Shared ledger, retries=0, SQL session закрыта до HTTP; **1 physical read, 0 writes**. Это выборочная сверка одного дня, не бухгалтерское подтверждение всего периода.

## Production acceptance

- На окончательном образе: **9 финансовых сценариев / 24 layouts**, 71 начисление, независимый пересчёт totals по валютам, 12/12 фотографий связанных товаров. Фильтры, страницы с закреплённым snapshot, расшифровка, reload/Escape и empty search прошли; 0 JS/HTTP/overflow errors, 0 refresh POST и новых provider writes.
- Общий проход: **25 страниц / 150 layouts**, включая шесть разных категорий; **60/60 фото каталога**, без fallback/pending. Шесть ценовых представлений подтвердили base 1462, seller 1059 и unknown buyer/скидку площадки. Не было JS, local HTTP, route или overflow errors; все browser mutations запрещены.
- Внешние HTTPS/TLS, login и четыре публичных asset проверены; SHA-256 asset совпадает с проверенным кодом. Singleton scheduler жив и держит exclusive lock.
- Ценовые операции #7/#8 сохранили uncertain/succeeded и по одному physical attempt; restored seller price 1059. Stock proposal #3 — pending_review, operation_id=NULL. Flags: general/manual/commercial=1, auto-publish=0.
- Последний observed catalog sync #26: 8719 товаров, 10 страниц, 0 warnings, завершён 08:34:03 UTC. Это состояние фонового каталога, не финансовая сверка.

## Выпуск и восстановление

Свежий SQLite backup от **08:19:07 UTC**: 13 620 535 296 байт; gzip 1 781 326 056 байт. `quick_check=ok`; полное распаковывание проверено по размеру/SHA-256 `94b06449b1c19fdc146f33584ebd834e1c6bcd7a28d3d4c079bf5db333b6b878`. Архив `/app/data/backups/ozon-20260925-finance-workspace-predeploy.sqlite.gz`, manifest рядом. Лимиты: архив до 2 GiB, дисковый резерв не ниже 2 GiB. Production DB не vacuum-илась.

Обычный guarded migration runner выполнен на копии базы. Финальная сборка повторно прошла проверку того же backend bundle/schema journal; обходов entrypoint нет. Начальный запуск прошёл полный migration runner; финальная правка двух JS-контроллеров отдельно подтвердила неизменный backend bundle и current production journal, поэтому повторный startup выполнил штатную быструю проверку. Полный hash comparison подтвердил ровно восемь ожидаемых runtime-различий, включая общий refresh metadata callback и добавление `offer_id` в локальный presenter. Critical environment и feature flags сохранены; auto-publish выключен. Docker Hub build встретил TLS timeout; использована локальная проверенная предыдущая production base с теми же зависимостями и точным набором изменённых файлов. Секретов в build context/image нет.

Private evidence: release directory `finance-image-diff.json`, `finance-files.json`, `finance-browser/report.json`, `finance-fullapp/report.json`; production `/app/data/ozon_release_reports/20260925-finance/`. Скриншоты и реальные идентификаторы остаются вне Git. После терминального завершения всех rehearsal/browser процессов удалена только временная raw QA DB/WAL/SHM; архив и manifests сохранены. Свободно 16 275 636 224 байт. Итоговые private records: `finance-release.json`, `finance-recovery-diff.json`, `finance-external.json`, `finance-production/finance-flows.json`, `browser-final.json` и `cleanup.json`.

В deployment Telegram доставлены два содержательных статуса: начало production-выкладки и результат приёмки; постоянная рассылка не включалась.

## Открытые условия полного запуска

W10: сверка с бухгалтерским отчётом Ozon за репрезентативный период, возвраты/поздние корректировки, export и дальнейшая аналитика остаются открытыми. Независимый пересчёт сохранённых сумм не заменяет сверку с Ozon Seller.

Buyer price/скидка Ozon остаются unknown из-за неподтверждённого доступа/контракта price-details. Новые probes без изменения доступа не выполнялись. Real product/stock pilots, shipping/replies, эксплуатационные и остальные A+B gates этим выпуском не закрываются. Stock proposal #3 не является одобренной записью; ценовой пилот повторно не выполняется.
