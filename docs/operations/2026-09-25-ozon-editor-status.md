# Редактор Ozon: достоверный статус незавершённой отправки

Развёрнут 25.09.2026 в **07:44 UTC / 10:44 МСК**. Production image `sha256:95d6af011230286937c7385c53a52248711fdfe0c1bf82cd53020cddc7b3d34f`, healthy, 0 restarts. Rollback image `sha256:41536969c86dfd699a693c498d0ccb01a8275093e6ec7e8ea0fccc094d3b56bb` сохранён. Проверка завершена около 07:45 UTC.

## Исправление

Редактор раньше называл любую active operation «Отправка выполняется», включая историческую `uncertain` с остановленной автоматической сверкой. Верхний блок также обещал текущее выполнение. Теперь оба сообщения используют exact `active_operation_id` из уже полученного scoped списка: очередь, отправка, проверка результата, запланированная сверка либо остановленная проверка. При отсутствии подтверждённого статуса предлагается открыть операцию. Во всех неопределённых состояниях сохраняются прежние блокировки редактирования и повторной отправки.

Изменены ровно два runtime-файла: `static/ozon-draft-editor.js` и `templates/marketplace_draft_detail.html`. Hash-сравнение образов подтвердило неизменность backend, migration bundle и остального runtime. Нового polling, provider I/O и изменения write-flow нет.

## Подтверждения

- **12 tests passed:** editor local behavior и существующий result-status UI. Exact ID выбирается при наличии другой операции в списке; unknown/missing/несогласованный terminal status не снимают блокировку.
- Offline Chromium, реальные template/JS/CSS: **8 состояний, 48 layouts** — 1440/390/320, light/dark; 0 JS errors/overflow/POST. Проверены подписи, поле ввода, кнопка отправки, фото и exact переход в операцию.
- Production Chromium: **3 read-only сценария, 6 layouts**, 0 JS/local HTTP errors/overflow/POST. Исторический draft #8 показывает «Нужна сверка» и остановленную проверку; ссылка ведёт в #6, input и отправка disabled. Список черновиков доступен.
- Provider reads/writes в этой проверке: **0/0**. Операции #6/#7 остаются uncertain, #8 succeeded, у всех attempt_count=1. Stock proposal #3 остаётся pending_review, operation_id=NULL. Живой singleton scheduler держит exclusive lock.
- Внешние HTTPS/TLS, login и editor JS — 200; SHA-256 публичного JS совпадает с проверенным образом. Critical environment, credentials configuration и feature flags не изменены.

## Выпуск и восстановление

Использован существующий проверенный архив `/app/data/backups/ozon-20260925-fulfillment-workspace-predeploy.sqlite.gz` от 06:12:38 UTC, `quick_check=ok`, полный gzip roundtrip из предыдущего выпуска подтверждён. Новых DB-изменений/миграций нет; на production перед выкладкой проверено совпадение migration bundle и schema journal. Полный многогигабайтный backup и migration runner для двух display-файлов повторно не запускались. Обычный entrypoint подтвердил `verified migration bundle and schema are current`; обходов startup-check нет. Возврат образа не требует отката базы.

Private evidence: release directory `editor-status-preflight.json`, `editor-status-files.json`, `editor-status-browser/report.json`, `editor-status-external.json`, `editor-status-production/report.json`; production `/app/data/ozon_release_reports/20260925-editor-status/`. Эти отчёты не содержат ключей. Скриншоты остаются приватными. Это узкий выпуск: широкий проход каталога/категорий/заказов зафиксирован в [предыдущем отчёте](2026-09-25-ozon-fulfillment-workspace.md), а не заявляется повторно выполненным здесь.

## Открытые условия полного запуска

Покупательская цена/скидка Ozon по-прежнему unknown: оба ключа ранее получили 403 price-details при наличии метода в ролях; причина не подтверждена. Обычный Chromium отдельно подтвердил 403 сайта документации. Подготовлен [проект обращения](2026-09-25-ozon-price-access-support-draft.md), не отправлен. Настоящие product/stock pilots, shipping/replies, сверка финансов и остальные A+B gates остаются открытыми. Исправление статуса не подтверждает исход старой операции и не закрывает эти условия.
