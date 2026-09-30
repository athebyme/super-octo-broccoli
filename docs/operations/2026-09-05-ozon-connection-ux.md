# Ozon: подключение и понятный рабочий сценарий

Цель пользователя: добить интеграцию Ozon, сделать её простой и понятной по UX, довести до рабочего подключаемого состояния. Реализация развёрнута и проверена 2026-09-05; ниже отдельно зафиксированы результаты нового сквозного сценария, не только предыдущего hardening.

## Критерии завершения

1. Новый продавец понимает, где взять Client-Id/API key, может подключить кабинет без обязательных настроек публикации и получает понятную ошибку рядом с формой. Секрет не возвращается в HTML/JSON/логи. Дубликаты и чужие кабинеты не допускаются.
2. Одно явное действие запускает проверку и первичную загрузку каталога. HTTP не ждёт обхода страниц; durable работа продолжается после ухода со страницы/перезапуска. Дедупликация, seller/account scope, bounded API budgets и обработка 401/403/429/5xx обязательны.
3. Кабинет и каталог показывают честные состояния подключения/загрузки/ожидания/ошибки и следующее действие. Пустой успешный кабинет отличается от ещё не загруженного и от сбоя. Ключ только для чтения не объявляется разрешением на запись.
4. Из подключённого кабинета есть понятный путь в каталог и подготовку/загрузку товаров; выбранный кабинет сохраняется. Цены/остатки/фото отображаются из наблюдённого read model, без fake WB identity и без выдуманных значений.
5. Путь «товар → черновик → обязательные исправления → явное подтверждение загрузки → поштучный результат» проверен. Статусы Ozon, ожидание справочников, доступы, модерация и неизвестный результат объясняются пользователю, а не только внутренними кодами/JSON. Safety gates и ручные значения не ослабляются.
6. Проверены tenant/credential/race/restart/partial-sync invariants, релевантные unit/service/route tests и настоящий браузер: desktop/mobile, light/dark, new/empty/loading/error/disabled состояния. Затем один согласованный deploy и read-only smoke фактического приложения; внешний write не используется как произвольный тест.

## Исходные доказательства

- Рабочий контейнер healthy на image `bd7e411922cd219b53c2d5a410103e39bc9472c0e71a2d5fc18d927c0ce16f9e`. Предыдущий общий прогон: 2266 tests / 469 subtests. Пользовательские и параллельные изменения (включая Luna parsing artifacts) сохраняются.
- `routes/marketplace_accounts.py` сейчас только сохраняет подключение; проверка выполняется отдельным POST. Шаблон требует НДС до подключения, хотя сервис уже допускает отсутствие default_vat.
- `routes/marketplace_listings.py:sync_account` делает provider I/O в HTTP, UI передаёт max_pages=5 и после паузы требует повторного клика. Завершение загрузки не имеет durable task, автоматически продолжающего страницы.
- `OzonAdapter.check_connection` наблюдает `/v1/roles`, но capability mapping сейчас учитывает только reviews/questions. Успешный roles GET не доказывает доступность всего каталога или публикации.
- Основной catalog template не показывает фото, теряет account context в части переходов, пустое состояние сводит к «листингов нет».
- Имеющиеся отдельные `MarketplaceCatalogSync` cursor/phase/finalization guards, account locks, BackgroundJob и singleton scheduler следует переиспользовать; нового worker service/треда/LLM контура не требуется.
- После hardening реальный Ozon account #1 имеет completed sweep #9: 8719 товаров. Historical operation #6 остаётся uncertain/attempt 1; не повторять write и не объявлять success. Предоставленные диагностические ключи только read-only, хранятся вне Git.

## План выполнения

1. Единый bounded durable connect/catalog workflow, сохранение ошибок/Retry-After и restart-safe cursor, локальные request/status routes и regression tests.
2. Страница подключения с последовательностью шагов, неблокирующей формой, фоновым прогрессом, понятными правами/ошибками и необязательными настройками публикации; общий статус в каталоге и task tray.
3. Сквозной UX-аудит каталога/подготовки/массового редактора/результатов, устранение подтверждённых разрывов и ошибок отображения. Нельзя ограничить цель одним красивым экраном подключения.
4. Полная проверка текущего worktree, браузерные сценарии с synthetic accounts и read-only live проверки, обновление AGENTS.md, deploy и финальный аудит каждого критерия.

## Реализация и проверки

Реализованы durable workflow, единая форма подключения и восстановления, фоновые статусы в обоих каталогах и task tray. API settings ведёт в одну форму. Client-Id существующего кабинета теперь неизменяем, чтобы история не могла перейти к другому магазину.

Классический каталог получил observed фото, корректные обе формы цен и unknown/zero stock; чтение latest syncs стало grouped MAX вместо загрузки всей истории. Account context сохранён в основных переходах; выбранный кабинет без НДС предлагает подготовку, а не молчаливую отправку в другой магазин. Bulk history ограничивает account до LIMIT, beta сохраняет входящие фильтры.

Проверено локально (не деплой):

- 87 focused tests + 19 subtests: accounts, durable worker, price/stock presentation, listing service, exact role grants. Отдельный combined regression: 144 tests + 3 subtests, включая publication/bulk state machines. После них добавлены проверки account selection/history и recovery budgets; итоговый полный прогон ещё впереди.
- Настоящий Chromium, настоящее Flask/Jinja/Alpine/Vue приложение и изолированная SQLite, provider HTTP полностью запрещён: подключение без НДС → pending → три страницы → completed. Затем 7 экранов × desktop/mobile × light/dark: нет JS errors/горизонтального overflow. Артефакты в ignored `.playwright-mcp/ozon-setup-20260905/`.
- Браузер обнаружил важную ошибку: undefined в начальном `busy.new` приводил к disabled-кнопке в текущем Alpine. Исправлено явными boolean states; повторный end-to-end прошёл. Один параллельный unit-прогон пересёкся с synthetic browser account lock (разные SQLite, одинаковый account ID); тестовый browser lock directory изолирован, повторный unit-прогон чистый.

Финальные локальные проверки:

- Полный suite: **2334 passed, 491 subtests, 278 warnings, 517.62 s**. JUnit: `/tmp/seller-hub-ozon-complete-20260905.xml`. Поздняя проекция признака explicit taxonomy отдельно проверена readiness-test; браузер повторён после последнего изменения.
- Финальный browser harness: **50 экранов/состояний**, включая подключение, отключение/восстановление того же ID, invalid key, empty complete, disabled, создание настоящего draft из synthetic source, явный bulk run → needs_input → массовый редактор; 10 экранов × 4 theme/viewport. JS errors/overflow — 0; операция Ozon для неполной карточки не создана. Host provider HTTP запрещён, никаких реальных keys в synthetic test.
- Browser поймал ложную readiness-индикацию `0/0` и «загружается» при отсутствии типа: заменено на неизвестные требования и конкретный шаг выбора категории. Explicit taxonomy и consensus по опубликованным карточкам теперь маркируются отдельно. Убран повторный Alpine init `/my-products`, вызывавший два одинаковых GET активных задач.
- Образ `cdac7373a5338d3295ceb5bca1fee8333bdac5ce36f9ae35d9f4074d081bdaf2` собран обычным `docker compose build seller-platform`. Runtime-проверка в этом образе, без сети и без volume production: **73 unittest tests OK**. pytest в production-образ не добавлялся; полный pytest выполнен в рабочем venv.
- Параллельные Luna edits уже были hot-applied в работающий контейнер. SHA-256 всех семи соответствующих source files совпали с worktree; обычная сборка сохраняет их. Дополнительно `/tmp/wb-luna-pilot-20260905` скопирован в persistent `/app/data/backups/ozon-setup-20260905-runtime/`, побайтовый `diff -rq` чистый, чтобы recreate не потерял пользовательские артефакты.
- Создан read-only WAL snapshot БД перед deploy: 11 809 796 096 bytes, `quick_check=ok`, 398.46 s. Сжатый `/app/data/backups/ozon-setup-20260905-predeploy.sqlite.gz`: 1 540 440 566 bytes, mode 0600, `gzip -t` exit 0; свободно 18 GiB. Обычный incremental backup повторно начинался из-за текущих записей: это предусмотренное поведение [SQLite Online Backup API](https://www.sqlite.org/backup.html). Удалён только первый незавершённый временный файл; успешный backup фиксирует read snapshot в WAL и имеет deadline/progress. Основная БД и обе ранее валидные резервные копии не изменялись.
- После последних параллельных Luna edits повторная целевая проверка: 26 tests / 25 subtests, 6.32 s. Сборка повторена с актуальным worktree; live Python-файлы Luna совпадают с ним, новая версия шаблона также включена. Ни пользовательский код, ни артефакты не откатываются к предыдущему образу.

## Развёртывание и фактический результат

- Один recreate основного сервиса: `docker compose up -d --no-deps --no-build --force-recreate seller-platform`. Финальный образ — `9e751c51a1784326cf955bc1bc3ef0187f545dac9fff011b6731487a78553482`. Первый дополнительный build встретил TLS timeout Docker Registry, повторный обычный build успешно завершился; зависимости и конфигурация ради обхода не менялись.
- Контейнер стартовал в **14:32:36 UTC**, healthy наблюдён в **14:39:15 UTC**, restart count 0. Штатный полный migration bundle завершён и записан самим startup runner; после запуска `migration_journal_current=true`, read-only проверка журнала 0.04 s. Проверки миграций не пропускались и не помечались вручную.
- Все актуальные Luna source edits включены в образ. Восстановлен `/tmp/wb-luna-pilot-20260905` из persistent backup, `diff -rq` exit 0; отдельные временные test scripts также сохранены и восстановлены без перезаписи новых файлов. Старый runtime log сохранён только в backup, не подменяет новый log.
- Настоящий Chromium → Gunicorn → CSRF POST с кнопки «Обновить каталог»: **HTTP 202**, durable job `oc:1:2705654493a94c9591b227afe262ba44`. Singleton scheduler сам прошёл **9 страниц / 8719 товаров**. Browser end-to-end **101.19 s**, job completed; `MarketplaceCatalogSync #10` completed, seen=8719, updated=8719, created=0. После завершения переход в каталог сохраняет `marketplace=ozon&account_id=1`. JS errors 0, overflow false. Ручного продолжения после пяти страниц нет.
- Дополнительный read-only live browser smoke: **46 проверок / 0 failures**, включая 19 основных страниц, responsive 390/1440 и light/dark, оба каталога, account context в источниках/черновиках/истории, клавиатуру source picker и переключение периода аналитики. Все HTTP 200, JS/HTTP errors 0, broken images 0, горизонтальный overflow отсутствует. Наблюдённый TTFB accounts 10–40 ms, классического Ozon catalog 125–178 ms (во время синхронизации и после неё); это результаты конкретного smoke, не нагрузочный SLA.
- Скриншоты production: persistent `/app/data/ui-ozon-live-20260905/` и `/app/data/ui-ozon-final-20260905/`; synthetic и выбранные live копии — ignored `.playwright-mcp/ozon-setup-20260905/`. Они не коммитятся и не публикуются.
- После миграций и новой синхронизации сохранены **31358 Product / 21076 histories**; legacy Product 175 / связанных histories 145, active legacy=0, orphan histories=0. Новых физических marketplace writes сегодня **0**. Historical operation #6 по-прежнему `uncertain`, attempt 1, `update_postwrite_drift`: не повторена и не объявлена успешной.

## Сверка критериев

| Критерий | Подтверждение |
| --- | --- |
| Простое безопасное подключение | Одна форма без обязательного НДС; synthetic реальный browser create/reconnect/invalid key/empty; tenant/CSRF/secret/immutable Client-Id regression tests. |
| Один неблокирующий запуск | Live HTTP 202 → 9 страниц без продолжения; unit crash/restart/claim/version/429/403/budget tests, один singleton scheduler. |
| Честные статусы и следующий шаг | 50 synthetic экранов/состояний; unknown readiness вместо ложного 0/0, права не приравниваются к разрешению записи, failed/empty/pending различаются. |
| Путь каталог → подготовка с сохранением кабинета | Live 46 проверок и account scope tests; фото/цены/остатки из observed data, zero/unknown parser tests, context в обоих каталогах и bulk history. |
| Подготовка → подтверждение → результат | Synthetic настоящий draft и явно подтверждённый bulk run → needs_input → редактор; service/route regression publication state machines. Неполная карточка не создаёт внешнюю операцию. |
| Проверки и deploy | 2334 tests / 491 subtests, поздний focused прогон, runtime offline unittest, backup quick_check + gzip CRC, успешные миграции, healthy и live read-only workflow. |

## Границы результата

Реальная отправка новых/изменённых карточек не использовалась как произвольный smoke: предоставленные диагностические ключи read-only. Для отправки по-прежнему нужны подходящие права, включённый write gate, НДС и подтверждённые обязательные факты/справочники, затем явное подтверждение продавца. Успешное подключение и загрузка каталога не объявляют все карточки готовыми к публикации и не гарантируют завершение модерации Ozon.

Historical uncertain #6 требует отдельной сверки и остаётся защищённой. 403 отзывов/вопросов из-за подписки или прав — ограничение этого раздела, а не отказ каталога. Неизвестные значения required dictionaries и непроверенные source facts не заменялись выдуманными. Эти ограничения явно отражены в UX и [пользовательской памятке](../OZON_USER_GUIDE.md).

## Повторная сверка после продолжения пользователем

В **16:42–16:46 UTC** сервис повторно проверен после паузы: тот же release image, healthy, restart count 0; job и catalog run #10 остаются completed / 8719 / 9 страниц, физические marketplace writes сегодня по-прежнему 0, protected uncertain #6 не изменена. Product/history counts и резервные копии сохранены.

Параллельно были hot-applied `services/supplier_luna_enrichment.py` и `scripts/validate_luna_parsing.py`; оба совпадают с текущим worktree и не откатывались. Код Ozon подключения не изменён. Поэтому свежая проверка migration journal честно показывает `bundle_matches=false`, **`schema_matches=true`**: последняя успешная миграция записана в **14:38:53.850035 UTC**, расхождение вызвано последующим изменением Python-кода, а не повреждением схемы. Журнал не переписывался вручную; при следующем запуске startup runner обязан снова проверить полный bundle. Дополнительный recreate ради стороннего hot patch в рамках этой задачи не выполнялся.

Повторная проверка текущего worktree: **155 tests / 99 subtests passed, 60.58 s** — durable Ozon workflow, accounts, price/stock display, drafts и актуальные Luna contracts. Повторный production Chromium smoke: **46 проверок / 0 failures**, все страницы HTTP 200, JS/HTTP errors и overflow отсутствуют. `git diff --check` чистый. Последние параллельные изменения AGENTS.md прочитаны и сохранены.
