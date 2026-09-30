# Ozon: настройки магазина и история изменений — 26.09.2026

**Принято в production 26.09.2026, 18:38 МСК** (`15:38:08.752691 UTC`). Runtime `29a5f754e159…`, healthy, 0 restarts. Внешнее хранилище резервных копий отложено владельцем; локальные расписание и проверка восстановления работают.


## Изменение поведения

Раньше обычное изменение названия/НДС проходило через credential mutation и блокировалось при durable pending/uncertain. Теперь reviewed local settings меняет только label/default VAT и account version под тем же physical account lock. Ключ, срок, права, активность/default, существующие карточки/черновики/proposals/attempts сохраняются; отключённый кабинет не активируется. Явный пустой VAT снимает default для будущих черновиков. No-op не создаёт версии/события.

Actor приходит из authenticated user. Mutation и append-only `MarketplaceAccountEvent` в одной транзакции: connect, key replacement, settings, default, disconnect. Журнал не содержит key/ciphertext/fingerprint/raw provider bodies. Старые события не выдумываются; `credential_version` означает формат encryption envelope, а не количество замен. Default fan-out/create/disconnect replacement сериализуются seller lock + sorted bounded account locks; версии/события каждого реально изменённого default согласованы. Старые gates удаления ключа при attempted write сохранены.

Vue-форма настроек имеет независимый от ключа конфликт. Explicit readback показывает текущие название/НДС, сохраняет ввод и не обновляет просмотренную версию автоматически. Отдельный выбор разрешает дальнейшее сохранение или загружает текущие значения. Потеря ответа не вызывает повторный POST. История открывается лениво, seller/account/marketplace-scoped keyset 30+1, no provider/polling, retry и session state. Тема, читаемые формы, keyboard focus и mobile layouts сохранены.

## Проверки точного образа

- Runtime `sha256:29a5f754e1599f3a03a9e154c76afd7bd1e49e9c915308fda507a8f6816d1eaf` (`seller-hub:ozon-account-history-20260926`). Ровно 12 runtime files поверх `sha256:e81fd556ead03439caf1b1dd77bad675b836ab5ba5524c412371deccf0003e6a`; dependencies, secrets, flags, rate ledger не меняются. Auto-publish остаётся выключенным.
- Изолированный exact-image browser: **38 сценариев / 200 layouts** (general 14/86, bulk 9/64, credential 7/25, settings/history 8/25), exit 0. Real Flask/HTTP/CSRF/Vue, synthetic SQLite, network none, provider I/O forbidden. Это не live API proof.
- Focused service/migration regression: **103 tests +15 subtests**, exit 0. Full contracts: **1464 tests +385 subtests**, 200 существующих warnings, без failures/errors/skips за 470.23s. Full runner exit 0 за **635.08s**, все четыре browser stages также приняты: 38 сценариев / 200 layouts. JUnit 1849 cases включает 385 subtests; это не 1849 независимых основных тестов. CI image `cee6609b4e44…`, все 12 runtime inputs совпадают с кандидатом.
- Визуально просмотрены screenshots mobile light/dark, conflict review и desktop history. Horizontal overflow/JS/unexpected HTTP/external/provider calls отсутствуют в принятом exact-image browser.
- Начальная CI build попытка завершилась TLS timeout при загрузке DockerHub token. Final CI source image наследует уже принятый local CI base с теми же dependencies и явным публичным COPY; credentials/data/private artifacts не попадают в context. Первый широкий прогон остановлен после исправления самого screenshot harness (явный foreground первой вкладки после открытия второй); он не считается приёмкой. Final run идёт отдельно на immutable image.

## Проверка БД и deploy

Additive `migrate_add_marketplace_account_events.py` проверяет constraints/FK/unique-version/scope index и повторный no-op; включена во все startup paths. Восстановление локального verified snapshot проходит в отдельной staging, production volume read-only, shared actual backup flock удерживает дисковый бюджет до cleanup. Репетиция принята: 13 658 419 200 bytes действительно восстановлены, SHA/size/full quick_check совпали; restore + migration + cleanup заняли 539.44s. Добавлены ровно три объекта (таблица, unique-version autoindex и scope index), второй запуск no-op. Шесть protected tables и 21 историческое FK-нарушение остались неизменны. Новая история пуста. Только собственный временный DB удалён после проверки; свободно 19,898,933,248 bytes. Репетиция использовала intermediate image `83fd6460ce7c…`; финальные schema/startup inputs проверяются SHA-256 перед deploy, последующие изменения относятся к шаблону.

Rollback — предыдущий runtime image; новую таблицу не удалять, live DB/WAL и rate ledger не откатывать. Smoke читает реальные страницы и снимки, без замены ключа или изменения label/VAT владельца. Никаких новых Ozon endpoints/price/stock/publication writes в этом пакете.

## Границы готовности

Не закрывает buyer price/скидку Ozon (403), actual inbox access/replies, missing publication facts, stock/shipping pilots, operator quarantine или пользовательский pilot. Локальное сохранение ключа не является успешной проверкой доступа. Нет backfill исторических акторов. Внешние backups явно отложены; текущая схема остаётся local-only.

Private evidence: `~/.local/share/seller-hub/releases/ozon-20260924/account-history-*`. План: [settings/history](../design/ozon-account-settings-history.md). Общая launch readiness сохраняет открытые A+B gates.

## Production-приёмка

Контейнер запущен `2026-09-26T15:27:20.556899816Z`; штатный полный migration bundle записан успешно. Monitor наблюдал healthy через 500.4s своего ожидания; это измерение конкретного полного запуска, не обещание RTO. Rollback image `e81fd556ead03439caf1b1dd77bad675b836ab5ba5524c412371deccf0003e6a` сохранён. Startup journal далее может переиспользовать только неизменный bundle+schema.

- Общий реальный Chromium: **25 страниц / 150 layouts**, 6 категорий, 60/60 фотографий каталога, 6 ценовых представлений. JS errors, local HTTP errors, horizontal overflow и browser mutations — 0.
- Массовое исправление: 3 реальные партии, 6 сценариев /48 layouts, 13/13 фотографий; версии черновиков и число операций неизменны.
- Доступы/settings/history: **5 проверок /18 layouts** в light/dark на 320/390/1440. Реальные label/VAT и reviewed versions совпали, ключ пустой, expiry согласован со страницей состояния. Seller-scoped history действительно пуста, старые события не выдуманы. Production settings/key mutations — 0. Mobile dark screenshot просмотрен отдельно.
- Всего production **216 layouts /73 фотографии**. Это read-only smoke поверх настоящей БД/Gunicorn/TLS; состояния конфликта, потерянного POST и записи audit проверены в изолированном real-Flask/CSRF стенде, а не изменением рабочего магазина.
- Fingerprints четырёх наборов accounts/operations/drafts/proposals совпали до/после. Новая таблица и scope index существуют, events=0. Новые endpoints Seller API / price/stock/publication writes — 0; flags/crypto/ledger не менялись.
- Host probe: healthy, scheduler healthy, issues=[]; backup/observer timers и Telegram receiver active. Free 19,883,048,960 bytes, next-backup budget 17,955,364,864 bytes; последний verified snapshot возрастом около 2.5 часов. Эти значения — наблюдение, не бесконечный запас.

CI `1464` основных tests +`385` subtests, JUnit `1849` includes subtests. Exact CI/runtime input hash match и schema/rehearsal hash match проверены до deploy. Telegram delivery фиксируется отдельно после единственной попытки рассылки; деплой и доставка — разные исходы.

Telegram: общий статус доставлен **2/2 активным подписчикам**, одна попытка каждому; rejected/unconfirmed/deferred=0. Сообщение различает выпущенный код, production-проверки и оставшиеся live gates.
