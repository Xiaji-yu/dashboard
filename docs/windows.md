# Windows 移植说明

看板用 `psutil` + Python 标准库实现，**核心指标（CPU/内存/磁盘/网络/进程/端口/电池/鉴权/前端）在
Windows 上原样可用**；Linux 专有接口（`/proc`、`/sys`、`systemctl`、`bluetoothctl`）在
`platform_win.py` 里用 **PowerShell(ConvertTo-Json) + netsh + arp** 做了等价替换。
前端零改动——同一套载荷结构。

## 装机

```powershell
# 1) Python 3.9+（勾选 Add python.exe to PATH）
python -m pip install psutil

# 2) 先手动跑起来看看
cd C:\path\\to\dashboard
.\run.ps1 fg                 # Ctrl+C 结束；启动日志里会打印首次的随机账号密码
```

启动日志里那行就是初始凭据：

```
[auth] 首次启动，已生成初始账号：admin / xxxxxxxxxxxxxxxxxxxx
[auth] 凭据文件：C:\path\to\dashboard\auth.json
```

**登录后立刻在侧栏「账号」里改密码。**

## 三种运行方式

| 方式 | 命令 | 适用 |
| --- | --- | --- |
| 前台 | `.\run.ps1 fg` | 先跑通、看日志 |
| 后台 | `.\run.ps1 start` / `stop` / `status` / `log 50` | 临时用 |
| 开机自启 | `cd deploy; .\install-windows.ps1`（管理员 PowerShell） | 长期跑 |

装成服务用系统自带 `sc.exe`（不需要 NSSM），并配置了失败重启。默认以 **LocalSystem** 运行——
等价 Linux 的 root：能读到全部进程与端口归属。想降权用
`.\install-windows.ps1 -User ".\普通用户"`（会要求密码，并需要「作为服务登录」权限）。

## 能力对照表

| 页面/指标 | Windows 下的来源 | 状态 |
| --- | --- | --- |
| CPU / 每核占用 / 频率 | psutil | ✅ 频率范围来自 WMI `MaxClockSpeed` |
| 内存 / 交换 | psutil | ✅ |
| 磁盘容量 / 读写速率 / 分区 | psutil + WMI `Win32_DiskDrive`（型号、容量、机械/固态） | ✅ 型号与容量是真的 |
| 网卡 / 网速 / 连接 / 监听端口归属 | psutil | ✅（非管理员只能映射自己的进程，与 Linux 非 root 同） |
| 进程表 / 容器归属 | psutil | ✅ 容器列显示「—」（Windows 无 cgroup） |
| 电池 | psutil `sensors_battery` | ✅ 循环次数/放电功率「不可用」 |
| 功耗（原 RAPL） | — | ⚠️ **不可用**：RAPL 是 Linux 接口；整机实时功耗需要额外方案 |
| 温度 / 风扇 | — | ⚠️ **不可用**：需要 WMI `MSAcpi_ThermalZoneTemperature`（多数机器不可读）或 LibreHardwareMonitor |
| 核显频率 | — | ⚠️ **不可用** |
| USB 设备 | PowerShell `Get-PnpDevice -Class USB` | ✅ 真实设备名；根集线器按名称识别 |
| 蓝牙 | PowerShell `Get-PnpDevice -Class Bluetooth` | ✅ 适配器 + 已配对设备（MAC 尽量从 InstanceId 提取） |
| 局域网设备 | `arp -a` + `ping -n 1 -w 1000` + SSDP 组播 | ✅ 邻居表来源换成 `arp -a`；ping 参数换成 Windows 的 `-n/-w` |
| 系统服务（原 systemd） | PowerShell `Get-Service` | ✅ 列表同形，前端无需改动 |
| 容器 | `docker ps`（Docker Desktop 装了且 in PATH 就可用） | ✅ / 未装则不可用 |
| 鉴权 / 只读 API Token | 纯 Python | ✅ 与 Linux 完全一致 |

> 说明：上表标「不可用」的项在前端会显示「不可用」+ 原因，不会造假；这是本项目一贯的降级契约。

## 我想请你在 Windows 上帮我验证

前提：`python -m pip install psutil` 已装好（缺它会直接 `ModuleNotFoundError`，
现在导入 `platform_win` 时会给出这句人话提示）。

我在 Linux 上没有 Windows 机器，**以下都是未实测的**。麻烦在有 Windows 的机器上跑一遍，把
输出发我（直接贴文本即可）：

```powershell
# A. 解析器层（不需要启动服务）
python -c "import platform_win as w; print(w.parse_arp(open('nul','r') and __import__('subprocess').run(['arp','-a'],capture_output=True,text=True).stdout))"
python -c "import platform_win as w, subprocess; print(w.parse_netsh_ssid(subprocess.run(['netsh','wlan','show','interfaces'],capture_output=True,text=True).stdout or ''))"

# B. 采集层（会真正调 PowerShell / netsh / arp）
python -c "import platform_win as w, json; print(json.dumps(w.collect_device_static(), ensure_ascii=False, indent=1))"

# C. 局域网
python -c "import collector; c=collector.Collector(); print(c._local_subnet()); print('邻居:', c._neighbors())"

# D. 整机
python -c "import collector; c=collector.Collector(); import json; print(json.dumps(c.device_info(), ensure_ascii=False)[:2000])"
```

重点想确认三件事：

1. `powershell.exe` 能否用 `-NoProfile -NonInteractive -Command` 正常返回 `ConvertTo-Json`
   （有些机器会因执行策略/企业策略被拦）；
2. `Get-PnpDevice -Class USB / Bluetooth`、`Get-CimInstance Win32_*` 是否返回数据
   （家庭版通常可以；精简版/企业管控机可能没有）；
3. `arp -a` 与 `ping -n 1 -w 1000` 的输出格式是否符合 `platform_win.py` 里解析器的预期
   （中文系统与英文系统的表头不同，我按两种都写了）。

## 代码位置

- `platform_win.py`：Windows 专有采集（纯解析函数 + 子进程封装，可在 Linux 上单测）
- `collector.py`：`if IS_WINDOWS:` 分派点（共 15 处，Linux 原样不动）
- `tests/test_windows_port.py`：解析器回归测试（CI 在 Linux 上也能跑）
- `run.ps1`、`deploy/install-windows.ps1`：启动与装机
- Linux 的 `run.sh` / `deploy/dashboard.service` 不受影响

## 已知取舍

- **不加第三方依赖**：Windows 主机信息走 PowerShell 而不是 `pywin32`/`WMI` 包，保持与 Linux 版一样
  「一条 `pip install psutil` 就能跑」。代价是每次查询要起一个 PowerShell 进程（约 0.2~1 秒），
  但设备页本来就有 60 秒缓存，且服务页/系统服务也是 PowerShell（Get-Service，几十毫秒）。
  如果你更想要常驻低延迟，可以换 `pywin32`，那是另一个取舍（要我改成这样就可以说）。
- **服务页的「系统服务」**：Linux 上是 systemd 单元，Windows 上换成 Windows 服务列表，
  前端标题已按平台文案处理，字段结构一致。
