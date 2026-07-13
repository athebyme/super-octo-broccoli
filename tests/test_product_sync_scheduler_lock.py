import fcntl
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import product_sync_scheduler


class ProductSyncSchedulerLockTest(unittest.TestCase):
    def tearDown(self):
        product_sync_scheduler.shutdown_scheduler()

    def test_process_lock_is_held_until_explicit_release(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = os.path.join(directory, 'scheduler.lock')
            with patch.dict(os.environ, {'SCHEDULER_LOCK_FILE': lock_path}):
                self.assertTrue(
                    product_sync_scheduler._acquire_scheduler_process_lock(),
                )
                self.assertIsNotNone(product_sync_scheduler._scheduler_lock_handle)
                with open(lock_path, encoding='utf-8') as handle:
                    self.assertEqual(handle.read(), str(os.getpid()))

                contender = open(lock_path, 'a+', encoding='utf-8')
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(
                            contender.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                finally:
                    contender.close()

                product_sync_scheduler._release_scheduler_process_lock()
                self.assertIsNone(product_sync_scheduler._scheduler_lock_handle)

    def test_contender_starts_scheduler_after_lock_becomes_available(self):
        waits = []
        flask_app = object()

        def wait_once(seconds):
            waits.append(seconds)
            return False

        with patch.object(
            product_sync_scheduler,
            '_acquire_scheduler_process_lock',
            return_value=True,
        ), patch.object(
            product_sync_scheduler,
            'init_scheduler',
        ) as init_scheduler:
            product_sync_scheduler._scheduler_lock_retry_loop(
                flask_app, wait_fn=wait_once,
            )

        self.assertEqual(waits, [15.0])
        init_scheduler.assert_called_once_with(
            flask_app, retry_if_locked=False,
        )

    def test_partial_brand_checkpoint_gets_bounded_resume_without_hot_loop(self):
        pending = SimpleNamespace(
            is_active=True,
            brands_sync_status='partial',
            brands_sync_checkpoint='{"next_index":10}',
        )
        complete = SimpleNamespace(
            is_active=True,
            brands_sync_status='success',
            brands_sync_checkpoint=None,
        )
        failed_without_checkpoint = SimpleNamespace(
            is_active=True,
            brands_sync_status='failed',
            brands_sync_checkpoint=None,
        )

        self.assertEqual(product_sync_scheduler.BRAND_SYNC_RESUME_MINUTES, 10)
        self.assertTrue(product_sync_scheduler._brand_sync_needs_resume(pending))
        self.assertFalse(product_sync_scheduler._brand_sync_needs_resume(complete))
        self.assertFalse(product_sync_scheduler._brand_sync_needs_resume(
            failed_without_checkpoint,
        ))


if __name__ == '__main__':
    unittest.main()
