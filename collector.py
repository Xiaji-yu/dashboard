"""本机实时指标采集（Python 标准库 + psutil）。

设计原则：任何一项采不到都不抛异常，而是返回 available=False 与原因，
由前端显示为「不可用」。这样在缺传感器、缺权限、缺 docker 的机器上页面依然可用。
"""

from __future__ import annotations

import os
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
        psutil.cpu_percent(interval=None)  # 预热，让首次采样就有意义
        self._prime_processes()

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
        return {
            "ts": now,
            "host": socket.gethostname(),
            "cores": self.cores,
            "uptime_s": max(0.0, now - self.boot_time),
            "cpu": self._cpu(),
            "memory": self._memory(),
            "power": self._power(now),
            "net": self._net(now),
            "disk": self._disk(),
            "temp": self._temp(),
            "load": self._load(),
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
            swap_used = psutil.swap_memory().used / GIB
        except Exception as exc:
            return {"available": False, "reason": f"内存采样失败：{exc}"}
        return {
            "available": True,
            "used_gb": round(used, 1),
            "total_gb": round(total, 1),
            "percent": round(used / total * 100, 1) if total else 0.0,
            "swap_used_gb": round(swap_used, 1),
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
        try:
            sensors = psutil.sensors_temperatures() or {}
        except Exception as exc:
            return {"available": False, "reason": f"温度采样失败：{exc}"}
        if not sensors:
            return {"available": False, "reason": "本机没有可读的温度传感器"}

        for entry in sensors.get("coretemp", ()):
            label = (entry.label or "").lower()
            if "package" in label and entry.current:
                return {"available": True, "celsius": round(entry.current, 1),
                        "source": entry.label}
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
            for entry in sensors.get(key, ()):
                if entry.current:
                    return {"available": True, "celsius": round(entry.current, 1),
                            "source": entry.label or key}
        for key, entries in sensors.items():
            for entry in entries:
                if entry.current:
                    return {"available": True, "celsius": round(entry.current, 1),
                            "source": entry.label or key}
        return {"available": False, "reason": "温度传感器没有返回数值"}

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
