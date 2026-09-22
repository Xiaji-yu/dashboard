"""Windows 平台的采集实现：psutil + PowerShell(ConvertTo-Json) + netsh / arp。

设计原则：
- 与 Linux 版**共用同一套载荷结构**，前端零改动；
- 只用标准库：主机信息经 PowerShell 取（Win7+ 自带，无需装依赖）；
  解析与子进程调用分开写成纯函数，便于在没有 Windows 的机器上写测试；
- 拿不到的数据一律 `available=False` + 中文原因（沿袭 Linux 版的降级契约），绝不造假；
- 已知在 Windows 上不可用：温度、风扇、RAPL 功耗、核显频率
  （psutil 的 sensors_temperatures/fans 明确不支持 Windows；功耗与会话需要驱动或额外方案）。

入口只有 `collector.py` 调：见文件末尾的 `collect_device_static()` 与各小函数。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys

POWERSHELL_TIMEOUT = 8.0
PING_TIMEOUT_MS = 1000
ARP_TIMEOUT = 5.0

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    # 提前到模块导入时检查：缺包时给一句人话，而不是让人从 traceback 里猜
    try:
        import psutil  # noqa: F401  （本模块几乎每个函数都要用，装一次就够）
    except ImportError as exc:      # pragma: no cover - 仅在缺包的 Windows 上命中
        raise SystemExit(
            "缺少依赖 psutil，看板无法启动。\n"
            "  请先安装：  python -m pip install psutil\n"
            f"  原始错误：  {exc}")


# ---------------- 子进程封装 ----------------

def run_powershell(script, timeout=POWERSHELL_TIMEOUT):
    """跑 PowerShell 并返回其标准输出文本；失败返回 None。

    优先用 Windows 自带的 powershell.exe（5.1）；有 pwsh 也支持，命令行参数相同。
    """
    executable = "powershell"
    if not _command_exists(executable):
        executable = "pwsh"
    try:
        done = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False)
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not (done.stdout or "").strip():
        return None
    return done.stdout


def _command_exists(name):
    """PATH 里有没有这个可执行文件。"""
    paths = os.environ.get("PATH", "").split(os.pathsep)
    for directory in paths:
        candidate = os.path.join(directory.strip('"'), name)
        for attempt in (candidate, candidate + ".exe", candidate + ".CMD", candidate + ".BAT"):
            if os.path.isfile(attempt):
                return True
    return False


def powershell_json(script, timeout=POWERSHELL_TIMEOUT):
    """跑 PowerShell 并解析 ConvertTo-Json 输出；单个对象的 JSON 会包成 dict。"""
    text = run_powershell(script, timeout)
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if isinstance(payload, dict):
        # 单条结果 ConvertTo-Json 输出对象；统一成 list 方便调用方遍历
        return [payload]
    if isinstance(payload, list):
        return payload
    return None


# ---------------- 纯解析函数（可在任意平台测试） ----------------

def parse_arp(text):
    """解析 `arp -a` 输出：{ip: mac}，跳过不完整条目。

    真实输出形如：
        接口: 192.168.1.111 --- 0x5
          Internet 地址      物理地址            类型
          192.168.1.2         5a-42-70-53-b0-5c    动态
    """
    rows = {}
    for line in (text or "").splitlines():
        match = re.match(r"\s*(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})", line)
        if not match:
            continue
        rows.setdefault(match.group(1), match.group(2).replace("-", ":").lower())
    return rows


def parse_netsh_ssid(text):
    """解析 `netsh wlan show interfaces`：接口名 -> {ssid, signal}。

    真实输出形如：
        ...  接口名称: WLAN ...
        SSID                  : MyWiFi
        信号                  : 90%
    """
    result, current = {}, None
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key.endswith("接口名称") or key == "Interface Name":
            current = value
            result.setdefault(current, {"ssid": None, "signal": None})
        elif current is None:
            continue
        elif key in ("SSID", "SSID 名称") and value:
            result[current]["ssid"] = value
        elif key in ("信号", "Signal") and value.endswith("%"):
            digits = re.findall(r"\d+", value)
            result[current]["signal"] = int(digits[0]) if digits else None
    return result


def parse_ping_alive(returncode):
    """ping 的返回码：0 = 存活。Windows 用 `-n` 次数与 `-w` 毫秒，不是 Linux 的 `-c`/`-W`。"""
    return returncode == 0


def parse_pnp_devices(payload, kind="device"):
    """把 Get-PnpDevice 的 JSON 结果转成统一的条目列表。

    Get-PnpDevice 对根集线器/控制器同样有输出，Windows 侧没有 Linux 那种
    「1d6b 厂商 ID」的稳定判定，因此保留名称与原始终止，标记 hub 为 None。
    """
    rows = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        name = (item.get("FriendlyName") or item.get("Name") or "").strip()
        instance = (item.get("InstanceId") or "").strip()
        if not name and not instance:
            continue
        rows.append({
            "name": name or instance,
            "vendor": None,
            "product": name or None,
            "id": instance,
            "status": (item.get("Status") or "").strip(),
            "kind": kind,
            "hub": None,
        })
    rows.sort(key=lambda row: (row["name"] == "", row["name"]))
    return rows


def parse_services(payload):
    """把 Get-Service 的 JSON 结果转成与 systemd 列表同形的条目。"""
    rows = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        name = (item.get("Name") or "").strip()
        if not name:
            continue
        rows.append({"unit": name, "description": (item.get("DisplayName") or "").strip()})
    rows.sort(key=lambda row: row["unit"])
    return rows


def parse_wmi_instance(payload):
    """WMI 单实例字段：dict 或 list 均可，取第一个有值的。"""
    rows = payload if isinstance(payload, list) else [payload or {}]
    for row in rows:
        if isinstance(row, dict) and any(value not in (None, "") for value in row.values()):
            return row
    return rows[0] if rows else {}


# ---------------- Windows 采集（每个都可能降级） ----------------

def os_info():
    """操作系统名、版本、架构（platform 拿到架构，WSH/PowerShell 拿版本号）。"""
    info = {"os": None, "kernel": None, "arch": None}
    info["arch"] = __import__("platform").machine() or None
    payload = powershell_json(
        "Get-CimInstance Win32_OperatingSystem | "
        "Select-Object Caption,Version,BuildNumber,OSArchitecture | ConvertTo-Json -Compress")
    row = parse_wmi_instance(payload) if payload else {}
    caption = (row.get("Caption") or "").strip()
    version = (row.get("Version") or "").strip()
    build = (row.get("BuildNumber") or "").strip()
    arch = (row.get("OSArchitecture") or "").strip()
    if caption:
        info["os"] = f"{caption} {arch}".strip() if arch else caption
    elif version:
        info["os"] = f"Windows {version}"
    else:
        # WMI 拿不到时退回纯 platform：给出版本号，页面会显示数字版
        release, _version_name, _csd, _ptype = __import__("platform").win32_ver()
        info["os"] = f"Windows {release}" if release else None
    if version:
        info["kernel"] = f"{version} (build {build})" if build else version
    return info


def machine_info():
    """整机型号与 BIOS（Win32_ComputerSystem / Win32_BIOS）。"""
    system = powershell_json(
        "Get-CimInstance Win32_ComputerSystem | "
        "Select-Object Manufacturer,Model,SystemFamily | ConvertTo-Json -Compress")
    bios = powershell_json(
        "Get-CimInstance Win32_BIOS | Select-Object SMBIOSBIOSVersion,Manufacturer | "
        "ConvertTo-Json -Compress")
    machine = parse_wmi_instance(system) if system else {}
    firmware = parse_wmi_instance(bios) if bios else {}
    reason = None if machine or firmware else "WMI 查询失败（可能是系统限制或 PowerShell 不可用）"
    return {
        "vendor": (machine.get("Manufacturer") or "").strip() or None,
        "product": (machine.get("Model") or "").strip() or None,
        "board": (machine.get("SystemFamily") or "").strip() or None,
        "bios": (firmware.get("SMBIOSBIOSVersion") or "").strip() or None,
        "reason": reason,
    }


def cpu_summary():
    """CPU 型号与核心数：psutil 给核心数，Win32_Processor 给型号与频率。"""
    import platform

    import psutil
    payload = powershell_json(
        "Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,"
        "NumberOfLogicalProcessors,MaxClockSpeed | ConvertTo-Json -Compress")
    row = parse_wmi_instance(payload) if payload else {}
    model = (row.get("Name") or "").strip() or platform.processor() or None
    min_mhz = None
    try:
        freq = psutil.cpu_freq()
    except (NotImplementedError, OSError, RuntimeError):
        freq = None
    if freq:
        min_mhz = round(freq.min) if freq.min else None
        max_mhz = round(freq.max) if freq.max else None
    else:
        max_mhz = None
    if not max_mhz and row.get("MaxClockSpeed"):
        try:
            max_mhz = int(row["MaxClockSpeed"])
        except (TypeError, ValueError):
            max_mhz = None
    return {
        "model": model,
        "cores": psutil.cpu_count(logical=False),
        "threads": psutil.cpu_count(logical=True),
        "min_mhz": min_mhz,
        "max_mhz": max_mhz,
        "cache_text": None,
        "virtualization": None,
        "reason": None if (model or payload) else "WMI 查询失败",
    }


def disk_static():
    """磁盘型号/容量/机械还是固态（Win32_DiskDrive）。"""
    payload = powershell_json(
        "Get-CimInstance Win32_DiskDrive | Select-Object Model,Size,MediaType,"
        "InterfaceType,DeviceID | ConvertTo-Json -Compress")
    if not payload:
        return {"available": False, "reason": "WMI 查询失败（Win32_DiskDrive）"}
    rows = []
    for item in payload:
        name = (item.get("Model") or "").strip()
        if not name:
            continue
        size_gb = None
        try:
            size_gb = round(int(item.get("Size")) / 1024 ** 3, 1)
        except (TypeError, ValueError):
            size_gb = None
        media = (item.get("MediaType") or "").strip()
        rotational = None
        if media:
            rotational = "Fixed hard disk" in media or "Fixed hard disk media" in media
        rows.append({
            "device": (item.get("DeviceID") or "").strip() or None,
            "model": name,
            "size_gb": size_gb,
            "rotational": rotational,
            "bus": (item.get("InterfaceType") or "").strip() or None,
        })
    if not rows:
        return {"available": False, "reason": "WMI 没返回磁盘信息"}
    # 页面用「第一块盘」；有系统盘则优先
    system_drive = os.environ.get("SystemDrive", "C:")
    first = next((row for row in rows
                  if row["device"] and row["device"].lower().startswith(system_drive.lower())),
                 rows[0])
    first = dict(first)
    first.update({"available": True, "reason": None, "mounts": mounts_of_system()})
    return first


def mounts_of_system():
    """本机各盘容量（Windows 上盘符就是挂载点，型号交给上面的 disk_static）。"""
    import psutil
    rows = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except (OSError, PermissionError):
        partitions = []
    for part in partitions:
        if "cdrom" in (part.opts or "").lower() or part.fstype == "":
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (OSError, PermissionError):
            continue
        rows.append({"mount": part.mountpoint, "device": part.device,
                     "fstype": part.fstype,
                     "total_gb": round(usage.total / 1024 ** 3, 1),
                     "used_percent": round(usage.percent, 1),
                     "free_gb": round(usage.free / 1024 ** 3, 1)})
    rows.sort(key=lambda row: row["mount"])
    return rows


def usb_devices():
    """USB 设备（Get-PnpDevice -Class USB）。输出与 Linux 版同形。"""
    payload = powershell_json(
        "Get-PnpDevice -PresentOnly -Class USB | "
        "Select-Object FriendlyName,InstanceId,Status | ConvertTo-Json -Compress")
    if payload is None:
        return {"available": False, "reason": "PowerShell 查询失败（Get-PnpDevice）", "list": []}
    rows = []
    for item in payload:
        name = (item.get("FriendlyName") or "").strip()
        instance = (item.get("InstanceId") or "").strip()
        if not name and not instance:
            continue
        # Windows 侧没有 Linux 的 1d6b 厂商 ID；根集线器用名字/InstanceId 识别
        hub = "ROOT_HUB" in instance.upper() or bool(re.search(r"(?i)root hub", name))
        rows.append({"id": instance or None, "vendor": None, "product": name or instance,
                     "bus": None, "device": None, "hub": hub})
    rows.sort(key=lambda row: (row["hub"], row["product"] or ""))
    return {"available": bool(rows), "list": rows, "total": len(rows),
            "external": sum(1 for row in rows if not row["hub"]),
            "reason": None if rows else "没有枚举到 USB 设备"}


def bluetooth_devices():
    """蓝牙适配器与已配对设备（Get-PnpDevice -Class Bluetooth）。"""
    payload = powershell_json(
        "Get-PnpDevice -PresentOnly -Class Bluetooth | "
        "Select-Object FriendlyName,InstanceId,Status | ConvertTo-Json -Compress")
    if payload is None:
        return {"available": False, "reason": "PowerShell 查询失败（Get-PnpDevice）",
                "adapters": [], "devices": []}
    devices, adapters = [], []
    for item in payload:
        name = (item.get("FriendlyName") or "").strip()
        instance = (item.get("InstanceId") or "").strip()
        if not name and not instance:
            continue
        # 有些 InstanceId 里带 MAC（形如 ..._AABBCCDDEEFF），能取就取
        mac_match = re.search(r"([0-9A-F]{12})$", instance.upper())
        mac = ":".join(mac_match.group(1)[i:i + 2] for i in (0, 2, 4, 6, 8, 10)) \
            if mac_match else None
        devices.append({"mac": mac, "name": name or instance})
        if re.search(r"(?i)bluetooth|蓝牙", name):
            adapters.append(name)
    return {"available": bool(devices), "adapters": adapters, "devices": devices,
            "reason": None if devices else "没有蓝牙设备/适配器"}


def windows_services(limit=80):
    """运行中的 Windows 服务（Get-Service）；页面上直接把 systemd 卡换成「系统服务」。"""
    payload = powershell_json(
        "Get-Service | Where-Object { $_.Status -eq 'Running' } | "
        "Select-Object Name,DisplayName | ConvertTo-Json -Compress")
    if payload is None:
        return {"available": False, "reason": "PowerShell 查询失败（Get-Service）"}
    rows = parse_services(payload)
    return {"available": True, "total": len(rows), "list": rows[:limit], "reason": None}


def arp_table():
    """邻居表：Windows 没有 /proc/net/arp，用 `arp -a`。"""
    try:
        done = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=ARP_TIMEOUT,
                              check=False)
    except FileNotFoundError:
        return {}
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_arp(done.stdout or "")


def ping(ip):
    """ICMP 探测：Windows 是 `-n 1 -w 1000`（次数 / 毫秒），与 Linux 的 `-c/-W` 相反。"""
    try:
        done = subprocess.run(["ping", "-n", "1", "-w", str(PING_TIMEOUT_MS), ip],
                              capture_output=True, timeout=5, check=False)
    except FileNotFoundError:
        raise
    except (OSError, subprocess.SubprocessError):
        return None
    return ip if parse_ping_alive(done.returncode) else None


def wireless_ssid(interface):
    """无线网卡的 SSID 与信号：netsh 免 root。"""
    try:
        done = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=6, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    by_name = parse_netsh_ssid(done.stdout or "")
    if not by_name:
        return None
    # 精确匹配；匹配不到时回落到唯一接口（Windows 网卡名带 "Wi-Fi" 而非 wlan0）
    entry = by_name.get(interface)
    if entry is None:
        entry = next(iter(by_name.values())) if len(by_name) == 1 else None
    if entry is None or not entry.get("ssid"):
        return None
    return {"ssid": entry["ssid"], "signal": entry.get("signal")}


def temps():
    return {"available": False,
            "reason": "Windows 暂不支持：温度需要 WMI MSAdapter 或 LibreHardwareMonitor 之类的驱动"}


def fans():
    return {"available": False,
            "reason": "Windows 暂不支持：风扇转速需要 WMI/LibreHardwareMonitor 之类的驱动"}


def power():
    return {"available": False,
            "reason": "Windows 暂不支持：RAPL 是 Intel 的 Linux 接口；整机实时功耗需要额外方案"}


def gpu():
    return {"available": False, "reason": "Windows 暂不支持：核显频率需通过专用接口读取"}


def runtime_versions():
    """运行环境：Python / psutil 跨平台；Docker Desktop 装了就报版本。"""
    import platform
    import psutil
    docker = None
    try:
        done = subprocess.run(["docker", "--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=4, check=False)
        if done.returncode == 0:
            match = re.search(r"(\d+\.\d+\.\d+)", done.stdout or done.stderr or "")
            docker = match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        docker = None
    return {"python": platform.python_version(), "psutil": psutil.__version__,
            "docker": docker,
            "reason": None if docker else "没有 docker 命令（Docker Desktop 未装或不在 PATH）"}


def collect_device_static():
    """汇总设备页的静态信息，字段与 Linux 版一致。"""
    os_release = os_info()
    machine = machine_info()
    cpu = cpu_summary()
    disk = disk_static()
    usb = usb_devices()
    bluetooth = bluetooth_devices()
    return {
        "summary": {
            "os": os_release.get("os"),
            "kernel": os_release.get("kernel"),
            "arch": os_release.get("arch"),
            "vendor": machine.get("vendor"),
            "product": machine.get("product"),
            "board": machine.get("board"),
            "bios": machine.get("bios"),
            "cpu": cpu.get("model"),
            "cores": cpu.get("cores"),
            "threads": cpu.get("threads"),
            "min_mhz": cpu.get("min_mhz"),
            "max_mhz": cpu.get("max_mhz"),
            "cache_text": cpu.get("cache_text"),
            "virtualization": cpu.get("virtualization"),
            "reason": machine.get("reason"),
        },
        "disk": disk,
        "usb": usb,
        "bluetooth": bluetooth,
    }


def gateway_address():
    """默认网关：Windows 上用 route print 0.0.0.0，拿不到就 None。"""

    for command in (["route", "print", "0.0.0.0"], ["netstat", "-nr"]):
        try:
            done = subprocess.run(command, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=6, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in (done.stdout or "").splitlines():
            match = re.match(r"\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d{1,3}(?:\.\d{1,3}){3})", line)
            if match:
                return match.group(1)
    return None


def hostname():
    return socket.gethostname()
