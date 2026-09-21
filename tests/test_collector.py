"""采集层测试：网卡挑选、端口解析、快照结构，以及各项的降级路径。

降级路径是重点：本项目的设计原则是「采不到就显示不可用并给出原因」，
所以每种失败都要有用例守着。
"""

import os
import socket
import time
import unittest
from unittest import mock

import psutil

from collector import Collector, is_virtual_nic, listen_ports, pick_nic

PROC_NET_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 12345
   1: 0100007F:0035 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 12346
   2: 0100007F:C350 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1000 0 12347
"""


class VirtualNicTest(unittest.TestCase):
    def test_virtual_names_detected(self):
        for name in ["lo", "docker0", "br-18e01ed58ac0", "veth02a10c5", "virbr0", "tun0", "snap0"]:
            self.assertTrue(is_virtual_nic(name), name)

    def test_physical_names_kept(self):
        for name in ["eth0", "enx000ec6c87fb8", "enp3s0", "wlan0", "wlp2s0"]:
            self.assertFalse(is_virtual_nic(name), name)


class PickNicTest(unittest.TestCase):
    def _patch(self, stats, addrs):
        return (
            mock.patch.object(psutil, "net_if_stats", return_value=stats),
            mock.patch.object(psutil, "net_if_addrs", return_value=addrs),
        )

    def test_prefers_physical_nic_with_ipv4(self):
        stats = {
            "lo": mock.Mock(isup=True),
            "docker0": mock.Mock(isup=True),
            "br-abc": mock.Mock(isup=True),
            "veth123": mock.Mock(isup=True),
            "enx000ec6c87fb8": mock.Mock(isup=True),
        }
        addrs = {
            "lo": [mock.Mock(family=socket.AF_INET)],
            "docker0": [mock.Mock(family=socket.AF_INET)],
            "br-abc": [mock.Mock(family=socket.AF_INET)],
            "veth123": [mock.Mock(family=socket.AF_INET)],
            "enx000ec6c87fb8": [mock.Mock(family=socket.AF_INET)],
        }
        stats_patch, addrs_patch = self._patch(stats, addrs)
        with stats_patch, addrs_patch:
            self.assertEqual(pick_nic(), "enx000ec6c87fb8")

    def test_skips_interface_without_ipv4(self):
        stats = {"eth0": mock.Mock(isup=True), "eth1": mock.Mock(isup=True)}
        addrs = {"eth0": [], "eth1": [mock.Mock(family=socket.AF_INET)]}
        stats_patch, addrs_patch = self._patch(stats, addrs)
        with stats_patch, addrs_patch:
            self.assertEqual(pick_nic(), "eth1")

    def test_returns_none_when_only_virtual(self):
        stats = {"lo": mock.Mock(isup=True), "docker0": mock.Mock(isup=True)}
        addrs = {"lo": [mock.Mock(family=socket.AF_INET)]}
        stats_patch, addrs_patch = self._patch(stats, addrs)
        with stats_patch, addrs_patch:
            self.assertIsNone(pick_nic())

    def test_env_override_wins(self):
        stats = {"eth0": mock.Mock(isup=True)}
        addrs = {"eth0": [mock.Mock(family=socket.AF_INET)]}
        stats_patch, addrs_patch = self._patch(stats, addrs)
        with stats_patch, addrs_patch, mock.patch.dict(os.environ, {"DASHBOARD_NIC": "eth9"}):
            self.assertEqual(pick_nic(), "eth9")


class ListenPortsTest(unittest.TestCase):
    def test_parses_listen_ports_and_ignores_other_states(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=PROC_NET_TCP)):
            ports = listen_ports()
        # 1F90=8080 与 0035=53 是 LISTEN；C350 那条是 ESTABLISHED，必须忽略
        self.assertEqual(ports, [53, 8080])

    def test_missing_proc_file_returns_empty(self):
        with mock.patch("builtins.open", side_effect=OSError):
            self.assertEqual(listen_ports(), [])


class CollectorSnapshotTest(unittest.TestCase):
    """对着真实系统跑：只断言结构与不变量，不断言具体数值。"""

    @classmethod
    def setUpClass(cls):
        cls.collector = Collector()
        cls.collector.sample()
        time.sleep(0.2)
        cls.snapshot = cls.collector.sample()

    def test_snapshot_has_all_sections(self):
        for key in ("ts", "host", "cores", "uptime_s", "cpu", "memory", "power",
                    "net", "disk", "temp", "load"):
            self.assertIn(key, self.snapshot)
        self.assertGreater(self.snapshot["cores"], 0)
        self.assertEqual(self.snapshot["host"], socket.gethostname())

    def test_cpu_percent_in_range(self):
        cpu = self.snapshot["cpu"]
        self.assertTrue(cpu["available"])
        self.assertGreaterEqual(cpu["percent"], 0.0)
        self.assertLessEqual(cpu["percent"], 100.0)

    def test_memory_consistent(self):
        memory = self.snapshot["memory"]
        self.assertTrue(memory["available"])
        self.assertGreater(memory["total_gb"], 0)
        self.assertGreaterEqual(memory["used_gb"], 0)
        self.assertLessEqual(memory["used_gb"], memory["total_gb"])
        self.assertGreaterEqual(memory["percent"], 0.0)
        self.assertLessEqual(memory["percent"], 100.0)

    def test_disk_consistent(self):
        disk = self.snapshot["disk"]
        self.assertTrue(disk["available"])
        self.assertEqual(disk["path"], self.collector.disk_path)
        self.assertGreater(disk["total_gb"], 0)
        self.assertLessEqual(disk["free_gb"], disk["total_gb"])
        self.assertLessEqual(disk["used_percent"], 100.0)

    def test_load_averages_present(self):
        load = self.snapshot["load"]
        self.assertTrue(load["available"])
        for key in ("avg1", "avg5", "avg15"):
            self.assertIn(key, load)

    def test_unavailable_metrics_always_carry_reason(self):
        for key in ("cpu", "memory", "power", "net", "disk", "temp"):
            metric = self.snapshot[key]
            if not metric["available"]:
                self.assertTrue(metric.get("reason"), f"{key} 缺少不可用原因")

    def test_processes_sorted_desc_and_limited(self):
        rows = self.collector.processes(limit=5)
        self.assertLessEqual(len(rows), 5)
        for row in rows:
            self.assertIn("pid", row)
            self.assertIn("name", row)
            self.assertGreaterEqual(row["cpu"], 0.0)
            self.assertGreaterEqual(row["rss_mb"], 0.0)
        cpus = [row["cpu"] for row in rows]
        self.assertEqual(cpus, sorted(cpus, reverse=True))


class DegradationTest(unittest.TestCase):
    """构造失败场景，确认返回的是「不可用 + 原因」而不是抛异常。"""

    def _bare_collector(self):
        collector = Collector.__new__(Collector)  # 跳过 __init__，避免真实采样
        collector.disk_path = "/"
        collector.nic = "eth0"
        collector.cores = 4
        collector._net_prev = None
        collector._rapl_prev = None
        collector._rapl_note = None
        return collector

    def test_power_permission_error_mentions_root(self):
        collector = self._bare_collector()
        with mock.patch("builtins.open", side_effect=PermissionError):
            result = collector._power(1000.0)
        self.assertFalse(result["available"])
        self.assertIn("root", result["reason"])

    def test_power_missing_rapl_file(self):
        collector = self._bare_collector()
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            result = collector._power(1000.0)
        self.assertFalse(result["available"])
        self.assertIn("RAPL", result["reason"])

    def test_power_first_sample_is_warming_up(self):
        collector = self._bare_collector()
        with mock.patch("builtins.open", mock.mock_open(read_data="1000")):
            result = collector._power(1000.0)
        self.assertFalse(result["available"])
        self.assertIn("预热", result["reason"])

    def test_power_second_sample_computes_watts(self):
        collector = self._bare_collector()
        with mock.patch("builtins.open", mock.mock_open(read_data="1000000")):
            collector._power(1000.0)
        with mock.patch("builtins.open", mock.mock_open(read_data="3000000")):
            result = collector._power(1002.0)
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["watts"], 1.0, places=3)  # 2e6 uJ / 2s = 1 W

    def test_temperature_without_sensors(self):
        collector = self._bare_collector()
        with mock.patch.object(psutil, "sensors_temperatures", return_value={}):
            result = collector._temp()
        self.assertFalse(result["available"])
        self.assertIn("传感器", result["reason"])

    def test_net_without_nic(self):
        collector = self._bare_collector()
        collector.nic = None
        result = collector._net(1000.0)
        self.assertFalse(result["available"])
        self.assertIn("网卡", result["reason"])

    def test_net_first_sample_is_warming_up(self):
        collector = self._bare_collector()
        counters = mock.Mock(bytes_sent=10, bytes_recv=20)
        with mock.patch.object(psutil, "net_io_counters", return_value={"eth0": counters}):
            result = collector._net(1000.0)
        self.assertFalse(result["available"])
        self.assertIn("预热", result["reason"])

    def test_net_rate_from_delta(self):
        collector = self._bare_collector()
        first = mock.Mock(bytes_sent=0, bytes_recv=0)
        second = mock.Mock(bytes_sent=2048, bytes_recv=4096)
        with mock.patch.object(psutil, "net_io_counters", return_value={"eth0": first}):
            collector._net(1000.0)
        with mock.patch.object(psutil, "net_io_counters", return_value={"eth0": second}):
            result = collector._net(1002.0)
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["down_bps"], 2048.0, places=3)
        self.assertAlmostEqual(result["up_bps"], 1024.0, places=3)

    def test_docker_absent_is_reported(self):
        with mock.patch("collector.subprocess.run", side_effect=FileNotFoundError):
            rows, note = Collector._docker_containers(self._bare_collector())
        self.assertEqual(rows, [])
        self.assertIn("docker", note)


if __name__ == "__main__":
    unittest.main()
