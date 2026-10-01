"""Single-use, tenant-scoped WB edit preview claims."""

import copy
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch

from flask import Flask
from sqlalchemy.pool import StaticPool
from werkzeug.datastructures import MultiDict


class WBBulkReviewReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        import sqlalchemy as sa
        import seller_platform
        from models import db

        cls.app = seller_platform.app
        cls.app.config.update(
            SECRET_KEY='synthetic-wb-review-test-secret',
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SQLALCHEMY_ENGINE_OPTIONS={},
            WTF_CSRF_ENABLED=False,
            TESTING=True,
        )
        cls.engine = sa.create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        db._app_engines[cls.app] = {None: cls.engine}
        cls.db = db
        cls.seller_platform = seller_platform

    def setUp(self):
        from models import (
            Marketplace,
            MarketplaceCategory,
            MarketplaceCategoryCharacteristic,
            MarketplaceDirectory,
            Product,
            Seller,
            User,
        )

        with self.app.app_context():
            self.db.session.remove()
            self.db.drop_all()
            self.db.create_all()

            user = User(
                username='review-owner',
                email='review-owner@example.test',
                password_hash='synthetic',
            )
            self.db.session.add(user)
            self.db.session.flush()
            seller = Seller(
                user_id=user.id,
                company_name='Synthetic WB seller',
                wb_seller_id='synthetic-wb-account',
            )
            seller.wb_api_key = 'synthetic-provider-key'
            self.db.session.add(seller)
            self.db.session.flush()

            marketplace = Marketplace(
                name='Wildberries',
                code='wb',
                is_active=True,
                categories_sync_status='success',
                categories_synced_at=datetime.utcnow(),
            )
            self.db.session.add(marketplace)
            self.db.session.flush()
            category = MarketplaceCategory(
                marketplace_id=marketplace.id,
                subject_id=5880,
                subject_name='Synthetic exact subject',
                is_enabled=True,
                is_leaf=True,
                is_available=True,
                characteristics_synced_at=datetime.utcnow(),
                characteristics_sync_status='success',
                characteristics_schema_hash='a' * 64,
                characteristics_version=1,
                characteristics_count=4,
            )
            self.db.session.add(category)
            self.db.session.flush()
            self.db.session.add_all([
                MarketplaceCategoryCharacteristic(
                    category_id=category.id,
                    marketplace_id=marketplace.id,
                    charc_id=101,
                    name='Synthetic free-text field',
                    charc_type=1,
                    required=False,
                    max_count=1,
                    dictionary_json='[]',
                    dictionary_source='none',
                    is_enabled=True,
                    is_available=True,
                ),
                MarketplaceCategoryCharacteristic(
                    category_id=category.id,
                    marketplace_id=marketplace.id,
                    charc_id=202,
                    name='Страна производства',
                    charc_type=1,
                    required=False,
                    max_count=1,
                    dictionary_json='[]',
                    dictionary_source='none',
                    is_enabled=True,
                    is_available=True,
                ),
                MarketplaceCategoryCharacteristic(
                    category_id=category.id,
                    marketplace_id=marketplace.id,
                    charc_id=303,
                    name='Вес товара',
                    charc_type=4,
                    unit_name='г',
                    required=False,
                    max_count=1,
                    dictionary_json='[]',
                    dictionary_source='none',
                    is_enabled=True,
                    is_available=True,
                ),
                MarketplaceCategoryCharacteristic(
                    category_id=category.id,
                    marketplace_id=marketplace.id,
                    charc_id=404,
                    name='Материал',
                    charc_type=1,
                    required=False,
                    max_count=3,
                    dictionary_json='[]',
                    dictionary_source='none',
                    is_enabled=True,
                    is_available=True,
                ),
            ])
            self.db.session.add(MarketplaceDirectory(
                marketplace_id=marketplace.id,
                directory_type='countries',
                data_json='["Россия", "Китай"]',
                synced_at=datetime.utcnow(),
                sync_status='success',
                items_count=2,
            ))
            product = Product(
                seller_id=seller.id,
                nm_id=9741,
                vendor_code='SYNTHETIC-9741',
                title='Synthetic WB product',
                brand='Pipedream',
                object_name='Свечи эротик',
                subject_id=5880,
                characteristics_json=json.dumps([
                    {'id': 101, 'name': 'Synthetic free-text field', 'value': ['old']},
                ]),
                sizes_json=json.dumps([
                    {'techSize': 'S', 'skus': ['synthetic-size-sku']},
                ]),
                is_active=True,
            )
            exact_brand_sibling = Product(
                seller_id=seller.id,
                nm_id=9742,
                vendor_code='SYNTHETIC-9742',
                title='Synthetic sibling product',
                brand='Pipedream Classic',
                object_name='Synthetic category',
                subject_id=5880,
                is_active=True,
            )
            foreign_user = User(
                username='review-foreign-owner',
                email='review-foreign-owner@example.test',
                password_hash='synthetic',
            )
            self.db.session.add(foreign_user)
            self.db.session.flush()
            foreign_seller = Seller(
                user_id=foreign_user.id,
                company_name='Foreign synthetic seller',
                wb_seller_id='foreign-wb-account',
            )
            self.db.session.add(foreign_seller)
            self.db.session.flush()
            foreign_product = Product(
                seller_id=foreign_seller.id,
                nm_id=9741,
                vendor_code='FOREIGN-9741',
                title='Foreign Pipedream product',
                brand='Pipedream',
                object_name='Synthetic category',
                subject_id=5880,
                is_active=True,
            )
            self.db.session.add_all([product, exact_brand_sibling, foreign_product])
            self.db.session.commit()
            self.user_id = user.id
            self.seller_id = seller.id
            self.product_id = product.id
            self.sibling_product_id = exact_brand_sibling.id
            self.foreign_product_id = foreign_product.id
            self.nm_id = product.nm_id

    def tearDown(self):
        with self.app.app_context():
            self.db.session.remove()
            self.db.drop_all()

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def _client(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(self.user_id)
            session['_fresh'] = True
        return client

    def _review_submission(self):
        from models import Product
        from services.product_selection import (
            issue_product_selection_token,
            load_product_selection_token,
            parse_product_list_state,
        )
        from services.wb_edit_review import (
            build_bulk_characteristic_preview,
            issue_wb_edit_preview_token,
        )

        with self.app.app_context():
            product = Product.query.filter_by(
                id=self.product_id, seller_id=self.seller_id,
            ).one()
            state = parse_product_list_state({}, strict=True)
            selection_token = issue_product_selection_token(
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                product_ids=[self.product_id],
                state=state,
                return_to='/products?page=1',
                wb_account_id='synthetic-wb-account',
            )
            selection_payload = load_product_selection_token(
                selection_token,
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                wb_account_id='synthetic-wb-account',
            )
            preview = build_bulk_characteristic_preview(
                [product],
                operation='update_characteristic',
                subject_id=5880,
                change_input=[{'char_id': '101', 'value': ['new']}],
            )
            self.assertEqual(preview['changed_count'], 1)
            preview_token = issue_wb_edit_preview_token(
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                selection_payload=selection_payload,
                preview=preview,
            )
            return {
                'selection_token': selection_token,
                'preview_token': preview_token,
                'operation': 'update_characteristic',
                'selected_category': '5880',
                'characteristics_batch': json.dumps([
                    {'char_id': '101', 'value': ['new']},
                ]),
            }

    def _manual_review_submission(self, operation, value, *, product_ids=None):
        from models import Product
        from services.product_selection import (
            issue_product_selection_token,
            load_product_selection_token,
            parse_product_list_state,
        )
        from services.wb_edit_review import (
            build_bulk_manual_preview,
            issue_wb_edit_preview_token,
        )

        submission = self._review_submission()
        submission.pop('preview_token')
        submission.pop('selected_category')
        submission.pop('characteristics_batch')
        submission.update({'operation': operation, 'value': value})
        exact_ids = list(product_ids or [self.product_id])
        with self.app.app_context():
            products_by_id = {
                int(product.id): product
                for product in Product.query.filter(
                    Product.seller_id == self.seller_id,
                    Product.id.in_(exact_ids),
                ).all()
            }
            if set(products_by_id) != set(exact_ids):
                raise AssertionError('test selection fixture is incomplete')
            products = [products_by_id[product_id] for product_id in exact_ids]
            selection_token = issue_product_selection_token(
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                product_ids=exact_ids,
                state=parse_product_list_state({}, strict=True),
                return_to='/products?page=1',
                wb_account_id='synthetic-wb-account',
            )
            submission['selection_token'] = selection_token
            selection = load_product_selection_token(
                selection_token,
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                wb_account_id='synthetic-wb-account',
            )
            preview = build_bulk_manual_preview(
                products, operation=operation, value=value,
            )
            submission['preview_token'] = issue_wb_edit_preview_token(
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                selection_payload=selection,
                preview=preview,
            )
        return submission

    def _characteristic_review_submission(
        self, operation, *, char_id=None, value=None, changes=None,
        product_ids=None,
    ):
        from models import Product
        from services.product_selection import (
            issue_product_selection_token,
            load_product_selection_token,
            parse_product_list_state,
        )
        from services.wb_edit_review import (
            build_bulk_characteristic_preview,
            issue_wb_edit_preview_token,
        )

        exact_ids = list(product_ids or [self.product_id])
        with self.app.app_context():
            products_by_id = {
                int(product.id): product
                for product in Product.query.filter(
                    Product.seller_id == self.seller_id,
                    Product.id.in_(exact_ids),
                ).all()
            }
            products = [products_by_id[product_id] for product_id in exact_ids]
            selection_token = issue_product_selection_token(
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                product_ids=exact_ids,
                state=parse_product_list_state({}, strict=True),
                return_to='/products?page=1',
                wb_account_id='synthetic-wb-account',
            )
            selection_payload = load_product_selection_token(
                selection_token,
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                wb_account_id='synthetic-wb-account',
            )
            preview = build_bulk_characteristic_preview(
                products,
                operation=operation,
                subject_id=5880,
                change_input=changes,
                char_id=char_id,
                value=value,
            )
            token = issue_wb_edit_preview_token(
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                selection_payload=selection_payload,
                preview=preview,
            )
            normalized = preview['normalized_changes']
            return {
                'selection_token': selection_token,
                'preview_token': token,
                'operation': operation,
                'selected_category': '5880',
                'characteristics_batch': json.dumps([
                    {'char_id': str(row['id']), 'value': row['value']}
                    for row in normalized
                ]) if operation == 'update_characteristic' else '',
                'char_id': str(normalized[0]['id']) if operation == 'add_characteristic' else '',
                'value': json.dumps(normalized[0]['value'], ensure_ascii=False)
                if operation == 'add_characteristic' else '',
            }

    def _run_review_with_fake_provider(self, *, ambiguous=False):
        from models import BulkEditHistory

        submission = self._review_submission()
        fake_state = {'reads': 0, 'writes': 0}

        class FakeWBClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def fetch_cards_by_nm_ids(self, nm_ids, **_kwargs):
                fake_state['reads'] += 1
                # The claim must already be committed when the provider boundary
                # is reached. Query it from this request's ORM session.
                claimed = BulkEditHistory.query.filter_by(
                    seller_id=self_owner.seller_id,
                ).filter(BulkEditHistory.review_key.isnot(None)).first()
                assert claimed is not None
                return {
                    nm_id: {
                        'nmID': nm_id,
                        'vendorCode': 'SYNTHETIC-9741',
                        'subjectID': 5880,
                        'title': 'Synthetic WB product',
                        'brand': 'Pipedream',
                        'description': '',
                        'sizes': [{'techSize': '0', 'skus': ['synthetic-barcode']}],
                        'characteristics': [
                            {'id': 101, 'name': 'Synthetic free-text field', 'value': ['old']},
                        ],
                    }
                    for nm_id in nm_ids
                }

            def update_cards_batch(self, cards, **_kwargs):
                fake_state['writes'] += 1
                if ambiguous:
                    raise TimeoutError('synthetic ambiguous provider outcome')
                return {'accepted': len(cards)}

        self_owner = self

        def fake_prepare_batch(products, updates_fn, client, *, fresh_cards_out=None, **_kwargs):
            fresh = client.fetch_cards_by_nm_ids([product.nm_id for product in products])
            cards = []
            product_map = {}
            for product in products:
                full_card = fresh.get(product.nm_id)
                if fresh_cards_out is not None:
                    fresh_cards_out[product.nm_id] = copy.deepcopy(full_card)
                patch = updates_fn(product, full_card)
                if patch is None:
                    continue
                prepared = copy.deepcopy(full_card)
                changes = {
                    int(item['id']): item
                    for item in patch.get('characteristics') or []
                }
                current = {
                    int(item['id']): copy.deepcopy(item)
                    for item in prepared.get('characteristics') or []
                }
                current.update(changes)
                prepared['characteristics'] = list(current.values())
                cards.append(prepared)
                product_map[product.nm_id] = product
            return cards, product_map, []

        with patch.object(
            self.seller_platform, 'WildberriesAPIClient', FakeWBClient,
        ), patch(
            'services.wb_validators.prepare_batch_cards_safe', fake_prepare_batch,
        ):
            client = self._client()
            first = client.post('/products/bulk-edit', data=submission)
            self.assertEqual(first.status_code, 302)
            with self.app.app_context():
                history = BulkEditHistory.query.filter_by(
                    seller_id=self.seller_id,
                ).one()
                self.assertTrue(history.review_key)
                history_id = history.id

            # Re-send precisely the same signed POST body. A successful local
            # change or an ambiguous provider timeout must never trigger another
            # provider read or write.
            replay = client.post('/products/bulk-edit', data=submission)
            self.assertEqual(replay.status_code, 302)
            self.assertEqual(
                replay.headers['Location'],
                f'/bulk-history/{history_id}',
            )
            detail = client.get(replay.headers['Location'])
            self.assertEqual(detail.status_code, 200)

        return fake_state, history_id

    def test_ambiguous_provider_result_does_not_retry_same_signed_post(self):
        fake_state, _history_id = self._run_review_with_fake_provider(ambiguous=True)
        self.assertEqual(fake_state, {'reads': 1, 'writes': 1})

    def test_successful_apply_reload_does_not_retry_same_signed_post(self):
        fake_state, _history_id = self._run_review_with_fake_provider()
        self.assertEqual(fake_state, {'reads': 1, 'writes': 1})

    def test_manual_wb_claim_precedes_provider_and_replay_is_single_use(self):
        from models import BulkEditHistory

        submission = self._manual_review_submission('update_brand', 'Synthetic brand')
        state = {'calls': 0}

        class FakeWBClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def update_cards_merged(self, updates, **_kwargs):
                state['calls'] += 1
                self_owner = owner
                claimed = BulkEditHistory.query.filter_by(
                    seller_id=self_owner.seller_id,
                ).filter(BulkEditHistory.review_key.isnot(None)).first()
                assert claimed is not None
                assert updates == {9741: {'brand': 'Synthetic brand'}}
                return {
                    'sent': [], 'missing': [9741], 'invalid': {}, 'failed': {},
                    'snapshots': {}, 'requests': 1,
                }

        owner = self
        with patch.object(self.seller_platform, 'WildberriesAPIClient', FakeWBClient):
            client = self._client()
            first = client.post('/products/bulk-edit', data=submission)
            self.assertEqual(first.status_code, 302)
            with self.app.app_context():
                history_id = BulkEditHistory.query.filter_by(
                    seller_id=self.seller_id,
                ).one().id
            replay = client.post('/products/bulk-edit', data=submission)
            self.assertEqual(replay.status_code, 302)
            self.assertEqual(replay.headers['Location'], f'/bulk-history/{history_id}')
        self.assertEqual(state['calls'], 1)

    def test_brand_apply_uses_only_changed_review_rows_and_history_counts(self):
        from models import BulkEditHistory, Product

        submission = self._manual_review_submission(
            'update_brand', 'Pipedream',
            product_ids=[self.product_id, self.sibling_product_id],
        )
        state = {'updates': None, 'callback_ids': []}

        class FakeWBClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def update_cards_merged(self, updates, *, pre_merge_callback=None, **_kwargs):
                state['updates'] = updates
                for nm_id, patch_data in updates.items():
                    state['callback_ids'].append(nm_id)
                    pre_merge_callback(
                        nm_id,
                        {'nmID': nm_id, 'subjectID': 5880, 'brand': 'Pipedream Classic'},
                        patch_data,
                    )
                return {
                    'sent': [9742], 'missing': [], 'invalid': {}, 'failed': {},
                    'snapshots': {
                        9742: {
                            'before': {'nmID': 9742, 'brand': 'Pipedream Classic'},
                            'after': {'nmID': 9742, 'brand': 'Pipedream'},
                        },
                    },
                }

        with patch.object(self.seller_platform, 'WildberriesAPIClient', FakeWBClient):
            response = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(state['updates'], {9742: {'brand': 'Pipedream'}})
        self.assertEqual(state['callback_ids'], [9742])
        with self.app.app_context():
            operation = BulkEditHistory.query.filter_by(seller_id=self.seller_id).one()
            self.assertEqual(operation.total_products, 2)
            self.assertEqual(operation.success_count, 1)
            self.assertEqual(operation.operation_params['review_summary'], {
                'selected': 2,
                'eligible': 2,
                'changed': 1,
                'skipped': 1,
                'errors': 0,
                'changed_product_ids': [self.sibling_product_id],
                'mode': 'replace',
                'subject_id': None,
            })
            products = {
                product.id: product
                for product in Product.query.filter(
                    Product.id.in_([self.product_id, self.sibling_product_id]),
                ).all()
            }
            self.assertEqual(products[self.product_id].brand, 'Pipedream')
            self.assertEqual(products[self.sibling_product_id].brand, 'Pipedream')

    def test_brand_provider_drift_skips_row_before_write(self):
        from models import BulkEditHistory, Product

        submission = self._manual_review_submission(
            'update_brand', 'Synthetic brand',
        )
        state = {'writes': 0}

        class FakeWBClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def update_cards_merged(self, updates, *, pre_merge_callback=None, **_kwargs):
                for nm_id, patch_data in updates.items():
                    try:
                        pre_merge_callback(
                            nm_id,
                            {'nmID': nm_id, 'subjectID': 5880, 'brand': 'Provider changed'},
                            patch_data,
                        )
                    except ValueError as exc:
                        return {
                            'sent': [], 'missing': [], 'invalid': {nm_id: str(exc)},
                            'failed': {}, 'snapshots': {},
                        }
                state['writes'] += 1
                return {'sent': list(updates), 'missing': [], 'invalid': {}, 'failed': {}, 'snapshots': {}}

        with patch.object(self.seller_platform, 'WildberriesAPIClient', FakeWBClient):
            response = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(state['writes'], 0)
        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            operation = BulkEditHistory.query.filter_by(seller_id=self.seller_id).one()
            self.assertEqual(product.brand, 'Pipedream')
            self.assertEqual(operation.success_count, 0)
            self.assertEqual(operation.error_count, 1)
            self.assertIn('изменилось в WB после предпросмотра', operation.errors_details[0])

    def test_append_description_provider_drift_does_not_rebuild_unreviewed_after(self):
        from models import BulkEditHistory, Product

        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            product.description = 'Local A'
            self.db.session.commit()
        submission = self._manual_review_submission('append_description', 'Reviewed B')
        state = {'writes': 0, 'fresh_after': []}

        class FakeWBClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def update_cards_batch(self, cards, **_kwargs):
                state['writes'] += 1
                state['fresh_after'].extend(card['description'] for card in cards)

        def stale_prepare(products, updates_fn, _client, *, fresh_cards_out=None, **_kwargs):
            for product in products:
                full_card = {
                    'nmID': product.nm_id,
                    'subjectID': 5880,
                    'sizes': [{'skus': ['synthetic-description-drift']}],
                    'description': 'Provider C',
                }
                if fresh_cards_out is not None:
                    fresh_cards_out[product.nm_id] = copy.deepcopy(full_card)
                with self.assertRaisesRegex(ValueError, 'изменилось в WB'):
                    updates_fn(product, full_card)
            return [], {}, ['Provider description drift; review again']

        with patch.object(self.seller_platform, 'WildberriesAPIClient', FakeWBClient), patch(
            'services.wb_validators.prepare_batch_cards_safe', stale_prepare,
        ):
            response = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(state['writes'], 0)
        self.assertEqual(state['fresh_after'], [])
        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            operation = BulkEditHistory.query.filter_by(seller_id=self.seller_id).one()
            self.assertEqual(product.description, 'Local A')
            self.assertEqual(operation.success_count, 0)
            self.assertEqual(operation.error_count, 1)
            self.assertIn('Provider description drift', operation.errors_details[0])

    def test_local_keyword_apply_has_no_wb_client_and_replay_does_not_repeat(self):
        from models import BulkEditHistory, Product

        submission = self._manual_review_submission('update_keywords', 'synthetic, local')
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('local keyword operation must not create WB client'),
        ):
            client = self._client()
            first = client.post('/products/bulk-edit', data=submission)
            self.assertEqual(first.status_code, 302)
            with self.app.app_context():
                history_id = BulkEditHistory.query.filter_by(
                    seller_id=self.seller_id,
                ).one().id
                product = Product.query.filter_by(id=self.product_id).one()
                self.assertEqual(json.loads(product.tags_json), ['synthetic', 'local'])
            replay = client.post('/products/bulk-edit', data=submission)
            self.assertEqual(replay.status_code, 302)
            self.assertEqual(replay.headers['Location'], f'/bulk-history/{history_id}')
        with self.app.app_context():
            self.assertEqual(BulkEditHistory.query.filter_by(seller_id=self.seller_id).count(), 1)

    def test_keyword_apply_changes_only_products_in_review_diff(self):
        from models import BulkEditHistory, Product

        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            product.tags_json = json.dumps(['one', 'two'])
            self.db.session.commit()
        submission = self._manual_review_submission(
            'update_keywords', 'one, two',
            product_ids=[self.product_id, self.sibling_product_id],
        )
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('keyword review is local-only'),
        ):
            response = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            operation = BulkEditHistory.query.filter_by(seller_id=self.seller_id).one()
            products = {
                product.id: product
                for product in Product.query.filter(
                    Product.id.in_([self.product_id, self.sibling_product_id]),
                ).all()
            }
            self.assertEqual(json.loads(products[self.product_id].tags_json), ['one', 'two'])
            self.assertEqual(json.loads(products[self.sibling_product_id].tags_json), ['one', 'two'])
            self.assertEqual(operation.total_products, 2)
            self.assertEqual(operation.success_count, 1)
            self.assertEqual(operation.operation_params['review_summary']['changed'], 1)
            self.assertEqual(operation.operation_params['review_summary']['skipped'], 1)

    def test_keyword_apply_detects_tags_race_after_claim_without_overwrite(self):
        from models import BulkEditHistory, Product
        from services.wb_edit_review import commit_wb_bulk_review_claim as original_claim

        submission = self._manual_review_submission('update_keywords', 'reviewed tags')

        def claim_then_race(history):
            result = original_claim(history)
            product = Product.query.filter_by(id=self.product_id).one()
            product.tags_json = json.dumps(['concurrent', 'tags'])
            self.db.session.commit()
            return result

        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('keyword operation must remain local-only'),
        ), patch(
            'services.wb_edit_review.commit_wb_bulk_review_claim',
            side_effect=claim_then_race,
        ):
            response = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            operation = BulkEditHistory.query.filter_by(seller_id=self.seller_id).one()
            self.assertEqual(json.loads(product.tags_json), ['concurrent', 'tags'])
            self.assertEqual(operation.status, 'failed')
            self.assertIn('изменились', operation.errors_details[0])

    def test_keyword_race_on_later_row_rolls_back_earlier_cas_in_batch(self):
        from models import BulkEditHistory, Product
        from services.wb_edit_review import commit_wb_bulk_review_claim as original_claim

        submission = self._manual_review_submission(
            'update_keywords', 'reviewed tags',
            product_ids=[self.product_id, self.sibling_product_id],
        )

        def claim_then_race_later_product(history):
            result = original_claim(history)
            product = Product.query.filter_by(id=self.sibling_product_id).one()
            product.tags_json = json.dumps(['concurrent', 'sibling'])
            self.db.session.commit()
            return result

        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('keyword CAS batch must be local-only'),
        ), patch(
            'services.wb_edit_review.commit_wb_bulk_review_claim',
            side_effect=claim_then_race_later_product,
        ):
            response = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            first = Product.query.filter_by(id=self.product_id).one()
            sibling = Product.query.filter_by(id=self.sibling_product_id).one()
            operation = BulkEditHistory.query.filter_by(seller_id=self.seller_id).one()
            self.assertIsNone(first.tags_json)
            self.assertEqual(json.loads(sibling.tags_json), ['concurrent', 'sibling'])
            self.assertEqual(operation.status, 'failed')
            self.assertEqual(operation.success_count, 0)
            self.assertEqual(operation.error_count, 1)

    def test_characteristic_preview_is_local_and_shows_exact_scope_and_diff(self):
        submission = self._review_submission()
        submission.pop('preview_token')
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('preview must not construct provider client'),
        ):
            response = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Synthetic WB seller · WB account synthetic-wb-account', html)
        self.assertIn('subjectID 5880', html)
        self.assertIn('Предпросмотр рассчитан по локальному снимку и справочнику. Он не отправляет данные в WB.', html)
        self.assertIn('Выбрано', html)
        self.assertIn('Подходит по subjectID', html)
        self.assertIn('Изменится', html)
        self.assertIn('old', html)
        self.assertIn('new', html)

    def test_characteristic_apply_sends_only_reviewed_product_field_pairs(self):
        from models import BulkEditHistory, Product
        from services.product_selection import (
            issue_product_selection_token,
            load_product_selection_token,
            parse_product_list_state,
        )
        from services.wb_edit_review import (
            build_bulk_characteristic_preview,
            issue_wb_edit_preview_token,
        )

        with self.app.app_context():
            sibling = Product.query.filter_by(id=self.sibling_product_id).one()
            sibling.characteristics_json = json.dumps([
                {'id': 101, 'name': 'Synthetic free-text field', 'value': ['new']},
            ])
            self.db.session.commit()
            products = [
                Product.query.filter_by(id=product_id, seller_id=self.seller_id).one()
                for product_id in [self.product_id, self.sibling_product_id]
            ]
            product_ids = [self.product_id, self.sibling_product_id]
            selection_token = issue_product_selection_token(
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                product_ids=product_ids,
                state=parse_product_list_state({}, strict=True),
                return_to='/products',
                wb_account_id='synthetic-wb-account',
            )
            selection_payload = load_product_selection_token(
                selection_token,
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                wb_account_id='synthetic-wb-account',
            )
            changes = [
                {'char_id': '101', 'value': ['new']},
                {'char_id': '202', 'value': ['Россия']},
            ]
            preview = build_bulk_characteristic_preview(
                products,
                operation='update_characteristic',
                subject_id=5880,
                change_input=changes,
            )
            self.assertEqual(preview['selected_count'], 2)
            self.assertEqual(preview['changed_count'], 2)
            self.assertEqual(preview['diff_count'], 3)
            preview_token = issue_wb_edit_preview_token(
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                selection_payload=selection_payload,
                preview=preview,
            )

        state = {'patches': {}, 'writes': []}

        class FakeWBClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def update_cards_batch(self, cards, **_kwargs):
                state['writes'].extend(copy.deepcopy(cards))
                return {'accepted': len(cards)}

        def fake_prepare(products_arg, updates_fn, client, *, fresh_cards_out=None, **_kwargs):
            cards = []
            product_map = {}
            for product in products_arg:
                chars = (
                    [{'id': 101, 'name': 'Synthetic free-text field', 'value': ['old']}]
                    if product.id == self.product_id else
                    [{'id': 101, 'name': 'Synthetic free-text field', 'value': ['new']}]
                )
                full_card = {
                    'nmID': int(product.nm_id),
                    'vendorCode': product.vendor_code,
                    'subjectID': 5880,
                    'sizes': [{'skus': [f'synthetic-{product.nm_id}']}],
                    'characteristics': copy.deepcopy(chars),
                }
                if fresh_cards_out is not None:
                    fresh_cards_out[int(product.nm_id)] = copy.deepcopy(full_card)
                patch_data = updates_fn(product, full_card)
                state['patches'][int(product.nm_id)] = copy.deepcopy(patch_data['characteristics'])
                by_id = {item['id']: item for item in chars}
                by_id.update({item['id']: item for item in patch_data['characteristics']})
                full_card['characteristics'] = list(by_id.values())
                cards.append(full_card)
                product_map[int(product.nm_id)] = product
            return cards, product_map, []

        submission = {
            'selection_token': selection_token,
            'preview_token': preview_token,
            'operation': 'update_characteristic',
            'selected_category': '5880',
            'characteristics_batch': json.dumps(changes),
        }
        with patch.object(self.seller_platform, 'WildberriesAPIClient', FakeWBClient), patch(
            'services.wb_validators.prepare_batch_cards_safe', fake_prepare,
        ):
            response = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            {nm_id: {row['id'] for row in patch_rows}
             for nm_id, patch_rows in state['patches'].items()},
            {9741: {101, 202}, 9742: {202}},
        )
        with self.app.app_context():
            operation = BulkEditHistory.query.filter_by(seller_id=self.seller_id).one()
            self.assertEqual(operation.success_count, 2)
            self.assertEqual(operation.operation_params['review_summary']['changed'], 2)
            self.assertEqual(operation.operation_params['review_summary']['selected'], 2)

    def test_characteristic_provider_drift_skips_row_before_batch_write(self):
        from models import BulkEditHistory, Product

        submission = self._review_submission()
        state = {'writes': 0}

        class FakeWBClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def update_cards_batch(self, *_args, **_kwargs):
                state['writes'] += 1

        def stale_prepare(products, updates_fn, _client, *, fresh_cards_out=None, **_kwargs):
            for product in products:
                full_card = {
                    'nmID': product.nm_id,
                    'subjectID': 5880,
                    'sizes': [{'skus': ['synthetic-stale']}],
                    'characteristics': [
                        {'id': 101, 'name': 'Synthetic free-text field', 'value': ['provider-newer']},
                    ],
                }
                if fresh_cards_out is not None:
                    fresh_cards_out[product.nm_id] = copy.deepcopy(full_card)
                with self.assertRaisesRegex(ValueError, 'изменилась в WB'):
                    updates_fn(product, full_card)
            return [], {}, ['Provider content drift; refresh preview']

        with patch.object(self.seller_platform, 'WildberriesAPIClient', FakeWBClient), patch(
            'services.wb_validators.prepare_batch_cards_safe', stale_prepare,
        ):
            response = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(state['writes'], 0)
        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            operation = BulkEditHistory.query.filter_by(seller_id=self.seller_id).one()
            self.assertEqual(json.loads(product.characteristics_json)[0]['value'], ['old'])
            self.assertEqual(operation.status, 'failed')
            self.assertEqual(operation.error_count, 1)
            self.assertIn('Provider content drift', operation.errors_details[0])

    def test_fill_missing_apply_rejects_new_provider_value_without_write(self):
        from models import BulkEditHistory, Product

        submission = self._characteristic_review_submission(
            'add_characteristic', char_id='202', value='Россия',
        )
        state = {'writes': 0}

        class FakeWBClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def update_cards_batch(self, *_args, **_kwargs):
                state['writes'] += 1

        def stale_prepare(products, updates_fn, _client, *, fresh_cards_out=None, **_kwargs):
            skipped = []
            for product in products:
                full_card = {
                    'nmID': product.nm_id,
                    'subjectID': 5880,
                    'sizes': [{'skus': ['synthetic-fill-missing']}],
                    'characteristics': [
                        {'id': 202, 'name': 'Страна производства', 'value': ['Китай']},
                    ],
                }
                if fresh_cards_out is not None:
                    fresh_cards_out[product.nm_id] = copy.deepcopy(full_card)
                with self.assertRaisesRegex(ValueError, 'изменилась в WB'):
                    updates_fn(product, full_card)
                skipped.append('Provider content drift; review again')
            return [], {}, skipped

        with patch.object(self.seller_platform, 'WildberriesAPIClient', FakeWBClient), patch(
            'services.wb_validators.prepare_batch_cards_safe', stale_prepare,
        ):
            response = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(state['writes'], 0)
        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            operation = BulkEditHistory.query.filter_by(seller_id=self.seller_id).one()
            current = json.loads(product.characteristics_json)
            self.assertFalse(any(row.get('id') == 202 for row in current))
            self.assertEqual(operation.success_count, 0)
            self.assertGreaterEqual(operation.error_count, 1)

    def test_manual_bulk_operations_preview_locally_before_any_write(self):
        cases = (
            ('update_brand', 'Fixture brand', 'Бренд', 'Pipedream', 'Fixture brand'),
            ('append_description', 'New paragraph', 'Описание', '', 'New paragraph'),
            ('replace_description', 'Replacement', 'Описание', '', 'Replacement'),
            ('update_keywords', 'one, two\nthree', 'Ключевые слова', '', 'one, two, three'),
        )
        for operation, value, label, old, new in cases:
            with self.subTest(operation=operation):
                submission = self._review_submission()
                submission.pop('preview_token')
                submission.pop('selected_category')
                submission.pop('characteristics_batch')
                submission.update({'operation': operation, 'value': value})
                with patch.object(
                    self.seller_platform,
                    'WildberriesAPIClient',
                    side_effect=AssertionError('preview must not construct WB client'),
                ):
                    response = self._client().post('/products/bulk-edit', data=submission)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn('Выбрано', html)
                self.assertIn('Изменится', html)
                self.assertIn(label, html)
                self.assertIn(new, html)
                if old:
                    self.assertIn(old, html)
                if operation == 'update_keywords':
                    self.assertIn('Изменятся только локальные ключевые слова Seller Hub', html)

    def test_manual_bulk_preview_blocks_empty_and_noop_values_without_token(self):
        from models import Product

        submission = self._review_submission()
        submission.pop('preview_token')
        submission.pop('selected_category')
        submission.pop('characteristics_batch')
        submission.update({'operation': 'update_brand', 'value': ''})
        empty = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(empty.status_code, 200)
        empty_html = empty.get_data(as_text=True)
        self.assertIn('Значение операции не должно быть пустым', empty_html)
        self.assertIn('name="preview_token" value=""', empty_html)
        self.assertNotIn('name="preview_token" value=".', empty_html)

        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            product.brand = 'Already there'
            self.db.session.commit()
        submission['value'] = 'Already there'
        noop = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(noop.status_code, 200)
        noop_html = noop.get_data(as_text=True)
        self.assertIn('Нет изменений для применения', noop_html)
        self.assertIn('name="preview_token" value=""', noop_html)

    def test_mixed_manual_and_ai_post_is_rejected_without_hidden_followup(self):
        from models import BulkEditHistory

        submission = self._review_submission()
        submission.pop('preview_token')
        submission.pop('selected_category')
        submission.pop('characteristics_batch')
        submission.update({
            'operation': 'update_brand',
            'value': 'Synthetic brand',
            'ai_operations': ['ai_keywords'],
        })
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('mixed action must not reach WB'),
        ):
            response = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Ручная и AI-операции не объединяются'.encode(), response.data)
        with self.app.app_context():
            self.assertEqual(BulkEditHistory.query.filter_by(seller_id=self.seller_id).count(), 0)

    def test_manual_preview_token_is_bound_to_operation_and_local_content(self):
        from models import Product
        from services.product_selection import (
            issue_product_selection_token,
            load_product_selection_token,
            parse_product_list_state,
        )
        from services.wb_edit_review import (
            build_bulk_manual_preview,
            issue_wb_edit_preview_token,
            load_wb_edit_preview_token,
            validate_preview_against_current,
        )

        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            state = parse_product_list_state({}, strict=True)
            selection_token = issue_product_selection_token(
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                product_ids=[self.product_id],
                state=state,
                return_to='/products',
                wb_account_id='synthetic-wb-account',
            )
            selection = load_product_selection_token(
                selection_token,
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                wb_account_id='synthetic-wb-account',
            )
            preview = build_bulk_manual_preview(
                [product], operation='replace_description', value='reviewed text',
            )
            token = issue_wb_edit_preview_token(
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                selection_payload=selection,
                preview=preview,
            )
            payload = load_wb_edit_preview_token(
                token,
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
            )
            checked = validate_preview_against_current(payload, [product])
            self.assertEqual(checked['changed_count'], 1)
            product.description = 'drifted after preview'
            self.db.session.commit()
            with self.assertRaisesRegex(ValueError, 'изменил|изменились'):
                validate_preview_against_current(payload, [product])

    def test_product_edit_get_uses_cached_schema_and_keeps_sizes_read_only(self):
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('GET must not construct WB client'),
        ):
            response = self._client().get(f'/products/{self.product_id}/edit')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Страна производства', html)
        self.assertIn('Вес товара', html)
        self.assertIn('Материал', html)
        self.assertIn('name="char_202"', html)
        self.assertIn('name="char_303"', html)
        self.assertIn('name="char_404"', html)
        self.assertIn('synthetic-size-sku', html)
        self.assertNotIn('name="sizes_json"', html)
        self.assertNotIn('name="sku"', html)

    def test_product_edit_validates_country_weight_and_multi_value_patch(self):
        from models import CardEditHistory, Product
        from services.wb_edit_review import product_characteristics_form

        with self.app.app_context():
            product = Product.query.filter_by(
                id=self.product_id, seller_id=self.seller_id,
            ).one()
            revision = product_characteristics_form(product)['schema_revision']['revision']

        patch_payload = [
            ('schema_revision', revision),
            ('char_202', 'Россия'),
            ('char_303', '125'),
            ('char_404', 'Хлопок\nЛён'),
        ]
        fake_state = {'writes': 0, 'callbacks': 0}

        class FakeWBClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def update_card(
                self, nm_id, updates, *, snapshot_context, before_send_callback, **_kwargs,
            ):
                assert nm_id == 9741
                changes = {item['id']: item['value'] for item in updates['characteristics']}
                assert changes[202] == ['Россия']
                assert changes[303] == 125
                assert changes[404] == ['Хлопок', 'Лён']
                before = {
                    'nmID': 9741,
                    'subjectID': 5880,
                    'vendorCode': 'SYNTHETIC-9741',
                    'title': 'Synthetic WB product',
                    'brand': 'Pipedream',
                    'description': '',
                    'sizes': [{'techSize': 'S', 'skus': ['synthetic-size-sku']}],
                    'characteristics': [
                        {'id': 101, 'name': 'Synthetic free-text field', 'value': ['old']},
                    ],
                }
                snapshot_context['before'] = copy.deepcopy(before)
                before_send_callback({'before': before})
                fake_state['callbacks'] += 1
                after = copy.deepcopy(before)
                after['characteristics'].extend([
                    {'id': char_id, 'value': value}
                    for char_id, value in changes.items()
                ])
                snapshot_context['after'] = after
                fake_state['writes'] += 1

        with patch.object(self.seller_platform, 'WildberriesAPIClient', FakeWBClient):
            response = self._client().post(
                f'/products/{self.product_id}/edit',
                data=MultiDict(patch_payload),
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(fake_state, {'writes': 1, 'callbacks': 1})
        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            characteristics = json.loads(product.characteristics_json)
            changed = {row['id']: row['value'] for row in characteristics}
            self.assertEqual(changed[202], ['Россия'])
            self.assertEqual(changed[303], 125)
            self.assertEqual(changed[404], ['Хлопок', 'Лён'])
            history = CardEditHistory.query.filter_by(
                product_id=self.product_id, seller_id=self.seller_id,
            ).one()
            self.assertEqual(history.changed_fields, ['characteristics'])

    def test_product_edit_stale_schema_fails_before_provider_boundary(self):
        from models import MarketplaceCategory, MarketplaceCategoryCharacteristic, Product

        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            category = MarketplaceCategory.query.filter_by(subject_id=5880).one()
            category.characteristics_synced_at = datetime.utcnow() - timedelta(hours=49)
            self.db.session.commit()

        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('stale local schema must block provider I/O'),
        ):
            response = self._client().post(
                f'/products/{self.product_id}/edit',
                data=MultiDict([
                    ('schema_revision', 'stale-revision'),
                    ('char_202', 'Россия'),
                ]),
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn('устарела'.encode(), response.data)
        self.assertNotIn(b'name="char_202"', response.data)
        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            self.assertEqual(len(json.loads(product.characteristics_json)), 1)
            self.assertEqual(MarketplaceCategoryCharacteristic.query.count(), 4)

    def test_all_filtered_resolver_uses_exact_filters_and_binds_account(self):
        from services.product_selection import (
            ProductSelectionError,
            load_product_selection_token,
        )

        client = self._client()
        response = client.post('/products/selection/resolve', json={
            'mode': 'all_filtered',
            'filters': {'brand': 'Pipedream'},
            'sort': 'title',
            'order': 'asc',
            'page': 2,
            'per_page': 50,
            'return_to': '/products?brand=Pipedream&page=2&sort=title&order=asc',
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['ids'], [self.product_id])
        self.assertEqual(payload['count'], 1)
        with self.app.app_context():
            selection = load_product_selection_token(
                payload['selection_token'],
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                wb_account_id='synthetic-wb-account',
            )
            self.assertEqual(selection['ids'], [self.product_id])
            self.assertEqual(selection['filters']['brand'], 'Pipedream')
            self.assertEqual(selection['page'], 2)
            with self.assertRaises(ProductSelectionError):
                load_product_selection_token(
                    payload['selection_token'],
                    secret_key=self.app.config['SECRET_KEY'],
                    user_id=self.user_id,
                    seller_id=self.seller_id,
                    wb_account_id='changed-account',
                )

    def test_resolver_rejects_foreign_duplicate_and_filter_drift(self):
        client = self._client()
        base = {
            'mode': 'ids',
            'filters': {},
            'sort': 'updated_at',
            'order': 'desc',
            'page': 1,
            'per_page': 50,
            'return_to': '/products',
        }
        foreign = client.post('/products/selection/resolve', json={
            **base, 'ids': [self.product_id, self.foreign_product_id],
        })
        self.assertEqual(foreign.status_code, 403)
        duplicate = client.post('/products/selection/resolve', json={
            **base, 'ids': [self.product_id, self.product_id],
        })
        self.assertEqual(duplicate.status_code, 400)
        drift = client.post('/products/selection/resolve', json={
            **base, 'ids': [self.product_id], 'filters': {'brand': 'Pipedream Classic'},
        })
        self.assertEqual(drift.status_code, 409)

    def test_all_filtered_over_cap_returns_no_partial_selection(self):
        from models import Product

        with self.app.app_context():
            self.db.session.add_all([
                Product(
                    seller_id=self.seller_id,
                    nm_id=30000 + index,
                    vendor_code=f'CAP-{index}',
                    title=f'Cap product {index}',
                    brand='Pipedream',
                    is_active=True,
                )
                for index in range(200)
            ])
            self.db.session.commit()

        response = self._client().post('/products/selection/resolve', json={
            'mode': 'all_filtered',
            'filters': {'brand': 'Pipedream'},
            'sort': 'updated_at',
            'order': 'desc',
            'page': 1,
            'per_page': 50,
            'return_to': '/products',
        })
        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertEqual(payload['count'], 201)
        self.assertEqual(payload['limit'], 200)
        self.assertNotIn('selection_token', payload)
        self.assertNotIn('ids', payload)

    def test_legacy_category_name_lookup_uses_exact_local_seller_subject(self):
        from urllib.parse import quote

        path = '/api/characteristics/' + quote('Свечи эротик', safe='')
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('name resolver must not call WB'),
        ):
            response = self._client().get(path)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['subject_id'], 5880)
        self.assertEqual(payload['schema_source'], 'local_authoritative_cache')
        self.assertFalse(payload['provider_io'])
        self.assertEqual(payload['count'], 4)

    def test_subject_lookup_requires_a_seller_product_and_returns_local_alias(self):
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('subject lookup must use local cache'),
        ):
            response = self._client().get('/api/products/characteristics/5880')
            missing = self._client().get('/api/products/characteristics/5070')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['schema_source'], 'local_authoritative_cache')
        self.assertFalse(payload['provider_io'])
        self.assertEqual(payload['count'], 4)
        self.assertEqual(payload['data'][0]['charcID'], 101)
        self.assertEqual(payload['data'][2]['unitName'], 'г')
        self.assertEqual(missing.status_code, 404)

    def test_legacy_category_name_lookup_clears_stale_schema_and_rejects_ambiguous_name(self):
        from urllib.parse import quote

        from models import MarketplaceCategory, Product

        path = '/api/characteristics/' + quote('Свечи эротик', safe='')
        with self.app.app_context():
            category = MarketplaceCategory.query.filter_by(subject_id=5880).one()
            category.characteristics_synced_at = datetime.utcnow() - timedelta(hours=49)
            self.db.session.commit()
        stale = self._client().get(path)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()['characteristics'], [])
        self.assertFalse(stale.get_json()['provider_io'])

        with self.app.app_context():
            self.db.session.add(Product(
                seller_id=self.seller_id,
                nm_id=9743,
                vendor_code='AMBIGUOUS-SUBJECT',
                object_name='Свечи эротик',
                subject_id=5070,
                is_active=True,
            ))
            self.db.session.commit()
        ambiguous = self._client().get(path)
        self.assertEqual(ambiguous.status_code, 409)
        self.assertEqual(ambiguous.get_json()['subject_ids'], [5070, 5880])
        self.assertFalse(ambiguous.get_json()['provider_io'])

    def test_multi_category_api_requires_owned_products_and_exact_subject_subset(self):
        response = self._client().post('/api/characteristics/multi-category', json={
            'product_ids': [self.product_id],
            'subject_ids': [5880],
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['schema_source'], 'local_authoritative_cache')
        self.assertFalse(payload['provider_io'])
        self.assertEqual(payload['categories_count'], 1)
        self.assertEqual([group['subject_id'] for group in payload['groups']], [5880])

        foreign_subject = self._client().post('/api/characteristics/multi-category', json={
            'product_ids': [self.product_id],
            'subject_ids': [5070],
        })
        self.assertEqual(foreign_subject.status_code, 403)


def test_concurrent_review_claim_is_single_use(tmp_path):
    from models import BulkEditHistory, Seller, User, db
    from services.wb_edit_review import commit_wb_bulk_review_claim

    database_path = tmp_path / 'review-claim.sqlite'
    app = Flask('review-claim-concurrency')
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f'sqlite:///{database_path}',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={'connect_args': {'timeout': 10}},
    )
    db.init_app(app)
    key = 'b' * 64
    barrier = threading.Barrier(2)

    with app.app_context():
        db.create_all()
        user = User(
            username='claim-concurrency',
            email='claim-concurrency@example.test',
            password_hash='synthetic',
        )
        db.session.add(user)
        db.session.flush()
        seller = Seller(user_id=user.id, company_name='Concurrent synthetic seller')
        db.session.add(seller)
        db.session.commit()
        seller_id = seller.id

    def submit_claim():
        with app.app_context():
            barrier.wait(timeout=5)
            history, created = commit_wb_bulk_review_claim(BulkEditHistory(
                seller_id=seller_id,
                operation_type='update_characteristic',
                status='in_progress',
                review_key=key,
            ))
            result = (history.id, created)
            db.session.remove()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: submit_claim(), range(2)))

    assert len({history_id for history_id, _created in outcomes}) == 1
    assert sum(1 for _history_id, created in outcomes if created) == 1

    with app.app_context():
        assert BulkEditHistory.query.filter_by(review_key=key).count() == 1
        db.session.remove()
        db.drop_all()
