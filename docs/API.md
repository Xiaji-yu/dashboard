# API 参考

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
curl -s -c cookies.txt -X POST -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"你的密码"}' \
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

| 接口 | 用途 | 建议轮询 |
| --- | --- | --- |
| `/api/overview` | 一屏所需的全部瞬时指标（含最忙进程前 6 条、服务摘要） | 2 秒 |
| `/api/performance` | 性能与电源页：每核/温度/风扇/核显/电池/内存/负载/功耗 | 1–2 秒 |
| `/api/network` | 网卡、连接概况、网关/外网延迟、磁盘读写 | 2 秒 |
| `/api/device` | 主机摘要、USB/蓝牙/网络接口/局域网设备、运行环境 | 30 秒（后端缓存 60 秒） |
| `/api/services` | 容器、systemd 服务、监听端口、远程探测结果 | 5–10 秒 |
| `/api/processes` | 完整进程表（约 300 行，**载荷大**） | 按需 |


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


## `GET /api/overview`

顶层字段：`cores`、`cpu`、`disk`、`host`、`interval`、`load`、`memory`、`net`、`performance`、`power`、`process_count`、
  `processes`、`ready`、`services`、`temp`、`ts`、`uptime_s`、`window`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ts` | float | 服务器时间戳（Unix 秒，浮点） |
| `host` | str | 主机名 |
| `cores` | int | 逻辑核心数 |
| `uptime_s` | float | 开机时长（秒） |
| `cpu.available` | bool | available（布尔） |
| `cpu.percent` | float | CPU 总占用率（0–100，按逻辑核心数归一化） |
| `cpu.freq_mhz` | int | 当前平均频率（MHz） |
| `cpu.per_core.available` | bool | 每核数据是否可用 |
| `cpu.per_core.per_cpu` | 数组（标量，示例长度 4） | 每个逻辑核心的占用率（0–100） |
| `cpu.per_core.freq_mhz` | 数组（标量，示例长度 4） | 每个逻辑核心的频率（MHz） |
| `cpu.per_core.topology` | 数组（标量，示例长度 4） | 每个逻辑核心所属物理核编号（同值 = 同物理核的兄弟线程） |
| `memory.available` | bool | available（布尔） |
| `memory.used_gb` | float | 已用内存（GB）= 总量 − 空闲 − buffers − cached |
| `memory.total_gb` | float | 内存总量（GB） |
| `memory.percent` | float | 内存占用率（0–100） |
| `memory.free_gb` | float | 空闲（GB） |
| `memory.swap_total_gb` | float | 交换区总量（GB） |
| `memory.swap_used_gb` | float | 交换区已用（GB） |
| `memory.buffers_gb` | float | 内核缓冲（GB） |
| `memory.cached_gb` | float | 页缓存（GB） |
| `memory.shared_gb` | float | 共享内存（GB） |
| `power.available` | bool | available（布尔） |
| `power.watts` | float | 功耗（瓦）；主值优先取最接近整机的域 |
| `power.source` | str | 主值来自哪个域（如「CPU 封装」） |
| `power.domains` | 数组（标量，示例长度 5） | 各 RAPL 域的明细 |
| `power.domains[].name` | str | 域 ID（package-0/core/uncore/dram/psys） |
| `power.domains[].label` | str | 域的中文名 |
| `power.domains[].watts` | float | 该域功耗（瓦） |
| `power.skipped` | 数组（标量，示例长度 0） | 读不到而跳过的域 ID 列表 |
| `net.available` | bool | available（布尔） |
| `net.nic` | str | 统计的网卡名 |
| `net.down_bps` | float | 下行速率（字节/秒） |
| `net.up_bps` | float | 上行速率（字节/秒） |
| `disk.available` | bool | available（布尔） |
| `disk.path` | str | 统计的挂载点 |
| `disk.free_gb` | float | 剩余空间（GB） |
| `disk.total_gb` | float | 该挂载点总容量（GB） |
| `disk.used_percent` | float | 已用百分比（0–100） |
| `disk.mounts` | 数组（标量，示例长度 2） | mounts |
| `disk.mounts[].mount` | str | 挂载点 |
| `disk.mounts[].device` | str | 设备路径 |
| `disk.mounts[].fstype` | str | 文件系统类型 |
| `disk.mounts[].total_gb` | float | 容量（GB） |
| `disk.mounts[].used_percent` | float | 已用百分比 |
| `disk.mounts[].free_gb` | float | 剩余（GB） |
| `disk.device` | str | 根挂载对应的设备 |
| `disk.block` | str | 块设备名（如 sda） |
| `disk.model` | str | 磁盘型号 |
| `disk.size_gb` | float | 磁盘容量（GB） |
| `disk.rotational` | bool | true=机械盘，false=固态 |
| `disk.bus` | NoneType | 总线类型（多为 null） |
| `disk.read_bps` | float | 读取速率（字节/秒，差分） |
| `disk.write_bps` | float | 写入速率（字节/秒，差分） |
| `disk.read_total_gb` | float | 累计读取（GB） |
| `disk.write_total_gb` | float | 累计写入（GB） |
| `temp.available` | bool | available（布尔） |
| `temp.celsius` | float | 温度（摄氏度） |
| `temp.source` | str | 温度来源通道 |
| `load.available` | bool | available（布尔） |
| `load.avg1` | float | 1 分钟平均负载 |
| `load.avg5` | float | 5 分钟平均负载 |
| `load.avg15` | float | 15 分钟平均负载 |
| `performance.cpu.available` | bool | available（布尔） |
| `performance.cpu.percent` | float | 同 cpu.percent（性能页复用） |
| `performance.cpu.freq_mhz` | int | freq_mhz（MHz） |
| `performance.cpu.per_core.available` | bool | available（布尔） |
| `performance.cpu.per_core.per_cpu` | 数组（标量，示例长度 4） | per_cpu |
| `performance.cpu.per_core.freq_mhz` | 数组（标量，示例长度 4） | freq_mhz（MHz） |
| `performance.cpu.per_core.topology` | 数组（标量，示例长度 4） | topology |
| `performance.gpu.available` | bool | available（布尔） |
| `performance.gpu.card` | str | 核显设备节点 |
| `performance.gpu.freq_mhz` | int | 核显当前频率（MHz） |
| `performance.gpu.max_mhz` | int | 核显最大频率（MHz） |
| `performance.temps.available` | bool | available（布尔） |
| `performance.temps.list` | 数组（标量，示例长度 5） | 全部温度通道 |
| `performance.temps.list[].key` | str | 通道稳定 ID（如 acpi、coretemp/core0） |
| `performance.temps.list[].label` | str | 通道中文名 |
| `performance.temps.list[].celsius` | float | 温度（摄氏度） |
| `performance.fans.available` | bool | available（布尔） |
| `performance.fans.list` | 数组（标量，示例长度 2） | 全部风扇 |
| `performance.fans.list[].key` | str | 风扇稳定 ID（如 cpu_fan/gpu_fan） |
| `performance.fans.list[].label` | str | 风扇中文名 |
| `performance.fans.list[].rpm` | int | 转速（RPM，0 = 停转） |
| `performance.battery.available` | bool | available（布尔） |
| `performance.battery.percent` | float | 电量（%） |
| `performance.battery.plugged` | bool | 是否接着电源 |
| `performance.battery.status` | str | 厂商状态字符串（Charging/Discharging/Not charging/Full） |
| `performance.battery.cycles` | int | 循环次数 |
| `performance.battery.power_w` | NoneType | 瞬时功率（瓦，可能为 null） |
| `performance.battery.secsleft` | NoneType | 预计剩余秒数（可能为 null） |
| `performance.memory.available` | bool | available（布尔） |
| `performance.memory.used_gb` | float | used_gb（GB） |
| `performance.memory.total_gb` | float | total_gb（GB） |
| `performance.memory.percent` | float | percent（%） |
| `performance.memory.free_gb` | float | free_gb（GB） |
| `performance.memory.swap_total_gb` | float | swap_total_gb（GB） |
| `performance.memory.swap_used_gb` | float | swap_used_gb（GB） |
| `performance.memory.buffers_gb` | float | buffers_gb（GB） |
| `performance.memory.cached_gb` | float | cached_gb（GB） |
| `performance.memory.shared_gb` | float | shared_gb（GB） |
| `performance.load.available` | bool | available（布尔） |
| `performance.load.avg1` | float | avg1 |
| `performance.load.avg5` | float | avg5 |
| `performance.load.avg15` | float | avg15 |
| `performance.power.available` | bool | available（布尔） |
| `performance.power.watts` | float | watts（瓦） |
| `performance.power.source` | str | source |
| `performance.power.domains` | 数组（标量，示例长度 5） | domains |
| `performance.power.domains[].name` | str | name |
| `performance.power.domains[].label` | str | label |
| `performance.power.domains[].watts` | float | watts（瓦） |
| `performance.power.skipped` | 数组（标量，示例长度 0） | skipped |
| `processes` | 数组（标量，示例长度 6） | processes |
| `processes[].pid` | int | 进程号 |
| `processes[].name` | str | 进程名 |
| `processes[].user` | str | 所属用户 |
| `processes[].cpu` | float | CPU 占用率（0–100） |
| `processes[].rss_mb` | float | 常驻内存（MB） |
| `processes[].status` | str | 状态（running/sleeping/…） |
| `processes[].threads` | int | 线程数 |
| `processes[].started` | int | 启动时间（Unix 秒） |
| `processes[].cmd` | str | 命令行 |
| `processes[].container` | NoneType | 所属容器名（非容器进程为 null） |
| `process_count` | int | 进程总数 |
| `ready` | bool | 采样线程是否已产出第一份快照；false 时其余字段可能为空 |
| `window` | float | 曲线窗口长度（秒） |
| `interval` | float | 采样间隔（秒） |
| `services` | 数组（标量，示例长度 9） | services |
| `services[].group` | str | 分组（容器/本机/健康） |
| `services[].name` | str | 服务名 |
| `services[].status` | str | ok/warn/down/unknown |
| `services[].detail` | str | 一句话状态 |
| `services[].groupNote` | str | 分组补充说明；没有就是 null |


## `GET /api/performance`

顶层字段：`battery`、`cpu`、`fans`、`gpu`、`interval`、`load`、`memory`、`power`、`ready`、`temps`、`ts`、`window`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `cpu.available` | bool | available（布尔） |
| `cpu.percent` | float | CPU 总占用率（0–100，按逻辑核心数归一化） |
| `cpu.freq_mhz` | int | 当前平均频率（MHz） |
| `cpu.per_core.available` | bool | 每核数据是否可用 |
| `cpu.per_core.per_cpu` | 数组（标量，示例长度 4） | 每个逻辑核心的占用率（0–100） |
| `cpu.per_core.freq_mhz` | 数组（标量，示例长度 4） | 每个逻辑核心的频率（MHz） |
| `cpu.per_core.topology` | 数组（标量，示例长度 4） | 每个逻辑核心所属物理核编号（同值 = 同物理核的兄弟线程） |
| `gpu.available` | bool | available（布尔） |
| `gpu.card` | str | card |
| `gpu.freq_mhz` | int | freq_mhz（MHz） |
| `gpu.max_mhz` | int | max_mhz（MHz） |
| `temps.available` | bool | available（布尔） |
| `temps.list` | 数组（标量，示例长度 5） | list |
| `temps.list[].key` | str | key |
| `temps.list[].label` | str | label |
| `temps.list[].celsius` | float | celsius（°C） |
| `fans.available` | bool | available（布尔） |
| `fans.list` | 数组（标量，示例长度 2） | list |
| `fans.list[].key` | str | key |
| `fans.list[].label` | str | label |
| `fans.list[].rpm` | int | rpm（RPM） |
| `battery.available` | bool | available（布尔） |
| `battery.percent` | float | percent（%） |
| `battery.plugged` | bool | plugged（布尔） |
| `battery.status` | str | status |
| `battery.cycles` | int | cycles |
| `battery.power_w` | NoneType | power_w（可能为 null） |
| `battery.secsleft` | NoneType | secsleft（可能为 null） |
| `memory.available` | bool | available（布尔） |
| `memory.used_gb` | float | 已用内存（GB）= 总量 − 空闲 − buffers − cached |
| `memory.total_gb` | float | 内存总量（GB） |
| `memory.percent` | float | 内存占用率（0–100） |
| `memory.free_gb` | float | 空闲（GB） |
| `memory.swap_total_gb` | float | 交换区总量（GB） |
| `memory.swap_used_gb` | float | 交换区已用（GB） |
| `memory.buffers_gb` | float | 内核缓冲（GB） |
| `memory.cached_gb` | float | 页缓存（GB） |
| `memory.shared_gb` | float | 共享内存（GB） |
| `load.available` | bool | available（布尔） |
| `load.avg1` | float | 1 分钟平均负载 |
| `load.avg5` | float | 5 分钟平均负载 |
| `load.avg15` | float | 15 分钟平均负载 |
| `power.available` | bool | available（布尔） |
| `power.watts` | float | 功耗（瓦）；主值优先取最接近整机的域 |
| `power.source` | str | 主值来自哪个域（如「CPU 封装」） |
| `power.domains` | 数组（标量，示例长度 5） | 各 RAPL 域的明细 |
| `power.domains[].name` | str | 域 ID（package-0/core/uncore/dram/psys） |
| `power.domains[].label` | str | 域的中文名 |
| `power.domains[].watts` | float | 该域功耗（瓦） |
| `power.skipped` | 数组（标量，示例长度 0） | 读不到而跳过的域 ID 列表 |
| `ready` | bool | 采样线程是否已产出第一份快照；false 时其余字段可能为空 |
| `ts` | float | 服务器时间戳（Unix 秒，浮点） |
| `window` | float | 曲线窗口长度（秒） |
| `interval` | float | 采样间隔（秒） |


## `GET /api/network`

顶层字段：`connection`、`disk`、`interval`、`net`、`nic`、`ready`、`ts`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `nic.available` | bool | 网卡信息是否可用 |
| `nic.name` | str | 网卡名 |
| `nic.wireless` | bool | 是否无线网卡 |
| `nic.up` | bool | 链路是否 up |
| `nic.speed_mbps` | int | 协商速率（Mbps） |
| `nic.duplex` | str | 双工模式 |
| `nic.mtu` | int | MTU |
| `nic.ipv4` | str | IPv4 地址 |
| `nic.netmask` | str | 子网掩码 |
| `nic.ipv6` | str | IPv6 地址（链路本地） |
| `nic.mac` | str | MAC 地址 |
| `nic.recv_total_gb` | float | 累计接收（GB） |
| `nic.sent_total_gb` | float | 累计发送（GB） |
| `nic.dropin` | int | 接收丢包计数 |
| `nic.dropout` | int | 发送丢包计数 |
| `connection.available` | bool | available（布尔） |
| `connection.total` | int | 连接总数 |
| `connection.established` | int | ESTABLISHED 数量 |
| `connection.connection_listening` | int | 监听套接字数量 |
| `connection.remotes` | 数组（标量，示例长度 12） | 连接数最多的对端 |
| `connection.remotes[].addr` | str | 对端地址:端口 |
| `connection.remotes[].count` | int | 连接数 |
| `connection.process_attribution` | bool | 连接是否能关联到进程（非 root 常为 false） |
| `connection.local_ip` | str | 本机 IP |
| `connection.gateway` | str | 默认网关 |
| `connection.medium` | str | 介质（有线/无线） |
| `connection.gateway_ms` | float | 网关 TCP 握手耗时（ms） |
| `connection.internet_ms` | float | 外网 TCP 握手耗时（ms） |
| `connection.internet_target` | str | 外网探测目标 host:port |
| `connection.proxy_port` | NoneType | 本机常见代理端口（无则 null） |
| `connection.listening` | int | 监听端口总数 |
| `ready` | bool | 采样线程是否已产出第一份快照；false 时其余字段可能为空 |
| `ts` | float | 服务器时间戳（Unix 秒，浮点） |
| `interval` | float | 采样间隔（秒） |
| `disk.available` | bool | available（布尔） |
| `disk.path` | str | 统计的挂载点 |
| `disk.free_gb` | float | 剩余空间（GB） |
| `disk.total_gb` | float | 该挂载点总容量（GB） |
| `disk.used_percent` | float | 已用百分比（0–100） |
| `disk.mounts` | 数组（标量，示例长度 2） | mounts |
| `disk.mounts[].mount` | str | 挂载点 |
| `disk.mounts[].device` | str | 设备路径 |
| `disk.mounts[].fstype` | str | 文件系统类型 |
| `disk.mounts[].total_gb` | float | 容量（GB） |
| `disk.mounts[].used_percent` | float | 已用百分比 |
| `disk.mounts[].free_gb` | float | 剩余（GB） |
| `disk.device` | str | 根挂载对应的设备 |
| `disk.block` | str | 块设备名（如 sda） |
| `disk.model` | str | 磁盘型号 |
| `disk.size_gb` | float | 磁盘容量（GB） |
| `disk.rotational` | bool | true=机械盘，false=固态 |
| `disk.bus` | NoneType | 总线类型（多为 null） |
| `disk.read_bps` | float | 读取速率（字节/秒，差分） |
| `disk.write_bps` | float | 写入速率（字节/秒，差分） |
| `disk.read_total_gb` | float | 累计读取（GB） |
| `disk.write_total_gb` | float | 累计写入（GB） |
| `net.available` | bool | available（布尔） |
| `net.nic` | str | 统计的网卡名 |
| `net.down_bps` | float | 下行速率（字节/秒） |
| `net.up_bps` | float | 上行速率（字节/秒） |


## `GET /api/device`

顶层字段：`battery`、`bluetooth`、`interfaces`、`lan`、`ready`、`runtime`、`summary`、`ts`、`usb`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `summary.hostname` | str | 主机名 |
| `summary.os` | str | 操作系统 |
| `summary.kernel` | str | 内核版本 |
| `summary.arch` | str | 架构 |
| `summary.uptime_s` | int | 开机时长（秒） |
| `summary.vendor` | str | 整机厂商（DMI） |
| `summary.product` | str | 整机型号 |
| `summary.bios` | str | BIOS 版本 |
| `summary.cpu` | str | CPU 型号 |
| `summary.cores` | int | 物理核数 |
| `summary.threads` | int | 逻辑核数 |
| `summary.cache_text` | str | 各级缓存汇总（人类可读） |
| `summary.virtualization` | str | 虚拟化能力（VT-x/AMD-V） |
| `summary.memory_gb` | float | 内存总量（GB） |
| `summary.swap_gb` | float | 交换区总量（GB） |
| `summary.gpu` | int | 核显最大频率（MHz） |
| `summary.disk.device` | str | device |
| `summary.disk.block` | str | block |
| `summary.disk.model` | str | model |
| `summary.disk.size_gb` | float | size_gb（GB） |
| `summary.disk.rotational` | bool | rotational（布尔） |
| `summary.disk.bus` | NoneType | bus（可能为 null） |
| `summary.mounts` | 数组（标量，示例长度 2） | mounts |
| `summary.mounts[].mount` | str | mount |
| `summary.mounts[].device` | str | device |
| `summary.mounts[].fstype` | str | fstype |
| `summary.mounts[].total_gb` | float | total_gb（GB） |
| `summary.mounts[].used_percent` | float | used_percent（%） |
| `summary.mounts[].free_gb` | float | free_gb（GB） |
| `usb.list` | 数组（标量，示例长度 3） | USB 设备列表 |
| `usb.list[].id` | str | 厂商:产品 ID（如 0b95:772a） |
| `usb.list[].vendor` | str | 厂商名 |
| `usb.list[].product` | str | 产品名 |
| `usb.list[].bus` | int | 总线号 |
| `usb.list[].device` | int | 设备号 |
| `usb.list[].hub` | bool | true = 根集线器（控制器本身，非外接设备） |
| `usb.total` | int | USB 设备总数 |
| `usb.external` | int | 外接设备数（不含根集线器） |
| `bluetooth.available` | bool | 是否有蓝牙适配器 |
| `bluetooth.reason` | str | reason |
| `bluetooth.adapters` | 数组（标量，示例长度 0） | 适配器列表（如 hci0） |
| `bluetooth.devices` | 数组（标量，示例长度 0） | 已配对设备 |
| `interfaces.physical` | 数组（标量，示例长度 2） | 物理与无线接口 |
| `interfaces.physical[].name` | str | 接口名 |
| `interfaces.physical[].kind` | str | 类型（有线/无线/回环/虚拟） |
| `interfaces.physical[].up` | bool | 是否 up |
| `interfaces.physical[].speed_mbps` | int | 速率（Mbps） |
| `interfaces.physical[].mtu` | int | MTU |
| `interfaces.physical[].ipv4` | str | IPv4（可能缺失） |
| `interfaces.physical[].ipv6` | str | IPv6（可能缺失） |
| `interfaces.physical[].mac` | str | MAC |
| `interfaces.physical[].wireless` | NoneType | 无线接口的 SSID/信号等；非无线接口为 null |
| `interfaces.virtual` | 数组（标量，示例长度 12） | 虚拟接口（docker/网桥/veth） |
| `interfaces.virtual[].name` | str | name |
| `interfaces.virtual[].kind` | str | kind |
| `interfaces.virtual[].up` | bool | up（布尔） |
| `interfaces.virtual[].speed_mbps` | NoneType | speed_mbps（可能为 null） |
| `interfaces.virtual[].mtu` | int | mtu |
| `interfaces.virtual[].ipv4` | str | IPV4 |
| `interfaces.virtual[].ipv6` | NoneType | IPV6 |
| `interfaces.virtual[].mac` | str | MAC |
| `interfaces.virtual[].wireless` | NoneType | wireless（可能为 null） |
| `lan.hosts` | 数组（标量，示例长度 7） | 发现的局域网设备 |
| `lan.hosts[].ip` | str | 设备 IP |
| `lan.hosts[].alive` | NoneType | ICMP 是否存活（null = 仅邻居表） |
| `lan.hosts[].name` | NoneType | 反向 DNS 名称（可能为 null） |
| `lan.hosts[].sources` | 数组（标量，示例长度 1） | 发现来源（icmp/arp/ssdp） |
| `lan.hosts[].mac` | str | MAC（可能为 null） |
| `lan.hosts[].vendor` | NoneType | MAC 厂商（查不到为 null） |
| `lan.subnet` | str | 扫描网段（如 192.168.1.0/24） |
| `lan.scanned_at` | NoneType | 上次扫描完成时间（Unix 秒，未扫过为 null） |
| `lan.scan_seconds` | NoneType | 上次扫描耗时（秒） |
| `lan.swept` | int | 上次扫描的地址数 |
| `lan.live` | int | 上次扫描存活数 |
| `lan.note` | NoneType | 扫描降级说明（如没有 ping 命令） |
| `lan.oui` | bool | OUI 厂商库是否可用 |
| `battery.available` | bool | available（布尔） |
| `battery.percent` | float | percent（%） |
| `battery.plugged` | bool | plugged（布尔） |
| `battery.status` | str | status |
| `battery.cycles` | int | cycles |
| `battery.power_w` | NoneType | power_w（可能为 null） |
| `battery.secsleft` | NoneType | secsleft（可能为 null） |
| `runtime.python` | str | python |
| `runtime.psutil` | str | psutil |
| `runtime.docker` | str | docker |
| `ready` | bool | 采样线程是否已产出第一份快照；false 时其余字段可能为空 |
| `ts` | float | 服务器时间戳（Unix 秒，浮点） |


## `GET /api/services`

顶层字段：`containers`、`interval`、`ports`、`probes`、`ready`、`systemd`、`ts`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `containers.list` | 数组（标量，示例长度 6） | 容器列表 |
| `containers.list[].name` | str | 容器名 |
| `containers.list[].up` | bool | 是否运行中 |
| `containers.list[].status` | str | docker 的状态字符串 |
| `containers.list[].image` | str | 镜像名 |
| `containers.note` | NoneType | 降级说明（如没有 docker 命令） |
| `containers.total` | int | 容器总数 |
| `containers.running` | int | 运行中数量 |
| `systemd.available` | bool | systemctl 是否可用 |
| `systemd.total` | int | 运行中的服务数 |
| `systemd.list` | 数组（标量，示例长度 49） | 运行中的服务列表 |
| `systemd.list[].unit` | str | 单元名 |
| `systemd.list[].description` | str | 服务描述 |
| `ports` | 数组（标量，示例长度 24） | 监听端口表 |
| `ports[].port` | int | 端口号 |
| `ports[].proto` | str | 协议（tcp/tcp6/udp） |
| `ports[].addr` | str | 绑定地址 |
| `ports[].scope` | str | 访问范围（局域网/仅本机） |
| `ports[].process` | NoneType | 占用进程名（非 root 常为 null） |
| `ports[].known` | str | IANA 惯例用途（如 SSH） |
| `probes.list` | 数组（标量，示例长度 1） | 探测目标与最近一次握手耗时 |
| `probes.list[].name` | str | 目标名 |
| `probes.list[].host` | str | 主机 |
| `probes.list[].port` | int | 端口 |
| `probes.list[].ms` | float | TCP 握手耗时（ms，失败为 null） |
| `probes.error` | NoneType | probes.json 配置问题（无问题为 null） |
| `probes.path` | str | 配置文件路径 |
| `probes.interval` | int | 探测间隔（秒） |
| `ready` | bool | 采样线程是否已产出第一份快照；false 时其余字段可能为空 |
| `ts` | float | 服务器时间戳（Unix 秒，浮点） |
| `interval` | float | 采样间隔（秒） |


## `GET /api/processes`

顶层字段：`count`、`interval`、`processes`、`ready`、`ts`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ready` | bool | 采样线程是否已产出第一份快照；false 时其余字段可能为空 |
| `ts` | float | 服务器时间戳（Unix 秒，浮点） |
| `count` | int | 进程总数 |
| `interval` | float | 采样间隔（秒） |
| `processes` | 数组（标量，示例长度 291） | processes |
| `processes[].pid` | int | 进程号 |
| `processes[].name` | str | 进程名 |
| `processes[].user` | str | 所属用户 |
| `processes[].cpu` | float | CPU 占用率（0–100） |
| `processes[].rss_mb` | float | 常驻内存（MB） |
| `processes[].status` | str | 状态（running/sleeping/…） |
| `processes[].threads` | int | 线程数 |
| `processes[].started` | int | 启动时间（Unix 秒） |
| `processes[].cmd` | str | 命令行 |
| `processes[].container` | NoneType | 所属容器名（非容器进程为 null） |


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

