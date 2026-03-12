# -*- coding: utf-8 -*-
"""
Сервис управления агентами.

Предоставляет бизнес-логику для:
- Регистрации / обновления агентов
- Создания и управления задачами
- Логирования шагов выполнения
- Heartbeat и мониторинг состояния
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from models import db, ServiceAgent, AgentTask, AgentTaskStep, Notification

logger = logging.getLogger(__name__)


# ── Агенты ──────────────────────────────────────────────────────────

def register_agent(
    name: str,
    display_name: str,
    agent_type: str = 'external',
    description: str = '',
    endpoint_url: str = '',
    version: str = '',
    capabilities: list = None,
    config: dict = None,
) -> ServiceAgent:
    """Регистрирует нового агента или обновляет существующего."""
    agent = ServiceAgent.query.filter_by(name=name).first()
    if agent:
        agent.display_name = display_name
        agent.description = description
        agent.agent_type = agent_type
        agent.endpoint_url = endpoint_url
        agent.version = version
        agent.capabilities = json.dumps(capabilities or [], ensure_ascii=False)
        agent.config_json = json.dumps(config or {}, ensure_ascii=False)
        agent.updated_at = datetime.utcnow()
    else:
        agent = ServiceAgent(
            id=str(uuid.uuid4()),
            name=name,
            display_name=display_name,
            description=description,
            agent_type=agent_type,
            endpoint_url=endpoint_url,
            version=version,
            capabilities=json.dumps(capabilities or [], ensure_ascii=False),
            config_json=json.dumps(config or {}, ensure_ascii=False),
            status='offline',
        )
        db.session.add(agent)

    db.session.commit()
    logger.info(f"Agent registered: {name} ({agent_type})")
    return agent


def heartbeat(agent_id: str, status: str = 'online', error: str = None) -> Optional[ServiceAgent]:
    """Обновляет heartbeat агента."""
    agent = ServiceAgent.query.get(agent_id)
    if not agent:
        return None

    agent.last_heartbeat = datetime.utcnow()
    agent.status = status
    agent.last_error = error
    db.session.commit()
    return agent


def get_agent(agent_id: str) -> Optional[ServiceAgent]:
    return ServiceAgent.query.get(agent_id)


def get_agent_by_name(name: str) -> Optional[ServiceAgent]:
    return ServiceAgent.query.filter_by(name=name).first()


def list_agents(status: str = None) -> list:
    q = ServiceAgent.query
    if status:
        q = q.filter_by(status=status)
    return q.order_by(ServiceAgent.name).all()


def mark_stale_agents(timeout_seconds: int = 120):
    """Помечает агентов как offline, если heartbeat устарел."""
    threshold = datetime.utcnow() - timedelta(seconds=timeout_seconds)
    stale = ServiceAgent.query.filter(
        ServiceAgent.status == 'online',
        ServiceAgent.last_heartbeat < threshold,
    ).all()
    for agent in stale:
        agent.status = 'offline'
        logger.warning(f"Agent {agent.name} marked offline (no heartbeat)")
    if stale:
        db.session.commit()
    return len(stale)


# ── Задачи ──────────────────────────────────────────────────────────

def create_task(
    agent_id: str,
    seller_id: int,
    task_type: str,
    title: str,
    input_data: dict = None,
    priority: int = 0,
    total_steps: int = 0,
) -> AgentTask:
    """Создаёт новую задачу для агента."""
    task = AgentTask(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        seller_id=seller_id,
        task_type=task_type,
        title=title,
        status='queued',
        priority=priority,
        total_steps=total_steps,
        input_data=json.dumps(input_data or {}, ensure_ascii=False),
    )
    db.session.add(task)
    db.session.commit()
    logger.info(f"Task created: {task.id[:8]} [{task_type}] for agent {agent_id[:8]}")
    return task


def start_task(task_id: str) -> Optional[AgentTask]:
    """Помечает задачу как запущенную."""
    task = AgentTask.query.get(task_id)
    if not task or task.status != 'queued':
        return None
    task.status = 'running'
    task.started_at = datetime.utcnow()
    db.session.commit()
    return task


def update_task_progress(
    task_id: str,
    completed_steps: int,
    current_step_label: str = None,
    total_steps: int = None,
) -> Optional[AgentTask]:
    """Обновляет прогресс задачи."""
    task = AgentTask.query.get(task_id)
    if not task:
        return None
    task.completed_steps = completed_steps
    if current_step_label is not None:
        task.current_step_label = current_step_label
    if total_steps is not None:
        task.total_steps = total_steps
    task.updated_at = datetime.utcnow()
    db.session.commit()
    return task


def complete_task(task_id: str, result_data: dict = None) -> Optional[AgentTask]:
    """Завершает задачу успешно."""
    task = AgentTask.query.get(task_id)
    if not task:
        return None
    task.status = 'completed'
    task.completed_at = datetime.utcnow()
    task.result_data = json.dumps(result_data or {}, ensure_ascii=False)
    if task.total_steps:
        task.completed_steps = task.total_steps
    db.session.commit()

    # Уведомление продавцу
    _notify_seller(task, 'success', f'Задача завершена: {task.title}')
    return task


def fail_task(task_id: str, error_message: str, result_data: dict = None) -> Optional[AgentTask]:
    """Помечает задачу как проваленную."""
    task = AgentTask.query.get(task_id)
    if not task:
        return None
    task.status = 'failed'
    task.completed_at = datetime.utcnow()
    task.error_message = error_message
    if result_data:
        task.result_data = json.dumps(result_data, ensure_ascii=False)
    db.session.commit()

    _notify_seller(task, 'error', f'Ошибка агента: {task.title}')
    return task


def cancel_task(task_id: str) -> Optional[AgentTask]:
    """Отменяет задачу."""
    task = AgentTask.query.get(task_id)
    if not task or task.status in ('completed', 'failed', 'cancelled'):
        return None
    task.status = 'cancelled'
    task.completed_at = datetime.utcnow()
    db.session.commit()
    return task


def get_task(task_id: str) -> Optional[AgentTask]:
    return AgentTask.query.get(task_id)


def list_tasks(
    seller_id: int = None,
    agent_id: str = None,
    status: str = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple:
    """Возвращает (tasks, total_count)."""
    q = AgentTask.query
    if seller_id:
        q = q.filter_by(seller_id=seller_id)
    if agent_id:
        q = q.filter_by(agent_id=agent_id)
    if status:
        if status == 'active':
            q = q.filter(AgentTask.status.in_(['queued', 'running']))
        else:
            q = q.filter_by(status=status)
    total = q.count()
    tasks = q.order_by(AgentTask.created_at.desc()).offset(offset).limit(limit).all()
    return tasks, total


def get_pending_tasks(agent_id: str, limit: int = 10) -> list:
    """Получает очередь задач для агента (FIFO по приоритету)."""
    return AgentTask.query.filter_by(
        agent_id=agent_id, status='queued'
    ).order_by(
        AgentTask.priority.desc(),
        AgentTask.created_at.asc(),
    ).limit(limit).all()


# ── Шаги задач ──────────────────────────────────────────────────────

def add_task_step(
    task_id: str,
    step_type: str,
    title: str,
    detail: str = None,
    status: str = 'completed',
    duration_ms: int = None,
    metadata: dict = None,
) -> Optional[AgentTaskStep]:
    """Добавляет шаг выполнения задачи."""
    task = AgentTask.query.get(task_id)
    if not task:
        return None

    last_step = AgentTaskStep.query.filter_by(task_id=task_id).order_by(
        AgentTaskStep.step_number.desc()
    ).first()
    step_number = (last_step.step_number + 1) if last_step else 1

    step = AgentTaskStep(
        task_id=task_id,
        step_number=step_number,
        step_type=step_type,
        title=title,
        detail=detail,
        status=status,
        duration_ms=duration_ms,
        metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
    )
    db.session.add(step)
    db.session.commit()
    return step


def get_task_steps(task_id: str, limit: int = 100) -> list:
    """Получает шаги задачи."""
    return AgentTaskStep.query.filter_by(task_id=task_id).order_by(
        AgentTaskStep.step_number.asc()
    ).limit(limit).all()


# ── Статистика ──────────────────────────────────────────────────────

def get_agent_stats(agent_id: str = None, seller_id: int = None, days: int = 7) -> dict:
    """Статистика по задачам агентов."""
    since = datetime.utcnow() - timedelta(days=days)
    q = AgentTask.query.filter(AgentTask.created_at >= since)
    if agent_id:
        q = q.filter_by(agent_id=agent_id)
    if seller_id:
        q = q.filter_by(seller_id=seller_id)

    tasks = q.all()
    total = len(tasks)
    completed = sum(1 for t in tasks if t.status == 'completed')
    failed = sum(1 for t in tasks if t.status == 'failed')
    running = sum(1 for t in tasks if t.status == 'running')
    queued = sum(1 for t in tasks if t.status == 'queued')

    avg_duration = 0
    completed_tasks = [t for t in tasks if t.status == 'completed' and t.duration_seconds > 0]
    if completed_tasks:
        avg_duration = sum(t.duration_seconds for t in completed_tasks) / len(completed_tasks)

    return {
        'total': total,
        'completed': completed,
        'failed': failed,
        'running': running,
        'queued': queued,
        'success_rate': round(completed / total * 100, 1) if total > 0 else 0,
        'avg_duration_seconds': round(avg_duration),
        'period_days': days,
    }


# ── Внутренние ──────────────────────────────────────────────────────

def _notify_seller(task: AgentTask, category: str, title: str):
    """Создаёт уведомление для продавца о результате задачи."""
    try:
        notif = Notification(
            seller_id=task.seller_id,
            category=category,
            title=title,
            message=task.error_message if category == 'error' else f'Агент завершил задачу за {task.duration_seconds}с',
            link=f'/agents/tasks/{task.id}',
        )
        db.session.add(notif)
        db.session.commit()
    except Exception as e:
        logger.error(f"Failed to create agent notification: {e}")
