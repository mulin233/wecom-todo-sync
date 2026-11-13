"""企业微信一侧：驱动官方 `@wecom/cli`（npm 包 @wecom/cli）拉取待办。

为什么要包一层 CLI，而不是直接调 qyapi？
    企业微信服务端"待办"接口（developer.work.weixin.qq.com/document/path/101529~101534）
    只有 创建待办 / 查看待办详情（按 todo_id）/ 更新待办状态 / 删除待办，
    没有"列出本人全部待办"的接口；而官方 CLI 的 todo 服务可以按凭证读取待办。
    因此这里用官方 CLI 做数据源。

CLI 的服务目录是服务端下发的（可用 `wecom-cli schema list` 查看），
所以方法名不写死：优先用配置里的 list_methods，否则自动发现。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import Todo, flatten_todos, iter_todo_dicts

# 方法名排序偏好："列出"类优先，"写"类靠后
_LIST_HINTS = ("list", "query", "search", "all", "page", "scan", "recent")
_WRITE_HINTS = ("create", "add", "new", "update", "modify", "delete", "remove", "del", "complete", "finish")

# schema JSON 里的结构性键名，拼方法名时要跳过
_CONTAINER_WORDS = {
    "method", "methods", "tool", "tools", "service", "services", "resource", "resources",
    "command", "commands", "api", "apis", "name", "names", "id", "ids", "path", "paths",
    "schema", "schemas", "params", "input", "inputs", "output", "outputs", "type", "types",
}

# `--help` 文本里的结构性小标题（只放标题，别放 list/get/show 这类可能是真方法名的词）
_HELP_STOPWORDS = {
    "usage", "options", "option", "flags", "flag", "arguments", "argument", "args",
    "commands", "command", "subcommands", "subcommand", "global", "examples", "example",
    "description", "about", "help",
}

# 只读类方法名特征：自动发现时只自动调用这些，绝不自动调用 create/update/delete/finish
_READ_HINTS = ("list", "query", "search", "get", "detail", "read", "find", "recent", "mine", "all")
_NEVER_CALL = {"help", "version", "schema", "doc"}

# 服务端文档确认存在的方法，作为发现失败时的兜底
DEFAULT_LIST_METHODS = [["todo", "list"]]

# 从管道读取时理论上没有颜色，但不同平台/包装方式可能残留 ANSI 转义序列
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class CliError(RuntimeError):
    """wecom-cli 调用失败（未安装 / 未授权 / 参数错误 / 服务端报错）。"""


class WeComCli:
    def __init__(self, cfg: Dict[str, Any]):
        wecom_cfg = cfg["wecom"]
        prefix = wecom_cfg.get("cli") or ["wecom-cli"]
        if isinstance(prefix, str):
            prefix = prefix.split()
        self.prefix: List[str] = [str(part) for part in prefix]
        self.timeout = int(wecom_cfg.get("timeout_seconds", 300))
        self.page_count = int(wecom_cfg.get("page_count", 20))
        self.page_delay_ms = int(wecom_cfg.get("page_delay_ms", 150))
        self.extra_args: List[str] = [str(a) for a in (wecom_cfg.get("extra_args") or [])]
        self.list_params: Dict[str, Any] = wecom_cfg.get("list_params") or {}
        self.configured_methods: List[List[str]] = [list(m) for m in (wecom_cfg.get("list_methods") or [])]

    # ------------------------------------------------------------ 进程执行

    def _build_command(self, args: Sequence[str]) -> List[str]:
        """拼出最终要执行的命令，处理几个 Windows 上的坑：

        1. npm 全局安装只生成 `wecom-cli.cmd` 垫片，CreateProcess 不能直接执行 .cmd；
        2. 走 `cmd /c` 时，含空格的路径和带引号的 JSON 参数容易被改坏。

        所以优先绕过垫片：找到垫片背后的 node 脚本直接 `node <script>` 执行。
        """
        extra = [str(a) for a in args]
        command = list(self.prefix) + extra
        resolved = shutil.which(command[0])

        if resolved:
            command[0] = resolved
            if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
                node_command = _node_script_invocation(resolved, extra)
                if node_command:
                    return node_command
                command = ["cmd", "/c"] + command
        return command

    def run_cli(self, args: Sequence[str], timeout: Optional[int] = None) -> str:
        command = self._build_command(args)
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CliError(
                "找不到 wecom-cli，请先安装：npm install -g @wecom/cli"
                f"（当前命令：{' '.join(self.prefix)}）"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CliError(f"wecom-cli 执行超时（{timeout or self.timeout}s）：{' '.join(args)}") from exc

        stdout = _ANSI_RE.sub("", completed.stdout.decode("utf-8", errors="replace")).strip()
        stderr = _ANSI_RE.sub("", completed.stderr.decode("utf-8", errors="replace")).strip()

        if completed.returncode != 0:
            message = _extract_error_message(stdout) or stderr or f"退出码 {completed.returncode}"
            raise CliError(f"wecom-cli {' '.join(args)} 失败：{message}")
        return stdout

    # ---------------------------------------------------------------- 体检

    def version(self) -> str:
        return self.run_cli(["--version"], timeout=60)

    def auth_status(self) -> str:
        """返回 authorized / unauthorized / unknown。"""
        try:
            out = self.run_cli(["auth", "show", "--status"], timeout=60)
        except CliError:
            return "unknown"
        text = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if "authorized" in text and "unauthorized" not in text:
            return "authorized"
        if "unauthorized" in text:
            return "unauthorized"
        return "unknown"

    def schema_list(self) -> Optional[Any]:
        try:
            out = self.run_cli(["schema", "list"], timeout=120)
        except CliError:
            return None
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------ 方法发现

    @staticmethod
    def is_read_method(method: Sequence[str]) -> bool:
        """这个方法是不是只读的（只读的才会被自动采集调用）。"""
        return _is_read_method(method)

    def candidate_methods(self, read_only: bool = True) -> List[List[str]]:
        """按优先级给出"列出待办"的候选方法。

        read_only=True（默认）：只保留只读方法，避免"list 返回空"时误调 create/update/delete。
        显式配置了 list_methods 时原样返回，不做过滤。
        """
        if self.configured_methods:
            return self.configured_methods

        candidates: List[List[str]] = []

        schema = self.schema_list()
        if schema is not None:
            candidates.extend(_discover_from_schema(schema))

        # 兜底：解析 `wecom-cli todo --help` 的帮助文本
        try:
            help_text = self.run_cli(["todo", "--help"], timeout=60)
            candidates.extend([["todo", token] for token in _tokens_from_help(help_text)])
        except CliError:
            pass

        ranked = _rank_methods(_dedupe(candidates))
        if read_only:
            ranked = [method for method in ranked if _is_read_method(method)]
        if not ranked:
            ranked = [list(method) for method in DEFAULT_LIST_METHODS]
        return ranked

    def fetch_pages(
        self,
        method: Sequence[str],
        params: Optional[Dict[str, Any]] = None,
        page_count: Optional[int] = None,
        paginate: bool = True,
    ) -> List[Any]:
        """调用一个方法，返回解析后的每一页 JSON。

        paginate=False 时不传 --page-count（例如 `todo get` 是按 id 批量查、没有游标）。
        """
        args: List[str] = list(method)
        payload = dict(self.list_params)
        payload.update(params or {})
        args += _params_to_args(payload)

        pages = page_count if page_count is not None else self.page_count
        if paginate and pages and pages > 1:
            args += ["--page-count", str(pages), "--page-delay", str(self.page_delay_ms)]
        args += self.extra_args

        stdout = self.run_cli(args)
        return parse_cli_output(stdout, method)


    def call_method(
        self,
        method: Sequence[str],
        payload: Dict[str, Any],
        dry_run: bool = False,
    ) -> List[Any]:
        """用 `--json` 调任意方法（create/update/finish 都是 items 数组入参）。

        dry_run=True 时加 CLI 的 --dry-run：只在本地校验并打印请求，不真的写。
        """
        args: List[str] = list(method) + ["--json", json.dumps(payload, ensure_ascii=False)]
        if dry_run:
            args.append("--dry-run")
        args += self.extra_args
        return parse_cli_output(self.run_cli(args), method)

    def create_items(
        self,
        payload: Dict[str, Any],
        method: Optional[Sequence[str]] = None,
        dry_run: bool = False,
    ) -> List[Any]:
        """调 `todo create`（保留旧名字，内部走 call_method）。"""
        return self.call_method(method or ["todo", "create"], payload, dry_run=dry_run)


# ---------------------------------------------------------------- 输出解析

def _node_script_invocation(shim_path: str, args: Sequence[str]) -> Optional[List[str]]:
    """从 npm 的 .cmd 垫片反推 node 脚本（<bin>/node_modules/@wecom/cli/bin/wecom.js）。

    找到就直接 `node <script> ...` 执行，避开 cmd.exe 的引号问题；找不到返回 None。
    """
    shim = Path(shim_path)
    candidates = [
        shim.parent / "node_modules" / "@wecom" / "cli" / "bin" / "wecom.js",
        shim.parent / "node_modules" / "@wecom" / "cli" / "bin" / "wecom.mjs",
    ]
    for candidate in candidates:
        if candidate.is_file():
            node = shutil.which("node")
            if node:
                return [node, str(candidate)] + [str(a) for a in args]
    return None


def _params_to_args(payload: Dict[str, Any]) -> List[str]:
    """把参数转成 CLI 参数。

    默认用 `--set key=value`（避免 JSON 里的引号/花括号在 Windows 的 cmd 上被转义坏掉），
    只有当参数里有嵌套对象/数组时才退回 `--json`。
    """
    if not payload:
        return []

    simple = all(
        value is None or isinstance(value, (str, int, float, bool))
        for value in payload.values()
    )
    if simple:
        args: List[str] = []
        for key, value in payload.items():
            if isinstance(value, bool):
                text = "true" if value else "false"
            elif value is None:
                text = "null"
            else:
                text = str(value)
            args += ["--set", f"{key}={text}"]
        return args

    return ["--json", json.dumps(payload, ensure_ascii=False)]


def parse_cli_output(stdout: str, method: Sequence[str]) -> List[Any]:
    """解析 CLI 输出：可能是单行 compact JSON，也可能是 --page-count 的 NDJSON。"""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return []

    # 先按 NDJSON 逐行解析
    objects: List[Any] = []
    ok = True
    for line in lines:
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError:
            ok = False
            break

    if not ok:
        try:
            objects = [json.loads(stdout)]
        except json.JSONDecodeError as exc:
            raise CliError(
                f"wecom-cli {' '.join(method)} 的输出不是合法 JSON：{stdout[:400]}"
            ) from exc

    for obj in objects:
        if isinstance(obj, dict) and "error" in obj:
            raise CliError(f"wecom-cli {' '.join(method)} 返回错误：{json.dumps(obj, ensure_ascii=False)}")

    return objects


def _extract_error_message(stdout: str) -> str:
    for line in reversed([line for line in stdout.splitlines() if line.strip()]):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
            err = obj["error"]
            return f"{err.get('type', 'Error')}({err.get('code', '?')}): {err.get('message', '')}"
    return ""


def _discover_from_schema(schema: Any) -> List[List[str]]:
    """从 `wecom-cli schema list` 的 JSON 里推断"列出待办"的调用方式。

    schema 的具体形状由服务端决定，常见有：
        {"services": [{"name": "todo", "methods": [{"name": "list"}]}]}
        {"todo": {"list": {...}, "get": {...}}}
        {"tools": [{"name": "todo.list"}]}
    所以这里不去猜固定结构，而是：
      1. 找到"自身就提到 todo"的最深节点（todo 服务节点）；
      2. 把这些节点子树里所有像方法名的字符串取出来，拼成 ["todo", 方法名]。
    """
    candidates: List[List[str]] = []
    for node in _todo_service_nodes(schema):
        for token in _subtree_tokens(node):
            tail = _split_identifier(token)
            if tail:
                candidates.append(["todo"] + tail)
    return candidates


def _todo_service_nodes(node: Any, out: Optional[List[Any]] = None) -> List[Any]:
    """找出自身键/值里出现 todo 的最深 dict 节点。"""
    out = [] if out is None else out
    if isinstance(node, dict):
        own = [str(key) for key in node.keys()]
        own += [str(value) for value in node.values() if isinstance(value, str)]
        if any("todo" in item.lower() for item in own):
            out.append(node)
            return out
        for value in node.values():
            _todo_service_nodes(value, out)
    elif isinstance(node, list):
        for item in node:
            _todo_service_nodes(item, out)
    return out


def _subtree_tokens(node: Any) -> List[str]:
    """取出子树里所有像"方法名"的字符串（键名 + 字符串值）。"""
    tokens: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            tokens.append(str(key))
            tokens.extend(_subtree_tokens(value))
    elif isinstance(node, list):
        for item in node:
            tokens.extend(_subtree_tokens(item))
    elif isinstance(node, str):
        text = node.strip()
        if text:
            tokens.append(text)
    return tokens


def _split_identifier(text: str) -> List[str]:
    """把 "list" / "todo.list" / "wecom_todo_list" 这类写法拆成方法路径片段。"""
    text = text.strip()
    if not text:
        return []

    parts: List[str] = []
    for chunk in re.split(r"[./\s]+", text):
        if not chunk:
            continue
        lowered = chunk.lower()
        if "todo" in lowered and lowered != "todo":
            # wecom_todo_list / todo_list 这类写法：切出 todo 后面的部分
            segments = [seg for seg in re.split(r"[-_]+", chunk) if seg]
            for index, segment in enumerate(segments):
                if "todo" in segment.lower():
                    segments = segments[index + 1:]
                    break
            parts.extend(segments)
        elif lowered == "todo":
            continue
        else:
            parts.append(chunk)

    return [
        part
        for part in parts
        if part.isidentifier() and part.lower() not in _CONTAINER_WORDS
    ]


def _tokens_from_help(help_text: str) -> List[str]:
    tokens = []
    for line in help_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        first = stripped.split()[0]
        if first.startswith("-"):
            continue  # 选项行（--doc / -h）不是方法名
        token = first.strip(":,|-")
        if token and token.isidentifier():
            tokens.append(token.lower())
        # 形如 `wecom-cli todo list [flags]`
        if "wecom-cli" in stripped:
            parts = stripped.split()
            if "todo" in parts:
                index = parts.index("todo")
                if len(parts) > index + 1:
                    candidate = parts[index + 1].strip(":,|-")
                    if candidate.isidentifier() and not candidate.startswith("-"):
                        tokens.append(candidate.lower())
    # 帮助文本里的固定小标题不是方法名
    return [token for token in tokens if token not in _HELP_STOPWORDS]


def _dedupe(items: Iterable[List[str]]) -> List[List[str]]:
    seen = set()
    result = []
    for item in items:
        key = tuple(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(list(item))
    return result


def _is_read_method(method: Sequence[str]) -> bool:
    """判断是不是只读方法（自动采集只允许调只读的）。"""
    last = method[-1].lower()
    if last in _NEVER_CALL:
        return False
    if any(hint in last for hint in _WRITE_HINTS):
        return False
    return any(hint in last for hint in _READ_HINTS)


def _rank_methods(methods: List[List[str]]) -> List[List[str]]:
    def score(method: List[str]) -> tuple:
        last = method[-1].lower()
        list_rank = 0 if any(hint in last for hint in _LIST_HINTS) else 1
        write_rank = 1 if any(hint in last for hint in _WRITE_HINTS) else 0
        return (write_rank, list_rank, len(method), ".".join(method))

    return sorted(methods, key=score)


# ------------------------------------------------- 离线数据源（便于自检/演示）

def load_raw_from_file(path: Path | str) -> List[Any]:
    """把本地 JSON 文件当作一次 CLI 返回读取（结构与 CLI 输出一致）。

    支持：
      - JSON 数组：[{...}, {...}]
      - 单个 JSON 对象：{"data": {"todos": [...]}}
      - NDJSON：每行一个 JSON
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        pages = []
        for line in text.splitlines():
            if line.strip():
                pages.append(json.loads(line))
        return pages


def extract_raw_items(pages: Iterable[Any], mapping: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """从若干页 JSON 中掏出所有像待办的 dict。

    先按严格规则找（必须有 id + 标题/正文），找不到再放宽一次。
    """
    pages = list(pages)
    for strict in (True, False):
        items: List[Dict[str, Any]] = []
        for page in pages:
            items.extend(iter_todo_dicts(page, mapping, strict))
        if items:
            return items
    return []


def to_todos(
    raw_items: Iterable[Dict[str, Any]],
    mapping: Dict[str, List[str]],
    status_map: Dict[str, str],
) -> List[Todo]:
    return flatten_todos(raw_items, mapping, status_map)
