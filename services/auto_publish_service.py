# -*- coding: utf-8 -*-
"""
Сервис автоматической публикации товаров на маркетплейсы.

Pipeline: select candidates → validate → upload card → upload photos → set prices → verify → finalize
Каждый шаг идемпотентен. Ошибки классифицируются на retryable/non-retryable.
Circuit breaker останавливает обработку при серии ошибок подряд.
"""
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import text

from models import (
    db, Seller, ImportedProduct, Product, Notification,
    AutoPublishSettings, AutoPublishRun, AutoPublishItem,
    PricingSettings, SellerSupplier,
)
from services.upload_readiness_validator import validate_product_upload_readiness
from services.wb_product_importer import WBProductImporter
from services.wb_api_client import WBAPIException, WBAuthException

logger = logging.getLogger(__name__)

# Ошибки WB, при которых есть смысл повторять
_RETRYABLE_PATTERNS = [
    'rate limit', '429', 'too many requests',
    '500', '502', '503', '504',
    'timeout', 'timed out', 'connection',
    'nmid не получен', 'не доступен',
    'temporarily unavailable', 'service unavailable',
]

# Ошибки WB, при которых повторять бесполезно (плохие данные)
_NON_RETRYABLE_PATTERNS = [
    'bad request', '400',
    'не зарегистрирован', 'не найден',
    'запрещён', 'prohibited', 'brand_prohibited',
    'не готов к импорту', 'не задан',
    'не настроен', 'api ключ',
]


FINAL_ITEM_STATUSES = ('completed', 'failed', 'skipped')


def _is_retryable_error(error_msg: str) -> bool:
    """Определяет, стоит ли повторять запрос при данной ошибке."""
    error_lower = error_msg.lower()
    for pattern in _NON_RETRYABLE_PATTERNS:
        if pattern in error_lower:
            return False
    for pattern in _RETRYABLE_PATTERNS:
        if pattern in error_lower:
            return True
    return False


class AutoPublishService:
    """
    Оркестратор автопубликации товаров на маркетплейс.

    Использует WBProductImporter для фактической загрузки,
    но управляет пошаговым трекингом, retry и circuit breaker.
    """

    def __init__(self, seller: Seller, settings: AutoPublishSettings):
        self.seller = seller
        self.settings = settings
        self.logger = logging.getLogger(f'auto_publish.seller_{seller.id}')

    # ================================================================
    # PUBLIC: Основной запуск
    # ================================================================

    def execute_run(self, triggered_by: str = 'scheduler') -> Optional[AutoPublishRun]:
        """
        Выполнить один цикл автопубликации.
        Возвращает AutoPublishRun или None если нечего обрабатывать.
        """
        # Атомарный захват лока через UPDATE WHERE run_lock_token IS NULL.
        # SQLite сериализует write-транзакции — два процесса/потока
        # не смогут одновременно установить токен.
        lock_token = str(uuid.uuid4())
        if not self._try_acquire_lock(lock_token):
            self.logger.info("Другой запуск уже выполняется, пропускаем")
            return None

        run = None
        try:
            # Daily limit reset
            self._reset_daily_counter_if_needed()

            # Проверка дневного лимита
            if self.settings.daily_published_count >= self.settings.max_daily_publishes:
                self.logger.info(
                    f"Дневной лимит достигнут ({self.settings.daily_published_count}/{self.settings.max_daily_publishes})"
                )
                self._schedule_next_run()
                return None

            # Создаём запуск
            run = AutoPublishRun(
                seller_id=self.seller.id,
                run_uid=str(uuid.uuid4()),
                status='running',
                triggered_by=triggered_by,
                started_at=datetime.utcnow(),
            )
            db.session.add(run)
            db.session.commit()

            try:
                # 1. Отбираем кандидатов
                candidates = self._select_candidates(run)
                if not candidates:
                    self._complete_empty_run(run)
                    self.logger.info("Нет кандидатов для публикации")
                    return run

                run.total_candidates = len(candidates)
                db.session.commit()

                # 2. Обрабатываем каждый товар
                consecutive_failures = 0
                importer = WBProductImporter(self.seller)

                for item in candidates:
                    if self._is_run_cancelled(run.id):
                        self._mark_pending_items_skipped(run, 'Отменено пользователем')
                        break

                    # Проверяем circuit breaker
                    if consecutive_failures >= self.settings.failure_threshold:
                        self._trigger_circuit_breaker(run, consecutive_failures)
                        # Помечаем оставшиеся как skipped
                        self._mark_pending_items_skipped(run, 'Авто-пауза после серии ошибок')
                        break

                    # Проверяем дневной лимит
                    if self.settings.daily_published_count >= self.settings.max_daily_publishes:
                        self.logger.warning("Дневной лимит достигнут в процессе запуска")
                        self._mark_pending_items_skipped(run, 'Дневной лимит публикаций достигнут')
                        break

                    success = self._process_item(item, importer)
                    if success:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1

                # 3. Сверка с WB: карточка принимается асинхронно (202),
                # ошибки обработки видны только в cards/error/list
                self._check_wb_processing_errors(run)

                # 4. Финализация запуска
                self._finalize_run(run)
                return run

            except Exception as e:
                # Откатываем всё незакоммиченное в сессии перед записью статуса fail.
                db.session.rollback()
                self.logger.error(f"Критическая ошибка запуска: {e}", exc_info=True)
                try:
                    run.status = 'failed'
                    run.error_summary = json.dumps({'critical_error': str(e)[:500]}, ensure_ascii=False)
                    run.completed_at = datetime.utcnow()
                    if run.started_at:
                        run.duration_seconds = (run.completed_at - run.started_at).total_seconds()
                    self._unlock_publishing_products(run)
                    db.session.commit()
                except Exception as inner:
                    self.logger.error(f"Не удалось записать failure-статус: {inner}", exc_info=True)
                    db.session.rollback()
                return run
        finally:
            # Лок освобождается всегда — даже при необработанных исключениях
            # выше try.
            self._release_lock(lock_token)

    # ================================================================
    # PIPELINE STEPS
    # ================================================================

    def _select_candidates(self, run: AutoPublishRun) -> List[AutoPublishItem]:
        """Отбирает и блокирует ImportedProduct'ы для обработки."""
        remaining_daily = self.settings.max_daily_publishes - self.settings.daily_published_count
        limit = min(self.settings.batch_size, remaining_daily)

        if limit <= 0:
            return []

        # Self-heal: прайс поставщика мог прийти позже импорта карточки —
        # подтягиваем цену из SupplierProduct до отбора, чтобы товар не
        # выпадал из выборки из-за устаревшего NULL.
        self._backfill_supplier_prices()

        # Базовый запрос: validated товары этого продавца.
        # Товары с заведомо непубликуемыми данными (нет цены, фото или
        # категории WB) отсекаются на уровне SQL — они не должны занимать
        # слоты батча и порождать бесконечные skip/retry. Полная валидация
        # в _validate_product остаётся последним рубежом.
        query = ImportedProduct.query.filter(
            ImportedProduct.seller_id == self.seller.id,
            ImportedProduct.import_status == 'validated',
            ImportedProduct.supplier_price.isnot(None),
            ImportedProduct.supplier_price > 0,
            ImportedProduct.wb_subject_id.isnot(None),
            ImportedProduct.photo_urls.isnot(None),
            ImportedProduct.photo_urls != '',
            ImportedProduct.photo_urls != '[]',
        )

        # Фильтр по поставщикам
        supplier_ids = self.settings.get_supplier_ids()
        if supplier_ids:
            query = query.filter(ImportedProduct.supplier_id.in_(supplier_ids))

        # Исключаем товары, у которых исчерпаны retry
        if self.settings.max_retries_per_product > 0:
            # Подзапрос: ID товаров с исчерпанными retry из прошлых запусков
            exhausted_subq = db.session.query(AutoPublishItem.imported_product_id).filter(
                AutoPublishItem.seller_id == self.seller.id,
                AutoPublishItem.status == 'failed',
                AutoPublishItem.retry_count >= self.settings.max_retries_per_product,
            ).subquery()
            query = query.filter(~ImportedProduct.id.in_(db.session.query(exhausted_subq)))

        # Backoff: исключаем товары, для которых запланирован retry в будущем.
        # next_retry_at ставится в _process_item при retryable-ошибке и в
        # _validate_product при провале валидации (validation cooldown).
        now = datetime.utcnow()
        pending_retry_subq = db.session.query(AutoPublishItem.imported_product_id).filter(
            AutoPublishItem.seller_id == self.seller.id,
            AutoPublishItem.status.in_(('failed', 'skipped')),
            AutoPublishItem.next_retry_at.isnot(None),
            AutoPublishItem.next_retry_at > now,
        ).subquery()
        query = query.filter(~ImportedProduct.id.in_(db.session.query(pending_retry_subq)))

        candidates = query.order_by(ImportedProduct.id.asc()).limit(limit).all()

        if not candidates:
            return []

        items = []
        for product in candidates:
            # Атомарно блокируем товар
            product.import_status = 'publishing'
            previous_retry_count = self._get_previous_retry_count(product.id)

            item = AutoPublishItem(
                run_id=run.id,
                imported_product_id=product.id,
                seller_id=self.seller.id,
                step='queued',
                status='pending',
                retry_count=previous_retry_count,
            )
            db.session.add(item)
            items.append(item)

        db.session.commit()
        self.logger.info(f"Отобрано {len(items)} кандидатов для публикации")
        return items

    def _backfill_supplier_prices(self) -> int:
        """Подтягивает закупочную цену из каталога поставщика для validated-товаров без цены.

        Прайс-файл поставщика приходит отдельно от каталога: карточка могла
        импортироваться до появления цены и остаться с supplier_price = NULL.
        Один bulk-запрос на запуск; розничная цена пересчитается в
        _validate_product по PricingSettings.
        """
        from models import SupplierProduct
        try:
            rows = (
                db.session.query(ImportedProduct, SupplierProduct)
                .join(SupplierProduct, SupplierProduct.id == ImportedProduct.supplier_product_id)
                .filter(
                    ImportedProduct.seller_id == self.seller.id,
                    ImportedProduct.import_status == 'validated',
                    db.or_(
                        ImportedProduct.supplier_price.is_(None),
                        ImportedProduct.supplier_price <= 0,
                    ),
                    SupplierProduct.supplier_price.isnot(None),
                    SupplierProduct.supplier_price > 0,
                )
                .all()
            )
            if not rows:
                return 0
            for imported, sp in rows:
                imported.supplier_price = sp.supplier_price
                if imported.supplier_quantity is None and sp.supplier_quantity is not None:
                    imported.supplier_quantity = sp.supplier_quantity
                # Сбрасываем устаревший расчёт — цена изменилась с NULL
                imported.calculated_price = None
            db.session.commit()
            self.logger.info(f"Backfill закупочных цен из каталога поставщика: {len(rows)} товаров")
            return len(rows)
        except Exception as e:
            self.logger.warning(f"Backfill цен не удался: {e}")
            db.session.rollback()
            return 0

    def _get_previous_retry_count(self, imported_product_id: int) -> int:
        """Возвращает retry_count последней failed/skipped попытки товара."""
        previous = AutoPublishItem.query.filter(
            AutoPublishItem.seller_id == self.seller.id,
            AutoPublishItem.imported_product_id == imported_product_id,
            AutoPublishItem.status.in_(('failed', 'skipped')),
        ).order_by(AutoPublishItem.id.desc()).first()
        if not previous:
            return 0
        try:
            return max(0, int(previous.retry_count or 0))
        except (TypeError, ValueError):
            return 0

    def _process_item(self, item: AutoPublishItem, importer: WBProductImporter) -> bool:
        """Обрабатывает один товар через весь pipeline. Возвращает True при успехе."""
        item.status = 'processing'
        item.started_at = datetime.utcnow()
        db.session.commit()

        imported_product = ImportedProduct.query.get(item.imported_product_id)
        if not imported_product:
            item.status = 'failed'
            item.error_message = 'ImportedProduct не найден'
            item.error_step = 'queued'
            item.completed_at = datetime.utcnow()
            run = self._get_run(item.run_id)
            if run:
                run.total_failed += 1
            db.session.commit()
            return False

        # Шаг 1: Валидация
        item.step = 'validating'
        db.session.commit()

        if not self._validate_product(item, imported_product):
            item.status = 'skipped'
            item.step = 'skipped'
            item.completed_at = datetime.utcnow()
            # Cooldown c эскалацией: повторный skip той же карточки означает,
            # что проблему ещё не починили руками — каждая следующая попытка
            # отодвигается вдвое (cap 24 часа). Без эскалации карточки с
            # постоянно битыми данными возвращались в выборку каждые 15 минут
            # и вытесняли публикуемые товары из батча.
            item.next_retry_at = datetime.utcnow() + timedelta(
                minutes=self._validation_skip_cooldown_minutes(item)
            )
            imported_product.import_status = 'validated'  # Разблокируем
            self._get_run(item.run_id).total_skipped += 1
            db.session.commit()
            return True  # skipped != failure для circuit breaker

        item.step = 'uploading_card'
        run = self._get_run(item.run_id)
        run.total_validated += 1
        db.session.commit()

        # Шаг 2: Загрузка карточки (через WBProductImporter)
        # Временно ставим import_status='validated' чтобы WBProductImporter не отклонил
        imported_product.import_status = 'validated'
        db.session.commit()

        try:
            success, error_msg, created_product = importer.import_product_to_wb(imported_product)
        except Exception as e:
            success = False
            error_msg = str(e)
            created_product = None

        if success:
            # WBProductImporter сам ставит import_status='imported' и создаёт Product
            if created_product:
                item.wb_nm_id = created_product.nm_id
                item.product_id = created_product.id
            else:
                item.add_error(
                    'uploading_card',
                    'Карточка принята WB, но nmID пока не получен',
                    retryable=False
                )

            # Шаг 3: post-upload verify — убедиться, что карточка реально
            # появилась на WB, а не получили ложный success. WB API
            # eventually-consistent: если verify не нашёл — это не failure,
            # помечаем warning и оставляем как 'completed_unverified'.
            item.step = 'verifying'
            db.session.commit()
            verify_status = self._verify_upload(imported_product, created_product)

            item.status = 'completed'
            item.completed_at = datetime.utcnow()
            if verify_status == 'verified':
                item.step = 'completed'
            elif not created_product:
                item.step = 'completed_pending_sync'
                item.error_message = (
                    'Карточка принята WB, но nmID не получен. '
                    'Связь с Product появится после синхронизации WB.'
                )
            elif verify_status == 'unverified':
                item.step = 'completed_unverified'
                item.error_message = (
                    'WB API не подтвердил наличие карточки сразу после создания. '
                    'Возможно, eventual consistency — проверьте через 1-2 минуты.'
                )
            else:  # 'verify_skipped'
                item.step = 'completed'

            run.total_published += 1
            self.settings.daily_published_count += 1
            db.session.commit()

            self.logger.info(
                f"Товар {imported_product.external_id} опубликован"
                f"{f' (nmID={created_product.nm_id})' if created_product else ''}"
                f" verify={verify_status}"
            )

            if self.settings.notify_on_success:
                self._create_notification(
                    'success',
                    'Товар опубликован',
                    f'Товар "{imported_product.title[:50]}" успешно загружен на WB',
                    link=f'/auto-publish'
                )
            return True
        else:
            # Ошибка — классифицируем и решаем retry
            retryable = _is_retryable_error(error_msg or '')
            item.add_error(item.step, error_msg, retryable)
            item.error_message = error_msg
            item.error_step = item.step

            if retryable and item.retry_count < self.settings.max_retries_per_product:
                # Планируем retry
                item.retry_count += 1
                delay = self.settings.retry_delay_minutes * (2 ** (item.retry_count - 1))
                item.next_retry_at = datetime.utcnow() + timedelta(minutes=delay)
                item.status = 'failed'
                item.step = 'failed'
                item.completed_at = datetime.utcnow()
                # Разблокируем товар для следующего retry
                imported_product.import_status = 'validated'
                self.logger.warning(
                    f"Товар {imported_product.external_id} — retryable ошибка, "
                    f"retry #{item.retry_count} через {delay} мин: {error_msg}"
                )
            else:
                # Финальный fail
                item.status = 'failed'
                item.step = 'failed'
                item.completed_at = datetime.utcnow()
                # WBProductImporter уже поставил import_status='failed'
                self.logger.error(
                    f"Товар {imported_product.external_id} — окончательная ошибка: {error_msg}"
                )

            run.total_failed += 1
            db.session.commit()

            if self.settings.notify_on_failure:
                self._create_notification(
                    'warning',
                    'Ошибка публикации',
                    f'Товар "{imported_product.title[:50]}": {(error_msg or "")[:200]}',
                    link=f'/auto-publish'
                )
            return False

    def _validation_skip_cooldown_minutes(self, current_item: AutoPublishItem) -> int:
        """Кулдаун перед следующей попыткой после skip по валидации.

        Экспоненциальная эскалация по числу прошлых validation-скипов товара:
        base, base*2, base*4, ... с капом в 24 часа. После успешной публикации
        товар уходит из 'validated' и история скипов перестаёт влиять.
        """
        base = max(self.settings.retry_delay_minutes, 15)
        previous_skips = AutoPublishItem.query.filter(
            AutoPublishItem.seller_id == self.seller.id,
            AutoPublishItem.imported_product_id == current_item.imported_product_id,
            AutoPublishItem.status == 'skipped',
            AutoPublishItem.error_step == 'validating',
            AutoPublishItem.id != current_item.id,
        ).count()
        return min(base * (2 ** min(previous_skips, 7)), 24 * 60)

    def _validate_product(self, item: AutoPublishItem, product: ImportedProduct) -> bool:
        """Валидация товара перед загрузкой. Возвращает True если можно загружать."""
        try:
            result = validate_product_upload_readiness(product, self.seller)
            item.validation_result_json = json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            self.logger.warning(f"Ошибка валидации товара {product.id}: {e}")
            item.error_message = f'Ошибка валидации: {e}'
            item.error_step = 'validating'
            return False

        if self.settings.validation_mode == 'strict':
            if not result.get('is_ready', False):
                issues = result.get('issues', [])
                error_msgs = [i.get('message', '') for i in issues if i.get('level') == 'error']
                item.error_message = f"Валидация (strict): {'; '.join(error_msgs[:3])}"
                item.error_step = 'validating'
                return False
        else:
            # lenient — только критические ошибки блокируют
            issues = result.get('issues', [])
            critical = [
                i for i in issues
                if i.get('level') == 'error' and i.get('field') in ('price', 'category', 'characteristics', 'brand')
            ]
            if critical:
                error_msgs = [i.get('message', '') for i in critical]
                item.error_message = f"Валидация (lenient): {'; '.join(error_msgs[:3])}"
                item.error_step = 'validating'
                return False

        # Дополнительная проверка: цена рассчитана
        if not product.calculated_price or product.calculated_price <= 0:
            # Пробуем рассчитать на лету
            try:
                from services.pricing_engine import calculate_price
                pricing = PricingSettings.query.filter_by(seller_id=self.seller.id).first()
                if pricing and product.supplier_price and product.supplier_price > 0:
                    calc = calculate_price(product.supplier_price, pricing, product.id)
                    if calc and calc.get('final_price'):
                        product.calculated_price = calc['final_price']
                        product.calculated_discount_price = calc.get('discount_price')
                        product.calculated_price_before_discount = calc.get('price_before_discount')
                        db.session.commit()
                    else:
                        item.error_message = 'Не удалось рассчитать цену'
                        item.error_step = 'validating'
                        return False
                elif not product.supplier_price:
                    item.error_message = 'Нет закупочной цены поставщика'
                    item.error_step = 'validating'
                    return False
            except Exception as e:
                self.logger.warning(f"Ошибка расчёта цены: {e}")
                item.error_message = f'Ошибка расчёта цены: {e}'
                item.error_step = 'validating'
                return False

        # Дополнительная проверка: фото доступны
        try:
            photo_urls = json.loads(product.photo_urls) if product.photo_urls else []
        except (json.JSONDecodeError, TypeError):
            photo_urls = []
        if not photo_urls:
            item.error_message = 'Нет фотографий товара'
            item.error_step = 'validating'
            return False

        return True

    def _verify_upload(self, imported_product: ImportedProduct, created_product: Optional[Product]) -> str:
        """Проверка что карточка реально создана на WB после import_product_to_wb.

        Поиск по vendor_code через get_card_by_vendor_code (textSearch).
        Возвращает:
          'verified'        — карточка найдена на WB
          'unverified'      — не найдена (eventual consistency или скрытый сбой)
          'verify_skipped'  — verify невозможен (нет vendor_code или WB-клиент)

        Verify не выкидывает исключения и не помечает ничего как failed —
        это диагностическая ступень. Это даёт возможность ловить ложный
        success без повторной публикации.
        """
        vendor_code = None
        if created_product:
            vendor_code = getattr(created_product, 'vendor_code', None)
        if not vendor_code:
            return 'verify_skipped'

        try:
            from services.wb_api_client import WildberriesAPIClient
            api_key = self.seller.wb_api_key
            if not api_key:
                return 'verify_skipped'
            client = WildberriesAPIClient(api_key=api_key)
            # Это endpoint /content/v2/get/cards/list с textSearch — eventually
            # consistent сразу после создания, поэтому unverified ≠ failure.
            client.get_card_by_vendor_code(vendor_code)
            return 'verified'
        except WBAPIException as e:
            self.logger.warning(
                f"Verify: карточка vendor_code={vendor_code} не подтверждена WB API: {e}"
            )
            return 'unverified'
        except Exception as e:
            # Сеть/таймаут/любая другая ошибка — не ломаем pipeline,
            # просто отмечаем как unverified.
            self.logger.warning(
                f"Verify: исключение при проверке vendor_code={vendor_code}: {e}"
            )
            return 'unverified'

    def _check_wb_processing_errors(self, run: AutoPublishRun) -> int:
        """Сверяет успешные items запуска со списком ошибок обработки WB.

        WB принимает карточку сразу (202), а отклонить может позже при
        асинхронной обработке — такие ошибки видны только в
        /content/v2/cards/error/list. Один запрос на весь запуск.
        Item остаётся completed (карточка ушла), но помечается
        step='wb_processing_error' с текстом ошибок WB — это сигнал
        для ручного исправления, а не для повторной публикации.
        """
        completed_items = AutoPublishItem.query.filter_by(
            run_id=run.id, status='completed'
        ).all()
        if not completed_items:
            return 0

        try:
            from services.wb_api_client import WildberriesAPIClient, normalize_cards_error_list
            api_key = self.seller.wb_api_key
            if not api_key:
                return 0
            client = WildberriesAPIClient(api_key=api_key)
            wb_errors = client.get_cards_error_list(seller_id=self.seller.id)
        except Exception as e:
            self.logger.warning(f"Не удалось получить cards/error/list: {e}")
            return 0

        if not wb_errors:
            return 0

        errors_by_nm, errors_by_vendor = normalize_cards_error_list(wb_errors)

        flagged = 0
        for item in completed_items:
            msgs = None
            if item.wb_nm_id and int(item.wb_nm_id) in errors_by_nm:
                msgs = errors_by_nm[int(item.wb_nm_id)]
            elif item.product and getattr(item.product, 'vendor_code', None):
                msgs = errors_by_vendor.get(str(item.product.vendor_code))
            if not msgs:
                continue
            item.step = 'wb_processing_error'
            item.error_step = 'wb_processing'
            item.error_message = (
                'WB отклонил обработку карточки: '
                + '; '.join(str(m) for m in msgs[:3])
            )[:500]
            flagged += 1

        if flagged:
            db.session.commit()
            self.logger.warning(
                f"WB сообщил об ошибках обработки для {flagged} карточек запуска #{run.id}"
            )
            if self.settings.notify_on_failure:
                self._create_notification(
                    'warning',
                    'WB отклонил карточки после приёма',
                    f'{flagged} карточек из запуска автопубликации получили '
                    f'ошибки обработки на стороне WB. Проверьте раздел автопубликации.',
                    link='/auto-publish'
                )
        return flagged

    # ================================================================
    # CIRCUIT BREAKER & FINALIZATION
    # ================================================================

    def _trigger_circuit_breaker(self, run: AutoPublishRun, consecutive_failures: int):
        """Активирует circuit breaker — ставит на паузу."""
        reason = f"Слишком много ошибок подряд ({consecutive_failures}/{self.settings.failure_threshold})"
        self.settings.is_paused = True
        self.settings.paused_reason = reason
        self.settings.paused_at = datetime.utcnow()
        run.status = 'paused'

        self.logger.error(f"Circuit breaker: {reason}")

        if self.settings.notify_on_pause:
            self._create_notification(
                'error',
                'Авто-публикация приостановлена',
                reason,
                link='/auto-publish'
            )

    def _finalize_run(self, run: AutoPublishRun):
        """Обновляет итоговые счётчики запуска."""
        run.completed_at = datetime.utcnow()
        if run.started_at:
            run.duration_seconds = (run.completed_at - run.started_at).total_seconds()

        if run.status == 'running':
            if run.total_failed > 0 and run.total_published == 0:
                run.status = 'failed'
            else:
                run.status = 'completed'

        # Error summary
        error_items = AutoPublishItem.query.filter_by(run_id=run.id, status='failed').all()
        if error_items:
            error_categories = {}
            for ei in error_items:
                step = ei.error_step or 'unknown'
                error_categories[step] = error_categories.get(step, 0) + 1
            run.error_summary = json.dumps(error_categories, ensure_ascii=False)

        self.settings.last_run_at = datetime.utcnow()
        self.settings.next_run_at = datetime.utcnow() + timedelta(
            minutes=self.settings.check_interval_minutes
        )

        db.session.commit()
        self.logger.info(
            f"Запуск завершён: опубликовано={run.total_published}, "
            f"ошибок={run.total_failed}, пропущено={run.total_skipped}"
        )

    def _complete_empty_run(self, run: AutoPublishRun):
        """Корректно завершает запуск без кандидатов и обновляет расписание."""
        run.status = 'completed'
        run.completed_at = datetime.utcnow()
        if run.started_at:
            run.duration_seconds = (run.completed_at - run.started_at).total_seconds()
        self._schedule_next_run()
        db.session.commit()

    def _schedule_next_run(self):
        """Обновляет last/next run timestamps без создания AutoPublishRun."""
        now = datetime.utcnow()
        self.settings.last_run_at = now
        self.settings.next_run_at = now + timedelta(minutes=self.settings.check_interval_minutes)

    def _is_run_cancelled(self, run_id: int) -> bool:
        """Проверяет статус запуска напрямую из БД, обходя cache текущей session."""
        try:
            status = db.session.execute(
                text("SELECT status FROM auto_publish_runs WHERE id = :rid"),
                {'rid': run_id}
            ).scalar()
            return status == 'cancelled'
        except Exception as exc:
            self.logger.warning(f"Не удалось проверить cancellation run_id={run_id}: {exc}")
            return False

    def _mark_pending_items_skipped(self, run: AutoPublishRun, reason: str):
        """Пропускает pending items и разблокирует их товары."""
        remaining = AutoPublishItem.query.filter_by(run_id=run.id, status='pending').all()
        for item in remaining:
            item.status = 'skipped'
            item.step = 'skipped'
            item.error_message = reason
            item.completed_at = datetime.utcnow()
            product = ImportedProduct.query.get(item.imported_product_id)
            if product and product.import_status == 'publishing':
                product.import_status = 'validated'
            run.total_skipped += 1
        db.session.commit()

    def _unlock_publishing_products(self, run: AutoPublishRun):
        """Разблокирует товары, застрявшие в 'publishing' при критической ошибке запуска."""
        items = AutoPublishItem.query.filter_by(run_id=run.id).all()
        for item in items:
            if item.status not in FINAL_ITEM_STATUSES:
                item.status = 'failed'
                item.step = 'failed'
                item.error_message = 'Запуск прерван критической ошибкой'
                item.completed_at = datetime.utcnow()
                product = ImportedProduct.query.get(item.imported_product_id)
                if product and product.import_status == 'publishing':
                    product.import_status = 'validated'

    # ================================================================
    # HELPERS
    # ================================================================

    def _try_acquire_lock(self, lock_token: str) -> bool:
        """Атомарно захватить лок через UPDATE settings.run_lock_token.

        Условие WHERE run_lock_token IS NULL делает захват эксклюзивным:
        SQLite сериализует write-транзакции, а на Postgres UPDATE с условием
        атомарен по строке. Возвращает True при успешном захвате.
        """
        try:
            result = db.session.execute(
                text(
                    "UPDATE auto_publish_settings "
                    "SET run_lock_token = :tok "
                    "WHERE id = :sid AND run_lock_token IS NULL"
                ),
                {'tok': lock_token, 'sid': self.settings.id}
            )
            db.session.commit()
        except Exception as e:
            self.logger.error(f"Ошибка захвата лока: {e}")
            db.session.rollback()
            return False
        if result.rowcount == 1:
            # Подгрузить актуальное значение в session-объект
            db.session.refresh(self.settings)
            return True
        return False

    def _release_lock(self, lock_token: str):
        """Освободить лок (только если он наш — сравнение по токену)."""
        try:
            db.session.execute(
                text(
                    "UPDATE auto_publish_settings "
                    "SET run_lock_token = NULL "
                    "WHERE id = :sid AND run_lock_token = :tok"
                ),
                {'tok': lock_token, 'sid': self.settings.id}
            )
            db.session.commit()
        except Exception as e:
            self.logger.error(f"Не удалось освободить лок: {e}")
            db.session.rollback()

    def _reset_daily_counter_if_needed(self):
        """Сбрасывает дневной счётчик если наступил новый день."""
        now = datetime.utcnow()
        if self.settings.daily_count_reset_at:
            if now.date() > self.settings.daily_count_reset_at.date():
                self.settings.daily_published_count = 0
                self.settings.daily_count_reset_at = now
                db.session.commit()
        else:
            self.settings.daily_count_reset_at = now
            db.session.commit()

    def _get_run(self, run_id: int) -> AutoPublishRun:
        return db.session.get(AutoPublishRun, run_id)

    def _create_notification(self, category: str, title: str, message: str, link: str = None):
        """Создаёт уведомление для продавца."""
        try:
            notif = Notification(
                seller_id=self.seller.id,
                category=category,
                title=title,
                message=message[:500],
                link=link,
            )
            db.session.add(notif)
            db.session.commit()
        except Exception as e:
            self.logger.warning(f"Не удалось создать уведомление: {e}")
            db.session.rollback()


# ================================================================
# SCHEDULER ENTRY POINT
# ================================================================

def check_and_auto_publish_all_sellers(flask_app):
    """
    Точка входа для планировщика.
    Проверяет всех продавцов с включённой автопубликацией
    и запускает обработку в отдельных потоках.
    """
    with flask_app.app_context():
        try:
            settings_list = AutoPublishSettings.query.filter_by(
                is_enabled=True, is_paused=False
            ).all()

            if not settings_list:
                return

            now = datetime.utcnow()

            for settings in settings_list:
                # Проверяем интервал
                if settings.last_run_at:
                    next_allowed = settings.last_run_at + timedelta(
                        minutes=settings.check_interval_minutes
                    )
                    if now < next_allowed:
                        continue

                # Best-effort: пропустить если лок уже занят (реальный
                # атомарный захват всё равно делается в execute_run).
                if settings._run_lock_token:
                    logger.debug(f"Seller {settings.seller_id}: лок занят, пропускаем")
                    continue

                seller = Seller.query.get(settings.seller_id)
                if not seller or not seller.wb_api_key:
                    continue

                # Запускаем в отдельном потоке
                thread = threading.Thread(
                    target=_run_auto_publish_for_seller,
                    args=(flask_app, seller.id, settings.id),
                    name=f'auto-publish-seller-{seller.id}',
                    daemon=True,
                )
                thread.start()
                logger.info(f"Запущена автопубликация для seller {seller.id}")

        except Exception as e:
            logger.error(f"Ошибка в check_and_auto_publish_all_sellers: {e}", exc_info=True)


def _run_auto_publish_for_seller(flask_app, seller_id: int, settings_id: int):
    """Выполняет автопубликацию для одного продавца в отдельном потоке."""
    with flask_app.app_context():
        try:
            seller = db.session.get(Seller, seller_id)
            settings = db.session.get(AutoPublishSettings, settings_id)
            if not seller or not settings:
                return

            service = AutoPublishService(seller, settings)
            service.execute_run(triggered_by='scheduler')
        except Exception as e:
            logger.error(f"Ошибка автопубликации для seller {seller_id}: {e}", exc_info=True)


def reset_stuck_auto_publish(flask_app):
    """Сбрасывает зависшие запуски после рестарта сервера."""
    with flask_app.app_context():
        try:
            # Сбрасываем running запуски
            stuck_runs = AutoPublishRun.query.filter_by(status='running').all()
            for run in stuck_runs:
                run.status = 'failed'
                run.error_summary = json.dumps(
                    {'critical_error': 'Запуск прерван перезапуском сервера'},
                    ensure_ascii=False
                )
                run.completed_at = datetime.utcnow()
                logger.warning(f"Сброшен зависший запуск автопубликации #{run.id}")

            # Сбрасываем items в processing/pending (висящие после краша)
            stuck_items = AutoPublishItem.query.filter(
                AutoPublishItem.status.in_(('pending', 'processing'))
            ).all()
            for it in stuck_items:
                original_step = it.step or 'unknown'
                it.status = 'failed'
                it.step = 'failed'
                it.error_message = 'Прерван перезапуском сервера'
                it.error_step = original_step
                it.completed_at = datetime.utcnow()

            # Сбрасываем товары в 'publishing'
            stuck_products = ImportedProduct.query.filter_by(import_status='publishing').all()
            for p in stuck_products:
                p.import_status = 'validated'

            # Сбрасываем висящие лок-токены — после рестарта они уже невалидны
            db.session.execute(
                text(
                    "UPDATE auto_publish_settings SET run_lock_token = NULL "
                    "WHERE run_lock_token IS NOT NULL"
                )
            )

            if stuck_runs or stuck_products or stuck_items:
                db.session.commit()
                logger.info(
                    f"Сброшено: {len(stuck_runs)} запусков, "
                    f"{len(stuck_items)} items, "
                    f"{len(stuck_products)} товаров в 'publishing'"
                )
            else:
                # Всё равно коммитим возможный UPDATE токенов
                db.session.commit()
        except Exception as e:
            logger.error(f"Ошибка сброса зависших автопубликаций: {e}")
            db.session.rollback()
