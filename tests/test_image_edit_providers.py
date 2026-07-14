# -*- coding: utf-8 -*-
import os
import unittest
from unittest import mock

os.environ.setdefault("SKIP_SCHEDULER", "1")

from services.image_generation_service import (
    ImageGenerationConfig,
    ImageGenerator,
    ImageProvider,
    is_censorship_refusal,
)


class CensorshipClassifierTests(unittest.TestCase):
    def test_nsfw_marker_detected(self):
        self.assertTrue(is_censorship_refusal("Request flagged by content policy"))
        self.assertTrue(is_censorship_refusal("NSFW content detected"))

    def test_technical_error_not_nsfw(self):
        self.assertFalse(is_censorship_refusal("HTTP 502 Bad Gateway"))

    def test_none_and_empty_are_not_nsfw(self):
        self.assertFalse(is_censorship_refusal(None))
        self.assertFalse(is_censorship_refusal(""))


class FromEnvTests(unittest.TestCase):
    def test_gen_api_key_from_env(self):
        with mock.patch.dict(os.environ, {"GEN_API_KEY": "k1"}):
            cfg = ImageGenerationConfig.from_env(ImageProvider.GEN_API)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.provider, ImageProvider.GEN_API)
        self.assertEqual(cfg.gen_api_key, "k1")

    def test_aitunnel_key_from_env(self):
        with mock.patch.dict(os.environ, {"AITUNNEL_API_KEY": "k2"}):
            cfg = ImageGenerationConfig.from_env(ImageProvider.AITUNNEL)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.aitunnel_api_key, "k2")

    def test_missing_key_returns_none(self):
        env = {k: v for k, v in os.environ.items() if k != "GEN_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(ImageGenerationConfig.from_env(ImageProvider.GEN_API))


class BaseEditContractTests(unittest.TestCase):
    def test_default_edit_reports_unsupported(self):
        class Dummy(ImageGenerator):
            def generate(self, prompt, width=1440, height=810, reference_image_url=None):
                return True, b"x", ""

        ok, data, err = Dummy().edit(prompt="p", source_image_url="http://x/1.png")
        self.assertFalse(ok)
        self.assertIsNone(data)
        self.assertIn("не поддерживает", err)


def _resp(status_code=200, json_data=None, content=b""):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = json_data or {}
    r.content = content
    r.text = str(json_data or "")
    return r


class FitImageTests(unittest.TestCase):
    def _png(self, w, h):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (w, h), (30, 20, 40)).save(buf, format="PNG")
        return buf.getvalue()

    def test_oversized_result_cropped_to_exact(self):
        import io

        from PIL import Image

        from services.image_generation_service import fit_image_to_size

        out = fit_image_to_size(self._png(912, 1216), 900, 1200)
        self.assertEqual(Image.open(io.BytesIO(out)).size, (900, 1200))

    def test_exact_size_untouched(self):
        from services.image_generation_service import fit_image_to_size

        original = self._png(900, 1200)
        self.assertIs(fit_image_to_size(original, 900, 1200), original)

    def test_broken_bytes_returned_as_is(self):
        from services.image_generation_service import fit_image_to_size

        self.assertEqual(fit_image_to_size(b"not-a-png", 900, 1200), b"not-a-png")


class GenApiGeneratorTests(unittest.TestCase):
    def _config(self):
        return ImageGenerationConfig(
            provider=ImageProvider.GEN_API,
            api_key="k",
            gen_api_key="k",
            timeout=10,
        )

    @mock.patch("services.image_generation_service.time.sleep", lambda *_: None)
    @mock.patch("services.image_generation_service.requests.get")
    @mock.patch("services.image_generation_service.requests.post")
    def test_edit_success_via_poll(self, m_post, m_get):
        from services.image_generation_service import GenApiImageGenerator

        m_post.return_value = _resp(json_data={"request_id": 42})
        m_get.side_effect = [
            _resp(json_data={"status": "processing"}),
            _resp(json_data={"status": "success", "result": ["http://cdn/img.png"]}),
            _resp(content=b"PNGDATA"),
        ]
        gen = GenApiImageGenerator(self._config())
        ok, data, err = gen.edit(
            prompt="scene", source_image_bytes=b"\x89PNG\r\n\x1a\nSRC"
        )
        self.assertTrue(ok, err)
        self.assertEqual(data, b"PNGDATA")
        # edit идёт в i2i-сеть из конфига
        self.assertIn("nano-banana", m_post.call_args[0][0])
        # files_array передаётся multipart, не JSON data URI: Flux 2 иначе даёт 422.
        self.assertNotIn("json", m_post.call_args[1])
        self.assertEqual(m_post.call_args[1]["files"][0][0], "image_urls[]")
        self.assertEqual(m_post.call_args[1]["data"]["prompt"], "scene")
        self.assertNotIn("Content-Type", m_post.call_args[1]["headers"])

    @mock.patch("services.image_generation_service.time.sleep", lambda *_: None)
    @mock.patch("services.image_generation_service.requests.get")
    @mock.patch("services.image_generation_service.requests.post")
    def test_nsfw_failure_is_marked(self, m_post, m_get):
        from services.image_generation_service import GenApiImageGenerator

        m_post.return_value = _resp(json_data={"request_id": 43})
        m_get.return_value = _resp(
            json_data={"status": "error", "error": "blocked by content policy"}
        )
        gen = GenApiImageGenerator(self._config())
        ok, data, err = gen.edit(
            prompt="scene", source_image_bytes=b"\x89PNG\r\n\x1a\nSRC"
        )
        self.assertFalse(ok)
        self.assertTrue(err.startswith("NSFW:"), err)

    @mock.patch("services.image_generation_service.time.sleep", lambda *_: None)
    @mock.patch("services.image_generation_service.requests.get")
    @mock.patch("services.image_generation_service.requests.post")
    def test_failure_reason_extracted_from_full_response(self, m_post, m_get):
        from services.image_generation_service import GenApiImageGenerator

        m_post.return_value = _resp(json_data={"request_id": 44})
        m_get.return_value = _resp(json_data={
            "status": "failed",
            "full_response": [{"finish_reason": "PROHIBITED_CONTENT"}],
        })
        gen = GenApiImageGenerator(self._config())
        ok, _, err = gen.edit(
            prompt="scene", source_image_bytes=b"\x89PNG\r\n\x1a\nSRC"
        )
        self.assertFalse(ok)
        self.assertTrue(err.startswith("NSFW:"), err)

    @mock.patch("services.image_generation_service.requests.post")
    def test_generate_uses_t2i_network(self, m_post):
        from services.image_generation_service import GenApiImageGenerator

        m_post.return_value = _resp(status_code=500, json_data={"error": "boom"})
        gen = GenApiImageGenerator(self._config())
        ok, _, err = gen.generate(prompt="bg", width=900, height=1200)
        self.assertFalse(ok)
        self.assertIn("flux-2", m_post.call_args[0][0])
        # Gen-API отклоняет размеры, не кратные 16 (HTTP 422): вверх до 912,
        # результат затем кропается до точного размера
        self.assertEqual(m_post.call_args[1]["json"]["width"], 912)
        self.assertEqual(m_post.call_args[1]["json"]["height"], 1200)

    def test_edit_without_source_rejected(self):
        from services.image_generation_service import GenApiImageGenerator

        ok, data, err = GenApiImageGenerator(self._config()).edit(prompt="x")
        self.assertFalse(ok)
        self.assertIn("исходное", err)

    @mock.patch("services.image_generation_service.requests.post")
    def test_edit_bytes_and_references_use_multipart_files_array(self, m_post):
        from services.image_generation_service import GenApiImageGenerator

        m_post.return_value = _resp(status_code=500, json_data={"error": "stop"})
        config = self._config()
        config.gen_api_edit_model = "flux-2"
        GenApiImageGenerator(config).edit(
            prompt="scene",
            source_image_bytes=b"\x89PNG\r\n\x1a\nMAIN",
            additional_source_images=[b"\xff\xd8\xffPACK", b"RIFFxxxxWEBPDETAIL"],
            width=900,
            height=1200,
        )
        payload = m_post.call_args.kwargs["data"]
        files = m_post.call_args.kwargs["files"]
        self.assertEqual([item[0] for item in files], ["image_urls[]"] * 3)
        self.assertEqual(
            [item[1][2] for item in files],
            ["image/png", "image/jpeg", "image/webp"],
        )
        self.assertEqual(payload["width"], 912)


class AITunnelGeneratorTests(unittest.TestCase):
    def _config(self):
        return ImageGenerationConfig(
            provider=ImageProvider.AITUNNEL,
            api_key="k",
            aitunnel_api_key="k",
            timeout=10,
        )

    @mock.patch("services.image_generation_service.requests.post")
    def test_generate_b64_success(self, m_post):
        import base64 as b64

        from services.image_generation_service import AITunnelImageGenerator

        m_post.return_value = _resp(
            json_data={"data": [{"b64_json": b64.b64encode(b"IMG").decode()}]}
        )
        ok, data, err = AITunnelImageGenerator(self._config()).generate(
            prompt="bg", width=900, height=1200
        )
        self.assertTrue(ok, err)
        self.assertEqual(data, b"IMG")
        self.assertIn("/images/generations", m_post.call_args[0][0])
        self.assertEqual(m_post.call_args[1]["json"]["model"], "gpt-image-2")
        # GPT Image принимает фиксированный портретный размер, финал локально 900x1200.
        self.assertEqual(m_post.call_args[1]["json"]["size"], "1024x1536")

    def test_seedream_sensitive_refusal_is_nsfw(self):
        from services.image_generation_service import is_censorship_refusal

        self.assertTrue(is_censorship_refusal(
            "AITunnel: HTTP 400 {\"error\":{\"message\":\"The request failed because "
            "the output image may contain sensitive information.\"}}"))

    def test_effective_size_seedream_min_pixels(self):
        from services.image_generation_service import AITunnelImageGenerator

        gen = AITunnelImageGenerator(self._config())
        w, h = gen._effective_size(900, 1200, "seedream-4.5")
        self.assertEqual(w % 16, 0)
        self.assertEqual(h % 16, 0)
        self.assertGreaterEqual(w * h, gen._SEEDREAM_MIN_PIXELS)
        # пропорции ~3:4 сохранены
        self.assertAlmostEqual(w / h, 900 / 1200, delta=0.05)

    def test_effective_size_gpt_image_uses_supported_portrait_size(self):
        from services.image_generation_service import AITunnelImageGenerator

        gen = AITunnelImageGenerator(self._config())
        self.assertEqual(gen._effective_size(900, 1200, "gpt-image-2"), (1024, 1536))
        self.assertEqual(gen._effective_size(1200, 900, "gpt-image-2"), (1536, 1024))
        self.assertEqual(gen._effective_size(1000, 1000, "gpt-image-2"), (1024, 1024))

    @mock.patch("services.image_generation_service.requests.post")
    def test_edit_multipart_with_bytes(self, m_post):
        import base64 as b64

        from services.image_generation_service import AITunnelImageGenerator

        m_post.return_value = _resp(
            json_data={"data": [{"b64_json": b64.b64encode(b"OUT").decode()}]}
        )
        ok, data, err = AITunnelImageGenerator(self._config()).edit(
            prompt="scene", source_image_bytes=b"SRC"
        )
        self.assertTrue(ok, err)
        self.assertEqual(data, b"OUT")
        self.assertIn("/images/edits", m_post.call_args[0][0])
        self.assertEqual(m_post.call_args[1]["data"]["model"], "seedream-4.5")
        self.assertIn("image", m_post.call_args[1]["files"])

    @mock.patch("services.image_generation_service.requests.post")
    def test_moderation_refusal_marked_nsfw(self, m_post):
        from services.image_generation_service import AITunnelImageGenerator

        m_post.return_value = _resp(
            status_code=400,
            json_data={"error": {"message": "rejected by moderation"}},
        )
        ok, _, err = AITunnelImageGenerator(self._config()).edit(
            prompt="scene", source_image_bytes=b"SRC"
        )
        self.assertFalse(ok)
        self.assertTrue(err.startswith("NSFW:"), err)

    @mock.patch("services.image_generation_service.requests.post")
    def test_edit_sends_additional_references_and_protection_mask(self, m_post):
        import base64 as b64

        from services.image_generation_service import AITunnelImageGenerator

        m_post.return_value = _resp(
            json_data={"data": [{"b64_json": b64.b64encode(b"OUT").decode()}]}
        )
        ok, _, err = AITunnelImageGenerator(self._config()).edit(
            prompt="scene",
            source_image_bytes=b"MAIN",
            additional_source_images=[b"PACK"],
            mask_bytes=b"MASK",
        )
        self.assertTrue(ok, err)
        files = m_post.call_args.kwargs["files"]
        self.assertEqual([item[0] for item in files], ["image[]", "image[]", "mask"])

    @mock.patch("services.image_generation_service.requests.post")
    def test_edit_labels_multi_reference_files_by_actual_format(self, m_post):
        import base64 as b64

        from services.image_generation_service import AITunnelImageGenerator

        m_post.return_value = _resp(
            json_data={"data": [{"b64_json": b64.b64encode(b"OUT").decode()}]}
        )
        ok, _, err = AITunnelImageGenerator(self._config()).edit(
            prompt="angle",
            source_image_bytes=b"\xff\xd8\xffMAIN",
            additional_source_images=[b"RIFFxxxxWEBPDETAIL"],
        )
        self.assertTrue(ok, err)
        files = m_post.call_args.kwargs["files"]
        self.assertEqual(
            [item[1][2] for item in files],
            ["image/jpeg", "image/webp"],
        )

    @mock.patch("services.image_generation_service.requests.post")
    def test_gpt_image_2_edit_uses_reference_fidelity_and_supported_size(self, m_post):
        import base64 as b64

        from services.image_generation_service import AITunnelImageGenerator

        m_post.return_value = _resp(
            json_data={"data": [{"b64_json": b64.b64encode(b"OUT").decode()}]}
        )
        config = self._config()
        config.aitunnel_edit_model = "gpt-image-2"
        ok, _, err = AITunnelImageGenerator(config).edit(
            prompt="scene",
            source_image_bytes=b"MAIN",
            input_fidelity="high",
            width=900,
            height=1200,
        )
        self.assertTrue(ok, err)
        request_data = m_post.call_args.kwargs["data"]
        self.assertEqual(request_data["model"], "gpt-image-2")
        self.assertEqual(request_data["input_fidelity"], "high")
        self.assertEqual(request_data["size"], "1024x1536")

    @mock.patch("services.image_generation_service.requests.get")
    @mock.patch("services.image_generation_service.requests.post")
    def test_edit_downloads_source_url_when_no_bytes(self, m_post, m_get):
        import base64 as b64

        from services.image_generation_service import AITunnelImageGenerator

        m_get.return_value = _resp(content=b"SRCIMG")
        m_post.return_value = _resp(
            json_data={"data": [{"b64_json": b64.b64encode(b"OUT").decode()}]}
        )
        ok, data, err = AITunnelImageGenerator(self._config()).edit(
            prompt="scene", source_image_url="http://p/1.jpg"
        )
        self.assertTrue(ok, err)
        m_get.assert_called_once()


class ServiceFacadeTests(unittest.TestCase):
    def test_factory_routes_new_providers(self):
        from services.image_generation_service import (
            AITunnelImageGenerator,
            GenApiImageGenerator,
            ImageGenerationService,
        )

        svc = ImageGenerationService(ImageGenerationConfig(
            provider=ImageProvider.GEN_API, api_key="k", gen_api_key="k"))
        self.assertIsInstance(svc.generator, GenApiImageGenerator)

        svc = ImageGenerationService(ImageGenerationConfig(
            provider=ImageProvider.AITUNNEL, api_key="k", aitunnel_api_key="k"))
        self.assertIsInstance(svc.generator, AITunnelImageGenerator)

    def test_edit_image_delegates_with_default_size(self):
        from services.image_generation_service import ImageGenerationService

        svc = ImageGenerationService(ImageGenerationConfig(
            provider=ImageProvider.GEN_API, api_key="k", gen_api_key="k"))
        svc.generator = mock.Mock()
        svc.generator.edit.return_value = (True, b"X", "")
        ok, data, err = svc.edit_image(prompt="p", source_image_url="http://s/1.png")
        self.assertTrue(ok)
        kwargs = svc.generator.edit.call_args[1]
        self.assertEqual(kwargs["width"], 900)
        self.assertEqual(kwargs["height"], 1200)


if __name__ == "__main__":
    unittest.main()
