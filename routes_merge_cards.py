"""
Роуты для объединения/разъединения карточек товаров WB
"""
from flask import render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from datetime import datetime
import json

from models import db, Product, CardMergeHistory
from wb_api_client import WildberriesAPIClient, WBAPIException


def register_merge_routes(app):
    """Регистрация роутов для объединения карточек"""

    @app.route('/products/merge', methods=['GET'])
    @login_required
    def products_merge():
        """Страница объединения карточек"""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для объединения карточек необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('settings'))

        # Группировка карточек по imtID для показа уже объединенных
        products_query = Product.query.filter_by(
            seller_id=current_user.seller.id,
            is_active=True
        ).order_by(Product.subject_id, Product.imt_id, Product.vendor_code)

        # Фильтры
        subject_filter = request.args.get('subject_id', type=int)
        if subject_filter:
            products_query = products_query.filter_by(subject_id=subject_filter)

        products = products_query.all()

        # Группировка по imtID
        imt_groups = {}
        for product in products:
            imt_id = product.imt_id or f"single_{product.nm_id}"
            if imt_id not in imt_groups:
                imt_groups[imt_id] = {
                    'imt_id': product.imt_id,
                    'subject_id': product.subject_id,
                    'subject_name': product.object_name,
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

        # Список уникальных категорий
        subjects = db.session.query(
            Product.subject_id,
            Product.object_name
        ).filter_by(
            seller_id=current_user.seller.id,
            is_active=True
        ).distinct().all()

        return render_template(
            'products_merge.html',
            imt_groups=list(imt_groups.values()),
            subjects=[{'id': s[0], 'name': s[1]} for s in subjects],
            selected_subject=subject_filter
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
            client = WildberriesAPIClient(current_user.seller.wb_api_key)

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
            app.logger.error(f"Error in products_merge_execute: {str(e)}")
            flash('Произошла ошибка при объединении карточек', 'danger')
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
            client = WildberriesAPIClient(current_user.seller.wb_api_key)

            try:
                result = client.unmerge_cards(
                    nm_ids=merge.merged_nm_ids,
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
