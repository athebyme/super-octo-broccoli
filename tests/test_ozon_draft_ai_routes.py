"""HTTP contract for seller-owned Ozon draft suggestions and review."""

from concurrent.futures import Future
from datetime import datetime
import json
import unittest
from unittest.mock import patch

from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask import g

from models import (
    AIParsingAttempt, MarketplaceAttributeDefinition, MarketplaceOperation,
    OzonDraftCompletionReview, OzonDraftCompletionRun, User, db,
)
from routes.ozon_draft_ai import register_ozon_draft_ai_routes
from services.ozon_draft_ai_transport import FlashOutcome, FlashUsage
from services import ozon_draft_ai_worker as worker
from tests.test_marketplace_publications import OzonPublicationFixture


API = '/marketplaces/api/drafts'


class ImmediateExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future


class OzonDraftAIRoutesTest(OzonPublicationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(SECRET_KEY='route-ai-fixture',
                               MARKETPLACE_OZON_ENABLED=True,
                               WTF_CSRF_ENABLED=True)
        login = LoginManager(self.app)
        login.user_loader(lambda identifier: db.session.get(User, int(identifier)))
        CSRFProtect(self.app)
        register_ozon_draft_ai_routes(self.app)
        self.client = self.app.test_client()
        self._login(self.user.id)

        self.source.original_data = json.dumps({
            'title': 'Товар красный гладкий',
            'description': 'красный гладкий',
            'colors': ['красный'],
        }, ensure_ascii=False)
        for external_id, name in [('909001', 'Цвет'), ('909002', 'Фактура')]:
            db.session.add(MarketplaceAttributeDefinition(
                marketplace_id=self.marketplace.id, product_type_id=self.product_type.id,
                external_attribute_id=external_id, name=name, data_type='String',
                is_required=False, is_collection=False, max_value_count=1,
                is_enabled=True, is_available=True, last_seen_at=datetime.utcnow()))
        self.product_type.attributes_count += 2
        db.session.commit()

        profile = type('Profile', (), dict(provider='deepseek', model='deepseek-flash',
            api_key='synthetic-key', api_base_url='https://api.deepseek.com/v1',
            task_profile='seller_draft_completion_flash', max_retries=1,
            proxy_enabled=False))()
        for name in ('services.ozon_draft_ai_completion.resolve_config',
                     'services.ozon_draft_ai_worker.resolve_config'):
            mocked = patch(name, return_value=profile)
            mocked.start()
            self.addCleanup(mocked.stop)
        worker._inflight.clear()
        self.addCleanup(worker._inflight.clear)
        self.addCleanup(lambda: [row['permit'].release() for row in worker._inflight.values()])

    def _login(self, user_id):
        with self.client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        g.pop('_login_user', None)

    def _csrf(self):
        response = self.client.get(f'{API}/{self.draft.id}/ai-suggestions')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()['csrf_token']

    def _post(self, url, body, *, csrf=None):
        return self.client.post(url, json=body,
            headers={'X-CSRFToken': csrf or self._csrf()})

    def _generation(self, key='route-generate-request-1234567890'):
        return {'account_id': self.account.id, 'draft_ids': [self.draft.id],
                'expected_versions': {str(self.draft.id): self.draft.version},
                'confirm_generate': True, 'request_key': key}

    def _create(self):
        response = self._post(f'{API}/ai-completions', self._generation())
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        return response.get_json()['run']

    def _generate_suggestions(self):
        run = self._create()
        outcome = FlashOutcome('success', content={'items': [{
            'draft_id': self.draft.id,
            'suggestions': [{
                'attribute_id': attribute, 'complex_id': '0', 'group_ordinal': 0,
                'values': [{'value': value}],
                'evidence': [{'path': '/description', 'quote': value}],
                'provenance_code': 'literal_source',
            } for attribute, value in [('909001', 'красный'), ('909002', 'гладкий')]],
        }]}, http_status=200,
            usage=FlashUsage(prompt_tokens=100, completion_tokens=20))
        with patch.object(worker, 'flash_completion', return_value=outcome) as model:
            worker.tick(executor=ImmediateExecutor())
            worker.tick(executor=ImmediateExecutor())
        self.assertEqual(model.call_count, 1)
        return run

    def test_session_csrf_and_feature_gate_precede_durable_work(self):
        with self.client.session_transaction() as session:
            session.pop('_user_id', None)
        g.pop('_login_user', None)
        unauthenticated = self.client.get(f'{API}/{self.draft.id}/ai-suggestions')
        self.assertIn(unauthenticated.status_code, (302, 401))

        self._login(self.user.id)
        missing_csrf = self.client.post(f'{API}/ai-completions', json=self._generation())
        self.assertEqual(missing_csrf.status_code, 400)
        self.assertEqual(OzonDraftCompletionRun.query.count(), 0)

        csrf = self._csrf()
        self.app.config['MARKETPLACE_OZON_ENABLED'] = False
        disabled = self._post(f'{API}/ai-completions', self._generation(), csrf=csrf)
        self.assertEqual(disabled.status_code, 404)
        self.assertEqual(disabled.get_json()['code'], 'ozon_feature_disabled')
        self.assertEqual(OzonDraftCompletionRun.query.count(), 0)
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_strict_json_and_scope_reject_before_generation(self):
        url = f'{API}/ai-completions'
        csrf = self._csrf()
        valid = self._generation()
        cases = [
            (json.dumps({**valid, 'unknown': True}), 'ai_request_invalid'),
            (json.dumps(valid).replace('"account_id":', '"account_id": 99, "account_id":', 1),
             'ai_request_invalid'),
            (json.dumps({**valid, 'account_id': True}), 'ai_scope_invalid'),
            (json.dumps({**valid, 'draft_ids': [True]}), 'ai_selection_invalid'),
            (json.dumps({**valid, 'expected_versions': {str(self.draft.id): True}}),
             'ai_versions_required'),
            (json.dumps({**valid, 'confirm_generate': 1}), 'ai_confirmation_required'),
            (json.dumps(valid).replace('"account_id": 1', '"account_id": 1e309', 1),
             'ai_request_invalid'),
            ('{' + ' ' * 65536 + '}', 'ai_request_invalid'),
        ]
        for raw, code in cases:
            with self.subTest(code=code, raw_prefix=raw[:50]):
                response = self.client.post(url, data=raw, content_type='application/json',
                    headers={'X-CSRFToken': csrf})
                self.assertEqual(response.status_code, {
                    'ai_scope_invalid': 404, 'ai_versions_required': 409,
                }.get(code, 400))
                self.assertEqual(response.get_json()['code'], code)
                self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
                self.assertTrue(response.get_json()['csrf_token'])
        self.assertEqual(OzonDraftCompletionRun.query.count(), 0)
        self.assertEqual(AIParsingAttempt.query.count(), 0)
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_flag_pause_preserves_owned_readback_and_cancellation(self):
        run = self._create()
        self.app.config['MARKETPLACE_OZON_ENABLED'] = False
        for path, headers in (
            (f"{API}/ai-completions/{run['job_uid']}", {}),
            (f'{API}/ai-completions/by-request?account_id={self.account.id}',
             {'X-AI-Request-Key': self._generation()['request_key']}),
            (f'{API}/{self.draft.id}/ai-suggestions', {}),
        ):
            response = self.client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200, response.get_json())
        cancelled = self._post(f"{API}/ai-completions/{run['job_uid']}/cancel", {})
        self.assertEqual(cancelled.status_code, 200, cancelled.get_json())
        self.assertEqual(cancelled.json['run']['status'], 'cancelled')
        blocked = self._post(f'{API}/ai-completions', self._generation())
        self.assertEqual(blocked.status_code, 404)
        self.assertEqual(AIParsingAttempt.query.count(), 0)

    def test_durable_generation_readback_replay_and_conflicting_body(self):
        key = self._generation()['request_key']
        readback_url = f'{API}/ai-completions/by-request?account_id={self.account.id}'
        missing = self.client.get(readback_url, headers={'X-AI-Request-Key': key})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()['code'], 'ai_run_not_found')
        self.assertTrue(missing.get_json()['csrf_token'])

        created = self._post(f'{API}/ai-completions', self._generation())
        self.assertEqual(created.status_code, 202, created.get_data(as_text=True))
        first = created.get_json()
        self.assertFalse(first['replayed'])
        self.assertEqual(first['run']['mode'], 'draft_suggestions')
        self.assertEqual(first['run']['items'][0]['draft_id'], self.draft.id)
        self.assertEqual(created.headers['Cache-Control'], 'private, no-store')
        self.assertIn(first['run']['job_uid'], created.headers['Location'])
        self.assertEqual(AIParsingAttempt.query.count(), 0)

        found = self.client.get(readback_url, headers={'X-AI-Request-Key': key})
        detail = self.client.get(f"{API}/ai-completions/{first['run']['job_uid']}")
        self.assertEqual(found.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(found.get_json()['run']['job_uid'], first['run']['job_uid'])
        self.assertEqual(detail.get_json()['run']['job_uid'], first['run']['job_uid'])

        replay = self._post(f'{API}/ai-completions', self._generation())
        self.assertEqual(replay.status_code, 202)
        self.assertTrue(replay.get_json()['replayed'])
        self.assertEqual(replay.get_json()['run']['job_uid'], first['run']['job_uid'])
        conflict = self._post(f'{API}/ai-completions', {
            **self._generation(),
            'expected_versions': {str(self.draft.id): self.draft.version + 1},
        })
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()['code'], 'ai_request_key_conflict')
        self.assertEqual(OzonDraftCompletionRun.query.count(), 1)
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_readback_and_review_are_seller_scoped(self):
        run = self._create()
        self._login(self.foreign_user.id)
        foreign_run = self.client.get(f"{API}/ai-completions/{run['job_uid']}")
        foreign_draft = self.client.get(f'{API}/{self.draft.id}/ai-suggestions')
        foreign_request = self.client.get(
            f'{API}/ai-completions/by-request?account_id={self.account.id}',
            headers={'X-AI-Request-Key': self._generation()['request_key']})
        for response in (foreign_run, foreign_draft, foreign_request):
            self.assertEqual(response.status_code, 404)
            self.assertFalse(response.get_json()['success'])
            self.assertTrue(response.get_json()['csrf_token'])
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_cancel_requires_csrf_and_only_closes_local_ai_run(self):
        run = self._create()
        url = f"{API}/ai-completions/{run['job_uid']}/cancel"
        without_csrf = self.client.post(url, json={})
        self.assertEqual(without_csrf.status_code, 400)
        self.assertEqual(OzonDraftCompletionRun.query.one().status, 'pending')
        cancelled = self._post(url, {})
        self.assertEqual(cancelled.status_code, 200, cancelled.get_data(as_text=True))
        self.assertEqual(cancelled.get_json()['run']['status'], 'cancelled')
        self.assertEqual(cancelled.get_json()['run']['items'][0]['status'], 'cancelled')
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_review_apply_replay_stale_token_and_reject(self):
        self._generate_suggestions()
        initial = self.client.get(f'{API}/{self.draft.id}/ai-suggestions').get_json()
        self.assertEqual(len(initial['suggestions']), 2)
        self.assertTrue(all(row['applicable'] for row in initial['suggestions']))
        first_id, second_id = [row['id'] for row in initial['suggestions']]
        body = {'suggestion_ids': [first_id], 'expected_version': initial['version'],
                'review_token': initial['review_token'],
                'request_key': 'route-apply-review-1234567890'}
        url = f'{API}/{self.draft.id}/ai-suggestions/apply'
        self._login(self.foreign_user.id)
        foreign = self._post(url, body, csrf=initial['csrf_token'])
        self.assertEqual(foreign.status_code, 409)
        self.assertEqual(foreign.get_json()['code'], 'ai_review_required')
        self.assertEqual(OzonDraftCompletionReview.query.count(), 0)
        self._login(self.user.id)
        applied = self._post(url, body)
        self.assertEqual(applied.status_code, 200, applied.get_data(as_text=True))
        self.assertFalse(applied.get_json()['review']['replayed'])
        self.assertEqual(applied.get_json()['review']['version_after'], initial['version'] + 1)

        replay = self._post(url, body)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()['review']['replayed'])
        self.assertEqual(OzonDraftCompletionReview.query.count(), 1)
        key_conflict = self._post(url, {**body, 'suggestion_ids': [second_id]})
        self.assertEqual(key_conflict.status_code, 409)
        self.assertEqual(key_conflict.get_json()['code'], 'ai_review_key_conflict')
        stale = self._post(url, {**body, 'suggestion_ids': [second_id],
            'request_key': 'route-stale-review-1234567890'})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(OzonDraftCompletionReview.query.count(), 1)

        fresh = self.client.get(f'{API}/{self.draft.id}/ai-suggestions').get_json()
        self.assertEqual(fresh['version'], initial['version'] + 1)
        self.assertEqual([row['id'] for row in fresh['suggestions'] if row['applicable']], [second_id])
        wrong_id = self._post(url, {'suggestion_ids': [second_id + 1000000],
            'expected_version': fresh['version'], 'review_token': fresh['review_token'],
            'request_key': 'route-wrong-id-review-1234567890'})
        self.assertEqual(wrong_id.status_code, 409)
        self.assertEqual(wrong_id.get_json()['code'], 'ai_suggestion_conflict')
        self.assertEqual(OzonDraftCompletionReview.query.count(), 1)
        rejected = self._post(f'{API}/{self.draft.id}/ai-suggestions/reject', {
            'suggestion_ids': [second_id], 'expected_version': fresh['version'],
            'review_token': fresh['review_token'],
            'request_key': 'route-reject-review-1234567890'})
        self.assertEqual(rejected.status_code, 200, rejected.get_data(as_text=True))
        self.assertIsNone(rejected.get_json()['review']['version_after'])
        self.assertEqual(OzonDraftCompletionReview.query.count(), 2)
        self.assertEqual(MarketplaceOperation.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
