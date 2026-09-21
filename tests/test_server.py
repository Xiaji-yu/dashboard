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


class PerformanceSeriesTest(unittest.TestCase):
    """performance 段 -> 性能页曲线序列（cpu0..3、风扇、GPU、ACPI 温度）。"""

    def test_extracts_perf_series(self):
        snapshot = {
            "performance": {
                "cpu": {"available": True, "percent": 10.0,
                        "per_core": {"available": True, "per_cpu": [5.0, 6.0, 7.0, 8.0]}},
                "fans": {"available": True,
                         "list": [{"key": "cpu_fan", "label": "CPU 风扇", "rpm": 2600},
                                  {"key": "gpu_fan", "label": "GPU 风扇", "rpm": 0}]},
                "gpu": {"available": True, "freq_mhz": 350, "max_mhz": 1000},
                "temps": {"available": True,
                          "list": [{"key": "acpi", "label": "机身温区", "celsius": 60.0},
                                   {"key": "package", "label": "CPU 封装", "celsius": 62.0}]},
            }
        }
        values = server.performance_series(snapshot)
        self.assertEqual(values, {
            "cpu0": 5.0, "cpu1": 6.0, "cpu2": 7.0, "cpu3": 8.0, "cpu_max": 8.0,
            "fan_cpu": 2600, "gpu_mhz": 350, "temp_acpi": 60.0,
        })

    def test_missing_performance_section_maps_to_empty(self):
        self.assertEqual(server.performance_series(make_snapshot()), {})
        self.assertEqual(server.performance_series({}), {})

    def test_unavailable_pieces_are_skipped(self):
        snapshot = {"performance": {"cpu": {"per_core": {"available": False}},
                                    "fans": {"available": False}}}
        self.assertEqual(server.performance_series(snapshot), {})


class SeriesKeyWhitelistTest(unittest.TestCase):
    def test_known_keys_pass(self):
        for key in server.SERIES_KEYS:
            self.assertTrue(server.valid_series_key(key), key)
        for key in server.PERF_SERIES_KEYS:
            self.assertTrue(server.valid_series_key(key), key)
        self.assertTrue(server.valid_series_key("cpu0"))
        self.assertTrue(server.valid_series_key("cpu23"))

    def test_unknown_keys_rejected(self):
        for key in ("temp2x", "mem", "../etc", "cpu;rm", ""):
            self.assertFalse(server.valid_series_key(key), repr(key))


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.collector = Collector()
        cls.collector.sample()          # 预热差分型指标
        time.sleep(0.2)
        snapshot = cls.collector.sample()
        rows = cls.collector.process_list()
        snapshot["processes"] = rows[:6]
        snapshot["process_count"] = len(rows)

        with server.state_lock:
            server.state["snapshot"] = snapshot
            server.state["processes"] = rows
            server.state["network"] = cls.collector.network_info()
            server.state["services"] = cls.collector.services_detail()
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
                    "temp", "load", "processes", "process_count", "services", "window",
                    "interval"):
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
        self.assertGreater(payload["interval"], 0, "要带上采样间隔，前端据此设置断线阈值")
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

    def test_performance_contract(self):
        status, ctype, body = self.request("/api/performance")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        payload = json.loads(body)
        self.assertTrue(payload["ready"])
        for key in ("ts", "cpu", "gpu", "temps", "fans", "battery", "memory", "load",
                    "power", "window", "interval"):
            self.assertIn(key, payload, key)
        self.assertIsInstance(payload["temps"].get("list", []), list)

    def test_processes_contract(self):
        status, ctype, body = self.request("/api/processes")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        payload = json.loads(body)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["count"], len(payload["processes"]))
        self.assertGreater(payload["count"], 10)
        first = payload["processes"][0]
        for key in ("pid", "name", "user", "cpu", "rss_mb", "status", "threads",
                    "started", "cmd", "container"):
            self.assertIn(key, first, key)

    def test_device_contract(self):
        status, ctype, body = self.request("/api/device")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        payload = json.loads(body)
        self.assertTrue(payload["ready"])
        for key in ("summary", "usb", "bluetooth", "interfaces", "pci", "runtime"):
            self.assertIn(key, payload, key)
        self.assertTrue(payload["summary"]["hostname"])
        self.assertIn("list", payload["usb"])

    def test_services_contract(self):
        status, ctype, body = self.request("/api/services")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        payload = json.loads(body)
        self.assertTrue(payload["ready"])
        for key in ("containers", "systemd", "ports", "probes", "ts", "interval"):
            self.assertIn(key, payload, key)
        self.assertIn("path", payload["probes"])
        self.assertIsInstance(payload["ports"], list)
        if payload["ports"]:
            row = payload["ports"][0]
            for key in ("port", "proto", "addr", "scope", "process", "known"):
                self.assertIn(key, row, key)

    def test_network_contract(self):
        status, ctype, body = self.request("/api/network")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        payload = json.loads(body)
        self.assertTrue(payload["ready"])
        for key in ("nic", "connection", "disk", "net", "interval", "ts"):
            self.assertIn(key, payload, key)
        self.assertIn("mounts", payload["disk"])
        self.assertIn("read_bps", payload["disk"])

    def test_overview_keeps_process_list_small(self):
        """概览快照只带前 6 条，完整列表由 /api/processes 单独提供。"""
        _, _, body = self.request("/api/overview")
        payload = json.loads(body)
        self.assertLessEqual(len(payload["processes"]), 6)
        self.assertGreater(payload["process_count"], 10)

    def test_series_keys_param_filters_output(self):
        status, _, body = self.request("/api/series?keys=cpu,temp")
        self.assertEqual(status, 200)
        series = json.loads(body)["series"]
        self.assertEqual(sorted(series.keys()), ["cpu", "temp"])

    def test_series_keys_param_supports_perf_keys(self):
        status, _, body = self.request("/api/series?keys=fan_cpu,gpu_mhz,cpu0")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(json.loads(body)["series"].keys()),
                         ["cpu0", "fan_cpu", "gpu_mhz"])

    def test_series_unknown_key_returns_400(self):
        status, _, _ = self.request("/api/series?keys=cpu,definitely-not-a-key")
        self.assertEqual(status, 400)

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
