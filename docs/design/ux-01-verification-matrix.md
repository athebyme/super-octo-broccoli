# UX-01.11: контрольная матрица для каждого изменения

Дата: 30.09.2026. Статус: план проверок. Строка считается пройденной только при наличии результата для точного изменения; список критериев сам по себе не является успешным тестом.

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

На текущем этапе подготовлены матрица и автономная концепция. Production UI, текущая БД, Ozon API и реальные публикационные сценарии этой работой не проверяются. Результат проверки прототипа имеет отдельный scope и не закрывает проверки маршрутов приложения.
