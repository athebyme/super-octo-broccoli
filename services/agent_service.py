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


# ── Каталог типов агентов ───────────────────────────────────────────

AGENT_CATALOG = [
    # ── Каталог и импорт ──
    {
        'name': 'category-mapper',
        'display_name': 'Агент категорий',
        'description': 'Определяет правильную категорию WB для товара, анализируя название, описание и характеристики. Подбирает subjectID по справочнику маркетплейса.',
        'category': 'catalog',
        'icon': 'tag',
        'color': 'blue',
        'capabilities': ['map_category', 'suggest_subject', 'validate_category', 'analyze_parent_tree'],
        'task_types': ['map_single', 'map_batch', 'remap_incorrect'],
    },
    {
        'name': 'size-normalizer',
        'display_name': 'Агент размеров',
        'description': 'Нормализует размеры, габариты и вес из данных поставщика в формат WB. Парсит строки типа "42-44 RU", конвертирует единицы измерения, заполняет размерные сетки.',
        'category': 'catalog',
        'icon': 'ruler',
        'color': 'cyan',
        'capabilities': ['parse_sizes', 'convert_units', 'build_size_grid', 'validate_dimensions'],
        'task_types': ['normalize_single', 'normalize_batch', 'fill_size_grid'],
    },
    {
        'name': 'auto-importer',
        'display_name': 'Агент импорта',
        'description': 'Полный цикл импорта товаров поставщика: парсинг каталога, маппинг категорий, обогащение данных, загрузка фото, создание карточки на WB.',
        'category': 'catalog',
        'icon': 'download',
        'color': 'violet',
        'capabilities': ['parse_csv', 'parse_xlsx', 'download_photos', 'create_wb_card', 'orchestrate_pipeline'],
        'task_types': ['import_batch', 'import_single', 'reimport_failed'],
    },
    {
        'name': 'characteristics-filler',
        'display_name': 'Агент характеристик',
        'description': 'Заполняет обязательные и рекомендованные характеристики карточки по справочнику WB. Извлекает данные из описания поставщика, подбирает значения из словарей.',
        'category': 'catalog',
        'icon': 'list',
        'color': 'indigo',
        'capabilities': ['extract_characteristics', 'match_dictionary', 'fill_required', 'validate_charcs'],
        'task_types': ['fill_single', 'fill_batch', 'validate_existing'],
    },

    # ── Контент и SEO ──
    {
        'name': 'seo-writer',
        'display_name': 'Агент SEO',
        'description': 'Генерирует SEO-оптимизированные заголовки и описания для карточек WB. Учитывает ключевые слова, ограничения по длине и требования маркетплейса.',
        'category': 'content',
        'icon': 'pen',
        'color': 'emerald',
        'capabilities': ['generate_title', 'generate_description', 'extract_keywords', 'optimize_seo'],
        'task_types': ['seo_single', 'seo_batch', 'rewrite_titles'],
    },
    {
        'name': 'photo-optimizer',
        'display_name': 'Агент фото',
        'description': 'Анализирует и оптимизирует фотографии товаров: проверка качества, обрезка белого фона, сортировка по релевантности, подготовка к загрузке на WB.',
        'category': 'content',
        'icon': 'camera',
        'color': 'pink',
        'capabilities': ['analyze_photos', 'check_quality', 'crop_background', 'sort_by_relevance'],
        'task_types': ['optimize_photos', 'replace_bad_photos', 'generate_infographics'],
    },
    {
        'name': 'brand-resolver',
        'display_name': 'Агент брендов',
        'description': 'Распознаёт и нормализует бренды: сопоставляет вариации написания, проверяет наличие в реестре WB, предлагает корректный бренд для карточки.',
        'category': 'content',
        'icon': 'badge',
        'color': 'amber',
        'capabilities': ['resolve_brand', 'match_aliases', 'validate_wb_brand', 'suggest_brand'],
        'task_types': ['resolve_single', 'resolve_batch', 'audit_brands'],
    },

    # ── Ценообразование ──
    {
        'name': 'price-optimizer',
        'display_name': 'Агент цен',
        'description': 'Оптимизирует цены: расчёт себестоимости с учётом логистики WB, анализ маржинальности, обнаружение аномалий, рекомендации по корректировке.',
        'category': 'pricing',
        'icon': 'chart',
        'color': 'emerald',
        'capabilities': ['calculate_cost', 'analyze_margin', 'detect_anomaly', 'suggest_price'],
        'task_types': ['optimize_prices', 'margin_audit', 'anomaly_scan'],
    },

    # ── Модерация и compliance ──
    {
        'name': 'card-doctor',
        'display_name': 'Агент модерации',
        'description': 'Диагностирует причины блокировки и скрытия карточек. Анализирует ошибки, предлагает исправления, проверяет на стоп-слова и нарушения правил WB.',
        'category': 'compliance',
        'icon': 'shield',
        'color': 'red',
        'capabilities': ['diagnose_block', 'check_prohibited_words', 'fix_violations', 'verify_compliance'],
        'task_types': ['diagnose_single', 'diagnose_batch', 'preventive_scan'],
    },

    # ── Аналитика ──
    {
        'name': 'review-analyst',
        'display_name': 'Агент отзывов',
        'description': 'Анализирует отзывы покупателей: выявляет тренды, классифицирует проблемы, генерирует рекомендации по улучшению товара и карточки.',
        'category': 'analytics',
        'icon': 'message',
        'color': 'violet',
        'capabilities': ['analyze_sentiment', 'classify_issues', 'extract_insights', 'suggest_improvements'],
        'task_types': ['analyze_reviews', 'weekly_report', 'product_insights'],
    },
]


def get_agent_catalog():
    """Возвращает каталог доступных типов агентов, сгруппированный по категориям."""
    categories = {
        'catalog': {'label': 'Каталог и импорт', 'icon': 'download', 'agents': []},
        'content': {'label': 'Контент и SEO', 'icon': 'pen', 'agents': []},
        'pricing': {'label': 'Ценообразование', 'icon': 'chart', 'agents': []},
        'compliance': {'label': 'Модерация', 'icon': 'shield', 'agents': []},
        'analytics': {'label': 'Аналитика', 'icon': 'chart', 'agents': []},
    }
    # Подтягиваем актуальный статус из БД
    registered = {a.name: a for a in ServiceAgent.query.all()}

    for spec in AGENT_CATALOG:
        entry = dict(spec)
        db_agent = registered.get(spec['name'])
        if db_agent:
            entry['registered'] = True
            entry['id'] = db_agent.id
            entry['status'] = db_agent.status
            entry['is_online'] = db_agent.is_online()
            entry['version'] = db_agent.version
            entry['last_heartbeat'] = db_agent.last_heartbeat
        else:
            entry['registered'] = False
            entry['id'] = None
            entry['status'] = 'not_registered'
            entry['is_online'] = False
            entry['version'] = None
            entry['last_heartbeat'] = None

        cat = spec.get('category', 'general')
        if cat in categories:
            categories[cat]['agents'].append(entry)

    return categories


def seed_agent(name: str) -> Optional[ServiceAgent]:
    """Регистрирует агент из каталога по имени."""
    spec = None
    for s in AGENT_CATALOG:
        if s['name'] == name:
            spec = s
            break
    if not spec:
        return None

    return register_agent(
        name=spec['name'],
        display_name=spec['display_name'],
        description=spec['description'],
        agent_type='external',
        capabilities=spec.get('capabilities', []),
        config={'task_types': spec.get('task_types', [])},
        category=spec.get('category', 'general'),
        icon=spec.get('icon', 'cpu'),
        color=spec.get('color', 'blue'),
    )


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
    category: str = 'general',
    icon: str = 'cpu',
    color: str = 'blue',
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
        agent.category = category
        agent.icon = icon
        agent.color = color
        task_types = (config or {}).get('task_types', [])
        if task_types:
            agent.task_types = json.dumps(task_types, ensure_ascii=False)
        agent.updated_at = datetime.utcnow()
    else:
        task_types = (config or {}).get('task_types', [])
        agent = ServiceAgent(
            id=str(uuid.uuid4()),
            name=name,
            display_name=display_name,
            description=description,
            category=category,
            icon=icon,
            color=color,
            task_types=json.dumps(task_types, ensure_ascii=False),
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
