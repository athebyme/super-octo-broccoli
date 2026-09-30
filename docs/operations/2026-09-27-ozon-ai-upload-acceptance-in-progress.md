# Ozon AI / загрузка карточек — приёмка в работе, 27.09.2026

Это промежуточный отчёт о коде и изолированном стенде. Production остаётся на выпуске `sha256:d7d3f54dbea423867828b9ea714e777929d4893fb59521e841bba25b08d80fda`. Реальный Flash-пилот и новая публикация карточки ещё не выполнены.

## Реализовано

- Отдельная seller-owned очередь AI-предложений, exact source/reference/version seals, ручной apply/reject с атомарным audit и восстановлением по ключу запроса.
- Native `deepseek-flash`, общий admin/seller ledger 3 global / 2 seller, чанки до 6 карточек, явные 429/cancel/unknown состояния. Physical permit приобретается до durable reservation; занятый локальный слот не расходует попытку.
- Исходные поля отделены от старых AI-результатов; literal evidence не означает семантическую истину. Явно именованные характеристики связываются с назначением поля; ссылки, контакты и явные credentials удаляются из передаваемого текста. Составные/регулируемые/измерительные поля первой seller дорожкой не генерируются.
- Vue выбор партии, прогресс, подсветка proposed/accepted и просмотр подтверждающего текста. Публикация готовых карточек независима от AI.
- Старые start POST административного парсера закрыты actionable 409; UI переносит точные ID/фильтры на Flash review. История старых задач сохранена.
- Quota 429/transient error откладывает queued publication без записи; due очередь обслуживает и ожидающие, и новые карточки, используя свободные слоты.
- Create import task требует полного exact provider readback перед записью наблюдённых полей listing. `imported` сам по себе не доказывает модерацию/видимость.
- Связанные карточки не допускают неподдержанную смену категории. Legacy mismatch можно исправить только до свежей exact observed категории через impact review.

## Зафиксированные проверки

- Root integration: **157 tests / 30 subtests passed**, 59.99 s. Frozen manifest `/tmp/ozon-ai-integrated-root-v1-q_k_h2ji/manifest.json`, SHA256 `9c0184099868719cee298b8ac64262dae025e9d58e6e4ec70a24d199cdd25389`. В этом прогоне `marketplace_drafts.py` взят из принятого root-v2 до независимого category guard; это не полная приёмка final tree.
- Publication focused: **68 / 5**, `/tmp/ozon-publication-readback-v3/`. После него root дополнительно исправил заполнение свободных queue slots; нужен общий повтор на final bytes.
- Linked category: исправленный focused **9 / 9**, `/tmp/ozon-linked-category-ci-fixed-gjqxdc2a/`; прежний широкий прогон 98 tests / 67 subtests прошёл, три ошибки новой fixture SECRET_KEY исправлены. Полный final-tree прогон ещё нужен.
- Vue upload: **8 checks / 48 layouts**, `/tmp/ozon-upload-ui-browser-v11-pYUqLi/`.
- Vue AI: **5 / 40**, `/tmp/ozon-ai-ui-browser-v9-_ttsy2fn/`; после него backend strengthened и synthetic labels исправлены на обычные цвет/фактуру. Повтор входит в final release runner.
- Первый полный frozen прогон `3954285e1c3c16cea0a91245c61ba2e50cc7b8b286be04f453404a9d92adce9f`: **1 911 tests / 482 subtests passed, 4 failures**, 619.68 s. Три устаревших upload test expectations и несовместимость quarantine proof с новым create full readback требуют исправления. Приёмка не пройдена; артефакты `/tmp/ozon-ai-release-frozen-qryzkt5t/artifacts/` сохранены.
- Все эти проверки использовали синтетические данные, network-none контейнеры; live API/model success ими не доказан.

## Реальный каталог и пилот

Read-only audit: 21 948 supplier rows с source snapshot; 11 685 защищены gate существующего marketplace content; 10 263 не имеют такого контента. Из них 5 093 имеют свежую enabled WB leaf/category schema (5 027 / 66 по двум поставщикам). Это **не** показатель заполненности Ozon и **не** результат AI.

Подготовлен private exact manifest: pilot12 (6+6 товаров, 10 различных предметов WB), затем disjoint wave60 и wave200. Перед каждой волной повторяется source/content revision/category schema проверка. Старый `not_parsed` не является текущим resumable Flash backlog: Flash не подделывает legacy `ai_parsed_data_json`. Pilot review 100%; существующие marketplace поля не перезаписываются.

## Открытые ворота выпуска

1. Общий frozen backend/migration/browser runner на final tree и проверка артефактов root.
2. Verified backup завершён 26.09 в 23:11 UTC: 13 675 048 960 байт восстановлены с проверкой целостности и SHA256, сохранены две локальные копии. Additive migration rehearsal и deployment/production smoke ещё предстоят.
3. Реальный Flash pilot12 с actual usage, latency, evidence/native validation review. Только после его результата расширять до 60/200.
4. Реальная reviewed card publication требует отдельно подготовленной конкретной карточки. Наличие key grants не доказывает успешный import/модерацию.
5. **Media readback:** исходящий create использует signed Seller Hub URL, canonical full fingerprint сравнивает URL буквально. Возможная замена Ozon на CDN URL пока не подтверждена конкретным create receipt; synthetic adapter возвращает те же URL. Этот риск нельзя объявлять исправленным или обходить игнорированием media. Требуется provider observation и явный контракт доказательства gallery outcome, сохраняющий observed snapshot отдельно от outgoing payload. Update/rollback equality не ослаблять. [Официальное сообщение о версии picture API](https://t.me/OzonSellerAPI/660) само по себе не описывает преобразование URL.

Покупательская цена/скидка Ozon остаются отдельным незавершённым контрактом, не заполняются seller-price fallback.
