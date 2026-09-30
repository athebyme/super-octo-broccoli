# Ozon: фоновое обновление входящих и Unicode-поиск

## Состояние выпуска

Образ `sha256:20e795f9925e010ac09d75d9d9087526d371e8e43d39de856943269036721cfd` (`seller-hub:ozon-inbox-durable-20260926`) прошёл тесты и изолированную браузерную приёмку. Production-контейнер запущен 25.09.2026 в 21:37:40 UTC / 26.09 в 00:37:40 МСК. Production-приёмка завершена 25.09 в 21:45:11 UTC / 26.09 в 00:45:11 МСК: **healthy, 0 restarts**. Новый DDL шаг завершился за 31.736 с; сохранены identities всех прежних 3 schedules и 3 requests, CHECK/FK контракты подтверждены.

Предыдущий принятый образ: `sha256:ffd4920d2a2afb546a23dc5cc67065f59e5af73482869bbafe3786c1d359d57b`. Новый образ использует его локальный runtime/base и полный source overlay. Хеши изменившихся 16 runtime-файлов сверены с workspace; dependencies и write endpoints не менялись. Секреты не включены в build context.

## Что изменилось

- POST обновления отзывов/вопросов только создаёт дедуплицированную заявку и возвращает 202. Общий worker продолжает работу после закрытия страницы, сохраняя курсор и паузы при 429/сбоях.
- Отдельные очереди `reviews` и `questions`, единственный период `90d`, exact account/kind/run scope. Регулярный sweep разделяет бюджет двух пар кабинет/тип раз в 15 минут; explicit requests обслуживаются общим worker раз в 10 секунд. До 3 страниц за шаг, общий ledger, один physical attempt/transport call, account lock, 120s lease, 12 calls/45s start budget, timeout 3/6s.
- Capability и due проверяются до bounded candidate LIMIT. Discovery сохраняет действующий legacy denial cooldown. Новый явный recheck может снять только endpoint-level denial; 429 и Client-Id ledger сохраняются. Code 7 не выдаётся за поломку кабинета или доказанный тариф.
- Vue показывает очередь, ожидание Ozon, завершение и отказ. Опрос останавливается на скрытой вкладке. Late/foreign status не подменяет текущий кабинет или тип. Неизвестный POST разрешается наблюдением заявки через GET, без повторного POST; старый terminal/idle ответ не снимает блокировку.
- Фоновое перечитывание ленты сохраняет открытый редактор и несохранённый текст. Известный CSRF 400 не создаёт ложный pending.
- Unicode NFKC/casefold-поиск по тексту, названию, артикулу и SKU; русский регистр не влияет на поиск, `%/_` буквальные, исходный запрос сохраняется в UI/URL.

## Проверки

`241 passed, 67 subtests passed` (59.12 с): новая очередь и migration, legacy read domains, transport/ledger, scheduler, scope/CSRF, retention/drafts, Unicode, Vue request lifecycle и startup guard. Результат сохранён в private release artifacts и `/tmp/ozon-inbox-durable-regression.xml`.

Новая миграция имеет 12 отдельных проверок: старые строки/IDs/lease/cooldown/indexes/sequence, domain-period constraints, rollback после ошибки второй таблицы, unexpected dependencies, existing unrelated FK violations, managed FK rejection, fresh/repeat/outer transaction.

Кроме синтетической БД, из production read-only скопированы только реальные DDL/индексы/строки двух таблиц очереди (3 schedules + 3 requests, без credentials/текста покупателей). На отдельной SQLite-копии миграция перестроила обе таблицы, сохранила все строки и прошла повторный no-op/FK-check. Это узкая репетиция очереди, не объявляется восстановлением всей production-базы.

Изолированный полный Flask/Vue стенд (`network=none`, synthetic credentials, scheduler/write flags выключены): **20 сценариев / 104 проверки вёрстки**, две темы, ширины 1440/768/390/320. Загружены **19/19** фото связанных товаров; одна строка намеренно без связи. Проверены реальные CSRF routes, local save/copy/conflict/lost response, доступность клавиатурой, enqueue/reload, отдельные вкладки, 429, неизвестный enqueue, продолжение worker после нового session, сохранность редактора и terminal denial. Worker использовал только synthetic adapter; actual Seller API/LLM calls и writes — **0**. Скриншоты мобильного ожидания и desktop-редактора просмотрены вручную.

## Production-приёмка

- Inbox: **2 сценария / 16 layouts**, оба exact-kind enqueue/status GET, две темы и четыре ширины. Состояния scope/period подтверждены, POST заблокированы самим harness.
- Общий проход: **25 страниц / 150 layouts**, **6 категорий**, **60/60** первичных фото каталога, без fallback/pending. Ошибки страниц/JavaScript/local HTTP и горизонтальные переполнения — **0**.
- **6 ценовых представлений** сохраняют seller/base/promotion/unknown buyer lanes. Pilot price = 1059; операции 7/8 остаются uncertain/succeeded с attempt_count=1, stock proposal 3 — pending_review, operation NULL. Новых marketplace writes нет.
- Scheduler жив и держит единственный lock. Ключевые environment flags и shared API ledger не менялись, auto-publish=0. TLS/login и SHA опубликованных JS/CSS/Vue assets совпадают с проверенным source.
- В обоих inbox-разделах сохранён наблюдённый отказ Ozon, строк = 0. Реальный доступ не объявляется подтверждённым. Latest completed catalog = 8719 карточек, 10 страниц, 0 warnings; это сохранённое наблюдение, не новый принудительный probe.

## Миграция и восстановление

`migrate_add_inbox_read_queue.py` — standalone шаг после двух additive queue migrations. SQLite create/copy/drop/rename выполняется в одной own `BEGIN IMMEDIATE` транзакции, с FK off вне транзакции и восстановлением исходного режима. Row equality, индексы, AUTOINCREMENT high-water и FK проверяются до commit. `writable_schema` и rename исходной таблицы не применяются. Процедура соответствует [официальному порядку SQLite](https://www.sqlite.org/lang_altertable.html).

Снимки каталога, inbox items/drafts/syncs, price/stock operations и shared API ledger этим DDL не перестраиваются. Обычный startup guard должен завершить полный изменившийся backend bundle; ручной success stamp и уменьшение digest не применялись.

Для выпуска использован принятый [verified backup](2026-09-25-verified-backup.md), snapshot 25.09 в 19:27:50 UTC, возраст 2.16 часа при деплое; его фактическое восстановление уже проверено. Новый многогигабайтный backup не создавался. Перед стартом было 17 287 979 008 байт свободно. Таблицы очереди дополнительно сохранены в приватном release-каталоге; это не самостоятельный полный DB backup.

При rollback на `ffd4920d…` сначала остановить новый worker и убедиться, что нет active requests с domain `reviews|questions`: старый общий worker их не понимает. Если они есть, предпочтительно исправление вперёд; не переписывать статусы и не выполнять blind replay. Расширенные CHECK совместимы со старыми тремя domain, сужать схему или подменять живую БД архивом ради отката UI не нужно. Восстановление всей БД остаётся отдельным проверяемым процессом.

## Открытые границы

Этот выпуск закрывает durable manual inbox refresh и Unicode-поиск. Он не подтверждает реальный доступ кабинета к Ozon inbox, отправку ответов, buyer price/скидку Ozon, stock/create pilots или остальные запускные A+B gates. Никаких повторных live inbox/price-details probes в рамках приёмки; уведомление поддержки и платная подписка не выполнялись.

## Статусы Telegram

Доставка сообщения перед деплоем не подтверждена, повтор не выполнялся. Итоговый статус нового контрольного результата доставлен одной попыткой. Стенд остановлен с exit code 0; production остаётся healthy.
