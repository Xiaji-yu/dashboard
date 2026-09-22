"""打包成 exe（PyInstaller）后的运行期路径与首启提示。

打包与源码运行有两个本质区别，这里集中处理：

1. **只读资源与可写数据分家**
   - `resource_dir()`：static/ 等只读资源所在目录。onefile 模式下 PyInstaller 会把它们
     解到临时目录（`sys._MEIPASS`），所以必须走这个函数，不能再用 `__file__`；
   - `data_dir()`：auth.json / probes.json / 日志等**可写**文件所在目录。
     onefile 的解包目录退出即删，写在那里等于丢数据；优先用 exe 所在目录，
     不可写（例如装在 Program Files）时退回 `%LOCALAPPDATA%\\dashboard`。

2. **没有控制台就看不到初始账号密码**
   源码运行时初始凭据打印在日志里；打包成无控制台窗口的 exe 后，用户看不到。
   这时把凭据写进数据目录的一个文本文件，并弹一次 Windows 消息框。
"""

from __future__ import annotations

import os
import sys

APP_DIR_NAME = "dashboard"
INITIAL_CREDENTIALS_FILE = "初始账号-登录后请删除.txt"


def is_frozen():
    """是否运行在 PyInstaller/cx_Freeze 之类的打包产物里。"""
    return bool(getattr(sys, "frozen", False))


def source_dir():
    """源码所在目录（未打包时的数据/资源目录）。"""
    return os.path.dirname(os.path.abspath(__file__))


def resource_dir():
    """只读资源目录：打包后是解包目录，未打包就是源码目录。"""
    if is_frozen():
        bundled = getattr(sys, "_MEIPASS", None)
        if bundled:
            return bundled
        return os.path.dirname(os.path.abspath(sys.executable))
    return source_dir()


def _writable(path):
    """目录是否可写（尝试建一个临时文件）。"""
    if not path or not os.path.isdir(path):
        return False
    probe = os.path.join(path, ".dashboard-write-test")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def data_dir():
    """可写数据目录：打包后是 exe 所在目录；不可写时退回用户目录。"""
    if not is_frozen():
        return source_dir()
    beside_exe = os.path.dirname(os.path.abspath(sys.executable))
    if _writable(beside_exe):
        return beside_exe
    fallback = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                            APP_DIR_NAME)
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        return beside_exe
    return fallback


def announce_initial_credentials(user, secret, directory=None, popup=None):
    """把初始账号密码写到数据目录并（Windows 上）弹窗提示一次。

    仅在打包运行时调用：源码运行时控制台/日志已经能看到这行。
    返回写出的文件路径（写失败返回 None）。
    """
    directory = directory or data_dir()
    path = os.path.join(directory, INITIAL_CREDENTIALS_FILE)
    text = (f"账号：{user}\n密码：{secret}\n\n"
            f"登录后请立刻在侧栏「账号」里修改密码，然后删除本文件。\n"
            f"地址：http://127.0.0.1:8282/\n")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        path = None

    if popup is None:
        popup = os.name == "nt"
    if popup:
        message = (f"初始账号：{user}\n初始密码：{secret}\n\n"
                   f"登录后请立刻修改密码。")
        if path:
            message += f"\n\n已写入：{path}"
        _message_box("8282 总控台 - 首次启动", message)
    return path


def _message_box(title, message):
    """Windows 消息框（无控制台时唯一的提示途径）；非 Windows 静默跳过。"""
    if os.name != "nt":
        return False
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
        return True
    except Exception:            # pragma: no cover - 仅 Windows 且 API 异常时
        return False
