import json
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from models import (
    Marketplace,
    MarketplaceCancellation,
    MarketplacePosting, MarketplacePostingItem, MarketplacePostingStatusEvent,
    MarketplaceListing, MarketplaceFulfillmentSync,
    MarketplaceReturn,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.marketplace_fulfillment import register_marketplace_fulfillment_routes
from services.marketplace_fulfillment import MarketplaceFulfillmentService


class MarketplaceFulfillmentRoutesTest(unittest.TestCase):
    def setUp(self):
        observed_at = datetime.utcnow()
        self.app = Flask(__name__, template_folder="../templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-fulfillment-routes",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        LoginManager(self.app)
        CSRFProtect(self.app)
        register_marketplace_fulfillment_routes(self.app)
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.seller1_id, self.user1_id = self._seller("one", "one@test.local")
            self.seller2_id, self.user2_id = self._seller("two", "two@test.local")
            marketplace = Marketplace(
                code="ozon", name="Ozon", adapter_code="ozon", is_active=True,
            )
            db.session.add(marketplace)
            db.session.flush()
            own = SellerMarketplaceAccount(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                external_account_id="own-client",
                label="Own Ozon",
                is_active=True,
                connection_status="connected",
            )
            foreign = SellerMarketplaceAccount(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                external_account_id="foreign-client",
                label="Foreign Secret Label",
                is_active=True,
                connection_status="connected",
            )
            db.session.add_all([own, foreign])
            db.session.flush()
            posting = MarketplacePosting(
                seller_id=self.seller1_id,
                marketplace_id=marketplace.id,
                account_id=own.id,
                posting_number="own-posting",
                fulfillment_kind="fbs",
                status="delivered",
                source_endpoint="/v4/posting/fbs/list",
                sync_fingerprint="a" * 64,
                last_seen_at=observed_at,
            )
            foreign_posting = MarketplacePosting(
                seller_id=self.seller2_id,
                marketplace_id=marketplace.id,
                account_id=foreign.id,
                posting_number="foreign-secret-posting",
                fulfillment_kind="fbo",
                status="delivered",
                source_endpoint="/v3/posting/fbo/list",
                sync_fingerprint="b" * 64,
                last_seen_at=observed_at,
            )
            db.session.add_all([posting, foreign_posting])
            db.session.flush()
            db.session.add_all([
                MarketplaceReturn(
                    seller_id=self.seller1_id,
                    marketplace_id=marketplace.id,
                    account_id=own.id,
                    posting_id=posting.id,
                    source_kind="fbo_fbs",
                    external_return_id="ret-1",
                    posting_number="own-posting",
                    fulfillment_kind="fbs",
                    status="MovingToSeller",
                    quantity=1,
                    source_endpoint="/v1/returns/list",
                    sync_fingerprint="c" * 64,
                    last_seen_at=observed_at,
                ),
                MarketplaceCancellation(
                    seller_id=self.seller1_id,
                    marketplace_id=marketplace.id,
                    account_id=own.id,
                    posting_id=posting.id,
                    source_kind="posting_fbs",
                    external_cancellation_id="cancel-1",
                    posting_number="own-posting",
                    status="cancelled",
                    source_endpoint="/v4/posting/fbs/list",
                    sync_fingerprint="d" * 64,
                    last_seen_at=observed_at,
                ),
            ])
            db.session.commit()
            self.own_account_id = own.id
            self.foreign_account_id = foreign.id
            self.own_posting_id = posting.id

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
    def _user(seller_id, user_id):
        return SimpleNamespace(
            id=user_id,
            seller=SimpleNamespace(id=seller_id),
            is_authenticated=True,
            is_active=True,
            is_admin=False,
        )

    def _auth(self):
        user = self._user(self.seller1_id, self.user1_id)
        return (
            patch("routes.marketplace_fulfillment.current_user", user),
            patch("flask_login.utils._get_user", return_value=user),
        )

    def test_all_read_routes_are_exact_account_scoped(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            orders = self.client.get(
                f"/marketplaces/api/orders?account_id={self.own_account_id}",
            )
            returns = self.client.get(
                f"/marketplaces/api/returns?account_id={self.own_account_id}",
            )
            cancellations = self.client.get(
                f"/marketplaces/api/cancellations?account_id={self.own_account_id}",
            )
            detail = self.client.get(
                f"/marketplaces/api/orders/{self.own_posting_id}"
                f"?account_id={self.own_account_id}",
            )
            foreign = self.client.get(
                f"/marketplaces/api/orders?account_id={self.foreign_account_id}",
            )
        self.assertEqual(orders.status_code, 200)
        self.assertEqual(returns.status_code, 200)
        self.assertEqual(cancellations.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            orders.get_json()["data"]["items"][0]["posting_number"],
            "own-posting",
        )
        self.assertEqual(foreign.status_code, 404)
        encoded = json.dumps(foreign.get_json(), ensure_ascii=False)
        self.assertNotIn("Foreign Secret Label", encoded)
        self.assertNotIn("foreign-secret-posting", encoded)

    def test_scope_smuggling_and_loose_types_fail_before_service(self):
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch.object(
            MarketplaceFulfillmentService,
            "sync_account",
        ) as sync:
            smuggled = self.client.post(
                f"/marketplaces/api/fulfillment/sync?account_id={self.own_account_id}",
                json={
                    "period": "30d",
                    "force": False,
                    "account_id": self.foreign_account_id,
                },
            )
            loose_bool = self.client.post(
                f"/marketplaces/api/fulfillment/sync?account_id={self.own_account_id}",
                json={"period": "30d", "force": "false"},
            )
            loose_pages = self.client.post(
                f"/marketplaces/api/fulfillment/sync?account_id={self.own_account_id}",
                json={"period": "30d", "force": False, "max_pages": 1.5},
            )
        self.assertEqual(smuggled.status_code, 400)
        self.assertEqual(loose_bool.status_code, 400)
        self.assertEqual(loose_pages.status_code, 400)
        sync.assert_not_called()

    def test_sync_passes_only_authenticated_query_scope(self):
        result = {"id": 900, "status": "pending", "active": True}
        user_patch, login_patch = self._auth()
        with user_patch, login_patch, patch(
            "routes.marketplace_fulfillment.enqueue_read",
            return_value=result,
        ) as sync:
            response = self.client.post(
                f"/marketplaces/api/fulfillment/sync?account_id={self.own_account_id}",
                json={"period": "7d", "force": True, "max_pages": 4},
            )
        self.assertEqual(response.status_code, 202)
        sync.assert_called_once_with(
            seller_id=self.seller1_id,
            account_id=self.own_account_id,
            period_code="7d",
            force=True,
            domain="fulfillment",
        )

    def _seed_lines(self, count=121):
        account = db.session.get(SellerMarketplaceAccount, self.own_account_id)
        own = MarketplaceListing(seller_id=self.seller1_id, marketplace_id=account.marketplace_id,
            account_id=account.id, offer_id='own', external_product_id='1001', title='Наш товар',
            media_json=json.dumps({'primary_image':'https://cdn.example.test/own.jpg'}), sync_fingerprint='a'*64)
        foreign = MarketplaceListing(seller_id=self.seller2_id, marketplace_id=account.marketplace_id,
            account_id=self.foreign_account_id, offer_id='foreign', external_product_id='1002',
            title='Foreign private title', media_json=json.dumps({'primary_image':'https://cdn.example.test/foreign.jpg'}), sync_fingerprint='b'*64)
        db.session.add_all([own, foreign]); db.session.flush()
        for i in range(count):
            db.session.add(MarketplacePostingItem(posting_id=self.own_posting_id,
                seller_id=self.seller1_id, account_id=account.id, listing_id=own.id if i != 1 else foreign.id,
                identity_key=str(i), offer_id='own-'+str(i), name='Товар 100%_Хлопок '+str(i),
                quantity=1, unit_price=0 if i==0 else None, currency='RUB' if i==0 else None))
        # Corrupt legacy child scope must not leak even though its parent FK is ours.
        db.session.add(MarketplacePostingItem(posting_id=self.own_posting_id,
            seller_id=self.seller2_id, account_id=self.foreign_account_id, identity_key='bad-scope',
            name='Foreign private search text', quantity=1))
        now=datetime.utcnow()
        for i in range(102):
            db.session.add(MarketplacePostingStatusEvent(posting_id=self.own_posting_id,
                seller_id=self.seller1_id, account_id=account.id, status='delivered',
                event_fingerprint=str(i).zfill(64), observed_at=now))
        db.session.add(MarketplacePostingStatusEvent(posting_id=self.own_posting_id,
            seller_id=self.seller2_id, account_id=self.foreign_account_id, status='FOREIGN',
            event_fingerprint='f'*64, observed_at=now))
        db.session.commit()

    def test_compact_preview_and_paged_detail_are_bounded_and_exact_scoped(self):
        with self.app.app_context(): self._seed_lines()
        a,b=self._auth()
        with a,b:
            response=self.client.get(f'/marketplaces/api/orders?account_id={self.own_account_id}&view=compact')
            self.assertEqual(response.status_code,200)
            row=response.json['data']['items'][0]
            self.assertEqual((len(row['items']),row['item_count'],row['quantity']),(3,121,121))
            self.assertTrue(row['items_truncated'])
            self.assertNotIn('status_history',row)
            self.assertEqual(row['items'][0]['listing']['title'],'Наш товар')
            self.assertEqual(row['items'][0]['unit_price'],'0.0000')
            self.assertIsNone(row['items'][1]['listing'])
            self.assertIsNone(row['items'][1]['listing_id'])
            self.assertNotIn('foreign',json.dumps(row))
            ids=[]
            for page in (1,2,3):
                detail=self.client.get(f'/marketplaces/api/orders/{self.own_posting_id}?account_id={self.own_account_id}&view=compact&item_page={page}')
                self.assertEqual(detail.status_code,200)
                data=detail.json['data'];ids += [x['id'] for x in data['items']]
                self.assertEqual(data['item_pagination']['total'],121)
                self.assertEqual(data['item_pagination']['pages'],3)
                self.assertEqual(len(data['status_history']),100)
                self.assertTrue(data['history_truncated'])
                self.assertNotIn('FOREIGN',json.dumps(data))
            self.assertEqual(len(set(ids)),121)
            self.assertEqual(self.client.get(f'/marketplaces/api/orders/{self.own_posting_id}?account_id={self.foreign_account_id}&view=compact').status_code,404)

    def test_compact_search_is_literal_unicode_and_cannot_match_foreign_child(self):
        with self.app.app_context(): self._seed_lines(count=2)
        a,b=self._auth()
        with a,b:
            for term,total in [('тОвАр',1),('100%_хлопок',1),('%_',1),('Foreign private search',0),('100Xхлопок',0)]:
                with self.subTest(term=term):
                    response=self.client.get('/marketplaces/api/orders',query_string={
                        'account_id':self.own_account_id,'view':'compact','search':term})
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(response.json['data']['pagination']['total'],total)
            response=self.client.get('/marketplaces/api/orders',query_string={
                'account_id':self.own_account_id,'view':'compact','status':'cancelled','fulfillment':'fbs'})
            self.assertEqual(response.json['data']['status_counts'],{'delivered':1})
            self.assertEqual(response.json['data']['pagination']['total'],0)

    def test_return_and_cancel_links_require_exact_owned_posting(self):
        a,b=self._auth()
        with a,b:
            for path in ('returns','cancellations'):
                response=self.client.get(f'/marketplaces/api/{path}?account_id={self.own_account_id}&view=compact')
                self.assertEqual(response.json['data']['items'][0]['posting_url'],
                    f'/marketplaces/orders?account_id={self.own_account_id}&posting_id={self.own_posting_id}')
            with self.app.app_context():
                foreign=MarketplacePosting.query.filter_by(account_id=self.foreign_account_id).one()
                for model in (MarketplaceReturn,MarketplaceCancellation):
                    model.query.filter_by(account_id=self.own_account_id).one().posting_id=foreign.id
                db.session.commit()
            for path in ('returns','cancellations'):
                response=self.client.get(f'/marketplaces/api/{path}?account_id={self.own_account_id}&view=compact')
                self.assertIsNone(response.json['data']['items'][0]['posting_url'])

    def test_strict_queries_fail_without_reading_other_scopes(self):
        a,b=self._auth()
        with a,b:
            for path in ('orders','returns','cancellations','fulfillment/sync'):
                for extra in ('&account_id=2','&seller_id=2','&period=7d&period=30d'):
                    with self.subTest(path=path,extra=extra):
                        self.assertEqual(self.client.get(f'/marketplaces/api/{path}?account_id={self.own_account_id}'+extra).status_code,400)
            for field in ('page','per_page'):
                for value in ('0','01','1.0','-1','١','9999999999999999999'):
                    with self.subTest(field=field,value=value):
                        self.assertEqual(self.client.get(f'/marketplaces/api/orders?account_id={self.own_account_id}&{field}={value}').status_code,400)

    def test_compact_freshness_does_not_claim_running_as_completed_or_other_period(self):
        from datetime import timedelta
        with self.app.app_context():
            account=db.session.get(SellerMarketplaceAccount,self.own_account_id);now=datetime.utcnow()
            for period,status,stamp in [('30d','completed',now-timedelta(hours=2)),('7d','completed',now-timedelta(hours=1)),('30d','running',now)]:
                db.session.add(MarketplaceFulfillmentSync(seller_id=self.seller1_id,marketplace_id=account.marketplace_id,
                    account_id=account.id,period_code=period,period_start=now.date(),period_end=now.date(),
                    status=status,completed_at=stamp if status=='completed' else None,request_fingerprint=(period+status).ljust(64,'0')))
            db.session.commit()
        a,b=self._auth()
        with a,b:
            for path in ('orders','returns','cancellations'):
                response=self.client.get(f'/marketplaces/api/{path}?account_id={self.own_account_id}&view=compact')
                self.assertEqual(response.status_code,200)
                data=response.json['data']
                self.assertEqual(data['last_completed_sync']['period_code'],'30d')
                self.assertEqual(data['last_completed_sync']['status'],'completed')
                self.assertEqual(data['sync']['status'],'running')

    def test_preview_query_count_does_not_grow_per_posting(self):
        from sqlalchemy import event
        from services.marketplace_fulfillment_display import posting_previews
        with self.app.app_context():
            self._seed_lines(count=5)
            account=db.session.get(SellerMarketplaceAccount,self.own_account_id)
            posting=db.session.get(MarketplacePosting,self.own_posting_id)
            others=[]
            for i in range(12):
                row=MarketplacePosting(seller_id=account.seller_id,marketplace_id=account.marketplace_id,account_id=account.id,
                    posting_number='batch-'+str(i),fulfillment_kind='fbs',status='delivered',source_endpoint='/v4/posting/fbs/list',sync_fingerprint='a'*64,last_seen_at=datetime.utcnow())
                db.session.add(row);others.append(row)
            db.session.commit()
            # Materialize scalar rows before counting presentation queries.
            for row in [posting]+others:row.to_public_dict()
            _ = account.id, account.seller_id, account.marketplace_id
            calls=[]
            def count(*args):calls.append(args[2])
            event.listen(db.engine,'before_cursor_execute',count)
            try:
                posting_previews([posting],account=account);one=len(calls);calls.clear()
                result=posting_previews([posting]+others,account=account)
                self.assertEqual(len(calls),one)
                self.assertLessEqual(one,4)
                self.assertEqual(len(result),13)
            finally:event.remove(db.engine,'before_cursor_execute',count)

    def test_feature_flag_blocks_api(self):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        user_patch, login_patch = self._auth()
        with user_patch, login_patch:
            response = self.client.get(
                f"/marketplaces/api/orders?account_id={self.own_account_id}",
            )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
