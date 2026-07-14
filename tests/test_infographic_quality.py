# -*- coding: utf-8 -*-
import io
import unittest

from PIL import Image, ImageDraw

from services.infographic_quality import (
    ImageQualityError,
    apply_text_overlay,
    apply_watermark,
    compose_identity_preserving,
    compose_multi_identity_preserving,
    evaluate_background_text,
    evaluate_final_image,
)


def png(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class IdentityCompositeTests(unittest.TestCase):
    def setUp(self):
        self.background = png(Image.new("RGB", (640, 640), (30, 40, 50)))
        self.source = png(Image.new("RGB", (120, 240), (220, 20, 30)))

    def cutout(self, _source):
        # RGB is deliberately wrong: compositor must retain source red RGB and
        # consume only this alpha matte.
        image = Image.new("RGBA", (120, 240), (0, 255, 0, 0))
        ImageDraw.Draw(image).rectangle((20, 20, 99, 219), fill=(0, 255, 0, 255))
        return png(image)

    def test_exact_canvas_and_original_rgb(self):
        result = compose_identity_preserving(
            self.background, self.source, cutout=self.cutout)
        image = Image.open(io.BytesIO(result.image_bytes)).convert("RGB")
        self.assertEqual(image.size, (900, 1200))
        x = result.metadata["placement"]["x"] + result.metadata["rendered_foreground_size"][0] // 2
        y = result.metadata["placement"]["y"] + result.metadata["rendered_foreground_size"][1] // 2
        red, green, blue = image.getpixel((x, y))
        self.assertGreater(red, 180)
        self.assertLess(green, 80)
        self.assertEqual(result.metadata["identity_mode"], "pixel_preserved_composite")
        self.assertEqual(result.metadata["rgb_source"], "original_bytes_decoded_no_retouch")
        self.assertFalse(result.metadata["mask_verified"])

    def test_extractor_size_change_rejected(self):
        def wrong_size(_source):
            return png(Image.new("RGBA", (10, 10), (1, 2, 3, 255)))

        with self.assertRaises(ImageQualityError):
            compose_identity_preserving(self.background, self.source, cutout=wrong_size)

    def test_original_transparency_is_a_verified_source_mask(self):
        transparent = Image.new("RGBA", (120, 240), (0, 0, 0, 0))
        ImageDraw.Draw(transparent).rectangle(
            (20, 20, 99, 219), fill=(220, 20, 30, 255))
        result = compose_identity_preserving(self.background, png(transparent))
        self.assertTrue(result.metadata["mask_verified"])
        self.assertEqual(result.metadata["mask_assurance"], "source_alpha")

    def test_multi_photo_composite_preserves_every_source_rgb(self):
        blue = png(Image.new("RGB", (120, 240), (20, 30, 220)))
        result = compose_multi_identity_preserving(
            self.background,
            [self.source, blue],
            cutout=self.cutout,
        )
        image = Image.open(io.BytesIO(result.image_bytes)).convert("RGB")
        self.assertEqual(image.size, (900, 1200))
        self.assertEqual(result.metadata["source_count"], 2)
        self.assertEqual(result.metadata["composition_mode"], "collage")
        colors = []
        for placement in result.metadata["placements"]:
            x = placement["x"] + placement["width"] // 2
            y = placement["y"] + placement["height"] // 2
            colors.append(image.getpixel((x, y)))
        self.assertGreater(colors[0][0], 180)
        self.assertGreater(colors[1][2], 180)
        self.assertIsNotNone(result.protection_mask_bytes)

    def test_text_and_watermark_are_deterministic_local_overlays(self):
        base = png(Image.new("RGB", (900, 1200), (30, 40, 50)))
        text = apply_text_overlay(base, title="Точный текст", subtitle="Без опечаток")
        self.assertEqual(text.metadata["rendered_texts"], ["Точный текст", "Без опечаток"])
        logo = Image.new("RGBA", (100, 50), (0, 0, 0, 0))
        ImageDraw.Draw(logo).rectangle((5, 5, 95, 45), fill=(255, 120, 0, 255))
        marked = apply_watermark(
            text.image_bytes,
            png(logo),
            position="bottom_right",
            scale_percent=15,
            opacity_percent=80,
        )
        self.assertEqual(Image.open(io.BytesIO(marked.image_bytes)).size, (900, 1200))
        self.assertEqual(marked.metadata["mode"], "deterministic_png_overlay")


class QualityGateTests(unittest.TestCase):
    def setUp(self):
        self.image = png(Image.effect_noise((900, 1200), 40).convert("RGB"))
        self.meta = {
            "identity_mode": "pixel_preserved_composite",
            "cutout_sha256": "a",
            "rendered_foreground_sha256": "b",
            "mask_verified": True,
        }

    def test_composite_auto_pass_requires_background_check(self):
        review = evaluate_final_image(
            self.image,
            identity_mode="pixel_preserved_composite",
            composite_metadata=self.meta,
        )
        self.assertEqual(review["status"], "review_required")
        passed = evaluate_final_image(
            self.image,
            identity_mode="pixel_preserved_composite",
            composite_metadata=self.meta,
            background_text_check={"checked": True, "pass": True},
            background_scene_check={"checked": True, "pass": True},
        )
        self.assertEqual(passed["status"], "auto_pass")
        self.assertTrue(passed["publishable"])

    def test_automated_mask_requires_review_even_with_clean_background(self):
        metadata = dict(self.meta, mask_verified=False)
        result = evaluate_final_image(
            self.image,
            identity_mode="pixel_preserved_composite",
            composite_metadata=metadata,
            background_text_check={"checked": True, "pass": True},
            background_scene_check={"checked": True, "pass": True},
        )
        self.assertEqual(result["status"], "review_required")
        self.assertIsNone(result["identity_pass"])

    def test_unverified_ai_scene_requires_review(self):
        result = evaluate_final_image(
            self.image,
            identity_mode="pixel_preserved_composite",
            composite_metadata=self.meta,
            background_text_check={"checked": True, "pass": True},
            background_scene_check={"checked": False, "pass": None},
        )
        self.assertEqual(result["status"], "review_required")

    def test_failed_scene_check_is_rejected(self):
        result = evaluate_final_image(
            self.image,
            identity_mode="pixel_preserved_composite",
            composite_metadata=self.meta,
            background_text_check={"checked": True, "pass": True},
            background_scene_check={"checked": True, "pass": False},
        )
        self.assertEqual(result["status"], "rejected")

    def test_detected_background_text_rejected(self):
        result = evaluate_final_image(
            self.image,
            identity_mode="pixel_preserved_composite",
            composite_metadata=self.meta,
            background_text_check={"checked": True, "pass": False, "detected_text": "SALE"},
        )
        self.assertEqual(result["status"], "rejected")

    def test_generative_edit_is_never_auto_publishable(self):
        result = evaluate_final_image(self.image, identity_mode="generative_edit")
        self.assertEqual(result["status"], "review_required")
        self.assertFalse(result["publishable"])

    def test_ocr_injection(self):
        blank = png(Image.new("RGB", (900, 1200), "white"))
        self.assertTrue(evaluate_background_text(blank, ocr=lambda _image: "")["pass"])
        self.assertFalse(evaluate_background_text(blank, ocr=lambda _image: "SALE")["pass"])


if __name__ == "__main__":
    unittest.main()
