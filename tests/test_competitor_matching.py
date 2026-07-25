# -*- coding: utf-8 -*-
"""Shared competitor matching: source boundary, cache and tenant isolation."""
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from flask import Flask

from models import (
    CompetitorGroup,
    CompetitorProduct,
    CompetitorProductMatch,
    ImportedProduct,
    Product,
    Seller,
    SellerCompetitorMatchReview,
    Supplier,
    SupplierProduct,
    User,
    db,
)
from services.competitor_matching import (
    _run_llm,
    _supplier_fact_pack,
    invalidate_supplier_match_index,
    process_global_match,
    queue_group_matching,
    review_match,
    run_competitor_matching_tick,
    serialize_group_matches,
    shared_exact_match_is_admissible,
)


class CompetitorMatchingTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='test',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.seller1, self.user1 = self._seller('seller-one', 'one@test.local')
        self.seller2, self.user2 = self._seller('seller-two', 'two@test.local')
        self.supplier = Supplier(name='Observed supplier', code='observed')
        db.session.add(self.supplier)
        db.session.commit()
        invalidate_supplier_match_index()

    def tearDown(self):
        invalidate_supplier_match_index()
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _seller(self, name, email):
        user = User(username=name, email=email, is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name=name)
        db.session.add(seller)
        db.session.commit()
        return seller, user

    def _supplier_product(self):
        raw = {
            'external_id': 'feed-100',
            'vendor_code': 'RAW-100',
            'title': 'Массажёр Alpha 10 режимов',
            'description': 'Описание из реального фида',
            'brand': 'Alpha',
            'category': 'Массажёры',
            'barcodes': ['4600000000100'],
            'photo_urls': ['https://supplier.example.test/raw-100.jpg'],
            'colors': ['чёрный'],
        }
        product = SupplierProduct(
            supplier_id=self.supplier.id,
            external_id='edited-id',
            vendor_code='EDITED-CODE',
            additional_vendor_code='AI-FALLBACK-CODE',
            title='AI и ручное название, которое нельзя использовать',
            description='AI описание',
            brand='Edited brand',
            category='Edited category',
            photo_urls_json=json.dumps(['https://seller.example.test/edited.jpg']),
            original_data_json=json.dumps(raw, ensure_ascii=False),
            ai_seo_title='AI SEO title',
            ai_description='AI generated description',
            ai_parsed_data_json=json.dumps({'title': 'AI parsed'}),
            season='AI-normalized season',
            supplier_price=1000,
            status='draft',
        )
        db.session.add(product)
        db.session.commit()
        return product

    def _competitor(self, seller, nm_id=777001, title='Массажёр Alpha 10 режимов'):
        group = CompetitorGroup(seller_id=seller.id, name=f'Group {seller.id}')
        db.session.add(group)
        db.session.flush()
        product = CompetitorProduct(
            seller_id=seller.id,
            group_id=group.id,
            nm_id=nm_id,
            title=title,
            brand='Alpha',
            subject_name='Массажёры',
            image_url='https://basket.example.test/1.webp',
            photo_count=1,
            characteristics_json=json.dumps([
                {'name': 'Количество режимов', 'value': '10'},
            ], ensure_ascii=False),
            current_sale_price=1500,
        )
        db.session.add(product)
        db.session.commit()
        return group, product

    def test_supplier_fact_pack_prefers_raw_feed_and_excludes_ai(self):
        product = self._supplier_product()
        facts = _supplier_fact_pack(product)
        serialized = json.dumps(facts, ensure_ascii=False)

        self.assertEqual(facts['title'], 'Массажёр Alpha 10 режимов')
        self.assertEqual(facts['vendor_code'], 'RAW-100')
        self.assertEqual(facts['brand'], 'Alpha')
        self.assertEqual(facts['additional_vendor_code'], '')
        self.assertEqual(facts['source_mode'], 'original_data_json')
        self.assertEqual(
            facts['photos'], ['https://supplier.example.test/raw-100.jpg'])
        self.assertNotIn('AI и ручное', serialized)
        self.assertNotIn('AI generated', serialized)
        self.assertNotIn('AI parsed', serialized)
        self.assertNotIn('AI-normalized season', serialized)

    def test_completed_fingerprint_is_global_and_llm_runs_once(self):
        supplier_product = self._supplier_product()
        group1, _ = self._competitor(self.seller1)
        group2, _ = self._competitor(self.seller2)
        image_evidence = {
            'status': 'matched', 'strong_pair': True,
            'marketplace_images': 1, 'supplier_images': 1,
            'compared_pairs': 1,
        }
        llm_result = {
            'status': 'completed', 'called': True, 'model': 'test/model',
            'supplier_product_id': supplier_product.id, 'verdict': 'same',
            'matched_facts': ['название', 'фото'], 'conflicts': [],
            'reason': 'Совпали модель и изображение', 'usage': {'api_requests': 1},
        }
        with patch(
            'services.competitor_matching._image_similarity',
            return_value=(97, image_evidence),
        ) as image, patch(
            'services.competitor_matching._provider_identity',
            return_value='test/model',
        ), patch(
            'services.competitor_matching._run_llm', return_value=llm_result,
        ) as llm:
            first = process_global_match(777001)
            # Commercial feed changes belong to per-seller comparison and
            # must not invalidate the shared identity fingerprint.
            raw = json.loads(supplier_product.original_data_json)
            raw.update({'supplier_price': 1400, 'supplier_quantity': 3})
            supplier_product.original_data_json = json.dumps(
                raw, ensure_ascii=False)
            supplier_product.supplier_price = 1400
            db.session.commit()
            invalidate_supplier_match_index()
            second = process_global_match(777001)

        self.assertEqual(first['status'], 'completed')
        self.assertEqual(second['status'], 'cached')
        self.assertTrue(second['cache_hit'])
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(image.call_count, 1)
        match = CompetitorProductMatch.query.filter_by(nm_id=777001).one()
        self.assertEqual(match.suggested_supplier_product_id, supplier_product.id)
        self.assertEqual(match.llm_status, 'completed')
        self.assertTrue(shared_exact_match_is_admissible(match))

        seller1_data = serialize_group_matches(self.seller1.id, group1.id)
        seller2_data = serialize_group_matches(self.seller2.id, group2.id)
        self.assertEqual(
            seller1_data['items'][0]['match']['id'],
            seller2_data['items'][0]['match']['id'],
        )
        self.assertTrue(seller1_data['shared_cache'])
        self.assertEqual(
            seller1_data['source_scope'], 'supplier_observed_only')

    def test_llm_same_cannot_override_weak_text_and_different_photo(self):
        supplier_product = self._supplier_product()
        self._competitor(
            self.seller1,
            nm_id=777099,
            title='Массажёр',
        )
        llm_result = {
            'status': 'completed',
            'called': True,
            'model': 'test/model',
            'supplier_product_id': supplier_product.id,
            'verdict': 'same',
            'matched_facts': ['общий тип товара'],
            'conflicts': ['фото и конкретная модель не совпали'],
            'reason': 'Слишком общая похожесть',
            'usage': {'api_requests': 1},
        }
        image_evidence = {
            'status': 'different',
            'strong_pair': False,
            'marketplace_images': 1,
            'supplier_images': 1,
            'compared_pairs': 1,
        }
        with patch(
            'services.competitor_matching._image_similarity',
            return_value=(22, image_evidence),
        ), patch(
            'services.competitor_matching._provider_identity',
            return_value='test/model',
        ), patch(
            'services.competitor_matching._run_llm',
            return_value=llm_result,
        ):
            result = process_global_match(777099)

        self.assertEqual(result['status'], 'completed')
        match = CompetitorProductMatch.query.filter_by(nm_id=777099).one()
        self.assertEqual(match.predicted_match_type, 'uncertain')
        self.assertFalse(shared_exact_match_is_admissible(match))
        evidence = json.loads(match.evidence_json)
        self.assertFalse(evidence['exact_same_gate']['admitted'])
        self.assertIn(
            'llm_reported_conflicts',
            evidence['exact_same_gate']['reasons'],
        )

    def test_price_is_resolved_per_seller_by_exact_import_fk(self):
        supplier_product = self._supplier_product()
        group1, _ = self._competitor(self.seller1, nm_id=900100)
        group2, _ = self._competitor(self.seller2, nm_id=900100)
        match = CompetitorProductMatch(
            nm_id=900100,
            suggested_supplier_product_id=supplier_product.id,
            processing_status='completed',
            predicted_match_type='same',
            final_score=95,
            text_score=92,
            deterministic_score=94,
            candidates_json='[]',
            evidence_json='{}',
            marketplace_facts_json='{}',
            algorithm_version='test',
            llm_status='completed',
        )
        own1 = Product(
            seller_id=self.seller1.id, nm_id=10001,
            title='Seller edited title 1', price=2100, discount_price=2000,
        )
        own2 = Product(
            seller_id=self.seller2.id, nm_id=10002,
            title='Seller edited title 2', price=3300, discount_price=3000,
        )
        db.session.add_all([match, own1, own2])
        db.session.flush()
        db.session.add_all([
            ImportedProduct(
                seller_id=self.seller1.id,
                supplier_product_id=supplier_product.id,
                supplier_id=self.supplier.id,
                product_id=own1.id,
            ),
            ImportedProduct(
                seller_id=self.seller2.id,
                supplier_product_id=supplier_product.id,
                supplier_id=self.supplier.id,
                product_id=own2.id,
            ),
        ])
        db.session.commit()

        row1 = serialize_group_matches(self.seller1.id, group1.id)['items'][0]
        row2 = serialize_group_matches(self.seller2.id, group2.id)['items'][0]
        self.assertEqual(row1['own_product']['effective_price'], 2000.0)
        self.assertEqual(row2['own_product']['effective_price'], 3000.0)
        self.assertEqual(row1['own_product']['identity_source'],
                         'exact_imported_product_fk')
        self.assertEqual(row2['own_product']['identity_source'],
                         'exact_imported_product_fk')

    def test_seller_review_does_not_mutate_shared_suggestion(self):
        suggested = self._supplier_product()
        alternate = SupplierProduct(
            supplier_id=self.supplier.id,
            external_id='feed-200',
            title='Другой исходный товар',
            original_data_json=json.dumps({'title': 'Другой исходный товар'}),
        )
        db.session.add(alternate)
        group1, _ = self._competitor(self.seller1, nm_id=800200)
        group2, _ = self._competitor(self.seller2, nm_id=800200)
        match = CompetitorProductMatch(
            nm_id=800200,
            suggested_supplier_product_id=suggested.id,
            processing_status='completed', predicted_match_type='same',
            final_score=90, text_score=90, deterministic_score=90,
            candidates_json='[]', evidence_json='{}',
            marketplace_facts_json='{}', algorithm_version='test',
            llm_status='completed',
        )
        db.session.add(match)
        db.session.commit()

        review_match(
            self.seller1.id, self.user1.id, match.id,
            action='confirm', supplier_product_id=alternate.id,
            match_type='same',
        )
        db.session.refresh(match)
        self.assertEqual(match.suggested_supplier_product_id, suggested.id)
        self.assertEqual(SellerCompetitorMatchReview.query.count(), 1)

        row1 = serialize_group_matches(self.seller1.id, group1.id)['items'][0]
        row2 = serialize_group_matches(self.seller2.id, group2.id)['items'][0]
        self.assertEqual(row1['effective_supplier']['id'], alternate.id)
        self.assertEqual(row1['effective_source'], 'seller_confirmed')
        self.assertEqual(row2['effective_supplier']['id'], suggested.id)
        self.assertEqual(row2['effective_source'], 'shared_suggestion')

    def test_supplier_search_does_not_search_edited_columns_when_raw_exists(self):
        product = self._supplier_product()
        from services.competitor_matching import search_supplier_products

        self.assertEqual(search_supplier_products('AI ручное'), [])
        observed = search_supplier_products('Массажёр Alpha')
        self.assertEqual([item['id'] for item in observed], [product.id])

    def test_active_global_claim_blocks_external_work(self):
        self._supplier_product()
        self._competitor(self.seller1, nm_id=733100)
        db.session.add(CompetitorProductMatch(
            nm_id=733100,
            processing_status='processing',
            algorithm_version='supplier-observed-v1',
            claim_token='other-worker',
            claim_expires_at=datetime.utcnow() + timedelta(minutes=5),
            llm_status='pending',
        ))
        db.session.commit()

        with patch(
            'services.competitor_matching._provider_identity',
            return_value='test/model',
        ), patch(
            'services.competitor_matching._image_similarity',
        ) as image, patch(
            'services.competitor_matching._run_llm',
        ) as llm:
            result = process_global_match(733100)

        self.assertEqual(result['status'], 'shared_in_progress')
        image.assert_not_called()
        llm.assert_not_called()

    def test_scheduler_tick_is_bounded_and_resumable(self):
        group = CompetitorGroup(seller_id=self.seller1.id, name='Batch')
        db.session.add(group)
        db.session.flush()
        for nm_id in (910001, 910002, 910003, 910004):
            db.session.add(CompetitorProduct(
                seller_id=self.seller1.id,
                group_id=group.id,
                nm_id=nm_id,
                title=f'Observed {nm_id}',
            ))
        db.session.commit()
        job = queue_group_matching(self.seller1.id, group.id)

        def completed(nm_id, *, allow_llm=True):
            return {
                'status': 'completed', 'nm_id': nm_id,
                'match_id': nm_id, 'llm_called': allow_llm,
                'cache_hit': False,
            }

        with patch.dict(os.environ, {
            'COMPETITOR_MATCH_ITEMS_PER_TICK': '2',
            'COMPETITOR_MATCH_TICK_SECONDS': '55',
        }), patch(
            'services.competitor_matching.process_global_match',
            side_effect=completed,
        ):
            first = run_competitor_matching_tick(self.app)
            db.session.refresh(job)
            self.assertEqual(first['processed'], 2)
            self.assertEqual(job.status, 'running')
            self.assertEqual(job.processed, 2)

            second = run_competitor_matching_tick(self.app)
            db.session.refresh(job)

        self.assertEqual(second['processed'], 2)
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.processed, 4)
        self.assertEqual(job.succeeded, 4)

    def test_llm_classifier_disables_thinking_and_sanitizes_parse_error(self):
        captured = {}
        candidate = {
            'supplier_product_id': 42,
            'text_score': 96,
            'image_evidence': {'strong_pair': True},
            '_record': {'facts': {
                'supplier_product_id': 42,
                'source_mode': 'original_data_json',
                'title': 'Observed supplier product',
                'photos': ['https://supplier.test/source.jpg'],
            }},
        }

        class GoodLLM:
            def structured_output_with_usage(self, **kwargs):
                captured['request'] = kwargs
                return {
                    'data': {
                        'supplier_product_id': 42,
                        'verdict': 'same',
                        'matched_facts': ['model'],
                        'conflicts': [],
                        'reason': 'Observed facts agree',
                    },
                    'usage': {'input_tokens': 10, 'output_tokens': 5},
                }

        def make_good(profile):
            captured['profile'] = profile
            return GoodLLM()

        central = {
            'provider': 'deepseek', 'api_key': 'synthetic-key',
            'model': 'deepseek-v4-pro', 'base_url': '',
        }
        with patch(
            'services.llm_config.get_central_llm_config',
            return_value=central,
        ), patch(
            'agents.llm.create_llm_from_profile', side_effect=make_good,
        ):
            good = _run_llm({'title': 'Observed WB product'}, [candidate])

        self.assertEqual(good['status'], 'completed')
        self.assertIs(captured['profile']['thinking'], False)
        self.assertEqual(captured['request']['max_tokens'], 650)

        class InvalidJSONLLM:
            def structured_output_with_usage(self, **kwargs):
                error = ValueError(
                    'Cannot extract JSON from LLM response: secret raw body')
                error.llm_usage = {'input_tokens': 9, 'output_tokens': 650}
                raise error

        with patch(
            'services.llm_config.get_central_llm_config',
            return_value=central,
        ), patch(
            'agents.llm.create_llm_from_profile',
            return_value=InvalidJSONLLM(),
        ):
            failed = _run_llm({'title': 'Observed WB product'}, [candidate])

        self.assertEqual(failed['error_code'], 'llm_invalid_json')
        self.assertNotIn('secret raw body', json.dumps(failed))


if __name__ == '__main__':
    unittest.main()
