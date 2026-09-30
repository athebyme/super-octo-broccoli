# Ozon: проверка сценариев и выпуск

Продолжение общего плана `OZON_LAUNCH_READINESS_PLAN_2026-09-24.md`. Развёртывание явно поручено пользователем. Таблица фиксирует доказательства, а не заменяет проверку конечного результата наличием теста или флага.

Уровни проверки: **U/API** — локальные сервисы/маршруты с синтетическим provider; **B** — реальные templates/Vue в Chromium с synthetic backend; **S** — полный новый runtime на согласованной копии рабочей базы (либо отдельно отмеченный synthetic-provider runtime); **L** — реальный Ozon; **P** — приложение после deployment. Значение «предстоит» не считается пройденным gate.

Волна read-синхронизаций и Vue review принята на runtime `d7d3f54d…`: healthy, restart=0. Backend 1702 tests + 452 subtests унаследованы от CI `c101…`; на точном новом image прошли 72 browser checks / 344 layouts. Production: общий UI 25 страниц / 150 layouts / 60 фото, bulk 6 / 48 / 13 фото; native read-only проверка 5/24 подтвердила фото ID 57 после 202→200, старые warehouse/FBS jobs и восемь protected групп сохранились. **Полный запуск Ozon остаётся открытым:** buyer price/inbox, реальные create/update и stock-write пилоты, shipping, hosted CI и пользовательская проверка не доказаны этой read/UI приёмкой. После сообщения владельца о полном доступе `/v1/roles` подтвердил 11 exact grants загрузки карточек; реальная публикация и другие методы этим не проверены. [Отчёт и границы](operations/2026-09-26-ozon-resumable-reads-and-vue-review.md).

| Сценарий | Что должно быть проверено | Имеющееся evidence | Следующая проверка |
| --- | --- | --- | --- |
| Качество карточек | Полный список/фото, объяснимые причины, GET без записей, очередь после закрытия вкладки | U: 130 + 1 tests; B: 12/72; P: 4/16, 25/25 фото, один CSRF POST, 8111 завершённых локальных пересчётов | 5 реальных пользователей и длительный пилот; не разрешает публикацию |
| Подключение | Client-Id/key, права, отсутствие ключа в HTML/state, очередь и reload | U/API account sync; B connection 1440/390/320, обе темы; S: реальная авторизация/CSRF | S: rotation; L: ключ и актуальные права |
| Восстановление доступа | Неверный/истёкший ключ, exact Client-Id + viewed version, сохранение каталога/истории | U/API: expiry/dedup/rotation/busy/tenant/atomicity; S/B: 7 сценариев/25 layouts, real CSRF, stale name/two tabs, retained input; P: 3/12, ключ и версии сохранены, future expiry | L: реальная замена при появлении нового ключа/истечении; production expiry не подделывался |
| Каталог | Полный active+archived sweep, atomic page, цены/остатки/photos, пустой полный ответ | U/API catalog/sync; S: каталог/6 категорий; L: 8 719 listings/10 pages/50 physical reads, complete без warnings | L: repeat sweep выполнен (50 reads, 191s); P: новый complete sweep |
| Периодичность | Автоматическая загрузка stale, дедуп manual, restart, fairness/cooldown | 28 account/catalog scheduler/UI tests; P: после restart completed catalog 53, 8719/10/0 warnings; heartbeat +120,1 s | Длительный пилот discovery/worker/freshness по всем read domains |
| Категории | Поиск кириллицей, exact выбор и смена; чужой/stale dictionary отклонён | U/API + B; S: смена 6 actual типов, схемы 40/43 полей, dictionary select/save | P: фактические категории после запуска |
| Проверка смены категории — UI принят, реальная отправка открыта | Сравнение сохранённой версии с новым типом: простые/составные поля, значения и планы удаления; несохранённый ввод остаётся; просмотр всех страниц, явный token/version review и отдельный save | U/API и B на synthetic candidate; P на 09cd: read-only preview фактической категории без сохранения, protected rows неизменны. На d7d3 exact-runtime 72/344 browser suite прошёл. [Отчёт волны](operations/2026-09-26-ozon-resumable-reads-and-vue-review.md) | Реальный пользовательский draft/edit journey и отдельная разрешённая отправка; review не отправляет в Ozon сам |
| Поля | String, Decimal, Integer, Boolean=false, URL, dictionary, complex/repeated; required и ограничения | U/API drafts/editor; B typed fields | S: примеры каждой формы и native validation |
| Одиночная подготовка | Поиск по всему source-каталогу, выбранный ID, магазин, один create | U/API + B keyboard/source; S: actual source search/create | P: навигация на работающем приложении |
| Редактирование | Только изменённые блоки, нет потери неизвестных атрибутов; version conflict сохраняет ввод | U/API + B; S: save/reload без потери других блоков, конфликт 2 вкладок сохраняет ввод | P: smoke без provider write |
| Существующая карточка | Полный update baseline, сохраняемые фото/баркод, явное удаление атрибута, drift | U/API publication/full-state, B preserve preview | S: фактическая связанная карточка; L: допустимый pilot/readback |
| Ручная связь в Vue — UI принят, пользовательский pilot открыт | Поиск seller-owned внутренней карточки и real photo; просмотр Ozon→внутренняя карточка, отдельная отвязка без rebind, bound-draft запрет, версия/CSRF, lost POST → GET → явное принятие | U/API и B на synthetic candidate; d7d3 exact-runtime browser 72/344. P read-only native 5/24: source ID 57, photo 202→200 за 2.03s, без error overlay/записей; 7 CDN images доступны. [Отчёт волны](operations/2026-09-26-ozon-resumable-reads-and-vue-review.md) | Пять пользовательских сценариев и реальное reviewed link/unlink только по явной задаче; фото/название не доказывает связь |
| Одиночная отправка | Просмотренная версия, явное подтверждение, стабильный key, unknown outcome без повторной записи | U/API publication + B one confirmed POST | S: create/update завершены с synthetic provider, одна физическая попытка на операцию, readback и signed media delivery; L: разрешённый корректный pilot |
| Массовая отправка | Один магазин, cap, точный список/версии, частичные результаты, неизвестные операции не повторяются | 114 publication/draft/Vue tests + 8 subtests; B confirmation | S: mixed run, worker, результат и навигация |
| Исправление партии | Vue/XLSX применяет локальные изменения; отдельное подтверждение публикации | U/API: 17 route/service + Node controller; B/S: 9 сценариев/64 layouts, 200 строк/6 типов, real CSRF, partial/conflict/lost POST/relogin; P: 3 партии/13 строк, 6/48, фото/preview/фильтры/classic, версии сохранены | Реальная повторная публикация после получения недостающих фактов; отдельный XLSX import journey |
| Фото | Фактические изображения, fallback/pending, некорректные URL; нет host I/O в GET cache miss | U/API; S: 6 actual primary photos, fallback/pending; cache preview для exact source URL | P: фото каталога и cache delivery после deployment |
| Цена и склад | Exact scope, актуальная before-state, proposal/confirm, нулевой vs неизвестный остаток | Существующие U/API commercial suites, B catalogue facts | S: preview/confirm/result; L/P readback |
| Склады и exact FBS refresh — read контур принят | HTTP 202 только ставит seller/account или exact listing job; общий scheduler продолжает после закрытия вкладки, GET не вызывает Ozon, last-good сохраняется до полного снимка; duplicate/403/429/cooldown/lease/CSRF | U/API, capacity и B на synthetic candidate; P/L на 09cd: warehouse job 1 и exact FBS job 2 завершены через HTTP и singleton, 2 склада/2 строки. На d7d3 оба completed job и их снимки сохранились после deploy; защищённые группы не изменились. [Отчёт волны](operations/2026-09-26-ozon-resumable-reads-and-vue-review.md) | Долгий пилот freshness/устойчивости; это не успешный stock write |
| Ежедневные разделы | Заказы/возвраты, аналитика, финансы: exact account/period, refresh и freshness | U/API + L bounded reads; B refresh states; S: страницы с фактическими данными без JS errors/overflow | S/P: обновление и восстановление ожидания |
| Отзывы/вопросы | Доступ проверяется отдельно; 403 объясняется без объявления всей интеграции сломанной | L: ранее наблюдён 403 при наличии роли; U/API contracts | S/B: отсутствие доступа и восстановление |
| Негативные сценарии | Другой tenant, CSRF, 429, timeout, 5xx, malformed, restart, unknown outcome | Профильные U/API suites; B conflict/timeout/login | S: с реальной auth/session и fault injection |
| Вёрстка/доступность | 1440/390/320 px, обе темы, keyboard/dialog focus, длинные названия, empty/loading/error | B + S: 21 routes/126 viewport-theme cases, 0 JS errors/overflow; safe aggregate в output/ozon-ui | S: create/update result 1440/390/320 обе темы, overflow исправлен и перепроверен; P: smoke |
| Миграции/rollout | Изолированная копия, сохранённые данные/FK, повторный старт, runtime budgets | S: согласованный backup+SHA256; bundle 6m26s, unchanged restart 0.17s | S: final bundle 7m14s, повтор пропущен по journal; P: health/scheduler/queues/smoke |

## Наблюдения рабочей системы до выпуска

Контроль 24.09.2026, seller/account 2/1, read-only: 8 719 listings, все доступны; 7 471 имеют связанный local product type. Последний complete sweep от 05.09.2026, поэтому давность цен/остатков/контента не скрывается интерфейсом. Схемы использованных типов обновлены 24.09 и включают String, Decimal, Integer, Boolean и URL, словари и составные поля. В базе 19 черновиков: 17 blocked/invalid, один needs_category/invalid, один ready/valid. Сохранённый ready ещё не доказывает прохождения текущей публикационной проверки.

Исторический живой upload-check 05.09 не завершил provider write: отсутствовали подтверждённые габариты упаковки и обязательные юридические классификации. Эти данные нельзя выдумывать для зелёного E2E. Перед реальным pilot требуется заново проверить текущие факты и preflight; старый результат не переносится автоматически на 24.09.

## Развёртывание

Старый работающий image закреплён как `seller-hub:pre-ozon-release-20260924`. Rehearsal использует отдельную копию SQLite, отдельные synthetic secrets и runtime без внешней сети. Рабочий container остаётся на прежнем образе до приёмки. Секреты, database artifacts и снимки с приватными данными не помещаются в Git.

Старые backup-файлы 05.09 переведены в gzip с полным round-trip SHA-256; их логические данные сохранены. Текущая база скопирована SQLite backup API из фиксированной read-транзакции; production writers продолжали работу. Архив до миграций хранится рядом с rehearsal DB. Для окончательного rollout потребуется актуальная точка возврата непосредственно перед сменой runtime, проверка миграций, запуск Compose без сторонних уведомлений и после запуска проверка фактических страниц/очередей/каталога.

Последнее расширенное evidence: `output/ozon-ui/rehearsal-summary.json`; 17 stateful S checks без provider writes. Подробности F02/F03, реального catalog sweep и границ проверки добавлены в основной план. Физические write/readback на реальном Ozon пока не подтверждены.


## Финальная проверка перед переключением

Общий regression: **2 454 tests / 531 subtests passed**. Дополнительно финальный transport/account/read-scheduler набор: **69 tests / 32 subtests passed**, result UI/publication/routes: **98 tests / 8 subtests passed**. Эти числа относятся к отдельным прогонам и не складываются в число уникальных тестов.

Повторный реальный read-only catalog sweep: **8 719 listings, 10 страниц, 50 физических API reads, 191s, 0 warnings**. Отдельный roles-check с уже сохранённым рабочим ключом тоже успешен (один read). Реальные provider writes не выполнялись.

Полный Flask synthetic-provider сценарий прошёл настоящие validation/queue/worker/readback, create и update той же карточки, signed immutable JPEG delivery. После исправления длинной кнопки результат обновления не расширяет 320px страницу; смена статуса больше не перезагружает документ и не теряет ввод. Финальный S визуальный прогон снова дал 21 route / 126 viewport-theme cases / 0 JS errors / 0 local HTTP failures / 0 overflow. Все шесть Ozon primary photos загрузились; семь внешних supplier photo URLs на изолированном стенде недоступны и имеют fallback.

Свежий production backup от **24.09.2026 16:24 UTC**: SQLite quick_check=ok, 13 597 745 152 bytes → gzip 1 776 561 949 bytes, full decompression SHA-256 verified. Архив и manifest находятся в private data volume. Старый image сохранён; финальный startup bundle дополнительно проверяется на отдельной копии базы до rollout.

Финальный image `sha256:5418b43bf875b0955825d9b702fa1ce3098b9c2bf25c7231a7fb21abca807510` прошёл дополнительный migration rehearsal за 434.2s; повторный старт подтвердил неизменность code+schema и пропустил bundle. 24.09 в 16:49 UTC этот image запущен через Compose с прежними critical env и write flags. После старта проходят production migrations; health/проверка браузером ещё не подтверждены этой записью. Дополнительные S checks: XLSX import через реальную форму и stale-version rejection — оба passed.


## Production evidence — 24.09.2026, 17:19 UTC

Развёрнут image `sha256:a7247b80458d1b3ba2092cd317e33200831c344eadc2b9bb6d0770fca6ecd6ca`, сайт [seller-platform.tech](https://seller-platform.tech) healthy, внешний HTTPS проверен с проверкой TLS. Critical env/ключи/write flags сохранены; auto-publish не включался. Основной rollout применил проверенный migration bundle; последующее UI-исправление штатно пропустило неизменные code+schema. Сохранены исходный image 05.09, первый image 24.09 и проверенный backup. Тестовые Vue изменения не требуют миграций.

**P: автоматический каталог**: singleton discovery сам создал job в 16:57:55 UTC; worker начал sync в 16:58:05, закончил в **16:59:37**. Загружены **8 719 listings / 10 страниц / 0 warnings**. До выпуска последний completed был 05.09. Новый job completed; running scheduler lock принадлежит одному web worker. Числа исторических publication attempts не изменились.

**P: рабочий браузер**: **21 страница / 126 сочетаний (1440,390,320 × light,dark) / 0 route errors / 0 JS errors / 0 failed local HTTP / 0 overflow**. Все шесть проверенных категорий показывают загруженные primary photos. Дополнительная прокрутка первого экрана каталога: **60 из 60 primary photos loaded, 0 fallback, 0 pending**. Проверка использовала подписанную внутри runtime seller session без смены пароля; все browser POST запрещены. Реальный password login/CSRF проверен отдельно на S.

Первый P-прогон ждал networkidle и столкнулся с зависшим ancillary фото x-story.ru на двух страницах; сами страницы и primary Ozon photos готовы за 0.52–1.0s. Введён display deadline 12s с видимым fallback/ручным повтором. Lazy image не получает таймер до приближения к viewport; смена URL и unmount очищают состояние. S fault injection подтвердил timeout/retry и layout обеих тем; P итоговый прогон проверяет готовность интерфейса и фактическую загрузку primary photos.

**P: аналитика/заказы/финансы**: через штатные кнопки отправлены три exact-period read-refresh (18–24.09). Каждый вернул 202 с заявкой, reload сохранил её identity; все три завершились `completed` без error_code. Auth/CSRF настоящие, provider endpoints только read, JS/local HTTP ошибок нет.

UI доведён после visual inspection: известные коды проверок редактора представлены русскими названиями полей; ошибки единиц/валюты фокусируют точный select. Слабые path-only предложения категории скрыты из начальных подсказок, явный поиск сохраняет весь доступный справочник. Проверены **17 focused tests** и **4 S browser checks**. Backend validation/versions/proposal gates не изменены.

**Открыто:** реальный create/update/commercial pilot и readback по подтверждённому товару/цене/остатку; оставшиеся пункты полной матрицы A+B. При наблюдении обнаружены четыре проблемных reference scope: два пустых словаря «Особенности состава» и два сокращённых списка «Российский размер» (84→39 и 77→19); старые значения сохранены shrink guard. Их контракты/семантику нужно проверить отдельно, а не объявлять весь справочник актуальным по шести успешным категориям.

Дополнительный L diagnostic подтвердил четыре проблемных reference scope: **12 physical reads / 0 writes**, для каждого прочитана актуальная schema и дважды полный ответ values с явным `has_next=false`; повторные hashes совпали. В «Средство для чистки игрушек» и «Хранение секс игрушек» optional «Особенности состава» имеет dictionary_id, но 0 значений (и прежний cache пуст). «Российский размер»: «Эротический набор» 84→39 (optional), «Портупея эротическая» 77→19 (**required**). Последний scope мешает подготовке новых товаров этой категории и не считается закрытым. Cache не менялся этим probe, shrink guard не отключён. Нужен различающий эти случаи путь: корректное наблюдение пустого нового optional-словаря и проверяемое admin recovery для подтверждённого сокращения существующего словаря. Прямой официальный swagger и страница документации повторно недоступны через redirect loop; сторонние копии не использованы как подтверждение контракта.

## Следующий пакет: проверка сокращённых словарей

Добавлены exact admin review и повторный полный provider read перед применением. Подтверждение привязано к версии кандидата, hash полного списка, baseline hash/version, схеме и exact category/type/attribute scope. Отзыв прав администратора, истечение 24 часов или изменённый ответ отменяют разрешение. HTTP review/approval читает только локальную базу. Изменения активного словаря и audit применения коммитятся вместе; удалённые значения становятся unavailable, история не удаляется. Миграция additive, добавляет только review table/index.

Фокусный набор перед сборкой: **77 tests / 5 subtests passed** (`/tmp/ozon-reference-final-tests.log`). В него входят новая state machine, повторное чтение, stale-version и admin route boundaries, сохранение last-good при смене схемы во время I/O, отсутствие автоматических повторов pending review, demand dispatch после approval, storage fail-closed, права/expiry и идемпотентность/целостность миграции. Реальный CSRF middleware и Vue UI дополнительно проверяются полным приложением на копии базы; этот результат ещё не подтверждён данной записью.

Кандидат image: `sha256:e55ad5659c1b2baaa7de95471c67756e754fb5e71a1357ef5ea77d8c0067c55a`. Общий regression, миграционная репетиция, новая резервная копия и браузерная проверка запущены. Этот пакет пока не заменяет работающий image a7247b80458d.

## Финальный пакет справочников — 24.09.2026, 18:18 UTC

Общий regression после устранения N+1: **2 472 tests / 536 subtests passed**, 278 прежних предупреждений (`/tmp/ozon-reference-full-final.log`). Первый общий прогон нашёл одну регрессию числа SQL-запросов в `list_type_rows`: review metadata теперь приходит одним indexed join, а большой payload остаётся deferred. Дополнительный набор reference/compliance/editor — **123 tests / 5 subtests passed**; последний узкий editor/admin route набор — **20 passed**. Числа отдельных прогонов не складываются.

**S+L:** четыре реальные категории прочитаны через read-only Ozon API на изолированной базе: 4 schemas + 4 dictionaries. Оба исходно пустых optional scope стали fresh с нулём значений. Два сокращения сохранили last-good active cache и создали кандидатов 84→39 и 77→19. Через настоящий Flask/Vue admin UI подтверждены exact версии; повторные provider reads для каждого scope совпали и применились (ещё 2 reads). Всего в этом проходе **10 успешных физических reads, 0 provider writes**; два сокращения применены только в изолированной базе, не в production.

Финальный браузерный report: **8 checks passed / 6 viewport-theme cases / 0 JS errors / 0 failed local HTTP**. Проверены реальный admin password login, три списка сравнения, GET/POST network fault, CSRF 400, stale version 409, обязательное подтверждение, reload сохранённого approval и применение только после нового ответа Ozon. Первая попытка нашла 13px overflow старой таблицы на 320px и неверное экранирование JSON ID в Alpine HTML-атрибутах; оба дефекта исправлены, финальный проход чистый. Снимки и отчёт private в `browser-reference-review-final`; начальные неуспешные отчёты не выдаются за чистые.

Backend image `ac278111686b…` прошёл полный migration rehearsal; повторный запуск подтвердил current bundle/schema. Финальный image **`sha256:1b30eda13a7d755a1502d411f02f0af2e43230131cabf7156f27e09a283448d3`** содержит тот же проверенный backend и точные frontend bytes из успешного браузерного прохода (пять файлов сверены SHA-256). Он запущен в production **18:18:16 UTC**, с прежними critical env и write flags. Стартовые production migrations и P-проверки ещё идут на момент этой записи.

Новый backup: snapshot **17:49:53 UTC**, quick_check=ok, 13 602 193 408 bytes → gzip 1 778 354 909 bytes; SHA-256 **`fabe9b727f683e73c456183da4e1f48a7f461d6a70afc354304153e9f6403d92`**, полный round-trip проверен. Файл `backups/ozon-20260924-reference-predeploy.sqlite.gz` и manifest остаются private в data volume. Сохраняется rollback image a7247b80458d; предыдущие backup/images также сохранены.

## Production подтверждён — 24.09.2026, 18:36 UTC

Image **1b30eda13a7d…** healthy; web workers слушают с **18:26:24 UTC**. Внешние login/Vue/editor/catalog/illustration дают 200 с проверенным TLS. Единственный scheduler lock принадлежит одному worker. Настройки/ключи/flags сохранены, auto-publish выключен. Новый полный catalog sync №12 ещё до этого обновления автоматически завершился в **18:01:52 UTC**: 8 719/10 pages/0 warnings; все 8 719 listings доступны. Historical publication attempt counts остались прежними.

**P UI:** 21 seller route + 9 admin reference routes = **30 страниц / 180 сочетаний viewport/theme / 0 route errors / 0 JS errors / 0 local HTTP failures / 0 overflow**. Проверены 1440/390/320 и light/dark; 6 товарных категорий. Прокрутка первых 60 карточек: **60 loaded, 0 fallback, 0 pending**. Admin scope после применения перепроверен отдельно; все четыре страницы характеристик, четыре страницы типов и taxonomy открываются корректно. Session подписана внутри runtime; пароли не менялись.

**P reference recovery:** обычный scheduler сам наблюдал оба пустых optional словаря и staged два сокращения в 18:27–18:28 UTC. Затем в рамках порученного восстановления интеграции выполнена операторская проверка exact hash и всех current/removed ID+названий против двух ранее полученных независимых полных Ozon ответов. Через новый admin Vue UI сохранены два подтверждения: 962 (84→39) и 1912 (77→19). Это не публикация товара и не изменение цены/остатка. Штатный worker в **18:32:26 UTC** заново прочитал оба словаря, подтвердил точное совпадение и атомарно применил cache + audit.

Все четыре scope теперь `success`, без `values_sync_error`; counts 0/0/39/19. По двум сокращениям есть 2 approval + 2 application audit events, старые value rows сохранены unavailable. Подтверждения в production были выполнены при сопровождении релиза, отдельно от изолированного теста, который сам production cache не менял. Никаких Ozon product/price/stock writes этим пакетом не выполнялось. Старая uncertain publication operation сохранена без сброса попыток/ложного success.

Агрегированное evidence: `output/ozon-ui/rehearsal-summary.json → reference_release`. Private P screenshots/reports: `ozon_release_reports/20260924-reference`, включая точные reviewed snapshots и recovery report. QA container остановлен; воспроизводимая копия базы и отчёты сохранены. Следующий этап — W4/W5: подготовка настоящего product/price/stock пилота, проверка исходных фактов и интерфейса коммерческих операций. Остальные A+B gates остаются открыты.


## Vue: цены и остатки — 24.09.2026, 19:40 UTC

Image `780e742f11aa…` развёрнут в production и healthy после полного проверенного migration bundle. Vue-экран содержит выбор магазина/товара, фактические фото, точные сравнения цены/остатка, отдельное подтверждение, историю, создание rollback proposal и frozen batch review. Недоступная карточка/склад блокирует новую отправку до I/O и повторно под account lock; уже attempted операции продолжают read-only reconciliation. Classic fallback сохранён.

**U/API:** общий набор **964 tests / 314 subtests passed** (`/tmp/ozon-commercial-full-final.log`); финальный профильный набор **48 tests / 35 subtests passed**. **S:** 15 сценариев, 42 layout/theme cases, 0 JS errors, 2/2 фотографии; 6 synthetic provider writes, сеть контейнера отключена. Проверены отдельный review, decimal price, exact restore, нулевой FBS stock, drift/version conflicts, недоступный target, lost create/approve response, ambiguous outcome, frozen batch и partial result. Это не реальные записи Ozon.

**P:** 22 seller pages / 132 layout/theme cases + 12 видов price/stock формы; 0 route/JS/local HTTP errors, 0 overflow. Шесть категорий и 60/60 primary photos загружены. Через CSRF-protected UI обновлены 2 реальных склада; exact stock refresh вернул 2 наблюдения. Provider writes в этом браузерном проходе — 0. HTTPS/TLS и локальные Vue/catalog/editor assets проверены.

Backup `backups/ozon-20260924-commercial-predeploy.sqlite.gz`: snapshot 19:08:33 UTC, quick_check=ok, 13 602 373 632 bytes → gzip 1 778 655 635 bytes, SHA-256 `ac32e45141e25706dfee7c072464bb90596b2dea234ac090838a87a6008aded0`; полный decompression round-trip проверен. Прошлый image 1b30eda13a7d и предыдущие архивы сохранены.

**Живой price pilot разрешён пользователем:** «любой артикул, цену +25%, потом вернуть». Предварительный read остановился до proposal/write: `/v5/product/info/prices` вернул 5/5 requested items, `total=5` и cursor, затем пустой конец с `total=0`. Это выявило неверное ожидание неизменного total на пустом sentinel. Исправление сохраняет total непустой страницы и обязательный полный exact-set, не допускает partial/duplicate/foreign rows. Новый focused набор **45 tests / 42 subtests passed**; общий regression и изолированная репетиция нового image идут. Подтверждённого real write/restore на момент этой записи ещё нет. Официальная docs.ozon.ru вновь отвечает redirect loop, поэтому наблюдение помечено live evidence, а не новой документальной гарантией API.


## Production и живой price pilot — 24.09.2026, 20:11 UTC

Финальный текущий image **`51eb399275753262f670011d00161a729ff4f0f50b2f04686353940e8aee17ad`** healthy. Исправленная price pagination прошла **966 tests / 321 subtests**, 186 прежних warnings, 336.64s; focused — **45 tests / 42 subtests**. Полный migration rehearsal завершён 19:51:16, повторный запуск пропустил проверенный bundle. S с реальным app + synthetic provider, воспроизводящим последний cursor и пустой total=0: **15 E2E / 42 layout-theme cases / 0 JS errors / 2 loaded photos / 6 synthetic writes / 0 real writes**.

После deployment P smoke повторён на точном image: **22 страницы / 132 layout-theme cases + 12 видов коммерческих форм / 0 route, JS, local HTTP и overflow errors**. Шесть категорий, 60/60 primary photos. Реальные warehouse/stock reads снова завершились; 2 склада и 2 наблюдения. Внешний HTTPS с проверкой TLS вернул 200 для login, Vue/catalog/editor и новых `ozon-commercial.js/css`. Ключи и critical env сохранены; auto-publish=0. Scheduler жив и держит единственный exclusive lock. Каталог сам завершил следующий sweep №14 в **20:07:29 UTC**, 8 719/10 pages/0 warnings.

**L+P price test:** пользователь явно разрешил любой свой артикул, +25% и возврат. Артикул `1366Z1C1S2013`, listing 31303. Proposal №1/operation №7 запросили **1 323,75 ₽** из **1 059 ₽**; Ozon ответил updated=true, но независимые price reads наблюдали **1 324 ₽**. Exact-state gate сохранил proposal conflict / operation uncertain, attempt_count=1, без повторной отправки и без blind rollback. Известное расхождение не объявлено точным успехом.

По свежему состоянию 1 324 через ту же Vue-форму подготовлена новая user proposal №2 с исходной **1 059 ₽**, явным разрешением снижения и описанием причины. Operation №8 — succeeded, одна физическая попытка; proposal applied. В 20:08:38 восстановлена локальная проекция, независимое повторное чтение в **20:10 UTC** подтвердило 1 059 и прежние old/min price, валюту и promo flags. Итого две реальные физические price writes, исходная цена восстановлена. Статус №7 намеренно не подменён успехом; это не автоматический rollback и не полностью зелёный дробный price contract. Следующая доработка — явная целая RUB-цена на review и согласованные UI/API ограничения до отправки.

Безопасное aggregate evidence: `output/ozon-ui/rehearsal-summary.json → commercial_release`. Private отчёты, снимки и state checkpoints — `ozon_release_reports/20260924-commercial`; scripts/credentials остаются за пределами репозитория и образа. [Подробный ценовой отчёт](operations/2026-09-24-ozon-price-pilot.md). Product create/update, real stock write и остальные A+B gates этим результатом не закрыты.

## Явная цена перед подтверждением — 24.09.2026, 20:43 UTC

Новый image **`749a56fc6c350d203da5fb71c144c9b19a802de5c4b14bbede33ddda98171177`** запущен в production в **20:43:47 UTC**; на момент этой записи идут startup migrations, P-проверки ещё не завершены. Ключи, настройки и flags сохранены. Старый image 51eb39927575 остаётся для rollback.

Локальная политика Seller Hub готовит целую RUB-сумму через Decimal/ROUND_HALF_UP до review; исходный ввод и итог видны в форме, single review и frozen batch. Approval и write/readback не округляют. Legacy fractional pending/queued-before-attempt блокируются до provider I/O; attempted reconciliation сохраняет точность. Historical fractional rollback не округляет исходную сумму молча. Сценарий third-state после записи объясняет возможное применение и ведёт к новому сравнению.

**U/API:** 973 tests / 335 subtests, 186 прежних warnings, 356.89s. Focused: 52 tests / 56 subtests. **S:** 18 сценариев / 54 layout-theme cases / 0 JS errors / 2/2 фото; 8 synthetic writes, 0 real writes. Проверены half-up .49/.50, запятая, нижняя граница, точная округлённая сумма подтверждения, legacy fractional single/classic/batch guards, provider-adjustment conflict и отдельный recovery review. Визуально просмотрены light desktop и dark mobile rounding/detail/batch/conflict screens. Полные migrations завершены на свежей копии в 20:42:19, repeat startup подтвердил current bundle/schema.

Backup `backups/ozon-20260924-price-review-predeploy.sqlite.gz`: snapshot **20:22:25 UTC**, содержит исходы реальных операций 7/8, quick_check=ok, 13 602 435 072 bytes → gzip 1 778 878 590 bytes; SHA-256 **`50bee95f4d37c4990bb2f336eec2e61cddeda42c798c517bef1a4356d7b1c1f4`**, полный decompression round-trip проверен. Переиспользована только остановленная disposable QA DB; production и прежние архивы не удалялись.

По просьбе владельца добавлены редкие содержательные Telegram-статусы через отдельный `scripts/notify_task_status.py`, первая доставка подтверждена. `.env.autodeploy` больше не включается в новый Docker image, локальный файл закрыт 0600; существующие rollback images не удалялись. Уточнено требование к отдельным ценам и скидке площадки: [спецификация](design/ozon-prices.md). Read-only sample в 20:36:45 снова подтвердил seller price 1059, old price 1462 и marketing seller price 0, без поля `marketing_price`. Это не подтверждение финальной покупательской цены или скидки Ozon; новый price-display этап остаётся открытым.


## P-проверка пакета цен завершена — 24.09.2026, 20:54 UTC

Image 749a56fc6c35… healthy после штатных production migrations. **22 страницы / 132 layout-theme cases + 12 видов price/stock формы, 0 route/JS/local HTTP/overflow errors**; шесть категорий, **60/60 фото**, без pending/fallback. Реальные read-only warehouse/stock refresh прошли: 2 склада / 2 наблюдения. Внешний HTTPS/TLS и все проверенные Vue/catalog/editor/commercial assets — 200. Browser mutations ограничены read refresh; новых price/stock/product writes нет.

Read-only verification сохранила прежние outcomes: operation 7 uncertain / proposal conflict, operation 8 succeeded / proposal applied, по одной попытке. Независимый Ozon read в **20:52 UTC** подтвердил восстановленную seller price **1059**. Scheduler жив и держит exclusive lock, critical flags прежние, auto-publish=0. Backup и rollback image сохранены. Итоговое evidence: `output/ozon-ui/rehearsal-summary.json → price_review_release`; private production UI report/screens — `ozon_release_reports/20260924-price-review`.

Следующий незакрытый дефект: текущий общий каталог предпочитает `marketing_seller_price=0` обычной положительной цене продавца для 3 208 строк сохранённого snapshot. Отдельные базовая/продавца/покупательская цены и скидка площадки ещё требуют реализации и проверки; green layout smoke этого не доказывает. Telegram-уведомления и уточнённый режим редких сообщений закреплены в AGENTS.md.

## Раздельные цены: production verification — 25.09, 00:54 МСК

Image **`e94c3ca4ef95e3a2bdbfe141472b194af82af3dce2a8f779043209e1a0c1d6cb`** healthy и проверен после deployment 24.09 в 21:44:21 UTC. Дефект нулевой основной цены из предыдущей записи исправлен. База, seller price и его акции раздельны; buyer price/скидка Ozon явно unknown и остаются открытой частью задачи. Price-details API реально ответил 403 обоим ключам, несмотря на method grant текущего ключа; это не основание рисовать `0%` или объявлять контракт завершённым.

**U/API:** 1000 tests / 344 subtests, 186 прежних warnings; focused 85 / 9, final display 32. **S на окончательном image:** 7 ценовых и 18 коммерческих сценариев, 108 layout-theme cases суммарно, 0 JS errors; 8 synthetic writes только в изолированном коммерческом стенде, 0 real writes. Полный migration rehearsal завершён; финальный CSS-only образ подтвердил тот же journal/schema. По скриншотам исправлены hidden classic price labels, mobile table overflow от `sr-only` и overlapping search icon.

**P:** 22 страницы / 132 layout-theme cases + 12 видов коммерческой формы; 6 ценовых представлений, 6 категорий, **60/60 фото**, 0 route/JS/local HTTP/overflow errors и неожиданных browser mutations. Два склада / два stock observations успешно обновлены read-only. HTTPS/TLS и новый shared price CSS/JS доступны. State verification сохранила operation 7 uncertain / 8 succeeded и исходные price/old/min amounts. Независимый live read в **21:54:12 UTC** подтвердил seller 1059 / old 1462 / promotion 0 / min 0 / RUB. Новых реальных записей цен, остатков и товаров нет.

Проверенный backup 21:20:26 UTC сохранён, прежний image 749a56fc6c35… доступен. После сохранения evidence удалена только disposable QA DB; production DB и архивы сохранены, свободно около 20 ГБ. Для следующего rehearsal нужно заново создать/распаковать QA-копию. [Полный отчёт и SHA-256 архива](operations/2026-09-25-ozon-price-lanes-release.md). Безопасное evidence: `output/ozon-ui/rehearsal-summary.json → price_lanes_release`; private P screenshots — `ozon_release_reports/20260925-price-lanes`.

## 25.09 — паузы API и конечный срок сверки

Развёрнут `cbec4f1cc4f4…`, healthy, restart 0. 979 tests / 335 subtests, B: 5 состояний / 30 layouts; P: 25 страниц / 150 layouts + 12 commercial form variants, 6 категорий, 60/60 фото, без JS/HTTP/overflow errors. Long/fractional/overflow Retry-After, ручной early poll после reload, update accepted + live-read outage после deadline, price и warehouse-stock read failures проверены без повторного write. P сохранил цену 1059, историю #7/#8, uncertain #6 и pending stock proposal #3. Backup/полный QA migration runner/production startup пройдены. [Подробный отчёт и границы](operations/2026-09-25-ozon-operation-retry.md).

## 25.09 10:15 МСК — fulfillment workspace

Проверен и развернут image `41536969c86dfd699a693c498d0ccb01a8275093e6ec7e8ea0fccc094d3b56bb`. Scoped compact list/detail, Unicode/literal search, 121-line paging, foreign FK/history protection, lost POST/session gates: 98 tests + 29 subtests. Browser: 13 synthetic scenarios/54 layouts, 8 full-app snapshot scenarios/36 layouts; production new flows 8/24/15 photos. General production 25 pages/150 layouts +12 forms, six categories, 60/60 catalog photos, six price surfaces; 0 route/JS/HTTP/overflow errors. TLS/login/13 assets 200. Price restored/history unchanged, stock proposal3 pending/no operation, no new provider writes. Full task gates (buyer price, current product/stock pilots, shipping/replies and finance verification) remain open. [Evidence](operations/2026-09-25-ozon-fulfillment-workspace.md).

## 25.09 10:45 МСК — статус отправки в редакторе

Image `95d6af011230…` healthy. Только editor JS/template отличаются от предыдущего образа; backend/migration bundle неизменны, обычный startup подтвердил текущий migration journal. 12 tests; offline 8 состояний/48 layouts; production 3 read-only сценария/6 layouts. Реальная остановленная #6 отражена как «Нужна сверка», exact переход проверен, повторная отправка disabled. 0 JS/HTTP/overflow errors, 0 новых provider reads/writes; #6/#7/#8 attempt_count=1, stock #3 pending/null. TLS/login/публичный JS проверены. [Отчёт и границы](operations/2026-09-25-ozon-editor-status.md).

## 25.09 11:59 МСК — finance workspace

Image `9fb48834bec4…` healthy, 0 restarts. **87 tests + 29 subtests**, отдельная recovery regression 8 tests. Offline 15 сценариев/54 layouts; full app на свежей копии 8/24; production 9 финансовых сценариев/24 layouts/12 фото, все 71 начисление пройдены с закреплённым snapshot и независимым пересчётом totals. Общий production проход 25 страниц/150 layouts, 6 категорий, 60/60 фото, 6 ценовых представлений; 0 ошибок. TLS/login/4 assets hash verified, backup gzip roundtrip и normal migrations проверены. Один live read за 24.09 совпал с сохранённым полным днём (1 факт); full-period accounting reconciliation/export ещё открыты. Новых provider writes нет, цена восстановлена, stock #3 pending/null. [Отчёт](operations/2026-09-25-ozon-finance-workspace.md).


## 25.09 12:55 МСК — финансовый Excel

Image `6e7c579014975…` healthy, 0 restarts. **98 tests + 29 subtests**, финальные 11 export tests; offline 5 сценариев/24 layouts; full app 4/24 и production четыре настоящих скачивания/24 layouts. Все 71 начисление, 41 товарная строка, 91 компонент, валютные суммы и фильтры сверены с API. Общий проход 25 страниц/150 layouts, 6 категорий, 60/60 фото, 6 ценовых представлений; 0 ошибок. Проверены normal migrations, повторное восстановление backup от 08:19 UTC, TLS/asset hashes и scheduler; временная QA DB удалена. Новых provider writes нет, price restored, stock #3 pending/null. Export закрыт; representative-period accounting reconciliation и прочие A+B gates открыты. [Отчёт](operations/2026-09-25-ozon-finance-export.md).


## 25.09 14:33 МСК — аналитика заказов на Vue

аналитика заказов на Vue развёрнута в image `6282ec860fc2…`, healthy, 0 restarts. Один закреплённый снимок: точные суммы/единицы, динамика и таблица по дням, товары/фото/поиск/доли. 88 tests + 11 subtests; offline 16 сценариев/72 layouts; полное приложение 5/36. Production: 1899 SKU на 19 API-страницах, дневные значения и 25 фото сверены; 5 сценариев/36 layouts. Общий проход 25 страниц/150 layouts, 6 категорий, 60/60 фото и 6 ценовых представлений — без ошибок. Backup restore, normal migrations, TLS и scheduler проверены. [Отчёт](operations/2026-09-25-ozon-analytics-workspace.md). Полная бухгалтерская сверка и остальные A+B gates открыты.


## 25.09 22:04 МСК — startup migrations

**Актуальный статус 25.09, 22:04 МСК:** image `0632b1e7e6cf…` развёрнут, healthy, 0 restarts. W8: повторные FK-проверки объединены с проверкой неизменности БД; полный migration guard на копии сократился с 431 до 314 секунд (27%), production — 338.437 с. 114 tests + 5 subtests; fresh/historical/repeat проверки, неизменность схемы и protected history подтверждены. Production: 25 страниц/150 layouts, 6 категорий, 60/60 фото, 6 ценовых представлений; аналитика 5 сценариев/36 layouts, 1899 SKU/19 API-страниц — без ошибок. [Отчёт](operations/2026-09-25-startup-migration-performance.md). Buyer price/скидка Ozon и остальные A+B gates открыты; ёмкость для свежего backup требует отдельного решения.


## 25.09 22:39 МСК — verified backup

**Актуальный статус 25.09, 22:39 МСК:** app image `0632b1e7e6cf…` остаётся healthy, 0 restarts. W8: штатная backup-команда использует согласованный SQLite snapshot и фактическое восстановление с проверкой SHA/size/quick_check; новый production backup от 22:27:50 МСК принят. 15 tests; после освобождения старого восстанавливаемого JPEG cache проверены 7 страниц, 6 категорий и 60/60 фото. [Отчёт](operations/2026-09-25-verified-backup.md). Startup-ускорение и предыдущая полная UI-приёмка сохранены. Постоянная ёмкость, off-host backup, key recovery, RPO/RTO и остальные A+B gates открыты.


## 25.09 23:10 МСК — controlled recovery

**Актуальный статус 25.09, 23:10 МСК:** app image `0632b1e7e6cf…` остаётся healthy, 0 restarts. W8: опасный restore через raw cp заменён проверяемой копией в новом каталоге; 41 tests. Принятый архив реально восстановлен за 382.544 с, тот же image стартовал в network-none стенде за 9.071 с. Analytics 5 сценариев/36 layouts, 1900 SKU/19 API-страниц; catalog 7 страниц/14 layouts, 6 категорий, 60 первичных фото. Шесть protected tables/схема/FK observations неизменны, provider writes=0. Изображения для стенда подготовлены отдельно и не объявлены частью DB backup. [Отчёт](operations/2026-09-25-controlled-recovery.md). Production cutover, off-host/key/media recovery, RPO/RTO и остальные A+B gates остаются открыты.


## 26.09 00:03 МСК — отзывы и вопросы на Vue

**Актуальный статус 26.09, 00:03 МСК:** image `ffd4920d2a2a…` развёрнут, healthy, 0 restarts. W9: отзывы/вопросы на Vue, фото точно связанных товаров, фильтры/URL, диалог и локальные версии ручных ответов с защитой от конфликтов и потерянного ответа. 137 tests + 38 subtests; стенд 14 сценариев/72 layouts; production inbox 2/16. Общий проход: 25 страниц/150 layouts, 6 категорий, 60/60 фото, 6 ценовых представлений, без ошибок. История price/stock и scheduler сохранены. На текущем кабинете Ozon по-прежнему отклоняет read-доступ к обоим inbox-разделам; реальная лента и отправка не объявлены готовыми. [Отчёт](operations/2026-09-26-ozon-inbox-workspace.md). Остальные A+B gates остаются открыты.


## 26.09 00:45 МСК — фоновое обновление входящих

**Актуальный статус 26.09, 00:45 МСК:** image `20e795f9925e…` развёрнут, healthy, 0 restarts. W9: отзывы и вопросы обновляются через общую фоновую очередь, переживают закрытие страницы/перезапуск и сохраняют паузы Ozon; Unicode-поиск понимает русский регистр. 241 tests + 67 subtests; стенд 20 сценариев/104 layouts. Production: inbox 2/16, общий проход 25 страниц/150 layouts, 6 категорий, 60/60 фото, 6 ценовых представлений, без ошибок. Миграция сохранила старые заявки и расписания; история price/stock и scheduler подтверждены. Реальный read-доступ к inbox и отправка ответов остаются открытыми. [Отчёт](operations/2026-09-26-ozon-inbox-durable-refresh.md). Остальные A+B gates не закрываются этим выпуском.


## 26.09 01:28 МСК — состояние магазина

**Принятый выпуск 26.09, 01:28 МСК:** image `72cdc6236fa18…` развёрнут, healthy, 0 restarts. W8: Vue «Состояние магазина» показывает подключение, живой обработчик, шесть независимых разделов и неподтверждённые операции; наблюдение не вызывает Ozon и не повторяет отправки. 164 tests; стенд 14 сценариев/80 layouts. Production: новая страница 2/8, общий проход 25 страниц/150 layouts, 6 категорий, 60/60 фото, 6 ценовых представлений, без ошибок. Heartbeat действительно обновляется; история price/stock сохранена. [Отчёт](operations/2026-09-26-ozon-account-health.md). Внешний монитор/аварийные alerts, recovery и остальные A+B gates остаются открыты.

- [x] Full Flask/Vue: свежие/старые/частичные снимки, 429, denial, expired/disabled key, unknown/stale worker, четыре состояния операции, network loss, foreign scope и session expiry.
- [x] Production: exact-account API с private/no-store, GET-only refresh, вход из настроек, обе темы 1440/768/390/320; CLI подтверждает два разных свежих heartbeat.
- [x] Повторный проход каталога/категорий/фото/цен и сохранность истории операций.
- [ ] Внешние alerts/полный recovery и остальные открытые launch gates этим этапом не закрыты.

## 26.09 12:29 МСК — история начислений и API-сверка

**Актуальный статус 26.09, 12:29 МСК:** image `97cfb9c9f4e9…` развёрнут, healthy, 0 restarts. W10: Vue-история сравнивает две финансовые загрузки по общим дням, показывает изменившиеся факты и точные версии. 69 tests; стенд 12 сценариев/56 layouts. Production: новая страница 3/16, общий проход 25 страниц/150 layouts, 6 категорий, 60/60 фото, 6 ценовых представлений, без ошибок. Реальный API: 29 закрытых дней, 70 начислений, 41 SKU-строка и 90 компонентов полностью совпали; отдельный `accrual/postings` дважды вернул 429, включая попытку после cooldown, поэтому этот контракт не подтверждён. Свежий DB backup реально восстановлен и проверен до deploy. [Отчёт](operations/2026-09-26-ozon-finance-comparison.md). Независимый отчёт владелец предоставит позже; бухгалтерская сверка и остальные A+B gates остаются открыты.

L-проверка: 33 physical reads, ноль provider/основных DB writes; общий ledger сохранил локальную отсрочку и provider cooldown. P-проверка независимо пересчитала Decimal totals и child signatures из SQLite для двух пар и сверила с HTTP API. Ненаблюдённые записи не объявлены нулевыми; nested fees повторно не суммировались. Полный startup guard выполнен; scheduler heartbeat продвинулся на 90.09 с; история price pilot и pending stock proposal не изменились. Два provider 429 по postings остаются открытым ограничением; внешний отчёт и полный launch не объявлены готовыми.

## 26.09 13:22 МСК — качество карточек на Vue

image `b2fda91b09480…` развёрнут, healthy, 0 restarts. W6/W7/W8: Vue-список качества с фото, объяснимыми причинами, сохранением поиска/выбора/URL и фоновым пересчётом всего каталога. 130 regression tests + 1 отдельный CSRF test; стенд 12 сценариев/72 layouts. Production: quality 4/16 и 25/25 фото; общий проход 25 страниц/150 layouts, 6 категорий, 60/60 фото, 6 ценовых представлений, без ошибок. Один явный local POST завершил пересчёт всех 8111 карточек после закрытия браузера за 406.5 с; повторное открытие показало completed. Новых Ozon writes/probes нет. [Отчёт](operations/2026-09-26-ozon-quality-workspace.md). Финансовая API-проверка предыдущего выпуска сохранена; внешний отчёт владелец предоставит позже. Остальные A+B gates открыты.


## 26.09 14:26 МСК — повторяемая регрессия и возврат после входа

image `9d0bf9fd8fe54…` развёрнут, healthy, 0 restarts. W8: повторяемая регрессия одной командой — 1357 tests + 373 subtests, browser 14 сценариев/86 layouts, negative drill и экспорт evidence, без production credentials/network. Исправлен возврат после входа с сохранением account/фильтров и защитой от внешнего redirect. Production: 25 страниц/150 layouts, 6 категорий, 60/60 фото, 6 ценовых представлений; quality дополнительно 4/16/25 фото. История price/stock сохранена, heartbeat продвинулся на 120.53s. [Отчёт](operations/2026-09-26-ozon-release-regression.md). Workflow GitHub подготовлен; hosted CI/branch gate ещё не подтверждён. Рассылка статусов всем активным `/start` подписчикам работает. Остальные A+B gates открыты.

- [x] Clean image/network-none, все suites без skip, export JSON/XML/screenshots; induced failure даёт exit 1 и failure screenshot.
- [x] Настоящие login/CSRF/cookies/redirects и сохранение draft; lost POST не создаёт дубль local job.
- [x] Production TLS, страницы/категории/фото/цены, scheduler и история операций.
- [ ] Hosted GitHub CI/обязательный branch gate и оставшиеся launch gates этим локальным успехом не закрыты.


Принятый Vue repair и полная повторяемая регрессия 26.09: [отчёт](operations/2026-09-26-ozon-bulk-repair-vue.md). Финальный образ `f7a81b581899…`; 1375 tests + 373 subtests и 23 browser scenarios. Непройденные live gates таблицы этим не закрываются.


## 26.09, 16:23 МСК — local operations

Host-only выпуск принят без изменения app image: 101 focused tests; ежедневный timer и observer реально выполнены; fresh SQLite snapshot 13 658 419 200 bytes восстановлен из gzip 1 786 467 111 bytes с SHA/size/full quick_check. После cache maintenance production: 25 страниц/150 layouts/60 фото +3 repair-партии/48 layouts/13 фото; ошибок нет, draft/operation state сохранён. Внешнее хранилище отложено владельцем; это локальный контур. [Доказательства и открытые границы](operations/2026-09-26-local-backups-and-observer.md).

## W1: срок ключа и версия ротации — 26.09, 17:33 МСК

Принят `e81fd556ead03…`: 1435 tests +373 subtests, 30 browser scenarios/175 layouts; production 25 страниц/150 layouts, recovery 3/12 и партии 6/48, всего 73/73 фото. Реальный локальный snapshot восстановлен и проверен; additive journal migration/repeat/FK/шесть protected tables приняты. Production accounts/operations/drafts/proposals сохранены; expiry пока вне окна предупреждения, fake expiry/real rotation не выполнялись. Host observer/backup timers healthy, внешнее хранилище отложено владельцем. Подробные границы и evidence — [выпуск](operations/2026-09-26-ozon-credential-expiry.md).


## W1: безопасные настройки и история магазина — 26.09, 18:38 МСК

Принят image `29a5f754e159…`, healthy/0 restarts. Название/default VAT сохраняются при durable uncertain без изменения ключа, готовых карточек или активации отключённого кабинета; просмотренная версия и независимый Vue review защищают две вкладки/lost POST. Atomic credential-free audit фиксирует акторов и default fan-out; старые события не выдумываются. Full CI: 1464 tests +385 subtests, exact-image browser 38 сценариев/200 layouts. Реальный local backup восстановлен и проверен, миграция/repeat/FK/protected tables приняты. Production: 25 страниц/216 layouts, 6 категорий, 73/73 фотографии; accounts/operations/drafts/proposals неизменны, реальные key/label/VAT ради smoke не менялись. [Отчёт](operations/2026-09-26-ozon-account-settings-history.md). Внешние backups отложены владельцем. Operator quarantine, недоступные buyer/inbox API, missing publication facts, live stock/shipping и пользовательский pilot остаются открыты.


## W1/W4: разбор неизвестного исхода — 26.09, 20:14 МСК

image `595fa6cb7ea7…` принят: healthy/0 restarts. W1/W4: отдельный Vue-разбор неизвестного исхода, явная остановка новых записей по immutable товару или просмотренному кабинету, append-only журнал и снятие только после доказанного результата исходной операции. Общий guard защищает карточки, цены, остатки, queue/batch/rollback. Full CI: 1634 tests +452 subtests; exact-image 52 сценария/272 layouts. Production: 25 основных страниц +3 новых страницы разбора, 240 layouts суммарно, 6 категорий, 73/73 фото. Local backup реально восстановлен, миграция проверена; четыре protected набора данных неизменны, реальных решений ради smoke не создавали. Статус доставлен 2/2 Telegram-подписчикам. [Отчёт](operations/2026-09-26-ozon-operation-quarantine.md). Внешние backups отложены; buyer price/inbox, факты для публикации, live stock/shipping и пилот остаются открыты.

Дополнительные browser stages: `quarantine-browser` — 10 сценариев/40 layouts, `quarantine-commercial-browser` — 4/32. Real workflow fixtures проверяют release proof create/update/8229/rejected task/price/stock/archive; dedicated 9024 quarantine end-to-end и новый live-provider эксперимент отсутствуют. Реальные операции 6/7 остались uncertain, 8 succeeded; production review — 11 проверок/24 layouts, только GET/HEAD, все три наблюдённые области product. Account-wide fallback доказан в synthetic suites. При rollback на старый runtime сначала остановить новые operator/provider mutations; безопасный вариант — все три Ozon write flags выключены, ledger/история сохранены.
