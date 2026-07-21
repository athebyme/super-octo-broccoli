# -*- coding: utf-8 -*-
import json
import io
import os
import tempfile
import unittest
from unittest import mock

from PIL import Image

from flask import Flask
from flask_login import LoginManager

from models import (
    ImageGenerationExperiment,
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    Product,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from routes.image_lab import register_image_lab_routes
from services import image_lab_service as image_lab_service


class ImageLabRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Flask(__name__, template_folder=os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates"))
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="test",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            IMAGE_LAB_DATA_DIR=self.temp.name,
            MARKETPLACE_OZON_ENABLED=True,
        )
        db.init_app(self.app)
        login = LoginManager(self.app)
        login.user_loader(lambda user_id: db.session.get(User, int(user_id)))
        register_image_lab_routes(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        user1 = User(username="seller1", email="one@example.test", password_hash="x")
        user2 = User(username="seller2", email="two@example.test", password_hash="x")
        db.session.add_all([user1, user2])
        db.session.flush()
        seller1 = Seller(user_id=user1.id, company_name="One")
        seller2 = Seller(user_id=user2.id, company_name="Two")
        db.session.add_all([seller1, seller2])
        db.session.flush()
        wb_product = Product(
            seller_id=seller1.id,
            nm_id=40404,
            title="Товар WB с исторической галереей",
            photos_json=json.dumps([1, 2]),
        )
        db.session.add(wb_product)
        db.session.flush()
        product1 = ImportedProduct(
            seller_id=seller1.id,
            title="Товар 1",
            photo_urls=json.dumps([
                "https://cdn.example.test/1.png",
                "https://cdn.example.test/1-side.png",
                "https://cdn.example.test/1-back.png",
            ]),
        )
        product2 = ImportedProduct(
            seller_id=seller2.id,
            title="Товар 2",
            photo_urls=json.dumps(["https://cdn.example.test/2.png"]),
        )
        product3 = ImportedProduct(
            seller_id=seller1.id,
            title="Товар 3",
            photo_urls=json.dumps(["https://cdn.example.test/3.png"]),
        )
        wb_fallback_product = ImportedProduct(
            seller_id=seller1.id,
            product_id=wb_product.id,
            title=wb_product.title,
            photo_urls=None,
        )
        db.session.add_all([product1, product2, product3, wb_fallback_product])
        db.session.flush()
        ozon = Marketplace(
            name="Ozon",
            code="ozon",
            adapter_code="ozon",
            is_active=True,
        )
        db.session.add(ozon)
        db.session.flush()
        account1 = SellerMarketplaceAccount(
            seller_id=seller1.id,
            marketplace_id=ozon.id,
            external_account_id="synthetic-one",
            label="Ozon One",
            is_active=True,
            connection_status="connected",
        )
        account2 = SellerMarketplaceAccount(
            seller_id=seller2.id,
            marketplace_id=ozon.id,
            external_account_id="synthetic-two",
            label="Ozon Two",
            is_active=True,
            connection_status="connected",
        )
        db.session.add_all([account1, account2])
        db.session.flush()
        listing1 = MarketplaceListing(
            seller_id=seller1.id,
            marketplace_id=ozon.id,
            account_id=account1.id,
            imported_product_id=product1.id,
            offer_id="offer-one",
            external_product_id="101",
            title="Ozon Товар 1",
            normalized_status="active",
            media_json=json.dumps({
                "primary_image": "https://ozon.example.test/one.png",
                "images": ["https://ozon.example.test/one.png"],
            }),
            sync_fingerprint="1" * 64,
        )
        listing3 = MarketplaceListing(
            seller_id=seller1.id,
            marketplace_id=ozon.id,
            account_id=account1.id,
            imported_product_id=product3.id,
            offer_id="offer-three",
            external_product_id="303",
            title="Ozon Товар 3",
            normalized_status="active",
            sync_fingerprint="3" * 64,
        )
        foreign_listing = MarketplaceListing(
            seller_id=seller2.id,
            marketplace_id=ozon.id,
            account_id=account2.id,
            imported_product_id=product2.id,
            offer_id="offer-two",
            external_product_id="202",
            title="Ozon Товар 2",
            normalized_status="active",
            sync_fingerprint="2" * 64,
        )
        db.session.add_all([listing1, listing3, foreign_listing])
        db.session.flush()
        foreign = ImageGenerationExperiment(
            seller_id=seller2.id,
            imported_product_id=product2.id,
            backend="gen_api",
            model="flux-2",
            scene_key="luxury",
            prompt="background",
            prompt_sha256="a" * 64,
            status="completed",
            estimated_cost_rub=3.3,
        )
        db.session.add(foreign)
        db.session.commit()
        self.user1_id = user1.id
        self.seller1_id = seller1.id
        self.seller2_id = seller2.id
        self.product1_id = product1.id
        self.product3_id = product3.id
        self.wb_fallback_product_id = wb_fallback_product.id
        self.account1_id = account1.id
        self.listing1_id = listing1.id
        self.listing3_id = listing3.id
        self.foreign_listing_id = foreign_listing.id
        self.foreign_experiment_id = foreign.id
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.user1_id)
            session["_fresh"] = True

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.temp.cleanup()

    def test_foreign_experiment_is_not_visible(self):
        response = self.client.get(
            f"/image-lab/api/experiments/{self.foreign_experiment_id}")
        self.assertEqual(response.status_code, 404)

    def test_expired_session_api_response_is_json(self):
        with self.client.session_transaction() as session:
            session.clear()

        response = self.client.get(
            "/image-lab/api/analytics", follow_redirects=False)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(response.get_json(), {
            "success": False,
            "code": "auth_required",
            "error": "Сессия истекла. Войдите снова",
        })

    @mock.patch("routes.image_lab.render_template", return_value="ok")
    def test_page_bootstrap_contains_all_product_photo_slots(self, render):
        response = self.client.get("/image-lab")
        self.assertEqual(response.status_code, 200)
        products = render.call_args.kwargs["lab_products"]
        product = next(item for item in products if item["id"] == self.product1_id)
        self.assertEqual(product["photo_count"], 3)
        self.assertEqual(product["photos"][-1], {"index": 2, "label": "Фото 3"})
        self.assertEqual(
            [target["listing_id"] for target in product["marketplace_targets"]],
            [self.listing1_id],
        )
        self.assertNotIn("ozon.example.test", json.dumps(product))

    @mock.patch("routes.image_lab.render_template", return_value="ok")
    def test_recommended_exact_wb_gallery_is_visible_without_canonical_urls(self, render):
        recommendation = {
            "id": 91,
            "product_id": self.wb_fallback_product_id,
            "target_ready": True,
        }
        with mock.patch(
            "routes.image_lab.AdminSalesIntelligenceService.seller_recommendations",
            return_value=[recommendation],
        ):
            response = self.client.get("/image-lab")

        self.assertEqual(response.status_code, 200)
        products = render.call_args.kwargs["lab_products"]
        product = next(
            item for item in products
            if item["id"] == self.wb_fallback_product_id
        )
        self.assertEqual(product["photo_count"], 2)
        self.assertEqual(product["photos"][-1], {"index": 1, "label": "Фото 2"})

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    @mock.patch(
        "services.image_lab_service.exact_linked_wb_photo_urls",
        return_value=[
            "https://basket.test/wb-1.webp",
            "https://basket.test/wb-2.webp",
        ],
    )
    def test_confirmed_wb_experiment_persists_exact_gallery_urls(
        self,
        fallback_urls,
        launch,
    ):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.wb_fallback_product_id,
            "scene_key": "luxury",
            "custom_scene": "",
            "targets": [{
                "backend": "openrouter",
                "model": "google/gemini-3.1-flash-lite-image",
            }],
        })

        self.assertEqual(response.status_code, 202, response.get_json())
        product = db.session.get(ImportedProduct, self.wb_fallback_product_id)
        self.assertEqual(json.loads(product.photo_urls), [
            "https://basket.test/wb-1.webp",
            "https://basket.test/wb-2.webp",
        ])
        fallback_urls.assert_called_once()
        launch.assert_called_once()

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    def test_exact_marketplace_target_is_persisted_without_attachment(self, launch):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "scene_key": "luxury",
            "custom_scene": "",
            "targets": [{
                "backend": "openrouter",
                "model": "google/gemini-3.1-flash-lite-image",
            }],
            "marketplace_target": {
                "entity_kind": "marketplace_listing",
                "listing_id": self.listing1_id,
                "marketplace_code": "ozon",
                "account_id": self.account1_id,
            },
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        result = response.get_json()["experiments"][0]
        experiment = db.session.get(ImageGenerationExperiment, result["id"])
        self.assertEqual(experiment.marketplace_listing_id, self.listing1_id)
        self.assertEqual(result["marketplace_target"]["listing_id"], self.listing1_id)
        self.assertFalse(
            result["marketplace_target"]["constraints"]["automatic_attachment"]
        )
        self.assertNotIn("ozon.example.test", json.dumps(result))
        launch.assert_called_once()

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    def test_foreign_or_wrong_product_target_is_rejected_before_launch(self, launch):
        for listing_id in (self.foreign_listing_id, self.listing3_id):
            with self.subTest(listing_id=listing_id):
                response = self.client.post("/image-lab/api/experiments", json={
                    "product_id": self.product1_id,
                    "scene_key": "luxury",
                    "custom_scene": "",
                    "targets": [{
                        "backend": "openrouter",
                        "model": "google/gemini-3.1-flash-lite-image",
                    }],
                    "marketplace_target": {
                        "entity_kind": "marketplace_listing",
                        "listing_id": listing_id,
                        "marketplace_code": "ozon",
                        "account_id": self.account1_id,
                    },
                })
                self.assertEqual(response.status_code, 400, response.get_json())
        launch.assert_not_called()

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    def test_bare_listing_target_is_rejected_before_launch(self, launch):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "scene_key": "luxury",
            "custom_scene": "",
            "targets": [{
                "backend": "openrouter",
                "model": "google/gemini-3.1-flash-lite-image",
            }],
            "marketplace_target": {
                "entity_kind": "marketplace_listing",
                "listing_id": self.listing1_id,
            },
        })
        self.assertEqual(response.status_code, 400, response.get_json())
        launch.assert_not_called()

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    def test_disabled_feature_rejects_target_but_keeps_legacy_lab(self, launch):
        self.app.config["MARKETPLACE_OZON_ENABLED"] = False
        target_response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "scene_key": "luxury",
            "custom_scene": "",
            "targets": [{
                "backend": "openrouter",
                "model": "google/gemini-3.1-flash-lite-image",
            }],
            "marketplace_target": {
                "entity_kind": "marketplace_listing",
                "listing_id": self.listing1_id,
            },
        })
        self.assertEqual(target_response.status_code, 404)
        legacy_response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "scene_key": "luxury",
            "custom_scene": "",
            "targets": [{
                "backend": "openrouter",
                "model": "google/gemini-3.1-flash-lite-image",
            }],
        })
        self.assertEqual(legacy_response.status_code, 202, legacy_response.get_json())
        launch.assert_called_once()

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    def test_target_is_revalidated_before_any_provider_work(self):
        with mock.patch("routes.image_lab.lab.launch_experiments"):
            response = self.client.post("/image-lab/api/experiments", json={
                "product_id": self.product1_id,
                "scene_key": "luxury",
                "custom_scene": "",
                "targets": [{
                    "backend": "openrouter",
                    "model": "google/gemini-3.1-flash-lite-image",
                }],
                "marketplace_target": {
                    "entity_kind": "marketplace_listing",
                    "listing_id": self.listing1_id,
                    "marketplace_code": "ozon",
                    "account_id": self.account1_id,
                },
            })
        self.assertEqual(response.status_code, 202, response.get_json())
        experiment_id = response.get_json()["experiments"][0]["id"]
        account = db.session.get(SellerMarketplaceAccount, self.account1_id)
        account.is_active = False
        db.session.commit()
        with mock.patch(
            "services.image_lab_service._generate_provider_output"
        ) as generate:
            image_lab_service._run_experiment(self.app, experiment_id)
        generate.assert_not_called()
        experiment = db.session.get(ImageGenerationExperiment, experiment_id)
        self.assertEqual(experiment.status, "failed")
        self.assertIn("Неактивный кабинет", experiment.error)

    def test_bool_product_id_is_rejected(self):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": True,
            "scene_key": "luxury",
            "custom_scene": "",
            "targets": [],
        })
        self.assertEqual(response.status_code, 400)

    def test_non_string_target_is_json_validation_error(self):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "scene_key": "luxury",
            "targets": [{"backend": ["openrouter"], "model": True}],
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])
        self.assertIn("строками", response.get_json()["error"])

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    def test_create_is_scoped_to_current_seller(self, launch):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "seller_id": 999,
            "scene_key": "luxury",
            "custom_scene": "warm stone",
            "targets": [{
                "backend": "openrouter",
                "model": "google/gemini-3.1-flash-lite-image",
            }],
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        experiment_id = response.get_json()["experiments"][0]["id"]
        experiment = db.session.get(ImageGenerationExperiment, experiment_id)
        self.assertEqual(experiment.seller_id, self.seller1_id)
        self.assertEqual(experiment.imported_product_id, self.product1_id)
        self.assertEqual(experiment.generation_strategy, "native_scene")
        self.assertNotIn("Visual identity context follows", experiment.prompt)
        launch.assert_called_once()

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    def test_reference_set_persists_primary_roles_and_context(self, launch):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "scene_key": "luxury",
            "generation_mode": "reference_set",
            "generation_strategy": "native_scene",
            "photo_indices": [0, 1, 2],
            "primary_photo_index": 1,
            "photo_roles": [
                {"index": 0, "role": "angle"},
                {"index": 1, "role": "angle"},
                {"index": 2, "role": "packaging"},
            ],
            "include_product_context": True,
            "overlay": {"title": "Точный заголовок", "subtitle": "Характеристика"},
            "additional_prompt": "more space above",
            "targets": [{
                "backend": "openrouter",
                "model": "google/gemini-3.1-flash-lite-image",
            }],
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        item = response.get_json()["experiments"][0]
        self.assertEqual(item["composition_mode"], "reference_set")
        self.assertEqual(item["primary_photo_index"], 1)
        self.assertEqual(item["photo_roles"]["2"], "packaging")
        self.assertIn("identity evidence", item["prompt"])
        self.assertIn("Точный заголовок", item["prompt"])
        launch.assert_called_once()

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    def test_native_scene_persists_raw_reference_contract(self, launch):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "scene_key": "spa",
            "generation_mode": "reference_set",
            "generation_strategy": "native_scene",
            "photo_indices": [0, 1, 2],
            "primary_photo_index": 1,
            "photo_roles": [
                {"index": 0, "role": "detail"},
                {"index": 1, "role": "angle"},
                {"index": 2, "role": "packaging"},
            ],
            "targets": [{
                "backend": "openrouter",
                "model": "google/gemini-3.1-flash-lite-image",
            }],
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        item = response.get_json()["experiments"][0]
        self.assertEqual(item["generation_strategy"], "native_scene")
        self.assertEqual(item["composition_mode"], "reference_set")
        self.assertEqual(item["primary_photo_index"], 1)
        self.assertIn("native image-to-image", item["prompt"])
        self.assertIn("role=packaging", item["prompt"])
        launch.assert_called_once()

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    def test_native_scene_rejects_collage_and_packaging_primary(self):
        base = {
            "product_id": self.product1_id,
            "scene_key": "spa",
            "generation_strategy": "native_scene",
            "photo_indices": [0, 1],
            "targets": [{
                "backend": "openrouter",
                "model": "google/gemini-3.1-flash-lite-image",
            }],
        }
        collage = self.client.post("/image-lab/api/experiments", json={
            **base,
            "generation_mode": "collage",
        })
        self.assertEqual(collage.status_code, 400)
        packaging = self.client.post("/image-lab/api/experiments", json={
            **base,
            "generation_mode": "reference_set",
            "primary_photo_index": 1,
            "photo_roles": [
                {"index": 0, "role": "angle"},
                {"index": 1, "role": "packaging"},
            ],
        })
        self.assertEqual(packaging.status_code, 400)

    def test_png_watermark_upload_and_tenant_scoped_preview(self):
        logo = io.BytesIO()
        Image.new("RGBA", (80, 40), (255, 120, 0, 180)).save(logo, format="PNG")
        with mock.patch.dict(os.environ, {"IMAGE_LAB_DATA_DIR": self.temp.name}):
            response = self.client.post(
                "/image-lab/api/watermarks",
                data={"file": (io.BytesIO(logo.getvalue()), "logo.png")},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 201, response.get_json())
            watermark_id = response.get_json()["watermark"]["id"]
            preview = self.client.get(f"/image-lab/api/watermarks/{watermark_id}")
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.mimetype, "image/png")
            preview.close()

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    def test_each_photo_mode_creates_one_job_per_photo_and_target(self, launch):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "scene_key": "luxury",
            "custom_scene": "",
            "generation_mode": "each",
            "photo_indices": [0, 2],
            "targets": [{"backend": "openrouter", "model": "google/gemini-3.1-flash-lite-image"}],
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        experiments = response.get_json()["experiments"]
        self.assertEqual(len(experiments), 2)
        self.assertEqual([item["photo_indices"] for item in experiments], [[0], [2]])
        self.assertTrue(all(item["composition_mode"] == "single" for item in experiments))
        launch.assert_called_once()

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    def test_angle_mode_creates_one_research_job_per_requested_view(self, launch):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "scene_key": "luxury",
            "generation_mode": "angles",
            "generation_strategy": "angle_synthesis",
            "photo_indices": [0, 1, 2],
            "primary_photo_index": 1,
            "photo_roles": [
                {"index": 0, "role": "angle"},
                {"index": 1, "role": "angle"},
                {"index": 2, "role": "packaging"},
            ],
            "requested_views": ["back", "three_quarter_right"],
            "targets": [{"backend": "openrouter", "model": "google/gemini-3.1-flash-image"}],
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        experiments = response.get_json()["experiments"]
        self.assertEqual(len(experiments), 2)
        self.assertEqual(
            [item["requested_view"] for item in experiments],
            ["back", "three_quarter_right"],
        )
        self.assertTrue(all(item["composition_mode"] == "angles" for item in experiments))
        self.assertTrue(all(item["generation_strategy"] == "angle_synthesis" for item in experiments))
        self.assertIn("role=packaging", experiments[0]["prompt"])
        self.assertIn("rear view", experiments[0]["prompt"])
        launch.assert_called_once()

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    def test_angle_mode_rejects_missing_views_and_packaging_primary(self):
        base = {
            "product_id": self.product1_id,
            "scene_key": "luxury",
            "generation_mode": "angles",
            "generation_strategy": "angle_synthesis",
            "photo_indices": [0, 1],
            "targets": [{"backend": "openrouter", "model": "google/gemini-3.1-flash-image"}],
        }
        missing = self.client.post("/image-lab/api/experiments", json=base)
        self.assertEqual(missing.status_code, 400)
        packaging = self.client.post("/image-lab/api/experiments", json={
            **base,
            "requested_views": ["back"],
            "primary_photo_index": 1,
            "photo_roles": [
                {"index": 0, "role": "angle"},
                {"index": 1, "role": "packaging"},
            ],
        })
        self.assertEqual(packaging.status_code, 400)

    def test_angle_finalize_never_recomposes_or_becomes_publishable(self):
        experiment = ImageGenerationExperiment(
            seller_id=self.seller1_id,
            imported_product_id=self.product1_id,
            backend="gen_api",
            model="flux-2",
            scene_key="luxury",
            generation_strategy="angle_synthesis",
            composition_mode="angles",
            source_photo_indices_json="[0]",
            source_photo_roles_json='{"0":"angle"}',
            primary_photo_index=0,
            requested_view="back",
            prompt="novel view",
            prompt_sha256="f" * 64,
            status="finalizing",
            estimated_cost_rub=3.3,
        )
        db.session.add(experiment)
        db.session.commit()
        source = io.BytesIO()
        output = io.BytesIO()
        Image.effect_noise((900, 1200), 40).convert("RGB").save(output, format="PNG")
        Image.new("RGB", (100, 100), "red").save(source, format="PNG")
        product = db.session.get(ImportedProduct, self.product1_id)
        with mock.patch.dict(os.environ, {"IMAGE_LAB_DATA_DIR": self.temp.name}), mock.patch(
            "services.image_lab_service._experiment_sources",
            return_value=(product, [0], [source.getvalue()], "angles", 0, {"0": "angle"}),
        ), mock.patch(
            "services.image_lab_service._compose_experiment_foreground"
        ) as compose, mock.patch(
            "services.image_lab_service.evaluate_background_text",
            return_value={"checked": True, "pass": True, "detected_text": ""},
        ):
            image_lab_service._finalize_experiment(experiment, output.getvalue(), 1.2)
        compose.assert_not_called()
        quality = json.loads(experiment.quality_json)
        self.assertEqual(quality["status"], "review_required")
        self.assertFalse(quality["publishable"])
        self.assertIsNone(quality["identity_pass"])

    def test_masked_and_native_finalize_never_add_second_foreground(self):
        product = db.session.get(ImportedProduct, self.product1_id)
        source = io.BytesIO()
        output = io.BytesIO()
        Image.new("RGB", (100, 100), "red").save(source, format="PNG")
        Image.effect_noise((900, 1200), 40).convert("RGB").save(output, format="PNG")
        experiments = []
        for strategy in ("reference_guided", "native_scene"):
            experiment = ImageGenerationExperiment(
                seller_id=self.seller1_id,
                imported_product_id=self.product1_id,
                backend="aitunnel",
                model="gpt-image-2",
                scene_key="spa",
                generation_strategy=strategy,
                composition_mode="single",
                source_photo_indices_json="[0]",
                source_photo_roles_json='{"0":"angle"}',
                primary_photo_index=0,
                prompt="one native product",
                prompt_sha256=("d" if strategy == "reference_guided" else "e") * 64,
                status="finalizing",
                estimated_cost_rub=1.53,
                composite_metadata_json=json.dumps({
                    "identity_mode": "generative_edit",
                    "generation_strategy": strategy,
                }),
            )
            db.session.add(experiment)
            experiments.append(experiment)
        db.session.commit()
        with mock.patch.dict(os.environ, {"IMAGE_LAB_DATA_DIR": self.temp.name}), mock.patch(
            "services.image_lab_service._experiment_sources",
            return_value=(product, [0], [source.getvalue()], "single", 0, {"0": "angle"}),
        ), mock.patch(
            "services.image_lab_service._compose_experiment_foreground"
        ) as compose, mock.patch(
            "services.image_lab_service.evaluate_background_text",
            return_value={"checked": True, "pass": True, "detected_text": ""},
        ):
            for experiment in experiments:
                image_lab_service._finalize_experiment(
                    experiment, output.getvalue(), 1.2
                )
        compose.assert_not_called()
        for experiment in experiments:
            quality = json.loads(experiment.quality_json)
            metadata = json.loads(experiment.composite_metadata_json)
            self.assertEqual(quality["status"], "review_required")
            self.assertFalse(quality["publishable"])
            self.assertIsNone(quality["identity_pass"])
            self.assertFalse(metadata["local_foreground_overlay"])
            self.assertEqual(metadata["identity_mode"], "generative_edit")
            with open(os.path.join(self.temp.name, experiment.background_path), "rb") as raw:
                provider_artifact = raw.read()
            with open(os.path.join(self.temp.name, experiment.final_path), "rb") as raw:
                final_artifact = raw.read()
            self.assertEqual(final_artifact, provider_artifact)

    def test_native_model_input_is_raw_primary_without_mask(self):
        experiment = ImageGenerationExperiment(
            seller_id=self.seller1_id,
            imported_product_id=self.product1_id,
            backend="aitunnel",
            model="gpt-image-2",
            scene_key="spa",
            generation_strategy="native_scene",
            composition_mode="reference_set",
            source_photo_indices_json="[0,1]",
            source_photo_roles_json='{"0":"detail","1":"angle"}',
            primary_photo_index=1,
            prompt="native",
            prompt_sha256="9" * 64,
            status="running",
            estimated_cost_rub=1.53,
        )
        db.session.add(experiment)
        db.session.commit()
        first = io.BytesIO()
        primary = io.BytesIO()
        Image.new("RGB", (80, 90), "red").save(first, format="PNG")
        Image.new("RGB", (90, 80), "blue").save(primary, format="PNG")
        product = db.session.get(ImportedProduct, self.product1_id)
        with mock.patch.dict(os.environ, {"IMAGE_LAB_DATA_DIR": self.temp.name}), mock.patch(
            "services.image_lab_service._experiment_sources",
            return_value=(
                product,
                [0, 1],
                [first.getvalue(), primary.getvalue()],
                "reference_set",
                1,
                {"0": "detail", "1": "angle"},
            ),
        ):
            model_input, additional, mask_bytes = (
                image_lab_service._prepare_native_model_input(experiment)
            )
        self.assertEqual(model_input, primary.getvalue())
        self.assertEqual(additional, [first.getvalue()])
        self.assertIsNone(mask_bytes)
        metadata = json.loads(experiment.composite_metadata_json)
        self.assertTrue(metadata["raw_primary_photo_sent"])
        self.assertFalse(metadata["protection_mask_used"])
        self.assertFalse(metadata["local_foreground_overlay"])

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"})
    def test_collage_requires_two_valid_unique_photo_indices(self):
        for indices in ([0], [0, 0], [0, 99], [True, 1]):
            with self.subTest(indices=indices):
                response = self.client.post("/image-lab/api/experiments", json={
                    "product_id": self.product1_id,
                    "scene_key": "luxury",
                    "generation_mode": "collage",
                    "photo_indices": indices,
                    "targets": [{"backend": "openrouter", "model": "google/gemini-3.1-flash-lite-image"}],
                })
                self.assertEqual(response.status_code, 400)

    @mock.patch("routes.image_lab.lab.fetch_original_product_bytes")
    def test_original_endpoint_passes_selected_index_and_preview_mode(self, fetch):
        fetch.return_value = b"not-an-image-but-route-contract-isolated"
        response = self.client.get(
            f"/image-lab/api/products/{self.product1_id}/original?photo_index=2&preview=1"
        )
        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[1], 2)
        self.assertTrue(fetch.call_args.kwargs["prefer_preview"])

    def test_rating_validation_and_tenant_scope(self):
        own = ImageGenerationExperiment(
            seller_id=self.seller1_id,
            imported_product_id=self.product1_id,
            backend="gen_api",
            model="flux-2",
            scene_key="luxury",
            prompt="background",
            prompt_sha256="b" * 64,
            status="completed",
            estimated_cost_rub=3.3,
        )
        db.session.add(own)
        db.session.commit()
        bad = self.client.post(
            f"/image-lab/api/experiments/{own.id}/rating",
            json={"rating": 7, "tags": [], "comment": ""},
        )
        self.assertEqual(bad.status_code, 400)
        good = self.client.post(
            f"/image-lab/api/experiments/{own.id}/rating",
            json={"rating": 5, "tags": ["product_preserved"], "comment": "ok"},
        )
        self.assertEqual(good.status_code, 200)
        analytics = self.client.get("/image-lab/api/analytics").get_json()
        variant = next(
            row for row in analytics["variants"]
            if row["backend"] == "gen_api" and row["model"] == "flux-2"
        )
        self.assertEqual(variant["human_accepted"], 1)
        self.assertEqual(variant["accepted"], 1)
        foreign = self.client.post(
            f"/image-lab/api/experiments/{self.foreign_experiment_id}/rating",
            json={"rating": 5, "tags": [], "comment": ""},
        )
        self.assertEqual(foreign.status_code, 404)

    def test_csv_export_is_tenant_scoped(self):
        own = ImageGenerationExperiment(
            seller_id=self.seller1_id,
            imported_product_id=self.product1_id,
            backend="gen_api",
            model="flux-2",
            scene_key="luxury",
            prompt="background",
            prompt_sha256="e" * 64,
            status="completed",
            estimated_cost_rub=3.3,
        )
        db.session.add(own)
        db.session.commit()
        response = self.client.get("/image-lab/api/export.csv")
        text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("e" * 64, text)
        self.assertNotIn("a" * 64, text)
        self.assertIn("attachment;", response.headers["Content-Disposition"])

    def test_cancel_is_atomic_and_tenant_scoped(self):
        own = ImageGenerationExperiment(
            seller_id=self.seller1_id,
            imported_product_id=self.product1_id,
            backend="gen_api",
            model="flux-2",
            scene_key="luxury",
            prompt="background",
            prompt_sha256="c" * 64,
            status="queued",
            estimated_cost_rub=3.3,
        )
        db.session.add(own)
        db.session.commit()
        response = self.client.post(
            f"/image-lab/api/experiments/{own.id}/cancel", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["experiment"]["status"], "cancelled")
        repeated = self.client.post(
            f"/image-lab/api/experiments/{own.id}/cancel", json={})
        self.assertEqual(repeated.status_code, 409)
        foreign = self.client.post(
            f"/image-lab/api/experiments/{self.foreign_experiment_id}/cancel", json={})
        self.assertEqual(foreign.status_code, 404)


if __name__ == "__main__":
    unittest.main()
