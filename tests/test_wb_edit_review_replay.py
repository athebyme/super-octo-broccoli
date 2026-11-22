"""Single-use, tenant-scoped WB edit preview claims."""

import copy
import json
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit
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

    def _review_submission(
        self, *, return_to='/products?page=1', workspace_source_ids=None,
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

        with self.app.app_context():
            exact_product_ids = product_ids or [self.product_id]
            by_id = {
                int(product.id): product
                for product in Product.query.filter(
                    Product.id.in_(exact_product_ids),
                    Product.seller_id == self.seller_id,
                ).all()
            }
            products = [by_id[int(product_id)] for product_id in exact_product_ids]
            product = products[0]
            state = parse_product_list_state({}, strict=True)
            selection_token = issue_product_selection_token(
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                product_ids=exact_product_ids,
                state=state,
                return_to=return_to,
                wb_account_id='synthetic-wb-account',
                workspace_source_ids=workspace_source_ids,
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
                operation='update_characteristic',
                subject_id=product.subject_id,
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

    def _manual_review_submission(
        self, operation, value, *, product_ids=None, return_to='/products?page=1',
        workspace_source_ids=None,
    ):
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
                return_to=return_to,
                wb_account_id='synthetic-wb-account',
                workspace_source_ids=workspace_source_ids,
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

    def _review_exclusion_submission(self, base_submission, excluded_product_id, **extra):
        return {
            'csrf_token': 'synthetic-csrf',
            'selection_token': base_submission['selection_token'],
            'review_exclude': '1',
            'base_preview_token': base_submission['preview_token'],
            'excluded_product_id': str(excluded_product_id),
            **extra,
        }

    @staticmethod
    def _submitted_review_tokens(html):
        class FormParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.forms = []
                self.current = None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'form':
                    self.current = {}
                elif tag == 'input' and self.current is not None:
                    name = attrs.get('name')
                    if name:
                        self.current.setdefault(name, []).append(attrs.get('value', ''))

            def handle_endtag(self, tag):
                if tag == 'form' and self.current is not None:
                    self.forms.append(self.current)
                    self.current = None

        parser = FormParser()
        parser.feed(html)
        for form in parser.forms:
            preview_tokens = form.get('preview_token') or []
            if preview_tokens and preview_tokens[-1]:
                return form.get('selection_token', [''])[-1], preview_tokens[-1]
        raise AssertionError('fresh apply form tokens were not rendered')

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

    def _run_review_with_fake_provider(
        self, *, ambiguous=False, submission=None, expected_workspace_return=None,
    ):
        from models import BulkEditHistory

        submission = submission or self._review_submission()
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
            if expected_workspace_return is not None:
                self.assertEqual(first.headers['Location'], expected_workspace_return)
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
            replay_url = urlsplit(replay.headers['Location'])
            self.assertEqual(replay_url.path, f'/bulk-history/{history_id}')
            replay_query = parse_qs(replay_url.query)
            if expected_workspace_return is None:
                self.assertNotIn('workspace_return', replay_query)
            else:
                self.assertEqual(
                    replay_query.get('workspace_return'),
                    [expected_workspace_return],
                )
            detail = client.get(replay.headers['Location'])
            self.assertEqual(detail.status_code, 200)
            detail_html = detail.get_data(as_text=True)
            if expected_workspace_return is not None:
                self.assertIn('Вернуться к моим товарам', detail_html)
                self.assertIn(f'href="{expected_workspace_return}"', detail_html)
                invalid_hint = client.get(
                    f'/bulk-history/{history_id}?workspace_return='
                    'https%3A%2F%2Fevil.example%2Fmy-products%2Fbeta'
                )
                self.assertEqual(invalid_hint.status_code, 200)
                invalid_html = invalid_hint.get_data(as_text=True)
                self.assertIn('Назад к истории операций', invalid_html)
                self.assertNotIn('Вернуться к моим товарам', invalid_html)

        return fake_state, history_id

    def test_ambiguous_provider_result_does_not_retry_same_signed_post(self):
        fake_state, _history_id = self._run_review_with_fake_provider(ambiguous=True)
        self.assertEqual(fake_state, {'reads': 1, 'writes': 1})

    def test_successful_apply_reload_does_not_retry_same_signed_post(self):
        fake_state, _history_id = self._run_review_with_fake_provider()
        self.assertEqual(fake_state, {'reads': 1, 'writes': 1})

    def test_workspace_replay_keeps_history_result_and_safe_return_without_rewriting(self):
        from models import ImportedProduct

        workspace_return = '/my-products/beta#workspace-return=' + 'd' * 32
        with self.app.app_context():
            source = ImportedProduct(
                seller_id=self.seller_id,
                product_id=self.product_id,
                wb_nm_id=self.nm_id,
                external_id='workspace-replay-source',
            )
            self.db.session.add(source)
            self.db.session.commit()
            source_id = source.id

        submission = self._review_submission(
            return_to=workspace_return,
            workspace_source_ids=[source_id],
        )
        fake_state, _history_id = self._run_review_with_fake_provider(
            submission=submission,
            expected_workspace_return=workspace_return,
        )
        self.assertEqual(fake_state, {'reads': 1, 'writes': 1})

    def test_excluding_one_changed_workspace_card_issues_exact_fresh_review_without_writes(self):
        from models import BulkEditHistory, ImportedProduct, Product
        from services.product_selection import load_product_selection_token
        from services.wb_edit_review import (
            load_wb_edit_preview_token,
            validate_preview_against_current,
        )
        from sqlalchemy import event

        return_to = (
            '/my-products/beta?import_batch=a.b.c#workspace-return='
            + ('d' * 32)
        )
        with self.app.app_context():
            sources = [
                ImportedProduct(
                    seller_id=self.seller_id,
                    product_id=self.sibling_product_id,
                    wb_nm_id=9742,
                    external_id='workspace-exclusion-sibling',
                ),
                ImportedProduct(
                    seller_id=self.seller_id,
                    product_id=self.product_id,
                    wb_nm_id=9741,
                    external_id='workspace-exclusion-product',
                ),
            ]
            self.db.session.add_all(sources)
            self.db.session.commit()
            source_ids = [row.id for row in sources]

        original_order = [self.sibling_product_id, self.product_id]
        base = self._manual_review_submission(
            'update_brand', 'Narrowed review brand',
            product_ids=original_order,
            return_to=return_to,
            workspace_source_ids=source_ids,
        )
        writes = []

        def capture_sql(_conn, _cursor, statement, _parameters, _context, _many):
            stripped = statement.lstrip()
            if not stripped:
                return
            first_word = stripped.split(None, 1)[0].upper()
            if first_word in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                writes.append(first_word)

        event.listen(self.engine, 'before_cursor_execute', capture_sql)
        try:
            with patch.object(
                self.seller_platform,
                'WildberriesAPIClient',
                side_effect=AssertionError('review narrowing must not create a WB client'),
            ):
                response = self._client().post(
                    '/products/bulk-edit',
                    data=self._review_exclusion_submission(base, self.sibling_product_id),
                )
        finally:
            event.remove(self.engine, 'before_cursor_execute', capture_sql)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(writes, [])
        html = response.get_data(as_text=True)
        new_selection_token, new_preview_token = self._submitted_review_tokens(html)
        self.assertNotEqual(new_selection_token, base['selection_token'])
        self.assertNotEqual(new_preview_token, base['preview_token'])

        with self.app.app_context():
            selection = load_product_selection_token(
                new_selection_token,
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                wb_account_id='synthetic-wb-account',
            )
            review = load_wb_edit_preview_token(
                new_preview_token,
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
            )
            product = Product.query.filter_by(id=self.product_id).one()
            sibling = Product.query.filter_by(id=self.sibling_product_id).one()
            self.assertEqual(
                validate_preview_against_current(review, [product])['changed_count'],
                1,
            )
            # The original stateless review is not revoked by narrowing.
            original = load_wb_edit_preview_token(
                base['preview_token'],
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
            )
            self.assertEqual(
                validate_preview_against_current(original, [sibling, product])['changed_count'],
                2,
            )
            self.assertEqual(BulkEditHistory.query.filter_by(seller_id=self.seller_id).count(), 0)
            self.assertEqual(product.brand, 'Pipedream')
            self.assertEqual(sibling.brand, 'Pipedream Classic')

        self.assertEqual(selection['ids'], [self.product_id])
        self.assertEqual(selection['workspace_source_ids'], [source_ids[1]])
        self.assertEqual(selection['return_to'], return_to)
        self.assertEqual(review['product_ids'], [self.product_id])
        self.assertEqual(review['operation'], 'update_brand')
        self.assertEqual(review['normalized_changes'], [{
            'field': 'brand', 'value': 'Narrowed review brand',
        }])

    def test_excluded_review_token_flows_through_existing_exact_apply(self):
        from models import BulkEditHistory, Product
        from services.product_selection import load_product_selection_token
        from services.wb_edit_review import load_wb_edit_preview_token

        base = self._manual_review_submission(
            'update_brand', 'New exact brand',
            product_ids=[self.sibling_product_id, self.product_id],
        )
        response = self._client().post(
            '/products/bulk-edit',
            data=self._review_exclusion_submission(base, self.sibling_product_id),
        )
        self.assertEqual(response.status_code, 200)
        selection_token, preview_token = self._submitted_review_tokens(
            response.get_data(as_text=True),
        )
        with self.app.app_context():
            selection = load_product_selection_token(
                selection_token,
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                wb_account_id='synthetic-wb-account',
            )
            review = load_wb_edit_preview_token(
                preview_token,
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
            )
        self.assertEqual(selection['ids'], [self.product_id])
        self.assertEqual(review['product_ids'], [self.product_id])

        writes = {'updates': None}

        class FakeWBClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def update_cards_merged(self, updates, *, pre_merge_callback=None, **_kwargs):
                writes['updates'] = updates
                for nm_id, patch_data in updates.items():
                    pre_merge_callback(
                        nm_id,
                        {'nmID': nm_id, 'brand': 'Pipedream'},
                        patch_data,
                    )
                return {
                    'sent': [9741], 'missing': [], 'invalid': {}, 'failed': {},
                    'snapshots': {
                        9741: {
                            'before': {'nmID': 9741, 'brand': 'Pipedream'},
                            'after': {'nmID': 9741, 'brand': 'New exact brand'},
                        },
                    },
                }

        with patch.object(self.seller_platform, 'WildberriesAPIClient', FakeWBClient):
            applied = self._client().post('/products/bulk-edit', data={
                'csrf_token': 'synthetic-csrf',
                'selection_token': selection_token,
                'preview_token': preview_token,
                'operation': 'update_brand',
                'value': 'New exact brand',
            })
        self.assertEqual(applied.status_code, 302)
        self.assertEqual(writes['updates'], {9741: {'brand': 'New exact brand'}})
        with self.app.app_context():
            operation = BulkEditHistory.query.filter_by(seller_id=self.seller_id).one()
            product = Product.query.filter_by(id=self.product_id).one()
            sibling = Product.query.filter_by(id=self.sibling_product_id).one()
            self.assertEqual(operation.total_products, 1)
            self.assertEqual(operation.operation_params['review_summary']['selected'], 1)
            self.assertEqual(operation.operation_params['review_summary']['changed_product_ids'], [self.product_id])
            self.assertEqual(product.brand, 'New exact brand')
            self.assertEqual(sibling.brand, 'Pipedream Classic')

    def test_review_exclusion_rejects_tampering_foreign_duplicates_and_mixed_modes(self):
        from models import Product
        from services.product_selection import load_product_selection_token
        from services.wb_edit_review import (
            build_bulk_manual_preview,
            issue_wb_edit_preview_token,
        )
        from werkzeug.datastructures import MultiDict

        base = self._manual_review_submission(
            'update_brand', 'New exact brand',
            product_ids=[self.sibling_product_id, self.product_id],
        )
        tampered_token = base['preview_token'][:-1] + (
            'a' if base['preview_token'][-1] != 'a' else 'b'
        )
        with self.app.app_context():
            selection = load_product_selection_token(
                base['selection_token'],
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                wb_account_id='synthetic-wb-account',
            )
            products_by_id = {
                int(product.id): product
                for product in Product.query.filter(
                    Product.seller_id == self.seller_id,
                    Product.id.in_([self.sibling_product_id, self.product_id]),
                ).all()
            }
            preview = build_bulk_manual_preview(
                [products_by_id[product_id] for product_id in selection['ids']],
                operation='update_brand',
                value='New exact brand',
            )
            from services.wb_edit_review import issue_wb_edit_preview_token as issue_preview

            with patch('itsdangerous.timed.TimestampSigner.get_timestamp',
                       return_value=int(time.time()) - 901):
                expired_token = issue_preview(
                    secret_key=self.app.config['SECRET_KEY'],
                    user_id=self.user_id,
                    seller_id=self.seller_id,
                    selection_payload=selection,
                    preview=preview,
                )
        cases = [
            ('empty exclusion', self._review_exclusion_submission(base, '')),
            ('foreign product', self._review_exclusion_submission(base, self.foreign_product_id)),
            ('tampered review', self._review_exclusion_submission(
                {**base, 'preview_token': tampered_token}, self.sibling_product_id,
            )),
            ('expired review', self._review_exclusion_submission(
                {**base, 'preview_token': expired_token}, self.sibling_product_id,
            )),
            ('operation smuggling', self._review_exclusion_submission(
                base, self.sibling_product_id, operation='update_brand', value='attacker',
            )),
            ('back mode smuggling', self._review_exclusion_submission(
                base, self.sibling_product_id, back_to_editor='1',
            )),
            ('apply token smuggling', self._review_exclusion_submission(
                base, self.sibling_product_id, preview_token=base['preview_token'],
            )),
        ]
        duplicate_data = MultiDict([
            ('csrf_token', 'synthetic-csrf'),
            ('selection_token', base['selection_token']),
            ('review_exclude', '1'),
            ('base_preview_token', base['preview_token']),
            ('excluded_product_id', str(self.sibling_product_id)),
            ('excluded_product_id', str(self.product_id)),
        ])
        cases.append(('duplicate excluded IDs', duplicate_data))

        for label, data in cases:
            with self.subTest(case=label), patch.object(
                self.seller_platform,
                'WildberriesAPIClient',
                side_effect=AssertionError('invalid exclusion must not create WB client'),
            ):
                response = self._client().post('/products/bulk-edit', data=data)
                self.assertEqual(response.status_code, 200)
                with self.assertRaises(AssertionError):
                    self._submitted_review_tokens(response.get_data(as_text=True))

        from models import BulkEditHistory
        with self.app.app_context():
            self.assertEqual(BulkEditHistory.query.filter_by(seller_id=self.seller_id).count(), 0)

    def test_review_exclusion_fails_closed_on_local_drift_claim_and_empty_remainder(self):
        from models import BulkEditHistory, ImportedProduct, Product
        from services.wb_edit_review import load_wb_edit_preview_token

        base = self._manual_review_submission(
            'update_brand', 'New exact brand',
            product_ids=[self.sibling_product_id, self.product_id],
        )
        with self.app.app_context():
            payload = load_wb_edit_preview_token(
                base['preview_token'],
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
            )
            self.db.session.add(BulkEditHistory(
                seller_id=self.seller_id,
                operation_type='update_brand',
                review_key=payload['review_key'],
                status='in_progress',
                total_products=2,
            ))
            self.db.session.commit()
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('claimed review must not create WB client'),
        ):
            claimed = self._client().post(
                '/products/bulk-edit',
                data=self._review_exclusion_submission(base, self.sibling_product_id),
            )
        self.assertEqual(claimed.status_code, 200)
        with self.assertRaises(AssertionError):
            self._submitted_review_tokens(claimed.get_data(as_text=True))

        drift_base = self._manual_review_submission(
            'update_brand', 'Another target',
            product_ids=[self.sibling_product_id, self.product_id],
        )
        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            product.brand = 'Concurrent value'
            self.db.session.commit()
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('drifted review must not create WB client'),
        ):
            drifted = self._client().post(
                '/products/bulk-edit',
                data=self._review_exclusion_submission(drift_base, self.sibling_product_id),
            )
        self.assertEqual(drifted.status_code, 200)
        with self.assertRaises(AssertionError):
            self._submitted_review_tokens(drifted.get_data(as_text=True))

        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            product.brand = 'Narrow target'
            self.db.session.commit()
        one_change = self._manual_review_submission(
            'update_brand', 'Narrow target',
            product_ids=[self.sibling_product_id, self.product_id],
        )
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('empty remainder must not create WB client'),
        ):
            empty_remainder = self._client().post(
                '/products/bulk-edit',
                data=self._review_exclusion_submission(one_change, self.sibling_product_id),
            )
        self.assertEqual(empty_remainder.status_code, 200)
        self.assertIn('После исключения не останется карточек'.encode(), empty_remainder.data)
        with self.assertRaises(AssertionError):
            self._submitted_review_tokens(empty_remainder.get_data(as_text=True))

        with self.app.app_context():
            sources = [
                ImportedProduct(
                    seller_id=self.seller_id,
                    product_id=self.sibling_product_id,
                    wb_nm_id=9742,
                    external_id='workspace-exclusion-drift-sibling',
                ),
                ImportedProduct(
                    seller_id=self.seller_id,
                    product_id=self.product_id,
                    wb_nm_id=9741,
                    external_id='workspace-exclusion-drift-product',
                ),
            ]
            self.db.session.add_all(sources)
            self.db.session.commit()
            source_ids = [row.id for row in sources]
        workspace_return = '/my-products/beta#workspace-return=' + ('e' * 32)
        workspace_base = self._manual_review_submission(
            'update_brand', 'Workspace target',
            product_ids=[self.sibling_product_id, self.product_id],
            return_to=workspace_return,
            workspace_source_ids=source_ids,
        )
        with self.app.app_context():
            source = ImportedProduct.query.filter_by(id=source_ids[0]).one()
            source.wb_nm_id = 999999
            self.db.session.commit()
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('workspace mapping drift must not create WB client'),
        ):
            mapping_drift = self._client().post(
                '/products/bulk-edit',
                data=self._review_exclusion_submission(
                    workspace_base, self.sibling_product_id,
                ),
            )
        self.assertEqual(mapping_drift.status_code, 302)
        self.assertEqual(mapping_drift.headers['Location'], workspace_return)


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

    def test_successful_keyword_apply_does_not_log_raw_posted_form_values(self):
        operation_value = 'RAW_OPERATION_VALUE_SENTINEL'
        submission = self._manual_review_submission(
            'update_keywords', operation_value,
        )
        submission.update({
            'csrf_token': 'RAW_CSRF_SENTINEL',
            'unknown_form_key_sentinel': 'RAW_UNKNOWN_FIELD_SENTINEL',
            'return_to': 'https://untrusted.example/RAW_RETURN_TO_SENTINEL',
        })

        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('keyword operation is local-only'),
        ), self.assertLogs(self.seller_platform.app.logger.name, level='INFO') as captured:
            response = self._client().post('/products/bulk-edit', data=submission)

        self.assertEqual(response.status_code, 302)
        log_text = '\n'.join(captured.output)
        self.assertIn('operation=update_keywords', log_text)
        self.assertIn('selected=1', log_text)
        self.assertIn(f'value_length={len(operation_value)}', log_text)
        for raw_value in (
            'RAW_OPERATION_VALUE_SENTINEL',
            'RAW_CSRF_SENTINEL',
            'unknown_form_key_sentinel',
            'RAW_UNKNOWN_FIELD_SENTINEL',
            'RAW_RETURN_TO_SENTINEL',
            submission['selection_token'],
            submission['preview_token'],
        ):
            self.assertNotIn(raw_value, log_text)

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
        self.assertIn('Маркетплейс: Wildberries', html)
        self.assertIn('Категория: Synthetic exact subject', html)
        self.assertNotIn('subjectID 5880', html)
        self.assertIn('Предпросмотр рассчитан по локальным данным и справочнику Wildberries. Он не отправляет изменения в Wildberries.', html)
        self.assertIn('Выбрано', html)
        self.assertIn('Подходит для операции', html)
        self.assertIn('Для этой категории подходят 1 из 1 выбранных карточек.', html)
        self.assertIn('Изменится', html)
        self.assertIn('old', html)
        self.assertIn('new', html)

    def test_mixed_category_preview_states_exact_eligible_scope_in_plain_language(self):
        from models import Product

        with self.app.app_context():
            sibling = Product.query.filter_by(
                id=self.sibling_product_id, seller_id=self.seller_id,
            ).one()
            sibling.subject_id = 5070
            sibling.object_name = 'Synthetic other category'
            self.db.session.commit()

        submission = self._review_submission(
            product_ids=[self.product_id, self.sibling_product_id],
        )
        submission.pop('preview_token')
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('mixed-category preview must remain local'),
        ):
            response = self._client().post('/products/bulk-edit', data=submission)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Для этой категории подходят 1 из 2 выбранных карточек.', html)
        self.assertIn('Карточка относится к другой категории Wildberries', html)
        self.assertIn('Пропущено', html)
        self.assertNotIn('subjectID 5880', html)

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
        schema_marker = '<script type="application/json" id="wb-characteristic-schema">'
        self.assertIn(schema_marker, html)
        schema_json = html.split(schema_marker, 1)[1].split('</script>', 1)[0]
        schema = json.loads(schema_json)
        fields = {row['id']: row for row in schema}
        self.assertEqual(set(fields), {101, 202, 303, 404})

        # The saved characteristic is rendered as an editable server-side
        # control. Empty optional fields stay in the cached schema and enter
        # the client-side picker; they are not fabricated as empty SSR inputs.
        self.assertTrue(fields[101]['present'])
        self.assertEqual(fields[101]['current_values'], ['old'])
        self.assertIn('name="char_101"', html)
        self.assertIn('id="wb-optional-characteristic-picker"', html)
        self.assertIn('availableOptionalCharacteristics()', html)
        self.assertNotIn('name="char_202"', html)
        self.assertNotIn('name="char_303"', html)
        self.assertNotIn('name="char_404"', html)

        country = fields[202]
        self.assertEqual(country['name'], 'Страна производства')
        self.assertTrue(country['editable'])
        self.assertFalse(country['required'])
        self.assertEqual(country['dictionary_source'], 'countries')
        self.assertEqual(country['dictionary_values'], ['Россия', 'Китай'])

        weight = fields[303]
        self.assertEqual(weight['name'], 'Вес товара')
        self.assertEqual(weight['charc_type'], 4)
        self.assertEqual(weight['unit_name'], 'г')
        self.assertTrue(weight['editable'])

        material = fields[404]
        self.assertEqual(material['name'], 'Материал')
        self.assertEqual(material['max_count'], 3)
        self.assertTrue(material['editable'])
        self.assertIn('synthetic-size-sku', html)
        self.assertNotIn('name="sizes_json"', html)
        self.assertNotIn('name="sku"', html)

    def test_product_edit_get_renders_nullable_core_fields_as_blank(self):
        with self.app.app_context():
            from models import Product

            product = Product.query.filter_by(
                id=self.product_id, seller_id=self.seller_id,
            ).one()
            product.vendor_code = None
            product.title = None
            product.brand = None
            product.description = None
            self.db.session.commit()

        response = self._client().get(f'/products/{self.product_id}/edit')
        self.assertEqual(response.status_code, 200)

        class CoreFieldParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.input_values = {}
                self.description_parts = []
                self.in_description = False

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == 'input' and attributes.get('id') in {
                    'vendor_code', 'title', 'brand',
                }:
                    self.input_values[attributes['id']] = attributes.get('value', '')
                if tag == 'textarea' and attributes.get('id') == 'description':
                    self.in_description = True

            def handle_data(self, data):
                if self.in_description:
                    self.description_parts.append(data)

            def handle_endtag(self, tag):
                if tag == 'textarea' and self.in_description:
                    self.in_description = False

        parser = CoreFieldParser()
        parser.feed(response.get_data(as_text=True))
        self.assertEqual(
            parser.input_values,
            {'vendor_code': '', 'title': '', 'brand': ''},
        )
        self.assertEqual(''.join(parser.description_parts).strip(), '')

    def test_product_edit_validates_typed_patch_and_preserves_untouched_core_values(self):
        from models import CardEditHistory, Product
        from services.wb_edit_review import product_characteristics_form

        legacy_values = {
            'vendor_code': '  SYNTHETIC-9741  ',
            'title': '  Synthetic WB product  ',
            'brand': '  Pipedream  ',
            'description': '  Legacy\n description  ',
        }
        with self.app.app_context():
            product = Product.query.filter_by(
                id=self.product_id, seller_id=self.seller_id,
            ).one()
            for field, value in legacy_values.items():
                setattr(product, field, value)
            revision = product_characteristics_form(product)['schema_revision']['revision']
            self.db.session.commit()

        patch_payload = [
            ('vendor_code', legacy_values['vendor_code']),
            ('title', legacy_values['title']),
            ('brand', legacy_values['brand']),
            ('description', '  Legacy\r\n description  '),
            ('schema_revision', revision),
            ('char_202', 'Россия'),
            ('char_303', '125'),
            ('char_404', 'Хлопок\nЛён'),
        ]
        fake_state = {
            'writes': 0,
            'callbacks': 0,
            'updates': [],
            'card': {
                'nmID': 9741,
                'subjectID': 5880,
                'vendorCode': legacy_values['vendor_code'],
                'title': legacy_values['title'],
                'brand': legacy_values['brand'],
                'description': legacy_values['description'],
                'sizes': [{'techSize': 'S', 'skus': ['synthetic-size-sku']}],
                'characteristics': [
                    {'id': 101, 'name': 'Synthetic free-text field', 'value': ['old']},
                ],
            },
        }

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
                fake_state['updates'].append(copy.deepcopy(updates))
                before = copy.deepcopy(fake_state['card'])
                snapshot_context['before'] = copy.deepcopy(before)
                if before_send_callback is not None:
                    before_send_callback({'before': before})
                    fake_state['callbacks'] += 1
                after = copy.deepcopy(before)
                for key in ('vendorCode', 'title', 'brand', 'description'):
                    if key in updates:
                        after[key] = updates[key]
                if 'characteristics' in updates:
                    changes = {
                        item['id']: item['value']
                        for item in updates['characteristics']
                    }
                    assert changes[202] == ['Россия']
                    assert changes[303] == 125
                    assert changes[404] == ['Хлопок', 'Лён']
                    after['characteristics'].extend([
                        {'id': char_id, 'value': value}
                        for char_id, value in changes.items()
                    ])
                fake_state['card'] = after
                snapshot_context['after'] = after
                fake_state['writes'] += 1

        with patch.object(self.seller_platform, 'WildberriesAPIClient', FakeWBClient):
            response = self._client().post(
                f'/products/{self.product_id}/edit',
                data=MultiDict(patch_payload),
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(set(fake_state['updates'][0]), {'characteristics'})

            explicit_edit_response = self._client().post(
                f'/products/{self.product_id}/edit',
                data=MultiDict([
                    ('vendor_code', legacy_values['vendor_code']),
                    ('title', 'Explicit nonempty title'),
                    ('brand', legacy_values['brand']),
                    ('description', '  Updated first line\r\nsecond line  '),
                    ('schema_revision', revision),
                ]),
            )
        self.assertEqual(explicit_edit_response.status_code, 302)
        self.assertEqual(
            fake_state['updates'][1],
            {
                'title': 'Explicit nonempty title',
                'description': 'Updated first line\r\nsecond line',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(fake_state['writes'], 2)
        self.assertEqual(fake_state['callbacks'], 1)
        with self.app.app_context():
            product = Product.query.filter_by(id=self.product_id).one()
            self.assertEqual(product.vendor_code, legacy_values['vendor_code'])
            self.assertEqual(product.title, 'Explicit nonempty title')
            self.assertEqual(product.brand, legacy_values['brand'])
            self.assertEqual(product.description, 'Updated first line\r\nsecond line')
            characteristics = json.loads(product.characteristics_json)
            changed = {row['id']: row['value'] for row in characteristics}
            self.assertEqual(changed[202], ['Россия'])
            self.assertEqual(changed[303], 125)
            self.assertEqual(changed[404], ['Хлопок', 'Лён'])
            history = CardEditHistory.query.filter_by(
                product_id=self.product_id, seller_id=self.seller_id,
            ).order_by(CardEditHistory.id.asc()).all()
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0].changed_fields, ['characteristics'])
            self.assertEqual(history[1].changed_fields, ['title', 'description'])

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

    def test_resolver_rejects_unhashable_mode_with_bad_request(self):
        response = self._client().post('/products/selection/resolve', json={
            'mode': [],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['code'], 'invalid_selection')

    def test_workspace_resolver_binds_exact_ordered_imported_and_wb_ids(self):
        from models import ImportedProduct
        from services.product_selection import load_product_selection_token

        return_to = '/my-products/beta#workspace-return=' + 'a' * 32
        with self.app.app_context():
            from models import Seller

            sources = [
                ImportedProduct(
                    seller_id=self.seller_id,
                    product_id=self.product_id,
                    wb_nm_id=9741,
                    external_id='workspace-source-a',
                ),
                ImportedProduct(
                    seller_id=self.seller_id,
                    product_id=self.sibling_product_id,
                    wb_nm_id=9742,
                    external_id='workspace-source-b',
                ),
            ]
            self.db.session.add_all(sources)
            Seller.query.filter_by(id=self.seller_id).one().wb_seller_id = None
            self.db.session.commit()
            source_ids = [row.id for row in sources]

        response = self._client().post('/products/selection/resolve', json={
            'mode': 'workspace',
            'ids': source_ids,
            'wb_ids': [self.product_id, self.sibling_product_id],
            'return_to': return_to,
        })
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result['ids'], [self.product_id, self.sibling_product_id])
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['marketplace'], 'wb')
        with self.app.app_context():
            selection = load_product_selection_token(
                result['selection_token'],
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                wb_account_id=None,
            )
        self.assertEqual(selection['ids'], [self.product_id, self.sibling_product_id])
        self.assertEqual(selection['workspace_source_ids'], source_ids)
        self.assertEqual(selection['return_to'], return_to)

        reversed_mapping = self._client().post('/products/selection/resolve', json={
            'mode': 'workspace',
            'ids': source_ids,
            'wb_ids': [self.sibling_product_id, self.product_id],
            'return_to': return_to,
        })
        self.assertEqual(reversed_mapping.status_code, 409)
        self.assertEqual(
            reversed_mapping.get_json()['code'], 'workspace_mapping_changed',
        )

        unsafe = self._client().post('/products/selection/resolve', json={
            'mode': 'workspace',
            'ids': source_ids,
            'wb_ids': [self.product_id, self.sibling_product_id],
            'return_to': 'https://evil.example/my-products/beta',
        })
        self.assertEqual(unsafe.status_code, 200)
        with self.app.app_context():
            unsafe_selection = load_product_selection_token(
                unsafe.get_json()['selection_token'],
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                wb_account_id=None,
            )
        self.assertEqual(unsafe_selection['return_to'], '/my-products/beta')

    def test_workspace_resolver_rejects_foreign_duplicate_unlinked_and_drifted_links(self):
        from models import ImportedProduct, Product

        with self.app.app_context():
            foreign_product = Product.query.filter_by(id=self.foreign_product_id).one()
            sources = [
                ImportedProduct(
                    seller_id=self.seller_id,
                    product_id=self.product_id,
                    wb_nm_id=9741,
                    external_id='workspace-linked-source',
                ),
                ImportedProduct(
                    seller_id=self.seller_id,
                    product_id=None,
                    wb_nm_id=9741,
                    external_id='workspace-unlinked-source',
                ),
                ImportedProduct(
                    seller_id=foreign_product.seller_id,
                    product_id=self.foreign_product_id,
                    wb_nm_id=9741,
                    external_id='workspace-foreign-source',
                ),
                ImportedProduct(
                    seller_id=self.seller_id,
                    product_id=self.product_id,
                    wb_nm_id=9742,
                    external_id='workspace-drifted-source',
                ),
            ]
            self.db.session.add_all(sources)
            self.db.session.commit()
            linked_id, unlinked_id, foreign_id, drifted_id = [
                row.id for row in sources
            ]

        client = self._client()
        duplicate = client.post('/products/selection/resolve', json={
            'mode': 'workspace',
            'ids': [linked_id, linked_id],
            'wb_ids': [self.product_id, self.product_id],
            'return_to': '/my-products/beta',
        })
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(duplicate.get_json()['code'], 'invalid_workspace_selection')

        mixed_unlinked = client.post('/products/selection/resolve', json={
            'mode': 'workspace',
            'ids': [linked_id, unlinked_id],
            'wb_ids': [self.product_id, self.sibling_product_id],
            'return_to': '/my-products/beta',
        })
        self.assertEqual(mixed_unlinked.status_code, 409)
        self.assertEqual(mixed_unlinked.get_json()['code'], 'workspace_link_unavailable')

        foreign = client.post('/products/selection/resolve', json={
            'mode': 'workspace',
            'ids': [foreign_id],
            'wb_ids': [self.foreign_product_id],
            'return_to': '/my-products/beta',
        })
        self.assertEqual(foreign.status_code, 409)
        self.assertEqual(foreign.get_json()['code'], 'workspace_link_unavailable')

        drifted = client.post('/products/selection/resolve', json={
            'mode': 'workspace',
            'ids': [drifted_id],
            'wb_ids': [self.product_id],
            'return_to': '/my-products/beta',
        })
        self.assertEqual(drifted.status_code, 409)
        self.assertEqual(drifted.get_json()['code'], 'workspace_mapping_changed')

    def test_workspace_resolver_requires_available_wb_account(self):
        from models import ImportedProduct, Seller

        with self.app.app_context():
            source = ImportedProduct(
                seller_id=self.seller_id,
                product_id=self.product_id,
                wb_nm_id=9741,
                external_id='workspace-account-source',
            )
            self.db.session.add(source)
            seller = Seller.query.filter_by(id=self.seller_id).one()
            seller.wb_api_key = None
            self.db.session.commit()
            source_id = source.id

        response = self._client().post('/products/selection/resolve', json={
            'mode': 'workspace',
            'ids': [source_id],
            'wb_ids': [self.product_id],
            'return_to': '/my-products/beta',
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['code'], 'workspace_account_unavailable')

    def test_bulk_edit_revalidates_workspace_links_before_preview_or_write(self):
        from models import CardEditHistory, ImportedProduct
        from services.product_selection import load_product_selection_token

        return_to = '/my-products/beta#workspace-return=' + 'c' * 32
        with self.app.app_context():
            source = ImportedProduct(
                seller_id=self.seller_id,
                product_id=self.product_id,
                wb_nm_id=9741,
                external_id='workspace-drift-source',
            )
            self.db.session.add(source)
            self.db.session.commit()
            source_id = source.id

        resolved = self._client().post('/products/selection/resolve', json={
            'mode': 'workspace',
            'ids': [source_id],
            'wb_ids': [self.product_id],
            'return_to': return_to,
        })
        self.assertEqual(resolved.status_code, 200)
        token = resolved.get_json()['selection_token']
        with self.app.app_context():
            selection = load_product_selection_token(
                token,
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                wb_account_id='synthetic-wb-account',
            )
            source = ImportedProduct.query.filter_by(id=source_id).one()
            source.product_id = self.sibling_product_id
            source.wb_nm_id = 9742
            self.db.session.commit()

        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('stale workspace links must block provider I/O'),
        ):
            response = self._client().post('/products/bulk-edit', data={
                'selection_token': token,
                'operation': 'update_brand',
                'brand': 'Should not preview',
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], return_to)
        self.assertEqual(selection['workspace_source_ids'], [source_id])
        with self.app.app_context():
            self.assertEqual(CardEditHistory.query.count(), 0)

    def test_bulk_filter_drift_returns_only_to_signed_safe_catalog_context(self):
        from models import Product
        from services.product_selection import (
            issue_product_selection_token,
            parse_product_list_state,
            safe_products_return_url,
        )

        return_to = (
            '/products?brand=Pipedream&sort=title&order=asc&page=2&per_page=50'
        )
        state = parse_product_list_state({
            'brand': 'Pipedream',
            'sort': 'title',
            'order': 'asc',
            'page': 2,
            'per_page': 50,
        }, strict=True)
        with self.app.app_context():
            token = issue_product_selection_token(
                secret_key=self.app.config['SECRET_KEY'],
                user_id=self.user_id,
                seller_id=self.seller_id,
                product_ids=[self.product_id],
                state=state,
                return_to=return_to,
                wb_account_id='synthetic-wb-account',
            )
            product = Product.query.filter_by(
                id=self.product_id, seller_id=self.seller_id,
            ).one()
            product.brand = 'Local brand drift'
            self.db.session.commit()

        client = self._client()
        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('filter drift must fail before WB'),
        ):
            response = client.post('/products/bulk-edit', data={
                'selection_token': token,
            })
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, safe_products_return_url(return_to))
            catalog = client.get(response.location)
            self.assertEqual(catalog.status_code, 200)
            self.assertIn(
                'Точный выбор изменился или больше не соответствует фильтрам'.encode(),
                catalog.data,
            )

        # The posted return_to field is untrusted. Without a valid signed
        # selection token, a hostile local/external target falls back to the
        # catalog root.
        forged = self._client().post('/products/bulk-edit', data={
            'selection_token': 'forged.selection.token',
            'return_to': 'https://outside.example/collect',
        })
        self.assertEqual(forged.status_code, 302)
        self.assertEqual(forged.location, '/products')

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

    def test_multi_category_subject_ids_keep_exact_shape_bounds_and_subset_checks(self):
        from services.product_selection import MAX_SIGNED_SQLITE_ID

        with patch.object(
            self.seller_platform,
            'WildberriesAPIClient',
            side_effect=AssertionError('local subject parsing must not call WB'),
        ):
            valid_string = self._client().post('/api/characteristics/multi-category', json={
                'product_ids': [self.product_id],
                'subject_ids': ['5880'],
            })
            self.assertEqual(valid_string.status_code, 200)

            # 5070 and signed-64 max are valid numeric identities, but are not
            # backed by this selected product; they must reach the ownership
            # subset guard, not fail parsing or trigger schema/provider reads.
            for subject_id in ('5070', str(MAX_SIGNED_SQLITE_ID)):
                with self.subTest(subject_id_length=len(subject_id)):
                    foreign = self._client().post('/api/characteristics/multi-category', json={
                        'product_ids': [self.product_id],
                        'subject_ids': [subject_id],
                    })
                    self.assertEqual(foreign.status_code, 403)

            invalid_values = (
                '9' * 5000,
                str(MAX_SIGNED_SQLITE_ID + 1),
                '0', 0, -1, True, 5880.0, '５８８０',
            )
            for subject_id in invalid_values:
                with self.subTest(value_type=type(subject_id).__name__,
                                  size=len(subject_id) if isinstance(subject_id, str) else None):
                    with patch(
                        'services.wb_edit_review.bulk_characteristics_payload',
                        side_effect=AssertionError('invalid subject must stop before schema lookup'),
                    ):
                        rejected = self._client().post('/api/characteristics/multi-category', json={
                            'product_ids': [self.product_id],
                            'subject_ids': [subject_id],
                        })
                    self.assertEqual(rejected.status_code, 400)

            duplicate = self._client().post('/api/characteristics/multi-category', json={
                'product_ids': [self.product_id],
                'subject_ids': [5880, '05880'],
            })
            self.assertEqual(duplicate.status_code, 400)

    def test_bulk_edit_rejects_unrepresentable_subject_before_preview_or_provider(self):
        submission = self._review_submission()
        submission['preview_token'] = ''
        for value in ('9' * 5000, '9223372036854775808', '５８８０'):
            with self.subTest(value_length=len(value)):
                submission['selected_category'] = value
                with patch.object(
                    self.seller_platform,
                    'WildberriesAPIClient',
                    side_effect=AssertionError('invalid subject must not create WB client'),
                ):
                    response = self._client().post('/products/bulk-edit', data=submission)
                self.assertEqual(response.status_code, 200)
                self.assertIn('Выберите точный subjectID WB'.encode(), response.data)


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
