# Хранение базы и восстановление

SQLite находится в `/app/data/seller_platform.db`, в named volume сервиса `seller-platform`. Обычная пересборка образа и `docker compose down` без удаления volumes сохраняют этот volume. Сам named volume не является резервной копией: удаление volume, потеря диска или ошибка записи затрагивают данные.

Пути `/app/data/...` в командах ниже относятся к выбранному контейнеру. Фактический mount проверяется через Docker inspect; имя volume зависит от Compose project. Bind mount сам по себе также не стирает данные при пересборке — важно, какой каталог реально подключён и кто в него пишет.

## Согласованная резервная копия

Из корня репозитория:

```bash
bash scripts/backup_database.sh
```

Команда запускает reviewed local helper в работающем контейнере от пользователя `app`. По умолчанию результат — unique `.sqlite.gz` и `.sqlite.json` в `/app/data/backups`, права 0600. JSON `status=complete` выдаётся только после реальной распаковки, сравнения SHA-256/размера с исходным snapshot и полного `quick_check` восстановленного SQLite. Source читается через SQLite Backup API с закреплённым снимком, включая WAL.

`docker cp` или `cp` одного основного файла работающей SQLite-базы не заменяют этот путь: подтверждённые записи могут оставаться в WAL. Не копируйте отдельно `seller_platform.db` из live volume для восстановления или анализа.

Defaults: archive cap 2 GiB, свободный резерв 2 GiB, общий deadline 1800 секунд. До backup требуется место под raw размер базы + archive cap + reserve; во время работы резерв также проверяется. Exact-source lock запрещает параллельные backup-команды. Нехватка места/времени — ошибка, а не повод автоматически удалить старые архивы.

Для отдельно доступной локальной базы:

```bash
venv/bin/python scripts/verified_sqlite_backup.py \
  --database /absolute/source.db --output-dir /absolute/backups
```

Остановленный контейнер не вызывает fallback к raw copy. В recovery-окружении helper запускается явно, с доступными source/output paths. `SELLER_BACKUP_CONTAINER` меняет контейнер для shell wrappers; `--database`, `--output-dir`, `--reserve-bytes`, `--archive-limit-bytes`, `--timeout-seconds` задаются явно при необходимости.

## Восстановление в новый каталог

```bash
bash scripts/restore_database.sh \
  /app/data/backups/<name>.sqlite.json \
  /app/data/recovery-<unique>/seller_platform.db
```

Сначала подставьте выбранный существующий manifest и уникальное имя нового каталога. Команда восстанавливает отдельную копию и **не переключает production**. Существующий parent/DB отклоняется; старые DB/WAL, архивы и сервис остаются на месте. Поддерживаются новый complete manifest и проверенные legacy release manifests с raw SHA/size и подтверждённым roundtrip. Bare `.db` без manifest не принимается.

Перед публикацией restored SQLite проверяются archive size/hash (если есть в legacy manifest), raw SHA/size и `quick_check`. Defaults: raw размер до 64 GiB, reserve 2 GiB, deadline 1800 секунд. `restore-receipt.json` со `status=complete`, `mode=staged_restore` подтверждает только отдельную восстановленную копию. Kill/crash может оставить незавершённый каталог без receipt — это не готовое восстановление.

Локальный вариант:

```bash
venv/bin/python scripts/verified_sqlite_backup.py \
  --restore-manifest /absolute/backup.sqlite.json \
  --destination /absolute/new-directory/seller_platform.db
```

## Перед боевым переключением

1. Зафиксировать точный snapshot и допустимую потерю изменений после него (RPO). Проверить приложение на восстановленной копии и её связь с правильным encryption key.
2. Сначала запускать восстановленное приложение без внешней сети, с `SKIP_SCHEDULER=1` и отключёнными marketplace writes. Snapshot может предшествовать реальным отправкам в Ozon/WB: старые pending/attempt=0 jobs нельзя автоматически переигрывать.
3. Для фактического cutover остановить **всех** writers, включая отдельные workers и ручные процессы, сохранить прежний полный DB/WAL/SHM и возможность возврата. Не заменять SQLite-файл под работающими соединениями.
4. Сохранить внешний по отношению к ORM Ozon rate ledger и действующие cooldowns. Восстановление старой базы не должно сбрасывать ограничения или повторять уже отправленные операции.
5. После отдельно разрешённого переключения проверить migration journal, health, scheduler singleton, seller scopes, данные и внешние outcomes. Возобновлять записи только после reconciliation.

Этот порядок не является автоматической командой production restore. Для отката совместимого кода предыдущий проверенный image может сохранить актуальную БД без возврата к старому snapshot; совместимость схемы должна быть подтверждена.

## Что SQLite-архив не сохраняет

Encryption key и остальные deployment secrets, Ozon rate ledger, публикационные media assets, uploads и прочие файловые данные требуют своего плана сохранения/восстановления. Восстанавливаемый display photo cache — отдельная категория; он не заменяет оригиналы и immutable publication media.

Архивы на том же сервере не защищают от потери этого сервера. Расписание, off-host storage, retention, RPO/RTO и key recovery должны быть явно настроены и проверены. Эти scripts не устанавливают cron и не удаляют старые архивы автоматически. Не добавляйте blind `find ... -delete` как часть release-команд.

## Диагностика

При неожиданно пустой базе сначала проверьте фактический mount, `DATABASE_URL`, existence/размер файлов и migration journal. Не создавайте новую базу поверх ожидаемого source и не удаляйте volume в попытке исправить ошибку.

`database is locked` не считается навсегда устранённым одним WAL mode. Найдите действующие writers и длительные транзакции; не запускайте одновременно миграции, restore и приложение. Перезапуск сам по себе не доказывает исправления причины.

Проверенные выпуски: [backup](operations/2026-09-25-verified-backup.md), [план controlled recovery](design/controlled-sqlite-recovery.md). Актуальные команды и safety-инварианты — в [AGENTS.md](../AGENTS.md).
