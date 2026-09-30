# Передача UX-01 и исправления startup главному агенту

**Готово к переносу.** UX-01 и startup исправление приняты и опубликованы на production. Основное дерево другого агента сохранено.

## Откуда переносить

Все ветки локальные, в общем Git-репозитории; push и merge в основную ветку не выполнялись.

| Часть | Worktree | Ветка |
| --- | --- | --- |
| UX-01: код, концепция, проверки | `/home/athebyme/worktrees/seller-hub-ux-01` | `codex/ux-01-2026-09-30` |
| UX production release и отчёт | `/home/athebyme/worktrees/seller-hub-ux-01-release` | `codex/ux-01-release-20260930` |
| Исправление повторных миграций | `/home/athebyme/worktrees/seller-hub-startup-20260930` | `codex/startup-migration-steps-20260930` |

## Порядок интеграции

Сначала главный агент фиксирует собственную текущую интеграцию Ozon: основное дерево имеет незакоммиченные изменения, его содержимое не изменялось этой работой. Затем переносит только собственные коммиты UX в порядке:

```sh
git cherry-pick f681d74bec5a622b0a97c8c290bc07c9af6350cc
git cherry-pick c4dd103759411eb033591c623cceb2976344b66d
git cherry-pick a27ee3c3f9155ecd158ff2880c2ec16c457a314e
git cherry-pick 1bb28b7405a73afe878be6e94929d02c5696f98b
git cherry-pick 0b65cde2a3b9c743e369413e765fe18418ecc123
```

Далее — startup commit:

```sh
git cherry-pick 959178bbcfabfd3e64c86b27f10b24e062b839b2
```

Затем отчёты production/handoff: отчёт UX deployment доступен отдельным `3f52d1c` (его эквивалент в startup ветке — `0d9ff32`; переносить один из них), а итоговый commit `docs(ops): finalize production startup and main handoff` содержит окончательные результаты и эту инструкцию. Его можно найти в startup ветке по заголовку:

```sh
git log codex/startup-migration-steps-20260930 --oneline -3
```

Own commit `959178b` содержит runner, explicit plan, bootstrap, exact legacy certificate, scoped runner integration,42 tests, AGENTS и manifest. Не переносить файлы выборочно из разных снимков: guard/plan/certificate/scoped callbacks образуют единый контракт.

## Что сохранить при конфликтах

- В `AGENTS.md` объединить актуальные Ozon правила с политикой GPT-6.1 Sol/xhigh → GPT-6 Luna/max и новым startup контрактом.
- В routes/templates/static сохранить актуальные Ozon контракты и все полезные действия UX. Карта перемещённых действий: `docs/design/ux-01-action-map.md`. Результаты по всем UX-01.x: `docs/design/ux-01-implementation.md`.
- `docker-entrypoint.sh` вызывает новый per-step runner. Новые миграции главного агента добавляются явно в `scripts/startup_migration_steps.py`; порядок уже зарегистрированных шагов защищён. Добавление новых шагов в конец не повторяет прежние; изменение зависимости затрагивает соответствующие шаги. Изменение исторического порядка требует отдельного reviewed решения.
- Старый source manifest удостоверяет выпущенный снимок; он не удостоверяет будущий объединённый main. После разрешения конфликтов заново зафиксировать итоговые исходники и выполнить затронутые проверки.
- Операционные флаги и persistent Ozon API ledger не сбрасывать. Auto-publish=0 и commercial writes=1 оставлены такими, какими были до UX deployment.

## Коммиты снимка чужой работы

`ae6994b`, `6488975`, `ba63371` содержат снимки незавершённой работы другого агента. Целиком ветки UX/release/startup в main не вливать: иначе эти снимки попадут в историю как отдельная реализация Ozon. `d505dfa` — фиксация release manifest и актуальных на тот момент Ozon документов; актуальные документы главного агента нельзя заменять этой копией. Его нужные deployment инструкции в AGENTS при необходимости объединяются вручную.

## Production и границы проверки

UX runtime опубликован на `https://seller-platform.tech`; текущая запись deployment — `docs/operations/2026-09-30-ux-01-production.md`. Полный frozen UX gate: 109 tests +38 subtests/493 layouts. Полный Ozon v6 gate: 1918 tests +482 subtests/93 browser scenarios/444 layouts. В дополнительном повторе Ozon была одна проверка недопуска process-shared limiter при нагрузке; изолированный модуль прошёл 4/4. Runtime в этом doc-only повторе не менялся.

Production HTTPS, source asset и scheduler проверены; admin вход работает, но у этого admin нет seller profile. Seller сценарии подтверждены synthetic browser matrices; production seller E2E таким входом не проверен. Реальные публикации/цены/остатки/rollback и LLM pilot для приёмки не запускались.

Владелец отменил новую резервную копию для текущего deployment; прежние accepted archives сохранены. По его поручению root filesystem расширен online до147 GiB, около66 GiB свободно; повторного backup из-за расширения автоматически не создавали.

## Startup: итог

Own commit `959178bbcfabfd3e64c86b27f10b24e062b839b2`; report `docs/operations/2026-09-30-startup-step-journal.md`. 42 focused tests, exact schema-only legacy acceptance и первый/повторный79-step запуск на пустой SQLite passed в изолированном CI image/network none. V1 fresh gate обнаружил ошибку row_factory; она исправлена и проверена в окончательной v2. Source manifest `41f25edeb2712b41eedf4bd0bd8e824e3f667e1645015f81018418f162201d31`,1210 files.

Production runtime сейчас `seller-hub:startup-steps-20260930-v2`, exact image `sha256:4773978b6330ed0e32d40f6b59d2ad222884855e5e90d23ce7f9fbd0cd685dea`. Старт30.09 в19:15:49UTC; verified legacy transition занял1.262s, первый healthy event — через7.167s. Журнал содержит79 certified receipts, old migrations не выполнялись; HTTPS200, source hashes и scheduler подтверждены. Предыдущий guard при UX deployment занял639s.

Baseline переход уже выполнен на production: главный агент может добавить новые Ozon шаги в явный план и проверить их как новые, не сертифицируя задним числом. Старые миграции не помечаются выполненными по наличию таблиц: перенос разрешён только для удостоверенного successful bundle/source/schema предыдущего runtime. При неизвестной schema drift/прерванном шаге с неполным DDL или несовместимом порядке запуск останавливается для review.

Production том/ledger и все flags сохранены. Следующий deployment объединённого main должен исходить из этой актуальной runtime конфигурации и заново проверенных источников, а не из старого generic runbook. Старый runtime image сохранён, но rollback на прежний guard с новым журналом не проверялся. Никакие `.env`, private Compose overrides, credentials или DB data из private release evidence в Git не переносить.
