# 贡献指南

感谢有兴趣改进这个项目。下面是本地开发与提交的基本约定。

## 环境准备

```bash
git clone https://github.com/Xiaji-yu/dashboard.git
cd dashboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

只需要 Python 3.9+ 与 `psutil`；开发额外需要 `pytest`、`ruff`（可选，测试用 `unittest` 就能跑）。

## 本地验证

提交前请确保这些都通过（与 CI 门禁一致）：

```bash
python3 -m unittest discover -v        # 287 个用例，本机约 40 秒
node tests/chart.test.js               # 前端图表用例
node tests/login.test.js               # 登录页回归（含「响应丢失不误报」路径）
bash tests/deploy/install.test.sh      # 部署脚本：参数校验与 dry-run 生成的 unit
bash tests/deploy/update.test.sh       # 部署脚本：沙箱集成（真实 git + 桩 systemd）
ruff check .                           # 无告警
shellcheck run.sh deploy/*.sh          # 需自行安装 shellcheck（CI 里必跑）
```

## CI 门禁与代码审查

- `ci.yml` 的每个 job 汇总到 **`CI / gate`**：建议在仓库 Settings → Branches → Branch protection
  里把它设为必须通过的检查，任何一个 job 失败 PR 都无法合并。
- `review.yml` 对每个 PR 自动审查：gitleaks 密钥扫描、pip-audit 依赖漏洞、shellcheck，
  结果汇总成一条 PR 评论（重复触发会更新同一条，不刷屏）。这三项有问题会让审查失败。
- AI 评审是**可选**且不阻塞：给仓库配密钥 `REVIEW_LLM_API_KEY`（默认智谱开放平台，
  可用变量 `REVIEW_LLM_BASE_URL` / `REVIEW_LLM_MODEL` 换任何 OpenAI 兼容服务）后自动启用，
  实现在 `tools/ai_review.py`。
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

## 仓库元数据

- 名称 / 版本 / 依赖 / 许可证 / 链接集中在 `pyproject.toml` 的 `[project]` 表（本项目不打包分发，
  这些只是元数据，安装路径仍是 `requirements.txt` + 直接跑脚本）；
- **发版时同步 `version` 与 `CHANGELOG.md`**：把 `Unreleased` 改名成 `x.y.z - 日期` 并打同名 tag；
- 只读文档（README / docs / CONTRIBUTING）里的数字（用例数、分派点数等）改完后顺手核对，
  别让「66 个用例」这种过期数字长期挂着。

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
