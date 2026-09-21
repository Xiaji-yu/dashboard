"""History 时序环形缓冲的行为测试。"""

import time
import unittest

from history import History


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.history = History(maxlen=10)

    def test_append_and_window_keeps_order(self):
        now = time.time()
        for i in range(5):
            self.history.append("cpu", now + i, i)
        points = self.history.window("cpu", seconds=600)
        self.assertEqual(len(points), 5)
        self.assertAlmostEqual(points[0][0], now, places=3)
        self.assertEqual([p[1] for p in points], [0.0, 1.0, 2.0, 3.0, 4.0])

    def test_window_drops_points_outside_range(self):
        now = time.time()
        self.history.append("cpu", now - 300, 1)
        self.history.append("cpu", now, 2)
        self.assertEqual([p[1] for p in self.history.window("cpu", seconds=120)], [2.0])

    def test_since_returns_only_newer_points(self):
        now = time.time()
        self.history.append("cpu", now, 1)
        self.history.append("cpu", now + 1, 2)
        self.assertEqual([p[1] for p in self.history.since("cpu", now)], [2.0])

    def test_since_with_zero_returns_everything(self):
        now = time.time()
        self.history.append("cpu", now, 7)
        self.assertEqual(len(self.history.since("cpu", 0)), 1)

    def test_none_values_are_skipped(self):
        """采不到的指标（如功耗）不应写入曲线，避免图上出现断崖。"""
        self.history.append("power", time.time(), None)
        self.assertEqual(self.history.window("power"), [])

    def test_maxlen_keeps_most_recent(self):
        history = History(maxlen=3)
        now = time.time()
        for i in range(5):
            history.append("cpu", now + i, i)
        self.assertEqual([p[1] for p in history.window("cpu", seconds=600)], [2.0, 3.0, 4.0])

    def test_unknown_key_returns_empty(self):
        self.assertEqual(self.history.window("nope"), [])
        self.assertEqual(self.history.since("nope", 0), [])

    def test_keys_lists_series(self):
        now = time.time()
        self.history.append("cpu", now, 1)
        self.history.append("mem_used", now, 2)
        self.assertEqual(sorted(self.history.keys()), ["cpu", "mem_used"])


if __name__ == "__main__":
    unittest.main()
