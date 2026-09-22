"""打包（PyInstaller）后的运行期路径与首启提示。

这些测试**不需要真的打包**：把 `sys.frozen` / `sys._MEIPASS` / `sys.executable`
打桩即可在 Linux 上验证——这正是「打包后才会踩」的那类问题。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

import auth
import collector
import runtime


class ResourceDirTest(unittest.TestCase):
    def test_source_mode_uses_project_dir(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertFalse(runtime.is_frozen())
            self.assertEqual(runtime.resource_dir(), runtime.source_dir())
            self.assertEqual(runtime.data_dir(), runtime.source_dir())

    def test_frozen_uses_meipass_for_resources(self):
        """onefile 把 static/ 解到临时目录：只读资源必须走 _MEIPASS。"""
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", "/tmp/meipass-xyz", create=True):
            self.assertTrue(runtime.is_frozen())
            self.assertEqual(runtime.resource_dir(), "/tmp/meipass-xyz")

    def test_frozen_without_meipass_uses_exe_dir(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", "/opt/dashboard/dashboard.exe"):
            self.assertNotIn("_MEIPASS", vars(sys))
            self.assertEqual(runtime.resource_dir(), "/opt/dashboard")


class DataDirTest(unittest.TestCase):
    def test_frozen_writes_beside_exe_when_writable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        exe = os.path.join(tmp.name, "dashboard.exe")
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", exe):
            self.assertEqual(runtime.data_dir(), tmp.name)

    def test_frozen_falls_back_to_localappdata(self):
        """装在 Program Files 时 exe 目录不可写，必须退回用户目录，否则账号会丢。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", "/Program Files/dashboard/dashboard.exe"), \
                mock.patch.object(runtime, "_writable", return_value=False), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp.name}):
            expected = os.path.join(tmp.name, runtime.APP_DIR_NAME)
            self.assertEqual(runtime.data_dir(), expected)
            self.assertTrue(os.path.isdir(expected), "用户目录应被创建出来")

    def test_writable_probe(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertTrue(runtime._writable(tmp.name))
        self.assertFalse(runtime._writable(os.path.join(tmp.name, "不存在")))
        self.assertFalse(runtime._writable(None))
        self.assertEqual(os.listdir(tmp.name), [], "探测文件用完要删掉")


class FrozenPathWiringTest(unittest.TestCase):
    """凭据与 probes.json 必须落在可写数据目录，不能落在解包临时目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.exe = os.path.join(self.tmp.name, "dashboard.exe")

    def test_auth_file_beside_exe(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", self.exe), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DASHBOARD_AUTH_FILE", None)
            self.assertEqual(auth.auth_file_path(), os.path.join(self.tmp.name, "auth.json"))

    def test_probes_file_beside_exe(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", self.exe):
            os.environ.pop("DASHBOARD_PROBES", None)
            self.assertEqual(collector.probes_path(), os.path.join(self.tmp.name, "probes.json"))

    def test_env_override_still_wins(self):
        with mock.patch.dict(os.environ, {"DASHBOARD_AUTH_FILE": "/tmp/custom-auth.json",
                                          "DASHBOARD_PROBES": "/tmp/custom-probes.json"}):
            self.assertEqual(auth.auth_file_path(), "/tmp/custom-auth.json")
            self.assertEqual(collector.probes_path(), "/tmp/custom-probes.json")


class AnnounceCredentialsTest(unittest.TestCase):
    """无控制台的 exe：初始凭据必须落到文件，并且只在首次生成时提示一次。"""

    def test_writes_file_without_popup(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = runtime.announce_initial_credentials("admin", "secret-pass-1",
                                                    directory=tmp.name, popup=False)
        self.assertEqual(path, os.path.join(tmp.name, runtime.INITIAL_CREDENTIALS_FILE))
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("admin", text)
        self.assertIn("secret-pass-1", text)
        self.assertIn("修改密码", text)

    def test_missing_directory_returns_none(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = runtime.announce_initial_credentials("admin", "x",
                                                    directory=os.path.join(tmp.name, "nope"),
                                                    popup=False)
        self.assertIsNone(path)

    def test_popup_is_skipped_off_windows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(runtime, "_message_box", return_value=True) as box, \
                mock.patch.object(runtime.os, "name", "posix"):
            runtime.announce_initial_credentials("admin", "x", directory=tmp.name)
        box.assert_not_called()

    def test_first_run_flag_and_credentials_recorded(self):
        """AuthStore 只在**新建**凭据时记下明文，供打包模式提示用。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "auth.json")
        store = auth.AuthStore(path, username="admin", password="pw-12345678",
                               logger=lambda message: None)
        self.assertEqual(store.initial_credentials, ("admin", "pw-12345678"))
        again = auth.AuthStore(path, logger=lambda message: None)
        self.assertIsNone(again.initial_credentials, "已有凭据时不该再提示初始密码")

    def test_message_box_is_noop_off_windows(self):
        with mock.patch.object(runtime.os, "name", "posix"):
            self.assertFalse(runtime._message_box("标题", "内容"))


if __name__ == "__main__":
    unittest.main()
