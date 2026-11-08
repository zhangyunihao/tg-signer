"""静态扫描 WebUI 中「事件循环上直接做同步 IO」的调用点。

用法（项目根目录）：
    python scripts/check_ui_blocking_io.py                 # 报告模式，退出码 0
    python scripts/check_ui_blocking_io.py --json out.json # 同时输出机器可读清单
    python scripts/check_ui_blocking_io.py --strict        # 有 high/medium 命中则退出 1

判定范围：
- async 函数（NiceGUI 直接调度到事件循环）
- ui.timer / on_click / on_change 等注册的回调
- @ui.page、ui.run(build_ui) 页面构建入口
- 上述入口同步可达的同模块/跨模块函数（含 self.method、模块别名调用）

允许写法：await asyncio.to_thread(...)、await run.io_bound(...)、
await store.xxx(...)（P1 之后提供）。行内可用 ``# tgs-io-allow: 理由`` 豁免。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCAN_ROOT = REPO_ROOT / "tg_signer" / "webui"

IO_FUNCS = {"open", "atomic_write_json", "atomic_write_text"}
IO_MODULE_MEMBERS: dict[str, set[str]] = {
    "sqlite3": {"connect"},
    "json": {"load", "dump"},
    "subprocess": {"Popen", "run", "call", "check_call", "check_output"},
    "shutil": {"copy", "copy2", "copyfile", "copytree", "move", "rmtree"},
    "zipfile": {"ZipFile"},
    "time": {"sleep"},
    "os": {
        "stat",
        "scandir",
        "listdir",
        "walk",
        "remove",
        "unlink",
        "rename",
        "replace",
        "mkdir",
        "makedirs",
        "rmdir",
        "removedirs",
    },
    "io": {"open"},
}
IO_DOTTED_MEDIUM = {
    "os.path.getsize",
    "os.path.getmtime",
    "os.path.getctime",
}
IO_DOTTED_LOW = {
    "os.path.exists",
    "os.path.isfile",
    "os.path.isdir",
    "os.path.islink",
}
IO_METHODS = {
    "read_text",
    "write_text",
    "read_bytes",
    "write_bytes",
    "iterdir",
    "glob",
    "rglob",
    "stat",
    "lstat",
    "unlink",
    "mkdir",
    "touch",
    "rename",
    "samefile",
    "readlink",
    "symlink_to",
    "hardlink_to",
    "chmod",
    "open",
}
IO_METHODS_LOW = {"exists", "is_file", "is_dir"}
OFFLOAD_NAMES = {"to_thread", "io_bound", "run_in_executor"}
EVENT_KWARGS = {
    "on_click",
    "on_change",
    "on_value_change",
    "on_select",
    "on_upload",
    "on_delete",
    "on_repeat",
}
PAGE_DECORATORS = {"ui.page", "page"}
OFFLOAD_ATTRS = {"run.io_bound", "asyncio.to_thread"}
ALLOW_MARKER = "tgs-io-allow:"


@dataclass
class FuncInfo:
    module: str
    path: Path
    qualname: str
    node: ast.AST
    is_async: bool
    class_name: Optional[str] = None
    parent: Optional["FuncInfo"] = None
    children: dict[str, "FuncInfo"] = field(default_factory=dict)
    def_line: int = 0
    contexts: set[str] = field(default_factory=set)


@dataclass
class Violation:
    path: str
    line: int
    func: str
    context: str
    severity: str
    call: str
    via: str
    allowlisted: bool = False


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _module_name(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    prefix = root.relative_to(REPO_ROOT).parts
    return ".".join((*prefix, *parts))


def _resolve_from(module: str, level: int, imported: Optional[str]) -> str:
    if level <= 0:
        return imported or ""
    package = module.split(".")
    if level > 1:
        package = package[: -(level - 1)]
    else:
        package = package[:-1]
    if imported:
        package = [*package, imported]
    return ".".join(package)


def _build_imports(tree: ast.Module, module: str) -> dict[str, tuple[str, str, str]]:
    """local name -> (kind, module, member)。kind 为 module/member。"""

    imports: dict[str, tuple[str, str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                target = alias.name if alias.asname else alias.name.split(".", 1)[0]
                imports[local] = ("module", target, "")
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_from(module, node.level, node.module)
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                imports[local] = ("member", target, alias.name)
    return imports


def _collect_funcs(
    tree: ast.Module, module: str, path: Path, root: Path
) -> tuple[list[FuncInfo], dict[tuple[str, str], FuncInfo], dict[str, dict]]:
    funcs: list[FuncInfo] = []
    table: dict[tuple[str, str], FuncInfo] = {}

    def visit_body(
        body: list[ast.stmt], parent: Optional[FuncInfo], class_name: Optional[str]
    ):
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit_body(node.body, parent, node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = f"{parent.qualname}." if parent else ""
                if parent is None and class_name:
                    name = f"{class_name}.{node.name}"
                elif parent is not None and class_name:
                    name = f"{parent.qualname}.{class_name}.{node.name}"
                else:
                    name = f"{prefix}{node.name}"
                info = FuncInfo(
                    module=module,
                    path=path,
                    qualname=name,
                    node=node,
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                    class_name=parent.class_name if parent else class_name,
                    parent=parent,
                    def_line=node.lineno,
                )
                funcs.append(info)
                if parent is None:
                    table[(module, name)] = info
                else:
                    parent.children[node.name] = info
                visit_body(node.body, info, None)
            elif isinstance(node, ast.If):
                visit_body(node.body, parent, class_name)
                visit_body(node.orelse, parent, class_name)
            elif isinstance(node, (ast.For, ast.While, ast.With, ast.Try)):
                for attr in ("body", "orelse", "finalbody"):
                    visit_body(getattr(node, attr, []), parent, class_name)
                for handler in getattr(node, "handlers", []):
                    visit_body(handler.body, parent, class_name)

    visit_body(tree.body, None, None)
    return funcs, table, {}


def _is_offload(call: ast.Call, text: str) -> bool:
    name = dotted(call.func)
    if name in OFFLOAD_ATTRS:
        return True
    if name.split(".")[-1] in OFFLOAD_NAMES:
        return True
    return text in OFFLOAD_ATTRS


def _is_primitive(
    call: ast.Call,
    imports: dict[str, tuple[str, str, str]],
) -> Optional[tuple[str, str]]:
    """返回 (severity, 展示文本)；不是同步 IO 返回 None。"""

    name = dotted(call.func)
    if not name:
        return None
    first = name.split(".", 1)[0]
    imported = imports.get(first)
    expanded = name
    if imported:
        kind, module, member = imported
        rest = name.split(".", 1)[1] if "." in name else ""
        if kind == "member":
            expanded = f"{module}.{member}" + (f".{rest}" if rest else "")
        else:
            expanded = module + (f".{rest}" if rest else "")

    if expanded == "open" or expanded in IO_FUNCS:
        return "high", expanded
    if imported and imported[1] == "tg_signer.utils" and imported[2] in IO_FUNCS:
        return "high", f"{imported[1]}.{imported[2]}"
    if expanded in IO_DOTTED_LOW:
        return "low", expanded
    if expanded in IO_DOTTED_MEDIUM:
        return "medium", expanded
    for module, members in IO_MODULE_MEMBERS.items():
        prefix = f"{module}."
        if expanded.startswith(prefix):
            member = expanded[len(prefix) :].split(".", 1)[0]
            if member in members:
                return "high", expanded

    attr = name.rsplit(".", 1)[-1]
    if attr in IO_METHODS:
        return "high", f".{attr}()"
    if attr in IO_METHODS_LOW:
        return "low", f".{attr}()"
    return None


def _walk_calls(node: ast.AST, visit) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Call):
            if visit(child):
                continue
            _walk_calls(child, visit)
            continue
        _walk_calls(child, visit)


def _callable_node(value: ast.AST) -> bool:
    return isinstance(value, (ast.Name, ast.Attribute, ast.Lambda))


def scan(paths: Iterable[Path], root: Path) -> list[Violation]:
    files = [path for path in paths if path.suffix == ".py"]
    funcs_all: list[FuncInfo] = []
    table: dict[tuple[str, str], FuncInfo] = {}
    imports_by_module: dict[str, dict[str, tuple[str, str, str]]] = {}

    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        module = _module_name(path, root)
        imports_by_module[module] = _build_imports(tree, module)
        funcs, module_table, _ = _collect_funcs(tree, module, path, root)
        funcs_all.extend(funcs)
        table.update(module_table)

    for info in funcs_all:
        for decorator in getattr(info.node, "decorator_list", []):
            if dotted(decorator).split("(")[0] in PAGE_DECORATORS:
                info.contexts.add("page")
        if info.is_async:
            info.contexts.add("async")

    def resolve(value: ast.AST, scope: Optional[FuncInfo]) -> Optional[FuncInfo]:
        name = dotted(value)
        if not name or scope is None:
            return None
        parts = name.split(".")
        if parts[0] in {"self", "cls"} and len(parts) == 2 and scope.class_name:
            return table.get((scope.module, f"{scope.class_name}.{parts[1]}"))
        current: Optional[FuncInfo] = scope
        while current is not None:
            if len(parts) == 1 and parts[0] in current.children:
                return current.children[parts[0]]
            current = current.parent
        imports = imports_by_module.get(scope.module, {})
        imported = imports.get(parts[0])
        if len(parts) == 1:
            found = table.get((scope.module, name))
            if found is not None:
                return found
            if imported:
                kind, module, member = imported
                if kind == "member":
                    return table.get((module, member))
                return table.get((module, name))
            return None
        if imported:
            kind, module, member = imported
            rest = name.split(".", 1)[1]
            if kind == "member":
                return table.get((module, f"{member}.{rest}"))
            return table.get((module, rest))
        return table.get((scope.module, name))

    seeds_list: list[tuple[FuncInfo, str]] = []

    def seed(value: ast.AST, context: str, scope: Optional[FuncInfo]) -> None:
        if isinstance(value, ast.Lambda) and scope is not None:
            pseudo = FuncInfo(
                module=scope.module,
                path=scope.path,
                qualname=f"{scope.qualname}.<lambda>",
                node=value,
                is_async=False,
                class_name=scope.class_name,
                parent=scope,
                def_line=value.lineno,
            )
            seeds_list.append((pseudo, context))
            return
        target = resolve(value, scope)
        if target is not None:
            target.contexts.add(context)

    for info in funcs_all:
        for call in ast.walk(info.node):
            if not isinstance(call, ast.Call):
                continue
            name = dotted(call.func)
            if not name:
                continue
            if name.endswith(".timer") or name == "timer":
                if len(call.args) >= 2:
                    seed(call.args[1], "callback", info)
                for keyword in call.keywords:
                    if keyword.arg == "callback":
                        seed(keyword.value, "callback", info)
            for keyword in call.keywords:
                if keyword.arg in EVENT_KWARGS and _callable_node(keyword.value):
                    seed(keyword.value, "callback", info)
            if name.endswith(".on") and len(call.args) >= 2:
                if _callable_node(call.args[1]):
                    seed(call.args[1], "callback", info)
            if name in {"ui.run", "run"} and call.args:
                seed(call.args[0], "page", info)
            if name.endswith(("on_page_exception", "on_exception")) and call.args:
                seed(call.args[0], "page", info)

    for info in funcs_all:
        for context in info.contexts:
            seeds_list.append((info, context))

    violations: dict[tuple[str, int, str, str], Violation] = {}

    def record(
        info: FuncInfo,
        context: str,
        call: ast.Call,
        severity: str,
        label: str,
        chain: tuple[str, ...],
    ) -> None:
        key = (str(info.path), call.lineno, info.qualname, context)
        if key in violations:
            return
        violations[key] = Violation(
            path=str(info.path),
            line=call.lineno,
            func=info.qualname,
            context=context,
            severity=severity,
            call=label,
            via=" -> ".join((*chain, info.qualname)),
        )

    queue: list[tuple[FuncInfo, str, tuple[str, ...]]] = [
        (info, context, ()) for info, context in seeds_list
    ]
    seen: set[tuple[str, str, str]] = set()
    while queue:
        info, context, chain = queue.pop(0)
        marker = (str(info.path), info.qualname, context)
        if marker in seen:
            continue
        seen.add(marker)
        imports = imports_by_module.get(info.module, {})

        def visit(
            call: ast.Call,
            _info: FuncInfo = info,
            _context: str = context,
            _chain: tuple[str, ...] = chain,
            _imports: dict = imports,
        ) -> bool:
            call_text = dotted(call.func)
            if _is_offload(call, call_text):
                return True
            found = _is_primitive(call, _imports)
            if found:
                record(_info, _context, call, found[0], found[1], _chain)
                return False
            target = resolve(call.func, _info)
            if target is not None:
                queue.append((target, _context, (*_chain, _info.qualname)))
            return False

        _walk_calls(info.node, visit)

    for item in violations.values():
        lines = (
            Path(item.path).read_text(encoding="utf-8", errors="replace").splitlines()
        )
        nearby = []
        for lineno in (item.line - 2, item.line - 1, item.line):
            if 0 <= lineno < len(lines):
                nearby.append(lines[lineno])
        info = next(
            (
                f
                for f in funcs_all
                if str(f.path) == item.path and f.qualname == item.func
            ),
            None,
        )
        if info is not None and 0 <= info.def_line - 1 < len(lines):
            nearby.append(lines[info.def_line - 1])
        if any(ALLOW_MARKER in line for line in nearby):
            item.allowlisted = True

    return sorted(violations.values(), key=lambda v: (v.severity, v.path, v.line))


def _severity_rank(severity: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(severity, 3)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="扫描 WebUI 事件循环上的同步 IO")
    parser.add_argument("--path", action="append", help="扫描目录或文件，可重复")
    parser.add_argument("--json", help="把完整清单写入该 JSON 文件")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="存在 high/medium 未豁免命中时退出 1",
    )
    parser.add_argument(
        "--include-low",
        action="store_true",
        help="明细里同时打印 low（exists/is_file 等）",
    )
    parser.add_argument("--max-detail", type=int, default=80, help="控制台最多打印条数")
    args = parser.parse_args(argv)

    targets = [Path(p) for p in args.path] if args.path else [DEFAULT_SCAN_ROOT]
    paths: list[Path] = []
    for target in targets:
        if target.is_dir():
            paths.extend(sorted(target.rglob("*.py")))
        else:
            paths.append(target)

    violations = scan(paths, DEFAULT_SCAN_ROOT)
    real = [v for v in violations if not v.allowlisted]
    stats = {"high": 0, "medium": 0, "low": 0}
    contexts: dict[str, int] = {}
    for item in violations:
        stats[item.severity] = stats.get(item.severity, 0) + 1
        contexts[item.context] = contexts.get(item.context, 0) + 1

    print("=" * 72)
    print(f"扫描文件：{len(paths)}  命中调用点：{len(violations)}")
    print(
        "严重度：high {high} / medium {medium} / low {low}；"
        "豁免 {allow}；未豁免 {real}".format(
            high=stats.get("high", 0),
            medium=stats.get("medium", 0),
            low=stats.get("low", 0),
            allow=len(violations) - len(real),
            real=len(real),
        )
    )
    print(
        "入口类型："
        + ("、".join(f"{k}={v}" for k, v in sorted(contexts.items())) or "-")
    )
    print("=" * 72)

    shown = 0
    for item in real:
        if item.severity == "low" and not args.include_low:
            continue
        if shown >= args.max_detail:
            print(f"... 其余 {len(real) - shown} 条见 JSON 输出")
            break
        shown += 1
        relative = Path(item.path).relative_to(REPO_ROOT)
        print(
            f"{item.severity.upper():<6} {relative}:{item.line} "
            f"[{item.context}] {item.call}  ({item.via})"
        )

    if args.json:
        payload = [
            {
                "path": str(Path(v.path).relative_to(REPO_ROOT)),
                "line": v.line,
                "function": v.func,
                "context": v.context,
                "severity": v.severity,
                "call": v.call,
                "via": v.via,
                "allowlisted": v.allowlisted,
            }
            for v in violations
        ]
        output = Path(args.json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n完整清单：{output}")

    if args.strict:
        blocking = [v for v in real if v.severity in {"high", "medium"}]
        return 1 if blocking else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
