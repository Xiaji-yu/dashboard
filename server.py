#!/usr/bin/env python3
"""8282 总控台：零依赖 HTTP 服务（Python 标准库 + psutil）。

路由：
  GET  /                     单页应用入口（前端 hash 路由切换页面）
  GET  /login                登录页
  GET  /api/auth             当前登录状态
  POST /api/login            登录（JSON：username / password）
  POST /api/logout           退出登录
  POST /api/password         修改密码 / 用户名（需登录）
  GET  /api/tokens           列出只读 API 令牌（仅页面会话）
  POST /api/tokens           新建只读令牌，完整令牌只返回一次（仅页面会话）
  POST /api/tokens/revoke    撤销只读令牌（仅页面会话）

只读令牌：`Authorization: Bearer <token>` 或 `?token=<token>`，只能访问 GET 接口。
  GET /static/*              前端资源
  GET /api/overview          瞬时快照：指标 + 最忙进程 + 服务状态
  GET /api/performance       性能与电源：每核占用、温度、风扇、GPU、电池、内存构成
  GET /api/processes         全部进程（含命令行、用户、容器归属）
  GET /api/network           网络与磁盘：网卡、连接、网关/外网延迟、磁盘容量与读写
  GET /api/services          服务：容器、systemd 服务、监听端口、远程探测延迟
  GET /api/device            设备：主机、处理器、内存磁盘、网络、运行环境（60 秒缓存）
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
import sys
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from auth import (MAX_TOKENS, SESSION_TTL, AuthConfigError, AuthStore, auth_file_path)
from runtime import announce_initial_credentials, data_dir, is_frozen, resource_dir
from collector import Collector
from history import History, WINDOW_SECONDS

SESSION_COOKIE = "dsh_session"
MAX_BODY_BYTES = 8192


def trust_proxy():
    """是否信任反向代理发来的 X-Forwarded-For。

    默认关闭：否则任何人都能伪造该头绕过登录限速。
    部署在 Nginx/Caddy 后面时设 DASHBOARD_TRUST_PROXY=1（见 deploy/README.md）。
    """
    return os.environ.get("DASHBOARD_TRUST_PROXY", "").strip().lower() in (
        "1", "true", "yes", "on")


def bearer_token(headers):
    """从 `Authorization: Bearer <token>` 取只读令牌。"""
    raw = headers.get("Authorization") or ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip() or None
    return None


def redact_token(text):
    """把 URL 里的 token 参数抹掉，避免令牌被写进日志。"""
    return re.sub(r"(token=)[^&\s\"]+", r"\1<redacted>", text or "")


def client_ip(headers, peer, trust=False):
    """取真实来源 IP：只有显式信任代理时才看 X-Forwarded-For / X-Real-IP。"""
    if trust:
        forwarded = headers.get("X-Forwarded-For") or headers.get("X-Real-IP")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    return peer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 静态资源必须走 resource_dir()：打包成 onefile exe 后它们是解包到临时目录的，
# 用 __file__ 拼路径会指向不存在的目录
STATIC_DIR = os.path.join(resource_dir(), "static")
HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("DASHBOARD_PORT", "8282"))
INTERVAL = float(os.environ.get("DASHBOARD_INTERVAL", "1.0"))

# 曲线序列：键 -> 取自快照的哪个字段
SERIES_KEYS = ("cpu", "mem_used", "power", "net_down", "net_up", "disk_free", "temp",
               "disk_read", "disk_write")

# 性能页额外序列（从快照 performance 段提取，见 performance_series）与每核键的白名单
PERF_SERIES_KEYS = ("fan_cpu", "gpu_mhz", "temp_acpi", "cpu_max")
PERCORE_KEY_RE = re.compile(r"cpu\d{1,2}")

state_lock = threading.Lock()
state = {"snapshot": None, "history": History(), "series_ts": 0.0,
         "processes": [], "network": {}, "services": {}}


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
        "disk_read": disk.get("read_bps") if disk["available"] else None,
        "disk_write": disk.get("write_bps") if disk["available"] else None,
        "temp": temp["celsius"] if temp["available"] else None,
    }


def sample_once(collector):
    """跑一轮采集并发布到 state（拆出来便于测试与逐段兜底）。

    关键点：任何一段失败都只丢那一段，**快照本身照常发布**。
    否则像 services_detail() 抛异常这种小问题会让整站数字永久停住。
    """
    snapshot = collector.sample()
    try:
        rows = collector.process_list()
    except Exception as exc:
        print(f"[collector] 进程列表失败：{exc}", flush=True)
        with state_lock:
            rows = state.get("processes") or []
    snapshot["processes"] = rows[:6]
    snapshot["process_count"] = len(rows)

    try:
        network = collector.network_info()
    except Exception as exc:
        print(f"[collector] 网络信息失败：{exc}", flush=True)
        network = None
    try:
        services = collector.services_detail()
    except Exception as exc:
        print(f"[collector] 服务信息失败：{exc}", flush=True)
        services = None

    with state_lock:
        if network is not None:
            state["network"] = network
        if services is not None:
            state["services"] = services
        state["snapshot"] = snapshot
        state["series_ts"] = snapshot["ts"]
        state["processes"] = rows
        for key, value in series_values(snapshot).items():
            state["history"].append(key, snapshot["ts"], value)
        for key, value in performance_series(snapshot).items():
            state["history"].append(key, snapshot["ts"], value)
    return snapshot


def sampler(collector):
    """后台采样线程：即使单次采集失败也继续跑。"""
    while True:
        started = time.time()
        try:
            sample_once(collector)
        except Exception as exc:  # 采集异常不应终止采样
            print(f"[collector] 采样失败：{exc}", flush=True)
        time.sleep(max(0.05, INTERVAL - (time.time() - started)))


class Handler(BaseHTTPRequestHandler):
    server_version = "Dashboard/1.0"
    protocol_version = "HTTP/1.1"
    collector = None
    auth = None

    # ---------------- 路由 ----------------

    # ---------------- 鉴权 ----------------

    def _token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return None

    def _session(self):
        """当前会话；未登录返回 None。"""
        if self.auth is None:
            return None
        return self.auth.session(self._token())

    def _auth_context(self, parsed=None):
        """返回 ("token", 记录) / ("session", 会话) / (None, None)。

        只读令牌可用 `Authorization: Bearer` 或 `?token=` 携带；令牌无效时**不回退**成会话，
        避免「带着坏令牌却用 Cookie 通过了」这种歧义。
        """
        if self.auth is None:
            return None, None
        token = bearer_token(self.headers)
        if not token and parsed is not None:
            token = (parse_qs(parsed.query).get("token", [""])[0] or "").strip() or None
        if token:
            record = self.auth.verify_token(token)
            return ("token", record) if record else (None, None)
        session = self.auth.session(self._token())
        return ("session", session) if session else (None, None)

    @staticmethod
    def _read_only_error():
        return {"error": "read_only",
                "message": "该只读令牌不能执行写操作（改密码/退出/管理令牌请用页面登录）"}

    @staticmethod
    def _session_cookie(token):
        return (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={SESSION_TTL}")

    @staticmethod
    def _clear_cookie():
        return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _read_body(self):
        """把请求体整块读掉（含超长时读满上限后关连接）。

        必须真的排空：HTTP/1.1 是长连接，没读掉的字节会被当成下一条请求行，
        被 log_message 原样打出来——登录密码就这么明文进过日志。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return b""
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            self.close_connection = True          # 超出上限的剩余部分不再留在连接里
            length = MAX_BODY_BYTES
            return self._read_exactly(length)
        return self._read_exactly(length)

    def _read_exactly(self, length):
        chunks = []
        remaining = length
        while remaining > 0:
            try:
                chunk = self.rfile.read(min(remaining, 4096))
            except OSError:
                self.close_connection = True
                break
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_json(self, raw=None):
        """解析请求体 JSON；raw 为 None 时自行读取。格式不对返回 None。"""
        if raw is None:
            raw = self._read_body()
        if not raw or len(raw) > MAX_BODY_BYTES:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _post_allowed(self):
        """挡跨站表单：要求 JSON 内容类型，且 Origin 若存在必须同源。"""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return origin.split("//")[-1].rstrip("/") == host

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        raw = self._read_body()          # 无论结果如何都要读掉，避免密码被当成请求行回显到日志
        if path not in ("/api/login", "/api/logout", "/api/password",
                        "/api/tokens", "/api/tokens/revoke"):
            return self.send_error(404)
        if not self._post_allowed():
            return self._send_json(
                {"error": "forbidden", "message": "需要同源 JSON 请求"}, status=403)
        payload = self._read_json(raw)
        if not isinstance(payload, dict):
            return self._send_json(
                {"error": "bad_request", "message": "请求体需要是 JSON 对象"}, status=400)

        if path == "/api/login":
            return self._login(payload)

        kind, _info = self._auth_context(urlparse(self.path))
        if kind == "token":
            # 只读令牌不能做任何写操作（含管理令牌、退出、改密码）
            return self._send_json(self._read_only_error(), status=403)

        if path == "/api/tokens":
            if kind != "session":
                return self._send_json({"error": "unauthorized"}, status=401)
            record, token = self.auth.create_token(payload.get("name"))
            return self._send_json({"ok": True, "token": token, "record": record})

        if path == "/api/tokens/revoke":
            if kind != "session":
                return self._send_json({"error": "unauthorized"}, status=401)
            ok = self.auth.revoke_token((payload.get("id") or "").strip())
            return self._send_json(
                {"ok": ok, "message": "已撤销" if ok else "没有这个令牌"},
                status=200 if ok else 404)

        if path == "/api/logout":
            token = self._token()
            if self.auth:
                if payload.get("all") is True:
                    self.auth.logout_all()          # 退出所有设备（含当前）
                elif token:
                    self.auth.logout(token)
            return self._send_json({"ok": True}, cookies=[self._clear_cookie()])
        return self._change_password(payload)

    def _login(self, payload):
        if self.auth is None:
            return self._send_json({"error": "auth_disabled"}, status=503)
        username = payload.get("username")
        password = payload.get("password")
        # 非字符串（数组/对象/数字）直接拒掉：别让它一路走到 .strip()/.encode() 抛异常
        if not isinstance(username, str) or not isinstance(password, str):
            return self._send_json(
                {"error": "bad_request", "message": "账号与密码都必须是字符串"}, status=400)

        ip = client_ip(self.headers, self.client_address[0], trust_proxy())
        wait = self.auth.retry_after(ip)
        if wait:
            return self._send_json(
                {"error": "rate_limited",
                 "message": f"失败次数过多，请 {wait} 秒后再试"},
                status=429, extra_headers={"Retry-After": str(wait)})
        token = self.auth.login(username, password, ip)
        if not token:
            # 记下来源与账号（不含密码），方便发现有人在猜口令
            print(f"[auth] 登录失败 ip={ip} user={username[:32]!r}", flush=True)
            return self._send_json(
                {"error": "invalid_credentials", "message": "账号或密码不正确"}, status=401)
        print(f"[auth] 登录成功 ip={ip} user={self.auth.username()}", flush=True)
        return self._send_json({"ok": True, "username": self.auth.username()},
                               cookies=[self._session_cookie(token)])

    def _change_password(self, payload):
        session = self._session()
        if not session:
            return self._send_json({"error": "unauthorized"}, status=401)
        ok, message = self.auth.change_credentials(
            self._token(), payload.get("old_password"), payload.get("new_password"),
            (payload.get("new_username") or "").strip() or None)
        return self._send_json({"ok": ok, "message": message},
                               status=200 if ok else 400)

    # ---------------- 路由 ----------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        # 登录页与它的资源不需要鉴权
        if path in ("/static/style.css", "/static/login.js"):
            return self._send_file(os.path.join(STATIC_DIR, os.path.basename(path)))
        if path == "/api/auth":
            kind, info = self._auth_context(parsed)
            return self._send_json({
                "authenticated": bool(kind),
                "kind": kind,
                "username": (info or {}).get("username") if kind == "session" else None,
                "token_name": (info or {}).get("name") if kind == "token" else None,
                "host": socket.gethostname()})
        if path == "/login":
            if self._session():
                return self._redirect("/")
            return self._send_file(os.path.join(STATIC_DIR, "login.html"))
        kind, info = self._auth_context(parsed)
        if kind is None:
            # 未登录：接口给 401，页面与前端资源跳登录页
            if path.startswith("/api/"):
                return self._send_json({"error": "unauthorized"}, status=401)
            return self._redirect("/login")

        if path == "/api/tokens":
            # 管理只读令牌只允许页面会话：被泄露的只读令牌不能借此提权
            if kind != "session":
                return self._send_json(self._read_only_error(), status=403)
            return self._send_json({
                "list": self.auth.tokens(), "max": MAX_TOKENS,
                "env_seeded": self.auth.env_seeded()})

        if path == "/api/overview":
            return self._send_json(self._overview())
        if path == "/api/performance":
            return self._send_json(self._performance())
        if path == "/api/processes":
            return self._send_json(self._processes())
        if path == "/api/network":
            return self._send_json(self._network())
        if path == "/api/services":
            return self._send_json(self._services())
        if path == "/api/device":
            return self._send_json(self._device())
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

    def _device(self):
        with state_lock:
            snapshot = state["snapshot"]
        payload = dict(self.collector.device_info())
        payload["ready"] = True
        payload["ts"] = snapshot["ts"] if snapshot else None
        return payload

    @staticmethod
    def _services():
        with state_lock:
            services = state["services"]
            snapshot = state["snapshot"]
        if not services or snapshot is None:
            return {"ready": False, "window": WINDOW_SECONDS}
        payload = dict(services)
        payload["ready"] = True
        payload["ts"] = snapshot["ts"]
        payload["interval"] = INTERVAL
        return payload

    @staticmethod
    def _network():
        with state_lock:
            network = state["network"]
            snapshot = state["snapshot"]
        if not network or snapshot is None:
            return {"ready": False, "window": WINDOW_SECONDS}
        payload = dict(network)
        payload["ready"] = True
        payload["ts"] = snapshot["ts"]
        payload["interval"] = INTERVAL
        payload["disk"] = snapshot.get("disk") or {}
        payload["net"] = snapshot.get("net") or {}      # 当前瞬时速率（大数字用）
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

    def _send_json(self, payload, status=200, cookies=None, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies or ():
            self.send_header("Set-Cookie", cookie)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
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
            # 浏览器自动请求 favicon，没放图标时全是 404，没有诊断价值
            if "favicon.ico" in self.path:
                return
            line = redact_token(fmt % args)
            print(f"[http] {self.address_string()} {line}", flush=True)


class DashboardServer(ThreadingHTTPServer):
    """默认实现会把客户端断连（关标签页、探测浏览器直接断开）打成整段 traceback，
    这类噪声没有诊断价值，这里吞掉；其他异常照旧打印。"""

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, BrokenPipeError,
                              ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def create_server(collector, host=HOST, port=PORT, auth=None):
    """构建（但不启动）HTTP 服务。

    返回一个绑定了该 collector 的 ThreadingHTTPServer，调用方负责 serve_forever()。
    测试里传 port=0 让内核分配空闲端口，避免和在跑的服务抢 8282。
    """
    bound_handler = type("BoundHandler", (Handler,),
                         {"collector": collector, "auth": auth})
    return DashboardServer((host, port), bound_handler)


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
    try:
        auth_store = AuthStore(
            auth_file_path(),
            username=os.environ.get("DASHBOARD_USER"),
            password=os.environ.get("DASHBOARD_PASSWORD"),
            logger=lambda message: print(message, flush=True))
    except AuthConfigError as exc:
        # 凭据读不到或损坏时宁可不启动，也不能静默重建覆盖原账号
        raise SystemExit(f"凭据不可用，服务未启动：\n{exc}")
    # 打包成无控制台的 exe 时，初始凭据看不到控制台输出：写文件 + 弹窗
    if is_frozen() and auth_store.initial_credentials:
        user, secret = auth_store.initial_credentials
        written = announce_initial_credentials(user, secret, data_dir())
        if written:
            print(f"[auth] 初始凭据已写入：{written}", flush=True)
    auth_store.seed_env_token()
    collector = Collector()
    threading.Thread(target=sampler, args=(collector,), daemon=True).start()
    # 延迟探测单独一个线程：连接超时不该拖慢 1 秒采样
    threading.Thread(target=collector.probe_loop, daemon=True).start()
    # 局域网扫描也放后台：整段 ping 要十来秒，不能占着请求线程
    threading.Thread(target=collector.lan_scan_loop, daemon=True).start()

    try:
        httpd = create_server(collector, HOST, PORT, auth=auth_store)
    except OSError as exc:
        raise SystemExit(f"无法监听 {HOST}:{PORT} —— {exc}")

    lan_ip = lan_address()
    print(f"总控台已启动：http://127.0.0.1:{PORT}/   局域网：http://{lan_ip}:{PORT}/", flush=True)
    print(f"网卡 {collector.nic} ｜ 磁盘 {collector.disk_path} ｜ "
          f"{collector.cores} 核 ｜ 采样 {INTERVAL}s ｜ 窗口 {WINDOW_SECONDS:.0f}s", flush=True)
    print(f"鉴权已启用 ｜ 账号 {auth_store.username()} ｜ 凭据 {auth_file_path()}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
