"""Synthetic regressions for read-only WB sync status and settings GETs."""

from __future__ import annotations

from datetime import datetime, timedelta
from html.parser import HTMLParser
import json
import os
import re
import shutil
import subprocess

os.environ.setdefault('SKIP_SCHEDULER', '1')
os.environ.setdefault('DISABLE_SECURE_COOKIE', '1')

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.pool import StaticPool

import seller_platform
from models import Product, ProductSyncSettings, Seller, User, db


@pytest.fixture
def sync_app():
    app = seller_platform.app
    config = {
        key: app.config.get(key)
        for key in (
            'SQLALCHEMY_DATABASE_URI', 'SQLALCHEMY_TRACK_MODIFICATIONS',
            'SQLALCHEMY_ENGINE_OPTIONS', 'WTF_CSRF_ENABLED', 'TESTING',
            'MARKETPLACE_OZON_ENABLED', 'MARKETPLACE_WB_COMMON_READ_ENABLED',
        )
    }
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={},
        WTF_CSRF_ENABLED=False,
        TESTING=True,
        MARKETPLACE_OZON_ENABLED=False,
        MARKETPLACE_WB_COMMON_READ_ENABLED=False,
    )
    engine = sa.create_engine(
        'sqlite:///:memory:',
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    previous_engines = db._app_engines.get(app)
    db._app_engines[app] = {None: engine}

    with app.app_context():
        db.session.remove()
        db.create_all()

        users = [
            User(username='sync-owner-a', email='sync-a@example.test', password_hash='synthetic'),
            User(username='sync-owner-b', email='sync-b@example.test', password_hash='synthetic'),
        ]
        db.session.add_all(users)
        db.session.flush()
        sellers = [
            Seller(user_id=users[0].id, company_name='Synthetic seller A', wb_seller_id='A'),
            Seller(user_id=users[1].id, company_name='Synthetic seller B', wb_seller_id='B'),
        ]
        for seller in sellers:
            seller.wb_api_key = 'synthetic-not-a-real-key'
        sellers[0].api_sync_status = 'syncing'
        sellers[0].api_last_sync = datetime.utcnow() - timedelta(hours=5)
        db.session.add_all(sellers)
        db.session.flush()
        db.session.add_all([
            Product(seller_id=sellers[0].id, nm_id=81001, vendor_code='OWN-A', title='Owned A', is_active=True),
            Product(seller_id=sellers[0].id, nm_id=81002, vendor_code='OWN-B', title='Owned B', is_active=False),
            Product(seller_id=sellers[1].id, nm_id=82001, vendor_code='OTHER-A', title='Other seller', is_active=True),
        ])
        db.session.commit()
        ids = {
            'user_a': users[0].id,
            'user_b': users[1].id,
            'seller_a': sellers[0].id,
            'seller_b': sellers[1].id,
        }
        db.session.remove()

    yield app, engine, ids

    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.session.remove()
    engine.dispose()
    if previous_engines is None:
        db._app_engines.pop(app, None)
    else:
        db._app_engines[app] = previous_engines
    for key, value in config.items():
        if value is None:
            app.config.pop(key, None)
        else:
            app.config[key] = value


def client_for(app, user_id):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
    return client


def persisted_snapshot(engine, seller_id):
    with engine.connect() as connection:
        seller = connection.execute(sa.text(
            'SELECT api_sync_status, api_last_sync FROM sellers WHERE id=:seller_id'
        ), {'seller_id': seller_id}).one()
        settings = connection.execute(sa.text(
            'SELECT id, is_enabled, sync_interval_minutes, sync_products, sync_stocks, '
            'last_sync_at, next_sync_at, last_sync_status, last_sync_error, '
            'last_sync_duration, products_synced, products_added, products_updated '
            'FROM product_sync_settings WHERE seller_id=:seller_id'
        ), {'seller_id': seller_id}).first()
        product_count = connection.execute(sa.text(
            'SELECT COUNT(*) FROM products WHERE seller_id=:seller_id'
        ), {'seller_id': seller_id}).scalar_one()
        settings_count = connection.execute(sa.text(
            'SELECT COUNT(*) FROM product_sync_settings'
        )).scalar_one()
    return tuple(seller), tuple(settings) if settings else None, product_count, settings_count


def watch_db_writes(engine):
    writes = []
    commits = []

    def on_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        first_word = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ''
        if first_word in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'DROP', 'ALTER'}:
            writes.append(statement)

    def on_commit(_session):
        commits.append(True)

    event.listen(engine, 'before_cursor_execute', on_sql)
    session_class = db.session.session_factory.class_
    event.listen(session_class, 'after_commit', on_commit)
    return writes, commits, on_sql, on_commit, session_class


def stop_watching(engine, watch):
    _writes, _commits, on_sql, on_commit, session_class = watch
    event.remove(engine, 'before_cursor_execute', on_sql)
    event.remove(session_class, 'after_commit', on_commit)


def test_old_success_with_sync_guard_all_status_gets_are_read_only(sync_app):
    app, engine, ids = sync_app
    with app.app_context():
        db.session.add(ProductSyncSettings(
            seller_id=ids['seller_a'], is_enabled=True, sync_interval_minutes=90,
            sync_products=False, sync_stocks=True,
            last_sync_at=datetime.utcnow() - timedelta(hours=5),
            last_sync_status='running', products_synced=31,
            products_added=4, products_updated=27,
        ))
        db.session.commit()
        db.session.remove()

    before = persisted_snapshot(engine, ids['seller_a'])
    watch = watch_db_writes(engine)
    client = client_for(app, ids['user_a'])
    try:
        product_page = client.get('/products')
        status_page = client.get('/products/sync-status')
        status_api = client.get('/api/products/sync-status')
        settings_api = client.get('/api/products/sync-settings')
        tray_api = client.get('/api/tasks/tray')
    finally:
        stop_watching(engine, watch)

    for response in (product_page, status_page, status_api, settings_api, tray_api):
        assert response.status_code == 200
    payload = status_api.get_json()
    assert payload['last_sync_status'] == 'syncing'
    assert payload['sync_guard_active'] is True
    assert payload['sync_execution_confirmed'] is False
    assert payload['is_syncing'] is True  # Legacy poller guard remains compatible.
    assert 'не подтверждены' in payload['status_message']
    assert payload['products_synced'] == 31  # Timestamped historical observation remains visible.
    assert payload['last_sync_at'] is not None
    tray_items = [item for item in tray_api.get_json()['items'] if item['kind'] == 'sync']
    assert len(tray_items) == 1
    assert tray_items[0]['status'] == 'unknown'
    assert tray_items[0]['started_at'] is None
    assert 'уточняется' in tray_items[0]['title']
    page_html = product_page.get_data(as_text=True)
    assert 'Последнее успешное завершение' in page_html
    assert 'время начала пока не подтверждены' in page_html
    assert 'Синхронизация...' not in page_html

    writes, commits, *_ = watch
    assert writes == []
    assert commits == []
    assert persisted_snapshot(engine, ids['seller_a']) == before


def test_missing_settings_gets_use_defaults_and_valid_post_creates_one_row(sync_app):
    app, engine, ids = sync_app
    client = client_for(app, ids['user_a'])
    assert persisted_snapshot(engine, ids['seller_a'])[1] is None

    read_watch = watch_db_writes(engine)
    try:
        product_page = client.get('/products')
        settings_get = client.get('/api/products/sync-settings')
        status_get = client.get('/api/products/sync-status')
        status_page = client.get('/products/sync-status')
    finally:
        stop_watching(engine, read_watch)
    read_writes, read_commits, *_ = read_watch
    assert read_writes == []
    assert read_commits == []
    assert product_page.status_code == settings_get.status_code == status_get.status_code == status_page.status_code == 200
    defaults = settings_get.get_json()
    assert defaults['id'] is None
    assert defaults['seller_id'] == ids['seller_a']
    assert defaults['is_enabled'] is False
    assert defaults['sync_interval_minutes'] == 60
    assert defaults['sync_products'] is True
    assert defaults['sync_stocks'] is True
    assert defaults['last_sync_at'] is None
    assert defaults['products_synced'] is None
    assert defaults['products_added'] is None
    assert defaults['products_updated'] is None
    status_data = status_get.get_json()
    assert status_data['products_synced'] is None
    assert status_data['products_added'] is None
    assert status_data['products_updated'] is None
    assert status_data['last_sync_duration'] is None
    status_html = status_page.get_data(as_text=True)
    assert 'sync_interval_minutes' in status_html and '60' in status_html
    assert persisted_snapshot(engine, ids['seller_a'])[1] is None

    post_watch = watch_db_writes(engine)
    try:
        saved = client.post('/api/products/sync-settings', json={
            'is_enabled': True,
            'sync_interval_minutes': 95,
            'sync_products': False,
            'sync_stocks': True,
        })
    finally:
        stop_watching(engine, post_watch)
    post_writes, post_commits, *_ = post_watch
    assert saved.status_code == 200
    assert sum('INSERT INTO product_sync_settings' in statement for statement in post_writes) == 1
    assert post_commits == [True]
    body = saved.get_json()
    assert body['success'] is True
    assert body['settings']['is_enabled'] is True
    assert body['settings']['sync_interval_minutes'] == 95
    assert body['settings']['sync_products'] is False
    assert body['settings']['sync_stocks'] is True
    assert body['settings']['next_sync_at'] is not None
    with app.app_context():
        rows = ProductSyncSettings.query.filter_by(seller_id=ids['seller_a']).all()
        assert len(rows) == 1
        assert ProductSyncSettings.query.count() == 1
        assert rows[0].products_synced == 0
        assert rows[0].products_added == 0
        assert rows[0].products_updated == 0


def test_invalid_posts_do_not_create_or_change_settings_and_sellers_are_isolated(sync_app):
    app, engine, ids = sync_app
    client_a = client_for(app, ids['user_a'])
    client_b = client_for(app, ids['user_b'])

    invalid_watch = watch_db_writes(engine)
    try:
        invalid_new = client_a.post('/api/products/sync-settings', json={
            'is_enabled': True,
            'sync_interval_minutes': 'not-an-interval',
        })
    finally:
        stop_watching(engine, invalid_watch)
    invalid_writes, invalid_commits, *_ = invalid_watch
    assert invalid_new.status_code == 400
    assert invalid_writes == []
    assert invalid_commits == []
    assert persisted_snapshot(engine, ids['seller_a'])[1] is None

    original_next = datetime.utcnow() + timedelta(hours=2)
    with app.app_context():
        db.session.add(ProductSyncSettings(
            seller_id=ids['seller_a'], is_enabled=True, sync_interval_minutes=240,
            sync_products=False, sync_stocks=True, next_sync_at=original_next,
        ))
        db.session.add(ProductSyncSettings(
            seller_id=ids['seller_b'], is_enabled=False, sync_interval_minutes=300,
            sync_products=True, sync_stocks=False,
        ))
        db.session.commit()
        db.session.remove()

    before_a = persisted_snapshot(engine, ids['seller_a'])[1]
    before_b = persisted_snapshot(engine, ids['seller_b'])[1]
    invalid_watch = watch_db_writes(engine)
    try:
        invalid_existing = client_a.post('/api/products/sync-settings', json={
            'is_enabled': False,
            'sync_interval_minutes': 'not-an-interval',
        })
    finally:
        stop_watching(engine, invalid_watch)
    invalid_writes, invalid_commits, *_ = invalid_watch
    assert invalid_existing.status_code == 400
    assert invalid_writes == []
    assert invalid_commits == []
    assert persisted_snapshot(engine, ids['seller_a'])[1] == before_a
    assert persisted_snapshot(engine, ids['seller_b'])[1] == before_b

    own_view = client_a.get('/api/products/sync-settings').get_json()
    other_view = client_b.get('/api/products/sync-settings').get_json()
    assert own_view['sync_interval_minutes'] == 240
    assert own_view['sync_products'] is False
    assert other_view['sync_interval_minutes'] == 300
    assert other_view['sync_stocks'] is False
    assert persisted_snapshot(engine, ids['seller_a'])[3] == 2

    form_update = client_a.post('/api/products/sync-settings', data={
        'sync_interval_minutes': '360',
        'sync_products': 'true',
    })
    assert form_update.status_code == 200
    assert form_update.get_json()['settings']['sync_interval_minutes'] == 360
    assert persisted_snapshot(engine, ids['seller_a'])[3] == 2
    assert persisted_snapshot(engine, ids['seller_b'])[1] == before_b


def test_task_tray_template_labels_unknown_without_showing_progress(sync_app):
    app, _engine, _ids = sync_app
    with app.app_context():
        html = app.jinja_env.get_template(
            'partials/tasks_tray.html',
        ).render()
    match = re.search(
        r'<div class="sh-progress".*?x-show="([^"]+)"', html, re.S,
    )
    assert match, 'rendered task tray must contain its production progress visibility condition'
    label_match = re.search(
        r'<span class="sh-tray-status".*?x-text="([^"]+)"', html, re.S,
    )
    assert label_match, 'rendered task tray must contain its production status label expression'
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable')

    expression_json = json.dumps(match.group(1))
    label_expression_json = json.dumps(label_match.group(1))
    script = f"""
      const visible = new Function('item', 'return ' + {expression_json});
      const label = new Function('item', 'return ' + {label_expression_json});
      const statuses = ['unknown', 'running', 'pending', 'queued'];
      const result = Object.fromEntries(statuses.map(status => [status, {{
        progressVisible: visible({{status}}),
        label: label({{status}}),
      }}]));
      process.stdout.write(JSON.stringify(result));
    """
    result = subprocess.run(
        [node, '-e', script], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        'unknown': {'progressVisible': False, 'label': 'Результат уточняется'},
        'running': {'progressVisible': True, 'label': 'Выполняется'},
        'pending': {'progressVisible': False, 'label': 'В очереди'},
        'queued': {'progressVisible': False, 'label': 'В очереди'},
    }


def test_status_page_initial_alpine_guard_and_zero_duration_are_honest(sync_app):
    app, _engine, ids = sync_app
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable')

    cases = (
        (ids['user_a'], True, 'не подтверждены'),
        (ids['user_b'], False, 'Загружаем статус'),
    )
    for user_id, guard_expected, message_expected in cases:
        response = client_for(app, user_id).get('/products/sync-status')
        assert response.status_code == 200
        html = response.get_data(as_text=True)

        class ManualSyncButtonParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_manual_sync_form = False
                self.disabled = None

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == 'form':
                    self.in_manual_sync_form = attributes.get('action') == '/products/sync'
                elif tag == 'button' and self.in_manual_sync_form:
                    self.disabled = 'disabled' in attributes

            def handle_endtag(self, tag):
                if tag == 'form':
                    self.in_manual_sync_form = False

        button_parser = ManualSyncButtonParser()
        button_parser.feed(html)
        assert button_parser.disabled is guard_expected

        script_match = re.search(
            r'<script>\s*(function syncStatusApp\(\)\s*\{.*?)</script>',
            html, re.S,
        )
        assert script_match, 'must evaluate the production syncStatusApp handler'
        disabled_match = re.search(
            r'<form action="/products/sync"[^>]*>.*?<button[^>]*:disabled="([^"]+)"',
            html, re.S,
        )
        assert disabled_match, 'must evaluate the production manual-sync disabled expression'
        spinner_match = re.search(
            r'<svg[^>]*x-show="(status\.sync_execution_confirmed)"', html,
        )
        assert spinner_match, 'must evaluate the production progress-spinner condition'

        source_json = json.dumps(script_match.group(1))
        disabled_json = json.dumps(disabled_match.group(1))
        spinner_json = json.dumps(spinner_match.group(1))
        node_script = f"""
          const source = {source_json};
          const app = new Function(source + '\\nreturn syncStatusApp();')();
          const disabled = new Function('status', 'return ' + {disabled_json})(app.status);
          const spinner = new Function('status', 'return ' + {spinner_json})(app.status);
          process.stdout.write(JSON.stringify({{
            is_syncing: app.status.is_syncing,
            guard: app.status.sync_guard_active,
            execution_confirmed: app.status.sync_execution_confirmed,
            status_message: app.status.status_message,
            disabled,
            spinner,
            duration_zero: app.formatDuration(0),
            duration_null: app.formatDuration(null),
            duration_negative: app.formatDuration(-0.1),
            duration_nan: app.formatDuration(Number.NaN),
            duration_infinite: app.formatDuration(Number.POSITIVE_INFINITY),
          }}));
        """
        result = subprocess.run(
            [node, '-e', node_script], capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        observed = json.loads(result.stdout)
        assert observed['guard'] is guard_expected
        assert observed['is_syncing'] is guard_expected
        assert observed['execution_confirmed'] is False
        assert observed['disabled'] is guard_expected
        assert observed['spinner'] is False
        assert message_expected in observed['status_message']
        assert observed['duration_zero'] == '0.0с'
        assert observed['duration_null'] == '—'
        assert observed['duration_negative'] == '—'
        assert observed['duration_nan'] == '—'
        assert observed['duration_infinite'] == '—'
