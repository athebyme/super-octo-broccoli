# -*- coding: utf-8 -*-
"""
Роуты для раздела документации
"""
import os
import re
from pathlib import Path

from flask import Blueprint, render_template, abort
from flask_login import login_required

docs_bp = Blueprint('docs', __name__, url_prefix='/docs')

# Путь к папке с документацией
DOCS_DIR = Path(__file__).resolve().parent.parent / 'docs'

# Метаданные документов: slug -> {title, icon, category, order, description}
DOCS_REGISTRY = {
    'CATEGORY_INSTRUCTIONS': {
        'title': 'Категории товаров',
        'description': 'Инструкции по работе с категориями, маппинг на маркетплейсы, справочник WB-категорий',
        'icon': 'tag',
        'category': 'Каталог',
        'order': 1,
    },
    'AUTO_IMPORT_README': {
        'title': 'Автоимпорт товаров',
        'description': 'Настройка и использование системы автоимпорта из CSV/поставщиков',
        'icon': 'upload',
        'category': 'Инструменты',
        'order': 2,
    },
    'WB_API_SETUP': {
        'title': 'Настройка WB API',
        'description': 'Подключение к Wildberries API, получение ключей, конфигурация',
        'icon': 'key',
        'category': 'Интеграции',
        'order': 3,
    },
    'WILDBERRIES_API_ANALYSIS': {
        'title': 'Анализ WB API',
        'description': 'Детальный разбор эндпоинтов и возможностей Wildberries API',
        'icon': 'code',
        'category': 'Интеграции',
        'order': 4,
    },
    'WB_API_EDIT_LIMITATIONS': {
        'title': 'Ограничения WB API',
        'description': 'Известные лимиты и ограничения при работе с WB API',
        'icon': 'alert-triangle',
        'category': 'Интеграции',
        'order': 5,
    },
    'INTEGRATION_GUIDE': {
        'title': 'Руководство интеграции',
        'description': 'Подключение внешних систем и сервисов к платформе',
        'icon': 'link',
        'category': 'Интеграции',
        'order': 6,
    },
    'MERGE_CARDS_READY': {
        'title': 'Объединение карточек',
        'description': 'Функциональность объединения и разъединения карточек товаров',
        'icon': 'layers',
        'category': 'Инструменты',
        'order': 7,
    },
    'MERGE_IMPROVEMENTS': {
        'title': 'Улучшения объединения',
        'description': 'Последние обновления и улучшения механизма объединения карточек',
        'icon': 'git-merge',
        'category': 'Инструменты',
        'order': 8,
    },
    'BULK_OPERATIONS_OPTIMIZATION': {
        'title': 'Массовые операции',
        'description': 'Оптимизация пакетных операций над товарами',
        'icon': 'zap',
        'category': 'Инструменты',
        'order': 9,
    },
    'PLATFORM_README': {
        'title': 'О платформе',
        'description': 'Обзор платформы, архитектура и основные возможности',
        'icon': 'info',
        'category': 'Общее',
        'order': 10,
    },
    'QUICKSTART': {
        'title': 'Быстрый старт',
        'description': 'Минимальные шаги для начала работы с платформой',
        'icon': 'play',
        'category': 'Общее',
        'order': 11,
    },
    'DOCKER_QUICKSTART': {
        'title': 'Docker: быстрый старт',
        'description': 'Запуск платформы в Docker-контейнерах',
        'icon': 'box',
        'category': 'Деплой',
        'order': 12,
    },
    'DOCKER_DATA_PERSISTENCE': {
        'title': 'Docker: данные',
        'description': 'Настройка постоянного хранилища данных для Docker',
        'icon': 'database',
        'category': 'Деплой',
        'order': 13,
    },
    'DATABASE_PERSISTENCE': {
        'title': 'Хранение данных',
        'description': 'Управление базой данных, бэкапы и восстановление',
        'icon': 'hard-drive',
        'category': 'Деплой',
        'order': 14,
    },
    'MIGRATION_GUIDE': {
        'title': 'Миграции БД',
        'description': 'Руководство по применению миграций базы данных',
        'icon': 'refresh-cw',
        'category': 'Деплой',
        'order': 15,
    },
    'MIGRATION_INSTRUCTIONS': {
        'title': 'Инструкции миграции',
        'description': 'Пошаговые инструкции по обновлению структуры БД',
        'icon': 'list',
        'category': 'Деплой',
        'order': 16,
    },
    'PLAN': {
        'title': 'План разработки',
        'description': 'Текущий план и роадмап развития платформы',
        'icon': 'map',
        'category': 'Разработка',
        'order': 17,
    },
}


def _simple_md_to_html(md_text: str) -> str:
    """
    Простейшая конвертация Markdown → HTML
    Превращает заголовки, списки, code-блоки, жирный/курсив, таблицы, ссылки.
    """
    lines = md_text.split('\n')
    html_lines = []
    in_code_block = False
    code_lang = ''
    in_table = False
    in_list = False
    in_details = False

    for line in lines:
        # Блоки кода
        if line.strip().startswith('```'):
            if in_code_block:
                html_lines.append('</code></pre>')
                in_code_block = False
            else:
                code_lang = line.strip()[3:].strip()
                lang_class = f' class="language-{code_lang}"' if code_lang else ''
                html_lines.append(f'<pre class="code-block"><code{lang_class}>')
                in_code_block = True
            continue

        if in_code_block:
            # Экранируем HTML внутри блоков кода
            escaped = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            html_lines.append(escaped)
            continue

        # <details> и <summary> — пропускаем как есть
        stripped = line.strip()
        if stripped.startswith('<details') or stripped == '</details>':
            if stripped.startswith('<details'):
                in_details = True
            else:
                in_details = False
            html_lines.append(line)
            continue
        if stripped.startswith('<summary') or stripped == '</summary>':
            html_lines.append(line)
            continue

        # Таблицы
        if '|' in stripped and stripped.startswith('|') and stripped.endswith('|'):
            # Разделительная строка таблицы (|---|---|)
            if re.match(r'^\|[\s\-:|]+\|$', stripped):
                continue

            cells = [c.strip() for c in stripped.split('|')[1:-1]]
            if not in_table:
                html_lines.append('<div class="table-wrapper"><table>')
                # Первая строка — заголовок
                html_lines.append('<thead><tr>')
                for cell in cells:
                    html_lines.append(f'<th>{_inline_format(cell)}</th>')
                html_lines.append('</tr></thead><tbody>')
                in_table = True
            else:
                html_lines.append('<tr>')
                for cell in cells:
                    html_lines.append(f'<td>{_inline_format(cell)}</td>')
                html_lines.append('</tr>')
            continue
        elif in_table:
            html_lines.append('</tbody></table></div>')
            in_table = False

        # Пустая строка
        if not stripped:
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            html_lines.append('')
            continue

        # Заголовки
        heading_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)
        if heading_match:
            if in_list:
                html_lines.append('</ul>')
                in_list = False
            level = len(heading_match.group(1))
            text = _inline_format(heading_match.group(2))
            slug = re.sub(r'[^\w\s-]', '', heading_match.group(2).lower())
            slug = re.sub(r'[\s]+', '-', slug).strip('-')
            html_lines.append(f'<h{level} id="{slug}">{text}</h{level}>')
            continue

        # Горизонтальная линия
        if re.match(r'^---+\s*$', stripped):
            html_lines.append('<hr>')
            continue

        # Blockquote с предупреждениями
        if stripped.startswith('> **ВАЖНО:**') or stripped.startswith('> **'):
            text = stripped[2:].strip()
            html_lines.append(f'<div class="callout callout-warning">{_inline_format(text)}</div>')
            continue
        if stripped.startswith('>'):
            text = stripped[1:].strip()
            html_lines.append(f'<blockquote>{_inline_format(text)}</blockquote>')
            continue

        # Чекбоксы
        checkbox_match = re.match(r'^-\s*\[([ xX/])\]\s+(.+)$', stripped)
        if checkbox_match:
            if not in_list:
                html_lines.append('<ul class="checklist">')
                in_list = True
            state = checkbox_match.group(1)
            text = _inline_format(checkbox_match.group(2))
            if state in ('x', 'X'):
                html_lines.append(f'<li class="checked">✅ {text}</li>')
            elif state == '/':
                html_lines.append(f'<li class="in-progress">🔄 {text}</li>')
            else:
                html_lines.append(f'<li class="unchecked">⬜ {text}</li>')
            continue

        # Маркированные списки
        list_match = re.match(r'^[-*]\s+(.+)$', stripped)
        if list_match:
            if not in_list:
                html_lines.append('<ul>')
                in_list = True
            text = _inline_format(list_match.group(1))
            html_lines.append(f'<li>{text}</li>')
            continue

        # Нумерованные списки
        ol_match = re.match(r'^\d+\.\s+(.+)$', stripped)
        if ol_match:
            text = _inline_format(ol_match.group(1))
            html_lines.append(f'<li class="ol-item">{text}</li>')
            continue

        # Обычный абзац
        if in_list:
            html_lines.append('</ul>')
            in_list = False
        html_lines.append(f'<p>{_inline_format(stripped)}</p>')

    # Закрываем открытые теги
    if in_code_block:
        html_lines.append('</code></pre>')
    if in_table:
        html_lines.append('</tbody></table></div>')
    if in_list:
        html_lines.append('</ul>')

    return '\n'.join(html_lines)


def _inline_format(text: str) -> str:
    """Форматирование инлайновых элементов: жирный, курсив, код, ссылки"""
    # Код (backticks)
    text = re.sub(r'`([^`]+)`', r'<code class="inline-code">\1</code>', text)
    # Жирный + курсив
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<strong><em>\1</em></strong>', text)
    # Жирный
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Курсив
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    # Ссылки
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    return text


# ==================== WEB ROUTES ====================

@docs_bp.route('/')
@login_required
def docs_index():
    """Главная страница документации"""
    # Группируем документы по категориям
    categories = {}
    for slug, meta in sorted(DOCS_REGISTRY.items(), key=lambda x: x[1]['order']):
        filepath = DOCS_DIR / f'{slug}.md'
        if not filepath.exists():
            continue
        cat = meta['category']
        if cat not in categories:
            categories[cat] = []
        categories[cat].append({
            'slug': slug,
            'title': meta['title'],
            'description': meta['description'],
            'icon': meta['icon'],
            'file_size': filepath.stat().st_size,
        })

    return render_template('docs_index.html', categories=categories)


@docs_bp.route('/<slug>')
@login_required
def docs_view(slug):
    """Просмотр документа"""
    meta = DOCS_REGISTRY.get(slug)
    if not meta:
        abort(404)

    filepath = DOCS_DIR / f'{slug}.md'
    if not filepath.exists():
        abort(404)

    md_content = filepath.read_text(encoding='utf-8')
    html_content = _simple_md_to_html(md_content)

    # Собираем оглавление (TOC)
    toc = []
    for match in re.finditer(r'^(#{1,3})\s+(.+)$', md_content, re.MULTILINE):
        level = len(match.group(1))
        text = match.group(2).strip()
        anchor = re.sub(r'[^\w\s-]', '', text.lower())
        anchor = re.sub(r'[\s]+', '-', anchor).strip('-')
        toc.append({'level': level, 'text': text, 'anchor': anchor})

    # Навигация (предыдущий/следующий)
    sorted_docs = sorted(DOCS_REGISTRY.items(), key=lambda x: x[1]['order'])
    current_idx = next((i for i, (s, _) in enumerate(sorted_docs) if s == slug), None)
    prev_doc = None
    next_doc = None
    if current_idx is not None and current_idx > 0:
        ps, pm = sorted_docs[current_idx - 1]
        prev_doc = {'slug': ps, 'title': pm['title']}
    if current_idx is not None and current_idx < len(sorted_docs) - 1:
        ns, nm = sorted_docs[current_idx + 1]
        next_doc = {'slug': ns, 'title': nm['title']}

    return render_template(
        'docs_view.html',
        meta=meta,
        slug=slug,
        html_content=html_content,
        toc=toc,
        prev_doc=prev_doc,
        next_doc=next_doc,
    )


def register_docs_routes(app):
    """Зарегистрировать blueprint в приложении"""
    app.register_blueprint(docs_bp)
