"""Commercial entry points expose only seller-owned quarantine navigation."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from models import (MarketplaceOperation, MarketplaceWriteQuarantine,
                    MarketplaceCommercialProposal, db)
from services.marketplace_commercial import QuarantinedCommercialConflict

sys.path.insert(0, str(Path(__file__).parent))
import test_marketplace_commercial_routes as route_fixture


class CommercialQuarantineRoutesTest(unittest.TestCase):
    setUp = route_fixture.MarketplaceCommercialRoutesTest.setUp
    tearDown = route_fixture.MarketplaceCommercialRoutesTest.tearDown
    _seller = staticmethod(route_fixture.MarketplaceCommercialRoutesTest._seller)
    _listing = staticmethod(route_fixture.MarketplaceCommercialRoutesTest._listing)
    _proposal = staticmethod(route_fixture.MarketplaceCommercialRoutesTest._proposal)
    _user = staticmethod(route_fixture.MarketplaceCommercialRoutesTest._user)
    _auth = route_fixture.MarketplaceCommercialRoutesTest._auth

    def _hold(self):
        own = db.session.get(MarketplaceCommercialProposal, self.own_proposal_id)
        origin = MarketplaceOperation(
            seller_id=own.seller_id, marketplace_id=own.marketplace_id,
            account_id=own.account_id, listing_id=own.listing_id,
            operation_kind="price_update", status="uncertain",
            idempotency_key="quarantine-origin-route-0001",
            request_fingerprint="a" * 64, contract_version="test",
            request_summary_json="{}", attempt_count=1,
        )
        db.session.add(origin)
        db.session.flush()
        hold = MarketplaceWriteQuarantine(
            seller_id=own.seller_id, marketplace_id=own.marketplace_id,
            account_id=own.account_id, operation_id=origin.id,
            scope_kind="product", offer_id="own-commercial-offer",
            product_id="101", scope_reason="immutable_target_verified",
            reviewed_scope_token="a" * 64, status="active",
        )
        db.session.add(hold)
        db.session.commit()
        return hold

    def test_detail_and_list_expose_exact_review_link_only_to_owner(self):
        with self.app.app_context():
            hold = self._hold()
            hold_id, origin_id = hold.id, hold.operation_id
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch:
            detail = self.client.get(
                f"/marketplaces/commercial/api/{self.own_proposal_id}")
            listing = self.client.get("/marketplaces/commercial/?status=pending_review",
                                      headers={"Accept": "application/json"})
            foreign = self.client.get(
                f"/marketplaces/commercial/api/{self.foreign_proposal_id}")
        self.assertEqual((detail.status_code, listing.status_code, foreign.status_code),
                         (200, 200, 404))
        expected = {"id": hold_id, "scope": "product", "status": "active",
                    "operation_id": origin_id, "media_operation_id": None,
                    "review_url": f"/marketplaces/operations/{origin_id}/review"}
        self.assertEqual(detail.get_json()["proposal"]["write_quarantine"], expected)
        self.assertEqual(listing.get_json()["items"][0]["write_quarantine"], expected)
        self.assertNotIn("foreign-commercial-secret",
                         json.dumps(listing.get_json(), ensure_ascii=False))

    def test_typed_approval_conflict_returns_review_link_without_write(self):
        with self.app.app_context():
            hold = self._hold()
            error = QuarantinedCommercialConflict(hold)
        self.app.config["MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED"] = True
        user_patch, login_patch = self._auth(self.seller1_id, self.user1_id)
        with user_patch, login_patch, patch(
            "routes.marketplace_commercial.MarketplaceCommercialService.approve_proposal",
            side_effect=error,
        ) as approve:
            response = self.client.post(
                f"/marketplaces/commercial/{self.own_proposal_id}/approve",
                json={"expected_version": 1, "confirm_write": True},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "ozon_write_quarantined")
        self.assertEqual(response.get_json()["write_quarantine"], error.write_quarantine)
        approve.assert_called_once()


def test_vue_refuses_held_approval_and_rejects_untrusted_review_urls():
    node = shutil.which("node")
    if not node:
        return
    script = (Path(__file__).parents[1] / "static" / "ozon-commercial.js").read_text()
    setup = r'''
const assert=require('node:assert/strict');
global.window=global;
global.location=new URL('https://fixture.test/marketplaces/commercial/');
global.document={
  getElementById:id=>id==='ozon-commercial-app'?{}:id==='oc-bootstrap'?{textContent:JSON.stringify({mode:'list',base:'/marketplaces/commercial/',catalog:'/marketplaces/catalog/',writeEnabled:true})}:null,
  querySelectorAll:selector=>{assert.equal(selector,'form[data-oc-refresh-form]');return []}
};
global.mcatShared={ozonPrices:{},imageDeadline:{}};
let options;global.Vue={createApp:value=>{options=value;return {directive(){},mount(){}}}};
'''
    scenario = r'''
const hold={id:7,scope:'product',status:'active',operation_id:22,review_url:'/marketplaces/operations/22/review'};
const page={...options.data(),...options.methods};
assert.equal(page.reviewUrl(hold),hold.review_url);
assert.equal(page.reviewUrl({...hold,review_url:'https://other.test/'}),'');
assert.equal(page.reviewUrl({...hold,review_url:'/marketplaces/operations/23/review'}),'');
let sends=0;page.decision=()=>sends++;page.confirmed=true;page.writeEnabled=true;
page.proposal={status:'pending_review',target_available:true,write_quarantine:hold};
page.approve();assert.equal(sends,0);
page.busy=false;page.batchBlocked=false;page.batchConfirmed=true;page.batchReview=[{id:1,write_quarantine:hold}];
page.approveBatch();assert.equal(sends,0);
'''
    result = subprocess.run([node, "-e", setup + script + scenario],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
