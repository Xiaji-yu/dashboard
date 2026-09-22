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

import base64
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
    return interpret_output(done.returncode, done.stdout, done.stderr)


# PowerShell 报「找不到命令/参数」时的特征串（中英文都覆盖）
MISSING_COMMAND_HINTS = ("CommandNotFoundException", "is not recognized",
                         "无法将", "不是内部或外部命令", "未被识别为", "找不到命令",
                         "找不到与参数名称匹配", "A parameter cannot be found")


def interpret_output(returncode, stdout, stderr):
    """子进程结果 -> 文本；None 表示这次查询失败（调用方降级为「不可用」）。

    关键区别（Linux 版也是这个契约）：
    - 返回空字符串 = **命令成功但没有对象**，例如机器没有蓝牙适配器；
    - 返回 None = 命令失败，例如没有 Get-PnpDevice（家庭版/被管控的机器）。
    「没输出 + 有报错」按失败处理，否则会把缺命令误报成「没有这类设备」。
    """
    if returncode != 0:
        return None
    stdout = stdout or ""
    error_text = stderr or ""
    if not stdout.strip() and any(hint in error_text for hint in MISSING_COMMAND_HINTS):
        return None
    return stdout


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
    """跑 PowerShell 并解析 ConvertTo-Json 输出。

    输出用 base64 传回：Windows PowerShell 5.1 在重定向时按 **UTF-16LE** 输出（无 BOM），
    直接按 UTF-8 解码会把中文变成乱码（实测「专业工作站版」→「רҵ����վ��」）。
    转成 base64 后是纯 ASCII，与代码页、BOM、PS 版本都无关。

    返回 list（单条结果也包成 list）：
    - 命令失败 → None（调用方为「不可用」）
    - 成功但没有对象 → []（调用方为「没有这类设备」）
    """
    wrapped = ("[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("
               "(" + script + " | Out-String)))")
    raw = run_powershell(wrapped, timeout)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return []
    try:
        decoded = base64.b64decode(text).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not decoded.strip():
        return []
    try:
        payload = json.loads(decoded)
    except ValueError:
        return None
    if isinstance(payload, dict):
        return [payload]
    return payload if isinstance(payload, list) else None


# WMI 里代表「没填」的占位字符串（主板厂商常写 Default string）
PLACEHOLDER_VALUES = {"default string", "to be filled by o.e.m.", "system product name",
                      "system version", "none", "unknown", "o.e.m.", "not applicable",
                      "not specified", "填充由 o.e.m.", "默认字符串"}


def clean_text(value):
    """去掉空白与厂商占位串；拿不到就返回 None（前端显示「—」而不是假的型号）。"""
    text = (value or "").strip()
    if not text or text.lower() in PLACEHOLDER_VALUES:
        return None
    return text


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
        "vendor": clean_text(machine.get("Manufacturer")),
        "product": clean_text(machine.get("Model")),
        # 主板厂商常写 "Default string"：那是占位符，不是型号，按未知处理
        "board": clean_text(machine.get("SystemFamily")),
        "bios": clean_text(firmware.get("SMBIOSBIOSVersion")),
        "reason": reason,
    }


def cpu_summary():
    """CPU 型号与核心数：psutil 给核心数，Win32_Processor 给型号与频率。"""
    import platform

    import psutil
    payload = powershell_json(
        "Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,"
        "NumberOfLogicalProcessors,MaxClockSpeed,L2CacheSize,L3CacheSize,"
        "VirtualizationFirmwareEnabled | ConvertTo-Json -Compress")
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
        # Win32_Processor 的 L2/L3 单位是 KB，拼成与 Linux 版同风格的文本
        "cache_text": format_cpu_cache(row.get("L2CacheSize"), row.get("L3CacheSize")),
        # VirtualizationFirmwareEnabled 的语义是「固件里开着虚拟化」，
        # 只有为 True 时才有正面信息可报（False 通常只是没开 Hyper-V，不代表 CPU 不支持）
        "virtualization": "已启用" if row.get("VirtualizationFirmwareEnabled") is True else None,
        "reason": None if (model or payload) else "WMI 查询失败",
    }


# MSFT_PhysicalDisk.MediaType：3=HDD、4=SSD、5=SCM；其它值一律当作未知（不猜）
DISK_MEDIA_NUM = {3: True, 4: False, 5: None}
DISK_MEDIA_NAME = {"HDD": True, "SSD": False, "SCM": None,
                   "UNSPECIFIED": None, "UNKNOWN": None}
# MSFT_BusType：有把握的部分；其它值返回 None，宁可显示未知也不写错
DISK_BUS_NUM = {1: "SCSI", 3: "ATA", 4: "1394", 6: "FC", 7: "SAS", 8: "SATA",
                11: "虚拟", 14: "NVMe", 15: "SCM"}
DISK_BUS_NAME = {"SCSI": "SCSI", "ATA": "ATA", "SATA": "SATA", "SAS": "SAS", "NVME": "NVMe",
                 "RAID": "RAID", "USB": "USB", "VIRTUAL": "虚拟", "FILEBACKEDVIRTUAL": "虚拟",
                 "STORAGESPACES": "存储空间", "SCM": "SCM", "FC": "FC", "1394": "1394",
                 "UNSPECIFIED": None, "UNKNOWN": None}


def enum_number(value):
    """把枚举值统一成整数：PowerShell 有时给数字、有时给数字字符串（拿不准返回 None）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        return int(text) if text.isdigit() else None
    return None


def disk_media(value):
    """MediaType -> 是否机械盘。数字与枚举名（SSD/HDD）都能认，认不出返回 None。"""
    number = enum_number(value)
    if number is not None and number in DISK_MEDIA_NUM:
        return DISK_MEDIA_NUM[number]
    if isinstance(value, str):
        return DISK_MEDIA_NAME.get(value.strip().upper())
    return None


def disk_bus(value):
    """BusType -> 可读总线名。数字与枚举名都能认，认不出返回 None。"""
    number = enum_number(value)
    if number is not None and number in DISK_BUS_NUM:
        return DISK_BUS_NUM[number]
    if isinstance(value, str):
        return DISK_BUS_NAME.get(value.strip().upper())
    return None


def to_mb(kb):
    """KB -> MB（Win32_Processor 的缓存单位是 KB）。"""
    try:
        value = int(kb)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    mb = value / 1024
    return int(mb) if mb == int(mb) else round(mb, 1)


def format_cpu_cache(l2_kb, l3_kb):
    """拼成与 Linux 版同风格的缓存文本，例如 L2 20 MB · L3 24 MB。"""
    parts = []
    for label, value in (("L2", l2_kb), ("L3", l3_kb)):
        mb = to_mb(value)
        if mb:
            parts.append(f"{label} {mb} MB")
    return " · ".join(parts) or None


def to_gb(value):
    try:
        return round(int(value) / 1024 ** 3, 1)
    except (TypeError, ValueError):
        return None


def drive_letter(mount):
    """把监控路径换成一个盘符；Windows 上 "/" 视为系统盘。"""
    match = re.match(r"([A-Za-z]):", (mount or "").strip())
    if match:
        return match.group(1).upper()
    system = os.environ.get("SystemDrive", "C:")
    return system[0].upper() if system else None


def disk_number_for_letter(letter):
    """盘符 -> 物理磁盘号（Get-Partition <盘符> | Get-Disk）。拿不到返回 None。"""
    if not letter:
        return None
    payload = powershell_json(
        f"Get-Partition -DriveLetter {letter} -ErrorAction SilentlyContinue | "
        "Get-Disk | Select-Object Number | ConvertTo-Json -Compress")
    if not payload:
        return None
    number = parse_wmi_instance(payload).get("Number")
    return number if isinstance(number, int) else None


def disk_rows_from_physical(payload):
    """Get-PhysicalDisk 的结果 -> 统一磁盘条目。

    实测（MSI MS-7D99 / NVMe）：`Get-Disk` 的 MediaType **全为空**，
    而 `Get-PhysicalDisk` 明确给出 SSD/NVMe——所以这个是主数据源。
    """
    rows = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        number = item.get("DeviceId")
        rows.append({
            "number": number if isinstance(number, int) else None,
            "device": f"磁盘 {number}" if number is not None else None,
            "model": clean_text(item.get("FriendlyName")),
            "size_gb": to_gb(item.get("Size")),
            "rotational": disk_media(item.get("MediaType")),
            "bus": disk_bus(item.get("BusType")),
        })
    return [row for row in rows if row["model"] or row["size_gb"]]


def disk_rows_from_storage(payload):
    """Get-Disk 的结果 -> 统一磁盘条目。"""
    rows = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        number = item.get("Number")
        rows.append({
            "number": number if isinstance(number, int) else None,
            "device": f"磁盘 {number}" if number is not None else None,
            "model": clean_text(item.get("FriendlyName")),
            "size_gb": to_gb(item.get("Size")),
            "rotational": disk_media(item.get("MediaType")),
            "bus": disk_bus(item.get("BusType")),
        })
    return [row for row in rows if row["model"] or row["size_gb"]]


def disk_rows_from_wmi(payload):
    """Win32_DiskDrive 的结果 -> 统一磁盘条目；判不出机械/固态就留 None。"""
    rows = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("Model"))
        if not name:
            continue
        media = (item.get("MediaType") or "").strip()
        rotational = None
        if re.search(r"(?i)\bSSD\b", media) or "固态" in media:
            rotational = False
        # 注意："Fixed hard disk media" 是内建硬盘的泛化值，**SSD 也会报**（本机实测），
        # 它不含任何旋转信息，所以这里保持 None（未知），绝不据此断言是机械盘。
        # 真正的判据是存储模块的 MediaType（见 disk_static）。
        rows.append({
            "device": clean_text(item.get("DeviceID")),
            "model": name,
            "size_gb": to_gb(item.get("Size")),
            "rotational": rotational,
            "bus": clean_text(item.get("InterfaceType")),
        })
    return rows


def infer_rotational(model, bus, current):
    """MediaType 拿不到时的兜底判断——只用**定义性**证据，绝不瞎猜：

    - 型号/名称里写着 SSD（例如实测的 "SSD 1TB"）；
    - 挂在 NVMe 总线上（NVMe 只有 NAND，不可能有机械盘）。

    其它情况保持 None（未知），前端显示「—」，不会谎报成机械盘。
    """
    if current is not None:
        return current
    if re.search(r"(?i)\bSSD\b", model or ""):
        return False
    if (bus or "") == "NVMe":
        return False
    return None


def merge_same_number(row, rows, number):
    """用**同一块物理盘**的另一份数据补齐 rotational/bus。

    Get-PhysicalDisk 与 Get-Disk 的字段互补（实测前者有 MediaType、后者没有；
    反过来在别的机器上也可能），按磁盘号匹配后互相补空即可。
    """
    if number is None or not rows:
        return row
    for candidate in rows:
        if candidate.get("number") != number:
            continue
        if row.get("rotational") is None:
            row["rotational"] = candidate.get("rotational")
        if row.get("bus") is None:
            row["bus"] = candidate.get("bus")
        break
    return row


def disk_static(mount=None):
    """磁盘型号/容量/机械还是固态，优先取「监控盘所在的那块物理盘」。

    实测教训：`Win32_DiskDrive.MediaType` 在 MSI 这台机器上把 SSD 报成泛化的
    "Fixed hard disk media"，于是被误判为机械盘。存储模块的 `Get-Disk` 给出
    MediaType（3=HDD / 4=SSD）与 BusType（SATA/NVMe），所以优先用它。
    """
    letter = drive_letter(mount)
    number = disk_number_for_letter(letter)
    # 主数据源：Get-PhysicalDisk（MediaType/BusType 最全）
    rows = disk_rows_from_physical(powershell_json(
        "Get-PhysicalDisk -ErrorAction SilentlyContinue | Select-Object DeviceId,"
        "FriendlyName,Size,@{n='MediaType';e={$_.MediaType.ToString()}},"
        "@{n='BusType';e={$_.BusType.ToString()}} | ConvertTo-Json -Compress"))
    if not rows:
        # 回退一：Get-Disk（NVMe 上 MediaType 可能为空，但至少型号与容量可用）
        rows = disk_rows_from_storage(powershell_json(
            "Get-Disk -ErrorAction SilentlyContinue | "
            "Select-Object Number,FriendlyName,Size,"
            "@{n='MediaType';e={$_.MediaType.ToString()}},"
            "@{n='BusType';e={$_.BusType.ToString()}} | ConvertTo-Json -Compress"))
    if not rows:
        # 回退二：WMI
        rows = disk_rows_from_wmi(powershell_json(
            "Get-CimInstance Win32_DiskDrive | Select-Object Model,Size,MediaType,"
            "InterfaceType,DeviceID | ConvertTo-Json -Compress") or [])
    if not rows:
        return {"available": False, "reason": "拿不到磁盘信息（存储模块与 WMI 都失败）"}
    # 优先选监控盘所在的那块物理盘（拿不到盘号就用第一块）
    target = next((row for row in rows if number is not None and row.get("number") == number),
                  None) or rows[0]
    first = dict(target)
    if first.get("rotational") is None or first.get("bus") is None:
        # 主源缺字段时，用同号盘的另一份数据互补（Get-Disk 与 Get-PhysicalDisk 字段互补）
        first = merge_same_number(first, disk_rows_from_storage(powershell_json(
            "Get-Disk -ErrorAction SilentlyContinue | "
            "Select-Object Number,FriendlyName,Size,"
            "@{n='MediaType';e={$_.MediaType.ToString()}},"
            "@{n='BusType';e={$_.BusType.ToString()}} | ConvertTo-Json -Compress")), number)
    # 最后仍未知时，用型号/NVMe 这两条定义性证据兜底（绝不谎报机械盘）
    first["rotational"] = infer_rotational(first.get("model"), first.get("bus"),
                                           first.get("rotational"))
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


def filter_usb_instances(payload):
    """只保留真正挂在 USB 总线上的设备。

    `Get-PnpDevice -Class USB` 会连带列出「USB 控制器」这类 PCI 设备
    （实测有 Intel(R) USB 3.20 可扩展主机控制器，InstanceId 以 PCI\\ 开头），
    它们不是外接设备，混进列表会让人以为插了东西。
    """
    rows = []
    for item in payload or []:
        instance = (item.get("InstanceId") or "").strip().upper()
        if instance and not instance.startswith("USB\\"):
            continue
        rows.append(item)
    return rows


def is_usb_hub(name, instance):
    """USB 集线器判定。

    Windows 没有 Linux 的 1d6b 厂商 ID，按名字与 InstanceId 识别：
    根集线器（ROOT_HUB30）、以及「通用 USB 集线器」这类主板/机箱内的集线器。
    集线器不算「外接设备」，这样计数才等于真正插上去的东西。
    """
    return ("ROOT_HUB" in (instance or "").upper()
            or bool(re.search(r"(?i)root hub|集线器|\bhub\b", name or "")))


def usb_devices():
    """USB 设备（Get-PnpDevice -Class USB）。输出与 Linux 版同形。"""
    payload = powershell_json(
        "Get-PnpDevice -PresentOnly -Class USB -ErrorAction SilentlyContinue | "
        "Select-Object FriendlyName,InstanceId,Status | ConvertTo-Json -Compress")
    if payload is None:
        return {"available": False, "reason": "PowerShell 查询失败（Get-PnpDevice）", "list": []}
    rows = []
    for item in filter_usb_instances(payload):
        name = (item.get("FriendlyName") or "").strip()
        instance = (item.get("InstanceId") or "").strip()
        if not name and not instance:
            continue
        hub = is_usb_hub(name, instance)
        rows.append({"id": instance or None, "vendor": None, "product": name or instance,
                     "bus": None, "device": None, "hub": hub})
    rows.sort(key=lambda row: (row["hub"], row["product"] or ""))
    return {"available": bool(rows), "list": rows, "total": len(rows),
            "external": sum(1 for row in rows if not row["hub"]),
            "reason": None if rows else "没有枚举到 USB 设备"}


def bluetooth_devices():
    """蓝牙适配器与已配对设备（Get-PnpDevice -Class Bluetooth）。"""
    payload = powershell_json(
        "Get-PnpDevice -PresentOnly -Class Bluetooth -ErrorAction SilentlyContinue | "
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
            "reason": None if devices else "没有蓝牙适配器（设备管理器里也没有蓝牙类设备）"}


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
