# Ozon и остаточная UX-приёмка, 01.10.2026

База: `c6c5431081199eba1dad421063705b8b5476b6c3`, отдельная ветка `codex/ozon-ux-completion-20261001`. Поручение владельца: завершить интеграцию Ozon и переданные 19 задач, проверить сценарии, интегрировать принятые изменения и передеплоить. JSON-экспорт от 30.09 21:19:42 UTC уточняет объём и доказательства; сам экспорт не является разрешением на запись. Разрешение на проверенный deployment дано отдельно в предшествующем сообщении владельца. Записи в маркетплейс проверяются на безопасных фикстурах; реальный пилот требует готовой карточки и явного seller review.

## Основа и последовательность

Сохраняются существующие [карта действий](ux-01-action-map.md), [визуальное направление](ux-01-visual-direction.md), [матрица](ux-01-verification-matrix.md) и [план Ozon](../../OZON_INTEGRATION_STATUS.md). Новая реализация не дублирует уже перенесённый UX-01. Интерфейс остаётся рабочим инструментом продавца в теме «Тёплая редакция»: контекст аккаунта и канала, понятный текущий результат и следующее действие, вторичная техническая диагностика, доступные формы и локальный scroll широких таблиц.

1. Инвентаризация Ozon и происхождения контента; сверка каждой задачи с текущим кодом и существующими проверками.
2. Приоритетные исправления доказанных пробелов Ozon; затем небольшие зависимые изменения WB/common/UI с явными владельцами файлов.
3. Focused contract checks и synthetic browser scenarios на exact принятом исходном дереве; light/dark, 390/1024/1280/1440 px, длинные данные, empty/error/loading, keyboard и права.
4. Обновлённый source manifest, полные offline Ozon/UX gates последовательно с учётом ресурсов хоста; пригодная локальная backup/restore и startup rehearsal.
5. Review и интеграция только собственных коммитов, защищённый deployment через `scripts/deploy_safety.py`, exact image/health/HTTPS/scheduler и read-only smoke. Отдельный отчёт по кодам и внешним ограничениям.

Покупательская цена и скидка Ozon остаются unknown; повторные blind probes после наблюдённого 403 не выполняются. `imported`, роль API и окончание batch не выдаются за модерацию, фактическую доступность метода или успех всех элементов. Изменения цены/остатков сохраняют proposal gates. Локальное сохранение общего контента или черновика не публикует карточку.

## Владельцы и интеграция

Root принимает архитектурные решения, проверяет diff и evidence, владеет этим планом, `AGENTS.md`, общими source manifests, итоговым отчётом, merge и deployment. Workers запускаются явно на `gpt-6-luna / max`, максимум три. У каждого отдельная ветка от базы; main не изменяется во время разработки. Исходное дерево main чистое и опережает origin на четыре коммита на момент начала. На хосте обнаружен уже работающий baseline Ozon gate `/tmp/ozon-ai-repair-gate-20261001`; его результат применим только к его exact источникам.

После смены окружения Git-метаданные основного репозитория и прежние worktrees доступны только для чтения. Разработка продолжена в отдельных локальных клонах `/tmp/seller-hub-{completion,ozon-audit,wb-edit,catalog-ux}-20261001`, читающих исходные Git objects, но пишущих только собственные метаданные и файлы. Это не merge в исходный main. Проверенный отказ доступа к Docker socket и запрет создания локального socket блокируют полные browser/container gates, startup rehearsal на восстановленном volume и deployment. Эти проверки не заменяются статическими проверками и не объявляются passed; независимые unit/Node проверки продолжаются. Доступ должен быть восстановлен до исполнения соответствующих заключительных шагов.

| Worker | Worktree | Начальный scope |
| --- | --- | --- |
| Ozon | `seller-hub-ozon-audit-20261001` | Подключение → чтение → черновик/AI → публикация → readback/recovery, read workspaces; сначала audit, затем согласованные файлы |
| WB edit | `seller-hub-wb-edit-20261001` | WB-EDIT-01..04 и WB-возврат UX-01.2; сначала контракт и предложения ownership |
| Catalog/UX | `seller-hub-catalog-ux-20261001` | CAT-EDIT-01/02, читаемые ошибки и операции, остаточная UX-матрица; сначала карта и ownership |

Общие `models.py`, `seller_platform.py`, `templates/base.html`, runner inputs и миграции не меняются workers без нового согласования. После исследования CAT root разрешил Catalog/UX worker добавить только две колонки ImportedProduct, отдельную additive migration и append-step, dedicated common-content service/routes/UI и override guards в выявленных source/agent/reverse-proposal writers. WB worker остаётся единственным владельцем `seller_platform.py`, включая последующую регистрацию common routes. Ozon worker добавляет сквозную synthetic browser-приёмку с реальными core services и заменой только внешнего транспорта. Frozen inputs удостоверяются новым manifest и затронутой проверкой. API/LLM/production данные не используются synthetic проверками. Secrets, raw responses и личные данные не попадают в отчёты, Git и Telegram.

## Принятый контракт общего редактора

Редактируется seller-owned `ImportedProduct`: title, description, выбор/порядок уже известных фото и общие именованные характеристики. Категории, provider IDs, цены, остатки, `imtID`, связи и публикационные статусы исключены. `content_overrides_json` хранит typed ручные значения с server-derived автором; `content_edit_version` и fingerprint текущего содержимого защищают сохранение от конкурирующих изменений. Существующий `AgentChangeSnapshot` хранит отдельный audit, а `original_data` сохраняет наблюдённые факты источника.

Сначала local-only preview показывает effective/inherited/manual значения и точные контексты связанных каналов. До 50 exact товаров, bounded JSON и строгие типы; signed preview привязан к продавцу/пользователю, версиям, source fingerprints, изменениям и просмотренным channel refs, TTL 600 секунд. Apply атомарно проверяет весь набор и сохраняет только общий товар; любой drift требует нового preview. Existing drafts/live остаются самостоятельными снимками, их просмотренные «получатели» не являются скрытой командой записи. Изменения в канал идут через существующие отдельные формы/review и publication gates.

Refresh поставщика/CSV обновляет исходные факты и наследуемые поля, сохраняя ручные overrides. Agent write конфликтует с изменяемым вручную полем; reverse Ozon→common proposal сохраняет прежний exact-account контракт и требует сначала явного reset-inheritance при конфликте. Ручные характеристики/фото не становятся source evidence. Фото выбираются только из exact существующего source/common pool, без arbitrary URL, нового proxy или неподтверждённого upload.

WB preview также получает durable single-use claim: подписанный nonce сохраняется в nullable unique `BulkEditHistory.review_key` до любого provider I/O. Повтор той же подтверждённой формы возвращает существующую seller-owned историю и не создаёт новый вызов, включая failed/unknown результат. Наличие свежего local fingerprint и transport single-attempt само по себе не защищает повторный POST. Legacy history остаётся совместимой с `review_key=NULL`.

Изменение model schema ожидаемо меняет fingerprints шести прежних startup steps (bootstrap и пять зависимых migrations), плюс добавляются два последних шага: common overrides и WB bulk review key; остальные receipts не пересертифицируются. Приёмка должна проверить именно этот scoped rerun и повторный no-op запуск на отдельной восстановленной копии, а также expected fail-closed для несовместимого legacy bridge.

## Сверка переданного списка

Статусы ниже отражают начало новой работы, а не отменяют прежние результаты. Итоговые passed/failed/not-tested/blocked, коммиты и ограничения будут записаны в отдельный отчёт приёмки.

| Код | Уже существует / что сверить | Остаток и зависимость | Приёмка |
| --- | --- | --- | --- |
| UX-01 | Навигация и первые пакеты дочерних задач | Эпик без дублирующей реализации | Все дочерние evidence, карта, права и reflow |
| UX-01.1 | Карта действий, пять композиций, tokens, preview | Проверка реальных экранов и контраста | Состояния, keyboard, WCAG AA и узкий экран |
| UX-01.2 | Safe listing return и account fallback | WB exact выбор/сортировка/страница; зависит от WB-EDIT-01 | Pipedream → 50 → bulk → назад; malicious return |
| UX-01.3 | Локальный analytics stylesheet и async dataset guards | 1024/1280/1440/mobile, long/empty/error | Нет root overflow, локальный table scroll, расчёты |
| UX-01.4 | Группы меню и legacy routes | Keyboard, старые ссылки, узкий экран; .1 | Каждый полезный вход доступен |
| UX-01.5 | Header, обзор/управление, diagnostics | DESCRIPTION_DECLINE, Unicode и склейка; .1/.2 | Читаемая причина/следующий шаг; WB/FBS/link сохранены |
| UX-01.6 | Existing preparation journey и review | Fixture import/save/prepare/publish/result/fix; .1/CAT | Переходы не публикуют, сохранение отличимо |
| UX-01.7 | Existing WB history/Ozon operations | Batch31 и success/partial/pending; .1 | Результат каждого элемента, fix links, safe rollback |
| UX-01.8 | Legacy/beta/студия и channel links | Возможности/фото/merge/counters; .1 | WB-only явно указан, freshness не смешивается |
| UX-01.9 | Price lanes, proposal review и guards | Fixture preview/confirm/conflict/reconcile/result; .1 | Current/calculated/proposed и exact warehouse |
| UX-01.10 | Settings groups и secret-free health | Reconnect/off/not-ready/schedules/notifications; .1 | Настройка отличима от фактической работы |
| UX-01.11 | Existing synthetic browser matrices | Расширить только gaps текущей версии; .1 | Route/action/rights/account/warehouse/mobile matrix |
| UX-01.12 | Legacy100000/effective1000 и strict validation | Источник cap и boundary tests | Unrelated save не обрезает legacy |
| WB-EDIT-01 | Existing bulk/history | Межстраничный exact-set, all-filtered/exclusions и limits | Scope не расширяется; каталог не загружается в UI |
| WB-EDIT-02 | Working category и schema API | Subject для «Свечи эротик», no stale fields | Problem/working category и invalid denial |
| WB-EDIT-03 | Existing single edit и bulk dictionaries | Add missing permitted fields, country/grams/types | Сохранение/rights, sizes/SKU read-only |
| WB-EDIT-04 | Existing operations/fill-missing/confirmation | Доказанный preview/counts/diff и режим | Empty/invalid не пишет; preview без WB I/O |
| CAT-EDIT-01 | Common model и channel preview | Карта происхождения/priorities/manual/AI/source | Common content отдельно от price/stock/imtID |
| CAT-EDIT-02 | Проверить existing editor/API прежде расширения | Минимальный single/bounded bulk; CAT-EDIT-01/.1 | Inherit/override/photo order, cancel/reopen, recipients |

## Внешние зависимости

Реальная новая карточка пока не имеет подтверждённого полного набора packaging/VAT/compliance. У владельца запрошены данные одного товара; отсутствие этих фактов нельзя обходить defaults или AI. Доступ к buyer price требует отдельного подтверждённого источника. Synthetic acceptance можно завершать независимо, но такие внешние ограничения остаются явно открытыми.

## Зафиксированный кандидат и ограничения среды

После review приняты общий редактор и sealed local apply, typed WB single/bulk edit с exact reviewed changed-set, сквозной Ozon journey, readable moderation/history, coherent UTC analytics и semantic contrast/focus tokens. CAT worker завершил также все выявленные source/agent/canonical/Image Lab writers: fallback WB фото проверяется после request/budget validation и writer lock, без предварительной мутации caller и без восстановления явно пустой ручной галереи. Root разрешил этот узкий пакет в `services/image_lab_service.py`, `routes/internal_api.py`, двух соответствующих route/security test files, worker report и обязательном release test list. WB batch-update test включён как обязательный input из-за изменённого pre-merge drift gate.

Source manifest пересобирается по фактическому explicit COPY Dockerfile; mutable result exclusions дополняются только итоговым отчётом приёмки и отдельным каталогом evidence этого запуска. AGENTS, архитектурный план, runtime/CI/tests и worker contract documents остаются frozen inputs. Host unit stages проверяются на synthetic SQLite в отдельном private TMPDIR и с выключенными scheduler/writes; они не заменяют строгие network-none non-root Docker/browser gates. Chromium даже с `about:blank` не запустился из-за `setsockopt: Operation not permitted`; DOM не проверен. Docker, loopback bind, исходный `.git` и private deployment volume остаются недоступными. Нет разрешения обходить эти ограничения или ослаблять release gate.
