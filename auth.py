"""账号与登录：只用标准库实现的凭据存储、会话与限速。

设计要点：
  * 口令用 PBKDF2-HMAC-SHA256 + 每用户随机盐（20 万轮），只存哈希不存明文；
  * 会话令牌是 secrets 随机的 URL-safe 字符串，服务端保存，Cookie 用 HttpOnly + SameSite=Lax；
  * 首次启动自动生成随机初始密码，写进凭据文件（0600）并打印到日志，便于首次登录后自行修改；
  * 登录失败按来源 IP 限速，避免被暴力猜口令。

凭据文件（默认项目根目录 auth.json，可用 DASHBOARD_AUTH_FILE 指定）：
    {"version": 1, "username": "...", "password": {"algo", "rounds", "salt", "hash"},
     "created_at": ..., "updated_at": ..., "sessions": {token: {"created", "last_seen"}}}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

PBKDF2_ROUNDS = 200_000
SESSION_TTL = 7 * 24 * 3600      # 会话有效期：7 天
MAX_SESSIONS = 20                # 最多保留的会话数，超出淘汰最旧的
LOGIN_WINDOW = 300.0             # 失败计数窗口：5 分钟
LOGIN_MAX_FAILURES = 5           # 窗口内允许的失败次数
PASSWORD_MIN_LENGTH = 8


def hash_password(password, salt=None, rounds=PBKDF2_ROUNDS):
    """生成口令哈希记录（salt/rounds/hash 都是 base64 文本，便于写进 JSON）。"""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return {
        "algo": "pbkdf2_sha256",
        "rounds": rounds,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
    }


def verify_password(password, record):
    """校验口令。任何字段异常都当作失败，不抛异常。"""
    if not isinstance(record, dict):
        return False
    try:
        salt = base64.b64decode(record["salt"])
        rounds = int(record.get("rounds") or PBKDF2_ROUNDS)
        expected = base64.b64decode(record["hash"])
    except (KeyError, TypeError, ValueError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(digest, expected)


def generate_password(length=20):
    """随机初始密码。去掉容易看错的字符，方便手抄。"""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


class AuthStore:
    """凭据与会话。所有方法都加锁，供多线程的 HTTP 服务使用。"""

    def __init__(self, path, username=None, password=None, logger=None):
        self.path = path
        self.logger = logger or (lambda message: None)
        self._lock = threading.Lock()
        self._failures = {}          # ip -> [失败时间戳]
        self.data = {}
        self._load_or_create(username, password)

    # ---------------- 读写文件 ----------------

    def _load_or_create(self, username, password):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                self.data = json.load(handle)
        except (OSError, ValueError):
            self.data = {}
        if self.data.get("username") and isinstance(self.data.get("password"), dict):
            return
        # 首次启动：优先用环境变量/参数给的账密，否则随机生成
        user = username or "admin"
        secret = password or generate_password()
        self.data = {
            "version": 1,
            "username": user,
            "password": hash_password(secret),
            "created_at": time.time(),
            "updated_at": time.time(),
            "sessions": {},
        }
        self._save()
        self.logger(f"[auth] 首次启动，已生成初始账号：{user} / {secret}")
        self.logger(f"[auth] 凭据文件：{self.path}（权限 600，已加入 .gitignore；登录后请自行修改密码）")

    def _save(self):
        """写文件：目录不存在就建，权限收到 600（凭据不外泄给其他用户）。"""
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            self.logger(f"[auth] 凭据写入失败：{exc}")

    # ---------------- 限速 ----------------

    def retry_after(self, ip):
        """该 IP 还要等多少秒才能再试；没被限速返回 0。"""
        now = time.time()
        with self._lock:
            stamps = [stamp for stamp in self._failures.get(ip, [])
                      if now - stamp < LOGIN_WINDOW]
            self._failures[ip] = stamps
            if len(stamps) < LOGIN_MAX_FAILURES:
                return 0
            return max(1, int(LOGIN_WINDOW - (now - stamps[0])))

    def _record_failure(self, ip):
        self._failures.setdefault(ip, []).append(time.time())

    def _clear_failures(self, ip):
        self._failures.pop(ip, None)

    # ---------------- 会话 ----------------

    def _prune_sessions(self, now=None):
        now = now or time.time()
        sessions = self.data.get("sessions") or {}
        fresh = {token: info for token, info in sessions.items()
                 if now - info.get("last_seen", 0) < SESSION_TTL}
        if len(fresh) > MAX_SESSIONS:
            ordered = sorted(fresh.items(), key=lambda pair: pair[1].get("last_seen", 0))
            fresh = dict(ordered[-MAX_SESSIONS:])
        self.data["sessions"] = fresh
        return fresh

    def login(self, username, password, ip="unknown"):
        """校验账密；成功返回会话令牌，失败返回 None（并计入失败次数）。"""
        with self._lock:
            if (username or "").strip() != self.data.get("username"):
                # 用户名也不对时同样计入失败，避免探测
                self._record_failure(ip)
                return None
            if not verify_password(password or "", self.data.get("password")):
                self._record_failure(ip)
                return None
            self._clear_failures(ip)
            now = time.time()
            sessions = self._prune_sessions(now)
            token = secrets.token_urlsafe(32)
            sessions[token] = {"created": now, "last_seen": now}
            self.data["sessions"] = sessions
            self._save()
            return token

    def session(self, token):
        """校验会话令牌；有效则刷新 last_seen 并返回 {username, created}。"""
        if not token:
            return None
        with self._lock:
            sessions = self._prune_sessions()
            info = sessions.get(token)
            if not info:
                return None
            info["last_seen"] = time.time()
            return {"username": self.data.get("username"), "created": info.get("created")}

    def logout(self, token):
        with self._lock:
            sessions = self.data.get("sessions") or {}
            if token in sessions:
                sessions.pop(token, None)
                self._save()
                return True
            return False

    def logout_all(self, keep_token=None):
        with self._lock:
            sessions = self.data.get("sessions") or {}
            if keep_token and keep_token in sessions:
                self.data["sessions"] = {keep_token: sessions[keep_token]}
            else:
                self.data["sessions"] = {}
            self._save()
            return True

    # ---------------- 账户 ----------------

    def username(self):
        return self.data.get("username")

    def change_credentials(self, token, old_password, new_password=None, new_username=None):
        """改口令 / 改用户名。返回 (ok, message)。

        改口令需要旧口令；改完把其他设备的会话都踢掉（当前会话保留），
        这样口令泄露后重新登录能立刻断开别人的会话。
        """
        with self._lock:
            sessions = self.data.get("sessions") or {}
            if token not in sessions:
                return False, "会话已失效，请重新登录"
            if not verify_password(old_password or "", self.data.get("password")):
                return False, "当前密码不正确"

            changed = []
            if new_password:
                if len(new_password) < PASSWORD_MIN_LENGTH:
                    return False, f"新密码至少 {PASSWORD_MIN_LENGTH} 位"
                if verify_password(new_password, self.data.get("password")):
                    return False, "新密码不能与当前密码相同"
                self.data["password"] = hash_password(new_password)
                changed.append("密码")
            if new_username:
                if new_username != self.data.get("username"):
                    self.data["username"] = new_username
                    changed.append("用户名")
            if not changed:
                return False, "没有需要修改的内容"

            now = time.time()
            self.data["sessions"] = {token: {**sessions[token], "last_seen": now}}
            self.data["updated_at"] = now
            self._save()
            return True, "已更新" + "、".join(changed) + "，其他设备的登录已失效"


def auth_file_path():
    """凭据文件路径（DASHBOARD_AUTH_FILE 可覆盖，默认项目根目录 auth.json）。"""
    return os.environ.get("DASHBOARD_AUTH_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "auth.json")
