# 部署说明

看板是「零依赖的单个 Python 脚本 + 静态前端」，部署本身很简单；麻烦的是它**要看到宿主机的很多东西**
（`/proc` 的进程与网络、`/sys` 的温度风扇与功耗、docker 与 systemd 的状态）。下面按推荐顺序给两种方式。

## 结论：优先 systemd，不建议容器

| | systemd（推荐） | 容器 |
| --- | --- | --- |
| 看到宿主机指标 | 天然可以 | 要 `pid: host` + `network_mode: host` + 挂 `/proc` `/sys` |
| 容器列表 | 用 `docker ps` 即可 | 必须挂 `docker.sock`（**等价于给容器 root**） |
| systemd 服务列表 | 可直接读 | **读不到**（容器里没有 systemd），服务页少一块 |
| 系统识别 | 真实（Ubuntu 24.04 / 机型 / BIOS） | 要额外挂 `/etc/os-release`，否则显示容器镜像的系统 |
| 功耗（RAPL） | unit 启动时 chmod 即可 | 容器里改不了，得先在宿主机放开权限 |
| 权限控制 | `NoNewPrivileges` + `ProtectSystem` + 精确 CAP | 挂上 `docker.sock` 后前面这些基本白给 |

一句话：这个看板的价值就在于「能看到宿主机」，容器化要把它需要的宿主能力全开后，隔离还剩的不多。
**只有当你想让这台机器上所有服务都用 compose 统一管理时，才值得选容器**（见文末）。

## 方式一：systemd（推荐）

仓库里已经带了 `deploy/dashboard.service`。

### 1. 停掉 run.sh 起的实例（避免和 systemd 抢 8282）

```bash
cd /home/xiaji/code/dashboard
./run.sh stop
```

### 2. 按需修改 unit

```bash
sudo cp deploy/dashboard.service /etc/systemd/system/dashboard.service
sudo nano /etc/systemd/system/dashboard.service      # 改 User= 和 WorkingDirectory=
```

需要确认的三处：

- `User=xiaji`：用你自己的账号。**这个用户要在 `docker` 组里**（`id` 看一下有没有 `984(docker)`），
  否则容器列表会显示「permission denied」。
  **千万别把 `User=` 删掉或留空**：systemd 系统服务在这种情况下默认以 **root** 运行
  （原文见 `man systemd.exec`：*for system services ... the default is "root"*）。unit 里已经加了一条
  `ExecStartPre` 兜底——真被删掉时它会直接拒绝启动并打印原因，而不是悄悄拿到 root。
- `WorkingDirectory` / `ExecStart`：改成你的实际路径。
- `AmbientCapabilities=CAP_NET_RAW`：**别删**。`/usr/bin/ping` 靠文件能力工作，而 unit 开了
  `NoNewPrivileges=true` 会让文件能力失效；没有这一行，设备页的「局域网设备」会退化成只能看邻居表。

### 3. 启动并设为开机自启

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dashboard
systemctl status dashboard
```

### 4. 拿初始账号密码

**首次启动**才会生成随机密码并打印到日志：

```bash
journalctl -u dashboard | grep auth
# [auth] 首次启动，已生成初始账号：admin / xxxxxxxxxxxxxxxxxxxx
# [auth] 凭据文件：/home/xiaji/code/dashboard/auth.json（权限 600，已加入 .gitignore；登录后请自行修改密码）
```

journald 的日志只有特权用户能读；用 `run.sh` 启动时日志文件是 600（脚本里设了 `umask 077`）。
改完密码后可以清掉那一行：`journalctl --rotate && journalctl --vacuum-time=1s` 或直接
`sed -i '/初始账号/d' server.log`。

登录后立刻在侧栏「账号」里改密码。忘记密码就 `rm auth.json && sudo systemctl restart dashboard`。

不想用随机密码，也可以指定初始账密（**只在凭据文件不存在时生效**）：

```ini
Environment=DASHBOARD_USER=admin
Environment=DASHBOARD_PASSWORD=换成你自己的强密码
```

### 5. 功耗（RAPL）权限

unit 里已经有一行：以 root 身份在**每次启动时**把功耗计数器放开读权限：

```ini
ExecStartPre=+/bin/sh -c 'chmod 0444 /sys/class/powercap/intel-rapl*/energy_uj 2>/dev/null || true'
```

这比手动 chmod 持久（重启机器后 systemd 启动服务时会再执行一次）。如果你的内核不允许 chmod sysfs，
改用 udev 规则或 tmpfiles：

```bash
sudo cp deploy/60-dashboard-rapl.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger --action=add --subsystem-match=powercap
```

### 6. 网络与安全

看板**没有 TLS**（内置账号鉴权是明文传输的），所以：

```bash
# 只放行内网网段，别直接暴露公网
sudo ufw allow from 192.168.1.0/24 to any port 8282 proto tcp
```

要出内网就用反向代理终结 TLS，例如 Caddy 两行搞定自动证书：

```
dashboard.example.com {
    reverse_proxy 127.0.0.1:8282
}
```

两件必须一起做的事：

1. 把 unit 里的 `DASHBOARD_HOST` 改成 `127.0.0.1`，让看板只监听回环；
2. **设 `Environment=DASHBOARD_TRUST_PROXY=1`**。反代之后所有请求的来源 IP 都是代理自己，
   而登录失败限速（5 次 / 5 分钟）是按来源 IP 计的——不开这个开关，任何人都能连错几次
   把唯一账号一起锁在门外。开了之后看板按 `X-Forwarded-For` 的第一跳计数。
   （默认不信任该头，是为了防止直接访问时伪造它绕过限速。）

### 7. 日志、更新、备份

```bash
journalctl -u dashboard -f          # 跟日志（journald 自动轮转）
journalctl -u dashboard -n 100      # 看最近 100 行
```

更新（前端是静态文件，没有构建步骤，重启即生效）：

```bash
cd /home/xiaji/code/dashboard && git pull
sudo systemctl restart dashboard
```

需要备份的只有两个文件：`auth.json`（账号与会话）与 `probes.json`（服务页探测目标）。
曲线历史只在内存里，不用备份。

### 8. 到底要不要用 root？（不建议）

以 root 运行确实能多看几样东西，但代价与收益不成比例：

| 以 root 运行能多出来的 | 值不值 |
| --- | --- |
| 功耗（RAPL）不用 ExecStartPre 放开权限 | 不值：现在这条已经把它解决了 |
| 端口归属能显示 root 进程（`docker-proxy`、`sshd` 等） | 不值：为几行进程名把整站交给 root |
| 能读其他用户的命令行 | 不值：非 root 下本机实测 306 个进程全部可读 |

代价是：这是一个**监听 `0.0.0.0`、只有单账号、且没有 TLS** 的 HTTP 服务，
解析表单或 JSON 的任何一处疏漏都会变成 root 级别的漏洞。所以：

- 想严格最小权限：保持 `User=xiaji` + unit 里的 `NoNewPrivileges` / `ProtectSystem=full` / `PrivateTmp`，
  只额外授 `CAP_NET_RAW`（局域网扫描要发 ICMP）；
- 想让端口归属更全（可选）：再加 `AmbientCapabilities=CAP_NET_RAW CAP_SYS_PTRACE`。
  但要知道 **`CAP_SYS_PTRACE` 能读写任意进程的内存，读权限上几乎等价于 root**，
  只是不能改系统文件——是否接受由你判断。

### 9. 排错对照表

| 现象 | 原因与处理 |
| --- | --- |
| 服务启动失败/被跳过，日志里有「拒绝以 root 运行」 | `User=` 被删掉或留空了，补上 `User=你的账号` 即可 |
| 反代后所有人都登不进来（429） | 忘了设 `DASHBOARD_TRUST_PROXY=1`，限速把所有人算成了代理一个 IP |
| 凭据损坏后服务起不来 | 这是有意为之（避免静默覆盖丢账号）；看提示里的 `auth.json.corrupt-*` 备份，确认要重置就移走 `auth.json` 再启动 |
| 服务起不来，端口被占 | 还有 run.sh 起的实例：`./run.sh stop` 或 `ss -tlnp \| grep 8282` 看看是谁 |
| 页面「局域网设备」只有邻居表 | 少了 `AmbientCapabilities=CAP_NET_RAW`，或本机没装 `iputils-ping` |
| 容器卡显示 permission denied | 服务用户不在 `docker` 组，或 docker 未启动（unit 的 `After=docker.service` 已处理顺序） |
| 功耗显示「不可用」 | 看 journald 里 auth/power 相关行；多数是 RAPL 权限，用上面的 ExecStartPre 或 udev |
| 提示 401 / 老是跳登录页 | 会话默认 7 天；改了密码会把其他设备踢下线，重新登录即可 |
| 局域网扫描不到设备 | 只在物理网卡所在网段扫；访客网络/AP 隔离会挡住；路由器没开 UPnP 时看不到 SSDP 结果 |

## 方式二：Docker（想统一用 compose 管理时才选）

`deploy/docker-compose.yml` + `deploy/Dockerfile` 已经写好，代价写在上面那张表里。要点：

- `pid: host` 与 `network_mode: host` 必须开，否则进程与网络指标是空的；
- 必须挂 `/proc`、`/sys`（只读）以及 `docker.sock`——**挂上 sock 就相当于给了容器 root 能力**，
  请确认你接受这一点；
- 挂 `/etc/os-release:ro`，否则设备页会显示容器镜像的系统而不是宿主机的；
- 服务页的「系统服务」在容器里读不到 systemd，会显示不可用；
- 功耗仍需要在**宿主机**上放开 RAPL 读权限（容器里改不了 sysfs）。

```bash
cd deploy
docker compose up -d --build
docker compose logs -f | grep auth      # 首次启动的初始账号密码
```

## 账号与会话（两种方式通用）

- 凭据只存 PBKDF2-SHA256 哈希（20 万轮 + 随机盐），文件权限 600；
- 会话 7 天、最多 20 个，存在同一个文件里（重启服务不会掉登录）；
- 改密码会踢掉其他设备；登录失败按来源 IP 限速（5 分钟 5 次）；
- 状态变更接口要求 JSON + 同源，挡 CSRF。
