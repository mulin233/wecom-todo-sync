"""飞书任务（Task v2）客户端：tenant_access_token + 任务创建/更新/查询。

接口依据飞书开放平台「任务概述」（open.feishu.cn/document/task-v2/overview）：
    POST   /open-apis/task/v2/tasks                 创建任务
    GET    /open-apis/task/v2/tasks                 列取任务
    GET    /open-apis/task/v2/tasks/{task_guid}     获取任务
    PATCH  /open-apis/task/v2/tasks/{task_guid}     更新任务（body: {"task": {...}, "update_fields": [...]}）
    POST   /open-apis/task/v2/tasks/search          搜索任务

鉴权：Authorization: Bearer <tenant_access_token | user_access_token>
应用需要开启任务相关权限（如 task:task / task:task:write、task:task:read）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

from .models import Todo

TASKS_PATH = "/open-apis/task/v2/tasks"
TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"

# 飞书侧的长度上限，留一点余量
MAX_SUMMARY_CHARS = 500
MAX_DESCRIPTION_CHARS = 8000


class FeishuError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None, log_id: str = ""):
        super().__init__(message)
        self.code = code
        self.log_id = log_id


class FeishuTaskClient:
    def __init__(self, cfg: Dict[str, Any]):
        feishu_cfg = cfg.get("feishu") or {}
        self.base_url = str(feishu_cfg.get("base_url") or "https://open.feishu.cn").rstrip("/")
        self.app_id = str(feishu_cfg.get("app_id") or "")
        self.app_secret = str(feishu_cfg.get("app_secret") or "")
        self.user_access_token = str(feishu_cfg.get("user_access_token") or "")
        self.user_id_type = str(feishu_cfg.get("user_id_type") or "open_id")
        self.timeout = int(feishu_cfg.get("timeout_seconds", 30))
        self._token: str = ""
        self._token_expire_at: float = 0.0

    # ---------------------------------------------------------------- HTTP

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
    ) -> Dict[str, Any]:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        data = None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if auth:
            headers["Authorization"] = "Bearer " + self.token()

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise FeishuError(_http_error_message(exc.code, raw), code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise FeishuError(f"访问飞书失败：{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise FeishuError("飞书返回的不是合法 JSON") from exc

        code = payload.get("code")
        if code not in (0, None):
            raise FeishuError(
                f"飞书接口报错：code={code} msg={payload.get('msg') or payload.get('message')}",
                code=int(code),
                log_id=str(payload.get("log_id") or ""),
            )
        return payload

    # --------------------------------------------------------------- 鉴权

    def token(self) -> str:
        if self.user_access_token:
            return self.user_access_token
        if self._token and time.time() < self._token_expire_at:
            return self._token
        if not self.app_id or not self.app_secret:
            raise FeishuError(
                "缺少飞书 app_id / app_secret（可写入 config.json 的 feishu 段，"
                "或用环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET）"
            )
        payload = self._request(
            "POST",
            TOKEN_PATH,
            body={"app_id": self.app_id, "app_secret": self.app_secret},
            auth=False,
        )
        token = payload.get("tenant_access_token") or (payload.get("data") or {}).get("tenant_access_token")
        if not token:
            raise FeishuError(f"获取 tenant_access_token 失败：{json.dumps(payload, ensure_ascii=False)}")
        self._token = str(token)
        expire = int(payload.get("expire") or 7200)
        self._token_expire_at = time.time() + max(expire - 120, 60)
        return self._token

    def token_source(self) -> str:
        return "user_access_token" if self.user_access_token else "tenant_access_token"

    # --------------------------------------------------------------- 任务

    def create_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._request(
            "POST",
            TASKS_PATH,
            params={"user_id_type": self.user_id_type},
            body=task,
        )
        return ((payload.get("data") or {}).get("task")) or {}

    def update_task(
        self,
        task_guid: str,
        task: Dict[str, Any],
        update_fields: List[str],
    ) -> Dict[str, Any]:
        payload = self._request(
            "PATCH",
            f"{TASKS_PATH}/{urllib.parse.quote(task_guid)}",
            params={"user_id_type": self.user_id_type},
            body={"task": task, "update_fields": update_fields},
        )
        return ((payload.get("data") or {}).get("task")) or {}

    def get_task(self, task_guid: str) -> Dict[str, Any]:
        payload = self._request(
            "GET",
            f"{TASKS_PATH}/{urllib.parse.quote(task_guid)}",
            params={"user_id_type": self.user_id_type},
        )
        return ((payload.get("data") or {}).get("task")) or {}

    def list_tasks(self, page_size: int = 20, page_token: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page_size": page_size, "page_token": page_token, "user_id_type": self.user_id_type}
        params.update(extra)
        payload = self._request("GET", TASKS_PATH, params=params)
        return payload.get("data") or {}

    def search_tasks(self, query: str = "", **extra: Any) -> Dict[str, Any]:
        body: Dict[str, Any] = {"query": query}
        body.update(extra)
        payload = self._request(
            "POST",
            TASKS_PATH + "/search",
            params={"user_id_type": self.user_id_type},
            body=body,
        )
        return payload.get("data") or {}

    def iter_tasks(
        self,
        page_size: int = 50,
        max_pages: int = 20,
        **extra: Any,
    ) -> Iterable[Dict[str, Any]]:
        """翻页遍历任务列表（反向同步用）。"""
        token: Optional[str] = None
        for _ in range(max_pages):
            data = self.list_tasks(page_size=page_size, page_token=token, **extra)
            for item in data.get("items") or []:
                if isinstance(item, dict):
                    yield item
            token = data.get("page_token") or data.get("next_page_token")
            if not data.get("has_more") or not token:
                return

    # ------------------------------------------------------------ 成员查询

    def resolve_open_ids(self, mobiles: List[str] = None, emails: List[str] = None) -> Dict[str, str]:
        """手机号/邮箱 -> open_id，用于填 default_assignee_open_id。

        依赖权限：contact:user.id:readonly（获取用户 user ID）。
        """
        body: Dict[str, Any] = {}
        if mobiles:
            body["mobiles"] = list(mobiles)
        if emails:
            body["emails"] = list(emails)
        if not body:
            return {}

        payload = self._request(
            "POST",
            "/open-apis/contact/v3/users/batch_get_id",
            params={"user_id_type": "open_id"},
            body=body,
        )
        data = payload.get("data") or {}
        found: Dict[str, str] = {}
        for item in data.get("user_list") or []:
            if not isinstance(item, dict):
                continue
            key = item.get("mobile") or item.get("email")
            user_id = item.get("user_id") or item.get("open_id")
            if key and user_id:
                found[str(key)] = str(user_id)
        return found


# ---------------------------------------------------------------- 任务构造

def build_task_payload(
    todo: Todo,
    cfg: Dict[str, Any],
    now_ms: int,
) -> tuple[Dict[str, Any], List[str]]:
    """把 Todo 转成飞书任务请求体。

    返回 (task, update_fields)。create 时只用 task；update 时需要 update_fields。
    """
    feishu_cfg = cfg.get("feishu") or {}
    summary = (todo.title or todo.display_title).strip() or f"企业微信待办 {todo.source_id}"
    summary = summary[:MAX_SUMMARY_CHARS]

    raw = todo.raw if isinstance(todo.raw, dict) else {}
    followers = raw.get("followers")
    participant_names: List[str] = []
    if isinstance(followers, list):
        for person in followers:
            if not isinstance(person, dict):
                continue
            name = person.get("user_name") or person.get("userid")
            if name and str(name) not in participant_names:
                participant_names.append(str(name))

    description = (todo.description or "").strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rstrip()

    footer = ""
    if feishu_cfg.get("include_source_id", True):
        footer_lines = [f"来源：企业微信待办 {todo.source_id}"]
        if participant_names:
            footer_lines.append("参与人：" + "、".join(participant_names))
        footer = "\n".join(footer_lines)
    if footer:
        description = f"{description}\n\n---\n{footer}" if description else footer

    task: Dict[str, Any] = {"summary": summary}
    update_fields: List[str] = ["summary"]
    if description:
        task["description"] = description
        update_fields.append("description")

    # deadline.type == "date" 表示只有日期，飞书侧标成全天更准确
    deadline = raw.get("deadline")
    all_day = isinstance(deadline, dict) and str(deadline.get("type") or "").lower() == "date"
    if todo.due_ms:
        task["due"] = {"timestamp": str(todo.due_ms), "is_all_day": all_day}
        update_fields.append("due")

    if todo.status == "completed":
        task["completed_at"] = str(now_ms)
        update_fields.append("completed_at")
    elif feishu_cfg.get("allow_reopen"):
        task["completed_at"] = "0"
        update_fields.append("completed_at")

    assignee = str(feishu_cfg.get("default_assignee_open_id") or "")
    if assignee:
        task["members"] = [{"id": assignee, "type": "user", "role": "assignee"}]
        update_fields.append("members")

    tasklist_guid = str(feishu_cfg.get("tasklist_guid") or "")
    if tasklist_guid:
        task["tasklists"] = [{"guid": tasklist_guid}]
        update_fields.append("tasklists")

    return task, update_fields


def _http_error_message(status: int, raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return f"飞书 HTTP {status}：{raw[:300]}"
    return (
        f"飞书 HTTP {status}：code={payload.get('code')} "
        f"msg={payload.get('msg') or payload.get('message')} log_id={payload.get('log_id')}"
    )
