#!/usr/bin/env bash
# 8282 总控台 启停脚本
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/server.pid"
LOG_FILE="$DIR/server.log"
PORT="${DASHBOARD_PORT:-8282}"
HOST="${DASHBOARD_HOST:-0.0.0.0}"
PATTERN="python3 $DIR/server.py"

# 优先用 pid 文件；失效时按命令行匹配兜底（setsid 后 pid 可能不易追踪）
find_pid() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      printf '%s' "$pid"
      return 0
    fi
  fi
  pgrep -f "$PATTERN" 2>/dev/null | head -1
}

case "${1:-start}" in
  start)
    pid="$(find_pid || true)"
    if [[ -n "$pid" ]]; then
      echo "总控台已在运行（PID $pid，端口 $PORT）"
      exit 0
    fi
    cd "$DIR"
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
      echo "已启动：http://127.0.0.1:$PORT/  （PID $pid，日志 $LOG_FILE）"
    else
      echo "启动失败，日志末尾："
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
