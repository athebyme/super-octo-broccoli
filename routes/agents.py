# -*- coding: utf-8 -*-
"""
UI маршруты для дашборда агентов.

Позволяет продавцам и админам:
- Видеть список агентов и их статус
- Просматривать задачи и прогресс в реальном времени
- Смотреть пошаговые логи рассуждений агента
- Создавать задачи вручную
- Отменять задачи
"""
import json
import logging
import uuid
from datetime import datetime

from flask import render_template, request, redirect, url_for, flash, jsonify, abort, session
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash

from models import db, ServiceAgent, AgentTask, AgentTaskStep, AgentMessage
from services import agent_service
from services import agent_harness

logger = logging.getLogger(__name__)


def register_agents_routes(app):
    """Регистрирует маршруты дашборда агентов."""

    def _task_seller_scope():
        """Return seller scope; only an explicit admin may use global scope."""
        if current_user.is_admin:
            return None
        if not current_user.seller:
            abort(403)
        return current_user.seller.id

    # ── Дашборд ─────────────────────────────────────────────────────

    @app.route('/agents')
    @login_required
    def agents_dashboard():
        """Unified chat assistant."""
        if not current_user.seller:
            flash('Нет доступа', 'danger')
            return redirect(url_for('dashboard'))

        seller_id = current_user.seller.id
        conversations = agent_harness.list_conversations(
            seller_id, current_user.id, limit=40,
        )
        requested_id = request.args.get('conversation')
        conversation = None
        if requested_id:
            conversation = agent_harness.get_conversation(
                requested_id, seller_id, current_user.id,
            )
        if conversation is None and conversations:
            conversation = conversations[0]
        if conversation is None:
            conversation = agent_harness.create_conversation(seller_id, current_user.id)
            conversations = [conversation]

        initial_payload = agent_harness.conversation_payload(conversation)
        return render_template(
            'agents.html', conversations=conversations,
            conversation_items=[item.to_dict() for item in conversations],
            conversation=conversation, initial_payload=initial_payload,
            runtime=agent_harness.runtime_state(),
            model_policy=agent_harness.get_model_policy(seller_id),
        )

    # ── Unified chat API ───────────────────────────────────────────

    def _owned_conversation(conversation_id):
        if not current_user.seller:
            return None
        return agent_harness.get_conversation(
            conversation_id, current_user.seller.id, current_user.id,
        )

    @app.route('/agents/api/conversations', methods=['POST'])
    @login_required
    def agents_api_create_conversation():
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        body = request.get_json(silent=True) or {}
        conversation = agent_harness.create_conversation(
            current_user.seller.id, current_user.id, body.get('title'),
        )
        return jsonify({'conversation': conversation.to_dict()}), 201

    @app.route('/agents/api/conversations/<conversation_id>')
    @login_required
    def agents_api_conversation(conversation_id):
        conversation = _owned_conversation(conversation_id)
        if not conversation:
            return jsonify({'error': 'Диалог не найден'}), 404
        step_after = max(request.args.get('step_after', 0, type=int), 0)
        return jsonify(agent_harness.conversation_payload(
            conversation, step_after=step_after,
        ))

    @app.route('/agents/api/conversations/<conversation_id>/messages', methods=['POST'])
    @login_required
    def agents_api_send_message(conversation_id):
        conversation = _owned_conversation(conversation_id)
        if not conversation:
            return jsonify({'error': 'Диалог не найден'}), 404
        body = request.get_json(silent=True) or {}
        try:
            result = agent_harness.submit_turn(
                conversation, body.get('message', ''), body.get('product_ids'),
                body.get('page_context'), body.get('entity_kind'),
                body.get('scope_mode'),
            )
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({'error': str(exc)}), 409
        return jsonify({
            'conversation': conversation.to_dict(),
            'messages': [
                item.to_dict() for item in (
                    result.get('user_message'), result.get('assistant_message'), result.get('run')
                ) if item
            ],
        }), 201

    @app.route('/agents/api/conversations/<conversation_id>/plans/<message_id>/approve', methods=['POST'])
    @login_required
    def agents_api_approve_plan(conversation_id, message_id):
        conversation = _owned_conversation(conversation_id)
        if not conversation:
            return jsonify({'error': 'Диалог не найден'}), 404
        try:
            message = agent_harness.approve_plan(conversation, message_id)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({'error': str(exc)}), 503
        return jsonify({'message': message.to_dict()})

    @app.route('/agents/api/conversations/<conversation_id>/plans/<message_id>/reject', methods=['POST'])
    @login_required
    def agents_api_reject_plan(conversation_id, message_id):
        conversation = _owned_conversation(conversation_id)
        if not conversation:
            return jsonify({'error': 'Диалог не найден'}), 404
        try:
            message = agent_harness.reject_plan(conversation, message_id)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        return jsonify({'message': message.to_dict()})

    @app.route('/agents/api/tasks/<task_id>/cancel', methods=['POST'])
    @login_required
    def agents_api_cancel_run(task_id):
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        try:
            task = agent_harness.cancel_run(task_id, current_user.seller.id)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        return jsonify({'task': task.to_dict()})

    @app.route('/agents/api/tasks/<task_id>/rollback', methods=['POST'])
    @login_required
    def agents_api_rollback_run(task_id):
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        try:
            result = agent_harness.rollback_task_tree(task_id, current_user.seller.id)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        except Exception:
            logger.exception('Unified agent rollback failed for task %s', task_id)
            return jsonify({'error': 'Не удалось откатить изменения'}), 500
        return jsonify({'success': True, **result})

    @app.route('/agents/api/proposals/<int:proposal_id>/<decision>', methods=['POST'])
    @login_required
    def agents_api_review_proposal(proposal_id, decision):
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        try:
            proposal = agent_harness.review_proposal(
                proposal_id, current_user.seller.id, current_user.id, decision,
            )
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        return jsonify({'proposal': proposal.to_dict()})

    # ── Детали задачи ───────────────────────────────────────────────

    @app.route('/agents/tasks/<task_id>')
    @login_required
    def agent_task_detail(task_id):
        """Детальный просмотр задачи со всеми шагами."""
        seller_id = _task_seller_scope()
        task = agent_service.get_task(task_id)
        if not task:
            abort(404)

        if seller_id is not None and task.seller_id != seller_id:
            abort(403)

        steps = agent_service.get_task_steps(task_id)

        return render_template('agent_task_detail.html',
            task=task,
            steps=steps,
        )

    # ── API для UI (AJAX) ──────────────────────────────────────────

    @app.route('/agents/api/tasks')
    @login_required
    def agents_api_list_tasks():
        """API: список задач с фильтрацией (для AJAX-обновления)."""
        seller_id = _task_seller_scope()
        status = request.args.get('status')
        agent_id = request.args.get('agent_id')
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)

        tasks, total = agent_service.list_tasks(
            seller_id=seller_id,
            agent_id=agent_id,
            status=status,
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        return jsonify({
            'tasks': [t.to_dict() for t in tasks],
            'total': total,
            'page': page,
        })

    @app.route('/agents/api/tasks/<task_id>/status')
    @login_required
    def agents_api_task_status(task_id):
        """API: статус конкретной задачи (для polling)."""
        seller_id = _task_seller_scope()
        task = agent_service.get_task(task_id)
        if not task:
            return jsonify({'error': 'Not found'}), 404
        if not current_user.is_admin and (not seller_id or task.seller_id != seller_id):
            return jsonify({'error': 'Access denied'}), 403
        steps = agent_service.get_task_steps(task_id, limit=50)

        # Если это pipeline-задача — добавляем статус подзадач
        subtasks_data = []
        if task.subtasks.count() > 0:
            for st in task.subtasks.order_by(AgentTask.created_at.asc()).all():
                subtasks_data.append(st.to_dict())

        return jsonify({
            'task': task.to_dict(),
            'steps': [s.to_dict() for s in steps],
            'subtasks': subtasks_data,
        })

    @app.route('/agents/api/stats')
    @login_required
    def agents_api_stats():
        """API: статистика агентов."""
        seller_id = _task_seller_scope()
        days = request.args.get('days', 7, type=int)
        stats = agent_service.get_agent_stats(
            seller_id=seller_id,
            days=days,
        )
        return jsonify(stats)

    @app.route('/agents/api/agents')
    @login_required
    def agents_api_list_agents():
        """API: статус всех агентов (для polling)."""
        agents = agent_service.list_agents()
        agent_service.mark_stale_agents()
        return jsonify({
            'agents': [a.to_dict() for a in agents],
        })

    @app.route('/agents/api/available-actions')
    @login_required
    def agents_api_available_actions():
        """API: доступные AI-действия для товаров (online агенты с task_types)."""
        agents = agent_service.list_agents()
        agent_service.mark_stale_agents()

        # Map agent names to product-relevant actions
        PRODUCT_ACTIONS = {
            'seo-writer': {
                'single': 'seo_single',
                'batch': 'seo_batch',
                'label': 'SEO оптимизация',
                'label_batch': 'SEO пакетом',
                'icon': 'pen',
                'color': 'emerald',
                'title_template': 'SEO: {product}',
            },
            'category-mapper': {
                'single': 'map_single',
                'batch': 'map_batch',
                'label': 'Подобрать категорию',
                'label_batch': 'Категории пакетом',
                'icon': 'tag',
                'color': 'blue',
                'title_template': 'Категория: {product}',
            },
            'characteristics-filler': {
                'single': 'fill_single',
                'batch': 'fill_batch',
                'label': 'Заполнить характеристики',
                'label_batch': 'Характеристики пакетом',
                'icon': 'list',
                'color': 'indigo',
                'title_template': 'Характеристики: {product}',
            },
            'size-normalizer': {
                'single': 'normalize_single',
                'batch': 'normalize_batch',
                'label': 'Нормализовать размеры',
                'label_batch': 'Размеры пакетом',
                'icon': 'ruler',
                'color': 'cyan',
                'title_template': 'Размеры: {product}',
            },
            'card-doctor': {
                'single': 'diagnose_single',
                'batch': 'diagnose_batch',
                'label': 'Проверить карточку',
                'label_batch': 'Проверка пакетом',
                'icon': 'shield',
                'color': 'red',
                'title_template': 'Диагностика: {product}',
            },
            'brand-resolver': {
                'single': 'resolve_single',
                'batch': 'resolve_batch',
                'label': 'Определить бренд',
                'label_batch': 'Бренды пакетом',
                'icon': 'badge',
                'color': 'amber',
                'title_template': 'Бренд: {product}',
            },
            'price-optimizer': {
                'single': 'optimize_prices',
                'batch': 'optimize_prices',
                'label': 'Оптимизировать цену',
                'label_batch': 'Цены пакетом',
                'icon': 'chart',
                'color': 'emerald',
                'title_template': 'Цена: {product}',
            },
            'review-analyst': {
                'single': 'analyze_reviews',
                'batch': 'analyze_reviews',
                'label': 'Анализ отзывов',
                'label_batch': 'Отзывы пакетом',
                'icon': 'message',
                'color': 'violet',
                'title_template': 'Отзывы: {product}',
            },
        }

        available = []
        orchestrator_id = None
        for a in agents:
            if a.name == 'orchestrator' and a.status == 'online':
                orchestrator_id = str(a.id)
            action = PRODUCT_ACTIONS.get(a.name)
            if action and a.status == 'online':
                available.append({
                    'agent_id': str(a.id),
                    'agent_name': a.name,
                    'display_name': a.display_name,
                    **action,
                })

        # Добавляем pipeline-пресеты если оркестратор online
        pipelines = []
        if orchestrator_id:
            pipelines = [
                {
                    'id': 'full_prepare',
                    'label': 'Подготовить к WB',
                    'description': 'Категория → Характеристики → SEO → Модерация',
                    'icon': 'rocket',
                    'color': 'brand',
                },
                {
                    'id': 'seo_boost',
                    'label': 'SEO тексты',
                    'description': 'SEO-оптимизация → Проверка модерации',
                    'icon': 'pen',
                    'color': 'emerald',
                },
                {
                    'id': 'audit',
                    'label': 'Аудит карточек',
                    'description': 'Модерация → Цены → Отзывы',
                    'icon': 'shield',
                    'color': 'amber',
                },
                {
                    'id': 'category_fix',
                    'label': 'Исправить категории',
                    'description': 'Категория → Характеристики',
                    'icon': 'tag',
                    'color': 'blue',
                },
            ]

        return jsonify({
            'actions': available,
            'pipelines': pipelines,
            'orchestrator_id': orchestrator_id,
        })

    # ── Действия ────────────────────────────────────────────────────

    @app.route('/agents/tasks/<task_id>/cancel', methods=['POST'])
    @login_required
    def agent_task_cancel(task_id):
        """Отменить задачу."""
        seller_id = _task_seller_scope()
        task = agent_service.get_task(task_id)
        if not task:
            abort(404)
        if seller_id is not None and task.seller_id != seller_id:
            abort(403)

        agent_service.cancel_task(task_id)
        flash('Задача отменена', 'info')
        return redirect(url_for('agents_dashboard'))

    @app.route('/agents/tasks/create', methods=['POST'])
    @login_required
    def agent_task_create():
        """Создать задачу для агента вручную."""
        fallback_url = request.form.get('redirect_url') or url_for('agents_dashboard')

        if not current_user.seller:
            flash('Нет профиля продавца', 'danger')
            return redirect(fallback_url)

        agent_id = request.form.get('agent_id')
        task_type = request.form.get('task_type', '')
        title = request.form.get('title', '')

        if not agent_id or not task_type:
            flash('Заполните все поля', 'warning')
            return redirect(fallback_url)

        agent = agent_service.get_agent(agent_id)
        if not agent:
            flash('Агент не найден', 'danger')
            return redirect(fallback_url)

        input_data = {}
        try:
            raw = request.form.get('input_data', '{}')
            if raw.strip():
                input_data = json.loads(raw)
        except json.JSONDecodeError:
            flash('Некорректный JSON во входных данных', 'warning')
            return redirect(fallback_url)

        task = agent_service.create_task(
            agent_id=agent_id,
            seller_id=current_user.seller.id,
            task_type=task_type,
            title=title,
            input_data=input_data,
        )
        flash(f'Задача создана: {task.id[:8]}', 'success')
        return redirect(url_for('agent_task_detail', task_id=task.id))

    # ── Откат изменений агента ───────────────────────────────────────

    @app.route('/agents/tasks/<task_id>/rollback', methods=['POST'])
    @login_required
    def agent_task_rollback(task_id):
        """Откатить все изменения, сделанные задачей агента."""
        seller_id = _task_seller_scope()
        task = agent_service.get_task(task_id)
        if not task:
            abort(404)
        if seller_id is not None and task.seller_id != seller_id:
            abort(403)

        try:
            result = agent_harness.rollback_task_tree(task_id, task.seller_id)
        except ValueError as exc:
            flash(str(exc), 'warning')
            return redirect(url_for('agent_task_detail', task_id=task_id))
        except Exception:
            logger.exception('Rollback failed for task %s', task_id)
            flash('Не удалось откатить изменения', 'danger')
            return redirect(url_for('agent_task_detail', task_id=task_id))

        if result.get('conflicts'):
            flash(
                f"Откат выполнен частично: {result['products']} товаров, "
                f"конфликтов ручных изменений: {result['conflicts']}",
                'warning',
            )
        elif not result['snapshots']:
            flash('Нет изменений для отката (или они уже откачены)', 'warning')
        else:
            flash(
                f"Откат выполнен: {result['products']} товаров, "
                f"{result['snapshots']} изменений",
                'success',
            )
        return redirect(url_for('agent_task_detail', task_id=task_id))

    @app.route('/agents/api/tasks/<task_id>/changes')
    @login_required
    def agents_api_task_changes(task_id):
        """API: получить историю изменений задачи (для отображения в UI)."""
        from models import AgentChangeSnapshot

        seller_id = _task_seller_scope()
        task = agent_service.get_task(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        if seller_id is not None and task.seller_id != seller_id:
            return jsonify({'error': 'Access denied'}), 403

        snapshots = AgentChangeSnapshot.query.filter_by(task_id=task_id).order_by(
            AgentChangeSnapshot.created_at
        ).all()

        return jsonify({
            'changes': [
                {
                    'id': s.id,
                    'product_id': s.imported_product_id,
                    'previous_values': json.loads(s.previous_values),
                    'new_values': json.loads(s.new_values),
                    'is_rolled_back': s.is_rolled_back,
                    'rolled_back_at': s.rolled_back_at.isoformat() if s.rolled_back_at else None,
                    'created_at': s.created_at.isoformat() if s.created_at else None,
                }
                for s in snapshots
            ],
            'count': len(snapshots),
            'has_rollback': any(not s.is_rolled_back for s in snapshots),
        })

    # ── Админ: управление агентами ──────────────────────────────────

    @app.route('/agents/admin/register', methods=['POST'])
    @login_required
    def agents_admin_register():
        """Зарегистрировать нового агента (только админ)."""
        if not current_user.is_admin:
            abort(403)

        name = request.form.get('name', '').strip()
        display_name = request.form.get('display_name', '').strip()
        agent_type = request.form.get('agent_type', 'external')
        endpoint_url = request.form.get('endpoint_url', '').strip()
        description = request.form.get('description', '').strip()
        category = request.form.get('category', 'general')
        icon = request.form.get('icon', 'cpu')
        color = request.form.get('color', 'blue')

        if not name or not display_name:
            flash('Имя и отображаемое имя обязательны', 'warning')
            return redirect(url_for('agents_dashboard'))

        agent = agent_service.register_agent(
            name=name,
            display_name=display_name,
            agent_type=agent_type,
            description=description,
            endpoint_url=endpoint_url,
            category=category,
            icon=icon,
            color=color,
        )

        # Генерируем API ключ
        raw_key = str(uuid.uuid4())
        agent.api_key_hash = generate_password_hash(raw_key)
        db.session.commit()

        env_prefix = name.upper().replace('-', '_')
        session['agent_credentials'] = {
            'display_name': display_name,
            'agent_name': name,
            'agent_id': str(agent.id),
            'agent_key': raw_key,
            'env_id_var': f'AGENT_{env_prefix}_ID',
            'env_key_var': f'AGENT_{env_prefix}_KEY',
        }
        return redirect(url_for('agents_dashboard'))

    @app.route('/agents/admin/seed/<agent_name>', methods=['POST'])
    @login_required
    def agents_admin_seed(agent_name):
        """Активировать агента из каталога (только админ)."""
        if not current_user.is_admin:
            abort(403)

        agent = agent_service.seed_agent(agent_name)
        if not agent:
            flash(f'Агент "{agent_name}" не найден в каталоге', 'danger')
            return redirect(url_for('agents_dashboard'))

        # Генерируем API ключ
        raw_key = str(uuid.uuid4())
        agent.api_key_hash = generate_password_hash(raw_key)
        db.session.commit()

        # Передаём credentials через session — шаблон покажет модалку
        env_prefix = agent_name.upper().replace('-', '_')
        session['agent_credentials'] = {
            'display_name': agent.display_name,
            'agent_name': agent_name,
            'agent_id': str(agent.id),
            'agent_key': raw_key,
            'env_id_var': f'AGENT_{env_prefix}_ID',
            'env_key_var': f'AGENT_{env_prefix}_KEY',
        }
        return redirect(url_for('agents_dashboard'))

    @app.route('/agents/admin/regenerate-key/<agent_id>', methods=['POST'])
    @login_required
    def agents_admin_regenerate_key(agent_id):
        """Перегенерировать API-ключ агента (только админ)."""
        if not current_user.is_admin:
            abort(403)

        agent = agent_service.get_agent(agent_id)
        if not agent:
            flash('Агент не найден', 'danger')
            return redirect(url_for('agents_dashboard'))

        # Генерируем новый API ключ
        raw_key = str(uuid.uuid4())
        agent.api_key_hash = generate_password_hash(raw_key)
        db.session.commit()

        env_prefix = agent.name.upper().replace('-', '_')
        session['agent_credentials'] = {
            'display_name': agent.display_name,
            'agent_name': agent.name,
            'agent_id': str(agent.id),
            'agent_key': raw_key,
            'env_id_var': f'AGENT_{env_prefix}_ID',
            'env_key_var': f'AGENT_{env_prefix}_KEY',
            'regenerated': True,
        }
        return redirect(url_for('agents_dashboard'))
