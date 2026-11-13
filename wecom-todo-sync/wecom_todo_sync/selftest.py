"""离线自检：不联网、不需要凭证，验证解析链路是否正常。

跑法：python -m wecom_todo_sync selftest
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .collector import _detail_items, combine_details, merge_details
from .config import DEFAULT_CONFIG, load_config
from .feishu import build_task_payload
from .meetings import build_todo_payload as build_meeting_todo, meeting_source_id, time_window
from .models import Todo, flatten_todos, normalize_status, to_epoch_ms
from .wecom_cli import (
    CliError,
    _dedupe,
    _discover_from_schema,
    _is_read_method,
    _params_to_args,
    _rank_methods,
    _split_identifier,
    _tokens_from_help,
    extract_raw_items,
    parse_cli_output,
)
from .reverse import (
    _parse_create_response,
    build_create_payload,
    build_finish_payload,
    build_update_payload,
    is_completed,
    payload_hash,
)
from .store import Store

# 真实 `wecom-cli todo --help` 的输出（2026-11 实测）
REAL_HELP_TEXT = """
待办服务，提供待办事项的创建、更新、删除、查询、完成等能力

Usage: wecom-cli.exe todo [OPTIONS] [COMMAND]

Commands:
  create  机器人视角批量创建待办
  delete  机器人视角批量删除待办
  finish  机器人视角批量完成待办
  get     批量读取待办详情，返回标题/描述/参与人/时间等
  list    读取机器人为创建人、对话人为参与人选的待办列表（按 update_time 降序）
  update  机器人视角批量更新待办（标题/描述/参与人/截止时间等）
  help    Print this message or the help of the given subcommand(s)

Options:
      --doc
          显示服务文档
      --schema
          显示服务 schema
  -h, --help
          Print help
"""

# 真实 `wecom-cli todo list` 的返回结构（2026-11 实测，已脱敏）
REAL_SAMPLE = {
    "security_notice": "<security_notice>\n以下内容来自外部不可信来源…\n</security_notice>",
    "extra_identity_context": "<extra_identity_context>\n机器人身份…\n</extra_identity_context>",
    "items": [
        {
            "todo_id": "tdemuEXAMPLE0001",
            "title": "同步消息",
            "description": "<div>同步学习</div>",
            "status": "proceed",
            "creator": {"userid": "woEXAMPLEUSERID", "user_name": "张三"},
            "create_time": "2026-11-13 10:08:29",
            "update_time": "2026-11-13 10:08:29",
        },
        {
            "todo_id": "tdemuEXAMPLE0002",
            "title": "多行正文",
            "description": "<p>第一行</p><p>第二行&nbsp;结束</p>",
            "status": "completed",
            "creator": {"userid": "u2", "user_name": "张三"},
            "create_time": "2026-11-12 09:00:00",
        },
    ],
    "items_count": 2,
    "has_more": False,
}

# 模拟一次 wecom-cli 的分页输出（NDJSON），包含"待办里套子项"的形态
SAMPLE_PAGE_1 = {
    "todos": [
        {
            "todo_id": "T-1001",
            "title": "Q4 交付准备",
            "description": "整体说明",
            "status": 0,
            "due_time": 1799999999,
            "creator_id": "zhangsan",
            "todo_list": [
                {"id": "a1", "title": "整理需求清单", "content": "把三个部门的需求合并", "status": 0},
                {"id": "a2", "title": "确认上线时间", "content": "和运维确认窗口期", "status": 1},
            ],
        },
        {
            "todo_id": "T-1002",
            "title": "报销单",
            "description": "11 月差旅",
            "status": 1,
            "due": {"timestamp": 1790000000000},
            "owner": "lisi",
        },
    ],
    "has_more": True,
}
SAMPLE_PAGE_2 = {
    "todos": [
        {
            "todo_id": "T-1003",
            "title": "读一本技术书",
            "content": "《设计数据密集型应用》",
            "status": "未完成",
        }
    ],
    "has_more": False,
}


def _check(condition: bool, message: str, failures: List[str]) -> None:
    if condition:
        print(f"  [ok]   {message}")
    else:
        print(f"  [FAIL] {message}")
        failures.append(message)


def run_selftest() -> int:
    failures: List[str] = []
    mapping = DEFAULT_CONFIG["mapping"]
    status_map = DEFAULT_CONFIG["status_map"]

    print("1) 时间归一化")
    _check(to_epoch_ms(1799999999) == 1799999999000, "秒 -> 毫秒", failures)
    _check(to_epoch_ms("1799999999000") == 1799999999000, "数字字符串毫秒保持", failures)
    _check(to_epoch_ms({"timestamp": 1790000000000}) == 1790000000000, "嵌套 timestamp", failures)
    _check(to_epoch_ms("2026-11-13 10:30:00") is not None, "本地时间字符串", failures)
    _check(to_epoch_ms(1) is None, "小整数不当成时间戳", failures)
    _check(to_epoch_ms(None) is None, "None 安全", failures)

    print("2) 状态归一化")
    _check(normalize_status(0, status_map) == "open", "0 -> open", failures)
    _check(normalize_status(1, status_map) == "completed", "1 -> completed", failures)
    _check(normalize_status(True, status_map) == "completed", "True -> completed", failures)
    _check(normalize_status("未完成", status_map) == "open", "中文未完成", failures)
    _check(normalize_status("", status_map) == "open", "空值 -> open", failures)

    print("3) CLI 输出解析（NDJSON 分页）")
    ndjson = json.dumps(SAMPLE_PAGE_1, ensure_ascii=False) + "\n" + json.dumps(SAMPLE_PAGE_2, ensure_ascii=False)
    pages = parse_cli_output(ndjson, ["todo", "list"])
    _check(len(pages) == 2, "两页都能解析", failures)

    print("4) 待办抽取与子项展开")
    raw_items = extract_raw_items(pages, mapping)
    _check(len(raw_items) == 3, f"抽出 3 个待办对象（实际 {len(raw_items)}）", failures)

    wrapper = {"code": 0, "status": "ok", "has_more": False, "data": {"todos": SAMPLE_PAGE_1["todos"]}}
    wrapped = extract_raw_items([wrapper], mapping)
    _check(len(wrapped) == 2, f"包装对象不被误判成待办（实际 {len(wrapped)}）", failures)

    # 真实返回结构：HTML 正文、嵌套 creator、status=proceed、中文时间
    real_todos = flatten_todos(extract_raw_items([REAL_SAMPLE], mapping), mapping, status_map)
    _check(len(real_todos) == 2, f"真实结构解析出 2 条（实际 {len(real_todos)}）", failures)
    real = real_todos[0]
    _check(real.description == "同步学习", f"HTML 正文被清洗（实际 {real.description!r}）", failures)
    _check(real.owner == "张三", f"嵌套 creator 取 user_name（实际 {real.owner!r}）", failures)
    _check(real.status == "open", f"proceed -> open（实际 {real.status}）", failures)
    _check(real.created_ms is not None, "`2026-11-13 10:08:29` 能解析成时间戳", failures)
    multi = real_todos[1]
    _check(
        "<" not in multi.description and "&nbsp;" not in multi.description and "第二行" in multi.description,
        f"多行 HTML 清洗（实际 {multi.description!r}）",
        failures,
    )
    _check(multi.status == "completed", "completed 状态解析", failures)
    todos = flatten_todos(raw_items, mapping, status_map)
    ids = sorted(todo.source_id for todo in todos)
    _check(
        ids == ["T-1001#a1", "T-1001#a2", "T-1002", "T-1003"],
        f"source_id 展开正确：{ids}",
        failures,
    )
    by_id = {todo.source_id: todo for todo in todos}
    _check(by_id["T-1001#a1"].title == "整理需求清单", "子项继承标题", failures)
    _check(by_id["T-1001#a1"].status == "open", "子项状态解析", failures)
    _check(by_id["T-1001#a2"].status == "completed", "子项已完成状态", failures)
    _check(by_id["T-1002"].due_ms == 1790000000000, "父项 due 保留", failures)
    _check(by_id["T-1003"].description.startswith("《设计"), "content 兜底为描述", failures)

    print("5) 飞书任务请求体")
    cfg = {"feishu": {"include_source_id": True, "default_assignee_open_id": "ou_test", "tasklist_guid": ""}}
    task, fields = build_task_payload(by_id["T-1001#a1"], cfg, now_ms=1800000000000)
    _check(task["summary"] == "整理需求清单", "summary 正确", failures)
    _check(task["due"]["timestamp"] == "1799999999000", "due 用毫秒字符串", failures)
    _check(task["members"][0]["role"] == "assignee", "指派人角色", failures)
    _check("description" in fields and "due" in fields, "update_fields 含变化的字段", failures)

    done_task, done_fields = build_task_payload(by_id["T-1001#a2"], cfg, now_ms=1800000000000)
    _check(done_task["completed_at"] == "1800000000000", "已完成任务带 completed_at", failures)
    _check("completed_at" in done_fields, "completed_at 在 update_fields 里", failures)

    print("6) 详情合并（list -> get）与 deadline/参与人/已删除")
    detail = {
        "todo_id": "tdemuEXAMPLE0001",
        "title": "同步消息",
        "description": "<div>同步学习</div>",
        "status": "proceed",
        "creator": {"userid": "woEXAMPLEUSERID", "user_name": "张三"},
        "deadline": {"type": "date", "value": "2026-11-20"},
        "followers": [{"userid": "u2", "user_name": "张三", "user_status": "accept"}],
        "create_time": "2026-11-13 10:08:29",
        "update_time": "2026-11-13 10:08:29",
        "success": True,
    }
    merged_items = merge_details(REAL_SAMPLE["items"], {detail["todo_id"]: detail}, mapping)
    merged_todos = flatten_todos(merged_items, mapping, status_map)
    first_merged = merged_todos[0]
    _check(first_merged.due_ms is not None, "deadline.value 能解析成毫秒", failures)
    _check(
        datetime.fromtimestamp(first_merged.due_ms / 1000).strftime("%Y-%m-%d") == "2026-11-20",
        "deadline 日期正确",
        failures,
    )
    merged_task, _ = build_task_payload(first_merged, cfg, now_ms=1800000000000)
    _check(merged_task["due"]["is_all_day"] is True, "date 类型标成全天", failures)
    _check("参与人：张三" in merged_task["description"], "参与人写进描述", failures)

    detail_pages = [
        {"items": [detail, {"todo_id": "zzz", "success": False, "errmsg": "无权查看"}], "items_count": 2}
    ]
    parsed_details = _detail_items(detail_pages, mapping)
    _check(len(parsed_details) == 2, f"get 响应按 items 取全（含失败项，实际 {len(parsed_details)}）", failures)

    # 关键场景：待办在企业微信里被标记完成/删除后，list 不再返回它，
    # 只能靠"本地已知 id -> get"把它补回来，否则飞书永远停在未完成。
    gone = {"todo_id": "tdemuGONE0001", "title": "已完成的待办", "description": "做完了", "status": "finished"}
    combined = combine_details([], {"tdemuGONE0001": gone}, mapping)
    _check(len(combined) == 1, "list 里消失的待办能靠 get 补回来", failures)
    gone_todos = flatten_todos(combined, mapping, status_map)
    _check(gone_todos[0].status == "completed", "finished -> completed", failures)

    combined_mixed = combine_details(
        REAL_SAMPLE["items"],
        {detail["todo_id"]: detail, "tdemuGONE0001": gone},
        mapping,
    )
    _check(len(combined_mixed) == 3, f"已有的不重复、缺的补进来（实际 {len(combined_mixed)}）", failures)

    deleted_todos = flatten_todos(
        [{"todo_id": "x1", "title": "已删的待办", "status": "deleted"}], mapping, status_map
    )
    _check(deleted_todos[0].status == "deleted", "deleted 状态保留（sync 会跳过）", failures)

    # 反向同步：飞书任务 -> todo create 请求体
    feishu_task = {
        "guid": "abc123def456789",
        "summary": "写周报",
        "description": "汇总三个项目",
        "due": {"timestamp": "1800000000000", "is_all_day": False},
        "completed_at": "0",
    }
    rev_cfg = {"reverse": {"default_follower_userid": "wo123"}}
    rev_item = build_create_payload(feishu_task, rev_cfg)["items"][0]
    _check(rev_item["title"] == "写周报", "标题映射", failures)
    _check("来源：飞书任务 abc123de" in rev_item["description"], "描述带来源标记", failures)
    _check(rev_item["deadline"]["type"] == "datetime", "datetime 类型", failures)
    _check(len(rev_item["deadline"]["value"]) == 19, "deadline 用 YYYY-MM-DD HH:MM:SS", failures)
    _check(rev_item["follower_ids"] == ["wo123"], "参与人用 follower_ids（字符串数组）", failures)
    _check("follower_ids" not in build_create_payload(feishu_task, {})["items"][0], "未配置就不带参与人", failures)
    all_day_item = build_create_payload(
        {"guid": "g2", "summary": "s", "due": {"timestamp": "1800000000000", "is_all_day": True}}, {}
    )["items"][0]
    _check(
        all_day_item["deadline"]["type"] == "date" and len(all_day_item["deadline"]["value"]) == 10,
        "全天任务用 date 类型",
        failures,
    )
    _check(
        payload_hash(build_create_payload(feishu_task, rev_cfg)) == payload_hash(build_create_payload(feishu_task, rev_cfg)),
        "请求体哈希稳定",
        failures,
    )
    created_ids, create_errors = _parse_create_response(
        [
            {
                "items": [
                    {"todo_id": "t1", "title": "写周报", "success": True},
                    {"title": "坏的", "success": False, "errmsg": "标题超长"},
                ],
                "items_count": 2,
            }
        ]
    )
    _check(created_ids == ["t1"], "create 响应能取到 todo_id", failures)
    _check(len(create_errors) == 1 and "标题超长" in create_errors[0], "create 失败项能识别", failures)
    _check(not is_completed({"completed_at": "0"}), 'completed_at="0" 不算完成（这是真实坑）', failures)
    _check(not is_completed({"completed_at": 0}), "completed_at=0 不算完成", failures)
    _check(not is_completed({}), "没有 completed_at 不算完成", failures)
    _check(is_completed({"completed_at": "1800000000000"}), "有时间戳才算完成", failures)
    _check(is_completed({"completed_at": 1800000000000}), "数字时间戳也算完成", failures)

    upd_item = build_update_payload(feishu_task, rev_cfg, "tdemu123")["items"][0]
    _check(upd_item["todo_id"] == "tdemu123", "update 必须带 todo_id", failures)
    _check(upd_item["title"] == "写周报", "update 带标题", failures)
    _check(
        "followers" not in upd_item and "follower_ids" not in upd_item,
        "update 不传参与人（followers 是全量替换，传了会清空）",
        failures,
    )
    fin_item = build_finish_payload("tdemu123")["items"][0]
    _check(fin_item["finished_all"] is True, "finish 必须 finished_all=True（否则要二次确认）", failures)

    print("7) SQLite 幂等")
    store = Store(":memory:")
    first = store.upsert_todo(by_id["T-1001#a1"])
    second = store.upsert_todo(by_id["T-1001#a1"])
    changed = Todo(
        source_id="T-1001#a1",
        title="整理需求清单",
        description="改过的描述",
        status="open",
        due_ms=1799999999000,
    )
    third = store.upsert_todo(changed)
    _check(first == "created", "首次写入 created", failures)
    _check(second == "unchanged", "重复写入 unchanged", failures)
    _check(third == "updated", "内容变化 updated", failures)
    _check(store.counts()["total"] == 1, "计数正确", failures)

    # 防环：反向映射 + 前向识别
    store.set_reverse_mapping("guid-rev-1", "wecom-rev-1", "hash-x")
    _check(store.get_reverse_mapping("guid-rev-1") is not None, "反向映射可写入", failures)
    _check(store.is_reverse_created("wecom-rev-1"), "反向建的待办能被前向识别（防环）", failures)
    _check(not store.is_reverse_created("T-1001#a1"), "普通待办不会被误判成反向建的", failures)
    _check(store.get_mapping_by_guid("不存在的guid") is None, "未知 guid 不误判为前向任务", failures)
    store.close()

    print("8) 待办方法发现（schema 形状兼容）")
    schema_a = {"services": [{"name": "todo", "methods": [{"name": "list"}, {"name": "create"}]}]}
    schema_b = {"todo": {"list": {"input": {}}, "get": {"input": {}}}}
    schema_c = {"tools": [{"name": "todo.list"}]}
    schema_d = {"services": [{"name": "message", "methods": [{"name": "send"}]}]}
    found_a = _rank_methods(_dedupe(_discover_from_schema(schema_a)))
    found_b = _rank_methods(_dedupe(_discover_from_schema(schema_b)))
    found_c = _rank_methods(_dedupe(_discover_from_schema(schema_c)))
    found_d = _discover_from_schema(schema_d)
    _check(found_a[:2] == [["todo", "list"], ["todo", "create"]], f"services/methods 形状：{found_a[:2]}", failures)
    _check(["todo", "list"] in found_b and ["todo", "get"] in found_b, f"字典形状：{found_b}", failures)
    _check(found_c == [["todo", "list"]], f"点分字符串形状：{found_c}", failures)
    _check(found_d == [], f"不相关的 service 不误报：{found_d}", failures)
    _check(
        _rank_methods([["todo", "create"], ["todo", "list"]])[0] == ["todo", "list"],
        "读写方法排序：list 优先",
        failures,
    )
    _check(_split_identifier("wecom_todo_list") == ["list"], "下划线写法能拆出来", failures)

    tokens = _tokens_from_help(REAL_HELP_TEXT)
    _check("list" in tokens, f"真实 help 能识别 list（当前 {tokens}）", failures)
    _check("get" in tokens, "真实 help 能识别 get", failures)
    _check("commands" not in tokens and "options" not in tokens, "小标题不会混进方法名", failures)
    _check("doc" not in tokens and "h" not in tokens, "选项行不会混进方法名", failures)
    _check(
        _is_read_method(["todo", "list"]) and _is_read_method(["todo", "get"]),
        "list/get 判定为只读",
        failures,
    )
    _check(
        not _is_read_method(["todo", "create"])
        and not _is_read_method(["todo", "delete"])
        and not _is_read_method(["todo", "finish"])
        and not _is_read_method(["todo", "update"]),
        "写方法不会被自动调用",
        failures,
    )
    _check(not _is_read_method(["todo", "help"]), "help 不会被自动调用", failures)
    ranked_real = _rank_methods([["todo", token] for token in tokens if _is_read_method(["todo", token])])
    _check(
        bool(ranked_real) and ranked_real[0] == ["todo", "list"],
        f"只读候选里 list 排第一（当前 {ranked_real}）",
        failures,
    )

    print("9) 参数与错误输出")
    _check(_params_to_args({}) == [], "空参数不产生 flag", failures)
    _check(_params_to_args({"status": 0}) == ["--set", "status=0"], "标量用 --set（避免 Windows 引号问题）", failures)
    _check(_params_to_args({"completed": False}) == ["--set", "completed=false"], "布尔值", failures)
    nested_args = _params_to_args({"filter": {"a": 1}})
    _check(nested_args[0] == "--json", "嵌套参数退回 --json", failures)
    try:
        parse_cli_output('{"error": {"code": 1, "message": "boom"}}', ["todo", "list"])
        _check(False, "错误对象应抛 CliError", failures)
    except CliError:
        _check(True, "错误对象抛出 CliError", failures)

    print("10) 配置文件样例可加载")
    sample = Path(__file__).resolve().parent.parent / "config.example.json"
    if sample.is_file():
        config, _ = load_config(str(sample))
        _check(isinstance(config["mapping"]["id"], list), "config.example.json 可解析", failures)
    else:
        print("  [skip] 未找到 config.example.json")

    print("11) 会议 -> 待办")
    meet_cfg = {"meetings": {"days_ahead": 7, "days_back": 0, "follower_userid": "wo123"}}
    begin, end = time_window(meet_cfg, now=datetime(2026, 11, 13, 10, 0, 0))
    _check(
        (begin, end) == ("2026-11-13 10:00:00", "2026-11-20 10:00:00"),
        f"时间窗口正确（{begin} ~ {end}）",
        failures,
    )
    meeting = {
        "meeting_id": "m1",
        "sub_meeting_id": "s1",
        "subject": "项目周会",
        "begin_time": "2026-11-20 14:00:00",
        "end_time": "2026-11-20 15:00:00",
        "timezone": {"timezone_id": "Asia/Shanghai"},
        "meeting_room": "3F-301",
        "location": "深圳",
        "attendee_count": 5,
        "creator_name": "张三",
        "is_repeat_meeting": False,
    }
    _check(meeting_source_id(meeting) == "meeting:m1:s1", "会议 source_id 含子会议", failures)
    meet_item = build_meeting_todo(meeting, meet_cfg)["items"][0]
    _check(meet_item["title"] == "项目周会", "会议标题", failures)
    _check(
        meet_item["deadline"] == {"type": "datetime", "value": "2026-11-20 14:00:00"},
        "deadline 直接用会议开始时间",
        failures,
    )
    _check(meet_item["follower_ids"] == ["wo123"], "会议待办参与人", failures)
    meet_desc = meet_item["description"]
    _check(
        "会议室：3F-301" in meet_desc and "参会人数：5" in meet_desc and "Asia/Shanghai" in meet_desc,
        "描述含会议室/人数/时区",
        failures,
    )
    _check("来源：企业微信会议 m1" in meet_desc, "描述带来源标记", failures)

    print()
    if failures:
        print(f"自检失败：{len(failures)} 项")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("自检全部通过 ✅")
    return 0
