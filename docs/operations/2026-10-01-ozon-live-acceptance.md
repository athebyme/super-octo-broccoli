# Ozon: реальные чтения, секреты и текущая готовность, 01.10.2026

Продолжение на базе main `b7f93bd` в отдельном worktree. Этот отчёт разделяет готовый код, проверки на синтетических данных и фактические ответы Ozon. Предыдущая объединённая приёмка и результат всех 19 задач: [Ozon/UX](2026-10-01-ozon-ux-acceptance.md). Новые runtime-правки ещё проходят приёмку; их merge/deployment до получения квитанций не объявляются выполненными.

## Ключи и Git

Действующий Ozon API key не найден в проверенной достижимой истории Git. Проверены все локально достижимые blobs (включая бинарные/большие) и commit messages, с точным сравнением известных значений и их представлений. 20 известных записей, 14 разных значений, 0 совпадений. Приватные отчёты не включают значения ключей или их хэши; [публичная квитанция](../design/ozon-live-acceptance-artifacts-2026-10-01/git-secret-audit.json) содержит только scope и результаты.

Отдельный тестовый ключ владельца хранится вне Git, файл 0600, каталог 0700. Он отличается от operational key и не записывался в production account. Operational credentials зашифрованы Fernet, валидный ENCRYPTION_KEY обязателен. Файл `.env` переведён из 0644 в 0600 без изменения содержимого; он игнорируется Git. Отслеживаемые env-примеры содержат placeholders, действующих известных ключей там не обнаружено. Тестовые литералы в `check_ux01.py` относятся к isolated child environment с временной SQLite и отключёнными workers.

Историческая эвристика обнаружила 14 старых fallback-кандидатов SECRET_KEY: они не совпадают с известными действующими значениями; их прежнее использование не установлено. Текущий Compose требует непустой SECRET_KEY в обоих сервисах. Неизвестные исторические секреты, unreachable objects, unfetched refs и annotated tag messages этим аудитом не исключены. История не переписывалась, operational keys не ротировались.

## Фактические проверки

| Контур | Выполнено | Результат и граница доказательства |
| --- | --- | --- |
| Каталог собственного товара | Roles, list, info, attributes | HTTP 200, exact собственный product/offer подтверждён; существующие нормализаторы применены в памяти. Не полный обход каталога. |
| Цены, остатки, склады | Prices, stocks, warehouses | HTTP 200; product/offer совпали. Prices/stocks не вернули SKU, это явно ограничивает identity proof. Склады: complete response, 2 строки. |
| Старое import-задание | Fresh roles + один exact task-status read | Roles 200, exact grant есть; status 404. Исход неизвестен, операция не изменена и не отправлена повторно. |
| Фото | Один authenticated HTTPS cache-hit GET | HTTP 200, JPEG 1200×1200, 103974 bytes, exact cache file unchanged. Не cold miss и не публикация/преобразование фото Ozon. |
| Заказы/возвраты/финансы/inbox | 6 bounded reads за завершённый UTC-день 30.09 | FBS/FBO/returns: HTTP 200, по 0 строк, observed has_next=false; finance/day: HTTP 200, 1 строка. Reviews/questions: HTTP 403, cause unknown, без повтора. Это один дневной sample, не полный sync или подтверждённое отсутствие заказов за всё время. |
| Buyer price/скидка площадки | Ранее один exact own-SKU read новым ключом | Наблюдён 403, причина неизвестна. Повторных blind probes нет, данные unknown; old/seller delta не выдаётся за скидку Ozon. |

Квитанции: [7 чтений](../design/ozon-live-acceptance-artifacts-2026-10-01/phase1-result.json), [старое задание](../design/ozon-live-acceptance-artifacts-2026-10-01/phase2-result.json), [фото](../design/ozon-live-acceptance-artifacts-2026-10-01/photo-result.json). [Дневные контуры](../design/ozon-live-acceptance-artifacts-2026-10-01/phase3-result.json). Physical attempts: 7 в первой фазе, 2 во второй, 6 в третьей (15 всего: 12×200, 1×404, 2×403); provider writes и LLM calls — 0. Общий production rate ledger, no retries, no redirects, verified TLS, trust_env=false; транзакция чтения закрыта до HTTP. Raw API bodies, ключи и покупательские данные не сохранялись. Roles подтверждают grant, не фактический доступ к каждому методу.

## Исправления текущего выпуска

Старый единственный `ready/valid` черновик проверялся 25 июля; сохранённое `publishable=true` не соответствовало текущим обязательным данным упаковки/VAT. Backend уже повторно валидировал публикацию; подтверждённый пробел был в UI/DTO, а не доказанная невалидная внешняя запись.

Редактор теперь получает отдельный `current_validation`, рассчитанный существующим локальным валидатором. Исторические validation/status/version сохраняются. Текущие ошибки дают переход к полю, dirty или отсутствующий текущий результат блокирует отправку. Список показывает нейтральную сохранённую готовность и дату; bulk enqueue по-прежнему повторно проверяет exact version/scope. На восстановленной копии snapshot 18:28:04 UTC readonly/query_only проверка старого draft дала 15 текущих errors против 0 сохранённых: изменившийся источник и baseline удаления атрибутов, контент, упаковка, commercial и обязательные характеристики. [Квитанция](../design/ozon-live-acceptance-artifacts-2026-10-01/draft-local-validation.json): 0 DML/provider/LLM calls. Некорректный linked baseline не доказывает отсутствие этих фактов в live карточке; автоматически подставлять физические или регулируемые данные нельзя. Настроенный default VAT подтверждён в аккаунте: его не нужно повторно запрашивать у владельца, но он не доказывает ставку старого конкретного SKU.

Inbox больше не советует подключить Premium Plus по отсутствующему grant или 403 без подтверждённой причины. UI объясняет проверку прав/условий метода и показывает фактический отказ. Existing cooldown, scopes и локальные reply drafts сохранены.

Focused root проверки: editor/routes/list — 32 passed; inbox/service/routes/queue/UI — 51 passed + 8 subtests. Новый stale-ready browser сценарий встроен в существующий сквозной journey и ожидает полного запуска. Отдельные operator helper tests доказывают границы вызовов и честную отчётность, не заменяют runtime gates.

## Полнота функций и ограничения

Account lifecycle, каталог, категории/словари, общий контент, черновики, AI run/review, bulk preparation/import, task/result/quarantine, seller prices, stock proposals/warehouses, read-only заказы/возвраты/финансы/аналитика и inbox уже реализованы; подробные route/action и acceptance карты сохранены в [UX-01](../design/ux-01-action-map.md) и предыдущем [результате 19 задач](2026-10-01-ozon-ux-acceptance.md). Новые правки затрагивают UX-01.5/.6/.8/.10/.11; принятые WB/common изменения не реализуются повторно. UX-01.1/.3/.4/.7/.9/.12, WB-EDIT-01…04 и CAT-EDIT-01/02 сохраняют свои проверки и внешние ограничения предыдущего отчёта.

Настоящая новая публикация требует конкретного подготовленного SKU, подтверждённых физических и регулируемых фактов, review изменения/получателей и одного разрешённого import с task/readback. Старый `ready` не считается таким кандидатом; неизвестные операции не повторяются. Live WB update/merge/rollback, social post, новый AI apply и Ozon запись в этом продолжении не выполнялись.

Отправка ответов покупателям и операции отгрузки не реализованы: текущий план оставляет inbox replies локальными, fulfillment read-only. Это явная граница функций. Для них нужны актуальные официальные payload/outcome/readback contracts и отдельный review write flow; причину недоступности метода или условия подписки не угадываем. [Контракты отгрузки](../design/ozon-fulfillment-write-contracts.md), [inbox](../design/ozon-inbox-workspace.md).

## Выпуск

Свежий online backup: snapshot 18:28:04 UTC, завершён 18:37:21 UTC; raw 14005719040 bytes, gzip 1833150878 bytes, actual restore SHA/size и quick_check=ok. Внешнее хранилище не подключалось. Отдельное восстановление завершилось 18:48:25 UTC в task-owned subpath: exact SHA/size совпали, quick_check=ok; production DB не подменяется. Предварительный c81 runtime startup прошёл 81 current receipts, 0 changed, первый/повторный no-op, protected counts 7 таблиц и receipt-state одинаковы. Эта квитанция не заменяет startup окончательного образа.

Frozen source/manifest/image, полный 8 UX / 13 Ozon gate, первый/повторный startup, merge/push, guarded deployment и production smoke добавляются только после фактической приёмки. До этого эти пункты not-tested/pending. Прямые compose restart/down и повтор неизвестных provider writes не используются.

Промежуточные неуспехи сохранены: первый startup helper остановился на несовпадении private baseline contract до старта контейнера; исправленный baseline использует настоящую imported_products таблицу. Первый UX gate — 7/8: WB browser читал старый DOM до завершения POST; исправленное ожидание exact POST/navigation прошло отдельный 33-check/30-layout debug. Новый stale-ready browser обнаружил Vue render error (validationDate в computed вместо callable methods), затем реальное переполнение text200% на320px; эти проверки доводятся до полного прохода, не объявляются passed заранее.
