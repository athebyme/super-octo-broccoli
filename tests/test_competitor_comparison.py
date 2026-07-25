# -*- coding: utf-8 -*-
"""Cross-competitor exact-product comparison and HTTP boundary."""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

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
from routes.competitors import register_competitor_routes
from services.competitor_comparison import build_competitor_comparison


class CompetitorComparisonTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY='comparison-test',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        register_competitor_routes(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.user, self.seller = self._seller('comparison', 'comparison@test.local')
        self.foreign_user, self.foreign_seller = self._seller(
            'foreign-comparison', 'foreign-comparison@test.local',
        )
        self.supplier = Supplier(name='Observed supplier', code='comparison-feed')
        db.session.add(self.supplier)
        db.session.flush()
        self.source_product = self._supplier_product(
            'source-1', 'RAW-1', 'Исходный товар Alpha',
        )
        self.alternate_product = self._supplier_product(
            'source-2', 'RAW-2', 'Исходный товар Beta',
        )
        self.group_a = CompetitorGroup(
            seller_id=self.seller.id, name='Знакомый А', color='#7C5CF6',
            auto_source='seller', auto_source_value='11001',
        )
        self.group_b = CompetitorGroup(
            seller_id=self.seller.id, name='Знакомый Б', color='#06B6D4',
            auto_source='seller', auto_source_value='22002',
        )
        self.foreign_group = CompetitorGroup(
            seller_id=self.foreign_seller.id, name='Чужая группа',
            auto_source='seller', auto_source_value='33003',
        )
        db.session.add_all([self.group_a, self.group_b, self.foreign_group])
        db.session.flush()
        self.offer_a = self._offer(
            self.seller.id, self.group_a.id, 700001, 900,
            supplier_id=11001, supplier_name='WB seller A',
        )
        self.offer_b = self._offer(
            self.seller.id, self.group_b.id, 700002, 1100,
            supplier_id=22002, supplier_name='WB seller B',
        )
        self.foreign_offer = self._offer(
            self.foreign_seller.id, self.foreign_group.id, 700003, 1,
            supplier_id=33003, supplier_name='Must not leak',
        )
        self.match_a = self._match(700001, self.source_product.id)
        self.match_b = self._match(700002, self.source_product.id)
        self.foreign_match = self._match(700003, self.source_product.id)
        own = Product(
            seller_id=self.seller.id, nm_id=900001,
            title='Отредактированное название нашей карточки',
            price=1200, discount_price=1000, quantity=8,
            wb_public_base_price=1200,
            wb_public_final_price=1000,
            wb_public_price_synced_at=datetime.utcnow(),
        )
        db.session.add(own)
        db.session.flush()
        db.session.add(ImportedProduct(
            seller_id=self.seller.id,
            supplier_id=self.supplier.id,
            supplier_product_id=self.source_product.id,
            product_id=own.id,
        ))
        db.session.commit()
        self.client = self.app.test_client()
        self.login = MagicMock()
        self.login.is_authenticated = True
        self.login.id = self.user.id
        self.login.seller = self.seller

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _seller(self, username, email):
        user = User(username=username, email=email, is_active=True)
        user.set_password('synthetic-password')
        seller = Seller(user=user, company_name=username)
        db.session.add(seller)
        db.session.commit()
        return user, seller

    def _supplier_product(self, external_id, vendor_code, title):
        product = SupplierProduct(
            supplier_id=self.supplier.id,
            external_id=f'edited-{external_id}',
            vendor_code=f'EDITED-{vendor_code}',
            title=f'AI/edited {title}',
            ai_seo_title='AI title must not be rendered as identity',
            original_data_json=json.dumps({
                'external_id': external_id,
                'vendor_code': vendor_code,
                'title': title,
                'brand': 'Observed brand',
                'category': 'Observed category',
                'photo_urls': [f'https://supplier.test/{external_id}.jpg'],
            }, ensure_ascii=False),
        )
        db.session.add(product)
        db.session.flush()
        return product

    def _offer(
        self, seller_id, group_id, nm_id, price,
        *, supplier_id, supplier_name,
    ):
        product = CompetitorProduct(
            seller_id=seller_id,
            group_id=group_id,
            nm_id=nm_id,
            title=f'Observed WB {nm_id}',
            brand='Observed brand',
            wb_supplier_id=supplier_id,
            supplier_name=supplier_name,
            current_price=price + 100,
            current_sale_price=price,
            current_total_stock=5,
            last_price_at=datetime.utcnow(),
            is_active=True,
        )
        db.session.add(product)
        db.session.flush()
        return product

    def _match(self, nm_id, supplier_product_id, match_type='same'):
        match = CompetitorProductMatch(
            nm_id=nm_id,
            suggested_supplier_product_id=supplier_product_id,
            processing_status='completed',
            predicted_match_type=match_type,
            text_score=90,
            image_score=88,
            deterministic_score=89,
            final_score=91,
            candidates_json='[]',
            evidence_json='{}',
            marketplace_facts_json='{}',
            algorithm_version='test',
            llm_status='completed',
        )
        db.session.add(match)
        db.session.flush()
        return match

    def _as_user(self):
        return patch('flask_login.utils._get_user', return_value=self.login)

    def test_groups_same_identity_across_competitors_and_joins_own_price(self):
        payload = build_competitor_comparison(self.seller.id)

        self.assertEqual(payload['summary']['identities'], 1)
        self.assertEqual(payload['summary']['offers'], 2)
        self.assertEqual(payload['summary']['competitors'], 2)
        self.assertEqual(payload['summary']['with_own'], 1)
        self.assertEqual(payload['summary']['undercut'], 1)
        self.assertEqual(payload['identity_scope'], 'exact_same_only')
        row = payload['items'][0]
        self.assertEqual(row['supplier']['title'], 'Исходный товар Alpha')
        self.assertEqual(row['supplier']['vendor_code'], 'RAW-1')
        self.assertNotIn('AI/edited', json.dumps(row['supplier'], ensure_ascii=False))
        self.assertEqual(row['own_product']['effective_price'], 1000)
        self.assertEqual(
            row['own_product']['identity_source'], 'exact_imported_product_fk',
        )
        self.assertEqual(row['metrics']['min_competitor_price'], 900)
        self.assertEqual(row['metrics']['median_competitor_price'], 1000)
        self.assertEqual(row['metrics']['own_position'], 2)
        self.assertEqual(
            [offer['effective_price'] for offer in row['offers']], [900, 1100],
        )
        self.assertEqual(
            [offer['delta_to_own_percent'] for offer in row['offers']],
            [-10.0, 10.0],
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('Must not leak', serialized)
        self.assertNotIn('700003', serialized)

    def test_one_competitor_filter_and_search_are_bounded_to_group(self):
        payload = build_competitor_comparison(
            self.seller.id, group_id=self.group_b.id,
            query='Знакомый Б', scope='linked', sort='price',
        )
        self.assertEqual(payload['pagination']['total'], 1)
        self.assertEqual(payload['summary']['offers'], 1)
        self.assertEqual(len(payload['items'][0]['offers']), 1)
        self.assertEqual(
            payload['items'][0]['offers'][0]['competitor']['group_id'],
            self.group_b.id,
        )

        missing = build_competitor_comparison(
            self.seller.id, group_id=self.group_b.id, query='нет такого товара',
        )
        self.assertEqual(missing['pagination']['total'], 0)
        self.assertEqual(missing['items'], [])

    def test_review_override_wins_and_non_exact_rows_are_excluded(self):
        self.match_b.predicted_match_type = 'analog'
        db.session.add_all([
            SellerCompetitorMatchReview(
                seller_id=self.seller.id,
                match_id=self.match_a.id,
                supplier_product_id=self.alternate_product.id,
                status='confirmed', match_type='same',
            ),
            SellerCompetitorMatchReview(
                seller_id=self.seller.id,
                match_id=self.match_b.id,
                supplier_product_id=None,
                status='rejected', match_type=None,
            ),
        ])
        db.session.commit()

        payload = build_competitor_comparison(self.seller.id)
        self.assertEqual(payload['summary']['identities'], 1)
        self.assertEqual(payload['summary']['offers'], 1)
        self.assertEqual(payload['summary']['excluded_non_exact'], 1)
        row = payload['items'][0]
        self.assertEqual(row['supplier']['id'], self.alternate_product.id)
        self.assertEqual(row['offers'][0]['match']['source'], 'seller_confirmed')
        self.assertTrue(row['offers'][0]['match']['reviewed'])
        self.assertIsNone(row['own_product'])

    def test_duplicate_nm_id_across_groups_is_not_double_counted(self):
        duplicate = self._offer(
            self.seller.id, self.group_b.id, 700001, 880,
            supplier_id=11001, supplier_name='Duplicate group placement',
        )
        duplicate.last_price_at = datetime.utcnow() + timedelta(seconds=5)
        db.session.commit()

        payload = build_competitor_comparison(self.seller.id)
        row = payload['items'][0]
        self.assertEqual(row['metrics']['offer_count'], 2)
        nm_ids = [offer['nm_id'] for offer in row['offers']]
        self.assertEqual(nm_ids.count(700001), 1)
        chosen = next(offer for offer in row['offers'] if offer['nm_id'] == 700001)
        self.assertEqual(chosen['effective_price'], 880)

    def test_api_is_read_only_tenant_scoped_and_validates_filters(self):
        with self._as_user(), patch(
            'services.competitor_matching._run_llm',
        ) as llm, patch(
            'routes.competitors.CompetitorFetchService',
        ) as wb:
            response = self.client.get('/api/competitors/comparison')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['summary']['offers'], 2)
        llm.assert_not_called()
        wb.assert_not_called()

        with self._as_user():
            foreign = self.client.get(
                f'/api/competitors/comparison?group_id={self.foreign_group.id}',
            )
            bad_group = self.client.get(
                '/api/competitors/comparison?group_id=not-an-int',
            )
            too_large = self.client.get(
                '/api/competitors/comparison?per_page=51',
            )
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(bad_group.status_code, 400)
        self.assertEqual(too_large.status_code, 400)


if __name__ == '__main__':
    unittest.main()
