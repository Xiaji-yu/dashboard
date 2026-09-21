#!/usr/bin/env python3
"""8282 总控台：零依赖 HTTP 服务（Python 标准库 + psutil）。

路由：
  GET /                      单页应用入口（前端 hash 路由切换页面）
  GET /static/*              前端资源
  GET /api/overview          瞬时快照：指标 + 最忙进程 + 服务状态
  GET /api/performance       性能与电源：每核占用、温度、风扇、GPU、电池、内存构成
  GET /api/series?since=TS   曲线数据；省略 since 返回整个时间窗口
  GET /api/series?keys=a,b   只返回指定曲线键（未知键返回 400）

环境变量：
  DASHBOARD_HOST  监听地址，默认 0.0.0.0
  DASHBOARD_PORT  监听端口，默认 8282
  DASHBOARD_INTERVAL  采样间隔秒，默认 1.0
  DASHBOARD_NIC   指定网卡（默认自动挑物理网卡）
  DASHBOARD_DISK  指定磁盘挂载点，默认 /
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from collector import Collector
from history import History, WINDOW_SECONDS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("DASHBOARD_PORT", "8282"))
INTERVAL = float(os.environ.get("DASHBOARD_INTERVAL", "1.0"))

# 曲线序列：键 -> 取自快照的哪个字段
SERIES_KEYS = ("cpu", "mem_used", "power", "net_down", "net_up", "disk_free", "temp")

# 性能页额外序列（从快照 performance 段提取，见 performance_series）与每核键的白名单
PERF_SERIES_KEYS = ("fan_cpu", "gpu_mhz", "temp_acpi", "cpu_max")
PERCORE_KEY_RE = re.compile(r"cpu\d{1,2}")

state_lock = threading.Lock()
state = {"snapshot": None, "history": History(), "series_ts": 0.0, "processes": []}


def valid_series_key(key):
    return key in SERIES_KEYS or key in PERF_SERIES_KEYS or bool(PERCORE_KEY_RE.fullmatch(key))


def performance_series(snapshot):
    """从快照 performance 段提取性能页曲线序列；该段缺失时返回空。"""
    perf = snapshot.get("performance") or {}
    out = {}
    cores = (perf.get("cpu") or {}).get("per_core") or {}
    if cores.get("available"):
        per_cpu = cores.get("per_cpu") or []
        for index, percent in enumerate(per_cpu):
            out[f"cpu{index}"] = percent
        if per_cpu:
            out["cpu_max"] = max(per_cpu)
    fans = perf.get("fans") or {}
    if fans.get("available"):
        for fan in fans["list"]:
            if fan["key"] == "cpu_fan":
                out["fan_cpu"] = fan["rpm"]
    gpu = perf.get("gpu") or {}
    if gpu.get("available"):
        out["gpu_mhz"] = gpu["freq_mhz"]
    temps = perf.get("temps") or {}
    if temps.get("available"):
        for entry in temps["list"]:
            if entry["key"] == "acpi":
                out["temp_acpi"] = entry["celsius"]
    return out


def series_values(snapshot):
    """把快照摊平成曲线序列，None 表示该点无数据。"""
    cpu = snapshot["cpu"]
    memory = snapshot["memory"]
    power = snapshot["power"]
    net = snapshot["net"]
    disk = snapshot["disk"]
    temp = snapshot["temp"]
    return {
        "cpu": cpu["percent"] if cpu["available"] else None,
        "mem_used": memory["used_gb"] if memory["available"] else None,
        "power": power.get("watts") if power["available"] else None,
        "net_down": net["down_bps"] if net["available"] else None,
        "net_up": net["up_bps"] if net["available"] else None,
        "disk_free": disk["free_gb"] if disk["available"] else None,
        "temp": temp["celsius"] if temp["available"] else None,
    }


def sampler(collector):
    """后台采样线程：即使单次采集失败也继续跑。"""
    while True:
        started = time.time()
        try:
            snapshot = collector.sample()
            rows = collector.process_list()
            # 完整进程表放在 state 里单独服务 /api/processes，
            # 概览快照只留前 6 条，避免每次轮询都拖着 30KB 的列表。
            snapshot["processes"] = rows[:6]
            snapshot["process_count"] = len(rows)
            with state_lock:
                state["snapshot"] = snapshot
                state["series_ts"] = snapshot["ts"]
                state["processes"] = rows
                for key, value in series_values(snapshot).items():
                    state["history"].append(key, snapshot["ts"], value)
                for key, value in performance_series(snapshot).items():
                    state["history"].append(key, snapshot["ts"], value)
        except Exception as exc:  # 采集异常不应终止采样
            print(f"[collector] 采样失败：{exc}", flush=True)
        time.sleep(max(0.05, INTERVAL - (time.time() - started)))


class Handler(BaseHTTPRequestHandler):
    server_version = "Dashboard/1.0"
    protocol_version = "HTTP/1.1"
    collector = None

    # ---------------- 路由 ----------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/overview":
            return self._send_json(self._overview())
        if path == "/api/performance":
            return self._send_json(self._performance())
        if path == "/api/processes":
            return self._send_json(self._processes())
        if path == "/api/series":
            query = parse_qs(parsed.query)
            raw = query.get("since", ["0"])[0]
            try:
                since_ts = float(raw)
            except ValueError:
                since_ts = 0.0
            keys = None
            raw_keys = query.get("keys", [""])[0]
            if raw_keys:
                keys = [item.strip() for item in raw_keys.split(",") if item.strip()]
                unknown = [item for item in keys if not valid_series_key(item)]
                if unknown:
                    return self.send_error(400, f"unknown series keys: {', '.join(unknown)}")
                keys = keys or None
            return self._send_json(self._series(since_ts, keys))
        if path in ("/", "/index.html"):
            return self._send_file(os.path.join(STATIC_DIR, "index.html"))
        if path.startswith("/static/"):
            rel = posixpath.normpath(path[len("/static/"):]).lstrip("/")
            if rel.startswith("..") or os.path.isabs(rel):
                return self.send_error(404)
            target = os.path.join(STATIC_DIR, rel)
            if os.path.isfile(target):
                return self._send_file(target)
        return self.send_error(404)

    # ---------------- 数据 ----------------

    def _overview(self):
        with state_lock:
            snapshot = state["snapshot"]
        if snapshot is None:
            return {"ready": False, "window": WINDOW_SECONDS}
        payload = dict(snapshot)
        payload["ready"] = True
        payload["window"] = WINDOW_SECONDS
        payload["interval"] = INTERVAL
        payload["services"] = self.collector.services()
        return payload

    def _performance(self):
        with state_lock:
            snapshot = state["snapshot"]
        if snapshot is None:
            return {"ready": False, "window": WINDOW_SECONDS}
        payload = dict(snapshot.get("performance") or {})
        payload["ready"] = True
        payload["ts"] = snapshot["ts"]
        payload["window"] = WINDOW_SECONDS
        payload["interval"] = INTERVAL
        return payload

    @staticmethod
    def _processes():
        with state_lock:
            rows = state["processes"]
            snapshot = state["snapshot"]
        if not rows or snapshot is None:
            return {"ready": False, "window": WINDOW_SECONDS}
        return {"ready": True, "ts": snapshot["ts"], "count": len(rows),
                "interval": INTERVAL, "processes": rows}

    @staticmethod
    def _series(since_ts, keys=None):
        selected = keys if keys else SERIES_KEYS
        with state_lock:
            history = state["history"]
            ts = state["series_ts"]
        if since_ts <= 0:
            series = {key: history.window(key) for key in selected}
        else:
            series = {key: history.since(key, since_ts) for key in selected}
        return {"ts": ts, "window": WINDOW_SECONDS, "interval": INTERVAL, "series": series}

    # ---------------- 响应 ----------------

    def _send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            return self.send_error(404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # 默认每条请求都打日志；这里只保留错误，避免刷屏
        status = args[1] if len(args) > 1 else ""
        if str(status).startswith(("4", "5")):
            print(f"[http] {self.address_string()} {fmt % args}", flush=True)


def create_server(collector, host=HOST, port=PORT):
    """构建（但不启动）HTTP 服务。

    返回一个绑定了该 collector 的 ThreadingHTTPServer，调用方负责 serve_forever()。
    测试里传 port=0 让内核分配空闲端口，避免和在跑的服务抢 8282。
    """
    bound_handler = type("BoundHandler", (Handler,), {"collector": collector})
    return ThreadingHTTPServer((host, port), bound_handler)


def lan_address():
    """问内核「本机默认出口的源地址」。UDP connect 不发包，无外网也能用。"""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return "127.0.0.1"


def main():
    collector = Collector()
    threading.Thread(target=sampler, args=(collector,), daemon=True).start()

    try:
        httpd = create_server(collector, HOST, PORT)
    except OSError as exc:
        raise SystemExit(f"无法监听 {HOST}:{PORT} —— {exc}")

    lan_ip = lan_address()
    print(f"总控台已启动：http://127.0.0.1:{PORT}/   局域网：http://{lan_ip}:{PORT}/", flush=True)
    print(f"网卡 {collector.nic} ｜ 磁盘 {collector.disk_path} ｜ "
          f"{collector.cores} 核 ｜ 采样 {INTERVAL}s ｜ 窗口 {WINDOW_SECONDS:.0f}s", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
