"""The supplier seed works on a new ORM schema with Python-only defaults."""

import sqlite3
from unittest.mock import patch

from flask import Flask

from migrations.migrate_add_sexopt_supplier import run_migration
from models import Supplier, User, db


def test_fresh_orm_schema_seed_supplies_required_defaults_and_is_idempotent(tmp_path):
    database = tmp_path / "new-install.db"
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + str(database)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        admin = User(username="synthetic-admin", email="admin@example.test", is_admin=True)
        admin.set_password("synthetic-password")
        db.session.add(admin)
        db.session.commit()
        with patch("migrations.migrate_add_sexopt_supplier.get_db_path", return_value=str(database)):
            run_migration()
            run_migration()
        rows = db.session.query(Supplier).filter_by(code="andrey").all()
        assert len(rows) == 1
        supplier = rows[0]
        assert supplier.ai_enabled is False
        assert supplier.ai_proxy_enabled is False
        assert supplier.image_gen_enabled is False
        assert supplier.auto_sync_prices is False
        assert supplier.is_active is False
        assert supplier.csv_source_url is None
        assert supplier.created_by_user_id == admin.id
        assert supplier.created_at is not None

        # Deployment must not reset the admin's supplier credentials/settings.
        supplier.name = 'Reviewed supplier name'
        supplier.csv_source_url = 'https://supplier.example.test/private.csv?token=synthetic'
        supplier.csv_encoding = 'cp1251'
        supplier.csv_column_mapping = '{"title": {"column": "reviewed_title"}}'
        supplier.image_target_size = 777
        supplier.is_active = True
        db.session.commit()
        previous_updated_at = supplier.updated_at
        with patch("migrations.migrate_add_sexopt_supplier.get_db_path", return_value=str(database)):
            run_migration()
        db.session.expire_all()
        assert supplier.name == 'Reviewed supplier name'
        assert supplier.csv_source_url.endswith('token=synthetic')
        assert supplier.csv_encoding == 'cp1251'
        assert supplier.csv_column_mapping == '{"title": {"column": "reviewed_title"}}'
        assert supplier.image_target_size == 777
        assert supplier.is_active is True
        assert supplier.updated_at == previous_updated_at

        from migrations.migrate_andrey_feed_full_ingest import run_migration as run_feed_migration
        with patch("migrations.migrate_andrey_feed_full_ingest.get_db_path", return_value=str(database)):
            run_feed_migration()
        db.session.expire_all()
        assert supplier.csv_column_mapping == '{"title": {"column": "reviewed_title"}}'
        assert supplier.updated_at == previous_updated_at
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database) as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
