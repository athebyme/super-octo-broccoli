# -*- coding: utf-8 -*-
"""Draft HTTP APIs preserve tenant, strict JSON, flag and CSRF boundaries."""

from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceOperation,
    MarketplaceProductDraft,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_drafts import register_marketplace_draft_routes
from services.marketplace_drafts import MarketplaceDraftService


class MarketplaceDraftRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-draft-routes",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_draft_routes(self.app)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.seller1_id, self.user1_id = self._seller(
                "draft-route-one", "draft-route1@test.local"
            )
            self.seller2_id, self.user2_id = self._seller(
                "draft-route-two", "draft-route2@test.local"
            )
            ozon = Marketplace(
                name="Ozon",
                code="ozon",
                adapter_code="ozon",
                is_active=True,
            )
            db.session.add(ozon)
            db.session.flush()
            account1 = SellerMarketplaceAccount(
                seller_id=self.seller1_id,
                marketplace_id=ozon.id,
                external_account_id="client-one",
                label="Ozon One",
                is_active=True,
                connection_status="connected",
            )
            account2 = SellerMarketplaceAccount(
                seller_id=self.seller2_id,
                marketplace_id=ozon.id,
                external_account_id="client-two",
                label="Ozon Two",
                is_active=True,
                connection_status="connected",
            )
            own_source = ImportedProduct(
                seller_id=self.seller1_id,
                external_id="own-source",
                external_vendor_code="own-offer",
                title="Own draft",
                description="Own description",
                category="Own category",
            )
            foreign_source = ImportedProduct(
                seller_id=self.seller2_id,
                external_id="foreign-source",
                external_vendor_code="foreign-offer",
                title="Foreign draft secret",
                description="Foreign description",
                category="Foreign category",
            )
            db.session.add_all([account1, account2, own_source, foreign_source])
            db.session.flush()
            own = MarketplaceProductDraft(
                seller_id=self.seller1_id,
                marketplace_id=ozon.id,
                account_id=account1.id,
                imported_product_id=own_source.id,
                offer_id="own-offer",
                status="needs_category",
                source_fact_hash="a" * 64,
            )
            foreign = MarketplaceProductDraft(
                seller_id=self.seller2_id,
                marketplace_id=ozon.id,
                account_id=account2.id,
                imported_product_id=foreign_source.id,
                offer_id="foreign-offer",
                status="needs_category",
                source_fact_hash="b" * 64,
            )
            db.session.add_all([own, foreign])
            db.session.commit()
            self.account1_id = account1.id
            self.account2_id = account2.id
            self.own_source_id = own_source.id
            self.foreign_source_id = foreign_source.id
            self.own_id = own.id
            self.foreign_id = foreign.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    @staticmethod
    def _seller(username, email):
        user = User(username=username, email=email, is_active=True)
        user.set_password("synthetic-password")
        seller = Seller(user=user, company_name=username)
        db.session.add(seller)
        db.session.commit()
        return seller.id, user.id

    @staticmethod
    def _user(seller_id=None, user_id=10):
        seller = SimpleNamespace(id=seller_id) if seller_id else None
        return SimpleNamespace(
            id=user_id,
            seller=seller,
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def _auth(self, seller_id, user_id=10):
        user = self._user(seller_id, user_id)
        return (
            patch("routes.marketplace_drafts.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_list_and_detail_are_tenant_scoped(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch:
            listed = self.client.get(
                "/marketplaces/drafts/api",
                headers={"Accept": "application/json"},
            )
            own = self.client.get(
                f"/marketplaces/drafts/{self.own_id}",
                headers={"Accept": "application/json"},
            )
            foreign = self.client.get(
                f"/marketplaces/drafts/{self.foreign_id}",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [item["id"] for item in listed.get_json()["items"]],
            [self.own_id],
        )
        self.assertEqual(own.status_code, 200)
        self.assertEqual(foreign.status_code, 404)
        self.assertNotIn("Foreign draft secret", foreign.get_data(as_text=True))

    def test_vue_editor_and_dictionary_reads_keep_tenant_and_query_scope(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch:
            own = self.client.get(f'/marketplaces/drafts/{self.own_id}/editor')
            foreign = self.client.get(f'/marketplaces/drafts/{self.foreign_id}/editor')
            duplicate = self.client.get(
                f'/marketplaces/drafts/{self.own_id}/dictionary/32?product_type_id=1&q=a&q=b')
            foreign_dictionary = self.client.get(
                f'/marketplaces/drafts/{self.foreign_id}/dictionary/32?product_type_id=1')
            with patch('routes.marketplace_drafts.render_template', return_value='vue') as render:
                html = self.client.get(f'/marketplaces/drafts/{self.own_id}')
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.get_json()['draft']['id'], self.own_id)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign_dictionary.status_code, 404)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(html.status_code, 200)
        self.assertEqual(render.call_args.args[0], 'marketplace_draft_detail.html')

    def test_editor_get_recomputes_legacy_ready_without_persisting_or_network(self):
        with self.app.app_context():
            draft = db.session.get(MarketplaceProductDraft, self.own_id)
            draft.status = 'ready'
            draft.validation_status = 'valid'
            draft.validated_at = datetime(2026, 7, 25, 12, 0, 0)
            draft.validation_result_json = json.dumps({
                'version': 1, 'marketplace': 'ozon', 'publishable': True,
                'errors': [], 'warnings': [], 'validated_at': '2026-07-25T12:00:00',
            })
            draft.dimensions_json = json.dumps({})
            draft.commercial_json = json.dumps({
                'price': '1000', 'currency_code': 'RUB',
            })
            db.session.commit()
            saved = {
                'status': draft.status,
                'validation_status': draft.validation_status,
                'validation_result_json': draft.validation_result_json,
                'validated_at': draft.validated_at,
                'dimensions_json': draft.dimensions_json,
                'commercial_json': draft.commercial_json,
                'version': draft.version,
            }

        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
            with user_patch, login_patch:
                response = self.client.get(f'/marketplaces/drafts/{self.own_id}/editor')

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        document = response.get_json()
        current = document['current_validation']
        codes = {issue['code'] for issue in current['errors']}
        self.assertFalse(current['publishable'])
        self.assertIn('physical_fact_required', codes)
        self.assertIn('vat_required', codes)
        self.assertEqual(document['draft']['status'], 'ready')
        self.assertEqual(document['draft']['validation_status'], 'valid')
        self.assertTrue(document['draft']['validation']['publishable'])
        self.assertEqual(document['operations'], [])

        with self.app.app_context():
            persisted = db.session.get(MarketplaceProductDraft, self.own_id)
            self.assertEqual(persisted.status, saved['status'])
            self.assertEqual(persisted.validation_status, saved['validation_status'])
            self.assertEqual(persisted.validation_result_json, saved['validation_result_json'])
            self.assertEqual(persisted.validated_at, saved['validated_at'])
            self.assertEqual(persisted.dimensions_json, saved['dimensions_json'])
            self.assertEqual(persisted.commercial_json, saved['commercial_json'])
            self.assertEqual(persisted.version, saved['version'])
            self.assertEqual(MarketplaceOperation.query.filter_by(
                seller_id=self.seller1_id,
            ).count(), 0)

    def test_classic_form_returns_to_classic_editor_after_saving(self):
        with self.app.app_context():
            version = db.session.get(MarketplaceProductDraft, self.own_id).version
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch:
            response = self.client.post(f'/marketplaces/drafts/{self.own_id}?view=classic', data={
                'expected_version':str(version), 'content_json':'{"name":"Edited name"}',
            })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith(f'/marketplaces/drafts/classic/{self.own_id}'))

    def test_source_search_reaches_old_rows_and_handles_cyrillic_without_leaks(self):
        with self.app.app_context():
            source = db.session.get(ImportedProduct, self.own_source_id)
            source.title = 'Тестовый Подарок 100%'
            db.session.add_all([ImportedProduct(
                seller_id=self.seller1_id, external_id=f'new-{index}', title='Новый товар',
            ) for index in range(205)])
            db.session.commit()
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch:
            found = self.client.get('/marketplaces/drafts/sources?q=пОдАрОк')
            literal = self.client.get('/marketplaces/drafts/sources?q=%25')
            foreign = self.client.get('/marketplaces/drafts/sources?q=Foreign')
            recent = self.client.get('/marketplaces/drafts/sources')
        self.assertEqual(found.status_code, 200)
        self.assertEqual([item['id'] for item in found.get_json()['items']], [self.own_source_id])
        self.assertEqual([item['id'] for item in literal.get_json()['items']], [self.own_source_id])
        self.assertEqual(foreign.get_json()['items'], [])
        self.assertEqual(len(recent.get_json()['items']), 20)
        self.assertTrue(recent.get_json()['has_more'])
        self.assertEqual(set(found.get_json()['items'][0]), {'id', 'title', 'external_id', 'vendor_code'})

    def test_source_search_rejects_scope_smuggling_duplicate_or_oversized_query(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch:
            for query in ['seller_id=2', 'q=a&q=b', 'q=' + 'a' * 101]:
                response = self.client.get('/marketplaces/drafts/sources?' + query)
                self.assertEqual(response.status_code, 400)
            self.app.config['MARKETPLACE_OZON_ENABLED'] = False
            self.assertEqual(self.client.get('/marketplaces/drafts/sources').status_code, 404)

    def test_foreign_account_and_source_are_denied(self):
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch:
            foreign_account = self.client.post(
                "/marketplaces/drafts/",
                json={
                    "account_id": self.account2_id,
                    "imported_product_id": self.own_source_id,
                    "save_mapping": False,
                },
            )
            foreign_source = self.client.post(
                "/marketplaces/drafts/",
                json={
                    "account_id": self.account1_id,
                    "imported_product_id": self.foreign_source_id,
                    "save_mapping": False,
                },
            )
        self.assertEqual(foreign_account.status_code, 404)
        self.assertEqual(foreign_source.status_code, 404)

    def test_create_json_types_and_unknown_fields_are_strict(self):
        created = SimpleNamespace(
            id=99,
            to_public_dict=lambda detail=False: {"id": 99, "detail": detail},
        )
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService,
            "create_draft",
            return_value=created,
        ) as create:
            loose = self.client.post(
                "/marketplaces/drafts/",
                json={
                    "account_id": str(self.account1_id),
                    "imported_product_id": self.own_source_id,
                    "save_mapping": "false",
                },
            )
            loose_validate = self.client.post(
                "/marketplaces/drafts/",
                json={
                    "account_id": self.account1_id,
                    "imported_product_id": self.own_source_id,
                    "save_mapping": False,
                    "validate": "true",
                },
            )
            unknown = self.client.post(
                "/marketplaces/drafts/",
                json={
                    "account_id": self.account1_id,
                    "imported_product_id": self.own_source_id,
                    "save_mapping": False,
                    "seller_id": self.seller2_id,
                },
            )
            valid = self.client.post(
                "/marketplaces/drafts/",
                json={
                    "account_id": self.account1_id,
                    "imported_product_id": self.own_source_id,
                    "save_mapping": False,
                },
            )
        self.assertEqual(loose.status_code, 400)
        self.assertEqual(loose_validate.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(valid.status_code, 201)
        create.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.account1_id,
            imported_product_id=self.own_source_id,
            product_type_id=None,
            offer_id=None,
            save_mapping=False,
            corrected_by_user_id=self.user1_id,
        )

    def test_existing_listing_create_precondition_is_forwarded_only_for_strict_json(self):
        created = SimpleNamespace(
            id=100,
            to_public_dict=lambda detail=False: {"id": 100, "detail": detail},
        )
        expected = {
            "listing_id": 1,
            "link_version": 1,
            "link_status": "linked",
            "link_source": None,
            "link_evidence_sha256": "a" * 64,
            "linked_at": None,
            "offer_id": "offer-one",
            "external_product_id": "123456",
            "external_category_id": "10",
            "external_type_id": "777",
            "source_fact_hash": "b" * 64,
            "baseline_fingerprint": "c" * 64,
            "baseline_contract_version": "product-full-state@2026-07-25",
        }
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService,
            "create_draft",
            return_value=created,
        ) as create:
            accepted = self.client.post(
                "/marketplaces/drafts/",
                json={
                    "account_id": self.account1_id,
                    "imported_product_id": self.own_source_id,
                    "save_mapping": False,
                    "validate": False,
                    "expected_existing_listing": expected,
                },
            )
            validate = self.client.post(
                "/marketplaces/drafts/",
                json={
                    "account_id": self.account1_id,
                    "imported_product_id": self.own_source_id,
                    "save_mapping": False,
                    "validate": True,
                    "expected_existing_listing": expected,
                },
            )

        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(validate.status_code, 400)
        create.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.account1_id,
            imported_product_id=self.own_source_id,
            product_type_id=None,
            offer_id=None,
            save_mapping=False,
            corrected_by_user_id=self.user1_id,
            expected_existing_listing=expected,
        )

    def test_create_can_run_local_validation_immediately(self):
        created = SimpleNamespace(
            id=99,
            version=4,
            to_public_dict=lambda detail=False: {
                "id": 99,
                "validation": {"publishable": False},
            },
        )
        validated = SimpleNamespace(
            id=99,
            version=5,
            to_public_dict=lambda detail=False: {
                "id": 99,
                "validation": {"publishable": True},
            },
        )
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService,
            "create_draft",
            return_value=created,
        ) as create, patch.object(
            MarketplaceDraftService,
            "validate_draft",
            return_value=validated,
        ) as validate:
            response = self.client.post(
                "/marketplaces/drafts/",
                json={
                    "account_id": self.account1_id,
                    "imported_product_id": self.own_source_id,
                    "save_mapping": False,
                    "validate": True,
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.get_json()["draft"]["validation"]["publishable"])
        create.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.account1_id,
            imported_product_id=self.own_source_id,
            product_type_id=None,
            offer_id=None,
            save_mapping=False,
            corrected_by_user_id=self.user1_id,
        )
        validate.assert_called_once_with(
            seller_id=self.seller1_id,
            draft_id=99,
            expected_version=4,
        )

    def test_update_json_contract_and_authenticated_scope(self):
        updated = SimpleNamespace(
            id=self.own_id,
            to_public_dict=lambda detail=False: {"id": self.own_id},
        )
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService,
            "update_draft",
            return_value=updated,
        ) as update:
            loose = self.client.patch(
                f"/marketplaces/drafts/{self.own_id}",
                json={"expected_version": "1", "patch": {"offer_id": "new"}},
            )
            valid = self.client.patch(
                f"/marketplaces/drafts/{self.own_id}",
                json={"expected_version": 1, "patch": {"offer_id": "new"}},
            )
        self.assertEqual(loose.status_code, 400)
        self.assertEqual(valid.status_code, 200)
        update.assert_called_once_with(
            seller_id=self.seller1_id,
            draft_id=self.own_id,
            expected_version=1,
            patch={"offer_id": "new"},
            corrected_by_user_id=self.user1_id,
            category_review_token=None,
            category_review_required=True,
        )

    def test_feature_flag_and_non_seller_block_writes(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch.object(
            MarketplaceDraftService,
            "validate_draft",
        ) as validate:
            disabled = self.client.post(
                f"/marketplaces/drafts/{self.own_id}/validate",
                json={"expected_version": 1},
            )
        self.assertEqual(disabled.status_code, 404)
        validate.assert_not_called()

        self.app.config["MARKETPLACE_OZON_ENABLED"] = True
        user = self._user()
        with patch("routes.marketplace_drafts.current_user", user), patch(
            "flask_login.utils._get_user", return_value=user
        ), patch.object(MarketplaceDraftService, "validate_draft") as validate:
            denied = self.client.post(
                f"/marketplaces/drafts/{self.own_id}/validate",
                json={"expected_version": 1},
            )
        self.assertEqual(denied.status_code, 403)
        validate.assert_not_called()

    def test_write_route_is_csrf_protected_when_enabled(self):
        self.app.config["WTF_CSRF_ENABLED"] = True
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch:
            response = self.client.post(
                f"/marketplaces/drafts/{self.own_id}/validate",
                data={"expected_version": "1"},
            )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
