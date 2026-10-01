# Ozon и UX: приёмка выпуска 01.10.2026

Статус: работа продолжается; этот документ не объявляет выпуск завершённым. Объём и исходные зависимости: [план](../design/ozon-ux-completion-plan-2026-10-01.md). Итоговые результаты ниже заполняются по фактически принятым коммитам, проверкам и production image. Отчёт является изменяемым результатом приёмки; runtime, tests, CI и архитектурный контракт удостоверяются отдельным source manifest.

## Принятые изменения

- UX-01.5: вложенные ошибки модерации, в том числе JSON с Unicode, показывают читаемую причину и следующий шаг. Особая причина «Слишком длинные слова в описании» требует одновременно кода и соответствующего сообщения площадки. Другие причины не подменяются. Техническое содержимое раскрывается отдельно и экранируется. Фото имеют осмысленную подпись и доступный выбор. Локализованный статус площадки отделён от общего статуса.
- UX-01.7: история WB показывает названия полей и значения до/после, сохраняет полные структурированные сведения в раскрываемой части. Переходы к карточке разрешены только для подтверждённого товара продавца. При ошибочной связи с чужой карточкой её сведения скрыты. Принятые root commits: `18a587a`, `0e22e83`; focused проверка первого пакета — 13 passed. Общая браузерная приёмка ещё не выполнена.
- CAT-EDIT-01/02: приняты общий service/API и additive migration (`7c6341d`), append-only регистрация двух миграций (`a7b4d1c`) и полные raw/source/recipient seals (`658d685`). На объединённом коде — 57 focused startup/migration/service/route проверок; после seals — 112 passed и 69 subtests для common service, draft preparation и actual native Flash capture. Просмотренные связанные каналы не изменяются при общем сохранении. Отдельная ручная projection используется только при явной новой подготовке черновика; без overrides legacy fact hash сохранён. UI и writer guards ещё в работе.
- Ozon/UX-01.6: принят сквозной synthetic journey и исправление per-draft VAT UI gate (`2ba2c73`). На объединённом коде — 17 passed и 7 subtests для journey/runner. Browser evidence ниже относится к scoped worker source до ограничения среды.
- WB-EDIT-01..04/UX-01.2: приняты exact selection, typed single/bulk schema, local preview и durable reviewed apply (`0270d52`, `d7980a1`); common routes зарегистрированы (`c51b110`). Root focused проверка на объединённом коде: 114 passed, 66 subtests, включая selection DOM, no-op/exact changed-set, свежий provider drift, replay, local keyword CAS/rollback и весь quarantine service. Browser fixture ещё готовится; live WB write не выполнялась. Ручные и AI операции проверяются отдельно, без скрытого follow-up.

## Новые доказательства и review

Сквозной Ozon browser fixture до ограничения окружения прошёл на изолированном CI image с `network=none`: 12 проверок и 20 раскладок, обе темы, 390/1440 px; JS errors, unexpected HTTP, external requests и реальные provider attempts — ноль. Проверены создание нового источника/черновика, exact категория/тип, явные packaging/VAT, выборочное AI apply, fresh validation, reviewed upload и четыре полных synthetic readbacks. Отдельные success/new-source ветви имеют по одной synthetic записи; unknown-ветвь — одну запись, ноль повторов, quarantine и безопасную сверку. Квитанция: `/tmp/ozon-audit-journey-r6/ozon-complete-journey-browser.json`. Это evidence scoped worker source, не итоговый merged release.

Обнаружен и исправлен Ozon UI gate: готовая карточка с собственным VAT блокировалась при отсутствии VAT по умолчанию у аккаунта. Backend уже проверял VAT самой карточки. Остальные права, доступность аккаунта/ключа и fresh per-draft validation сохраняются. Diff принят; полный итоговый browser gate ещё не выполнен.

Analytics scoped worker evidence: 48 long-data layouts (320..1440 px, обе темы, оба положения sidebar и text scale) и 16 отдельных empty/error state cases (390/1024/1280/1440 px, обе темы), без root overflow, JS/API/external errors и provider calls. Квитанция: `/tmp/ux01-analytics-audit-20261001/analytics-report.json`. Эти результаты не являются повторной приёмкой final merged source. Review theme tokens выявил недостаточный контраст muted text и нескольких accent/warning пар; точечное исправление в работе.

Review выявил пробелы WB selection JS (clear/page/all-filtered сначала меняли Set, затем восстанавливали старые DOM-отметки) и покрытия preview остальных ручных WB операций. При расширении preview также выявлены необходимость exact changed-set при записи, сравнения reviewed before со свежей full-card и локального CAS для keywords. Worker исправляет эти конкретные случаи; первоначальные focused результаты не являются доказательством исправления найденных пробелов. Полнота source/channel fingerprints общего редактора исправлена и проверена отдельным принятым пакетом.

Широкая unit-проверка 166 доступных release-contract файлов на source `0de582e`: 2004 passed, 482 subtests, 2 failed, ноль skips. Она не является полным release gate (новый WB selection файл ещё не был интегрирован, browser/container стадии отсутствуют). Выявлено расхождение UTC day синхронизации с local `date.today()` default экранного workspace; исправление и детерминированные clock regressions в работе. Второй случай получил честный `QuarantineBusy` при общем host temporary namespace; повтор всего quarantine service вместе с новым WB/common пакетом в отдельном `TMPDIR` прошёл, runtime flock не менялся. Предположение о межпроцессном столкновении fixtures не выдаётся за доказанный provider сбой. JUnit: `/tmp/seller-hub-root-contracts-20261001-r2/contracts.xml`, новый focused JUnit: `/tmp/seller-hub-wb-root-check-20261001-r1/contracts.xml`.

После смены окружения работа продолжается в изолированных локальных клонах `/tmp`. Проверенные ограничения: `docker info` — отказ доступа к `/var/run/docker.sock`; создание loopback fixture socket — `PermissionError: Operation not permitted`; основной `.git` и исходные worktrees доступны только для чтения. Unit/Node проверки возможны. Полные browser/container gates, merge исходного main, startup rehearsal на private volume и deployment пока blocked доступом, а не объявлены выполненными.

## Проверки данных и эксплуатации

Свежая локальная копия production SQLite создана через штатный verified-backup контур. Snapshot: 30.09 21:50:49 UTC, backup завершён в 22:00:22 UTC. Исходная БД: 13 970 239 488 байт; архив: 1 826 896 438 байт. Фактическое восстановление подтвердило размер, SHA-256 и `quick_check=ok`. Архив и manifest остаются в приватном `/app/data/backups/`; внешнее хранилище не подключалось.

Отдельная копия для startup rehearsal восстановлена в приватный task-owned каталог, завершение 22:12:36 UTC. Production cutover и provider replay не выполнялись. На копии записана безопасная исходная квитанция: 79 завершённых startup steps, ноль активных запусков, counts семи затронутых таблиц. Кандидат ещё не применял миграции к этой копии. Будущая проверка должна подтвердить только ожидаемый scoped rerun, сохранность counts и повторный no-op.

Read-only health текущего аккаунта Ozon в 22:17 UTC показал connected/read-enabled, healthy scheduler и свежие catalog/analytics/fulfillment/finance. Reviews/questions были unknown. Позднее наблюдение показало `inbox_access_denied` для reviews/questions и временный `account_busy`/stale для fulfillment; это разные наблюдения, а не постоянная гарантия свежести. Две исторические записи имеют uncertain/expired; повтор или признание успеха не выполнялись. Проверка health не вызывает Seller API и не изменяет очередь.

WB-EDIT-02, ограниченное read-only наблюдение: для продавца имеются 32 карточки «Свечи эротик» с exact subjectID 5880; свежий локальный справочник содержит 32 доступные характеристики. Рабочая категория «Мастурбаторы мужские»: subjectID 5070, 31 характеристика. Прежний клиент искал subject по названию и выбрасывал «Subject не найден», если не смог получить ID. Исторического provider response нет; конкретная причина прежнего ответа не установлена. Новый путь использует exact ID, не подменяя категорию.

## Результат по кодам задач

| Код | Уже реализовано / принято | Осталось и зависимости | Проверки и ограничения |
| --- | --- | --- | --- |
| UX-01 | Сохранены прежняя карта и визуальная система | Общая приёмка дочерних задач | Пока не завершена |
| UX-01.1 | Существующие композиции и правила сохранены | Новые формы, keyboard, contrast, narrow screen | Финальный browser gate впереди |
| UX-01.2 | Safe return Ozon; принят WB exact selection/safe return | Final Pipedream browser scenario, sort/page | Selection/DOM/URL/account boundaries unit passed |
| UX-01.3 | Прежняя responsive аналитика; добавлены empty/error scenarios | Повторная приёмка final merged source | Scoped browser: 48 layouts + 16 state cases passed |
| UX-01.4 | Прежнее меню и legacy входы | Приёмка текущего merged source | Keyboard/contrast browser matrix впереди |
| UX-01.5 | Читаемые причины, status, photo semantics | Браузерная проверка | Focused пакет принят |
| UX-01.6 | Прежние flows; приняты полный synthetic journey и per-draft VAT gate | Итоговый browser rerun | Unit17/7subtests; scoped browser12checks/20layouts; live новая публикация требует фактов/review |
| UX-01.7 | Читаемые изменения и защищённые fix links | Route wiring и browser acceptance | Focused пакет принят; реальный rollback не выполнялся |
| UX-01.8 | Legacy/beta/студия/merge сохранены | Проверка нового доступа к common editor | Existing capabilities matrix впереди |
| UX-01.9 | Прежние price/stock proposal guards | Повторная synthetic приёмка | Buyer price/скидка Ozon unknown |
| UX-01.10 | Прежние секрет-free settings/health | Повторная synthetic приёмка | Inbox access denied; method grant не доказывает доступ |
| UX-01.11 | Существующая route/action/rights matrix | Добавить новые редакторы и итоговые evidence | Все write сценарии только на фикстурах |
| UX-01.12 | Прежнее разделение saved/effective cap | Повторная проверка 1..1000 и legacy100000 | Не обрезать сохранённое значение молча |
| WB-EDIT-01 | Exact межстраничный Set, page/all-filtered/exclusions, подписанная выборка и safe return приняты | Final browser, old links | DOM/tenant/filter/URL/signed64 bounds passed; cap200 — внутренний, не лимит WB |
| WB-EDIT-02 | Exact subjectID и typed local-schema путь приняты | Browser problem/working category | Unit5880/5070/schema drift passed; исторический provider response отсутствует |
| WB-EDIT-03 | Single edit с missing fields, dictionaries/types/grams принят | Browser fixture save/rights | Sizes/SKU read-only; local validation/fresh subject guards passed |
| WB-EDIT-04 | Manual preview/diff/counts/confirm, exact changed-set, live drift guards, single-use claim приняты | Browser, final history summary | Unit114+66subtests в общем пакете; preview без provider I/O, write только synthetic |
| CAT-EDIT-01 | Контракт, service/API, migrations, raw/source/recipient seals и manual projection приняты | Writer guards и photo delivery | Focused57; sealing/draft/Flash112+69subtests; source facts сохранены |
| CAT-EDIT-02 | Backend single/bulk preview/apply принят; UI в реализации | UI cancel/reopen/photos, final browser | Только общий товар; применение в канал отдельно; preview/apply rights/drift покрыты unit |

## Внешние ограничения

Подтверждённого источника buyer price и скидки площадки Ozon нет. Наблюдённый 403 не исправляется догадками, повторными blind probes или расчётом old/seller delta. Reviews/questions также не объявляются пустыми успешными наборами при отказе доступа.

Для реального пилота новой карточки у владельца запрошены подтверждённые размеры упаковки, вес с упаковкой, VAT и применимые compliance-данные, а также разрешение отправить конкретный подготовленный товар после review. Ответ ещё не получен. Изолированная приёмка продолжается независимо. Реальная нормализация publication media CDN ещё не наблюдалась.

Параллельный main handoff `49e8698`/`d1d56fc` принят без повторного pilot: runtime `553d7ab` развёрнут в image `daefb00c5732b6d829db2f3dd36a10e79e01926726373f4cbaf305fb4a30b5cf` в 22:32 UTC. После исправления формы AI values один native Flash вызов сохранил одно предложение; независимая повторная валидация дала 1 valid/0 rejected, review — applicable. Предложение не принято, версия черновика не изменена, Ozon write отсутствует. Это доказательство одного live run/review, не массового качества. Дополнительный read-only smoke наблюдал 24 карточки и декодирование двух публичных фото. Эти результаты принадлежат предыдущему runtime, а не ещё не выпущенной ветке completion.

Merge, окончательные gates, startup rehearsal и deployment этой ветки пока не завершены.
