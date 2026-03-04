# -*- coding: utf-8 -*-
"""
Роуты для управления запрещёнными словами WB.

- Админ: управляет глобальным словарём (для всех продавцов)
- Продавец: управляет своим персональным словарём
"""
import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user

from models import db, ProhibitedWord
from services.prohibited_words_filter import (
    PROHIBITED_WORDS_REPLACEMENTS,
    invalidate_filter_cache,
    get_prohibited_words_filter,
)

logger = logging.getLogger(__name__)

prohibited_words_bp = Blueprint('prohibited_words', __name__)


# ============================================================================
# Админ: глобальные запрещённые слова
# ============================================================================

@prohibited_words_bp.route('/admin/prohibited-words')
@login_required
def admin_index():
    """Список глобальных запрещённых слов."""
    if not current_user.is_admin:
        flash('Доступ запрещён', 'error')
        return redirect(url_for('index'))

    # Слова из БД
    db_words = ProhibitedWord.query.filter_by(scope='global').order_by(ProhibitedWord.word).all()

    # Дефолтные слова (из кода), которых нет в БД
    db_word_set = {w.word.lower() for w in db_words}
    default_words = [
        {'word': word, 'replacement': repl}
        for word, repl in sorted(PROHIBITED_WORDS_REPLACEMENTS.items())
        if word.lower() not in db_word_set
    ]

    return render_template(
        'admin_prohibited_words.html',
        db_words=db_words,
        default_words=default_words,
        default_count=len(PROHIBITED_WORDS_REPLACEMENTS),
    )


@prohibited_words_bp.route('/admin/prohibited-words/add', methods=['POST'])
@login_required
def admin_add():
    """Добавить глобальное запрещённое слово."""
    if not current_user.is_admin:
        return jsonify({'error': 'Доступ запрещён'}), 403

    word = request.form.get('word', '').strip().lower()
    replacement = request.form.get('replacement', '').strip()

    if not word:
        flash('Слово не может быть пустым', 'error')
        return redirect(url_for('prohibited_words.admin_index'))

    existing = ProhibitedWord.query.filter_by(word=word, scope='global').first()
    if existing:
        flash(f'Слово "{word}" уже существует', 'warning')
        return redirect(url_for('prohibited_words.admin_index'))

    pw = ProhibitedWord(
        word=word,
        replacement=replacement,
        scope='global',
        created_by_user_id=current_user.id,
    )
    db.session.add(pw)
    db.session.commit()

    invalidate_filter_cache()
    flash(f'Слово "{word}" добавлено', 'success')
    return redirect(url_for('prohibited_words.admin_index'))


@prohibited_words_bp.route('/admin/prohibited-words/<int:word_id>/edit', methods=['POST'])
@login_required
def admin_edit(word_id):
    """Редактировать глобальное запрещённое слово."""
    if not current_user.is_admin:
        return jsonify({'error': 'Доступ запрещён'}), 403

    pw = ProhibitedWord.query.get_or_404(word_id)
    pw.word = request.form.get('word', pw.word).strip().lower()
    pw.replacement = request.form.get('replacement', '').strip()
    pw.is_active = request.form.get('is_active') == 'on'
    db.session.commit()

    invalidate_filter_cache()
    flash(f'Слово "{pw.word}" обновлено', 'success')
    return redirect(url_for('prohibited_words.admin_index'))


@prohibited_words_bp.route('/admin/prohibited-words/<int:word_id>/delete', methods=['POST'])
@login_required
def admin_delete(word_id):
    """Удалить глобальное запрещённое слово."""
    if not current_user.is_admin:
        return jsonify({'error': 'Доступ запрещён'}), 403

    pw = ProhibitedWord.query.get_or_404(word_id)
    word = pw.word
    db.session.delete(pw)
    db.session.commit()

    invalidate_filter_cache()
    flash(f'Слово "{word}" удалено', 'success')
    return redirect(url_for('prohibited_words.admin_index'))


@prohibited_words_bp.route('/admin/prohibited-words/toggle/<int:word_id>', methods=['POST'])
@login_required
def admin_toggle(word_id):
    """Вкл/выкл глобальное запрещённое слово."""
    if not current_user.is_admin:
        return jsonify({'error': 'Доступ запрещён'}), 403

    pw = ProhibitedWord.query.get_or_404(word_id)
    pw.is_active = not pw.is_active
    db.session.commit()

    invalidate_filter_cache()
    return jsonify({'success': True, 'is_active': pw.is_active})


@prohibited_words_bp.route('/admin/prohibited-words/test', methods=['POST'])
@login_required
def admin_test():
    """Протестировать фильтрацию текста (AJAX)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Доступ запрещён'}), 403

    text = request.json.get('text', '')
    word_filter = get_prohibited_words_filter()
    filtered = word_filter.filter_text(text)
    found = word_filter.has_prohibited_words(text)

    return jsonify({
        'original': text,
        'filtered': filtered,
        'found_words': found,
        'changed': text != filtered,
    })


# ============================================================================
# Продавец: персональные запрещённые слова
# ============================================================================

@prohibited_words_bp.route('/prohibited-words')
@login_required
def seller_index():
    """Список запрещённых слов продавца."""
    if not current_user.seller:
        flash('Необходим профиль продавца', 'error')
        return redirect(url_for('index'))

    seller_id = current_user.seller.id

    # Персональные слова продавца
    seller_words = ProhibitedWord.query.filter_by(
        scope='seller', seller_id=seller_id
    ).order_by(ProhibitedWord.word).all()

    # Глобальные слова (только для просмотра)
    global_words = ProhibitedWord.query.filter_by(
        scope='global', is_active=True
    ).order_by(ProhibitedWord.word).all()

    return render_template(
        'seller_prohibited_words.html',
        seller_words=seller_words,
        global_words=global_words,
        default_count=len(PROHIBITED_WORDS_REPLACEMENTS),
    )


@prohibited_words_bp.route('/prohibited-words/add', methods=['POST'])
@login_required
def seller_add():
    """Добавить персональное запрещённое слово."""
    if not current_user.seller:
        flash('Необходим профиль продавца', 'error')
        return redirect(url_for('index'))

    seller_id = current_user.seller.id
    word = request.form.get('word', '').strip().lower()
    replacement = request.form.get('replacement', '').strip()

    if not word:
        flash('Слово не может быть пустым', 'error')
        return redirect(url_for('prohibited_words.seller_index'))

    existing = ProhibitedWord.query.filter_by(
        word=word, scope='seller', seller_id=seller_id
    ).first()
    if existing:
        flash(f'Слово "{word}" уже добавлено', 'warning')
        return redirect(url_for('prohibited_words.seller_index'))

    pw = ProhibitedWord(
        word=word,
        replacement=replacement,
        scope='seller',
        seller_id=seller_id,
        created_by_user_id=current_user.id,
    )
    db.session.add(pw)
    db.session.commit()

    invalidate_filter_cache(seller_id)
    flash(f'Слово "{word}" добавлено', 'success')
    return redirect(url_for('prohibited_words.seller_index'))


@prohibited_words_bp.route('/prohibited-words/<int:word_id>/edit', methods=['POST'])
@login_required
def seller_edit(word_id):
    """Редактировать персональное запрещённое слово."""
    if not current_user.seller:
        return jsonify({'error': 'Необходим профиль продавца'}), 403

    pw = ProhibitedWord.query.get_or_404(word_id)
    if pw.seller_id != current_user.seller.id:
        return jsonify({'error': 'Доступ запрещён'}), 403

    pw.word = request.form.get('word', pw.word).strip().lower()
    pw.replacement = request.form.get('replacement', '').strip()
    pw.is_active = request.form.get('is_active') == 'on'
    db.session.commit()

    invalidate_filter_cache(current_user.seller.id)
    flash(f'Слово "{pw.word}" обновлено', 'success')
    return redirect(url_for('prohibited_words.seller_index'))


@prohibited_words_bp.route('/prohibited-words/<int:word_id>/delete', methods=['POST'])
@login_required
def seller_delete(word_id):
    """Удалить персональное запрещённое слово."""
    if not current_user.seller:
        return jsonify({'error': 'Необходим профиль продавца'}), 403

    pw = ProhibitedWord.query.get_or_404(word_id)
    if pw.seller_id != current_user.seller.id:
        return jsonify({'error': 'Доступ запрещён'}), 403

    word = pw.word
    db.session.delete(pw)
    db.session.commit()

    invalidate_filter_cache(current_user.seller.id)
    flash(f'Слово "{word}" удалено', 'success')
    return redirect(url_for('prohibited_words.seller_index'))


@prohibited_words_bp.route('/prohibited-words/test', methods=['POST'])
@login_required
def seller_test():
    """Протестировать фильтрацию текста (AJAX)."""
    if not current_user.seller:
        return jsonify({'error': 'Необходим профиль продавца'}), 403

    text = request.json.get('text', '')
    seller_id = current_user.seller.id
    word_filter = get_prohibited_words_filter(seller_id)
    filtered = word_filter.filter_text(text)
    found = word_filter.has_prohibited_words(text)

    return jsonify({
        'original': text,
        'filtered': filtered,
        'found_words': found,
        'changed': text != filtered,
    })


# ============================================================================
# Регистрация blueprint
# ============================================================================

def register_prohibited_words_routes(app):
    """Регистрация blueprint в приложении."""
    app.register_blueprint(prohibited_words_bp)
