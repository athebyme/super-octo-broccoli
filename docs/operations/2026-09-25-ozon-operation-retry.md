# Паузы Ozon и конечный срок сверки операций

25.09.2026. Кандидат `seller-hub:ozon-operation-retry-20260925`, image `cbec4f1cc4f4347c73982e82d3d418bf0966cafb846e2c810da17c5295ddae03`. Развёрнут в 22:38:44 UTC (01:38:44 МСК), healthy около 22:47 UTC; post-deploy проверки завершены в 22:49 UTC. Restart count 0.

## Исправленные дефекты

- Task polling сокращал `Retry-After` до 600 секунд. Accepted update task при API-ошибке полного live read не проверял deadline и продолжал автоматически опрашиваться.
- Commercial price и warehouse-stock read wrappers теряли `retry_after`, а сверка назначалась через 30 секунд.
- Ручная сверка не учитывала сохранённый provider cooldown на уровне workflow. Account-wide transport ledger уже предотвращал физический вызов, но это не заменяет корректное durable расписание и не должно быть единственной защитой после восстановления ledger.

`marketplace_operation_retry.py` сохраняет только UTC `provider_read_not_before` в существующем request summary. Полный finite delay не сокращается, дробные секунды округляются вверх, непредставимый положительный delay становится `datetime.max`. Время проверяется под account lock; обычный локальный интервал не запрещает явный ручной read, а пауза провайдера запрещает. Срок, выходящий за deadline, останавливает автоматическую сверку, сохраняя неизвестный исход и cooldown. До записи операция завершается без write. Создание, обновление, archive и price/stock reconciliation используют этот контракт. Exact read-back, tenant scope, snapshots и максимум одна попытка записи сохранены.

Vue-уведомление на странице операции показывает паузу в локальном времени браузера и отдельное состояние «Автоматическая проверка остановлена». GET статуса остаётся локальным; остановленная операция не вызывает автоматический GET polling. Ручная сверка остаётся доступна после паузы. Схема БД, ключи и deployment flags не меняются.

## Проверка кандидата

- 121 тест публикаций и commercial, 17 subtests; `/tmp/ozon-operation-retry-focused.log`.
- 11 edge-case проверок shared cooldown: fractional, invalid, overflow, сохранение более длинной паузы; `/tmp/ozon-operation-retry-unit.log`.
- Настоящий Vue component из release image: 5 состояний, 30 desktop/mobile light/dark варианта, 0 JS errors/overflow, остановка локального polling подтверждена. Сеть контейнера отключена; 0 реальных provider reads/writes. Приватные screenshots/report: `operation-retry-browser/` в release directory. Первый запуск harness не имел UTF-8 meta/header; исправлена только тестовая оболочка, приложение уже задаёт UTF-8.
- Общая Ozon regression: 979 passed, 335 subtests, 186 прежних warnings, 88 test files в трёх последовательных частях (357/322/300). JUnit reports без failures/errors/skips. Первый единый прогон завершился сигналом 143 до итогового отчёта и не засчитан. Свежий backup: snapshot 22:18:41 UTC, quick_check и полный gzip round-trip подтверждены, SHA-256 `2cabd5f817a17beef0e7d5b32756fb92976da2db2158a86d6f89cf2a0d26b3f9`. Полный штатный migration runner завершился на копии в 22:37:45 UTC с exit 0 и собственным success journal. Первый deployment preflight остановился до переключения production: read-only mount не позволил SQLite создать служебный sidecar. Повторная проверка использовала writable mount только QA-копии при том же `mode=ro` соединении; журнал не правился, полный migration runner не повторялся. Затем проверенный candidate был развёрнут.

Реальная stock proposal №3 остаётся pending_review и не утверждена. Реальные price/stock/product writes этим изменением не выполнялись. Ранее восстановленная цена и неопределённая product operation не переписываются вручную.

## Production после переключения

- **25 страниц / 150 layout-theme вариантов**, дополнительно 12 вариантов commercial form, 6 ценовых представлений, 6 фактических категорий, **60/60 фото**. 0 route errors / JS errors / local HTTP errors / overflow / blocked browser mutations. Проверены также реальные страницы операций #6/#8 и pending stock proposal #3. У #6 отображается явная остановка автоматической сверки.
- HTTPS/TLS login и 10 static assets: 200. UI через CSRF успешно прочитал два склада и две stock observations; ни creation, ни approve POST не выполнялись.
- История price pilot сохранена: #7 uncertain / #8 succeeded, по одной попытке; сохранённая наблюдённая seller price 1059, old_price 1462, min_price 0. Каталог повторно завершился автоматически: run #16, 8719 карточок / 10 страниц / 0 warnings, 22:12:14 UTC. Это свежесть provider snapshot, не время деплоя.
- Scheduler жив и держит exclusive lock. General/manual/commercial flags = 1, auto-publish = 0. Ключи и параметры окружения сохранены.
- Proposal #3 осталась `pending_review`, operation_id=NULL. Operation #6: uncertain, attempt_count=1, poll_count=3, next_poll_at=NULL. Новых реальных price/stock/product writes этим выпуском нет.
- После завершения QA и production gates удалена только временная raw QA DB с WAL/SHM. Все подтверждённые архивы, manifest и приватные отчёты сохранены. Освобождено 13 604 171 776 logical bytes, доступно 18 852 802 560 bytes. Следующий QA-прогон требует новой копии либо восстановления проверенного архива.

Приватные отчёты: `/app/data/ozon_release_reports/20260925-operation-retry/`; host copy — `operation-retry-production/` в закрытом release directory. Логи `/tmp/ozon-operation-retry-{deploy-second,post-deploy,production-browser,external-health,state-verification,cleanup}.log`. Проверенная предыдущая image `e94c3ca4ef95e3a2bdbfe141472b194af82af3dce2a8f779043209e1a0c1d6cb` сохранена для rollback.

Этот выпуск закрывает дефекты расписания/конечного срока сверки, а не весь план Ozon: buyer price/скидка площадки, current real product/stock pilots и ежедневные write-сценарии остаются открытыми.
