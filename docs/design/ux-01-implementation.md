# UX-01: реализация и приёмка

30.09.2026. **Готово к совместной интеграции; production не изменён**. Все 12 задач реализованы в worktree `/home/athebyme/worktrees/seller-hub-ux-01`, ветка `codex/ux-01-2026-09-30`. Приватный трекер не изменялся. Оркестрация и review — root `gpt-6.1-sol / xhigh`; код и тесты — три явно запущенных worker `gpt-6-luna / max`. Политика закреплена в [AGENTS.md](../../AGENTS.md).

Исходный [согласованный план](ux-01-plan-2026-09-30.md) содержит пересечения с Ozon, зависимости и порядок работ. Перед кодом подготовлены [визуальное направление](ux-01-visual-direction.md), [пять композиций](ux-01-preview.html), [карта действий](ux-01-action-map.md) и [контрольная матрица](ux-01-verification-matrix.md). После просмотра плана владелец поручил реализацию. Плановые статусы первоначального JSON не используются как утверждение об изменении трекера.

## Проверенный снимок

- База изолированной реализации: `ba63371`; финальный [манифест](ux-01-source-manifest.json) — **1207 файлов**, SHA-256 `a69f940834b3f8eb558c5f2a22e618f066fdd69ec7f8d88ac05523328f1e0543`.
- Тестовый образ: `seller-hub-ux-check:20260930-v6`, image ID `sha256:df80f0b0101619ce5be3956cc10b719c93268d99222b7c7092ca0729079f2ea5`. Это образ проверки из существующего `tests/ozon_release/Dockerfile`.
- UX: **109 tests passed**, 38 subtests passed (147 JUnit records); **6/6 стадий**, **493 layout rows**. [Отчёт](ux-01-implementation-artifacts/ux01-report.json), [JUnit](ux-01-implementation-artifacts/ux01-results.xml).
- Ozon regression на том же образе: **1918 tests passed**, 482 subtests passed (2400 JUnit records); **12/12 стадий**, **93 browser checks**, **444 layout rows**. [Отчёт](ux-01-implementation-artifacts/ozon-summary.json), [JUnit](ux-01-implementation-artifacts/ozon-contracts.xml).
- В обоих итоговых прогонах failures/errors/skips — **0**. Во всех browser fixtures provider attempts, неожиданные внешние запросы, JavaScript errors и неожиданные HTTP errors — **0**. Console errors — 0 в fixtures с отдельным console capture. UX допускает ровно один POST авторизации синтетического пользователя; UX fixtures не запускают операции продавца. В Ozon browser fixtures разрешены перечисленные синтетические POST/link/save сценарии в одноразовую БД, без provider I/O.
- Хеши проверены до/после каждой UX-стадии и до/после полного Ozon gate. [Паспорт проверки и индекс изображений](ux-01-implementation-artifacts/acceptance.json).

Проверки выполнялись последовательно, внутри non-root контейнеров с `--network=none`, `--cpus=2`, `--memory=4g`, без mounted production volumes и credentials. Использованы temporary SQLite, вымышленные sellers/accounts, закреплённые локальные assets, отключённый scheduler. Ozon contract tests проверяют writes, retry и миграционные защиты на synthetic transport/DB; они не отправляют запросы маркетплейсам. Состояние production, реальная публикация и миграция актуальной БД здесь не проверялись.

### Объём браузерной проверки UX

| Fixture | Проверки |
| --- | --- |
| Analytics | 48 layouts: 320/390/768/1024/1280/1440 px, light/dark, открытый/сжатый sidebar, text 100/200%; 96 наблюдений локальных API reads |
| Listing | 28 geometry rows, 70 keyboard-focus observations, 8 text-200 cases, 9 сценарных checks |
| Workspace | 37 страниц, 43 layouts, 28 interactions; seller/admin, меню, command palette, настройки и качество |
| Preparation | 25 страниц, 118 layouts, 31 checks, 22 interactions, 12 text-200 cases |
| Operations/pricing | 32 страницы, 256 layouts, 32 interactions, 10 checks; длинные значения, null/zero, ошибки, сверка и обе темы |

Layout rows не суммируются повторно, если `layouts` и `geometry` — два имени одного списка. 96 analytics reads не называются 96 пользовательскими сценариями. Увеличение текста выполняется по computed font-size ×2 отдельно от viewport reflow. Preparation fixture восстанавливает исходные inline styles после каждого случая и проверяет ratio `2.0`/возврат `1.0`; ожидание двух animation frames учитывает существующий font transition.

## Результаты по задачам

### UX-01.1 — цельный визуальный язык

**Изменения.** «Тёплая редакция», Inter/Instrument Serif, существующие Flask/Jinja/Vue/Alpine. Palette tokens остаются в `base.html`; scoped styles согласуют отступы, поверхности, формы, таблицы, подписи и focus. Пять композиций каталога, карточки, подготовки, операций и настроек подготовлены до внедрения на широком/узком экране в двух темах. В приложении объект, канал/кабинет и следующее действие показаны через общие contextual primitives.

**Проверки.** Концепция имеет отдельный scope: 84 layouts, 10 text-zoom cases, 11 interactions. Финальные actual application fixtures приведены выше. Новые пояснения — не меньше 12px, новые mobile navigation targets — не меньше 44px; sidebar text contrast ≥4.5:1 и focus ≥3:1 проверяются в обеих фактических темах.

**Ограничения.** Локальные композиции и синтетический браузер не подтверждают вид production с реальными данными; оставшиеся legacy controls не объявлены полностью переоформленными.

### UX-01.2 — сохранение найденного каталога

**Изменения.** `services/listing_navigation.py` разрешает только canonical/beta/classic каталоги и поддерживаемые параметры marketplace/account/status/link/include_unavailable/search/page/per_page. Контекст сохраняется в открытии карточки, смене режима/участника, form redirects, возврате и Browser Back/Forward. Direct entry получает безопасный fallback по доступной карточке. Query не заменяет tenant authorization.

**Проверки.** Unit/route tests отклоняют внешний адрес, чужой внутренний путь, scheme/host/fragment, encoded separators, control characters и duplicate/unknown keys. Listing browser проверяет filtered beta/canonical/classic, page/per_page и переходы.

**Ограничения.** Нового API сортировки нет: существующие локальные grid/table preferences сохранены. Переносятся только параметры, поддерживаемые текущим каталогом.

### UX-01.3 — аналитика без обрезания KPI

**Изменения.** Причина воспроизведена до правки: при viewport 1024px/sidebar 260px root scrollWidth был **1851px**, при 320px — **836px**. Scoped `minmax(0, …)`/`min-width:0`, переносы и размеры canvas устраняют intrinsic overflow. Широкая таблица остаётся в именованном keyboard-accessible region; overflow body не скрывается. KPI first-load error показывает отсутствие фактов. `loadedPeriod`/revision guards сохраняют согласованный summary/products/daily dataset и отсеивают поздние ответы.

**Проверки.** Та же матрица: 48/48 layouts, большие значения, обе темы, sidebar и text 200%. Четыре Node state regressions проверяют first-load error, confirmed zero, delayed summary/daily. [До, 1024px](ux-01-implementation-artifacts/before/analytics-ba63371-light-1024.png), [после](ux-01-implementation-artifacts/after/analytics-worktree-light-1024.png).

**Ограничения.** Ширина внешнего аудита неизвестна; указанные размеры относятся к pinned-assets synthetic fixture.

### UX-01.4 — компактное меню

**Изменения.** Восемь групп: обзор, товары, цены, операции, общение, аналитика, конкуренты, продвижение. Открыта текущая группа; настройки, помощник и документация закреплены. Внутренний товар, карточка WB/кабинета, черновик Ozon и источник названы различимо. Command palette согласована с меню. Все **58 исходных destinations**, admin branch, права и rollout gates сохранены; новое место каждого входа — в карте действий.

**Проверки.** Frozen baseline action manifest, route/render tests, Enter/Tab, collapsed sidebar, single-open panels, поиск/Arrow/Escape palette, seller/admin и themes в workspace fixture.

**Ограничения.** Группировка не вводит новую модель данных и не выдаёт seller доступ к административным инструментам.

### UX-01.5 — рабочая область карточки

**Изменения.** Общая идентификация и режимы «Обзор / Управление» на прежних URL. Overview сохраняет фото, атрибуты, модерацию, freshness и ошибки; Management — внутреннюю связь, price proposals и exact FBS warehouse stock. Raw snapshots доступны через диагностику, guards остаются видимыми. Возврат использует .2.

**Проверки.** Listing fixture и полный Ozon gate проверяют оба режима, account/channel, связь, категории и warehouse protections. [Узкий обзор](ux-01-implementation-artifacts/after/listing-after-matrix-390-light-overview.png), [широкий обзор](ux-01-implementation-artifacts/after/listing-after-matrix-1440-dark-overview.png).

**Ограничения.** WB imtID не превращён в связь внутренних товаров; текущие Ozon source/write/readback contracts сохранены.

### UX-01.6 — источник, подготовка, отправка и результат

**Изменения.** На existing source/internal/draft/review/upload/result routes показаны текущий объект и следующее действие. Полный путь из семи этапов раскрывается отдельно. Локальный импорт/save отделён от публикации; unavailable шаги объясняются без имитации готового flow. Fresh exact-version/set review, отдельное подтверждение отправки, category/source/dirty/quarantine/AI evidence остаются обязательными. Imported не означает модерацию или продажную видимость.

**Проверки.** Preparation matrix: 25 страниц, 118 layouts, 31 checks, 22 interactions; old/classic и Vue, default-closed screenshots, оба канала/темы, source/error links и text 200%. Ozon gate повторяет exact-set/version, two-step upload и AI guards. [До](ux-01-implementation-artifacts/before/journey-ba63371-draft_detail_vue-light-390.png), [после](ux-01-implementation-artifacts/after/journey-worktree-draft_detail_vue-light-390.png).

**Ограничения.** Новый publication engine не создан. Реальные публикационные проверки остаются отдельным безопасным E2E интеграции Ozon.

### UX-01.7 — читаемые истории и результаты

**Изменения.** Общие входы ведут в родные WB/Ozon истории по типу и каналу с account/rollout context. WB `completed` 2/0/2 показывает «Завершено с ошибками», mixed — частичный результат, неполные счётчики — неподтверждённый исход. Provider sync показан отдельно. Строки ошибок видны до диагностики: артикул/ID, причина, следующий допустимый шаг. Десять подтверждённых local reason codes получают русский смысл; human error/message сохраняются, неизвестные коды не интерпретируются. Полный исходный JSON доступен в закрытом disclosure. Неоднозначный ID не превращён в ссылку. Перед новым изменением нужна сверка WB.

**Проверки.** Template regressions покрывают counters, uncertain/unknown/no-retry, reason mapping, legacy string, malformed shape, null/numeric/nested values, XSS escaping и raw preservation. Operations browser проверяет видимые причины вне closed details. [До](ux-01-implementation-artifacts/before/wb_bulk_detail-light-390.png), [после](ux-01-implementation-artifacts/after/wb_bulk_detail-light-390.png).

**Ограничения.** Историческая запись без причины получает честное пояснение, не выдуманный диагноз. `can_revert`, snapshots, reconciliation, quarantine и rollback не ослаблены; uncertain не предлагает повтор записи.

### UX-01.8 — качество и медиа

**Изменения.** Contextual navigation связывает WB classic/beta quality, Ozon quality и Image Lab. Старые filters/mass actions/beta prioritization сохранены. WB-only инструменты явно относятся к WB; source photos доступны в supplier detail. Тип изменяемого объекта не смешивается.

**Проверки.** Workspace route/render/browser: quality classic/beta, Image Lab, narrow/wide, обе темы, text 200%, links/focus. В промежуточном прогоне Image Lab дал 400 из-за DNS-неразрешимого synthetic `.test` photo URL: исправлена fixture, runtime SSRF validator сохранён. Финальный gate ошибок HTTP не содержит.

**Ограничения.** Подготовка медиа, локальный artifact и публикационная галерея остаются разными действиями; provider media pipeline не перестраивается.

### UX-01.9 — текущие, расчётные и предлагаемые цены

**Изменения.** Общая навигация связывает WB current values, расчёт/preview/proposals, историю, защиту, supplier formulas и мониторинг; Ozon proposals остаются отдельным контекстом. Baseline — снимок при создании заявки, proposed — цена продавца. Канал, account, currency и exact FBS/rFBS warehouse различимы; buyer price и скидка площадки остаются unknown. Null закупочной цены не вызывает JavaScript error; подтверждённый числовой ноль сохраняется. Формы и KPI переносятся, таблицы прокручиваются локально.

**Проверки.** Pricing template/Node regressions и 256 layouts operations/pricing matrix: classic/Vue, nullable/zero/long prices, разные currency/warehouses, обе темы, text 200%. Полный Ozon gate проверяет preview/confirm/reconcile/rollback и исходные write payloads.

**Ограничения.** Расчёт/formulas и операции автоматически не запускаются. Свежий Ozon handoff уточняет: текущая подпись `old_price` «До скидок» не доказывает экономическую базу до всех скидок. Exact mapping собственного SKU в API/кабинете и buyer/marketplace discount остаются открытыми; UX не усиливает эту трактовку.

### UX-01.10 — настройки и здоровье подключений

**Изменения.** Подключения, товары, уведомления и диагностика имеют общий nav. Enabled, stored connection health, credential expiry и platform rollout разделены. Authorization blockers видны рядом с действием; sync schedules обозначают канал/dataset. GET использует existing helpers без provider I/O. Failed Ozon pending-count read показывает unknown, не 0. Exact account сохраняется при health→auto-publish; inactive account не теряется. Secrets не выдаются в HTML.

**Проверки.** Route/template tests для active/inactive/stale/foreign accounts, generic WB context, expiry, rollout и failed counts; workspace browser и Ozon credential/account-history gate. [Узкие настройки](ux-01-implementation-artifacts/after/worktree-api_settings-light-390.png), [широкие](ux-01-implementation-artifacts/after/worktree-api_settings-dark-1440.png).

**Ограничения.** Saved health/grants не доказывают текущую доступность каждого provider метода. Access flags, keys и автопубликация не изменялись.

### UX-01.11 — сохранность действий и сценариев

**Изменения.** Матрица применялась к каждому пакету. `scripts/check_ux01.py` проверяет frozen source до/после стадий, полноту reports/telemetry, непустые tests, skips, внешние/provider попытки и неожиданные записи. Action manifest фиксирует все 58 прежних menu destinations. До/после actual app и концепция имеют отдельные scopes.

**Проверки.** Итоговые 6 UX и 12 Ozon стадии на одном source/image; assertions не ослаблялись ради зелёного отчёта. В ходе проверок устранены реальные overflow/contrast/null-price defects и ошибки fixture (неверный DOM selector, накопление text scale, font transition, theme restore); финальные результаты относятся к исправленному harness. Полный v4 Ozon gate и отдельный повтор воспроизвели преждевременную проверку photo request counter в `vue_link_category_browser.py`: reactive loading label появляется до Vue src commit/lazy-image GET. Тест теперь ограниченно ждёт ответ точного synthetic запроса; exact queries/counts и hidden/no-auto-retry/unmount guards сохранены, runtime photo component не менялся. Итоговые результаты отчёта относятся только к повторённому полному v5 gate. [Артефакты](ux-01-implementation-artifacts/README.md).

**Ограничения.** Реальные товарные записи, публикация/rollback, внешние API/LLM, migration rehearsal на актуальной production-копии, live media CDN equivalence и production browser E2E не выполнены. После совместной интеграции требуется новый общий source manifest и gates.

### UX-01.12 — действующий лимит мониторинга

**Изменения.** Установлено из model/handler/worker: сохранённые 100000 — legacy настройка, обработка ограничена **1000 за цикл**; это не квота хранения всех групп. Подсказка/input названы по этому смыслу, stored/effective values разделены. Unrelated save сохраняет legacy value. Явное изменение принимает только JSON integer 1..1000; bool/fraction/string/out-of-range дают 400 до создания/мутации строки. UI отправляет поле только после явного редактирования.

**Проверки.** 30 targeted tests и 7 subtests в первоначальном пакете, повторены в финальном UX gate: legacy GET/save, границы, invalid input без partial mutation, tenant authorization и actual JS payload. Старый silent-clamp test заменён проверкой явного контракта.

**Ограничения.** Backend fix ограничен подтверждённым settings save дефектом. Model default, scheduler, import/group/comparison caps и сохранённые данные не изменены.

## Повтор проверки и совместная интеграция

В подготовленном non-root image выполнить последовательно, каждый прогон — в новом контейнере без volumes/credentials:

```sh
python scripts/check_ux01.py --manifest /app/docs/design/ux-01-source-manifest.json --output /artifacts/ux01 --chromium /usr/bin/chromium
python scripts/check_ozon_release.py
```

Host manifest verify обрамляет полный Ozon gate; точные команды контейнеров и source verification сохранены в паспорте проверки. Результат не означает deployment: test image не используется как production image.

**Переносить только собственные коммиты:** `f681d74` (policy), `c4dd103` (concept/plan), `a27ee3c` (Luna/max), `1bb28b7` (реализация/тесты/AGENTS/manifest), затем commit этого итогового отчёта и артефактов. **Не переносить snapshot commits `ae6994b`, `6488975`, `ba63371` и не сливать всю worktree-ветку.** Они содержат чужую незавершённую базу Ozon.

Основная рабочая копия не изменялась. На сверке перед приёмкой runtime-файлы из нашего scope совпадали с baseline; пересечение — `AGENTS.md`. В main также обновлены Ozon status и pricing contract, отсутствует один baseline helper `scripts/prepare_ozon_ai_release.py`: UX-коммиты не должны восстанавливать этот чужой файл. При интеграции сохранить свежие Ozon price semantics, все write/readback guards и текущие rollout flags, разрешить конфликты, создать новый общий manifest и повторить UX/Ozon gates. Состояние main могло продолжить меняться после сверки.
