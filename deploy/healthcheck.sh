#!/usr/bin/env bash
# 容器健康检查：判断「这一轮同步跑得起来」的前置条件是否成立。
#
# 检查三件事（任一失败即 unhealthy，便于 docker ps / 监控发现配置问题）：
#   1. data/ 目录可写（SQLite 库、token、CLI 凭证都落在这里）；
#   2. 企业微信 CLI 已授权（未授权时 collect/pull 必然失败）；
#   3. config.json（或环境变量）里的飞书 app_id / app_secret 已填。
#
# 注意：这是「依赖就绪」检查，不是业务检查；还没完成授权/填配置时显示
# unhealthy 是正常的，按 README 完成初始化后会自动变为 healthy。

set -uo pipefail

APP_DIR="${APP_DIR:-/app}"
cd "$APP_DIR" || exit 1

# ---- 1. 数据目录可写 ----
if [ ! -w "$APP_DIR/data" ]; then
  echo "unhealthy: $APP_DIR/data 不可写（检查 bind mount 属主与 PUID/PGID）" >&2
  exit 1
fi

# ---- 2. 企业微信 CLI 授权状态 ----
status_out="$(wecom-cli auth show --status 2>/dev/null)"
case "$status_out" in
  *unauthorized*)
    echo "unhealthy: 企业微信 CLI 未授权（执行 wecom-cli auth init）" >&2
    exit 1
    ;;
  *authorized*)
    ;;
  *)
    echo "unhealthy: 无法确认企业微信 CLI 授权状态：${status_out:-（无输出）}" >&2
    exit 1
    ;;
esac

# ---- 3. 飞书应用凭证 ----
if ! python3 - <<'PY'
import sys

from wecom_todo_sync.config import load_config

cfg, _ = load_config()
feishu = cfg.get("feishu") or {}
problems = []
if not str(feishu.get("app_id") or "").strip():
    problems.append("feishu.app_id")
if not str(feishu.get("app_secret") or "").strip():
    problems.append("feishu.app_secret")
if problems:
    print("unhealthy: 缺少 " + " / ".join(problems), file=sys.stderr)
    sys.exit(1)
PY
then
  exit 1
fi

echo "healthy"
exit 0