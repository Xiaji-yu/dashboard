#!/usr/bin/env bash
# deploy/update.sh 的集成测试：在沙箱里跑真实 git，systemd 用桩替身。
#
# 覆盖：
#   - unit 不存在 / 工作目录不一致 → 拒绝（--force 可绕过目录不一致）
#   - 工作区脏 → 拒绝；--force 放行
#   - 正常更新：pull → 重启 → 健康检查通过
#   - 已是最新 → 直接退出
#   - 本地有未推送提交 → 拒绝 ff-only
#   - 健康检查失败 → 自动回滚到旧提交并再次重启
#
# 运行：bash tests/deploy/update.test.sh（不需要 sudo，不改动系统）

set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
UPDATE="$(pwd)/deploy/update.sh"

WORK="$(mktemp -d /tmp/dashboard-update-test.XXXXXX)"
SRV_PID=""
cleanup() {
  [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf 'ok   - %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL - %s\n' "$1" >&2; }
contains() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }

# ---------- 桩：systemd / sudo / journalctl ----------
BIN="$WORK/bin"
mkdir -p "$BIN"
STUB_LOG="$WORK/stub.log"
: > "$STUB_LOG"

cat > "$BIN/systemctl" <<'STUB'
#!/usr/bin/env bash
printf '%s systemctl %s\n' "$(date +%s%3N)" "$*" >> "${STUB_LOG:?}"
if [[ "${1:-}" == show ]]; then
  key=""
  args=("$@")
  for i in "${!args[@]}"; do
    [[ "${args[$i]}" == -p ]] && key="${args[$((i + 1))]}"
  done
  if [[ -n "$key" ]]; then
    grep -m1 "^${key}=" "${STUB_UNIT_ENV:?}" | cut -d= -f2-
  fi
  exit 0
fi
exit 0
STUB

cat > "$BIN/sudo" <<'STUB'
#!/usr/bin/env bash
exec "$@"
STUB

cat > "$BIN/journalctl" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$BIN/systemctl" "$BIN/sudo" "$BIN/journalctl"

# 沙箱 venv：update.sh 会按 unit 里的 ExecStart 找解释器并检查 psutil，
# CI 的系统 python 没有 psutil，所以给它一个确定可用的解释器
VENV="$WORK/venv"
if ! python3 -m venv "$VENV"; then
  echo "无法创建沙箱 venv（缺 python3-venv？）" >&2
  exit 1
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check -q psutil \
  || { echo "沙箱 venv 安装 psutil 失败" >&2; exit 1; }

# unit.env：测试写的「systemd 里那条 unit」
write_unit_env() { # $1 wc  $2 端口  [$3 WorkingDirectory]  [$4 LoadState（absent=unit 不存在）]
  cat > "$WORK/unit.env" <<EOF
LoadState=${4:-loaded}
ActiveState=${4:-active}
User=$(id -un)
WorkingDirectory=${3:-$1}
Environment=DASHBOARD_HOST=127.0.0.1 DASHBOARD_PORT=$2 DASHBOARD_INTERVAL=1.0
ExecStart={ path=$VENV/bin/python ; argv[]=$VENV/bin/python $1/server.py ; ignore_errors=no ; start_time=[x] ; stop_time=[n/a] ; pid=1 ; code=(null) ; status=0/0 }
EOF
}

restart_count() { grep -c "systemctl restart" "$STUB_LOG" 2>/dev/null || true; }

# ---------- 沙箱：origin(bare) + dev（制造上游提交）+ wc（被更新的工作副本） ----------
SB=""
WC=""
new_sandbox() { # $1 名字
  local sb="$WORK/$1"
  rm -rf "$sb"
  mkdir -p "$sb"
  # 不依赖 init.defaultBranch（各机器默认分支名不同），直接把 HEAD 指到 main
  git init -q --bare "$sb/origin.git"
  git -C "$sb/origin.git" symbolic-ref HEAD refs/heads/main
  git -c user.email=t@t -c user.name=t clone -q "$sb/origin.git" "$sb/dev"
  git -C "$sb/dev" symbolic-ref HEAD refs/heads/main
  (
    cd "$sb/dev" || exit 1
    echo base > README.md
    echo notes > NOTES.md
    : > requirements.txt
    git -c user.email=t@t -c user.name=t add README.md requirements.txt NOTES.md
    git -c user.email=t@t -c user.name=t commit -qm base
    echo seed >> README.md
    git -c user.email=t@t -c user.name=t commit -qam seed
    git push -q origin main
  )
  git -c user.email=t@t -c user.name=t clone -q "$sb/origin.git" "$sb/wc"
  mkdir -p "$sb/wc/deploy"
  cp "$UPDATE" "$sb/wc/deploy/update.sh"
  git -C "$sb/wc" reset -q --hard HEAD~1   # 工作副本固定落后一个提交
  SB="$sb"
  WC="$sb/wc"
  : > "$STUB_LOG"
}
run_update() { ( cd "$WC" && PATH="$BIN:$PATH" GIT_TERMINAL_PROMPT=0 \
    STUB_LOG="$STUB_LOG" STUB_UNIT_ENV="$WORK/unit.env" "$WC/deploy/update.sh" "$@" ) 2>&1; }
start_health() { # $1 端口
  python3 - "$1" <<'PY' &
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass

HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
  SRV_PID=$!
}
stop_health() {
  [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null
  wait "${SRV_PID:-}" 2>/dev/null
  SRV_PID=""
}

# ---------- t1：unit 不存在 ----------
new_sandbox t1
write_unit_env "$WC" 18765 "" absent
out="$(run_update --dry-run)"; rc=$?
if [ "$rc" -ne 0 ] && contains "$out" "找不到 dashboard.service"; then
  ok "unit 不存在 → 拒绝"
else
  fail "unit 不存在 (rc=$rc)"
fi

# ---------- t2：unit 工作目录与本仓库不一致 ----------
new_sandbox t2
write_unit_env "$WC" 18765 /home/somewhere-else
out="$(run_update --no-restart)"; rc=$?
if [ "$rc" -ne 0 ] && contains "$out" "不一致"; then
  ok "工作目录不一致 → 拒绝"
else
  fail "工作目录不一致 (rc=$rc)"
fi
out="$(run_update --force --no-restart)"; rc=$?
if [ "$rc" -eq 0 ] && contains "$out" "已更新到"; then
  ok "不一致 + --force → 放行并更新"
else
  fail "不一致 + --force (rc=$rc)"
fi

# ---------- t3：工作区脏 ----------
new_sandbox t3
write_unit_env "$WC" 18765
echo dirty >> "$WC/NOTES.md"
out="$(run_update)"; rc=$?
if [ "$rc" -ne 0 ] && contains "$out" "未提交的改动"; then
  ok "工作区脏 → 拒绝"
else
  fail "工作区脏 (rc=$rc)"
fi
out="$(run_update --force --no-restart)"; rc=$?
if [ "$rc" -eq 0 ] && contains "$(cat "$WC/NOTES.md")" "dirty" \
   && contains "$(cat "$WC/README.md")" "seed"; then
  ok "工作区脏 + --force → 继续更新"
else
  fail "工作区脏 + --force (rc=$rc)"
fi

# ---------- t4：正常更新 → 重启 → 健康检查通过 ----------
new_sandbox t4
write_unit_env "$WC" 18765
start_health 18765
out="$(run_update)"; rc=$?
stop_health
if [ "$rc" -eq 0 ] && contains "$out" "健康检查通过" \
   && contains "$out" "更新完成" && [ "$(restart_count)" -eq 1 ]; then
  ok "正常更新：pull + 重启 + 健康检查"
else
  fail "正常更新 (rc=$rc, restarts=$(restart_count))"
fi

# ---------- t5：已是最新 ----------
out="$(run_update)"; rc=$?
if [ "$rc" -eq 0 ] && contains "$out" "已是最新"; then
  ok "无更新时直接退出"
else
  fail "无更新 (rc=$rc)"
fi

# ---------- t6：本地领先 → 拒绝 ff-only ----------
new_sandbox t6
write_unit_env "$WC" 18765
git -C "$WC" -c user.email=t@t -c user.name=t commit -q --allow-empty -m "local only"
out="$(run_update --no-restart)"; rc=$?
if [ "$rc" -ne 0 ] && contains "$out" "未推送提交"; then
  ok "本地领先 → 拒绝 ff-only"
else
  fail "本地领先 (rc=$rc)"
fi

# ---------- t7：健康检查失败 → 自动回滚 ----------
new_sandbox t7
write_unit_env "$WC" 18999        # 没有任何服务在听 18999，健康检查必失败
OLD_HEAD="$(git -C "$WC" rev-parse HEAD)"
out="$(run_update)"; rc=$?
now_head="$(git -C "$WC" rev-parse HEAD)"
if [ "$rc" -ne 0 ] && contains "$out" "回滚" \
   && [ "$now_head" = "$OLD_HEAD" ] && [ "$(restart_count)" -eq 2 ]; then
  ok "健康检查失败 → 回滚 + 二次重启"
else
  fail "健康检查失败回滚 (rc=$rc, old=$OLD_HEAD, head=$now_head, restarts=$(restart_count))"
fi

echo
echo "update.test.sh：$PASS 通过，$FAIL 失败"
[ "$FAIL" -eq 0 ]
