"""Windows 移植层的解析器测试。

这些测试**不需要 Windows**：被测的都是纯函数（文本 -> 结构化数据），
因此可以在 Linux/CI 上守住 Windows 分支的正确性。
"""

import base64
import json
import unittest
from unittest import mock

import platform_win
from platform_win import (clean_text, disk_rows_from_storage, disk_rows_from_wmi, drive_letter,
                          filter_usb_instances, interpret_output, parse_arp, parse_netsh_ssid,
                          parse_ping_alive, parse_pnp_devices, parse_services,
                          parse_wmi_instance, to_gb)

ARP_TEXT = """
接口: 192.168.1.111 --- 0x5
  Internet 地址      物理地址            类型
  192.168.1.2         5a-42-70-53-b0-5c    动态
  192.168.1.110       00-f1-f3-b1-ae-d3    动态
  192.168.1.255       ff-ff-ff-ff-ff-ff    静态
"""

NETSH_TEXT = """
接口名称: WLAN

    主机控制的网络上的媒体状态已断开连接...

  接口名称: 以太网

  接口名称: WLAN

    SSID                  : MyWiFi-2G
    信号                  : 88%
    无线电类型            : 802.11n
"""

USB_PNP = """[
  {"FriendlyName":"USB Root Hub (USB 3.0)","InstanceId":"ROOT_HUB30\\\\{EC9A0F93}","Status":"OK"},
  {"FriendlyName":"USB 输入设备","InstanceId":"USB\\\\VID_1A2C&PID_2C81\\\\6&ABCD","Status":"OK"},
  {"FriendlyName":"AX88772A","InstanceId":"USB\\\\VID_0B95&PID_772A\\\\0001","Status":"OK"}
]"""

SERVICES_JSON = """[
  {"Name":"wuauserv","DisplayName":"Windows Update"},
  {"Name":"TermService","DisplayName":"Remote Desktop Services"}
]"""


class WindowsParserTest(unittest.TestCase):
    def test_parse_arp(self):
        rows = parse_arp(ARP_TEXT)
        self.assertEqual(rows["192.168.1.2"], "5a:42:70:53:b0:5c")
        self.assertEqual(rows["192.168.1.110"], "00:f1:f3:b1:ae:d3")
        self.assertEqual(rows["192.168.1.255"], "ff:ff:ff:ff:ff:ff")
        self.assertEqual(len(rows), 3)

    def test_parse_arp_ignores_noise(self):
        self.assertEqual(parse_arp("接口: x\n  无条目\n"), {})
        self.assertEqual(parse_arp(""), {})
        self.assertEqual(parse_arp("  1.2.3.4  incomplete"), {}, "MAC 不完整要跳过")

    def test_parse_netsh_ssid(self):
        by_name = parse_netsh_ssid(NETSH_TEXT)
        self.assertIn("WLAN", by_name)
        self.assertEqual(by_name["WLAN"]["ssid"], "MyWiFi-2G")
        self.assertEqual(by_name["WLAN"]["signal"], 88)

    def test_parse_netsh_without_interface(self):
        self.assertEqual(parse_netsh_ssid("  没有冒号的行\n"), {})
        self.assertEqual(parse_netsh_ssid("  接口名称: WLAN\n  没有SSID\n"),
                         {"WLAN": {"ssid": None, "signal": None}})

    def test_ping_alive_uses_returncode(self):
        self.assertTrue(parse_ping_alive(0))
        for code in (1, -1, 110):
            self.assertFalse(parse_ping_alive(code), code)

    def test_parse_pnp_devices_marks_root_hub_helper(self):
        import json as jsonlib
        rows = parse_pnp_devices(jsonlib.loads(USB_PNP))
        self.assertEqual(len(rows), 3)
        names = [row["name"] for row in rows]
        self.assertIn("AX88772A", names)
        self.assertIn("USB Root Hub (USB 3.0)", names)

    def test_parse_services(self):
        import json as jsonlib
        rows = parse_services(jsonlib.loads(SERVICES_JSON))
        self.assertEqual(rows[0], {"unit": "TermService", "description": "Remote Desktop Services"})
        self.assertEqual(rows[1]["unit"], "wuauserv")

    def test_parse_wmi_instance_list_and_dict(self):
        self.assertEqual(parse_wmi_instance({"Caption": "Microsoft Windows 11 专业版"})
                         ["Caption"], "Microsoft Windows 11 专业版")
        self.assertEqual(parse_wmi_instance([{"a": None}, {"b": 1}])["b"], 1)
        self.assertEqual(parse_wmi_instance([]), {})


class WindowsRealDataTest(unittest.TestCase):
    """用 Windows 实测数据（用户在 MSI MS-7D99 / Win11 上跑出来的）做回归。"""

    def test_usb_filters_pci_controller(self):
        """实测：`Get-PnpDevice -Class USB` 会连带列出 PCI 上的 USB 控制器。"""
        payload = [
            {"FriendlyName": "G502 HERO", "InstanceId": "USB\\VID_046D&PID_C08B\\1194"},
            {"FriendlyName": "Intel(R) USB 3.20 可扩展主机控制器 - 1.20 (Microsoft)",
             "InstanceId": "PCI\\VEN_8086&DEV_7A60&SUBSYS_7D991462&REV_11\\3&11583659&0&A0"},
            {"FriendlyName": "USB 根集线器(USB 3.0)", "InstanceId": "USB\\ROOT_HUB30\\4&11FD050C&0&0"},
        ]
        rows = filter_usb_instances(payload)
        self.assertEqual(len(rows), 2)
        self.assertNotIn("PCI", " ".join(row["InstanceId"] for row in rows))

    def test_placeholder_strings_are_not_models(self):
        """实测：主板 SystemFamily 返回 "Default string"，那是占位符不是型号。"""
        self.assertIsNone(clean_text("Default string"))
        self.assertIsNone(clean_text("To be filled by O.E.M."))
        self.assertIsNone(clean_text(""))
        self.assertEqual(clean_text(" MS-7D99 "), "MS-7D99")
        self.assertEqual(clean_text("1.G0"), "1.G0")

    def test_drive_letter(self):
        self.assertEqual(drive_letter("D:\\code\\dashboard"), "D")
        self.assertEqual(drive_letter("/"), "C")          # Windows 上 "/" 折算成系统盘
        self.assertEqual(drive_letter(None), "C")

    def test_to_gb(self):
        self.assertEqual(to_gb("1024209543168"), 953.9)   # 实测 E: 盘容量
        self.assertIsNone(to_gb("not-a-number"))
        self.assertIsNone(to_gb(None))

    def test_storage_rows_read_ssd_and_bus(self):
        """实测：Get-Disk 的 MediaType=4(SSD)、BusType=8(SATA) 才是可靠判据。"""
        rows = disk_rows_from_storage([{"Number": 1, "FriendlyName": "SSD 1TB",
                                        "BusType": 8, "MediaType": 4,
                                        "Size": "1024209543168"}])
        self.assertEqual(rows[0]["model"], "SSD 1TB")
        self.assertFalse(rows[0]["rotational"], "MediaType=4 是 SSD")
        self.assertEqual(rows[0]["bus"], "SATA")
        self.assertEqual(rows[0]["device"], "磁盘 1")

    def test_storage_rows_unknown_media_is_none(self):
        rows = disk_rows_from_storage([{"Number": 0, "FriendlyName": "X",
                                        "BusType": 99, "MediaType": 0, "Size": 1000}])
        self.assertIsNone(rows[0]["rotational"], "认不出就别猜")
        self.assertIsNone(rows[0]["bus"], "未映射的总线类型返回 None")

    def test_wmi_fallback_never_claims_mechanical(self):
        """实测：Win32_DiskDrive 对 SSD 也报 "Fixed hard disk media"，不能据此判机械盘。"""
        rows = disk_rows_from_wmi([{"Model": "SSD 1TB", "Size": "1024209543168",
                                    "MediaType": "Fixed hard disk media",
                                    "InterfaceType": "SCSI"}])
        self.assertIsNone(rows[0]["rotational"])
        self.assertEqual(rows[0]["bus"], "SCSI")
        explicit = disk_rows_from_wmi([{"Model": "X", "MediaType": "SSD"}])
        self.assertFalse(explicit[0]["rotational"], "明确写 SSD 时才敢下结论")


class PowershellEncodingTest(unittest.TestCase):
    """编码与空结果语义：不需要 Windows，把子进程 mock 掉即可验证核心修复。"""

    def test_decodes_chinese_payload(self):
        """实测乱码（专业工作站版 -> רҵ����վ��）的根因是 UTF-16LE，base64 传回后必须正常。"""
        text = json.dumps([{"Caption": "Microsoft Windows 11 专业工作站版 64 位"}],
                          ensure_ascii=False)
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        with mock.patch("platform_win.run_powershell", return_value=encoded):
            rows = platform_win.powershell_json("Get-CimInstance Win32_OperatingSystem | ...")
        self.assertEqual(rows[0]["Caption"], "Microsoft Windows 11 专业工作站版 64 位")

    def test_single_object_is_wrapped_in_list(self):
        encoded = base64.b64encode(json.dumps({"Number": 1}).encode()).decode()
        with mock.patch("platform_win.run_powershell", return_value=encoded):
            self.assertEqual(platform_win.powershell_json("Get-Disk | ..."), [{"Number": 1}])

    def test_empty_output_means_no_objects(self):
        """没有蓝牙设备时 Get-PnpDevice 静默返回空——这是「没有设备」，不是失败。"""
        with mock.patch("platform_win.run_powershell", return_value=""):
            self.assertEqual(platform_win.powershell_json("Get-PnpDevice -Class Bluetooth | ..."), [])

    def test_command_failure_is_none(self):
        with mock.patch("platform_win.run_powershell", return_value=None):
            self.assertIsNone(platform_win.powershell_json("Get-PnpDevice -Class USB | ..."))

    def test_garbage_output_is_none(self):
        with mock.patch("platform_win.run_powershell", return_value="!!!not base64!!!"):
            self.assertIsNone(platform_win.powershell_json("..."))

    def test_interpret_output(self):
        self.assertEqual(interpret_output(0, "ok", ""), "ok")
        self.assertEqual(interpret_output(0, "", ""), "", "成功但无对象 -> 空字符串")
        self.assertIsNone(interpret_output(1, "ok", ""), "非零退出码 -> 失败")
        self.assertIsNone(interpret_output(0, "", "Get-PnpDevice 无法识别"),
                          "没输出且有报错 -> 失败，别误报成没有设备")


if __name__ == "__main__":
    unittest.main()
