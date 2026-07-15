# -*- coding: utf-8 -*-
"""Тесты read-only агрегатора трея фоновых задач (/api/tasks/tray).

Проверяют: нормализованную форму, расчёт прогресса, источник sync-флага,
tenant-изоляцию (чужие задачи не видны) и пустой трей.
"""

import os
import unittest


class TestTasksTrayApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        import sqlalchemy as _sa
        from sqlalchemy.pool import StaticPool
        import seller_platform  # noqa
        from models import db
        cls.app = seller_platform.app
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        cls.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        cls.app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {}
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['TESTING'] = True
        cls._engine = _sa.create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        db._app_engines[cls.app] = {None: cls._engine}
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            cls._seed()

    @classmethod
    def _seed(cls):
        from datetime import datetime, timedelta
        from models import User, Seller, BackgroundJob
        u1 = User(username='t1', email='t1@e.com', password_hash='x')
        cls.db.session.add(u1); cls.db.session.flush()
        s1 = Seller(user_id=u1.id, company_name='Один', wb_seller_id='1')
        s1.wb_api_key = 'k1'
        s1.api_sync_status = 'syncing'  # источник sync-элемента
        cls.db.session.add(s1); cls.db.session.flush()
        u2 = User(username='t2', email='t2@e.com', password_hash='x')
        cls.db.session.add(u2); cls.db.session.flush()
        s2 = Seller(user_id=u2.id, company_name='Два', wb_seller_id='2')
        s2.wb_api_key = 'k2'
        cls.db.session.add(s2); cls.db.session.flush()
        # Третий продавец без задач и без sync
        u3 = User(username='t3', email='t3@e.com', password_hash='x')
        cls.db.session.add(u3); cls.db.session.flush()
        s3 = Seller(user_id=u3.id, company_name='Три', wb_seller_id='3')
        s3.wb_api_key = 'k3'
        cls.db.session.add(s3); cls.db.session.flush()
        cls.user1_id, cls.user2_id, cls.user3_id = u1.id, u2.id, u3.id
        cls.db.session.add_all([
            BackgroundJob(job_uid='j-own', seller_id=s1.id, job_type='bulk_wb_import',
                          status='running', total=10, processed=4),
            BackgroundJob(job_uid='j-other', seller_id=s2.id, job_type='bulk_wb_import',
                          status='running', total=5, processed=1),
            # завершённая задача продавца 1 — НЕ активна, в трей не идёт
            BackgroundJob(job_uid='j-done', seller_id=s1.id, job_type='bulk_wb_import',
                          status='completed', total=3, processed=3),
            # «зомби»: running, но начата давно (воркер умер) — должна быть отсечена
            BackgroundJob(job_uid='j-zombie', seller_id=s1.id, job_type='bulk_wb_import',
                          status='running', total=10, processed=2,
                          created_at=datetime.utcnow() - timedelta(days=90)),
        ])
        cls.db.session.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.drop_all()
        cls._engine.dispose()

    def _client(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True
        return client

    def test_own_active_and_sync_present(self):
        resp = self._client(self.user1_id).get('/api/tasks/tray')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        kinds = [i['kind'] for i in data['items']]
        self.assertIn('import', kinds)   # активная BackgroundJob
        self.assertIn('sync', kinds)     # sync-флаг
        self.assertEqual(data['count'], len(data['items']))
        # нормализованная форма
        for i in data['items']:
            self.assertEqual(set(i.keys()), {'kind', 'title', 'status', 'progress', 'started_at', 'link'})

    def test_progress_calculation(self):
        data = self._client(self.user1_id).get('/api/tasks/tray').get_json()
        imp = [i for i in data['items'] if i['kind'] == 'import']
        self.assertEqual(len(imp), 1)             # completed не попал
        self.assertEqual(imp[0]['progress'], 40)  # 4/10
        sync = [i for i in data['items'] if i['kind'] == 'sync']
        self.assertIsNone(sync[0]['progress'])

    def test_tenant_isolation(self):
        # У продавца 1 не видно задачи продавца 2
        data = self._client(self.user1_id).get('/api/tasks/tray').get_json()
        imp = [i for i in data['items'] if i['kind'] == 'import']
        self.assertEqual(len(imp), 1)  # только своя, не 2
        # У продавца 2 — своя одна, без sync (у него флаг не выставлен)
        data2 = self._client(self.user2_id).get('/api/tasks/tray').get_json()
        self.assertEqual([i['kind'] for i in data2['items']], ['import'])
        self.assertEqual(data2['items'][0]['progress'], 20)  # 1/5

    def test_zombie_excluded(self):
        # У продавца 1 две running-задачи import (свежая j-own + зомби 90 дней),
        # но трей показывает только свежую.
        data = self._client(self.user1_id).get('/api/tasks/tray').get_json()
        imp = [i for i in data['items'] if i['kind'] == 'import']
        self.assertEqual(len(imp), 1)

    def test_empty_tray(self):
        data = self._client(self.user3_id).get('/api/tasks/tray').get_json()
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['items'], [])


if __name__ == '__main__':
    unittest.main()
