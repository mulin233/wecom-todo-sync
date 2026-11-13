#!/usr/bin/env bash
# 一次性同步脚本：给宝塔「计划任务」调用。
# 用法：bash /www/wwwroot/wecom-todo-sync/deploy/sync.sh
#
# 做三件事：
#   1. 飞书 -> 企业微信（pull --apply）
#   2. 企业微信 -> 飞书（sync --export）
#   3. 输出全部追加到 logs/sync.log，超过 5MB 自动裁剪
#
# 用 flock 防止上一轮还没跑完又叠加一轮。

set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR" || exit 1

# 服务器常是 UTC，这里固定成东八区，否则时间戳会偏 8 小时
export TZ="${TZ:-Asia/Shanghai}"
# cron 的 PATH 很短，把常见的 node / npm 全局目录补上
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:${PATH:-}"

LOG_DIR="$APP_DIR/logs"
DATA_DIR="$APP_DIR/data"
mkdir -p "$LOG_DIR" "$DATA_DIR" "$APP_DIR/exports" 2>/dev/null
LOG_FILE="$LOG_DIR/sync.log"
LOCK_FILE="$DATA_DIR/.sync.lock"

# ---- 防重叠 ----
exec 9>"$LOCK_FILE" 2>/dev/null
if command -v flock >/dev/null 2>&1; then
  if ! flock -n 9; then
    echo "[$(date '+%F %T')] 上一轮仍在执行，本次跳过" >>"$LOG_FILE"
    exit 0
  fi
fi

# ---- 选 Python ----
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -x "$APP_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$APP_DIR/.venv/bin/python"
fi

# ---- 跑同步 ----
{
  echo "===== $(date '+%F %T') 开始 ====="
  "$PYTHON_BIN" -m wecom_todo_sync pull --apply
  pull_rc=$?
  "$PYTHON_BIN" -m wecom_todo_sync sync --export
  sync_rc=$?
  echo "===== $(date '+%F %T') 结束 pull=$pull_rc sync=$sync_rc ====="
} >>"$LOG_FILE" 2>&1

# ---- 日志裁剪（保留最后 2000 行） ----
if [ -f "$LOG_FILE" ]; then
  size=$(wc -c <"$LOG_FILE" 2>/dev/null | tr -d ' ')
  if [ "${size:-0}" -gt 5242880 ]; then
    tail -n 2000 "$LOG_FILE" >"$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE"
  fi
fi

if [ "${pull_rc:-0}" -ne 0 ] || [ "${sync_rc:-0}" -ne 0 ]; then
  exit 1
fi
exit 0
