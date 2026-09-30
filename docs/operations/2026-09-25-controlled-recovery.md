# Проверка восстановления приложения из SQLite-архива

Статус: **host-side restore-команда обновлена; изолированное приложение восстановлено и проверено; production healthy, 0 restarts**.

## Что исправлено

Прежний `scripts/restore_database.sh` копировал файл поверх рабочей SQLite до остановки writers. Теперь команда принимает verified manifest и создаёт отдельную копию только в новом каталоге. Existing parent/DB, symlink target, bare DB без manifest и неподтверждённый источник отклоняются. Автоматической замены production, перезапуска сервиса и отката текущих DB/WAL нет.

Helper общий с backup-командой: `scripts/verified_sqlite_backup.py`. Он передаётся из host в существующий контейнер через stdin от пользователя app, поэтому обновление эксплуатационной команды не требует app deployment. Production image остался `sha256:0632b1e7e6cf3386afe6497cf1119dfb930ae196ced0bf2819c2e74a5656a745`. Новые команды и границы отражены в AGENTS.md и переписанном `docs/DATABASE_PERSISTENCE.md`; оттуда убраны raw live copy, blind volume deletion, безусловный restore/restart и автоматическое удаление старых архивов.

## Контракт команды

```bash
bash scripts/restore_database.sh \
  /app/data/backups/<name>.sqlite.json \
  /app/data/recovery-<unique>/seller_platform.db
```

Пути относятся к контейнеру. Локальный helper поддерживает `--restore-manifest` и `--destination`. Manifest ограничен 64 KiB; version 1 требует complete + restore verification, legacy release-форма — raw SHA/size и подтверждённые quick_check/roundtrip. Проверяются archive name/path, размеры, archive SHA при наличии, raw SHA и полный quick_check восстановленного файла. Default raw cap 64 GiB, reserve 2 GiB, deadline 1800s. Требуется место под raw size + reserve.

Данные публикуются без overwrite, в каталоге 0700, SQLite/receipt 0600. Только complete `restore-receipt.json` подтверждает staging. Прерванная публикация без receipt не является готовым восстановлением. Повтор в тот же существующий каталог не удаляет предыдущую копию. Encryption key, provider rate ledger и разрешение переигрывать внешние записи в receipt явно остаются false.

## Проверки кода

**41 tests passed**: прежняя backup-семантика; new и legacy restore manifests; byte-preserved live DB/WAL/archive/history; existing directory с DB и без DB; broken symlink; duplicate/oversized JSON, unsupported version, malformed types/time/hash, traversal; corrupt gzip, wrong size/digest, изменение archive во время проверки; capacity/deadline и cleanup; CLI и отказ старого copy/restart пути. Python compilation, shell syntax и git diff check прошли.

## Настоящий архив и запуск

Использован принятый архив `seller-platform-20260925T193939Z-9d27f378a977.sqlite.gz`, snapshot **25.09 19:27:50 UTC**. Восстановлено **13,638,524,928 байт**; SHA-256 `3a78fa7d815704b5199e002087e37cb3c2cd421d5267a436b222b029c73c2047`, quick_check=ok. Штатная новая restore-команда завершилась за **382.544 секунды**.

Приложение запущено из того же image отдельным контейнером, **network none**, без опубликованных host ports, с SKIP_SCHEDULER=1 и Ozon publication/commercial/auto-publish flags=0. Использована синтетическая конфигурация стенда; это не проверка восстановления внешнего хранилища encryption key. Штатный entrypoint признал journal соответствующим exact image и schema, без ручной пометки. Health достигнут за **9.071 секунды**. Это время старта после готового restore; оно не является полным disaster-recovery RTO.

## Приёмка приложения

Завершена **25.09 20:10:52 UTC / 23:10 МСК**.

- Аналитика: **5 сценариев / 36 layouts**, обе темы и 1440/390/320 px; **1900 SKU / 19 API-страниц**, exact totals/daily/product values, pagination/reload/search/periods, 25 первичных фото.
- Каталог: **7 страниц / 14 layouts**, шесть категорий, **60/60 первичных фотографий**. 0 route/JS/local HTTP/overflow errors.
- До и после startup/browser совпали схема (777 объектов), наблюдения FK (21 прежнее legacy-нарушение) и полные fingerprints шести защищённых таблиц: accounts, operations, commercial proposals, listing snapshots, drafts и background jobs. Указанные очереди и история сохранили прежние строки; provider writes — 0.
- Первое исполнение private browser harness потребовало создания отсутствующего каталога артефактов. Исправлен только harness; приложение не перезапускалось, архив не восстанавливался повторно.

### Фотографии — отдельная зависимость

SQLite хранит ссылки на внешние изображения. В network-none стенде CDN недоступен, поэтому для браузерной проверки отдельно подготовлен private image fixture по реально запрошенным URL архивного seller scope. Это дополнение к уже сохранённым CSS/font/static fixtures и read-only display cache; оно **не содержалось в SQLite-архиве** и не доказывает автономное восстановление всех media assets.

Сделано **52 credential-free public image GET**, получено **51 изображение / 3,427,976 байт**. Один connect timeout к x-story.ru, следующая ссылка не запрашивалась, retries=0. Эти две дополнительные ссылки не включены в fixture; прохождение первичных фото не подтверждает их доступность. Seller API вызовов нет. Сам standby сохранял network none; только browser harness отдавал зафиксированные реальные изображения. Недоступный CDN не маскируется утверждением о полном media backup.

## Завершение и границы

После успешной приёмки остановлен только isolated контейнер (exited 0). Удалены только его owned восстановленные DB/WAL/SHM, принятый архив и evidence сохранены. Свободно **17,594,363,904 байт**. Production остаётся healthy на прежнем image, 0 restarts; production cutover не выполнялся.

Полный operational план остаётся открытым: off-host storage, достаточная ёмкость, key/media recovery, RPO/RTO, сохранение rate ledger/cooldowns и сверка внешних writes после snapshot. Before-live процедуры запрещают слепой replay старых pending/attempt=0 jobs. Изолированная приёмка приложения не заменяет отдельно разрешённое переключение рабочей базы.

Private evidence: `controlled-recovery-files.json`, `controlled-recovery-receipt.json`, `recovery-before.json`, `recovery-after.json`, `controlled-recovery-browser/`, `recovery-image-capture.json`, `controlled-recovery-accepted.json`, `controlled-recovery-terminal.json`, `controlled-recovery-cleanup.json`, `controlled-recovery-release.json`. Реальные изображения, URL fixtures, screenshots и конфигурация стенда остаются вне Git.


Контрольный Telegram-статус о verified isolated recovery доставлен одной попыткой.
