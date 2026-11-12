#!/usr/bin/env bash
# 8282 总控台 Linux 一键部署（装成 systemd 服务）
#
# 用法：
#   ./deploy/install.sh                     # 装成 systemd 服务（需要时自己调 sudo）
#   sudo ./deploy/install.sh                # 也可以整条 sudo 跑，服务仍用 SUDO_USER 身份
#   ./deploy/install.sh --port 9000         # 换端口
#   ./deploy/install.sh --base-path /dashboard --trust-proxy   # 反向代理子路径
#   ./deploy/install.sh --state-dir ~/apps/dashboard-state    # 生产副本：配置放到仓库外
#   ./deploy/install.sh --rapl-udev         # 另外装 RAPL udev 规则（内核不允许 chmod 时用）
#   ./deploy/install.sh --dry-run           # 只打印要执行的命令和生成的 unit
#   ./deploy/install.sh --uninstall         # 卸载（保留 auth.json 等数据）
#
# 三个设计取舍：
# - **脚本本身不需要 sudo 启动**：只有写 /etc/systemd/system、systemctl、RAPL chmod、
#   udev 规则这些步骤才提权；服务进程始终以普通用户运行（unit 里的 User=），
#   unit 自带的 ExecCondition 也会拒绝以 root 启动。
# - **幂等**：重复运行＝更新 unit + 重启服务，不会装出两份；不会动 auth.json / probes.json。
# - **不碰系统包**：缺 psutil 时在项目里建 .venv 装依赖，不改全局 pip、不调用 apt。

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="dashboard"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}.service"
UNIT_TEMPLATE="$REPO/deploy/dashboard.service"
VENV="$REPO/.venv"

PORT="${DASHBOARD_PORT:-8282}"
HOST="${DASHBOARD_HOST:-0.0.0.0}"
TARGET_USER=""
BASE_PATH=""
STATE_DIR=""
TRUST_PROXY=0
RAPL_UDEV=0
DRY_RUN=0
FORCE=0
UNINSTALL=0

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}
# ---------- 参数 ----------
while (($#)); do
  case "$1" in
    --user) [[ $# -ge 2 ]] || die "--user 需要用户名"; TARGET_USER="$2"; shift 2 ;;
    --port) [[ $# -ge 2 ]] || die "--port 需要端口号"; PORT="$2"; shift 2 ;;
    --host) [[ $# -ge 2 ]] || die "--host 需要监听地址"; HOST="$2"; shift 2 ;;
    --base-path) [[ $# -ge 2 ]] || die "--base-path 需要路径"; BASE_PATH="$2"; shift 2 ;;
    --state-dir) [[ $# -ge 2 ]] || die "--state-dir 需要目录"; STATE_DIR="$2"; shift 2 ;;
    --trust-proxy) TRUST_PROXY=1; shift ;;
    --rapl-udev) RAPL_UDEV=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage ;;
    *) die "未知参数：$1（--help 看用法）" ;;
  esac
done

[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) \
  || die "端口不合法：$PORT"
[[ "$HOST" =~ ^[A-Za-z0-9:._-]+$ ]] || die "监听地址不合法：$HOST"
if [[ -n "$BASE_PATH" ]]; then
  [[ "$BASE_PATH" == /?* && "$BASE_PATH" != */ ]] \
    || die "--base-path 需要形如 /dashboard（以 / 开头、结尾不带 /）"
fi
if [[ -n "$STATE_DIR" ]]; then
  STATE_DIR="${STATE_DIR/#\~/$HOME}"          # 展开 ~
  [[ "$STATE_DIR" = /* ]] || die "--state-dir 需要绝对路径（或 ~/...）"
  case "$STATE_DIR/" in
    "$REPO"/*) die "--state-dir 不能放在仓库里：auth.json 会弄脏工作区，update.sh 会拒绝更新" ;;
  esac
fi

# ---------- 运行身份 ----------
# 服务用户：--user > sudo 的原始用户 > 当前用户；root 直接跑又没给 --user 就不猜
if [[ -z "$TARGET_USER" ]]; then
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    TARGET_USER="$SUDO_USER"
  elif ((EUID != 0)); then
    TARGET_USER="$(id -un)"
  else
    die "以 root 直接运行、又无法推断该用哪个账号跑服务：请加 --user <用户名>"
  fi
fi
id "$TARGET_USER" >/dev/null 2>&1 || die "没有这个用户：$TARGET_USER"

if ! ((DRY_RUN)) && ((EUID != 0)); then
  command -v sudo >/dev/null 2>&1 || die "需要 sudo 才能写入 $UNIT_PATH"
  step "请求 sudo 授权（下面只用于 systemd 与权限相关步骤）"
  sudo -v || die "sudo 授权失败"
fi

# 提权执行；dry-run 只打印
as_root() {
  if ((DRY_RUN)); then
    if ((EUID == 0)); then printf '  $ %s\n' "$*"; else printf '  $ sudo %s\n' "$*"; fi
    return 0
  fi
  if ((EUID == 0)); then "$@"; else sudo "$@"; fi
}
# 以服务用户执行（脚本被 sudo 跑时切回去）
user_exec() {
  if ((EUID == 0)) && [[ "$TARGET_USER" != "root" ]]; then
    sudo -u "$TARGET_USER" -H "$@"
  else
    "$@"
  fi
}
run_user() {
  if ((DRY_RUN)); then printf '  $ %s\n' "$*"; return 0; fi
  user_exec "$@"
}

# ---------- 卸载 ----------
if ((UNINSTALL)); then
  step "卸载 $UNIT_NAME.service"
  as_root systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
  as_root rm -f "$UNIT_PATH"
  as_root systemctl daemon-reload
  as_root systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true
  if ((DRY_RUN)); then
    say "（dry-run）以上为将执行的卸载命令，什么都没有改。"
    exit 0
  fi
  say "已卸载。保留的数据：$REPO/auth.json（账号会话）、probes.json、.venv、server.log"
  say "要彻底清掉账号：rm $REPO/auth.json（下次启动会重新生成初始密码）"
  exit 0
fi

# ---------- 前置检查 ----------
step "检查运行环境"
((DRY_RUN)) || command -v systemctl >/dev/null 2>&1 \
  || die "这台机器没有 systemd（容器里？）——请改用 ./run.sh start 或 deploy/docker-compose.yml"
if ((!DRY_RUN)) && ! systemctl show-environment >/dev/null 2>&1; then
  die "systemd 未运行（容器 / WSL1？）——请改用 ./run.sh start"
fi
[[ -r "$UNIT_TEMPLATE" ]] || die "缺少 $UNIT_TEMPLATE"
for f in server.py auth.py collector.py history.py runtime.py requirements.txt; do
  [[ -r "$REPO/$f" ]] || die "仓库不完整：缺少 $f"
done

PY=""
if [[ -x "$VENV/bin/python" ]]; then
  PY="$VENV/bin/python"
else
  PY="$(command -v python3 || true)"
fi
[[ -n "$PY" ]] || die "找不到 python3，请先安装（apt install python3 python3-venv）"
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
[[ -n "$PYVER" ]] || die "$PY 跑不起来"
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "需要 Python ≥ 3.9，当前 $PYVER"

# 服务用户要能读写项目目录：auth.json / probes.json / server.log 都落在仓库里
if ! ((DRY_RUN)); then
  user_exec test -r "$REPO/server.py" \
    || die "用户 $TARGET_USER 读不到 $REPO（检查目录权限）"
  user_exec test -w "$REPO" \
    || die "用户 $TARGET_USER 对 $REPO 没有写权限（auth.json / probes.json 默认落在这里；用 --state-dir 可放到仓库外）"
fi

# psutil：优先复用项目 .venv；缺了就建一个，不碰系统 pip
if ((!DRY_RUN)) && ! user_exec "$PY" -c 'import psutil' >/dev/null 2>&1; then
  step "安装依赖（$REPO/.venv）"
  if [[ -x "$VENV/bin/python" ]]; then
    run_user "$VENV/bin/python" -m pip install --disable-pip-version-check -q -r "$REPO/requirements.txt" \
      || die "在 .venv 里装 psutil 失败（检查网络 / pip 配置）"
  else
    run_user "$PY" -m venv "$VENV" \
      || die "创建 venv 失败：可能缺 python3-venv（apt install python3-venv）"
    run_user "$VENV/bin/python" -m pip install --disable-pip-version-check -q --upgrade pip >/dev/null 2>&1 || true
    run_user "$VENV/bin/python" -m pip install --disable-pip-version-check -q -r "$REPO/requirements.txt" \
      || die "在 .venv 里装 psutil 失败（检查网络 / pip 配置）"
  fi
  PY="$VENV/bin/python"
fi

# 状态目录：把凭据与探测目标放到仓库外，生产副本（git clone）才能一直保持干净
# —— update.sh 要求工作区干净，而 probes.json 在仓库里是被跟踪的，改了就算脏。
if [[ -n "$STATE_DIR" ]]; then
  step "准备状态目录 $STATE_DIR"
  if ((DRY_RUN)); then
    printf '  $ install -d -m 0700 %s\n' "$STATE_DIR"
    for name in auth.json probes.json; do
      [[ -e "$REPO/$name" ]] && printf '  $ install -m 0600 %s %s\n' "$REPO/$name" "$STATE_DIR/$name"
    done
  else
    user_exec install -d -m 0700 "$STATE_DIR" \
      || die "建不了状态目录 $STATE_DIR（检查父目录是否存在、是否可写）"
    user_exec test -w "$STATE_DIR" || die "用户 $TARGET_USER 对 $STATE_DIR 没有写权限"
    for name in auth.json probes.json; do
      if [[ ! -e "$STATE_DIR/$name" && -e "$REPO/$name" ]]; then
        user_exec install -m 0600 "$REPO/$name" "$STATE_DIR/$name" \
          && say "已迁移 $name → $STATE_DIR/$name"
      fi
    done
  fi
fi

# 旧实例：run.sh 起的进程会和 systemd 抢 8282；systemd 已托管的则不动
unit_active=0
if ((!DRY_RUN)) && systemctl is-active --quiet "$UNIT_NAME"; then unit_active=1; fi
if ((!unit_active)); then
  step "停掉 run.sh 起的旧实例（如果有）"
  run_user "$REPO/run.sh" stop || true
fi

# 端口占用：只在我们没接管的情况下拦一下
if ((!DRY_RUN)) && ((!unit_active)) && command -v ss >/dev/null 2>&1; then
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}\$"; then
    if ((FORCE)); then
      say "警告：端口 $PORT 已被占用，按 --force 继续（启动可能失败）"
    else
      die "端口 $PORT 已被占用：ss -tlnp | grep $PORT 看是谁；换端口用 --port，或加 --force 跳过"
    fi
  fi
fi

# ---------- 生成 unit ----------
step "生成 $UNIT_PATH"
build_unit() {
  awk -v user="$TARGET_USER" -v dir="$REPO" -v py="$PY" \
      -v host="$HOST" -v port="$PORT" -v base="$BASE_PATH" -v trust="$TRUST_PROXY" \
      -v state="$STATE_DIR" '
    /^User=/                     { print "User=" user; next }
    /^WorkingDirectory=/         { print "WorkingDirectory=" dir; next }
    /^ExecStart=/                { print "ExecStart=" py " " dir "/server.py"; next }
    /^Environment=DASHBOARD_HOST=/ { print "Environment=DASHBOARD_HOST=" host; next }
    /^Environment=DASHBOARD_PORT=/ { print "Environment=DASHBOARD_PORT=" port; next }
    /^Environment=DASHBOARD_INTERVAL=/ {
      print
      if (base != "") print "Environment=DASHBOARD_BASE_PATH=" base
      if (trust == "1") print "Environment=DASHBOARD_TRUST_PROXY=1"
      if (state != "") {
        print "Environment=DASHBOARD_AUTH_FILE=" state "/auth.json"
        print "Environment=DASHBOARD_PROBES=" state "/probes.json"
      }
      next
    }
    { print }
  ' "$UNIT_TEMPLATE"
}

UNIT_TMP="$(mktemp)"
trap 'rm -f "$UNIT_TMP"' EXIT
build_unit > "$UNIT_TMP"
grep -q "^User=$TARGET_USER\$" "$UNIT_TMP" || die "unit 生成异常：User= 没写进去"
grep -q "^Environment=DASHBOARD_PORT=$PORT\$" "$UNIT_TMP" || die "unit 生成异常：端口没写进去"
if [[ -n "$STATE_DIR" ]]; then
  grep -q "^Environment=DASHBOARD_AUTH_FILE=$STATE_DIR/auth.json\$" "$UNIT_TMP" \
    || die "unit 生成异常：状态目录没写进去"
fi

changed=1
if [[ -f "$UNIT_PATH" ]] && cmp -s "$UNIT_TMP" "$UNIT_PATH"; then changed=0; fi

if ((DRY_RUN)); then
  say "（dry-run）将写入的 unit："
  sed 's/^/  | /' "$UNIT_TMP"
else
  as_root install -m 0644 -o root -g root "$UNIT_TMP" "$UNIT_PATH"
fi

# ---------- RAPL udev（可选） ----------
if ((RAPL_UDEV)); then
  step "安装 RAPL udev 规则"
  command -v udevadm >/dev/null 2>&1 || die "没有 udevadm，去掉 --rapl-udev 再试"
  as_root install -m 0644 -o root -g root \
    "$REPO/deploy/60-dashboard-rapl.rules" /etc/udev/rules.d/60-dashboard-rapl.rules
  as_root udevadm control --reload-rules
  as_root udevadm trigger --action=add --subsystem-match=powercap
fi

# ---------- 启用 ----------
step "启用并启动服务"
as_root systemctl daemon-reload
as_root systemctl enable "$UNIT_NAME"
if ((changed)); then
  as_root systemctl restart "$UNIT_NAME"
else
  as_root systemctl start "$UNIT_NAME"
fi

if ((DRY_RUN)); then
  say ""
  say "dry-run 结束：什么都没改。去掉 --dry-run 即会按上面的命令执行。"
  exit 0
fi

# ---------- 验证与提示 ----------
sleep 1
if ! systemctl is-active --quiet "$UNIT_NAME"; then
  printf '\n服务没起来，最近日志：\n'
  sudo journalctl -u "$UNIT_NAME" -n 25 --no-pager || true
  exit 1
fi

URLS="http://127.0.0.1:$PORT/"
if [[ "$HOST" == "0.0.0.0" || "$HOST" == "::" ]]; then
  LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [[ -n "$LAN_IP" ]] && URLS="$URLS  http://$LAN_IP:$PORT/"
fi

printf '\n完成：%s 已%s并运行（用户 %s）\n' "$UNIT_NAME" \
  "$([[ $changed == 1 ]] && echo 安装/更新 || echo 已在位)" "$TARGET_USER"
say "访问：$URLS"
if [[ -n "$STATE_DIR" ]]; then
  say "状态目录：$STATE_DIR（auth.json / probes.json 都在这里，不进 git）"
  say "以后重跑本脚本要带同样参数：--state-dir $STATE_DIR"
fi

CREDS="$(sudo journalctl -u "$UNIT_NAME" --no-pager -n 80 2>/dev/null | grep -m1 '初始账号' || true)"
if [[ -n "$CREDS" ]]; then
  say ""
  say "首次登录的初始账号（登录后请立刻在侧栏「账号」里改掉）："
  say "  ${CREDS#*] }"
else
  say ""
  say "初始账号密码只在首次启动时生成，查看：sudo journalctl -u $UNIT_NAME | grep 初始账号"
fi

cat <<EOF

常用命令：
  sudo systemctl status $UNIT_NAME       # 状态
  sudo journalctl -u $UNIT_NAME -f       # 跟日志
  sudo systemctl restart $UNIT_NAME      # 重启
  ./deploy/install.sh --uninstall        # 卸载（保留 auth.json）

安全提醒：
  - 看板自带账号认证但没有 TLS：只放行内网，或套一层反向代理再出网
      sudo ufw allow from 192.168.1.0/24 to any port $PORT proto tcp
  - 服务用户需要 docker 组才能看到容器列表（不给也能跑）
  - 反代之后记得重跑一次并加 --trust-proxy，否则限速会把所有人算成一个 IP
EOF
