#!/usr/bin/env python3
"""从真实的接口响应生成 docs/API.md。

字段表完全由实际 payload 推导，避免手写文档与实现漂移：
    1. 起一个看板（或直接用本机在跑的那个）；
    2. python3 tools/gen_api_doc.py --url http://127.0.0.1:8282 --user admin --password '...'
    3. 输出覆盖 docs/API.md

测试 tests/test_api_docs.py 会反向校验：文档里写到的每个字段都必须真的存在。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

ENDPOINTS = [
    ("/api/overview", "GET /api/overview",
     "一屏所需的全部瞬时指标（含最忙进程前 6 条、服务摘要）", "2 秒"),
    ("/api/performance", "GET /api/performance",
     "性能与电源页：每核/温度/风扇/核显/电池/内存/负载/功耗", "1–2 秒"),
    ("/api/network", "GET /api/network",
     "网卡、连接概况、网关/外网延迟、磁盘读写", "2 秒"),
    ("/api/device", "GET /api/device",
     "主机摘要、USB/蓝牙/网络接口/局域网设备、运行环境", "30 秒（后端缓存 60 秒）"),
    ("/api/services", "GET /api/services",
     "容器、systemd 服务、监听端口、远程探测结果", "5–10 秒"),
    ("/api/processes", "GET /api/processes", "完整进程表（约 300 行，**载荷大**）", "按需"),
]

DESC = {
    "ts": "服务器时间戳（Unix 秒，浮点）",
    "ready": "采样线程是否已产出第一份快照；false 时其余字段可能为空",
    "interval": "采样间隔（秒）",
    "window": "曲线窗口长度（秒）",
    "host": "主机名", "cores": "逻辑核心数", "uptime_s": "开机时长（秒）",
    "available": "该指标是否可采；false 时只有 reason，其余字段可能缺失",
    "reason": "不可用原因（中文，可直接展示）",
    "cpu.percent": "CPU 总占用率（0–100，按逻辑核心数归一化）",
    "cpu.freq_mhz": "当前平均频率（MHz）",
    "cpu.per_core.available": "每核数据是否可用",
    "cpu.per_core.per_cpu": "每个逻辑核心的占用率（0–100）",
    "cpu.per_core.freq_mhz": "每个逻辑核心的频率（MHz）",
    "cpu.per_core.topology": "每个逻辑核心所属物理核编号（同值 = 同物理核的兄弟线程）",
    "memory.used_gb": "已用内存（GB）= 总量 − 空闲 − buffers − cached",
    "memory.total_gb": "内存总量（GB）", "memory.percent": "内存占用率（0–100）",
    "memory.free_gb": "空闲（GB）", "memory.buffers_gb": "内核缓冲（GB）",
    "memory.cached_gb": "页缓存（GB）", "memory.shared_gb": "共享内存（GB）",
    "memory.swap_total_gb": "交换区总量（GB）", "memory.swap_used_gb": "交换区已用（GB）",
    "power.watts": "功耗（瓦）；主值优先取最接近整机的域",
    "power.source": "主值来自哪个域（如「CPU 封装」）",
    "power.domains": "各 RAPL 域的明细",
    "power.domains[].name": "域 ID（package-0/core/uncore/dram/psys）",
    "power.domains[].label": "域的中文名", "power.domains[].watts": "该域功耗（瓦）",
    "power.skipped": "读不到而跳过的域 ID 列表",
    "net.nic": "统计的网卡名", "net.down_bps": "下行速率（字节/秒）",
    "net.up_bps": "上行速率（字节/秒）",
    "disk.path": "统计的挂载点", "disk.free_gb": "剩余空间（GB）",
    "disk.total_gb": "该挂载点总容量（GB）", "disk.used_percent": "已用百分比（0–100）",
    "disk.mounts[].mount": "挂载点", "disk.mounts[].device": "设备路径",
    "disk.mounts[].fstype": "文件系统类型", "disk.mounts[].total_gb": "容量（GB）",
    "disk.mounts[].used_percent": "已用百分比", "disk.mounts[].free_gb": "剩余（GB）",
    "disk.device": "根挂载对应的设备", "disk.block": "块设备名（如 sda）",
    "disk.model": "磁盘型号", "disk.size_gb": "磁盘容量（GB）",
    "disk.rotational": "true=机械盘，false=固态", "disk.bus": "总线类型（多为 null）",
    "disk.read_bps": "读取速率（字节/秒，差分）", "disk.write_bps": "写入速率（字节/秒，差分）",
    "disk.read_total_gb": "累计读取（GB）", "disk.write_total_gb": "累计写入（GB）",
    "temp.celsius": "温度（摄氏度）", "temp.source": "温度来源通道",
    "load.avg1": "1 分钟平均负载", "load.avg5": "5 分钟平均负载",
    "load.avg15": "15 分钟平均负载",
    "performance.cpu.percent": "同 cpu.percent（性能页复用）",
    "performance.gpu.card": "核显设备节点", "performance.gpu.freq_mhz": "核显当前频率（MHz）",
    "performance.gpu.max_mhz": "核显最大频率（MHz）",
    "performance.temps.list": "全部温度通道",
    "performance.temps.list[].key": "通道稳定 ID（如 acpi、coretemp/core0）",
    "performance.temps.list[].label": "通道中文名",
    "performance.temps.list[].celsius": "温度（摄氏度）",
    "performance.fans.list": "全部风扇",
    "performance.fans.list[].key": "风扇稳定 ID（如 cpu_fan/gpu_fan）",
    "performance.fans.list[].label": "风扇中文名",
    "performance.fans.list[].rpm": "转速（RPM，0 = 停转）",
    "performance.battery.percent": "电量（%）", "performance.battery.plugged": "是否接着电源",
    "performance.battery.status": "厂商状态字符串（Charging/Discharging/Not charging/Full）",
    "performance.battery.cycles": "循环次数",
    "performance.battery.power_w": "瞬时功率（瓦，可能为 null）",
    "performance.battery.secsleft": "预计剩余秒数（可能为 null）",
    "processes[]": "最忙的若干进程（概览只给前 6 条，完整表在 /api/processes）",
    "processes[].pid": "进程号", "processes[].name": "进程名", "processes[].user": "所属用户",
    "processes[].cpu": "CPU 占用率（0–100）", "processes[].rss_mb": "常驻内存（MB）",
    "processes[].status": "状态（running/sleeping/…）", "processes[].threads": "线程数",
    "processes[].started": "启动时间（Unix 秒）", "processes[].cmd": "命令行",
    "processes[].container": "所属容器名（非容器进程为 null）",
    "process_count": "进程总数",
    "services[]": "关键服务状态摘要（容器/Docker/SSH/端口/散热/负载）",
    "services[].group": "分组（容器/本机/健康）", "services[].name": "服务名",
    "services[].status": "ok/warn/down/unknown", "services[].detail": "一句话状态",
    "services[].groupNote": "分组补充说明；没有就是 null",
    "nic.available": "网卡信息是否可用", "nic.name": "网卡名", "nic.wireless": "是否无线网卡",
    "nic.up": "链路是否 up", "nic.speed_mbps": "协商速率（Mbps）",
    "nic.duplex": "双工模式", "nic.mtu": "MTU",
    "nic.ipv4": "IPv4 地址", "nic.netmask": "子网掩码",
    "nic.ipv6": "IPv6 地址（链路本地）",
    "nic.mac": "MAC 地址", "nic.recv_total_gb": "累计接收（GB）",
    "nic.sent_total_gb": "累计发送（GB）",
    "nic.dropin": "接收丢包计数", "nic.dropout": "发送丢包计数",
    "connection.total": "连接总数", "connection.established": "ESTABLISHED 数量",
    "connection.connection_listening": "监听套接字数量",
    "connection.remotes": "连接数最多的对端",
    "connection.remotes[].addr": "对端地址:端口", "connection.remotes[].count": "连接数",
    "connection.process_attribution": "连接是否能关联到进程（非 root 常为 false）",
    "connection.local_ip": "本机 IP", "connection.gateway": "默认网关",
    "connection.medium": "介质（有线/无线）",
    "connection.gateway_ms": "网关 TCP 握手耗时（ms）",
    "connection.internet_ms": "外网 TCP 握手耗时（ms）",
    "connection.internet_target": "外网探测目标 host:port",
    "connection.proxy_port": "本机常见代理端口（无则 null）",
    "connection.listening": "监听端口总数",
    "summary.hostname": "主机名", "summary.os": "操作系统", "summary.kernel": "内核版本",
    "summary.arch": "架构", "summary.uptime_s": "开机时长（秒）",
    "summary.vendor": "整机厂商（DMI）", "summary.product": "整机型号",
    "summary.bios": "BIOS 版本",
    "summary.cpu": "CPU 型号", "summary.cores": "物理核数", "summary.threads": "逻辑核数",
    "summary.cache_text": "各级缓存汇总（人类可读）",
    "summary.virtualization": "虚拟化能力（VT-x/AMD-V）",
    "summary.memory_gb": "内存总量（GB）", "summary.swap_gb": "交换区总量（GB）",
    "summary.gpu": "核显最大频率（MHz）",
    "usb.list": "USB 设备列表", "usb.total": "USB 设备总数",
    "usb.external": "外接设备数（不含根集线器）",
    "usb.list[].id": "厂商:产品 ID（如 0b95:772a）",
    "usb.list[].vendor": "厂商名", "usb.list[].product": "产品名",
    "usb.list[].bus": "总线号", "usb.list[].device": "设备号",
    "usb.list[].hub": "true = 根集线器（控制器本身，非外接设备）",
    "bluetooth.available": "是否有蓝牙适配器",
    "bluetooth.adapters": "适配器列表（如 hci0）",
    "bluetooth.devices": "已配对设备", "bluetooth.devices[].mac": "设备 MAC",
    "bluetooth.devices[].name": "设备名",
    "interfaces.physical": "物理与无线接口",
    "interfaces.virtual": "虚拟接口（docker/网桥/veth）",
    "interfaces.physical[].name": "接口名",
    "interfaces.physical[].kind": "类型（有线/无线/回环/虚拟）",
    "interfaces.physical[].up": "是否 up",
    "interfaces.physical[].speed_mbps": "速率（Mbps）",
    "interfaces.physical[].mtu": "MTU", "interfaces.physical[].ipv4": "IPv4（可能缺失）",
    "interfaces.physical[].ipv6": "IPv6（可能缺失）",
    "interfaces.physical[].mac": "MAC",
    "interfaces.physical[].wireless": "无线接口的 SSID/信号等；非无线接口为 null",
    "lan.hosts": "发现的局域网设备", "lan.subnet": "扫描网段（如 192.168.1.0/24）",
    "lan.scanned_at": "上次扫描完成时间（Unix 秒，未扫过为 null）",
    "lan.scan_seconds": "上次扫描耗时（秒）", "lan.swept": "上次扫描的地址数",
    "lan.live": "上次扫描存活数", "lan.note": "扫描降级说明（如没有 ping 命令）",
    "lan.oui": "OUI 厂商库是否可用",
    "lan.hosts[].ip": "设备 IP", "lan.hosts[].alive": "ICMP 是否存活（null = 仅邻居表）",
    "lan.hosts[].name": "反向 DNS 名称（可能为 null）",
    "lan.hosts[].mac": "MAC（可能为 null）",
    "lan.hosts[].sources": "发现来源（icmp/arp/ssdp）",
    "lan.hosts[].vendor": "MAC 厂商（查不到为 null）",
    "containers.list": "容器列表", "containers.note": "降级说明（如没有 docker 命令）",
    "containers.total": "容器总数", "containers.running": "运行中数量",
    "containers.list[].name": "容器名", "containers.list[].up": "是否运行中",
    "containers.list[].status": "docker 的状态字符串", "containers.list[].image": "镜像名",
    "systemd.available": "systemctl 是否可用", "systemd.total": "运行中的服务数",
    "systemd.list": "运行中的服务列表", "systemd.list[].unit": "单元名",
    "systemd.list[].description": "服务描述",
    "ports": "监听端口表", "ports[].port": "端口号", "ports[].proto": "协议（tcp/tcp6/udp）",
    "ports[].addr": "绑定地址", "ports[].scope": "访问范围（局域网/仅本机）",
    "ports[].process": "占用进程名（非 root 常为 null）",
    "ports[].known": "IANA 惯例用途（如 SSH）",
    "probes.list": "探测目标与最近一次握手耗时",
    "probes.list[].name": "目标名", "probes.list[].host": "主机",
    "probes.list[].port": "端口",
    "probes.list[].ms": "TCP 握手耗时（ms，失败为 null）",
    "probes.error": "probes.json 配置问题（无问题为 null）",
    "probes.path": "配置文件路径", "probes.interval": "探测间隔（秒）",
    "count": "进程总数",
}

UNIT_SUFFIX = {"_gb": "GB", "_bps": "字节/秒", "_ms": "毫秒", "_mhz": "MHz", "_s": "秒",
               "celsius": "°C", "rpm": "RPM", "watts": "瓦", "percent": "%"}

HEAD = """# API 参考

看板的数据全部通过 HTTP JSON 接口暴露，与页面同一个进程、同一个端口，**没有单独的 API 端口**
（默认 `http://<主机>:8282`）。除登录相关接口外都需要先登录（见下）。

约定：

- 时间戳统一是 **Unix 秒（浮点）**；速率单位是 **字节/秒**（页面上显示时才换算成 KB/s、Mb/s）。
- 容量单位是 **GB**（GiB，1024 进制）。
- 每个指标段都可能带 `available: false` + `reason`（中文），表示这台机器采不到（缺传感器、缺权限、没有 docker 等）。
  **取数时先看 `available`**；false 时其余字段可能不存在，别把缺字段当成 0。
- 顶层都带 `ready`：采样线程还没产出第一份快照时为 false（服务刚启动的一两秒内）。

## 鉴权

接口与页面共用一套会话（Cookie）。机器调用建议：

```bash
# 1) 登录并保存会话 Cookie（有效期 7 天）
curl -s -c cookies.txt -X POST -H 'Content-Type: application/json' \\
  -d '{"username":"admin","password":"你的密码"}' \\
  http://127.0.0.1:8282/api/login
# => {"ok": true, "username": "admin"}

# 2) 之后带上 Cookie
curl -s -b cookies.txt http://127.0.0.1:8282/api/overview
```

- 未登录访问接口：`401 {"error": "unauthorized"}`（页面会跳登录页）。
- 会话过期、或改密码导致被踢时，重新登录即可。
- 登录失败按来源 IP 限速（5 分钟 5 次，超限 `429` 且带 `Retry-After`）。
- **放在反向代理后面**要给看板设 `DASHBOARD_TRUST_PROXY=1`，否则限速会把所有客户端算成代理一个 IP。
- 状态变更接口（登录/退出/改密码）要求 `Content-Type: application/json` 且同源（挡 CSRF）。

| 接口 | 说明 |
| --- | --- |
| `POST /api/login` | 登录，JSON `{"username","password"}`，成功下发会话 Cookie |
| `POST /api/logout` | 退出登录；`{"all": true}` 退出全部设备 |
| `GET /api/auth` | 当前登录状态（**无需登录**）：`{authenticated, username, host}` |
| `POST /api/password` | 改密码/用户名：`{old_password, new_password?, new_username?}` |

### 只读 API Token（推荐给 Bot / 脚本）

不想让 bot 保存账号密码、也不想维护 Cookie 会话，就发一个**只读令牌**：

- 生成：页面侧栏「账号」→「新建只读令牌」（**完整令牌只显示这一次**）；
  或在启动时用环境变量 `DASHBOARD_API_TOKEN` 播种（**仅在还没有任何令牌时生效**）。
- 携带方式二选一：

  ```bash
  curl -H "Authorization: Bearer dshk_xxx" http://127.0.0.1:8282/api/overview
  curl "http://127.0.0.1:8282/api/series?keys=cpu&token=dshk_xxx"
  ```

- **只能读**：令牌访问任何写接口（改密码 / 退出 / 管理令牌）都返回 `403 {"error":"read_only"}`；
  也不能用令牌创建或撤销令牌——管理令牌必须用页面会话。
- 存储：服务端只保存令牌的 SHA-256（256 位随机量，没有可猜分布，所以不需要慢哈希），
  明文只在创建时返回一次。
- 撤销即时生效；`/api/auth` 会如实回报当前身份（`kind: "session"` 或 `"token"` + `token_name`）。
- 令牌出现在 URL 里会被日志脱敏成 `token=<redacted>`，但仍建议优先用 `Authorization` 头。

| 接口 | 说明 |
| --- | --- |
| `GET /api/tokens` | 列出只读令牌（仅页面会话可用） |
| `POST /api/tokens` | 新建令牌：`{name}` → `{token, record}`，`token` 只返回这一次 |
| `POST /api/tokens/revoke` | 撤销令牌：`{id}` |

## 接口总览

| 接口 | 用途 | 建议轮询 |
| --- | --- | --- |
"""

SERIES = """
## `GET /api/series`

曲线数据。每个 key 返回 `[[时间戳, 值], ...]`，值为 `null` 表示那一刻没采到
（前端据此断线，不会连成直线）。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `keys` | 全部 | 逗号分隔，只取需要的序列（**建议只取要用的**）；未知 key 返回 400 |
| `since` | 0 | 只返回时间戳 **大于** `since` 的点；轮询时传上次响应的 `ts` 即增量 |

响应：

```json
{
  "ts": 1790025695.48,
  "window": 120.0,
  "interval": 1.0,
  "series": {
    "cpu": [[1790025694.48, 31.4], [1790025695.48, 15.4]],
    "mem_used": [[1790025694.48, 4.2], [1790025695.48, 4.2]]
  }
}
```

可用 key（`cpu0`…`cpuN`、`cpu_max`、`temp_acpi`、`fan_cpu`、`gpu_mhz`
只在对应指标可采时才有点）：

| key | 单位 | 说明 |
| --- | --- | --- |
| `cpu` | % | CPU 总占用 |
| `mem_used` | GB | 已用内存 |
| `power` | 瓦 | 功耗（RAPL） |
| `net_down` / `net_up` | 字节/秒 | 下行 / 上行速率 |
| `disk_free` | GB | 剩余空间 |
| `disk_read` / `disk_write` | 字节/秒 | 磁盘读写速率 |
| `temp` | °C | 主温度通道 |
| `temp_acpi` | °C | 机身温区（ACPI） |
| `fan_cpu` | RPM | CPU 风扇转速 |
| `gpu_mhz` | MHz | 核显频率 |
| `cpu0`…`cpuN` | % | 每个逻辑核心的占用 |
| `cpu_max` | % | 各核心占用的最大值 |

历史只存在内存里（最多 1200 点 ≈ 20 分钟），服务重启后重新累积。
"""

BOT = [x for x in ["""
## Bot 取数配方

**推荐先建一个只读令牌**，这样 bot 不用存账号密码、也不会因为改密码而被踢：

```python
import json
import urllib.request

BASE = "http://192.168.1.111:8282"
TOKEN = "dshk_你的只读令牌"


def get(path):
    request = urllib.request.Request(BASE + path,
                                     headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


ov = get("/api/overview")
print(f"CPU {ov['cpu']['percent']:.0f}%｜温度 {ov['temp']['celsius']:.0f}°C"
      f"｜功耗 {ov['power']['watts']:.1f} W")
```

（下面这段是**用账号密码 + Cookie 会话**的写法，适合需要写操作或不想额外发令牌的场景。）

**只取几个标量时，用 `/api/overview` 一次拿完**（CPU/内存/功耗/网速/磁盘/温度/负载/最忙进程/服务摘要都在里面），
不要为省流量去拼多个接口。

```python
import http.cookiejar
import json
import urllib.error
import urllib.request

BASE = "http://192.168.1.111:8282"
USER, PASSWORD = "admin", "你的密码"

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def login():
    body = json.dumps({"username": USER, "password": PASSWORD}).encode()
    req = urllib.request.Request(BASE + "/api/login", data=body,
                                 headers={"Content-Type": "application/json"})
    opener.open(req, timeout=10).read()


def get(path):
    # 带会话请求；401（过期或被踢）时重新登录一次
    try:
        return json.loads(opener.open(BASE + path, timeout=10).read())
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        login()
        return json.loads(opener.open(BASE + path, timeout=10).read())


login()
ov = get("/api/overview")
cpu, mem, net = ov["cpu"], ov["memory"], ov["net"]
temp, power, disk = ov["temp"], ov["power"], ov["disk"]
print(f"CPU {cpu['percent']:.0f}%｜内存 {mem['used_gb']:.1f}/{mem['total_gb']:.1f} GB"
      f"｜温度 {temp['celsius']:.0f}°C｜功耗 {power['watts']:.1f} W"
      f"｜网速 ↓{net['down_bps'] / 1024:.0f} KB/s ↑{net['up_bps'] / 1024:.0f} KB/s"
      f"｜磁盘剩余 {disk['free_gb']:.1f} GB")
```

增量画曲线（1 秒轮询，只取需要的序列）：

```python
import time

since, series = 0, {}
while True:
    data = get(f"/api/series?keys=cpu,mem_used,power&since={since}")
    since = data["ts"]
    for key, points in data["series"].items():
        series.setdefault(key, []).extend(points)
        del series[key][:-120]          # 只留最近 120 个点
    time.sleep(1)
```

几个坑：

- `/api/processes` 一次约 60–70 KB（本机 297 个进程），**别按秒轮询**；
  要常看就用 `/api/overview` 里的 `processes`（前 6 条）。
- `/api/device` 后端缓存 60 秒，轮询再快也不会更新；局域网扫描本身每 120 秒一次。
- `/api/network` 的 `connection.gateway_ms` / `internet_ms` 来自后台探测线程（每 10 秒一次），
  不是每次请求现测。
- 服务刚启动的 1–2 秒内 `ready` 为 false，先判断再取值。
- 页面上的「不可用」对应接口里的 `available: false` + `reason`。
"""] if x][0]


def fetch(url, cookie_path, path):
    request = urllib.request.Request(url + path)
    if cookie_path:
        request.add_header("Cookie", cookie_path)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def login(base, user, password):
    body = json.dumps({"username": user, "password": password}).encode("utf-8")
    request = urllib.request.Request(base + "/api/login", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.headers.get("Set-Cookie") or ""
        return raw.split(";")[0]


def walk(node, prefix="", rows=None):
    """把 payload 摊平成 (路径, 类型, 示例) 列表；对象数组只展开第一个元素。"""
    rows = [] if rows is None else rows
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                walk(value, path, rows)
            elif isinstance(value, list):
                if value and isinstance(value[0], (dict, list)):
                    rows.append((path, f"array[{len(value)}]", value[0]))
                    walk(value[0], path + "[]", rows)
                else:
                    rows.append((path, f"array[{len(value)}]",
                                 value[0] if value else None))
            else:
                rows.append((path, type(value).__name__, value))
    return rows


def describe(path, kind):
    if path in DESC:
        return DESC[path]
    leaf = path.rsplit(".", 1)[-1]
    for suffix, unit in UNIT_SUFFIX.items():
        if leaf.endswith(suffix):
            return f"{leaf}（{unit}）"
    if leaf in ("mac", "ipv4", "ipv6"):
        return leaf.upper()
    if kind == "bool":
        return f"{leaf}（布尔）"
    if kind == "NoneType":
        return f"{leaf}（可能为 null）"
    return leaf


def field_table(rows):
    seen, lines = set(), []
    for name, kind, _sample in rows:
        if name in seen:
            continue
        seen.add(name)
        if kind.startswith("array") and not name.endswith("[]"):
            length = kind.split("[")[1].rstrip("]")
            has_objects = any(item[0] == name + "[]" for item in rows)
            kind_text = f"数组（{'对象' if has_objects else '标量'}，示例长度 {length}）"
        elif name.endswith("[]") and kind.startswith("array"):
            kind_text = "数组元素（对象）"
        else:
            kind_text = kind
        lines.append(f"| `{name}` | {kind_text} | {describe(name, kind)} |")
    return lines


def build(base, user, password):
    cookie = login(base, user, password)
    out = [HEAD]
    out.append("| 接口 | 用途 | 建议轮询 |\n| --- | --- | --- |")
    for path, _title, purpose, cadence in ENDPOINTS:
        out.append(f"| `{path}` | {purpose} | {cadence} |")
    out.append("")
    out.append(SERIES)
    for path, title, _purpose, _cadence in ENDPOINTS:
        payload = fetch(base, cookie, path)
        rows = walk(payload)
        out.append(f"\n## `{title}`\n")
        # 字段之间只有「、」没有空格，textwrap 断不开，这里按宽度手工换行
        lines, current = [], "顶层字段："
        for key in sorted(payload):
            part = f"`{key}`"
            candidate = current + (part if current.endswith("：") else "、" + part)
            if len(candidate) > 108:
                lines.append(current + "、")
                current = "  " + part
            else:
                current = candidate
        lines.append(current)
        out.append("\n".join(lines) + "\n")
        out.append("| 字段 | 类型 | 说明 |\n| --- | --- | --- |")
        out.extend(field_table(rows))
        out.append("")
    out.append(BOT)
    out.append("")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="从真实响应生成 docs/API.md")
    parser.add_argument("--url", default="http://127.0.0.1:8282")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", required=False)
    parser.add_argument("--out", default="docs/API.md")
    args = parser.parse_args()
    if not args.password:
        parser.error("需要 --password（用来看板自己的账号密码）")
    text = build(args.url.rstrip("/"), args.user, args.password)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"已写入 {args.out}（{len(text.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
