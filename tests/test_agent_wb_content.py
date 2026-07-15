# -*- coding: utf-8 -*-
"""Contracts for publishing a prepared chat diff to WB without regeneration."""

import json
import unittest
from datetime import datetime, timedelta

from flask import Flask

from models import (
    db, AgentConversation, AgentTask, CardEditHistory, Product, Seller,
    ServiceAgent, User,
)
from services.agent_harness import snapshot_count
from services.agent_wb_content import publish_confirmed_content_proposals
from services.wb_api_client import WBTransportUncertainException


class _FakeWBClient:
    def __init__(self, result=None, error=None, on_call=None):
        self.result = result or {'sent': [], 'missing': [], 'invalid': {}, 'failed': {}}
        self.error = error
        self.on_call = on_call
        self.calls = []
        self.closed = False

    def update_cards_merged(self, updates, seller_id):
        self.calls.append((updates, seller_id))
        if self.on_call:
            self.on_call()
        if self.error:
            raise self.error
        return self.result

    def close(self):
        self.closed = True


class AgentWBContentTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        user = User(username='seller', email='seller@example.test', password_hash='x')
        db.session.add(user)
        db.session.flush()
        self.seller = Seller(user_id=user.id, company_name='Seller')
        self.seller.wb_api_key = 'test-wb-key'
        self.agent = ServiceAgent(id='agent', name='orchestrator', display_name='Agent')
        db.session.add_all([self.seller, self.agent])
        db.session.flush()
        self.conversation = AgentConversation(
            id='conversation', seller_id=self.seller.id, user_id=user.id,
            title='Карточка', created_at=datetime.utcnow() - timedelta(minutes=1),
        )
        db.session.add(self.conversation)
        db.session.flush()
        self.source_task = AgentTask(
            id='source-task', agent_id=self.agent.id, seller_id=self.seller.id,
            task_type='custom', title='Подготовить diff', status='completed',
            input_data=json.dumps({'conversation_id': self.conversation.id}),
        )
        self.publish_task = AgentTask(
            id='publish-task', agent_id=self.agent.id, seller_id=self.seller.id,
            task_type='custom', title='Отправить в WB', status='running',
            input_data=json.dumps({'conversation_id': self.conversation.id}),
        )
        self.product = Product(
            seller_id=self.seller.id, nm_id=778899,
            title='Новое название', description='Новое описание',
        )
        db.session.add_all([self.source_task, self.publish_task, self.product])
        db.session.flush()
        self.history = CardEditHistory(
            product_id=self.product.id, seller_id=self.seller.id,
            action='update', changed_fields=['title', 'description'],
            snapshot_before={'title': 'Старое название', 'description': 'Старое описание'},
            snapshot_after={'title': 'Новое название', 'description': 'Новое описание'},
            wb_synced=False, wb_sync_status='pending',
            user_comment=f'agent_task:{self.source_task.id}',
        )
        db.session.add(self.history)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_publishes_exact_snapshot_once_and_marks_original_history(self):
        def assert_pre_send_state():
            db.session.refresh(self.history)
            self.assertEqual(self.history.wb_sync_status, 'uncertain')
            self.assertEqual(snapshot_count(self.source_task.id), 0)

        client = _FakeWBClient(result={
            'sent': [self.product.nm_id], 'missing': [],
            'invalid': {}, 'failed': {},
        }, on_call=assert_pre_send_state)
        self.assertEqual(snapshot_count(self.source_task.id), 1)

        result = publish_confirmed_content_proposals(
            self.publish_task,
            self.seller.id,
            [self.product.id],
            ['title', 'description'],
            client_factory=lambda _key: client,
        )

        self.assertEqual(result['published'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(client.calls, [({
            self.product.nm_id: {
                'title': 'Новое название',
                'description': 'Новое описание',
            },
        }, self.seller.id)])
        db.session.refresh(self.history)
        self.assertTrue(self.history.wb_synced)
        self.assertEqual(self.history.wb_sync_status, 'success')
        self.assertEqual(snapshot_count(self.source_task.id), 0)
        self.assertTrue(client.closed)

        second = publish_confirmed_content_proposals(
            self.publish_task,
            self.seller.id,
            [self.product.id],
            ['title', 'description'],
            client_factory=lambda _key: self.fail('WB must not be called twice'),
        )
        self.assertEqual(second['already_published'], 1)

    def test_concurrent_content_change_blocks_external_request(self):
        self.product.description = 'Ручное изменение после предложения'
        db.session.commit()

        result = publish_confirmed_content_proposals(
            self.publish_task,
            self.seller.id,
            [self.product.id],
            ['title', 'description'],
            client_factory=lambda _key: self.fail('conflict must block WB call'),
        )

        self.assertEqual(result['published'], 0)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['results'][0]['code'], 'proposal_conflict')
        db.session.refresh(self.history)
        self.assertEqual(self.history.wb_sync_status, 'pending')

    def test_timeout_is_uncertain_and_not_locally_rollbackable(self):
        client = _FakeWBClient(error=TimeoutError('provider timeout'))

        result = publish_confirmed_content_proposals(
            self.publish_task,
            self.seller.id,
            [self.product.id],
            ['title', 'description'],
            client_factory=lambda _key: client,
        )

        self.assertEqual(result['results'][0]['code'], 'wb_result_uncertain')
        db.session.refresh(self.history)
        self.assertEqual(self.history.wb_sync_status, 'uncertain')
        self.assertEqual(snapshot_count(self.source_task.id), 0)

        repeated = publish_confirmed_content_proposals(
            self.publish_task,
            self.seller.id,
            [self.product.id],
            ['title', 'description'],
            client_factory=lambda _key: self.fail('uncertain request must not retry'),
        )
        self.assertEqual(repeated['results'][0]['code'], 'proposal_uncertain')

    def test_malformed_batch_accounting_is_uncertain_and_not_retried(self):
        client = _FakeWBClient(result={
            'sent': [], 'missing': [], 'invalid': {}, 'failed': {},
            'unexpected': True,
        })

        result = publish_confirmed_content_proposals(
            self.publish_task,
            self.seller.id,
            [self.product.id],
            ['title', 'description'],
            client_factory=lambda _key: client,
        )

        self.assertEqual(result['results'][0]['code'], 'wb_result_uncertain')
        db.session.refresh(self.history)
        self.assertEqual(self.history.wb_sync_status, 'uncertain')
        self.assertEqual(snapshot_count(self.source_task.id), 0)

    def test_client_initialization_failure_is_definite_local_failure(self):
        def unavailable(_key):
            raise RuntimeError('client init failed')

        result = publish_confirmed_content_proposals(
            self.publish_task,
            self.seller.id,
            [self.product.id],
            ['title', 'description'],
            client_factory=unavailable,
        )

        self.assertEqual(result['results'][0]['code'], 'wb_client_unavailable')
        db.session.refresh(self.history)
        self.assertEqual(self.history.wb_sync_status, 'failed')
        self.assertEqual(snapshot_count(self.source_task.id), 1)

    def test_prefetch_transport_failure_is_definite_and_rollbackable(self):
        client = _FakeWBClient(error=WBTransportUncertainException(
            'DNS lookup failed', request_may_have_been_applied=False,
        ))

        result = publish_confirmed_content_proposals(
            self.publish_task,
            self.seller.id,
            [self.product.id],
            ['title', 'description'],
            client_factory=lambda _key: client,
        )

        self.assertEqual(result['results'][0]['code'], 'wb_request_not_sent')
        db.session.refresh(self.history)
        self.assertEqual(self.history.wb_sync_status, 'failed')
        self.assertEqual(snapshot_count(self.source_task.id), 1)

    def test_atomic_claim_blocks_second_publish_task(self):
        other_task = AgentTask(
            id='publish-task-2', agent_id=self.agent.id,
            seller_id=self.seller.id, task_type='custom',
            title='Повторная отправка', status='running',
            input_data=json.dumps({'conversation_id': self.conversation.id}),
        )
        db.session.add(other_task)
        db.session.commit()
        nested = []

        def client_factory(_key):
            nested.append(publish_confirmed_content_proposals(
                other_task,
                self.seller.id,
                [self.product.id],
                ['title', 'description'],
                client_factory=lambda _nested_key: self.fail(
                    'A claimed proposal must not issue a second WB request'
                ),
            ))
            return _FakeWBClient(result={
                'sent': [self.product.nm_id], 'missing': [],
                'invalid': {}, 'failed': {},
            })

        result = publish_confirmed_content_proposals(
            self.publish_task,
            self.seller.id,
            [self.product.id],
            ['title', 'description'],
            client_factory=client_factory,
        )

        self.assertEqual(result['published'], 1)
        self.assertEqual(nested[0]['results'][0]['code'], 'proposal_uncertain')


if __name__ == '__main__':
    unittest.main()
