"""会议 -> 待办：把企业微信里"我创建的会议"同步成待办。

数据源：`meeting list`（按开始时间范围拉取）
  - created_meetings   我创建的会议
  - attended_meetings  我参加的会议（可选）
字段（MeetingRecord）：meeting_id / sub_meeting_id / subject / begin_time / end_time /
                      timezone / meeting_room / location / is_repeat_meeting / attendee_count / creator_name

写回：`todo create`（items[].{title, description, deadline{type,value}, follower_ids}）

去重：source_id = meeting:{meeting_id}:{sub_meeting_id}，记在 meeting_map 表里；
      同一场会议第二次跑就跳过（除非加上 --force 重推）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .identity import resolve_follower_userid
from .models import dump_json
from .store import Store
from .wecom_cli import CliError, WeComCli

MAX_TITLE_CHARS = 4000
MAX_DESCRIPTION_CHARS = 4000
TIME_FMT = "%Y-%m-%d %H:%M:%S"


# ------------------------------------------------------------------ 拉取

def time_window(cfg: Dict[str, Any], now: Optional[datetime] = None) -> tuple[str, str]:
    """按配置算出 [begin_time, end_time]。"""
    meeting_cfg = cfg.get("meetings") or {}
    now = now or datetime.now()
    days_back = int(meeting_cfg.get("days_back", 0) or 0)
    days_ahead = int(meeting_cfg.get("days_ahead", 7) or 7)
    begin = now - timedelta(days=days_back)
    end = now + timedelta(days=days_ahead)
    return begin.strftime(TIME_FMT), end.strftime(TIME_FMT)


def fetch_meetings(
    cli: WeComCli,
    cfg: Dict[str, Any],
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """拉取时间窗口内的会议（自动翻页）。返回带 __origin 标记的列表。"""
    meeting_cfg = cfg.get("meetings") or {}
    method = [str(part) for part in (meeting_cfg.get("list_method") or ["meeting", "list"])]
    begin, end = time_window(cfg)
    params = {"begin_time": begin, "end_time": end, "limit": 100}

    if verbose:
        print(f"[meetings] 拉取 {begin} ~ {end}")

    pages = cli.fetch_pages(method, params=params)
    meetings: List[Dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        for item in page.get("created_meetings") or []:
            if isinstance(item, dict):
                item = dict(item)
                item["__origin"] = "created"
                meetings.append(item)
        if meeting_cfg.get("include_attended"):
            for item in page.get("attended_meetings") or []:
                if isinstance(item, dict):
                    item = dict(item)
                    item["__origin"] = "attended"
                    meetings.append(item)
    return meetings


# ------------------------------------------------------------------ 转换

def meeting_source_id(meeting: Dict[str, Any]) -> str:
    meeting_id = str(meeting.get("meeting_id") or "")
    sub_id = str(meeting.get("sub_meeting_id") or "")
    return f"meeting:{meeting_id}:{sub_id}"


def _tz_text(meeting: Dict[str, Any]) -> str:
    tz = meeting.get("timezone")
    if isinstance(tz, dict):
        if tz.get("timezone_id"):
            return str(tz["timezone_id"])
        offset = tz.get("timezone_offset")
        if isinstance(offset, int):
            hours = offset / 3600
            return f"UTC{hours:+g}"
    return ""


def build_todo_payload(meeting: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """会议 -> `todo create` 请求体。"""
    meeting_cfg = cfg.get("meetings") or {}
    subject = str(meeting.get("subject") or "").strip() or "未命名会议"

    lines: List[str] = []
    begin = str(meeting.get("begin_time") or "").strip()
    end = str(meeting.get("end_time") or "").strip()
    tz = _tz_text(meeting)
    when = " ~ ".join(part for part in (begin, end) if part)
    if when:
        lines.append(f"时间：{when}" + (f"（{tz}）" if tz else ""))
    if meeting.get("location"):
        lines.append(f"地点：{meeting['location']}")
    if meeting.get("meeting_room"):
        lines.append(f"会议室：{meeting['meeting_room']}")
    if meeting.get("attendee_count") is not None:
        lines.append(f"参会人数：{meeting['attendee_count']}")
    if meeting.get("creator_name"):
        lines.append(f"创建人：{meeting['creator_name']}")
    if meeting.get("is_repeat_meeting"):
        lines.append("周期性会议：是")

    marker = f"来源：企业微信会议 {meeting.get('meeting_id') or ''}".rstrip()
    lines.append("---")
    lines.append(marker)
    description = "\n".join(lines)

    item: Dict[str, Any] = {
        "title": subject[:MAX_TITLE_CHARS],
        "description": description[:MAX_DESCRIPTION_CHARS],
    }

    # begin_time 就是 "YYYY-MM-DD HH:MM:SS"，正好是 deadline.datetime 要求的格式
    if begin:
        item["deadline"] = {"type": "datetime", "value": begin}

    follower = str(meeting_cfg.get("follower_userid") or "").strip()
    if not follower:
        follower = str((cfg.get("reverse") or {}).get("default_follower_userid") or "").strip()
    if follower:
        item["follower_ids"] = [follower]

    return {"items": [item]}


def payload_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(dump_json(payload).encode("utf-8")).hexdigest()[:32]


def _parse_result_items(response: List[Any]) -> tuple[List[str], List[str]]:
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
                errors.append(f"{item.get('title') or '?'}：{item.get('errmsg') or '未知原因'}")
                continue
            todo_id = item.get("todo_id")
            if isinstance(todo_id, str) and todo_id:
                ok_ids.append(todo_id)
    return ok_ids, errors


# ------------------------------------------------------------------ 主流程

def sync_meetings(
    cfg: Dict[str, Any],
    store: Store,
    apply: bool = False,
    limit: Optional[int] = None,
    force: bool = False,
    verbose: bool = False,
) -> Dict[str, int]:
    """把会议同步成待办。apply=False 只预演。"""
    stats = {"scanned": 0, "created": 0, "planned": 0, "skipped": 0, "failed": 0}
    meeting_cfg = cfg.get("meetings") or {}
    create_method = [str(part) for part in (meeting_cfg.get("create_method") or ["todo", "create"])]
    run_id = store.start_run("meetings")

    cli = WeComCli(cfg)

    # 会议待办同样支持“扫码账号即参与人”：显式配置 > 自动识别
    follower = str(meeting_cfg.get("follower_userid") or "").strip()
    if not follower:
        follower = str((cfg.get("reverse") or {}).get("default_follower_userid") or "").strip()
    if not follower:
        follower = resolve_follower_userid(cfg, store, cli, verbose=verbose)
        if follower:
            cfg.setdefault("reverse", {})["default_follower_userid"] = follower
            if verbose:
                print(f"[auto] 待办参与人使用扫码账号：{follower}")

    meetings = fetch_meetings(cli, cfg, verbose=verbose)
    stats["scanned"] = len(meetings)

    seen = set()
    for meeting in meetings:
        source_id = meeting_source_id(meeting)
        subject = str(meeting.get("subject") or "(未命名)")
        if source_id in seen:
            continue
        seen.add(source_id)

        if limit and (stats["created"] + stats["planned"]) >= limit:
            break

        existing = store.get_meeting_mapping(source_id)
        if existing is not None and existing["todo_id"] and not force:
            stats["skipped"] += 1
            if verbose:
                print(f"[meetings] 跳过 {subject[:40]}（已建待办 {existing['todo_id'][:12]}）")
            continue

        payload = build_todo_payload(meeting, cfg)
        digest = payload_hash(payload)

        if not apply:
            stats["planned"] += 1
            print(f"[plan] {str(meeting.get('begin_time') or '')[:16]}  {subject[:50]}")
            print(json.dumps(payload, ensure_ascii=False))
            continue

        try:
            response = cli.call_method(create_method, payload)
        except CliError as exc:
            stats["failed"] += 1
            store.set_meeting_mapping(source_id, str(meeting.get("meeting_id") or ""), subject, None, digest, error=str(exc))
            print(f"[meetings] 失败 {subject[:40]}：{exc}")
            continue

        todo_ids, errors = _parse_result_items(response)
        if errors:
            stats["failed"] += 1
            store.set_meeting_mapping(
                source_id,
                str(meeting.get("meeting_id") or ""),
                subject,
                todo_ids[0] if todo_ids else None,
                digest,
                error="; ".join(errors),
            )
            print(f"[meetings] 失败 {subject[:40]}：{'; '.join(errors)}")
            continue

        store.set_meeting_mapping(
            source_id,
            str(meeting.get("meeting_id") or ""),
            subject,
            todo_ids[0] if todo_ids else None,
            digest,
        )
        stats["created"] += 1
        print(f"[meetings] 已建待办 {subject[:40]} -> {todo_ids[0] if todo_ids else '(没拿到 todo_id)'}")

    store.finish_run(run_id, stats)
    return stats
