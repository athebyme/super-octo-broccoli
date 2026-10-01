# UX-01.11: контрольная матрица для каждого изменения

Дата исходной приёмки: 30.09.2026. Тогда 6 UX и 12 Ozon стадий прошли на своём frozen source; результаты и ограничения — в [отчёте реализации](ux-01-implementation.md). Продолжение 01.10 имеет новые исходники и 8 UX/13 Ozon стадий; прежний результат их не удостоверяет. Final r24 на snapshot7e9f077:8/8UX,13/13Ozon;startupcopy81/counts/no-op passed. Текущие passed/failed/not-tested/blocked: [отчёт продолжения](../operations/2026-10-01-ozon-ux-acceptance.md). Строка считается пройденной только при наличии результата для точного изменения; список критериев сам по себе не является успешным тестом.

## Обязательные измерения

| Измерение | Что проверить | Доказательство |
| --- | --- | --- |
| Объект | SupplierProduct, внутренний товар, WB Product, Ozon listing, draft, proposal и operation названы различимо | Скриншот и путь конкретной синтетической сущности |
| Канал и кабинет | WB/Ozon, два кабинета одного seller; переключение не переносит чужой выбор или запоздалый ответ | Synthetic browser + tenant/account route tests |
| Склад | FBS exact warehouse и FBW read показаны раздельно; смена кабинета сбрасывает несовместимый выбор | Fixtures с двумя складами и отдельной read/write семантикой |
| Возврат | Classic, канонический Vue и beta alias; search/status/link/account/page/per_page и только поддерживаемая сортировка | Адреса до/после, browser Back/Forward, direct detail fallback |
| Допустимый URL | Внешний URL, `//host`, обратные слеши, encoded separators, чужой локальный путь и duplicate query | Server test, отклонение или безопасный fallback |
| Действия | Все старые входы, массовые действия, exports, диагностика и разрешённое восстановление имеют новое место | Scoped action manifest со ссылкой на карту действий |
| Права | Неавторизованный вход, seller без кабинета, другой seller/account, admin-only функции | Flask route tests без реальных ключей |
| Загрузка и отсутствие данных | Loading, empty successful result, no observation, unavailable, stale и failed различаются | Синтетические ответы и скриншоты каждой ветки |
| Цена и KPI | Подтверждённый ноль отличается от unknown; seller/buyer/proposal/currency/source/freshness не смешаны | Таблица fixtures, DOM labels и сохранённый контракт |
| Формы | Dirty ввод, ошибки валидации, conflict, session expiry, потерянный POST, повторное открытие | Browser + contract tests на temporary DB |
| Review и публикация | Локальный save, предложение и external write различимы; версии/CSRF/exact-set/one-attempt/quarantine сохранены | Scoped regression на synthetic transport; реальные writes не выполняются |
| Результаты | 2 строки / 0 успехов / 2 ошибки; частичный успех; running; uncertain; imported без модерации | Отдельные execution/outcome/readback fixtures |
| Фото | Exact source, pending/placeholder/fallback/retry; preview не изменяет publication gallery | Offline source image fixtures и текущие photo contract tests |
| Геометрия | 320/360/390/768/1024/1280/1440 CSS px; sidebar open/closed; light/dark; длинные значения | Viewport, scrollWidth и вхождение текста в контейнер, before/after screenshots |
| Увеличение | Text 200% отдельно от эквивалентного viewport reflow; основные действия и значения читаемы | Метод увеличения записан явно, без подмены text zoom обычной сменой viewport |
| Клавиатура | Tab/Shift+Tab, видимый focus, Escape, возврат focus, доступный tab/menu/dialog | Browser actions и DOM state; смысл не только цветом |
| Работа фона | Polling только допустимых локальных states, без повторного write или сброса dirty context | Spy/fake transport: явный список запросов, zero provider calls |
| Нагрузка проверки | Один тяжёлый browser/DB прогон одновременно; нет production volume/credentials | Команда, image/source manifest и временные каталоги |

## Сценарии приёмки первых задач

**UX-01.2:** `/dashboard/beta` → filtered beta catalog → detail → возврат; отдельно `/marketplaces/listings/` и `/classic`, прямой detail и смена участника карточки. Без нового сортировочного контракта, если он не существует в текущем каталоге. Server allowlist для возврата дополнительно к общему `is_safe_local_path()`.

**UX-01.3:** до исправления воспроизвести именно WB `/analytics`, записать viewport и sidebar state. Синтетические крупные суммы/счётчики и длинный KPI label; 1024 px обязательно, поскольку breakpoint проверяет окно целиком, а sidebar уменьшает содержимое. После исправления повторить ту же матрицу, отдельно local table scroll. `overflow-x:hidden` на body не считается устранением причины.

**UX-01.12:** legacy `max_products=100000`, effective worker cap 1000, значение внутри диапазона, изменение только интервала/порога, отдельное явное изменение лимита. GET и сохранение несвязанных полей не должны молча обрезать legacy значение. Не считать лимиты 300 per request/group и 1000 comparison offers одной глобальной квотой хранения.

## Отчёт после каждой реализованной задачи

```text
Код UX-01.x:
Изменение: проблема, новое действие/состояние и новое место прежних действий.
Источник: точный commit/source manifest; runtime/production отдельно.
Проверки: команды, fixtures, ширины/темы, результаты и артефакты до/после.
Сохранённые контракты: seller/account/warehouse, версии, review, write/retry.
Ограничения: непроверенные и блокированные сценарии; внешние writes не запускались.
Следующая зависимость: файл/контракт/решение, необходимое следующему пакету.
```

Приёмка выполняется actual Flask/Jinja/Vue browser fixtures и контрактными тестами на synthetic temporary DB. Production принимается отдельным bounded read-only source/health/scheduler/HTTPS/browser smoke; его состояние указано в текущем отчёте. Реальные Ozon API writes и публикационные последствия не входят в synthetic gate. Результат прототипа имеет отдельный scope и не закрывает проверки маршрутов приложения. Frozen manifest и команды повторной проверки приведены в отчёте реализации; после объединения с новым Ozon состоянием требуется новый manifest общего source.

## Дополнительные критические сценарии 01.10

| Коды | Сценарий | Обязательное доказательство |
| --- | --- | --- |
| UX-01.2 / WB-EDIT-01 | Pipedream → 50 exact ID между страницами → bulk → назад; сортировка/страница, all-filtered с исключениями, смена фильтра/аккаунта | Actual DOM и server selection, разрешённый URL/безопасный fallback, чужие ID и oversized выбор отклонены |
| WB-EDIT-02/03 | Exact subjects 5880/5070; убрать старые поля при неуспешной схеме; добавить country/weight/multiple | Local fixture schema, dictionary/type/grams validation, права; историческая ошибка провайдера не реконструируется без ответа |
| WB-EDIT-04 / UX-01.7 | Preview selected50/changed2/skipped48 → confirm → replay; provider before drift и local keyword race | Preview без внешнего I/O; только reviewed changed-set; второй POST не пишет; история различает пропуски и ошибки |
| CAT-EDIT-01/02 | Override/inherit/intentional empty/cancel/reopen, порядок двух фото, bulk50, supplier/CSV/AI конфликт | Source facts не содержат ручных/AI значений; exact preview/apply seals, whole-batch conflict, выбранный legacy URL не подменяется supplier slot |
| CAT-EDIT-02 / UX-01.1/.11 | Selected/empty × 7 ширин 320..1440 × light/dark; keyboard reorder в оба края, возврат из preview и async refresh | Все 28 комбинаций без duplicates; same-photo enabled focus, actual focus-visible/outline/viewport; внешний фокус не перехватывается; empty не делает product reads/preview/apply. Node semantics не заменяет browser geometry |
| CAT-EDIT-02 / UX-01.11 | Delayed preview/apply/GET, timeout/409, смена выбранной карточки и уход с dirty вводом | Нет stale token apply или blind retry; перечитывается exact набор; ошибка чтения не маскирует возможную успешную запись |
| CAT-EDIT-01/02 / UX-01.8 | Image Lab manual-empty/manual-photo, exact WB fallback, invalid experiment и late common/link/gallery race | Нет backfill/experiment/checkpoint/launch при конфликте; fallback сохраняется только после request/budget validation и writer lock; read count/fetch не возвращают очищенные фото |
| Ozon / UX-01.6 | Новый источник → draft → exact category/type → human packaging/VAT → AI review → upload → полные readbacks | По одному synthetic write, invalid AI не пишет; unknown write не повторяется и проходит quarantine/read-only reconciliation |
| UX-01.1/.3/.4 | Long data, empty/error, light/dark, 390/1024/1280/1440, keyboard/focus | Нет root clipping; локальный scroll; статические token contrast checks дополняют, но не заменяют actual accessibility/browser matrix |

Каждый synthetic worker/test процесс получает отдельный private `TMPDIR` до запуска Python; production process-shared locks не ослабляются ради тестов. Startup rehearsal выполняется только на task-owned восстановленной копии: шесть ожидаемых изменённых существующих steps, два новых, сохранённые counts, повторный no-op и несовместимый legacy bridge fail-closed. Запреты среды фиксируются как blocked, не как skipped/pass.
