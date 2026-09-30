"""Seller opt-in AI: actual local service, native grounding and atomic review."""
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from models import (db, MarketplaceAttributeDefinition, MarketplaceOperation,
    AIParsingAttempt, OzonDraftCompletionSuggestion as Suggestion,
    OzonDraftCompletionReview as Review, OzonDraftCompletionItem as Item,
    ImportedProduct, MarketplaceProductDraft)
from services.ozon_draft_ai_completion import OzonDraftAICompletionService as Service, DraftAIError
from services.ozon_draft_ai_transport import FlashOutcome, FlashUsage
from services import ozon_draft_ai_worker as worker
from tests.test_marketplace_publications import OzonPublicationFixture


class ImmediateExecutor:
    def submit(self, function, *args, **kwargs):
        result = Future()
        try:
            result.set_result(function(*args, **kwargs))
        except Exception as exc:
            result.set_exception(exc)
        return result


class OzonDraftAICompletionTest(OzonPublicationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app.config['SECRET_KEY'] = 'local-ai-fixture'
        self.source.original_data = json.dumps({'title': 'Товар красный гладкий',
            'description': 'красный гладкий', 'colors': ['красный']}, ensure_ascii=False)
        for external_id, name in [('909001', 'Цвет'), ('909002', 'Фактура')]:
            db.session.add(MarketplaceAttributeDefinition(
                marketplace_id=self.marketplace.id, product_type_id=self.product_type.id,
                external_attribute_id=external_id, name=name, data_type='String',
                is_required=False, is_collection=False, max_value_count=1,
                is_enabled=True, is_available=True, last_seen_at=datetime.utcnow()))
        self.product_type.attributes_count += 2
        db.session.commit()
        config = SimpleNamespace(provider='deepseek', model='deepseek-flash',
            api_key='synthetic-key', api_base_url='https://api.deepseek.com/v1',
            task_profile='seller_draft_completion_flash', max_retries=1, proxy_enabled=False)
        for name in ('services.ozon_draft_ai_completion.resolve_config',
                     'services.ozon_draft_ai_worker.resolve_config'):
            mocked = patch(name, return_value=config)
            mocked.start(); self.addCleanup(mocked.stop)
        worker._inflight.clear()
        self.addCleanup(worker._inflight.clear)
        self.addCleanup(lambda: [row['permit'].release() for row in worker._inflight.values()])

    def accept(self, key='ai-exact-synthetic-request-1234567890'):
        return Service.accept(seller_id=self.seller.id, account_id=self.account.id,
            draft_ids=[self.draft.id], expected_versions={str(self.draft.id): self.draft.version},
            request_key=key, actor_user_id=self.user.id)

    def model_response(self, values=None):
        values = values or [('909001', 'красный'), ('909002', 'гладкий')]
        return FlashOutcome('success', content={'items': [{'draft_id': self.draft.id,
            'suggestions': [{'attribute_id': identity, 'complex_id': '0', 'group_ordinal': 0,
                'values': [{'value': value}], 'evidence': [{'path': '/description', 'quote': value}],
                'provenance_code': 'literal_source'} for identity, value in values]}]},
            http_status=200, usage=FlashUsage(prompt_tokens=100, completion_tokens=20))

    def generate(self, outcome=None):
        run, _ = self.accept()
        before = self.draft.attributes_json
        def physical(*args, **kwargs):
            attempt = AIParsingAttempt.query.one()
            self.assertEqual(attempt.status, 'reserved')
            self.assertEqual(Item.query.one().status, 'reserved')
            self.assertFalse(db.session.connection().connection.driver_connection.in_transaction)
            return outcome or self.model_response()
        with patch.object(worker, 'flash_completion', side_effect=physical) as call:
            worker.tick(executor=ImmediateExecutor())
            worker.tick(executor=ImmediateExecutor())
            self.assertEqual(call.call_count, 1)
        self.assertEqual(self.draft.attributes_json, before)
        self.assertEqual(MarketplaceOperation.query.count(), 0)
        return run

    def review_doc(self):
        return Service.suggestions_document(seller_id=self.seller.id,
            draft_id=self.draft.id, actor_user_id=self.user.id)

    def second_draft(self):
        source = ImportedProduct(seller_id=self.seller.id, external_id='source-2',
            external_vendor_code='safe-offer-2', source_type='synthetic',
            title='Второй товар красный гладкий', category='Категория',
            description='красный гладкий', original_data=self.source.original_data)
        db.session.add(source)
        db.session.flush()
        values = {column.name: getattr(self.draft, column.name)
                  for column in MarketplaceProductDraft.__table__.columns
                  if column.name not in {'id', 'imported_product_id', 'offer_id',
                                         'created_at', 'updated_at'}}
        values.update(imported_product_id=source.id, offer_id='safe-offer-2')
        draft = MarketplaceProductDraft(**values)
        db.session.add(draft)
        db.session.commit()
        return draft

    def test_admission_is_local_replay_and_prevents_concurrent_duplicate(self):
        run, replayed = self.accept()
        again, second = self.accept()
        self.assertFalse(replayed); self.assertTrue(second)
        self.assertEqual(run.id, again.id)
        self.assertEqual(AIParsingAttempt.query.count(), 0)
        with self.assertRaises(DraftAIError):
            self.accept('different-key-for-same-draft-1234567890')
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_whole_scope_is_seller_owned(self):
        with self.assertRaises(Exception):
            Service.accept(seller_id=self.foreign_seller.id, account_id=self.account.id,
                draft_ids=[self.draft.id], expected_versions={str(self.draft.id): self.draft.version},
                request_key='foreign-key-exact-12345678901234', actor_user_id=self.foreign_user.id)
        self.assertEqual(Item.query.count(), 0)

    def test_category_prompt_prefix_does_not_contain_draft_identity_or_source(self):
        from services.ozon_draft_ai_validation import OzonDraftAIValidation
        context = OzonDraftAIValidation.capture(self.draft)
        altered = deepcopy(context)
        altered['draft_id'] += 999
        altered['source_facts']['title'] = 'Другой исходный товар'
        first, second = worker._messages([context]), worker._messages([altered])
        self.assertEqual(first[0], second[0])
        self.assertNotIn('Товар красный гладкий', first[0]['content'])
        self.assertNotEqual(first[1], second[1])
        self.assertEqual(json.loads(second[1]['content'])['items'][0]['draft_id'], altered['draft_id'])

    def test_prompt_uses_rooted_json_pointers_and_exact_source_quotes(self):
        prompt = ' '.join(worker._SYSTEM.split())
        self.assertIn('RFC 6901 JSON Pointer', prompt)
        self.assertIn('source_facts itself is the root', prompt)
        for path in ('/description', '/title', '/colors/0', '/characteristics/0/value'):
            self.assertIn(path, prompt)
        self.assertIn('point to a scalar value; never invent a path', prompt)
        self.assertIn('quote must be exact literal text copied from the value at that path', prompt)
        self.assertIn('do not paraphrase, normalize, translate, or combine facts', prompt)

    def test_missing_source_is_explicit_without_model_call(self):
        self.source.original_data = None
        db.session.commit()
        run, _ = self.accept()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(Item.query.one().status, 'needs_input')
        self.assertEqual(AIParsingAttempt.query.count(), 0)

    def test_generation_then_partial_apply_revalidation_and_exact_replay(self):
        run = self.generate()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(Suggestion.query.count(), 2)
        first = self.review_doc()
        one = first['suggestions'][0]['id']
        kwargs = dict(seller_id=self.seller.id, draft_id=self.draft.id, actor_user_id=self.user.id,
            suggestion_ids=[one], expected_version=first['version'], review_token=first['review_token'],
            request_key='accept-one-exact-key-12345678901234')
        applied = Service.review(**kwargs)
        replay = Service.review(**kwargs)
        self.assertEqual(applied['version_after'], first['version'] + 1)
        self.assertTrue(replay['replayed'])
        self.assertEqual(Review.query.count(), 1)
        with self.assertRaises(DraftAIError):
            Service.review(**{**kwargs, 'suggestion_ids': [first['suggestions'][1]['id']],
                              'request_key': 'stale-review-key-1234567890123456'})
        fresh = self.review_doc()
        remaining = [x for x in fresh['suggestions'] if x['applicable']]
        self.assertEqual(len(remaining), 1)
        Service.review(seller_id=self.seller.id, draft_id=self.draft.id, actor_user_id=self.user.id,
            suggestion_ids=[remaining[0]['id']], expected_version=fresh['version'],
            review_token=fresh['review_token'], request_key='accept-two-exact-key-12345678901234')
        self.assertEqual(Review.query.count(), 2)
        self.assertEqual(self.draft.version, first['version'] + 2)
        self.assertEqual(MarketplaceOperation.query.count(), 0)

    def test_manual_change_blocks_apply_and_preserves_values(self):
        self.generate()
        doc = self.review_doc()
        self.draft.attributes_json = json.dumps([{'attribute_id': '909001', 'complex_id': '0',
                                                   'values': [{'value': 'синий'}]}])
        db.session.commit()
        with self.assertRaises(DraftAIError):
            Service.review(seller_id=self.seller.id, draft_id=self.draft.id, actor_user_id=self.user.id,
                suggestion_ids=[doc['suggestions'][0]['id']], expected_version=doc['version'],
                review_token=doc['review_token'], request_key='stale-edit-key-123456789012345678')
        self.assertEqual(json.loads(self.draft.attributes_json)[0]['values'][0]['value'], 'синий')
        self.assertEqual(Review.query.count(), 0)

    def test_ungrounded_result_does_not_become_suggestion(self):
        run = self.generate(self.model_response([('909001', 'зелёный')]))
        self.assertEqual(Suggestion.query.count(), 0)
        self.assertEqual(Item.query.one().status, 'no_evidence')
        self.assertEqual(Item.query.one().safe_code,
                         'ai_rejection:quote_not_in_source')
        summary = run.job.get_result()
        self.assertEqual(summary['rejection_reasons'], {'quote_not_in_source': 1})
        run_document = Service.document(run)
        self.assertEqual(run_document['items'][0]['code'], 'ai_fields_rejected')
        review_document = Service.suggestions_document(seller_id=self.seller.id,
            draft_id=self.draft.id, actor_user_id=self.user.id)
        self.assertEqual(review_document['item']['code'], 'ai_fields_rejected')

    def test_rejection_codes_are_allowlisted_primary_and_do_not_leak_values(self):
        secret_value = 'MODEL_ONLY_VALUE_7b93'
        self.assertTrue(all(len(worker.AI_REJECTION_SAFE_PREFIX + code) <= 80
                            for code in worker.VALIDATOR_REJECTION_CODES))
        self.assertEqual(worker._primary_rejection_code(['value_not_grounded']),
                         'ai_rejection:value_not_grounded')
        self.assertEqual(worker._primary_rejection_code([
            'value_not_grounded', 'value_not_grounded', 'quote_not_in_source',
        ]), 'ai_rejection:value_not_grounded')
        self.assertEqual(worker._primary_rejection_code([
            'value_not_grounded', 'quote_not_in_source',
        ]), 'ai_rejection:quote_not_in_source')
        self.assertEqual(worker._primary_rejection_code([secret_value]),
                         'ai_rejection:unclassified')
        self.assertNotIn(secret_value, worker._primary_rejection_code([secret_value]))

        second = self.second_draft()
        run, _ = Service.accept(seller_id=self.seller.id, account_id=self.account.id,
            draft_ids=[self.draft.id, second.id],
            expected_versions={str(self.draft.id): self.draft.version,
                               str(second.id): second.version},
            request_key='multi-rejection-run-key-1234567890', actor_user_id=self.user.id)

        def row(attribute_id, value, quote):
            return {'attribute_id': attribute_id, 'complex_id': '0', 'group_ordinal': 0,
                    'values': [{'value': value}], 'evidence': [
                        {'path': '/description', 'quote': quote}],
                    'provenance_code': 'literal_source'}

        outcome = FlashOutcome('success', content={'items': [
            {'draft_id': self.draft.id, 'suggestions': [
                row('909001', secret_value, 'QUOTE_NOT_IN_SOURCE'),
                row('909002', 'UNSUPPORTED_VALUE', 'красный'),
            ]},
            {'draft_id': second.id, 'suggestions': [
                row('909002', 'гладкий', 'гладкий'),
                row('909001', secret_value, 'красный'),
            ]},
        ]}, http_status=200, usage=FlashUsage(prompt_tokens=100, completion_tokens=20))
        with patch.object(worker, 'flash_completion', return_value=outcome) as call:
            worker.tick(executor=ImmediateExecutor())
            worker.tick(executor=ImmediateExecutor())
            self.assertEqual(call.call_count, 1)

        items = Item.query.filter_by(run_id=run.id).order_by(Item.ordinal).all()
        self.assertEqual([item.status for item in items], ['no_evidence', 'proposed'])
        self.assertEqual([item.safe_code for item in items], [
            'ai_rejection:quote_not_in_source',
            'ai_rejection:value_not_grounded',
        ])
        self.assertEqual(Suggestion.query.count(), 1)
        document = Service.document(run)
        expected = {'quote_not_in_source': 1, 'value_not_grounded': 1}
        self.assertEqual(document['summary']['rejection_reasons'], expected)
        self.assertEqual(document['summary']['counts'], {'no_evidence': 1, 'proposed': 1})
        self.assertEqual([item['code'] for item in document['items']],
                         ['ai_fields_rejected', 'ai_fields_rejected'])
        diagnostics = json.dumps({'codes': [item.safe_code for item in items],
                                  'summary': document['summary']}, ensure_ascii=False)
        self.assertNotIn(secret_value, diagnostics)
        self.assertNotIn('UNSUPPORTED_VALUE', diagnostics)
        self.assertNotIn('/description', diagnostics)

        Service._refresh(run, flush=True)
        self.assertEqual(Service.document(run)['summary']['rejection_reasons'], expected)

    def test_legacy_rejection_code_is_read_as_unclassified_for_old_runs(self):
        run = self.generate()
        item = Item.query.one()
        item.safe_code = 'ai_fields_rejected'
        db.session.commit()
        self.assertEqual(Service.document(run)['summary']['rejection_reasons'],
                         {'unclassified': 1})
        Service._refresh(run, flush=True)
        self.assertEqual(Service.document(run)['summary']['rejection_reasons'],
                         {'unclassified': 1})

    def test_unknown_response_is_terminal_and_never_retried(self):
        self.generate(FlashOutcome('unknown_response', safe_code='ai_response_unknown'))
        self.assertEqual(Item.query.one().status, 'unknown_response')
        with patch.object(worker, 'flash_completion', side_effect=AssertionError('must not retry')):
            worker.tick(executor=ImmediateExecutor())
        self.assertEqual(AIParsingAttempt.query.count(), 1)

    def test_cancelled_late_response_only_records_usage(self):
        run, _ = self.accept()
        future = Future()
        executor = SimpleNamespace(submit=lambda *a, **k: future)
        worker.tick(executor=executor)
        Service.cancel(seller_id=self.seller.id, job_uid=run.job.job_uid)
        future.set_result(self.model_response())
        worker.tick(executor=executor)
        self.assertEqual(Suggestion.query.count(), 0)
        self.assertEqual(Item.query.one().status, 'cancelled')
        self.assertEqual(AIParsingAttempt.query.one().prompt_tokens, 100)

    def test_observed_rate_limit_preserves_full_due_and_does_not_repeat(self):
        run = self.generate(FlashOutcome('rate_limited', http_status=429,
            retry_after_seconds=900, safe_code='ai_rate_limited'))
        item = Item.query.one()
        self.assertEqual(item.status, 'pending')
        self.assertGreater((item.next_due_at - datetime.utcnow()).total_seconds(), 890)
        self.assertEqual(item.next_due_at, run.next_due_at)
        with patch.object(worker, 'flash_completion', side_effect=AssertionError('provider cooldown')):
            worker.tick(executor=ImmediateExecutor())
        self.assertEqual(AIParsingAttempt.query.count(), 1)

    def test_source_drift_after_admission_consumes_no_physical_budget(self):
        self.accept()
        self.source.original_data = json.dumps({'title': 'Другой исходный товар'})
        db.session.commit()
        with patch.object(worker, 'flash_completion', side_effect=AssertionError('source drift')):
            worker.tick(executor=ImmediateExecutor())
        self.assertEqual(Item.query.one().status, 'stale')
        self.assertEqual(AIParsingAttempt.query.count(), 0)

    def test_physical_capacity_defers_before_reserving_durable_budget(self):
        self.accept()
        with patch.object(worker, 'try_acquire_flash_permit', return_value=None), \
             patch.object(worker, 'flash_completion', side_effect=AssertionError('no physical slot')):
            result = worker.tick(executor=ImmediateExecutor())
        self.assertEqual(result['started_calls'], 0)
        self.assertEqual(result['deferred'], 1)
        self.assertEqual(AIParsingAttempt.query.count(), 0)
        self.assertEqual(Item.query.one().status, 'pending')
        self.assertEqual(Item.query.one().attempt_count, 0)
        self.assertEqual(Item.query.one().safe_code, 'ai_local_capacity')

    def test_crash_after_durable_reservation_never_replays_and_readback_counts_it(self):
        run, _ = self.accept()
        chunk = worker._claim_chunk(run.id, datetime.utcnow())
        from services.ai_parsing_budget import reserve_attempt, SELLER_LANE
        reserve_attempt(call_id=chunk['call_id'], lane=SELLER_LANE,
            run_uid=run.job.job_uid, request_fingerprint='e' * 64,
            seller_id=self.seller.id, item_count=run.item_count)
        self.assertEqual(run.requested_calls, 0)  # simulated crash before HTTP admission
        self.assertEqual(Service.document(run)['physical_calls'], 1)
        worker._recover_expired(datetime.utcnow() + timedelta(seconds=125))
        self.assertEqual(Item.query.one().status, 'unknown_response')
        with patch.object(worker, 'flash_completion', side_effect=AssertionError('crash replay')):
            worker.tick(executor=ImmediateExecutor())
        self.assertEqual(AIParsingAttempt.query.count(), 1)

    def test_review_audit_failure_rolls_back_draft_suggestion_and_request_key(self):
        from sqlalchemy import event
        self.generate()
        before = self.draft.attributes_json
        doc = self.review_doc()
        def fail_review(*_args):
            raise RuntimeError('synthetic audit commit failure')
        event.listen(Review, 'before_insert', fail_review)
        try:
            with self.assertRaises(RuntimeError):
                Service.review(seller_id=self.seller.id, draft_id=self.draft.id, actor_user_id=self.user.id,
                    suggestion_ids=[doc['suggestions'][0]['id']], expected_version=doc['version'],
                    review_token=doc['review_token'], request_key='atomic-audit-failure-key-1234567890')
        finally:
            event.remove(Review, 'before_insert', fail_review)
        db.session.expire_all()
        self.assertEqual(self.draft.version, doc['version'])
        self.assertEqual(self.draft.attributes_json, before)
        self.assertEqual(Review.query.count(), 0)
        self.assertTrue(all(row.status == 'proposed' for row in Suggestion.query.all()))

    def test_two_hundred_cards_start_two_distinct_chunks_with_shared_admin_slot(self):
        from models import ImportedProduct, MarketplaceProductDraft
        from services.ai_parsing_budget import reserve_attempt, ADMIN_LANE, SELLER_LANE, BudgetDenied
        ids = [self.draft.id]
        base = {column.name: deepcopy(getattr(self.draft, column.name))
                for column in MarketplaceProductDraft.__table__.columns
                if column.name not in ('id', 'version', 'imported_product_id', 'offer_id', 'created_at', 'updated_at')}
        for number in range(1, 200):
            source = ImportedProduct(seller_id=self.seller.id, external_id=f'ai-source-{number}',
                source_type='synthetic', title='Красный товар', original_data=self.source.original_data)
            db.session.add(source); db.session.flush()
            draft = MarketplaceProductDraft(**base, imported_product_id=source.id, offer_id=f'ai-offer-{number}')
            db.session.add(draft); db.session.flush()
            ids.append(draft.id)
        db.session.commit()
        versions = dict(db.session.query(MarketplaceProductDraft.id, MarketplaceProductDraft.version).filter(
            MarketplaceProductDraft.id.in_(ids)))
        run, _ = Service.accept(seller_id=self.seller.id, account_id=self.account.id,
            draft_ids=ids, expected_versions={str(k): v for k, v in versions.items()},
            request_key='two-hundred-exact-request-1234567890', actor_user_id=self.user.id)
        self.assertEqual(run.item_count, 200)
        futures = []
        def deferred(*args, **kwargs):
            future = Future(); futures.append(future); return future
        stats = worker.tick(executor=SimpleNamespace(submit=deferred))
        self.assertEqual(stats['started_calls'], 2)
        self.assertEqual(len(futures), 2)
        self.assertEqual(Item.query.filter_by(status='reserved').count(), 12)
        self.assertEqual(Item.query.filter_by(status='pending').count(), 188)
        self.assertEqual(AIParsingAttempt.query.count(), 2)
        admin = reserve_attempt(call_id='a' * 32, lane=ADMIN_LANE, run_uid='synthetic-admin',
            request_fingerprint='a' * 64, max_run_calls=12)
        self.assertNotIsInstance(admin, BudgetDenied)
        fourth = reserve_attempt(call_id='b' * 32, lane=SELLER_LANE, run_uid='synthetic-other-seller',
            request_fingerprint='b' * 64, seller_id=self.foreign_seller.id, item_count=1)
        self.assertIsInstance(fourth, BudgetDenied)
        self.assertEqual(fourth.code, 'ai_global_capacity_full')
        self.assertEqual(MarketplaceOperation.query.count(), 0)


class OzonDraftAIProfileTest(OzonPublicationFixture, unittest.TestCase):
    def test_native_seller_key_is_supported_without_central_profile(self):
        from models import AutoImportSettings
        from services.ozon_draft_ai_completion import resolve_config
        settings = AutoImportSettings(seller_id=self.seller.id, ai_provider='deepseek',
            ai_api_key='synthetic-seller-native-key', ai_api_base_url='https://api.deepseek.com/v1')
        db.session.add(settings); db.session.commit()
        with patch('services.llm_config.get_central_llm_config', return_value=None):
            profile = resolve_config(self.seller.id)
        self.assertEqual(profile.api_key, 'synthetic-seller-native-key')
        self.assertEqual(profile.model, 'deepseek-flash')
        self.assertEqual(profile.task_profile, 'seller_draft_completion_flash')
        self.assertFalse(profile.proxy_enabled)
        self.assertEqual(profile.max_retries, 1)

    def test_central_native_policy_and_nonnative_rejection(self):
        from services.ozon_draft_ai_completion import resolve_config
        central = {'provider': 'deepseek', 'api_key': 'synthetic-central-key',
                   'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-v4-pro'}
        with patch('services.llm_config.get_central_llm_config', return_value=central):
            profile = resolve_config(self.seller.id)
        self.assertEqual(profile.api_key, 'synthetic-central-key')
        self.assertEqual(profile.model, 'deepseek-flash')
        for changed in ({'provider': 'openrouter', 'base_url': 'https://openrouter.ai/api/v1'},
                        {'base_url': 'https://unrelated.invalid/v1'}):
            with self.subTest(changed=changed), patch('services.llm_config.get_central_llm_config',
                                                   return_value={**central, **changed}):
                with self.assertRaises(DraftAIError):
                    resolve_config(self.seller.id)
