#!/usr/bin/env bash
# Docker 入口脚本。
#
# 两种用法：
#   1) 不带参数：进入定时循环，每隔 SYNC_INTERVAL_SECONDS 秒执行一次 deploy/sync.sh；
#   2) 带参数  ：直接执行该命令（exec），用于一次性运维/授权命令，例如
#        docker compose run --rm app python -m wecom_todo_sync doctor
#        docker compose run --rm -it app wecom-cli auth init --noninteractive ...
#
# 首次启动时会从 config.example.json 生成 config.json，并建好数据目录。

set -uo pipefail

APP_DIR="${APP_DIR:-/app}"
cd "$APP_DIR" || exit 1

PYTHON_BIN="${PYTHON_BIN:-python3}"

# 企业微信 CLI 的凭证目录（config.json / credentials.enc / encryption-key）
# 放在 data/ 下由 bind mount 持久化，容器重建后不用重新扫码。
: "${WECOM_CLI_CONFIG_DIR:=${APP_DIR}/data/wecom-cli}"
export WECOM_CLI_CONFIG_DIR
: "${WECOM_CLI_LOG_DIR:=${APP_DIR}/logs/wecom-cli}"
export WECOM_CLI_LOG_DIR

mkdir -p "$WECOM_CLI_CONFIG_DIR" "$WECOM_CLI_LOG_DIR" \
         "$APP_DIR/data/raw" "$APP_DIR/exports" "$APP_DIR/logs" 2>/dev/null

# ---- 配置目录（挂载在 /app/config，/app/config.json 是指向它的软链）----
CONFIG_DIR="${CONFIG_DIR:-${APP_DIR}/config}"
mkdir -p "$CONFIG_DIR" 2>/dev/null
if [ ! -f "$CONFIG_DIR/config.json" ] && [ -f "$APP_DIR/config.example.json" ]; then
  if cp "$APP_DIR/config.example.json" "$CONFIG_DIR/config.json" 2>/dev/null; then
    chmod 600 "$CONFIG_DIR/config.json" 2>/dev/null || true
    echo "[docker] 未找到 config/config.json，已从镜像内的示例生成。"
    echo "[docker] 请填写 feishu.app_id / app_secret / default_assignee_open_id，然后重启容器。"
  else
    echo "[docker] 警告：无法写入 ${CONFIG_DIR}/config.json（检查挂载目录属主与 PUID/PGID）。" >&2
  fi
fi

# ---- 带参数：执行一次性命令 ----
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

INTERVAL="${SYNC_INTERVAL_SECONDS:-300}"
ON_START="${SYNC_ON_START:-1}"

case "$INTERVAL" in
  ''|*[!0-9]*)
    echo "[docker] SYNC_INTERVAL_SECONDS 不是非负整数（当前：$INTERVAL），回退为 300" >&2
    INTERVAL=300
    ;;
esac

term_handler() {
  echo "[docker] 收到退出信号，正在停止…"
  exit 0
}
trap term_handler TERM INT

echo "[docker] 企业微信 CLI 凭证目录：$WECOM_CLI_CONFIG_DIR"

if [ "$INTERVAL" -le 0 ]; then
  echo "[docker] SYNC_INTERVAL_SECONDS=0：只在启动时跑一轮，之后空闲（用 docker compose exec 手动执行）"
  bash "$APP_DIR/deploy/sync.sh"
  while :; do
    sleep 3600 & wait $!
  done
fi

echo "[docker] 定时同步已启动：每 ${INTERVAL}s 一轮（SYNC_ON_START=${ON_START}）"
if [ "$ON_START" = "1" ]; then
  bash "$APP_DIR/deploy/sync.sh"
fi

while :; do
  # sleep 放到后台 + wait，确保 TERM 信号能立刻唤醒循环退出
  sleep "$INTERVAL" & wait $!
  bash "$APP_DIR/deploy/sync.sh"
done