# -*- coding: utf-8 -*-
"""Marketplace account side effects share one non-blocking process lock."""

import unittest

from services.marketplace_operation_locks import (
    release_account_operation_lock,
    try_account_operation_lock,
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


if __name__ == "__main__":
    unittest.main()
