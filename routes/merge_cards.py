"""
Роуты для объединения/разъединения карточек товаров WB
"""
from flask import render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from datetime import datetime
import json

from models import db, Product, CardMergeHistory, APILog
from services.wb_api_client import WildberriesAPIClient, WBAPIException
from sqlalchemy import or_
from services.merge_recommendations import get_merge_recommendations_for_seller


def _get_wb_client(seller):
    """Создать WB API клиент с логированием для merge-операций."""
    return WildberriesAPIClient(
        api_key=seller.wb_api_key,
        db_logger_callback=lambda **kwargs: APILog.log_request(**kwargs)
    )


def register_merge_routes(app):
    """Регистрация роутов для объединения карточек"""

    @app.route('/products/merge', methods=['GET'])
    @login_required
    def products_merge():
        """Страница объединения карточек с пагинацией и расширенными фильтрами"""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для объединения карточек необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('settings'))

        # Параметры фильтрации и пагинации
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        subject_filter = request.args.get('subject_id', type=int)
        brand_filter = request.args.get('brand', type=str)
        search_query = request.args.get('search', type=str, default='').strip()

        # Базовый запрос
        products_query = Product.query.filter_by(
            seller_id=current_user.seller.id,
            is_active=True
        )

        # Применяем фильтры
        if subject_filter:
            products_query = products_query.filter_by(subject_id=subject_filter)

        if brand_filter:
            products_query = products_query.filter_by(brand=brand_filter)

        if search_query:
            # Поиск по названию, артикулу, бренду
            search_pattern = f"%{search_query}%"
            products_query = products_query.filter(
                or_(
                    Product.title.ilike(search_pattern),
                    Product.vendor_code.ilike(search_pattern),
                    Product.brand.ilike(search_pattern)
                )
            )

        # Сортировка
        products_query = products_query.order_by(
            Product.subject_id,
            Product.imt_id,
            Product.brand,
            Product.vendor_code
        )

        # Получаем все подходящие товары (до пагинации) для группировки
        all_products = products_query.all()

        # Группировка по imtID
        imt_groups = {}
        for product in all_products:
            imt_id = product.imt_id or f"single_{product.nm_id}"
            if imt_id not in imt_groups:
                imt_groups[imt_id] = {
                    'imt_id': product.imt_id,
                    'subject_id': product.subject_id,
                    'subject_name': product.object_name,
                    'brand': product.brand,
                    'cards': []
                }
            imt_groups[imt_id]['cards'].append({
                'nm_id': product.nm_id,
                'imt_id': product.imt_id,
                'vendor_code': product.vendor_code,
                'title': product.title,
                'brand': product.brand,
                'subject_id': product.subject_id
            })

        # Пагинация групп (не карточек)
        groups_list = list(imt_groups.values())
        total_groups = len(groups_list)
        total_pages = (total_groups + per_page - 1) // per_page

        # Корректируем номер страницы
        if page < 1:
            page = 1
        elif page > total_pages and total_pages > 0:
            page = total_pages

        # Получаем группы для текущей страницы
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_groups = groups_list[start_idx:end_idx]

        # Список уникальных категорий (для фильтра)
        subjects = db.session.query(
            Product.subject_id,
            Product.object_name
        ).filter_by(
            seller_id=current_user.seller.id,
            is_active=True
        ).distinct().order_by(Product.object_name).all()

        # Список уникальных брендов (для фильтра)
        brands = db.session.query(Product.brand).filter(
            Product.seller_id == current_user.seller.id,
            Product.is_active == True,
            Product.brand != None,
            Product.brand != ''
        ).distinct().order_by(Product.brand).all()
        brands_list = [b[0] for b in brands if b[0]]

        return render_template(
            'products_merge.html',
            imt_groups=paginated_groups,
            subjects=[{'id': s[0], 'name': s[1]} for s in subjects],
            brands=brands_list,
            selected_subject=subject_filter,
            selected_brand=brand_filter,
            search_query=search_query,
            page=page,
            per_page=per_page,
            total_groups=total_groups,
            total_pages=total_pages,
            total_cards=len(all_products)
        )

    @app.route('/products/merge/execute', methods=['POST'])
    @login_required
    def products_merge_execute():
        """Выполнить объединение карточек"""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ не настроен'}), 400

        try:
            target_nm_id = request.form.get('target_nm_id', type=int)
            nm_ids_str = request.form.get('nm_ids', '')
            nm_ids = [int(x.strip()) for x in nm_ids_str.split(',') if x.strip().isdigit()]

            if not target_nm_id or not nm_ids:
                flash('Выберите главную карточку и карточки для объединения', 'warning')
                return redirect(url_for('products_merge'))

            if len(nm_ids) > 30:
                flash('Можно объединить максимум 30 карточек за раз', 'warning')
                return redirect(url_for('products_merge'))

            # Получаем целевую карточку
            target_product = Product.query.filter_by(
                nm_id=target_nm_id,
                seller_id=current_user.seller.id
            ).first()

            if not target_product or not target_product.imt_id:
                flash('Целевая карточка не найдена или не имеет imtID', 'danger')
                return redirect(url_for('products_merge'))

            # Проверка что все карточки имеют одинаковый subject_id
            products_to_merge = Product.query.filter(
                Product.nm_id.in_(nm_ids),
                Product.seller_id == current_user.seller.id
            ).all()

            if not all(p.subject_id == target_product.subject_id for p in products_to_merge):
                flash('Можно объединять только карточки с одинаковой категорией', 'warning')
                return redirect(url_for('products_merge'))

            # Создаем запись истории
            start_time = datetime.utcnow()

            # Снимок ДО
            snapshot_before = {
                str(p.nm_id): {
                    'imt_id': p.imt_id,
                    'vendor_code': p.vendor_code,
                    'title': p.title,
                    'subject_id': p.subject_id
                } for p in products_to_merge + [target_product]
            }

            merge_history = CardMergeHistory(
                seller_id=current_user.seller.id,
                operation_type='merge',
                target_imt_id=target_product.imt_id,
                merged_nm_ids=nm_ids,
                snapshot_before=snapshot_before,
                status='in_progress'
            )
            db.session.add(merge_history)
            db.session.commit()

            # Выполняем объединение через API
            client = _get_wb_client(current_user.seller)

            try:
                result = client.merge_cards(
                    target_imt_id=target_product.imt_id,
                    nm_ids=nm_ids,
                    log_to_db=True,
                    seller_id=current_user.seller.id
                )

                # Обновляем БД
                for product in products_to_merge:
                    product.imt_id = target_product.imt_id

                # Снимок ПОСЛЕ
                snapshot_after = {
                    str(p.nm_id): {
                        'imt_id': p.imt_id,
                        'vendor_code': p.vendor_code,
                        'title': p.title,
                        'subject_id': p.subject_id
                    } for p in products_to_merge + [target_product]
                }

                merge_history.snapshot_after = snapshot_after
                merge_history.status = 'completed'
                merge_history.wb_synced = True
                merge_history.wb_sync_status = 'success'
                merge_history.completed_at = datetime.utcnow()
                merge_history.duration_seconds = (datetime.utcnow() - start_time).total_seconds()

                db.session.commit()

                flash(f'Успешно объединено {len(nm_ids)} карточек к imtID={target_product.imt_id}', 'success')
                return redirect(url_for('products_merge_history', id=merge_history.id))

            except WBAPIException as e:
                merge_history.status = 'failed'
                merge_history.wb_synced = False
                merge_history.wb_sync_status = 'failed'
                merge_history.wb_error_message = str(e)
                db.session.commit()

                flash(f'Ошибка при объединении карточек: {str(e)}', 'danger')
                return redirect(url_for('products_merge'))

        except Exception as e:
            app.logger.error(f"Error in products_merge_execute: {str(e)}", exc_info=True)
            error_msg = str(e)
            if 'no such table' in error_msg.lower():
                flash('Таблица истории объединений не найдена. Запустите миграцию: python migrations/run_all_migrations.py', 'danger')
            elif 'operational' in error_msg.lower():
                flash(f'Ошибка базы данных: {error_msg[:200]}', 'danger')
            else:
                flash(f'Ошибка при объединении карточек: {error_msg[:200]}', 'danger')
            return redirect(url_for('products_merge'))

    @app.route('/products/merge/unmerge/<int:imt_id>', methods=['POST'])
    @login_required
    def products_merge_unmerge_group(imt_id):
        """Разъединить группу карточек по imtID"""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ не настроен'}), 400

        try:
            # Находим все карточки с этим imt_id
            products = Product.query.filter_by(
                imt_id=imt_id,
                seller_id=current_user.seller.id,
                is_active=True
            ).all()

            if not products:
                flash('Карточки не найдены', 'warning')
                return redirect(url_for('products_merge'))

            if len(products) < 2:
                flash('Эта карточка не объединена с другими', 'info')
                return redirect(url_for('products_merge'))

            # Собираем nm_ids
            nm_ids = [p.nm_id for p in products]

            # Создаем запись истории
            start_time = datetime.utcnow()

            # Снимок ДО
            snapshot_before = {
                str(p.nm_id): {
                    'imt_id': p.imt_id,
                    'vendor_code': p.vendor_code,
                    'title': p.title,
                    'subject_id': p.subject_id
                } for p in products
            }

            unmerge_history = CardMergeHistory(
                seller_id=current_user.seller.id,
                operation_type='unmerge',
                merged_nm_ids=nm_ids,
                snapshot_before=snapshot_before,
                status='in_progress',
                user_comment=f"Разъединение группы imtID={imt_id}"
            )
            db.session.add(unmerge_history)
            db.session.commit()

            # Разъединяем через API
            client = _get_wb_client(current_user.seller)

            import time
            api_errors = []

            try:
                # ВАЖНО: отправляем каждую карточку отдельным запросом,
                # иначе WB объединит их в новую группу с новым imtID.
                # Последнюю карточку не трогаем — она автоматически останется одна.
                for nm_id in nm_ids[:-1]:
                    try:
                        client.unmerge_cards(
                            nm_ids=[nm_id],
                            log_to_db=True,
                            seller_id=current_user.seller.id
                        )
                    except WBAPIException as e:
                        # Если карточка уже отдельная на WB — не критично, продолжаем
                        err_str = str(e)
                        app.logger.warning(f"unmerge nm_id={nm_id}: {err_str}")
                        api_errors.append(err_str)
                    time.sleep(0.3)

                # Обновляем imt_id в локальной БД: сбрасываем в None
                # (реальные новые imtID подтянутся при следующей синхронизации с WB)
                for product in products:
                    product.imt_id = None
                    product.last_sync = datetime.utcnow()

                unmerge_history.snapshot_after = snapshot_before.copy()
                unmerge_history.status = 'completed'
                unmerge_history.wb_synced = True
                unmerge_history.wb_sync_status = 'success'
                unmerge_history.wb_error_message = '; '.join(api_errors) if api_errors else None
                unmerge_history.completed_at = datetime.utcnow()
                unmerge_history.duration_seconds = (datetime.utcnow() - start_time).total_seconds()

                db.session.commit()

                if api_errors:
                    flash(f'Карточки разъединены локально. Часть карточек уже была разъединена на WB ранее.', 'success')
                else:
                    flash(f'Успешно разъединено {len(nm_ids)} карточек.', 'success')
                return redirect(url_for('products_merge_history', id=unmerge_history.id))

            except Exception as e:
                # Даже при ошибке сбрасываем imt_id в БД, чтобы карточки
                # не зависали в состоянии "объединено" вечно
                for product in products:
                    product.imt_id = None
                unmerge_history.status = 'failed'
                unmerge_history.wb_synced = False
                unmerge_history.wb_sync_status = 'failed'
                unmerge_history.wb_error_message = str(e)
                db.session.commit()

                flash(f'Ошибка WB API при разъединении: {str(e)}. Локальные данные сброшены — выполните синхронизацию.', 'warning')
                return redirect(url_for('products_merge'))

        except Exception as e:
            app.logger.error(f"Error in products_merge_unmerge_group: {str(e)}")
            flash('Произошла ошибка при разъединении карточек', 'danger')
            return redirect(url_for('products_merge'))

    @app.route('/products/merge/unmerge-single/<int:nm_id>', methods=['GET', 'POST'])
    @login_required
    def products_merge_unmerge_single(nm_id):
        """Разъединить одну карточку из группы"""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('API ключ не настроен', 'danger')
            return redirect(url_for('products_merge'))

        try:
            # Находим карточку
            product = Product.query.filter_by(
                nm_id=nm_id,
                seller_id=current_user.seller.id,
                is_active=True
            ).first()

            if not product:
                flash('Карточка не найдена', 'warning')
                return redirect(url_for('products_merge'))

            if not product.imt_id:
                flash('Карточка не имеет imtID', 'warning')
                return redirect(url_for('products_merge'))

            # Проверяем, что карточка действительно в группе
            group_count = Product.query.filter_by(
                imt_id=product.imt_id,
                seller_id=current_user.seller.id,
                is_active=True
            ).count()

            if group_count < 2:
                flash('Эта карточка не объединена с другими', 'info')
                return redirect(url_for('products_merge'))

            # Создаем запись истории
            start_time = datetime.utcnow()

            snapshot_before = {
                str(product.nm_id): {
                    'imt_id': product.imt_id,
                    'vendor_code': product.vendor_code,
                    'title': product.title,
                    'subject_id': product.subject_id
                }
            }

            unmerge_history = CardMergeHistory(
                seller_id=current_user.seller.id,
                operation_type='unmerge',
                merged_nm_ids=[nm_id],
                snapshot_before=snapshot_before,
                status='in_progress',
                user_comment=f"Отсоединение карточки nmID={nm_id} от группы imtID={product.imt_id}"
            )
            db.session.add(unmerge_history)
            db.session.commit()

            # Разъединяем через API (только одну карточку)
            client = _get_wb_client(current_user.seller)

            wb_error = None
            try:
                client.unmerge_cards(
                    nm_ids=[nm_id],
                    log_to_db=True,
                    seller_id=current_user.seller.id
                )
            except WBAPIException as e:
                # Карточка уже могла быть разъединена на WB — не критично
                wb_error = str(e)
                app.logger.warning(f"unmerge single nm_id={nm_id}: {wb_error}")

            # В любом случае обновляем локальную БД
            product.imt_id = None
            product.last_sync = datetime.utcnow()

            unmerge_history.snapshot_after = snapshot_before.copy()
            unmerge_history.status = 'completed'
            unmerge_history.wb_synced = not bool(wb_error)
            unmerge_history.wb_sync_status = 'failed' if wb_error else 'success'
            unmerge_history.wb_error_message = wb_error
            unmerge_history.completed_at = datetime.utcnow()
            unmerge_history.duration_seconds = (datetime.utcnow() - start_time).total_seconds()

            db.session.commit()

            if wb_error:
                flash(f'Карточка {product.vendor_code} уже была отсоединена на WB. Локальные данные обновлены.', 'success')
            else:
                flash(f'Карточка {product.vendor_code} отсоединена от группы.', 'success')
            return redirect(url_for('products_merge'))

        except Exception as e:
            app.logger.error(f"Error in products_merge_unmerge_single: {str(e)}")
            flash('Произошла ошибка при отсоединении карточки', 'danger')
            return redirect(url_for('products_merge'))

    @app.route('/products/merge/history')
    @login_required
    def products_merge_history_list():
        """История объединений карточек"""
        if not current_user.seller:
            flash('Для просмотра истории необходим профиль продавца', 'warning')
            return redirect(url_for('dashboard'))

        history = CardMergeHistory.query.filter_by(
            seller_id=current_user.seller.id
        ).order_by(CardMergeHistory.created_at.desc()).limit(100).all()

        return render_template('products_merge_history.html', history=history)

    @app.route('/products/merge/history/<int:id>')
    @login_required
    def products_merge_history(id):
        """Детали объединения"""
        merge = CardMergeHistory.query.filter_by(
            id=id,
            seller_id=current_user.seller.id
        ).first_or_404()

        return render_template('products_merge_detail.html', merge=merge)

    @app.route('/products/merge/recommendations')
    @login_required
    def products_merge_recommendations():
        """Автоматические рекомендации для объединения карточек"""
        if not current_user.seller:
            flash('Для просмотра рекомендаций необходим профиль продавца', 'warning')
            return redirect(url_for('dashboard'))

        # Параметры
        min_score = request.args.get('min_score', 0.6, type=float)
        show_top = request.args.get('show_top', 20, type=int)

        # Получаем рекомендации
        try:
            all_recommendations = get_merge_recommendations_for_seller(
                seller_id=current_user.seller.id,
                db_session=db.session,
                min_score=min_score
            )

            # Ограничиваем количество
            recommendations = all_recommendations[:show_top]

            # Статистика
            total_recommendations = len(all_recommendations)
            total_cards_to_merge = sum(len(r['cards']) for r in recommendations)

            return render_template(
                'products_merge_recommendations.html',
                recommendations=recommendations,
                total_recommendations=total_recommendations,
                total_cards=total_cards_to_merge,
                min_score=min_score,
                show_top=show_top
            )

        except Exception as e:
            app.logger.error(f"Error getting merge recommendations: {str(e)}")
            flash('Ошибка при получении рекомендаций', 'danger')
            return redirect(url_for('products_merge'))

    @app.route('/products/merge/revert/<int:id>', methods=['POST'])
    @login_required
    def products_merge_revert(id):
        """Откатить объединение"""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ не настроен'}), 400

        merge = CardMergeHistory.query.filter_by(
            id=id,
            seller_id=current_user.seller.id
        ).first_or_404()

        if not merge.can_revert():
            flash('Эту операцию нельзя откатить', 'warning')
            return redirect(url_for('products_merge_history', id=id))

        try:
            # Создаем запись истории отката
            start_time = datetime.utcnow()

            snapshot_before = merge.snapshot_after.copy()

            revert_history = CardMergeHistory(
                seller_id=current_user.seller.id,
                operation_type='unmerge',
                merged_nm_ids=merge.merged_nm_ids,
                snapshot_before=snapshot_before,
                status='in_progress',
                user_comment=f"Откат операции #{merge.id}"
            )
            db.session.add(revert_history)
            db.session.commit()

            # Разъединяем через API
            client = _get_wb_client(current_user.seller)

            # ВАЖНО: Нужно передать ВСЕ nmID которые объединены, включая target
            # merged_nm_ids содержит только те, что были в чекбоксах (без target)
            # Получаем все nmID из snapshot_after - там все карточки с одинаковым imt_id
            all_nm_ids = [int(nm_id) for nm_id in merge.snapshot_after.keys()]

            app.logger.info(f"🔓 Unmerging {len(all_nm_ids)} cards: {all_nm_ids}")

            try:
                result = client.unmerge_cards(
                    nm_ids=all_nm_ids,
                    log_to_db=True,
                    seller_id=current_user.seller.id
                )

                # Отмечаем оригинальную операцию как откаченную
                merge.reverted = True
                merge.reverted_at = datetime.utcnow()
                merge.reverted_by_user_id = current_user.id
                merge.revert_operation_id = revert_history.id

                revert_history.status = 'completed'
                revert_history.wb_synced = True
                revert_history.wb_sync_status = 'success'
                revert_history.completed_at = datetime.utcnow()
                revert_history.duration_seconds = (datetime.utcnow() - start_time).total_seconds()

                db.session.commit()

                flash(f'Объединение успешно отменено. Карточки разъединены.', 'success')
                return redirect(url_for('products_merge_history', id=revert_history.id))

            except WBAPIException as e:
                revert_history.status = 'failed'
                revert_history.wb_synced = False
                revert_history.wb_sync_status = 'failed'
                revert_history.wb_error_message = str(e)
                db.session.commit()

                flash(f'Ошибка при откате объединения: {str(e)}', 'danger')
                return redirect(url_for('products_merge_history', id=id))

        except Exception as e:
            app.logger.error(f"Error in products_merge_revert: {str(e)}")
            flash('Произошла ошибка при откате объединения', 'danger')
            return redirect(url_for('products_merge_history', id=id))

    @app.route('/products/merge/force-unmerge/<int:imt_id>', methods=['POST'])
    @login_required
    def products_merge_force_unmerge(imt_id):
        """Принудительно сбросить объединение в локальной БД (без вызова WB API).
        Используется когда карточки уже разъединены на WB, но в нашей БД всё ещё числятся объединёнными."""
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 400

        products = Product.query.filter_by(
            imt_id=imt_id,
            seller_id=current_user.seller.id,
            is_active=True
        ).all()

        if not products:
            flash('Карточки не найдены', 'warning')
            return redirect(url_for('products_merge'))

        count = len(products)
        for product in products:
            product.imt_id = None
            product.last_sync = datetime.utcnow()

        db.session.commit()

        flash(f'Локальные данные обновлены: {count} карточек помечены как несвязанные. '
              f'Выполните синхронизацию с WB чтобы получить актуальные imtID.', 'success')
        return redirect(url_for('products_merge'))

