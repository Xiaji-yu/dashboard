# 安全策略

## 这个项目默认没有身份验证

这是需要先说清楚的一点：**看板没有任何登录、口令或 Token**。任何能连上该端口的人都可以看到：

- 主机名、监听端口清单、网络接口名
- 全部进程名与资源占用、Docker 容器名与状态
- CPU / 内存 / 磁盘 / 温度等运行指标

因此请按「它等同于一个只读的系统信息接口」来对待。

### 安全暴露方式（推荐）

只监听回环地址，用 SSH 隧道访问，这样流量与认证都走 SSH：

```bash
DASHBOARD_HOST=127.0.0.1 ./run.sh start
# 在本地机器上：
ssh -L 8282:127.0.0.1:8282 user@server
# 然后打开 http://127.0.0.1:8282
```

### 需要局域网访问时

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8282 proto tcp   # 只放行内网网段
```

比起直接用 `ufw allow 8282/tcp`，限定来源网段能显著缩小暴露面。

### 不要做的事

- 不要把 8282 直接映射到公网，或放进路由器的 DMZ。
- 如果确实要公网访问，请在前面加一层带认证的反向代理（如 Nginx + Basic Auth / OAuth），
  或等待上游加入 Token 鉴权（见 `CHANGELOG.md` 的 `Unreleased`）。

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
