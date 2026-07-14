# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path

from scripts.gpu_pilot.http_bridge import Bridge


class GPUBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bridge = Bridge(root / "bridge", root / "queue", "x" * 32, 3)

    def tearDown(self):
        self.temp.cleanup()

    def test_creates_background_only_worker_job(self):
        prompt = (
            "Empty product photography backdrop: warm stone. nothing in the middle. "
            "No people, no text, no watermarks."
        )
        job_id = self.bridge.create({"prompt": prompt, "steps": 4, "true_cfg": 1.0})
        queued = json.loads((self.bridge.queue / f"{job_id}.json").read_text())
        self.assertEqual(queued["kind"], "t2i")
        self.assertNotIn("photo", queued)
        manifest = json.loads((Path(queued["bundle"]) / "manifest.json").read_text())
        self.assertEqual(
            manifest["production_policy"],
            "background_only_pixel_preserved_composite",
        )

    def test_unsafe_positive_instruction_rejected(self):
        with self.assertRaises(ValueError):
            self.bridge.create({"prompt": "add a product and write SALE", "steps": 4})

    def test_completed_artifact_is_bounded_to_job_directory(self):
        prompt = "Empty product photography backdrop. No people, no text, no watermarks."
        job_id = self.bridge.create({"prompt": prompt})
        (self.bridge.queue / f"{job_id}.json").rename(
            self.bridge.queue / f"{job_id}.done")
        out = self.bridge.job_dir(job_id) / "out"
        (out / "background.png").write_bytes(b"PNG")
        (out / "results.jsonl").write_text(json.dumps({
            "status": "background_only",
            "artifact": "background.png",
            "error": "",
        }) + "\n")
        self.assertEqual(self.bridge.status(job_id)["status"], "completed")
        self.assertEqual(self.bridge.image_path(job_id).name, "background.png")


if __name__ == "__main__":
    unittest.main()
