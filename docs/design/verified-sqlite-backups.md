# Проверяемая резервная копия SQLite

В W8 обнаружен отдельный старый путь: `scripts/backup_database.sh` выполняет `docker cp` файла работающей БД, а при остановленном контейнере — обычный `cp` из volume. Ни один путь не подтверждает согласованность с WAL и отсутствие других writers. Релизные архивы 24–25.09 создавались отдельными Python-скриптами через SQLite Backup API; этот документ не объявляет их ошибочными.

## План изменения

1. Заменить старый shell entrypoint на запуск одного stdlib Python helper от пользователя `app` в существующем контейнере. Нет fallback к прямому копированию файла; остановленный контейнер требует явного запуска helper на доступном SQLite source.
2. Source открывается `mode=ro`, read snapshot закрепляется до incremental Backup API. Процесс не импортирует приложение, не запускает scheduler и не читает ключи. Один advisory backup lock на exact DB не допускает параллельные backup-команды. Работающий WAL writer может продолжать; pinned read ограничен deadline и дисковым резервом.
3. До копирования рассчитать полный raw size из закреплённой БД и потребовать место под raw + ограниченный gzip + reserve. Во время всех этапов проверять deadline/место. Ошибка не превращается в success и не удаляет прежние архивы.
4. Создать raw snapshot, сжать с SHA-256/size и cap, затем освободить только owned raw temp. Реально распаковать архив в новый temp, сравнить SHA/size и выполнить `quick_check` восстановленного SQLite. Одновременно на диске не нужны две raw-копии.
5. Только после проверки публиковать unique `.sqlite.gz` и manifest `status=complete`, файлы 0600. Прерывание может оставить private temp / архив без manifest, которые не являются принятой копией. Ничего автоматически не удалять по retention policy.
6. Проверить WAL, конкурирующего writer после pin, повтор, lock, нехватку места, deadline, cap, corrupt archive и failure cleanup. На production сначала измерить доступное место; при необходимости штатным maintenance освободить только восстанавливаемый JPEG cache, затем создать свежий архив и проверить его. Backup не подменяет отдельное сохранение encryption key или внешнее хранилище.

Основание: [SQLite Online Backup API](https://www.sqlite.org/backup.html), [Python Connection.backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup). В WAL закреплённый read snapshot требует места для дальнейших записей в WAL; deadline/reserve уменьшают риск, но не заменяют достаточный диск и мониторинг.


## Уточнение после прогона на 13,6 GB

Первый прогон выявил избыточную стоимость двух полных quick_check одних и тех же байтов: исходной копии перед сжатием и восстановленного файла. Итоговая команда выполняет полный quick_check именно восстановленного SQLite, а SHA/size строго связывают его с исходным raw snapshot. Manifest явно задаёт `quick_check_scope=restored_snapshot`. Общий default deadline увеличен до 1800 секунд; source/output reserve и archive cap сохранены. Первый незавершённый прогон остановлен без принятия архива и без изменения production DB.


## Приёмка

15 tests passed. Финальная host-side команда создала свежий production snapshot в 19:27:50 UTC, реально восстановила 13,638,524,928 байт, подтвердила SHA/size/quick_check за 709.034 секунды. После очистки восстанавливаемого кэша проверены 7 страниц, шесть категорий и 60/60 фото; production image не менялся, healthy. [Подробности и открытые W8 gates](../operations/2026-09-25-verified-backup.md).
