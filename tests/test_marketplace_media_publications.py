# -*- coding: utf-8 -*-
"""Durable WB media preview/write/reconcile/rollback and Ozon exact reserve."""

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from unittest import mock

from flask import Flask
from flask_login import LoginManager
from PIL import Image

from models import (
    InfographicCampaign,
    InfographicCampaignItem,
    InfographicCampaignSlide,
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    MarketplaceMediaOperation,
    Product,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services import marketplace_media_publications as publications
from services.marketplace_adapters.types import MarketplaceCredentials
from services.marketplace_media_channels import (
    MediaGallery,
    MediaPhoto,
    WbMediaTarget,
    WildberriesMediaChannel,
)
from services.wb_api_client import WBTransportUncertainException
from routes.infographic_campaigns import register_infographic_campaign_routes
from routes.marketplace_media_publications import (
    register_marketplace_media_publication_routes,
)


def _wb_photo(index):
    base = f"https://basket-10.wbbasket.ru/vol1/part1/1001/images"
    return MediaPhoto(
        source_url=f"{base}/big/{index}.webp",
        fingerprint_url=f"{base}/tm/{index}.webp",
    )


class FakeWbMediaChannel:
    constraints = WildberriesMediaChannel.constraints

    def __init__(self, gallery, *, submitted_gallery=None, submit_error=None):
        self.gallery = gallery
        self.submitted_gallery = submitted_gallery or gallery
        self.submit_error = submit_error
        self.submit_calls = []
        self.after_submit = False

    def read_galleries(self, _credentials, targets, **_kwargs):
        return {target.nm_id: self.gallery for target in targets}

    def read_gallery(self, _credentials, _target, **_kwargs):
        return self.submitted_gallery if self.after_submit else self.gallery

    def submit_gallery_once(self, _credentials, target, urls, **_kwargs):
        self.submit_calls.append((target.nm_id, list(urls)))
        self.after_submit = True
        if self.submit_error:
            raise self.submit_error
        return {"error": False}


class MarketplaceMediaPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {
            "IMAGE_LAB_DATA_DIR": self.temp.name,
            "MEDIA_PUBLICATION_DATA_DIR": os.path.join(self.temp.name, "media"),
            "MEDIA_PUBLICATION_INLINE_WORKER": "0",
        })
        self.environment.start()
        publications._submitted_operation_ids.clear()
        self.app = Flask(
            __name__,
            template_folder=os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "templates",
            ),
            static_folder=os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "static",
            ),
        )
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="marketplace-media-publication-secret",
            PUBLIC_BASE_URL="https://seller.example.test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            MEDIA_PUBLICATION_INLINE_WORKER=False,
        )
        db.init_app(self.app)
        self.app.jinja_env.globals["csrf_token"] = lambda: "test-csrf"
        login = LoginManager(self.app)
        login.user_loader(lambda user_id: db.session.get(User, int(user_id)))
        register_infographic_campaign_routes(self.app)
        register_marketplace_media_publication_routes(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        self.user = User(username="media-publisher", email="media@publisher.test")
        self.user.set_password("password")
        db.session.add(self.user)
        db.session.flush()
        self.seller = Seller(
            user_id=self.user.id,
            company_name="Media publisher",
            wb_seller_id="seller-1",
        )
        self.seller.wb_api_key = "wb-key"
        db.session.add(self.seller)
        db.session.flush()
        self.product = Product(
            seller_id=self.seller.id,
            nm_id=1001,
            vendor_code="V-1001",
            title="Крем",
            is_active=True,
        )
        db.session.add(self.product)
        db.session.flush()
        self.imported = ImportedProduct(
            seller_id=self.seller.id,
            product_id=self.product.id,
            external_id="cream",
            title="Крем",
            photo_urls='["https://source.test/cream.jpg"]',
        )
        db.session.add(self.imported)
        db.session.flush()
        self.campaign = InfographicCampaign(
            seller_id=self.seller.id,
            created_by_user_id=self.user.id,
            name="Кремы",
            template_key="botanical",
            mode="catalog",
            status="approved",
            total_items=1,
            runnable_items=1,
            completed_items=1,
            approved_items=1,
            total_slides=2,
            completed_slides=2,
            approved_slides=2,
        )
        db.session.add(self.campaign)
        db.session.flush()
        self.item = InfographicCampaignItem(
            campaign_id=self.campaign.id,
            seller_id=self.seller.id,
            imported_product_id=self.imported.id,
            product_title="Крем",
            status="ready",
            source_fingerprint="a" * 64,
            total_slides=2,
            completed_slides=2,
            approved_slides=2,
        )
        db.session.add(self.item)
        db.session.flush()
        self.slides = [self._slide(1, (220, 180, 120)), self._slide(2, (80, 140, 190))]
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.user.id)
            session["_fresh"] = True

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        publications._submitted_operation_ids.clear()
        self.environment.stop()
        self.temp.cleanup()

    def _slide(self, position, color):
        relative = Path(str(self.seller.id)) / "campaigns" / str(self.campaign.id) / str(self.item.id) / f"{position:02d}.png"
        path = Path(self.temp.name) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (900, 1200), color).save(path, format="PNG")
        data = path.read_bytes()
        slide = InfographicCampaignSlide(
            campaign_id=self.campaign.id,
            item_id=self.item.id,
            seller_id=self.seller.id,
            position=position,
            slide_type="hero" if position == 1 else "facts",
            status="completed",
            review_status="approved",
            artifact_path=str(relative),
            artifact_sha256=hashlib.sha256(data).hexdigest(),
            reviewed_by_user_id=self.user.id,
        )
        db.session.add(slide)
        db.session.flush()
        return slide

    def _prepare(self, gallery=None, channel=None):
        gallery = gallery or MediaGallery("1001", (_wb_photo(1),))
        channel = channel or FakeWbMediaChannel(gallery)
        publication = publications.prepare_publication(
            self.campaign,
            seller_id=self.seller.id,
            user_id=self.user.id,
            marketplace_code="wb",
            account_id=None,
            item_ids=[self.item.id],
            channel=channel,
        )
        return publication, channel

    @staticmethod
    def _fake_cache(_url, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (900, 1200), (120, 120, 120)).save(target, "JPEG")
        state = publications._local_visual_state(target)
        return {**state, "mime_type": "image/jpeg"}

    def test_preview_prepends_approved_and_explicitly_drops_tail(self):
        gallery = MediaGallery("1001", tuple(_wb_photo(index) for index in range(1, 30)))
        publication, channel = self._prepare(gallery)
        operation = publication.operations.filter_by(operation_kind="publish").one()
        summary = publications.operation_summary(operation, detail=True)

        self.assertEqual(publication.status, "ready")
        self.assertEqual(summary["proposed_count"], 30)
        self.assertEqual(summary["generated_count"], 2)
        self.assertEqual(summary["dropped_count"], 1)
        self.assertEqual(
            [entry["kind"] for entry in summary["proposed_media"][:3]],
            ["generated", "generated", "current"],
        )
        self.assertEqual(len(channel.submit_calls), 0)

    def test_mass_wizard_shows_wb_and_typed_ozon_reserve(self):
        with mock.patch(
            "routes.marketplace_media_publications.render_template",
            return_value="wizard",
        ) as render:
            response = self.client.get(
                f"/image-lab/campaigns/{self.campaign.id}/publication/new"
            )
        self.assertEqual(response.status_code, 200)
        readiness = render.call_args.kwargs["readiness"]
        self.assertTrue(readiness["channels"]["wb"]["publication_supported"])
        self.assertFalse(readiness["channels"]["ozon"]["publication_supported"])
        self.assertEqual(readiness["channels"]["wb"]["linked_items"], 1)

    def test_ozon_reserve_keeps_only_bounded_media_observation(self):
        marketplace = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add(marketplace)
        db.session.flush()
        account = SellerMarketplaceAccount(
            seller_id=self.seller.id,
            marketplace_id=marketplace.id,
            external_account_id="ozon-smoke",
            label="Ozon smoke",
            is_active=True,
            connection_status="connected",
        )
        db.session.add(account)
        db.session.flush()
        secret_url = "https://cdn1.ozone.ru/s3/multimedia-secret/image.jpg"
        listing = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=marketplace.id,
            account_id=account.id,
            imported_product_id=self.imported.id,
            offer_id="offer-smoke",
            external_product_id="ozon-product-smoke",
            title="Ozon Крем",
            normalized_status="active",
            link_status="linked",
            is_available=True,
            is_archived=False,
            media_json=json.dumps({
                "primary_image": secret_url,
                "images": [secret_url],
            }),
            sync_fingerprint="f" * 64,
        )
        db.session.add(listing)
        db.session.commit()

        publication = publications.prepare_publication(
            self.campaign,
            seller_id=self.seller.id,
            user_id=self.user.id,
            marketplace_code="ozon",
            account_id=account.id,
            item_ids=[self.item.id],
        )
        summary = publications.publication_summary(publication, detail=True)
        serialized = json.dumps(summary, ensure_ascii=False)

        self.assertEqual(publication.status, "draft")
        self.assertNotIn(secret_url, serialized)
        self.assertEqual(
            summary["operations"][0]["target"]["observed_media"]["main_image_count"],
            1,
        )
        self.assertFalse(summary["constraints"]["publication_supported"])

    def test_wb_channel_disables_transport_retry_for_media_write(self):
        captured = {}

        class Client:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def upload_photos_by_url(self, nm_id, urls, seller_id=None):
                captured.update({
                    "nm_id": nm_id,
                    "urls": list(urls),
                    "seller_id": seller_id,
                })
                return {"error": False}

        channel = WildberriesMediaChannel(client_factory=Client)
        url = "https://seller.example.test/media/one.png"
        result = channel.submit_gallery_once(
            MarketplaceCredentials(api_key="synthetic-wb-key"),
            WbMediaTarget(1001),
            [url],
            audit_seller_id=self.seller.id,
        )

        self.assertEqual(result, {"error": False})
        self.assertEqual(captured["max_retries"], 0)
        self.assertEqual(captured["nm_id"], 1001)
        self.assertEqual(captured["urls"], [url])
        self.assertEqual(captured["seller_id"], self.seller.id)

    def test_confirmation_is_optimistic_and_requires_public_https(self):
        publication, _channel = self._prepare()
        with self.assertRaises(publications.MarketplaceMediaPublicationError) as missing:
            publications.confirm_publication(
                publication,
                seller_id=self.seller.id,
                user_id=self.user.id,
                expected_version=publication.version,
                confirm_exact_order=True,
                config={"PUBLIC_BASE_URL": "", "SECRET_KEY": "x" * 32},
            )
        self.assertEqual(missing.exception.code, "public_base_url_missing")

        stale_version = publication.version + 1
        with self.assertRaises(publications.MarketplaceMediaPublicationError) as stale:
            publications.confirm_publication(
                publication,
                seller_id=self.seller.id,
                user_id=self.user.id,
                expected_version=stale_version,
                confirm_exact_order=True,
                config=self.app.config,
            )
        self.assertEqual(stale.exception.code, "version_conflict")

        queued = publications.confirm_publication(
            publication,
            seller_id=self.seller.id,
            user_id=self.user.id,
            expected_version=publication.version,
            confirm_exact_order=True,
            config=self.app.config,
        )
        self.assertEqual(queued, 1)
        self.assertEqual(publication.operations.first().status, "queued")

    def test_due_reconciliation_is_processed_before_a_new_write(self):
        publication, _channel = self._prepare()
        queued = publication.operations.first()
        queued.status = "queued"
        due = MarketplaceMediaOperation(
            publication_id=publication.id,
            seller_id=self.seller.id,
            created_by_user_id=self.user.id,
            imported_product_id=self.imported.id,
            marketplace_code="wb",
            external_item_id="reconcile-first",
            operation_kind="rollback",
            status="reconciling",
            placement_policy="restore_snapshot",
            target_json="{}",
            source_snapshot_json="{}",
            baseline_media_json="[]",
            proposed_media_json="[]",
            dropped_media_json="[]",
            baseline_fingerprint="b" * 64,
            proposed_fingerprint="c" * 64,
            attempt_count=1,
            next_reconcile_at=datetime.utcnow(),
        )
        db.session.add(due)
        db.session.commit()

        with mock.patch.object(
            publications,
            "process_operation",
            return_value=False,
        ) as process:
            processed = publications.process_pending_once(self.app, limit=1)

        self.assertEqual(processed, 1)
        process.assert_called_once_with(self.app, due.id)

    def test_single_attempt_write_reconciles_visual_order(self):
        baseline = MediaGallery("1001", (_wb_photo(1),))
        submitted = MediaGallery("1001", tuple(_wb_photo(index) for index in range(11, 14)))
        channel = FakeWbMediaChannel(baseline, submitted_gallery=submitted)
        publication, _ = self._prepare(channel=channel)
        publications.confirm_publication(
            publication,
            seller_id=self.seller.id,
            user_id=self.user.id,
            expected_version=publication.version,
            confirm_exact_order=True,
            config=self.app.config,
        )
        operation = publication.operations.first()

        with mock.patch.object(publications, "_normalize_remote_to_cache", side_effect=self._fake_cache), mock.patch.object(publications, "_remote_visual_hash", return_value="0" * 16):
            publications.process_operation(self.app, operation.id, channel=channel)
            db.session.expire_all()
            operation = db.session.get(MarketplaceMediaOperation, operation.id)
            self.assertEqual(operation.status, "reconciling")
            self.assertEqual(operation.attempt_count, 1)
            self.assertEqual(len(channel.submit_calls), 1)
            self.assertEqual(len(channel.submit_calls[0][1]), 3)
            self.assertTrue(all(url.startswith("https://seller.example.test/media-publications/assets/") for url in channel.submit_calls[0][1]))

            asset_path = urlsplit(channel.submit_calls[0][1][0]).path
            public_client = self.app.test_client()
            self.assertEqual(public_client.get(asset_path).status_code, 200)
            filename = asset_path.rsplit("/", 1)[1]
            replacement = "0" if filename[0] != "0" else "1"
            invalid_path = asset_path.rsplit("/", 1)[0] + "/" + replacement + filename[1:]
            self.assertEqual(public_client.get(invalid_path).status_code, 403)
            expired_path = (
                f"/media-publications/assets/{operation.id}/1/1/deadbeef.img"
            )
            self.assertEqual(public_client.get(expired_path).status_code, 410)

            publications.process_operation(self.app, operation.id, channel=channel)

        db.session.expire_all()
        operation = db.session.get(MarketplaceMediaOperation, operation.id)
        self.assertEqual(operation.status, "succeeded")
        self.assertEqual(operation.attempt_count, 1)
        self.assertEqual(len(channel.submit_calls), 1)
        self.assertEqual(len(json.loads(db.session.get(Product, self.product.id).photos_json)), 3)

    def test_ambiguous_write_is_never_retried_and_can_reconcile(self):
        baseline = MediaGallery("1001", (_wb_photo(1),))
        submitted = MediaGallery("1001", tuple(_wb_photo(index) for index in range(11, 14)))
        channel = FakeWbMediaChannel(
            baseline,
            submitted_gallery=submitted,
            submit_error=WBTransportUncertainException(
                "timeout", request_may_have_been_applied=True,
            ),
        )
        publication, _ = self._prepare(channel=channel)
        publications.confirm_publication(
            publication,
            seller_id=self.seller.id,
            user_id=self.user.id,
            expected_version=publication.version,
            confirm_exact_order=True,
            config=self.app.config,
        )
        operation = publication.operations.first()
        with mock.patch.object(publications, "_normalize_remote_to_cache", side_effect=self._fake_cache), mock.patch.object(publications, "_remote_visual_hash", return_value="0" * 16):
            publications.process_operation(self.app, operation.id, channel=channel)
            db.session.expire_all()
            operation = db.session.get(MarketplaceMediaOperation, operation.id)
            self.assertEqual(operation.status, "uncertain")
            publications.process_operation(self.app, operation.id, channel=channel)

        db.session.expire_all()
        operation = db.session.get(MarketplaceMediaOperation, operation.id)
        self.assertEqual(operation.status, "succeeded")
        self.assertEqual(operation.attempt_count, 1)
        self.assertEqual(len(channel.submit_calls), 1)

    def test_rollback_uses_cached_original_and_fresh_exact_live_state(self):
        baseline = MediaGallery("1001", (_wb_photo(1),))
        submitted = MediaGallery("1001", tuple(_wb_photo(index) for index in range(11, 14)))
        channel = FakeWbMediaChannel(baseline, submitted_gallery=submitted)
        publication, _ = self._prepare(channel=channel)
        publications.confirm_publication(
            publication,
            seller_id=self.seller.id,
            user_id=self.user.id,
            expected_version=publication.version,
            confirm_exact_order=True,
            config=self.app.config,
        )
        original = publication.operations.first()
        with mock.patch.object(publications, "_normalize_remote_to_cache", side_effect=self._fake_cache), mock.patch.object(publications, "_remote_visual_hash", return_value="0" * 16):
            publications.process_operation(self.app, original.id, channel=channel)
            publications.process_operation(self.app, original.id, channel=channel)
        db.session.expire_all()
        original = db.session.get(MarketplaceMediaOperation, original.id)
        channel.after_submit = False
        channel.gallery = submitted
        rollbacks = publications.create_rollbacks(
            publication,
            seller_id=self.seller.id,
            user_id=self.user.id,
            operation_ids=[original.id],
            confirm_exact_state=True,
            channel=channel,
        )
        self.assertEqual(len(rollbacks), 1)
        rollback = rollbacks[0]
        self.assertEqual(rollback.operation_kind, "rollback")
        self.assertEqual(rollback.status, "queued")
        restore = json.loads(rollback.proposed_media_json)
        self.assertEqual(len(restore), 1)
        self.assertEqual(restore[0]["kind"], "restore")
        self.assertEqual(restore[0]["storage_kind"], "media_publication")


if __name__ == "__main__":
    unittest.main()
