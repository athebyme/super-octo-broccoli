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

from app import (
    DEFAULT_COLUMN_INDICES,
    column_letters_to_indices,
    compute_profit_table,
    gather_columns,
    read_statistics,
    save_processed_report,
)
from models import db, User, Seller, SellerReport
from wildberries_api import WildberriesAPIError, list_cards

# Настройка приложения
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'wb-seller-platform-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///seller_platform.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.jinja_env.filters['basename'] = lambda value: Path(value).name if value else ''

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


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))
