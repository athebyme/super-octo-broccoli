# -*- coding: utf-8 -*-
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from services.image_lab_service import (
    _generate_provider_output,
    ImageLabError,
    build_experiment_prompt,
    capabilities,
    download_public_image,
    fetch_original_product_bytes,
    photo_entries,
    openrouter_balance_usd,
    validate_photo_roles,
    validate_photo_indices,
    validate_requested_views,
    validate_target,
)


class ImageLabPromptTests(unittest.TestCase):
    @staticmethod
    def _png(color="red"):
        output = io.BytesIO()
        Image.new("RGB", (100, 100), color).save(output, format="PNG")
        return output.getvalue()

    def test_custom_scene_is_wrapped_in_reference_contract(self):
        prompt = build_experiment_prompt(
            "luxury",
            "warm stone and soft side light",
            generation_strategy="reference_guided",
        )
        self.assertIn("warm stone", prompt)
        self.assertIn("no people", prompt.lower())
        self.assertIn("do not generate text", prompt.lower())
        self.assertIn("preserve the complete foreground", prompt.lower())

    def test_default_prompt_is_native_and_background_is_explicit(self):
        default_prompt = build_experiment_prompt("luxury", "warm stone")
        background_prompt = build_experiment_prompt(
            "luxury", "warm stone", generation_strategy="background_only"
        )
        self.assertIn("native image-to-image", default_prompt)
        self.assertIn("exactly one", default_prompt)
        self.assertIn("requires human identity review", default_prompt)
        self.assertIn("nothing in the middle", background_prompt)

    def test_background_control_keeps_empty_center_contract(self):
        prompt = build_experiment_prompt(
            "luxury", "warm stone", generation_strategy="background_only"
        )
        self.assertIn("nothing in the middle", prompt)

    def test_angle_prompt_declares_research_and_hidden_geometry_contract(self):
        prompt = build_experiment_prompt(
            "luxury", "warm stone", generation_strategy="angle_synthesis"
        )
        self.assertIn("novel-view", prompt)
        self.assertIn("requires human review", prompt)
        self.assertIn("exactly one product", prompt)

    def test_product_or_text_instruction_rejected(self):
        for prompt in (
            "add product in center",
            "крупная надпись СКИДКА",
            "woman near the table",
        ):
            with self.subTest(prompt=prompt), self.assertRaises(ImageLabError):
                build_experiment_prompt("luxury", prompt)

    def test_unknown_scene_rejected(self):
        with self.assertRaises(ImageLabError):
            build_experiment_prompt("unknown", "")

    def test_backend_requires_server_side_secret(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImageLabError):
                validate_target(
                    "openrouter", "google/gemini-3.1-flash-lite-image"
                )
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}, clear=True):
            self.assertEqual(
                validate_target(
                    "openrouter", "google/gemini-3.1-flash-lite-image"
                ),
                3.3,
            )

    @mock.patch("services.image_lab_service.requests.get")
    def test_openrouter_balance_is_bounded_and_hides_key(self, get):
        get.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "data": {"total_credits": 58.0, "total_usage": 48.49},
                "unrelated": "ignored",
            },
        )
        with mock.patch.dict(
            os.environ, {
                "OPENROUTER_API_KEY": "server-secret",
                "AI_PROXY": "http://proxy.test:8080",
            }, clear=True,
        ):
            self.assertAlmostEqual(openrouter_balance_usd(), 9.51)

        self.assertEqual(
            get.call_args.args[0],
            "https://openrouter.ai/api/v1/credits",
        )
        self.assertNotIn("server-secret", get.call_args.args[0])
        self.assertEqual(
            get.call_args.kwargs["proxies"]["https"],
            "http://proxy.test:8080",
        )

    def test_openrouter_models_are_the_only_visible_targets(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}, clear=True):
            config = capabilities()
            self.assertEqual(
                validate_target(
                    "openrouter",
                    "google/gemini-3.1-flash-lite-image",
                    "native_scene",
                ),
                3.3,
            )
            self.assertEqual(
                config["policy"]["default_target"],
                {
                    "backend": "openrouter",
                    "model": "google/gemini-3.1-flash-lite-image",
                },
            )
            self.assertEqual(config["policy"]["default_model_input"], "native_scene")
            self.assertEqual([item["id"] for item in config["backends"]], ["openrouter"])
            model = next(
                model
                for backend in config["backends"]
                if backend["id"] == "openrouter"
                for model in backend["models"]
                if model["id"] == "google/gemini-3.1-flash-lite-image"
            )
            self.assertTrue(model["supports_reference"])
            self.assertEqual(model["resolution"], "1K")
            self.assertNotIn("reference_guided", model["supported_strategies"])
            gpt_image = next(
                model
                for backend in config["backends"]
                if backend["id"] == "openrouter"
                for model in backend["models"]
                if model["id"] == "openai/gpt-image-2"
            )
            self.assertEqual(gpt_image["quality"], "medium")
            self.assertEqual(gpt_image["profile_label"], "Medium · автоформат")
            self.assertEqual(gpt_image["cost_rub"], 4.5)
            self.assertEqual(
                validate_target(
                    "openrouter",
                    "openai/gpt-image-2",
                    "native_scene",
                    reference_count=10,
                ),
                4.5,
            )

    def test_legacy_and_masked_targets_are_rejected_for_new_runs(self):
        with mock.patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "secret",
            "AITUNNEL_API_KEY": "legacy-secret",
        }, clear=True):
            with self.assertRaises(ImageLabError):
                validate_target("aitunnel", "gpt-image-2", "native_scene")
            with self.assertRaises(ImageLabError):
                validate_target(
                    "openrouter",
                    "google/gemini-3.1-flash-lite-image",
                    "reference_guided",
                )

    @mock.patch("services.image_lab_service.canonicalize_image", side_effect=lambda value: value)
    @mock.patch(
        "services.image_lab_service._prepare_native_model_input",
        return_value=(b"raw-primary", [], None),
    )
    @mock.patch("services.image_generation_service.ImageGenerationService")
    @mock.patch("services.image_generation_service.ImageGenerationConfig.from_env")
    def test_native_scene_uses_openrouter_model_and_resolution(
        self, from_env, service_class, prepare_native, canonicalize
    ):
        from_env.return_value = SimpleNamespace(
            timeout=120,
            openrouter_model="",
            openrouter_resolution="",
        )
        service = service_class.return_value
        service.edit_image.return_value = (True, b"provider-output", "")
        experiment = SimpleNamespace(
            backend="openrouter",
            model="google/gemini-3.1-flash-lite-image",
            generation_strategy="native_scene",
            prompt="native scene",
        )

        self.assertEqual(_generate_provider_output(experiment), b"provider-output")
        prepare_native.assert_called_once_with(experiment)
        self.assertIsNone(service.edit_image.call_args.kwargs["input_fidelity"])
        self.assertIsNone(service.edit_image.call_args.kwargs["quality"])
        self.assertEqual(
            from_env.return_value.openrouter_model,
            "google/gemini-3.1-flash-lite-image",
        )
        self.assertEqual(from_env.return_value.openrouter_resolution, "1K")

    def test_angle_synthesis_and_grok_reference_limit(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}, clear=True):
            self.assertEqual(
                validate_target(
                    "openrouter",
                    "google/gemini-3.1-flash-image",
                    "angle_synthesis",
                    reference_count=10,
                ),
                8.5,
            )
            with self.assertRaises(ImageLabError):
                validate_target(
                    "openrouter",
                    "x-ai/grok-imagine-image-quality",
                    "angle_synthesis",
                    reference_count=4,
                )
            with self.assertRaises(ImageLabError):
                validate_target(
                    "openrouter",
                    "openai/gpt-image-2",
                    "angle_synthesis",
                    reference_count=11,
                )

    @mock.patch("services.image_lab_service.canonicalize_image", side_effect=lambda value: value)
    @mock.patch(
        "services.image_lab_service._prepare_native_model_input",
        return_value=(b"raw-primary", [], None),
    )
    @mock.patch("services.image_generation_service.ImageGenerationService")
    @mock.patch("services.image_generation_service.ImageGenerationConfig.from_env")
    def test_gpt_image_2_uses_fixed_medium_profile(
        self, from_env, service_class, prepare_native, canonicalize
    ):
        from_env.return_value = SimpleNamespace(
            timeout=120,
            openrouter_model="",
            openrouter_resolution="1K",
            openrouter_aspect_ratio="3:4",
            openrouter_quality=None,
            openrouter_background=None,
        )
        service_class.return_value.edit_image.return_value = (
            True, b"provider-output", "",
        )
        experiment = SimpleNamespace(
            backend="openrouter",
            model="openai/gpt-image-2",
            generation_strategy="native_scene",
            prompt="native scene",
        )

        self.assertEqual(_generate_provider_output(experiment), b"provider-output")
        prepare_native.assert_called_once_with(experiment)
        self.assertEqual(from_env.return_value.openrouter_model, "openai/gpt-image-2")
        self.assertEqual(from_env.return_value.openrouter_resolution, "")
        self.assertEqual(from_env.return_value.openrouter_aspect_ratio, "")
        self.assertEqual(from_env.return_value.openrouter_quality, "medium")
        self.assertEqual(from_env.return_value.openrouter_background, "opaque")

    @mock.patch("services.image_lab_service.requests.Session.get")
    def test_private_source_url_is_rejected_before_request(self, get):
        with self.assertRaises(ImageLabError):
            download_public_image("http://127.0.0.1/secret.png")
        get.assert_not_called()

    @mock.patch("services.image_lab_service._assert_public_http_url")
    @mock.patch("services.image_lab_service.requests.Session.get")
    def test_bounded_html_meta_refresh_is_followed_and_revalidated(self, get, validate):
        redirect = mock.MagicMock()
        redirect.status_code = 200
        redirect.headers = {"Content-Type": "text/html"}
        redirect.iter_content.return_value = [
            b'<meta http-equiv="Refresh" content="0; URL=https://cdn.test/final.png">'
        ]
        image = mock.MagicMock()
        image.status_code = 200
        image.headers = {"Content-Type": "image/png"}
        payload = self._png()
        image.iter_content.return_value = [payload]
        get.side_effect = [redirect, image]

        self.assertEqual(
            download_public_image("https://source.test/photo", timeout=(1, 1)),
            payload,
        )
        self.assertEqual(
            [call.args[0] for call in validate.call_args_list],
            ["https://source.test/photo", "https://cdn.test/final.png"],
        )

    def test_all_photo_slots_are_normalized_without_exposing_invalid_items(self):
        entries = photo_entries(json.dumps([
            {"original": "https://cdn.test/1.jpg", "blur": "https://cdn.test/1-small.jpg"},
            "https://cdn.test/2.jpg",
            {"unexpected": "https://cdn.test/ignored.jpg"},
        ]))
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["candidates"][0], "https://cdn.test/1.jpg")
        self.assertEqual(entries[0]["preview_candidates"][0], "https://cdn.test/1-small.jpg")

    def test_photo_indices_are_strict_unique_and_bounded(self):
        self.assertEqual(validate_photo_indices([2, 0], 3), [2, 0])
        for invalid in ([True], [1.0], ["1"], [3], [1, 1], []):
            with self.subTest(invalid=invalid), self.assertRaises(ImageLabError):
                validate_photo_indices(invalid, 3)

    def test_photo_roles_are_strict_and_default_to_angle(self):
        self.assertEqual(
            validate_photo_roles([{"index": 1, "role": "packaging"}], [0, 1]),
            {"0": "angle", "1": "packaging"},
        )
        for invalid in (
            [{"index": True, "role": "angle"}],
            [{"index": 0, "role": "unknown"}],
            [{"index": 0, "role": "angle"}, {"index": 0, "role": "detail"}],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ImageLabError):
                validate_photo_roles(invalid, [0, 1])

    def test_requested_angle_views_are_strict_unique_and_bounded(self):
        self.assertEqual(
            validate_requested_views(["front", "three_quarter_right"]),
            ["front", "three_quarter_right"],
        )
        for invalid in (None, [], ["unknown"], ["front", "front"], [True]):
            with self.subTest(invalid=invalid), self.assertRaises(ImageLabError):
                validate_requested_views(invalid)

    @mock.patch("services.image_lab_service.download_public_image")
    def test_preview_prefers_blur_and_original_falls_back(self, download):
        product = SimpleNamespace(
            id=7,
            supplier_product=None,
            photo_urls=json.dumps([{
                "original": "https://cdn.test/full.jpg",
                "sexoptovik": "https://cdn.test/fallback.jpg",
                "blur": "https://cdn.test/preview.jpg",
            }]),
        )
        download.return_value = self._png()
        fetch_original_product_bytes(product, 0, prefer_preview=True)
        self.assertEqual(download.call_args.args[0], "https://cdn.test/fallback.jpg")

        download.reset_mock()
        product.photo_urls = json.dumps([{
            "original": "https://cdn.test/full.jpg",
            "processed": "https://cdn.test/processed.jpg",
        }])
        download.side_effect = [ImageLabError("timeout"), self._png("blue")]
        data = fetch_original_product_bytes(product, 0)
        self.assertTrue(data)
        self.assertEqual(
            [call.args[0] for call in download.call_args_list],
            ["https://cdn.test/full.jpg", "https://cdn.test/processed.jpg"],
        )


if __name__ == "__main__":
    unittest.main()
