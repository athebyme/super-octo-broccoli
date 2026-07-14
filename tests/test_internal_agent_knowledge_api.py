# -*- coding: utf-8 -*-
"""Security and validation for task-scoped agent knowledge retrieval."""
import unittest

from flask import Flask
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from models import db, AgentTask, Seller, ServiceAgent, User
from routes.internal_api import internal_api_bp
from services.agent_knowledge import ingest_document


class InternalAgentKnowledgeApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.app.register_blueprint(internal_api_bp)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        users = [
            User(username='knowledge-one', email='knowledge1@example.com', password_hash='x'),
            User(username='knowledge-two', email='knowledge2@example.com', password_hash='x'),
        ]
        db.session.add_all(users)
        db.session.flush()
        self.seller1 = Seller(user_id=users[0].id, company_name='One')
        self.seller2 = Seller(user_id=users[1].id, company_name='Two')
        db.session.add_all([self.seller1, self.seller2])
        db.session.flush()
        self.agent = ServiceAgent(
            id='knowledge-agent', name='knowledge-agent', display_name='Knowledge Agent',
            api_key_hash=generate_password_hash('secret-key'),
        )
        db.session.add(self.agent)
        db.session.flush()
        self.task = AgentTask(
            id='knowledge-task', agent_id=self.agent.id, seller_id=self.seller1.id,
            task_type='answer_knowledge', title='Knowledge', status='running',
        )
        db.session.add(self.task)
        db.session.commit()
        ingest_document(
            title='Правила контента WB', source_key='wb/content',
            source_type='wb_official',
            source_uri='https://seller.wildberries.ru/instructions/content',
            version='1',
            valid_until='2099-12-31T00:00:00',
            content='# Контент\nВ описании нельзя размещать внешние ссылки и контакты.',
        )
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.session.execute(text('DROP TABLE IF EXISTS agent_knowledge_chunks_fts'))
        db.session.commit()
        db.drop_all()
        self.ctx.pop()

    @property
    def auth(self):
        return {
            'X-Agent-Id': self.agent.id,
            'X-Agent-Key': 'secret-key',
            'X-Task-Id': self.task.id,
        }

    def _post(self, seller_id, payload, headers=None):
        return self.client.post(
            f'/internal/v1/sellers/{seller_id}/knowledge/search',
            json=payload, headers=self.auth if headers is None else headers,
        )

    def test_requires_auth_and_active_assigned_task(self):
        self.assertEqual(self._post(
            self.seller1.id, {'query': 'внешние ссылки'}, headers={},
        ).status_code, 401)
        response = self._post(
            self.seller1.id, {'query': 'внешние ссылки'},
            headers={**self.auth, 'X-Task-Id': 'foreign-task'},
        )
        self.assertEqual(response.status_code, 403)

    def test_rejects_foreign_seller_scope(self):
        response = self._post(self.seller2.id, {'query': 'внешние ссылки'})
        self.assertEqual(response.status_code, 403)

    def test_returns_bounded_cited_results(self):
        response = self._post(self.seller1.id, {
            'query': 'можно ли использовать внешние ссылки',
            'limit': 8, 'max_chars': 700,
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['has_results'])
        self.assertLessEqual(payload['context_chars'], 700)
        self.assertEqual(payload['citations'][0]['citation_id'], 'K1')
        self.assertNotIn('seller_id', payload['hits'][0])

    def test_rejects_loose_types_and_unknown_fields(self):
        for payload in (
            {'query': 'правила', 'limit': True},
            {'query': 'правила', 'max_chars': '6000'},
            {'query': 'правила', 'raw_sql': 'select *'},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self._post(self.seller1.id, payload).status_code, 400)


if __name__ == '__main__':
    unittest.main()
