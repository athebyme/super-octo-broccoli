# Рекомендации по оптимизации массовых операций

## 🐛 Исправлена критическая ошибка

**Проблема:** При массовом обновлении характеристик появлялась ошибка "Укажите ID характеристики и новое значение"

**Причина:** Несоответствие имён полей между HTML формой и серверным кодом:
- HTML форма отправляла: `name="value"`
- Серверный код искал: `value_update` и `value_add` ❌

**Решение:** Исправлено в `seller_platform.py:2441,2544` - теперь используется правильное поле `value` ✅

---

## ⚠️ Текущие проблемы производительности

### Проблема #1: Отсутствие батчинга

**Текущая реализация** (`seller_platform.py:2306-2543`):
```python
for product in products:  # ❌ ПЛОХО!
    client.update_card(product.nm_id, {'brand': new_brand})
```

**Что не так:**
- ❌ Каждый товар = отдельный HTTP запрос к WB API
- ❌ При 1000 товарах = 1000 запросов
- ❌ Упираемся в rate limit (100 req/min)
- ❌ Очень медленно (~1-2 сек на товар)

---

## 📊 Ограничения WB API (из 02-products.yaml)

```yaml
Эндпоинт: POST /content/v2/cards/update
Максимум карточек: 3000 в одном запросе
Максимальный размер: 10 МБ
Rate limits: ~100 запросов в минуту
```

**WB API поддерживает массовое обновление!** Просто нужно использовать это правильно.

---

## ✅ Рекомендуемое решение: Батчинг

### Шаг 1: Добавить метод `update_cards_batch()` в `wb_api_client.py`

```python
def update_cards_batch(
    self,
    cards: List[Dict[str, Any]],
    log_to_db: bool = False,
    seller_id: int = None,
    validate: bool = True
) -> Dict[str, Any]:
    """
    Обновить несколько карточек одним запросом (Content API v2)

    Args:
        cards: Список карточек для обновления
               Каждая карточка должна содержать:
               - nmID: обязательно
               - vendorCode: обязательно
               - sizes: обязательно (массив)
               - другие поля опционально
        log_to_db: Логировать запрос в БД
        seller_id: ID продавца для логирования
        validate: Валидировать данные перед отправкой

    Returns:
        Результат обновления

    Note:
        Максимум 3000 карточек за раз
        Максимальный размер запроса 10 МБ
    """
    from wb_validators import validate_and_log_errors

    if len(cards) > 3000:
        raise WBAPIException(
            f"Слишком много карточек ({len(cards)}). "
            f"Максимум 3000 за запрос. Используйте chunking."
        )

    # Валидация
    if validate:
        for i, card in enumerate(cards):
            if not validate_and_log_errors(card, operation="update"):
                raise WBAPIException(f"Validation failed for card #{i} (nmID={card.get('nmID')})")

    # Проверка размера запроса
    import json
    import sys
    size_bytes = sys.getsizeof(json.dumps(cards))
    size_mb = size_bytes / 1024 / 1024

    if size_mb > 10:
        raise WBAPIException(
            f"Размер запроса слишком большой ({size_mb:.2f} МБ). "
            f"Максимум 10 МБ. Уменьшите размер батча."
        )

    logger.info(f"📤 Batch update: {len(cards)} cards, size: {size_mb:.2f} МБ")

    endpoint = "/content/v2/cards/update"

    try:
        response = self._make_request(
            'POST', 'content', endpoint,
            log_to_db=log_to_db,
            seller_id=seller_id,
            json=cards  # Отправляем массив карточек
        )
        result = response.json()
        logger.info(f"✅ Batch update result: {result}")
        return result
    except Exception as e:
        logger.error(f"❌ Batch update failed: {str(e)}")
        raise
```

### Шаг 2: Добавить функцию чанкинга

```python
def chunk_list(items: List, chunk_size: int) -> List[List]:
    """
    Разбить список на чанки (батчи)

    Args:
        items: Список элементов
        chunk_size: Размер чанка

    Returns:
        Список чанков

    Example:
        >>> chunk_list([1,2,3,4,5], 2)
        [[1,2], [3,4], [5]]
    """
    chunks = []
    for i in range(0, len(items), chunk_size):
        chunks.append(items[i:i + chunk_size])
    return chunks
```

### Шаг 3: Переписать массовое обновление в `seller_platform.py`

```python
# Пример для update_brand
if operation == 'update_brand':
    new_brand = operation_value
    if not new_brand:
        flash('Укажите новый бренд', 'warning')
        return ...

    # Подготавливаем данные для батч-обновления
    cards_to_update = []

    for product in products:
        # Получаем полную карточку
        full_card = client.get_card_by_nm_id(
            product.nm_id,
            log_to_db=False,  # Не логируем каждый GET
            seller_id=current_user.seller.id
        )

        if not full_card:
            errors.append(f"Товар {product.vendor_code}: карточка не найдена")
            error_count += 1
            continue

        # Обновляем бренд
        full_card['brand'] = new_brand

        # Очищаем нередактируемые поля
        from wb_validators import prepare_card_for_update
        card_ready = prepare_card_for_update(full_card, {})

        cards_to_update.append(card_ready)

    # Разбиваем на батчи по 100 карточек
    # (можно больше, но безопаснее меньше)
    batches = chunk_list(cards_to_update, chunk_size=100)

    app.logger.info(f"📦 Split into {len(batches)} batches")

    # Обновляем батчами
    for batch_num, batch in enumerate(batches, 1):
        try:
            app.logger.info(f"📤 Batch {batch_num}/{len(batches)}: {len(batch)} cards")

            result = client.update_cards_batch(
                batch,
                log_to_db=True,  # Логируем только батч-запросы
                seller_id=current_user.seller.id
            )

            # Обновляем БД
            for card in batch:
                product = Product.query.filter_by(nm_id=card['nmID']).first()
                if product:
                    product.brand = new_brand
                    product.last_sync = datetime.utcnow()

                    # История
                    snapshot_before = _create_product_snapshot(product)
                    snapshot_after = snapshot_before.copy()
                    snapshot_after['brand'] = new_brand

                    card_history = CardEditHistory(
                        product_id=product.id,
                        seller_id=current_user.seller.id,
                        bulk_edit_id=bulk_operation.id,
                        action='update',
                        changed_fields=['brand'],
                        snapshot_before=snapshot_before,
                        snapshot_after=snapshot_after,
                        wb_synced=True,
                        wb_sync_status='success'
                    )
                    db.session.add(card_history)

            success_count += len(batch)
            db.session.commit()

            app.logger.info(f"✅ Batch {batch_num}/{len(batches)} completed")

        except Exception as e:
            error_count += len(batch)
            error_msg = f"Batch {batch_num}: {str(e)}"
            errors.append(error_msg)
            app.logger.error(f"❌ {error_msg}")
            continue  # Продолжаем со следующим батчем
```

---

## 📈 Прирост производительности

### До оптимизации:
- 1000 товаров × 2 сек = **33 минуты**
- 100 запросов / минуту → Rate limit каждые 100 товаров

### После оптимизации:
- 1000 товаров / 100 = 10 батчей
- 10 батчей × 2 сек = **20 секунд**
- **Ускорение в ~100 раз!** 🚀

---

## 🎯 Рекомендуемые параметры

```python
# Batch size
BATCH_SIZE = 100  # Безопасный размер для большинства случаев
# Можно увеличить до 500 если карточки небольшие
# Максимум 3000, но не рекомендуется

# Max request size
MAX_REQUEST_SIZE_MB = 9.5  # С запасом от лимита 10 МБ

# Проверка размера перед отправкой
if calculate_size(batch) > MAX_REQUEST_SIZE_MB:
    # Уменьшить batch или убрать тяжелые поля
    pass
```

---

## 🔍 Что делать при ошибках

### Если батч не обновился:

1. **Проверить логи** (`/api-logs`):
   - Какой именно запрос упал
   - Ответ от WB API
   - Код ошибки

2. **Проверить размер запроса**:
   ```python
   import sys, json
   size_mb = sys.getsizeof(json.dumps(batch)) / 1024 / 1024
   print(f"Batch size: {size_mb:.2f} MB")
   ```

3. **Попробовать с меньшим батчем**:
   ```python
   chunk_size = 50  # Вместо 100
   ```

4. **Проверить список ошибок WB**:
   ```python
   errors = client.get_cards_errors_list()
   ```

---

## 📝 TODO: Реализация

- [ ] Добавить `update_cards_batch()` в `wb_api_client.py`
- [ ] Добавить `chunk_list()` в `wb_api_client.py`
- [ ] Переписать `update_brand` с батчингом
- [ ] Переписать `append_description` с батчингом
- [ ] Переписать `replace_description` с батчингом
- [ ] Переписать `update_characteristic` с батчингом
- [ ] Переписать `add_characteristic` с батчингом
- [ ] Добавить progress bar на фронтенде
- [ ] Добавить возможность отмены операции
- [ ] Добавить retry logic для failed batches

---

## 🚨 Важно!

1. **Тестируй на малых объёмах** (5-10 товаров) перед массовыми операциями
2. **Проверяй логи** после каждой операции
3. **Делай бэкапы БД** перед большими обновлениями
4. **Мониторь rate limits** - WB может заблокировать при превышении

---

**Версия документа:** 1.0
**Дата:** 2025-11-25
**Автор:** Claude Code Assistant
