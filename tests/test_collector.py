"""采集层测试：网卡挑选、端口解析、快照结构，以及各项的降级路径。

降级路径是重点：本项目的设计原则是「采不到就显示不可用并给出原因」，
所以每种失败都要有用例守着。
"""

import io
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import psutil

import collector as collector_module
from collector import (RAPL_RETRY_SECONDS, Collector, battery_payload, container_id_from_cgroup,
                       parse_probe_targets,
                       format_khz, format_sockaddr, is_virtual_nic, is_wireless_nic, listen_ports,
                       listen_sockets, load_oui, oui_vendor, parse_arp_table, parse_cpu_flags,
                       parse_cpu_model, parse_default_gateway, parse_os_release,
                       parse_ssdp_response, pick_nic, subnet_candidates, subnet_label,
                       temp_entry_key, virtualization_label)

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


class ContainerCgroupTest(unittest.TestCase):
    """从 cgroup 识别容器归属：兼容 docker / containerd / podman 的写法。"""

    def test_docker_scope_and_path_forms(self):
        cases = {
            "0::/system.slice/docker-1a2b3c4d5e6f7890a1b2c3d4e5f60718.scope": "1a2b3c4d5e6f7890a1b2c3d4e5f60718",
            "12:memory:/docker/1a2b3c4d5e6f7890a1b2c3d4e5f60718": "1a2b3c4d5e6f7890a1b2c3d4e5f60718",
            "0::/kubepods/besteffort/pod1/cri-containerd-1a2b3c4d5e6f7890a1b2c3d4e5f60718.scope":
                "1a2b3c4d5e6f7890a1b2c3d4e5f60718",
            "0::/user.slice/user-1000.slice/session-1.scope": None,
            "0::/system.slice/ssh.service": None,
            "": None,
        }
        for text, expected in cases.items():
            self.assertEqual(container_id_from_cgroup(text), expected, text)


class ProcessListTest(unittest.TestCase):
    """对着真实系统跑：只断言结构与不变量。"""

    @classmethod
    def setUpClass(cls):
        cls.collector = Collector()
        cls.collector.process_list()
        time.sleep(0.2)
        cls.rows = cls.collector.process_list()

    def test_rows_carry_all_fields(self):
        self.assertTrue(self.rows)
        for row in self.rows[:20]:
            for key in ("pid", "name", "user", "cpu", "rss_mb", "status",
                        "threads", "started", "cmd", "container"):
                self.assertIn(key, row, key)
            self.assertGreater(row["pid"], 0)
            self.assertGreaterEqual(row["cpu"], 0.0)
            self.assertLessEqual(row["cpu"], 100.0, "CPU 应当归一化到 0-100")
            self.assertGreaterEqual(row["rss_mb"], 0.0)
            self.assertGreaterEqual(row["threads"], 1)
            self.assertGreater(row["started"], 0)

    def test_sorted_by_cpu_desc(self):
        cpus = [row["cpu"] for row in self.rows]
        self.assertEqual(cpus, sorted(cpus, reverse=True))

    def test_contains_current_process(self):
        pids = {row["pid"] for row in self.rows}
        self.assertIn(os.getpid(), pids)

    def test_processes_helper_limits(self):
        self.assertLessEqual(len(self.collector.processes(limit=5)), 5)

    def test_container_id_from_proc(self):
        collector = Collector.__new__(Collector)
        with mock.patch.object(Collector, "_read_sys",
                               return_value="0::/system.slice/docker-1a2b3c4d5e6f7890a1b2.scope"):
            self.assertEqual(collector._process_container_id(1234), "1a2b3c4d5e6f")
        with mock.patch.object(Collector, "_read_sys",
                               return_value="0::/user.slice/session-1.scope"):
            self.assertIsNone(collector._process_container_id(1234))

    def test_container_name_resolved_live(self):
        """名字映射可能晚于首次见到进程才建立，所以每次读取时实时映射。"""
        collector = Collector.__new__(Collector)
        collector._container_names = {}
        self.assertEqual(collector._container_of("abcdef123456"),
                         {"id": "abcdef123456", "name": None})
        collector._container_names = {"abcdef123456": "homeassistant"}
        self.assertEqual(collector._container_of("abcdef123456"),
                         {"id": "abcdef123456", "name": "homeassistant"})
        self.assertIsNone(collector._container_of(None))


class DefaultGatewayTest(unittest.TestCase):
    """/proc/net/route 解析：网关是十六进制小端。"""

    ROUTE = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "enx000ec6c87fb8\t00000000\t0201A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        "enx000ec6c87fb8\t0000A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
    )

    def test_parses_default_gateway(self):
        self.assertEqual(parse_default_gateway(self.ROUTE), "192.168.1.2")

    def test_missing_default_route(self):
        self.assertIsNone(parse_default_gateway("Iface\tDestination\neth0\t0000A8C0\n"))
        self.assertIsNone(parse_default_gateway(""))
        self.assertIsNone(parse_default_gateway(None))

    def test_wireless_detection(self):
        for name in ("wlp2s0", "wlan0", "wlx00c0ca123456"):
            self.assertTrue(is_wireless_nic(name), name)
        for name in ("enx000ec6c87fb8", "eth0", "enp3s0", None):
            self.assertFalse(is_wireless_nic(name), name)


class NetworkDiskInfoTest(unittest.TestCase):
    """对着真实系统跑：只断言结构与不变量。"""

    @classmethod
    def setUpClass(cls):
        cls.collector = Collector()
        cls.collector.sample()
        cls.network = cls.collector.network_info()

    def test_nic_carries_link_fields(self):
        nic = self.network["nic"]
        if not nic.get("available"):
            self.assertTrue(nic.get("reason"))
            return
        for key in ("name", "wireless", "up", "mtu"):
            self.assertIn(key, nic, key)
        self.assertIsInstance(nic["wireless"], bool)

    def test_connection_summary(self):
        conn = self.network["connection"]
        for key in ("local_ip", "gateway", "medium", "listening", "proxy_port", "total"):
            self.assertIn(key, conn, key)
        if conn.get("available"):
            self.assertGreaterEqual(conn["total"], 1)
            for item in conn["remotes"]:
                self.assertIn("addr", item)
                self.assertIn("count", item)

    def test_latency_helper_degrades_quietly(self):
        self.assertIsNone(Collector.tcp_latency(None, 443))
        self.assertIsNone(Collector.tcp_latency("127.0.0.1", 1, timeout=0.2))

    def test_disk_bundle_fields_and_rates(self):
        self.collector._disk(time.time())          # 第一次只建立基准
        time.sleep(0.3)
        disk = self.collector._disk(time.time())
        self.assertTrue(disk["available"])
        for key in ("device", "model", "size_gb", "rotational", "mounts",
                    "read_bps", "write_bps", "read_total_gb", "write_total_gb"):
            self.assertIn(key, disk, key)
        for item in disk["mounts"]:
            self.assertIn("mount", item)
            self.assertIn("used_percent", item)
            self.assertTrue(item["device"].startswith("/dev/"))
            self.assertNotIn("/dev/loop", item["device"], "snap 的 loop 挂载不该出现")
            self.assertNotEqual(item["fstype"], "squashfs")
        # 这里只校验字段齐全与挂载过滤；速率的有无取决于根挂载是否落在真实块设备上，
        # 具体计算由 DiskRateCalculationTest 用夹具驱动（不再拿实现自己的输出当期望值）。
        for key in ("read_bps", "write_bps", "read_total_gb", "write_total_gb"):
            self.assertIn(key, disk, key)

    def test_series_values_include_disk_io(self):
        import server
        snapshot = {"cpu": {"available": False}, "memory": {"available": False},
                    "power": {"available": False}, "net": {"available": False},
                    "temp": {"available": False},
                    "disk": {"available": True, "free_gb": 10.0,
                             "read_bps": 1234.5, "write_bps": 67.8}}
        values = server.series_values(snapshot)
        self.assertEqual(values["disk_read"], 1234.5)
        self.assertEqual(values["disk_write"], 67.8)


class ListenSocketsTest(unittest.TestCase):
    """解析 /proc/net/tcp{,6}：端口 + 绑定地址 + 谁能访问。"""

    PROC_TCP = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 12345\n"
        "   1: 0100007F:0035 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 12346\n"
        "   2: 0100007F:C350 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1000 0 12347\n"
    )
    PROC_TCP6 = "  sl  local_address                         rem_address   st inode\n"

    @classmethod
    def _patch_open(cls):
        def fake_open(path, *args, **kwargs):
            if path == "/proc/net/tcp":
                return mock.mock_open(read_data=cls.PROC_TCP)()
            if path == "/proc/net/tcp6":
                return mock.mock_open(read_data=cls.PROC_TCP6)()
            raise OSError("no such file")
        return mock.patch("builtins.open", side_effect=fake_open)

    def test_scope_from_bind_address(self):
        with self._patch_open():
            rows = listen_sockets()
        self.assertEqual([row["port"] for row in rows], [53, 8080])   # 已建立那条要忽略
        self.assertEqual(rows[0]["scope"], "仅本机")
        self.assertEqual(rows[1]["scope"], "局域网")
        self.assertEqual(rows[1]["addr"], "0.0.0.0")
        self.assertEqual(rows[1]["proto"], "tcp")

    def test_format_sockaddr(self):
        self.assertEqual(format_sockaddr("0100007F", False), "127.0.0.1")
        self.assertEqual(format_sockaddr("00000000", False), "0.0.0.0")
        self.assertEqual(format_sockaddr("zzzz", False), "zzzz")
        self.assertEqual(format_sockaddr("00000000000000000000000000000000", True), "::")


class ServicePortsTest(unittest.TestCase):
    """端口合并：同一端口在 tcp/tcp6 各一条时合成一行，范围取更宽的。"""

    def _collector(self):
        collector = Collector.__new__(Collector)
        collector._port_procs = {8080: "python3"}   # 真实实现用 int 端口做键
        collector._port_procs_at = time.time() + 3600   # 别触发刷新
        collector._port_procs_lock = threading.Lock()
        return collector

    def test_merge_and_scope(self):
        collector = self._collector()
        sockets = [
            {"port": 22, "proto": "tcp", "addr": "0.0.0.0", "scope": "局域网"},
            {"port": 22, "proto": "tcp6", "addr": "::", "scope": "局域网"},
            {"port": 631, "proto": "tcp", "addr": "127.0.0.1", "scope": "仅本机"},
            {"port": 8080, "proto": "tcp", "addr": "0.0.0.0", "scope": "局域网"},
        ]
        with mock.patch.object(collector_module, "listen_sockets", return_value=sockets):
            rows = collector.service_ports()
        by_port = {row["port"]: row for row in rows}
        self.assertEqual(by_port[22]["proto"], "tcp/tcp6")
        self.assertEqual(by_port[22]["addr"], "0.0.0.0", "地址应保留先出现的 IPv4")
        self.assertEqual(by_port[22]["known"], "SSH")
        self.assertEqual(by_port[631]["scope"], "仅本机")
        self.assertEqual(by_port[8080]["process"], "python3")
        self.assertIsNone(by_port[8080]["known"], "有进程名时不必给常见用途提示")

    def test_systemd_parsing(self):
        output = (
            "ssh.service                loaded active running OpenBSD Secure Shell server\n"
            "docker.service             loaded active running Docker Application Container Engine\n"
            "not-a-service              loaded active running 忽略这一行\n"
        )
        done = mock.Mock(returncode=0, stdout=output, stderr="")
        with mock.patch("collector.subprocess.run", return_value=done):
            result = Collector._collect_systemd()
        self.assertTrue(result["available"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["list"][0]["unit"], "docker.service")
        self.assertEqual(result["list"][0]["description"],
                         "Docker Application Container Engine")

    def test_systemd_absent(self):
        with mock.patch("collector.subprocess.run", side_effect=FileNotFoundError):
            result = Collector._collect_systemd()
        self.assertFalse(result["available"])
        self.assertIn("systemctl", result["reason"])


class ProbeTargetsTest(unittest.TestCase):
    """probes.json 读取：默认没文件不算错误，格式错误要报出来，mtime 变了要重载。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "probes.json")
        env = mock.patch.dict(os.environ, {"DASHBOARD_PROBES": self.path})
        env.start()
        self.addCleanup(env.stop)
        self.collector = Collector.__new__(Collector)
        self.collector._probes_cache = ([], None, None)

    def test_missing_file_is_not_an_error(self):
        targets, error, _mtime = self.collector.probe_targets()
        self.assertEqual(targets, [])
        self.assertIsNone(error, "文件不存在时不该报错")

    def test_parses_targets_and_skips_incomplete(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"probes": [
                {"name": "腾讯云", "host": "1.2.3.4", "port": 443},
                {"name": "缺端口", "host": "example.com"},
                {"host": "9.9.9.9", "port": "53"},
            ]}, handle)
        targets, error, _mtime = self.collector.probe_targets()
        # 现在会明确告诉用户哪一项被跳过，而不是静默丢弃
        self.assertIn("第 2 项", error)
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0], {"name": "腾讯云", "host": "1.2.3.4", "port": 443})
        self.assertEqual(targets[1]["name"], "9.9.9.9", "没有名字就用 host")
        self.assertEqual(targets[1]["port"], 53)

    def test_broken_json_reports_error(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")
        targets, error, _mtime = self.collector.probe_targets()
        self.assertEqual(targets, [])
        self.assertIn("probes.json", error)

    def test_reloads_when_file_changes(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"probes": [{"name": "A", "host": "1.1.1.1", "port": 80}]}, handle)
        self.assertEqual(len(self.collector.probe_targets()[0]), 1)
        time.sleep(0.01)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"probes": [
                {"name": "A", "host": "1.1.1.1", "port": 80},
                {"name": "B", "host": "2.2.2.2", "port": 22}]}, handle)
        os.utime(self.path, (time.time() + 1, time.time() + 1))
        self.assertEqual(len(self.collector.probe_targets()[0]), 2, "mtime 变了应重载")


class DeviceInfoTest(unittest.TestCase):
    """设备页信息：解析函数 + 真实系统的结构断言。"""

    def test_parse_os_release(self):
        text = 'NAME="Ubuntu"\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.5 LTS"\n# 注释\n空行上面\n'
        info = parse_os_release(text)
        self.assertEqual(info["NAME"], "Ubuntu")
        self.assertEqual(info["PRETTY_NAME"], "Ubuntu 24.04.5 LTS")

    def test_parse_cpu_model_and_flags(self):
        text = ("processor\t: 0\nmodel name\t: Intel(R) Core(TM) i5-7200U CPU @ 2.50GHz\n"
                "flags\t\t: fpu vme de pse vmx lm\n")
        self.assertEqual(parse_cpu_model(text), "Intel(R) Core(TM) i5-7200U CPU @ 2.50GHz")
        flags = parse_cpu_flags(text)
        self.assertIn("vmx", flags)
        self.assertEqual(virtualization_label(flags), "VT-x")
        self.assertEqual(virtualization_label({"svm"}), "AMD-V")
        self.assertIsNone(virtualization_label(set()))
        self.assertIsNone(parse_cpu_model(""))

    def test_format_khz(self):
        self.assertEqual(format_khz("3100000"), 3100)
        self.assertEqual(format_khz("400000"), 400)
        self.assertIsNone(format_khz(""))
        self.assertIsNone(format_khz(None))

    def test_device_info_structure(self):
        info = Collector().device_info()
        for key in ("summary", "usb", "bluetooth", "interfaces", "lan", "runtime"):
            self.assertIn(key, info, key)
        summary = info["summary"]
        self.assertEqual(summary["hostname"], socket.gethostname())
        self.assertTrue(summary["kernel"])
        self.assertTrue(summary["arch"])
        self.assertGreater(summary["uptime_s"], 0)
        self.assertGreater(summary["threads"], 0)
        self.assertTrue(summary["cpu"])
        self.assertGreater(summary["memory_gb"], 0)
        # USB：结构要对；虚机可能整条 USB 总线都没有，所以不断言非空
        self.assertIsInstance(info["usb"]["list"], list)
        self.assertLessEqual(info["usb"]["external"], info["usb"]["total"])
        for item in info["usb"]["list"]:
            self.assertIn("id", item)
            self.assertIn("hub", item)
        # 网络：至少回环
        self.assertTrue(info["interfaces"]["physical"])
        # 局域网：结构齐全（本机可能一个邻居都没有）
        self.assertIn("hosts", info["lan"])
        self.assertIn("subnet", info["lan"])
        self.assertIsInstance(info["lan"]["hosts"], list)
        self.assertNotIn("pci", info, "PCI 已按需求移除")
        self.assertTrue(info["runtime"]["python"])

    def test_interface_kind(self):
        cases = {"lo": "回环", "wlan0": "无线", "wlx00c0ca123456": "无线",
                 "docker0": "虚拟", "br-18e01ed58ac0": "虚拟", "veth02a10c5": "虚拟",
                 "eth0": "有线", "enx000ec6c87fb8": "有线"}
        for name, kind in cases.items():
            self.assertEqual(Collector.interface_kind(name), kind, name)


    def test_usb_devices_flags_root_hubs(self):
        """1d6b 是根集线器（控制器），要和外接设备区分开。"""
        collector = Collector.__new__(Collector)
        values = {
            "/sys/bus/usb/devices/1-1/idVendor": "0b95",
            "/sys/bus/usb/devices/1-1/idProduct": "772a",
            "/sys/bus/usb/devices/1-1/manufacturer": "ASIX Elec. Corp.",
            "/sys/bus/usb/devices/1-1/product": "AX88772A",
            "/sys/bus/usb/devices/1-1/busnum": "1",
            "/sys/bus/usb/devices/1-1/devnum": "2",
            "/sys/bus/usb/devices/usb1/idVendor": "1d6b",
            "/sys/bus/usb/devices/usb1/idProduct": "0002",
            "/sys/bus/usb/devices/usb1/product": "xHCI Host Controller",
            "/sys/bus/usb/devices/usb1/busnum": "1",
            "/sys/bus/usb/devices/usb1/devnum": "1",
        }
        with mock.patch("os.listdir", return_value=["1-1", "usb1"]), \
                mock.patch.object(Collector, "_read_sys", side_effect=values.get):
            rows = collector._usb_devices()
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["hub"], "外接设备排在前面")
        self.assertEqual(rows[0]["id"], "0b95:772a")
        self.assertEqual(rows[0]["product"], "AX88772A")
        self.assertTrue(rows[1]["hub"])

    def test_wireless_info_without_nic(self):
        collector = Collector.__new__(Collector)
        with mock.patch.object(Collector, "_read_sys", return_value=None):
            self.assertIsNone(collector._wireless_info("wlan0"))

    def test_wireless_info_parses_signal_and_ssid(self):
        collector = Collector.__new__(Collector)
        proc = ("Inter-| sta-|   Quality        |   Discarded packets\n"
                " face | tus | link level noise |  nwid  crypt   frag  retry\n"
                "wlan0: 0000   55.  -55.  -256        0      0      0      0\n")
        done = mock.Mock(returncode=0, stdout='wlan0     IEEE 802.11  ESSID:"MyWiFi"\n', stderr="")
        with mock.patch.object(Collector, "_read_sys", return_value=proc), \
                mock.patch("collector.subprocess.run", return_value=done):
            info = collector._wireless_info("wlan0")
        self.assertEqual(info["ssid"], "MyWiFi")
        self.assertEqual(info["signal_dbm"], -55.0)
        self.assertEqual(info["quality"], 55.0)

    def test_subnet_candidates(self):
        hosts = subnet_candidates("192.168.1.111", "255.255.255.0")
        self.assertEqual(len(hosts), 253, "整段 /24 去掉网络地址、广播地址与本机")
        self.assertNotIn("192.168.1.111", hosts)
        self.assertNotIn("192.168.1.0", hosts)
        self.assertNotIn("192.168.1.255", hosts)
        self.assertIn("192.168.1.1", hosts)
        # 网段比 /24 大时收敛到本机所在 /24
        big = subnet_candidates("10.1.2.3", "255.255.0.0")
        self.assertEqual(len(big), 253)
        self.assertTrue(all(item.startswith("10.1.2.") for item in big))
        self.assertNotIn("10.1.2.3", big)
        # 异常输入不该炸
        self.assertEqual(subnet_candidates(None, None), [])
        self.assertEqual(subnet_candidates("1.2.3", "255.255.255.0"), [])
        self.assertEqual(subnet_candidates("1.2.3.4", "x"), [])

    def test_subnet_label(self):
        self.assertEqual(subnet_label("192.168.1.111", "255.255.255.0"), "192.168.1.0/24")
        self.assertEqual(subnet_label("10.1.2.3", "255.255.0.0"), "10.1.0.0/16")
        self.assertIsNone(subnet_label("bad", "255.255.255.0"))
        self.assertIsNone(subnet_label(None, None))

    def test_parse_arp_table(self):
        text = ("IP address       HW type     Flags       HW address            Mask     Device\n"
                "192.168.1.2      0x1         0x2         5a:42:70:53:b0:5c     *        enx0\n"
                "192.168.1.236    0x1         0x0         00:00:00:00:00:00     *        enx0\n"
                "172.24.0.2       0x1         0x2         5E:B7:8D:07:F7:E1     *        br-1\n")
        rows = parse_arp_table(text)
        self.assertEqual(rows["192.168.1.2"], "5a:42:70:53:b0:5c")
        self.assertNotIn("192.168.1.236", rows, "全零 MAC 的失败条目要跳过")
        self.assertEqual(rows["172.24.0.2"], "5e:b7:8d:07:f7:e1")
        self.assertEqual(parse_arp_table(""), {})

    def test_parse_ssdp_response(self):
        text = ("HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\n"
                "SERVER: Linux/3.14 UPnP/1.0 MiniUPnPd/2.2\r\n"
                "LOCATION: http://192.168.1.2:1900/rootDesc.xml\r\n"
                "ST: upnp:rootdevice\r\nUSN: uuid:x::upnp:rootdevice\r\n\r\n")
        info = parse_ssdp_response(text)
        self.assertIn("MiniUPnPd", info["server"])
        self.assertEqual(info["location"], "http://192.168.1.2:1900/rootDesc.xml")
        self.assertEqual(info["st"], "upnp:rootdevice")
        self.assertIsNone(parse_ssdp_response("not a response"))

    def test_oui_vendor(self):
        table = {"00:0E:C6": "ASIX ELECTRONICS CORP."}
        self.assertEqual(oui_vendor("00:0e:c6:c8:7f:b8", table), "ASIX ELECTRONICS CORP.")
        self.assertIsNone(oui_vendor("aa:bb:cc:dd:ee:ff", table))
        self.assertIsNone(oui_vendor("00:00:00:00:00:00", table))
        self.assertIsNone(oui_vendor(None, table))

    def test_load_oui_from_system(self):
        table = load_oui()
        self.assertIsInstance(table, dict)
        for key in list(table)[:5]:
            self.assertEqual(len(key), 8, "键是 AA:BB:CC 形式")

    def test_lan_devices_structure(self):
        """没有扫描缓存时也能给出结构：邻居表里的条目 + 网段信息。"""
        collector = Collector()
        result = collector.lan_devices()
        for key in ("hosts", "subnet", "swept", "live", "oui"):
            self.assertIn(key, result, key)
        self.assertIsInstance(result["hosts"], list)
        subnet = collector._local_subnet()
        if subnet and result["hosts"]:
            for host in result["hosts"]:
                self.assertTrue(host["ip"].startswith(subnet["prefix"]),
                                "docker 网桥的条目不该混进局域网设备")
                self.assertIn("sources", host)

    def test_reverse_dns_strips_lan_suffix(self):
        collector = Collector.__new__(Collector)
        with mock.patch("socket.gethostbyaddr", return_value=("Xiaomi-14-Pro.lan", [], ["1"])):
            self.assertEqual(collector._reverse_dns("192.168.1.236"), "Xiaomi-14-Pro")
        with mock.patch("socket.gethostbyaddr", side_effect=socket.herror):
            self.assertIsNone(collector._reverse_dns("192.168.1.236"))

    def test_bluetooth_without_adapter(self):
        collector = Collector.__new__(Collector)
        with mock.patch("os.listdir", side_effect=OSError):
            result = collector._bluetooth()
        self.assertFalse(result["available"])
        self.assertIn("适配器", result["reason"])

    def test_bluetooth_lists_devices(self):
        collector = Collector.__new__(Collector)
        done = mock.Mock(returncode=0,
                         stdout="Device AA:BB:CC:DD:EE:FF 键鼠套装\n", stderr="")
        with mock.patch("os.listdir", return_value=["hci0"]), \
                mock.patch("collector.subprocess.run", return_value=done):
            result = collector._bluetooth()
        self.assertTrue(result["available"])
        self.assertEqual(result["adapters"], ["hci0"])
        self.assertEqual(result["devices"][0]["name"], "键鼠套装")

    def test_device_info_is_cached(self):
        collector = Collector()
        first = collector.device_info()
        second = collector.device_info()
        self.assertIs(first, second, "60 秒缓存内应当是同一份对象")


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

class ProbeTargetParsingTest(unittest.TestCase):
    """probes.json 的校验必须只记录问题、绝不抛异常（否则会连带冻结整站采样）。"""

    def test_valid_file(self):
        targets, problems = parse_probe_targets({
            "probes": [{"name": "百度", "host": "www.baidu.com", "port": 443},
                       {"host": " 1.1.1.1 ", "port": "53"}]})
        self.assertEqual(problems, [])
        self.assertEqual(targets[0], {"name": "百度", "host": "www.baidu.com", "port": 443})
        self.assertEqual(targets[1]["host"], "1.1.1.1", "host 要去空白")
        self.assertEqual(targets[1]["port"], 53, "字符串端口要转成整数")
        self.assertEqual(targets[1]["name"], "1.1.1.1", "没有 name 时用 host")

    def test_bad_shapes_return_problems_instead_of_raising(self):
        cases = [
            (["not", "an", "object"], "顶层应当是对象"),
            ("a string", "顶层应当是对象"),
            (123, "顶层应当是对象"),
            ({}, "没有 probes 数组"),
            ({"probes": "not-a-list"}, "probes 应当是数组"),
            ({"probes": {"host": "x"}}, "probes 应当是数组"),
        ]
        for data, expected in cases:
            with self.subTest(data=data):
                targets, problems = parse_probe_targets(data)
                self.assertEqual(targets, [])
                self.assertTrue(any(expected in item for item in problems),
                                f"{data} -> {problems}")

    def test_skips_bad_entries_with_reasons(self):
        targets, problems = parse_probe_targets({"probes": [
            "字符串元素",
            {"host": "ok.example", "port": 80},
            {"port": 80},
            {"host": "x", "port": "abc"},
            {"host": "x", "port": 70000},
            {"host": "x", "port": 0},
            {"host": "   ", "port": 80},
            {"host": ["not", "str"], "port": 80},
        ]})
        self.assertEqual([item["host"] for item in targets], ["ok.example"])
        self.assertEqual(len(problems), 7, problems)

    def test_probe_targets_survives_malformed_file(self):
        """真实文件是顶层数组时，方法应当返回空列表 + 说明，而不是抛 AttributeError。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bad = os.path.join(tmp.name, "probes.json")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write('[{"name": "x"}]')
        with mock.patch.dict(os.environ, {"DASHBOARD_PROBES": bad}):
            collector = Collector()
            targets, error, _mtime = collector.probe_targets()
        self.assertEqual(targets, [])
        self.assertIn("顶层应当是对象", error)


class SamplerResilienceTest(unittest.TestCase):
    """某一项采集失败时，快照仍必须继续发布（否则页面数字会永久停住）。"""

    def test_snapshot_published_even_when_services_fail(self):
        import server
        collector = Collector()
        collector.sample()
        with mock.patch.object(Collector, "services_detail",
                               side_effect=AttributeError("boom")), \
                mock.patch.object(Collector, "network_info",
                                  side_effect=OSError("net boom")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            snapshot = server.sample_once(collector)
        self.assertIsNotNone(snapshot)
        self.assertGreater(snapshot["ts"], 0)
        with server.state_lock:
            self.assertEqual(server.state["snapshot"]["ts"], snapshot["ts"],
                             "快照必须照常发布")
        text = out.getvalue()
        self.assertIn("服务信息失败", text)
        self.assertIn("网络信息失败", text)

    def test_lan_devices_does_not_resolve_dns_in_request_path(self):
        """反向 DNS 只能在后台扫描线程里做：放请求线程会拖住 /api/device 并占着锁。"""
        collector = Collector()
        with mock.patch.object(Collector, "_reverse_dns",
                               side_effect=AssertionError("请求线程里不该解析 DNS")):
            result = collector.lan_devices()
        self.assertIn("hosts", result)

class DiskRateCalculationTest(unittest.TestCase):
    """读写速率用夹具驱动：期望值来自构造的计数差，而不是实现自己的输出。

    历史问题：修 CI 时把断言写成「有 block 就期望有速率」，而 block 正是实现算出来的，
    于是这组断言在 NVMe/MMC 根盘上等于空跑，永远绿。
    """

    @staticmethod
    def _counters(read_bytes, write_bytes):
        return SimpleNamespace(read_bytes=read_bytes, write_bytes=write_bytes)

    def _collector(self):
        collector = Collector.__new__(Collector)
        collector._disk_io_prev = None
        return collector

    def test_rates_are_delta_over_elapsed(self):
        collector = self._collector()
        with mock.patch.object(Collector, "_disk_static", return_value={"block": "sda"}), \
                mock.patch("psutil.disk_io_counters",
                           return_value={"sda": self._counters(1000, 500)}):
            first = collector._disk_io(1000.0)
        self.assertIsNone(first["read_bps"], "第一次只建立基准")
        self.assertEqual(first["read_total_gb"], round(1000 / 1024 ** 3, 2))

        with mock.patch.object(Collector, "_disk_static", return_value={"block": "sda"}), \
                mock.patch("psutil.disk_io_counters",
                           return_value={"sda": self._counters(3000, 1500)}):
            second = collector._disk_io(1002.0)
        self.assertEqual(second["read_bps"], 1000.0, "2 秒读了 2000 字节")
        self.assertEqual(second["write_bps"], 500.0)

    def test_counter_reset_does_not_produce_negative_rate(self):
        collector = self._collector()
        with mock.patch.object(Collector, "_disk_static", return_value={"block": "sda"}), \
                mock.patch("psutil.disk_io_counters",
                           return_value={"sda": self._counters(5000, 5000)}):
            collector._disk_io(1000.0)
        with mock.patch.object(Collector, "_disk_static", return_value={"block": "sda"}), \
                mock.patch("psutil.disk_io_counters",
                           return_value={"sda": self._counters(10, 10)}):
            out = collector._disk_io(1001.0)
        self.assertEqual(out["read_bps"], 0.0, "计数器回绕不该算出负数")

    def test_no_block_device_means_no_rates(self):
        """虚机的根是 /dev/root 或 overlay：认不出块设备时如实为空。"""
        collector = self._collector()
        with mock.patch.object(Collector, "_disk_static", return_value={"block": None}):
            out = collector._disk_io(1000.0)
        for key in ("read_bps", "write_bps", "read_total_gb", "write_total_gb"):
            self.assertIsNone(out[key], key)

    def test_block_missing_from_counters(self):
        collector = self._collector()
        with mock.patch.object(Collector, "_disk_static", return_value={"block": "sdz"}), \
                mock.patch("psutil.disk_io_counters", return_value={}):
            out = collector._disk_io(1000.0)
        self.assertIsNone(out["read_bps"])
        self.assertIsNone(out["write_total_gb"])

class BatteryClampTest(unittest.TestCase):
    """实测：个别固件满电时报出 >100%（本机见过 121.7%），必须钳住而不是照实显示。"""

    def test_percent_is_clamped(self):
        batt = SimpleNamespace(percent=121.7, power_plugged=True, secsleft=-1)
        payload = battery_payload(batt, [])
        self.assertEqual(payload["percent"], 100.0)

        batt = SimpleNamespace(percent=-3.0, power_plugged=False, secsleft=-1)
        self.assertEqual(battery_payload(batt, [])["percent"], 0.0)

    def test_normal_percent_untouched(self):
        batt = SimpleNamespace(percent=94.7, power_plugged=True, secsleft=-1)
        self.assertEqual(battery_payload(batt, [])["percent"], 94.7)

    def test_missing_percent_stays_none(self):
        batt = SimpleNamespace(percent=None, power_plugged=False, secsleft=-1)
        self.assertIsNone(battery_payload(batt, [])["percent"])


class WindowsCompatTest(unittest.TestCase):
    """Windows 上不存在的 psutil 字段 / os 调用，必须在 Linux 上就能测出来。

    实测教训：Windows 的 svmem 没有 buffers/cached/shared，直接访问会把每轮采样
    都打断（日志刷「采样失败：'svmem' object has no attribute 'buffers'」）。
    """

    @staticmethod
    def _svmem_without_linux_fields(total, available, free, used):
        """Windows 的 svmem 只有这些字段（没有 buffers/cached/shared）。"""
        return SimpleNamespace(total=total, available=available, percent=50.0,
                               used=used, free=free)

    def test_memory_on_windows_does_not_touch_linux_only_fields(self):
        """模拟 Windows：svmem 只有 total/available/percent/used/free。"""
        total = 16 * 1024 ** 3
        available = 10 * 1024 ** 3
        free = 6 * 1024 ** 3
        fake = self._svmem_without_linux_fields(total, available, free, total - available)
        collector = Collector.__new__(Collector)
        with mock.patch("collector.IS_WINDOWS", True), \
                mock.patch("psutil.virtual_memory", return_value=fake), \
                mock.patch("psutil.swap_memory", return_value=SimpleNamespace(total=0, used=0)):
            mem = collector._memory()
        self.assertTrue(mem["available"], mem)
        self.assertEqual(mem["total_gb"], 16.0)
        self.assertEqual(mem["used_gb"], 6.0)
        self.assertEqual(mem["free_gb"], 6.0)
        self.assertEqual(mem["buffers_gb"], 0.0, "Windows 没有 buffers，如实给 0")
        self.assertEqual(mem["cached_gb"], 4.0, "缓存用 available - free 近似")
        self.assertEqual(mem["shared_gb"], 0.0)

    def test_memory_on_linux_keeps_sysfs_values(self):
        fake = SimpleNamespace(total=16 * 1024 ** 3, available=10 * 1024 ** 3,
                               percent=50.0, used=6 * 1024 ** 3, free=4 * 1024 ** 3,
                               buffers=512 * 1024 ** 2, cached=3 * 1024 ** 3,
                               shared=256 * 1024 ** 2)
        collector = Collector.__new__(Collector)
        with mock.patch("collector.IS_WINDOWS", False), \
                mock.patch("psutil.virtual_memory", return_value=fake), \
                mock.patch("psutil.swap_memory", return_value=SimpleNamespace(total=0, used=0)):
            mem = collector._memory()
        self.assertEqual(mem["buffers_gb"], 0.5)
        self.assertEqual(mem["cached_gb"], 3.0)
        self.assertEqual(mem["shared_gb"], 0.25)

    def test_load_works_when_os_getloadavg_is_missing(self):
        """Windows 上 os.getloadavg 不存在（AttributeError），要走 psutil。"""
        with mock.patch("psutil.getloadavg", return_value=(1.5, 1.0, 0.5)), \
                mock.patch("os.getloadavg", side_effect=AttributeError("no getloadavg")):
            load = Collector._load()
        self.assertTrue(load["available"])
        self.assertEqual(load["avg1"], 1.5)

    def test_load_degrades_when_both_missing(self):
        with mock.patch("psutil.getloadavg", side_effect=AttributeError), \
                mock.patch("os.getloadavg", side_effect=AttributeError):
            load = Collector._load()
        self.assertFalse(load["available"])
        self.assertIn("负载", load["reason"])

    def test_scope_of(self):
        from collector import scope_of
        self.assertEqual(scope_of("0.0.0.0"), "局域网")
        self.assertEqual(scope_of("::"), "局域网")
        self.assertEqual(scope_of("127.0.0.1"), "仅本机")
        self.assertEqual(scope_of("::1"), "仅本机")
        self.assertEqual(scope_of("192.168.1.10"), "其他")

    def test_listen_sockets_psutil_shape(self):
        """Windows 用 psutil 枚举端口，字段要和 /proc 版一致。"""
        import socket as socket_module
        from collector import listen_sockets_psutil
        conns = [
            SimpleNamespace(status="LISTEN", pid=1, family=socket_module.AF_INET,
                            laddr=SimpleNamespace(ip="0.0.0.0", port=8282)),
            SimpleNamespace(status="LISTEN", pid=1, family=socket_module.AF_INET6,
                            laddr=SimpleNamespace(ip="::1", port=8282)),
            SimpleNamespace(status="ESTABLISHED", pid=1, family=socket_module.AF_INET,
                            laddr=SimpleNamespace(ip="0.0.0.0", port=443)),
            SimpleNamespace(status="LISTEN", pid=None, family=socket_module.AF_INET,
                            laddr=None),
        ]
        with mock.patch("psutil.net_connections", return_value=conns):
            rows = listen_sockets_psutil()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {"port": 8282, "proto": "tcp",
                                   "addr": "0.0.0.0", "scope": "局域网"})
        self.assertEqual(rows[1]["proto"], "tcp6")
        self.assertEqual(rows[1]["scope"], "仅本机")

    def test_listen_sockets_psutil_survives_failure(self):
        from collector import listen_sockets_psutil
        with mock.patch("psutil.net_connections", side_effect=OSError("boom")):
            self.assertEqual(listen_sockets_psutil(), [])

    def test_windows_wireless_names(self):
        self.assertTrue(is_wireless_nic("Wi-Fi"))
        self.assertTrue(is_wireless_nic("WLAN"))
        self.assertTrue(is_wireless_nic("无线网络连接"))
        self.assertTrue(is_wireless_nic("wlan0"))
        self.assertFalse(is_wireless_nic("以太网"))
        self.assertFalse(is_wireless_nic("Ethernet"))
        self.assertEqual(Collector.interface_kind("Wi-Fi"), "无线")
        self.assertEqual(Collector.interface_kind("以太网"), "有线")

