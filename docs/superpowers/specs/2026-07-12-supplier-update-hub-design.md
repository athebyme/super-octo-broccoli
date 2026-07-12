# Обновления от поставщиков (Supplier Update Hub) — дизайн

Дата: 2026-07-12. Статус: одобрено (автономный режим — решения зафиксированы после исследования кодовой базы).

## Проблема

Пользователь не видит, какие карточки на маркетплейсе принадлежат какому поставщику,
и не имеет быстрого пути «у поставщика появились новые фото → дозагрузить их на WB».
Конкретный триггер: после фикса парсера у Андрея (supplier_id=2) в каталоге стало до 28 фото
на товар, а опубликованные карточки несут только 3.

## Факты кодовой базы (на которых стоит дизайн)

- `Product` не имеет FK на поставщика. Связь: `Product ← ImportedProduct(product_id,
  supplier_product_id, supplier_id, seller_id)`. В проде связано: sexoptovik — 13 558 карточек,
  andrey — 574.
- `Product.photos_json` для карточек, синхронизированных с WB, содержит **int-индексы**
  `[1..N]` — суррогат «сколько фото на WB» (`seller_platform.py` WB-sync path). Реальные URL
  текущих фото карточки в БД недоступны. Значит «дозагрузка» = **пересборка полного набора**
  из источника-поставщика, а не append.
- Источник фото: `SupplierProduct.photo_urls_json` (после ре-импорта — до 28 URL).
  `ImportedProduct.photo_urls` — устаревающая копия на момент импорта; НЕ использовать как источник.
- Публичные URL для WB: `/photos/public/<sp_id>/<idx>.jpg?sig=HMAC`
  (`routes/photos.py:generate_public_photo_url`) — отдаёт фото из `photo_urls_json` по индексу
  с ленивым скачиванием и кэшем. Работает для формата `{'original': url}`.
- Канонический движок применения: `apply_card_updates(product, {'photos': [urls]}, seller,
  wb_client, source=...)` (`services/card_improver.py`) → `upload_photos_by_url` → WB
  `content/v3/media/save` — **заменяет весь набор**, порядок сохраняется, кап 30
  (`MAX_WB_MEDIA_FILES`). Пишет `CardEditHistory` со снапшотом и перещитывает Quality Score.
- Стандартные фото продавца: `compose_card_photo_urls(own, media, seller_id, min_photos)`
  (`services/standard_photos.py`) — пины first/last, дедуп, кап 30; возвращает `[]` если
  нечего добавлять.
- Массовые операции с прогрессом: `BackgroundJob` + `threading.Thread` + поллинг статуса
  (паттерн `bulk_wb_import`, `routes/suppliers.py:2026+`). Rate limit WB — 100 req/мин
  (`RateLimiter` в `wb_api_client`), 574 карточки ≈ 6 минут → синхронный запрос невозможен,
  нужен фоновый job.
- UI-конвенции: Tailwind + Alpine + `sh-*` классы, `flash()`/`showToast`, sidebar-группы
  в `base.html`, bulk-паттерн: master-checkbox + `.product-checkbox` + sticky action bar
  (`products.html`), серверные фильтры GET-параметрами.

## Рассмотренные подходы

1. **Только фильтр по поставщику на /products + bulk-действие.** Дёшево, но generic-список
   не рассказывает «что устарело», а синхронное bulk-применение не масштабируется.
2. **Отдельный хаб «Обновление карточек» + интеграция в /products.** — ВЫБРАНО.
   Ясная ментальная модель («раздел, где видно и обновляется всё по поставщику»),
   расширяемо на другие типы обновлений, реиспользует cockpit-паттерн card-quality.
3. **Встроить в /card-quality.** Смешивает «качество» и «синхронизацию с поставщиком»,
   страница уже перегружена.

## Дизайн (v1)

### Новый модуль: `routes/supplier_updates.py` + `templates/supplier_updates.html`

Регистрация `register_supplier_updates_routes(app)` в `seller_platform.py`; сайдбар:
группа «Поставщики» → «Обновление карточек» (endpoint `supplier_updates_page`).
Все роуты seller-scoped (как card_quality: current_user → seller, продукты только своего seller).

### Данные страницы

Запрос: `Product JOIN ImportedProduct ip ON ip.product_id = Product.id
JOIN SupplierProduct sp ON sp.id = ip.supplier_product_id`
`WHERE Product.seller_id = :seller AND Product.is_active AND Product.nm_id IS NOT NULL`
(+ `ip.supplier_id = :supplier` при выбранном поставщике).

Для каждой карточки:
- `wb_count = len(json.loads(photos_json or '[]'))` — сколько фото на WB сейчас
  (ints или urls — только количество);
- `supplier_count = len(sp.get_photos())`;
- `delta = supplier_count - wb_count` (может быть ≤ 0).

Фильтры (GET): `supplier_id` (селектор с количеством карточек и суммой «есть новые фото»),
`only_new=1` (по умолчанию) — только `supplier_count > wb_count`, `search`
(vendor_code/title/nm_id), пагинация `page`/`per_page` (кап 200 как в /products).

UI-строка: миниатюра (`wb_photo_url(nm_id, 1)`), название + артикул + nm_id,
бейдж поставщика, «Фото: 3 на WB → 17 у поставщика (+14)», checkbox.
Sticky bar: «Выбрано M», кнопка «Дозагрузить фото», «Выбрать всё по фильтру»
(отправляет фильтр, а не 10k id).

### Пересборка набора фото (семантика «дозагрузить»)

Для карточки: `supplier_urls = [generate_public_photo_url(sp.id, i) for i in range(supplier_count)]`,
затем `target = compose_card_photo_urls(supplier_urls, get_standard_media(seller_id, subject_id),
seller_id, min_photos)`; если композер вернул `[]` (нет стандартных пинов) → `target =
supplier_urls[:30]`. Применение: `apply_card_updates(product, {'photos': target}, ...,
source='supplier-updates')`.

Замена, а не append — осознанно: реальные URL текущих фото WB-карточек в БД недоступны
(int-индексы), а после загрузки WB перехостит фото на своём CDN, так что строковый дедуп
против «уже загруженных» невозможен в принципе. Каталог поставщика — источник истины для
дропшип-карточек; ручные фото — краевой случай, защищённый предпросмотром количества,
явным выбором карточек и снапшотом в `CardEditHistory`.

Скип-условия внутри job (считаются в `skipped`): нет `nm_id`, `supplier_count == 0`,
`target` пуст.

### Фоновое применение

- `POST /api/supplier-updates/photos/start` — JSON: `{product_ids: [...]}` либо
  `{select_all: true, supplier_id, only_new, search}` (сервер сам разворачивает фильтр в id).
  Валидация: id принадлежат текущему seller и связаны с поставщиком. Создаёт
  `BackgroundJob(job_type='supplier_photos_update')`, поток обрабатывает карточки
  последовательно через `apply_card_updates` (RateLimiter клиента сам душит до 100/мин),
  инкрементит `processed/succeeded/failed_count`, копит per-item ошибки в `progress_data`
  (первые 50), итог в `result_data`. По завершении — строка `BulkEditHistory`
  (operation_type='supplier_photos_update') для страницы «История операций» + `Notification`.
- `GET /api/supplier-updates/jobs/<job_uid>/status` — прогресс для поллинга (каждые 2с),
  прогресс-бар в sticky-баре страницы. Один активный job на seller (409 при попытке второго).
- Отмена: `POST /api/supplier-updates/jobs/<job_uid>/cancel` — флаг в job, поток проверяет
  между карточками (паттерн `cancel_bulk_job`).

### Интеграция в /products

- Фильтр «Поставщик» (dropdown, поставщики имеющие связанные карточки этого seller)
  → `products_list` получает outerjoin `ImportedProduct→Supplier` и параметр `supplier_id`.
- Колонка-бейдж поставщика в строке (пусто для несвязанных карточек).
- При активном фильтре — кнопка-ссылка «Обновить фото из каталога → » на
  `/supplier-updates?supplier_id=X`.

### Что НЕ входит в v1 (осознанно)

- Fuzzy-привязка несвязанных карточек (`find_supplier_data`) — отдельная задача-backfill.
- Другие типы обновлений (описание/цена/остатки) — хаб расширяем (колонки-дельты добавятся),
  но v1 — только фото. Точечная работа с одной карточкой — существующая страница
  «Обогатить» (`/products/<id>/enrich`), ссылка из строки хаба.
- Перцептивный дедуп фото — невозможен дёшево, не нужен при семантике полной пересборки.

## Тесты (stdlib unittest, как в tests/)

1. Хелпер выборки: связка+дельта, фильтр `only_new`, seller-изоляция.
2. Билдер target-набора: supplier+пины, `[]` от композера → fallback, кап 30, скипы.
3. Роут start: валидация чужих id, 409 при активном job, разворачивание select_all-фильтра.
4. Логика job-потока с фейковым wb_client: счётчики, skipped, ошибки, cancel.
5. Status route: прогресс и seller-изоляция.
