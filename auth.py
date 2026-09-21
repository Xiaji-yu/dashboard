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
import shutil
import threading
import time

PBKDF2_ROUNDS = 200_000
SESSION_TTL = 7 * 24 * 3600      # 会话有效期：7 天
MAX_SESSIONS = 20                # 最多保留的会话数，超出淘汰最旧的
LOGIN_WINDOW = 300.0             # 失败计数窗口：5 分钟
LOGIN_MAX_FAILURES = 5           # 窗口内允许的失败次数
PASSWORD_MIN_LENGTH = 8
MAX_TRACKED_IPS = 1000           # 限速表最多记这么多来源，防止被刷爆内存
SESSION_SAVE_INTERVAL = 300.0    # 会话 last_seen 至少间隔这么久才落盘一次
TOKEN_SAVE_INTERVAL = 300.0      # 令牌 last_used 的落盘节流
TOKEN_PREFIX = "dshk_"           # 便于在日志/配置里一眼认出这是看板令牌
TOKEN_BYTES = 32                 # 256 位随机量
MAX_TOKENS = 20                  # 最多保留的只读令牌数


class AuthConfigError(RuntimeError):
    """凭据文件不可用（读不到或已损坏）。

    这种情况**绝不能**当成「首次启动」去生成新凭据：那会静默覆盖掉原账号，
    造成不可逆的数据丢失。宁可让服务起不来，由人来决定修复还是重置。
    """


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


def password_record_ok(record):
    """口令记录是否完整可用（缺字段的记录会让登录永远失败，必须当成损坏）。"""
    if not isinstance(record, dict):
        return False
    if record.get("algo") != "pbkdf2_sha256":
        return False
    try:
        salt = base64.b64decode(record["salt"])
        digest = base64.b64decode(record["hash"])
        int(record.get("rounds") or PBKDF2_ROUNDS)
    except (KeyError, TypeError, ValueError):
        return False
    return bool(salt) and bool(digest)


def verify_password(password, record):
    """校验口令。任何字段异常都当作失败，不抛异常。"""
    if not isinstance(password, str):
        return False
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


def generate_token():
    """生成只读 API 令牌：dshk_ + 43 个 URL-safe 字符（256 位随机量）。"""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token):
    """令牌只做一次 SHA-256。

    口令要用 PBKDF2 慢哈希是为了抵抗字典/暴力猜测；令牌是 256 位随机量，
    没有可猜的分布，慢哈希只会让每次请求多花上百毫秒，所以这里用 SHA-256。
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


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
        self._last_save = 0.0        # 上次落盘时间（session / token 刷新节流用）
        self._token_env_seeded = False
        self.data = {}
        self._load_or_create(username, password)

    # ---------------- 读写文件 ----------------

    def _load_or_create(self, username, password):
        """载入凭据；**只有文件确实不存在**时才生成初始凭据。

        读不到（权限/属主不匹配）或解析失败时一律抛 AuthConfigError，
        并把损坏文件另存一份，绝不覆盖——覆盖等于把原账号口令永久抹掉。
        """
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except FileNotFoundError:
            raw = None
        except OSError as exc:
            raise AuthConfigError(
                f"凭据文件读不到：{self.path}\n  {exc}\n"
                f"  为避免覆盖有效凭据，服务不会自动重建账号。\n"
                f"  请修好权限/属主（文件应为 600，属主为运行服务的用户）后重启；\n"
                f"  确实要重置账号，请手动把它移走再启动。") from exc

        if raw is not None:
            try:
                self.data = json.loads(raw)
            except ValueError as exc:
                backup = self._quarantine()
                raise AuthConfigError(
                    f"凭据文件解析失败：{self.path}\n  {exc}\n"
                    f"  原文件已另存为：{backup or '（另存失败，原文件未被修改）'}\n"
                    f"  服务不会自动重建账号：确认要重置就移走 {self.path} 后重启。") from exc
            if not (isinstance(self.data.get("username"), str) and self.data["username"].strip()
                    and password_record_ok(self.data.get("password"))):
                backup = self._quarantine()
                raise AuthConfigError(
                    f"凭据文件记录不完整：{self.path}\n"
                    f"  （缺用户名，或口令记录缺 salt/hash——这种文件会让登录永远失败）\n"
                    f"  原文件已另存为：{backup or '（另存失败，原文件未被修改）'}\n"
                    f"  服务不会自动重建账号：确认要重置就移走 {self.path} 后重启。")
            return

        # 到这里说明文件确实不存在：这时才生成初始凭据
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

    def seed_env_token(self):
        """把 DASHBOARD_API_TOKEN 播种成只读令牌（**仅在当前一个令牌都没有时**）。

        方便 systemd / 容器用环境变量下发令牌；一旦你在页面上建过或撤销过令牌，
        这里就不再插手（撤销后重启不会把它加回来）。
        """
        token = (os.environ.get("DASHBOARD_API_TOKEN") or "").strip()
        if not token:
            return None
        with self._lock:
            if self.data.get("tokens"):
                return None
            record = self._add_token("环境变量 DASHBOARD_API_TOKEN", token)
            self.logger(f"[auth] 已从 DASHBOARD_API_TOKEN 播种只读令牌：{record['id']}…")
            self._token_env_seeded = True
            return record

    # ---------------- 只读 API 令牌 ----------------

    def env_seeded(self):
        """本次启动是否用 DASHBOARD_API_TOKEN 播种过令牌。"""
        return bool(self._token_env_seeded)

    def tokens(self):
        """列出令牌（不含任何可用于验证的字段）。"""
        with self._lock:
            rows = []
            for record in self.data.get("tokens") or []:
                rows.append({"id": record.get("id"), "name": record.get("name"),
                             "created": record.get("created"),
                             "last_used": record.get("last_used")})
            rows.sort(key=lambda item: item.get("created") or 0)
            return rows

    def _add_token(self, name, token):
        record = {
            "id": token[:8],
            "name": (name or "").strip()[:40] or "未命名",
            "sha256": hash_token(token),
            "created": time.time(),
            "last_used": None,
        }
        tokens = list(self.data.get("tokens") or [])
        tokens.append(record)
        # 超出上限时淘汰最久没用过的
        if len(tokens) > MAX_TOKENS:
            tokens.sort(key=lambda item: item.get("last_used") or item.get("created") or 0)
            tokens = tokens[-MAX_TOKENS:]
        self.data["tokens"] = tokens
        self.data["updated_at"] = time.time()
        self._save()
        return {"id": record["id"], "name": record["name"],
                "created": record["created"], "last_used": record["last_used"]}

    def create_token(self, name, token=None):
        """新建只读令牌，返回 (记录, 完整令牌)。完整令牌只在此刻返回一次。"""
        with self._lock:
            token = token or generate_token()
            record = self._add_token(name, token)
            return record, token

    def revoke_token(self, token_id):
        with self._lock:
            tokens = list(self.data.get("tokens") or [])
            keep = [item for item in tokens if item.get("id") != token_id]
            if len(keep) == len(tokens):
                return False
            self.data["tokens"] = keep
            self.data["updated_at"] = time.time()
            self._save()
            return True

    def verify_token(self, token):
        """校验只读令牌；成功返回 {id, name}，失败返回 None。

        用 hmac.compare_digest 做定长比较；令牌表很小（默认上限 20），线性查找足够。
        """
        if not isinstance(token, str) or len(token) < 12:
            return None
        digest = hash_token(token)
        with self._lock:
            now = time.time()
            for record in self.data.get("tokens") or []:
                if not hmac.compare_digest(record.get("sha256") or "", digest):
                    continue
                record["last_used"] = now
                if now - self._last_save >= TOKEN_SAVE_INTERVAL:
                    self._save()
                return {"id": record.get("id"), "name": record.get("name")}
        return None

    def _quarantine(self):
        """把损坏的凭据另存一份（保留原文件，绝不覆盖），返回备份路径。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = f"{self.path}.corrupt-{stamp}"
        try:
            shutil.copy2(self.path, target)
            os.chmod(target, 0o600)
            return target
        except OSError:
            return None

    def _save(self):
        """写文件：目录不存在就建，**创建时就带 0600**，再原子替换。

        注意不能用 open() 之后再 chmod：那样中间会有一个 0644 的窗口，
        而临时文件里含全部活跃会话令牌。
        """
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
            tmp = f"{self.path}.tmp"
            handle = os.fdopen(
                os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                "w", encoding="utf-8")
            with handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._last_save = time.time()
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
        """记录一次失败；顺手清理过期记录并给字典设上限，避免被刷爆内存。"""
        now = time.time()
        self._failures[ip] = [stamp for stamp in self._failures.get(ip, [])
                              if now - stamp < LOGIN_WINDOW]
        self._failures[ip].append(now)
        if len(self._failures) > MAX_TRACKED_IPS:
            for key in sorted(self._failures,
                              key=lambda item: self._failures[item][-1])[:len(self._failures) // 4]:
                self._failures.pop(key, None)

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
            if not isinstance(username, str) or not isinstance(password, str):
                self._record_failure(ip)      # 类型不对也算一次失败，但不抛异常
                return None
            if username.strip() != self.data.get("username"):
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
        """校验会话令牌；有效则刷新 last_seen 并返回 {username, created}。

        last_seen 会按 SESSION_SAVE_INTERVAL 节流落盘：否则重启后按磁盘上的登录时间算 TTL，
        天天在用的浏览器也会被迫重新登录。
        """
        if not token or not isinstance(token, str):
            return None
        with self._lock:
            sessions = self._prune_sessions()
            info = sessions.get(token)
            if not info:
                return None
            now = time.time()
            info["last_seen"] = now
            if now - self._last_save >= SESSION_SAVE_INTERVAL:
                self._save()
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
