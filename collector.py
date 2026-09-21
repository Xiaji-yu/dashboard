"""本机实时指标采集（Python 标准库 + psutil）。

设计原则：任何一项采不到都不抛异常，而是返回 available=False 与原因，
由前端显示为「不可用」。这样在缺传感器、缺权限、缺 docker 的机器上页面依然可用。
"""

from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import threading
import time

import psutil

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

# 常见代理端口（「本地代理」一行用；8080 太通用，刻意不算）
PROXY_PORTS = (7890, 7891, 1080, 1081, 8118, 3128, 8889, 7897)
# 延迟探测：网关试这几个端口取最快的一个；外网目标可用环境变量改
GATEWAY_PROBE_PORTS = (53, 22, 443, 80)
NET_PROBE_TARGET = os.environ.get("DASHBOARD_NET_TARGET", "www.baidu.com:443")


def probes_path():
    """远程探测目标配置文件（可用 DASHBOARD_PROBES 指向别处）。"""
    return os.environ.get("DASHBOARD_PROBES") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "probes.json")


def format_sockaddr(addr_hex, is_v6):
    """把 /proc/net/tcp{,6} 里的十六进制地址还原成可读 IP。"""
    try:
        if is_v6:
            return socket.inet_ntop(socket.AF_INET6, bytes.fromhex(addr_hex))
        return socket.inet_ntoa(struct.pack("<I", int(addr_hex, 16)))
    except (ValueError, OSError):
        return addr_hex


def listen_sockets():
    """监听中的 TCP 端口，附带绑定地址与访问范围。

    「仅本机」= 绑在 127.x / ::1，只有本机能连；「局域网」= 绑在 0.0.0.0 / ::，
    同网段的机器都能连（参考图里的「谁能访问」一列）。
    """
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
                    if addr in ("0.0.0.0", "::"):
                        scope = "局域网"
                    elif addr.startswith("127.") or addr == "::1" or addr.endswith("127.0.0.1"):
                        scope = "仅本机"
                    else:
                        scope = "其他"
                    rows.append({"port": port, "proto": "tcp6" if is_v6 else "tcp",
                                 "addr": addr, "scope": scope})
        except OSError:
            continue
    rows.sort(key=lambda item: item["port"])
    return rows


def is_wireless_nic(name):
    """Linux 命名约定：wl* 是无线网卡。"""
    return (name or "").lower().startswith("wl")


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
    """从 /proc/net/tcp{,6} 读取 LISTEN 端口，免 root。"""
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
    return {"available": True, "percent": round(batt.percent, 1), "plugged": plugged,
            "status": status, "cycles": cycles, "power_w": power_w, "secsleft": secsleft}


class Collector:
    """采样本机指标。进程 CPU、网速、功耗都靠两次采样求差分。"""

    def __init__(self):
        self.disk_path = os.environ.get("DASHBOARD_DISK", "/")
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
        return {
            "available": True,
            "used_gb": round(used, 1),
            "total_gb": round(total, 1),
            "percent": round(used / total * 100, 1) if total else 0.0,
            "free_gb": round(vm.free / GIB, 1),
            "buffers_gb": round(vm.buffers / GIB, 2),
            "cached_gb": round(vm.cached / GIB, 2),
            "shared_gb": round(vm.shared / GIB, 2),
            "swap_total_gb": round(swap.total / GIB, 1),
            "swap_used_gb": round(swap.used / GIB, 1),
        }

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
        try:
            avg1, avg5, avg15 = os.getloadavg()
        except OSError:
            return {"available": False, "reason": "本机不支持负载查询"}
        return {"available": True, "avg1": round(avg1, 2),
                "avg5": round(avg5, 2), "avg15": round(avg15, 2)}

    # ---------------- 网络与磁盘 ----------------

    def _read_gateway(self):
        """默认网关：/proc/net/route 免 root 读取。"""
        return parse_default_gateway(self._read_sys("/proc/net/route"))

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

            self._probe = {"gateway_ms": gateway_ms, "internet_ms": internet_ms,
                           "internet_target": NET_PROBE_TARGET, "at": time.time(),
                           "targets": target_results}
            time.sleep(max(1.0, NET_PROBE_INTERVAL - (time.time() - started)))

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
            targets = []
            for item in (data.get("probes") or []):
                host = (item or {}).get("host")
                port = (item or {}).get("port")
                if not host or not port:
                    continue
                try:
                    targets.append({"name": item.get("name") or str(host),
                                    "host": str(host), "port": int(port)})
                except (TypeError, ValueError):
                    continue
            self._probes_cache = (targets, None, mtime)
        except (OSError, ValueError, TypeError) as exc:
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
        """磁盘静态信息：设备、型号、容量、是否机械盘、总线（读一次后缓存）。"""
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
