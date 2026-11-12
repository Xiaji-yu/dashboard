#!/usr/bin/env bash
# deploy/install.sh 的测试：只跑 dry-run / 参数校验这类不需要 sudo 的路径。
#
# 覆盖：
#   - --help / 非法参数 / 非法路径的报错文案
#   - --state-dir 的两条拒绝规则（相对路径、放进仓库内）
#   - dry-run 生成的 unit 内容（User/WorkingDirectory/ExecStart/端口/状态目录两行）
#   - dry-run 不带 --state-dir 时 unit 里不能出现状态目录两行
#   - --dry-run --uninstall 的输出
#
# 运行：bash tests/deploy/install.test.sh（不需要 sudo，不改动任何文件）

set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
REPO="$(pwd)"
INSTALL="$REPO/deploy/install.sh"

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf 'ok   - %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL - %s\n' "$1" >&2; }

run_install() {
  GIT_TERMINAL_PROMPT=0 "$INSTALL" "$@" 2>&1
}

contains() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }

# ---------- 用法 / 参数校验 ----------
out="$(run_install --help)"; rc=$?
if [ "$rc" -eq 0 ] && contains "$out" "一键部署"; then ok "--help 正常退出并带说明"; else fail "--help (rc=$rc)"; fi

out="$(run_install --nope)"; rc=$?
if [ "$rc" -ne 0 ] && contains "$out" "未知参数"; then ok "未知参数被拒绝"; else fail "未知参数 (rc=$rc)"; fi

out="$(run_install --port abc)"; rc=$?
if [ "$rc" -ne 0 ] && contains "$out" "端口不合法"; then ok "非法端口被拒绝"; else fail "非法端口 (rc=$rc)"; fi

out="$(run_install --base-path dashboard)"; rc=$?
if [ "$rc" -ne 0 ] && contains "$out" "--base-path"; then ok "非法 base-path 被拒绝"; else fail "非法 base-path (rc=$rc)"; fi

out="$(run_install --state-dir ./data)"; rc=$?
if [ "$rc" -ne 0 ] && contains "$out" "绝对路径"; then ok "相对路径 state-dir 被拒绝"; else fail "相对路径 state-dir (rc=$rc)"; fi

out="$(run_install --dry-run --state-dir "$REPO/data")"; rc=$?
if [ "$rc" -ne 0 ] && contains "$out" "不能放在仓库里"; then ok "仓库内 state-dir 被拒绝"; else fail "仓库内 state-dir (rc=$rc)"; fi

# ---------- dry-run：生成的 unit 内容 ----------
out="$(run_install --dry-run --port 9911 --host 127.0.0.1 --user nobody --base-path /dash --trust-proxy \
        --state-dir /srv/dashboard-state)"; rc=$?
if [ "$rc" -ne 0 ]; then fail "带参数 dry-run 失败 (rc=$rc)"; fi
check() { # $1 描述  $2 期望子串
  if contains "$out" "$2"; then ok "$1"; else fail "$1（缺：$2）"; fi
}
check "unit 写入 User=nobody" "| User=nobody"
check "unit 写入工作目录" "| WorkingDirectory=$REPO"
check "unit 写入端口 9911" "| Environment=DASHBOARD_PORT=9911"
check "unit 写入监听地址" "| Environment=DASHBOARD_HOST=127.0.0.1"
check "unit 写入 base-path" "| Environment=DASHBOARD_BASE_PATH=/dash"
check "unit 写入 trust-proxy" "| Environment=DASHBOARD_TRUST_PROXY=1"
check "unit 写入 AUTH_FILE" "| Environment=DASHBOARD_AUTH_FILE=/srv/dashboard-state/auth.json"
check "unit 写入 PROBES" "| Environment=DASHBOARD_PROBES=/srv/dashboard-state/probes.json"
check "ExecStart 用模板里的 python" "| ExecStart=/usr/bin/python3 $REPO/server.py"
check "保留 ExecCondition（拒绝 root）" "拒绝以 root 运行"
check "保留 RAPL ExecStartPre" "energy_uj"
check "dry-run 打印状态目录准备命令" "\$ install -d -m 0700 /srv/dashboard-state"
check "dry-run 明确不改东西" "dry-run 结束：什么都没改"

out="$(run_install --dry-run)"; rc=$?
if [ "$rc" -ne 0 ]; then fail "默认 dry-run 失败 (rc=$rc)"; fi
# 模板里带「注释掉」的示例行，所以要看去掉注释后的正文行
if contains "$out" "| Environment=DASHBOARD_AUTH_FILE=" \
   || contains "$out" "| Environment=DASHBOARD_PROBES="; then
  fail "未指定 --state-dir 时 unit 不应有状态目录两行"
else
  ok "未指定 --state-dir 时 unit 无状态目录两行"
fi
check "dry-run 明确不改东西" "dry-run 结束：什么都没改"

# ---------- dry-run：卸载 ----------
out="$(run_install --dry-run --uninstall)"; rc=$?
if [ "$rc" -eq 0 ] && contains "$out" "systemctl disable --now dashboard" \
   && contains "$out" "什么都没有改"; then
  ok "卸载 dry-run 打印命令且不改东西"
else
  fail "卸载 dry-run (rc=$rc)"
fi

echo
echo "install.test.sh：$PASS 通过，$FAIL 失败"
[ "$FAIL" -eq 0 ]
