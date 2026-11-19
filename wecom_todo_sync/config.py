"""配置加载：内置默认值 + JSON 配置文件 + 环境变量覆盖。

字段映射（mapping）全部集中在这里，是为了应对一个现实问题：
企业微信待办接口/CLI 的返回字段名在不同版本、不同租户下可能不同。
拿到真实返回后（`doctor` / `collect --raw-dump`），只改 mapping 就能适配，无需改代码。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

# ---------------------------------------------------------------- 默认配置

DEFAULT_CONFIG = {
    "wecom": {
        # 调用官方 CLI 的命令前缀；装了全局包一般就是 ["wecom-cli"]。
        # 如果没装全局包，可以写成 ["npx", "-y", "@wecom/cli"]。
        "cli": ["wecom-cli"],
        # 显式指定"列出待办"的方法，例如 [["todo", "list"]]。
        # 留空 = 自动从 `wecom-cli schema list` / `wecom-cli todo --help` 里发现。
        "list_methods": [],
        # 传给 list 方法的固定参数，例如 {"status": 0}、{"completed": false}。
        "list_params": {},
        # 自动分页拉取的最大页数（CLI 的 --page-count）。
        "page_count": 20,
        # 分页间隔毫秒（CLI 的 --page-delay）。
        "page_delay_ms": 150,
        "timeout_seconds": 300,
        # 附加到每次 CLI 调用的额外参数，例如 ["--output-dir", "data/files"]。
        "extra_args": [],
        # list 只返回标题/描述/人名；用 get 再补一次详情（截止时间、参与人、来源等）。
        "enrich_with_get": True,
        "get_method": ["todo", "get"],
        # get 每次最多 20 个 id（服务端限制）
        "detail_batch_size": 20,
        # 最多补多少条详情，避免待办特别多时拖很久
        "detail_limit": 200,
    },
    "feishu": {
        "base_url": "https://open.feishu.cn",  # Lark 国际版填 https://open.larksuite.com
        "app_id": "",
        "app_secret": "",
        # 二选一：填了 user_access_token 就优先用它（u-xxx），否则用 tenant_access_token。
        "user_access_token": "",
        "user_id_type": "open_id",  # open_id / union_id / user_id
        # 新建任务默认指派人（open_id）。留空 = 只创建任务、不指派。
        "default_assignee_open_id": "",
        # 可选：把任务放进指定清单（Task v2 的 tasklists 字段）。
        "tasklist_guid": "",
        # 是否在任务描述末尾附带来源标记（便于人工核对/去重）。
        "include_source_id": True,
        # 企业微信待办被重新打开时，是否也把飞书任务恢复为未完成（默认关闭）。
        "allow_reopen": False,
        "timeout_seconds": 30,
        # 用户授权（反向同步要读"你本人的"任务，必须用 user_access_token）
        "oauth_redirect": "http://localhost:8765/callback",
        "oauth_scope": "task:task:read",
        # 本地回调服务监听的地址。留空 = 用 oauth_redirect 里的主机名（一般 localhost）。
        # 跑在 Docker 里时必须设为 0.0.0.0，否则宿主机端口映射连不进来。
        "oauth_bind_host": "",
    },
    "store": {
        "db_path": "data/wecom_todo.db",
        "raw_dir": "data/raw",  # 每次采集的原始 JSON 归档目录
    },
    "export": {
        "dir": "exports",
        "formats": ["markdown", "json", "csv"],
    },
    # 反向同步：飞书任务 -> 企业微信待办（python -m wecom_todo_sync pull）
    "reverse": {
        "create_method": ["todo", "create"],
        "update_method": ["todo", "update"],
        "finish_method": ["todo", "finish"],
        # 建出来的待办挂给谁（企业微信 userid）。留空的话企业微信「待办」里可能看不到。
        "default_follower_userid": "",
        # 最多翻多少页飞书任务列表（每页 50）
        "max_pages": 20,
    },
    # 会议 -> 待办（python -m wecom_todo_sync meetings）——默认关闭
    "meetings": {
        "enabled": False,
        "list_method": ["meeting", "list"],
        "create_method": ["todo", "create"],
        # 时间窗口：从「今天 - days_back」到「今天 + days_ahead」
        "days_ahead": 7,
        "days_back": 0,
        # 除了"我创建的会议"，是否也同步"我参加的会议"
        "include_attended": False,
        # 待办参与人；留空则复用 reverse.default_follower_userid
        "follower_userid": "",
        "limit": 50,
    },
    # 从企业微信一侧的 dict 里挑字段用的候选键名（按顺序取第一个存在的）。
    "mapping": {
        "id": ["id", "todo_id", "todoId", "guid", "task_id", "taskId", "_id", "key"],
        "title": ["title", "summary", "name", "subject", "text"],
        "description": ["description", "content", "desc", "detail", "body", "remark", "note"],
        "status": ["status", "state", "completed", "is_completed", "isCompleted", "done", "finished"],
        "due": [
            "due", "due_time", "dueTime", "deadline", "end_time", "endTime",
            "remind_time", "remindTime", "expire_time", "expireTime",
        ],
        "created": ["create_time", "createTime", "created_at", "createdAt", "ctime"],
        "updated": ["update_time", "updateTime", "updated_at", "updatedAt", "mtime"],
        "owner": ["owner", "creator", "creator_id", "creatorId", "userid", "user_id", "userId"],
        # 一个"待办"对象里如果还嵌着子项，就把子项展开成独立待办。
        "children": ["todo_list", "todoList", "items", "todos", "children", "sub_todos", "subTodos"],
        # 时间字段里嵌套取值的键，例如 {"due": {"timestamp": 1}} 或 {"due": 1699999999}
        "nested_time": ["timestamp", "time", "ts", "value", "date"],
    },
    # 状态归一化：键为字符串化后的原始状态，值为 open / completed。
    # 企业微信待办实测状态值：proceed=进行中；其他版本可能是 0/1 或 done/completed。
    "status_map": {
        "0": "open",
        "1": "completed",
        "2": "completed",
        "false": "open",
        "true": "completed",
        "open": "open",
        "pending": "open",
        "todo": "open",
        "unfinished": "open",
        "proceed": "open",
        "proceeding": "open",
        "doing": "open",
        "in_progress": "open",
        "processing": "open",
        "started": "open",
        "未完成": "open",
        "进行中": "open",
        "已完成": "completed",
        "completed": "completed",
        "complete": "completed",
        "done": "completed",
        "finished": "completed",
        # 已删除的待办：归档但不同步（sync 会跳过）
        "deleted": "deleted",
        "已删除": "deleted",
    },
}

# 环境变量 -> 配置路径
ENV_MAP = {
    "FEISHU_APP_ID": "feishu.app_id",
    "FEISHU_APP_SECRET": "feishu.app_secret",
    "FEISHU_BASE_URL": "feishu.base_url",
    "FEISHU_USER_ACCESS_TOKEN": "feishu.user_access_token",
    "FEISHU_ASSIGNEE_OPEN_ID": "feishu.default_assignee_open_id",
    "FEISHU_TASKLIST_GUID": "feishu.tasklist_guid",
    "FEISHU_OAUTH_BIND": "feishu.oauth_bind_host",
    "WECOM_TODO_DB": "store.db_path",
}

DEFAULT_CONFIG_NAME = "config.json"


def _deep_merge(base: dict, override: dict) -> dict:
    """把 override 递归合并进 base 的副本。"""
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _set_path(cfg: dict, dotted: str, value) -> None:
    node = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def load_config(path: str | None = None) -> tuple[dict, Path]:
    """读取配置。

    返回 (config, base_dir)。base_dir 用于解析所有相对路径：
    给定了配置文件就用配置文件所在目录，否则用当前工作目录。
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)

    config_path = Path(path) if path else Path.cwd() / DEFAULT_CONFIG_NAME
    base_dir = config_path.parent if path else Path.cwd()

    if config_path.is_file():
        # utf-8-sig：Windows 记事本/PowerShell 保存的 UTF-8 常带 BOM，这里一并兼容
        with config_path.open("r", encoding="utf-8-sig") as handle:
            user_cfg = json.load(handle)
        if not isinstance(user_cfg, dict):
            raise ValueError(f"配置文件格式错误（应为 JSON object）：{config_path}")
        _deep_merge(cfg, user_cfg)
        base_dir = config_path.parent
    elif path:
        raise FileNotFoundError(f"找不到配置文件：{config_path}")

    for env_name, dotted in ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw:
            _set_path(cfg, dotted, raw)

    cli_env = os.environ.get("WECOM_CLI")
    if cli_env:
        cfg["wecom"]["cli"] = cli_env.split()

    # 归一化：cli 允许写成字符串
    if isinstance(cfg["wecom"].get("cli"), str):
        cfg["wecom"]["cli"] = cfg["wecom"]["cli"].split()

    # 归一化：list_methods 允许写成 [["todo","list"]] 或 ["todo list"]
    methods = cfg["wecom"].get("list_methods") or []
    normalized = []
    for item in methods:
        if isinstance(item, str):
            normalized.append(item.split())
        elif isinstance(item, (list, tuple)):
            normalized.append([str(part) for part in item])
    cfg["wecom"]["list_methods"] = normalized

    return cfg, base_dir


def resolve_path(base_dir: Path, value: str) -> Path:
    """把配置里的相对路径解析为绝对路径。"""
    p = Path(value)
    return p if p.is_absolute() else (base_dir / p)
