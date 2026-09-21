"""鉴权测试：口令哈希、会话生命周期、限速、改密与凭据文件。"""

import json
import os
import stat
import tempfile
import time
import unittest
from unittest import mock

from auth import (LOGIN_MAX_FAILURES, MAX_TOKENS, MAX_TRACKED_IPS, PASSWORD_MIN_LENGTH,
                  SESSION_SAVE_INTERVAL, SESSION_TTL, TOKEN_PREFIX, AuthConfigError,
                  AuthStore, generate_password, generate_token, hash_password,
                  hash_token, password_record_ok, verify_password)

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

class CredentialFileSafetyTest(unittest.TestCase):
    """凭据文件读不到/损坏时**绝不能**静默重建覆盖（会让原账号口令永久丢失）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "auth.json")
        self.logs = []
        self.store = AuthStore(self.path, username="admin", password=PASSWORD,
                               logger=self.logs.append)
        self.original = open(self.path, encoding="utf-8").read()
        self.since = len(self.logs)        # 只看重新打开之后产生的日志

    def _reopen(self, **kwargs):
        return AuthStore(self.path, logger=self.logs.append, **kwargs)

    def test_unreadable_file_refuses_instead_of_overwriting(self):
        os.chmod(self.path, 0o000)
        self.addCleanup(os.chmod, self.path, 0o600)
        with self.assertRaises(AuthConfigError) as ctx:
            self._reopen()
        self.assertIn("读不到", str(ctx.exception))
        os.chmod(self.path, 0o600)
        self.assertEqual(open(self.path, encoding="utf-8").read(), self.original,
                         "读失败时原文件必须原封不动")

    def test_corrupt_json_is_quarantined_not_replaced(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(self.original[: len(self.original) // 2])   # 截断
        with self.assertRaises(AuthConfigError) as ctx:
            self._reopen()
        self.assertIn("解析失败", str(ctx.exception))
        backups = [name for name in os.listdir(self.tmp.name) if ".corrupt-" in name]
        self.assertEqual(len(backups), 1, "损坏文件要另存一份")
        self.assertIn("corrupt", str(ctx.exception))
        self.assertTrue(open(os.path.join(self.tmp.name, backups[0]),
                             encoding="utf-8").read().startswith("{"))
        self.assertFalse(any("首次启动" in line for line in self.logs[self.since:]),
                         "损坏时不该谎报首次启动")

    def test_incomplete_record_is_treated_as_damaged(self):
        """有 username 但口令记录缺 hash：这种文件会让登录永远失败，必须当成损坏。"""
        import json as jsonlib
        data = jsonlib.loads(self.original)
        del data["password"]["hash"]
        with open(self.path, "w", encoding="utf-8") as handle:
            jsonlib.dump(data, handle)
        with self.assertRaises(AuthConfigError) as ctx:
            self._reopen()
        self.assertIn("记录不完整", str(ctx.exception))

    def test_valid_file_is_loaded_without_regenerating(self):
        again = self._reopen(username="someone-else", password="env-pass-1234")
        self.assertEqual(again.username(), "admin")
        self.assertTrue(verify_password(PASSWORD, again.data["password"]))
        self.assertFalse(any("首次启动" in line for line in self.logs[self.since:]))

    def test_missing_file_still_creates_credentials(self):
        fresh = os.path.join(self.tmp.name, "new.json")
        logs = []
        store = AuthStore(fresh, logger=logs.append)
        self.assertTrue(os.path.exists(fresh))
        line = [item for item in logs if "首次启动" in item][0]
        secret = line.split(" / ")[-1].strip()
        self.assertIsNotNone(store.login("admin", secret, "7.7.7.7"))

    def test_password_record_ok(self):
        self.assertTrue(password_record_ok(hash_password("x")))
        self.assertFalse(password_record_ok(None))
        self.assertFalse(password_record_ok({}))
        self.assertFalse(password_record_ok({"algo": "pbkdf2_sha256"}))
        broken = hash_password("x")
        broken["hash"] = "!!!"
        self.assertFalse(password_record_ok(broken))
        wrong_algo = hash_password("x")
        wrong_algo["algo"] = "md5"
        self.assertFalse(password_record_ok(wrong_algo))

    def test_verify_password_rejects_non_string_input(self):
        record = hash_password("x")
        for bad in (None, 123, b"x", ["x"], {"a": 1}):
            self.assertFalse(verify_password(bad, record), bad)

    def test_login_rejects_non_string_credentials_without_raising(self):
        self.assertIsNone(self.store.login({"a": 1}, PASSWORD, "1.2.3.4"))
        self.assertIsNone(self.store.login("admin", ["x"], "1.2.3.4"))
        self.assertIsNone(self.store.login(None, None, "1.2.3.4"))
        self.assertIsNone(self.store.session(["not-a-token"]))

    def test_temp_file_is_created_private(self):
        """临时文件里含全部会话令牌，创建时就该是 0600，不能先 0644 再 chmod。"""
        seen = []
        real_replace = os.replace

        def spy_replace(src_path, dst_path):
            seen.append(oct(os.stat(src_path).st_mode & 0o777))
            return real_replace(src_path, dst_path)

        with mock.patch("auth.os.replace", side_effect=spy_replace):
            self.store.login("admin", PASSWORD, "5.5.5.5")
        self.assertTrue(seen, "应当发生一次原子替换")
        self.assertEqual(seen[0], "0o600", f"临时文件权限应从一开始就是 600，实际 {seen[0]}")

    def test_session_touch_is_throttled_but_persisted(self):
        token = self.store.login("admin", PASSWORD, "6.6.6.6")
        with mock.patch.object(AuthStore, "_save", autospec=True) as save:
            self.store.session(token)
            save.assert_not_called()
            self.store._last_save = time.time() - SESSION_SAVE_INTERVAL - 1
            self.store.session(token)
            save.assert_called_once()

    def test_failure_table_is_bounded(self):
        for index in range(MAX_TRACKED_IPS + 200):
            self.store._record_failure(f"10.0.{index // 250}.{index % 250}")
        self.assertLessEqual(len(self.store._failures), MAX_TRACKED_IPS + 200)
        self.assertLess(len(self.store._failures), MAX_TRACKED_IPS + 201)


class AuthConfigErrorUsageTest(unittest.TestCase):
    def test_main_exits_cleanly_on_bad_credentials_file(self):
        """server.main() 遇到凭据损坏要给出可读提示并退出，而不是抛 traceback。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bad = os.path.join(tmp.name, "auth.json")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        env = mock.patch.dict(os.environ, {"DASHBOARD_AUTH_FILE": bad},
                              clear=False)
        env.start()
        self.addCleanup(env.stop)
        import server
        with self.assertRaises(SystemExit) as ctx:
            server.main()
        self.assertIn("凭据不可用", str(ctx.exception))

class ApiTokenTest(unittest.TestCase):
    """只读 API 令牌：生成、校验、撤销、只存哈希、上限与环境变量播种。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "auth.json")
        self.store = AuthStore(self.path, username="admin", password=PASSWORD,
                               logger=lambda message: None)

    def test_generate_token_shape(self):
        token = generate_token()
        self.assertTrue(token.startswith(TOKEN_PREFIX))
        self.assertGreaterEqual(len(token), 40)
        self.assertNotEqual(token, generate_token())
        self.assertEqual(hash_token(token), hash_token(token))

    def test_create_and_verify(self):
        record, token = self.store.create_token("my-bot")
        self.assertEqual(record["name"], "my-bot")
        self.assertEqual(record["id"], token[:8])
        self.assertIsNone(record["last_used"])
        info = self.store.verify_token(token)
        self.assertEqual(info, {"id": record["id"], "name": "my-bot"})
        self.assertIsNotNone(self.store.tokens()[0]["last_used"], "校验后应记录最近使用")

    def test_verify_rejects_junk(self):
        for bad in (None, "", "short", "dshk_wrong-token-value-1234567890", 12345,
                    ["dshk_x"], {"token": "x"}):
            self.assertIsNone(self.store.verify_token(bad), bad)

    def test_file_stores_only_hash(self):
        _record, token = self.store.create_token("bot")
        with open(self.path, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn(token, raw, "凭据文件里不能出现明文令牌")
        self.assertIn("sha256", raw)
        self.assertNotIn("dshk_", raw.replace(token[:8], ""), "除了 8 位可识别前缀，不该出现令牌本体")

    def test_list_hides_secrets(self):
        self.store.create_token("bot")
        row = self.store.tokens()[0]
        self.assertEqual(sorted(row), ["created", "id", "last_used", "name"])

    def test_revoke(self):
        record, token = self.store.create_token("bot")
        self.assertTrue(self.store.revoke_token(record["id"]))
        self.assertIsNone(self.store.verify_token(token), "撤销后应立即失效")
        self.assertFalse(self.store.revoke_token("dshk_nope"), "不存在的 id 返回 False")
        self.assertEqual(self.store.tokens(), [])

    def test_token_count_is_capped(self):
        for index in range(MAX_TOKENS + 5):
            self.store.create_token(f"bot-{index}")
        self.assertEqual(len(self.store.tokens()), MAX_TOKENS)

    def test_last_used_save_is_throttled(self):
        _record, token = self.store.create_token("bot")
        with mock.patch.object(AuthStore, "_save", autospec=True) as save:
            self.store.verify_token(token)
            save.assert_not_called()
            self.store._last_save = time.time() - 3600
            self.store.verify_token(token)
            save.assert_called_once()

    def test_seed_from_env_only_when_no_tokens(self):
        with mock.patch.dict(os.environ, {"DASHBOARD_API_TOKEN": "dshk_from-env-1234567890"}):
            seeded = self.store.seed_env_token()
            self.assertIsNotNone(seeded)
            self.assertTrue(self.store.env_seeded())
            self.assertIsNotNone(self.store.verify_token("dshk_from-env-1234567890"))
            # 已有令牌时不再插手（撤销后重启也不会把它加回来）
            self.assertIsNone(self.store.seed_env_token())

    def test_seed_from_env_absent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.store.seed_env_token())
            self.assertFalse(self.store.env_seeded())

    def test_seed_from_env_persists_across_restart(self):
        with mock.patch.dict(os.environ, {"DASHBOARD_API_TOKEN": "dshk_persist-1234567890"}):
            self.store.seed_env_token()
            again = AuthStore(self.path, logger=lambda message: None)
        self.assertIsNotNone(again.verify_token("dshk_persist-1234567890"))
        self.assertEqual(len(again.tokens()), 1)
