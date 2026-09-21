"""服务层测试：序列映射、HTTP 接口契约、静态资源与路径穿越防护。

接口测试会在 127.0.0.1 的空闲端口上真起一个服务（port=0 交给内核分配），
所以不需要联网，也不会和在跑的 8282 抢端口。
"""

import http.client
import json
import threading
import time
import unittest

import server
from collector import Collector


def make_snapshot(available=True):
    """构造一份快照，用于测试序列映射（不依赖真实系统）。"""
    return {
        "ts": 1000.0,
        "host": "test-host",
        "cores": 4,
        "uptime_s": 3600.0,
        "cpu": {"available": available, "percent": 12.5, "freq_mhz": 3100},
        "memory": {"available": available, "used_gb": 4.0, "total_gb": 11.6, "percent": 34.4},
        "power": {"available": available, "watts": 8.8},
        "net": {"available": available, "nic": "eth0", "down_bps": 1024.0, "up_bps": 512.0},
        "disk": {"available": available, "path": "/", "free_gb": 93.2, "total_gb": 232.6,
                 "used_percent": 57.8},
        "temp": {"available": available, "celsius": 55.0, "source": "coretemp"},
        "load": {"available": available, "avg1": 0.3, "avg5": 0.4, "avg15": 0.5},
        "processes": [],
        "process_count": 100,
    }


class SeriesValuesTest(unittest.TestCase):
    def test_maps_available_metrics(self):
        values = server.series_values(make_snapshot(available=True))
        self.assertEqual(values["cpu"], 12.5)
        self.assertEqual(values["mem_used"], 4.0)
        self.assertEqual(values["power"], 8.8)
        self.assertEqual(values["net_down"], 1024.0)
        self.assertEqual(values["net_up"], 512.0)
        self.assertEqual(values["disk_free"], 93.2)
        self.assertEqual(values["temp"], 55.0)

    def test_unavailable_metrics_map_to_none(self):
        values = server.series_values(make_snapshot(available=False))
        for key in ("cpu", "mem_used", "power", "net_down", "net_up", "disk_free", "temp"):
            self.assertIsNone(values[key], key)

    def test_all_series_keys_are_covered(self):
        values = server.series_values(make_snapshot())
        self.assertEqual(sorted(values.keys()), sorted(server.SERIES_KEYS))


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.collector = Collector()
        cls.collector.sample()          # 预热差分型指标
        time.sleep(0.2)
        snapshot = cls.collector.sample()
        snapshot["processes"] = cls.collector.processes()
        snapshot["process_count"] = cls.collector.process_count()

        with server.state_lock:
            server.state["snapshot"] = snapshot
            server.state["series_ts"] = snapshot["ts"]
            for key, value in server.series_values(snapshot).items():
                server.state["history"].append(key, snapshot["ts"], value)

        cls.httpd = server.create_server(cls.collector, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def request(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.getheader("Content-Type") or "", response.read()
        finally:
            conn.close()

    def test_overview_contract(self):
        status, ctype, body = self.request("/api/overview")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        payload = json.loads(body)
        self.assertTrue(payload["ready"])
        for key in ("ts", "host", "cores", "cpu", "memory", "power", "net", "disk",
                    "temp", "load", "processes", "process_count", "services", "window"):
            self.assertIn(key, payload)
        self.assertIsInstance(payload["processes"], list)
        self.assertIsInstance(payload["services"], list)
        for item in payload["services"]:
            self.assertIn("group", item)
            self.assertIn("status", item)

    def test_series_full_window(self):
        status, ctype, body = self.request("/api/series")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        payload = json.loads(body)
        self.assertIn("series", payload)
        self.assertGreater(payload["ts"], 0)
        for key in server.SERIES_KEYS:
            self.assertIn(key, payload["series"])

    def test_series_incremental_returns_nothing_for_current_ts(self):
        _, _, body = self.request("/api/series")
        since = json.loads(body)["ts"]
        _, _, body2 = self.request(f"/api/series?since={since}")
        payload = json.loads(body2)
        total = sum(len(points) for points in payload["series"].values())
        self.assertEqual(total, 0)

    def test_series_invalid_since_falls_back_to_full_window(self):
        status, _, body = self.request("/api/series?since=abc")
        self.assertEqual(status, 200)
        self.assertIn("series", json.loads(body))

    def test_index_page_served(self):
        status, ctype, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"<title>", body)

    def test_static_assets_served_with_types(self):
        for path, expected in (("/static/app.js", "javascript"),
                               ("/static/style.css", "text/css"),
                               ("/static/chart.js", "javascript")):
            status, ctype, body = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertIn(expected, ctype, path)
            self.assertTrue(body, path)

    def test_unknown_path_returns_404(self):
        status, _, _ = self.request("/definitely-not-here")
        self.assertEqual(status, 404)

    def test_path_traversal_is_blocked(self):
        for path in ("/static/../server.py", "/static/../../etc/passwd", "/static/..%2fserver.py"):
            status, _, body = self.request(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"import", body, path)


class OverviewBeforeFirstSampleTest(unittest.TestCase):
    def test_not_ready_payload(self):
        """采样线程还没产出第一份快照时，接口应返回 ready=False 而不是报错。"""
        with server.state_lock:
            original = server.state["snapshot"]
            server.state["snapshot"] = None
        try:
            payload = server.Handler._overview(self._handler_stub())
        finally:
            with server.state_lock:
                server.state["snapshot"] = original
        self.assertFalse(payload["ready"])

    @staticmethod
    def _handler_stub():
        """只需要一个能提供 collector 属性的对象。"""
        return type("Stub", (), {"collector": None})()


if __name__ == "__main__":
    unittest.main()
