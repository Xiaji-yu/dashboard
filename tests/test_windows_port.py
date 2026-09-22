"""Windows 移植层的解析器测试。

这些测试**不需要 Windows**：被测的都是纯函数（文本 -> 结构化数据），
因此可以在 Linux/CI 上守住 Windows 分支的正确性。
"""

import base64
import json
import unittest
from unittest import mock

import platform_win
from platform_win import (clean_text, disk_bus, disk_media, disk_rows_from_storage,
                          disk_rows_from_wmi, drive_letter, enum_number,
                          disk_number_for_letter, disk_rows_from_physical,
                          filter_usb_instances, format_cpu_cache, infer_rotational,
                          interpret_output, is_usb_hub, merge_same_number, parse_arp,
                          parse_netsh_ssid, parse_ping_alive,
                          parse_pnp_devices, parse_services, parse_wmi_instance,
                          to_gb, to_mb)

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
        self.assertIsNone(interpret_output(0, "", "无法将该项识别为 cmdlet"),
                          "命令不存在 -> 失败，别误报成没有设备")
        self.assertEqual(interpret_output(0, "", "找不到蓝牙类"), "",
                         "仅类目不存在 -> 当作「没有这类设备」")


class DiskEnumTest(unittest.TestCase):
    """实测教训：Get-Disk 的枚举值在不同 PowerShell 上可能是数字、数字字符串或枚举名。

    第二轮实测里 rotational/bus 都是 null，就是因为只按数字查表。
    """

    def test_enum_number(self):
        self.assertEqual(enum_number(4), 4)
        self.assertEqual(enum_number("4"), 4)
        self.assertIsNone(enum_number("SSD"))
        self.assertIsNone(enum_number(None))
        self.assertIsNone(enum_number(True), )
        self.assertIsNone(enum_number(4.5))

    def test_disk_media_all_forms(self):
        for value in (4, "4", "SSD", "ssd"):
            self.assertFalse(disk_media(value), f"{value!r} 应是固态")
        for value in (3, "3", "HDD"):
            self.assertTrue(disk_media(value), f"{value!r} 应是机械")
        for value in (0, 5, "UNKNOWN", "SCM", None, "", "??"):
            self.assertIsNone(disk_media(value), f"{value!r} 应判为未知")

    def test_disk_bus_all_forms(self):
        self.assertEqual(disk_bus(8), "SATA")
        self.assertEqual(disk_bus("8"), "SATA")
        self.assertEqual(disk_bus("SATA"), "SATA")
        self.assertEqual(disk_bus("NVMe"), "NVMe")
        self.assertEqual(disk_bus(14), "NVMe")
        self.assertEqual(disk_bus(11), "虚拟")
        self.assertIsNone(disk_bus(99))
        self.assertIsNone(disk_bus("UNKNOWN"))
        self.assertIsNone(disk_bus(None))

    def test_storage_rows_accept_string_enums(self):
        """PowerShell 端用 .ToString() 后，这里必须照样认得。"""
        rows = disk_rows_from_storage([{"Number": 0, "FriendlyName": "SSD 1TB",
                                        "BusType": "NVMe", "MediaType": "SSD",
                                        "Size": "1024209543168"}])
        self.assertFalse(rows[0]["rotational"])
        self.assertEqual(rows[0]["bus"], "NVMe")

    def test_to_mb_and_cache_text(self):
        self.assertEqual(to_mb(20480), 20)
        self.assertEqual(to_mb(1536), 1.5)
        self.assertIsNone(to_mb(0))
        self.assertIsNone(to_mb("x"))
        self.assertEqual(format_cpu_cache(20480, 24576), "L2 20 MB · L3 24 MB")
        self.assertEqual(format_cpu_cache(20480, 0), "L2 20 MB")
        self.assertIsNone(format_cpu_cache(None, None))

    def test_hub_detection(self):
        """实测：VID_05E3 的三个「通用 USB 集线器」不该算外接设备（external 7 -> 4）。"""
        self.assertTrue(is_usb_hub("USB 根集线器(USB 3.0)", "USB\\ROOT_HUB30\\4&11FD050C&0&0"))
        self.assertTrue(is_usb_hub("通用 SuperSpeed USB 集线器", "USB\\VID_05E3&PID_0620\\5&2A"))
        self.assertTrue(is_usb_hub("Generic USB Hub", "USB\\VID_05E3&PID_0608\\5&2A"))
        self.assertFalse(is_usb_hub("G502 HERO", "USB\\VID_046D&PID_C08B\\1194"))
        self.assertFalse(is_usb_hub("USB 大容量存储设备", "USB\\VID_1F75&PID_0903\\0000"))
        self.assertFalse(is_usb_hub("USB Composite Device", "USB\\VID_3837&PID_3028\\554A"))


class RotationalInferenceTest(unittest.TestCase):
    """实测：NVMe 盘的 Get-Disk 不报 MediaType（rotational 为 null），需要兜底。

    兜底只用定义性证据：型号里写 SSD、或挂在 NVMe 总线上（NVMe 只有 NAND）。
    """

    def test_model_says_ssd(self):
        self.assertFalse(infer_rotational("SSD 1TB", None, None))
        self.assertFalse(infer_rotational("CT1000P3PSSD8", "SATA", None))

    def test_nvme_bus_is_always_ssd(self):
        self.assertFalse(infer_rotational("Unknown Model", "NVMe", None))

    def test_never_claims_mechanical(self):
        """型号不带 SSD、总线也判不出时，必须是 None（未知），绝不能谎报机械盘。"""
        self.assertIsNone(infer_rotational("WDC WD10EZEX-08WN4A0", "SATA", None))
        self.assertIsNone(infer_rotational(None, None, None))
        self.assertIsNone(infer_rotational("", "SCSI", None))

    def test_existing_judgement_wins(self):
        self.assertTrue(infer_rotational("SSD 1TB", "NVMe", True))
        self.assertFalse(infer_rotational("SSD 1TB", "NVMe", False))

    def test_merge_same_number_fills_gaps(self):
        """Get-PhysicalDisk 有 MediaType、Get-Disk 没有：按号互补。"""
        row = {"number": 0, "model": "SSD 1TB", "size_gb": 953.9,
               "rotational": None, "bus": "NVMe"}
        merged = merge_same_number(row, [
            {"number": 1, "rotational": True, "bus": "SATA"},
            {"number": 0, "rotational": False, "bus": None}], 0)
        self.assertFalse(merged["rotational"])
        self.assertEqual(merged["bus"], "NVMe", "已有值不被覆盖")

    def test_merge_same_number_without_match_or_number(self):
        row = {"number": None, "rotational": None, "bus": None}
        self.assertIsNone(merge_same_number(dict(row), [{"number": 0, "rotational": False}],
                                            None)["rotational"])
        self.assertIsNone(merge_same_number(dict(row), [], 0)["rotational"])


class PhysicalDiskTest(unittest.TestCase):
    """Get-PhysicalDisk 是主数据源：实测 Get-Disk 在 NVMe 上 MediaType 全为空。"""

    PHYSICAL = [
        {"DeviceId": 2, "FriendlyName": "Innostor USB3.0", "MediaType": "SSD",
         "BusType": "USB", "Size": 128035676160},
        {"DeviceId": 1, "FriendlyName": "SSD 1TB", "MediaType": "SSD",
         "BusType": "NVMe", "Size": 1024209543168},
        {"DeviceId": 0, "FriendlyName": "SSD 1TB", "MediaType": "SSD",
         "BusType": "NVMe", "Size": 1024209543168},
    ]

    def test_rows_carry_number_media_and_bus(self):
        rows = disk_rows_from_physical(self.PHYSICAL)
        self.assertEqual([row["number"] for row in rows], [2, 1, 0])
        for row in rows:
            self.assertFalse(row["rotational"], "三块都是 SSD")
        self.assertEqual(rows[0]["bus"], "USB")
        self.assertEqual(rows[1]["bus"], "NVMe")
        self.assertEqual(rows[2]["size_gb"], 953.9)
        self.assertEqual(rows[2]["device"], "磁盘 0")

    def test_picks_monitored_disk_by_number(self):
        rows = disk_rows_from_physical(self.PHYSICAL)
        target = next(row for row in rows if row["number"] == 0)
        self.assertEqual(target["model"], "SSD 1TB")
        self.assertFalse(target["rotational"])

    def test_bad_payloads(self):
        self.assertEqual(disk_rows_from_physical(None), [])
        self.assertEqual(disk_rows_from_physical([]), [])
        self.assertEqual(disk_rows_from_physical([None, "x", {}]), [])

    def test_disk_number_for_letter(self):
        with mock.patch("platform_win.powershell_json", return_value=[{"Number": 0}]):
            self.assertEqual(disk_number_for_letter("C"), 0)
        with mock.patch("platform_win.powershell_json", return_value=None):
            self.assertIsNone(disk_number_for_letter("C"))
        with mock.patch("platform_win.powershell_json", return_value=[]):
            self.assertIsNone(disk_number_for_letter("C"))
        self.assertIsNone(disk_number_for_letter(None), "没有盘符就不查")


class InterpretOutputTest(unittest.TestCase):
    """空输出 + 报错时，要区分「类目不存在」与「命令不存在」。"""

    def test_class_not_found_is_not_a_failure(self):
        self.assertEqual(interpret_output(0, "", "Get-PnpDevice: 找不到蓝牙类"), "")

    def test_command_not_found_is_a_failure(self):
        for stderr in ("Get-PnpDevice : 无法将该项识别为 cmdlet",
                       "Get-PnpDevice : The term 'Get-PnpDevice' is not recognized",
                       "CommandNotFoundException"):
            self.assertIsNone(interpret_output(0, "", stderr), stderr)


if __name__ == "__main__":
    unittest.main()
