# Ozon: выгрузка начислений в Excel

Статус: **развёрнуто, healthy, production acceptance пройден**. Проверенный набор runtime-изменений: `services/marketplace_finance.py`, новый `services/marketplace_finance_export.py`, `routes/marketplace_finance.py`, `static/ozon-finance.js`, `templates/marketplace_finance.html`. Схема, модели, миграции, зависимости, scheduler и provider endpoints не изменены.

## Пользовательское поведение

«Скачать Excel» выгружает всю выборку текущего кабинета, закреплённого снимка, дат и фильтров, в том числе при запуске со второй страницы. Пять листов: параметры и полнота периода, итоги отдельно по валютам, начисления, товары и расшифровка услуг. Компоненты поясняют top-level суммы и не прибавляются повторно. Названия связанных карточек — текущий справочник, что явно указано в файле.

Номера, SKU и артикулы остаются текстом, сохраняют ведущие нули и длинные значения. Каждая сумма имеет дополнительную точную текстовую колонку; суммы свыше 15 значащих цифр остаются текстовыми также в основной колонке. Формул, макросов и активных ссылок нет. Пустой файл не объявляется нулевым балансом; неполный период помечен и в метаданных, и в имени файла.

Подготовка показывает ожидание и отмену. Ошибка не убирает журнал, повтор доступен отдельным действием. Смена фильтров/страницы и unmount отменяют выдачу устаревшего результата; смена сессии останавливает обращения. Download проверяет scope headers, MIME, размер и имя. Export не ставит задачу обновления и не обращается к Ozon.

## Проверки кода и браузера

- Regression: **98 tests + 29 subtests** — service/routes/contracts, export, shared refresh, fulfillment и snapshot/date pinning. После оптимизации сериализации повторно: **11 export tests passed**.
- Проверены 31 факт (больше одной UI-страницы), полный состав 121 товар + 121 компонент, отрицательные и малые дробные суммы, несколько валют, foreign children/FK, формулоподобные строки, leading-zero ID, неподдерживаемый текст, caps и очистка временных XML при ошибке.
- Реальная конкурентная WAL-проверка: другой SQLite connection удаляет snapshot/facts/children и commit-ит после начала export READ transaction; файл остаётся согласованным, новый connection уже видит удаление. READ transaction завершается до создания XLSX; незавершённый/чужой/удалённый snapshot не заменяется последним автоматически.
- Offline Chromium с реальными route/template/controller: **5 сценариев, 24 layouts**, 1440/390/320, светлая/тёмная темы; настоящее скачивание и чтение XLSX, фильтры, ошибка/retry, отмена, истёкшая сессия. 0 JS errors/overflow/POST/provider calls.
- Синтетический serialization benchmark на верхней границе: **10 000 фактов + 50 000 дочерних строк**, 12,46 секунды, 1 870 415 байт XLSX, peak RSS 160 188 KiB. Это измерение генерации файла с синтетической коллекцией, не end-to-end SLA чтения БД. Runtime ограничивает весь путь подготовки 20 секундами, 16 MiB текста/файла; превышение даёт 422 без частичного файла.

## Выпуск и восстановление

Production image: `sha256:6e7c579014975181f455074639063a904c4c7086c2ec58d26afb8b3f621a8f47`. Base/rollback: `sha256:9fb48834bec48be57a488ad06f899be2e3ed027253df2b309cd9d2c79b908fd7`. Полный hash diff runtime дерева подтвердил ровно пять перечисленных изменений; secret files в context/image отсутствуют.

Для этого read-only выпуска повторно используется проверенный backup финансового релиза от **25.09.2026 08:19:07 UTC / 11:19 МСК**, а не новый снимок production. Архив `/app/data/backups/ozon-20260925-finance-workspace-predeploy.sqlite.gz`: 1 781 326 056 байт; распакованный SQLite — 13 620 535 296 байт, SHA-256 `94b06449b1c19fdc146f33584ebd834e1c6bcd7a28d3d4c079bf5db333b6b878`. Перед rehearsal архив распакован в отдельную owned QA-копию с повторной проверкой SHA/размера/quick_check, резерв диска не ниже 2 GiB. Новый архив потребовал бы опустить резерв; прежние backups и rollback images не удаляются. Обычный migration guard успешно прошёл на копии и production без ручного изменения журнала. Для rollback самого export достаточно предыдущего образа; backup не заявляется снимком данных на момент новой выкладки.

Полное Flask-приложение на восстановленной реальной копии прошло **4 сценария / 24 layouts / 4 настоящих скачивания**: все 71 начисление при запуске со второй страницы, удержания, выбранный тип услуги, пустой поиск. Число товаров/компонентов и суммы по каждой валюте совпали с API, 0 JS/HTTP/overflow/mutation/provider errors. Итоговый глобальный Cache-Control содержит `no-store, no-cache, must-revalidate, max-age=0`; route-level `private, no-store` заменяется общим security handler, хранение файла в HTTP cache запрещено.

Миграции на восстановленной копии завершились exit 0. Production image запущен в **09:46:09 UTC / 12:46 МСК**, critical environment и flags сохранены. Штатный полный startup завершён; приёмка выполнена в **09:55:59 UTC / 12:55 МСК**. Состояние healthy и 0 restarts повторно подтверждены после завершения проверки и перед финализацией отчёта.

## Production acceptance

- Четыре настоящих XLSX скачаны из Vue: полная выборка со второй страницы (**71 начисление, 41 товарная строка, 91 компонент**), удержания (61), выбранный тип услуги (30) и пустой поиск. Все ID, суммы и валютные итоги сверены с API; counts полного состава совпали. **24 layouts**, 1440/390/320, обе темы, 0 JS/HTTP/overflow errors, 0 refresh POST и новых provider writes.
- Общий проход: **25 страниц / 150 layouts**, шесть разных категорий, **60/60 фото каталога**, без fallback/pending. Шесть ценовых представлений сохранили отдельные base/seller и unknown buyer/скидку Ozon. 0 route/JS/HTTP/overflow errors; browser mutations запрещены.
- Внешний HTTPS/TLS, login и четыре публичных asset: 200, SHA-256 совпали. Singleton scheduler жив и держит exclusive lock.
- Price operations #7/#8 сохранили uncertain/succeeded и по одному attempt; seller price восстановлен до 1059. Stock proposal #3 pending_review, operation_id=NULL. General/manual/commercial flags=1, auto-publish=0. Каталог #27: 8719 карточек, 10 страниц, 0 warnings, completed 09:36:34 UTC; это состояние каталога, не бухгалтерская сверка.
- После подтверждения завершения всех rehearsal/browser процессов удалены только собственные временные QA DB/WAL/SHM, 13 620 535 296 байт базы. Backup/archive/manifests и rollback image сохранены. Свободно **16 210 145 280 байт**.

Private evidence: `finance-export-image-diff.json`, `finance-export-files.json`, `finance-export-restore.json`, `finance-export-browser/report.json`, `finance-export-fullapp/report.json`, `finance-export-production/`, `finance-export-external.json`, `finance-export-verified.json`, `finance-export-release.json`. Реальные XLSX/скриншоты остаются вне Git. В Telegram доставлены два содержательных сообщения: о начале выкладки и о проверенном результате. Промежуточные polling-события не отправлялись.

## Границы результата

Экспорт сохраняет выбранные наблюдённые начисления. Полная сверка репрезентативного периода с бухгалтерским отчётом Ozon, поздние корректировки, P&L и дальнейшая аналитика остаются открытыми W10. Ценовой пилот повторно не выполняется. Stock proposal #3 не одобрен. Buyer price/скидка площадки остаются unknown; прежние price-details probes не повторяются. Остальные A+B gates не закрываются этим выпуском.
