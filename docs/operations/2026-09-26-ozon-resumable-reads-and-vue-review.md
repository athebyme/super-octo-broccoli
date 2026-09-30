# Следующая волна Ozon: возобновляемые загрузки и Vue review

Статус: **волна принята 26.09 в 20:52:12 UTC**. Runtime
`sha256:d7d3f54dbea423867828b9ea714e777929d4893fb59521e841bba25b08d80fda`
healthy без restart; точный поиск и ожидание локального фото исправлены.
CI image `sha256:ffb41bf220d7cee5198d640fe486504d48553b773f592cdc41a9bce6610b7063`.
Backend **1702 tests + 452 subtests** унаследованы от проверенного CI image
`c101b9b5…`, а не повторно прогнаны полностью на d7d3. На точном d7d3
восемь browser stages прошли **72 checks / 344 layouts**. Production-приёмка
этой волны read-only; она не закрывает полный запуск Ozon или provider writes.
Предыдущий image `2a87236b…` сохранён: backend и миграции в нём совпадают с
d7d3, но вернётся дефект фото; фактическое обратное переключение не выполнялось.
Ограничения более глубокого отката на `595fa6cb…` описаны ниже. Предыдущая принятая волна —
[карантин операций](2026-09-26-ozon-operation-quarantine.md).

## Объём

1. Сохранение прогресса внутри большой страницы каталога, когда enrichment
   не укладывается в один worker budget. Обычный размер страницы и физические
   лимиты не расширяются; public listings меняются только после полной страницы.
2. Durable очередь складов и exact listing FBS stock с HTTP 202, безопасным GET
   статуса и продолжением после закрытия страницы. Общий существующий 10s slot
   обслуживает и period requests, и warehouse jobs.
3. Ручная связь с canonical товаром внутри Vue detail и review последствий
   смены категории одиночного draft, включая сохранность несохранённых полей.
4. Найденное при интеграции исправление: encryption format `credential_version`
   не меняется при обычной замене ключа. Manual intents теперь используют
   private ciphertext marker; legacy неизвестный marker не backfill-ится.
   Domain snapshots/history сохраняются, политика provider writes не меняется.

Планы: [catalog checkpoints](../design/ozon-catalog-page-checkpoints.md),
[warehouse refresh](../design/ozon-warehouse-durable-refresh.md),
[Vue linking/category review](../design/ozon-vue-linking-and-category-review.md).

## Приёмка перед выпуском

- Focused проверки каждой ветки и независимый root review общей очереди,
  реальной смены ключа, migration order, tenant/identity/lease границ.
- Synthetic capacity: 1000 catalog IDs с 13–20 enrichment pages; warehouse
  pagination/caps и атомарный final apply на заявленной ёмкости. Финальные
  лимиты принимаются по измерениям, не по наличию константы в коде.
- Заморозка runtime/CI inputs, полный offline suite и browser matrix на точном
  candidate image; layouts, фото, категории, потерянные POST и session expiry.
- Свежая проверенная локальная резервная копия и migration/restore rehearsal.
- Авторизованный deploy, health/observer, read-only production acceptance,
  проверка сохранности provider operations/quarantine и итоговый Telegram статус.

Первый root focused прогон: **58 tests passed / 21.35s**, изолированный
`network=none` контейнер с synthetic DB, image
`sha256:1f5bac37fd1f016711dae03a479b9b0d5c1c475b97e558e4c94ae84eff3030ae`.
Проверены period queue regression, migration identity, реальная same-Client-Id
замена ключа, совместная диспетчеризация, cooldown/lease/занятые scopes.
Это ранний зафиксированный набор до последующих исправлений warehouse crash
boundary/retention/freshness; он не является приёмкой всей текущей волны.
Контрольная точка в Telegram доставлена всем **2/2** подписчикам.

Отдельный root frozen прогон общего bounded read-response helper:
**10 tests passed / 0.54s** на реальном `requests.Response` с synthetic stream.
Проверены остановка oversized 2xx до JSON, фактический byte cap независимо от
Content-Length, закрытие unread socket, сохранение 403 и полного трёхсуточного
Retry-After/ledger defer при oversized либо некорректном 429 body.

Catalog worker завершил frozen focused набор: **59 tests + 3 subtests / 24.30s**.
Изолированный benchmark передал 65 190 824 bytes настоящему streaming decoder,
вставил 1000 listings, затем обновил 1000, сохранив exact source link.
Peak RSS 640 288 KiB, полный apply 1.277/1.171s, от первого DML до возврата
0.314/0.218s. Это representative synthetic fixture без type rows и live API,
а не универсальная верхняя граница. Воспроизводимый helper:
`tests/ozon_release/catalog_checkpoint_capacity.py`.

Warehouse capacity v2: четыре synthetic случая по 10 000 строк (warehouses/FBS,
insert/update+disappearance), SQLite writer-held 0.393/0.789/0.628/0.797s,
wall 2.15–2.82s, peak RSS 218.4 MiB, concurrent read максимум 213ms,
ошибок чтения нет. V1 неверно проверял весь wall time против 2s вместо времени
writer-held: исходный failed artifact сохранён, исправлен только gate,
v2 заново выполнен на frozen inputs. Helper:
`tests/ozon_release/warehouse_read_capacity.py`.

Независимый static review подтвердил порядок трёх миграций и общий scheduler
slot. Rehearsal завершён 26.09 в 22:03 МСК на exact runtime: архив
13:10:16 UTC восстановлен (13 658 419 200 bytes, SHA-256 проверен, quick_check=ok)
за 443.344s. Полная проверка заняла 719.71s. Старые поля 14 таблиц неизменны,
прежние 21 FK violation сохранены без новых, повтор миграций — no-op. Legacy
credential markers остались NULL; новые таблицы пусты. Временная копия удалена,
production volume был read-only, сеть отключена.

Финальный candidate собран поверх принятого runtime: **37 runtime files**,
**52 CI files**, точный overlay проверен независимым чтением SHA-256 из images.
Runtime `sha256:09cd131cdcdcfc71ada09d1fd34bf1e2968509bcce61d36dcee15d557dfd2e71`,
CI `sha256:497a043552ec4ceb06138199adf2609c65c5f19982b713c66b17c2afb067db1a`.
Удалений исходников относительно baseline нет; посторонние worktree изменения
в overlay не включены. Первый полный offline CI завершился: **3 failed,
1698 passed, 452 subtests passed / 538.51s**. Сохранены image/manifest и весь
отчёт неуспешного прогона. Причины: mock route ожидал вызов без нового review
контракта; inbox test ожидал два физических бюджета одного кабинета за tick;
Node DOM fixture не реализовывал `querySelectorAll`. Выполнены точные обновления
тестовых контрактов с сохранением проверок tenant, отдельных domain runs и
held-write guards; runtime не менялся. Исправления проверены отдельно:
route 1/1, inbox 13/13, quarantine DOM 3/3. Второй frozen CI содержит 55 files,
image `sha256:f787bd6d95f931287f64ba3a8e374980a0b91c25ea986b3c83b2afb74f720bb2`;
полный повторный прогон завершился 26.09 в 21:47 МСК: **1701 tests + 452
subtests**, 2153 JUnit cases, failures/errors/skipped = 0. Все восемь browser
stages прошли: **68 сценариев / 336 layouts**, без JS/HTTP/external errors и
provider attempts. Весь runner занял 793.97s. Runtime image остался
`09cd131cdcdc…`. Отдельный Chromium-прогон на exact runtime также прошёл:
те же **68 сценариев / 336 layouts**, нулевые JS/HTTP/external errors и provider
attempts. Тестовые fixtures извлечены из frozen CI image; их SHA-256
сверяются независимо. Migration rehearsal на restored production backup также прошёл.

Warehouse final focused: **26/26**; frozen Chromium: **9 checks / 40 layouts**.
Vue link/category final focused: **24/24**; frozen Chromium:
**7 checks / 24 layouts**. Оба browser набора без JS/HTTP/external errors и
provider attempts. Проверены настоящий повторный вход и обновление CSRF,
сохранность ввода, lost POST без второго submit, hidden-tab polling и 320px
classic layout. Ранние browser failures сохранены в private artifacts;
исправлены реальные deep-link reload/mobile overflow, отдельно исправлен
negative CSRF probe, которому общий browser fetch wrapper добавлял токен.
Root просмотрел screenshots; новые flows используют существующие темы/типографику.
Контрольная точка candidate в Telegram доставлена **2/2** подписчикам.

Deployment preflight прошёл: protected history/links неизменны, все 70
environment values совпадают с прежним контейнером, autopublish=0, age
проверенного архива 5.91h, свободно 19 485 966 336 bytes. Runtime запущен
26.09 в 19:04:41 UTC. Первый запуск завершился healthy без restart; observation 675.52s после
старта мониторинга (около 11.5 минут после recreate). Окно Docker healthcheck
было временно превышено; automatic restart не выполнялся, новых traceback нет.
Общий production Chromium: 25 страниц/150 layouts, 60/60 фото, шесть ценовых
представлений, без ошибок. Bulk workspace: 6 сценариев/48 layouts, 13/13 фото,
versions/operations unchanged. Реальные durable reads через HTTP и singleton
завершили warehouse job 1 и FBS job 2 по одной странице: два склада и две строки
остатков, completed 19:18:35/45 UTC, защищённые таблицы неизменны.

Дополнительная read-only UI/API диагностика на 09cd прошла: Vue-редактор
показал preview последствий выбора фактической категории без сохранения,
commercial-экран прочитал статусы обновления и контролы без нового refresh;
после прохода версии черновиков, связи, события и операции не изменились.
Это подтверждает чтение и навигацию на 09cd; ручная exact-связь оставалась
заблокирована описанным ниже дефектом и полный seller journey не принят.

Финальная приёмка задержана обнаруженным UX-дефектом ручного поиска: старый
точный числовой ImportedProduct ID может быть вытеснен за LIMIT 20 более свежими
подстрочными совпадениями. На source ID 57 FK/tenant eligibility подтверждены;
все выбранные для проверки 57/131/294/337 имеют корректные связи. Узкий hotfix
даёт точному ID первый приоритет, сохраняет остальные сортировки, LIMIT и
FK/tenant guards; отдельная frozen регрессия с 23 decoys прошла, 17 focused tests.
В проверенных image inventories относительно 09cd изменён только
`services/marketplace_product_links.py`; CI image также содержит один новый
регрессионный тест. Frozen full CI на `c101b9b5…` завершил contracts:
**1702 tests + 452 subtests passed / 539.44s**, без failures; browser stages
ещё выполняются, поэтому общий runner пока не объявлен прошедшим. Hotfix
ещё не развёрнут и не принят.

Private browser helper v1 потерял отчёт из-за cleanup после остановки Playwright;
v2 сохраняет отчёт и зафиксировал отсутствие готовых exact candidates на пустом
поиске. V3 воспроизвёл настоящий numeric-ID ranking bug через обычный ручной
поиск. Эти артефакты сохранены. Дополнительный remaining-UI diagnostic проверяет
правильное ожидание поиска типов и выбор exact type вместо первого текстового
совпадения; его результат не считается итоговой приёмкой.

Перед hotfix запущена новая штатная локальная backup-процедура. Для двух
управляемых архивов существующий photo-cache maintenance с one-shot
`PHOTO_CACHE_MIN_FREE_BYTES=20000000000` освободил 523745785 bytes (3613 старых
восстанавливаемых JPEG); постоянный web environment, canonical media, архивы
и rollback images не менялись. Свободно после maintenance 20007264256 bytes;
после backup и финальных фото-проверок запас будет измерен снова. Новый
согласованный snapshot **26.09, 19:26:25.763572 UTC** сохранён в штатном архиве:
1 786 831 960 compressed bytes, 13 660 659 712 restored bytes. Manager
завершил restore/`quick_check=ok` и SHA-256 round trip за **729.939s**;
compressed archive hash перепроверен под manager/source locks, две управляемые
копии сохранены. Внешняя host-обёртка была прервана с code 143 уже после
завершённого manager manifest; итоговый receipt восстановлен из него и
независимой проверки archive hash, а не объявлен успехом по прерванной команде.
Артефакты: `sync-wave-hotfix-backup-receipt.json` и
`sync-wave-hotfix-backup-recovery-evidence.json` в private release directory.
External backup storage отложен владельцем.
Доступ к buyer price/скидке Ozon и остальные внешние launch gates не считаются
закрытыми этим изменением; одинаковые запрещённые/неуспешные API probes не повторяются.

## Контрольная точка 26.09, 23:12 МСК

Hotfix полного CI прошёл: **1702 tests + 452 subtests**, 2154 JUnit cases,
68 browser checks / 336 layouts, 800.47s. На exact runtime 2a87 отдельно
проверен затронутый сценарий: 7 checks / 24 layouts. Полный runtime browser
09cd 68/336 — отдельное наследуемое evidence, не новый прогон на 2a87.
Перед recreate сохранены все 70 env values и восемь защищённых групп данных.
Нормальный startup guard завершился за 679.468s; первый успешный healthcheck
за 691.975s после recreate, restart=0, app traceback=0.

После hotfix production: общий browser **25 страниц / 150 layouts / 60 фото**,
bulk **6 сценариев / 48 layouts / 13 фото**; ошибок нет. Пять локальных GET
подтвердили сохранение completed warehouse job 1 и FBS job 2 с прежними
снимками и freshness; новых refresh POST не было.

Native helper v1 прошёл 5 read-only checks / 24 layouts, но итоговый gate
его отклонил: helper блокировал 11 легитимных image-запросов `ir.ozone.ru`.
Исходный отчёт и failed gate сохранены. Отдельно обнаружен реальный runtime
дефект: 19 локальных ответов photo cache pending (202) превращались в
постоянный fallback диалога без bounded retry. Общие 73 фотографии выше
относятся к каталогу и партиям, не к этому диалогу. Нужны исправление
pending/retry/fallback и повторная проверка холодного кэша; выпуск не принят.
Production остаётся healthy, история операций/черновиков/связей неизменна.

Актуальный список оставшихся задач отправлен в Telegram: **2/2 delivered**.
Ранее сообщение было доставлено 1/2 с одним transport-unconfirmed; эта новая
контрольная точка содержит изменившийся статус, это не слепой retry.
Короткая [доска оставшейся работы](../OZON_REMAINING_WORK_2026-09-26.md).

## Контрольная точка исходников: ожидание фото

В production image `2a87236b…` точный ID уже ищется первым, но локальный
photo-cache miss с ответом 202 по-прежнему может сразу показать «Фото не
найдено» в диалоге связи. Подготовлен узкий source-only fix v7 для фото
**точного seller-owned ImportedProduct** в диалоге и связанной карточке.
Он не меняет снимок Ozon, фотографии для публикации, photo route или provider
API. Для отсутствующего/неподходящего URL запрос не выполняется. При ошибке
изображения UI показывает нейтральное «Загружаем фото…», делает максимум три
повтора через 2/4/6 секунд с 12-секундным deadline каждого запроса, затем
показывает «Фото недоступно» и отдельный ручной повтор. Ошибка `<img>` сама
по себе не доказывает HTTP 202, поэтому ожидание не выдаётся за подтверждённый
статус cache worker. Скрытая вкладка и закрытие диалога останавливают таймеры;
автоматического возобновления после возврата вкладки нет.

Frozen source manifest v7: `SHA-256 70a56552cf20284064a22dbe9884ebe30932dfc7f453696186f3d04b7c8f1c2b`.
На изолированном CI image `c101b9b5…` с `network=none`, non-root и только
тремя заменёнными UI-файлами **12 focused tests passed**; Chromium прошёл
**11 checks / 32 layouts** (320/390/768/1440, обе темы), без JS/HTTP/external
errors и provider attempts. Synthetic cold-cache 202 → JPEG проверен без
перезагрузки для диалога и связанной карточки; загруженное изображение не
содержит fallback. Отдельно проверены конечный fallback, полный размер кнопки,
отсутствие внутреннего переполнения и отсутствие новых GET после закрытия.
Root просмотрел screenshots 320 light и 390 dark. Headless Chromium не менял
`document.hidden` при переключении вкладок, поэтому проверки скрытого состояния
явно генерировали `visibilitychange`; это синтетическое evidence, не
production-наблюдение.

Root собрал candidate runtime `sha256:d7d3f54dbea423867828b9ea714e777929d4893fb59521e841bba25b08d80fda`
и CI `sha256:ffb41bf220d7cee5198d640fe486504d48553b773f592cdc41a9bce6610b7063`:
проверены точные три runtime и четыре CI отличия, неизменность конфигурации
образов и сохранение исходного префикса слоёв с новыми COPY-слоями; startup
bundle не изменился. На этой промежуточной точке общий exact-runtime browser
all8 ещё выполнялся; его итог указан ниже. Предварительная cache maintenance не достигла
целевого запаса 18.7 GB; builder удалил только неиспользуемый Docker cache
старше часа, не затронув образы и архивы. Повторная production-проверка
холодного кэша на этой точке ещё предстояла. Прочие внешние launch gates
оставались открытыми.

## Финальная приёмка волны 26.09, 20:52:12 UTC

Принят точный runtime `d7d3f54d…`, развёрнутый в 20:46:55 UTC: normal
startup guard показал healthy через **6.797s**, restart=0, singleton
scheduler healthy. Все **70** значений Compose environment и live schema
неизменны. Существующий проверенный migration rehearsal на runtime `09cd…`
переиспользован: новый UI photo fix не добавлял миграций. Receipt:
`sync-wave-photo-fix-release.json` в private release directory.

Backend contracts **1702 tests + 452 subtests / 2154 JUnit cases** относятся
к наследуемому image `c101b9b5…`; нового полного backend CI этим hotfix не
заявлено. На exact runtime `d7d3…` все восемь browser stages прошли
**72 checks / 344 layouts**, provider attempts 0. Отдельный source-only v7
дал 12 focused tests и 11 synthetic browser checks / 32 layouts; проверка
hidden-tab в нём использовала явно сгенерированное `visibilitychange`.

Native read-only production browser прошёл **5 checks / 24 layouts**. Для
фактического source ID 57 первый локальный photo GET вернул **202**, один
повтор — **200**: JPEG появился без error overlay за **2.03s**. Семь
разрешённых CDN images загрузились; ошибок JS/HTTP, заблокированных запросов
и новых записей не было. Общий production browser: **25 страниц / 150
layouts / 60 фото**; bulk: **6 сценариев / 48 layouts / 13 фото**. Пять
локальных GET подтвердили два старых completed warehouse/FBS jobs и их
снимки; все восемь protected групп до и после совпали. Новых enqueue/read
POST, provider writes и локальных операций ради этой приёмки не создавали.
Root просмотрел live screenshot 390px со считываемыми настоящими фото.

Владелец после приёмки сообщил о полном доступе ключа. Точечный `/v1/roles`
в 20:57:31 UTC вернул HTTP 200: 41 роль, 554 метода, exact grants всех 11
проверенных upload methods (import/import-status/quota/list/info/attributes/
pictures и category tree/attributes/values). Наблюдённое состояние подключения
осталось `connected`; его локальная версия 5→6. Роли подтверждают разрешения,
но не фактическую публикацию или доступ к buyer price/inbox: прежние 403
остаются историческими наблюдениями. Проверка использовала один read через
общий ledger, ноль provider writes; operations/drafts/proposals/quarantines
не изменились. Полный запуск Ozon, реальные create/update и stock-write
пилоты, shipping labels, finance postings, hosted CI, пользовательская
проверка и внешний backup остаются отдельными открытыми gate.

Итог принятой волны и переход к новому приоритету загрузки карточек отправлены
в Telegram всем активным подписчикам: **2/2 delivered**, без повторной отправки.

## Условный rollback

Прежний image `595fa6cb7ea7…` сохранён. Он поддерживает карантин, но не новые
warehouse jobs, catalog checkpoints и credential marker ручных заявок.
Старый image не запускался на схеме после этого upgrade; статическое ревью
не доказывает startup compatibility. Откат не проверен фактическим переключением
и не является автоматическим возобновлением новой очереди. Предпочтителен forward fix.

Перед откатом требуется остановить новые HTTP/enqueue действия и singleton,
дождаться границы tick/commit и зафиксировать активные jobs/checkpoints/requests,
holds и attempted operations. Сохранить текущую DB/WAL, staging/deadlines,
credentials, shared ledger и audit; не заменять базу старым архивом и не
сбрасывать заявки. Два runtime одновременно не запускать. Старый image
стартует с `MARKETPLACE_OZON_ENABLED=0` и publication/commercial/auto-publish
flags=0, пока новые структуры не завершены новым кодом либо не принят
проверенный план восстановления. Submission flags не отключают сверку уже
отправленных операций; для полного прекращения provider I/O scheduler также
остаётся остановленным. В фактическом rollback receipt должны быть время,
наблюдённые counts, flags и факт работы reconciliation, а не обещание нулевого
трафика. После drain прежний код всё равно не получает новую защиту credential
rotation автоматически.
