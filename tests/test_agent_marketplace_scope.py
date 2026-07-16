# -*- coding: utf-8 -*-
"""Exact marketplace/account scope for the unified seller assistant."""
import json
import unittest
from datetime import datetime

from flask import Flask
from werkzeug.security import generate_password_hash

from agents.platform_client import PlatformClient
from agents.unified import MarketplaceListingAuditSkill, UnifiedSellerAgent
from models import (
    AgentTask,
    Marketplace,
    MarketplaceListing,
    MarketplaceQualityAssessment,
    Seller,
    SellerMarketplaceAccount,
    ServiceAgent,
    User,
    db,
)
from routes.internal_api import internal_api_bp
from services.agent_harness import (
    _ground_marketplace_entity_scope,
    build_plan,
    conversation_payload,
    create_conversation,
    submit_turn,
)


class _MarketplacePlannerLLM:
    def __init__(self, skill):
        self.skill = skill

    def structured_output_with_usage(self, **kwargs):
        return {
            'data': {
                'title': 'Карточки Ozon',
                'summary': 'Проверить выбранные карточки',
                'risk': 'read',
                'confidence': 0.95,
                'scope_label': 'Недоверенная подпись модели',
                'steps': [{
                    'skill': self.skill,
                    'label': 'Проверка',
                    'params': {},
                }],
            },
            'usage': {'input_tokens': 10, 'output_tokens': 5, 'api_requests': 1},
        }


class _MarketplaceAuditPlatform:
    def __init__(self):
        self.calls = []

    def get_marketplace_listing_brief(
        self, seller_id, marketplace_code, account_id, listing_ids, focus_limit,
    ):
        self.calls.append((
            seller_id, marketplace_code, account_id, listing_ids, focus_limit,
        ))
        return {
            'total': len(listing_ids),
            'cards_with_issues': 1,
            'issue_summary': [{
                'code': 'missing_media', 'label': 'Нет изображений Ozon',
                'count': 1,
            }],
            'products': [{
                'id': listing_ids[0], 'title': 'Ozon listing',
                'issue_labels': ['Нет изображений Ozon'],
            }],
            'truncated': False,
        }


class AgentMarketplaceScopeTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.app.register_blueprint(internal_api_bp)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        user1 = User(
            username='market-seller-1', email='market1@test.local',
            password_hash='synthetic',
        )
        user2 = User(
            username='market-seller-2', email='market2@test.local',
            password_hash='synthetic',
        )
        db.session.add_all([user1, user2])
        db.session.flush()
        self.user1 = user1
        self.seller1 = Seller(user_id=user1.id, company_name='Seller One')
        self.seller2 = Seller(user_id=user2.id, company_name='Seller Two')
        self.marketplace = Marketplace(
            name='Ozon', code='ozon', adapter_code='ozon', is_active=True,
        )
        db.session.add_all([self.seller1, self.seller2, self.marketplace])
        db.session.flush()
        self.account1 = SellerMarketplaceAccount(
            seller_id=self.seller1.id,
            marketplace_id=self.marketplace.id,
            external_account_id='synthetic-client-1',
            label='Основной Ozon',
            is_active=True,
            connection_status='connected',
            _credentials_encrypted='must-never-leak',
        )
        self.account2 = SellerMarketplaceAccount(
            seller_id=self.seller2.id,
            marketplace_id=self.marketplace.id,
            external_account_id='synthetic-client-2',
            label='Foreign Ozon',
            is_active=True,
            connection_status='connected',
            _credentials_encrypted='foreign-secret',
        )
        db.session.add_all([self.account1, self.account2])
        db.session.flush()
        self.listing1 = self._listing(
            self.seller1.id, self.account1.id, 'offer-one', '101',
        )
        self.listing2 = self._listing(
            self.seller1.id, self.account1.id, 'offer-two', '102',
        )
        self.foreign_listing = self._listing(
            self.seller2.id, self.account2.id, 'foreign-offer', '201',
        )
        db.session.flush()
        db.session.add(MarketplaceQualityAssessment(
            seller_id=self.seller1.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account1.id,
            listing_id=self.listing1.id,
            status='scored',
            severity='warning',
            score=72,
            impact=8,
            listing_fingerprint='a' * 64,
            reasons_json=json.dumps([{
                'code': 'ozon_few_media',
                'label': 'Мало изображений',
                'severity': 'warning',
                'impact': 8,
            }]),
        ))
        self.agent = ServiceAgent(
            id='market-agent', name='orchestrator', display_name='Orchestrator',
            api_key_hash=generate_password_hash('synthetic-agent-key'),
            status='online', last_heartbeat=datetime.utcnow(),
        )
        db.session.add(self.agent)
        db.session.flush()
        self.task = AgentTask(
            id='market-task', agent_id=self.agent.id,
            seller_id=self.seller1.id, task_type='custom',
            title='Marketplace audit', status='running',
        )
        db.session.add(self.task)
        self._set_task_scope([self.listing1.id, self.listing2.id])
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _listing(self, seller_id, account_id, offer_id, product_id):
        listing = MarketplaceListing(
            seller_id=seller_id,
            marketplace_id=self.marketplace.id,
            account_id=account_id,
            offer_id=offer_id,
            external_product_id=product_id,
            title=f'Карточка {offer_id}',
            description='Короткое описание',
            normalized_status='active',
            is_available=True,
            media_json='{}',
            attributes_json='[]',
            complex_attributes_json='[]',
            barcodes_json='[]',
            dimensions_json='{}',
            price_summary_json='{"available":false}',
            sync_fingerprint=product_id.zfill(64),
        )
        db.session.add(listing)
        return listing

    def _set_task_scope(self, listing_ids, *, account_id=None):
        account_id = account_id or self.account1.id
        scope = {
            'kind': 'marketplace_listing',
            'ids': listing_ids,
            'marketplace_code': 'ozon',
            'account_id': account_id,
            'scope_mode': 'selected',
        }
        self.task.input_data = json.dumps({
            'seller_id': self.seller1.id,
            'product_ids': [],
            'imported_product_ids': [],
            'marketplace_listing_ids': listing_ids,
            'entity_scope': scope,
        })

    @property
    def headers(self):
        return {
            'X-Agent-Id': self.agent.id,
            'X-Agent-Key': 'synthetic-agent-key',
            'X-Task-Id': self.task.id,
        }

    def _payload(self, listing_ids=None, account_id=None):
        return {
            'marketplace_code': 'ozon',
            'account_id': account_id or self.account1.id,
            'listing_ids': listing_ids or [self.listing1.id, self.listing2.id],
            'focus_limit': 100,
        }

    def test_browser_scope_is_exactly_grounded(self):
        grounded = _ground_marketplace_entity_scope({
            'kind': 'marketplace_listing',
            'ids': [self.listing2.id, self.listing1.id],
            'marketplace_code': 'OZON',
            'account_id': self.account1.id,
            'scope_mode': 'selected',
        }, self.seller1.id)
        self.assertEqual(grounded, {
            'kind': 'marketplace_listing',
            'ids': [self.listing2.id, self.listing1.id],
            'marketplace_code': 'ozon',
            'account_id': self.account1.id,
            'scope_mode': 'selected',
        })
        for invalid_ids in ([str(self.listing1.id)], [True], [1.5], [1, 1]):
            with self.subTest(ids=invalid_ids), self.assertRaises(ValueError):
                _ground_marketplace_entity_scope({
                    'kind': 'marketplace_listing',
                    'ids': invalid_ids,
                    'marketplace_code': 'ozon',
                    'account_id': self.account1.id,
                    'scope_mode': 'selected',
                }, self.seller1.id)
        with self.assertRaises(ValueError):
            _ground_marketplace_entity_scope({
                'kind': 'marketplace_listing',
                'ids': [self.listing1.id, self.foreign_listing.id],
                'marketplace_code': 'ozon',
                'account_id': self.account1.id,
                'scope_mode': 'selected',
            }, self.seller1.id)

    def test_deterministic_plan_never_routes_listing_ids_to_wb_skills(self):
        scope = {
            'kind': 'marketplace_listing',
            'ids': [self.listing1.id],
            'marketplace_code': 'ozon',
            'account_id': self.account1.id,
            'scope_mode': 'selected',
        }
        audit = build_plan(
            'Проведи аудит выбранной карточки', [],
            entity_kind='marketplace_listing', entity_scope=scope,
        )
        insight = build_plan(
            'Что можешь сказать по этой карточке?', [],
            entity_kind='marketplace_listing', entity_scope=scope,
        )
        write = build_plan(
            'Улучши название и описание', [],
            entity_kind='marketplace_listing', entity_scope=scope,
        )
        self.assertEqual(audit.steps[0]['agent'], 'marketplace-listing-audit')
        self.assertEqual(insight.steps[0]['agent'], 'marketplace-listing-insight')
        self.assertIsNone(write)

    def test_submitted_run_keeps_listing_ids_out_of_product_envelopes(self):
        conversation = create_conversation(self.seller1.id, self.user1.id)
        scope = {
            'kind': 'marketplace_listing',
            'ids': [self.listing1.id],
            'marketplace_code': 'ozon',
            'account_id': self.account1.id,
            'scope_mode': 'selected',
        }
        result = submit_turn(
            conversation,
            'Проведи аудит выбранной карточки',
            product_ids=[],
            entity_kind='marketplace_listing',
            entity_scope=scope,
        )
        run = result['run']
        self.assertIsNotNone(run)
        task_input = run.task.get_input()
        self.assertEqual(task_input['product_ids'], [])
        self.assertEqual(task_input['imported_product_ids'], [])
        self.assertEqual(
            task_input['marketplace_listing_ids'], [self.listing1.id],
        )
        self.assertEqual(task_input['entity_scope'], scope)
        self.assertEqual(
            conversation_payload(conversation)['active_scope'],
            {**scope, 'page_context': {}},
        )

    def test_semantic_planner_allows_only_marketplace_read_skills(self):
        agent = object.__new__(UnifiedSellerAgent)
        agent.system_prompt = UnifiedSellerAgent.system_prompt
        scope = {
            'kind': 'marketplace_listing',
            'ids': [self.listing1.id],
            'marketplace_code': 'ozon',
            'account_id': self.account1.id,
            'scope_mode': 'selected',
        }
        input_data = {
            'text': 'Проверь карточку Ozon',
            'product_ids': [],
            'marketplace_listing_ids': [self.listing1.id],
            'entity_scope': scope,
        }
        agent.llm = _MarketplacePlannerLLM('marketplace-listing-audit')
        allowed = agent._plan_request({'id': 'plan-market'}, input_data)
        self.assertEqual(allowed['status'], 'completed')
        self.assertEqual(allowed['risk'], 'read')
        self.assertEqual(allowed['steps'][0]['agent'], 'marketplace-listing-audit')
        self.assertIn('кабинет', allowed['scope_label'])

        agent.llm = _MarketplacePlannerLLM('content-writer')
        blocked = agent._plan_request({'id': 'plan-market-write'}, input_data)
        self.assertEqual(blocked['status'], 'needs_clarification')

    def test_dedicated_audit_skill_keeps_marketplace_envelope(self):
        skill = object.__new__(MarketplaceListingAuditSkill)
        skill.platform = _MarketplaceAuditPlatform()
        result = skill.execute_task({
            'seller_id': self.seller1.id,
            'input_data': json.dumps({
                'product_ids': [],
                'imported_product_ids': [],
                'marketplace_listing_ids': [self.listing1.id],
                'entity_scope': {
                    'kind': 'marketplace_listing',
                    'ids': [self.listing1.id],
                    'marketplace_code': 'ozon',
                    'account_id': self.account1.id,
                    'scope_mode': 'selected',
                },
                'params': {'entity_kind': 'marketplace_listing'},
            }),
        })
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['entity_kind'], 'marketplace_listing')
        self.assertEqual(skill.platform.calls[0][1:4], (
            'ozon', self.account1.id, [self.listing1.id],
        ))

    def test_platform_client_never_coerces_or_clips_listing_ids(self):
        client = object.__new__(PlatformClient)
        calls = []
        client._request = lambda method, path, **kwargs: calls.append(
            (method, path, kwargs['json'])
        ) or {'count': len(kwargs['json']['listing_ids'])}
        ids = list(range(1, 201))
        result = client.get_marketplace_listing_brief(
            self.seller1.id, 'OZON', self.account1.id, ids, 200,
        )
        self.assertEqual(result['count'], 200)
        self.assertEqual(calls[0][2]['listing_ids'], ids)
        self.assertEqual(calls[0][2]['marketplace_code'], 'ozon')
        for invalid in ([1, 1], ['1'], [True], [1.0], list(range(1, 202))):
            with self.subTest(ids=invalid), self.assertRaises(ValueError):
                client.get_marketplace_listing_brief(
                    self.seller1.id, 'ozon', self.account1.id, invalid,
                )

    def test_internal_brief_is_task_bound_and_secret_free(self):
        response = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/marketplace-listings/brief',
            headers=self.headers,
            json=self._payload(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['entity_kind'], 'marketplace_listing')
        self.assertEqual(body['account_id'], self.account1.id)
        self.assertEqual([item['id'] for item in body['listings']], [
            self.listing1.id, self.listing2.id,
        ])
        serialized = response.get_data(as_text=True)
        self.assertNotIn('must-never-leak', serialized)
        self.assertNotIn('foreign-secret', serialized)
        self.assertNotIn('credentials', serialized.lower())

        mismatch = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/marketplace-listings/brief',
            headers=self.headers,
            json=self._payload([self.listing1.id]),
        )
        self.assertEqual(mismatch.status_code, 403)
        invalid = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/marketplace-listings/brief',
            headers=self.headers,
            json=self._payload([str(self.listing1.id), self.listing2.id]),
        )
        self.assertEqual(invalid.status_code, 400)

    def test_internal_brief_rejects_foreign_listing_even_if_task_is_tampered(self):
        ids = [self.listing1.id, self.foreign_listing.id]
        self._set_task_scope(ids)
        db.session.commit()
        response = self.client.post(
            f'/internal/v1/sellers/{self.seller1.id}/marketplace-listings/brief',
            headers=self.headers,
            json=self._payload(ids),
        )
        self.assertEqual(response.status_code, 409)


if __name__ == '__main__':
    unittest.main()
