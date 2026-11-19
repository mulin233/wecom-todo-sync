"""数据模型与"宽容解析"工具。

企业微信一侧的 JSON 字段名不完全可控，所以这里对所有字段都做多候选键匹配，
并把结果归一化成统一的 Todo 对象。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

# 小于该值的整数不可能是真实时间戳（避免把 status=1 当成时间）
_MIN_PLAUSIBLE_SECONDS = 100_000_000  # 1973-03-03

# 企业微信待办的 description 里可能是 HTML 片段，如 "<div>同步学习</div>"
_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"(?i)<\s*(br|/p|/div)\s*/?\s*>")


# ------------------------------------------------------------------ 工具函数

def pick(obj: Dict[str, Any], keys: Iterable[str], default=None):
    """按顺序返回第一个存在且非空的键值。"""
    for key in keys:
        if key in obj:
            value = obj[key]
            if value is None:
                continue
            if isinstance(value, str) and value.strip() == "":
                continue
            if isinstance(value, (list, dict)) and len(value) == 0:
                continue
            return value
    return default


def to_epoch_ms(value: Any, nested_keys: Optional[Iterable[str]] = None) -> Optional[int]:
    """把各种形态的时间归一化成毫秒时间戳（飞书 Task v2 用毫秒字符串）。

    支持：秒/毫秒时间戳（int/float/数字字符串）、ISO8601、"YYYY-MM-DD HH:MM:SS"、
    以及 {"timestamp": ...} 这类嵌套对象。
    """
    if value is None:
        return None

    if isinstance(value, dict):
        for key in (nested_keys or ["timestamp", "time", "ts", "value", "date"]):
            if key in value:
                return to_epoch_ms(value[key], nested_keys)
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        if number < _MIN_PLAUSIBLE_SECONDS:
            return None
        if number >= 1e12:  # 已经是毫秒
            return int(number)
        return int(number * 1000)  # 秒 -> 毫秒

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return to_epoch_ms(int(text), nested_keys)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return int(parsed.timestamp() * 1000)

    return None


def normalize_status(value: Any, status_map: Dict[str, str]) -> str:
    """把状态归一化为 open / completed。"""
    if value is None:
        return "open"
    if isinstance(value, bool):
        key = "true" if value else "false"
    else:
        key = str(value).strip().lower()

    if key in status_map:
        return status_map[key]

    # 兜底：数字非 0 视为已完成
    try:
        return "completed" if int(float(key)) != 0 else "open"
    except (TypeError, ValueError):
        pass

    if key in ("yes", "y"):
        return "completed"
    return "open"


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def strip_html(text: str) -> str:
    """把待办正文里的 HTML 片段转成纯文本。

    实测 description 形如 "<div>同步学习</div>"；不处理的话飞书任务里会是一堆标签。
    """
    if not text:
        return ""
    if "<" not in text and "&" not in text:
        return text.strip()

    cleaned = _BREAK_RE.sub("\n", text)
    cleaned = _TAG_RE.sub("", cleaned)
    cleaned = html.unescape(cleaned).replace("\xa0", " ")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def owner_text(value: Any) -> str:
    """creator 可能是字符串，也可能是 {"userid": ..., "user_name": ...} 这种对象。"""
    if isinstance(value, dict):
        for key in ("user_name", "name", "userid", "user_id", "id"):
            candidate = value.get(key)
            if candidate:
                return str(candidate)
        return ""
    if value is None:
        return ""
    return str(value)


# ------------------------------------------------------------------ Todo

@dataclass
class Todo:
    source_id: str
    title: str = ""
    description: str = ""
    status: str = "open"  # open | completed
    due_ms: Optional[int] = None
    created_ms: Optional[int] = None
    updated_ms: Optional[int] = None
    owner: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        text = (self.description or "").strip().splitlines()
        if text:
            return text[0][:80]
        return f"(无标题 {self.source_id})"

    def content_hash(self) -> str:
        """用于判断"内容是否变化"（变了才去更新飞书任务）。"""
        payload = dump_json(
            {
                "title": self.title,
                "description": self.description,
                "status": self.status,
                "due_ms": self.due_ms,
            }
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "due_ms": self.due_ms,
            "created_ms": self.created_ms,
            "updated_ms": self.updated_ms,
            "owner": self.owner,
        }


# ------------------------------------------------------------------ 解析

def looks_like_todo(obj: Any, mapping: Dict[str, List[str]], strict: bool = True) -> bool:
    """判断一个 dict 是否像一个待办对象。

    strict=True（默认）：必须有 id，且有标题或正文。
        这样 {"status": "ok", "has_more": false} 这类包装对象不会被误判。
    strict=False：放宽到"有标题/正文，且带时间或 id"，用于兜底
        （例如返回的是没有父级 id 的子项数组）。
    """
    if not isinstance(obj, dict):
        return False
    has_id = pick(obj, mapping.get("id", [])) is not None
    has_title = pick(obj, mapping.get("title", [])) is not None
    has_desc = pick(obj, mapping.get("description", [])) is not None
    has_time = (
        pick(obj, mapping.get("due", [])) is not None
        or pick(obj, mapping.get("created", [])) is not None
    )
    has_status = pick(obj, mapping.get("status", [])) is not None
    if strict:
        return has_id and (has_title or has_desc)
    return (has_title or has_desc) and (has_id or has_time or has_status)


def iter_todo_dicts(
    node: Any,
    mapping: Dict[str, List[str]],
    strict: bool = True,
) -> Iterable[Dict[str, Any]]:
    """深度优先遍历任意 JSON 结构，产出所有"像待办"的 dict。"""
    if isinstance(node, dict):
        if looks_like_todo(node, mapping, strict):
            yield node
            return  # 不再往里挖，避免把同一个待办重复产出
        for value in node.values():
            yield from iter_todo_dicts(value, mapping, strict)
    elif isinstance(node, list):
        for item in node:
            yield from iter_todo_dicts(item, mapping, strict)


def _fallback_id(obj: Dict[str, Any]) -> str:
    return "h:" + hashlib.sha256(dump_json(obj).encode("utf-8")).hexdigest()[:16]


def normalize_todo(
    obj: Dict[str, Any],
    mapping: Dict[str, List[str]],
    status_map: Dict[str, str],
    source_id: Optional[str] = None,
) -> Todo:
    """把一个原始 dict 归一化为 Todo。"""
    nested_time = mapping.get("nested_time")
    todo_id = source_id or pick(obj, mapping.get("id", [])) or _fallback_id(obj)

    title = strip_html(str(pick(obj, mapping.get("title", []), "") or ""))
    description_raw = pick(obj, mapping.get("description", []), "") or ""
    if not isinstance(description_raw, str):
        description_raw = json.dumps(description_raw, ensure_ascii=False)
    description = strip_html(description_raw)

    return Todo(
        source_id=str(todo_id),
        title=title,
        description=description,
        status=normalize_status(pick(obj, mapping.get("status", [])), status_map),
        due_ms=to_epoch_ms(pick(obj, mapping.get("due", [])), nested_time),
        created_ms=to_epoch_ms(pick(obj, mapping.get("created", [])), nested_time),
        updated_ms=to_epoch_ms(pick(obj, mapping.get("updated", [])), nested_time),
        owner=owner_text(pick(obj, mapping.get("owner", []))),
        raw=obj,
    )


def flatten_todos(
    raw_items: Iterable[Dict[str, Any]],
    mapping: Dict[str, List[str]],
    status_map: Dict[str, str],
) -> List[Todo]:
    """把可能出现"待办里套子项"的结构展开成扁平的 Todo 列表。

    企业微信的待办对象可能长这样：
        {"todo_id": "t1", "title": "本周工作", "todo_list": [{"title": "A"}, {"title": "B"}]}
    这种情况下 A、B 才是真正要同步的内容项，source_id 记为 "t1#0"、"t1#1"。
    """
    todos: List[Todo] = []
    children_keys = mapping.get("children", [])
    seen = set()

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        parent_id = pick(item, mapping.get("id", []))
        children = pick(item, mapping.get("children", []))
        if isinstance(children, list) and children:
            for index, child in enumerate(children):
                if not isinstance(child, dict):
                    continue
                merged = {k: v for k, v in item.items() if k not in children_keys}
                # 子项优先：子项只要提供了某一类字段（哪怕键名不同，
                # 例如父项用 description、子项用 content），就把父项同类键删掉，
                # 否则 pick() 会按候选键顺序取到父项的值。
                for group in ("title", "description", "status", "due"):
                    group_keys = mapping.get(group, [])
                    if any(key in child for key in group_keys):
                        for key in group_keys:
                            merged.pop(key, None)
                merged.update(child)

                child_key = pick(child, mapping.get("id", []))
                if parent_id is not None and child_key is not None:
                    source_id = f"{parent_id}#{child_key}"
                else:
                    source_id = f"{parent_id or _fallback_id(item)}#{index}"
                todo = normalize_todo(merged, mapping, status_map, source_id=source_id)
                if todo.source_id not in seen:
                    seen.add(todo.source_id)
                    todos.append(todo)
            continue

        todo = normalize_todo(item, mapping, status_map)
        if todo.source_id not in seen:
            seen.add(todo.source_id)
            todos.append(todo)

    return todos


def utc_now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def format_ms(value: Optional[int]) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M")
