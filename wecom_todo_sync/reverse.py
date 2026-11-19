"""反向同步：飞书任务 -> 企业微信待办。

三个动作：
  create  飞书新任务        -> `todo create`（参与人用 follower_ids 字符串数组）
  update  标题/描述/截止改了 -> `todo update`（**不传 followers**，否则会把参与人全量清空）
  finish  飞书里勾了完成     -> `todo finish`（必须 finished_all=True，否则返回 ask_finish_all 二次确认）

防环（双向同步的核心）：
  1. 飞书侧：guid 已在前向 `sync_map` 里 -> 是前向产物，跳过；
  2. 飞书侧：guid 已有反向记录且内容没变 -> 跳过（幂等）；
  3. 企业微信侧：前向 `sync` 会跳过 `reverse_map` 里的待办 id，避免推回飞书。

写企业微信是变更操作，所以 `pull` 默认只预演，`--apply` 才真的写。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from .feishu import FeishuTaskClient
from .identity import resolve_follower_userid
from .models import dump_json
from .store import Store
from .wecom_cli import CliError, WeComCli

# 服务端限制（TodoCreateItem / TodoUpdateItem）
MAX_TITLE_CHARS = 4000
MAX_DESCRIPTION_CHARS = 4000


def _format_deadline(ms: int, all_day: bool) -> str:
    moment = datetime.fromtimestamp(int(ms) / 1000)
    return moment.strftime("%Y-%m-%d") if all_day else moment.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 请求体

def build_create_payload(task: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """飞书任务 -> `todo create` 请求体（也用作"内容是否变化"的比较基准）。

    TodoCreateItem:
      title          必填，<=4000
      description    <=4000
      deadline       {type: date|datetime, value}
      follower_ids   参与人 userid 的字符串数组（注意不是 followers 对象数组）
    """
    reverse_cfg = cfg.get("reverse") or {}
    guid = str(task.get("guid") or "")
    summary = str(task.get("summary") or "").strip() or f"飞书任务 {guid[:8]}"
    description = str(task.get("description") or "").strip()

    marker = f"来源：飞书任务 {guid}"
    description = f"{description}\n\n---\n{marker}" if description else marker

    item: Dict[str, Any] = {
        "title": summary[:MAX_TITLE_CHARS],
        "description": description[:MAX_DESCRIPTION_CHARS],
    }

    due = task.get("due")
    if isinstance(due, dict):
        timestamp = due.get("timestamp")
        try:
            ms = int(timestamp) if timestamp else 0
        except (TypeError, ValueError):
            ms = 0
        if ms:
            all_day = bool(due.get("is_all_day"))
            item["deadline"] = {
                "type": "date" if all_day else "datetime",
                "value": _format_deadline(ms, all_day),
            }

    follower = str(reverse_cfg.get("default_follower_userid") or "").strip()
    if follower:
        item["follower_ids"] = [follower]

    return {"items": [item]}


def build_update_payload(task: Dict[str, Any], cfg: Dict[str, Any], todo_id: str) -> Dict[str, Any]:
    """飞书任务 -> `todo update` 请求体。

    TodoUpdateItem 的 `followers` 是**全量替换**语义，这里刻意不传，
    这样只改标题/描述/截止时间，不会把已有参与人清空。
    """
    created = build_create_payload(task, cfg)["items"][0]
    item: Dict[str, Any] = {
        "todo_id": todo_id,
        "title": created["title"],
        "description": created["description"],
    }
    if "deadline" in created:
        item["deadline"] = created["deadline"]
    return {"items": [item]}


def build_finish_payload(todo_id: str) -> Dict[str, Any]:
    """`todo finish` 请求体。

    finished_all=True = 以创建人（机器人）身份整体完成；
    不传会返回 ask_finish_all，让调用方二次确认。
    """
    return {"items": [{"todo_id": todo_id, "finished_all": True}]}


def payload_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(dump_json(payload).encode("utf-8")).hexdigest()[:32]


# ------------------------------------------------------------------ 判定

def is_completed(task: Dict[str, Any]) -> bool:
    """飞书对未完成的任务返回 completed_at="0"（字符串），不能用真值判断。"""
    value = task.get("completed_at")
    if value is None or value == "":
        return False
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return str(value) not in ("0", "null", "None")


def _parse_result_items(response: List[Any]) -> tuple[List[str], List[str]]:
    """解析 create/update/finish 的响应（三者都是 items[].{success,todo_id,errmsg}）。"""
    ok_ids: List[str] = []
    errors: List[str] = []
    for page in response:
        items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("success") is False:
                errors.append(f"{item.get('title') or item.get('todo_id') or '?'}：{item.get('errmsg') or '未知原因'}")
                continue
            todo_id = item.get("todo_id")
            if isinstance(todo_id, str) and todo_id:
                ok_ids.append(todo_id)
    return ok_ids, errors


# 兼容旧名字
_parse_create_response = _parse_result_items


# ------------------------------------------------------------------ 主流程

def pull(
    cfg: Dict[str, Any],
    store: Store,
    apply: bool = False,
    limit: Optional[int] = None,
    verbose: bool = False,
) -> Dict[str, int]:
    """扫描飞书任务，创建/更新/完成企业微信待办。apply=False 只预演。"""
    reverse_cfg = cfg.get("reverse") or {}
    create_method = [str(part) for part in (reverse_cfg.get("create_method") or ["todo", "create"])]
    update_method = [str(part) for part in (reverse_cfg.get("update_method") or ["todo", "update"])]
    finish_method = [str(part) for part in (reverse_cfg.get("finish_method") or ["todo", "finish"])]
    follower = str(reverse_cfg.get("default_follower_userid") or "").strip()
    max_pages = int(reverse_cfg.get("max_pages", 20) or 20)

    stats = {
        "scanned": 0,
        "created": 0,
        "updated": 0,
        "finished": 0,
        "planned": 0,
        "skipped": 0,
        "failed": 0,
    }
    skip_reasons: List[tuple] = []
    run_id = store.start_run("pull")

    client = FeishuTaskClient(cfg)
    cli: Optional[WeComCli] = WeComCli(cfg) if apply else None

    # 没显式配置参与人时，自动识别扫码账号（todo list 的对话人）
    if not follower:
        follower = resolve_follower_userid(cfg, store, cli, verbose=verbose)
        if follower:
            reverse_cfg["default_follower_userid"] = follower
            print(f"[auto] 待办参与人使用扫码账号：{follower}")
        else:
            print(
                "[info] 未能识别扫码账号的 userid，本次待办不指定参与人；"
                "可在 config 里设置 reverse.default_follower_userid。"
            )

    for task in client.iter_tasks(page_size=50, max_pages=max_pages):
        if limit and (stats["created"] + stats["updated"] + stats["finished"] + stats["planned"]) >= limit:
            break

        stats["scanned"] += 1
        guid = str(task.get("guid") or "")
        if not guid:
            stats["skipped"] += 1
            skip_reasons.append(("?", "响应里没有 guid"))
            continue

        # 防环 1：前向同步建出来的飞书任务
        if store.get_mapping_by_guid(guid) is not None:
            stats["skipped"] += 1
            skip_reasons.append((guid[:8], "前向同步建的任务"))
            if verbose:
                print(f"[pull] 跳过 {guid[:8]}（前向同步建的任务）")
            continue

        completed = is_completed(task)
        mapping = store.get_reverse_mapping(guid)
        todo_id = str(mapping["wecom_todo_id"]) if mapping is not None and mapping["wecom_todo_id"] else ""

        # ---------- 还没建过：create ----------
        if not todo_id:
            if completed:
                stats["skipped"] += 1
                skip_reasons.append((guid[:8], "任务已完成，不新建待办"))
                continue

            payload = build_create_payload(task, cfg)
            digest = payload_hash(payload)
            title = payload["items"][0]["title"]

            if not apply:
                stats["planned"] += 1
                print(f"[plan] create  {guid[:8]}  {title[:60]}")
                print(json.dumps(payload, ensure_ascii=False))
                continue

            try:
                assert cli is not None
                response = cli.call_method(create_method, payload)
            except CliError as exc:
                stats["failed"] += 1
                store.set_reverse_mapping(guid, None, digest, error=str(exc))
                print(f"[pull] 创建失败 {guid[:8]}：{exc}")
                continue

            created_ids, errors = _parse_result_items(response)
            if errors:
                stats["failed"] += 1
                store.set_reverse_mapping(guid, created_ids[0] if created_ids else None, digest, error="; ".join(errors))
                print(f"[pull] 创建失败 {guid[:8]}：{'; '.join(errors)}")
                continue

            store.set_reverse_mapping(guid, created_ids[0] if created_ids else None, digest)
            stats["created"] += 1
            print(f"[pull] 已建待办 {guid[:8]} -> {created_ids[0] if created_ids else '(没拿到 todo_id)'}  {title[:60]}")
            continue

        # ---------- 已建过：按需 update / finish ----------
        payload = build_create_payload(task, cfg)
        digest = payload_hash(payload)
        need_update = (mapping["pushed_hash"] or "") != digest
        need_finish = completed and not mapping["finished_synced"]

        if not need_update and not need_finish:
            stats["skipped"] += 1
            skip_reasons.append((guid[:8], "内容和状态都没变化"))
            if verbose:
                print(f"[pull] 跳过 {guid[:8]}（无变化）")
            continue

        if not apply:
            stats["planned"] += (1 if need_update else 0) + (1 if need_finish else 0)
            if need_update:
                print(f"[plan] update  {guid[:8]} -> {todo_id[:12]}  {payload['items'][0]['title'][:50]}")
                print(json.dumps(build_update_payload(task, cfg, todo_id), ensure_ascii=False))
            if need_finish:
                print(f"[plan] finish  {guid[:8]} -> {todo_id[:12]}")
                print(json.dumps(build_finish_payload(todo_id), ensure_ascii=False))
            continue

        failed = False
        if need_update:
            try:
                assert cli is not None
                _, errors = _parse_result_items(cli.call_method(update_method, build_update_payload(task, cfg, todo_id)))
            except CliError as exc:
                errors = [str(exc)]
            if errors:
                failed = True
                stats["failed"] += 1
                store.set_reverse_mapping(guid, todo_id, mapping["pushed_hash"], error="; ".join(errors))
                print(f"[pull] 更新失败 {guid[:8]}：{'; '.join(errors)}")
            else:
                stats["updated"] += 1
                print(f"[pull] 已更新待办 {todo_id[:12]}  {payload['items'][0]['title'][:50]}")

        if need_finish and not failed:
            try:
                assert cli is not None
                _, errors = _parse_result_items(cli.call_method(finish_method, build_finish_payload(todo_id)))
            except CliError as exc:
                errors = [str(exc)]
            if errors:
                failed = True
                stats["failed"] += 1
                store.set_reverse_mapping(guid, todo_id, digest if not need_update else mapping["pushed_hash"], error="; ".join(errors))
                print(f"[pull] 完成失败 {guid[:8]}：{'; '.join(errors)}")
            else:
                stats["finished"] += 1
                print(f"[pull] 已标记完成 {todo_id[:12]}")

        if not failed:
            store.set_reverse_mapping(guid, todo_id, digest, finished_synced=completed)

    if not stats["planned"] and not (stats["created"] or stats["updated"] or stats["finished"]) and skip_reasons:
        print("[pull] 这次没有可同步的任务，跳过原因：")
        for short, reason in skip_reasons[:10]:
            print(f"  - {short}  {reason}")
        if len(skip_reasons) > 10:
            print(f"  ... 还有 {len(skip_reasons) - 10} 条")

    store.finish_run(run_id, stats)
    return stats
