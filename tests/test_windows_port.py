"""Windows 移植层的解析器测试。

这些测试**不需要 Windows**：被测的都是纯函数（文本 -> 结构化数据），
因此可以在 Linux/CI 上守住 Windows 分支的正确性。
"""

import unittest

from platform_win import (parse_arp, parse_netsh_ssid, parse_ping_alive, parse_pnp_devices,
                          parse_services, parse_wmi_instance)

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
  {"FriendlyName":"USB Root Hub (USB 3.0)","InstanceId":"ROOT_HUB30\\\\{EC9A0F93-5BBD-99F5-A1F4-2C9E6B4C0E2F}","Status":"OK"},
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


if __name__ == "__main__":
    unittest.main()
