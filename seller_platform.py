# -*- coding: utf-8 -*-
"""
Платформа для продавцов WB - основной файл приложения
"""
import os
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, render_template, redirect, url_for, flash, request, send_file, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from sqlalchemy import or_

from app import (
    DEFAULT_COLUMN_INDICES,
    column_letters_to_indices,
    compute_profit_table,
    gather_columns,
    read_statistics,
    save_processed_report,
)
from models import (
    db, User, Seller, SellerReport, Product, APILog, ProductStock,
    CardEditHistory, BulkEditHistory, PriceMonitorSettings,
    PriceHistory, SuspiciousPriceChange, ProductSyncSettings
)
from wildberries_api import WildberriesAPIError, list_cards
import json
import time
import threading
from wb_api_client import WildberriesAPIClient, WBAPIException, WBAuthException

# Настройка приложения
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'wb-seller-platform-secret-key-change-in-production')
BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = BASE_DIR / 'data'
DEFAULT_DB_PATH = DATA_ROOT / 'seller_platform.db'
DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
default_db_uri = os.environ.get('DATABASE_URL') or f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"
app.config['SQLALCHEMY_DATABASE_URI'] = default_db_uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.jinja_env.filters['basename'] = lambda value: Path(value).name if value else ''

# Helper функции для работы с WB фотографиями
def wb_photo_url(nm_id: int, photo_index: int = 1, size: str = 'big') -> str:
    """
    Формирует URL фотографии товара Wildberries

    Args:
        nm_id: Артикул WB (nmID)
        photo_index: Номер фотографии (1, 2, 3, ...)
        size: Размер фотографии ('big', 'c246x328', 'c516x688', 'tm')

    Returns:
        Полный URL фотографии

    Формат URL: https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{nmId}/images/{size}/{index}.webp
    """
    if not nm_id:
        return ''

    vol = nm_id // 100000
    part = nm_id // 1000

    # Определяем номер корзины по vol
    if vol >= 0 and vol <= 143:
        basket = '01'
    elif vol >= 144 and vol <= 287:
        basket = '02'
    elif vol >= 288 and vol <= 431:
        basket = '03'
    elif vol >= 432 and vol <= 719:
        basket = '04'
    elif vol >= 720 and vol <= 1007:
        basket = '05'
    elif vol >= 1008 and vol <= 1061:
        basket = '06'
    elif vol >= 1062 and vol <= 1115:
        basket = '07'
    elif vol >= 1116 and vol <= 1169:
        basket = '08'
    elif vol >= 1170 and vol <= 1313:
        basket = '09'
    elif vol >= 1314 and vol <= 1601:
        basket = '10'
    elif vol >= 1602 and vol <= 1655:
        basket = '11'
    elif vol >= 1656 and vol <= 1919:
        basket = '12'
    elif vol >= 1920 and vol <= 2045:
        basket = '13'
    elif vol >= 2046 and vol <= 2189:
        basket = '14'
    else:
        basket = '15'

    return f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/images/{size}/{photo_index}.webp"


# Добавляем встроенные функции Python в контекст Jinja2
app.jinja_env.globals.update({
    'min': min,
    'max': max,
    'len': len,
    'int': int,
    'str': str,
    'wb_photo_url': wb_photo_url,
})

# Добавляем фильтры для шаблонов
app.jinja_env.filters['from_json'] = json.loads

def format_characteristic_value(value):
    """Форматирует значение характеристики (обрабатывает массивы и объекты)"""
    if value is None:
        return '—'
    if isinstance(value, list):
        if len(value) == 0:
            return '—'
        # Если список содержит только одно значение, возвращаем его
        if len(value) == 1:
            return str(value[0])
        # Иначе объединяем через запятую
        return ', '.join(str(v) for v in value)
    return str(value)

app.jinja_env.filters['format_char_value'] = format_characteristic_value

# Инициализация расширений
db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Пожалуйста, войдите в систему'
login_manager.login_message_category = 'info'

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_ROOT = BASE_DIR / 'uploads'
PROCESSED_ROOT = BASE_DIR / 'processed'


def ensure_storage_roots() -> None:
    for folder in (UPLOAD_ROOT, PROCESSED_ROOT):
        folder.mkdir(parents=True, exist_ok=True)


def get_seller_storage_dirs(seller_id: int) -> tuple[Path, Path]:
    ensure_storage_roots()
    upload_dir = UPLOAD_ROOT / f'seller_{seller_id}'
    processed_dir = PROCESSED_ROOT / f'seller_{seller_id}'
    upload_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir, processed_dir


def save_uploaded_file(file_storage, destination: Path) -> Optional[Path]:
    if not file_storage or not file_storage.filename:
        return None
    filename = Path(file_storage.filename).name
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    safe_name = f'{timestamp}_{filename}'
    target = destination / safe_name
    file_storage.save(target)
    return target


def build_preview_data(
    statistics_path: Optional[str],
    selected_columns: Optional[List[str]],
) -> tuple[List[str], List[dict], List[str], Optional[str]]:
    if not statistics_path or not Path(statistics_path).exists():
        return [], [], selected_columns or [], None
    try:
        df = read_statistics(Path(statistics_path))
        available_columns = gather_columns(df)
        resolved_columns = selected_columns or column_letters_to_indices(
            df.columns,
            DEFAULT_COLUMN_INDICES,
        )
        preview_rows: List[dict] = []
        if resolved_columns:
            preview_rows = (
                df[resolved_columns]
                .head(10)
                .fillna('')
                .to_dict(orient='records')
            )
        return available_columns, preview_rows, resolved_columns, None
    except Exception as exc:
        return [], [], selected_columns or [], str(exc)


def get_latest_report(seller_id: int) -> Optional[SellerReport]:
    return (
        SellerReport.query.filter_by(seller_id=seller_id)
        .order_by(SellerReport.created_at.desc())
        .first()
    )


@app.after_request
def after_request(response):
    """Устанавливаем правильную кодировку UTF-8 для всех ответов"""
    if response.content_type and response.content_type.startswith('text/html'):
        response.content_type = 'text/html; charset=utf-8'
    return response


UPLOAD_ROOT = BASE_DIR / 'uploads'
PROCESSED_ROOT = BASE_DIR / 'processed'


def ensure_storage_roots() -> None:
    for folder in (UPLOAD_ROOT, PROCESSED_ROOT, DATA_ROOT):
        folder.mkdir(parents=True, exist_ok=True)


def get_seller_storage_dirs(seller_id: int) -> tuple[Path, Path]:
    ensure_storage_roots()
    upload_dir = UPLOAD_ROOT / f'seller_{seller_id}'
    processed_dir = PROCESSED_ROOT / f'seller_{seller_id}'
    upload_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir, processed_dir


def save_uploaded_file(file_storage, destination: Path) -> Optional[Path]:
    if not file_storage or not file_storage.filename:
        return None
    filename = Path(file_storage.filename).name
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    safe_name = f'{timestamp}_{filename}'
    target = destination / safe_name
    file_storage.save(target)
    return target


def build_preview_data(
    statistics_path: Optional[str],
    selected_columns: Optional[List[str]],
) -> tuple[List[str], List[dict], List[str], Optional[str]]:
    if not statistics_path or not Path(statistics_path).exists():
        return [], [], selected_columns or [], None
    try:
        df = read_statistics(Path(statistics_path))
        available_columns = gather_columns(df)
        resolved_columns = selected_columns or column_letters_to_indices(
            df.columns,
            DEFAULT_COLUMN_INDICES,
        )
        preview_rows: List[dict] = []
        if resolved_columns:
            preview_rows = (
                df[resolved_columns]
                .head(10)
                .fillna('')
                .to_dict(orient='records')
            )
        return available_columns, preview_rows, resolved_columns, None
    except Exception as exc:
        return [], [], selected_columns or [], str(exc)


def get_latest_report(seller_id: int) -> Optional[SellerReport]:
    return (
        SellerReport.query.filter_by(seller_id=seller_id)
        .order_by(SellerReport.created_at.desc())
        .first()
    )


def summarise_card(card: Dict[str, Any]) -> Dict[str, Any]:
    """Подготовить ключевые поля карточки для отображения."""
    sizes = card.get('sizes') or []
    media_files = card.get('mediaFiles') or card.get('photos') or []
    barcode_count = 0
    for size in sizes:
        if isinstance(size, dict):
            barcode_count += len(size.get('skus') or [])

    return {
        'vendor_code': card.get('vendorCode') or card.get('supplierVendorCode'),
        'nm_id': card.get('nmID') or card.get('nmId'),
        'title': card.get('title') or card.get('subjectName') or card.get('name'),
        'brand': card.get('brand'),
        'subject': card.get('subjectName') or card.get('subjectID'),
        'updated_at': card.get('updatedAt') or card.get('updateAt') or card.get('modifiedAt'),
        'photo_count': len(media_files) if isinstance(media_files, list) else 0,
        'barcode_count': barcode_count,
        'card': card,
    }


@login_manager.user_loader
def load_user(user_id):
    """Загрузка пользователя для Flask-Login"""
    return User.query.get(int(user_id))


def admin_required(f):
    """Декоратор для проверки прав администратора"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('У вас нет прав для доступа к этой странице', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


# ============= МАРШРУТЫ АВТОРИЗАЦИИ =============

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Страница входа в систему"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember', False)

        if not username or not password:
            flash('Введите имя пользователя и пароль', 'warning')
            return render_template('login.html')

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            if not user.is_active:
                flash('Ваш аккаунт деактивирован. Обратитесь к администратору.', 'danger')
                return render_template('login.html')

            # Обновляем время последнего входа
            user.last_login = datetime.utcnow()
            db.session.commit()

            login_user(user, remember=remember)
            flash(f'Добро пожаловать, {user.username}!', 'success')

            # Перенаправление на запрошенную страницу или на дашборд
            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('dashboard'))
        else:
            flash('Неверное имя пользователя или пароль', 'danger')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    """Выход из системы"""
    logout_user()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('login'))


# ============= ДАШБОРД ПРОДАВЦА =============

@app.route('/')
@app.route('/dashboard')
@login_required
def dashboard():
    """Главная страница - дашборд продавца"""
    latest_report = None
    summary = None
    latest_filename = None
    if current_user.seller:
        latest_report = get_latest_report(current_user.seller.id)
        if latest_report:
            summary = latest_report.summary or {}
            processed_path = Path(latest_report.processed_path)
            latest_filename = processed_path.name if processed_path.exists() else None

    return render_template(
        'dashboard.html',
        user=current_user,
        latest_report=latest_report,
        summary=summary,
        latest_filename=latest_filename,
    )


@app.route('/reports', methods=['GET', 'POST'])
@login_required
def reports():
    if not current_user.seller:
        if current_user.is_admin:
            flash('Создайте продавца в админ-панели и войдите под его учётной записью, чтобы работать с отчётами.', 'info')
            return redirect(url_for('admin_panel'))
        flash('Для работы с отчётами обратитесь к администратору.', 'warning')
        return redirect(url_for('dashboard'))

    seller = current_user.seller
    upload_dir, processed_dir = get_seller_storage_dirs(seller.id)

    latest_report = get_latest_report(seller.id)
    statistics_path = latest_report.statistics_path if latest_report else None
    price_path = latest_report.price_path if latest_report else None
    selected_columns: List[str] = list(latest_report.selected_columns or []) if latest_report else []

    if request.method == 'POST':
        selected_columns = request.form.getlist('columns') or selected_columns
        stat_file = request.files.get('statistics')
        price_file = request.files.get('prices')

        new_stat_path = save_uploaded_file(stat_file, upload_dir)
        new_price_path = save_uploaded_file(price_file, upload_dir)

        stat_path = new_stat_path or statistics_path
        price_path_effective = new_price_path or price_path

        if not stat_path or not Path(stat_path).exists():
            flash('Загрузите отчёт Wildberries (xlsx).', 'warning')
            return redirect(url_for('reports'))

        if not price_path_effective or not Path(price_path_effective).exists():
            flash('Загрузите прайс поставщика (csv).', 'warning')
            return redirect(url_for('reports'))

        try:
            df, _, summary = compute_profit_table(
                Path(stat_path),
                Path(price_path_effective),
                selected_columns or None,
            )
        except Exception as exc:
            flash(f'Ошибка обработки: {exc}', 'danger')
            return redirect(url_for('reports'))

        try:
            processed_path = save_processed_report(
                df,
                summary,
                output_dir=processed_dir,
            )
            report = SellerReport(
                seller_id=seller.id,
                statistics_path=str(stat_path),
                price_path=str(price_path_effective),
                processed_path=str(processed_path),
                selected_columns=selected_columns or [],
                summary=summary,
            )
            db.session.add(report)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            flash(f'Не удалось сохранить отчёт: {exc}', 'danger')
            return redirect(url_for('reports'))

        flash('Отчёт обновлён.', 'success')
        return redirect(url_for('reports'))

    available_columns, preview_rows, selected_columns, preview_error = build_preview_data(
        statistics_path,
        selected_columns,
    )
    if preview_error:
        flash(f'Не удалось подготовить предпросмотр: {preview_error}', 'warning')

    reports_history = (
        SellerReport.query.filter_by(seller_id=seller.id)
        .order_by(SellerReport.created_at.desc())
        .limit(10)
        .all()
    )

    return render_template(
        'reports.html',
        seller=seller,
        latest_report=latest_report,
        reports=reports_history,
        available_columns=available_columns,
        selected_columns=selected_columns,
        preview_rows=preview_rows,
        statistics_ready=bool(statistics_path and Path(statistics_path).exists()),
        price_ready=bool(price_path and Path(price_path).exists()),
        statistics_path=statistics_path,
        price_path=price_path,
    )




@app.route('/cards', methods=['GET'])
@login_required
def cards():
    seller: Optional[Seller] = None
    seller_options: List[Seller] = []
    selected_seller_id: Optional[int] = None

    if current_user.is_admin:
        seller_options = (
            Seller.query.join(User)
            .order_by(Seller.company_name.asc())
            .all()
        )
        selected_seller_id = request.args.get('seller_id', type=int)
        if selected_seller_id:
            seller = Seller.query.get(selected_seller_id)
            if not seller:
                flash('Продавец с указанным ID не найден.', 'warning')
    else:
        seller = current_user.seller
        if not seller:
            flash('Учетная запись продавца не привязана к текущему пользователю.', 'warning')
            return redirect(url_for('dashboard'))

    search = request.args.get('search', '').strip()
    updated_at = request.args.get('updated_at', '').strip() or None
    limit = request.args.get('limit', type=int) or 50
    limit = max(1, min(limit, 1_000))

    cards_payload: List[Dict[str, Any]] = []
    prepared_cards: List[Dict[str, Any]] = []
    cursor: Dict[str, Any] = {}
    api_error: Optional[str] = None
    additional_errors: List[str] = []

    if seller:
        if not seller.wb_api_key:
            api_error = 'Для выбранного продавца не указан API токен Wildberries.'
        else:
            try:
                response = list_cards(
                    seller.wb_api_key,
                    limit=limit,
                    search=search or None,
                    updated_at=updated_at,
                )
                cards_payload = response.get('cards') or []
                cursor = response.get('cursor') or {}
                prepared_cards = [summarise_card(card) for card in cards_payload]

                error_text = (response.get('errorText') or '').strip()
                if error_text:
                    additional_errors.append(error_text)
                for extra in response.get('additionalErrors') or []:
                    text_message = str(extra).strip()
                    if text_message:
                        additional_errors.append(text_message)
            except WildberriesAPIError as exc:
                api_error = str(exc)

    return render_template(
        'cards.html',
        seller=seller,
        seller_options=seller_options,
        selected_seller_id=selected_seller_id,
        cards=prepared_cards,
        raw_cards=cards_payload,
        cursor=cursor,
        api_error=api_error,
        additional_errors=additional_errors,
        search=search,
        limit=limit,
        updated_at=updated_at,
    )


@app.route('/reports/<int:report_id>/download')
@login_required
def download_report(report_id: int):
    if not current_user.seller:
        flash('Недостаточно прав для скачивания отчёта.', 'warning')
        return redirect(url_for('dashboard'))

    report = SellerReport.query.get_or_404(report_id)
    if report.seller_id != current_user.seller.id:
        abort(404)

    path = Path(report.processed_path)
    if not path.exists():
        flash('Файл отчёта не найден. Сформируйте новый отчёт.', 'warning')
        return redirect(url_for('reports'))

    return send_file(path, as_attachment=True, download_name=path.name)


# ============= АДМИН ПАНЕЛЬ =============

@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    """Главная страница админ панели"""
    sellers = Seller.query.join(User).all()
    total_sellers = len(sellers)
    active_sellers = sum(1 for s in sellers if s.user.is_active)

    return render_template('admin.html',
                         sellers=sellers,
                         total_sellers=total_sellers,
                         active_sellers=active_sellers)


@app.route('/admin/sellers/add', methods=['GET', 'POST'])
@login_required
@admin_required
def add_seller():
    """Добавление нового продавца"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        company_name = request.form.get('company_name', '').strip()
        contact_phone = request.form.get('contact_phone', '').strip()
        wb_seller_id = request.form.get('wb_seller_id', '').strip()
        wb_api_key = request.form.get('wb_api_key', '').strip()
        notes = request.form.get('notes', '').strip()

        # Валидация
        if not all([username, email, password, company_name]):
            flash('Заполните все обязательные поля', 'warning')
            return render_template('add_seller.html')

        # Проверка на существующего пользователя
        if User.query.filter_by(username=username).first():
            flash(f'Пользователь с именем "{username}" уже существует', 'danger')
            return render_template('add_seller.html')

        if User.query.filter_by(email=email).first():
            flash(f'Пользователь с email "{email}" уже существует', 'danger')
            return render_template('add_seller.html')

        try:
            # Создаем пользователя
            user = User(
                username=username,
                email=email,
                is_admin=False,
                is_active=True
            )
            user.set_password(password)
            db.session.add(user)
            db.session.flush()  # Получаем ID пользователя

            # Создаем продавца
            seller = Seller(
                user_id=user.id,
                company_name=company_name,
                contact_phone=contact_phone,
                wb_seller_id=wb_seller_id,
                wb_api_key=wb_api_key or None,
                notes=notes
            )
            db.session.add(seller)
            db.session.commit()

            flash(f'Продавец "{company_name}" успешно добавлен', 'success')
            return redirect(url_for('admin_panel'))

        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка при создании продавца: {str(e)}', 'danger')
            return render_template('add_seller.html')

    return render_template('add_seller.html')


@app.route('/admin/sellers/<int:seller_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_seller(seller_id):
    """Редактирование продавца"""
    seller = Seller.query.get_or_404(seller_id)

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        company_name = request.form.get('company_name', '').strip()
        contact_phone = request.form.get('contact_phone', '').strip()
        wb_seller_id = request.form.get('wb_seller_id', '').strip()
        wb_api_key = request.form.get('wb_api_key', '').strip()
        notes = request.form.get('notes', '').strip()
        is_active = request.form.get('is_active') == 'on'
        new_password = request.form.get('new_password', '').strip()

        if not all([email, company_name]):
            flash('Заполните все обязательные поля', 'warning')
            return render_template('edit_seller.html', seller=seller)

        # Проверка email на уникальность
        existing_user = User.query.filter_by(email=email).first()
        if existing_user and existing_user.id != seller.user_id:
            flash(f'Email "{email}" уже используется', 'danger')
            return render_template('edit_seller.html', seller=seller)

        try:
            # Обновляем данные пользователя
            seller.user.email = email
            seller.user.is_active = is_active

            if new_password:
                seller.user.set_password(new_password)

            # Обновляем данные продавца
            seller.company_name = company_name
            seller.contact_phone = contact_phone
            seller.wb_seller_id = wb_seller_id
            seller.wb_api_key = wb_api_key or None
            seller.notes = notes

            db.session.commit()
            flash(f'Продавец "{company_name}" успешно обновлен', 'success')
            return redirect(url_for('admin_panel'))

        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка при обновлении: {str(e)}', 'danger')

    return render_template('edit_seller.html', seller=seller)


@app.route('/admin/sellers/<int:seller_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_seller(seller_id):
    """Удаление продавца"""
    seller = Seller.query.get_or_404(seller_id)
    company_name = seller.company_name

    try:
        # Удаляем пользователя (продавец удалится автоматически через cascade)
        db.session.delete(seller.user)
        db.session.commit()
        flash(f'Продавец "{company_name}" успешно удален', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при удалении: {str(e)}', 'danger')

    return redirect(url_for('admin_panel'))


# ============= НАСТРОЙКИ API =============

@app.route('/api-settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    """Настройка API ключей Wildberries"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        wb_api_key = request.form.get('wb_api_key', '').strip()
        force_save = request.form.get('force_save') == 'on'

        if not wb_api_key:
            flash('Введите API ключ', 'warning')
            return render_template('api_settings.html', seller=current_user.seller)

        # Проверить валидность ключа (если не force_save)
        if not force_save:
            try:
                app.logger.info(f"Testing WB API connection for seller {current_user.seller.id}")
                with WildberriesAPIClient(wb_api_key, sandbox=False, timeout=10) as client:
                    if client.test_connection():
                        # Ключ валиден — сохраняем
                        current_user.seller.wb_api_key = wb_api_key
                        current_user.seller.api_sync_status = 'ready'
                        db.session.commit()

                        flash('API ключ успешно сохранен и проверен ✓', 'success')
                        return redirect(url_for('dashboard'))
                    else:
                        # Тест не прошёл, но сохраняем с warning
                        app.logger.warning(f"API test failed for seller {current_user.seller.id}")
                        current_user.seller.wb_api_key = wb_api_key
                        current_user.seller.api_sync_status = 'error'
                        db.session.commit()

                        flash('API ключ сохранен, но проверка соединения не удалась. '
                              'Проверьте правильность ключа и наличие доступа к API Wildberries.', 'warning')
                        return redirect(url_for('dashboard'))

            except WBAuthException as e:
                app.logger.error(f"Auth error for seller {current_user.seller.id}: {e}")
                # Сохраняем с ошибкой
                current_user.seller.wb_api_key = wb_api_key
                current_user.seller.api_sync_status = 'auth_error'
                db.session.commit()
                flash(f'Ошибка авторизации: {str(e)}. Ключ сохранен, но требует проверки.', 'warning')
                return redirect(url_for('dashboard'))

            except WBAPIException as e:
                app.logger.error(f"API error for seller {current_user.seller.id}: {e}")
                # Сохраняем с ошибкой
                current_user.seller.wb_api_key = wb_api_key
                current_user.seller.api_sync_status = 'api_error'
                db.session.commit()
                flash(f'Ошибка API: {str(e)}. Ключ сохранен, возможны проблемы с соединением.', 'warning')
                return redirect(url_for('dashboard'))

            except Exception as e:
                app.logger.exception(f"Unexpected error testing API for seller {current_user.seller.id}: {e}")
                # Сохраняем с ошибкой
                current_user.seller.wb_api_key = wb_api_key
                current_user.seller.api_sync_status = 'unknown_error'
                db.session.commit()
                flash(f'Не удалось проверить ключ: {str(e)}. Ключ сохранен для повторной попытки.', 'warning')
                return redirect(url_for('dashboard'))
        else:
            # Принудительное сохранение без проверки
            app.logger.info(f"Force saving API key for seller {current_user.seller.id}")
            current_user.seller.wb_api_key = wb_api_key
            current_user.seller.api_sync_status = 'unchecked'
            db.session.commit()
            flash('API ключ сохранен без проверки', 'info')
            return redirect(url_for('dashboard'))

    return render_template('api_settings.html', seller=current_user.seller)


# ============= КАРТОЧКИ ТОВАРОВ =============

@app.route('/products')
@login_required
def products_list():
    """Список карточек товаров с расширенными фильтрами и сортировкой"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    try:
        # Параметры пагинации
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        per_page = min(per_page, 200)  # Увеличено до 200 на странице

        # Базовые фильтры
        search = request.args.get('search', '').strip()
        # Исправлено: чекбокс отправляет '1', нужна правильная обработка
        active_only = request.args.get('active_only', '').strip() in ['1', 'true', 'True', 'on']

        # Расширенные фильтры
        filter_brand = request.args.get('brand', '').strip()
        filter_category = request.args.get('category', '').strip()
        filter_has_stock = request.args.get('has_stock', '').strip()  # 'yes', 'no', ''

        # Сортировка
        sort_by = request.args.get('sort', 'updated_at')  # по умолчанию по дате обновления
        sort_order = request.args.get('order', 'desc')  # 'asc' или 'desc'

        # Построение запроса
        query = Product.query.filter_by(seller_id=current_user.seller.id)

        if active_only:
            query = query.filter_by(is_active=True)

        if search:
            # Поиск по артикулу, названию или бренду
            search_filter = or_(
                Product.vendor_code.ilike(f'%{search}%'),
                Product.title.ilike(f'%{search}%'),
                Product.brand.ilike(f'%{search}%'),
                Product.nm_id.cast(db.String).ilike(f'%{search}%')
            )
            query = query.filter(search_filter)

        # Фильтр по бренду
        if filter_brand:
            query = query.filter(Product.brand.ilike(f'%{filter_brand}%'))

        # Фильтр по категории
        if filter_category:
            query = query.filter(Product.object_name.ilike(f'%{filter_category}%'))

        # Фильтр по наличию остатков (исправлен JOIN для избежания дубликатов)
        if filter_has_stock == 'yes':
            # Товары у которых есть хотя бы один остаток > 0
            # Используем EXISTS вместо JOIN чтобы избежать дубликатов строк
            query = query.filter(
                db.exists().where(
                    db.and_(
                        ProductStock.product_id == Product.id,
                        ProductStock.quantity > 0
                    )
                )
            )
        elif filter_has_stock == 'no':
            # Товары без остатков или с нулевыми остатками
            # Используем NOT EXISTS для чистого запроса без группировки
            query = query.filter(
                ~db.exists().where(
                    db.and_(
                        ProductStock.product_id == Product.id,
                        ProductStock.quantity > 0
                    )
                )
            )

        # Сортировка
        sort_column = {
            'updated_at': Product.updated_at,
            'created_at': Product.created_at,
            'vendor_code': Product.vendor_code,
            'title': Product.title,
            'brand': Product.brand,
            'nm_id': Product.nm_id,
            'category': Product.object_name,
        }.get(sort_by, Product.updated_at)

        if sort_order == 'asc':
            query = query.order_by(sort_column.asc())
        else:
            query = query.order_by(sort_column.desc())

        # Пагинация
        pagination = query.paginate(
            page=page,
            per_page=per_page,
            error_out=False
        )

        products = pagination.items
        total_products = current_user.seller.products.count()
        active_products = current_user.seller.products.filter_by(is_active=True).count()

        # Получаем уникальные бренды и категории для фильтров
        brands = db.session.query(Product.brand).filter(
            Product.seller_id == current_user.seller.id,
            Product.brand.isnot(None),
            Product.brand != ''
        ).distinct().order_by(Product.brand).all()
        brands = [b[0] for b in brands]

        categories = db.session.query(Product.object_name).filter(
            Product.seller_id == current_user.seller.id,
            Product.object_name.isnot(None),
            Product.object_name != ''
        ).distinct().order_by(Product.object_name).all()
        categories = [c[0] for c in categories]

        return render_template(
            'products.html',
            products=products,
            pagination=pagination,
            total_products=total_products,
            active_products=active_products,
            search=search,
            active_only=active_only,
            filter_brand=filter_brand,
            filter_category=filter_category,
            filter_has_stock=filter_has_stock,
            sort_by=sort_by,
            sort_order=sort_order,
            brands=brands,
            categories=categories,
            seller=current_user.seller
        )
    except Exception as e:
        app.logger.exception(f"Error in products_list: {e}")
        flash(f'Ошибка при загрузке карточек товаров: {str(e)}', 'danger')
        return redirect(url_for('dashboard'))


@app.route('/products/bulk-action', methods=['POST'])
@login_required
def bulk_products_action():
    """Массовые операции с товарами"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    try:
        # Получаем параметры
        action = request.form.get('action', '').strip()
        product_ids = request.form.getlist('product_ids')

        if not action:
            flash('Не указано действие', 'warning')
            return redirect(url_for('products_list'))

        if not product_ids:
            flash('Не выбраны товары', 'warning')
            return redirect(url_for('products_list'))

        # Преобразуем в целые числа
        try:
            product_ids = [int(pid) for pid in product_ids]
        except ValueError:
            flash('Неверный формат ID товаров', 'danger')
            return redirect(url_for('products_list'))

        # Проверяем что все товары принадлежат текущему продавцу
        products = Product.query.filter(
            Product.id.in_(product_ids),
            Product.seller_id == current_user.seller.id
        ).all()

        if len(products) != len(product_ids):
            flash('Некоторые товары не найдены или не принадлежат вам', 'danger')
            return redirect(url_for('products_list'))

        # Выполняем действие
        affected_count = 0

        if action == 'activate':
            # Активировать товары
            for product in products:
                product.is_active = True
                affected_count += 1
            db.session.commit()
            flash(f'Активировано товаров: {affected_count}', 'success')

        elif action == 'deactivate':
            # Деактивировать товары
            for product in products:
                product.is_active = False
                affected_count += 1
            db.session.commit()
            flash(f'Деактивировано товаров: {affected_count}', 'success')

        elif action == 'delete':
            # Удалить товары (мягкое удаление - пометить как неактивные)
            for product in products:
                product.is_active = False
                affected_count += 1
            db.session.commit()
            flash(f'Помечено на удаление товаров: {affected_count}', 'success')

        elif action == 'export':
            # Экспорт в CSV
            import csv
            from io import StringIO
            from flask import make_response

            output = StringIO()

            # Добавляем UTF-8 BOM для корректного отображения кириллицы в Excel
            output.write('\ufeff')

            writer = csv.writer(output)

            # Заголовки
            writer.writerow([
                'ID', 'Артикул', 'NM ID', 'Название', 'Бренд',
                'Категория', 'Цена', 'Активен', 'Дата создания'
            ])

            # Данные
            for product in products:
                writer.writerow([
                    product.id,
                    product.vendor_code,
                    product.nm_id,
                    product.title,
                    product.brand,
                    product.object_name,
                    product.price or '',
                    'Да' if product.is_active else 'Нет',
                    product.created_at.strftime('%Y-%m-%d %H:%M:%S')
                ])

            # Создаем response с правильной кодировкой
            csv_data = output.getvalue().encode('utf-8-sig')
            response = make_response(csv_data)
            response.headers['Content-Type'] = 'text/csv; charset=utf-8'
            response.headers['Content-Disposition'] = f'attachment; filename=products_export_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.csv'
            return response

        else:
            flash(f'Неизвестное действие: {action}', 'danger')

    except Exception as e:
        app.logger.exception(f"Error in bulk_products_action: {e}")
        flash(f'Ошибка при выполнении массовой операции: {str(e)}', 'danger')

    return redirect(url_for('products_list'))


def _perform_product_sync_task(seller_id: int, flask_app):
    """
    Фоновая задача синхронизации товаров

    Args:
        seller_id: ID продавца
        flask_app: Экземпляр Flask приложения для контекста
    """
    with flask_app.app_context():
        try:
            # Получаем продавца из БД
            seller = Seller.query.get(seller_id)
            if not seller:
                app.logger.error(f"Seller {seller_id} not found for background sync")
                return

            seller.api_sync_status = 'syncing'
            db.session.commit()

            start_time = time.time()

            with WildberriesAPIClient(seller.wb_api_key) as client:
                # Получаем все карточки
                app.logger.info(f"🔄 Background sync: fetching cards for seller_id={seller_id}")
                all_cards = client.get_all_cards(batch_size=100)
                app.logger.info(f"✅ Background sync: got {len(all_cards)} cards from WB API")

                # Статистика
                created_count = 0
                updated_count = 0

                for card_data in all_cards:
                    nm_id = card_data.get('nmID')
                    if not nm_id:
                        continue

                    # Ищем существующую карточку
                    product = Product.query.filter_by(
                        seller_id=seller.id,
                        nm_id=nm_id
                    ).first()

                    # Извлекаем данные из API
                    vendor_code = card_data.get('vendorCode', '')
                    title = card_data.get('title', '')
                    brand = card_data.get('brand', '')
                    object_name = (
                        card_data.get('subjectName') or
                        card_data.get('objectName') or
                        card_data.get('object') or
                        ''
                    )
                    subject_id = card_data.get('subjectID')
                    description = card_data.get('description', '')

                    # Характеристики
                    characteristics = card_data.get('characteristics', [])
                    characteristics_json = json.dumps(characteristics, ensure_ascii=False) if characteristics else None

                    # Габариты
                    dimensions = card_data.get('dimensions', {})
                    dimensions_json = json.dumps(dimensions, ensure_ascii=False) if dimensions else None

                    # Медиа
                    media = card_data.get('mediaFiles', [])
                    photo_count_v1 = len([m for m in media if m.get('big') and m.get('mediaType') != 'video'])
                    photo_count_v2 = len([m for m in media if m.get('mediaType') != 'video'])
                    photos_field = card_data.get('photos', [])
                    photo_count_v3 = len(photos_field) if photos_field else 0
                    photo_count = max(photo_count_v1, photo_count_v2, photo_count_v3)
                    if photo_count == 0 and card_data.get('mediaFiles'):
                        photo_count = len(media) if media else 0
                    photo_indices = list(range(1, photo_count + 1)) if photo_count > 0 else []
                    photos_json = json.dumps(photo_indices) if photo_indices else None

                    # Видео
                    video_media = next((m for m in media if m.get('mediaType') == 'video'), None)
                    video = video_media.get('big') if video_media else None

                    # Размеры
                    sizes = card_data.get('sizes', [])
                    sizes_json = json.dumps(sizes, ensure_ascii=False) if sizes else None

                    if product:
                        # Обновление существующей карточки
                        product.vendor_code = vendor_code
                        product.title = title
                        product.brand = brand
                        product.object_name = object_name
                        product.subject_id = subject_id
                        product.description = description
                        product.characteristics_json = characteristics_json
                        product.dimensions_json = dimensions_json
                        product.photos_json = photos_json
                        product.video_url = video
                        product.sizes_json = sizes_json
                        product.last_sync = datetime.utcnow()
                        product.is_active = True
                        updated_count += 1
                    else:
                        # Создание новой карточки
                        product = Product(
                            seller_id=seller.id,
                            nm_id=nm_id,
                            imt_id=card_data.get('imtID'),
                            vendor_code=vendor_code,
                            title=title,
                            brand=brand,
                            object_name=object_name,
                            subject_id=subject_id,
                            description=description,
                            supplier_vendor_code=card_data.get('supplierVendorCode', ''),
                            characteristics_json=characteristics_json,
                            dimensions_json=dimensions_json,
                            photos_json=photos_json,
                            video_url=video,
                            sizes_json=sizes_json,
                            last_sync=datetime.utcnow(),
                            is_active=True
                        )
                        db.session.add(product)
                        created_count += 1

                # Сохраняем все изменения
                db.session.commit()

                app.logger.info(f"💾 Background sync saved: {created_count} new, {updated_count} updated")

                # Обновляем статус синхронизации
                seller.api_last_sync = datetime.utcnow()
                seller.api_sync_status = 'success'
                db.session.commit()

                elapsed = time.time() - start_time

                # Логируем успешный запрос
                APILog.log_request(
                    seller_id=seller.id,
                    endpoint='/content/v2/get/cards/list',
                    method='POST',
                    status_code=200,
                    response_time=elapsed,
                    success=True
                )

                app.logger.info(f"✅ Background sync completed in {elapsed:.1f}s: {created_count} new, {updated_count} updated")

        except WBAuthException as e:
            with flask_app.app_context():
                seller = Seller.query.get(seller_id)
                if seller:
                    seller.api_sync_status = 'auth_error'
                    db.session.commit()
                app.logger.error(f"❌ Background sync auth error: {str(e)}")

        except WBAPIException as e:
            with flask_app.app_context():
                seller = Seller.query.get(seller_id)
                if seller:
                    seller.api_sync_status = 'error'
                    db.session.commit()
                app.logger.error(f"❌ Background sync API error: {str(e)}")

        except Exception as e:
            with flask_app.app_context():
                seller = Seller.query.get(seller_id)
                if seller:
                    seller.api_sync_status = 'error'
                    db.session.commit()
                app.logger.exception(f"❌ Background sync unexpected error: {str(e)}")


@app.route('/products/sync', methods=['POST'])
@login_required
def sync_products():
    """Запуск синхронизации карточек товаров в фоновом режиме"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    if not current_user.seller.has_valid_api_key():
        flash('API ключ Wildberries не настроен. Настройте его в разделе "Настройки API".', 'warning')
        return redirect(url_for('api_settings'))

    # Проверяем что синхронизация не запущена уже
    if current_user.seller.api_sync_status == 'syncing':
        flash('Синхронизация уже выполняется. Пожалуйста, подождите...', 'warning')
        return redirect(url_for('products_list'))

    try:
        # Устанавливаем статус syncing
        current_user.seller.api_sync_status = 'syncing'
        db.session.commit()

        # Запускаем синхронизацию в фоновом потоке
        thread = threading.Thread(
            target=_perform_product_sync_task,
            args=(current_user.seller.id, app._get_current_object()),
            daemon=True
        )
        thread.start()

        flash('Синхронизация товаров запущена в фоновом режиме. Обновите страницу через минуту чтобы увидеть результаты.', 'info')
        app.logger.info(f"✅ Background product sync started for seller_id={current_user.seller.id}")

    except Exception as e:
        app.logger.exception(f"❌ Failed to start background sync: {str(e)}")
        flash(f'Ошибка запуска синхронизации: {str(e)}', 'danger')

    return redirect(url_for('products_list'))


@app.route('/products/sync-stocks', methods=['POST'])
@login_required
def sync_warehouse_stocks():
    """Синхронизация остатков по складам через API WB"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    if not current_user.seller.has_valid_api_key():
        flash('API ключ Wildberries не настроен. Настройте его в разделе "Настройки API".', 'warning')
        return redirect(url_for('api_settings'))

    try:
        start_time = time.time()

        with WildberriesAPIClient(current_user.seller.wb_api_key) as client:
            # Получаем все остатки через Statistics API
            # Используем дату 30 дней назад для получения актуальных данных
            from datetime import timedelta
            date_from = (datetime.utcnow() - timedelta(days=30)).strftime('%Y-%m-%d')

            app.logger.info(f"🔄 Начинаем загрузку остатков для seller_id={current_user.seller.id}")
            all_stocks = client.get_stocks(date_from=date_from)
            app.logger.info(f"✅ Получено {len(all_stocks)} записей об остатках из WB API")

            # Статистика
            created_count = 0
            updated_count = 0

            # Группируем остатки по nmId и складу (Statistics API может возвращать несколько записей)
            stocks_by_product = {}
            for stock_data in all_stocks:
                nm_id = stock_data.get('nmId')
                warehouse_name = stock_data.get('warehouseName', 'Неизвестный склад')

                if not nm_id:
                    continue

                key = (nm_id, warehouse_name)
                if key not in stocks_by_product:
                    stocks_by_product[key] = {
                        'nm_id': nm_id,
                        'warehouse_name': warehouse_name,
                        'quantity': 0,
                        'quantity_full': 0,
                        'barcode': stock_data.get('barcode', ''),
                        'subject': stock_data.get('subject', ''),
                    }

                # Суммируем количества (могут быть разные размеры)
                stocks_by_product[key]['quantity'] += stock_data.get('quantity', 0)
                stocks_by_product[key]['quantity_full'] += stock_data.get('quantityFull', 0)

            app.logger.info(f"📊 Агрегировано {len(stocks_by_product)} уникальных записей остатков")

            # Обрабатываем каждую агрегированную запись
            for key, stock_data in stocks_by_product.items():
                nm_id = stock_data['nm_id']
                warehouse_name = stock_data['warehouse_name']

                # Находим товар по nm_id
                product = Product.query.filter_by(
                    seller_id=current_user.seller.id,
                    nm_id=nm_id
                ).first()

                if not product:
                    # Товар не найден - пропускаем (возможно не синхронизированы карточки)
                    continue

                # Генерируем warehouse_id из имени (для уникальности)
                # WB не возвращает ID склада в Statistics API, используем хеш имени
                warehouse_id = hash(warehouse_name) % 1000000

                # Ищем существующую запись об остатках для этого товара и склада
                stock = ProductStock.query.filter_by(
                    product_id=product.id,
                    warehouse_id=warehouse_id
                ).first()

                quantity = stock_data['quantity']
                quantity_full = stock_data['quantity_full']

                if stock:
                    # Обновляем существующую запись
                    stock.warehouse_name = warehouse_name
                    stock.quantity = quantity
                    stock.quantity_full = quantity_full
                    stock.updated_at = datetime.utcnow()
                    updated_count += 1
                else:
                    # Создаем новую запись
                    stock = ProductStock(
                        product_id=product.id,
                        warehouse_id=warehouse_id,
                        warehouse_name=warehouse_name,
                        quantity=quantity,
                        quantity_full=quantity_full,
                        in_way_to_client=0,
                        in_way_from_client=0
                    )
                    db.session.add(stock)
                    created_count += 1

            # Сохраняем все изменения
            db.session.commit()

            app.logger.info(f"💾 Сохранено остатков в БД: {created_count} новых, {updated_count} обновлено")

            elapsed = time.time() - start_time

            # Логируем успешный запрос
            APILog.log_request(
                seller_id=current_user.seller.id,
                endpoint='/api/v1/supplier/stocks',
                method='GET',
                status_code=200,
                response_time=elapsed,
                success=True
            )

            app.logger.info(f"✅ Синхронизация остатков завершена успешно за {elapsed:.1f}с")

            flash(
                f'Синхронизация остатков завершена за {elapsed:.1f}с: '
                f'{created_count} новых, {updated_count} обновлено',
                'success'
            )

    except WBAuthException as e:
        app.logger.error(f"❌ Ошибка авторизации при загрузке остатков: {str(e)}")

        APILog.log_request(
            seller_id=current_user.seller.id,
            endpoint='/api/v1/supplier/stocks',
            method='GET',
            status_code=401,
            response_time=0,
            success=False,
            error_message=f'Authentication failed: {str(e)}'
        )

        flash('Ошибка авторизации. Проверьте API ключ.', 'danger')

    except WBAPIException as e:
        app.logger.error(f"❌ Ошибка WB API при загрузке остатков: {str(e)}")

        APILog.log_request(
            seller_id=current_user.seller.id,
            endpoint='/api/v1/supplier/stocks',
            method='GET',
            status_code=500,
            response_time=0,
            success=False,
            error_message=str(e)
        )

        flash(f'Ошибка API WB: {str(e)}', 'danger')

    except Exception as e:
        app.logger.exception(f"❌ Неожиданная ошибка синхронизации остатков: {str(e)}")
        flash(f'Ошибка синхронизации остатков: {str(e)}', 'danger')

    return redirect(url_for('products_list'))


@app.route('/products/<int:product_id>')
@login_required
def product_detail(product_id):
    """Детальная информация о товаре"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)

    # Проверка доступа
    if product.seller_id != current_user.seller.id:
        flash('У вас нет доступа к этому товару', 'danger')
        return redirect(url_for('products_list'))

    # Парсим JSON данные
    photos = json.loads(product.photos_json) if product.photos_json else []
    sizes = json.loads(product.sizes_json) if product.sizes_json else []
    characteristics = json.loads(product.characteristics_json) if product.characteristics_json else []
    dimensions = json.loads(product.dimensions_json) if product.dimensions_json else {}

    # Получаем остатки по складам для этого товара
    warehouse_stocks = ProductStock.query.filter_by(product_id=product.id).all()

    # Вычисляем общие остатки
    total_quantity = sum(stock.quantity for stock in warehouse_stocks)
    total_quantity_full = sum(stock.quantity_full for stock in warehouse_stocks)

    return render_template(
        'product_detail.html',
        product=product,
        photos=photos,
        sizes=sizes,
        characteristics=characteristics,
        dimensions=dimensions,
        warehouse_stocks=warehouse_stocks,
        total_quantity=total_quantity,
        total_quantity_full=total_quantity_full
    )


def _create_product_snapshot(product: Product) -> dict:
    """Создать снимок состояния продукта для истории изменений"""
    return {
        'nm_id': product.nm_id,
        'vendor_code': product.vendor_code,
        'title': product.title,
        'brand': product.brand,
        'description': product.description,
        'object_name': product.object_name,
        'price': float(product.price) if product.price else None,
        'discount_price': float(product.discount_price) if product.discount_price else None,
        'quantity': product.quantity,
        'characteristics': json.loads(product.characteristics_json) if product.characteristics_json else [],
        'last_sync': product.last_sync.isoformat() if product.last_sync else None,
        'is_active': product.is_active
    }


@app.route('/products/<int:product_id>/edit', methods=['GET', 'POST'])
@login_required
def product_edit(product_id):
    """Редактирование карточки товара"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)

    # Проверка доступа
    if product.seller_id != current_user.seller.id:
        flash('У вас нет доступа к этому товару', 'danger')
        return redirect(url_for('products_list'))

    if not current_user.seller.has_valid_api_key():
        flash('API ключ Wildberries не настроен. Настройте его в разделе "Настройки API".', 'warning')
        return redirect(url_for('api_settings'))

    # Парсим JSON данные
    characteristics = json.loads(product.characteristics_json) if product.characteristics_json else []
    sizes = json.loads(product.sizes_json) if product.sizes_json else []

    # Получаем конфигурацию доступных характеристик для категории товара
    characteristics_config = []
    try:
        if product.subject_id:
            # Если есть subject_id, используем его напрямую
            with WildberriesAPIClient(current_user.seller.wb_api_key) as client:
                config_data = client.get_card_characteristics_config(product.subject_id)
                characteristics_config = config_data.get('data', [])
        elif product.object_name:
            # Если нет subject_id, получаем характеристики по object_name
            with WildberriesAPIClient(current_user.seller.wb_api_key) as client:
                config_data = client.get_card_characteristics_by_object_name(product.object_name)
                characteristics_config = config_data.get('data', [])
    except Exception as e:
        app.logger.warning(f"Не удалось загрузить конфигурацию характеристик: {e}")

    if request.method == 'POST':
        try:
            app.logger.info(f"📝 Starting edit for product {product.id} (nmID={product.nm_id}, vendor_code={product.vendor_code})")

            # Создаем снимок ПЕРЕД изменениями
            snapshot_before = _create_product_snapshot(product)

            # Получаем данные из формы
            vendor_code = request.form.get('vendor_code', '').strip()
            title = request.form.get('title', '').strip()
            description = request.form.get('description', '').strip()
            brand = request.form.get('brand', '').strip()

            app.logger.debug(f"Form data: vendor_code={vendor_code}, title={title[:50] if title else 'N/A'}, brand={brand}")

            # Собираем характеристики из формы
            updated_characteristics = []
            for char in characteristics:
                char_id = char.get('id')
                char_name = char.get('name')
                if char_id:
                    new_value = request.form.get(f'char_{char_id}', '').strip()
                    if new_value:
                        updated_characteristics.append({
                            'id': char_id,
                            'name': char_name,
                            'value': new_value
                        })

            app.logger.debug(f"Updated characteristics count: {len(updated_characteristics)}")

            # Обновляем карточку через WB API
            with WildberriesAPIClient(
                current_user.seller.wb_api_key,
                db_logger_callback=APILog.log_request
            ) as client:
                updates = {}

                if vendor_code and vendor_code != product.vendor_code:
                    updates['vendorCode'] = vendor_code

                if title and title != product.title:
                    updates['title'] = title

                if description and description != product.description:
                    updates['description'] = description

                if brand and brand != product.brand:
                    updates['brand'] = brand

                if updated_characteristics:
                    updates['characteristics'] = updated_characteristics

                if updates:
                    app.logger.info(f"🔧 Sending updates to WB API: {list(updates.keys())}")

                    # Отправляем обновление в WB
                    try:
                        result = client.update_card(
                            product.nm_id,
                            updates,
                            log_to_db=True,
                            seller_id=current_user.seller.id
                        )
                        app.logger.info(f"✅ WB API response: {result}")
                    except Exception as api_error:
                        app.logger.error(f"❌ WB API error for nmID={product.nm_id}: {str(api_error)}")
                        app.logger.error(f"Request body: nmID={product.nm_id}, updates={updates}")
                        raise

                    # Обновляем локальную БД
                    changed_fields = []
                    if vendor_code:
                        product.vendor_code = vendor_code
                        changed_fields.append('vendor_code')
                    if title:
                        product.title = title
                        changed_fields.append('title')
                    if description:
                        product.description = description
                        changed_fields.append('description')
                    if brand:
                        product.brand = brand
                        changed_fields.append('brand')
                    if updated_characteristics:
                        product.characteristics_json = json.dumps(updated_characteristics, ensure_ascii=False)
                        changed_fields.append('characteristics')

                    product.last_sync = datetime.utcnow()

                    # Создаем снимок ПОСЛЕ изменений
                    snapshot_after = _create_product_snapshot(product)

                    # Сохраняем историю изменений
                    history = CardEditHistory(
                        product_id=product.id,
                        seller_id=current_user.seller.id,
                        action='update',
                        changed_fields=changed_fields,
                        snapshot_before=snapshot_before,
                        snapshot_after=snapshot_after,
                        wb_synced=True,
                        wb_sync_status='success'
                    )
                    db.session.add(history)
                    db.session.commit()

                    app.logger.info(f"✅ Product {product.id} updated successfully in database")
                    app.logger.info(f"📝 Created CardEditHistory record {history.id} with changed fields: {changed_fields}")
                    flash('Карточка успешно обновлена на Wildberries', 'success')
                    return redirect(url_for('product_detail', product_id=product.id))
                else:
                    app.logger.info(f"ℹ️ No changes detected for product {product.id}")
                    flash('Нет изменений для сохранения', 'info')

        except WBAuthException as e:
            app.logger.error(f"❌ Auth error: {str(e)}")
            flash(f'Ошибка авторизации WB API: {str(e)}. Проверьте API ключ в настройках.', 'danger')
        except WBAPIException as e:
            app.logger.error(f"❌ WB API error: {str(e)}")
            flash(f'Ошибка WB API: {str(e)}. Возможно, некоторые поля нельзя изменить через API или требуется полная карточка.', 'danger')
        except Exception as e:
            app.logger.exception(f"❌ Unexpected error editing product {product.id}: {e}")
            flash(f'Неожиданная ошибка при обновлении: {str(e)}. Проверьте логи для деталей.', 'danger')

    return render_template(
        'product_edit.html',
        product=product,
        characteristics=characteristics,
        characteristics_config=characteristics_config,
        sizes=sizes
    )


@app.route('/products/bulk-edit', methods=['GET', 'POST'])
@login_required
def products_bulk_edit():
    """Массовое редактирование карточек товаров"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    if not current_user.seller.has_valid_api_key():
        flash('API ключ Wildberries не настроен. Настройте его в разделе "Настройки API".', 'warning')
        return redirect(url_for('api_settings'))

    # Получаем выбранные товары из сессии или параметров
    selected_ids = request.args.getlist('ids') or request.form.getlist('product_ids')

    if not selected_ids:
        flash('Не выбраны товары для редактирования', 'warning')
        return redirect(url_for('products_list'))

    try:
        selected_ids = [int(pid) for pid in selected_ids]
    except ValueError:
        flash('Неверный формат ID товаров', 'danger')
        return redirect(url_for('products_list'))

    # Получаем выбранные товары
    products = Product.query.filter(
        Product.id.in_(selected_ids),
        Product.seller_id == current_user.seller.id
    ).all()

    if not products:
        flash('Товары не найдены', 'warning')
        return redirect(url_for('products_list'))

    # Варианты массового редактирования
    edit_operations = [
        {'id': 'update_brand', 'name': 'Обновить бренд', 'description': 'Установить одинаковый бренд для всех выбранных товаров'},
        {'id': 'append_description', 'name': 'Добавить к описанию', 'description': 'Добавить текст в конец описания всех товаров'},
        {'id': 'replace_description', 'name': 'Заменить описание', 'description': 'Заменить описание всех товаров на новое'},
        {'id': 'update_characteristic', 'name': 'Обновить характеристику', 'description': 'Изменить значение определенной характеристики'},
        {'id': 'add_characteristic', 'name': 'Добавить характеристику', 'description': 'Добавить новую характеристику ко всем товарам'},
    ]

    if request.method == 'POST':
        operation = request.form.get('operation', '')
        start_time = time.time()

        # Создаем запись bulk операции
        operation_value = request.form.get('value', '').strip()

        # Определяем описание операции
        operation_descriptions = {
            'update_brand': f'Обновление бренда на "{operation_value}"',
            'append_description': f'Добавление к описанию: "{operation_value[:50]}..."',
            'replace_description': f'Замена описания на: "{operation_value[:50]}..."',
            'update_characteristic': 'Обновление характеристики',
            'add_characteristic': 'Добавление характеристики',
        }

        bulk_operation = BulkEditHistory(
            seller_id=current_user.seller.id,
            operation_type=operation,
            operation_params={'value': operation_value} if operation_value else {},
            description=operation_descriptions.get(operation, 'Массовая операция'),
            total_products=len(products),
            status='in_progress'
        )
        db.session.add(bulk_operation)
        db.session.commit()  # Коммитим чтобы получить ID

        app.logger.info(f"🚀 Starting bulk operation {bulk_operation.id}: {operation} for {len(products)} products")

        try:
            with WildberriesAPIClient(
                current_user.seller.wb_api_key,
                db_logger_callback=APILog.log_request
            ) as client:
                success_count = 0
                error_count = 0
                errors = []

                if operation == 'update_brand':
                    new_brand = operation_value
                    if not new_brand:
                        flash('Укажите новый бренд', 'warning')
                        bulk_operation.status = 'failed'
                        bulk_operation.completed_at = datetime.utcnow()
                        db.session.commit()
                        return render_template('products_bulk_edit.html',
                                             products=[p.to_dict() for p in products],
                                             edit_operations=edit_operations)

                    for product in products:
                        try:
                            # Снимок ДО
                            snapshot_before = _create_product_snapshot(product)

                            client.update_card(
                                product.nm_id,
                                {'brand': new_brand},
                                log_to_db=True,
                                seller_id=current_user.seller.id
                            )
                            product.brand = new_brand
                            product.last_sync = datetime.utcnow()

                            # Снимок ПОСЛЕ
                            snapshot_after = _create_product_snapshot(product)

                            # Создаем запись истории
                            card_history = CardEditHistory(
                                product_id=product.id,
                                seller_id=current_user.seller.id,
                                bulk_edit_id=bulk_operation.id,
                                action='update',
                                changed_fields=['brand'],
                                snapshot_before=snapshot_before,
                                snapshot_after=snapshot_after,
                                wb_synced=True,
                                wb_sync_status='success'
                            )
                            db.session.add(card_history)

                            success_count += 1
                        except Exception as e:
                            error_count += 1
                            error_msg = f"Товар {product.vendor_code}: {str(e)}"
                            errors.append(error_msg)
                            app.logger.error(f"Error in bulk operation: {error_msg}")

                    db.session.commit()

                elif operation == 'append_description':
                    append_text = operation_value
                    if not append_text:
                        flash('Укажите текст для добавления', 'warning')
                        bulk_operation.status = 'failed'
                        bulk_operation.completed_at = datetime.utcnow()
                        db.session.commit()
                        return render_template('products_bulk_edit.html',
                                             products=[p.to_dict() for p in products],
                                             edit_operations=edit_operations)

                    for product in products:
                        try:
                            snapshot_before = _create_product_snapshot(product)

                            current_desc = product.description or ''
                            new_desc = f"{current_desc}\n\n{append_text}".strip()
                            client.update_card(
                                product.nm_id,
                                {'description': new_desc},
                                log_to_db=True,
                                seller_id=current_user.seller.id
                            )
                            product.description = new_desc
                            product.last_sync = datetime.utcnow()

                            snapshot_after = _create_product_snapshot(product)

                            card_history = CardEditHistory(
                                product_id=product.id,
                                seller_id=current_user.seller.id,
                                bulk_edit_id=bulk_operation.id,
                                action='update',
                                changed_fields=['description'],
                                snapshot_before=snapshot_before,
                                snapshot_after=snapshot_after,
                                wb_synced=True,
                                wb_sync_status='success'
                            )
                            db.session.add(card_history)

                            success_count += 1
                        except Exception as e:
                            error_count += 1
                            error_msg = f"Товар {product.vendor_code}: {str(e)}"
                            errors.append(error_msg)

                    db.session.commit()

                elif operation == 'replace_description':
                    new_description = operation_value
                    if not new_description:
                        flash('Укажите новое описание', 'warning')
                        bulk_operation.status = 'failed'
                        bulk_operation.completed_at = datetime.utcnow()
                        db.session.commit()
                        return render_template('products_bulk_edit.html',
                                             products=[p.to_dict() for p in products],
                                             edit_operations=edit_operations)

                    for product in products:
                        try:
                            snapshot_before = _create_product_snapshot(product)

                            client.update_card(
                                product.nm_id,
                                {'description': new_description},
                                log_to_db=True,
                                seller_id=current_user.seller.id
                            )
                            product.description = new_description
                            product.last_sync = datetime.utcnow()

                            snapshot_after = _create_product_snapshot(product)

                            card_history = CardEditHistory(
                                product_id=product.id,
                                seller_id=current_user.seller.id,
                                bulk_edit_id=bulk_operation.id,
                                action='update',
                                changed_fields=['description'],
                                snapshot_before=snapshot_before,
                                snapshot_after=snapshot_after,
                                wb_synced=True,
                                wb_sync_status='success'
                            )
                            db.session.add(card_history)

                            success_count += 1
                        except Exception as e:
                            error_count += 1
                            error_msg = f"Товар {product.vendor_code}: {str(e)}"
                            errors.append(error_msg)

                    db.session.commit()

                elif operation == 'update_characteristic':
                    characteristic_id = request.form.get('char_id', '').strip()
                    new_value = operation_value

                    if not characteristic_id or not new_value:
                        flash('Укажите ID характеристики и новое значение', 'warning')
                        bulk_operation.status = 'failed'
                        bulk_operation.completed_at = datetime.utcnow()
                        db.session.commit()
                        return render_template('products_bulk_edit.html',
                                             products=[p.to_dict() for p in products],
                                             edit_operations=edit_operations)

                    for product in products:
                        try:
                            snapshot_before = _create_product_snapshot(product)

                            # Получаем текущие характеристики
                            current_characteristics = product.get_characteristics()

                            # Обновляем значение характеристики
                            char_found = False
                            for char in current_characteristics:
                                if str(char.get('id')) == characteristic_id:
                                    char['value'] = new_value
                                    char_found = True
                                    break

                            if not char_found:
                                # Добавляем новую характеристику если не нашли
                                current_characteristics.append({
                                    'id': int(characteristic_id),
                                    'value': new_value
                                })

                            # Обновляем через API
                            client.update_card(
                                product.nm_id,
                                {'characteristics': current_characteristics},
                                log_to_db=True,
                                seller_id=current_user.seller.id
                            )
                            product.set_characteristics(current_characteristics)
                            product.last_sync = datetime.utcnow()

                            snapshot_after = _create_product_snapshot(product)

                            card_history = CardEditHistory(
                                product_id=product.id,
                                seller_id=current_user.seller.id,
                                bulk_edit_id=bulk_operation.id,
                                action='update',
                                changed_fields=['characteristics'],
                                snapshot_before=snapshot_before,
                                snapshot_after=snapshot_after,
                                wb_synced=True,
                                wb_sync_status='success'
                            )
                            db.session.add(card_history)

                            success_count += 1
                        except Exception as e:
                            error_count += 1
                            error_msg = f"Товар {product.vendor_code}: {str(e)}"
                            errors.append(error_msg)

                    db.session.commit()

                elif operation == 'add_characteristic':
                    characteristic_id = request.form.get('char_id', '').strip()
                    new_value = operation_value

                    if not characteristic_id or not new_value:
                        flash('Укажите ID характеристики и значение', 'warning')
                        bulk_operation.status = 'failed'
                        bulk_operation.completed_at = datetime.utcnow()
                        db.session.commit()
                        return render_template('products_bulk_edit.html',
                                             products=[p.to_dict() for p in products],
                                             edit_operations=edit_operations)

                    for product in products:
                        try:
                            snapshot_before = _create_product_snapshot(product)

                            # Получаем текущие характеристики
                            current_characteristics = product.get_characteristics()

                            # Проверяем, нет ли уже такой характеристики
                            char_exists = any(str(char.get('id')) == characteristic_id for char in current_characteristics)

                            if not char_exists:
                                # Добавляем новую характеристику
                                current_characteristics.append({
                                    'id': int(characteristic_id),
                                    'value': new_value
                                })

                                # Обновляем через API
                                client.update_card(
                                    product.nm_id,
                                    {'characteristics': current_characteristics},
                                    log_to_db=True,
                                    seller_id=current_user.seller.id
                                )
                                product.set_characteristics(current_characteristics)
                                product.last_sync = datetime.utcnow()

                                snapshot_after = _create_product_snapshot(product)

                                card_history = CardEditHistory(
                                    product_id=product.id,
                                    seller_id=current_user.seller.id,
                                    bulk_edit_id=bulk_operation.id,
                                    action='update',
                                    changed_fields=['characteristics'],
                                    snapshot_before=snapshot_before,
                                    snapshot_after=snapshot_after,
                                    wb_synced=True,
                                    wb_sync_status='success'
                                )
                                db.session.add(card_history)
                                success_count += 1
                            else:
                                # Характеристика уже существует, пропускаем
                                app.logger.info(f"Product {product.vendor_code} already has characteristic {characteristic_id}, skipping")
                                success_count += 1  # Считаем успешным (характеристика уже есть)

                        except Exception as e:
                            error_count += 1
                            error_msg = f"Товар {product.vendor_code}: {str(e)}"
                            errors.append(error_msg)

                    db.session.commit()

                else:
                    # Неизвестная операция
                    flash(f'Неизвестная операция: {operation}', 'danger')
                    bulk_operation.status = 'failed'
                    bulk_operation.completed_at = datetime.utcnow()
                    db.session.commit()
                    return render_template('products_bulk_edit.html',
                                         products=products,
                                         edit_operations=edit_operations)

                # Обновляем статус bulk операции
                bulk_operation.success_count = success_count
                bulk_operation.error_count = error_count
                bulk_operation.errors_details = errors if errors else None
                bulk_operation.status = 'completed'
                bulk_operation.wb_synced = True
                bulk_operation.completed_at = datetime.utcnow()
                bulk_operation.duration_seconds = time.time() - start_time
                db.session.commit()

                app.logger.info(f"✅ Bulk operation {bulk_operation.id} completed: {success_count} success, {error_count} errors")

                # Показываем результаты
                if success_count > 0:
                    flash(f'Успешно обновлено товаров: {success_count}', 'success')
                if error_count > 0:
                    flash(f'Ошибок при обновлении: {error_count}', 'warning')
                    for error in errors[:5]:  # Показываем только первые 5 ошибок
                        flash(error, 'danger')

                flash(f'Операция сохранена в историю. <a href="{url_for("bulk_edit_history_detail", bulk_id=bulk_operation.id)}" class="underline">Посмотреть детали</a>', 'info')
                return redirect(url_for('bulk_edit_history_detail', bulk_id=bulk_operation.id))

        except Exception as e:
            app.logger.exception(f"Ошибка массового редактирования: {e}")

            # Помечаем bulk операцию как failed
            bulk_operation.status = 'failed'
            bulk_operation.completed_at = datetime.utcnow()
            bulk_operation.duration_seconds = time.time() - start_time
            bulk_operation.errors_details = [str(e)]
            db.session.commit()

            flash(f'Ошибка: {str(e)}', 'danger')
            return redirect(url_for('bulk_edit_history_detail', bulk_id=bulk_operation.id))

    return render_template(
        'products_bulk_edit.html',
        products=[p.to_dict() for p in products],
        edit_operations=edit_operations
    )


# ============= ИСТОРИЯ ИЗМЕНЕНИЙ КАРТОЧЕК =============

@app.route('/products/<int:product_id>/history')
@login_required
def product_edit_history(product_id):
    """Просмотр истории изменений карточки товара"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)

    # Проверка доступа
    if product.seller_id != current_user.seller.id:
        flash('У вас нет доступа к этому товару', 'danger')
        return redirect(url_for('products_list'))

    # Получаем историю изменений
    history_records = CardEditHistory.query.filter_by(
        product_id=product.id
    ).order_by(CardEditHistory.created_at.desc()).all()

    return render_template(
        'product_edit_history.html',
        product=product,
        history_records=history_records
    )


@app.route('/products/<int:product_id>/history/<int:history_id>/revert', methods=['POST'])
@login_required
def revert_product_edit(product_id, history_id):
    """Откатить изменения карточки товара к предыдущему состоянию"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)

    # Проверка доступа
    if product.seller_id != current_user.seller.id:
        flash('У вас нет доступа к этому товару', 'danger')
        return redirect(url_for('products_list'))

    if not current_user.seller.has_valid_api_key():
        flash('API ключ Wildberries не настроен.', 'warning')
        return redirect(url_for('api_settings'))

    # Находим запись истории
    history = CardEditHistory.query.get_or_404(history_id)

    # Проверка что история принадлежит этому продукту
    if history.product_id != product.id:
        abort(403)

    # Проверка что можно откатить
    if not history.can_revert():
        flash('Это изменение нельзя откатить', 'warning')
        return redirect(url_for('product_edit_history', product_id=product.id))

    try:
        app.logger.info(f"🔄 Reverting product {product.id} to state before history {history.id}")

        # Создаем снимок текущего состояния (до отката)
        snapshot_before_revert = _create_product_snapshot(product)

        # Получаем состояние для восстановления
        snapshot_to_restore = history.snapshot_before

        # Подготавливаем данные для отправки в WB API
        updates = {}
        reverted_fields = []

        if 'vendor_code' in history.changed_fields and 'vendor_code' in snapshot_to_restore:
            updates['vendorCode'] = snapshot_to_restore['vendor_code']
            reverted_fields.append('vendor_code')

        if 'title' in history.changed_fields and 'title' in snapshot_to_restore:
            updates['title'] = snapshot_to_restore['title']
            reverted_fields.append('title')

        if 'description' in history.changed_fields and 'description' in snapshot_to_restore:
            updates['description'] = snapshot_to_restore['description']
            reverted_fields.append('description')

        if 'brand' in history.changed_fields and 'brand' in snapshot_to_restore:
            updates['brand'] = snapshot_to_restore['brand']
            reverted_fields.append('brand')

        if 'characteristics' in history.changed_fields and 'characteristics' in snapshot_to_restore:
            updates['characteristics'] = snapshot_to_restore['characteristics']
            reverted_fields.append('characteristics')

        if not updates:
            flash('Нет полей для отката', 'warning')
            return redirect(url_for('product_edit_history', product_id=product.id))

        # Отправляем обновление в WB API
        with WildberriesAPIClient(
            current_user.seller.wb_api_key,
            db_logger_callback=APILog.log_request
        ) as client:
            result = client.update_card(
                product.nm_id,
                updates,
                log_to_db=True,
                seller_id=current_user.seller.id
            )
            app.logger.info(f"✅ Revert WB API response: {result}")

        # Обновляем локальную БД
        if 'vendor_code' in reverted_fields:
            product.vendor_code = snapshot_to_restore['vendor_code']
        if 'title' in reverted_fields:
            product.title = snapshot_to_restore['title']
        if 'description' in reverted_fields:
            product.description = snapshot_to_restore['description']
        if 'brand' in reverted_fields:
            product.brand = snapshot_to_restore['brand']
        if 'characteristics' in reverted_fields:
            product.characteristics_json = json.dumps(
                snapshot_to_restore['characteristics'],
                ensure_ascii=False
            )

        product.last_sync = datetime.utcnow()

        # Создаем снимок после отката
        snapshot_after_revert = _create_product_snapshot(product)

        # Помечаем оригинальную запись как откаченную
        history.reverted = True
        history.reverted_at = datetime.utcnow()

        # Создаем новую запись истории для самого отката
        revert_history = CardEditHistory(
            product_id=product.id,
            seller_id=current_user.seller.id,
            action='revert',
            changed_fields=reverted_fields,
            snapshot_before=snapshot_before_revert,
            snapshot_after=snapshot_after_revert,
            wb_synced=True,
            wb_sync_status='success',
            reverted_by_history_id=history.id,
            user_comment=f'Откат изменений от {history.created_at.strftime("%Y-%m-%d %H:%M:%S")}'
        )

        # Связываем откат с оригинальной записью
        history.reverted_by_history_id = revert_history.id

        db.session.add(revert_history)
        db.session.commit()

        app.logger.info(f"✅ Product {product.id} reverted successfully")
        app.logger.info(f"📝 Created revert history record {revert_history.id}")
        flash('Изменения успешно откачены', 'success')

    except WBAuthException as e:
        app.logger.error(f"❌ Auth error during revert: {str(e)}")
        flash(f'Ошибка авторизации WB API: {str(e)}', 'danger')
    except WBAPIException as e:
        app.logger.error(f"❌ WB API error during revert: {str(e)}")
        flash(f'Ошибка WB API: {str(e)}', 'danger')
    except Exception as e:
        app.logger.exception(f"❌ Unexpected error during revert: {e}")
        flash(f'Ошибка при откате: {str(e)}', 'danger')

    return redirect(url_for('product_edit_history', product_id=product.id))


# ============= ИСТОРИЯ МАССОВЫХ ИЗМЕНЕНИЙ =============

@app.route('/bulk-history')
@login_required
def bulk_edit_history():
    """Просмотр истории массовых изменений"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    page = request.args.get('page', 1, type=int)
    per_page = 20

    # История для текущего продавца
    pagination = BulkEditHistory.query.filter_by(
        seller_id=current_user.seller.id
    ).order_by(BulkEditHistory.created_at.desc()).paginate(
        page=page,
        per_page=per_page,
        error_out=False
    )

    return render_template(
        'bulk_edit_history.html',
        history_records=pagination.items,
        pagination=pagination
    )


@app.route('/bulk-history/<int:bulk_id>')
@login_required
def bulk_edit_history_detail(bulk_id):
    """Детальная информация о массовой операции"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    bulk_operation = BulkEditHistory.query.get_or_404(bulk_id)

    # Проверка доступа
    if bulk_operation.seller_id != current_user.seller.id:
        flash('У вас нет доступа к этой операции', 'danger')
        return redirect(url_for('bulk_edit_history'))

    # Получаем все изменения в рамках этой операции
    product_changes = CardEditHistory.query.filter_by(
        bulk_edit_id=bulk_id
    ).order_by(CardEditHistory.created_at.asc()).all()

    return render_template(
        'bulk_edit_history_detail.html',
        bulk_operation=bulk_operation,
        product_changes=product_changes
    )


@app.route('/bulk-history/<int:bulk_id>/revert', methods=['POST'])
@login_required
def revert_bulk_edit(bulk_id):
    """Откатить массовую операцию"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    bulk_operation = BulkEditHistory.query.get_or_404(bulk_id)

    # Проверка доступа
    if bulk_operation.seller_id != current_user.seller.id:
        flash('У вас нет доступа к этой операции', 'danger')
        return redirect(url_for('bulk_edit_history'))

    if not current_user.seller.has_valid_api_key():
        flash('API ключ Wildberries не настроен.', 'warning')
        return redirect(url_for('api_settings'))

    # Проверка что можно откатить
    if not bulk_operation.can_revert():
        flash('Эту операцию нельзя откатить', 'warning')
        return redirect(url_for('bulk_edit_history_detail', bulk_id=bulk_id))

    try:
        app.logger.info(f"🔄 Reverting bulk operation {bulk_id}")

        # Получаем все изменения в рамках операции
        product_changes = CardEditHistory.query.filter_by(
            bulk_edit_id=bulk_id,
            reverted=False
        ).all()

        success_count = 0
        error_count = 0
        errors = []

        with WildberriesAPIClient(
            current_user.seller.wb_api_key,
            db_logger_callback=APILog.log_request
        ) as client:
            for change in product_changes:
                try:
                    if not change.can_revert():
                        continue

                    product = Product.query.get(change.product_id)
                    if not product:
                        continue

                    # Подготавливаем данные для отката
                    snapshot_to_restore = change.snapshot_before
                    updates = {}
                    reverted_fields = []

                    for field in change.changed_fields:
                        if field in snapshot_to_restore:
                            if field == 'vendor_code':
                                updates['vendorCode'] = snapshot_to_restore[field]
                            elif field in ['title', 'description', 'brand', 'characteristics']:
                                updates[field] = snapshot_to_restore[field]
                            reverted_fields.append(field)

                    if not updates:
                        continue

                    # Откатываем через WB API
                    result = client.update_card(
                        product.nm_id,
                        updates,
                        log_to_db=True,
                        seller_id=current_user.seller.id
                    )

                    # Обновляем локальную БД
                    for field in reverted_fields:
                        if field == 'vendor_code':
                            product.vendor_code = snapshot_to_restore[field]
                        elif field == 'title':
                            product.title = snapshot_to_restore[field]
                        elif field == 'description':
                            product.description = snapshot_to_restore[field]
                        elif field == 'brand':
                            product.brand = snapshot_to_restore[field]
                        elif field == 'characteristics':
                            product.characteristics_json = json.dumps(
                                snapshot_to_restore[field],
                                ensure_ascii=False
                            )

                    product.last_sync = datetime.utcnow()

                    # Помечаем изменение как откаченное
                    change.reverted = True
                    change.reverted_at = datetime.utcnow()

                    success_count += 1

                except Exception as e:
                    error_count += 1
                    errors.append(f"Товар {product.vendor_code if product else change.product_id}: {str(e)}")
                    app.logger.error(f"Error reverting product {change.product_id}: {e}")

        # Помечаем bulk операцию как откаченную
        bulk_operation.reverted = True
        bulk_operation.reverted_at = datetime.utcnow()
        bulk_operation.reverted_by_user_id = current_user.id

        db.session.commit()

        app.logger.info(f"✅ Bulk operation {bulk_id} reverted: {success_count} success, {error_count} errors")

        if success_count > 0:
            flash(f'Откачено изменений: {success_count}', 'success')
        if error_count > 0:
            flash(f'Ошибок при откате: {error_count}', 'warning')
            for error in errors[:5]:
                flash(error, 'danger')

    except Exception as e:
        app.logger.exception(f"❌ Unexpected error during bulk revert: {e}")
        flash(f'Ошибка при откате: {str(e)}', 'danger')

    return redirect(url_for('bulk_edit_history_detail', bulk_id=bulk_id))


# ============= API ЛОГИ =============

@app.route('/api-logs')
@login_required
def api_logs():
    """Просмотр логов API запросов"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    page = request.args.get('page', 1, type=int)
    per_page = 50

    # Логи для текущего продавца
    pagination = APILog.query.filter_by(
        seller_id=current_user.seller.id
    ).order_by(
        APILog.created_at.desc()
    ).paginate(
        page=page,
        per_page=per_page,
        error_out=False
    )

    logs = pagination.items

    # Статистика
    total_requests = current_user.seller.api_logs.count()
    failed_requests = current_user.seller.api_logs.filter_by(success=False).count()
    success_rate = ((total_requests - failed_requests) / total_requests * 100) if total_requests > 0 else 0

    return render_template(
        'api_logs.html',
        logs=logs,
        pagination=pagination,
        total_requests=total_requests,
        failed_requests=failed_requests,
        success_rate=success_rate
    )


# ============= API ENDPOINTS =============

@app.route('/api/characteristics/categories')
@login_required
def api_characteristics_categories():
    """Получить список всех категорий (object_name) из товаров продавца"""
    if not current_user.seller:
        return {'error': 'No seller profile'}, 403

    # Получаем уникальные object_name из товаров
    categories = db.session.query(Product.object_name).filter(
        Product.seller_id == current_user.seller.id,
        Product.object_name.isnot(None),
        Product.object_name != ''
    ).distinct().all()

    # Преобразуем в список строк
    category_list = [cat[0] for cat in categories]

    return {
        'categories': sorted(category_list),
        'count': len(category_list)
    }


@app.route('/api/characteristics/<object_name>')
@login_required
def api_characteristics_by_category(object_name):
    """
    Получить характеристики для категории из WB API

    Args:
        object_name: Название категории (например, "Футболки")

    Returns:
        JSON с характеристиками и их возможными значениями
    """
    if not current_user.seller:
        return {'error': 'No seller profile'}, 403

    if not current_user.seller.has_valid_api_key():
        return {'error': 'WB API key not configured'}, 400

    app.logger.info(f"📋 API request for characteristics: category='{object_name}'")

    # Сначала попробуем получить из WB API
    try:
        with WildberriesAPIClient(current_user.seller.wb_api_key) as client:
            result = client.get_card_characteristics_by_object_name(object_name)

            # Преобразуем результат в более удобный формат
            characteristics = []
            for item in result.get('data', []):
                char = {
                    'id': item.get('charcID'),
                    'name': item.get('name'),
                    'required': item.get('required', False),
                    'max_count': item.get('maxCount', 1),  # Количество возможных значений
                    'unit_name': item.get('unitName'),
                    'values': []
                }

                # Добавляем возможные значения если они есть
                if item.get('dictionary'):
                    for dict_item in item['dictionary']:
                        char['values'].append({
                            'id': dict_item.get('unitID'),
                            'value': dict_item.get('value')
                        })

                characteristics.append(char)

            app.logger.info(f"✅ Loaded {len(characteristics)} characteristics for '{object_name}'")

            return {
                'object_name': object_name,
                'characteristics': characteristics,
                'count': len(characteristics)
            }

    except WBAPIException as e:
        app.logger.error(f"❌ WB API error getting characteristics for '{object_name}': {e}")

        # Если endpoint не найден (404), предлагаем альтернативу
        if '404' in str(e):
            return {
                'error': 'WB API endpoint для получения характеристик недоступен (404). Этот endpoint может не поддерживаться для данной категории товаров или был изменён в новой версии API.',
                'suggestion': 'Вы можете редактировать товары по одному или использовать другие операции массового редактирования (бренд, описание).',
                'category': object_name
            }, 404

        return {'error': str(e)}, 400
    except Exception as e:
        app.logger.exception(f"💥 Unexpected error getting characteristics for '{object_name}': {e}")
        return {'error': 'Internal server error'}, 500


@app.route('/api/products/<int:product_id>/characteristics')
@login_required
def api_product_characteristics(product_id):
    """Получить текущие характеристики товара"""
    if not current_user.seller:
        return {'error': 'No seller profile'}, 403

    product = Product.query.get_or_404(product_id)

    # Проверка доступа
    if product.seller_id != current_user.seller.id:
        return {'error': 'Access denied'}, 403

    return {
        'product_id': product.id,
        'nm_id': product.nm_id,
        'vendor_code': product.vendor_code,
        'object_name': product.object_name,
        'characteristics': product.get_characteristics()
    }


# ============= МОНИТОРИНГ ЦЕН =============

@app.route('/price-monitor/settings', methods=['GET', 'POST'])
@login_required
def price_monitor_settings():
    """Настройки мониторинга цен"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'warning')
        return redirect(url_for('dashboard'))

    seller = current_user.seller

    # Получаем или создаем настройки
    settings = PriceMonitorSettings.query.filter_by(seller_id=seller.id).first()
    if not settings:
        settings = PriceMonitorSettings(seller_id=seller.id)
        db.session.add(settings)
        db.session.commit()

    if request.method == 'POST':
        try:
            settings.is_enabled = request.form.get('is_enabled') == 'on'
            settings.monitor_prices = request.form.get('monitor_prices') == 'on'
            settings.monitor_stocks = request.form.get('monitor_stocks') == 'on'
            settings.sync_interval_minutes = int(request.form.get('sync_interval_minutes', 60))
            settings.price_change_threshold_percent = float(request.form.get('price_change_threshold_percent', 10.0))
            settings.stock_change_threshold_percent = float(request.form.get('stock_change_threshold_percent', 50.0))

            db.session.commit()
            flash('Настройки мониторинга сохранены', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка сохранения настроек: {e}', 'danger')

        return redirect(url_for('price_monitor_settings'))

    return render_template('price_monitor_settings.html', settings=settings)


@app.route('/price-monitor/suspicious', methods=['GET'])
@login_required
def suspicious_price_changes():
    """Страница подозрительных изменений цен"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'warning')
        return redirect(url_for('dashboard'))

    seller = current_user.seller

    # Фильтры
    change_type = request.args.get('change_type', '')
    is_reviewed = request.args.get('is_reviewed', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    search = request.args.get('search', '')

    # Сортировка
    sort_by = request.args.get('sort_by', 'created_at')
    sort_order = request.args.get('sort_order', 'desc')

    # Пагинация
    page = request.args.get('page', 1, type=int)
    per_page = 50

    # Базовый запрос
    query = SuspiciousPriceChange.query.filter_by(seller_id=seller.id)

    # Применяем фильтры
    if change_type:
        query = query.filter(SuspiciousPriceChange.change_type == change_type)

    if is_reviewed == 'true':
        query = query.filter(SuspiciousPriceChange.is_reviewed == True)
    elif is_reviewed == 'false':
        query = query.filter(SuspiciousPriceChange.is_reviewed == False)

    if date_from:
        try:
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(SuspiciousPriceChange.created_at >= date_from_obj)
        except ValueError:
            pass

    if date_to:
        try:
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d')
            # Добавляем 1 день чтобы включить весь день
            from datetime import timedelta
            date_to_obj = date_to_obj + timedelta(days=1)
            query = query.filter(SuspiciousPriceChange.created_at < date_to_obj)
        except ValueError:
            pass

    if search:
        # Поиск по товару
        query = query.join(Product).filter(
            or_(
                Product.vendor_code.ilike(f'%{search}%'),
                Product.title.ilike(f'%{search}%'),
                Product.brand.ilike(f'%{search}%')
            )
        )

    # Применяем сортировку
    if sort_by == 'created_at':
        if sort_order == 'desc':
            query = query.order_by(SuspiciousPriceChange.created_at.desc())
        else:
            query = query.order_by(SuspiciousPriceChange.created_at.asc())
    elif sort_by == 'change_percent':
        if sort_order == 'desc':
            query = query.order_by(SuspiciousPriceChange.change_percent.desc())
        else:
            query = query.order_by(SuspiciousPriceChange.change_percent.asc())

    # Пагинация
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    changes = pagination.items

    # Получаем статистику
    total_changes = SuspiciousPriceChange.query.filter_by(seller_id=seller.id).count()
    unreviewed_changes = SuspiciousPriceChange.query.filter_by(seller_id=seller.id, is_reviewed=False).count()

    return render_template(
        'suspicious_price_changes.html',
        changes=changes,
        pagination=pagination,
        total_changes=total_changes,
        unreviewed_changes=unreviewed_changes,
        # Фильтры для формы
        change_type=change_type,
        is_reviewed=is_reviewed,
        date_from=date_from,
        date_to=date_to,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order
    )


@app.route('/api/price-monitor/settings', methods=['GET', 'POST'])
@login_required
def api_price_monitor_settings():
    """API для получения/обновления настроек мониторинга"""
    if not current_user.seller:
        return {'error': 'Seller profile not found'}, 404

    seller = current_user.seller

    if request.method == 'GET':
        settings = PriceMonitorSettings.query.filter_by(seller_id=seller.id).first()
        if not settings:
            settings = PriceMonitorSettings(seller_id=seller.id)
            db.session.add(settings)
            db.session.commit()

        return settings.to_dict()

    # POST - обновление настроек
    try:
        data = request.get_json()
        settings = PriceMonitorSettings.query.filter_by(seller_id=seller.id).first()
        if not settings:
            settings = PriceMonitorSettings(seller_id=seller.id)
            db.session.add(settings)

        if 'is_enabled' in data:
            settings.is_enabled = bool(data['is_enabled'])
        if 'monitor_prices' in data:
            settings.monitor_prices = bool(data['monitor_prices'])
        if 'monitor_stocks' in data:
            settings.monitor_stocks = bool(data['monitor_stocks'])
        if 'sync_interval_minutes' in data:
            settings.sync_interval_minutes = int(data['sync_interval_minutes'])
        if 'price_change_threshold_percent' in data:
            settings.price_change_threshold_percent = float(data['price_change_threshold_percent'])
        if 'stock_change_threshold_percent' in data:
            settings.stock_change_threshold_percent = float(data['stock_change_threshold_percent'])

        db.session.commit()
        return settings.to_dict()

    except Exception as e:
        db.session.rollback()
        return {'error': str(e)}, 400


@app.route('/api/price-monitor/suspicious-changes', methods=['GET'])
@login_required
def api_suspicious_price_changes():
    """API для получения списка подозрительных изменений"""
    if not current_user.seller:
        return {'error': 'Seller profile not found'}, 404

    seller = current_user.seller

    # Фильтры из query параметров
    change_type = request.args.get('change_type')
    is_reviewed = request.args.get('is_reviewed')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    # Пагинация
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)

    # Базовый запрос
    query = SuspiciousPriceChange.query.filter_by(seller_id=seller.id)

    # Применяем фильтры
    if change_type:
        query = query.filter(SuspiciousPriceChange.change_type == change_type)

    if is_reviewed is not None:
        query = query.filter(SuspiciousPriceChange.is_reviewed == (is_reviewed.lower() == 'true'))

    if date_from:
        try:
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(SuspiciousPriceChange.created_at >= date_from_obj)
        except ValueError:
            pass

    if date_to:
        try:
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d')
            from datetime import timedelta
            date_to_obj = date_to_obj + timedelta(days=1)
            query = query.filter(SuspiciousPriceChange.created_at < date_to_obj)
        except ValueError:
            pass

    # Сортировка
    query = query.order_by(SuspiciousPriceChange.created_at.desc())

    # Пагинация
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return {
        'items': [change.to_dict() for change in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'page': pagination.page,
        'per_page': pagination.per_page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }


@app.route('/api/price-monitor/suspicious-changes/<int:change_id>/review', methods=['POST'])
@login_required
def api_mark_change_reviewed(change_id):
    """API для пометки изменения как просмотренного"""
    if not current_user.seller:
        return {'error': 'Seller profile not found'}, 404

    change = SuspiciousPriceChange.query.get_or_404(change_id)

    # Проверка доступа
    if change.seller_id != current_user.seller.id:
        return {'error': 'Access denied'}, 403

    try:
        data = request.get_json() or {}
        change.is_reviewed = True
        change.reviewed_at = datetime.utcnow()
        change.reviewed_by_user_id = current_user.id
        if 'notes' in data:
            change.notes = data['notes']

        db.session.commit()
        return change.to_dict()

    except Exception as e:
        db.session.rollback()
        return {'error': str(e)}, 400


@app.route('/api/price-monitor/sync', methods=['POST'])
@login_required
def api_manual_price_sync():
    """API для ручного запуска синхронизации цен"""
    if not current_user.seller:
        return {'error': 'Seller profile not found'}, 404

    seller = current_user.seller

    # Проверяем настройки
    settings = PriceMonitorSettings.query.filter_by(seller_id=seller.id).first()
    if not settings:
        return {'error': 'Price monitoring settings not found'}, 404

    if not settings.is_enabled:
        return {'error': 'Price monitoring is disabled'}, 400

    # Проверяем наличие API ключа
    if not seller.has_valid_api_key():
        return {'error': 'WB API key not configured'}, 400

    try:
        # Выполняем синхронизацию
        result = perform_price_monitoring_sync(seller, settings)
        return result

    except Exception as e:
        return {'error': str(e)}, 500


@app.route('/api/price-monitor/history/<int:product_id>', methods=['GET'])
@login_required
def api_price_history(product_id):
    """API для получения истории изменений цен товара"""
    if not current_user.seller:
        return {'error': 'Seller profile not found'}, 404

    product = Product.query.get_or_404(product_id)

    # Проверка доступа
    if product.seller_id != current_user.seller.id:
        return {'error': 'Access denied'}, 403

    # Получаем историю
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 100, type=int)

    query = PriceHistory.query.filter_by(product_id=product_id).order_by(PriceHistory.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return {
        'product': product.to_dict(),
        'history': [h.to_dict() for h in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'page': pagination.page,
        'per_page': pagination.per_page,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }


def perform_price_monitoring_sync(seller: Seller, settings: PriceMonitorSettings) -> dict:
    """
    Выполняет синхронизацию цен и остатков для мониторинга

    Args:
        seller: Продавец
        settings: Настройки мониторинга

    Returns:
        dict: Результат синхронизации
    """
    # Обновляем статус
    settings.last_sync_status = 'running'
    settings.last_sync_at = datetime.utcnow()
    db.session.commit()

    try:
        # Создаем клиент API
        wb_client = WildberriesAPIClient(seller.wb_api_key)

        # Получаем ВСЕ товары через cursor-based пагинацию с детальным логированием
        app.logger.info(f"Starting to fetch all products for seller {seller.id} using cursor-based pagination...")

        all_cards = []
        cursor_updated_at = None
        cursor_nm_id = None
        page_num = 0

        while True:
            page_num += 1
            msg = f"📄 Fetching page {page_num} (cursor: updatedAt={cursor_updated_at}, nmID={cursor_nm_id})..."
            app.logger.info(msg)
            print(msg, flush=True)  # Явный вывод в stdout для Docker

            try:
                # Получаем одну страницу
                data = wb_client.get_cards_list(
                    limit=100,
                    cursor_updated_at=cursor_updated_at,
                    cursor_nm_id=cursor_nm_id
                )
            except Exception as e:
                err_msg = f"❌ Failed to fetch page {page_num}: {str(e)}"
                app.logger.error(err_msg)
                print(err_msg, flush=True)
                raise Exception(f'Failed to fetch products from WB API on page {page_num}: {str(e)}')

            # Получаем карточки из ответа
            page_cards = data.get('cards', [])

            if not page_cards:
                msg = f"⏹ No more cards on page {page_num}. Pagination complete."
                app.logger.info(msg)
                print(msg, flush=True)
                break

            all_cards.extend(page_cards)
            msg = f"✓ Page {page_num}: loaded {len(page_cards)} cards. Total so far: {len(all_cards)}"
            app.logger.info(msg)
            print(msg, flush=True)

            # Получаем cursor для следующей страницы
            cursor = data.get('cursor')
            if not cursor:
                msg = f"⏹ No cursor in response on page {page_num}. This is the last page."
                app.logger.info(msg)
                print(msg, flush=True)
                break

            # Логируем total если он есть (но НЕ используем для остановки пагинации)
            # Причина: WB API v2 может возвращать total=limit (100) вместо реального количества товаров
            total = cursor.get('total', 0)
            if total > 0:
                msg = f"📊 Cursor contains total field: {total} (current loaded: {len(all_cards)}) - continuing pagination..."
                app.logger.info(msg)
                print(msg, flush=True)

            # Получаем данные для следующего cursor
            next_updated_at = cursor.get('updatedAt')
            next_nm_id = cursor.get('nmID')

            if not next_updated_at or not next_nm_id:
                msg = f"⏹ No cursor data (updatedAt={next_updated_at}, nmID={next_nm_id}). Last page reached."
                app.logger.info(msg)
                print(msg, flush=True)
                break

            # Проверка на зацикливание
            if cursor_updated_at == next_updated_at and cursor_nm_id == next_nm_id:
                msg = f"⚠️  Cursor not changing! Breaking to avoid infinite loop."
                app.logger.warning(msg)
                print(msg, flush=True)
                break

            cursor_updated_at = next_updated_at
            cursor_nm_id = next_nm_id

            # Ограничение на количество страниц для безопасности
            if page_num >= 1000:
                msg = f"⚠️  Reached max pages limit (1000). Stopping."
                app.logger.warning(msg)
                print(msg, flush=True)
                break

        cards = all_cards
        msg = f"✅ Pagination complete! Fetched {len(cards)} cards in {page_num} pages for seller {seller.id}"
        app.logger.info(msg)
        print(msg, flush=True)

        if not cards:
            app.logger.warning(f"No cards returned from WB API for seller {seller.id}")

        if not isinstance(cards, list):
            raise Exception(f'Expected cards to be a list, got {type(cards).__name__}')

        changes_detected = 0
        suspicious_changes = 0
        products_checked = 0
        products_not_in_db = 0
        products_added = 0

        for card in cards:
            nm_id = card.get('nmID')
            if not nm_id:
                continue

            # Находим товар в БД
            product = Product.query.filter_by(seller_id=seller.id, nm_id=nm_id).first()

            # Если товара нет в БД - создаем его
            if not product:
                try:
                    # Создаем новый товар из данных WB API
                    product = Product(
                        seller_id=seller.id,
                        nm_id=nm_id,
                        imt_id=card.get('imtID'),
                        vendor_code=card.get('vendorCode'),
                        title=card.get('title'),
                        brand=card.get('brand'),
                        object_name=card.get('object'),
                        is_active=True,
                        created_at=datetime.utcnow(),
                        last_sync=datetime.utcnow()
                    )

                    # Устанавливаем цену и остатки
                    sizes = card.get('sizes', [])
                    if sizes:
                        size = sizes[0]
                        product.price = size.get('price')
                        product.discount_price = size.get('discountedPrice', product.price)

                    # Остатки
                    stocks = card.get('stocks', [])
                    if stocks:
                        product.quantity = sum(stock.get('qty', 0) for stock in stocks)

                    db.session.add(product)
                    db.session.flush()  # Чтобы получить ID
                    products_added += 1
                    products_not_in_db += 1

                    app.logger.info(f"Added new product: {nm_id} - {product.vendor_code}")

                    # Для нового товара не создаем историю изменений
                    continue

                except Exception as e:
                    app.logger.error(f"Failed to add product {nm_id}: {e}")
                    products_not_in_db += 1
                    continue

            products_checked += 1

            # Получаем цены из API
            sizes = card.get('sizes', [])
            if not sizes:
                continue

            # Берем первый размер для получения цены
            size = sizes[0]
            new_price = size.get('price')
            new_discount_price = size.get('discountedPrice', new_price)

            # Получаем остатки если нужно
            new_quantity = None
            if settings.monitor_stocks:
                stocks = card.get('stocks', [])
                new_quantity = sum(stock.get('qty', 0) for stock in stocks)

            # Сохраняем старые значения
            old_price = float(product.price) if product.price else None
            old_discount_price = float(product.discount_price) if product.discount_price else None
            old_quantity = product.quantity

            # Проверяем изменения
            price_changed = False
            quantity_changed = False

            if settings.monitor_prices and new_price is not None and old_price is not None and old_price > 0:
                if new_price != old_price or (new_discount_price and new_discount_price != old_discount_price):
                    price_changed = True

            if settings.monitor_stocks and new_quantity is not None and old_quantity is not None and old_quantity > 0:
                if new_quantity != old_quantity:
                    quantity_changed = True

            # Если есть изменения, создаем запись в истории
            if price_changed or quantity_changed:
                changes_detected += 1

                # Вычисляем процент изменения
                price_change_percent = None
                discount_price_change_percent = None
                quantity_change_percent = None

                if price_changed and old_price and old_price > 0:
                    price_change_percent = ((new_price - old_price) / old_price) * 100

                if new_discount_price and old_discount_price and old_discount_price > 0:
                    discount_price_change_percent = ((new_discount_price - old_discount_price) / old_discount_price) * 100

                if quantity_changed and old_quantity and old_quantity > 0:
                    quantity_change_percent = ((new_quantity - old_quantity) / old_quantity) * 100

                # Создаем запись в истории
                history = PriceHistory(
                    product_id=product.id,
                    seller_id=seller.id,
                    old_price=old_price,
                    old_discount_price=old_discount_price,
                    old_quantity=old_quantity,
                    new_price=new_price,
                    new_discount_price=new_discount_price,
                    new_quantity=new_quantity,
                    price_change_percent=price_change_percent,
                    discount_price_change_percent=discount_price_change_percent,
                    quantity_change_percent=quantity_change_percent
                )
                db.session.add(history)
                db.session.flush()  # Чтобы получить ID истории

                # Проверяем, есть ли подозрительные скачки
                if price_change_percent and abs(price_change_percent) > settings.price_change_threshold_percent:
                    suspicious = SuspiciousPriceChange(
                        price_history_id=history.id,
                        product_id=product.id,
                        seller_id=seller.id,
                        change_type='price',
                        old_value=old_price,
                        new_value=new_price,
                        change_percent=price_change_percent,
                        threshold_percent=settings.price_change_threshold_percent
                    )
                    db.session.add(suspicious)
                    suspicious_changes += 1

                if discount_price_change_percent and abs(discount_price_change_percent) > settings.price_change_threshold_percent:
                    suspicious = SuspiciousPriceChange(
                        price_history_id=history.id,
                        product_id=product.id,
                        seller_id=seller.id,
                        change_type='discount_price',
                        old_value=old_discount_price,
                        new_value=new_discount_price,
                        change_percent=discount_price_change_percent,
                        threshold_percent=settings.price_change_threshold_percent
                    )
                    db.session.add(suspicious)
                    suspicious_changes += 1

                if quantity_change_percent and abs(quantity_change_percent) > settings.stock_change_threshold_percent:
                    suspicious = SuspiciousPriceChange(
                        price_history_id=history.id,
                        product_id=product.id,
                        seller_id=seller.id,
                        change_type='quantity',
                        old_value=old_quantity,
                        new_value=new_quantity,
                        change_percent=quantity_change_percent,
                        threshold_percent=settings.stock_change_threshold_percent
                    )
                    db.session.add(suspicious)
                    suspicious_changes += 1

                # Обновляем товар
                if new_price is not None:
                    product.price = new_price
                if new_discount_price is not None:
                    product.discount_price = new_discount_price
                if new_quantity is not None:
                    product.quantity = new_quantity
                product.last_sync = datetime.utcnow()

        # Сохраняем все изменения
        db.session.commit()

        # Обновляем статус синхронизации
        settings.last_sync_status = 'success'
        settings.last_sync_error = None
        db.session.commit()

        app.logger.info(f"Sync completed: {products_added} added, {products_checked} checked, {changes_detected} changes, {suspicious_changes} suspicious")

        return {
            'status': 'success',
            'total_cards_from_api': len(cards),
            'products_added': products_added,
            'products_checked': products_checked,
            'products_not_in_db': products_not_in_db - products_added,  # Не добавленные (были пропущены)
            'changes_detected': changes_detected,
            'suspicious_changes': suspicious_changes,
            'timestamp': datetime.utcnow().isoformat()
        }

    except Exception as e:
        # Откатываем транзакцию
        db.session.rollback()

        # Сохраняем ошибку
        settings.last_sync_status = 'failed'
        settings.last_sync_error = str(e)
        db.session.commit()

        raise


# ============= ИНИЦИАЛИЗАЦИЯ БД =============

@app.cli.command()
def init_db():
    """Инициализация базы данных"""
    db.create_all()
    print('База данных инициализирована')


@app.cli.command()
def create_admin():
    """Создание администратора"""
    username = input('Введите имя пользователя: ')
    email = input('Введите email: ')
    password = input('Введите пароль: ')

    if User.query.filter_by(username=username).first():
        print(f'Пользователь {username} уже существует')
        return

    admin = User(
        username=username,
        email=email,
        is_admin=True,
        is_active=True
    )
    admin.set_password(password)

    db.session.add(admin)
    db.session.commit()

    print(f'Администратор {username} успешно создан')


@app.cli.command()
def apply_migrations():
    """Применить миграции базы данных"""
    import sqlite3
    from pathlib import Path

    print("🔄 Применение миграций...")

    # Получаем путь к БД из конфигурации
    db_url = app.config['SQLALCHEMY_DATABASE_URI']
    if db_url.startswith('sqlite:///'):
        db_path = db_url.replace('sqlite:///', '')
    else:
        print(f"❌ Неподдерживаемый тип БД: {db_url}")
        return

    if not Path(db_path).exists():
        print(f"❌ База данных не найдена: {db_path}")
        return

    print(f"📂 База данных: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Проверяем существование subject_id
        cursor.execute("PRAGMA table_info(products)")
        columns = {row[1] for row in cursor.fetchall()}

        if 'subject_id' in columns:
            print("  ✓ Колонка subject_id уже существует")
        else:
            print("  ➕ Добавление колонки subject_id...")
            cursor.execute("ALTER TABLE products ADD COLUMN subject_id INTEGER")
            conn.commit()
            print("  ✅ Колонка subject_id добавлена")

        print("\n✅ Миграции успешно применены!")

    except Exception as e:
        print(f"❌ Ошибка при применении миграций: {e}")
        conn.rollback()
    finally:
        conn.close()


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))
