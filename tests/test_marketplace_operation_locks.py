# -*- coding: utf-8 -*-
"""Marketplace account side effects share one non-blocking process lock."""

import unittest

from services.marketplace_operation_locks import (
    release_account_operation_lock,
    release_marketplace_category_mapping_lock,
    release_marketplace_source_link_lock,
    release_wb_seller_media_lock,
    release_wb_seller_content_lock,
    release_wb_seller_supplier_job_lock,
    try_account_operation_lock,
    try_marketplace_category_mapping_lock,
    try_marketplace_source_link_lock,
    try_wb_seller_media_lock,
    try_wb_seller_content_lock,
    try_wb_seller_supplier_photo_job_lock,
    try_wb_seller_supplier_verify_job_lock,
)


class MarketplaceOperationLockTest(unittest.TestCase):
    def test_same_account_is_exclusive_and_release_is_reusable(self):
        first = try_account_operation_lock(987654321)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(try_account_operation_lock(987654321))
        finally:
            release_account_operation_lock(first)

        repeated = try_account_operation_lock(987654321)
        self.assertIsNotNone(repeated)
        release_account_operation_lock(repeated)

    def test_account_id_is_strict_positive_integer(self):
        for value in (True, 0, -1, 1.0, "1", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    try_account_operation_lock(value)

    def test_wb_media_is_seller_exclusive_but_separate_from_account_scope(self):
        media = try_wb_seller_media_lock(987654321)
        self.assertIsNotNone(media)
        try:
            self.assertIsNone(try_wb_seller_media_lock(987654321))
            account = try_account_operation_lock(987654321)
            self.assertIsNotNone(account)
            release_account_operation_lock(account)
        finally:
            release_wb_seller_media_lock(media)

        repeated = try_wb_seller_media_lock(987654321)
        self.assertIsNotNone(repeated)
        release_wb_seller_media_lock(repeated)

    def test_wb_seller_id_is_strict_positive_integer(self):
        for value in (True, 0, -1, 1.0, "1", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    try_wb_seller_media_lock(value)

    def test_wb_content_is_seller_exclusive_and_separate_from_media(self):
        content = try_wb_seller_content_lock(987654322)
        self.assertIsNotNone(content)
        try:
            self.assertIsNone(try_wb_seller_content_lock(987654322))
            media = try_wb_seller_media_lock(987654322)
            self.assertIsNotNone(media)
            release_wb_seller_media_lock(media)
        finally:
            release_wb_seller_content_lock(content)

    def test_supplier_job_creation_is_exclusive_per_type(self):
        photo = try_wb_seller_supplier_photo_job_lock(987654323)
        self.assertIsNotNone(photo)
        try:
            self.assertIsNone(
                try_wb_seller_supplier_photo_job_lock(987654323)
            )
            verify = try_wb_seller_supplier_verify_job_lock(987654323)
            self.assertIsNotNone(verify)
            release_wb_seller_supplier_job_lock(verify)
        finally:
            release_wb_seller_supplier_job_lock(photo)

    def test_source_link_materialization_is_seller_exclusive(self):
        first = try_marketplace_source_link_lock(987654324)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(
                try_marketplace_source_link_lock(987654324)
            )
        finally:
            release_marketplace_source_link_lock(first)

        repeated = try_marketplace_source_link_lock(987654324)
        self.assertIsNotNone(repeated)
        release_marketplace_source_link_lock(repeated)

    def test_observed_category_mapping_is_seller_exclusive(self):
        first = try_marketplace_category_mapping_lock(987654325)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(
                try_marketplace_category_mapping_lock(987654325)
            )
        finally:
            release_marketplace_category_mapping_lock(first)

        repeated = try_marketplace_category_mapping_lock(987654325)
        self.assertIsNotNone(repeated)
        release_marketplace_category_mapping_lock(repeated)


if __name__ == "__main__":
    unittest.main()
