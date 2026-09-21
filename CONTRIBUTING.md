# 贡献指南

感谢有兴趣改进这个项目。下面是本地开发与提交的基本约定。

## 环境准备

```bash
git clone https://github.com/OWNER/dashboard.git
cd dashboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

只需要 Python 3.9+ 与 `psutil`；开发额外需要 `pytest`、`ruff`（可选，测试用 `unittest` 就能跑）。

## 本地验证

提交前请确保这两条都通过：

```bash
python3 -m unittest discover -v      # 全部用例应在数秒内通过
ruff check .                         # 无告警
```

调试界面时用 `./run.sh fg` 前台运行，日志直接打在终端上。

## 代码约定

- **注释与文档字符串用中文**，与现有代码保持一致。
- 行宽上限 **120**（`pyproject.toml` 里配置），因为中文注释按字符计数会比较宽。
- ruff 规则集为 `E`/`F`/`W`，保持保守；如需放宽请先讨论。
- 前端不使用构建工具、不引 CDN，也不引入任何前端依赖：新增图表请沿用 `static/chart.js` 的手绘 SVG 方式。
- **新增采集项必须处理失败路径**：采不到时返回 `{"available": False, "reason": "..."}`，
  不要抛异常、也不要静默返回 0——页面对应位置会显示「不可用」+ 原因。

## 测试约定

- 用例放在 `tests/`，文件名 `test_*.py`，用标准库 `unittest` 编写（pytest 亦可收集）。
- 涉及真实系统的断言只验证**结构与不变量**（范围、字段、排序），不要断言具体数值。
- 采集/网络/外部命令的失败路径用 `unittest.mock` 构造，保证测试离线可跑、结果稳定。
- 接口测试请用 `server.create_server(collector, "127.0.0.1", 0)`，让内核分配端口，
  不要在测试里绑定固定的 8282。

## 提交信息

采用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/) 前缀：

```
feat: 新增网络与磁盘页面
fix: 修正容器汇总把截断后的数量当总数
docs: 补充 RAPL 权限说明
test: 补 history 环形覆盖用例
chore: 升级 ruff 配置
```

一个提交只做一件事；涉及行为的改动请同时更新 `CHANGELOG.md` 的 `Unreleased` 段落。

## 提交 PR

1. 从 `main` 切出分支，例如 `feat/network-page`。
2. 保证测试与 ruff 通过。
3. PR 描述里写清楚：改了什么、为什么、怎么验证（贴命令或截图）。
4. 如果改动了界面，请附一张新截图（无头浏览器截图命令见 README 的「开发」一节思路即可：
   `chromium --headless --screenshot=... http://127.0.0.1:8282/`）。
