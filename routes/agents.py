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

from flask import render_template, request, redirect, url_for, flash, jsonify, abort
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash

from models import db, ServiceAgent, AgentTask, AgentTaskStep
from services import agent_service

logger = logging.getLogger(__name__)


def register_agents_routes(app):
    """Регистрирует маршруты дашборда агентов."""

    # ── Дашборд ─────────────────────────────────────────────────────

    @app.route('/agents')
    @login_required
    def agents_dashboard():
        """Главная страница агентов."""
        if not current_user.seller and not current_user.is_admin:
            flash('Нет доступа', 'danger')
            return redirect(url_for('dashboard'))

        agents = agent_service.list_agents()
        seller_id = current_user.seller.id if current_user.seller else None

        # Активные задачи
        active_tasks, _ = agent_service.list_tasks(
            seller_id=seller_id, status='active', limit=20
        )
        # Недавно завершённые
        recent_tasks, _ = agent_service.list_tasks(
            seller_id=seller_id, status='completed', limit=10
        )
        # Ошибки
        failed_tasks, _ = agent_service.list_tasks(
            seller_id=seller_id, status='failed', limit=10
        )

        stats = agent_service.get_agent_stats(seller_id=seller_id, days=7)

        return render_template('agents.html',
            agents=agents,
            active_tasks=active_tasks,
            recent_tasks=recent_tasks,
            failed_tasks=failed_tasks,
            stats=stats,
        )

    # ── Детали задачи ───────────────────────────────────────────────

    @app.route('/agents/tasks/<task_id>')
    @login_required
    def agent_task_detail(task_id):
        """Детальный просмотр задачи со всеми шагами."""
        task = agent_service.get_task(task_id)
        if not task:
            abort(404)

        seller_id = current_user.seller.id if current_user.seller else None
        if seller_id and task.seller_id != seller_id and not current_user.is_admin:
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
        seller_id = current_user.seller.id if current_user.seller else None
        status = request.args.get('status')
        agent_id = request.args.get('agent_id')
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)

        tasks, total = agent_service.list_tasks(
            seller_id=seller_id if not current_user.is_admin else None,
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
        task = agent_service.get_task(task_id)
        if not task:
            return jsonify({'error': 'Not found'}), 404
        steps = agent_service.get_task_steps(task_id, limit=50)
        return jsonify({
            'task': task.to_dict(),
            'steps': [s.to_dict() for s in steps],
        })

    @app.route('/agents/api/stats')
    @login_required
    def agents_api_stats():
        """API: статистика агентов."""
        seller_id = current_user.seller.id if current_user.seller else None
        days = request.args.get('days', 7, type=int)
        stats = agent_service.get_agent_stats(
            seller_id=seller_id if not current_user.is_admin else None,
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

    # ── Действия ────────────────────────────────────────────────────

    @app.route('/agents/tasks/<task_id>/cancel', methods=['POST'])
    @login_required
    def agent_task_cancel(task_id):
        """Отменить задачу."""
        task = agent_service.get_task(task_id)
        if not task:
            abort(404)
        seller_id = current_user.seller.id if current_user.seller else None
        if seller_id and task.seller_id != seller_id and not current_user.is_admin:
            abort(403)

        agent_service.cancel_task(task_id)
        flash('Задача отменена', 'info')
        return redirect(url_for('agents_dashboard'))

    @app.route('/agents/tasks/create', methods=['POST'])
    @login_required
    def agent_task_create():
        """Создать задачу для агента вручную."""
        if not current_user.seller:
            flash('Нет профиля продавца', 'danger')
            return redirect(url_for('agents_dashboard'))

        agent_id = request.form.get('agent_id')
        task_type = request.form.get('task_type', '')
        title = request.form.get('title', '')

        if not agent_id or not task_type or not title:
            flash('Заполните все поля', 'warning')
            return redirect(url_for('agents_dashboard'))

        agent = agent_service.get_agent(agent_id)
        if not agent:
            flash('Агент не найден', 'danger')
            return redirect(url_for('agents_dashboard'))

        input_data = {}
        try:
            raw = request.form.get('input_data', '{}')
            if raw.strip():
                input_data = json.loads(raw)
        except json.JSONDecodeError:
            flash('Некорректный JSON во входных данных', 'warning')
            return redirect(url_for('agents_dashboard'))

        task = agent_service.create_task(
            agent_id=agent_id,
            seller_id=current_user.seller.id,
            task_type=task_type,
            title=title,
            input_data=input_data,
        )
        flash(f'Задача создана: {task.id[:8]}', 'success')
        return redirect(url_for('agent_task_detail', task_id=task.id))

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

        if not name or not display_name:
            flash('Имя и отображаемое имя обязательны', 'warning')
            return redirect(url_for('agents_dashboard'))

        agent = agent_service.register_agent(
            name=name,
            display_name=display_name,
            agent_type=agent_type,
            description=description,
            endpoint_url=endpoint_url,
        )

        # Генерируем API ключ
        raw_key = str(uuid.uuid4())
        agent.api_key_hash = generate_password_hash(raw_key)
        db.session.commit()

        flash(f'Агент зарегистрирован. API ключ (сохраните!): {raw_key}', 'success')
        return redirect(url_for('agents_dashboard'))
