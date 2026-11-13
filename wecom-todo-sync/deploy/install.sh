#!/usr/bin/env bash
# 服务器初始化：检查依赖、安装 wecom-cli、建目录、跑自检。
# 用法：bash /www/wwwroot/wecom-todo-sync/deploy/install.sh

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR" || exit 1

echo "[1/6] 检查 Python（需要 3.8+）"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' || {
  echo "✗ Python 版本过低：$(python3 -V 2>&1)"
  echo "  宝塔面板 → 软件商店 → Python 项目管理器 里装一个 3.8+ 的版本"
  exit 1
}
echo "    $(python3 -V)"

echo "[2/6] 检查 Node.js（需要 18+）"
if ! command -v node >/dev/null 2>&1; then
  echo "✗ 没有 node。宝塔面板 → 软件商店 → Node.js 版本管理器，装一个 18+ 的版本"
  exit 1
fi
node -e 'const v=Number(process.versions.node.split(".")[0]);process.exit(v>=18?0:1)' || {
  echo "✗ Node 版本过低：$(node -v)"
  exit 1
}
echo "    node $(node -v)"

echo "[3/6] 安装 @wecom/cli"
if command -v wecom-cli >/dev/null 2>&1; then
  echo "    已安装：$(wecom-cli --version)"
else
  npm install -g @wecom/cli
fi

echo "[4/6] 初始化目录与配置"
mkdir -p data/raw logs exports
if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "    已生成 config.json（请填 feishu.app_id / app_secret / open_id / userid）"
else
  echo "    config.json 已存在，保持不变"
fi
chmod 600 config.json 2>/dev/null || true
chmod +x deploy/*.sh 2>/dev/null || true

echo "[5/6] 离线自检"
python3 -m wecom_todo_sync selftest

echo "[6/6] 下一步"
cat <<'EOF'

  ① 企业微信授权（服务器无图形界面，用二维码）：
       wecom-cli auth init --noninteractive --output-qrcode /tmp/wecom-qr.png
     用宝塔文件管理器把 /tmp/wecom-qr.png 下载下来，用企业微信扫码。
     然后确认：wecom-cli auth show --status   → authorized

  ② 飞书用户授权（pull 需要）：
     在你自己电脑上跑 python -m wecom_todo_sync auth，
     然后把 data/user_token.json 上传到服务器同路径。

  ③ 配 config.json：feishu.app_id / app_secret / default_assignee_open_id
                    reverse.default_follower_userid

  ④ 手动跑一次，确认能通：
       bash deploy/sync.sh && tail -n 30 logs/sync.log

  ⑤ 宝塔 → 计划任务 → 添加任务
       类型：Shell 脚本    周期：N 分钟（建议 5）
       脚本：bash /www/wwwroot/wecom-todo-sync/deploy/sync.sh

EOF
