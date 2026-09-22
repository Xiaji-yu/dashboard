"""校验 docs/API.md 与真实响应一致，防止「实现改了、文档没跟上」。

规则（兼顾 CI 虚机与真机的差异）：

* 文档里为每个接口列出的**顶层字段**必须与真实 payload 完全一致；
* 文档里写到的每条字段路径都要能在 payload 里解析出来；
* 但如果某一段整体不存在（例如虚机没有电池、没有 docker、没有温度传感器），
  或该段自己带 `available: false`（文档约定的降级形态），则跳过——这属于环境差异，不算文档漂移。

`/api/series` 段不在校验范围内：它文档化的是「可用 key」而不是响应字段。
"""

import http.client
import json
import os
import re
import tempfile
import threading
import time
import unittest

import server
from auth import AuthStore
from collector import Collector

DOC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "API.md")
# 文档里带字段表的接口；/api/series 文档化的是可用 key，不校验字段
CHECKED = {
    "/api/overview", "/api/performance", "/api/network",
    "/api/device", "/api/services", "/api/processes",
}
SECTION_RE = re.compile(r"^## `GET (?P<path>/api/[\w/]+)`")
FIELD_RE = re.compile(r"^\| `(?P<path>[^`]+)` \|")


def parse_doc(text):
    """把文档解析成 {接口: {顶层字段: [...], 字段路径: [...]}}。"""
    result = {}
    current = None
    for line in text.splitlines():
        match = SECTION_RE.match(line)
        if match:
            current = match.group("path")
            result[current] = {"top": [], "paths": []}
            continue
        if line.startswith("## "):
            current = None
            continue
        if current is None:
            continue
        if line.startswith("顶层字段："):
            result[current]["top"].extend(re.findall(r"`([^`]+)`", line))
        elif line.startswith("  "):
            result[current]["top"].extend(re.findall(r"`([^`]+)`", line))
        else:
            found = FIELD_RE.match(line)
            if found:
                result[current]["paths"].append(found.group("path"))
    return result


def walk_path(node, segments):
    """按路径段走下走一层；返回 (状态, 值)，状态含义见 resolve。"""
    if not segments:
        return "ok", node
    head, rest = segments[0], segments[1:]
    is_array = head.endswith("[]")
    key = head[:-2] if is_array else head
    if not isinstance(node, dict):
        return "degraded", None
    if key not in node:
        # 父级自己声明不可用（Linux 无电池、Windows 无温度…）→ 环境差异，不算漂移
        return ("degraded", None) if node.get("available") is False else ("missing", None)
    value = node[key]
    if not is_array:
        return walk_path(value, rest)
    if not isinstance(value, list) or not value:
        return "degraded", None
    # 数组元素：**任一元素**含该字段即可（不同接口有没有 IPv4 是环境差异，
    # 但字段名写错在哪个元素上都找不到）
    best = "missing"
    for item in value:
        status, found = walk_path(item, rest)
        if status == "ok":
            return status, found
        if status == "degraded":
            best = "degraded"
    return best, None


def resolve(payload, path):
    """按文档里的路径取值；返回 (状态, 值)。

    状态：ok=取到；degraded=父级声明不可用或数组为空（环境差异，跳过）；
    missing=父级存在且数组里有元素，但字段一个都没有（文档漂移）。
    """
    return walk_path(payload, path.split("."))


class ResolveRuleTest(unittest.TestCase):
    """resolve() 的判定规则：环境差异跳过、字段漂移报错、数组看任一元素。"""

    def test_missing_key_with_available_false_is_skipped(self):
        payload = {"temps": {"available": False, "reason": "没有传感器"}}
        self.assertEqual(resolve(payload, "temps.list"), ("degraded", None))

    def test_missing_key_on_present_parent_is_drift(self):
        """父级在、available 也是 true，但字段名对不上 → 文档漂移，必须报错。"""
        payload = {"temps": {"available": True, "reason": None}}
        self.assertEqual(resolve(payload, "temps.list"), ("missing", None))

    def test_present_key_with_empty_list_is_ok(self):
        """键存在但数组为空：字段没写错，不算漂移。"""
        payload = {"temps": {"available": True, "list": []}}
        self.assertEqual(resolve(payload, "temps.list"), ("ok", []))
        self.assertEqual(resolve(payload, "temps.list[]"), ("degraded", None))

    def test_array_passes_when_any_element_has_field(self):
        """CI 实测：有的接口没有 IPv4，但只要有一个接口有，就说明字段名没写错。"""
        payload = {"physical": [{"name": "eth0"},          # 没 ipv4
                                {"name": "eth1", "ipv4": "192.168.1.2"}]}
        self.assertEqual(resolve(payload, "physical[].ipv4"), ("ok", "192.168.1.2"))

    def test_array_all_elements_missing_is_drift(self):
        payload = {"physical": [{"name": "eth0"}, {"name": "eth1"}]}
        self.assertEqual(resolve(payload, "physical[].ipv4"), ("missing", None))

    def test_empty_array_is_environment_difference(self):
        payload = {"physical": []}
        self.assertEqual(resolve(payload, "physical[].ipv4"), ("degraded", None))

    def test_nested_object_inside_array(self):
        payload = {"containers": {"list": [{"name": "a", "image": "nginx"}]}}
        self.assertEqual(resolve(payload, "containers.list[].image"), ("ok", "nginx"))


class ApiDocsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.collector = Collector()
        cls.collector.sample()
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

        cls.tmp = tempfile.TemporaryDirectory()
        cls.auth = AuthStore(os.path.join(cls.tmp.name, "auth.json"), username="docs",
                             password="docs-pass-1234", logger=lambda message: None)
        cls.httpd = server.create_server(cls.collector, "127.0.0.1", 0, auth=cls.auth)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": "docs", "password": "docs-pass-1234"}),
                     headers={"Content-Type": "application/json"})
        cls.cookie = (conn.getresponse().getheader("Set-Cookie") or "").split(";")[0]
        conn.close()
        assert cls.cookie

        with open(DOC_PATH, encoding="utf-8") as handle:
            cls.doc = parse_doc(handle.read())

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    @classmethod
    def fetch(cls, path):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=15)
        try:
            conn.request("GET", path, headers={"Cookie": cls.cookie})
            response = conn.getresponse()
            return json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    def test_doc_covers_every_checked_endpoint(self):
        for path in CHECKED:
            self.assertIn(path, self.doc, f"文档缺少 {path} 的字段表")

    def test_documented_paths_exist(self):
        checked, checked_ok = 0, 0
        for path in sorted(CHECKED):
            payload = self.fetch(path)
            documented = self.doc[path]["paths"]
            self.assertTrue(documented, f"{path} 的字段表是空的")
            for field in documented:
                status, value = resolve(payload, field)
                checked += 1
                if status == "missing":
                    self.fail(f"{path} 文档里的 `{field}` 在真实响应里不存在"
                              f"（父级存在但缺该字段）")
                if status == "ok":
                    checked_ok += 1
        self.assertGreater(checked, 200, f"校验到的字段太少（{checked}），文档可能没解析对")
        self.assertGreater(checked_ok, 150, f"真正校验通过的字段太少（{checked_ok}）")

    def test_payload_has_no_undocumented_top_level_field(self):
        """实现新增了顶层字段而文档没更新时，这里会报警。"""
        for path in sorted(CHECKED):
            payload = self.fetch(path)
            documented = set(self.doc[path]["top"])
            missing = sorted(set(payload) - documented)
            self.assertEqual(missing, [], f"{path} 有未写进文档的顶层字段：{missing}")

    def test_series_keys_documented(self):
        """文档里列的曲线 key 必须是服务端认可的白名单成员。"""
        with open(DOC_PATH, encoding="utf-8") as handle:
            text = handle.read()
        section = text[text.index("## `GET /api/series`"):text.index("## `GET /api/overview`")]
        # 只看「可用 key」那张表：前面的参数表里是 keys/since，不是曲线 key
        keys_part = section[section.index("可用 key"):]
        keys = set(re.findall(r"^\| `([a-z_0-9]+)`(?: / `([a-z_0-9]+)`)? \|", keys_part, re.M))
        flat = {item for pair in keys for item in pair if item}
        self.assertTrue(flat, "没解析到任何曲线 key")
        for key in flat:
            self.assertTrue(server.valid_series_key(key), f"文档里的 key `{key}` 不被服务端接受")
        # 常见 key 一个都不能漏
        for key in ("cpu", "mem_used", "power", "net_down", "net_up", "disk_free", "temp"):
            self.assertIn(key, flat)


if __name__ == "__main__":
    unittest.main()
