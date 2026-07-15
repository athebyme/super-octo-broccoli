# Фронтенд: элевация «Тёплой редакции» — дизайн-спека

Дата: 2026-07-15 · Ветка: `feature/frontend-ui-polish` (worktree `.worktrees/frontend-ui`)

## Задача

Сделать взаимодействие с UI более интерактивным и приятным, единый стиль, продуманные
иконки и уведомления, цельная система попапов/оверлеев. Диагноз аудита: дизайн-система
«Тёплая редакция» сильна на уровне токенов и CSS, **но недостроена и внедрена лишь на ~10%
страниц** (`.sh-btn` в 10/97, `.sh-card` в 8/97, 707 инлайн-SVG, два конкурирующих стора
тостов, сломанная тёмная тема у части базовых компонентов, нет focus-visible/reduced-motion).

Решения продавца (владельца задачи):
- **Охват:** максимально широко — все страницы + sweep по всем шаблонам.
- **Оболочка:** добавить слим-топбар (крошки + видимый ⌘K + колокольчик-поповер + one-click тема).
- **Тон:** максимально богато, строго внутри палитры «Тёплая редакция», без marketing-декора.

## Айдентика — ЗАФИКСИРОВАНА, не переизобретается

`templates/base.html` остаётся единственным источником палитры (инвариант AGENTS.md).
Терракота (`--accent #c45d3e` / dark `#d97757`), тёплый off-white (`#faf9f7`), тёмный
сайдбар (`#0a0a0a`), Inter + Instrument Serif, радиусы ≤8px, без градиентов/orb.
«Бюджет смелости» тратится **не на базовый вид**, а на слой взаимодействия и обратной связи.

## Signature (одна фирменная вещь)

**Система активности/уведомлений.** Это операционная платформа управления магазином WB —
самое характерное «что происходит с твоим магазином». Здесь мотн богаче всего:
колокольчик → поповер-лента активности с категорийными accent-рейлами, относительным
временем, inline-действиями и пипами непрочитанного; единый визуальный язык тостов
(accent-рейл + статус-иконка + действие); аккуратный chime. Всё остальное — тихое и
дисциплинированное.

## Добавляемые слои токенов (base.html)

- **Elevation:** `--shadow-1/2/3` — мягкие, тёплые, деликатные; только для оверлеев/hover.
- **Радиусы:** `--r-xs 4 / --r-sm 6 / --r-md 8 / --r-pill 999`.
- **Z-шкала:** `--z-dropdown 1000 / --z-sticky 1010 / --z-backdrop 1040 / --z-overlay 1050 / --z-toast 1080 / --z-cmdpal 1090`.
- **Мотн:** `--ease-out cubic-bezier(.16,1,.3,1)` (фирменная кривая), `--ease-in cubic-bezier(.4,0,1,1)`, `--dur-1 120ms / --dur-2 200ms / --dur-3 320ms`.
- **Chart-палитра:** `--chart-1..6`, терракота-led, обе темы.

## Вокабуляр состояний (единый по приложению)

- Активная локация: рейл (nav), подчёркивание (tabs), точка (sublinks).
- Фокус: `--focus-ring` 2px через `:focus-visible` на ВСЕХ интерактивных `.sh-*`.
- Нажатие: `translateY(1px)`/лёгкий scale на `:active`.
- Hover карточек: `--bg-hover` + hairline-lift.
- Статусы: ok/warn/danger/info — один цветовой язык для badge/alert/toast/notif-рейлов.
- `prefers-reduced-motion` — глобальный guard.

## Фазы

- **Ф0. Фундамент токенов** — новые токены; починка тёмной темы (`.sh-alert`, `.sh-confirm-icon`, `.sh-btn--primary/--danger`, pagination-active, tab-count, dropdown-danger); `:focus-visible`+`:active` на все `.sh-*`; глобальный `prefers-reduced-motion`; живой hover карточек; фикс z-index панелей.
- **Ф1. Примитивы** — `.sh-skeleton`, `.sh-spinner`, `.sh-progress`, `.sh-toggle`, `.sh-segmented`, `.sh-chip`, `.sh-avatar`, `.sh-stepper`, `.sh-btn.is-loading` + макросы.
- **Ф2. Реестр иконок** — `templates/macros/icons.html`: словарь ~40 path + `icon()`/`status_icon()`/`spinner()`; прогон shell/оверлеев/макросов; устранение топ-дублей.
- **Ф3. Оверлеи** — `@alpinejs/focus` (focus-trap + scroll-lock + возврат фокуса), единые токены оверлеев, рабочая command palette (фильтр + ↑↓/Enter + .active), theme-aware `ai_assistant`.
- **Ф4. Уведомления** — слияние двух сторов в один theme-aware `.sh-toast`; центр-поповер из колокольчика; фикс звука (`error`→нисходящая, mute, reduced-motion); категорийные цвета; относительное время; успокоенный колокольчик; фикс «пометить непрочитанным».
- **Ф5. Оболочка** — слим-топбар (крошки, ⌘K, колокольчик-поповер, one-click тема); персист сворачивания; flyout/тултипы рельса; `aria-current`; выправить «Конкуренты».
- **Ф6. Страницы** — sweep инлайн-`#hex`→токены (inventory/reviews/finances/analytics/api_settings и др.); миграция zero-adoption страниц на компоненты; chart-палитра; `login` в тёмную тему. **Обходя файлы 4 параллельных агентов** (`agents.html`, `card_quality.html`, `image_lab.html`, `admin_marketplace_category_detail.html`, `static/agent-chat.*`, `ai-chat-popup.*`, marketplace/ozon/infographic).
- **Ф7. Проверка** — component-gallery (self-contained, light/dark) + Playwright-скриншоты; py_compile + Jinja-smoke; `git diff --check`; обновление AGENTS.md (UI-инварианты: единый toast-стор, реестр иконок, запрет emoji-иконок, focus/reduced-motion).

## Инварианты качества

- Обе темы (`data-theme light|dark`), keyboard focus, `prefers-reduced-motion`, отсутствие
  horizontal overflow, mobile/desktop.
- Не трогать безопасность/tenant-логику, только представление.
- Не переписывать `seller_platform.py`/`models.py` без нужды.
- Все правки — в worktree `.worktrees/frontend-ui`; перенос в main — отдельно и аккуратно.
