# Evidence продолжения 01.10.2026

Здесь сохраняются только synthetic результаты и screenshots. Они не содержат production БД, credentials или raw buyer/provider responses. Этот каталог — изменяемые результаты приёмки, не runtime/test/CI input.

`scoped-ozon-journey.json` и два Ozon изображения относятся к worker fixture до финального объединения: 12 checks/20 layouts, zero real provider attempts. В исходной Ozon квитанции нет source hashes; её scope и зафиксированная история запуска не удостоверяют окончательное source tree.

`scoped-analytics.json` и два изображения аналитики относятся к отдельному worker source: 48 layouts/16 state cases, с source hashes внутри отчёта. Они не заменяют новый полный merged browser gate. Остальные изображения той же scoped сессии остались в её исходном временном каталоге.

[Индекс](scoped-artifact-index.json) фиксирует SHA-256 сохранённых артефактов. [Текущий отчёт](../../operations/2026-10-01-ozon-ux-acceptance.md) различает accepted code, unit evidence, scoped browser evidence и blocked заключительные проверки. Новые общий/WB редакторы ещё не имеют passed actual browser evidence: запрет loopback bind останавливает проверку до browser launch. Отсутствующее изображение не подменяется макетом.
