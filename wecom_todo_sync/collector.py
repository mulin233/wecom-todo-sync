"""采集编排：从 wecom-cli（或本地 JSON 文件）拿到待办，归一化成 Todo。

`todo list` 只返回 标题/描述/人名；截止时间、参与人、来源等要再调 `todo get` 补详情。
`get` 每次最多 20 个 id，所以这里按批补全，补失败也不影响主流程。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import resolve_path
from .models import Todo, flatten_todos, pick
from .wecom_cli import CliError, WeComCli, extract_raw_items, load_raw_from_file


@dataclass
class CollectResult:
    todos: List[Todo] = field(default_factory=list)
    method: Optional[List[str]] = None
    pages: int = 0
    raw_pages: List[Any] = field(default_factory=list)
    raw_items: int = 0
    details: int = 0
    detail_pages: List[Any] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    raw_dump_path: Optional[str] = None


def collect(
    cfg: Dict[str, Any],
    base_dir: Path,
    source: str = "cli",
    source_file: Optional[str] = None,
    dump_raw: bool = True,
    verbose: bool = False,
    known_ids: Optional[List[str]] = None,
) -> CollectResult:
    """执行一次采集。source 取 'cli' 或 'file'。

    known_ids：本地库里已知的 todo_id。`list` 只返回进行中的待办，
    已完成的会消失，所以要对这些 id 单独 get 一次才能发现状态变化。
    """
    result = CollectResult()
    cli: Optional[WeComCli] = None

    if source == "file":
        if not source_file:
            raise ValueError("source=file 时必须指定 --source-file")
        result.raw_pages = load_raw_from_file(resolve_path(base_dir, source_file))
        result.method = ["file", str(source_file)]
    else:
        cli = WeComCli(cfg)
        status = cli.auth_status()
        if status == "unauthorized":
            raise CliError("wecom-cli 未授权，请先执行：wecom-cli auth init")

        methods = cli.candidate_methods()
        if not methods:
            raise CliError(
                "没有发现可用的待办方法。请先执行 `wecom-cli todo --help` 查看实际方法，"
                "然后在 config.json 里显式设置 wecom.list_methods，例如 "
                '{"wecom": {"list_methods": [["todo", "list"]]}}'
            )

        for method in methods:
            if verbose:
                print(f"[collect] 尝试方法：{' '.join(method)}")
            try:
                pages = cli.fetch_pages(method)
            except CliError as exc:
                result.warnings.append(f"{' '.join(method)} 调用失败：{exc}")
                continue

            # 调用成功就采用，即使这次是空列表：
            # 已完成的待办会从 list 里消失，后面还要靠 get 按已知 id 补状态。
            result.raw_pages = pages
            result.method = method
            if not extract_raw_items(pages, cfg["mapping"]):
                result.warnings.append(
                    f"{' '.join(method)} 本次返回 0 条（未完成的待办可能真的没有了，"
                    "已完成的会从列表里消失，下面会按已知 id 调 get 确认状态）"
                )
            break

        if result.method is None:
            result.warnings.append(
                "所有候选方法调用都失败了。可以用 `python -m wecom_todo_sync doctor` 查看候选方法，"
                "并在 config.json 的 wecom.list_methods 中显式指定。"
            )
            return result

    result.pages = len(result.raw_pages)
    raw_items = extract_raw_items(result.raw_pages, cfg["mapping"])
    result.raw_items = len(raw_items)

    if cli is not None and (cfg.get("wecom") or {}).get("enrich_with_get", True):
        raw_items = enrich_with_details(
            cli, cfg, raw_items, result, verbose=verbose, extra_ids=known_ids
        )

    result.todos = flatten_todos(raw_items, cfg["mapping"], cfg["status_map"])

    if dump_raw and result.raw_pages:
        result.raw_dump_path = _dump_raw(cfg, base_dir, result)

    return result


# ------------------------------------------------------------------ 补详情

def merge_details(
    raw_items: List[Dict[str, Any]],
    details_by_id: Dict[str, Dict[str, Any]],
    mapping: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    """把 get 的详情合并进 list 的条目（详情优先，list 独有的字段保留）。

    纯函数，便于离线自检。
    """
    merged_items: List[Dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        todo_id = pick(item, mapping.get("id", []))
        detail = details_by_id.get(str(todo_id)) if todo_id else None
        if detail is None:
            merged_items.append(item)
            continue
        combined = dict(item)
        combined.update(detail)
        merged_items.append(combined)
    return merged_items


def enrich_with_details(
    cli: WeComCli,
    cfg: Dict[str, Any],
    raw_items: List[Dict[str, Any]],
    result: CollectResult,
    verbose: bool = False,
    extra_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """用 `todo get` 批量补详情；失败只记 warning，不影响主流程。

    extra_ids 是本地库里已知、但本次 list 没返回的 todo_id——已完成/已删除的
    待办会从 list 消失，只能靠 get 拿到它们的最新状态。
    """
    mapping = cfg["mapping"]
    wecom_cfg = cfg.get("wecom") or {}
    method = [str(part) for part in (wecom_cfg.get("get_method") or [])]
    if not method:
        return raw_items

    batch_size = int(wecom_cfg.get("detail_batch_size", 20) or 20)
    batch_size = max(1, min(20, batch_size))  # 服务端上限 20
    limit = int(wecom_cfg.get("detail_limit", 200) or 200)

    todo_ids: List[str] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        todo_id = pick(item, mapping.get("id", []))
        if todo_id and str(todo_id) not in todo_ids:
            todo_ids.append(str(todo_id))
    for todo_id in extra_ids or []:
        if todo_id and str(todo_id) not in todo_ids:
            todo_ids.append(str(todo_id))
    todo_ids = todo_ids[:limit]

    if not todo_ids:
        return raw_items

    details_by_id: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(todo_ids), batch_size):
        chunk = todo_ids[start:start + batch_size]
        if verbose:
            print(f"[collect] 补详情：{len(chunk)} 条（{start + 1}~{start + len(chunk)}）")
        try:
            pages = cli.fetch_pages(
                method,
                params={"items": [{"todo_id": todo_id} for todo_id in chunk]},
                paginate=False,  # get 按 id 批量查，没有游标分页
            )
        except CliError as exc:
            result.warnings.append(f"补详情失败（{len(chunk)} 条）：{exc}")
            continue

        result.detail_pages.extend(pages)
        for detail in _detail_items(pages, mapping):
            if detail.get("success") is False:
                result.warnings.append(
                    f"待办 {detail.get('todo_id') or '?'} 详情读取失败：{detail.get('errmsg') or '未知原因'}"
                )
                continue
            detail_id = pick(detail, mapping.get("id", []))
            if detail_id:
                details_by_id[str(detail_id)] = detail

    result.details = len(details_by_id)
    if not details_by_id:
        return raw_items
    return combine_details(raw_items, details_by_id, mapping)


def combine_details(
    raw_items: List[Dict[str, Any]],
    details_by_id: Dict[str, Dict[str, Any]],
    mapping: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    """合并详情，并补上"只在 get 里有、list 里没有"的待办。

    典型场景：待办在企业微信里被标记完成/删除后，`list` 就不返回它了，
    只有 `get` 还能按 id 查到最新状态。不补进来的话，飞书那边永远不会变。
    """
    merged = merge_details(raw_items, details_by_id, mapping)

    present = set()
    for item in raw_items:
        if isinstance(item, dict):
            todo_id = pick(item, mapping.get("id", []))
            if todo_id:
                present.add(str(todo_id))

    for todo_id, detail in details_by_id.items():
        if str(todo_id) not in present:
            merged.append(detail)
    return merged


def _detail_items(pages: List[Any], mapping: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """取出 get 返回的条目。

    `todo.get` 的响应结构是 {"items": [...], "items_count": n}，已知就直接按 items 取，
    这样 success=false（只有 errmsg、没有 title）的条目也能被识别出来并告警；
    结构对不上时退回启发式抽取。
    """
    collected: List[Dict[str, Any]] = []
    for page in pages:
        found: Optional[List[Dict[str, Any]]] = None
        if isinstance(page, dict) and isinstance(page.get("items"), list):
            found = [item for item in page["items"] if isinstance(item, dict)]
        if found is None:
            found = list(extract_raw_items([page], mapping))
        collected.extend(found)
    return collected


# ------------------------------------------------------------------ 归档

def _dump_raw(cfg: Dict[str, Any], base_dir: Path, result: CollectResult) -> Optional[str]:
    """把这次采集的原始 JSON 存一份，方便核对字段名。"""
    raw_dir_value = (cfg.get("store") or {}).get("raw_dir")
    if not raw_dir_value:
        return None
    raw_dir = resolve_path(base_dir, raw_dir_value)
    try:
        raw_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        method_part = "-".join(result.method) if result.method else "unknown"
        path = raw_dir / f"collect-{stamp}-{method_part}.json"
        payload = {
            "collected_at": datetime.now().isoformat(timespec="seconds"),
            "method": result.method,
            "details_fetched": result.details,
            "pages": result.raw_pages,
            "detail_pages": result.detail_pages,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
    except OSError as exc:
        result.warnings.append(f"原始数据归档失败：{exc}")
        return None
