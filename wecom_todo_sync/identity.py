"""识别当前企业微信扫码账号的 userid（用作待办参与人）。

为什么需要：
    `todo create` 的 `follower_ids` 是参与人；不填时待办未必出现在本人「待办」里。
    而 `wecom-cli auth show` 只输出 authorized/unauthorized，凭证里存的是机器人信息，
    没有扫码用户的 userid，所以直接问 auth 是拿不到身份的。

可行来源：
    `todo list` 的定义是「机器人为创建人、**对话人**为参与人」，
    每条待办返回的 `followers[].userid` 里都包含这个对话人（即扫码用户）。
    对多条待办取 followers 的交集：
      - 恰好 1 个 → 就是扫码用户；
      - 多个 → 存在多人共同参与所有待办，无法唯一确定，宁可不猜；
      - 空   → 列表为空或没有参与人，同样不猜。

    识别结果会缓存到本地库的 meta 表，避免列表数据变化导致参与人来回跳；
    如果以后识别到不同的 userid（比如重新扫码换了账号），会自动更新缓存。
    显式配置 `reverse.default_follower_userid` 的优先级永远最高。
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Set

from .wecom_cli import CliError, WeComCli

FOLLOWER_META_KEY = "wecom_follower_userid"


def list_method(cfg: dict) -> List[str]:
    """读取待办列表用的方法：优先用配置里的 list_methods，否则用 todo list。"""
    methods = (cfg.get("wecom") or {}).get("list_methods") or []
    if methods:
        return [str(part) for part in methods[0]]
    return ["todo", "list"]


def follower_sets(pages: Iterable[Any]) -> List[Set[str]]:
    """从 todo list 的返回里取出每条待办的参与人 userid 集合。"""
    result: List[Set[str]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        items = page.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            followers = item.get("followers")
            if not isinstance(followers, list):
                continue
            ids: Set[str] = set()
            for follower in followers:
                if isinstance(follower, dict):
                    userid = str(follower.get("userid") or "").strip()
                else:
                    userid = str(follower or "").strip()
                if userid:
                    ids.add(userid)
            if ids:
                result.append(ids)
    return result


def wecom_userid_from_pages(pages: Iterable[Any]) -> str:
    """取所有待办参与人的交集；只有唯一一个时才认为它是扫码用户。"""
    sets = follower_sets(pages)
    if not sets:
        return ""
    common = set(sets[0])
    for ids in sets[1:]:
        common &= ids
        if not common:
            return ""
    return next(iter(common)) if len(common) == 1 else ""


def resolve_follower_userid(
    cfg: dict,
    store: Optional[Any] = None,
    cli: Optional[WeComCli] = None,
    verbose: bool = False,
) -> str:
    """确定新待办的参与人。

    顺序：显式配置 > 本地缓存 > 从 todo list 识别扫码账号 > 放弃（返回空串）。
    """
    explicit = str((cfg.get("reverse") or {}).get("default_follower_userid") or "").strip()
    if explicit:
        return explicit

    cached = ""
    if store is not None:
        cached = str(store.get_meta(FOLLOWER_META_KEY) or "").strip()

    probe = cli or WeComCli(cfg)
    try:
        pages = probe.call_method(list_method(cfg), {"limit": 20})
    except CliError as exc:
        if verbose:
            print(f"[auto] 读取待办列表识别扫码账号失败：{exc}")
        return cached

    detected = wecom_userid_from_pages(pages)
    if not detected:
        if verbose:
            print("[auto] 待办列表无法唯一确定扫码账号，沿用已缓存/不指定参与人")
        return cached

    if store is not None:
        store.set_meta(FOLLOWER_META_KEY, detected)
    if verbose and detected != cached:
        print(f"[auto] 识别到扫码账号 userid：{detected}")
    return detected