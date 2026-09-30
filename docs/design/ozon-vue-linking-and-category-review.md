# Ozon: ручная связь в Vue-карточке и проверка смены категории

**Статус 26.09: реализовано в candidate worktree; итоговая frozen browser/integration проверка и production-приёмка ещё не завершены.** Ниже исходный проект; фактические тесты объединены в `tests/test_ozon_vue_link_category.py` и `tests/ozon_release/vue_link_category_browser.py`. Доказательства и ограничения ведутся в [отчёте выпуска](../operations/2026-09-26-ozon-resumable-reads-and-vue-review.md).

> Исторический текст проекта (статус реализации указан выше).

**Статус: предложение для следующей волны. Не реализовано.** Это два отдельных
локальных пакета W6 и W7 из [плана запуска](../OZON_LAUNCH_READINESS_PLAN_2026-09-24.md).
Нынешние Vue-каталог, одиночный редактор, bulk repair, account health и
quarantine остаются принятой базой; этот документ не меняет их runtime и не
объявляет готовой публикацию в Ozon. Оба пакета не вызывают Ozon, WB,
поставщика, LLM или media write и не создают publication/commercial operation.

## Пакет 1 — ручная exact-связь из основной Vue-карточки (W6)

### Проблема и действующая граница

`templates/marketplace_listing_beta_detail.html` отправляет «Выбрать вручную»
на `/<listing_id>` в классический экран. При нескольких точных внутренних
кандидатах продавец покидает основную `/marketplaces/listings/view/<id>`.
`static/marketplace-detail-beta.js` прямо рекомендует классическую версию.
При этом `MarketplaceProductLinkService.context()` уже отдаёт bounded
кандидатов, а `link()` и `unlink()` принимают `expected_link_version` и ведут
журнал. Vue bootstrap `GET /marketplaces/listings/view/<id>` уже включает
`product_link`, но Vue использует только `canonical_product`.

Смысл ручной связи: продавец выбирает **уже существующую seller-owned
`ImportedProduct.id`** и явно подтверждает, что это та же внутренняя карточка.
Поиск по названию, артикулу или WB nmID помогает найти строку, но не является
доказательством identity и никогда не создаёт автоматическую связь. Exact
автоматика в `services/marketplace_product_links.py` остаётся отдельной:
она опирается на проверенные source/offer identities, а не на title,
фотографию, AI или близость цены. Ручной выбор не меняет `Product.nm_id`,
`SupplierProduct`, source FK, Ozon listing identity или фото публикации.

`link()` уже запрещает прямую замену существующей связи и повторное
использование той же внутренней карточки другим listing того же кабинета.
`unlink()` уже запрещает отвязку, если listing используется опубликованным
Ozon-черновиком. Новый UI не превращает «выбрать другого» в скрытую пару
unlink+link: сначала отдельный review отвязки, затем после свежего GET
отдельный review нового кандидата. При занятом draft действие недоступно
с серверной причиной. Для display и записи кандидат с повреждённой
seller/source/WB FK-цепочкой должен отклоняться fail-closed, без подмены
другим `ImportedProduct` по title, фото, barcode или похожему ID.

### Предлагаемый контракт

| Место | Изменение |
| --- | --- |
| `GET /marketplaces/listings/view/<id>` | Сохраняет текущий detail bootstrap. Добавляет серверные `link_actions`/`reasons`: точная версия, можно ли link/unlink, конкретная блокировка и краткий impact отвязки. Никаких client-derived разрешений по badge. |
| `GET /marketplaces/listings/view/<id>/link-candidates?q=...` | Новое seller-scoped read-only действие для поиска в текущем listing: один `q` длиной до 200, без неизвестных/повторных параметров; до 20 результатов из `MarketplaceProductLinkService.search_candidates()` (service cap 25). Ответ содержит listing ID/version, кандидатов и признак исчерпания, без неограниченной пагинации или provider I/O. Если выбран другой канал во время запроса, ответ старого канала игнорируется. |
| `POST /marketplaces/listings/<id>/link` | Переиспользует существующий route и `MarketplaceProductLinkService.link()`: exact `imported_product_id`, positive `expected_link_version`, CSRF, seller scope, duplicate/occupied/rebind guards, audit. Новый Vue review не ослабляет ни одну проверку. |
| `POST /marketplaces/listings/<id>/unlink` | Переиспользует существующий route и `MarketplaceProductLinkService.unlink()`: exact версия, CSRF, bound-draft prohibition, audit. Отвязка не происходит как побочный эффект поиска/выбора нового кандидата. |

Кандидат показывает название, source/vendor identity, внутренний ID, WB
identity только при проверенной seller-owned цепочке, состояние AI-кэша
только как справку, и **реальное фото именно этой карточки**, если есть
проверенный источник. Display URL строит сервер из соответствующего
`ImportedProduct` через существующий authenticated
`/api/photos/imported-product/<id>/<slot>?deferred=1` и тот же приоритет
supplier slot/legacy snapshot, что в `services/source_photo_display.py` и
`routes/photos.py`. При отсутствии точного слота показывается текстовый
placeholder; нельзя брать Ozon listing media, произвольный URL кандидата,
фото другой строки или generic WB фото. Фото остаётся display-only и не
становится identity evidence. Список и DOM ограничены 20 строками; ошибки
cache/miss оставляют читаемые title/IDs и доступное действие.

Перед link dialog показывает две стороны: текущий Ozon listing с account,
offer ID и status и выбранный `ImportedProduct` с source/vendor/WB identity.
Кнопка «Связать карточки» доступна только после явного выбора кандидата и
проверки exact просмотренной версии. Перед unlink отдельный dialog показывает
текущую связь, что перестанет переиспользоваться в локальных представлениях,
и серверную причину блокировки; он не обещает удалить товары, черновики,
операции или данные Ozon. Никаких предложений «самый похожий» и
предустановленного кандидата. После успеха свежий detail GET обновляет
каналы, список кандидатов, историю и action availability.

Lost POST, timeout или разрыв сети дают состояние «результат неизвестен»;
Vue не повторяет POST и не показывает успех по локальному клику. Продавец
явно делает GET текущего listing, видит stored link/version/events и отдельно
решает, нужно ли новое действие. Version conflict также сохраняет выбранного
кандидата в памяти, показывает прежнюю и текущую связь и требует явного
принятия новой просмотренной версии перед новым POST. Search input и выбор
сохраняются при неуспешном GET/401, но не отправляются, пока сессия не
восстановлена и seller scope заново не прочитан. 403/404 не раскрывают
чужие title, фото и IDs. Busy reconcile и active link-write показываются
по серверному ответу; двойной submit блокируется. Фокус после поиска,
review, ошибки и закрытия dialog возвращается в осмысленный control;
клавиатурой доступны все строки и кнопки. Возврат в каталог сохраняет
валидированный account/filter/search контекст, если переход был из него.

### Файлы и приёмка пакета 1

- `services/marketplace_product_links.py`: bounded candidate/impact
  projection, точная photo identity и fail-closed FK checks; существующие
  `link()`/`unlink()` и журнал остаются владельцами записи.
- `routes/marketplace_listings.py`: read-only search и action projection;
  текущие POST routes остаются единственными write endpoints.
- `templates/marketplace_listing_beta_detail.html`,
  `static/marketplace-detail-beta.js` и
  `static/marketplace-catalog-beta.css`:
  search, review, impact, responsive dialog и состояния; убрать переход
  «в классическую версию» только после parity.
- `tests/test_marketplace_product_links.py` и новый
  `tests/test_marketplace_vue_linking.py`: seller/foreign
  scope, FK drift, search cap/invalid query, occupied candidate, linked
  rebind prohibition, bound-draft unlink, CSRF, version conflict и audit.
  Существующий service test покрывает tenant/version/audit, но не Vue journey.
- Новый `tests/ozon_release/linking_browser.py` на full-app Chromium без
  provider: ambiguous exact match → search →
  candidate review → link → reload/event; отдельно blocked unlink, allowed
  unlink, stale version, lost POST с подсчётом physical POST (ровно один),
  session expiry, foreign GET/POST, пустой поиск, отсутствие фото,
  keyboard-only и возврат в фильтрованный каталог.

## Пакет 2 — точный impact смены категории одиночного черновика (W7)

### Проблема и действующая граница

`static/ozon-draft-editor.js:applyType()` сейчас требует отсутствие `dirty`,
показывает общий `window.confirm` и сразу отправляет `product_type_id` через
`POST /marketplaces/drafts/<id>`. На сервере смена типа в
`MarketplaceDraftService.update_draft()` заново строит `attributes_json`
из сохранённых source facts, очищает `complex_attributes_json` и
`attribute_removals_json`, снимает прежний `category_mapping_id` и
инвалидирует validation. Seller не видит точных теряемых значений. Принятый
bulk repair имеет собственный impact review; этот пакет относится только к
одному draft и не меняет bulk parser/contract.

### Предлагаемый контракт

| Место | Изменение |
| --- | --- |
| `GET /marketplaces/drafts/<id>/category-impact` | Новый строго read-only seller-scoped preview с `expected_version` и ровно одним target: positive `target_product_type_id` либо явный `clear=true`; optional strict `save_mapping=false|true` (для clear только false). Неизвестные/повторные/неверно типизированные query args отклоняются. Читает **сохранённый draft именно просмотренной версии**, target type, current schema/source-fact identity и полный локальный результат того же type-change расчёта, что применит write. Никакого refresh, Ozon read, Ozon write или draft save. |
| Ответ preview | Старый/новый type path; exact `draft_id`, account ID, `version`; перечисление исчезающих или меняющихся `attributes` с именем, identity и сохранёнными values, всех затронутых экземпляров `complex_attributes` с group/position/value, `attribute_removals`, прежнего mapping и новых auto-mapped значений. Отдельно counts и причины; без утверждения publishable. Бounded paging для большого списка, stable digest и `has_more`; значения нельзя молча обрезать, неполный/слишком большой impact не выдаёт apply token. |
| `POST/PATCH /marketplaces/drafts/<id>` при смене типа | Сохраняет существующий `expected_version`, patch и CSRF, но требует top-level `category_review_token` для любого фактического изменения `product_type_id`, включая clear и classic form. Сервис пересчитывает тот же impact и проверяет signed token **перед мутацией** в той же DB-транзакции. Token связывает seller/user/account/draft, exact viewed version, old/target type, source-facts и document digests, target schema identity/hash, `save_mapping`, полное impact digest и короткий срок; после успешного изменения старая версия недействительна. Drift даёт 409 с новой read-only проверкой, без частичного save. |

Preview вычисляется из тех же сохранённых документов и нормализаторов, что
`update_draft()`, а не из DOM и не из повторного угадывания схемы. Один
server-owned helper формирует prospective type-change и impact; update
переиспользует его, чтобы preview и запись не расходились. Применение
остаётся локальным draft save; publication и media upload не запускаются.
Существующий optimistic `version` обязателен, token не заменяет его.
При смене reference/type availability или source-fact snapshot между GET и
POST сервер отвергает token. Для больших документов preview ограничен
существующими нормализованными пределами (`MAX_ATTRIBUTES`, complex groups,
values), страницей до 25 impact rows и бюджетом ответа; при превышении
безопасного общего byte/row budget выдаётся `impact_too_large` **без token**
и понятный путь отдельной очистки/поддержки, а не сокращённый список с
разрешённой записью. UI требует загрузить все страницы exact digest до
активации confirm; повторная страница другого digest отклоняется. Token
не содержит raw values, а values не пишутся в URL или browser storage.

В dialog продавец видит «Сохранено сейчас → после выбора» по именам и
значениям, отдельно обычные характеристики, составные группы, план
удалений и mapping. `save_mapping` по умолчанию выключен и имеет
самостоятельную понятную подпись; его переключение инвалидирует preview
и требует нового GET. Для пустого impact явно написано «Сохранённые
характеристики не будут удалены», но type change всё равно требует
подтверждения. `dirty`-поля остаются в памяти и не входят в authoritative
preview. При них действие «Применить категорию» заблокировано, есть
«Сохранить текущие правки» и «Вернуться к вводу»; сохранение выполняется
только по отдельному клику, после успеха preview читается заново с новой
версией. Закрытие dialog, back, ошибка GET и недоступный reference не
сбрасывают ввод. Нельзя автоматически сохранять или отбросить правки
ради открытия review.

После lost POST/timeout нет автоматического повторения. Отдельный GET
`/<id>/editor` показывает сохранённый type/version/values; пользователь
выбирает «Использовать просмотренное состояние», затем при необходимости
создаёт новый preview и отдельно подтверждает новый POST. Неизвестный
исход остаётся неизвестным до readback. Version conflict показывает
old/current по полям, сохраняет локальный ввод, сбрасывает старый token и
выбор подтверждения. 401 требует повторного входа без утечки form values;
403/404 не раскрывают чужой draft. GET/search имеют timeout и abort при
смене draft/target; focus возвращается к category control, dialog имеет
доступные labels, summary и Escape без потери ввода.

### Файлы и приёмка пакета 2

- `services/marketplace_drafts.py` и/или выделенный
  `services/marketplace_category_review.py`: общий pure impact builder,
  token verification и atomic preflight для `update_draft()`; source facts,
  schema и old values берутся из exact seller-owned draft.
- `routes/marketplace_drafts.py`: новый GET preview, обязательный token
  для category-changing JSON/form writes, строгие query/body types;
  остальные поля PATCH и отдельный bulk repair не меняются.
- `services/marketplace_draft_editor.py`,
  `templates/marketplace_draft_detail.html`,
  `static/ozon-draft-editor.js` и `static/ozon-draft-editor.css`:
  dialog с пагинацией
  impact, сохранением dirty ввода, readback и фокусом. Classic detail
  получает совместимый review перед своим category-changing form POST.
- `tests/test_marketplace_drafts.py` и новый
  `tests/test_marketplace_category_review.py`: exact before/after для simple, complex groups,
  removals и mapping; fresh/stale/foreign version, wrong target/token,
  changed source/schema, oversized impact без token, отсутствующий CSRF,
  duplicate/unknown keys, lost POST и отсутствие provider/operation write.
  Существующий `tests/ozon_release/bulk_repair_browser.py` проверяет
  bulk impact, но `tests/ozon_release/browser.py` для одиночного editor
  проверяет только save/reload; нужен отдельный category journey.
- Новый `tests/ozon_release/category_review_browser.py` на full-app
  Chromium: заполнить несколько simple/complex значений и
  removal → сохранить → выбрать другой type → увидеть точные потери →
  отменить и проверить неизменность → подтвердить → readback. Отдельно
  dirty input, stale preview, reference drift, oversized guard, lost POST
  с подсчётом одного physical POST, повторный вход, клавиатура и focus.

## Общая визуальная и проверочная матрица

Оба пакета используют текущие Seller Hub tokens, Inter/Instrument Serif,
существующий light/dark theme и настоящие карточки/фото; новый hero или
декоративные метрики не нужны. На 320 и 390 px dialogs становятся одной
колонкой с ограниченной высотой и внутренней прокруткой; на 768 и 1440 px
сравниваемые стороны видны рядом, если хватает места. Длинные title,
offer ID, values и ошибки переносятся, не расширяя viewport.

| Экран/состояние | 320 | 390 | 768 | 1440 |
| --- | --- | --- | --- | --- |
| Link: поиск, 20 строк, реальное фото/placeholder, review, blocked unlink | light/dark | light/dark | light/dark | light/dark |
| Category: saved/dirty, simple+complex+removals, stale/lost POST, длинное значение | light/dark | light/dark | light/dark | light/dark |

Для каждого размера и темы — screenshot **после Vue mount**, web fonts и
данных; проверка горизонтального overflow, JS errors, видимого фокуса,
порядка Tab/Escape, 200% reflow, reduced motion, no accidental POST при
GET/переключении/скриншоте. E2E запускается на синтетической БД с
настоящими Flask routes/CSRF в isolated network-none окружении; реальные
seller operations, provider calls, production writes и deployment в эти
два пакета не входят. Production acceptance после отдельного решения —
только authenticated GET/browser без нажатия link/unlink/category apply.
