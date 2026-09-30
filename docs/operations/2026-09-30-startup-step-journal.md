# Учёт применённых startup миграций — 30.09.2026

Статус: offline приёмка пройдена; production переключение ещё не выполнено.

## Причина исправления

Прежний whole-bundle digest включал все Python backend файлы. UI route/helper change запускал все старые DML/schema migrations на13GiB базе. UX production перезапущен17:56:10UTC; whole success18:06:49UTC, позже healthy. Это избыточная связь UI и schema работы.

## Контракт

Explicit ordered plan, durable per-step main-SQLite receipts, pending/changed-only execution; исходные FK/domain safety checks сохраняются. Certified legacy bootstrap переносит только exact фактический successful bundle/source/schema/completed_at предыдущего runtime, без повторной записи исторических данных. Новая compatibility map проходит root review и не выводится из наличия колонок. Другие legacy состояния/несовместимый plan/schema fail closed.

Инварианты: code verification до/после шага, success только послеexit0; неизменённые соседние шаги не повторяются; SQL schema drift и неоднозначное interrupted состояние не выдаются за успех; normal seller DML не invalidates schema receipts. Standalone migration CLI доступны.

## Проверки

42 targeted tests passed: fresh/repeat, UI-only, migration/helper change, explicit dependant/new step, source drift, exact legacy transfer/mismatch, schema drift, interruption/checkpoint, corrupt proof/schema и scoped SQLite row_factory. Production journal в момент подготовки: old bundle `8bb293a89bff925150aa6e883cfb10181594f23f911e0b5bc5ad4f359cea6bbb`, schema `25c975bf6724f99e3046763c4b9b6faa235cecf63345ca5891141363f24b6ae1`, completed `2026-09-30T18:06:49.653129+00:00`; factual current=True.

## Промежуточная isolated image проверка

CI image v1: focused suite41/41 и exact schema-only legacy bridge passed (79 certified rows, 0 migration invocations, repeat skip). Полный первый запуск выявил TypeError в новом schema checkpoint: scoped migration меняет SQLite row_factory на sqlite3.Row, который напрямую не сериализуется в JSON. Проверка остановилась; v1 не является принятой полной версией и не опубликована. Ошибка исправляется и полный fresh/repeat gate повторяется на новом frozen image.

## Принятая версия v2

Ошибка row_factory исправлена и покрыта отдельным тестом. CI image `sha256:8a9b34aaaadc898a145050cb50dfb366d468cce2cc6827ba216da4fab2f5657b`, source manifest `41f25edeb2712b41eedf4bd0bd8e824e3f667e1645015f81018418f162201d31` (1210 files). Проверка хешей выполнена до/после каждого gate. Контейнеры: UID1000, network=none, CPU2, memory2GiB, cap-drop=ALL, no-new-privileges; production mounts/credentials отсутствуют.

- Focused suite: 42 passed, 2.89s pytest / 4.18s container.
- Exact production schema-only replay: passed, 79 certified receipts, 0 migration invocations, repeat skipped; 5.85s container. Перенесены только DDL и фактическая whole-success metadata для synthetic fixture, таблицы данных не копировались.
- Empty database: все 79 реальных startup шагов выполнены, последующий повтор skipped и receipts не изменились; 26.90s container.
- `py_compile`, `bash -n`, `git diff --check`: passed. Механическая сверка с прежним entrypoint: 40 direct script+argv и38 scoped child/order совпадают, плюс bootstrap =79 шагов.

Plan digest `44ba2505b03cd00072f6666c62393ce36801efbd60655bb0974b88eebf0cd6d3`; compatibility pin `ed5eb1349fa6c94b5e9ccc0047b89ee0b2d02e269f72bfb5d7ff49394379746e`. Изменение обычного UI не инвалидирует этот план. Другой legacy bundle/schema или изменённый plan до первого certified перехода требуют review; успешный baseline переход предшествует deployment последующих Ozon migrations.

Private host evidence: `~/.local/share/seller-hub/releases/startup-20260930/gates-v2/`; это результаты, не source для build. Runtime image и production измерение будут записаны после переключения. Прежний UX runtime `41d6384c…` сохранён; image сам по себе не удостоверяет обратную совместимость startup journal старого guard. Откат runtime/схемы не проверялся. Новую DB backup/restore rehearsal владелец отменил для текущей операции; отмена сохраняется после расширения диска.
