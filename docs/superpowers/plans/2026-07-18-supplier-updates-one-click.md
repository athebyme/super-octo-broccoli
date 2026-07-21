# Обновления поставщика в один клик Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Продавец видит обновления общих карточек прямо в «Моих товарах» и в 2 клика доводит их до опубликованных WB-карточек. Дизайн-направление утверждено пользователем в сессии («минимум действий, просто и понятно»).

**Architecture:** Только существующие механизмы: сравнение `SupplierProduct.content_revision` vs `ImportedProduct.supplier_content_revision`, существующий `SupplierService.update_seller_products`, существующий экран `products_enrich_bulk`. Новое: индикация в «Моих товарах», тонкий endpoint «обновить из каталога» с CTA-цепочкой, preset характеристик на bulk-экране, авто-уведомление в scheduler.

**Tech Stack:** Flask/Jinja/Alpine, APScheduler (product_sync_scheduler), pytest.

## Global Constraints

- НЕ коммитить в git. Никаких новых инвариантов: локальное обновление не публикует; WB write остаётся отдельным подтверждаемым экраном enrich-bulk.
- Default экрана enrich-bulk (photos=true, остальное false) НЕ меняется; preset применяется только при явном переходе из CTA.
- Tenant scope: все выборки `id + seller_id`; ids — bounded positive int ≤ 200.
- AGENTS.md обновить в том же изменении.

### Task 1: Индикация в «Моих товарах»

**Files:** Modify `routes/suppliers.py` (seller_my_products), `templates/seller_my_products.html`.

- [ ] Route: outerjoin `SupplierProduct`, per-row флаг `has_supplier_update` (sp.content_revision > ip.supplier_content_revision); отдельный bounded COUNT `supplier_updates_count` по всему seller; фильтр `?updates=1` (join + условие).
- [ ] Template: чип «Обновления поставщика · N» рядом с фильтрами (виден при N>0, ведёт на `?updates=1`); жёлтый бейдж «Обновление» на строках; в панели массовых действий кнопка «Обновить из каталога» (форма POST на новый endpoint, selected imported ids).

### Task 2: Endpoint refresh-from-supplier + CTA

**Files:** Modify `routes/suppliers.py`, `templates/seller_my_products.html`; Test `tests/test_supplier_updates_one_click.py`.

- [ ] `POST /my-products/refresh-from-supplier`: `selected_ids` → validate (≤200, positive int, exact seller-owned); map → supplier_product_ids (строки без связи считаются skipped); вызвать `SupplierService.update_seller_products(seller.id, supplier_product_ids)`; посчитать среди выбранных опубликованные (import_status='imported' и product_id); их product_ids положить в `session['supplier_update_wb_ids']` (bounded 200); redirect `/my-products?refreshed=<n>&wb_ready=<m>`.
- [ ] Баннер в шаблоне при `wb_ready>0`: «Обновлено N карточек, M опубликованы на WB» + кнопка «Отправить характеристики на WB» — строит POST-форму на `products_enrich_bulk` с `selected_ids` из session и `preset=characteristics`.
- [ ] Тесты: валидация ids (bool/float/чужой seller → отказ), успешный refresh обновляет imported и кладёт session, счётчики верные.

### Task 3: Preset на bulk-экране

**Files:** Modify `routes/enrichment.py` (products_enrich_bulk), `templates/products_enrich_bulk.html`.

- [ ] Route: читать `request.form.get('preset')`; в шаблон передать `preset` (только известное значение 'characteristics', иначе None).
- [ ] Шаблон: при `preset == 'characteristics'` Alpine fields инициализируются `photos:false, characteristics:true, dimensions:true` (категория/бренд/тексты не трогаем); без пресета — прежний дефолт.
- [ ] Тест: route передаёт preset только из allowlist.

### Task 4: Авто-уведомление

**Files:** Modify `services/product_sync_scheduler.py`; Test в том же файле тестов.

- [ ] Job `notify_supplier_updates` каждые 6 часов: для sellers с imported-строками посчитать updates-count (join, тот же предикат); при count>0 и отсутствии Notification с тем же title за 24h — `create_notification(category='info', link='/my-products?updates=1')` с числом карточек.
- [ ] Тест: дедуп 24h, нет уведомления при count=0, tenant scope.

### Task 5: Документация и деплой

- [ ] AGENTS.md: раздел про «Обновить карточки» дополнить: индикация и refresh доступны из «Моих товаров», CTA ведёт на существующий подтверждаемый enrich-bulk с preset (default экрана не изменён), периодическое уведомление.
- [ ] py_compile, git diff --check, тесты; rebuild+restart после завершения активного run-а по презервативам; smoke в проде.
