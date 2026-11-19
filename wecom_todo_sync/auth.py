"""飞书用户授权（OAuth）——拿到 user_access_token。

为什么需要：应用身份（tenant_access_token）**列不出**任务列表（实测 `GET /tasks` 恒为空），
只能按 guid 读单条。要自动发现"你本人在飞书新建的任务"，必须用用户身份。

流程：
  1. 本地起一个 HTTP 服务监听 redirect_uri（默认 http://localhost:8765/callback）；
  2. 浏览器打开授权页，你点"授权"后飞书带 ?code=... 回调到本地；
  3. 用 code 换 access_token + refresh_token，存到 data/user_token.json；
  4. access_token 过期（约 2 小时）自动用 refresh_token 续。

前置：飞书开放平台 → 安全设置 → 重定向 URL，填 http://localhost:8765/callback
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL_V2 = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
TOKEN_URL_V1 = "https://open.feishu.cn/open-apis/authen/v1/access_token"
APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"

DEFAULT_REDIRECT = "http://localhost:8765/callback"
DEFAULT_SCOPE = "task:task:read"


class OAuthError(RuntimeError):
    pass


# ------------------------------------------------------------------ 存取

def token_path(base_dir: Path) -> Path:
    path = base_dir / "data" / "user_token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_token(base_dir: Path) -> Dict[str, Any]:
    path = token_path(base_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_token(base_dir: Path, payload: Dict[str, Any]) -> Path:
    path = token_path(base_dir)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# ------------------------------------------------------------------ HTTP

def _post_json(url: str, body: Dict[str, Any], bearer: Optional[str] = None) -> Dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        raw = exc.read().decode("utf-8", errors="replace")
        raise OAuthError(f"HTTP {exc.code}：{raw[:300]}") from exc
    except urllib.error.URLError as exc:  # type: ignore[attr-defined]
        raise OAuthError(f"网络错误：{exc.reason}") from exc


def _extract_token(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """兼容两种响应形态：{data:{...}} 或扁平 {...}。"""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    token = data.get("access_token") or data.get("user_access_token")
    if not token:
        return None
    return {
        "access_token": token,
        "refresh_token": data.get("refresh_token") or "",
        "expires_in": int(data.get("expires_in") or 7200),
        "refresh_expires_in": int(data.get("refresh_expires_in") or 0),
        "open_id": data.get("open_id") or "",
        "name": data.get("name") or "",
        "saved_at": int(time.time()),
    }


def _app_access_token(app_id: str, app_secret: str) -> str:
    payload = _post_json(APP_TOKEN_URL, {"app_id": app_id, "app_secret": app_secret})
    token = payload.get("app_access_token")
    if not token:
        raise OAuthError(f"拿 app_access_token 失败：{json.dumps(payload, ensure_ascii=False)[:300]}")
    return str(token)


# ------------------------------------------------------------------ 授权

def build_authorize_url(app_id: str, redirect_uri: str, scope: str, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "response_type": "code",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: Dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {key: values[0] for key, values in params.items() if values}

        body = (
            "<html><meta charset='utf-8'><body style='font-family:sans-serif;padding:40px'>"
            "<h2>授权完成</h2><p>可以关闭这个页面，回到终端继续。</p></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # 静音
        return


def wait_for_code(redirect_uri: str, timeout: int = 300, bind_host: str = "") -> Dict[str, str]:
    parsed = urllib.parse.urlparse(redirect_uri)
    # 绑定的地址和 redirect_uri 里的主机名可以分开：
    # Docker 部署时 redirect_uri 仍是 http://localhost:8765/callback（飞书后台也这么配），
    # 但容器内必须监听 0.0.0.0 才能被宿主机端口映射转发到。
    host = bind_host or parsed.hostname or "localhost"
    port = parsed.port or 80
    _CallbackHandler.result = {}

    server = http.server.HTTPServer((host, port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _CallbackHandler.result:
            break
        time.sleep(0.2)

    server.server_close()
    if not _CallbackHandler.result:
        raise OAuthError(f"等待授权回调超时（{timeout}s）")
    return dict(_CallbackHandler.result)


def exchange_code(
    app_id: str,
    app_secret: str,
    code: str,
    redirect_uri: str,
) -> Dict[str, Any]:
    """用授权码换用户 token：先试 v2（client_id/secret），失败再试 v1（app_access_token）。"""
    errors = []

    try:
        payload = _post_json(
            TOKEN_URL_V2,
            {
                "grant_type": "authorization_code",
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        token = _extract_token(payload)
        if token:
            return token
        errors.append("v2 未返回 access_token：" + json.dumps(payload, ensure_ascii=False)[:200])
    except OAuthError as exc:
        errors.append(f"v2 失败：{exc}")

    try:
        payload = _post_json(
            TOKEN_URL_V1,
            {"grant_type": "authorization_code", "code": code},
            bearer=_app_access_token(app_id, app_secret),
        )
        token = _extract_token(payload)
        if token:
            return token
        errors.append("v1 未返回 access_token：" + json.dumps(payload, ensure_ascii=False)[:200])
    except OAuthError as exc:
        errors.append(f"v1 失败：{exc}")

    raise OAuthError("换 token 失败：\n  " + "\n  ".join(errors))


def refresh(app_id: str, app_secret: str, refresh_token: str) -> Dict[str, Any]:
    payload = _post_json(
        TOKEN_URL_V2,
        {
            "grant_type": "refresh_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "refresh_token": refresh_token,
        },
    )
    token = _extract_token(payload)
    if not token:
        raise OAuthError("刷新 token 失败：" + json.dumps(payload, ensure_ascii=False)[:300])
    return token


def ensure_user_token(cfg: Dict[str, Any], base_dir: Path, verbose: bool = False) -> str:
    """返回可用的 user_access_token；没有就返回空串（不抛异常）。"""
    feishu_cfg = cfg.get("feishu") or {}
    stored = load_token(base_dir)
    if not stored:
        return ""

    saved_at = int(stored.get("saved_at") or 0)
    expires_in = int(stored.get("expires_in") or 7200)
    if time.time() < saved_at + expires_in - 120:
        return str(stored.get("access_token") or "")

    refresh_value = str(stored.get("refresh_token") or "")
    if not refresh_value:
        return ""

    app_id = str(feishu_cfg.get("app_id") or "")
    app_secret = str(feishu_cfg.get("app_secret") or "")
    if not app_id or not app_secret:
        return ""

    try:
        token = refresh(app_id, app_secret, refresh_value)
    except OAuthError:
        return ""
    if not token.get("refresh_token"):
        token["refresh_token"] = refresh_value
    save_token(base_dir, token)
    if verbose:
        print("[auth] user_access_token 已刷新")
    return str(token.get("access_token") or "")


def login(cfg: Dict[str, Any], base_dir: Path, open_browser: bool = True) -> Dict[str, Any]:
    """跑一次授权流程，成功则落盘并返回 token 信息。"""
    feishu_cfg = cfg.get("feishu") or {}
    app_id = str(feishu_cfg.get("app_id") or "")
    app_secret = str(feishu_cfg.get("app_secret") or "")
    if not app_id or not app_secret:
        raise OAuthError("缺少 feishu.app_id / app_secret，无法发起授权")

    redirect_uri = str(feishu_cfg.get("oauth_redirect") or DEFAULT_REDIRECT)
    scope = str(feishu_cfg.get("oauth_scope") or DEFAULT_SCOPE)
    bind_host = str(feishu_cfg.get("oauth_bind_host") or "")
    state = secrets.token_urlsafe(8)

    url = build_authorize_url(app_id, redirect_uri, scope, state)
    print("请在浏览器中完成授权（若未自动打开，复制下面的链接）：")
    print(url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    result = wait_for_code(redirect_uri, bind_host=bind_host)
    if result.get("error"):
        raise OAuthError(f"授权被拒绝：{result.get('error')} {result.get('error_description', '')}")
    code = result.get("code")
    if not code:
        raise OAuthError(f"回调里没有 code：{result}")
    if result.get("state") and result["state"] != state:
        raise OAuthError("state 不匹配，可能存在 CSRF 风险，已中止")

    token = exchange_code(app_id, app_secret, code, redirect_uri)
    save_token(base_dir, token)
    return token
