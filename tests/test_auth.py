"""鉴权测试：口令哈希、会话生命周期、限速、改密与凭据文件。"""

import json
import os
import stat
import tempfile
import time
import unittest

from auth import (LOGIN_MAX_FAILURES, PASSWORD_MIN_LENGTH, SESSION_TTL, AuthStore,
                  generate_password, hash_password, verify_password)

PASSWORD = "initial-pass-1"


class PasswordHashTest(unittest.TestCase):
    def test_roundtrip(self):
        record = hash_password("correct horse")
        self.assertTrue(verify_password("correct horse", record))
        self.assertFalse(verify_password("wrong horse", record))

    def test_only_hash_is_stored(self):
        record = hash_password("correct horse")
        self.assertEqual(record["algo"], "pbkdf2_sha256")
        self.assertNotIn("correct horse", json.dumps(record))
        self.assertNotIn("password", record)

    def test_salt_is_random(self):
        first = hash_password("same")
        second = hash_password("same")
        self.assertNotEqual(first["salt"], second["salt"])
        self.assertNotEqual(first["hash"], second["hash"])

    def test_broken_records_fail_closed(self):
        for record in (None, {}, {"salt": "x"}, {"salt": "x", "hash": "y"},
                       {"salt": "!!!", "hash": "!!!"}, "not-a-dict"):
            self.assertFalse(verify_password("whatever", record), record)

    def test_generate_password_shape(self):
        secret = generate_password()
        self.assertEqual(len(secret), 20)
        self.assertTrue(all(ch.isalnum() for ch in secret))
        self.assertNotEqual(secret, generate_password())


class AuthStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "auth.json")
        self.logs = []
        self.store = AuthStore(self.path, username="admin", password=PASSWORD,
                               logger=self.logs.append)

    def test_initial_credentials_written_and_logged(self):
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self.store.username(), "admin")
        self.assertTrue(any(PASSWORD in line for line in self.logs), "初始账密要打进日志")
        self.assertTrue(verify_password(PASSWORD, self.store.data["password"]))

    def test_file_is_owner_only(self):
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600, f"凭据文件权限应为 600，实际 {oct(mode)}")

    def test_login_and_session(self):
        self.assertIsNone(self.store.login("admin", "wrong", "1.1.1.1"))
        token = self.store.login("admin", PASSWORD, "1.1.1.1")
        self.assertTrue(token)
        session = self.store.session(token)
        self.assertEqual(session["username"], "admin")
        self.assertIsNone(self.store.session("bogus-token"))
        self.assertIsNone(self.store.session(None))

    def test_unknown_username_also_counts_as_failure(self):
        self.assertIsNone(self.store.login("root", PASSWORD, "2.2.2.2"))
        self.assertEqual(self.store.retry_after("2.2.2.2"), 0, "一次失败不该触发限速")
        for _ in range(LOGIN_MAX_FAILURES):
            self.store.login("root", PASSWORD, "2.2.2.2")
        self.assertGreater(self.store.retry_after("2.2.2.2"), 0)

    def test_rate_limit_counts_and_resets(self):
        ip = "3.3.3.3"
        for _ in range(LOGIN_MAX_FAILURES):
            self.store.login("admin", "wrong", ip)
        self.assertGreater(self.store.retry_after(ip), 0, "失败达到上限后应被限速")
        # 窗口过后自动放行
        self.store._failures[ip] = [time.time() - 3600]
        self.assertEqual(self.store.retry_after(ip), 0)
        # 登录成功也会清空失败计数
        for _ in range(LOGIN_MAX_FAILURES - 1):
            self.store.login("admin", "wrong", ip)
        self.assertTrue(self.store.login("admin", PASSWORD, ip))
        self.assertEqual(self.store.retry_after(ip), 0)

    def test_session_expires(self):
        token = self.store.login("admin", PASSWORD, "4.4.4.4")
        self.store.data["sessions"][token]["last_seen"] = time.time() - SESSION_TTL - 10
        self.assertIsNone(self.store.session(token), "过期会话应当失效")

    def test_logout(self):
        token = self.store.login("admin", PASSWORD, "5.5.5.5")
        self.assertTrue(self.store.logout(token))
        self.assertIsNone(self.store.session(token))

    def test_logout_all(self):
        first = self.store.login("admin", PASSWORD, "6.6.6.6")
        second = self.store.login("admin", PASSWORD, "6.6.6.6")
        self.store.logout_all()
        self.assertIsNone(self.store.session(first))
        self.assertIsNone(self.store.session(second))

    def test_change_password_keeps_current_drops_others(self):
        mine = self.store.login("admin", PASSWORD, "7.7.7.7")
        other = self.store.login("admin", PASSWORD, "8.8.8.8")
        self.world = None
        self.assertIsNotNone(self.store.session(other))
        ok, message = self.store.change_credentials(mine, PASSWORD, "brand-new-pass-2")
        self.assertTrue(ok, message)
        self.assertIn("其他设备", message)
        self.assertIsNone(self.store.session(other), "其他设备的会话应被踢掉")
        self.assertIsNotNone(self.store.session(mine), "当前会话应保留")
        self.assertIsNotNone(self.store.login("admin", "brand-new-pass-2", "9.9.9.9"))
        self.assertIsNone(self.store.login("admin", PASSWORD, "9.9.9.9"))

    def test_change_password_validations(self):
        token = self.store.login("admin", PASSWORD, "10.0.0.1")
        self.assertFalse(self.store.change_credentials(token, "wrong", "another-pass-1")[0])
        self.assertIn(str(PASSWORD_MIN_LENGTH),
                      self.store.change_credentials(token, PASSWORD, "short")[1])
        self.assertIn("不能与当前密码相同",
                      self.store.change_credentials(token, PASSWORD, PASSWORD)[1])
        self.assertIn("没有需要修改", self.store.change_credentials(token, PASSWORD)[1])

    def test_change_username(self):
        token = self.store.login("admin", PASSWORD, "10.0.0.2")
        ok, message = self.store.change_credentials(token, PASSWORD, None, "xiaji")
        self.assertTrue(ok, message)
        self.assertIn("用户名", message)
        self.assertEqual(self.store.username(), "xiaji")
        self.assertIsNone(self.store.login("admin", PASSWORD, "10.0.0.3"))
        self.assertTrue(self.store.login("xiaji", PASSWORD, "10.0.0.3"))

    def test_requires_valid_session_to_change(self):
        self.assertFalse(self.store.change_credentials("bogus", PASSWORD, "another-pass-1")[0])
        self.assertFalse(self.store.change_credentials(None, PASSWORD, "another-pass-1")[0])

    def test_persists_across_restart_and_keeps_env_unused(self):
        token = self.store.login("admin", PASSWORD, "11.1.1.1")
        again = AuthStore(self.path, username="other", password="ignored-pass-1",
                          logger=self.logs.append)
        self.assertEqual(again.username(), "admin", "已有凭据时不应被环境变量覆盖")
        self.assertIsNotNone(again.session(token), "会话应当持久化")

    def test_first_run_random_credentials(self):
        path = os.path.join(self.tmp.name, "fresh.json")
        logs = []
        store = AuthStore(path, logger=logs.append)
        self.assertEqual(store.username(), "admin")
        self.assertTrue(any("首次启动" in line for line in logs))
        # 日志里能拿到初始密码，用它登录应当成功
        line = [item for item in logs if "首次启动" in item][0]
        secret = line.split("/")[-1].strip()
        self.assertIsNotNone(store.login("admin", secret, "12.1.1.1"))


if __name__ == "__main__":
    unittest.main()
