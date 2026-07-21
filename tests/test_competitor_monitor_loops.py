import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from services import competitor_monitor


class CompetitorMonitorLoopTest(unittest.TestCase):
    def setUp(self):
        competitor_monitor._monitor_threads.clear()
        competitor_monitor._stop_events.clear()

    def tearDown(self):
        competitor_monitor._monitor_threads.clear()
        competitor_monitor._stop_events.clear()

    def test_restart_starts_outside_registry_lock(self):
        app = Flask(__name__)
        setting = SimpleNamespace(seller_id=17)
        query = SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: [setting])
        )
        model = SimpleNamespace(query=query)

        def assert_lock_released(seller_id, flask_app):
            self.assertEqual(seller_id, setting.seller_id)
            self.assertIs(flask_app, app)
            self.assertFalse(competitor_monitor._threads_lock.locked())

        with patch("models.CompetitorMonitorSettings", model), patch.object(
            competitor_monitor,
            "start_competitor_monitor_loop",
            side_effect=assert_lock_released,
        ) as start:
            competitor_monitor.check_and_restart_monitor_loops(app)

        start.assert_called_once_with(setting.seller_id, app)

    def test_live_thread_is_not_restarted(self):
        app = Flask(__name__)
        setting = SimpleNamespace(seller_id=18)
        competitor_monitor._monitor_threads[setting.seller_id] = SimpleNamespace(
            is_alive=lambda: True
        )
        query = SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: [setting])
        )
        model = SimpleNamespace(query=query)

        with patch("models.CompetitorMonitorSettings", model), patch.object(
            competitor_monitor,
            "start_competitor_monitor_loop",
        ) as start:
            competitor_monitor.check_and_restart_monitor_loops(app)

        start.assert_not_called()

    def test_removed_setting_stops_new_loop_cleanly(self):
        app = Flask(__name__)
        query = SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(first=lambda: None)
        )
        model = SimpleNamespace(query=query)

        class InlineThread:
            def __init__(self, *, target, **_kwargs):
                self._target = target

            def start(self):
                self._target()

            def is_alive(self):
                return False

        with patch("models.CompetitorMonitorSettings", model), patch.object(
            competitor_monitor.threading,
            "Thread",
            InlineThread,
        ):
            competitor_monitor.start_competitor_monitor_loop(19, app)

        self.assertIn(19, competitor_monitor._monitor_threads)

    def test_cycle_pause_is_bounded_away_from_hot_loop(self):
        self.assertEqual(
            competitor_monitor.normalize_cycle_pause_seconds(0),
            competitor_monitor.MIN_CYCLE_PAUSE_SECONDS,
        )
        self.assertEqual(
            competitor_monitor.normalize_cycle_pause_seconds("invalid"),
            competitor_monitor.MIN_CYCLE_PAUSE_SECONDS,
        )
        self.assertEqual(
            competitor_monitor.normalize_cycle_pause_seconds(120),
            120,
        )
        self.assertEqual(
            competitor_monitor.normalize_cycle_pause_seconds(999999),
            competitor_monitor.MAX_CYCLE_PAUSE_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
