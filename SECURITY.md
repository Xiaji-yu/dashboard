# 安全策略

## 当前状态

内置**单账号鉴权**：未登录只能看到登录页，接口一律返回 401。仍然建议按内网自用部署——
服务本身跑明文 HTTP，没有 TLS。

## 鉴权做了什么

| 项 | 实现 |
| --- | --- |
| 口令存储 | PBKDF2-HMAC-SHA256，20 万轮，每用户随机盐；只存哈希，文件权限 600 |
| 初始凭据 | 首次启动生成随机 20 位密码，写入 `auth.json` 并打印到日志；可用 `DASHBOARD_USER` / `DASHBOARD_PASSWORD` 指定 |
| 会话 | 服务端保存的随机令牌（`secrets.token_urlsafe(32)`），Cookie 为 `HttpOnly; SameSite=Lax`，默认 7 天，最多 20 个 |
| 改密码 | 需旧密码；改完踢掉其他设备的会话，当前设备保留 |
| 暴力破解 | 按来源 IP 限速：5 分钟内 5 次失败后拒绝，成功登录清零 |
| CSRF | 状态变更接口要求 `Content-Type: application/json`（跨站表单发不出来），`Origin` 存在时必须同源 |

## 还没做的

- 没有 TLS：请用反向代理终结 HTTPS，或只监听回环地址。
- 只有一组账号，没有多用户、权限分级、操作审计。
- 没有 2FA、没有登录通知、没有密码找回（忘了就删掉 `auth.json` 重启，会重新生成）。

## 这个项目仍然只读

即使登录了，看板也不会修改系统：没有结束进程、重启容器、关机之类的接口。

## 部署建议

### 需要外网访问时（推荐）

只监听回环地址，用 SSH 隧道访问，这样流量与认证都走 SSH：

```bash
DASHBOARD_HOST=127.0.0.1 ./run.sh start
# 在本地机器上：
ssh -L 8282:127.0.0.1:8282 user@server
# 然后打开 http://127.0.0.1:8282
```

### 只在内网使用时

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8282 proto tcp   # 只放行内网网段
```

比起直接用 `ufw allow 8282/tcp`，限定来源网段能显著缩小暴露面。

### 不要做的事

- 不要把 8282 直接映射到公网，或放进路由器的 DMZ：**登录页与口令都是明文传输的**。
- 公网访问请在前面加一层带 TLS 的反向代理（Nginx / Caddy），并保留本项目的登录鉴权。

### 排查是否已被外部访问

```bash
sudo ufw status numbered                       # 看放行规则
journalctl -k | grep 'UFW BLOCK' | grep 8282   # 看是否有被拦的探测
ss -tn state established '( sport = :8282 )'   # 看当前谁连着
```

## 支持的版本

只有最新发布版本接受安全修复。

| 版本 | 支持 |
| --- | --- |
| 0.1.x | ✅ |

## 报告漏洞

请**不要**通过公开 Issue 报告安全问题。优先使用 GitHub 的
[私密漏洞报告](https://docs.github.com/zh/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/about-coordinating-disclosures)
（仓库 **Security** 标签页 → *Report a vulnerability*）。

报告中请包含：

- 影响版本与部署方式（监听地址、是否在反向代理后面）
- 复现步骤或 PoC
- 影响范围（信息泄露 / 拒绝服务 / 其它）

一般会在 7 天内答复。修复发布后会在 `CHANGELOG.md` 中致谢（除非你希望匿名）。
