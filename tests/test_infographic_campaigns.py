# -*- coding: utf-8 -*-
"""Bulk infographic campaigns: exact tenant scope, durable render and review."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from flask import Flask
from flask_login import LoginManager

from models import (
    InfographicCampaign,
    InfographicCampaignItem,
    InfographicCampaignSlide,
    ImportedProduct,
    Seller,
    User,
    db,
)
from routes.infographic_campaigns import register_infographic_campaign_routes
from services import infographic_campaigns as campaigns


class InfographicCampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {
            "IMAGE_LAB_DATA_DIR": self.temp.name,
            "IMAGE_LAB_INLINE_WORKER": "0",
        })
        self.environment.start()
        self.photo_loader = mock.patch.object(
            campaigns,
            "_load_original_photo_bytes",
            return_value=b"verified-original-photo",
        )
        self.photo_loader_mock = self.photo_loader.start()
        campaigns._submitted_item_ids.clear()
        campaigns._last_inline_recovery.clear()
        self.app = Flask(__name__, template_folder=os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates",
        ))
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="infographic-campaign-test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            WTF_CSRF_ENABLED=False,
        )
        db.init_app(self.app)
        login = LoginManager(self.app)
        login.user_loader(lambda user_id: db.session.get(User, int(user_id)))
        register_infographic_campaign_routes(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        user1 = User(username="campaign-one", email="one@campaign.test")
        user1.set_password("test-password")
        user2 = User(username="campaign-two", email="two@campaign.test")
        user2.set_password("test-password")
        db.session.add_all([user1, user2])
        db.session.flush()
        seller1 = Seller(user_id=user1.id, company_name="Campaign One")
        seller2 = Seller(user_id=user2.id, company_name="Campaign Two")
        db.session.add_all([seller1, seller2])
        db.session.flush()

        ready = ImportedProduct(
            seller_id=seller1.id,
            external_id="ready-product",
            title="Крем для рук",
            category="Уход за руками",
            brand="Листья",
            characteristics=json.dumps({
                "Объём": "50 мл",
                "Страна производства": "Россия",
            }, ensure_ascii=False),
            photo_urls=json.dumps(["https://cdn.example.test/cream.png"]),
        )
        blocked = ImportedProduct(
            seller_id=seller1.id,
            external_id="blocked-product",
            title="Крем без фото",
            category="Уход за руками",
            characteristics=json.dumps({"Объём": "30 мл"}, ensure_ascii=False),
            photo_urls="[]",
        )
        foreign = ImportedProduct(
            seller_id=seller2.id,
            external_id="foreign-product",
            title="Чужой крем",
            category="Уход",
            characteristics=json.dumps({"Объём": "40 мл"}, ensure_ascii=False),
            photo_urls=json.dumps(["https://cdn.example.test/foreign.png"]),
        )
        db.session.add_all([ready, blocked, foreign])
        db.session.commit()

        self.user1_id = user1.id
        self.user2_id = user2.id
        self.seller1_id = seller1.id
        self.seller2_id = seller2.id
        self.ready_id = ready.id
        self.blocked_id = blocked.id
        self.foreign_id = foreign.id
        self.client = self.app.test_client()
        self._login(self.user1_id)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.photo_loader.stop()
        self.environment.stop()
        campaigns._submitted_item_ids.clear()
        campaigns._last_inline_recovery.clear()
        self.temp.cleanup()

    def _login(self, user_id):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True

    def _create(self, product_ids=None):
        return campaigns.create_campaign(
            seller_id=self.seller1_id,
            user_id=self.user1_id,
            product_ids=product_ids or [self.ready_id],
            template_key="botanical",
            slide_limit=6,
            name="Каталог кремов",
        )

    @staticmethod
    def _render_results():
        return [
            {
                "slide_number": 1,
                "slide_type": "hero",
                "success": True,
                "image_bytes": b"\x89PNG\r\nhero",
                "quality": {"status": "review_required", "publishable": False},
            },
            {
                "slide_number": 2,
                "slide_type": "characteristics",
                "success": True,
                "image_bytes": b"\x89PNG\r\nfact",
                "quality": {"status": "review_required", "publishable": False},
            },
        ]

    def test_create_is_exact_scoped_and_one_blocked_item_does_not_stop_rest(self):
        campaign = self._create([self.ready_id, self.blocked_id])
        items = campaign.items.order_by(InfographicCampaignItem.id).all()

        self.assertEqual(campaign.total_items, 2)
        self.assertEqual(campaign.runnable_items, 1)
        self.assertEqual(campaign.failed_items, 1)
        self.assertEqual([item.status for item in items], ["queued", "blocked"])
        self.assertEqual(items[1].error_code, "missing_photo")
        self.assertEqual(campaigns.campaign_summary(campaign)["estimated_cost_rub"], 0.0)

        with self.assertRaises(campaigns.InfographicCampaignError):
            self._create([self.ready_id, self.foreign_id])
        with self.assertRaises(campaigns.InfographicCampaignError):
            self._create([self.ready_id, self.ready_id])
        with self.assertRaises(campaigns.InfographicCampaignError):
            campaigns.create_campaign(
                seller_id=self.seller1_id,
                user_id=self.user1_id,
                product_ids=[True],
            )

    def test_preview_uses_the_same_second_slide_readiness_as_creation(self):
        product = ImportedProduct(
            seller_id=self.seller1_id,
            external_id="hero-only",
            title="Товар только для обложки",
            category="Категория",
            photo_urls=json.dumps(["https://cdn.example.test/hero.png"]),
        )
        db.session.add(product)
        db.session.commit()

        preview = campaigns.preview_products(self.seller1_id, [product.id])[0]
        self.assertFalse(preview["ready"])
        self.assertIn("второго слайда", preview["reasons"][0])

    @mock.patch("services.infographic_renderer.render_hybrid_slides")
    def test_render_persists_private_artifacts_then_human_review(self, render):
        render.return_value = self._render_results()
        campaign = self._create()
        item = campaign.items.first()

        self.assertTrue(campaigns.render_item(self.app, item.id))
        db.session.expire_all()
        item = db.session.get(InfographicCampaignItem, item.id)
        campaign = db.session.get(InfographicCampaign, campaign.id)
        slides = item.slides.order_by(InfographicCampaignSlide.position).all()

        self.assertEqual(item.status, "ready")
        self.assertEqual(campaign.status, "review")
        self.assertEqual(len(slides), 2)
        self.assertTrue(all(campaigns.slide_artifact_path(slide) for slide in slides))
        self.assertTrue(all(slide.review_status == "pending" for slide in slides))
        self.assertEqual(
            render.call_args.kwargs["source_photo_bytes"],
            b"verified-original-photo",
        )

        changed = campaigns.review_slides(
            campaign,
            seller_id=self.seller1_id,
            user_id=self.user1_id,
            action="approve",
            item_ids=[item.id],
        )
        self.assertEqual(changed, 2)
        self.assertEqual(campaign.status, "approved")
        self.assertEqual(campaign.approved_items, 1)
        self.assertEqual(campaign.approved_slides, 2)

    @mock.patch("services.infographic_renderer.render_hybrid_slides")
    def test_mixed_review_is_partial_and_quality_rejection_is_atomic(self, render):
        render.return_value = self._render_results()
        campaign = self._create()
        item = campaign.items.first()
        campaigns.render_item(self.app, item.id)
        db.session.expire_all()
        campaign = db.session.get(InfographicCampaign, campaign.id)
        item = db.session.get(InfographicCampaignItem, item.id)
        slides = item.slides.order_by(InfographicCampaignSlide.position).all()
        slides[1].quality_json = json.dumps({
            "status": "rejected", "publishable": False,
        })
        db.session.commit()

        with self.assertRaises(campaigns.InfographicCampaignError) as raised:
            campaigns.review_slides(
                campaign,
                seller_id=self.seller1_id,
                user_id=self.user1_id,
                action="approve",
                item_ids=[item.id],
            )
        self.assertEqual(raised.exception.code, "quality_rejected")
        self.assertTrue(all(slide.review_status == "pending" for slide in slides))

        campaigns.review_slides(
            campaign,
            seller_id=self.seller1_id,
            user_id=self.user1_id,
            action="approve",
            slide_ids=[slides[0].id],
        )
        campaigns.review_slides(
            campaign,
            seller_id=self.seller1_id,
            user_id=self.user1_id,
            action="reject",
            slide_ids=[slides[1].id],
        )
        self.assertEqual(campaign.status, "partial")
        self.assertEqual(campaign.approved_slides, 1)

    @mock.patch("services.infographic_renderer.render_hybrid_slides")
    def test_source_drift_blocks_provider_work(self, render):
        campaign = self._create()
        item = campaign.items.first()
        product = db.session.get(ImportedProduct, self.ready_id)
        product.title = "Карточка изменилась"
        db.session.commit()

        self.assertFalse(campaigns.render_item(self.app, item.id))
        db.session.expire_all()
        item = db.session.get(InfographicCampaignItem, item.id)
        self.assertEqual(item.status, "conflict")
        self.assertEqual(item.error_code, "source_drift")
        render.assert_not_called()

    @mock.patch("routes.infographic_campaigns.campaigns.launch_campaign")
    def test_json_route_rejects_foreign_and_non_integer_ids(self, launch):
        response = self.client.post("/image-lab/campaigns", json={
            "product_ids": [self.ready_id],
            "template_key": "studio",
            "slide_limit": 4,
            "name": "API campaign",
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        campaign_id = response.get_json()["campaign"]["id"]
        self.assertEqual(
            db.session.get(InfographicCampaign, campaign_id).seller_id,
            self.seller1_id,
        )
        launch.assert_called_once()

        for product_ids in ([self.foreign_id], [True], [self.ready_id, self.ready_id]):
            with self.subTest(product_ids=product_ids):
                response = self.client.post("/image-lab/campaigns", json={
                    "product_ids": product_ids,
                    "template_key": "studio",
                    "slide_limit": 4,
                })
                self.assertEqual(response.status_code, 400, response.get_json())

    @mock.patch("routes.infographic_campaigns.campaigns.launch_campaign")
    def test_form_create_uses_repeated_exact_product_ids(self, launch):
        response = self.client.post("/image-lab/campaigns", data={
            "product_ids": [str(self.ready_id), str(self.blocked_id)],
            "template_key": "contrast",
            "slide_limit": "5",
            "name": "Form campaign",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        campaign = InfographicCampaign.query.filter_by(
            seller_id=self.seller1_id, name="Form campaign",
        ).one()
        self.assertEqual(campaign.total_items, 2)
        self.assertEqual(campaign.runnable_items, 1)
        self.assertTrue(response.headers["Location"].endswith(
            f"/image-lab/campaigns/{campaign.id}",
        ))
        launch.assert_called_once()

    def test_foreign_campaign_is_hidden_from_routes(self):
        foreign_campaign = campaigns.create_campaign(
            seller_id=self.seller2_id,
            user_id=self.user2_id,
            product_ids=[self.foreign_id],
            name="Foreign campaign",
        )
        response = self.client.get(
            f"/image-lab/api/campaigns/{foreign_campaign.id}",
        )
        self.assertEqual(response.status_code, 404)

    def test_inline_launcher_has_a_global_bounded_submission_window(self):
        campaign = self._create()
        for index in range(12):
            db.session.add(InfographicCampaignItem(
                campaign_id=campaign.id,
                seller_id=self.seller1_id,
                imported_product_id=None,
                product_title=f"Queued {index}",
                status="queued",
                source_fingerprint=str(index).zfill(64),
            ))
        db.session.commit()
        campaigns._submitted_item_ids.clear()
        try:
            with mock.patch.dict(os.environ, {"IMAGE_LAB_INLINE_WORKER": "1"}), \
                    mock.patch.object(campaigns._executor, "submit") as submit:
                submitted = campaigns.launch_campaign(self.app, campaign.id)
            self.assertEqual(submitted, campaigns.INLINE_MAX_SUBMITTED)
            self.assertEqual(submit.call_count, campaigns.INLINE_MAX_SUBMITTED)
        finally:
            campaigns._submitted_item_ids.clear()

    def test_cancel_atomically_closes_queued_and_running_items(self):
        campaign = self._create()
        item = campaign.items.first()
        item.status = "running"
        campaign.status = "running"
        db.session.commit()

        self.assertEqual(
            campaigns.cancel_campaign(campaign, seller_id=self.seller1_id),
            1,
        )
        db.session.expire_all()
        self.assertEqual(
            db.session.get(InfographicCampaign, campaign.id).status,
            "cancelled",
        )
        self.assertEqual(
            db.session.get(InfographicCampaignItem, item.id).status,
            "cancelled",
        )
        self.assertEqual(
            campaigns.cancel_campaign(campaign, seller_id=self.seller1_id),
            0,
        )

    def test_stale_running_item_becomes_explicit_retryable_failure(self):
        campaign = self._create()
        item = campaign.items.first()
        item.status = "running"
        item.started_at = datetime.utcnow() - timedelta(minutes=31)
        campaign.status = "running"
        db.session.commit()

        self.assertEqual(
            campaigns.recover_stale_items(
                self.app, campaign_id=campaign.id, limit=5,
            ),
            1,
        )
        db.session.expire_all()
        item = db.session.get(InfographicCampaignItem, item.id)
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.error_code, "worker_interrupted")


if __name__ == "__main__":
    unittest.main()
