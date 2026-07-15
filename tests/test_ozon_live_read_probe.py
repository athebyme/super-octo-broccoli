# -*- coding: utf-8 -*-
"""The optional live probe is structurally incapable of Ozon writes."""

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts.probe_ozon_read_contracts import (
    ProbeConfigurationError,
    load_credentials,
    response_shape,
    run_probe,
)
from services.ozon_api_client import OZON_ENDPOINTS


class FakeReadClient:
    def __init__(self):
        self.calls = []

    def request(self, endpoint_name, payload):
        self.calls.append((endpoint_name, payload))
        if endpoint_name == "product_list":
            return {
                "result": {
                    "items": [{
                        "product_id": 10,
                        "offer_id": "private-offer",
                        "sku": 20,
                    }],
                    "total": 1,
                    "last_id": "private-cursor",
                }
            }
        return {
            "secret_value": "must-not-escape",
            "items": [{"private": 123}],
        }


class OzonLiveReadProbeTest(unittest.TestCase):
    def test_shape_never_contains_scalar_values(self):
        encoded = json.dumps(response_shape({
            "token": "must-not-escape",
            "id": 123456,
            "enabled": True,
        }))
        self.assertNotIn("must-not-escape", encoded)
        self.assertNotIn("123456", encoded)
        self.assertNotIn("true", encoded.lower())

    def test_probe_calls_only_manifest_read_endpoints_and_redacts_values(self):
        client = FakeReadClient()
        result = run_probe(client)
        self.assertEqual(result["mode"], "read_only")
        self.assertEqual(result["write_endpoint_count"], 0)
        for endpoint_name, _payload in client.calls:
            self.assertEqual(OZON_ENDPOINTS[endpoint_name].retry_class, "read")
        encoded = json.dumps(result)
        for forbidden in (
            "private-offer",
            "private-cursor",
            "must-not-escape",
            "123",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_env_file_requires_owner_only_mode_and_never_uses_shell_eval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ozon.env"
            path.write_text(
                "OZON_LIVE_CLIENT_ID=synthetic-client\n"
                "OZON_LIVE_API_KEY='synthetic-key'\n",
                encoding="utf-8",
            )
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            with patch.dict(os.environ, {}, clear=True):
                credentials = load_credentials(path)
            self.assertEqual(credentials.external_account_id, "synthetic-client")
            self.assertEqual(credentials.api_key, "synthetic-key")

            path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ProbeConfigurationError):
                    load_credentials(path)


if __name__ == "__main__":
    unittest.main()
