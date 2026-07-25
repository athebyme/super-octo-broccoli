"""Known marketplace wrappers resolve to exact supplier identities only."""

import unittest

from services.marketplace_source_identity import (
    SourceIdentityKey,
    encoded_record_identities,
    parse_encoded_source_identities,
    source_record_identities,
)


class MarketplaceSourceIdentityTest(unittest.TestCase):
    def test_sexoptovik_seller_suffix_is_not_part_of_source_identity(self):
        ozon = parse_encoded_source_identities("id-7725-1364")
        wb = encoded_record_identities("id-7725-1366")

        self.assertEqual(
            [item.key for item in ozon],
            [SourceIdentityKey("sexoptovik", "external_id", "7725")],
        )
        self.assertEqual(
            wb,
            frozenset({
                SourceIdentityKey("sexoptovik", "external_id", "7725"),
            }),
        )
        self.assertEqual(ozon[0].scheme, "sexoptovik_wrapped_id")

    def test_known_sexoptovik_s_lane_is_anchored(self):
        parsed = parse_encoded_source_identities("1366Z1C1S0021530")
        self.assertEqual(
            parsed[0].key,
            SourceIdentityKey(
                "sexoptovik",
                "external_id",
                "21530",
            ),
        )
        self.assertEqual(
            parse_encoded_source_identities("prefix1366Z1C1S21530"),
            (),
        )
        self.assertEqual(
            parse_encoded_source_identities("1366Z1C1S21530-tail"),
            (),
        )
        # K/L are separate historical namespaces and collide numerically with
        # Sexoptovik IDs; without their own supplier registry they stay
        # intentionally unmatched.
        self.assertEqual(
            parse_encoded_source_identities("1366Z1C1K21530"),
            (),
        )
        self.assertEqual(
            parse_encoded_source_identities("1366Z1C1L21530"),
            (),
        )

    def test_andrey_case_and_prefix_variants_share_unique_serial(self):
        ozon = {
            item.key
            for item in parse_encoded_source_identities(
                "1366Z1C1A0t-00011433"
            )
        }
        supplier = source_record_identities(
            source_code="andrey",
            external_id="0T-00011433",
        )
        typo_variant = {
            item.key
            for item in parse_encoded_source_identities(
                "1366Z1C1Ayt-00011433"
            )
        }

        expected_serial = SourceIdentityKey("andrey", "serial", "11433")
        self.assertIn(expected_serial, ozon)
        self.assertIn(expected_serial, supplier)
        self.assertIn(expected_serial, typo_variant)
        self.assertIn(
            SourceIdentityKey(
                "andrey",
                "external_id",
                "0t-00011433",
            ),
            ozon,
        )

    def test_andrey_wrapped_wb_variant_is_serial_only(self):
        parsed = parse_encoded_source_identities(
            "id-00006337-1366Z1C1A"
        )
        self.assertEqual(
            [item.key for item in parsed],
            [SourceIdentityKey("andrey", "serial", "6337")],
        )

    def test_v_lane_is_exact_vendor_key_without_source_guess(self):
        parsed = parse_encoded_source_identities("1366Z1C1V46852")
        self.assertEqual(
            [item.key for item in parsed],
            [SourceIdentityKey("*", "vendor_code", "46852")],
        )
        supplier = source_record_identities(
            source_code="andrey",
            external_id="0T-00012610",
            vendor_codes=["46852"],
        )
        self.assertIn(parsed[0].key, supplier)

    def test_unknown_or_malformed_values_never_parse_partially(self):
        for value in (
            "",
            "7725",
            "id-7725",
            "id-0-1364",
            "id-7725-1364-extra",
            "1366Z1C1S0",
            "1366Z1C1A",
            "HOT-id-7725-1364",
            "id-7725-1364\u0000",
            True,
            7725,
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_encoded_source_identities(value), ())


if __name__ == "__main__":
    unittest.main()
