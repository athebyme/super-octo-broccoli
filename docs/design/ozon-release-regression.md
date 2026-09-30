# Ozon: повторяемые проверки выпуска

## Проблема и план

W8 требует CI для контрактов, изоляции продавцов, неизвестного исхода записи, миграций и браузерных сценариев. Сейчас сервисные/Node-тесты находятся в репозитории, а сквозные Chromium-проверки выпусков — в приватном release-каталоге. Они подтверждают конкретные выпуски, но не являются воспроизводимой проверкой нового checkout. На момент начала работы production находился на принятом image `b2fda91b09480…`.

1. Добавить отдельный проверочный Docker image: Python 3.11, Node, системный Chromium, runtime requirements и pytest. Он не запускает production entrypoint, не прогревает AI-модели и не подключает рабочие volumes, `.env`, Docker socket или credentials. Установка зависимостей выполняется при build; сами проверки — в `--network=none`, без публикации портов, от непривилегированного пользователя.
2. Одна команда запускает существующие Ozon/marketplace contract, tenant, proposal/reconciliation, migration и UI-controller suites, затем браузерный сценарий полного Flask/Jinja/Vue приложения с новой синтетической SQLite. Отсутствие Node/Chromium, skipped tests, timeout и пустой набор не считаются успешной проверкой. Отчёты и screenshots сохраняются вне контейнера даже при ошибке.
3. Browser harness запускает настоящий Flask/Werkzeug HTTP-сервер на container loopback с авторизацией, CSRF, handlers, services и SQLite. Cookies/redirects проходят реальный Chromium transport; fault injection перехватывает отдельные ответы. Внешние API/LLM запрещены. Secure cookie отключён только для локального HTTP fixture. Это full-app browser integration, а не доказательство production Gunicorn/TLS или реального Ozon. Они остаются отдельными production/live gates.
4. Синтетические магазины и карточки покрывают чужой кабинет, разные категории, цены base/seller/promotion/unknown buyer, поиск/пагинацию/URL, формы, видимые ошибки и завершение фоновой локальной задачи после закрытия страницы. Публичные JS/fonts оболочки берутся из отдельного фиксированного test asset bundle с SHA/source/license; реальные фотографии, товары, ключи и snapshots туда не входят. Фото сценария — собственный SVG fixture. Runtime templates/assets остаются неизменными.
5. Workflow GitHub Actions использует тот же контейнер/команду на pull_request, push и ручном запуске; только `contents: read`, без secrets/deploy, actions закреплены commit SHA. Сохраняются отчёты, но не БД/сессии. Пока изменения не опубликованы в GitHub, успешный локальный запуск не объявляется успешным hosted CI.

## Приёмка

- Фактический запуск нового image из текущих файлов, без приватных release resources и production mounts.
- Подтверждённая Docker network isolation, синтетический reset, полный состав/число тестов, отсутствие skipped/errors и meaningful Chromium assertions.
- Проверка негативного исхода: намеренно сломанный браузерный сценарий или контракт возвращает ненулевой код и оставляет failure evidence, а не зелёный summary.
- Документированные границы покрытия, команда и status CI. Это не замена реальному товарному/stock пилоту, buyer price/inbox grants, независимому финансовому отчёту и семидневному наблюдению.

Workflow основан на [синтаксисе GitHub Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax); ограничения browser container сверены с [Playwright Docker](https://playwright.dev/python/docs/docker).

## Найденный дефект пользовательского сценария

Чистый прогон подтвердил потерю `next` при POST формы входа: сервер умел проверять адрес, но template его не передавал. Форма теперь сохраняет исходную ссылку и после неверного пароля. Проверка локального пути также отклоняет backslash/encoded separator/control characters, которые браузер может нормализовать иначе, чем URL parser. Это два runtime-файла, принятые в production 26.09 в 14:26 МСК после отдельной проверки точного образа.

## Статус

Принято локально и в production **26.09, 14:26 МСК**: 1357 tests + 373 subtests без skipped/errors; единая команда exit 0; browser 14 сценариев/86 layouts, подтверждённый negative drill. Production image `9d0bf9fd8fe54…`: 25 страниц/150 layouts, 6 категорий, 60/60 фото, healthy/0 restarts. [Операционный отчёт](../operations/2026-09-26-ozon-release-regression.md) различает CI image, runtime image и фактическое покрытие. Workflow подготовлен, hosted CI ещё не запускался. Telegram-подписки и общий статус приняты отдельно: `docs/operations/2026-09-26-deploy-telegram-subscribers.md`.


Расширение 26.09: отдельный stage `bulk-browser` (600s) на 200 карточках/6 типах. CSRF/session re-login, partial/version conflicts, unknown-response readback, preview 200 строк и bounded ожидание CSS shell transition перед строгим overflow assertion. Последняя полная приёмка: 1375 tests + 373 subtests, 23 browser scenarios/150 layouts, exit 0. [Выпуск](../operations/2026-09-26-ozon-bulk-repair-vue.md).

Принято 26.09, 18:38 МСК: `account-history-browser` (600s), не менее 8 сценариев. Real CSRF settings при uncertain operation, два настоящих браузерных документа, конфликт/явный review, потерянный ответ уже сохранённого POST, независимость контекста ключа, lazy history и 30+1 keyset, network/foreign/401 failures, 320–1440px обе темы и 200% equivalent reflow. Полный runner 1464 tests +385 subtests, 38 browser scenarios/200 layouts, exit 0; [отчёт](../operations/2026-09-26-ozon-account-settings-history.md).


Принято 26.09, 20:14 МСК: stages `quarantine-browser` (10/40) и `quarantine-commercial-browser` (4/32), каждый до 600s. Полный runner: 1634 tests +452 subtests, 52 browser scenarios/272 layouts, exit 0 за 708.47s; тот же runtime отдельно прошёл все шесть browser modules. Первый CI остановился на race старого credential harness: чтение fonts выполнялось до завершения document navigation. Теперь exact navigation ожидается до чтения mounted Vue; ошибки не подавляются и checks не ослаблены. Runtime не менялся из-за этой правки теста. [Приёмка и ограничения](../operations/2026-09-26-ozon-operation-quarantine.md); hosted GitHub execution по-прежнему не заявлен.
