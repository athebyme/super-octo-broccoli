# Предупреждения об окончании срока ключа Ozon

26.09.2026. Пакет W1 развёрнут и принят в 17:33 МСК. Production healthy, backup complete; внешнее хранилище остаётся отложенным. Итоговые проверки и оставшиеся границы — в отчёте выпуска ниже.

## Проверенная проблема

`credential_expires_at` уже наблюдается при штатной проверке доступа и блокирует запись после истечения. Account-health показывает дату, catalog умеет объяснять уже истёкший ключ. Но seller заранее не получает предупреждения; в форме подключения срок не показан, а текст замены ключа ошибочно говорит, что любой неподтверждённый результат блокирует замену, хотя same-Client-Id recovery уже разрешён при durable uncertain. Это разрыв между работающим backend и понятным восстановлением доступа.

## План

1. Единственная локальная проекция срока: unknown, вне окна предупреждения, до 14/7/1 суток, expired. Использовать только наблюдённый `credential_expires_at` текущей credential version. Будущая дата не доказывает действительность ключа/права, отсутствие даты не означает бессрочный доступ. UTC в exact моменте; читабельная дата в интерфейсе. Никаких `/roles`/provider probes ради уведомлений.
2. Additive durable notice journal: exact account/seller/marketplace, credential version, observed expiry, highest notified stage/time. Создание Notification и обновление journal в одной короткой транзакции. Удаление/прочтение общего уведомления не снимает dedup. При новом ключе/новой подтверждённой expiry — новая серия. После downtime выдавать только актуальную степень, не все пропущенные. Не выдавать stale version после конкурентной ротации.
3. Singleton scheduler выполняет bounded local discovery, due filter перед LIMIT, небольшой per-tick cap и time budget. Общий account lock сериализует отправку с mutation; текущие facts перечитываются под lock. Нет decrypt, HTTP, LLM, Telegram или provider write. Только существующий seller-scoped центр уведомлений; текст датирует наблюдение, ссылка открывает конкретный магазин и форму замены ключа.
4. Vue account setup: компактная строка срока и warning с конкретной датой/следующим действием. CTA раскрывает существующие настройки и переводит focus в поле нового ключа; обычные настройки/каталог доступны как прежде. Исправить неверное объяснение uncertain gate: активный занятый lock временно мешает ротации, сама uncertain история не запрещает восстановление доступа. Не создавать второй editor ключей и не показывать секреты.
5. Account-health и account API пользуются тем же смыслом срока. UI не снимает server-side expiry/capability/publication gates. После ротации unknown/pending не становится зелёным обещанием доступа. Для неизвестного срока не рассылать угаданное предупреждение.
6. Проверки: границы 14/7/1/0, unknown/future/disabled/WB, deleted/read Notification, restart/dedup, skipped stages, rotated credential and expiry drift, tenant ownership, rollback атомарности, busy account lock, bounded fairness на 100+ accounts, запрет decrypt/provider. Additive fresh/historical/repeat migration + scoped FK guard. Vue real CSRF/session/conflict, keyboard focus и 320/390/768/1440, обе темы/200% zoom; реальные expiry не менять ради теста.
7. Deploy только проверенного exact candidate с backup/rollback и сохранением flags/history/ledger. Production read-only smoke сроков/магазинов, проверка scheduler registration и отсутствие новых provider writes; rare milestone после принятия. Не повторять price-details/inbox probes и прежний price pilot.

## Дизайн

Сохраняются tokens Seller Hub: surface #ffffff, background #faf9f7, text #1a1a1a, muted #6b6b6b, accent #c45d3e; warning/danger и dark берутся из существующих semantic tokens. Шрифт/размеры наследуются из формы подключения; числовая дата обычным читаемым текстом. Выравнивание слева.

Композиция в текущем магазине: статус подключения → строка «Срок ключа» → при приближении срока короткое объяснение + «Заменить ключ» → обычные действия магазина. Unknown нейтрален, expired имеет текстовое объяснение, а не только красный цвет. Декоративные карточки/новые иллюстрации/новая палитра здесь не помогают. Единственный акцент — действие, предотвращающее остановку работы; раскрытие и фокус должны вести прямо к нему.

Критика плана: прошлые Notification являются датированной историей наблюдений, не текущим разрешением на работу. Повторные пороги не должны создавать spam после restart; обычное удаление уведомления не считается согласием получать его ещё раз. План не объявляет W1 целиком закрытым без проверки оставшихся lifecycle/операторских сценариев.


## Реализация

Добавлены pure expiry projection, atomically deduplicated in-app worker и additive journal migration. Scheduler: первый tick через 60s, затем 15 минут; selection cap 100, emit cap 25, budget 5s, SQLite busy timeout 200ms. Не выполняет Telegram/provider/LLM/decrypt. Vue setup показывает срок и раскрывает существующую форму; deep-link из уведомления фокусирует summary, явный CTA — поле key. Общий account-health подчёркивает предстоящее expiry, не смешивая его с provider permission.

Expiry/worker/migration/account-health/account lifecycle проверены вместе с общим regression: **1435 tests +373 subtests passed**. Production expiry не менялось; локальный worker включён, реального due account сейчас нет. Browser, migration rehearsal и deployment приняты отдельно.


Дополнительная проверенная P0-находка: прежний `rotate_ozon_key` не получал просмотренную версию, поэтому две вкладки могли последовательно заменить ключ без конфликта. Контракт расширен обязательным positive `expected_version`, exact сравнением под account lock и 409 до записи. Vue предлагает явный GET/review текущих настроек с сохранением private DOM input и отдельным повторным подтверждением; фоновый polling сам viewed version не подменяет. Это также предотвращает второй rotation при слепом повторе потерянного POST со старой версией. Новый ключ не возвращается в response/state.


## Изолированная проверка реального snapshot

Для schema rehearsal выбран принятый локальный архив от 26.09 13:10 UTC. Exact candidate запускается с `--network=none`, production volume монтируется read-only; отдельные writable mounts — private staging directory и существующий source `.backup.lock`. Lock удерживается на время фактического restore/migration/cleanup, чтобы daily backup не конкурировал за тот же дисковый budget; это не новая копия и не отключение observer. Общий deadline 1800s, reserve 2 GiB. Проверяются restore SHA/size/full quick_check, additive DDL, повторный no-op, полные FK до/после и fingerprints шести protected таблиц. Удаляется только восстановленная временная БД этого запуска; архив и receipt остаются. Restore/migration/cleanup приняты за 542.88s: три новых schema objects, repeat no-op, шесть protected tables и прежние FK violations сохранены.


## Итог 26.09, 17:33 МСК

Пакет реализован, развёрнут и принят: `e81fd556ead03…`, healthy/0 restarts. 1435 tests +373 subtests, 30 browser scenarios/175 layouts; restored real snapshot/FK/protected-row rehearsal и production 25 страниц + recovery + три партии, 73 фото. Новые in-app warnings пока не нужны фактическому expiry, production ключ не менялся. Полный отчёт и открытые границы — [выпуск](../operations/2026-09-26-ozon-credential-expiry.md).
