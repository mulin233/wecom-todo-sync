"""同步编排：采集 -> 入库 -> 推送到飞书任务。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .collector import CollectResult, collect
from .feishu import FeishuError, FeishuTaskClient, build_task_payload
from .models import Todo, utc_now_ms
from .store import Store

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_SKIP = "skip"


def todo_from_row(row) -> Todo:
    """从 SQLite 行还原 Todo。

    raw_json 必须还原：飞书的"全天"标记（deadline.type）和"参与人"（followers）
    都只在原始数据里，丢了会导致同步时缺信息。
    """
    raw: Dict[str, Any] = {}
    raw_json = row["raw_json"] if "raw_json" in row.keys() else ""
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                raw = parsed
        except (ValueError, TypeError):
            raw = {}

    return Todo(
        source_id=row["source_id"],
        title=row["title"] or "",
        description=row["description"] or "",
        status=row["status"] or "open",
        due_ms=row["due_ms"],
        created_ms=row["created_ms"],
        updated_ms=row["updated_ms"],
        owner=row["owner"] or "",
        raw=raw,
    )


def decide_action(store: Store, todo: Todo) -> str:
    mapping = store.get_mapping(todo.source_id)
    if mapping is None or not mapping["feishu_task_guid"]:
        return ACTION_CREATE
    if (mapping["synced_hash"] or "") != todo.content_hash():
        return ACTION_UPDATE
    return ACTION_SKIP


def run_collect(
    cfg: Dict[str, Any],
    base_dir: Path,
    store: Store,
    source: str = "cli",
    source_file: Optional[str] = None,
    verbose: bool = False,
    dump_raw: bool = True,
) -> tuple[CollectResult, Dict[str, int]]:
    """采集并写入本地库（不推送）。"""
    stats = {"collected": 0, "created": 0, "updated": 0, "skipped": 0}
    run_id = store.start_run(f"collect:{source}")

    result = collect(
        cfg,
        base_dir,
        source=source,
        source_file=source_file,
        dump_raw=dump_raw,
        verbose=verbose,
        known_ids=store.known_todo_ids(),
    )
    for todo in result.todos:
        outcome = store.upsert_todo(todo)
        stats["collected"] += 1
        if outcome == "created":
            stats["created"] += 1
        elif outcome == "updated":
            stats["updated"] += 1
        else:
            stats["skipped"] += 1

    store.finish_run(run_id, stats, note="; ".join(result.warnings[:3]))
    return result, stats


def sync_todos(
    cfg: Dict[str, Any],
    base_dir: Path,
    store: Store,
    source_ids: Optional[Sequence[str]] = None,
    sync_all: bool = False,
    dry_run: bool = False,
    limit: Optional[int] = None,
    verbose: bool = False,
) -> Dict[str, int]:
    """把库里的待办同步到飞书任务。

    幂等策略：
      - sync_map 里没有记录 -> 创建任务，记录返回的 task_guid
      - 内容哈希变了        -> PATCH 更新（只更新变化的字段集合）
      - 哈希没变            -> 跳过（不产生任何接口调用）
    """
    stats = {"collected": 0, "created": 0, "updated": 0, "completed": 0, "skipped": 0, "failed": 0}

    if source_ids is not None:
        rows = [row for row in (store.get_todo(sid) for sid in source_ids) if row is not None]
    else:
        rows = store.iter_todos(status=None if sync_all else "open", limit=limit)

    if limit:
        rows = rows[:limit]

    client: Optional[FeishuTaskClient] = None
    now_ms = utc_now_ms()
    run_id = store.start_run("sync")

    for row in rows:
        todo = todo_from_row(row)

        # 防环：这条待办是反向同步（飞书 -> 企业微信）建出来的，不能再推回飞书
        if store.is_reverse_created(todo.source_id):
            stats["skipped"] += 1
            if verbose:
                print(f"[sync] 跳过 {todo.source_id}（反向同步建的，避免和飞书任务重复）")
            continue

        mapping = store.get_mapping(todo.source_id)
        action = decide_action(store, todo)

        if action == ACTION_SKIP:
            stats["skipped"] += 1
            if verbose:
                print(f"[sync] 跳过 {todo.source_id}（内容未变化）")
            continue

        # 企业微信里已删除的待办：归档但不推到飞书（避免把删掉的东西建出来）
        if todo.status == "deleted":
            stats["skipped"] += 1
            print(f"[sync] 跳过 {todo.source_id}（企业微信里已删除）")
            continue

        task, update_fields = build_task_payload(todo, cfg, now_ms)

        # 已经同步过"已完成"状态的任务，不要重复下发 completed_at，否则会覆盖完成时间
        if (
            mapping is not None
            and mapping["completed_synced"]
            and todo.status == "completed"
            and "completed_at" in update_fields
        ):
            task.pop("completed_at", None)
            update_fields = [field for field in update_fields if field != "completed_at"]

        if dry_run:
            stats["created" if action == ACTION_CREATE else "updated"] += 1
            print(f"[dry-run] {action:6s} {todo.source_id}  {todo.display_title[:60]}  字段={','.join(update_fields)}")
            continue

        if client is None:
            client = FeishuTaskClient(cfg)

        try:
            if action == ACTION_CREATE:
                created = client.create_task(task)
                guid = created.get("guid") or (created.get("task") or {}).get("guid")
                if not guid:
                    raise FeishuError(f"创建任务成功但没拿到 guid：{created}")
                store.set_mapping(
                    todo.source_id,
                    str(guid),
                    todo.content_hash(),
                    completed_synced=(todo.status == "completed"),
                )
                stats["created"] += 1
                print(f"[sync] 创建任务 {todo.source_id} -> {guid}  {todo.title[:60]}")
            else:
                patch_body: Dict[str, Any] = dict(task)
                patch_body.pop("members", None)  # 成员变更不做自动覆盖，避免误改指派人
                fields = [field for field in update_fields if field != "members"]
                if not fields:
                    stats["skipped"] += 1
                    continue
                client.update_task(str(mapping["feishu_task_guid"]), patch_body, fields)
                store.set_mapping(
                    todo.source_id,
                    str(mapping["feishu_task_guid"]),
                    todo.content_hash(),
                    completed_synced=(todo.status == "completed") or bool(mapping["completed_synced"]),
                )
                stats["updated"] += 1
                if todo.status == "completed":
                    stats["completed"] += 1
                print(f"[sync] 更新任务 {todo.source_id} -> {mapping['feishu_task_guid']}  字段={','.join(fields)}")
        except (FeishuError, ValueError) as exc:
            stats["failed"] += 1
            store.record_sync_error(todo.source_id, str(exc))
            print(f"[sync] 失败 {todo.source_id}：{exc}")

    store.finish_run(run_id, stats)
    return stats
