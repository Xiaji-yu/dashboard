"""本机实时指标采集（Python 标准库 + psutil）。

设计原则：任何一项采不到都不抛异常，而是返回 available=False 与原因，
由前端显示为「不可用」。这样在缺传感器、缺权限、缺 docker 的机器上页面依然可用。
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time

import psutil

# 虚拟网卡前缀：网速默认只统计物理网卡，避免 docker 内部流量污染曲线
VIRTUAL_NIC_PREFIXES = (
    "lo", "docker", "br-", "veth", "virbr", "tun", "tap", "wg", "zt",
    "vmnet", "vboxnet", "snap", "dummy",
)
# Intel RAPL 整机功耗计数器（读 energy_uj 需要 root，通常不可用）
RAPL_ENERGY = "/sys/class/powercap/intel-rapl:0/energy_uj"
SERVICES_TTL = 5.0
GIB = 1024.0 ** 3
MIB = 1024.0 ** 2

# 风扇 key 的中文命名，其余 key 直接展示原始 label
FAN_LABELS = {"cpu_fan": "CPU 风扇", "gpu_fan": "GPU 风扇", "fan1": "机箱风扇"}


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
        self._rapl_prev = None
        self._rapl_note = None
        self._procs = {}
        self._services = None
        self._services_at = 0.0
        self._services_lock = threading.Lock()
        self._container_note = ""
        self._container_total = 0
        self._container_running = 0
        self._gpu_card = self._find_gpu_card()
        self._core_topology = self._read_core_topology()
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
        """CPU 占用最高的若干进程。

        CPU 值按逻辑核心数归一化到 0-100，口径贴近 macOS 活动监视器；
        `top`/`ps` 显示的原始值最高可达 100 x 核心数，顺序一致、数值约为其 1/核心数。
        """
        rows = []
        for proc in list(self._procs.values()):
            try:
                with proc.oneshot():
                    cpu = proc.cpu_percent(interval=None) / self.cores
                    rss = proc.memory_info().rss
                    name = proc.name()
                    pid = proc.pid
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._procs.pop(proc.pid, None)
                continue
            rows.append({"pid": pid, "name": name, "cpu": round(cpu, 1),
                         "rss_mb": round(rss / MIB, 1)})

        # 补进新出现的进程并预热，下次采样才有值
        for proc in psutil.process_iter(["pid"]):
            if proc.pid in self._procs:
                continue
            try:
                proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            self._procs[proc.pid] = proc

        rows.sort(key=lambda row: (row["cpu"], row["rss_mb"]), reverse=True)
        return rows[:limit]

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
            "disk": self._disk(),
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

    def _power(self, now):
        """整机功耗：Intel RAPL 能量计数器差分。energy_uj 通常仅 root 可读。"""
        if self._rapl_note:
            return {"available": False, "reason": self._rapl_note}
        try:
            with open(RAPL_ENERGY, "r") as handle:
                energy = int(handle.read().strip())
        except FileNotFoundError:
            self._rapl_note = "本机没有 Intel RAPL 功耗计数器"
            return {"available": False, "reason": self._rapl_note}
        except PermissionError:
            self._rapl_note = "读取 RAPL 需要 root 权限"
            return {"available": False, "reason": self._rapl_note}
        except (OSError, ValueError) as exc:
            self._rapl_note = f"RAPL 读取失败：{exc}"
            return {"available": False, "reason": self._rapl_note}

        watts = None
        if self._rapl_prev is not None:
            prev_ts, prev_energy = self._rapl_prev
            delta_t = now - prev_ts
            delta_e = energy - prev_energy
            if delta_t > 0 and delta_e >= 0:  # 计数器溢出时跳过该点
                watts = round(delta_e / 1e6 / delta_t, 2)
        self._rapl_prev = (now, energy)
        if watts is None:
            return {"available": False, "reason": "正在预热功耗采样"}
        return {"available": True, "watts": watts}

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

    def _disk(self):
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
                ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"],
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
        for line in done.stdout.splitlines():
            if not line.strip():
                continue
            name, _, status = line.partition("\t")
            status = status.strip()
            rows.append({"group": "容器", "name": name.strip(),
                         "status": "ok" if status.lower().startswith("up") else "down",
                         "detail": status})
        rows.sort(key=lambda row: (row["status"] != "ok", row["name"]))
        # 汇总用完整列表统计，展示则受 limit 限制
        self._container_total = len(rows)
        self._container_running = sum(1 for row in rows if row["status"] == "ok")
        if len(rows) > limit:
            self._container_note = f"另有 {len(rows) - limit} 个未显示"
            rows = rows[:limit]
        return rows, None
