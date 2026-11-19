# 企业微信待办 ↔ 飞书任务（Task v2）同步工具

把**企业微信待办**归档到本地，并与**飞书任务**双向同步：

```
官方 @wecom/cli（读取/写入待办） ──► 本地 SQLite（去重、幂等） ──► 飞书任务 v2 API
```

- 纯 Python 标准库实现，采集侧调用官方 `@wecom/cli`
- **`./build.sh` + `docker compose up -d` 完成部署**：Python、Node.js、`@wecom/cli` 全在镜像里
- 双向同步：`sync`（企业微信 → 飞书）、`pull`（飞书 → 企业微信）
- 幂等：`sync_map` / `reverse_map` 记录映射与内容哈希，内容没变不重复调用接口
- 容器内置定时循环、健康检查；数据和授权持久化在宿主机的 `data/` 下

---

## 1. 前置条件

| 项 | 要求 |
| --- | --- |
| 宿主机 | Docker 20.10+，含 `docker compose`（Compose v2）。**不需要**装 Python / Node / npm |
| 飞书 | 企业自建应用，已开通任务权限并**审批通过**，拿到 `App ID` / `App Secret` |
| 企业微信 | 一个能扫码授权的企业微信账号 |

飞书应用配置（[开放平台](https://open.feishu.cn/)）：

1. 权限：`task:task`（或 `task:task:read` + `task:task:write`）；按需加 `task:tasklist`（指定清单）、`contact:user.id:readonly`（手机号换 `open_id`）
2. 安全设置 → 重定向 URL：`http://localhost:8765/callback`
3. 发布版本并等管理员审批通过，否则接口会报权限错误

> **两个 ID 决定任务在不在「我的任务」里**
> - 飞书：`feishu.default_assignee_open_id`（用 `whoami --mobile 手机号` 查）。不指派，任务属于应用，你看不到。
> - 企业微信：`reverse.default_follower_userid`。不填参与人，企业微信待办里也可能看不到。

---

## 2. 快速开始

```bash
# 1) 进入包含 docker-compose.yml 的目录
cd wecom-todo-sync

# 2) 建运行时目录，生成配置
mkdir -p config data exports logs
cp config.example.json config/config.json
chmod 600 config/config.json
# 编辑 config/config.json：至少填 feishu.app_id / app_secret /
#   feishu.default_assignee_open_id / reverse.default_follower_userid

# 3) 构建镜像（默认走国内镜像源），然后启动
./build.sh
docker compose up -d
docker compose logs -f app
```

> `docker-compose.yml` 不负责构建，只使用预先构好的 `wecom-todo-sync:0.1`。
> 程序文件全部在镜像里，宿主机只需要 `config/`、`data/`、`exports/`、`logs/` 四个目录。
> 换了机器 / 升级代码后，先跑 `./build.sh`，再 `docker compose up -d`。

`build.sh` 默认使用国内镜像源，可用环境变量覆盖：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BASE_IMAGE` | `docker.m.daocloud.io/library/node:22-bookworm-slim` | 基础镜像（Docker Hub 加速） |
| `APT_MIRROR` | `mirrors.aliyun.com` | Debian apt 源 |
| `NPM_REGISTRY` | `https://registry.npmmirror.com` | npm 源 |
| `WECOM_CLI_VERSION` | `latest` | 建议锁定版本，如 `1.3.4` |
| `IMAGE_NAME` | `wecom-todo-sync:0.1` | 需与 compose 里的 `image:` 一致 |

```bash
# 示例：改用清华源 / 自己的镜像仓库
BASE_IMAGE=registry.example.com/library/node:22-bookworm-slim \
APT_MIRROR=mirrors.tuna.tsinghua.edu.cn ./build.sh

# 海外网络 / 想走官方源
BASE_IMAGE=node:22-bookworm-slim APT_MIRROR= \
NPM_REGISTRY=https://registry.npmjs.org ./build.sh
```

启动时若还没授权，日志里出现「未授权 / 扫描 0 条」是正常的。继续做下面两步授权。

### 2.1 企业微信授权（扫码，只需一次）

```bash
# 二维码写到宿主机 ./data/wecom-qr.png，命令会等你扫码
docker compose run --rm -it app \
  wecom-cli auth init --noninteractive --output-qrcode /app/data/wecom-qr.png

# 确认：Status: authorized
docker compose run --rm app wecom-cli auth show --status
```

打开 `data/wecom-qr.png` 用企业微信扫码。凭证保存在 `data/wecom-cli/`，容器重建、升级都不用重扫。不能扫码时用 `wecom-cli auth init --manual` 手动填 Bot ID / Secret。

### 2.2 飞书用户授权（反向同步需要）

用应用身份（tenant token）**列不出**任务列表，所以 `pull` 必须用你本人的 `user_access_token`。

```bash
# Docker 所在机器上有浏览器时（本机 Docker Desktop、桌面 Linux 等）
docker compose exec app python -m wecom_todo_sync auth --no-browser

# 复制终端打印的链接到宿主机浏览器，点「授权」后自动回调
docker compose exec app python -m wecom_todo_sync auth --status
```

**服务器上没有浏览器**：在自己电脑上（需要 Python 3.8+ 和本项目代码）跑 `python -m wecom_todo_sync auth`，把生成的 `data/user_token.json` 上传到服务器的 `data/` 下即可。token 约 2 小时过期，容器会用 `refresh_token` 自动续期。

### 2.3 验证

```bash
docker compose exec app python -m wecom_todo_sync doctor   # 体检
docker compose exec app bash deploy/sync.sh                # 手动跑一轮
docker compose logs -f --tail=100                          # 看输出
```

不写企业微信、只看计划：`docker compose exec app python -m wecom_todo_sync pull`（去掉 `--apply` 就是预演）。

---

## 3. 配置

优先级：**内置默认值 < `config/config.json` < 环境变量**（环境变量见 compose 的 `environment` 与 `.env.example`）。
配置文件在 `config/config.json`（容器内 `/app/config.json` 是指向它的软链），每轮运行都会重新读取，改完不用重启；`.env` 在创建容器时注入，改后要 `docker compose up -d`。

常用字段：

| 字段 | 说明 |
| --- | --- |
| `feishu.app_id` / `app_secret` | 飞书应用凭证（也可用 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`） |
| `feishu.default_assignee_open_id` | 新任务的指派人；不填任务只属于应用 |
| `feishu.user_id_type` | `open_id` / `union_id` / `user_id` |
| `feishu.oauth_redirect` | 必须与飞书后台登记的一致，默认 `http://localhost:8765/callback` |
| `reverse.default_follower_userid` | 反向同步建出来的企业微信待办的参与人 |
| `wecom.list_methods` | 固定列出待办的方法，如 `[["todo","list"]]`；留空自动发现 |
| `meetings.enabled` | 会议 → 待办，默认关闭 |
| `store.db_path` / `export.dir` | 数据库与导出目录（默认 `data/`、`exports/`） |

完整默认值、字段映射（`mapping`）、状态归一化（`status_map`）见 [config.example.json](config.example.json)。企业微信返回字段因版本/租户而异时，跑一次 `collect` 后看 `data/raw/` 的原始 JSON，只改 `mapping` 即可适配，无需改代码。

同步节奏与容器相关的环境变量（写在 `.env`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SYNC_INTERVAL_SECONDS` | `300` | 两轮间隔（秒）；`0` = 只在启动时跑一轮，之后空闲 |
| `SYNC_ON_START` | `1` | 启动后立刻跑一轮 |
| `TZ` | `Asia/Shanghai` | 容器时区，影响日志与截止时间 |
| `PUID` / `PGID` | `0` | 容器运行身份。桌面 Linux 建议设为 `id -u` / `id -g`，避免 `data/` 属主不一致 |
| `FEISHU_OAUTH_BIND` | `0.0.0.0` | OAuth 回调监听地址，Docker 端口映射下必须是 `0.0.0.0` |
| `WECOM_CLI_CONFIG_DIR` | `/app/data/wecom-cli` | 企业微信授权凭证目录（compose 已设） |

---

## 4. 数据、备份与迁移

| 路径 | 内容 | 必须保留 |
| --- | --- | --- |
| `config/config.json` | 飞书应用凭证与同步配置 | **是** |
| `data/wecom_todo.db` | 待办内容 + `sync_map` / `reverse_map`，**防重复的唯一依据** | **是** |
| `data/user_token.json` | 飞书用户授权（自动续期） | 是 |
| `data/wecom-cli/` | 企业微信 CLI 授权凭证 | 是 |
| `data/raw/` | 每次采集的接口原始 JSON（排查字段用） | 否 |
| `exports/`、`logs/` | 报表与日志 | 否 |

从旧部署迁移时，把 `config/config.json` 和 `data/` 里的内容直接放进来。**漏了 `wecom_todo.db` 会在飞书重复建任务，漏了 `wecom-cli/` 要重新扫码。**

备份：

```bash
docker compose stop app
tar czf wecom-todo-backup-$(date +%F).tar.gz config data exports
docker compose start app
```

---

## 5. 运行与日志

容器入口默认进入定时循环：每轮依次执行

1. `pull --apply`：飞书新建的任务 → 企业微信待办
2. `sync --export`：企业微信待办 → 飞书任务，并导出 `exports/`

用 `flock` 防止上一轮未结束又叠加。两边都有映射表兜底，顺序不影响正确性。

```bash
docker compose logs -f app            # 容器日志（含每轮同步输出）
tail -n 100 logs/sync.log             # 宿主机文件，超过 5MB 自动裁剪
```

想改用宿主机调度器（宝塔计划任务 / crontab）时，设 `SYNC_INTERVAL_SECONDS=0` 关掉内置循环，再定时执行：

```bash
docker compose exec -T app bash deploy/sync.sh
```

退出码：`0` = 成功（含全跳过），`1` = 有失败项，便于告警。

---

## 6. 健康检查

`docker compose ps` 的 STATUS 列会显示 `(healthy)` / `(unhealthy)`。检查三项前置条件：

1. `data/` 可写（bind mount 属主 / `PUID`、`PGID`）
2. `wecom-cli` 已授权
3. 飞书 `app_id` / `app_secret` 已填（`config/config.json` 或环境变量）

```bash
docker inspect --format '{{.State.Health.Status}}' wecom-todo-sync
docker compose exec app bash deploy/healthcheck.sh   # 查看具体原因
```

> 初始化完成前显示 `(unhealthy)` 属正常；三项满足后，约 1 分钟开始首个检查，之后每 5 分钟一次，转为 `healthy`。
> 健康状态只用于展示与监控，不会触发自动重启（重启只看主进程是否退出）。

---

## 7. 常用命令

Docker 下把命令包进 `docker compose exec app ...`：

| 命令 | 作用 |
| --- | --- |
| `doctor` | 体检：CLI 授权、飞书权限、数据库、最近运行记录 |
| `collect --export` | 只采集到本地库并导出，不推飞书 |
| `sync [--dry-run] [--export]` | 采集并同步到飞书；`--dry-run` 只打印动作 |
| `export [--format csv]` | 从本地库重新导出报表 |
| `pull [--apply]` | 飞书 → 企业微信；默认只预演 |
| `import --url <任务链接>` | 把指定飞书任务导入企业微信（`--dry-run` 预演） |
| `meetings [--apply]` | 会议 → 待办（需先开 `meetings.enabled`） |
| `auth --status` | 查看飞书用户授权状态 |
| `whoami --mobile 138...` | 手机号/邮箱 → `open_id` |
| `probe` | 调试：打印飞书任务接口原始响应 |

常用运维：

```bash
docker compose ps                                   # 状态 / 健康检查
docker compose restart app                          # 重启定时循环
docker compose down                                 # 停止并删除容器（数据保留）
git pull && ./build.sh && docker compose up -d         # 升级（先重新构建镜像）
docker compose exec app python -m wecom_todo_sync doctor
```

---

## 8. 排查

| 现象 | 处理 |
| --- | --- |
| `docker compose up` 报找不到镜像 | 先构建：`./build.sh`（标签需与 compose 的 `image:` 一致） |
| 从旧版本升级，配置还在根目录 `config.json` | `mkdir -p config && mv config.json config/config.json`（旧容器先 `docker compose down`） |
| 构建很慢 / 拉不到基础镜像 | `build.sh` 已默认国内源；仍不行就换 `BASE_IMAGE`（如 `docker.1ms.run/library/node:22-bookworm-slim` 或自己的仓库），npm 慢可设 `NPM_REGISTRY` |
| `permission denied .../docker-entrypoint.sh` | 宿主机脚本缺执行位：`chmod +x deploy/*.sh` |
| 无法写入 `config/config.json` / `data/` | 挂载目录属主不一致：`chown -R $(id -u):$(id -g) config data exports logs`，或设置 `PUID` / `PGID` |
| `pull` 一直「扫描 0 条」 | `data/user_token.json` 缺失或失效 → 重新做 2.2 授权 |
| 企业微信待办看不到 | `reverse.default_follower_userid` 没填，待办没有参与人 |
| 飞书出现重复任务 | `data/wecom_todo.db` 没迁过来，映射表是空的 |
| 日志时间差 8 小时 | `TZ` 被覆盖，确认是 `Asia/Shanghai` |
| 健康检查一直 unhealthy | `docker compose exec app bash deploy/healthcheck.sh` 看是哪一项 |
| 授权回调不到容器 | 8765 未映射，或 `FEISHU_OAUTH_BIND` 不是 `0.0.0.0`；服务器无浏览器时用 2.2 的上传方案 |
| 容器反复重启 | `docker compose logs --tail=200 app`；先 `bash -n deploy/*.sh` 检查脚本语法 |

---

## 9. 目录结构

```
wecom-todo-sync/                 # 仓库根目录（含 docker-compose.yml）
├── Dockerfile                   # Python 3 + Node.js 22 + @wecom/cli
├── build.sh                     # 构建镜像（默认国内镜像源）
├── docker-compose.yml           # 定时循环、健康检查、数据挂载（只使用镜像）
├── .env.example                 # 可选环境变量（复制成 .env）
├── config.example.json          # 完整配置示例（复制成 config/config.json）
├── deploy/
│   ├── docker-entrypoint.sh     # 容器入口：首次生成配置 + 定时循环
│   ├── healthcheck.sh           # 健康检查
│   └── sync.sh                  # 一行 pull + sync（含日志裁剪与防重叠）
├── samples/                     # 离线示例数据
└── wecom_todo_sync/             # Python 程序（零第三方依赖）
    ├── cli.py / config.py / models.py
    ├── wecom_cli.py             # 驱动 @wecom/cli
    ├── collector.py / sync.py / reverse.py / meetings.py
    ├── feishu.py / auth.py      # 飞书 Task v2 客户端与 OAuth
    ├── store.py                 # SQLite
    └── export.py / selftest.py
```

运行时会在宿主机生成 `config/`、`data/`、`exports/`、`logs/` 四个目录（均已加入 `.gitignore`）。镜像里**不包含**这些敏感数据，重建镜像不会影响它们。

---

## 10. 注意事项与安全

- **不要把 `config/`、`.env` 提交到仓库**。根目录 `.gitignore` 已覆盖 `config/`、`.env`、`data/`、`exports/`、`logs/`。
- 敏感文件：`config/config.json`（App Secret）、`.env`、`data/user_token.json`、`data/wecom-cli/`（企业微信授权凭证）、`data/wecom_todo.db` 与 `data/raw/`（待办内容和 userid）。泄露后先重置 App Secret 并重新授权。
- 写企业微信的操作（`pull`、`import`、`meetings`）默认只预演，必须加 `--apply`。
- 「已完成 → 未完成」默认不回写飞书，需要时开 `feishu.allow_reopen`。
- 企业微信待办正文里的图片/附件不会搬到飞书，只能保留文字。
- 飞书应用权限必须审批通过；权限不足时 `doctor` 会给出错误码。

MIT 许可证，见 `LICENSE`。