#!/usr/bin/env python3
"""AI 代码评审：读 diff，调用 OpenAI 兼容接口（默认智谱开放平台），输出 markdown 评审意见。

给 .github/workflows/review.yml 使用，也可手动运行：

    REVIEW_LLM_API_KEY=xxx python3 tools/ai_review.py \
        --base https://open.bigmodel.cn/api/paas/v4 \
        --model glm-4.6 \
        --diff /tmp/review.diff \
        --out /tmp/review-ai.md

密钥从环境变量 REVIEW_LLM_API_KEY 读取，不会出现在命令行参数里。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

DEFAULT_BASE = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4.6"

SYSTEM_PROMPT = (
    "你是这个仓库（一个零依赖的个人服务器监控看板：Python 标准库 + 原生前端 + bash 部署脚本）"
    "的资深代码审查者。只依据给出的 diff 审查，重点：安全问题（凭据/注入/越权/危险命令）、"
    "正确性（边界条件、错误处理、并发）、与仓库既有风格的一致性。用中文输出，"
    "格式为 markdown：`## 问题`（按 严重/一般/建议 分级，每条给出文件、行号或代码位置与理由），"
    "`## 结论`（一句话：可以合并 / 建议修改后再合并）。没有问题就明确写「未发现问题」。"
    "不要复述 diff，不要提与 diff 无关的建议。"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 代码评审")
    parser.add_argument("--base", default=DEFAULT_BASE, help="OpenAI 兼容接口根地址")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="模型名")
    parser.add_argument("--diff", required=True, help="diff 文件路径")
    parser.add_argument("--out", required=True, help="评审意见输出路径")
    parser.add_argument("--max-chars", type=int, default=50000, help="diff 截断长度")
    args = parser.parse_args()

    key = os.environ.get("REVIEW_LLM_API_KEY")
    if not key:
        print("未设置 REVIEW_LLM_API_KEY，跳过 AI 评审", file=sys.stderr)
        return 2

    with open(args.diff, encoding="utf-8", errors="replace") as handle:
        diff = handle.read()
    if len(diff) > args.max_chars:
        diff = diff[: args.max_chars] + "\n\n…（diff 过长，此处截断）\n"

    payload = {
        "model": args.model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "请审查下面的变更：\n\n```diff\n" + diff + "\n```"},
        ],
    }
    request = urllib.request.Request(
        args.base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
    )
    with urllib.request.urlopen(request, timeout=180) as resp:
        out = json.load(resp)
    content = out["choices"][0]["message"]["content"]

    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(f"### AI 评审（{args.model}）\n\n{content}\n")
    print(f"AI 评审完成：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
