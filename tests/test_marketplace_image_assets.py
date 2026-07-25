from io import BytesIO
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from flask import Flask
from PIL import Image

from routes.marketplace_image_assets import (
    register_marketplace_image_asset_routes,
)
from services.image_lab_service import ImageLabError
from services.marketplace_image_assets import (
    MarketplaceImageAssetError,
    materialize_product_payload,
)


def _jpeg(width=800, height=1000, color=(120, 80, 40)):
    output = BytesIO()
    Image.new("RGB", (width, height), color).save(
        output,
        format="JPEG",
        quality=92,
    )
    return output.getvalue()


class MarketplaceImageAssetsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config = {
            "PUBLIC_BASE_URL": "https://seller.test",
            "MARKETPLACE_IMAGE_ASSET_DIR": self.temporary.name,
            "OZON_MEDIA_ASSET_URLS_PER_ATTEMPT": 1,
            "OZON_MEDIA_ASSET_ATTEMPT_SECONDS": 40,
        }
        self.secret = "asset-test-secret"

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _payload(urls):
        return {
            "items": [{
                "offer_id": "safe-offer",
                "images": list(urls),
            }],
        }

    def test_bounded_partial_progress_resumes_without_refetching_asset(self):
        payload = self._payload([
            "https://source.test/one.jpg",
            "https://source.test/two.jpg",
        ])
        with patch(
            "services.marketplace_image_assets.download_public_image",
            side_effect=[_jpeg(color=(200, 10, 10)), _jpeg(color=(10, 200, 10))],
        ) as download:
            first = materialize_product_payload(
                payload,
                config=self.config,
                secret_key=self.secret,
            )
            self.assertFalse(first.complete)
            self.assertEqual(first.prepared_now, 1)
            self.assertEqual(first.prepared_total, 1)
            self.assertIn(
                "/marketplace-assets/images/",
                first.payload["items"][0]["images"][0],
            )
            self.assertEqual(
                first.payload["items"][0]["images"][1],
                "https://source.test/two.jpg",
            )

            second = materialize_product_payload(
                first.payload,
                config=self.config,
                secret_key=self.secret,
            )

        self.assertTrue(second.complete)
        self.assertEqual(second.prepared_now, 1)
        self.assertEqual(second.prepared_total, 2)
        self.assertEqual(download.call_count, 2)
        self.assertTrue(
            all(
                "/marketplace-assets/images/" in value
                for value in second.payload["items"][0]["images"]
            )
        )
        self.assertEqual(len(list(Path(self.temporary.name).rglob("*.jpg"))), 2)

    def test_same_image_bytes_are_deduplicated_after_materialization(self):
        payload = self._payload([
            "https://source.test/one.jpg",
            "https://source.test/two.jpg",
        ])
        same = _jpeg()
        with patch(
            "services.marketplace_image_assets.download_public_image",
            side_effect=[same, same],
        ):
            result = materialize_product_payload(
                payload,
                config=self.config,
                secret_key=self.secret,
                max_urls=2,
            )
        self.assertTrue(result.complete)
        self.assertEqual(len(result.payload["items"][0]["images"]), 1)
        self.assertEqual(len(list(Path(self.temporary.name).rglob("*.jpg"))), 1)

    def test_selected_slots_leave_observed_ozon_baseline_url_untouched(self):
        payload = {
            "items": [{
                "offer_id": "safe-offer",
                "primary_image": "https://cdn.ozon.test/live-primary.jpg",
                "images": ["https://source.test/new.jpg"],
            }],
        }
        with patch(
            "services.marketplace_image_assets.download_public_image",
            return_value=_jpeg(),
        ) as download:
            result = materialize_product_payload(
                payload,
                config=self.config,
                secret_key=self.secret,
                selected_slots=["images:0"],
            )
        self.assertTrue(result.complete)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(
            result.payload["items"][0]["primary_image"],
            "https://cdn.ozon.test/live-primary.jpg",
        )
        self.assertIn(
            "/marketplace-assets/images/",
            result.payload["items"][0]["images"][0],
        )

    def test_small_or_html_source_never_becomes_public_asset(self):
        with patch(
            "services.marketplace_image_assets.download_public_image",
            return_value=_jpeg(width=200, height=200),
        ):
            result = materialize_product_payload(
                self._payload(["https://source.test/small.jpg"]),
                config=self.config,
                secret_key=self.secret,
            )
        self.assertFalse(result.complete)
        self.assertEqual(
            result.error_code,
            "media_source_resolution_too_small",
        )
        self.assertFalse(result.retryable)
        self.assertEqual(list(Path(self.temporary.name).rglob("*.jpg")), [])

        with patch(
            "services.marketplace_image_assets.download_public_image",
            side_effect=ImageLabError(
                "Источник фото вернул HTML без безопасного redirect"
            ),
        ):
            html = materialize_product_payload(
                self._payload(["https://source.test/challenge"]),
                config=self.config,
                secret_key=self.secret,
            )
        self.assertEqual(html.error_code, "media_source_invalid_image")
        self.assertFalse(html.retryable)

    def test_deadline_failure_is_retryable_and_bounded(self):
        with patch(
            "services.marketplace_image_assets.download_public_image",
            side_effect=ImageLabError(
                "Истекло время подготовки исходного фото"
            ),
        ):
            result = materialize_product_payload(
                self._payload(["https://source.test/slow.jpg"]),
                config=self.config,
                secret_key=self.secret,
                deadline=time.monotonic() + 1,
            )
        self.assertFalse(result.complete)
        self.assertEqual(
            result.error_code,
            "media_source_temporarily_unavailable",
        )
        self.assertTrue(result.retryable)

    def test_public_route_serves_only_signed_existing_digest(self):
        with patch(
            "services.marketplace_image_assets.download_public_image",
            return_value=_jpeg(),
        ):
            prepared = materialize_product_payload(
                self._payload(["https://source.test/one.jpg"]),
                config=self.config,
                secret_key=self.secret,
            )
        asset_url = prepared.payload["items"][0]["images"][0]
        parsed = urlsplit(asset_url)

        app = Flask(__name__)
        app.config.update(
            TESTING=True,
            SECRET_KEY=self.secret,
            **self.config,
        )
        register_marketplace_image_asset_routes(app)
        client = app.test_client()

        valid = client.get(parsed.path + "?" + parsed.query)
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.mimetype, "image/jpeg")
        self.assertIn("immutable", valid.headers["Cache-Control"])
        self.assertEqual(valid.headers["X-Content-Type-Options"], "nosniff")

        invalid = client.get(parsed.path + "?sig=" + ("0" * 32))
        self.assertEqual(invalid.status_code, 403)
        stored = next(Path(self.temporary.name).rglob("*.jpg"))
        stored.write_bytes(b"corrupt")
        corrupted = client.get(parsed.path + "?" + parsed.query)
        self.assertEqual(corrupted.status_code, 404)
        missing = client.get(
            "/marketplace-assets/images/" + ("f" * 64) + ".jpg?sig="
            + ("0" * 32)
        )
        self.assertEqual(missing.status_code, 403)

    def test_main_app_preserves_explicit_immutable_asset_cache_policy(self):
        import seller_platform

        app = seller_platform.app
        previous = {
            key: app.config.get(key)
            for key in (
                "TESTING",
                "SECRET_KEY",
                "PUBLIC_BASE_URL",
                "MARKETPLACE_IMAGE_ASSET_DIR",
            )
        }
        app.config.update(
            TESTING=True,
            SECRET_KEY=self.secret,
            PUBLIC_BASE_URL=self.config["PUBLIC_BASE_URL"],
            MARKETPLACE_IMAGE_ASSET_DIR=self.temporary.name,
        )
        try:
            with patch(
                "services.marketplace_image_assets.download_public_image",
                return_value=_jpeg(),
            ):
                prepared = materialize_product_payload(
                    self._payload(["https://source.test/one.jpg"]),
                    config=app.config,
                    secret_key=self.secret,
                )
            parsed = urlsplit(prepared.payload["items"][0]["images"][0])
            client = app.test_client()
            valid = client.get(parsed.path + "?" + parsed.query)
            invalid = client.get(parsed.path + "?sig=" + ("0" * 32))
        finally:
            app.config.update(previous)

        self.assertEqual(valid.status_code, 200)
        self.assertEqual(
            valid.headers["Cache-Control"],
            "public, max-age=31536000, immutable",
        )
        self.assertNotIn("Pragma", valid.headers)
        self.assertEqual(invalid.status_code, 403)
        self.assertIn("no-store", invalid.headers["Cache-Control"])

    def test_public_base_must_be_public_https(self):
        for base in (
            "",
            "http://seller.test",
            "https://127.0.0.1",
            "https://user:pass@seller.test",
        ):
            config = dict(self.config, PUBLIC_BASE_URL=base)
            with self.subTest(base=base), self.assertRaises(
                MarketplaceImageAssetError
            ):
                materialize_product_payload(
                    self._payload(["https://source.test/one.jpg"]),
                    config=config,
                    secret_key=self.secret,
                )


if __name__ == "__main__":
    unittest.main()
