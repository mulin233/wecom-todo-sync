"""导出：Markdown / CSV / JSON。"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .config import resolve_path
from .models import format_ms
from .store import Store

STATUS_LABEL = {"open": "未完成", "completed": "已完成", "deleted": "已删除"}


def _rows_to_dicts(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    result = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        item["status_label"] = STATUS_LABEL.get(item.get("status"), item.get("status"))
        result.append(item)
    return result


def to_markdown(items: List[Dict[str, Any]], sync_map: Dict[str, Dict[str, Any]]) -> str:
    lines: List[str] = []
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    open_items = [item for item in items if item.get("status") == "open"]
    done_items = [item for item in items if item.get("status") == "completed"]
    deleted_items = [item for item in items if item.get("status") == "deleted"]

    lines.append("# 企业微信待办归档")
    lines.append("")
    lines.append(f"- 导出时间：{stamp}")
    lines.append(
        f"- 合计：{len(items)} 条（未完成 {len(open_items)} / 已完成 {len(done_items)} / 已删除 {len(deleted_items)}）"
    )
    lines.append("")

    for title, group in (
        ("未完成", open_items),
        ("已完成", done_items),
        ("已删除（仅归档，不同步）", deleted_items),
    ):
        if not group:
            continue
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| 标题 | 截止时间 | 负责人 | 来源 ID | 飞书任务 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in group:
            mapping = sync_map.get(item["source_id"]) or {}
            lines.append(
                "| {title} | {due} | {owner} | {sid} | {guid} |".format(
                    title=(item.get("title") or "").replace("|", "\\|") or "(无标题)",
                    due=format_ms(item.get("due_ms")),
                    owner=item.get("owner") or "",
                    sid=item["source_id"],
                    guid=mapping.get("feishu_task_guid") or "",
                )
            )
        lines.append("")
        lines.append("### 明细")
        lines.append("")
        for item in group:
            lines.append(f"#### {item.get('title') or item['source_id']}")
            lines.append("")
            if item.get("due_ms"):
                lines.append(f"- 截止时间：{format_ms(item.get('due_ms'))}")
            if item.get("created_ms"):
                lines.append(f"- 创建时间：{format_ms(item.get('created_ms'))}")
            if item.get("updated_ms"):
                lines.append(f"- 更新时间：{format_ms(item.get('updated_ms'))}")
            lines.append(f"- 状态：{item.get('status_label')}")
            if item.get("owner"):
                lines.append(f"- 负责人：{item['owner']}")
            lines.append(f"- 来源 ID：`{item['source_id']}`")
            lines.append("")
            description = (item.get("description") or "").strip()
            if description:
                for line in description.splitlines():
                    lines.append(f"> {line}")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_csv_text(items: List[Dict[str, Any]], sync_map: Dict[str, Dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["source_id", "title", "status", "due_time", "owner", "feishu_task_guid", "description"])
    for item in items:
        mapping = sync_map.get(item["source_id"]) or {}
        writer.writerow(
            [
                item["source_id"],
                item.get("title") or "",
                item.get("status_label") or "",
                format_ms(item.get("due_ms")),
                item.get("owner") or "",
                mapping.get("feishu_task_guid") or "",
                item.get("description") or "",
            ]
        )
    return buffer.getvalue()


def to_json_text(items: List[Dict[str, Any]], sync_map: Dict[str, Dict[str, Any]]) -> str:
    payload = []
    for item in items:
        mapping = sync_map.get(item["source_id"]) or {}
        entry = dict(item)
        entry["feishu_task_guid"] = mapping.get("feishu_task_guid")
        entry["last_error"] = mapping.get("last_error")
        payload.append(entry)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def export_all(
    cfg: Dict[str, Any],
    base_dir: Path,
    store: Store,
    formats: List[str] | None = None,
    out_dir: str | None = None,
) -> List[str]:
    export_cfg = cfg.get("export") or {}
    formats = formats or export_cfg.get("formats") or ["markdown", "json", "csv"]
    target_dir = resolve_path(base_dir, out_dir or export_cfg.get("dir") or "exports")
    target_dir.mkdir(parents=True, exist_ok=True)

    rows = store.iter_todos()
    items = _rows_to_dicts(rows)
    sync_map = {}
    for item in items:
        mapping = store.get_mapping(item["source_id"])
        if mapping is not None:
            sync_map[item["source_id"]] = {key: mapping[key] for key in mapping.keys()}

    written: List[str] = []
    if "markdown" in formats or "md" in formats:
        path = target_dir / "todos.md"
        path.write_text(to_markdown(items, sync_map), encoding="utf-8")
        written.append(str(path))
    if "csv" in formats:
        path = target_dir / "todos.csv"
        path.write_text(to_csv_text(items, sync_map), encoding="utf-8-sig")
        written.append(str(path))
    if "json" in formats:
        path = target_dir / "todos.json"
        path.write_text(to_json_text(items, sync_map), encoding="utf-8")
        written.append(str(path))
    return written
