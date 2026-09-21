"""服务层测试：序列映射、HTTP 接口契约、静态资源与路径穿越防护。

接口测试会在 127.0.0.1 的空闲端口上真起一个服务（port=0 交给内核分配），
所以不需要联网，也不会和在跑的 8282 抢端口。
"""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest

import server
from auth import AuthStore
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
    """已登录状态下的接口契约。服务建在空闲端口上，不和在跑的 8282 抢。"""

    PASSWORD = "http-test-pass-1"

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
            server.state["series_ts"] = snapshot["ts"]
            server.state["processes"] = rows
            server.state["network"] = cls.collector.network_info()
            server.state["services"] = cls.collector.services_detail()
            for key, value in server.series_values(snapshot).items():
                server.state["history"].append(key, snapshot["ts"], value)

        cls.tmp = tempfile.TemporaryDirectory()
        cls.auth = AuthStore(os.path.join(cls.tmp.name, "auth.json"), username="tester",
                             password=cls.PASSWORD, logger=lambda message: None)
        cls.httpd = server.create_server(cls.collector, "127.0.0.1", 0, auth=cls.auth)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

        status, headers, _body = cls.raw("POST", "/api/login",
                                         {"username": "tester", "password": cls.PASSWORD})
        assert status == 200, f"登录失败：{status}"
        cls.cookie = (headers.get("set-cookie") or "").split(";")[0]
        assert cls.cookie, "登录应当下发会话 Cookie"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    @classmethod
    def raw(cls, method, path, payload=None, cookie=None,
            content_type="application/json"):
        """发原始请求，返回 (状态码, 小写响应头, 响应体)。"""
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        try:
            headers = {}
            if cookie:
                headers["Cookie"] = cookie
            body = None
            if payload is not None:
                body = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = content_type
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
            return (response.status,
                    {key.lower(): value for key, value in response.getheaders()}, data)
        finally:
            conn.close()

    def request(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers={"Cookie": self.cookie})
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
        for key in ("summary", "usb", "bluetooth", "interfaces", "lan", "runtime"):
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


class AuthFlowTest(unittest.TestCase):
    """登录流程：未登录拦截、错密码、CSRF、改密、退出、限速。每个用例独立起一个服务。"""

    PASSWORD = "flow-pass-1"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth = AuthStore(os.path.join(self.tmp.name, "auth.json"), username="tester",
                              password=self.PASSWORD, logger=lambda message: None)
        self.httpd = server.create_server(Collector(), "127.0.0.1", 0, auth=self.auth)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def raw(self, method, path, payload=None, cookie=None, content_type="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {}
            if cookie:
                headers["Cookie"] = cookie
            body = None
            if payload is not None:
                body = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = content_type
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
            return (response.status,
                    {key.lower(): value for key, value in response.getheaders()}, data)
        finally:
            conn.close()

    def login(self, password=None):
        status, headers, body = self.raw("POST", "/api/login", {
            "username": "tester", "password": self.PASSWORD if password is None else password})
        return status, headers, body

    def test_protected_api_needs_session(self):
        status, headers, body = self.raw("GET", "/api/overview")
        self.assertEqual(status, 401)
        self.assertIn("application/json", headers.get("content-type", ""))
        self.assertEqual(json.loads(body)["error"], "unauthorized")

    def test_page_redirects_to_login(self):
        status, headers, _body = self.raw("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("location"), "/login")
        status, headers, body = self.raw("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn(b"login-form", body)

    def test_login_page_assets_are_public(self):
        for path, expected in (("/static/style.css", "text/css"),
                               ("/static/login.js", "javascript")):
            status, headers, body = self.raw("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(expected, headers.get("content-type", ""), path)
            self.assertTrue(body)

    def test_app_assets_need_session(self):
        for path in ("/static/app.js", "/static/pages/overview.js", "/"):
            status, _headers, _body = self.raw("GET", path)
            self.assertIn(status, (302, 401), path)

    def test_login_success_sets_httponly_cookie(self):
        status, headers, body = self.login()
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        cookie = headers.get("set-cookie") or ""
        self.assertIn(server.SESSION_COOKIE + "=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        token = cookie.split(";")[0]
        status, _headers, body = self.raw("GET", "/api/auth", cookie=token)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["authenticated"])

    def test_login_wrong_password(self):
        status, _headers, body = self.login("wrong-password")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "invalid_credentials")

    def test_login_rejects_non_json(self):
        """跨站表单发不了 JSON，所以要求 JSON 内容类型即可挡掉 CSRF。"""
        status, _headers, _body = self.raw(
            "POST", "/api/login", {"username": "tester", "password": self.PASSWORD},
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(status, 403)

    def test_login_rate_limited_after_failures(self):
        for _ in range(5):
            self.login("wrong-password")
        status, _headers, body = self.login("wrong-password")
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"], "rate_limited")
        self.assertIn("秒后再试", json.loads(body)["message"])

    def test_logout_invalidates_session(self):
        _status, headers, _body = self.login()
        cookie = (headers.get("set-cookie") or "").split(";")[0]
        status, _headers, _body = self.raw("POST", "/api/logout", {}, cookie=cookie)
        self.assertEqual(status, 200)
        status, _headers, _body = self.raw("GET", "/api/overview", cookie=cookie)
        self.assertEqual(status, 401)

    def test_change_password_flow(self):
        _status, headers, _body = self.login()
        cookie = (headers.get("set-cookie") or "").split(";")[0]
        # 另一台设备也登录着，改密后应当被踢掉
        _status, other_headers, _body = self.login()
        other = (other_headers.get("set-cookie") or "").split(";")[0]

        status, _headers, body = self.raw("POST", "/api/password", {
            "old_password": self.PASSWORD, "new_password": "brand-new-pass-2"}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.raw("GET", "/api/overview", cookie=other)[0], 401,
                         "其他设备应被踢下线")
        self.assertEqual(self.raw("GET", "/api/overview", cookie=cookie)[0], 200,
                         "当前设备应保持登录")
        self.assertEqual(self.login(self.PASSWORD)[0], 401, "旧密码应失效")
        self.assertEqual(self.login("brand-new-pass-2")[0], 200, "新密码应可用")

    def test_change_password_wrong_old(self):
        _status, headers, _body = self.login()
        cookie = (headers.get("set-cookie") or "").split(";")[0]
        status, _headers, body = self.raw("POST", "/api/password", {
            "old_password": "nope", "new_password": "brand-new-pass-2"}, cookie=cookie)
        self.assertEqual(status, 400)
        self.assertIn("当前密码不正确", json.loads(body)["message"])

    def test_change_password_needs_session(self):
        status, _headers, _body = self.raw("POST", "/api/password", {
            "old_password": self.PASSWORD, "new_password": "brand-new-pass-2"})
        self.assertEqual(status, 401)


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
