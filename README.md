# 企业微信待办 → 飞书任务（Task v2）同步工具

把**企业微信的待办内容**收集到本地归档，并（可选）同步成**飞书任务**。

```
官方 @wecom/cli 的 todo 服务  ──►  本地 SQLite 归档  ──►  飞书任务 v2（POST/PATCH /open-apis/task/v2/tasks）
        （读取待办）               （去重、增量）              （幂等：只创建一次，变了才更新）
```

- 纯 Python 标准库实现（3.8+，无需 `pip install` 任何东西）
- 采集侧用**官方 CLI**：`@wecom/cli`（Tencent 企业微信团队维护，npm 包名 `@wecom/cli`）
- 推送侧用**飞书开放平台 Task v2**：`https://open.feishu.cn/open-apis/task/v2/...`
- 幂等：靠本地 `sync_map` 表记录「企业微信待办 ID → 飞书任务 GUID + 内容哈希」，内容没变就不会重复调接口

---

## 1. 为什么不直接调企业微信服务端接口？

企业微信服务端「待办」接口（`developer.work.weixin.qq.com/document/path/101529 ~ 101534`）只有四件事：

| 接口 | 作用 |
| --- | --- |
| 创建待办 | 应用往成员待办里写 |
| 查看待办详情 | **按 `todo_id`** 查，必须事先知道 id |
| 更新待办状态 | 改未完成/已完成 |
| 删除待办 | 删 |

**没有「列出本人全部待办」的接口**，所以拿不到"我都有哪些待办"。而官方 CLI 的 `todo` 服务是按凭证读取（`wecom-cli todo ...`），因此本工具把 CLI 作为数据源。

> 如果你后续想改成直连 `qyapi.weixin.qq.com`（例如应用自己创建待办、自己存 `todo_id`），只需要替换 `wecom_todo_sync/collector.py` 里的采集步骤，后面的入库/同步逻辑完全不用动。

---

## 2. 前置条件

### 2.1 企业微信侧

```bash
# Node.js >= 18
npm install -g @wecom/cli

# 授权（扫码，或手动填 Bot ID / Secret），只需一次
wecom-cli auth init
wecom-cli auth show --status     # 应输出 authorized

# 看一眼待办服务实际有哪些方法（服务目录是服务端下发的）
wecom-cli todo --help
wecom-cli todo <方法名> --help
wecom-cli schema list
```

### 2.2 飞书侧

1. [飞书开放平台](https://open.feishu.cn/) → 创建**企业自建应用**
2. 开通任务相关权限（至少）：`task:task`（或读 `task:task:read` + 写 `task:task:write`）
   - 想把任务放进指定清单：再加 `task:tasklist`
   - 想把任务**指派给自己/同事**：再加 `contact:user.id:readonly`（用手机号换 `open_id`）
3. 发布版本并等待**管理员审批通过**（没审批过，接口会一直报权限错误）
4. 拿到 `App ID` / `App Secret`

> **为什么一定要指派**：用 `tenant_access_token` 创建的任务属于"应用"，不把人加进 `members` 的话，你在飞书"我的任务"里看不到它。所以建完应用后建议先跑一次：
>
> ```powershell
> python -m wecom_todo_sync whoami --mobile 13800138000
> ```
>
> 把输出的 `open_id` 填进 `config.json` 的 `feishu.default_assignee_open_id`（或环境变量 `FEISHU_ASSIGNEE_OPEN_ID`）。
> 不想多要一个通讯录权限的话，也可以把 `feishu.user_id_type` 改成 `user_id`，直接填管理后台里看到的成员 user_id。

---

## 3. 快速开始

```bash
git clone <这个仓库的地址>
cd wecom-todo-sync

# 1) 离线自检：不联网也能验证解析链路（不需要任何凭证）
python -m wecom_todo_sync selftest

# 2) 用示例数据跑通「采集 -> 入库 -> 导出」
python -m wecom_todo_sync collect --source file --source-file samples/wecom_cli_page.sample.json --export
cat exports/todos.md

# 3) 填你自己的凭证（交互式，会生成 config.json）
python -m wecom_todo_sync init

# 4) 体检：CLI 是否装好/授权、飞书权限是否够、数据库是否可写
python -m wecom_todo_sync doctor

# 5) 真实采集（只入本地库，不动飞书）
python -m wecom_todo_sync collect --export

# 6) 先干跑，确认动作符合预期
python -m wecom_todo_sync sync --dry-run

# 7) 真正同步到飞书任务
python -m wecom_todo_sync sync
```

---

## 4. 命令一览

| 命令 | 作用 |
| --- | --- |
| `init` | **交互式填写配置**（app_id / secret / open_id / userid），生成 `config.json` |
| `selftest` | 离线自检（时间/状态归一化、NDJSON 分页解析、子项展开、方法发现、SQLite 幂等、示例配置） |
| `doctor` | 体检：Python 环境、CLI 版本与授权、待办方法候选、飞书 token 与任务读取权限、库里最近运行记录 |
| `collect` | 只采集到本地库（`--export` 顺便导出报表；`--no-raw-dump` 不归档原始 JSON） |
| `sync` | 采集 + 同步到飞书（`--dry-run` 只看动作；`--all` 同步库里全部；`--no-collect` 跳过采集） |
| `export` | 从本地库导出 Markdown / CSV / JSON |
| `whoami` | 手机号/邮箱 → `open_id`（填飞书任务的指派人） |
| `pull` | 反向同步：飞书任务 → 企业微信待办（默认只预演，`--apply` 才写入） |
| `auth` | 飞书用户授权，拿 `user_access_token`（`--status` 看状态） |
| `import` | 按 guid/链接把单条飞书任务导入企业微信（`--dry-run` 预演） |
| `probe` | 调试：打印飞书任务接口的原始响应 |

通用参数：`--config 路径`、`--db 路径`、`--verbose`。

采集来源：`--source cli`（默认，走 `wecom-cli`）或 `--source file --source-file xxx.json`（离线/回放）。

导出文件：
- `exports/todos.md` —— 人看的清单 + 每条明细
- `exports/todos.csv` —— 表格（含飞书任务 GUID）
- `exports/todos.json` —— 结构化数据，便于喂给 AI/知识库

---

## 5. 配置

复制 `config.example.json` 为 `config.json` 后按需修改。所有字段都有默认值，**可以只写要覆盖的部分**。

```json
{
  "wecom": {
    "cli": ["wecom-cli"],
    "list_methods": [["todo", "list"]],
    "list_params": {},
    "page_count": 20
  },
  "feishu": {
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "default_assignee_open_id": "ou_xxx",
    "user_id_type": "open_id"
  },
  "store": { "db_path": "data/wecom_todo.db" }
}
```

环境变量优先级更高：

| 变量 | 覆盖的配置 |
| --- | --- |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | `feishu.app_id` / `feishu.app_secret` |
| `FEISHU_BASE_URL` | `feishu.base_url`（Lark 国际版填 `https://open.larksuite.com`） |
| `FEISHU_USER_ACCESS_TOKEN` | `feishu.user_access_token`（填了就不用 tenant token） |
| `FEISHU_ASSIGNEE_OPEN_ID` | `feishu.default_assignee_open_id` |
| `FEISHU_TASKLIST_GUID` | `feishu.tasklist_guid` |
| `WECOM_TODO_DB` | `store.db_path` |
| `WECOM_CLI` | `wecom.cli`（例如 `npx -y @wecom/cli`） |

### 5.1 待办方法名对不上怎么办

CLI 的服务目录是**服务端下发**的，不同版本方法名可能不同。本工具的策略是：

1. 优先用 `wecom.list_methods` 里配置的方法；
2. 否则跑 `wecom-cli schema list`，从里面挑与 `todo` 相关的方法；
3. 再否则解析 `wecom-cli todo --help` 的文本（`list`/`get` 这类词不会被当成小标题过滤掉）；
4. 都没发现时兜底用 `todo list`。

**只读方法才会被自动调用**：`list`、`get`、`query`、`search` 这类会被尝试；`create` / `update` / `delete` / `finish` **绝不会**被自动调用（防止"列表为空"时误写数据）。

已知方法（2026-11 实测 `wecom-cli todo --help`）：

| 方法 | 说明 |
| --- | --- |
| `list` | 机器人为创建人的待办列表，按 `update_time` 降序，返回标题/描述/人名 |
| `get` | 批量读取待办详情，返回标题/描述/参与人/时间 |
| `create` / `update` / `delete` / `finish` | 写操作，本工具不自动调用 |

> `list` 偏"摘要"，`get` 才有完整的参与人与时间。如果你需要更全的字段，先用 `list` 拿 id，再对 id 调 `get`（见 `wecom_cli.py` 的 `fetch_pages`）。

`doctor` 会把候选方法都打印出来。如果都不对，就手动指定：

```json
{ "wecom": { "list_methods": [["todo", "list"]] } }
```

`list_params` 只放**扁平**的键值对（内部用 CLI 的 `--set key=value` 传，避免 Windows 上 JSON 里的引号被 cmd 吃掉）；确实需要嵌套对象时会自动退回 `--json`。

> 想确认方法名，也可以直接手动跑一下：
> `wecom-cli todo --help` → 看有哪些子方法；`wecom-cli todo <方法> --schema` → 看参数。

### 5.1.1 实测到的返回结构（2026-11，`todo list`）

```json
{
  "security_notice": "…",
  "extra_identity_context": "…",
  "items": [
    {
      "todo_id": "tdemuEXAMPLE0001",
      "title": "同步消息",
      "description": "<div>同步学习</div>",
      "status": "proceed",
      "creator": { "userid": "wo…", "user_name": "张三" },
      "create_time": "2026-11-13 10:08:29",
      "update_time": "2026-11-13 10:08:29"
    }
  ],
  "items_count": 1,
  "has_more": false
}
```

几个容易踩的点，工具里已经处理：

| 现象 | 处理方式 |
| --- | --- |
| `status: "proceed"`（不是 0/1） | `status_map` 已把 `proceed`/`doing`/`in_progress` 归一为未完成 |
| `status: "deleted"` | 归一为"已删除"：**只归档、不推飞书**（导出里单列一组） |
| `description` 是 HTML（`<div>`/`<p>`/`&nbsp;`） | `models.strip_html()` 清洗成纯文本，`<br>`/`</p>` 转换行 |
| `creator` 是对象不是字符串 | 取 `user_name`（缺失时退回 `userid`） |
| `create_time` 是 `"YYYY-MM-DD HH:MM:SS"` | `to_epoch_ms()` 按本地时区解析成毫秒 |
| 顶层 `security_notice` / `extra_identity_context` | 属于外部不可信文本，**只当数据跳过**，不参与解析、也不会写进飞书任务 |
| `success: false` + `errmsg` | 该条详情跳过并给出告警，不影响其他待办 |

`list` 拿不到截止时间，所以默认会再调一次 `todo get` 补详情（`get` 每次最多 20 个 id，自动分批）：

| 详情字段 | 用途 |
| --- | --- |
| `deadline: {type, value}` | 转成毫秒；`type=date` 时飞书侧标成**全天** |
| `followers: [{user_name, ...}]` | 写进飞书任务描述末尾：`参与人：张三、李四` |
| `source` | 待办来源（单聊/文档/会议纪要等），只存进本地原始数据 |

想关掉补详情：`{"wecom": {"enrich_with_get": false}}`；想改调用上限：`detail_limit`（默认 200 条）。

### 5.2 字段名对不上怎么办

不同租户/版本返回的键名可能不一样，所以字段映射全部集中在配置的 `mapping` 段（候选键名按顺序取第一个命中的）。做法：

```bash
# 采集时会把原始 JSON 存到 data/raw/ 下
python -m wecom_todo_sync collect
# 打开 data/raw/collect-*.json 看真实字段名，然后改 config.json 的 mapping
```

例如真实返回是 `{"taskTitle": "...", "body": "..."}`，就改成：

```json
{ "mapping": { "title": ["taskTitle", "title"], "description": ["body", "content", "description"] } }
```

状态映射在 `status_map`（例如你的租户用 `2` 表示已完成，就加 `"2": "completed"`）。

### 5.3 待办里套子项

企业微信的待办对象可能长这样：

```json
{ "todo_id": "T-1001", "title": "Q4 交付准备",
  "todo_list": [ {"id": "a1", "title": "整理需求清单", "content": "...", "status": 0} ] }
```

这种情况真正要同步的是**子项**，工具会自动展开，`source_id` 记为 `T-1001#a1`（保证稳定、可幂等）。子项没提供的字段（如截止时间）会从父项继承。控制展开的键名在 `mapping.children`。

---

## 6. 同步策略（幂等性）

| 本地状态 | 动作 |
| --- | --- |
| `sync_map` 里没有该待办 | `POST /open-apis/task/v2/tasks` 创建任务，记下返回的 `guid` |
| 有记录，且内容哈希变了 | `PATCH /open-apis/task/v2/tasks/{guid}`，只更新变化的字段（`update_fields`） |
| 有记录，哈希没变 | 跳过，**不产生任何飞书接口调用** |
| 企业微信侧变成"已完成" | PATCH `completed_at`；已同步过完成的不会重复下发，避免覆盖完成时间 |

> **为什么"完成了、list 里却没了"也能同步**：`todo list` 只返回**进行中**的待办，标记完成/删除后它就从列表里消失了。所以采集时会拿本地库里已有的 `todo_id` 再批量 `get` 一次（`get` 的 `status` 枚举含 `finished` / `deleted` / `proceed`），把状态变化补回来，再决定是 PATCH 完成还是跳过。这也是 `detail_limit`（默认 200 条）存在的原因——只对最近见过的 id 回查，避免无限增长。

字段对应关系：

| 企业微信待办 | 飞书任务 |
| --- | --- |
| `title` | `summary` |
| `description` / `content` | `description`（末尾附一行来源 ID，可用 `feishu.include_source_id` 关掉） |
| `deadline` | `due.timestamp`（毫秒字符串）；`type=date` 时 `is_all_day: true` |
| `status = completed` | `completed_at` |
| `owner` / `default_assignee_open_id` | `members[]`（`role: assignee`） |

注意：更新时**不会**覆盖任务的指派人（避免把人工改过的指派改回去）；企业微信侧"重新打开"默认不同步回飞书（可开 `feishu.allow_reopen`）。

---

### 6.1 反向同步（飞书 → 企业微信）

```powershell
python -m wecom_todo_sync pull            # 预演：只打印将要发送的请求
python -m wecom_todo_sync pull --apply    # 真的在企业微信建待办
python -m wecom_todo_sync pull --apply --limit 5
```

**为什么默认只预演**：这是唯一会**写企业微信**的命令，写错了会留下垃圾待办。先看 `[plan]` 里打印的请求体，确认标题/描述/截止时间对得上再加 `--apply`。

**防环规则（三条，缺一条就会无限复制）**：

| 方向 | 跳过条件 | 作用 |
| --- | --- | --- |
| 飞书 → 企微 | 任务 guid 已在前向 `sync_map` 里 | 这是我们从企业微信建出来的，不能再建回去 |
| 飞书 → 企微 | 任务 guid 已有 `reverse_map` 记录 | 已经推过了 |
| 企微 → 飞书 | 待办 id 出现在 `reverse_map.wecom_todo_id` | 反向建的待办不能再推回飞书 |

配置：

```json
{
  "reverse": {
    "default_follower_userid": "woxxxxxxxxxxxxxxxx",
    "max_pages": 20
  }
}
```

`default_follower_userid` 是**企业微信的 userid**（不是飞书 open_id）。从你已有的采集数据里就能找到——`data/raw/collect-*.json` 里 `detail_pages[].items[].creator.userid` 就是你自己。

> 注意 create 和 update 的参与人字段**不对称**：`create` 用 `follower_ids: ["wo..."]`（字符串数组），`update` 用 `followers: [{"userid": "wo..."}]`（对象数组，且是全量替换）。代码里已按这个差异分别构造。

不填也不会报错，但建出来的待办参与者为空，你在企业微信「待办」里大概率看不到它。

支持的动作：

| 飞书侧变化 | 企业微信侧动作 | 说明 |
| --- | --- | --- |
| 新建任务 | `todo create` | 参与人用 `follower_ids`（字符串数组） |
| 改标题 / 描述 / 截止时间 | `todo update` | **不传 `followers`**，因为它是全量替换语义，传了会把参与人清空 |
| 勾选完成 | `todo finish` | 必须 `finished_all: true`，否则接口返回 `ask_finish_all` 要求二次确认 |

仍然不做：

- 企业微信侧改标题 / 勾完成 → **不回写飞书**（前向 `sync` 会跳过反向建的待办，见防环规则 3）；
- 删除：两个方向都不删除，只归档；
- 飞书任务取消完成后，企业微信待办不会自动恢复未完成（`finish` 没有逆操作）。

## 6.2 为什么反向同步需要用户授权

实测结论（2026-11，企业自建应用）：

| 接口 | tenant_access_token（应用身份） | 说明 |
| --- | --- | --- |
| `GET /open-apis/task/v2/tasks` | **恒返回空**（连 `total` 都没有） | 应用身份列不出任务 |
| `POST /open-apis/task/v2/tasks/search` | 只能搜到**应用自己创建**的任务 | 搜不到你本人建的任务 |
| `GET /open-apis/task/v2/tasks/{guid}` | 可以读 | 但前提是已经知道 guid |

所以要自动发现"你本人在飞书新建的任务"，必须用 `user_access_token`：

```powershell
# 1) 飞书后台 → 安全设置 → 重定向 URL，加一条：
#    http://localhost:8765/callback
# 2) 本地授权（浏览器点一下）
python -m wecom_todo_sync auth
python -m wecom_todo_sync auth --status
# 3) 之后 pull 会自动用 user token
python -m wecom_todo_sync pull
```

token 存在 `data/user_token.json`（已加入 `.gitignore`），约 2 小时过期，之后用 `refresh_token` 自动续；刷新失败就重新 `auth` 一次。

如果不想授权，也可以按 guid/链接单条导入（用应用身份就能读）：

```powershell
python -m wecom_todo_sync import --url "飞书任务链接" --dry-run
python -m wecom_todo_sync import --url "飞书任务链接"
```

## 6.3 会议 → 待办

`wecom-cli meeting list` 能按开始时间范围拉会议，所以可以直接把会议变成待办：

```powershell
python -m wecom_todo_sync meetings            # 预演
python -m wecom_todo_sync meetings --apply    # 真的建待办
python -m wecom_todo_sync meetings --apply --limit 10
```

映射关系：

| 会议字段（MeetingRecord） | 待办字段（TodoCreateItem） |
| --- | --- |
| `subject` | `title` |
| `begin_time`（`YYYY-MM-DD HH:MM:SS`） | `deadline: {type: "datetime", value}` |
| `begin_time ~ end_time` / `timezone` / `meeting_room` / `location` / `attendee_count` / `creator_name` | `description` 里的多行文本 |
| — | `follower_ids: ["你的 userid"]` |

去重：`source_id = meeting:{meeting_id}:{sub_meeting_id}`，记在 `meeting_map` 表；同一场会议第二次跑直接跳过（`--force` 可强制重推）。

配置：

```json
{
  "meetings": {
    "days_ahead": 7,
    "days_back": 0,
    "include_attended": false,
    "follower_userid": ""
  }
}
```

- `days_ahead` / `days_back`：时间窗口（默认"今天 ~ 未来 7 天"）。窗口太小会漏掉刚建的会议，太大则待办列表膨胀；
- `include_attended`：默认只同步**我创建的**会议（`created_meetings`）；设为 `true` 会把"我参加的"（`attended_meetings`）也同步；
- `follower_userid`：留空则复用 `reverse.default_follower_userid`；
- **`enabled` 默认 `false`**：不开启就不会参与任何定时任务，`meetings` 命令也不会执行。

当前限制：

- **只创建，不跟着改期更新**。会议改时间后待办还是旧的 `deadline`；要做的话需要 `todo update`（`deadline` 可直接覆盖）；
- 周期性会议按 `sub_meeting_id` 展开成多场，各建一条待办；
- 已取消/已结束的会议仍会被 `list` 返回（`list` 没有状态字段），要过滤得对每条再调 `meeting get` 看 `meeting_status`。

## 7. 定时运行
双向都跑的话，顺序建议 `pull` 在前、`sync` 在后：

```powershell
python -m wecom_todo_sync pull --apply    # 飞书 -> 企业微信（会跳过前向建的任务）
python -m wecom_todo_sync sync --export   # 企业微信 -> 飞书（会跳过反向建的待办）
```

顺序其实不影响正确性（两边都有 `sync_map` / `reverse_map` 兜底），但先 pull 能让新建的待办立刻参与后面的归档导出。

Windows 计划任务（每 30 分钟）：

```powershell
New-Item -ItemType Directory -Force logs | Out-Null
schtasks /create /tn "wecom-feishu-todo-sync" /sc minute /mo 30 /tr "cmd /c cd /d C:\Users\Admin\Documents\deepseek-harness\default-workspace\wecom-todo-sync && python -m wecom_todo_sync pull --apply >> logs\sync.log 2>&1 && python -m wecom_todo_sync sync --export >> logs\sync.log 2>&1"
```

> 计划任务读不到你当前终端里的 `$env:`，所以凭证必须写在 `config.json` 里（或设成系统环境变量）。

Windows 计划任务（每天 9:00）：

```powershell
schtasks /create /tn "wecom-todo-sync" /sc daily /st 09:00 ^
  /tr "cmd /c cd /d C:\path\to\wecom-todo-sync && python -m wecom_todo_sync sync --export >> logs\sync.log 2>&1"
```

Linux crontab：

```cron
0 9 * * * cd /opt/wecom-todo-sync && FEISHU_APP_ID=... FEISHU_APP_SECRET=... /usr/bin/python3 -m wecom_todo_sync sync --export >> logs/sync.log 2>&1
```

`sync` 的退出码：0 = 成功（含全跳过），1 = 有失败项或发生错误，便于告警。

---

## 8. 部署到服务器（宝塔面板）

目标是：**不用敲命令**，服务器每 5 分钟自动双向同步一次。

### 8.1 上传代码

宝塔「文件」→ 进 `/www/wwwroot/` → 新建目录 `wecom-todo-sync` → 把整个项目传上去（或用 Git 拉）。

目录应该是：`/www/wwwroot/wecom-todo-sync/wecom_todo_sync/...`、`deploy/`、`config.example.json`。

### 8.2 装依赖

宝塔「软件商店」里装两个：

| 组件 | 说明 |
| --- | --- |
| Node.js 版本管理器 | 装 **18+**，`wecom-cli` 需要 |
| Python 项目管理器 | 装 **3.8+**（本项目零第三方依赖） |

装完在服务器上执行：

```bash
cd /www/wwwroot/wecom-todo-sync
bash deploy/install.sh
```

它会检查版本、`npm install -g @wecom/cli`、建 `data/ logs/ exports/`、生成 `config.json`、跑一遍离线自检。

### 8.3 企业微信授权（服务器上没有浏览器）

```bash
cd /www/wwwroot/wecom-todo-sync
wecom-cli auth init --noninteractive --output-qrcode /tmp/wecom-qr.png
wecom-cli auth show --status
```

用宝塔文件管理器把 `/tmp/wecom-qr.png` 下载下来，用企业微信扫码。看到 `authorized` 即可。

> 也可以用 `wecom-cli auth init --manual`，直接填 Bot ID / Secret（适合不能扫码的环境）。

### 8.4 迁移本地数据（重要）

本地已经跑过的话，**一定要把这两样传上去**，否则飞书那边会重复建任务：

| 文件 | 为什么必须带 |
| --- | --- |
| `data/wecom_todo.db` | 里面有 `sync_map` / `reverse_map`，是防重复的唯一依据 |
| `data/user_token.json` | 飞书用户授权，`pull` 要用；不带就得在服务器重新授权 |

在你自己电脑上：

```powershell
scp data\wecom_todo.db root@服务器IP:/www/wwwroot/wecom-todo-sync/data/
scp data\user_token.json root@服务器IP:/www/wwwroot/wecom-todo-sync/data/
```

（宝塔文件管理器直接上传也行。）

> 不想迁 `user_token.json` 的话，也可以在本地跑 `python -m wecom_todo_sync auth` 后把文件传上去——重点是它必须存在，否则 `pull` 只能看到应用自己的任务，等于扫到 0 条。

### 8.5 填配置

```bash
nano /www/wwwroot/wecom-todo-sync/config.json
```

至少填这四项：

```json
{
  "feishu": {
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "default_assignee_open_id": "ou_xxx",
    "oauth_redirect": "http://localhost:8765/callback"
  },
  "reverse": { "default_follower_userid": "wo_xxx" }
}
```

然后：

```bash
chmod 600 /www/wwwroot/wecom-todo-sync/config.json
```

### 8.6 手动验证一次

```bash
cd /www/wwwroot/wecom-todo-sync
bash deploy/sync.sh
tail -n 40 logs/sync.log
```

`sync.sh` 会依次跑 `pull --apply` 和 `sync --export`，输出全进 `logs/sync.log`，日志超过 5MB 自动裁剪到 2000 行。

### 8.7 配宝塔计划任务

宝塔 →「计划任务」→ 添加任务：

| 项 | 值 |
| --- | --- |
| 任务类型 | Shell 脚本 |
| 任务名称 | wecom-todo-sync |
| 执行周期 | N 分钟 → **5** |
| 脚本内容 | `bash /www/wwwroot/wecom-todo-sync/deploy/sync.sh` |

保存后点「执行」一次，再去 `logs/sync.log` 看有没有新记录。

### 8.8 排查清单

| 现象 | 原因 / 处理 |
| --- | --- |
| `wecom-cli: command not found` | 计划任务的 PATH 太短。`sync.sh` 里已补 `/usr/local/bin` 等常见目录；还不行就在脚本里加一行 `export PATH=$PATH:$(npm root -g)/../bin` |
| 日志里时间差 8 小时 | 服务器是 UTC。`sync.sh` 已 `export TZ=Asia/Shanghai`，确认系统 `date` 是否也正常 |
| `pull` 一直"扫描 0 条" | `data/user_token.json` 没传或已过期 → 重新 `auth` |
| 企业微信里看不到新待办 | `reverse.default_follower_userid` 没填，待办没有参与人 |
| 飞书里出现重复任务 | `data/wecom_todo.db` 没迁过来，`sync_map` 是空的 |
| 脚本手动能跑、计划任务不行 | 计划任务是以 root 跑的，检查 `config.json` 权限（600 且属主是 root） |

## 9. 目录结构

```
wecom-todo-sync/
├── config.example.json          # 示例配置（复制成 config.json 使用）
├── deploy/
│   ├── install.sh               # 服务器初始化：查依赖、装 wecom-cli、建目录
│   └── sync.sh                  # 一次性双向同步（给宝塔计划任务调用）
├── samples/wecom_cli_page.sample.json
└── wecom_todo_sync/
    ├── cli.py         # 命令行入口：doctor/collect/sync/export/auth/pull/import/meetings/probe
    ├── config.py      # 配置加载 + 环境变量 + 字段映射默认值
    ├── models.py      # Todo 模型、时间/状态归一化、子项展开
    ├── wecom_cli.py   # 驱动 @wecom/cli：授权检查、方法发现、分页、输出解析、create/update/finish
    ├── collector.py   # 采集编排（cli / file 两种来源）+ 详情补全 + 原始数据归档
    ├── feishu.py      # 飞书 Task v2 客户端 + 请求体构造
    ├── reverse.py     # 反向同步：飞书任务 -> 企业微信待办（create/update/finish）
    ├── meetings.py    # 会议 -> 待办（默认关闭）
    ├── auth.py        # 飞书用户授权（OAuth，拿 user_access_token）
    ├── store.py       # SQLite：todos / sync_map / reverse_map / meeting_map / runs
    ├── sync.py        # 前向幂等同步编排
    ├── export.py      # Markdown / CSV / JSON 导出
    └── selftest.py    # 离线自检
```

数据都在本地：`data/wecom_todo.db`（SQLite）、`data/raw/*.json`（原始返回，排查字段名用）、`exports/*`（报表）。

---

## 10. 已知限制与注意事项

1. **采集依赖官方 CLI 的方法名**：CLI 的服务目录在服务端，本工具靠自动发现 + 配置兜底。第一次用请先跑 `doctor`，必要时用 `list_methods` 固定。
2. **飞书权限必须先开通并审批**，否则 `doctor` 会在飞书那一步报 99991672 / permission 类错误。
3. **不要把 `config.json` 提交到仓库**（含 App Secret）。建议只放环境变量，或把 `config.json` 加进 `.gitignore`。
4. **"重新打开"任务**（已完成 → 未完成）默认不同步，因为飞书侧清空 `completed_at` 的行为需要按你的版本确认；需要的话开 `feishu.allow_reopen` 并自行验证。
5. **描述里的图片/附件不会搬过去**：企业微信待办正文通常是纯文本，若有图片只能在描述里留占位说明。
6. 对企业微信：`collect` / `sync` 只读；`pull` / `import` / `meetings` 会**写**待办。写操作都默认"只预演"，必须加 `--apply` 才落库，并且有防环和 `sync_map` / `reverse_map` 兜底。
7. **Windows 下调用 CLI 的细节**：npm 全局安装只会生成 `wecom-cli.CMD` 垫片，而 `CreateProcess` 不能直接执行它。工具会自动找到垫片背后的 `node_modules/@wecom/cli/bin/wecom.js`，用 `node` 直接跑（绕开 `cmd /c` 的引号问题）；如果你的安装方式比较特殊导致找不到，就把 `wecom.cli` 改成 `["npx", "-y", "@wecom/cli"]` 或写绝对路径。
8. **"什么算一条待办"的判定**：先按严格规则（有 id + 标题/正文）在返回 JSON 里找，找不到再放宽一次。这样 `{"status":"ok","has_more":false}` 这类包装对象不会被误判成待办；如果你的返回结构特别，跑 `collect` 后看 `data/raw/` 里的原始 JSON，再调 `mapping`。
9. **验证状态**：Windows 本机与 CentOS 服务器均已实跑通过（采集、双向同步、幂等、防环、`auth`、`update`/`finish` 回写、宝塔计划任务）。

---

## 11. 隐私与安全

本仓库**不含任何个人数据**：示例数据（`samples/`、`selftest.py`、README 里的 JSON）统一使用 `tdemuEXAMPLE0001`、`woEXAMPLEUSERID`、`张三` 这类占位符。

只有运行时才会产生、且已被 `.gitignore` 排除的文件：

| 文件 | 内容 | 敏感 |
| --- | --- | --- |
| `config.json` | `app_secret`、`open_id`、企微 userid | **是** |
| `data/user_token.json` | 飞书 `user_access_token` / `refresh_token` | **是** |
| `data/wecom_todo.db` | 你的待办内容与同步映射 | 是 |
| `data/raw/*.json` | 接口原始返回（含真实 userid、待办标题） | 是 |
| `exports/*`、`logs/*` | 导出报表与运行日志 | 是 |

提交前自检：

```bash
git ls-files | grep -Ei 'config\.json|user_token|\.db$|^exports/|^logs/'   # 应无输出
git status --ignored --short | head
```

如果不小心提交过：**先重置 App Secret 和重新授权**，再用 `git filter-repo` 清理历史（只删文件不够，历史里仍然有）。

## 12. License

MIT，见 [LICENSE](LICENSE)。
