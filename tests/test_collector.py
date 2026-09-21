"""采集层测试：网卡挑选、端口解析、快照结构，以及各项的降级路径。

降级路径是重点：本项目的设计原则是「采不到就显示不可用并给出原因」，
所以每种失败都要有用例守着。
"""

import os
import shutil
import socket
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import psutil

from collector import (RAPL_RETRY_SECONDS, Collector, battery_payload, is_virtual_nic,
                       listen_ports, pick_nic, temp_entry_key)

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


class TempEntryKeyTest(unittest.TestCase):
    """hwmon chip/label -> 稳定曲线键 + 中文展示名。"""

    def test_coretemp_channels(self):
        self.assertEqual(temp_entry_key("coretemp", "Package id 0"), ("package", "CPU 封装"))
        self.assertEqual(temp_entry_key("coretemp", "Core 0"), ("core0", "CPU 核心 0"))
        self.assertEqual(temp_entry_key("coretemp", "Core 1"), ("core1", "CPU 核心 1"))

    def test_pch_and_acpi(self):
        self.assertEqual(temp_entry_key("pch_skylake", ""), ("pch", "芯片组 PCH"))
        self.assertEqual(temp_entry_key("acpitz", ""), ("acpi", "机身温区"))
        self.assertEqual(temp_entry_key("cpu_thermal", ""), ("package", "CPU 封装"))

    def test_unknown_chip_falls_back_to_slug(self):
        key, label = temp_entry_key("nvme", "Composite")
        self.assertEqual(key, "composite")
        self.assertEqual(label, "Composite")


class BatteryPayloadTest(unittest.TestCase):
    """电池载荷合成：plugged/放电两种形态，以及完全没有电池的降级。"""

    def test_without_battery_reports_unavailable(self):
        payload = battery_payload(None, [])
        self.assertFalse(payload["available"])
        self.assertTrue(payload["reason"])

    def test_plugged_battery_keeps_zero_power_out(self):
        batt = SimpleNamespace(percent=94.7, power_plugged=True, secsleft=-2)
        payload = battery_payload(
            batt, [{"status": "Not charging", "cycles": "239", "power_uw": "0"}])
        self.assertTrue(payload["available"])
        self.assertEqual(payload["percent"], 94.7)
        self.assertTrue(payload["plugged"])
        self.assertEqual(payload["status"], "Not charging")
        self.assertEqual(payload["cycles"], 239)
        self.assertIsNone(payload["power_w"])  # 接通电源时 power_now=0 不作为功率展示
        self.assertIsNone(payload["secsleft"])

    def test_discharging_battery_reports_power_and_timeleft(self):
        batt = SimpleNamespace(percent=61.2, power_plugged=False, secsleft=7200)
        payload = battery_payload(
            batt, [{"status": "Discharging", "cycles": "239", "power_uw": "12300000"}])
        self.assertTrue(payload["available"])
        self.assertFalse(payload["plugged"])
        self.assertAlmostEqual(payload["power_w"], 12.3)
        self.assertEqual(payload["secsleft"], 7200)


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

    def test_memory_carries_breakdown_fields(self):
        memory = self.snapshot["memory"]
        self.assertTrue(memory["available"])
        for key in ("free_gb", "buffers_gb", "cached_gb", "shared_gb", "swap_total_gb"):
            self.assertIn(key, memory)
            self.assertGreaterEqual(memory[key], 0)

    def test_performance_section_structure(self):
        perf = self.snapshot["performance"]
        for key in ("cpu", "gpu", "temps", "fans", "battery", "memory", "load", "power"):
            self.assertIn(key, perf, key)

        cores = perf["cpu"]["per_core"]
        self.assertTrue(cores["available"])
        self.assertEqual(len(cores["per_cpu"]), self.collector.cores)
        self.assertEqual(len(cores["topology"]), self.collector.cores)
        for percent in cores["per_cpu"]:
            self.assertGreaterEqual(percent, 0.0)
            self.assertLessEqual(percent, 100.0)

        temps = perf["temps"]
        if temps["available"]:
            self.assertTrue(temps["list"])
            for entry in temps["list"]:
                self.assertIn("key", entry)
                self.assertIn("label", entry)
                self.assertGreater(entry["celsius"], -60)
        else:
            self.assertTrue(temps["reason"])

        fans = perf["fans"]
        if fans["available"]:
            for fan in fans["list"]:
                self.assertIn("key", fan)
                self.assertIn("label", fan)
                self.assertGreaterEqual(fan["rpm"], 0)
        else:
            self.assertTrue(fans["reason"])

        battery = perf["battery"]
        if battery["available"]:
            self.assertGreaterEqual(battery["percent"], 0)
            self.assertLessEqual(battery["percent"], 100)
        else:
            self.assertTrue(battery["reason"])


class PowerRaplTest(unittest.TestCase):
    """RAPL 多域采集：夹具目录驱动，不需要 root，也不依赖真实硬件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DASHBOARD_RAPL_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.collector = self._fresh_collector()

    @staticmethod
    def _fresh_collector(root=None):
        collector = Collector.__new__(Collector)
        collector._rapl_prev = {}
        collector._rapl_note = None
        collector._rapl_domains = None
        return collector

    def write_at(self, dirname, name, energy):
        base = os.path.join(self.tmp.name, dirname)
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "name"), "w") as handle:
            handle.write(name)
        with open(os.path.join(base, "energy_uj"), "w") as handle:
            handle.write(str(energy))
        return os.path.join(base, "energy_uj")

    def write_domain(self, name, energy, index):
        return self.write_at("intel-rapl:%d" % index, name, energy)

    def test_first_sample_warms_up(self):
        self.write_domain("package-0", 1000000, 0)
        result = self.collector._power(1000.0)
        self.assertFalse(result["available"])
        self.assertIn("预热", result["reason"])

    def test_psys_is_primary_with_domain_breakdown(self):
        self.write_domain("package-0", 1000000, 0)
        self.write_domain("psys", 1000000, 1)
        self.collector._power(1000.0)
        self.write_domain("package-0", 3000000, 0)   # 2 J / 2 s = 1 W
        self.write_domain("psys", 5000000, 1)        # 4 J / 2 s = 2 W
        result = self.collector._power(1002.0)
        self.assertTrue(result["available"])
        self.assertEqual(result["source"], "平台功耗")
        self.assertAlmostEqual(result["watts"], 2.0, places=3)
        self.assertEqual([item["name"] for item in result["domains"]], ["psys", "package-0"])
        self.assertEqual(result["domains"][0]["label"], "平台功耗")
        self.assertEqual(result["domains"][1]["label"], "CPU 封装")
        self.assertAlmostEqual(result["domains"][1]["watts"], 1.0, places=3)

    def test_falls_back_to_package_without_psys(self):
        self.write_domain("package-0", 1000000, 0)
        self.collector._power(1000.0)
        self.write_domain("package-0", 2000000, 0)
        result = self.collector._power(1001.0)
        self.assertTrue(result["available"])
        self.assertEqual(result["source"], "CPU 封装")
        self.assertAlmostEqual(result["watts"], 1.0, places=3)

    def test_missing_dir_reports_no_counter(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        with mock.patch.dict(os.environ, {"DASHBOARD_RAPL_DIR": empty}):
            collector = self._fresh_collector()
            result = collector._power(1000.0)
        self.assertFalse(result["available"])
        self.assertIn("RAPL", result["reason"])

    def test_permission_denied_mentions_root(self):
        path = self.write_domain("package-0", 1000000, 0)
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o644)
        result = self.collector._power(1000.0)
        self.assertFalse(result["available"])
        self.assertIn("root", result["reason"])

    def test_msr_and_mmio_interfaces_are_deduped(self):
        """同一计数器同时暴露 MSR 与 MMIO 时只保留一个，且优先 MSR。"""
        self.write_at("intel-rapl-mmio:0", "package-0", 1000000)
        self.write_at("intel-rapl:0", "package-0", 1000000)
        paths = self.collector._rapl_paths()
        self.assertEqual([name for name, _ in paths], ["package-0"])
        self.assertIn("intel-rapl:0/", paths[0][1])
        self.assertNotIn("mmio", paths[0][1])

    def test_unimplemented_psys_is_not_primary(self):
        """psys 比封装还小时判为该平台未实现：主值改用封装，psys 标出来。"""
        self.write_at("intel-rapl:0", "package-0", 1000000)
        self.write_at("intel-rapl:1", "psys", 1000000)
        self.collector._power(1000.0)
        self.write_at("intel-rapl:0", "package-0", 5000000)   # 4 J / 2 s = 2 W
        self.write_at("intel-rapl:1", "psys", 2000000)        # 1 J / 2 s = 0.5 W
        result = self.collector._power(1002.0)
        self.assertTrue(result["available"])
        self.assertEqual(result["source"], "CPU 封装")
        self.assertAlmostEqual(result["watts"], 2.0, places=3)
        psys = [item for item in result["domains"] if item["name"] == "psys"][0]
        self.assertTrue(psys.get("suspect"))

    def test_counter_reset_is_skipped(self):
        self.write_domain("package-0", 5000000, 0)
        self.collector._power(1000.0)
        self.write_domain("package-0", 1000, 0)   # 计数器回绕：差值为负，跳过该点
        result = self.collector._power(1001.0)
        self.assertFalse(result["available"])
        self.assertIn("预热", result["reason"])

    def test_partial_permission_keeps_readable_domains(self):
        """只放开部分域时，剩下能读的照常显示，而不是整块变不可用。"""
        bad = self.write_domain("package-0", 1000000, 0)
        self.write_domain("psys", 1000000, 1)
        os.chmod(bad, 0o000)
        self.addCleanup(os.chmod, bad, 0o644)

        self.collector._power(1000.0)          # 首次只更新基准
        self.write_domain("psys", 2000000, 1)
        result = self.collector._power(1001.0)
        self.assertTrue(result["available"])
        self.assertEqual(result["source"], "平台功耗")
        self.assertAlmostEqual(result["watts"], 1.0, places=3)
        self.assertEqual(result["skipped"], ["package-0"])
        self.assertEqual([item["name"] for item in result["domains"]], ["psys"])

    def test_all_domains_unreadable_reports_permission(self):
        path = self.write_domain("package-0", 1000000, 0)
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o644)
        result = self.collector._power(1000.0)
        self.assertFalse(result["available"])
        self.assertIn("root", result["reason"])

    def test_retries_after_cooldown(self):
        """权限往往是事后才放开的：冷却结束后自动恢复，不必重启服务。"""
        path = self.write_domain("package-0", 1000000, 0)
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o644)
        first = self.collector._power(1000.0)
        self.assertFalse(first["available"])
        self.assertIn("root", first["reason"])

        # 冷却期内不重复读 sysfs：即使权限已放开，也仍返回缓存的原因
        os.chmod(path, 0o644)
        during = self.collector._power(1000.0 + RAPL_RETRY_SECONDS / 2)
        self.assertFalse(during["available"])
        self.assertIn("root", during["reason"])

        # 冷却结束：重新读到基准，下一次给出瓦数
        self.collector._power(1000.0 + RAPL_RETRY_SECONDS + 1)
        self.write_domain("package-0", 3000000, 0)
        result = self.collector._power(1000.0 + RAPL_RETRY_SECONDS + 2)
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["watts"], 2.0, places=3)


class DegradationTest(unittest.TestCase):
    """构造失败场景，确认返回的是「不可用 + 原因」而不是抛异常。"""

    def _bare_collector(self):
        collector = Collector.__new__(Collector)  # 跳过 __init__，避免真实采样
        collector.disk_path = "/"
        collector.nic = "eth0"
        collector.cores = 4
        collector._net_prev = None
        collector._rapl_prev = {}
        collector._rapl_note = None
        collector._rapl_domains = None
        return collector

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

    def test_temperature_exception_is_degraded(self):
        collector = self._bare_collector()
        with mock.patch.object(psutil, "sensors_temperatures", side_effect=RuntimeError("boom")):
            result = collector._temp()
        self.assertFalse(result["available"])
        self.assertIn("温度采样失败", result["reason"])

    def test_no_fans_is_degraded(self):
        collector = self._bare_collector()
        with mock.patch.object(psutil, "sensors_fans", return_value={}):
            result = collector._fans()
        self.assertFalse(result["available"])
        self.assertTrue(result["reason"])

    def test_gpu_without_card_is_degraded(self):
        collector = self._bare_collector()
        collector._gpu_card = None
        result = collector._gpu_freq()
        self.assertFalse(result["available"])
        self.assertIn("GPU", result["reason"])

    def test_gpu_frequency_read(self):
        collector = self._bare_collector()
        collector._gpu_card = "card0"
        with mock.patch.object(Collector, "_read_sys", side_effect=["350", "1000"]):
            result = collector._gpu_freq()
        self.assertTrue(result["available"])
        self.assertEqual(result["freq_mhz"], 350)
        self.assertEqual(result["max_mhz"], 1000)

    def test_battery_without_sensor_is_degraded(self):
        collector = self._bare_collector()
        with mock.patch.object(psutil, "sensors_battery", return_value=None):
            result = collector._battery()
        self.assertFalse(result["available"])
        self.assertIn("电池", result["reason"])


if __name__ == "__main__":
    unittest.main()
