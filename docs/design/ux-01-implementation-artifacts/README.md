# UX-01: артефакты приёмки

Источник, результаты и ограничения: [отчёт реализации](../ux-01-implementation.md), [паспорт проверки](acceptance.json), [source manifest](../ux-01-source-manifest.json). Все данные вымышленные, изображения относятся к isolated actual Flask/Jinja/Vue fixtures. Автономная концепция хранится отдельно в `../ux-01-preview-artifacts`.

## Итоговые отчёты

- [UX runner](ux01-report.json) и [JUnit](ux01-results.xml).
- [Analytics](analytics-after.json), [listing](listing-after.json), [workspace](workspace-after.json), [preparation](journey-after.json), [operations/pricing](operations-pricing-after.json).
- [Полный Ozon gate](ozon-summary.json) и [JUnit contracts](ozon-contracts.xml).

Исходные reports скопированы без изменения. Их временные пути указывают место исполнения в контейнере; индекс `acceptance.json` связывает сохранённые изображения с исходными fixture paths и хешами. Полные logs и screenshots финального прогона дополнительно остаются в `/tmp/ux01-final-v6-20260930` на host; этот каталог не заменяет проверенный source manifest.

## Пять рабочих областей после изменения

| Область | Узкий экран | Широкий экран |
| --- | --- | --- |
| Каталог и переход в карточку | [Обзор, light 390](after/listing-after-matrix-390-light-overview.png), [управление, light 390](after/listing-after-matrix-390-light-management.png) | [Каталог с page/filter context](after/listing-after-catalog-initial-page-two.png), [обзор, dark 1440](after/listing-after-matrix-1440-dark-overview.png) |
| Подготовка черновика | [Light 390](after/journey-worktree-draft_detail_vue-light-390.png), [dark 390](after/journey-worktree-draft_detail_vue-dark-390.png) | [Light 1440](after/journey-worktree-draft_detail_vue-light-1440.png), [dark 1440](after/journey-worktree-draft_detail_vue-dark-1440.png) |
| Операции | [Light 390](after/wb_bulk_detail-light-390.png), [dark 390](after/wb_bulk_detail-dark-390.png) | [Light 1440](after/wb_bulk_detail-light-1440.png), [dark 1440](after/wb_bulk_detail-dark-1440.png) |
| Настройки | [Light 390](after/worktree-api_settings-light-390.png), [dark 390](after/worktree-api_settings-dark-390.png) | [Light 1440](after/worktree-api_settings-light-1440.png), [dark 1440](after/worktree-api_settings-dark-1440.png) |
| Цены | [Light 390](after/wb_prices_change-light-390.png), [dark 390](after/wb_prices_change-dark-390.png) | [Light 1440](after/wb_prices_change-light-1440.png), [dark 1440](after/wb_prices_change-dark-1440.png) |

Разделы и режимы карточки проверены в двух темах матрицей listing, даже если выборочная галерея показывает часть комбинаций. Общая навигация: [light 390](after/worktree-light-390.png), [dark 390](after/worktree-dark-390.png), [light 1024](after/worktree-light-1024.png), [dark 1024](after/worktree-dark-1024.png).

## До / после

| Сценарий | До, baseline `ba63371` | После |
| --- | --- | --- |
| Analytics 1024/light | [Переполнение](before/analytics-ba63371-light-1024.png) | [Адаптивные KPI](after/analytics-worktree-light-1024.png) |
| Analytics 390/dark | [Переполнение](before/analytics-ba63371-dark-390.png) | [Узкий экран](after/analytics-worktree-dark-390.png) |
| Меню 390/light | [Исходное](before/ba63371-light-390.png) | [Группы](after/worktree-light-390.png) |
| Карточка | [Исходный обзор](before/listing-baseline-overview-before.png), [управление](before/listing-baseline-management-before.png) | [Общий header обзора](after/listing-after-matrix-390-light-overview.png), [управления](after/listing-after-matrix-390-light-management.png) |
| Черновик 390/light | [Исходный](before/journey-ba63371-draft_detail_vue-light-390.png) | [Текущий этап и следующий шаг](after/journey-worktree-draft_detail_vue-light-390.png) |
| Операция 390/light | [Исходная](before/wb_bulk_detail-light-390.png) | [Итог и причины по строкам](after/wb_bulk_detail-light-390.png) |

Before captures — доказательство исходного вида, не успешная after-приёмка. `workspace-before.json` содержит два synthetic Image Lab HTTP 400; в final fixture исправлен только seed фото, SSRF validator сохранён. Journey before report записывает страницы и изображения без geometry нового primitive. Размер и theme каждого изображения отражены в паспорте; разные viewport сравниваются только как разные композиции, не как равное измерение.
