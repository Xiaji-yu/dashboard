# 总控台 Dashboard

一个**零外部依赖**的本机状态看板：Python 标准库起的 HTTP 服务 + 原生前端，用浏览器实时查看这台机器的
CPU、内存、磁盘、网速、温度、进程与容器状态。

前端不引任何 CDN，后端除 `psutil` 外只用 Python 标准库，克隆下来一条命令就能跑。

<div align="center">
  <img src="docs/screenshot.png" alt="总控台概览页" width="900">
</div>

[![CI](https://github.com/Xiaji-yu/dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/Xiaji-yu/dashboard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-psutil-green.svg)](requirements.txt)

## 特性

- **零依赖前端**：曲线、进度条、状态点全是手写 HTML/CSS/SVG，没有构建步骤，也不需要 npm。
- **移动端自适应**：窄屏下侧边栏折叠为吸顶顶栏（页签横向滑动），指标与卡片单列排布；
  切后台/锁屏自动暂停轮询，回前台立即刷新。
- **多页面（分批上线）**：前端为 hash 路由单页应用，页面模块按需加载；
  已上线「概览」「性能与电源」（每核占用、5 路温度、风扇转速、核显频率、内存构成、电池）
  **六个页面全部上线**：概览、性能与电源（每核占用、温度、风扇、核显频率、内存构成、电池）、
  进程（全量列表、搜索、锁定排序、行展开）、网络与磁盘（网速曲线、连接、网卡、磁盘读写）、
  服务（可配置远程探测、容器、监听端口与「谁能访问」、systemd）、
  设备（与这台机器连接的设备：USB / 蓝牙 / 网络接口 / PCI）。
- **采不到就说清楚**：任何指标采集失败都不会让页面崩，而是显示「不可用」+ 具体原因
  （例如 `读取 RAPL 需要 root 权限`）。
- **省流量的增量接口**：曲线数据走 `since` 游标，每秒只传新增的点，不是每次重传整窗口。
- **真实数据源**：`psutil` + `/proc/net/tcp{,6}` + `docker ps`，不依赖任何外部服务或云端。
- **可配置**：监听地址、端口、采样间隔、统计哪块网卡、看哪个挂载点都能用环境变量改。
- **账号鉴权**：登录后才能看数据。口令用 PBKDF2-HMAC-SHA256（20 万轮 + 每用户随机盐）存储，
  会话是 HttpOnly + SameSite=Lax 的 Cookie；**首次启动自动生成随机密码并打印到日志**，
  登录后可在侧栏「账号」里改密码 / 改用户名 / 退出全部设备；登录失败按来源 IP 限速。
- **有测试**：66 个 Python 用例覆盖采集、缓冲、接口契约与降级路径，另有 8 个前端图表用例
  守住曲线绘制；CI 里跑 ruff + 两套测试 + 接口冒烟。

## 截图

概览页（本项目的实际运行截图，非设计稿）：

![概览页](docs/screenshot.png)

## 快速开始

```bash
git clone https://github.com/Xiaji-yu/dashboard.git
cd dashboard

python3 -m venv .venv && source .venv/bin/activate   # 可选
pip install -r requirements.txt

./run.sh start        # 后台启动，日志写入 server.log
```

打开 <http://127.0.0.1:8282/>，会先看到登录页。**首次启动的随机账号密码在日志里**：

```console
$ ./run.sh log | grep auth
[auth] 首次启动，已生成初始账号：admin / Hfpz9QFSa68fEVfQZTRT
[auth] 凭据文件：/home/xiaji/code/dashboard/auth.json（权限 600，已加入 .gitignore；登录后请自行修改密码）
```

登录后建议立刻在侧栏「账号」里改掉密码。默认监听 `0.0.0.0`，所以同网段的机器也能用
`http://<本机IP>:8282/` 访问（同样需要登录）。

其他命令：

```bash
./run.sh status       # 是否在跑 + 打印一段接口输出
./run.sh log          # 看日志
./run.sh stop         # 停止
./run.sh fg           # 前台运行，调试用
```

`run.sh` 用 `setsid` 启动服务，脱离当前会话/进程组——关掉终端或父进程被杀都不会带走它。

## 部署

**推荐用 systemd 常驻**（这个看板的价值就是能看到宿主机：`/proc` 的进程、`/sys` 的温度风扇功耗、
docker 与 systemd 的状态；容器化要把这些宿主能力全开后，隔离所剩不多）。完整步骤见
[`deploy/README.md`](deploy/README.md)，最小路径：

```bash
cd /home/xiaji/code/dashboard
./run.sh stop                                  # 先停掉 run.sh 起的实例，避免抢端口
sudo cp deploy/dashboard.service /etc/systemd/system/
sudo nano /etc/systemd/system/dashboard.service   # 改 User= 与 WorkingDirectory=
sudo systemctl daemon-reload && sudo systemctl enable --now dashboard
journalctl -u dashboard | grep auth            # 首次启动的随机账号密码
```

三个容易踩的点：

1. **`AmbientCapabilities=CAP_NET_RAW` 别删**：`/usr/bin/ping` 靠文件能力工作，而 unit 开了
   `NoNewPrivileges=true` 会让文件能力失效——没有它，设备页的「局域网设备」会退化成只能看邻居表。
2. **服务用户要在 `docker` 组里**（`id` 看一下），否则服务页的容器列表是空的；但不要用 root。
3. **功耗（RAPL）**：unit 里已经用 `ExecStartPre` 在每次启动时以 root 放开读权限，
   比手动 chmod 持久（重启机器后不会再丢）；内核不允许时改用 `deploy/60-dashboard-rapl.rules`。

看板没有 TLS（内置账号鉴权是明文传输的），所以只放行内网网段、别直接映射公网：

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8282 proto tcp
```

要出内网就在前面套一层带 TLS 的反向代理；也可以完全不上网络，只监听回环 + SSH 隧道：

```bash
DASHBOARD_HOST=127.0.0.1 ./run.sh start
ssh -L 8282:127.0.0.1:8282 user@server      # 本地打开 http://127.0.0.1:8282
```

想统一用 compose 管理就用 `deploy/docker-compose.yml`（**代价写在 `deploy/README.md` 的对比表里**：
要挂 `docker.sock`＝root 等价、容器里读不到 systemd、功耗仍需在宿主机放开权限）。

## 配置

全部通过环境变量，无需改代码：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DASHBOARD_HOST` | `0.0.0.0` | 监听地址；改成 `127.0.0.1` 则只允许本机访问 |
| `DASHBOARD_PORT` | `8282` | 监听端口 |
| `DASHBOARD_INTERVAL` | `1.0` | 采样间隔（秒） |
| `DASHBOARD_NIC` | 自动挑选 | 指定统计哪块网卡，如 `eth0` |
| `DASHBOARD_DISK` | `/` | 指定统计哪个挂载点 |
| `DASHBOARD_AUTH_FILE` | 项目目录 `auth.json` | 凭据文件路径（600 权限，已 gitignore） |
| `DASHBOARD_USER` | `admin` | 初始用户名（**仅在凭据文件不存在时生效**） |
| `DASHBOARD_PASSWORD` | 随机 20 位 | 初始密码（同上；留空则随机生成并打印到日志） |

```bash
DASHBOARD_PORT=9000 DASHBOARD_HOST=127.0.0.1 ./run.sh start
```

### 账号与会话

- 凭据文件 `auth.json` 只存口令哈希（PBKDF2-HMAC-SHA256，20 万轮，每用户随机盐）与活跃会话令牌，
  权限 600，**已加入 .gitignore**；
- 首次启动若文件不存在，就生成随机密码：写进该文件并打印到日志（见上）；
  也可以用 `DASHBOARD_USER` / `DASHBOARD_PASSWORD` 指定（只在首次生成时生效）；
- 会话默认 7 天有效，最多保留 20 个；**改密码会把其他设备的会话全部踢掉**（当前设备保留）；
- 登录失败按来源 IP 限速：5 分钟内 5 次失败后需等待；成功登录会清空计数；
- 删掉 `auth.json` 再启动即可**重置账号**（会重新生成随机密码并打印）。

### 远程探测目标（服务页）

`probes.json`（改完不用重启，看板 60 秒内自动重载）：

```json
{
  "probes": [
    { "name": "腾讯云服务器", "host": "1.2.3.4", "port": 443 },
    { "name": "AWS 东京 VPS", "host": "ec2.example.com", "port": 22 }
  ]
}
```

`name` 省略时用 `host` 当名字；`host`/`port` 缺一条就跳过该目标。连不上的目标会显示「不可用」而不是从列表里消失。

## 接口

除了登录相关接口，**其余接口都需要先登录**（未登录时接口返回 401，页面跳转登录页）。

| 接口 | 说明 |
| --- | --- |
| `GET /login` | 登录页（公开） |
| `POST /api/login` | 登录，JSON `{username, password}`，成功下发会话 Cookie |
| `POST /api/logout` | 退出登录（`{"all": true}` 为退出全部设备） |
| `GET /api/auth` | 当前登录状态（公开，登录页用它判断是否已登录） |
| `POST /api/password` | 修改密码 / 用户名，JSON `{old_password, new_password?, new_username?}` |
| `GET /` | 看板页面 |
| `GET /api/overview` | 瞬时快照：全部指标 + 最忙进程 Top6 + 服务状态 |
| `GET /api/series` | 曲线数据（最近 120 秒全窗口） |
| `GET /api/series?since=<ts>` | 增量曲线数据，只返回比 `ts` 新的点 |

采样线程每秒采一次并写入环形缓冲，所以**页面随时打开都有历史曲线**，不需要等数据积累。

```console
$ curl -s localhost:8282/api/overview | python3 -m json.tool | head -12
{
  "ts": 1790007212.11,
  "host": "xiaji-computer",
  "cores": 4,
  "uptime_s": 6583.11,
  "cpu": { "available": true, "percent": 34.1, "freq_mhz": 3100 },
  ...
}
```

## 指标口径

上半部分是 5 条「数字 + 曲线」指标：

| 指标 | 采集方式 | 说明 |
| --- | --- | --- |
| CPU 占用 | `psutil.cpu_percent()` | 固定 0–100 量程；副标题显示核心数与实时频率 |
| 内存 | `psutil.virtual_memory()` | 已用 / 总量，副标题带交换区 |
| 功耗 | Intel RAPL `energy_uj` 差分 | 主值取**可靠的域**并在副标题注明来源；`energy_uj` 默认仅 root 可读，见「部署到 systemd」 |
| 网速 ↓↑ | `psutil.net_io_counters()` 差分 | **只统计物理网卡**，docker 的 `br-*`/`veth`/`docker0` 不计入 |
| 磁盘剩余 | `psutil.disk_usage()` | 条形图按已用比例填充 |

### 功耗（Intel RAPL）实测口径

`powercap` 下同一个计数器常常同时暴露两个接口（`intel-rapl:*` 与 `intel-rapl-mmio:*`），
名字相同、数值一致，采集层按域名去重并优先 MSR。

各域读数是否可信要实测判断，本机（i5-7200U，Kaby Lake 移动版）的结果：

| 域 | 空载 | 4 核满载 | 是否可信 |
| --- | --- | --- | --- |
| `package-0` CPU 封装 | 1.1 W | 12.5 W | ✅ 跟着负载走，作主值 |
| `core` CPU 核心 | 0.7 W | 11.7 W | ✅ |
| `dram` 内存 | 0.3 W | 0.4 W | ✅（空闲时计数器可能长时间不前进，读数 0.00 W） |
| `psys` 平台功耗 | 0.4 W | **3.6 W** | ❌ 平台功耗不可能小于封装 → 该平台 PSYS 未实现，页面标「未实现」 |
| `uncore` 核显与内存控制器 | 0.0 W | 0.0 W | ⚠️ 计数器不前进，如实显示 0.00 W |

所以主值的选择规则是：`psys` 只有在**不小于 `package-0`** 时才当作平台功耗，否则用 `package-0`，
副标题会写明来源（如「CPU 封装 · RAPL」）。空闲域读数为 0.00 W 是真实测量值，不做推断性标注。

设备页（与这台机器连接的设备）：

局域网设备是怎么发现的（**全程不需要其他设备的账号密码**）：

| 手段 | 说明 |
| --- | --- |
| 邻居表 / ARP | 内核维护的 IP↔MAC 映射，被动读取，免权限；只能看到「最近通信过」的设备 |
| ICMP 并发探测 | 对本机所在网段逐个 `ping -c1 -W1`（32 并发，整段 /24 约 10 秒），结果缓存 120 秒；没有 `ping` 命令时页面会说明只能看邻居表 |
| SSDP / UPnP | 向 `239.255.255.250:1900` 发 `M-SEARCH` 组播，路由器/NAS/电视/打印机通常会回应；本机路由器没开 UPnP，所以现在只有 ICMP 结果 |
| 反向 DNS | 借路由器的 DNS 拿到设备名（如 `iStoreOS.lan`、`Xiaomi-14-Pro.lan`） |
| IEEE OUI 库 | 由 MAC 前缀查厂商（`/usr/share/ieee-data/oui.txt`）；随机 MAC（本地管理位）与较新设备查不到就只显示 MAC |

需要凭据的只有两种情况：想让**路由器**告诉你它自己的完整客户端列表（要路由器管理密码/SSH），
以及登录**某台设备**看它的详情（要那台设备的账号）。本页都不需要。

| 卡片 | 口径 |
| --- | --- |
| 本机 | 一行摘要：主机名、系统（`/etc/os-release`）、内核与架构、机型（DMI，本机免 root 可读）、BIOS、CPU、核心与缓存、虚拟化、内存、核显、磁盘与分区、运行时长、电池 |
| USB 设备 | 读 `/sys/bus/usb/devices/*`（厂商与型号分字段、免 root）；`1d6b` 是根集线器，标为「控制器」与外接设备区分 |
| 蓝牙 | 适配器看 `/sys/class/bluetooth`，已配对设备问 `bluetoothctl devices`；**没有适配器时如实显示占位说明**，插上后自动出现 |
| 网络接口 | 物理与无线逐条列（类型、状态、速率、MTU、IP、MAC）；无线额外显示 **SSID 与信号强度**（`/proc/net/wireless` + `iwconfig`）；docker/veth 等虚拟接口折叠成一行计数 |
| 局域网设备 | **免凭据发现**：邻居表（`/proc/net/arp`）+ ICMP 并发探测（整段 /24 约 10 秒，后台每 120 秒一次）+ SSDP/UPnP 组播查询；名称走反向 DNS（本机路由器提供 `xxx.lan`），厂商查 IEEE OUI 库；**不需要任何设备的账号密码** |

整页信息基本不变，后端缓存 **60 秒**（其中要起 `lsusb`/`lspci`/`bluetoothctl`）。

服务页：

| 项 | 口径 |
| --- | --- |
| 远程服务器 | 读项目根目录的 `probes.json`，每 30 秒对每个目标做一次 **TCP 连接**，记录握手耗时；改完文件不用重启（mtime 变了自动重载），也可用 `DASHBOARD_PROBES` 指向别处 |
| 容器 | `docker ps -a`（5 秒缓存）：名称、运行状态、镜像 |
| 监听端口 | 解析 `/proc/net/tcp{,6}` 的 LISTEN 项；**「谁能访问」来自绑定地址**：`0.0.0.0`/`::` → 局域网，`127.x`/`::1` → 仅本机 |
| 端口归属 | 需要读 `/proc/<pid>/fd`，**非 root 只能映射自己拥有的进程**；拿不到就显示 IANA 惯例的常见用途（如 22 → SSH），都没有则显示「—」 |
| 系统服务 | `systemctl list-units --type=service --state=running`（10 秒缓存），列运行中的服务名与描述 |

网络与磁盘页：

| 项 | 口径 |
| --- | --- |
| 网速 | 物理网卡 `net_io_counters` 差分，上下行各一条线，2 分钟窗口 |
| 连接 | 本机 IP、网关（读 `/proc/net/route`，免 root）、上网方式、本地代理、外网延迟、连接数 |
| 本地代理 | 探测常见代理端口（7890/7891/1080/1081/8118/3128/8889/7897）；8080 太通用，刻意不算 |
| 外网延迟 | **TCP 握手耗时**（没有 ICMP 权限，用 TCP 连接近似）；目标默认 `www.baidu.com:443`，可用 `DASHBOARD_NET_TARGET` 改。注意它可能被本地代理或运营商设备应答，只作趋势参考 |
| 连接归属 | 非 root 一般拿不到连接对应的进程，页面会如实标注 |
| 网卡 | 速率、双工、MTU、MAC、IPv4/IPv6、累计收发、丢包 |
| 磁盘 | 型号（`udevadm` 取完整名称，sysfs 只有 16 字符）、容量、SSD/HDD、挂载点、读写速率曲线、累计读写 |
| 挂载点过滤 | 跳过 tmpfs/overlay，以及 snap 的 `loop*`/`squashfs`（否则会被 20 多个只读挂载淹没） |
| 没做的 | SMART 健康度与磁盘温度需要 root；无线信号在本机不适用（USB 有线网卡） |

进程页：

| 项 | 口径 |
| --- | --- |
| 列表 | 全量进程（本机约 300 个），每 2 秒刷新；服务端按 CPU 降序返回 |
| CPU | 按逻辑核心数归一化到 0–100%，与概览页「最忙的程序」一致 |
| 排序 | **锁定排序**：刷新只更新数值不重排行；点列头换列或再点一次才重排，新进程追加到末尾并高亮 6 秒 |
| 搜索 | 匹配名称 / PID / 用户 / 命令行 / 容器，纯前端过滤 |
| 展开 | 命令行、启动时间（含已运行时长）、状态、线程数、用户、所属容器 |
| 容器归属 | 读 `/proc/<pid>/cgroup` 取容器 ID，再用 `docker ps` 的 ID→名字映射（5 秒缓存） |
| 载荷 | 约 300 行 / 67KB，2 秒一次 ≈ 0.27 Mbit/s（这是选「全量 + 2 秒」的代价，局域网无压力） |

下半部分（概览页）：

- **此刻最忙的程序**：按 CPU 排序 Top6。CPU 值按逻辑核心数归一化到 0–100，口径贴近 macOS 活动监视器；
  `top`/`ps` 的原始值最高可达 `100 × 核心数`，因此**顺序一致、数值约为其 1/核心数**。
- **服务状态**：分「容器 / 本机 / 健康」三组
  - 容器：`docker ps -a`（5 秒缓存），最多列 4 个，其余折叠成「另有 N 个未显示」
  - 本机：SSH 是否在听、监听端口总数（读 `/proc/net/tcp{,6}`，免 root）
  - 健康：CPU 温度（`psutil.sensors_temperatures()`）、系统负载（`os.getloadavg()`）

## 安全提示

内置账号鉴权（口令哈希 + 会话 Cookie + 登录限速），**但请仍然按内网自用来部署**：

- 服务是明文 HTTP，**没有 TLS**：不要把端口直接映射到公网。确实需要公网访问，请放在带 TLS 的
  反向代理后面，或只监听 `127.0.0.1` 再用 SSH 隧道：

  ```bash
  DASHBOARD_HOST=127.0.0.1 ./run.sh start
  ssh -L 8282:127.0.0.1:8282 user@server     # 本地打开 http://127.0.0.1:8282
  ```

- 首次登录后请立刻改密码；`auth.json` 请勿提交到仓库（已在 `.gitignore` 里）或分享给他人。
- 登录失败限速是「每 IP 5 次 / 5 分钟」，能挡脚本爆破，但挡不住分布式猜测——密码请设长一些。

细节见 [SECURITY.md](SECURITY.md)。

## 开发

```bash
pip install -r requirements-dev.txt

python3 -m unittest discover -v      # 66 个 Python 用例，约 1 秒
node tests/chart.test.js             # 8 个前端图表用例（不需要浏览器）
ruff check .                         # 代码风格（行宽 120，规则集 E/F/W）
./run.sh fg                          # 前台跑起来看效果
```

测试用标准库 `unittest` 编写，因此**不装 pytest 也能跑**；pytest 同样可以直接收集。
图表用例用 Node 直接桩出最小 DOM 跑 `static/chart.js`，覆盖「重复点/时间断层不能连成直线」这类
只在浏览器里才暴露的问题。
接口测试在 `127.0.0.1` 的空闲端口上真起一个服务（`port=0` 交给内核分配），
所以不会和在跑的 8282 抢端口，也不依赖网络。

代码风格：注释与文档字符串用中文，行宽上限 120（CJK 注释较宽）。贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 目录结构

```
dashboard/
├── server.py              # HTTP 服务、路由、JSON 接口
├── collector.py           # 指标采集（psutil + /proc + docker + hwmon），含降级处理
├── history.py             # 曲线用的时序环形缓冲
├── static/
│   ├── index.html         # 单页应用骨架（hash 路由）
│   ├── style.css          # 深色主题（含移动端断点）
│   ├── chart.js           # 手绘 SVG 折线图
│   ├── app.js             # 前端核心：路由、徽章轮询、公共工具
│   └── pages/             # 页面模块（概览、性能与电源……）按需加载
├── probes.json            # 服务页的远程探测目标（可改，改完不用重启）
├── tests/                 # Python 用例 + chart.test.js（Node 前端图表用例）
├── deploy/                # systemd 服务单元与 RAPL 权限 udev 规则
├── docs/
│   ├── screenshot.png     # 本项目运行截图
│   └── reference/         # UI 参考图（见该目录 README）
├── run.sh                 # 启停脚本
└── pyproject.toml         # 仅放 pytest / ruff 配置（本项目不做打包分发）
```

## 设计来源与差异

UI 结构参考了一组 macOS 系统监控面板的截图（见 `docs/reference/`），本项目按 Linux 的实际能力做了取舍：

1. **概览页去掉 MacBook 示意图整块**（含环境光、屏幕张开角度），上半部分改为 5 条指标曲线铺满全宽。
2. **性能与电源页尽量贴近参考图版式**：上半部分一张大卡（左侧大数字 + 按物理核分组的占用条 + GPU 频率，
   右侧 2 分钟大曲线与图例），下半部分三张并排小卡（温度 / 功耗与风扇 / 内存构成）。
   参考图的「能效核心 / 性能核心」分组在本机（i5-7200U，无大小核）退化为「按物理核心分组」。
3. Mac 专有指标（环境光、屏幕张开角度）在 Linux 上不存在，不显示；**电池反而能采到**——
   本机是笔记本，电量/接通状态/循环次数来自 sysfs 与 psutil。
4. 参考图的 GPU 占用率没有 Linux 通用接口，改为显示核显频率（`gt_cur_freq_mhz` + 最大值）。
5. 参考图的功耗卡按本机能力改写为「功耗与风扇」：RAPL 需 root，无权限时如实显示不可用并给出原因，
   有权限时显示整机瓦数；风扇转速作为该卡的大数字。
6. 参考图左下角的「关闭总控台」改为「暂停更新」：服务监听 `0.0.0.0` 且无鉴权，
   不做能被同网段任意触发的关机接口。
7. 参考图里的外网探测（腾讯云 / AWS / 百度延迟）留到「服务」页，做成可配置的 TCP 探测列表。
8. 曲线历史窗口与参考图一致，为最近 2 分钟。
9. 参考图里没有「进程」页，这一页是按现有视觉语言自行设计的：表格 + 搜索 + 锁定排序 + 行展开。
10. 参考图没有「设备」页，按现有视觉语言设计成「与这台机器连接的设备」：顶部一行本机摘要，
    下面 USB / 蓝牙 / 网络接口 / PCI 四张卡。（原先的「传感器」页与性能页重复度高，已删除。）
11. 网络页里参考图的「Wi-Fi 信号 / 附近的 Wi-Fi」两张卡，本机是 USB 有线网卡、没有无线，
    按实际能力换成「网卡」信息卡，光纤/有线信息如实标注；磁盘部分补了读写曲线与挂载点。

## 已知限制

- 没有 TLS，也没有多用户 / 权限分级：只有一组账号，登录后能看到全部指标。
- 曲线历史只存在内存里，服务重启后重新累积（最多 1200 点 / 约 20 分钟）。
- 功耗依赖 Intel RAPL 且需要 root，多数桌面环境会显示「不可用」（性能页会给出原因）。
- 容器面板每 5 秒执行一次 `docker ps`；docker 卡住时最多影响该面板状态。
- 设备页依赖 USB/PCI/蓝牙子系统：容器里可能读不到宿主机的设备，会如实显示「不可用」或「无适配器」。

## 许可证

[MIT](LICENSE)。`docs/reference/` 下的参考图不随该许可证授权，详见其目录内说明。
