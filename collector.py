"""本机实时指标采集（Python 标准库 + psutil）。

设计原则：任何一项采不到都不抛异常，而是返回 available=False 与原因，
由前端显示为「不可用」。这样在缺传感器、缺权限、缺 docker 的机器上页面依然可用。
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import platform
import re
import socket
import struct
import subprocess
import sys
import threading
import time

import psutil

from runtime import data_dir

# 虚拟网卡前缀：网速默认只统计物理网卡，避免 docker 内部流量污染曲线
VIRTUAL_NIC_PREFIXES = (
    "lo", "docker", "br-", "veth", "virbr", "tun", "tap", "wg", "zt",
    "vmnet", "vboxnet", "snap", "dummy",
)
# Intel RAPL 功耗计数器（energy_uj 默认 root 只读，见 README「功耗与 systemd」）
RAPL_DIR_DEFAULT = "/sys/class/powercap"
# 域名的展示名；psys 是平台级，最接近「整机功耗」
RAPL_LABELS = {
    "psys": "平台功耗",
    "package-0": "CPU 封装",
    "core": "CPU 核心",
    "uncore": "核显与内存控制器",
    "dram": "内存",
}
SERVICES_TTL = 5.0
# RAPL 读取失败后的冷却时间：期间直接返回缓存的原因；
# 过了冷却就再试一次——权限往往是事后才放开的，不该逼着用户重启服务。
RAPL_RETRY_SECONDS = 60.0
GIB = 1024.0 ** 3
MIB = 1024.0 ** 2

# 风扇 key 的中文命名，其余 key 直接展示原始 label
FAN_LABELS = {"cpu_fan": "CPU 风扇", "gpu_fan": "GPU 风扇", "fan1": "机箱风扇"}

# 从 /proc/<pid>/cgroup 识别容器归属：兼容 docker / containerd / podman 的几种写法
CONTAINER_ID_RE = re.compile(
    r"(?:docker-|/docker/|cri-containerd-|/cri-containerd/|libpod-)([0-9a-f]{12,64})"
)

# 端口 -> 常见用途（拿不到进程名时的提示；键是 IANA 惯例，标注为「常见用途」而非事实）
KNOWN_PORTS = {
    22: "SSH", 53: "DNS", 80: "HTTP", 443: "HTTPS", 631: "CUPS 打印",
    2375: "Docker API", 2376: "Docker API (TLS)", 3306: "MySQL", 5432: "PostgreSQL",
    6379: "Redis", 8123: "Home Assistant", 8282: "本总控台", 11434: "Ollama",
    27017: "MongoDB",
}
# 远程探测：网关/外网每 10 秒一次，probes.json 里的目标每 30 秒一次（别太频繁打人家）
NET_PROBE_INTERVAL = 10.0
PROBE_TARGET_INTERVAL = 30.0
PROBE_TARGET_TIMEOUT = 1.5
# systemd 服务列表的缓存时间
SYSTEMD_TTL = 10.0
# 设备信息（基本不变）的缓存时间；里面要起一次 docker --version
DEVICE_TTL = 60.0
# 局域网扫描：整段 /24 并发 ping 约 10 秒，放后台线程每 2 分钟跑一次
LAN_SWEEP_INTERVAL = 120.0
LAN_MAX_HOSTS = 256
LAN_PING_WORKERS = 32
LAN_SSDP_WINDOW = 2.5
# Windows 平台走 platform_win（psutil + PowerShell）；Linux 保持原有实现
IS_WINDOWS = sys.platform.startswith("win")
if IS_WINDOWS:                                  # pragma: no cover - 仅在 Windows 导入
    import platform_win as win

# IEEE OUI 厂商库（发行版可能放在这几个位置）
OUI_PATHS = ("/usr/share/ieee-data/oui.txt", "/var/lib/ieee-data/oui.txt",
             "/usr/share/misc/oui.txt")
_oui_cache = None

# 常见代理端口（「本地代理」一行用；8080 太通用，刻意不算）
PROXY_PORTS = (7890, 7891, 1080, 1081, 8118, 3128, 8889, 7897)
# 延迟探测：网关试这几个端口取最快的一个；外网目标可用环境变量改
GATEWAY_PROBE_PORTS = (53, 22, 443, 80)
NET_PROBE_TARGET = os.environ.get("DASHBOARD_NET_TARGET", "www.baidu.com:443")


def subnet_candidates(ip, netmask, cap=LAN_MAX_HOSTS):
    """本网段内可扫描的地址：跳过网络地址、广播地址与本机，最多 cap 个。

    网段比 /24 大时只扫本机所在的 /24，避免一次扫几万个地址。
    """
    try:
        ip_parts = [int(part) for part in str(ip).split(".")]
        mask_parts = [int(part) for part in str(netmask).split(".")]
    except (AttributeError, ValueError):
        return []
    if len(ip_parts) != 4 or len(mask_parts) != 4:
        return []

    def pack(parts):
        value = 0
        for part in parts:
            value = (value << 8) | part
        return value

    ip_value, mask_value = pack(ip_parts), pack(mask_parts)
    network = ip_value & mask_value
    broadcast = network | (~mask_value & 0xFFFFFFFF)
    if broadcast - network - 1 > cap:
        mask_value = 0xFFFFFF00
        network = ip_value & mask_value
        broadcast = network | 0xFF
    hosts = []
    for value in range(network + 1, broadcast):
        if value == ip_value:
            continue
        hosts.append(".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0)))
        if len(hosts) >= cap:
            break
    return hosts


def subnet_label(ip, netmask):
    """网段的展示写法，例如 192.168.1.0/24。"""
    try:
        parts = [int(part) for part in str(ip).split(".")]
        mask = [int(part) for part in str(netmask).split(".")]
    except (AttributeError, ValueError):
        return None
    if len(parts) != 4 or len(mask) != 4:
        return None
    network = [parts[i] & mask[i] for i in range(4)]
    prefix = sum(bin(part).count("1") for part in mask)
    return ".".join(str(part) for part in network) + f"/{prefix}"


def parse_arp_table(text):
    """解析 /proc/net/arp：{ip: mac}，跳过全零条目。"""
    rows = {}
    for line in (text or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        ip, mac = fields[0], fields[3]
        if mac and mac != "00:00:00:00:00:00":
            rows[ip] = mac.lower()
    return rows


def parse_ssdp_response(text):
    """从 SSDP 响应里取 SERVER / LOCATION / USN / ST。"""
    info = {}
    for line in (text or "").splitlines()[1:]:
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in ("server", "location", "usn", "st") and value.strip():
            info[key] = value.strip()
    return info or None


def load_oui():
    """IEEE OUI 库：MAC 前缀 -> 厂商名。读一次缓存；机器上没有库就返回空表。"""
    global _oui_cache
    if _oui_cache is not None:
        return _oui_cache
    table = {}
    for path in OUI_PATHS:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "(hex)" not in line:
                        continue
                    prefix, _, vendor = line.partition("(hex)")
                    key = prefix.strip().replace("-", ":").upper()
                    if len(key) == 8 and ":" in key and vendor.strip():
                        table.setdefault(key, vendor.strip())
        except OSError:
            continue
        if table:
            break
    _oui_cache = table
    return table


def oui_vendor(mac, table=None):
    """MAC -> 厂商名；查不到（或库缺失）返回 None。"""
    if not mac or mac == "00:00:00:00:00:00":
        return None
    table = load_oui() if table is None else table
    return table.get(mac.upper()[:8])


def parse_probe_targets(data):
    """校验 probes.json 的内容，返回 (targets, problems)。

    顶层必须是对象、probes 必须是数组、每个元素必须是有 host/port 的对象；
    任何不合规都只记录问题并跳过，绝不抛异常——否则会连带打死探测线程与整站采样。
    """
    problems = []
    if not isinstance(data, dict):
        return [], [f"probes.json 顶层应当是对象，实际是 {type(data).__name__}"]
    raw = data.get("probes")
    if raw is None:
        return [], ["probes.json 里没有 probes 数组"]
    if not isinstance(raw, list):
        return [], [f"probes 应当是数组，实际是 {type(raw).__name__}"]
    targets = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"第 {index + 1} 项不是对象，已跳过")
            continue
        host, port = item.get("host"), item.get("port")
        if not isinstance(host, str) or not host.strip():
            problems.append(f"第 {index + 1} 项缺少可用的 host，已跳过")
            continue
        try:
            port_number = int(port)
        except (TypeError, ValueError):
            problems.append(f"第 {index + 1} 项的 port 不是数字，已跳过")
            continue
        if not 1 <= port_number <= 65535:
            problems.append(f"第 {index + 1} 项的 port 超出范围，已跳过")
            continue
        name = item.get("name")
        targets.append({"name": name if isinstance(name, str) and name.strip() else host.strip(),
                        "host": host.strip(), "port": port_number})
    return targets, problems


def parse_os_release(text):
    """把 /etc/os-release 解析成字典（去掉引号）。"""
    info = {}
    for line in (text or "").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        info[key.strip()] = value.strip().strip('"')
    return info


def parse_cpu_model(text):
    """从 /proc/cpuinfo 取 CPU 型号。"""
    for line in (text or "").splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


def parse_cpu_flags(text):
    """从 /proc/cpuinfo 取 flags 行，返回集合。"""
    for line in (text or "").splitlines():
        low = line.lower()
        if low.startswith("flags") or low.startswith("features"):
            return set(line.split(":", 1)[1].split())
    return set()


def virtualization_label(flags):
    """虚拟化能力：Intel 看 vmx，AMD 看 svm。"""
    if "vmx" in flags:
        return "VT-x"
    if "svm" in flags:
        return "AMD-V"
    return None


def format_khz(khz):
    """kHz 文本 -> MHz 整数。"""
    try:
        return round(int(khz) / 1000)
    except (TypeError, ValueError):
        return None


def probes_path():
    """远程探测目标配置文件（可用 DASHBOARD_PROBES 指向别处）。

    默认在可写数据目录（打包成 exe 后是 exe 所在目录，见 runtime.data_dir）。
    """
    return os.environ.get("DASHBOARD_PROBES") or os.path.join(data_dir(), "probes.json")


def format_sockaddr(addr_hex, is_v6):
    """把 /proc/net/tcp{,6} 里的十六进制地址还原成可读 IP。"""
    try:
        if is_v6:
            return socket.inet_ntop(socket.AF_INET6, bytes.fromhex(addr_hex))
        return socket.inet_ntoa(struct.pack("<I", int(addr_hex, 16)))
    except (ValueError, OSError):
        return addr_hex


def scope_of(addr):
    """绑定地址 -> 访问范围（页面上的「谁能访问」）。"""
    if addr in ("0.0.0.0", "::"):
        return "局域网"
    if addr.startswith("127.") or addr in ("::1", "0:0:0:0:0:0:0:1"):
        return "仅本机"
    return "其他"


def listen_sockets_psutil():
    """用 psutil 枚举监听端口（Windows 没有 /proc/net/tcp）。

    字段与 /proc 版保持一致：port / proto / addr / scope。
    """
    rows = []
    try:
        connections = psutil.net_connections(kind="inet")
    except Exception:
        return rows
    seen = set()
    for conn in connections:
        if conn.status != "LISTEN" or not conn.laddr:
            continue
        addr = conn.laddr.ip or "0.0.0.0"
        proto = "tcp6" if conn.family == socket.AF_INET6 else "tcp"
        key = (conn.laddr.port, proto, addr)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"port": conn.laddr.port, "proto": proto,
                     "addr": addr, "scope": scope_of(addr)})
    rows.sort(key=lambda item: item["port"])
    return rows


def listen_sockets():
    """监听中的 TCP 端口，附带绑定地址与访问范围。

    「仅本机」= 绑在 127.x / ::1，只有本机能连；「局域网」= 绑在 0.0.0.0 / ::，
    同网段的机器都能连（参考图里的「谁能访问」一列）。
    """
    if IS_WINDOWS:
        return listen_sockets_psutil()
    rows = []
    for path, is_v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            with open(path, "r") as handle:
                next(handle, None)
                for line in handle:
                    fields = line.split()
                    if len(fields) < 4 or fields[3] != "0A":   # 0A = LISTEN
                        continue
                    try:
                        addr_hex, port_hex = fields[1].rsplit(":", 1)
                        port = int(port_hex, 16)
                    except (IndexError, ValueError):
                        continue
                    addr = format_sockaddr(addr_hex, is_v6)
                    rows.append({"port": port, "proto": "tcp6" if is_v6 else "tcp",
                                 "addr": addr, "scope": scope_of(addr)})
        except OSError:
            continue
    rows.sort(key=lambda item: item["port"])
    return rows


def is_wireless_nic(name):
    """无线网卡识别：Linux 是 wl* 命名；Windows 的适配器名是「Wi-Fi / WLAN / 无线」。"""
    low = (name or "").lower()
    if low.startswith("wl"):
        return True
    return bool(re.search(r"(?i)wi-?fi|wlan|wireless|无线", name or ""))


def parse_default_gateway(text):
    """从 /proc/net/route 取默认网关（Destination 0.0.0.0 那行，网关是十六进制小端）。"""
    for line in (text or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            try:
                raw = int(fields[2], 16)
            except ValueError:
                continue
            return ".".join(str((raw >> shift) & 0xFF) for shift in (0, 8, 16, 24))
    return None


def container_id_from_cgroup(text):
    """从 cgroup 内容里取出容器 ID；不是容器进程就返回 None。"""
    for line in (text or "").splitlines():
        match = CONTAINER_ID_RE.search(line)
        if match:
            return match.group(1)
    return None


def rapl_dir():
    """RAPL 根目录。环境变量可覆盖，便于测试时指向夹具目录。"""
    return os.environ.get("DASHBOARD_RAPL_DIR", RAPL_DIR_DEFAULT)


def is_virtual_nic(name):
    low = name.lower()
    return any(low == prefix or low.startswith(prefix) for prefix in VIRTUAL_NIC_PREFIXES)


def pick_nic():
    """挑一个物理网卡：环境变量指定 > 有 IPv4 的非虚拟网卡 > 任意非虚拟网卡。"""
    forced = os.environ.get("DASHBOARD_NIC")
    if forced:
        return forced
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    with_v4, physical = [], []
    for name, stat in stats.items():
        if not stat.isup or is_virtual_nic(name):
            continue
        physical.append(name)
        if any(a.family == socket.AF_INET for a in addrs.get(name, ())):
            with_v4.append(name)
    candidates = sorted(with_v4 or physical)
    return candidates[0] if candidates else None


def listen_ports():
    """监听中的端口集合：Linux 读 /proc/net/tcp{,6}，Windows 用 psutil。"""
    if IS_WINDOWS:
        return sorted({row["port"] for row in listen_sockets_psutil()})
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r") as handle:
                next(handle, None)
                for line in handle:
                    fields = line.split()
                    if len(fields) < 4 or fields[3] != "0A":  # 0A = LISTEN
                        continue
                    try:
                        ports.add(int(fields[1].rsplit(":", 1)[1], 16))
                    except (IndexError, ValueError):
                        continue
        except OSError:
            continue
    return sorted(ports)


def temp_entry_key(chip, label):
    """把 hwmon 的 chip/label 归一化成稳定通道 key 与中文展示名。

    key 会成为 /api/series 里的曲线键（temp_acpi 等），必须稳定；
    展示名给前端直接显示。
    """
    chip = (chip or "").lower()
    lab = (label or "").strip()
    low = lab.lower()
    if chip == "coretemp":
        if "package" in low:
            return "package", "CPU 封装"
        num = "".join(ch for ch in low if ch.isdigit())
        return (f"core{num or 'x'}", f"CPU 核心 {num}" if num else "CPU 核心")
    if chip.startswith("pch"):
        return "pch", "芯片组 PCH"
    if chip in ("acpitz", "acpi"):
        return "acpi", "机身温区"
    if chip in ("k10temp", "k8temp", "cpu_thermal"):
        return "package", "CPU 封装"
    slug = re.sub(r"[^a-z0-9]+", "_", low or chip).strip("_")[:24] or "misc"
    return slug, lab or chip


def battery_payload(batt, supplies):
    """把 psutil 电池对象与 /sys/class/power_supply 明细合成降级友好的一份载荷。

    supplies 形如 [{"status": "Not charging", "cycles": "239", "power_uw": "0"}]。
    放电功率（power_uw > 0）只有真正放电时才有意义，接通电源时保持 None。
    """
    if batt is None:
        return {"available": False, "reason": "本机没有电池"}
    plugged = bool(batt.power_plugged)
    status = None
    cycles = None
    power_w = None
    for item in supplies:
        status = status or item.get("status")
        raw = item.get("cycles")
        if cycles is None and raw and raw.isdigit():
            cycles = int(raw)
        raw = item.get("power_uw")
        if power_w is None and raw and raw.isdigit() and int(raw) > 0:
            power_w = round(int(raw) / 1e6, 2)
    secsleft = None
    if not plugged and isinstance(batt.secsleft, int) and 0 < batt.secsleft < 86400 * 30:
        secsleft = int(batt.secsleft)
    # 个别固件在满电附近会给出 >100%（energy_now 略高于 energy_full，实测见过 121.7%），
    # 页面上显示 121.7% 只会让人困惑，这里钳到 0–100。
    percent = None
    if batt.percent is not None:
        percent = round(max(0.0, min(100.0, float(batt.percent))), 1)
    return {"available": True, "percent": percent, "plugged": plugged,
            "status": status, "cycles": cycles, "power_w": power_w, "secsleft": secsleft}


class Collector:
    """采样本机指标。进程 CPU、网速、功耗都靠两次采样求差分。"""

    def __init__(self):
        default_disk = (os.environ.get("SystemDrive", "C:") + "\\") if IS_WINDOWS else "/"
        self.disk_path = os.environ.get("DASHBOARD_DISK", default_disk)
        self.nic = pick_nic()
        self.cores = psutil.cpu_count(logical=True) or 1
        self.boot_time = psutil.boot_time()
        self._net_prev = None
        self._rapl_prev = {}
        self._rapl_note = None
        self._rapl_note_at = 0.0
        self._rapl_domains = None
        self._procs = {}
        self._proc_static = {}
        self._container_names = {}
        self._services = None
        self._services_at = 0.0
        self._services_lock = threading.Lock()
        self._container_note = ""
        self._container_total = 0
        self._container_running = 0
        self._gpu_card = self._find_gpu_card()
        self._core_topology = self._read_core_topology()
        self._disk_io_prev = None
        self._disk_static_cache = None
        self._port_procs = None
        self._port_procs_at = 0.0
        self._port_procs_lock = threading.Lock()
        self._systemd = None
        self._systemd_at = 0.0
        self._systemd_lock = threading.Lock()
        self._probes_cache = ([], None, None)
        self._device = None
        self._device_at = 0.0
        self._device_lock = threading.Lock()
        self._lan_cache = ({}, 0.0)
        self._gateway = self._read_gateway()
        self._probe = {"gateway_ms": None, "internet_ms": None,
                       "internet_target": NET_PROBE_TARGET, "at": 0.0}
        psutil.cpu_percent(interval=None)  # 预热，让首次采样就有意义
        self._prime_processes()

    # ---------------- 静态探测（进程启动时做一次） ----------------

    @staticmethod
    def _find_gpu_card():
        """找第一个暴露 gt_cur_freq_mhz 的显卡（Intel 核显的 i915/xe 接口）。"""
        base = "/sys/class/drm"
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return None
        for name in names:
            if re.fullmatch(r"card\d+", name) and os.path.exists(
                os.path.join(base, name, "gt_cur_freq_mhz")
            ):
                return name
        return None

    def _read_core_topology(self):
        """逻辑核序号 -> 物理核心号（读不到为 -1），用于每核占用的展示口径。"""
        if IS_WINDOWS:                          # Windows 拿不到拓扑，前端退化成「线程 N」
            return [-1] * self.cores
        topology = []
        for i in range(self.cores):
            raw = self._read_sys(f"/sys/devices/system/cpu/cpu{i}/topology/core_id")
            topology.append(int(raw) if raw and raw.lstrip("-").isdigit() else -1)
        return topology

    @staticmethod
    def _read_sys(path):
        try:
            with open(path, "r") as handle:
                return handle.read().strip()
        except OSError:
            return None

    # ---------------- 进程 ----------------

    def _prime_processes(self):
        for proc in psutil.process_iter(["pid"]):
            try:
                proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            self._procs[proc.pid] = proc

    def processes(self, limit=6):
        """CPU 占用最高的若干进程（概览页用）。"""
        return self.process_list()[:limit]

    def process_list(self):
        """全部进程。

        CPU 值按逻辑核心数归一化到 0-100，口径贴近 macOS 活动监视器；
        `top`/`ps` 显示的原始值最高可达 100 x 核心数，顺序一致、数值约为其 1/核心数。

        静态字段（用户、命令行、容器归属）按 pid 缓存，只有首次见到该 pid 才读
        `/proc/<pid>/{cmdline,cgroup}`；pid 复用靠比对启动时间来识别。
        """
        rows = []
        for proc in list(self._procs.values()):
            try:
                with proc.oneshot():
                    cpu = proc.cpu_percent(interval=None) / self.cores
                    rss = proc.memory_info().rss
                    name = proc.name()
                    status = proc.status()
                    threads = proc.num_threads()
                    create_time = proc.create_time()
                    pid = proc.pid
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._procs.pop(proc.pid, None)
                self._proc_static.pop(proc.pid, None)
                continue
            static = self._process_static(proc, create_time)
            rows.append({
                "pid": pid,
                "name": name,
                "user": static["user"],
                "cpu": round(cpu, 1),
                "rss_mb": round(rss / MIB, 1),
                "status": status,
                "threads": threads,
                "started": round(create_time),
                "cmd": static["cmd"] or name,
                "container": self._container_of(static["container_id"]),
            })

        self._prime_processes()  # 补进新出现的进程并预热，下次采样才有值
        for pid in list(self._proc_static):
            if pid not in self._procs:
                self._proc_static.pop(pid, None)

        rows.sort(key=lambda row: (row["cpu"], row["rss_mb"]), reverse=True)
        return rows

    def _process_static(self, proc, create_time):
        """进程的不变信息：用户、命令行、容器归属。"""
        pid = proc.pid
        cached = self._proc_static.get(pid)
        if cached and cached["create_time"] == create_time:
            return cached
        info = {"create_time": create_time, "user": None, "cmd": "", "container_id": None}
        try:
            info["user"] = proc.username()
        except (psutil.AccessDenied, psutil.NoSuchProcess, KeyError):
            info["user"] = None
        name = proc.name() or ""
        try:
            parts = proc.cmdline()
            cmd = " ".join(parts)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            cmd = ""              # 非 root 读不到别人的命令行，前端显示「无权限」
        if len(cmd) > 400:
            cmd = cmd[:400]       # 极少数超长命令行，截断以控制轮询载荷
        info["cmd"] = "" if cmd == name else cmd   # 与进程名相同就不重复发
        info["container_id"] = self._process_container_id(pid)
        self._proc_static[pid] = info
        return info

    def _process_container_id(self, pid):
        """Windows 没有 cgroup 文件；容器归属留空由前端显示「—」。"""
        if IS_WINDOWS:
            return None
        """进程是否跑在容器里：读 cgroup 拿容器 ID（只缓存 ID，名字每次实时映射）。"""
        raw = self._read_sys(f"/proc/{pid}/cgroup")
        container_id = container_id_from_cgroup(raw)
        return container_id[:12] if container_id else None

    def _container_of(self, container_id):
        """容器 ID -> 展示用信息。名字来自 docker ps（5 秒缓存），查不到就只给 ID。"""
        if not container_id:
            return None
        return {"id": container_id, "name": self._container_names.get(container_id)}

    def process_count(self):
        return len(self._procs)

    # ---------------- 指标 ----------------

    def sample(self):
        now = time.time()
        cpu = self._cpu()
        cpu["per_core"] = self._percpu()
        memory = self._memory()
        temps = self._all_temps()
        load = self._load()
        power = self._power(now)
        return {
            "ts": now,
            "host": socket.gethostname(),
            "cores": self.cores,
            "uptime_s": max(0.0, now - self.boot_time),
            "cpu": cpu,
            "memory": memory,
            "power": power,
            "net": self._net(now),
            "disk": self._disk(now),
            "temp": self._temp_from(temps),
            "load": load,
            # 性能与电源页的数据段：与概览共享同一批采样结果，不重复读内核
            "performance": {
                "cpu": cpu,
                "gpu": self._gpu_freq(),
                "temps": temps,
                "fans": self._fans(),
                "battery": self._battery(),
                "memory": memory,
                "load": load,
                "power": power,
            },
        }

    def _cpu(self):
        try:
            percent = psutil.cpu_percent(interval=None)
        except Exception as exc:  # pragma: no cover - 平台差异
            return {"available": False, "reason": f"CPU 采样失败：{exc}"}
        freq_mhz = None
        try:
            freq = psutil.cpu_freq()
            if freq and freq.current:
                freq_mhz = round(freq.current)
        except Exception:
            freq_mhz = None
        return {"available": True, "percent": round(percent, 1), "freq_mhz": freq_mhz}

    def _memory(self):
        try:
            vm = psutil.virtual_memory()
            total = vm.total / GIB
            used = (vm.total - vm.available) / GIB
            swap = psutil.swap_memory()
        except Exception as exc:
            return {"available": False, "reason": f"内存采样失败：{exc}"}
        # Windows 的 svmem 没有 buffers/cached/shared（那是 Linux 专有字段，直接访问会抛
        # AttributeError 并把整轮采样打断）。这里按平台取字段：
        #   「缓存」用 available - free 近似（Windows 的 standby 列表，语义接近 Linux 的 cached），
        #    buffers/shared 在 Windows 上没有对应概念，如实给 0。
        if IS_WINDOWS:
            free_gb = vm.free / GIB
            cached_gb = max(0.0, (vm.available / GIB) - free_gb)
            extra = {"buffers_gb": 0.0, "cached_gb": round(cached_gb, 2), "shared_gb": 0.0}
        else:
            extra = {"buffers_gb": round(vm.buffers / GIB, 2),
                     "cached_gb": round(vm.cached / GIB, 2),
                     "shared_gb": round(vm.shared / GIB, 2)}
        payload = {
            "available": True,
            "used_gb": round(used, 1),
            "total_gb": round(total, 1),
            "percent": round(used / total * 100, 1) if total else 0.0,
            "free_gb": round(vm.free / GIB, 1),
            "swap_total_gb": round(swap.total / GIB, 1),
            "swap_used_gb": round(swap.used / GIB, 1),
        }
        payload.update(extra)
        return payload

    def _rapl_paths(self):
        """枚举 powercap 下的 RAPL 域。

        同一个计数器可能同时暴露 MSR 与 MMIO 两个接口（intel-rapl:* 与 intel-rapl-mmio:*），
        名字相同、数值一样，按名字去重并优先 MSR。
        """
        if self._rapl_domains is not None:
            return self._rapl_domains
        found = {}
        try:
            entries = sorted(os.listdir(rapl_dir()))
        except OSError:
            entries = []
        for entry in entries:
            base = os.path.join(rapl_dir(), entry)
            energy = os.path.join(base, "energy_uj")
            try:
                if not os.path.exists(energy):
                    continue
            except OSError:
                continue
            name = self._read_sys(os.path.join(base, "name")) or entry
            previous = found.get(name)
            is_mmio = "mmio" in entry
            if previous is None or ("mmio" in previous[0] and not is_mmio):
                found[name] = (entry, energy)
        self._rapl_domains = [(name, path) for name, (_, path) in sorted(found.items())]
        return self._rapl_domains

    def _rapl_fail(self, now, reason):
        """记住失败原因（冷却期内不重复读 sysfs），保证返回结构一致。"""
        self._rapl_note = reason
        self._rapl_note_at = now
        return {"available": False, "reason": reason}

    def _power(self, now):
        """功耗：逐域读取 RAPL 累计能量做差分。

        psys（平台）优先作为主值，退化到 package-0；同时给出各域明细，
        供性能页画参考图那样的功耗构成。
        """
        if self._rapl_note:
            if now - self._rapl_note_at < RAPL_RETRY_SECONDS:
                return {"available": False, "reason": self._rapl_note}
            self._rapl_note = None      # 冷却结束，重试（权限可能是后来才放开的）
            self._rapl_domains = None   # 域目录内容也可能变了，重新枚举
        domains = self._rapl_paths()
        if not domains:
            return self._rapl_fail(now, "本机没有 Intel RAPL 功耗计数器")

        readings = {}
        skipped = []
        first_error = None
        for name, path in domains:
            try:
                with open(path, "r") as handle:
                    readings[name] = int(handle.read().strip())
            except PermissionError:
                skipped.append(name)
                first_error = first_error or ("读取 RAPL 需要权限：以 root 运行，"
                                              "或用 deploy 里的 udev 规则放开 energy_uj 读权限")
            except FileNotFoundError:
                skipped.append(name)
                first_error = first_error or "RAPL 计数器不可读"
            except (OSError, ValueError) as exc:
                skipped.append(name)
                first_error = first_error or f"RAPL 读取失败：{exc}"

        if not readings:
            # 一个域都读不到：给出统一原因（权限不足最常见）
            return self._rapl_fail(now, first_error or "RAPL 计数器不可读")

        watts = {}
        for name, energy in readings.items():
            prev = self._rapl_prev.get(name)
            if prev is None:
                continue
            delta_t = now - prev[0]
            delta_e = energy - prev[1]
            if delta_t > 0 and delta_e >= 0:  # 计数器溢出时跳过该点
                watts[name] = round(delta_e / 1e6 / delta_t, 2)
        self._rapl_prev = {name: (now, energy) for name, energy in readings.items()}

        if not watts:
            return {"available": False, "reason": "正在预热功耗采样"}

        # 主值：psys 只有「不小于封装」时才当作平台功耗，否则用可靠的 package-0。
        # 依据是实测：本机满载时封装 12.5 W，而 psys 只报 3.5 W——平台功耗不可能小于封装，
        # 说明该平台的 PSYS 域没正确实现。（空闲域读数为 0.00 W 是真实测量值，不作特殊标注。）
        psys_sane = "psys" in watts and (
            "package-0" not in watts or watts["psys"] >= watts["package-0"])
        if psys_sane:
            primary = "psys"
        elif "package-0" in watts:
            primary = "package-0"
        else:
            primary = max(watts, key=lambda key: watts[key])

        detail = []
        for name in sorted(watts, key=lambda key: (key != primary, key)):
            entry = {"name": name, "label": RAPL_LABELS.get(name, name), "watts": watts[name]}
            if name == "psys" and not psys_sane:
                entry["suspect"] = "该平台 PSYS 未实现"
            detail.append(entry)
        return {
            "available": True,
            "watts": watts[primary],
            "source": RAPL_LABELS.get(primary, primary),
            "domains": detail,
            # 读不到的域（例如只放开了顶层权限，子域仍是 0400）
            "skipped": skipped,
        }

    def _net(self, now):
        if not self.nic:
            return {"available": False, "reason": "没有找到可用的物理网卡"}
        try:
            counters = psutil.net_io_counters(pernic=True).get(self.nic)
        except Exception as exc:
            return {"available": False, "reason": f"网速采样失败：{exc}"}
        if counters is None:
            return {"available": False, "reason": f"网卡 {self.nic} 不存在"}

        down = up = None
        if self._net_prev is not None:
            prev_ts, prev_sent, prev_recv = self._net_prev
            delta_t = now - prev_ts
            if delta_t > 0:
                down = max(0.0, (counters.bytes_recv - prev_recv) / delta_t)
                up = max(0.0, (counters.bytes_sent - prev_sent) / delta_t)
        self._net_prev = (now, counters.bytes_sent, counters.bytes_recv)
        if down is None:
            return {"available": False, "reason": "正在预热网速采样", "nic": self.nic}
        return {"available": True, "nic": self.nic,
                "down_bps": round(down, 1), "up_bps": round(up, 1)}

    def _disk(self, now):
        try:
            usage = psutil.disk_usage(self.disk_path)
        except Exception as exc:
            return {"available": False, "reason": f"磁盘采样失败：{exc}", "path": self.disk_path}
        return {
            "available": True,
            "path": self.disk_path,
            "free_gb": round(usage.free / GIB, 1),
            "total_gb": round(usage.total / GIB, 1),
            "used_percent": round(usage.percent, 1),
            "mounts": self._mounts(),
            **self._disk_static(),
            **self._disk_io(now),
        }

    def _temp(self):
        """概览用的单值温度：从全部通道里挑封装温度，退化到第一个通道。"""
        return self._temp_from(self._all_temps())
    def _all_temps(self):
        """全部温度通道。性能页展示列表，概览的单值温度也从这里挑，避免重复读 sysfs。"""
        if IS_WINDOWS:
            return win.temps()
        try:
            sensors = psutil.sensors_temperatures() or {}
        except Exception as exc:
            return {"available": False, "reason": f"温度采样失败：{exc}"}
        entries = []
        for chip, items in sensors.items():
            for item in items:
                if not item.current:
                    continue
                key, label = temp_entry_key(chip, item.label)
                entries.append({"key": key, "label": label,
                                "celsius": round(item.current, 1)})
        if not entries:
            return {"available": False, "reason": "本机没有可读的温度传感器"}
        unique = []
        seen = set()
        for entry in sorted(entries, key=lambda item: item["key"]):
            if entry["key"] in seen:  # 同名通道只留一个
                continue
            seen.add(entry["key"])
            unique.append(entry)
        return {"available": True, "list": unique}

    @staticmethod
    def _temp_from(temps):
        if not temps.get("available"):
            return {"available": False, "reason": temps.get("reason") or "温度不可用"}
        entries = temps["list"]
        for entry in entries:
            if entry["key"] == "package":
                return {"available": True, "celsius": entry["celsius"],
                        "source": entry["label"]}
        entry = entries[0]
        return {"available": True, "celsius": entry["celsius"], "source": entry["label"]}

    def _percpu(self):
        """每逻辑核占用与频率。percpu 与 _cpu() 的总量在 psutil 里各自独立记差分。"""
        try:
            percents = psutil.cpu_percent(interval=None, percpu=True)
            freqs = psutil.cpu_freq(percpu=True)
        except Exception as exc:
            return {"available": False, "reason": f"每核采样失败：{exc}"}
        if not percents:
            return {"available": False, "reason": "每核采样没有返回数据"}
        freq_list = None
        try:
            values = [round(item.current) for item in (freqs or []) if item and item.current]
            if len(values) == len(percents):
                freq_list = values
        except Exception:
            freq_list = None
        return {"available": True,
                "per_cpu": [round(p, 1) for p in percents],
                "freq_mhz": freq_list,
                "topology": self._core_topology}

    def _gpu_freq(self):
        """核显当前/最大频率（gt_cur_freq_mhz）。利用率无标准接口，如实显示不可用。"""
        if not self._gpu_card:
            return {"available": False, "reason": "本机没有可读的 GPU 频率接口"}
        base = f"/sys/class/drm/{self._gpu_card}"
        raw = self._read_sys(f"{base}/gt_cur_freq_mhz")
        if not raw or not raw.isdigit():
            return {"available": False, "reason": "GPU 频率读取失败"}
        top = self._read_sys(f"{base}/gt_max_freq_mhz")
        return {"available": True, "card": self._gpu_card, "freq_mhz": int(raw),
                "max_mhz": int(top) if top and top.isdigit() else None}

    def _fans(self):
        """全部风扇转速。读数为 0 的（如停转的 gpu_fan）也如实保留。"""
        if IS_WINDOWS:
            return win.fans()
        try:
            chips = psutil.sensors_fans() or {}
        except Exception as exc:
            return {"available": False, "reason": f"风扇采样失败：{exc}"}
        fans = []
        for chip, items in chips.items():
            for item in items:
                key = (item.label or chip).strip().lower().replace(" ", "_")[:24]
                fans.append({"key": key, "label": FAN_LABELS.get(key, item.label or chip),
                             "rpm": item.current})
        if not fans:
            return {"available": False, "reason": "本机没有可读的风扇转速"}
        return {"available": True, "list": fans}

    def _battery(self):
        """电池状态：psutil 提供电量与接通状态，循环次数/放电功率来自 sysfs。"""
        try:
            batt = psutil.sensors_battery()
        except Exception:
            batt = None
        return battery_payload(batt, self._power_supplies())

    def _power_supplies(self):
        if IS_WINDOWS:                          # 电池走 psutil.sensors_battery，无 sysfs 可读
            return []
        supplies = []
        try:
            names = sorted(os.listdir("/sys/class/power_supply"))
        except OSError:
            return supplies
        for name in names:
            path = os.path.join("/sys/class/power_supply", name)
            if self._read_sys(os.path.join(path, "type")) != "Battery":
                continue
            supplies.append({
                "status": self._read_sys(os.path.join(path, "status")),
                "cycles": self._read_sys(os.path.join(path, "cycle_count")),
                "power_uw": self._read_sys(os.path.join(path, "power_now")),
            })
        return supplies

    @staticmethod
    def _load():
        # 优先用 psutil：Windows 上 os.getloadavg() 根本不存在（AttributeError），
        # 而 psutil.getloadavg() 在 Windows 上会自己模拟（>= 5.6.2）。
        try:
            avg1, avg5, avg15 = psutil.getloadavg()
        except (AttributeError, OSError):
            try:
                avg1, avg5, avg15 = os.getloadavg()
            except (AttributeError, OSError):
                return {"available": False, "reason": "本机不支持负载查询"}
        return {"available": True, "avg1": round(avg1, 2),
                "avg5": round(avg5, 2), "avg15": round(avg15, 2)}

    # ---------------- 网络与磁盘 ----------------

    def _read_gateway(self):
        """默认网关：Linux 读 /proc/net/route 免 root；Windows 用 route print。"""
        if IS_WINDOWS:
            return win.gateway_address()
        return parse_default_gateway(self._read_sys("/proc/net/route"))

    def _neighbors(self):
        """邻居表（ARP）：Linux 读 /proc/net/arp，Windows 用 arp -a。"""
        if IS_WINDOWS:
            return win.arp_table()
        return parse_arp_table(self._read_sys("/proc/net/arp"))

    def _ping_host(self, ip):
        """单次 ICMP：两个平台的 ping 参数不同（Linux -c/-W，Windows -n/-w）。"""
        if IS_WINDOWS:
            return win.ping(ip)
        return self._ping(ip)

    @staticmethod
    def tcp_latency(host, port, timeout=0.6):
        """TCP 握手耗时（毫秒）。没有 ICMP 权限，用 TCP 连接近似往返延迟。"""
        if not host:
            return None
        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return round((time.perf_counter() - started) * 1000, 1)
        except OSError:
            return None

    def probe_loop(self):
        """后台线程：定期测网关、外网与 probes.json 里配置的远程目标。

        放在独立线程里，避免连接超时拖慢 1 秒采样；页面读的是最近一次结果。
        网关/外网每 10 秒一次；配置的远程目标每 30 秒一次（别太频繁打人家）。
        """
        targets_at = 0.0
        target_results = []
        while True:
            started = time.time()
            try:
                self._probe_once(started, targets_at, target_results)
                targets_at = self._probe_at
                target_results = self._probe.get("targets") or []
            except Exception as exc:      # 探测失败只记一行，线程继续跑
                print(f"[probe] 探测失败：{exc}", flush=True)
            time.sleep(max(1.0, NET_PROBE_INTERVAL - (time.time() - started)))

    def _probe_once(self, started, targets_at, target_results):
        """跑一轮探测并写入 self._probe（拆出来便于兜底与测试）。"""
        if True:
            gateway_ms = None
            if self._gateway:
                for port in GATEWAY_PROBE_PORTS:
                    value = self.tcp_latency(self._gateway, port)
                    if value is not None:
                        gateway_ms = value if gateway_ms is None else min(gateway_ms, value)
            host, _, port_text = NET_PROBE_TARGET.rpartition(":")
            try:
                port = int(port_text or "443")
            except ValueError:
                host, port = NET_PROBE_TARGET, 443
            internet_ms = self.tcp_latency(host, port, timeout=2.0) if host else None

            if started - targets_at >= PROBE_TARGET_INTERVAL:
                configured, _error, _mtime = self.probe_targets()
                target_results = [
                    dict(item, ms=self.tcp_latency(item["host"], item["port"],
                                                   timeout=PROBE_TARGET_TIMEOUT))
                    for item in configured
                ]
                targets_at = started

            self._probe_at = targets_at
            self._probe = {"gateway_ms": gateway_ms, "internet_ms": internet_ms,
                           "internet_target": NET_PROBE_TARGET, "at": time.time(),
                           "targets": target_results}

    def probe_targets(self):
        """读取 probes.json 里的远程探测目标。

        返回 (targets, error, mtime)；文件改了不用重启服务，mtime 变了就重载。
        """
        path = probes_path()
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            self._probes_cache = ([], None, None)
            return self._probes_cache
        if self._probes_cache[2] == mtime:
            return self._probes_cache
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            targets, problems = parse_probe_targets(data)
            self._probes_cache = (targets, "；".join(problems) if problems else None, mtime)
        except Exception as exc:      # 解析异常绝不能外抛：会连带打死探测线程与整站采样
            self._probes_cache = ([], f"probes.json 读取失败：{exc}", mtime)
        return self._probes_cache

    def _nic_info(self):
        if not self.nic:
            return {"available": False, "reason": "没有找到可用的物理网卡"}
        stats = psutil.net_if_stats().get(self.nic)
        info = {"available": True, "name": self.nic, "wireless": is_wireless_nic(self.nic)}
        if stats:
            info.update({
                "up": bool(stats.isup),
                "speed_mbps": stats.speed or None,
                "duplex": {0: None, 1: "半双工", 2: "全双工"}.get(stats.duplex),
                "mtu": stats.mtu,
            })
        for addr in psutil.net_if_addrs().get(self.nic, []):
            if addr.family == socket.AF_INET:
                info["ipv4"] = addr.address
                info["netmask"] = addr.netmask
            elif addr.family == socket.AF_INET6 and not info.get("ipv6"):
                info["ipv6"] = addr.address.split("%")[0]   # 去掉 %iface 后缀
            elif addr.family == psutil.AF_LINK:
                info["mac"] = addr.address
        try:
            counters = psutil.net_io_counters(pernic=True).get(self.nic)
        except Exception:
            counters = None
        if counters:
            info.update({
                "recv_total_gb": round(counters.bytes_recv / GIB, 2),
                "sent_total_gb": round(counters.bytes_sent / GIB, 2),
                "dropin": counters.dropin,
                "dropout": counters.dropout,
            })
        return info

    def _connections(self, limit=12):
        """连接概况 + 连接数最多的对端。非 root 一般拿不到连接归属的进程。"""
        try:
            conns = psutil.net_connections(kind="inet")
        except Exception as exc:
            return {"available": False, "reason": f"连接表读取失败：{exc}"}
        established = [item for item in conns if item.status == "ESTABLISHED"]
        counter = {}
        for item in established:
            if not item.raddr:
                continue
            key = f"{item.raddr.ip}:{item.raddr.port}"
            counter[key] = counter.get(key, 0) + 1
        remotes = [{"addr": addr, "count": count}
                   for addr, count in sorted(counter.items(),
                                             key=lambda pair: -pair[1])[:limit]]
        return {"available": True, "total": len(conns), "established": len(established),
                "connection_listening": sum(1 for item in conns if item.status == "LISTEN"),
                "remotes": remotes,
                "process_attribution": any(item.pid for item in established)}

    def network_info(self):
        """网络页数据（延迟取后台探测线程的最近一次结果）。"""
        nic = self._nic_info()
        ports = listen_ports()
        proxy = next((port for port in PROXY_PORTS if port in ports), None)
        connection = dict(self._connections())
        connection.update({
            "local_ip": nic.get("ipv4"),
            "gateway": self._gateway,
            "medium": "无线" if nic.get("wireless") else "有线",
            "gateway_ms": self._probe.get("gateway_ms"),
            "internet_ms": self._probe.get("internet_ms"),
            "internet_target": self._probe.get("internet_target"),
            "proxy_port": proxy,
            "listening": len(ports),
        })
        return {"nic": nic, "connection": connection}

    def _disk_static(self):
        """磁盘静态信息：设备、型号、容量、是否机械盘、总线（读一次后缓存）。

        Windows 没有 /sys/block，走 WMI（Win32_DiskDrive）取真实型号与容量。
        """
        if IS_WINDOWS:
            if self._disk_static_cache is None:
                # 传入监控路径：Windows 上会折算成盘符（"/" 视为系统盘）
                self._disk_static_cache = win.disk_static(self.disk_path)
            return self._disk_static_cache
        if self._disk_static_cache is not None:
            return self._disk_static_cache
        info = {"device": None, "block": None, "model": None,
                "size_gb": None, "rotational": None, "bus": None}
        for part in psutil.disk_partitions(all=False):
            if part.mountpoint == self.disk_path and part.device.startswith("/dev/"):
                info["device"] = part.device
                info["block"] = re.sub(r"\d+$", "", os.path.basename(part.device))
                break
        block = info["block"]
        if block:
            size = self._read_sys(f"/sys/block/{block}/size")
            if size and size.isdigit():
                info["size_gb"] = round(int(size) * 512 / GIB, 1)
            rotational = self._read_sys(f"/sys/block/{block}/queue/rotational")
            if rotational is not None:
                info["rotational"] = rotational == "1"
            info["model"] = (self._udev_model(block)
                             or self._read_sys(f"/sys/block/{block}/device/model"))
        self._disk_static_cache = info
        return info

    @staticmethod
    def _udev_model(block):
        """udevadm 给完整型号（sysfs 的 model 只有 16 字符），启动时读一次。"""
        try:
            done = subprocess.run(
                ["udevadm", "info", "--query=property", f"--path=/sys/block/{block}"],
                capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        for line in done.stdout.splitlines():
            if line.startswith("ID_MODEL="):
                return line.split("=", 1)[1].replace("_", " ").strip()
        return None

    def _mounts(self):
        """真实磁盘上的挂载点。跳过 tmpfs/overlay，以及 snap 的 loop/squashfs 噪声。"""
        mounts = []
        for part in psutil.disk_partitions(all=False):
            if not part.device.startswith("/dev/"):
                continue
            if part.device.startswith("/dev/loop") or part.fstype == "squashfs":
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            mounts.append({
                "mount": part.mountpoint, "device": part.device, "fstype": part.fstype,
                "total_gb": round(usage.total / GIB, 1),
                "used_percent": round(usage.percent, 1),
                "free_gb": round(usage.free / GIB, 1),
            })
        mounts.sort(key=lambda item: item["mount"])
        return mounts

    def _disk_io(self, now):
        """磁盘读写速率（差分）与累计量。"""
        out = {"read_bps": None, "write_bps": None,
               "read_total_gb": None, "write_total_gb": None}
        block = self._disk_static().get("block")
        if not block:
            return out
        try:
            counters = psutil.disk_io_counters(perdisk=True).get(block)
        except Exception:
            counters = None
        if counters is None:
            return out
        out["read_total_gb"] = round(counters.read_bytes / GIB, 2)
        out["write_total_gb"] = round(counters.write_bytes / GIB, 2)
        if self._disk_io_prev is not None:
            prev_ts, prev_read, prev_write = self._disk_io_prev
            delta_t = now - prev_ts
            if delta_t > 0:
                out["read_bps"] = round(max(0.0, (counters.read_bytes - prev_read) / delta_t), 1)
                out["write_bps"] = round(max(0.0, (counters.write_bytes - prev_write) / delta_t), 1)
        self._disk_io_prev = (now, counters.read_bytes, counters.write_bytes)
        return out

    # ---------------- 设备页：主机 / 处理器 / 内存磁盘 / 网络 / 运行环境 ----------------

    def _cpu_caches(self):
        """从 sysfs 读各级缓存（免 root、免起 lscpu）。Windows 无等价接口。"""
        if IS_WINDOWS:
            return []
        rows = []
        base = "/sys/devices/system/cpu/cpu0/cache"
        for index in range(5):
            level = self._read_sys(f"{base}/index{index}/level")
            kind = self._read_sys(f"{base}/index{index}/type")
            size = self._read_sys(f"{base}/index{index}/size")
            if not level or not size:
                continue
            rows.append({"level": int(level) if level.isdigit() else level,
                         "type": (kind or "").lower(), "size": size})
        return rows

    @staticmethod
    def _cache_label(item):
        """L1 分开写数据/指令，L2/L3 是统一缓存。"""
        if item["level"] == 1:
            if item["type"].startswith("d"):
                return "L1d"
            if item["type"].startswith("i"):
                return "L1i"
            return "L1"
        return f"L{item['level']}"

    @staticmethod
    def _format_cache_size(text):
        """'3072K' -> '3 MB'，'32K' -> '32 KB'。"""
        match = re.fullmatch(r"(\d+)K", (text or "").strip())
        if not match:
            return text or ""
        kb = int(match.group(1))
        if kb >= 1024:
            value = kb / 1024
            return f"{value:.0f} MB" if value == int(value) else f"{value:.1f} MB"
        return f"{kb} KB"

    @staticmethod
    def _docker_version():
        try:
            done = subprocess.run(["docker", "--version"], capture_output=True,
                                  text=True, timeout=4, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        text = (done.stdout or done.stderr or "").strip()
        match = re.search(r"(\d+\.\d+\.\d+)", text)
        return match.group(1) if match else (text or None)

    def device_info(self):
        """设备页信息：基本都是静态的，缓存 60 秒（其中 docker 版本要起子进程）。"""
        with self._device_lock:
            now = time.time()
            if self._device is None or now - self._device_at > DEVICE_TTL:
                self._device = self._collect_device()
                self._device_at = now
            return self._device

    @staticmethod
    def interface_kind(name):
        """网卡类型：回环 / 无线 / 虚拟（docker、网桥、veth）/ 有线。"""
        low = (name or "").lower()
        if low == "lo":
            return "回环"
        if low.startswith("wl") or re.search(r"(?i)wi-?fi|wlan|wireless|无线", name or ""):
            return "无线"
        if low.startswith(("docker", "br-", "veth", "virbr", "tun", "tap", "wg", "zt")):
            return "虚拟"
        return "有线"

    def _usb_devices(self):
        """USB 设备：Linux 读 sysfs（厂商与型号分字段、免 root）；Windows 枚举 PnP 设备。

        1d6b 是 Linux 基金会的根集线器——那是控制器本身，不是外接设备，标记出来由前端弱化。
        Windows 侧没有这种厂商 ID，按根集线器名称识别。
        """
        if IS_WINDOWS:
            return win.usb_devices()
        rows = []
        base = "/sys/bus/usb/devices"
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            entries = []
        for entry in entries:
            path = os.path.join(base, entry)
            vendor_id = self._read_sys(os.path.join(path, "idVendor"))
            product_id = self._read_sys(os.path.join(path, "idProduct"))
            if not vendor_id or not product_id:
                continue
            bus = self._read_sys(os.path.join(path, "busnum"))
            device = self._read_sys(os.path.join(path, "devnum"))
            rows.append({
                "id": f"{vendor_id}:{product_id}",
                "vendor": self._read_sys(os.path.join(path, "manufacturer")),
                "product": self._read_sys(os.path.join(path, "product")),
                "bus": int(bus) if bus and bus.isdigit() else None,
                "device": int(device) if device and device.isdigit() else None,
                "hub": vendor_id.lower() == "1d6b",
            })
        rows.sort(key=lambda item: (item["hub"], item["bus"] or 0, item["device"] or 0))
        return rows

    def _bluetooth(self):
        """蓝牙：Linux 看 /sys/class/bluetooth 并问 bluetoothctl；Windows 枚举 PnP。"""
        if IS_WINDOWS:
            return win.bluetooth_devices()
        try:
            adapters = sorted(os.listdir("/sys/class/bluetooth"))
        except OSError:
            adapters = []
        if not adapters:
            return {"available": False, "reason": "没有蓝牙适配器",
                    "adapters": [], "devices": []}
        devices = []
        try:
            done = subprocess.run(["bluetoothctl", "devices"], capture_output=True,
                                  text=True, timeout=5, check=False)
            for line in (done.stdout or "").splitlines():
                fields = line.split(None, 2)
                if len(fields) >= 2 and fields[0] == "Device":
                    devices.append({"mac": fields[1],
                                    "name": fields[2] if len(fields) > 2 else None})
        except (OSError, subprocess.SubprocessError):
            pass
        return {"available": True, "reason": None, "adapters": adapters, "devices": devices}

    def _wireless_info(self, name):
        """无线网卡的信号与 SSID（没有无线网卡时返回 None）。

        /proc/net/wireless 给链路质量与信号强度（dBm），iwconfig 给 ESSID；
        以后插上无线网卡，这一页会自动多出带 SSID 与信号的条目。
        """
        raw = self._read_sys("/proc/net/wireless")
        if not raw:
            return None
        for line in raw.splitlines():
            if not line.startswith(name + ":"):
                continue
            parts = line.split(":", 1)[1].split()
            if len(parts) < 3:
                return None

            def number(text):
                try:
                    return round(float(text.rstrip(".")), 1)
                except ValueError:
                    return None

            info = {"status": parts[0], "quality": number(parts[1]),
                    "signal_dbm": number(parts[2]), "ssid": None}
            try:
                done = subprocess.run(["iwconfig", name], capture_output=True,
                                      text=True, timeout=3, check=False)
                match = re.search(r'ESSID:"([^"]*)"', done.stdout or "")
                info["ssid"] = match.group(1) if match else None
            except (OSError, subprocess.SubprocessError):
                pass
            return info
        return None

    def _interfaces(self):
        """网络接口：物理与无线逐条列，虚拟接口只给数量与名字（docker 一多会淹没页面）。"""
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        physical, virtual = [], []
        for name in sorted(stats):
            kind = self.interface_kind(name)
            info = {"name": name, "kind": kind, "up": bool(stats[name].isup),
                    "speed_mbps": stats[name].speed or None, "mtu": stats[name].mtu}
            for addr in addrs.get(name, []):
                if addr.family == socket.AF_INET:
                    info["ipv4"] = addr.address
                elif addr.family == socket.AF_INET6 and not info.get("ipv6"):
                    info["ipv6"] = addr.address.split("%")[0]
                elif addr.family == psutil.AF_LINK:
                    info["mac"] = addr.address
            if kind == "无线":
                info["wireless"] = self._wireless_info(name)
            (virtual if kind == "虚拟" else physical).append(info)
        return {"physical": physical, "virtual": virtual}

    # ---------------- 局域网设备（免凭据发现） ----------------

    def _local_subnet(self):
        """本机物理网卡所在网段：返回 (ip, netmask, 前缀, 展示用标签)。"""
        if not self.nic:
            return None
        for addr in psutil.net_if_addrs().get(self.nic, []):
            if addr.family != socket.AF_INET or not addr.netmask:
                continue
            ip, netmask = addr.address, addr.netmask
            label = subnet_label(ip, netmask)
            if not label:
                continue
            return {"ip": ip, "netmask": netmask, "label": label,
                    "prefix": ip.rsplit(".", 1)[0] + "."}
        return None

    @staticmethod
    def _reverse_dns(ip):
        """反向解析主机名（本网段通常由路由器 DNS 提供，例如 Xiaomi-14-Pro.lan）。"""
        try:
            name = socket.gethostbyaddr(ip)[0]
        except (OSError, socket.herror):
            return None
        if not name:
            return None
        return name[:-4] if name.endswith(".lan") else name

    @staticmethod
    def _ping(ip):
        """单次 ICMP 探测（ping 有权限时可用；不可用就退化为只看 ARP 表）。"""
        try:
            done = subprocess.run(["ping", "-c", "1", "-W", "1", "-n", ip],
                                  capture_output=True, timeout=3)
        except FileNotFoundError:
            raise
        except (OSError, subprocess.SubprocessError):
            return None
        return ip if done.returncode == 0 else None

    def _ping_sweep(self, hosts):
        """并发 ping 整段网段；ping 不可用时抛出 FileNotFoundError 由上层降级。"""
        live = []
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=LAN_PING_WORKERS) as pool:
            for result in pool.map(self._ping_host, hosts):
                if result:
                    live.append(result)
        return live

    @staticmethod
    def _ssdp_scan(window=LAN_SSDP_WINDOW):
        """SSDP/UPnP 组播查询：路由器、NAS、电视、打印机等通常会回应。"""
        found = {}
        message = (b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                   b'MAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n')
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(0.6)
        except OSError:
            return found
        try:
            for _ in range(2):
                try:
                    sock.sendto(message, ("239.255.255.250", 1900))
                except OSError:
                    break
            deadline = time.time() + window
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                info = parse_ssdp_response(data.decode("utf-8", "replace"))
                if info and addr[0] not in found:
                    found[addr[0]] = info
        finally:
            sock.close()
        return found

    def _lan_scan(self):
        """主动扫描：ping 整段 + SSDP 查询 + 反向 DNS，随后从 ARP 表取 MAC。"""
        started = time.time()
        subnet = self._local_subnet()
        if not subnet:
            return {"hosts": [], "subnet": None, "swept": 0, "live": 0,
                    "note": "没有找到可用的物理网卡，无法判断网段"}
        hosts = subnet_candidates(subnet["ip"], subnet["netmask"])
        note = None
        live = []
        try:
            live = self._ping_sweep(hosts)
        except FileNotFoundError:
            note = "本机没有 ping 命令，只能看邻居表里出现过的设备"
        except Exception as exc:                      # 扫描失败不该影响页面
            note = f"ICMP 探测失败：{exc}"
        ssdp = self._ssdp_scan()
        arp = self._neighbors()
        rows = []
        for ip in sorted(set(live) | {item for item in ssdp if item.startswith(subnet["prefix"])},
                         key=lambda text: [int(part) for part in text.split(".")]):
            sources = ["icmp"] if ip in live else []
            if ip in ssdp:
                sources.append("ssdp")
            rows.append({"ip": ip, "alive": ip in live, "name": self._reverse_dns(ip),
                         "mac": arp.get(ip), "sources": sources,
                         "ssdp": ssdp.get(ip, {}).get("server"),
                         "location": ssdp.get(ip, {}).get("location")})
        return {"hosts": rows, "subnet": subnet["label"], "swept": len(hosts),
                "live": len(live), "note": note,
                "duration_s": round(time.time() - started, 1)}

    def lan_scan_loop(self):
        """后台线程：周期性扫描局域网（主动扫描不该拖慢采样与请求）。"""
        while True:
            started = time.time()
            try:
                result = self._lan_scan()
                self._lan_cache = (result, time.time())
            except Exception as exc:
                print(f"[lan] 扫描失败：{exc}", flush=True)
            time.sleep(max(5.0, LAN_SWEEP_INTERVAL - (time.time() - started)))

    def lan_devices(self):
        """局域网设备：主动扫描结果（缓存）+ 实时邻居表，补上厂商与名称。

        全程不需要任何设备的账号密码：邻居表/ARP 是内核维护的，
        ICMP 与 SSDP 组播只是「问一声」；只有想看路由器自己的客户端列表才需要路由器管理口令。
        """
        cache, scanned_at = self._lan_cache
        subnet = self._local_subnet()
        prefix = subnet["prefix"] if subnet else None
        arp = self._neighbors()
        table = load_oui()
        devices = {}

        for host in cache.get("hosts", []):
            devices[host["ip"]] = {
                "ip": host["ip"], "alive": host.get("alive"), "name": host.get("name"),
                "mac": host.get("mac"), "sources": list(host.get("sources") or []),
                "ssdp": host.get("ssdp"),
            }
        for ip, mac in arp.items():
            if prefix and not ip.startswith(prefix):
                continue                                  # docker 网桥那些不算局域网设备
            entry = devices.setdefault(ip, {"ip": ip, "alive": None, "name": None,
                                            "sources": []})
            entry["mac"] = mac
            if "arp" not in entry["sources"]:
                entry["sources"].append("arp")
            # 名称只在后台扫描线程里解析：反向 DNS 可能很慢，
            # 放在请求线程里会拖住 /api/device 并让其他请求排队等锁。
        for entry in devices.values():
            entry["vendor"] = oui_vendor(entry.get("mac"), table)
        hosts = sorted(devices.values(),
                       key=lambda item: [int(part) for part in item["ip"].split(".")
                                         if part.isdigit()] or [0])
        return {
            "hosts": hosts,
            "subnet": (cache.get("subnet") or (subnet or {}).get("label")),
            "scanned_at": scanned_at or None,
            "scan_seconds": cache.get("duration_s"),
            "swept": cache.get("swept", 0),
            "live": cache.get("live", 0),
            "note": cache.get("note"),
            "oui": bool(table),
        }

    def _collect_device(self):
        """设备页：本机摘要 + 与这台机器连接的设备（USB / 蓝牙 / 网络 / 局域网）。"""
        if IS_WINDOWS:
            return self._collect_device_windows()
        os_release = parse_os_release(self._read_sys("/etc/os-release"))
        cpuinfo = self._read_sys("/proc/cpuinfo") or ""
        caches = self._cpu_caches()
        memory = self._memory()
        disk = self._disk_static()
        battery = self._battery()
        gpu = self._gpu_freq()
        usb = self._usb_devices()
        return {
            "summary": {
                "hostname": socket.gethostname(),
                "os": os_release.get("PRETTY_NAME") or os_release.get("NAME"),
                "kernel": platform.release(),
                "arch": platform.machine(),
                "uptime_s": round(max(0.0, time.time() - self.boot_time)),
                "vendor": self._read_sys("/sys/class/dmi/id/sys_vendor"),
                "product": self._read_sys("/sys/class/dmi/id/product_name"),
                "bios": self._read_sys("/sys/class/dmi/id/bios_version"),
                "cpu": parse_cpu_model(cpuinfo),
                "cores": psutil.cpu_count(logical=False),
                "threads": self.cores,
                "cache_text": " · ".join(
                    f"{self._cache_label(item)} {self._format_cache_size(item['size'])}"
                    for item in caches),
                "virtualization": virtualization_label(parse_cpu_flags(cpuinfo)),
                "memory_gb": memory.get("total_gb"),
                "swap_gb": memory.get("swap_total_gb"),
                "gpu": gpu.get("max_mhz") if gpu.get("available") else None,
                "disk": disk,
                "mounts": self._mounts(),
            },
            "usb": {"list": usb, "total": len(usb),
                    "external": sum(1 for item in usb if not item["hub"])},
            "bluetooth": self._bluetooth(),
            "interfaces": self._interfaces(),
            "lan": self.lan_devices(),
            "battery": battery if battery.get("available") else None,
            "runtime": {
                "python": platform.python_version(),
                "psutil": psutil.__version__,
                "docker": self._docker_version(),
            },
        }

    def _collect_device_windows(self):
        """Windows 的设备页：Linux 专有接口（/proc、/sys）用 platform_win 替代，
        其余（内存/接口/局域网/电池）复用跨平台的 psutil 实现。"""
        static = win.collect_device_static()
        summary = static["summary"]
        summary.update({
            "hostname": socket.gethostname(),
            "uptime_s": round(max(0.0, time.time() - self.boot_time)),
            "memory_gb": self._memory().get("total_gb"),
            "swap_gb": self._memory().get("swap_total_gb"),
            "gpu": None,
            "disk": static["disk"],
            "mounts": static["disk"].get("mounts"),
        })
        stat_info = static["disk"]
        battery = self._battery()
        return {
            "summary": summary,
            "usb": static["usb"],
            "bluetooth": static["bluetooth"],
            "interfaces": self._interfaces(),
            "lan": self.lan_devices(),
            "battery": battery if battery.get("available") else None,
            "runtime": win.runtime_versions(),
        }

    # ---------------- 服务页：端口 / systemd / 容器 / 远程探测 ----------------

    def _port_processes(self):
        """端口 -> 进程名。非 root 只能映射自己拥有的进程（读不到 root 进程的 /proc/<pid>/fd）。"""
        with self._port_procs_lock:
            now = time.time()
            if self._port_procs is None or now - self._port_procs_at > SERVICES_TTL:
                mapping = {}
                try:
                    for conn in psutil.net_connections(kind="inet"):
                        if conn.status != "LISTEN" or not conn.pid or not conn.laddr:
                            continue
                        try:
                            mapping.setdefault(conn.laddr.port, psutil.Process(conn.pid).name())
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                except Exception:
                    pass
                self._port_procs = mapping
                self._port_procs_at = now
            return self._port_procs

    def service_ports(self):
        """监听端口表：同一端口可能在 tcp/tcp6 各有一条，合并成一行，范围取更宽的。"""
        processes = self._port_processes()
        merged = {}
        for item in listen_sockets():
            row = merged.get(item["port"])
            if row is None:
                merged[item["port"]] = {
                    "port": item["port"], "proto": item["proto"],
                    "addr": item["addr"], "scope": item["scope"],
                    "process": processes.get(item["port"]),
                    "known": KNOWN_PORTS.get(item["port"]),
                }
                continue
            if item["proto"] not in row["proto"].split("/"):
                row["proto"] = "/".join(sorted({row["proto"], item["proto"]}))
            if item["scope"] == "局域网" and row["scope"] != "局域网":
                # 范围取更宽的；地址保留先出现的（一般是 IPv4，比 :: 好读）
                row["scope"] = "局域网"
                row["addr"] = item["addr"]
        return sorted(merged.values(), key=lambda item: item["port"])

    def systemd_services(self):
        """运行中的 systemd 服务（带 TTL 缓存，避免频繁起子进程）。"""
        with self._systemd_lock:
            now = time.time()
            if self._systemd is None or now - self._systemd_at > SYSTEMD_TTL:
                self._systemd = self._collect_systemd()
                self._systemd_at = now
            return self._systemd

    @staticmethod
    def _collect_systemd(limit=80):
        """运行中的系统服务（Linux）或 Windows 服务（Windows）；都没有就如实降级。"""
        if IS_WINDOWS:
            return win.windows_services(limit)
        try:
            done = subprocess.run(
                ["systemctl", "list-units", "--type=service", "--state=running",
                 "--no-pager", "--no-legend", "--plain"],
                capture_output=True, text=True, timeout=5, check=False)
        except FileNotFoundError:
            return {"available": False, "reason": "没有 systemctl 命令"}
        except subprocess.TimeoutExpired:
            return {"available": False, "reason": "systemctl 调用超时"}
        except OSError as exc:
            return {"available": False, "reason": f"systemctl 调用失败：{exc}"}
        if done.returncode != 0:
            first = (done.stderr or "").strip().splitlines()
            return {"available": False,
                    "reason": first[0] if first else f"systemctl 返回码 {done.returncode}"}
        rows = []
        for line in done.stdout.splitlines():
            fields = line.split(None, 4)
            if len(fields) < 4 or not fields[0].endswith(".service"):
                continue
            rows.append({"unit": fields[0], "description": fields[4].strip() if len(fields) > 4 else ""})
        rows.sort(key=lambda item: item["unit"])
        return {"available": True, "total": len(rows), "list": rows[:limit]}

    def services_detail(self):
        """服务页需要的全部数据（容器 / systemd / 端口 / 远程探测结果）。"""
        containers = []
        rows, note = self._docker_containers(limit=100)
        if note:
            containers_note = note
        else:
            containers_note = None
            containers = [{"name": row["name"], "up": row["status"] == "ok",
                           "status": row["detail"], "image": row.get("image")} for row in rows]
        targets, error, _mtime = self.probe_targets()
        return {
            "containers": {"list": containers, "note": containers_note,
                           "total": self._container_total, "running": self._container_running},
            "systemd": self.systemd_services(),
            "ports": self.service_ports(),
            "probes": {"list": self._probe.get("targets") or [], "error": error,
                       "path": probes_path(), "interval": int(PROBE_TARGET_INTERVAL)},
        }

    # ---------------- 服务状态（带 TTL 缓存，避免频繁起 docker 子进程） ----------------

    def services(self):
        with self._services_lock:
            now = time.time()
            if self._services is None or now - self._services_at > SERVICES_TTL:
                self._services = self._collect_services()
                self._services_at = now
            return self._services

    def _collect_services(self):
        items = []
        containers, docker_note = self._docker_containers()
        if docker_note:
            items.append({"group": "容器", "name": "Docker", "status": "unknown",
                          "detail": docker_note})
        else:
            items.append({"group": "容器", "name": "Docker",
                          "status": "ok" if self._container_running else "warn",
                          "detail": f"{self._container_running}/{self._container_total} 运行中",
                          "groupNote": self._container_note})
            items.extend(containers)

        ports = listen_ports()
        ssh_ok = 22 in ports
        items.append({"group": "本机", "name": "SSH (22)",
                      "status": "ok" if ssh_ok else "down",
                      "detail": "监听中" if ssh_ok else "未监听"})
        items.append({"group": "本机", "name": "监听端口",
                      "status": "ok" if ports else "warn",
                      "detail": f"{len(ports)} 个"})

        temp = self._temp()
        if temp["available"]:
            hot = temp["celsius"] >= 85
            items.append({"group": "健康", "name": "散热",
                          "status": "warn" if hot else "ok",
                          "detail": f"{temp['source']} {temp['celsius']}°C"})
        else:
            items.append({"group": "健康", "name": "散热", "status": "unknown",
                          "detail": temp["reason"]})

        load = self._load()
        if load["available"]:
            items.append({"group": "健康", "name": "系统负载", "status": "ok",
                          "detail": f"{load['avg1']} / {load['avg5']} / {load['avg15']}"})
        return items

    def _docker_containers(self, limit=4):
        self._container_note = ""
        self._container_total = 0
        self._container_running = 0
        try:
            done = subprocess.run(
                ["docker", "ps", "-a", "--format",
                 "{{.Names}}\t{{.Status}}\t{{.ID}}\t{{.Image}}"],
                capture_output=True, text=True, timeout=4, check=False,
            )
        except FileNotFoundError:
            return [], "没有 docker 命令"
        except subprocess.TimeoutExpired:
            return [], "docker 命令超时"
        except OSError as exc:
            return [], f"docker 调用失败：{exc}"

        if done.returncode != 0:
            first_line = (done.stderr or "").strip().splitlines()
            return [], (first_line[0] if first_line else f"docker 返回码 {done.returncode}")

        rows = []
        names = {}
        for line in done.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            name = parts[0].strip()
            status = parts[1].strip() if len(parts) > 1 else ""
            container_id = parts[2].strip() if len(parts) > 2 else ""
            if container_id:
                names[container_id[:12]] = name
            rows.append({"group": "容器", "name": name,
                         "status": "ok" if status.lower().startswith("up") else "down",
                         "detail": status,
                         "image": parts[3].strip() if len(parts) > 3 else None})
        self._container_names = names   # 供进程页做「这个进程属于哪个容器」
        rows.sort(key=lambda row: (row["status"] != "ok", row["name"]))
        # 汇总用完整列表统计，展示则受 limit 限制
        self._container_total = len(rows)
        self._container_running = sum(1 for row in rows if row["status"] == "ok")
        if len(rows) > limit:
            self._container_note = f"另有 {len(rows) - limit} 个未显示"
            rows = rows[:limit]
        return rows, None
