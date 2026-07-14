# -*- coding: utf-8 -*-
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from services.image_lab_service import (
    ImageLabError,
    build_experiment_prompt,
    capabilities,
    download_public_image,
    fetch_original_product_bytes,
    photo_entries,
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

    def test_custom_scene_is_wrapped_in_reference_contract_by_default(self):
        prompt = build_experiment_prompt("luxury", "warm stone and soft side light")
        self.assertIn("warm stone", prompt)
        self.assertIn("no people", prompt.lower())
        self.assertIn("do not generate text", prompt.lower())
        self.assertIn("preserve the complete foreground", prompt.lower())

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
                validate_target("gen_api", "flux-2")
            gpu = next(x for x in capabilities()["backends"] if x["id"] == "gpu")
            self.assertFalse(gpu["enabled"])
        with mock.patch.dict(os.environ, {"GEN_API_KEY": "secret"}, clear=True):
            self.assertEqual(validate_target("gen_api", "flux-2"), 3.3)

    def test_gpt_image_2_supports_reference_edit_mode(self):
        with mock.patch.dict(os.environ, {"AITUNNEL_API_KEY": "secret"}, clear=True):
            self.assertEqual(
                validate_target("aitunnel", "gpt-image-2", "reference_guided"),
                1.53,
            )
            model = next(
                model
                for backend in capabilities()["backends"]
                if backend["id"] == "aitunnel"
                for model in backend["models"]
                if model["id"] == "gpt-image-2"
            )
            self.assertTrue(model["supports_reference"])

    def test_angle_synthesis_requires_reference_capable_target(self):
        with mock.patch.dict(os.environ, {"AITUNNEL_API_KEY": "secret"}, clear=True):
            self.assertEqual(
                validate_target("aitunnel", "gpt-image-2", "angle_synthesis"),
                1.53,
            )
        with mock.patch.dict(os.environ, {
            "GPU_IMAGE_SERVER_URL": "http://127.0.0.1:8787",
            "GPU_IMAGE_SERVER_TOKEN": "x" * 32,
            "GPU_IMAGE_ALLOW_HTTP": "1",
        }, clear=True):
            with self.assertRaises(ImageLabError):
                validate_target("gpu", "qwen-image-2512", "angle_synthesis")

    def test_gpu_requires_transport_and_long_token(self):
        base = {
            "GPU_IMAGE_SERVER_URL": "http://127.0.0.1:8787",
            "GPU_IMAGE_SERVER_TOKEN": "x" * 32,
        }
        with mock.patch.dict(os.environ, base, clear=True):
            self.assertFalse(next(
                item for item in capabilities()["backends"] if item["id"] == "gpu"
            )["enabled"])
        with mock.patch.dict(
            os.environ,
            {**base, "GPU_IMAGE_ALLOW_HTTP": "1", "GPU_IMAGE_RUB_PER_GENERATION": "2.5"},
            clear=True,
        ):
            gpu = next(
                item for item in capabilities()["backends"] if item["id"] == "gpu"
            )
            self.assertTrue(gpu["enabled"])
            self.assertEqual(gpu["models"][0]["cost_rub"], 2.5)

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
