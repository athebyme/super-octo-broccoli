# 26.09.2026: массовое исправление карточек Ozon на Vue

Статус: image `f7a81b581899…` развёрнут в 15:34 МСК, принят в **15:43 МСК**: healthy, 0 restarts. **1375 tests + 373 subtests**, единая команда exit 0 за 555,61 s; **23 browser scenarios / 150 layouts** на точном кандидате. Production: общий проход **25 страниц / 150 layouts**, 6 категорий, 60/60 фото и 6 ценовых представлений; repair — **6 сценариев / 48 layouts**, 3 реальные партии, 13 строк и 13 фото. Полный запуск Ozon остаётся открытым. План: [design](../design/ozon-bulk-repair-vue.md).

## Поведение

Основной экран исправления партии использует Vue и общие поля одиночного редактора. Фото исходного товара, название/артикул, фильтры, выбор между страницами и 15 раскрываемых строк на страницу помогают работать с партиями до 200 карточек. URL сохраняет поиск/категорию/состояние/page, sessionStorage — только выбранные IDs и маркер неопределённого сохранения; содержимое формы там не хранится. Прежняя форма доступна на `/repair/classic`.

Массовое заполнение показывает точные строки, значения до/после и исключения. Официальные значения заново разрешаются в справочнике каждого типа: чужой dictionary ID не копируется. Типизированные Boolean=false, Decimal, collections и max-values используют общий компонент. Смена типа предупреждает о замене характеристик/составных групп; сохранение category mapping требует отдельного явного выбора.

POST остаётся form-urlencoded с CSRF, scope/version/account lock и 2 MiB limit. Ответ содержит результат каждой строки. Успешные карточки перечитываются, ошибочный ввод остаётся в форме. При конфликте показываются сохранённые значения и правки; явный перенос затрагивает только изменённые поля. После timeout/потери ответа повторная отправка блокируется до отдельного GET, включая reload. Изменения касаются локальных черновиков; публикация в Ozon остаётся отдельным подтверждаемым действием.

Браузер обнаружил рассогласование поиска справочника: форма вычисляла текущие missing/replacement поля, а поиск читал старый сохранённый validation JSON. Теперь оба пути используют текущую локальную валидацию; пустой/malformed старый результат не вызывает 500. Проверки подтверждают отсутствие записи validation/version при чтении.

После повторного входа в новой вкладке scoped GET обновляет CSRF текущей сессии, сохраняя несохранённый ввод. Реальный браузерный сценарий очищает cookies, проходит форму входа заново, возвращается в прежний экран и сохраняет прежний текст; отдельный API test подтверждает отказ старому токену и принятие нового. CSRF/session timeout не ослабляются.

## Проверки кандидата

Точный runtime image проверен в network-none контейнере на новой synthetic SQLite с настоящим Flask/Werkzeug HTTP, cookies и CSRF. Подключены только публичный test harness и пустой каталог evidence. Production данные/credentials отсутствуют. Новый bulk-browser: **9 сценариев / 64 layouts**, 200 карточек, 6 типов/справочников, 6 реальных локальных POST; provider attempts=0. Начальное отображение после входа 1,52 s в этом окружении. Общий browser: **14 сценариев / 86 layouts**. Обе темы, 1440/768/390/320 px для repair, keyboard Escape/focus return, preview всех 200 строк; JS/HTTP/overflow errors=0.

Проверены частичный commit и stale version, сохранность ошибочного ввода и явный rebase, cross-type dictionary, смена категории, потерянный POST без повторения и readback после reload, offline/foreign response, истёкшая сессия. Фото в synthetic suite — собственный SVG; реальные фото требуют отдельной production-проверки.

Первая подготовка standalone QA не передала synthetic ENCRYPTION_KEY второму дочернему процессу: общий browser остановился при seed до проверок. Контейнер пересоздан с явным synthetic environment; оба набора завершились exit 0. Это исправление QA-конфигурации, не подключение настоящих ключей и не изменение приложения.

Финальная единая команда завершилась **exit 0 за 555,61 s**: **1375 tests + 373 subtests**, 200 предупреждений устаревающего SQLAlchemy API, без failed/skipped/errors; 440,38 s непосредственно pytest. Оба browser stage и экспорт evidence также прошли. CI image `7faa82df23ff…` содержит только исходники и synthetic tests; production candidate проверен отдельно.

Первый общий browser stage остановился на `/marketplaces/accounts` при 320 px: измерение через 80 ms попадало внутрь существующей CSS-анимации `margin-left` длительностью 200 ms. Отдельный probe с 12 сменами ширины зафиксировал два промежуточных overflow (margin около 76 px), а после завершения — ни одного. Оба browser suite теперь bounded-ожидают завершения анимации shell через Web Animations API (deadline 2s), затем выполняют прежнее строгое assertion отсутствия overflow. CSS/runtime не изменены, overflow assertion не отключён. После исправления и добавления CSRF-сценария полный прогон выполнен заново; результаты выше относятся к окончательным файлам.

## Границы

Image меняет ровно 7 runtime-файлов: route/service repair, основной и classic templates, новый Vue JS/CSS, экспорт общих компонентов одиночного редактора. Схема, миграции, dependencies, scheduler, provider endpoints и write flags не меняются. Общий runner теперь включает отдельный bulk-browser stage с deadline 600s; skipped/empty/ошибки по-прежнему отклоняются. Hosted GitHub CI не запускался.

Полный Ozon launch остаётся открытым: buyer price/скидка площадки, inbox access, реальные packaging/compliance facts, подтверждённые shipping/stock journeys, внешний финансовый отчёт, мониторинг/восстановление вне хоста и длительный пользовательский пилот. Этот выпуск не выполняет новых provider writes или повторных denied API probes.

## Production и восстановление

Переключение сохранило critical environment и credentials; auto-publish=0. Предыдущий image `9d0bf9fd8fe54…` оставлен для rollback. Резервная копия — ранее физически восстановленный и проверенный snapshot 09:06:17 UTC; точные manifest/archive и размер повторно сверены, возраст при деплое 3.48 h, свободно 15,224,647,680 bytes. Новый backup/isolated application restore этим выпуском не создавался.

После штатного startup наблюдён healthy и 0 restarts. Read-only browser проверил три реальные repair-партии, scoped private JSON, фото, форму, предпросмотр, keyboard/focus return, фильтр/reload и classic fallback. Версии черновиков и число операций не изменились. Общий проход 25 страниц/150 layouts и проверка 60 фото прошли без route/JS/HTTP/overflow ошибок; browser mutations=0. Heartbeat scheduler продвинулся на 120.1 s. Прежняя price history, восстановленная seller price и pending stock proposal сохранены.

Публичный HTTPS/TLS, login next и SHA четырёх JS/CSS assets проверены; SHA семи изменённых runtime-файлов совпадают с принятым image. Приватные receipts — `bulk-repair-release.json`, `bulk-repair-production-*`, `bulk-repair-external.json`; публичная агрегированная запись — `output/ozon-ui/rehearsal-summary.json`. Hosted CI не выполнялся; новые live API probes/provider writes не запускались.

Штатная загрузка каталога завершилась после перезапуска: run 53, **8719 карточек / 10 страниц / 0 предупреждений**, completed 12:41:55 UTC. Это работа существующего scheduler, не новая ручная API-проверка.

## Общий статус подписчикам

После принятия production отправлен общий статус готовых возможностей, проверок и открытых ограничений всем активным `/start` подписчикам. Telegram подтвердил **2/2 доставки**, rejected/unconfirmed/deferred=0. Receiver active/running, NRestarts=0; повторной рассылки нет. Приватная квитанция — `bulk-repair-telegram.json`.
