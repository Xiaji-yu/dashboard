#!/usr/bin/env bash
# 8282 总控台 启停脚本
set -euo pipefail

# 日志里可能出现首次启动的初始口令，pid 文件也不该人人可写：
# 一律按 600 创建（后面的 chmod 只是兜底已有的旧文件）。
umask 077

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/server.pid"
LOG_FILE="$DIR/server.log"
PORT="${DASHBOARD_PORT:-8282}"
HOST="${DASHBOARD_HOST:-0.0.0.0}"
# 优先用 pid 文件；失效时按命令行**精确**匹配兜底（setsid 后 pid 可能不易追踪）。
#
# 这里不能用 pgrep -f "$PATTERN"：它会匹配任何命令行里含这段文字的进程，
# 包括正在执行本脚本的 shell、编辑器、甚至 tail 日志的终端，
# 实测会把调用者自己当成服务杀掉。精确比较整条命令行才安全。
find_pid() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      printf '%s' "$pid"
      return 0
    fi
  fi
  # 判定条件：进程名是 python3、命令行**恰好两个参数**（解释器 + 本脚本路径）。
  # 这样既认 run.sh 起的 "python3 <路径>"，也认 systemd 单元的 "/usr/bin/python3 <路径>"，
  # 又不会把「命令行里恰好含这段文字」的 shell/编辑器当成服务。
  local pid comm args argv0 argv1 extra
  while read -r pid comm args; do
    [[ "$comm" == "python3" ]] || continue
    read -r argv0 argv1 extra <<< "$args"
    if [[ "$argv0" == *python3 && "$argv1" == "$DIR/server.py" && -z "$extra" ]]; then
      printf '%s' "$pid"
      return 0
    fi
  done < <(ps -eo pid=,comm=,args= 2>/dev/null)
  return 1
}

case "${1:-start}" in
  start)
    pid="$(find_pid || true)"
    if [[ -n "$pid" ]]; then
      echo "总控台已在运行（PID $pid，端口 $PORT）"
      exit 0
    fi
    cd "$DIR"
    touch "$LOG_FILE"; chmod 600 "$LOG_FILE" 2>/dev/null || true
    # setsid：脱离当前会话/进程组，避免父进程被杀时把服务一起带走（nohup 挡不住进程组信号）
    if command -v setsid >/dev/null 2>&1; then
      DASHBOARD_HOST="$HOST" DASHBOARD_PORT="$PORT" \
        setsid nohup python3 "$DIR/server.py" >> "$LOG_FILE" 2>&1 < /dev/null &
    else
      DASHBOARD_HOST="$HOST" DASHBOARD_PORT="$PORT" \
        nohup python3 "$DIR/server.py" >> "$LOG_FILE" 2>&1 < /dev/null &
    fi
    sleep 1.5
    pid="$(find_pid || true)"
    if [[ -n "$pid" ]]; then
      echo "$pid" > "$PID_FILE"
      chmod 600 "$PID_FILE" 2>/dev/null || true
      echo "已启动：http://127.0.0.1:$PORT/  （PID $pid，日志 $LOG_FILE）"
    else
      if grep -q "Address already in use" "$LOG_FILE" 2>/dev/null; then
        echo "启动失败：端口 $PORT 已被占用。"
        if systemctl is-active --quiet dashboard 2>/dev/null; then
          echo "  看起来是 systemd 在托管（dashboard.service）——请不要再用 run.sh 启停："
          echo "    sudo systemctl restart dashboard"
        else
          echo "  用 ss -tlnp | grep $PORT 看是谁占用。"
        fi
      else
        echo "启动失败，日志末尾："
      fi
      tail -n 20 "$LOG_FILE" || true
      exit 1
    fi
    ;;
  stop)
    pid="$(find_pid || true)"
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
      sleep 0.5
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
      rm -f "$PID_FILE"
      echo "已停止（PID $pid）"
    else
      rm -f "$PID_FILE"
      echo "没有在运行"
    fi
    ;;
  restart)
    "$0" stop || true
    sleep 1
    "$0" start
    ;;
  status)
    pid="$(find_pid || true)"
    if [[ -n "$pid" ]]; then
      echo "运行中（PID $pid，端口 $PORT）"
      curl -s -m 5 "http://127.0.0.1:$PORT/api/overview" | head -c 200; echo
    else
      echo "未运行"
      exit 1
    fi
    ;;
  fg)
    cd "$DIR"
    exec python3 "$DIR/server.py"
    ;;
  log)
    tail -n "${2:-40}" "$LOG_FILE"
    ;;
  *)
    echo "用法：$0 {start|stop|restart|status|fg|log}"
    exit 1
    ;;
esac
