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
    Seller,
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
        db.session.add_all([product1, product2])
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

    @mock.patch("routes.image_lab.render_template", return_value="ok")
    def test_page_bootstrap_contains_all_product_photo_slots(self, render):
        response = self.client.get("/image-lab")
        self.assertEqual(response.status_code, 200)
        products = render.call_args.kwargs["lab_products"]
        product = next(item for item in products if item["id"] == self.product1_id)
        self.assertEqual(product["photo_count"], 3)
        self.assertEqual(product["photos"][-1], {"index": 2, "label": "Фото 3"})

    def test_bool_product_id_is_rejected(self):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": True,
            "scene_key": "luxury",
            "custom_scene": "",
            "targets": [],
        })
        self.assertEqual(response.status_code, 400)

    @mock.patch.dict(os.environ, {"GEN_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    def test_create_is_scoped_to_current_seller(self, launch):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "seller_id": 999,
            "scene_key": "luxury",
            "custom_scene": "warm stone",
            "targets": [{"backend": "gen_api", "model": "flux-2"}],
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        experiment_id = response.get_json()["experiments"][0]["id"]
        experiment = db.session.get(ImageGenerationExperiment, experiment_id)
        self.assertEqual(experiment.seller_id, self.seller1_id)
        self.assertEqual(experiment.imported_product_id, self.product1_id)
        launch.assert_called_once()

    @mock.patch.dict(os.environ, {"GEN_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    def test_reference_set_persists_primary_roles_and_context(self, launch):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "scene_key": "luxury",
            "generation_mode": "reference_set",
            "generation_strategy": "reference_guided",
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
            "targets": [{"backend": "gen_api", "model": "flux-2"}],
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        item = response.get_json()["experiments"][0]
        self.assertEqual(item["composition_mode"], "reference_set")
        self.assertEqual(item["primary_photo_index"], 1)
        self.assertEqual(item["photo_roles"]["2"], "packaging")
        self.assertIn("identity evidence", item["prompt"])
        self.assertIn("Точный заголовок", item["prompt"])
        launch.assert_called_once()

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

    @mock.patch.dict(os.environ, {"GEN_API_KEY": "test"})
    @mock.patch("routes.image_lab.lab.launch_experiments")
    def test_each_photo_mode_creates_one_job_per_photo_and_target(self, launch):
        response = self.client.post("/image-lab/api/experiments", json={
            "product_id": self.product1_id,
            "scene_key": "luxury",
            "custom_scene": "",
            "generation_mode": "each",
            "photo_indices": [0, 2],
            "targets": [{"backend": "gen_api", "model": "flux-2"}],
        })
        self.assertEqual(response.status_code, 202, response.get_json())
        experiments = response.get_json()["experiments"]
        self.assertEqual(len(experiments), 2)
        self.assertEqual([item["photo_indices"] for item in experiments], [[0], [2]])
        self.assertTrue(all(item["composition_mode"] == "single" for item in experiments))
        launch.assert_called_once()

    @mock.patch.dict(os.environ, {"GEN_API_KEY": "test"})
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
            "targets": [{"backend": "gen_api", "model": "flux-2"}],
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

    @mock.patch.dict(os.environ, {"GEN_API_KEY": "test"})
    def test_angle_mode_rejects_missing_views_and_packaging_primary(self):
        base = {
            "product_id": self.product1_id,
            "scene_key": "luxury",
            "generation_mode": "angles",
            "generation_strategy": "angle_synthesis",
            "photo_indices": [0, 1],
            "targets": [{"backend": "gen_api", "model": "flux-2"}],
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

    @mock.patch.dict(os.environ, {"GEN_API_KEY": "test"})
    def test_collage_requires_two_valid_unique_photo_indices(self):
        for indices in ([0], [0, 0], [0, 99], [True, 1]):
            with self.subTest(indices=indices):
                response = self.client.post("/image-lab/api/experiments", json={
                    "product_id": self.product1_id,
                    "scene_key": "luxury",
                    "generation_mode": "collage",
                    "photo_indices": indices,
                    "targets": [{"backend": "gen_api", "model": "flux-2"}],
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
