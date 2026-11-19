#!/usr/bin/env bash
# 构建镜像（默认全部走国内镜像源）。
#
# 用法：
#   ./build.sh                  # 构建 wecom-todo-sync:0.1
#   ./build.sh --no-cache       # 额外参数原样传给 docker build
#
# 可用环境变量覆盖：
#   BASE_IMAGE         基础镜像，默认 docker.m.daocloud.io/library/node:22-bookworm-slim
#                      （Docker Hub 不可直连时用加速站；有自己的镜像仓库就直接填）
#   APT_MIRROR         Debian 源主机名，默认 mirrors.aliyun.com
#   NPM_REGISTRY       npm 源，默认 https://registry.npmmirror.com
#   WECOM_CLI_VERSION  @wecom/cli 版本，默认 latest（锁定版本更稳，例如 1.3.4）
#   IMAGE_NAME         镜像名，默认 wecom-todo-sync:0.1（需与 compose 的 image 一致）

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

BASE_IMAGE="${BASE_IMAGE:-docker.m.daocloud.io/library/node:22-bookworm-slim}"
APT_MIRROR="${APT_MIRROR:-mirrors.aliyun.com}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
WECOM_CLI_VERSION="${WECOM_CLI_VERSION:-latest}"
IMAGE_NAME="${IMAGE_NAME:-wecom-todo-sync:0.1}"

echo "[build] 基础镜像 : ${BASE_IMAGE}"
echo "[build] apt 源   : ${APT_MIRROR}"
echo "[build] npm 源   : ${NPM_REGISTRY}"
echo "[build] CLI 版本 : ${WECOM_CLI_VERSION}"
echo "[build] 镜像名   : ${IMAGE_NAME}"
echo

docker build \
  --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
  --build-arg "APT_MIRROR=${APT_MIRROR}" \
  --build-arg "NPM_REGISTRY=${NPM_REGISTRY}" \
  --build-arg "WECOM_CLI_VERSION=${WECOM_CLI_VERSION}" \
  -t "${IMAGE_NAME}" \
  "$@" \
  .

echo
echo "[build] 完成：${IMAGE_NAME}"
echo "[build] 下一步：docker compose up -d"