# Ozon: разделённые цены, 25.09.2026

## Состояние выпуска

Image `e94c3ca4ef95e3a2bdbfe141472b194af82af3dce2a8f779043209e1a0c1d6cb` запущен на production **24.09 в 21:44:21 UTC** (25.09, 00:44 МСК). Полные startup migrations завершены; через 491 секунду контейнер healthy. Production browser verification завершена в **21:54 UTC**. Предыдущий image `749a56fc6c350d203da5fb71c144c9b19a802de5c4b14bbede33ddda98171177` сохранён. Ключи, critical environment и commercial/publication flags не менялись; auto-publish остаётся выключенным.

## Изменение

В Vue-плитке, таблице, быстром просмотре, детальной карточке и раскрываемом контексте коммерческой формы используется общий блок цен. База до скидок, цена продавца и положительная цена его акции показаны раздельно. Нулевой `marketing_seller_price` больше не заменяет положительный seller `price`; исторические snapshots и write payload не переписываются. Общий процентный Ozon badge убран: разница старой и seller-цены не доказывает скидку площадки. Classic presenter следует тем же правилам. Контент-фабрика также не использует нулевую акцию как рекламу бесплатного товара и требует положительную наблюдённую сумму в RUB.

Покупательская цена и скидка Ozon пока явно неизвестны. Новый официальный `/v1/product/prices/details` реально проверен, но вернул 403 для RO-ключа и текущего seller-key, хотя метод присутствует в observed roles. Денежного ответа нет, причина ограничения не установлена. Платная подписка не подключалась. Это **открытая часть требования**, а не завершённая интеграция buyer price. [Контракт и evidence](../design/ozon-prices.md).

## Проверки до деплоя

- **U/API:** 1000 tests / 344 subtests, 186 прежних warnings, 347.84s. Focused: 85 / 9; финальные display tests: 32. Backend после полного regression не менялся.
- **S, цены:** 7 сценариев / 54 layout-theme cases; 0 JS/console/local HTTP/overflow errors, 0 real/synthetic provider writes. Проверены нулевая и положительная акция, отсутствующие base/seller/currency, unavailable snapshot, большие не-RUB суммы, несогласованная по величине база, все поверхности и группировка двух кабинетов.
- **S, коммерция:** 18 сценариев / 54 layout-theme cases; 0 JS errors, 2/2 фото, 8 synthetic writes и 0 real writes. Подтверждены существующие review/rounding/conflict/recovery/batch/stock guards.
- Изолированные контейнеры использовали реальное приложение и копию базы, `network=none`, синтетические внешние записи. Оба набора повторены на окончательном image после исправления отступа поиска в classic fallback.
- Полный migration bundle прошёл на свежей копии, repeat startup подтвердил schema/bundle. Последнее изменение только CSS; новый image независимо сверил тот же successful journal, без ручного изменения журнала и лишнего полного сканирования. Перед deployment проверены SHA-256 17 runtime UI/service файлов, отсутствие `.env`/`.env.autodeploy` в образе и неизменность critical environment.

При визуальной проверке исправлены три дефекта: desktop-подписи цен classic fallback были скрыты; absolute `sr-only` расширял viewport мобильной таблицы; более специфичный общий стиль формы перекрывал отступ поисковой иконки. Финальные screenshots просмотрены, фото/состояния без источника различены.

## Восстановление

Backup `backups/ozon-20260925-price-lanes-predeploy.sqlite.gz`, snapshot **21:20:26 UTC**: `quick_check=ok`, 13 602 459 648 bytes → gzip 1 778 918 749 bytes. SHA-256 исходной SQLite-копии: `01d401d292cc3a72abcb8dd3377c3f627909d5a14ec3086789ddb3fe3c05866f`. Полное распаковывание проверено по размеру и digest. Переиспользована только остановленная agent-owned QA DB; production и предыдущие архивы сохранены.

В этом выпуске **новых реальных price/stock/product writes нет**. Ранее разрешённый +25%/restore пилот не повторяется. Read-only проверка подтвердила прежние исходы: операция 7 uncertain, операция 8 succeeded, по одной попытке; price/old/min price точно равны восстановленному baseline. Первое сравнение в диагностическом harness обнаружило только различие JSON-типа `1059` и строки `"1059"` после catalog refresh; исправлено сравнение Decimal без округления и без изменения production rows. Независимый price-v5 read в **21:54:12 UTC** вновь подтвердил seller 1059, old 1462, promotion 0, min 0 и RUB.

Приватные browser screenshots/reports и диагностические метаданные не входят в репозиторий. Безопасный aggregate evidence сохранён в `output/ozon-ui/rehearsal-summary.json → price_lanes_release` после production verification.


## Итог на production

**22 страницы / 132 layout-theme cases + 12 видов коммерческой формы**, без route/JS/local HTTP/overflow errors и неожиданных browser mutations. Шесть категорий загрузились; **60/60 primary photos**, без pending/fallback. Base/seller/unknown buyer-discount совпадают в плитке, таблице, drawer, detail, classic и commercial context. Фактические мобильные light/dark price panels и desktop classic просмотрены. CSRF-protected read refresh подтвердил 2 склада и 2 stock observations; новых provider writes нет. Внешний HTTPS/TLS, Vue/catalog/editor/commercial и новые price assets — 200.

Scheduler жив и держит единственный exclusive lock. Последний автоматический catalog sweep №15 завершён в **21:09:44 UTC**, 8 719 товаров / 10 страниц / 0 warnings. Critical flags сохранены, auto-publish=0.

После фиксации evidence удалены только `seller_platform.db` и его WAL/SHM из остановленного agent-owned QA-каталога, 13 606 706 872 логических bytes. Production DB, проверенные gzip-архивы и browser reports сохранены; свободное место выросло с 7 253 528 576 до **20 860 239 872 bytes**. Для следующей репетиции нужна новая QA-копия либо восстановление архива: распакованной тестовой DB больше нет.
