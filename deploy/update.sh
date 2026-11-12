#!/usr/bin/env bash
# 8282 总控台 更新脚本（服务由 systemd 托管时用）
#
# 做的事：git 拉取最新代码 → 按需补依赖 → 重启服务 → 健康检查（失败自动回滚）
#
# 用法：
#   ./deploy/update.sh                  # 更新到当前分支的上游
#   ./deploy/update.sh --dry-run        # 只看会拉哪些提交、要执行什么，不做任何改动
#   ./deploy/update.sh --no-restart     # 只拉代码与依赖，不重启服务
#   ./deploy/update.sh --no-rollback    # 健康检查失败时不自动回滚
#   ./deploy/update.sh --force          # 工作区有未提交改动时也继续（ff-only 仍可能失败）
#   ./deploy/update.sh --service name   # 单元名不是 dashboard 时指定
#
# 约定：
# - **工作区有未提交改动时默认拒绝更新**（避免把线上的改动覆盖掉/合出冲突），--force 可跳过；
# - 只做 `git pull --ff-only`：本地有未推送提交、或需要合并时直接报错，不替你做合并或强推；
# - 不碰数据：auth.json、probes.json、server.log、.venv 都保持原样（依赖只在 requirements 变化时补）；
# - 脚本本身不用 sudo 启动：只有 systemctl 那几步自己提权；git 始终以服务用户执行；
# - 健康检查用公开的 /api/auth（200 即算活着）；失败时在「更新前工作区干净」的前提下自动
#   `git reset --hard` 回滚到更新前的提交并重启，回滚后仍不健康就把日志打出来。

set -euo pipefail
export GIT_TERMINAL_PROMPT=0

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="dashboard"
VENV="$REPO/.venv"
DRY_RUN=0
FORCE=0
DO_RESTART=1
ROLLBACK=1

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

while (($#)); do
  case "$1" in
    --service) [[ $# -ge 2 ]] || die "--service 需要单元名"; UNIT_NAME="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    --no-restart) DO_RESTART=0; shift ;;
    --no-rollback) ROLLBACK=0; shift ;;
    -h|--help) usage ;;
    *) die "未知参数：$1（--help 看用法）" ;;
  esac
done

# ---------- 服务与身份 ----------
step "检查 systemd 服务 $UNIT_NAME"
command -v systemctl >/dev/null 2>&1 || die "没有 systemctl：服务不是 systemd 托管，请用 ./run.sh restart 或 deploy/install.sh"
[[ "$(systemctl show "$UNIT_NAME" -p LoadState --value 2>/dev/null || true)" == "loaded" ]] \
  || die "找不到 $UNIT_NAME.service：先跑 ./deploy/install.sh 装成 systemd 服务"

UNIT_USER="$(systemctl show "$UNIT_NAME" -p User --value 2>/dev/null || true)"
UNIT_WD="$(systemctl show "$UNIT_NAME" -p WorkingDirectory --value 2>/dev/null || true)"
UNIT_ENV="$(systemctl show "$UNIT_NAME" -p Environment --value 2>/dev/null || true)"
UNIT_EXEC="$(systemctl show "$UNIT_NAME" -p ExecStart --value 2>/dev/null || true)"
UNIT_STATE="$(systemctl show "$UNIT_NAME" -p ActiveState --value 2>/dev/null || true)"
[[ -n "$UNIT_USER" ]] || UNIT_USER="${SUDO_USER:-$(id -un)}"

# 服务实际跑的不是这个仓库：拉代码也不会生效，直接拦下来
if [[ -n "$UNIT_WD" && "$UNIT_WD" != "$REPO" ]]; then
  ((FORCE)) || die "服务的工作目录是 $UNIT_WD，与当前仓库 $REPO 不一致（不在同一个 checkout？加 --force 仍继续）"
fi
say "服务用户：$UNIT_USER ｜ 状态：$UNIT_STATE ｜ 工作目录：${UNIT_WD:-?}"
if ((EUID != 0)) && [[ "$UNIT_USER" != "$(id -un)" ]]; then
  say "提示：当前是 $(id -un)，服务用户是 $UNIT_USER；git/pip 会以当前身份执行，注意文件归属。"
fi

# 以服务用户执行 git / pip（脚本被 sudo 跑时切回去，避免 root 拥有仓库文件）
user_exec() {
  if ((EUID == 0)) && [[ "$UNIT_USER" != "root" ]]; then
    sudo -u "$UNIT_USER" -H "$@"
  else
    "$@"
  fi
}
git_q() {  # 带输出执行（用于读取状态）
  user_exec git -C "$REPO" "$@"
}
git_do() {  # 会改变仓库（或需要体现 dry-run）
  if ((DRY_RUN)); then printf '  $ git -C %s %s\n' "$REPO" "$*"; return 0; fi
  git_q "$@"
}
as_root() {
  if ((DRY_RUN)); then
    if ((EUID == 0)); then printf '  $ %s\n' "$*"; else printf '  $ sudo %s\n' "$*"; fi
    return 0
  fi
  if ((EUID == 0)); then "$@"; else sudo "$@"; fi
}

# 提权只发生在最后重启那一步（这里先确认有 sudo；真正的密码提示留到重启前）
if ((!DRY_RUN)) && ((DO_RESTART)) && ((EUID != 0)); then
  command -v sudo >/dev/null 2>&1 || die "需要 sudo 才能重启服务（或用 --no-restart 只拉代码）"
fi

# ---------- 仓库 ----------
step "检查仓库"
git_q rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$REPO 不是 git 仓库（exe/Docker 部署请用各自的方式更新）"
BRANCH="$(git_q rev-parse --abbrev-ref HEAD)"
[[ "$BRANCH" != "HEAD" ]] || die "当前是 detached HEAD，先 git checkout 到分支再更新"

DIRTY="$(git_q status --porcelain --untracked-files=no)"
if [[ -n "$DIRTY" ]]; then
  if ((FORCE)); then
    say "警告：工作区有未提交改动，按 --force 继续（只做 ff-only 拉取，冲突就报错）"
    say "$DIRTY" | sed 's/^/  /'
  else
    printf '未提交的改动会挡住更新：\n' >&2
    printf '%s\n' "$DIRTY" | sed 's/^/  /' >&2
    die "先提交/暂存（git stash）或加 --force 再更新"
  fi
fi

step "拉取远程信息（$BRANCH）"
# 上游可能是 origin/main，也可能是别的远程（upstream/xxx）：拆成远程名 + 分支名再 fetch/pull
UPSTREAM="$(git_q rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
if [[ -n "$UPSTREAM" ]]; then
  REMOTE="${UPSTREAM%%/*}"
  REMOTE_REF="${UPSTREAM#*/}"
else
  REMOTE="origin"
  REMOTE_REF="$BRANCH"
fi
git_q remote | grep -qx "$REMOTE" || die "没有名为 $REMOTE 的远程：git remote -v 看一下"
git_q fetch --prune "$REMOTE" \
  || die "git fetch $REMOTE 失败：检查网络 / SSH key / 远程地址（git remote -v）"
UPSTREAM="$REMOTE/$REMOTE_REF"
git_q rev-parse --verify --quiet "$UPSTREAM" >/dev/null \
  || die "远程没有 $UPSTREAM：检查 git remote / 分支名"

BEHIND="$(git_q rev-list --count "HEAD..$UPSTREAM")"
AHEAD="$(git_q rev-list --count "$UPSTREAM..HEAD")"
((AHEAD == 0)) || die "本地有 $AHEAD 个未推送提交，不能用 ff-only 更新：先处理这些提交（push 或 rebase）再更新"

OLD_HEAD="$(git_q rev-parse HEAD)"
if ((BEHIND == 0)); then
  say "已是最新（$BRANCH @ ${OLD_HEAD:0:9}），没有要更新的内容。"
  exit 0
fi

say "将更新 ${OLD_HEAD:0:9} → $(git_q rev-parse --short "$UPSTREAM")（$BEHIND 个提交）："
git_q log --oneline --no-decorate "HEAD..$UPSTREAM" | sed 's/^/  /'

if ((DRY_RUN)); then
  step "（dry-run）接下来会做什么"
  say "  - git pull --ff-only（$UPSTREAM）"
  say "  - requirements.txt 变化时补依赖"
  ((DO_RESTART)) && say "  - sudo systemctl restart $UNIT_NAME"
  ((DO_RESTART)) && say "  - 健康检查 /api/auth，失败则回滚到 ${OLD_HEAD:0:9}"
  say ""
  say "dry-run 结束：什么都没改。"
  exit 0
fi

# ---------- 更新 ----------
REQ_BEFORE="$(sha256sum "$REPO/requirements.txt" 2>/dev/null | awk '{print $1}')"
step "拉取代码（git pull --ff-only）"
git_do pull --ff-only --no-rebase "$REMOTE" "$REMOTE_REF"
NEW_HEAD="$(git_q rev-parse HEAD)"
say "已更新到 ${NEW_HEAD:0:9}"

# 解释器：优先项目 .venv，其次 unit 里实际写的那个
PY=""
if [[ -x "$VENV/bin/python" ]]; then
  PY="$VENV/bin/python"
else
  PY="$(printf '%s' "$UNIT_EXEC" | sed -n 's/^{ path=\([^ ]*\) ;.*/\1/p')"
  [[ -n "$PY" ]] || PY="$(command -v python3 || true)"
fi
[[ -n "$PY" ]] || die "找不到 python3，无法继续（先跑 ./deploy/install.sh 修好环境）"

REQ_AFTER="$(sha256sum "$REPO/requirements.txt" 2>/dev/null | awk '{print $1}')"
need_deps=0
[[ "$REQ_BEFORE" != "$REQ_AFTER" ]] && need_deps=1
user_exec "$PY" -c 'import psutil' >/dev/null 2>&1 || need_deps=1
if ((need_deps)); then
  step "补依赖（$PY -m pip install -r requirements.txt）"
  user_exec "$PY" -m pip install --disable-pip-version-check -q -r "$REPO/requirements.txt" \
    || die "装依赖失败：可能需要在项目里建 venv（重跑 ./deploy/install.sh）或检查网络/pip 配置"
else
  say "依赖没变化，跳过 pip。"
fi

# unit 模板或安装脚本变了：提示同步（不会自动改，免得覆盖你本机的端口/用户配置）
if git_q diff --name-only "$OLD_HEAD" "$NEW_HEAD" | grep -qxE 'deploy/(dashboard\.service|install\.sh)'; then
  say ""
  say "注意：本次更新改了 deploy/ 下的 unit 模板或安装脚本。"
  say "      如果要同步到本机服务配置：./deploy/install.sh（会重启服务）"
fi

# ---------- 重启 + 健康检查 ----------
SVC_HOST="$(printf '%s' "$UNIT_ENV" | tr ' ' '\n' | sed -n 's/^DASHBOARD_HOST=//p' | head -1)"
PORT="$(printf '%s' "$UNIT_ENV" | tr ' ' '\n' | sed -n 's/^DASHBOARD_PORT=//p' | head -1)"
PORT="${PORT:-8282}"
case "${SVC_HOST:-0.0.0.0}" in
  0.0.0.0|::|"[::]") SVC_HOST="127.0.0.1" ;;
esac
if [[ "$SVC_HOST" != *:* ]]; then
  HEALTH_URL="http://$SVC_HOST:$PORT/api/auth"
else
  HEALTH_URL="http://[$SVC_HOST]:$PORT/api/auth"
fi

journal_tail() {
  if ((EUID == 0)); then journalctl -u "$UNIT_NAME" -n "${1:-30}" --no-pager || true
  else sudo journalctl -u "$UNIT_NAME" -n "${1:-30}" --no-pager || true
  fi
}

health() {
  local i
  for i in $(seq 1 15); do
    if "$PY" -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=3).read(1)' \
        "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

if ((DO_RESTART)); then
  step "重启 $UNIT_NAME 并做健康检查（$HEALTH_URL）"
  as_root systemctl restart "$UNIT_NAME"
  if health; then
    say "健康检查通过。"
  else
    printf '\n健康检查失败，最近日志：\n'
    journal_tail 30
    if ((ROLLBACK)) && [[ -z "$DIRTY" ]]; then
      step "回滚到 ${OLD_HEAD:0:9}"
      git_do reset --hard "$OLD_HEAD"
      if [[ "$REQ_BEFORE" != "$(sha256sum "$REPO/requirements.txt" 2>/dev/null | awk '{print $1}')" ]]; then
        user_exec "$PY" -m pip install --disable-pip-version-check -q -r "$REPO/requirements.txt" || true
      fi
      as_root systemctl restart "$UNIT_NAME"
      if health; then
        say "已回滚到 ${OLD_HEAD:0:9}，服务恢复正常。"
        die "更新失败（已回滚），先查上面的日志再重试"
      fi
      die "回滚后仍不健康：看 sudo journalctl -u $UNIT_NAME -n 100"
    fi
    die "服务没起来（未回滚）。回滚：git -C $REPO reset --hard $OLD_HEAD && sudo systemctl restart $UNIT_NAME"
  fi
else
  say ""
  say "已跳过重启（--no-restart）：sudo systemctl restart $UNIT_NAME 让新代码生效。"
fi

say ""
say "更新完成：${OLD_HEAD:0:9} → ${NEW_HEAD:0:9} ｜ $BEHIND 个提交 ｜ auth.json 未改动"
