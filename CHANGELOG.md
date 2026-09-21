# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与
[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 计划中

- 概览页之外的页面：性能与电源、进程、网络与磁盘、服务、设备、传感器。
- 可选的口令/Token 鉴权，便于把看板安全地暴露到内网之外。
- systemd 用户服务示例。

## [0.1.0] - 2026-09-22

首个版本：概览页可用。

### 新增

- 零依赖 HTTP 服务（Python 标准库 `http.server`），监听 `0.0.0.0:8282`。
- 指标采集层：CPU、内存、整机功耗（Intel RAPL）、网速、磁盘、温度、负载。
  任何一项采不到都返回「不可用 + 原因」，不影响其它指标。
- 增量曲线接口：`/api/series?since=<ts>` 只返回新增数据点。
- 时序环形缓冲：每秒采样，页面随时打开都有最近 120 秒的曲线。
- 深色看板前端：手写 SVG 折线图、渐变填充、随容器拉伸。
- 上半部分 5 条指标铺满全宽；下半部分为「此刻最忙的程序」与「本机服务状态」。
- 服务状态面板：Docker 容器（5 秒缓存）、SSH 与监听端口数、CPU 温度、系统负载。
- 网速默认只统计物理网卡，排除 `docker0`/`br-*`/`veth` 等虚拟接口。
- 启停脚本 `run.sh`（`setsid` 脱离会话，父进程退出不会带走服务）。
- 44 个 `unittest` 用例，覆盖采集、缓冲、接口契约与降级路径。
- GitHub Actions：多 Python 版本跑 ruff 与测试。

[Unreleased]: https://github.com/OWNER/dashboard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OWNER/dashboard/releases/tag/v0.1.0
