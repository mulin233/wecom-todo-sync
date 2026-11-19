"""命令行入口：doctor / collect / sync / export / selftest。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import sys
import time
from pathlib import Path

from . import __version__

from .auth import OAuthError, ensure_user_token, load_token, login, token_path
from .collector import collect
from .config import load_config, resolve_path
from .export import export_all
from .feishu import FeishuError, FeishuTaskClient
from .identity import resolve_follower_userid
from .meetings import sync_meetings
from .reverse import _parse_create_response, build_create_payload, is_completed, payload_hash, pull
from .selftest import run_selftest
from .store import Store
from .sync import run_collect, sync_todos
from .wecom_cli import CliError, WeComCli


def _print(msg: str = "") -> None:
    print(msg)


def _open_store(cfg, base_dir: Path, db_override: str | None) -> Store:
    db_value = db_override or (cfg.get("store") or {}).get("db_path") or "data/wecom_todo.db"
    return Store(resolve_path(base_dir, db_value))


# ------------------------------------------------------------------ doctor

def cmd_doctor(args) -> int:
    cfg, base_dir = load_config(args.config)
    failures = 0

    _print("== 运行环境 ==")
    _print(f"  工具版本   : wecom-todo-sync {__version__}")
    _print(f"  Python     : {sys.version.split()[0]} ({platform.system()} {platform.release()})")
    _print(f"  工作目录   : {Path.cwd()}")
    _print(f"  配置文件   : {args.config or base_dir / 'config.json'}")

    db_path = resolve_path(base_dir, args.db or (cfg.get("store") or {}).get("db_path", "data/wecom_todo.db"))
    _print(f"  数据库     : {db_path}")

    _print()
    _print("== 本地库 ==")
    try:
        store = _open_store(cfg, base_dir, args.db)
        counts = store.counts()
        _print(f"  [ok]   待办 {counts['total']} 条（未完成 {counts['open']}），已同步飞书 {counts['synced']} 条")
        for row in store.recent_runs(3):
            _print(
                "  最近一次运行: {cmd} 采集 {collected} / 新建 {created} / 更新 {updated} / 失败 {failed}".format(
                    cmd=row["command"],
                    collected=row["collected"],
                    created=row["created"],
                    updated=row["updated"],
                    failed=row["failed"],
                )
            )
        errored = store.failed_syncs(5)
        for row in errored:
            _print(f"  [warn] 同步失败 {row['source_id']}：{row['last_error']}")
        store.close()
    except Exception as exc:  # noqa: BLE001 - doctor 要把问题都显示出来
        failures += 1
        _print(f"  [FAIL] 打开数据库失败：{exc}")

    _print()
    _print("== 企业微信（官方 @wecom/cli）==")
    cli = WeComCli(cfg)
    _print(f"  调用命令   : {' '.join(cli.prefix)}")
    try:
        _print(f"  [ok]   版本：{cli.version()}")
    except CliError as exc:
        failures += 1
        _print(f"  [FAIL] {exc}")
        _print("         安装：npm install -g @wecom/cli")

    status = cli.auth_status()
    if status == "authorized":
        _print("  [ok]   授权状态：authorized")
    elif status == "unauthorized":
        failures += 1
        _print("  [FAIL] 授权状态：unauthorized（请执行 wecom-cli auth init）")
    else:
        _print("  [warn] 无法确认授权状态，可手动执行：wecom-cli auth show --status")

    _print("  待办方法候选（自动采集只会调用其中的只读方法）：")
    try:
        methods = cli.candidate_methods(read_only=False)
    except CliError as exc:
        methods = []
        _print(f"    [warn] 方法发现失败：{exc}")
    if methods:
        for method in methods[:12]:
            tag = "只读" if cli.is_read_method(method) else "写"
            _print(f"    - {' '.join(method)}  [{tag}]")
        _print("    如需固定，可写入 config.json：{\"wecom\": {\"list_methods\": [[\"todo\", \"list\"]]}}")
    else:
        _print("    未发现候选方法；请手动执行 `wecom-cli todo --help` 确认方法名后写入 list_methods")

    _print()
    _print("== 飞书任务（Task v2）==")
    _print(f"  域名       : {cfg['feishu']['base_url']}")
    client = FeishuTaskClient(cfg)
    _print(f"  鉴权方式   : {client.token_source()}")
    try:
        client.token()
        _print("  [ok]   获取 access token 成功")
    except FeishuError as exc:
        failures += 1
        _print(f"  [FAIL] 鉴权失败：{exc}")
    try:
        data = client.list_tasks(page_size=5)
        total = data.get("total")
        items = data.get("items") or []
        _print(f"  [ok]   可以读取任务列表（total={total}，本页 {len(items)} 条）")
        for item in items[:3]:
            if isinstance(item, dict):
                _print(
                    "    - {guid}  {summary}  completed={done}".format(
                        guid=str(item.get("guid") or "")[:10],
                        summary=str(item.get("summary") or "")[:36],
                        done=bool(item.get("completed_at")),
                    )
                )
        if total == 0:
            _print("    [warn] 应用可见的待办为 0。tenant_access_token 只能看到应用自己创建/参与的任务；")
            _print("           如果前向同步建过任务却仍是 0，说明该接口对应用身份的可见范围有限，需要换查询方式。")
        elif not items:
            _print("    [warn] total>0 但本页为空，可能是分页参数问题")
    except FeishuError as exc:
        failures += 1
        _print(f"  [FAIL] 读取任务失败：{exc}")
        _print("         请确认应用已开通任务权限（如 task:task:read / task:task:write）")

    _print()
    _print("== 反向同步（飞书任务 -> 企业微信待办）==")
    follower = str((cfg.get("reverse") or {}).get("default_follower_userid") or "")
    if follower:
        _print(f"  [ok]   待办参与人 userid：{follower}")
    else:
        _print("  [ok]   未指定参与人：pull 时自动识别扫码账号作为参与人")
        _print("         识别不出（多人共同参与）时可手动填 reverse.default_follower_userid")
    stored_token = load_token(base_dir)
    if stored_token:
        _print(f"  [ok]   用户授权：{stored_token.get('name') or '(未知)'}（pull 能看到你本人的任务）")
    else:
        _print("  [warn] 未做用户授权：pull 只能看到应用自己创建的任务（执行 auth 授权）")

    _print()
    if failures:
        _print(f"体检完成：发现 {failures} 个问题（见上面的 [FAIL]）")
        return 1
    _print("体检通过 ✅")
    return 0


# ----------------------------------------------------------------- collect

def cmd_collect(args) -> int:
    cfg, base_dir = load_config(args.config)
    store = _open_store(cfg, base_dir, args.db)
    try:
        result, stats = run_collect(
            cfg,
            base_dir,
            store,
            source=args.source,
            source_file=args.source_file,
            verbose=args.verbose,
            dump_raw=not getattr(args, "no_raw_dump", False),
        )
        _print()
        _print("== 采集结果 ==")
        _print(f"  使用方法   : {' '.join(result.method) if result.method else '(无)'}")
        _print(f"  页数 / 对象: {result.pages} 页 / {result.raw_items} 个原始对象")
        if result.details:
            _print(f"  补详情     : {result.details} 条（截止时间/参与人）")
        _print(f"  入库       : 共 {stats['collected']}（新建 {stats['created']}，更新 {stats['updated']}，未变 {stats['skipped']}）")
        if result.raw_dump_path:
            _print(f"  原始归档   : {result.raw_dump_path}")
        for warning in result.warnings:
            _print(f"  [warn] {warning}")

        if args.export:
            written = export_all(cfg, base_dir, store)
            for path in written:
                _print(f"  导出       : {path}")
        return 0
    finally:
        store.close()


# -------------------------------------------------------------------- sync

def cmd_sync(args) -> int:
    cfg, base_dir = load_config(args.config)
    store = _open_store(cfg, base_dir, args.db)
    try:
        source_ids = None
        if not args.all and not args.no_collect:
            result = collect(
                cfg,
                base_dir,
                source=args.source,
                source_file=args.source_file,
                verbose=args.verbose,
                known_ids=store.known_todo_ids(),
            )
            for warning in result.warnings:
                _print(f"[warn] {warning}")
            for todo in result.todos:
                store.upsert_todo(todo)
            source_ids = [todo.source_id for todo in result.todos]
            _print(f"本次采集到 {len(result.todos)} 条待办（方法：{' '.join(result.method) if result.method else '无'}）")

        _print()
        _print("== 同步到飞书任务 ==")
        stats = sync_todos(
            cfg,
            base_dir,
            store,
            source_ids=source_ids,
            sync_all=args.all,
            dry_run=args.dry_run,
            limit=args.limit,
            verbose=args.verbose,
        )
        _print()
        _print(
            "  新建 {created} / 更新 {updated}（其中完成 {completed}）/ 跳过 {skipped} / 失败 {failed}".format(**stats)
        )
        if args.export:
            for path in export_all(cfg, base_dir, store):
                _print(f"  导出       : {path}")
        return 1 if stats["failed"] else 0
    finally:
        store.close()


# ------------------------------------------------------------------ export

def cmd_export(args) -> int:
    cfg, base_dir = load_config(args.config)
    store = _open_store(cfg, base_dir, args.db)
    try:
        formats = args.format or None
        written = export_all(cfg, base_dir, store, formats=formats, out_dir=args.out_dir)
        if not written:
            _print("没有导出任何文件，请检查 --format 参数")
            return 1
        counts = store.counts()
        _print(f"已导出 {counts['total']} 条待办：")
        for path in written:
            _print(f"  {path}")
        return 0
    finally:
        store.close()


# ------------------------------------------------------------------ whoami

def cmd_whoami(args) -> int:
    """把手机号/邮箱换成 open_id（填 feishu.default_assignee_open_id 用）。"""
    cfg, base_dir = load_config(args.config)
    mobiles = args.mobile or []
    emails = args.email or []
    if not mobiles and not emails:
        _print("请用 --mobile 13800138000 或 --email you@example.com 指定成员")
        return 2

    client = FeishuTaskClient(cfg)
    found = client.resolve_open_ids(mobiles=mobiles, emails=emails)
    if not found:
        _print("没有查到成员。请确认：")
        _print("  1) 手机号/邮箱确实在企业通讯录里；")
        _print("  2) 应用已开通「获取用户 user ID」权限（contact:user.id:readonly）并审批通过。")
        return 1

    for key, open_id in found.items():
        _print(f"{key}  ->  open_id: {open_id}")
    _print()
    _print('填进 config.json：{"feishu": {"default_assignee_open_id": "%s"}}' % next(iter(found.values())))
    _print("或设环境变量：$env:FEISHU_ASSIGNEE_OPEN_ID=\"<open_id>\"")
    return 0


# -------------------------------------------------------------------- auth

def cmd_auth(args) -> int:
    """飞书用户授权：拿 user_access_token（反向同步必需）。"""
    cfg, base_dir = load_config(args.config)

    if args.status:
        stored = load_token(base_dir)
        if not stored:
            _print("尚未授权。执行：python -m wecom_todo_sync auth")
            return 1
        saved_at = int(stored.get("saved_at") or 0)
        expires_in = int(stored.get("expires_in") or 7200)
        remain = int(saved_at + expires_in - time.time())
        _print(f"用户：{stored.get('name') or '(未返回)'}  open_id={stored.get('open_id') or '-'}")
        _print(f"token 剩余有效：{max(remain, 0)} 秒；refresh_token：{'有' if stored.get('refresh_token') else '无'}")
        _print(f"文件：{token_path(base_dir)}")
        return 0

    token = login(cfg, base_dir, open_browser=not args.no_browser)
    _print()
    _print("[ok] 授权成功")
    if token.get("name"):
        _print(f"  用户   ：{token['name']}  {token.get('open_id') or ''}")
    _print(f"  有效期 ：{token.get('expires_in')}s（过期会自动用 refresh_token 续）")
    _print(f"  已保存 ：{token_path(base_dir)}")
    _print()
    _print("接下来可以跑：python -m wecom_todo_sync pull")
    return 0


# -------------------------------------------------------------------- pull

def cmd_pull(args) -> int:
    """反向同步：把飞书新建的任务推到企业微信待办。"""
    cfg, base_dir = load_config(args.config)
    store = _open_store(cfg, base_dir, args.db)
    try:
        token = ensure_user_token(cfg, base_dir, verbose=args.verbose)
        if token:
            cfg["feishu"]["user_access_token"] = token
            _print("[auth] 使用 user_access_token（能看到你本人创建的任务）")
        else:
            _print("[warn] 没有可用的 user_access_token：应用身份列不出任务，pull 大概率扫到 0 条。")
            _print("       先执行：python -m wecom_todo_sync auth")

        _print("== 飞书任务 -> 企业微信待办 ==")
        stats = pull(cfg, store, apply=args.apply, limit=args.limit, verbose=args.verbose)
        _print()
        if args.apply:
            _print(
                "  新建 {created} / 更新 {updated} / 完成 {finished} / 跳过 {skipped} / 失败 {failed}"
                "（扫描 {scanned} 条飞书任务）".format(**stats)
            )
        else:
            _print(
                "  计划动作 {planned} 个 / 跳过 {skipped}（扫描 {scanned} 条飞书任务）".format(**stats)
            )
            _print("  上面只是预演；确认无误后加 --apply 才会真的写进企业微信。")
        return 1 if stats["failed"] else 0
    finally:
        store.close()


# ------------------------------------------------------------------- probe

def cmd_probe(args) -> int:
    """调试：把飞书任务相关接口的原始响应打出来，用于定位"扫描到 0 条"。"""
    cfg, base_dir = load_config(args.config)
    client = FeishuTaskClient(cfg)

    variants = [
        ("GET /tasks 默认", lambda: client.list_tasks(page_size=5)),
        ("GET /tasks completed=false", lambda: client.list_tasks(page_size=5, completed="false")),
        ("GET /tasks completed=true", lambda: client.list_tasks(page_size=5, completed="true")),
    ]
    for label, call in variants:
        try:
            data = call()
        except FeishuError as exc:
            _print(f"[{label}] 失败：{exc}")
            continue
        items = data.get("items") or []
        _print(f"[{label}] total={data.get('total')} items={len(items)}")
        _print("    " + json.dumps(data, ensure_ascii=False)[:500])

    # 列表既然列不出来，试试搜索接口能不能绕过去
    searches = [
        ("POST /tasks/search query=''", ""),
        ("POST /tasks/search query='同步'", "同步"),
    ]
    for label, query in searches:
        try:
            data = client.search_tasks(query)
        except FeishuError as exc:
            _print(f"[{label}] 失败：{exc}")
            continue
        items = data.get("items") or []
        _print(f"[{label}] items={len(items)}")
        _print("    " + json.dumps(data, ensure_ascii=False)[:500])

    # 前向同步建过的任务，直接按 guid 读一次：能读到说明"写"没问题，只是"列"看不到
    store = _open_store(cfg, base_dir, args.db)
    try:
        rows = store.conn.execute(
            "SELECT feishu_task_guid FROM sync_map WHERE feishu_task_guid IS NOT NULL LIMIT 3"
        ).fetchall()
    finally:
        store.close()

    for row in rows:
        guid = row["feishu_task_guid"]
        try:
            task = client.get_task(guid)
        except FeishuError as exc:
            _print(f"[GET /tasks/{guid[:8]}] 失败：{exc}")
            continue
        _print(
            "[GET /tasks/{guid}] ok summary={summary!r} creator={creator} completed={done}".format(
                guid=guid[:8],
                summary=str(task.get("summary") or "")[:30],
                creator=task.get("creator"),
                done=bool(task.get("completed_at")),
            )
        )
    return 0


# ------------------------------------------------------------------ import

def _guid_from_url(url: str) -> str:
    """从飞书任务链接里抠出 guid（取最后一段长标识）。"""
    candidates = re.findall(r"[0-9a-zA-Z_-]{16,}", url or "")
    return candidates[-1] if candidates else ""


def cmd_import(args) -> int:
    """把指定的飞书任务导入成企业微信待办（按 guid 或任务链接）。"""
    cfg, base_dir = load_config(args.config)
    guid = (args.guid or "").strip() or _guid_from_url(args.url or "")
    if not guid:
        _print("请用 --guid <任务guid> 或 --url <任务链接> 指定任务")
        return 2

    client = FeishuTaskClient(cfg)
    task = client.get_task(guid)
    if not task:
        _print(f"没读到任务：{guid}")
        return 1
    if is_completed(task):
        _print("这条任务已完成，按规则不导入（先在飞书里取消完成再试）")
        return 1

    store = _open_store(cfg, base_dir, args.db)
    try:
        cli = WeComCli(cfg)
        # 没显式配置参与人时，自动识别扫码账号
        follower = resolve_follower_userid(cfg, store, cli, verbose=args.verbose)
        if follower:
            cfg.setdefault("reverse", {})["default_follower_userid"] = follower
            _print(f"[auto] 待办参与人使用扫码账号：{follower}")

        payload = build_create_payload(task, cfg)
        _print(f"任务：{task.get('summary')!r}  guid={guid}")
        _print("将发送：" + json.dumps(payload, ensure_ascii=False))

        if args.dry_run:
            _print("（--dry-run，未写入企业微信）")
            return 0

        if store.get_mapping_by_guid(guid) is not None:
            _print("这条任务是从企业微信同步过去的（前向），跳过以避免重复")
            return 0
        existing = store.get_reverse_mapping(guid)
        if existing is not None and existing["wecom_todo_id"] and not args.force:
            _print(f"已经导入过：{existing['wecom_todo_id']}（要重导加 --force）")
            return 0

        response = cli.create_items(payload)
        todo_ids, errors = _parse_create_response(response)
        if errors:
            _print("[错误] 创建失败：" + "; ".join(errors))
            return 1
        store.set_reverse_mapping(guid, todo_ids[0] if todo_ids else None, payload_hash(payload))
        _print(f"[ok] 已建企业微信待办：{todo_ids[0] if todo_ids else '(响应里没找到 todo_id)'}")
        return 0
    finally:
        store.close()


# ----------------------------------------------------------------- meetings

def cmd_meetings(args) -> int:
    """会议 -> 待办：把"我创建的会议"建成待办。"""
    cfg, base_dir = load_config(args.config)
    if not (cfg.get("meetings") or {}).get("enabled", True):
        _print("meetings.enabled = false，已跳过")
        return 0

    store = _open_store(cfg, base_dir, args.db)
    try:
        _print("== 会议 -> 待办 ==")
        stats = sync_meetings(
            cfg,
            store,
            apply=args.apply,
            limit=args.limit,
            force=args.force,
            verbose=args.verbose,
        )
        _print()
        if args.apply:
            _print(
                "  新建待办 {created} / 跳过 {skipped} / 失败 {failed}（扫描 {scanned} 场会议）".format(
                    **stats
                )
            )
        else:
            _print(
                "  计划新建 {planned} / 跳过 {skipped}（扫描 {scanned} 场会议）".format(**stats)
            )
            _print("  上面只是预演；确认无误后加 --apply 才会真的写进企业微信。")
        return 1 if stats["failed"] else 0
    finally:
        store.close()


# -------------------------------------------------------------------- init

def cmd_init(args) -> int:
    """交互式生成 config.json：每个人填自己的凭证，不需要手改 JSON。"""
    cfg, base_dir = load_config(args.config)
    config_file = resolve_path(base_dir, args.config or "config.json")

    def ask(label: str, current: str, required: bool = False) -> str:
        hint = f" [当前：{current}]" if current else ""
        while True:
            try:
                value = input(f"{label}{hint}：").strip()
            except EOFError:
                print("\n（没有交互终端，改为手动编辑 config.json）")
                return current
            if not value and current:
                return current
            if value:
                return value
            if not required:
                return ""
            print("  这一项不能为空，请重新输入。")

    print("== 初始化配置（直接回车 = 保留现有值 / 跳过）==")
    print()

    app_id = ask("飞书 App ID（cli_ 开头）", str(cfg["feishu"].get("app_id") or ""), required=True)

    secret = ""
    try:
        secret = getpass.getpass("飞书 App Secret（输入不回显，回车跳过）：").strip()
    except EOFError:
        secret = ""

    assignee = ask(
        "飞书指派人 open_id（ou_ 开头，可留空，稍后用 whoami 查）",
        str(cfg["feishu"].get("default_assignee_open_id") or ""),
    )
    follower = ask(
        "企业微信 userid（可选，留空 = 扫码账号；要挂到其他成员时填）",
        str(cfg["reverse"].get("default_follower_userid") or ""),
    )

    cfg["feishu"]["app_id"] = app_id
    if secret:
        cfg["feishu"]["app_secret"] = secret
    cfg["feishu"]["default_assignee_open_id"] = assignee
    cfg["reverse"]["default_follower_userid"] = follower

    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(config_file, 0o600)
    except OSError:
        pass

    print()
    print(f"[ok] 已写入 {config_file}")
    if not cfg["feishu"].get("app_secret"):
        print("     [warn] app_secret 还是空的，飞书接口会报 invalid param")
    print()
    print("下一步：")
    print("  python -m wecom_todo_sync doctor     # 体检")
    print("  python -m wecom_todo_sync auth       # 可选：飞书用户授权（反向同步需要）")
    return 0


# -------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wecom_todo_sync",
        description="把企业微信待办（通过官方 @wecom/cli）收集到本地，并同步到飞书任务（Task v2）。",
    )
    parser.add_argument("--config", help="配置文件路径（默认 ./config.json）")
    parser.add_argument("--db", help="覆盖数据库路径")
    parser.add_argument("--verbose", action="store_true", help="打印更多过程信息")
    parser.add_argument("--version", action="version", version=f"wecom-todo-sync {__version__}")

    # 全局参数同时挂到子命令上（写在子命令后面也能用）。
    # 用 SUPPRESS 作默认值，避免子解析器的默认值覆盖掉主解析器已经解析出来的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", parents=[common], help="体检：检查 CLI、授权、飞书权限、数据库")
    doctor.set_defaults(func=cmd_doctor)

    init_cmd = subparsers.add_parser("init", parents=[common], help="交互式填写配置（app_id/secret/open_id/userid）")
    init_cmd.set_defaults(func=cmd_init)

    collect_cmd = subparsers.add_parser("collect", parents=[common], help="只采集到本地库，不推送飞书")
    collect_cmd.add_argument("--source", choices=["cli", "file"], default="cli")
    collect_cmd.add_argument("--source-file", help="source=file 时的 JSON 文件路径")
    collect_cmd.add_argument("--no-raw-dump", action="store_true", help="不归档本次采集的原始 JSON")
    collect_cmd.add_argument("--export", action="store_true", help="采集后顺便导出 Markdown/CSV/JSON")
    collect_cmd.set_defaults(func=cmd_collect)

    sync_cmd = subparsers.add_parser("sync", parents=[common], help="采集并同步到飞书任务")
    sync_cmd.add_argument("--source", choices=["cli", "file"], default="cli")
    sync_cmd.add_argument("--source-file", help="source=file 时的 JSON 文件路径")
    sync_cmd.add_argument("--all", action="store_true", help="同步库里全部待办（默认只同步本次采集到的）")
    sync_cmd.add_argument("--no-collect", action="store_true", help="不采集，直接同步库里已有的数据")
    sync_cmd.add_argument("--dry-run", action="store_true", help="只打印将执行的动作，不调用飞书接口")
    sync_cmd.add_argument("--limit", type=int, help="最多处理多少条")
    sync_cmd.add_argument("--export", action="store_true", help="同步后导出报表")
    sync_cmd.set_defaults(func=cmd_sync)

    export_cmd = subparsers.add_parser("export", parents=[common], help="把本地库导出为 Markdown/CSV/JSON")
    export_cmd.add_argument("--format", action="append", choices=["markdown", "md", "csv", "json"])
    export_cmd.add_argument("--out-dir", help="导出目录")
    export_cmd.set_defaults(func=cmd_export)

    selftest_cmd = subparsers.add_parser("selftest", parents=[common], help="离线自检（不联网）")
    selftest_cmd.set_defaults(func=lambda args: run_selftest())

    whoami_cmd = subparsers.add_parser(
        "whoami", parents=[common], help="手机号/邮箱 -> open_id（用于指派飞书任务）"
    )
    whoami_cmd.add_argument("--mobile", action="append", help="手机号，可重复")
    whoami_cmd.add_argument("--email", action="append", help="邮箱，可重复")
    whoami_cmd.set_defaults(func=cmd_whoami)

    pull_cmd = subparsers.add_parser(
        "pull", parents=[common], help="反向同步：飞书任务 -> 企业微信待办（默认只预演）"
    )
    pull_cmd.add_argument("--apply", action="store_true", help="真的写入企业微信（默认只打印计划）")
    pull_cmd.add_argument("--limit", type=int, help="最多处理多少条")
    pull_cmd.set_defaults(func=cmd_pull)

    auth_cmd = subparsers.add_parser(
        "auth", parents=[common], help="飞书用户授权（反向同步要读你本人的任务）"
    )
    auth_cmd.add_argument("--status", action="store_true", help="只看当前授权状态")
    auth_cmd.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    auth_cmd.set_defaults(func=cmd_auth)

    meet_cmd = subparsers.add_parser(
        "meetings", parents=[common], help="会议 -> 待办：把会议同步成待办（默认只预演）"
    )
    meet_cmd.add_argument("--apply", action="store_true", help="真的写入企业微信待办")
    meet_cmd.add_argument("--limit", type=int, help="最多处理多少场会议")
    meet_cmd.add_argument("--force", action="store_true", help="已同步过的会议也重新创建")
    meet_cmd.set_defaults(func=cmd_meetings)

    probe_cmd = subparsers.add_parser(
        "probe", parents=[common], help="调试：打印飞书任务接口的原始响应"
    )
    probe_cmd.set_defaults(func=cmd_probe)

    import_cmd = subparsers.add_parser(
        "import", parents=[common], help="把指定飞书任务导入为企业微信待办"
    )
    import_cmd.add_argument("--guid", help="飞书任务 guid")
    import_cmd.add_argument("--url", help="飞书任务链接（会从里面解析 guid）")
    import_cmd.add_argument("--dry-run", action="store_true", help="只打印请求体，不写入")
    import_cmd.add_argument("--force", action="store_true", help="已导入过也重新创建")
    import_cmd.set_defaults(func=cmd_import)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _print("\n已中断")
        return 130
    except (CliError, FeishuError, OAuthError, FileNotFoundError, ValueError) as exc:
        _print(f"[错误] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
