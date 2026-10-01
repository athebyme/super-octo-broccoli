# Ozon integration audit — 01.10.2026

Статус: synthetic lifecycle acceptance расширена, один подтверждённый UX-блокер исправлен. Это не live-публикация и не приёмка общего merged tree: итоговый Ozon/UX gate в текущем ограниченном окружении не запускался.

## Подтверждённый дефект

Страница `/marketplaces/drafts` добавляла аккаунт в список отправки только при заданном `default_vat`, хотя публикационный сервис проверяет факты конкретного черновика. Поэтому новый либо готовый черновик с явно выбранным и прошедшим проверку НДС нельзя было отметить к отправке, если необязательное значение по умолчанию аккаунта отсутствовало. Это воспроизводится на подключённом аккаунте без истёкших credentials: заполнить НДС в карточке, получить свежую publishable-валидацию, оставить default НДС аккаунта unset и открыть список отправки. UI исключал аккаунт из `can_publish`.

Исправление в `routes/marketplace_drafts.py` убирает только глобальный default-VAT gate из списка отправки. Активность аккаунта, соединение, наличие и срок credentials сохранены; per-draft VAT по-прежнему обязателен и проверяется. На странице настроек текст теперь объясняет, что default — это подстановка для новых карточек и что его можно оставить пустым. Регрессия `test_ready_explicit_vat_draft_can_be_selected_without_account_default_vat` подтверждает обе стороны: готовая карточка с собственным НДС доступна для отправки, а карточка без собственного НДС остаётся непубликуемой.

Приоритет: P2 — ложная блокировка безопасного действия, без опасного обхода проверки или автоматической записи. Исправление покрыто новой synthetic browser journey и route/service regression.

## Путь и проверки

Добавленная lifecycle-проверка использует реальные Flask routes, CSRF, локальную БД, сервисы подготовки/валидации, AI reservation/review/apply, publication worker, task/readback и tenant checks. Подменены только AI transport и граница Ozon provider. В synthetic happy path проверены выбор источника и категории, атрибуты, фото в исходящем payload, положительное предложение AI для seller review, выборочное применение и повторное открытие, новое подтверждение перед отправкой, одна физическая synthetic provider write, worker task и full readback. Отдельные ветки проверяют невалидное AI-предложение без записи, ручной ремонт с повторной подготовкой, новый источник с пустым черновиком до ручного заполнения габаритов/НДС, и потерянный ответ с quarantine, нулём автоматических повторов и безопасной сверкой.

Результат browser journey из изолированного worker run: 12 checks, 20 layouts; `provider_attempts=0`, внешние запросы, неожиданные HTTP и JavaScript ошибки отсутствуют. Counters по synthetic веткам разделены: успешная AI-ветка — 1 AI call / 1 provider write / 4 readbacks; invalid — 1 AI call / 0 writes; новая карточка — 1 source / 1 draft / 1 category selection / 1 ручное заполнение packaging+VAT / 1 свежая validation / 1 write / 4 readbacks; ambiguous write — 1 write / 0 retries / 1 quarantine / 1 safe reconciliation. Фото подтверждено в submit payload; этот тест не утверждает декодирование реального фото CDN.

Артефакт journey: `/tmp/ozon-audit-journey-r6/ozon-complete-journey-browser.json`. Источник: `tests/ozon_release/ozon_complete_journey_browser.py` и `tests/test_ozon_complete_journey.py`.

Дополнительно расширен analytics browser fixture: прежний success matrix сохранён, добавлены состояния пустого списка товаров и ошибки сводки на 390/1024/1280/1440 px, light/dark. Проверены понятные empty/error labels, доступность/отсутствие stale KPI, локальная таблица и отсутствие горизонтального переполнения. 16 новых state cases прошли; общий fixture сообщил 48 layouts, 0 page overflow, 0 unexpected API/external calls, 0 blocked writes и 0 JS errors. Артефакт: `/tmp/ux01-analytics-audit-20261001/analytics-report.json`.

Для common-content редактора добавлены отдельные seller catalog entry points и обязательный browser stage. Fixture проверяет bulk selection, подтверждённый `common_only` effect, ручной пустой override описания, фото reorder, stale preview/apply conflicts, delayed preview/apply/readback и tenant-safe readback. Runner принимает только exact local preview/apply POST paths с отдельными счетчиками и требует не менее 8 checks и 4 layouts. focused Python/UI/route/service/runner suites: `41 passed, 7 subtests passed`; `node --check`, `py_compile` и `git diff --check` прошли.

Обновлённый common-content browser fixture в текущем restricted окружении **не прошёл**: synthetic app/setup закончился до browser launch, когда Werkzeug `make_server` попытался открыть `127.0.0.1:0` и получил `PermissionError: [Errno 1] Operation not permitted` на `socket.socket`. Artifact/report от этого запуска не создан, поэтому stage имеет статус blocked/not-tested, а не passed. Socket/network policy не обходилась.

Фокусированные тесты кода runner и Ozon lifecycle: `17 passed, 7 subtests passed`; `py_compile` и `git diff --check` прошли. Полный Ozon release runner, полный UX-01 runner и merged candidate gate не запускались. В новом restricted environment local Flask bind запрещён (`PermissionError: [Errno 1]`), Docker socket также недоступен; эти ограничения не заменялись статическими проверками или ослаблением порогов. Ранее проведённый journey browser run указан как отдельное synthetic evidence, а не как проверка финального объединённого дерева.

## Области Ozon и внешние ограничения

| Область | Текущий вывод |
| --- | --- |
| Подключение и аккаунт | UI различает credentials, expiry, method grants, rollout и observed health. Метод-grant не доказывает доступность метода. Новый ключ не переносился в приложение и в этом аудите не читался. |
| Каталог и категории | Существуют seller-scoped синхронизация, checkpoint/read paths и review выбора категории/типа. Новые production reads и полный live обход не выполнялись. |
| Источник, общий контент, черновик и фото | Local source/editor paths отделены от marketplace draft. Synthetic journey использует локальное тестовое фото; source media cache miss и холодную доставку этим артефактом не закрывает. |
| AI review | Путь предлагает значения с source evidence и требует явного seller apply. В journey транспорт synthetic. Реальный положительный Flash proposal этим прогоном не подтверждён; прежние отклонённые предложения не считаются успехом. |
| Подготовка, публикация, task и readback | Сервисы/worker и состояния проверяются end-to-end через synthetic adapter. Physical external provider attempts равны нулю. Реальная карточка не отправлялась; CDN URL normalization/moderation не подтверждены. |
| Цены, остатки и склады | Seller price/proposal и read-only stock lanes остаются отделены. Buyer price, marketplace discount и семантика `old_price` остаются unknown. Ранее полученный 403 на `/v1/product/prices/details` не повторялся; новых probes не было. |
| Finance, fulfillment, inbox | У них отдельные seller-scoped read workspaces и контракты. По последней переданной root health observation от 22:25 UTC reviews/questions были unknown после `inbox_access_denied`, fulfillment — stale после `account_busy`. Это не доказывает дефект интеграции; повторных отказов/403 запросов не запускали. |

Подробные контракты и прошлые проверенные статусы остаются в `OZON_INTEGRATION_STATUS.md`, `docs/design/ozon-release-regression.md` и тематических `docs/design/ozon-*.md`. Реальные обращения, записи, AI calls, buyer/inbox probes и изменения аккаунта в этом аудите не запускались.

## UX-01 acceptance status

| Код | Synthetic evidence | Граница текущего аудита |
| --- | --- | --- |
| UX-01.6 — источник → подготовка → отправка → результат | Historical preparation/two-step review gates плюс новая Ozon journey с task/readback/quarantine прошли в отдельных offline runs. Новый per-draft VAT regression закрыт. | Финальный merged browser/release gate здесь не запускался; live publish/moderation и media normalization не тестировались. |
| UX-01.9 — цены и предложения | Исторические pricing/operations matrix и Ozon proposal preview/confirm/reconcile checks остаются в regression suite. | Новый live buyer-price/marketplace-discount источник неизвестен; текущая подпись `old_price` не повышается до подтверждённой экономической семантики. Финальную merged matrix не повторяли. |
| UX-01.10 — настройки и здоровье | Существующие route/template/workspace/credential/account-history проверки покрывают active/inactive/stale/foreign accounts, expiry, rollout и failed-count states. | В этом аудите не меняли доступы и не делали provider I/O; сохранённый health/grant не доказывает текущую доступность каждого метода. Финальный merged browser gate не запускался. |

## Следующие условия принятия

1. Root интегрирует этот packet вместе с WB/CAT commits и обновляет source manifest/AGENTS по принятой процедуре.
2. Финальный Ozon runner включает `tests/test_product_selection.py`, `tests/test_wb_bulk_review_key_migration.py` и новые common-content/WB focused suites. Их отсутствие в отдельной базовой копии до интеграции соседних commits — dependency gap, а не причина ослаблять stage.
3. На финальном immutable source/image повторяются полный Ozon и UX-01 gates; browser artifacts должны быть получены в допустимом network-none harness.
4. Любой ограниченный live read/write pilot остаётся отдельным решением. До него нужны точный seller-owned SKU/account, durable shared ledger/limiter, bounded methods/budgets и отдельное review исходных фото, размеров и VAT. Этот отчёт не является разрешением на реальную запись или изменение настроек.
