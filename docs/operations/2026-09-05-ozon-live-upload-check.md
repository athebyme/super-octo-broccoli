# Ozon: попытка боевой загрузки и проверка готовности

Дата: 2026-09-05, 16:55–17:31 UTC. Пользователь предоставил новый API key с административными правами и явно запросил проверку загрузки, валидации и полного пути обработки карточек.

## Результат

**Боевой full flow до подтверждённой записи в Ozon не завершён: выбранные карточки заблокированы данными, а не правами API. Новых физических marketplace writes — 0.** Проверены реальные права, подготовка, валидация, сохранение поштучного результата и открытие массового редактора в production. Повторная отправка старой uncertain operation не выполнялась.

## Доступы и область

- Exact seller 2 / Ozon account 1. Новый ключ принят read-only вызовом `/v1/roles`; наблюдены exact grants `/v3/product/import`, `/v1/product/import/info`, `/v3/product/info/list`, `/v4/product/info/attributes`, `/v4/product/info/limit`.
- У уже сохранённого ключа того же Client-Id также наблюдены все эти права. Он отличается от нового ключа, но штатный operational account работоспособен; реквизиты не менялись в обход защиты незавершённой операции.
- General/manual publication flags включены. Административный ключ Seller API не подменяет admin-owned global reference account и не является подтверждением юридических свойств товара.
- Новый секрет сохранён вне Git в private credential directory (0700), файл 0600. Значения ключей не включены в этот отчёт, repository files, screenshots или stdout диагностик. В браузерном пилоте секрет не вводился: используется штатный уже подключённый кабинет.

## Проверенные этапы

1. Локальная read-only проверка всех 15 существовавших черновиков: ни один не publishable по свежей валидации, даже historical stored `ready` у #8. Основные причины — source drift, отсутствующие обязательные факты/атрибуты и ограничения полного обновления.
2. Для новых карточек выбран exact bounded pilot: draft IDs **3, 9, 14**, canonical ImportedProduct IDs **337, 57, 9201**. У них не было опубликованной linked Ozon карточки. Перед действием сохранены полные before rows этих drafts и canonical sources в private JSON, 49 986 bytes, mode 0600.
3. Настоящий Chromium, authenticated seller session, CSRF и обычный `POST /marketplaces/ozon/uploads/` с `confirm_write=true` и exact тремя source IDs. Provider endpoint напрямую не вызывался. Штатный сервис выполнил source hydration/rebase, применение доступных defaults и validation.
4. Run **`ozon-upload-98d5c653b99e4469b55cd0f56a75fe4c`** завершился: `total=3`, `needs_input=3`, `created=0`, `updated=0`, `failed=0`, `outcome=attention`, внешних operations не создано. HTTP 200 — ответ Seller Hub о завершении локальной подготовки, **не принятие загрузки Ozon**.
5. Result page и переход в repair editor открылись, JS errors 0, overflow false; browser pilot 9.47 s. Полная validation каждой из трёх карточек содержит 8 причин: отсутствуют width/height/depth/weight, единицы размеров/веса, `22232` ТН ВЭД и `23536` маркировка. Краткий run summary по текущему контракту показывает первые две причины; полный список доступен в draft/editor.
6. Дополнительно создан локальный draft **#16** для exact-linked ImportedProduct **888** / listing **31301**. Его полное обновление заблокировано несколькими существующими штрихкодами: current import переносит один, остальные могли бы потеряться. Штрихкоды не удалялись.
7. Бounded read-only поиск альтернативы: активная exact-linked карточка, fresh schema, не более одного barcode, наблюдённые `22232` и `23536`, отсутствие другого draft. Найден один кандидат — listing **34164**, ImportedProduct **275**. Его baseline проверен transient draft без INSERT; SQLite `query_only=ON`, network запрещён. Результат не готов: 6 значений атрибута `4543` при max 4, отсутствующий `8229`, значения вне fresh dictionary у `4559` и `22232`. Наблюдённый ТН ВЭД `3003200000` имеет provider display о лекарственных средствах с антибиотиками, тогда как type карточки — «Вибратор»; такое значение не переносилось и не заменялось догадкой. Новый draft этому кандидату не создавался.

## Старая операция

Для operation **#6** выполнен один штатный `poll_operation(..., allow_submission=False)`. Результат остался `uncertain`, `attempt_count=1`, `poll_count=2`, `next_poll_at=NULL`; после неуспешного подтверждения старого task и истёкшего deadline код — `ozon_task_poll_deadline_exceeded`. Внешняя запись не повторялась, success вручную не выставлялся. Это отдельная историческая сверка, не новый upload pilot.

## Что требуется для продолжения

Для хотя бы одной тестовой карточки нужны подтверждённые габариты и вес **упаковки** с единицами, корректный ТН ВЭД и признак маркировки. Active `ozon_compliance_defaults` и `ozon_marking_registry_versions` сейчас отсутствуют; подписанные решения администратора не фабриковались. Размер товара не приравнивался к упаковке, обязательные факты не угадывались, чужие/legacy dictionary IDs не считались эталоном.

После получения данных: записать их через штатный seller/admin UI в exact scope → повторить полную validation → подтвердить только готовый exact pilot → дождаться operation/task результата → проверить фактическую карточку Ozon. Цены и остатки этим тестом не меняются. До этого нельзя заявлять, что создание/обновление и post-write reconciliation прошли боевой тест.

## Сохранённые артефакты и финальное состояние

- Private `/app/data/ozon_live_checks/2026-09-05/`: before JSON, `pilot-result.json` (0600), result/repair screenshots и screenshot draft #16. Не включаются в Git и не публикуются.
- Сервис остаётся healthy, restart count 0, образ не менялся. Production source code, migrations и runtime flags в этой проверке не редактировались; параллельные изменения сохранены. `git diff --check` чистый.
- Карточки #3/#9/#14 локально актуализированы и провалидированы; создан только локальный #16. Предыдущие данные пилота доступны в before JSON, исторические операции и marketplace catalog не удалены.
