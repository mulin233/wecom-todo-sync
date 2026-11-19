# 企业微信待办 <-> 飞书任务 同步工具：Docker 镜像
#
# 运行时：
#   - Python 3（本项目零第三方依赖，只用标准库）
#   - Node.js + 官方 @wecom/cli（Rust 二进制，通过 npm 分发）
#   - tzdata（东八区时间戳）
#
# 构建默认使用国内镜像源，可用 build-arg 覆盖：
#   BASE_IMAGE        基础镜像（默认 DaoCloud 加速的官方 node:22-bookworm-slim）
#   APT_MIRROR        Debian 源主机名（默认 mirrors.aliyun.com）
#   NPM_REGISTRY      npm 源（默认 https://registry.npmmirror.com）
#   WECOM_CLI_VERSION @wecom/cli 版本（默认 latest）
#
# 推荐用 ./build.sh 构建，它会填好这些参数。

ARG BASE_IMAGE=node:22-bookworm-slim
FROM ${BASE_IMAGE}

ARG APT_MIRROR=mirrors.aliyun.com
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG WECOM_CLI_VERSION=latest
ARG DEBIAN_FRONTEND=noninteractive

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    APP_DIR=/app

# python3：跑同步工具；tzdata：容器内时间正确；ca-certificates：HTTPS 访问飞书
# 先把 Debian 源换成国内镜像（新镜像用 .sources 格式，旧镜像用 sources.list，两种都处理）
RUN set -eux; \
    if [ -n "${APT_MIRROR}" ]; then \
      for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \
        [ -f "$f" ] || continue; \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" "$f"; \
      done; \
      grep -R . /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null || true; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends python3 tzdata ca-certificates; \
    ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime; \
    echo "${TZ}" > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

# 官方 CLI（Rust 二进制，按当前构建平台自动选 optionalDependency）
RUN npm config set registry "${NPM_REGISTRY}" \
 && npm install -g "@wecom/cli@${WECOM_CLI_VERSION}" \
 && npm cache clean --force \
 && wecom-cli --version

# 方便直接使用文档里的 `python -m wecom_todo_sync ...`（Debian 只提供 python3）
RUN ln -snf /usr/bin/python3 /usr/local/bin/python

WORKDIR ${APP_DIR}

COPY requirements.txt config.example.json ./
COPY samples ./samples/
COPY wecom_todo_sync ./wecom_todo_sync/
COPY deploy ./deploy/

RUN chmod +x deploy/*.sh \
 && python3 -m wecom_todo_sync selftest >/dev/null

# 默认进入定时循环；带参数执行时（docker compose run --rm app <命令>）直接执行该命令
ENTRYPOINT ["/app/deploy/docker-entrypoint.sh"]
CMD []