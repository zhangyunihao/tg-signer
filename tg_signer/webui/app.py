import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict

from nicegui import app, ui
from pydantic import TypeAdapter

from tg_signer.webui.data import (
    CONFIG_META,
    DEFAULT_LOG_FILE,
    DEFAULT_WORKDIR,
    LOG_DIR,
    ConfigKind,
    account_identities,
    apply_account_order,
    delete_config,
    get_workdir,
    list_log_files,
    list_task_names,
    load_config,
    load_disabled_accounts,
    load_logs,
    load_sign_records,
    load_user_infos,
    save_config,
    rename_account_state,
    set_account_enabled,
    set_account_order,
)
from tg_signer.core import get_api_config, get_proxy
from tg_signer.webui.interactive import InteractiveSignerConfig
from tg_signer.webui.auto_leave import auto_leave_block
from tg_signer.webui.random_chat import random_chat_block
from tg_signer.webui.schema_utils import clean_schema

SIGNER_TEMPLATE: Dict[str, object] = {
    "chats": [
        {
            "chat_id": "@channel_or_user",
            "message_thread_id": None,
            "name": "示例任务",
            "delete_after": None,
            "actions": [{"action": 1, "text": "签到"}],
            "action_interval": 1,
        }
    ],
    "sign_at": "0 6 * * *",
    "random_seconds": 0,
    "sign_interval": 1,
}

MONITOR_TEMPLATE: Dict[str, object] = {
    "match_cfgs": [
        {
            "chat_id": "@channel_or_user",
            "rule": "contains",
            "rule_value": "关键词",
            "from_user_ids": None,
            "always_ignore_me": False,
            "default_send_text": "自动回复",
            "ai_reply": False,
            "ai_prompt": None,
            "send_text_search_regex": None,
            "send_text_template": None,
            "delete_after": None,
            "ignore_case": True,
            "forward_to_chat_id": None,
            "external_forwards": None,
            "push_via_server_chan": False,
            "server_chan_send_key": None,
        }
    ]
}


AUTH_CODE_ENV = "TG_SIGNER_GUI_AUTHCODE"
AUTH_STORAGE_KEY = "tg_signer_gui_auth_code"
SOURCE_ROOT = Path(__file__).resolve().parents[2]


class UIState:
    def __init__(self) -> None:
        self.workdir: Path = get_workdir(DEFAULT_WORKDIR)
        self.log_path: Path = DEFAULT_LOG_FILE
        self.log_limit: int = 200
        self.record_filter: str = ""

    def set_workdir(self, path_str: str) -> None:
        self.workdir = get_workdir(Path(path_str).expanduser())

    def set_log_path(self, path_str: str) -> None:
        self.log_path = Path(path_str).expanduser()


state = UIState()


def pretty_json(data: Dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def notify_error(exc: Exception) -> None:
    ui.notify(f"{exc}", type="negative")


class BaseConfigBlock:
    def __init__(
        self,
        kind: ConfigKind,
        template: Dict[str, object],
    ):
        self.kind = kind
        self.template = template
        self.title = "签到配置 (signer)" if kind == "signer" else "监控配置 (monitor)"
        self.root_dir, self.cfg_cls = CONFIG_META[kind]
        with ui.card().classes("w-full shadow-md"):
            ui.label(self.title).classes("text-lg font-semibold")
            ui.label(f"目录: {self.root_dir}/<name>/config.json").classes(
                "text-sm text-gray-500"
            )
            with ui.row().classes("items-end w-full gap-3"):
                self.select = ui.select(
                    label="选择配置",
                    options=[],
                    with_input=True,
                    on_change=self.load_current,
                ).classes("min-w-[240px]")
                ui.button("重置", on_click=self.clear_selection).props("outline")
                self.name_input = ui.input(
                    label="保存为/新建名称",
                    placeholder="my_task",
                ).classes("min-w-[200px]")
                ui.button("使用示例", on_click=self.fill_template)
                self.setup_toolbar()

            # MonitorConfig schema causes json_editor to fail rendering due to "format": "uri" etc.
            # We need to clean the schema before passing it to the editor.
            schema = TypeAdapter(self.cfg_cls | None).json_schema()
            if self.kind == "monitor":
                schema = clean_schema(schema)

            def on_change(e):
                self.editor.properties["content"] = e.content

            self.editor = ui.json_editor(
                {"content": {"json": None}},
                schema=schema,
                on_change=on_change,
            )
            self.selected_name: dict[str, str] = {"value": ""}

            with ui.row().classes("gap-2 items-center"):
                ui.button("刷新列表", on_click=self.refresh_options)
                ui.button("加载", on_click=self.load_current)
                ui.button("保存", color="primary", on_click=self.save_current)
                ui.button("删除", color="negative", on_click=self.delete_current)
            self.setup_footer()

    def clear_selection(self) -> None:
        self.select.value = None
        self.name_input.value = ""
        self.fill_template()
        self.selected_name["value"] = ""

    def setup_toolbar(self):
        """Override to add more buttons to the top toolbar"""
        pass

    def setup_footer(self):
        """Override to add more buttons to the bottom footer"""
        pass

    def __call__(self, *args, **kwargs):
        self.refresh_options()

    def refresh_options(self) -> None:
        options = list_task_names(self.kind, state.workdir)
        self.select.options = options
        # 只有一个配置时默认选中（会触发 load_current 自动加载）
        if len(options) == 1 and not self.select.value:
            self.select.value = options[0]
        self.select.update()

    def load_current(self) -> None:
        target = self.select.value
        if not target:
            return
        try:
            entry = load_config(self.kind, target, workdir=state.workdir)
            self.editor.properties["content"]["json"] = entry.payload
            self.name_input.value = entry.name
            self.editor.update()
            self.name_input.update()
            self.editor.run_editor_method(":expand", "[]", "path => true")
            self.selected_name["value"] = target
            self.on_loaded(target)
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)

    def on_loaded(self, target: str):
        """Hook called after config is loaded"""
        pass

    def save_current(self) -> None:
        target = (self.name_input.value or self.select.value or "").strip()
        if not target:
            ui.notify("请先填写配置名称", type="warning")
            return
        try:
            save_config(
                self.kind,
                target,
                self.editor.properties["content"]["json"] or "{}",
                workdir=state.workdir,
            )
            self.refresh_options()
            self.select.value = target
            self.select.update()
            ui.notify("保存成功", type="positive")
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)

    def fill_template(self) -> None:
        self.editor.properties["content"]["json"] = self.template
        self.editor.update()

    def delete_current(self) -> None:
        target = (self.select.value or "").strip() or (
            self.name_input.value or ""
        ).strip()
        if not target:
            ui.notify("请选择要删除的配置", type="warning")
            return
        try:
            delete_config(self.kind, target, workdir=state.workdir)
            self.refresh_options()
            if self.select.value == target:
                self.select.value = None
                self.select.update()
            ui.notify("已删除配置", type="positive")
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)


class SignerBlock(BaseConfigBlock):
    def __init__(
        self,
        template: Dict[str, object],
        *,
        goto_records: Callable[[str], None] = lambda _task: None,
    ):
        self.record_btn = None
        self.record_hint = None
        self._goto_records = goto_records
        super().__init__("signer", template)

    def setup_toolbar(self):
        ui.button("交互式配置", on_click=self.open_interactive).props("outline")

    def setup_footer(self):
        self.record_hint = ui.label("").classes("text-sm text-primary")
        self.record_btn = ui.button(
            "查看签到记录",
            color="primary",
            on_click=self.goto_records,
        ).classes("min-w-[120px]")
        self.record_btn.disable()

    def on_loaded(self, target: str):
        records = load_sign_records(state.workdir)
        has_record = any(r.task == target for r in records)
        if has_record:
            self.record_btn.enable()
            self.record_hint.text = f"发现签到记录: {target}"
        else:
            self.record_btn.disable()
            self.record_hint.text = "无签到记录"
        self.record_hint.update()
        self.record_btn.update()

    def goto_records(self):
        self._goto_records(self.selected_name["value"])

    def open_interactive(self):
        def on_complete():
            self.refresh_options()
            # If the user saved a config with the same name as currently selected, reload it
            if self.select.value:
                self.load_current()

        initial_config = self.editor.properties["content"].get("json")
        initial_name = self.name_input.value or self.select.value or ""

        wizard = InteractiveSignerConfig(
            state.workdir,
            on_complete=on_complete,
            initial_config=initial_config,
            initial_name=initial_name,
        )
        wizard.open()


class MonitorBlock(BaseConfigBlock):
    def __init__(self, template: Dict[str, object]):
        super().__init__("monitor", template)


TASK_HISTORY_FILE = "webui_task_history.json"
TASK_HISTORY_LIMIT = 100


def _task_history_path() -> Path:
    return get_workdir(state.workdir) / TASK_HISTORY_FILE


def load_task_history() -> list[dict]:
    path = _task_history_path()
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_task_history(items: list[dict]) -> None:
    path = _task_history_path()
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(items[-TASK_HISTORY_LIMIT:], fp, ensure_ascii=False, indent=2)


def append_task_history(record: dict) -> None:
    items = load_task_history()
    items.append(record)
    save_task_history(items)


def update_task_history(record: dict) -> None:
    """按记录 id 更新已存在的历史条目并持久化。"""
    items = load_task_history()
    for i, item in enumerate(items):
        if item.get("id") == record.get("id"):
            items[i] = record
            break
    save_task_history(items)


class TaskRunnerBlock:
    """执行任务面板：从 WebUI 直接运行签到/自动化/监控任务"""

    TASK_TYPES = [
        "签到 (run-once)",
        "签到 (run 定时)",
        "自动化 (automation run)",
        "监控 (monitor run)",
    ]

    def __init__(self) -> None:
        # record_id -> {"record": dict, "procs": [...], "queue": Queue, "start_ts": float}
        self.runs: dict[str, dict] = {}
        self.output_lines: list[str] = []

        with ui.card().classes("w-full shadow-md"):
            ui.label("签到").classes("text-lg font-semibold")
            ui.label(
                "点击「新增任务」在弹窗中选择任务类型、任务与账号；历史列表可「复制」任意历史任务的参数快速新建，运行中的任务可停止。"
            ).classes("text-sm text-gray-500")

            with ui.row().classes("gap-2 items-center"):
                ui.button("新增任务", icon="add", color="primary", on_click=self.open_dialog)
                ui.button("清空输出", on_click=self.clear_log).props("outline")

            self.status_label = ui.label("空闲").classes("text-sm text-gray-500")

            # ---- 任务历史 ----
            ui.separator().classes("my-2")
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("任务历史").classes("text-md font-semibold")
                ui.button("清空历史", on_click=self.clear_history).props(
                    "outline dense"
                )
            ui.label(
                "记录通过本页面手动执行过的任务（含类型、账号、状态与结果），保存在工作目录下。"
            ).classes("text-sm text-gray-500")
            self.history_summary = ui.label("").classes("text-sm text-gray-600")
            self.history_table = ui.table(
                columns=[
                    {
                        "name": "time",
                        "label": "开始时间",
                        "field": "time",
                        "align": "left",
                    },
                    {
                        "name": "type",
                        "label": "任务类型",
                        "field": "type",
                        "align": "left",
                    },
                    {
                        "name": "task",
                        "label": "任务名",
                        "field": "task",
                        "align": "left",
                    },
                    {
                        "name": "accounts",
                        "label": "账号",
                        "field": "accounts",
                        "align": "left",
                    },
                    {
                        "name": "status",
                        "label": "状态",
                        "field": "status",
                        "align": "left",
                    },
                    {
                        "name": "result",
                        "label": "结果",
                        "field": "result",
                        "align": "left",
                    },
                    {
                        "name": "actions",
                        "label": "操作",
                        "field": "actions",
                        "align": "left",
                    },
                ],
                rows=[],
                pagination=5,
            ).classes("w-full").props("flat dense")
            # 状态列渲染为彩色徽章：已完成=绿、失败=红、运行中=蓝、其他=灰
            self.history_table.add_slot(
                "body-cell-status",
                '<q-td :props="props">'
                '<q-badge :color="props.row.status === \'已完成\' ? \'positive\' : '
                "props.row.status === '失败' ? 'negative' : "
                "props.row.status === '运行中' ? 'info' : 'grey-6'\">"
                "{{ props.row.status }}</q-badge></q-td>",
            )
            # 结果列：给文字一个固定 DOM id（runres-<任务id>），
            # 运行中进度用 run_javascript 直接改写文本，避免整表重渲染吞掉按钮点击
            self.history_table.add_slot(
                "body-cell-result",
                '<q-td :props="props">'
                '<span :id="\'runres-\' + props.row.id">{{ props.row.result }}</span>'
                "</q-td>",
            )
            # 操作列：所有行显示「复制」「明细」（用该行参数打开新增弹窗/查看子任务明细），运行中的行额外显示「停止」
            self.history_table.add_slot(
                "body-cell-actions",
                '<q-td :props="props">'
                '<q-btn flat dense color="primary" '
                'label="复制" @click="$parent.$emit(\'duplicateTask\', props.row)" />'
                '<q-btn flat dense color="grey-8" '
                'label="明细" @click="$parent.$emit(\'detailTask\', props.row)" />'
                '<q-btn v-if="props.row.status === \'运行中\'" flat dense color="negative" '
                'label="停止" @click="$parent.$emit(\'stopTask\', props.row)" />'
                "</q-td>",
            )
            self.history_table.on("stopTask", self._on_stop_event)
            self.history_table.on("duplicateTask", self._on_duplicate_event)
            self.history_table.on("detailTask", self._on_detail_event)

            self.log_area = ui.scroll_area().classes(
                "w-full bg-gray-50 rounded-lg border border-gray-200"
            )
            self.log_area.style("max-height: 420px")
            with self.log_area:
                self.log_list = (
                    ui.column()
                    .classes("w-full gap-0 p-3 font-mono text-sm")
                    .style("white-space: pre;")
                )

            self._timer = ui.timer(0.3, self._poll_output)
            self._timer.deactivate()

        # ---- 新增任务弹窗 ----
        with ui.dialog() as self.dialog, ui.card().classes("w-full max-w-2xl"):
            ui.label("新增任务").classes("text-lg font-semibold")
            with ui.row().classes("items-end w-full gap-3 flex-wrap"):
                self.type_select = ui.select(
                    label="任务类型",
                    options=self.TASK_TYPES,
                    value=self.TASK_TYPES[0],
                    on_change=self._on_type_change,
                ).classes("min-w-[240px]")

                self.task_select = ui.select(
                    label="选择任务",
                    options=[],
                    with_input=True,
                ).classes("min-w-[200px]")

                self.account_select = ui.select(
                    label="选择账号（可多选）",
                    options=[],
                    multiple=True,
                ).classes("min-w-[240px]").props("use-chips")
            ui.label(
                "账号来自 *.session 文件；多选时每个账号各启动一个进程。"
            ).classes("text-xs text-gray-500")
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("取消", on_click=self.dialog.close).props("outline")
                ui.button("执行", color="primary", on_click=self.start_task)

    def _get_task_names(self) -> list[str]:
        task_type = self.type_select.value or ""
        workdir = state.workdir
        if "签到" in task_type:
            return list_task_names("signer", workdir)
        elif "自动化" in task_type:
            root = get_workdir(workdir) / "automations"
            if not root.is_dir():
                return []
            return sorted(p.name for p in root.iterdir() if p.is_dir())
        elif "监控" in task_type:
            return list_task_names("monitor", workdir)
        return []

    def _get_account_names(self) -> list[str]:
        """账号来自 session 文件（与 CLI 默认 session_dir='.' 一致），另附加 TG_ACCOUNT 环境变量；
        排除停用账户，按账户管理中拖动的顺序排列。"""
        names = sorted(p.stem for p in Path(".").glob("*.session"))
        env_account = os.environ.get("TG_ACCOUNT")
        if env_account and env_account not in names:
            names.append(env_account)
        disabled = load_disabled_accounts(state.workdir)
        return apply_account_order(
            [n for n in names if n not in disabled], state.workdir
        )

    def _refresh_accounts(self) -> None:
        options = self._get_account_names()
        self.account_select.options = options
        if self.account_select.value:
            self.account_select.value = [
                v for v in self.account_select.value if v in options
            ]
        self.account_select.update()

    def _refresh_history_table(self) -> None:
        items = load_task_history()
        counts: dict[str, int] = {s: 0 for s in ("运行中", "已完成", "失败", "已停止")}
        for r in items:
            status = r.get("status", "未知")
            if status in counts:
                counts[status] += 1
        summary = f"总任务数 {len(items)} | 运行中 {counts['运行中']} | 已完成 {counts['已完成']} | 失败 {counts['失败']} | 已停止 {counts['已停止']}"
        self.history_summary.text = summary
        self.history_summary.update()

        def _id_key(r: dict) -> int:
            v = str(r.get("id") or "")
            return int(v) if v.isdigit() else 0

        rows = [
            {
                "id": r.get("id", ""),
                "time": r.get("start_time", ""),
                "type": r.get("task_type", ""),
                "task": r.get("task_name", ""),
                "accounts": r.get("accounts", ""),
                "status": r.get("status", ""),
                "result": r.get("result", ""),
            }
            for r in sorted(items, key=_id_key, reverse=True)
        ]
        self.history_table.rows = rows
        self.history_table.update()

    def clear_history(self) -> None:
        save_task_history([])
        self._refresh_history_table()
        ui.notify("已清空任务历史", type="positive")

    def open_dialog(self) -> None:
        self._on_type_change()
        self._refresh_accounts()
        # 默认全选发现的账号
        if not self.account_select.value and self.account_select.options:
            self.account_select.value = list(self.account_select.options)
            self.account_select.update()
        self.dialog.open()

    def _on_type_change(self) -> None:
        self.task_select.options = self._get_task_names()
        # 只有一个任务时默认选中，否则清空选择
        if len(self.task_select.options) == 1:
            self.task_select.value = self.task_select.options[0]
        else:
            self.task_select.value = None
        self.task_select.update()

    def _build_args(self, account: str) -> list[str]:
        task_type = self.type_select.value
        task_name = self.task_select.value
        workdir = str(state.workdir)
        # 根命令选项（-w/-a）必须位于子命令之前
        if "run-once" in task_type:
            return ["-w", workdir, "-a", account, "run-once", task_name]
        elif "run 定时" in task_type:
            return ["-w", workdir, "-a", account, "run", task_name]
        elif "自动化" in task_type:
            return ["-w", workdir, "-a", account, "automation", "run", task_name]
        elif "监控" in task_type:
            return ["-w", workdir, "-a", account, "monitor", "run", task_name]
        return []

    def start_task(self) -> None:
        task_name = self.task_select.value
        if not task_name:
            ui.notify("请先选择任务", type="warning")
            return
        accounts = [a for a in (self.account_select.value or []) if a]
        if not accounts:
            ui.notify("请至少选择一个账号", type="warning")
            return
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SOURCE_ROOT)
        record = {
            "id": f"{time.time_ns()}",
            "start_time": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
            "task_type": self.type_select.value or "",
            "task_name": task_name,
            "accounts": ", ".join(accounts),
            "status": "运行中",
            "result": "",
        }
        procs: list[dict] = []
        run_queue: queue.Queue = queue.Queue()
        chats_labels = self._chat_labels(task_name)
        detail_rows: list[dict] = []
        for account in accounts:
            args = self._build_args(account)
            if not args:
                ui.notify(f"无法构建命令: {account}", type="warning")
                continue
            cmd = [
                sys.executable,
                "-c",
                "from tg_signer.cli import tg_signer; tg_signer()",
            ] + args
            # 每账号进程独立统计聊天级进度：started=已开始的聊天数，failed=签到失败数
            stats = {"started": 0, "failed": 0, "current_pos": None}
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )
            except Exception as exc:
                notify_error(exc)
                continue
            reader = threading.Thread(
                target=self._reader,
                args=(proc, task_name, account, run_queue, stats, chats_labels, detail_rows),
                daemon=True,
            )
            reader.start()
            procs.append({"proc": proc, "reader": reader, "account": account, "stats": stats})
        if not procs:
            ui.notify("未能启动任务", type="negative")
            return
        record["accounts"] = ", ".join(i["account"] for i in procs)
        self.runs[record["id"]] = {
            "record": record,
            "procs": procs,
            "queue": run_queue,
            "start_ts": time.time(),
            "total_chats": len(chats_labels) * len(procs),
            "details": detail_rows,
        }
        append_task_history(record)
        self._refresh_history_table()
        self._timer.activate()
        self._update_status()
        self.dialog.close()
        ui.notify(f"已启动: {task_name}（{len(procs)} 个账号）", type="positive")

    def _reader(
        self,
        proc: subprocess.Popen,
        task_name: str,
        account: str,
        q: "queue.Queue",
        stats: dict,
        chats_labels: list,
        detail_rows: list,
    ) -> None:
        prefix = f"[{task_name}:{account}] "
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            stripped = line.lstrip()
            # CLI 日志带格式前缀（[INFO] [name] 时间 ... 账户「x」- 任务「y」: 开始执行: ...），用子串匹配
            if "开始执行" in stripped:
                # 上一轮聊天结束，视为成功
                pos = stats["current_pos"]
                if pos is not None and detail_rows[pos]["status"] == "进行中":
                    detail_rows[pos]["status"] = "成功"
                # 按配置中的聊天顺序轮转取标签
                if chats_labels:
                    label = chats_labels[stats["started"] % len(chats_labels)]
                else:
                    label = f"第{stats['started'] + 1}个聊天"
                detail_rows.append({"account": account, "chat": label, "status": "进行中"})
                stats["current_pos"] = len(detail_rows) - 1
                stats["started"] += 1
            elif "签到失败" in stripped:
                stats["failed"] += 1
                pos = stats["current_pos"]
                if pos is not None and detail_rows[pos]["status"] == "进行中":
                    detail_rows[pos]["status"] = "失败"
            q.put(prefix + line.rstrip("\r\n"))

    def _chat_labels(self, task_name: str) -> list[str]:
        """读取签到配置的聊天标签（名称+chat_id），读取失败返回空列表。"""
        try:
            entry = load_config("signer", task_name, workdir=state.workdir)
            labels = []
            for c in entry.payload.get("chats") or []:
                name = str(c.get("name") or "").strip()
                labels.append(f"{name}({c.get('chat_id')})" if name else str(c.get("chat_id")))
            return labels
        except Exception:
            return []

    def _format_progress(self, run: dict) -> str:
        """根据各账号进程的日志统计生成进度文本。"""
        total = run.get("total_chats") or 0
        started = failed = completed = 0
        for item in run["procs"]:
            s = item["stats"]
            started += s["started"]
            failed += s["failed"]
            # 存活进程当前必有 1 个聊天进行中，不计入已完成
            alive = item["proc"].poll() is None
            completed += max(0, s["started"] - (1 if alive else 0))
        ok = max(0, completed - failed)
        task_type = run["record"].get("task_type", "")
        if total > 0 and "run-once" in task_type:
            return f"进度 {ok}/{total} · 失败 {failed}"
        if started > 0:
            return f"已完成 {ok} 个聊天 · 失败 {failed}"
        return ""

    def _on_stop_event(self, e) -> None:
        row = e.args
        if isinstance(row, list) and row and isinstance(row[0], dict):
            row = row[0]
        if isinstance(row, dict) and row.get("id"):
            self._stop_run(str(row["id"]))

    def _on_detail_event(self, e) -> None:
        """查看某条任务记录的子任务（账号 × 聊天）成功/失败明细"""
        row = e.args
        if isinstance(row, list) and row and isinstance(row[0], dict):
            row = row[0]
        if not isinstance(row, dict):
            return
        rid = str(row.get("id") or "")
        details: list[dict] = []
        if rid in self.runs:
            details = list(self.runs[rid]["details"])
        else:
            for r in load_task_history():
                if str(r.get("id")) == rid:
                    details = list(r.get("details") or [])
                    break
        counts = {"成功": 0, "失败": 0, "进行中": 0, "中断": 0}
        for d in details:
            counts[d.get("status", "中断")] = counts.get(d.get("status", "中断"), 0) + 1
        accounts = sorted({d.get("account", "") for d in details if d.get("account")})
        acc_stats = {
            acc: {
                "total": len([d for d in details if d.get("account", "") == acc]),
                "failed": len(
                    [
                        d
                        for d in details
                        if d.get("account", "") == acc and d.get("status") == "失败"
                    ]
                ),
            }
            for acc in accounts
        }
        with ui.dialog() as dlg, ui.card().classes("w-full max-w-xl"):
            ui.label(f"子任务明细 — {row.get('task', '')}").classes("text-lg font-semibold")
            ui.label(
                f"共 {len(details)} 个子任务 | 成功 {counts['成功']} · 失败 {counts['失败']}"
                f" · 进行中 {counts['进行中']} · 中断 {counts['中断']}"
            ).classes("text-sm text-gray-600")
            # 分账号失败统计：点击可直接筛选该账号
            if acc_stats:
                with ui.row().classes("w-full gap-1 flex-wrap items-center"):
                    ui.label("分账号：").classes("text-xs text-gray-500")

                    def set_acc(acc: str) -> None:
                        account_sel.value = acc
                        account_sel.update()

                    for acc in accounts:
                        st = acc_stats[acc]
                        ui.button(
                            f"{acc}（失败 {st['failed']}/{st['total']}）",
                            on_click=lambda a=acc: set_acc(a),
                        ).props("flat dense size=sm color=grey-8")
            with ui.row().classes("w-full gap-2 items-center"):
                status_sel = ui.select(
                    ["失败", "成功", "进行中", "中断", "全部"],
                    value="失败",
                    label="状态筛选",
                    on_change=lambda: render(),
                ).classes("min-w-[130px]")
            # 账号筛选用 radio button（横向排列），点选即切换
            account_sel = ui.radio(
                ["全部账号"] + accounts,
                value="全部账号",
                on_change=lambda: render(),
            ).props("inline dense").classes("w-full flex-wrap gap-x-4 gap-y-1 text-sm")
            count_label = ui.label("").classes("text-sm text-gray-500")
            with ui.scroll_area().classes("w-full max-h-96 border rounded-lg"):
                list_container = ui.column().classes("w-full gap-0 p-1")

            def render() -> None:
                st = status_sel.value
                acc = account_sel.value
                filtered = [
                    d
                    for d in details
                    if (st == "全部" or d.get("status", "") == st)
                    and (acc == "全部账号" or d.get("account", "") == acc)
                ]
                count_label.text = f"筛选后 {len(filtered)} 条"
                count_label.update()
                list_container.clear()
                with list_container:
                    if not filtered:
                        ui.label("无匹配的子任务").classes("text-sm text-gray-500 p-2")
                    for d in filtered:
                        status = d.get("status", "")
                        color = {
                            "成功": "text-green-700",
                            "失败": "text-red-700",
                            "进行中": "text-blue-700",
                        }.get(status, "text-gray-500")
                        mark = {"成功": "✓", "失败": "✗", "进行中": "…"}.get(status, "−")
                        ui.label(
                            f"{mark} {d.get('account', '')} → {d.get('chat', '')}：{status}"
                        ).classes(f"text-sm py-0.5 {color}").style("white-space: pre;")
                list_container.update()

            render()
            with ui.row().classes("w-full justify-end"):
                ui.button("关闭", on_click=dlg.close).props("flat")
        dlg.open()

    def _on_duplicate_event(self, e) -> None:
        """用历史行的参数打开「新增任务」弹窗"""
        row = e.args
        if isinstance(row, list) and row and isinstance(row[0], dict):
            row = row[0]
        if not isinstance(row, dict):
            return
        if row.get("type") in self.TASK_TYPES:
            self.type_select.value = row["type"]
            self.type_select.update()
        self._on_type_change()
        task = row.get("task") or ""
        if task:
            if task not in self.task_select.options:
                self.task_select.options = list(self.task_select.options) + [task]
            self.task_select.value = task
            self.task_select.update()
        accounts = [a.strip() for a in (row.get("accounts") or "").split(",") if a.strip()]
        if accounts:
            self._refresh_accounts()
            valid = [a for a in accounts if a in (self.account_select.options or [])]
            self.account_select.value = valid or accounts
            self.account_select.update()
        self.dialog.open()
        ui.notify("已复制任务参数，可调整后点击「执行」", type="positive")

    def _stop_run(self, record_id: str) -> None:
        run = self.runs.get(record_id)
        if run is None:
            return
        for item in run["procs"]:
            proc = item["proc"]
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        self.runs.pop(record_id, None)
        self._close_pending_details(run, "中断")
        record = run["record"]
        record["status"] = "已停止"
        record["details"] = list(run["details"])
        record["result"] = (
            f"手动停止，耗时 {self._format_duration_ts(run['start_ts'])}"
        )
        update_task_history(record)
        self._refresh_history_table()
        self._update_status()
        ui.notify(f"已停止: {record['task_name']}", type="positive")

    def _update_running_row(self, record_id: str, result_text: str) -> None:
        # 只同步内存行数据；进度文本用 JS 直接改写单元格（固定 DOM id），
        # 不调用 history_table.update()，避免每 0.3s 整表重渲染导致「明细/停止」点击丢失
        for row in self.history_table.rows:
            if row.get("id") == record_id:
                row["result"] = result_text
                break
        safe = (
            result_text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        )
        ui.run_javascript(
            f"var el = document.getElementById('runres-{record_id}');"
            f"if (el) el.textContent = '{safe}';"
        )

    def _close_pending_details(self, run: dict, default_status: str) -> None:
        """进程结束后，把仍在「进行中」的子任务标记为最终状态。"""
        for item in run["procs"]:
            pos = item["stats"].get("current_pos")
            rows = run["details"]
            if pos is not None and pos < len(rows) and rows[pos]["status"] == "进行中":
                if default_status == "自动":
                    rc = item["proc"].returncode
                    rows[pos]["status"] = "成功" if rc == 0 else "失败"
                else:
                    rows[pos]["status"] = default_status

    def _finalize_run(self, record_id: str) -> None:
        run = self.runs.pop(record_id)
        rcs = ", ".join(str(i["proc"].returncode) for i in run["procs"])
        ok = all(i["proc"].returncode == 0 for i in run["procs"])
        self._close_pending_details(run, "自动")
        record = run["record"]
        record["status"] = "已完成" if ok else "失败"
        record["details"] = list(run["details"])
        progress = self._format_progress(run)
        parts = []
        if progress:
            parts.append(progress)
        parts.append(f"返回码: {rcs}")
        parts.append(f"耗时 {self._format_duration_ts(run['start_ts'])}")
        record["result"] = "，".join(parts)
        update_task_history(record)
        self._refresh_history_table()

    @staticmethod
    def _format_duration_ts(start_ts: float) -> str:
        seconds = max(0, int(time.time() - start_ts))
        if seconds >= 60:
            return f"{seconds // 60}分{seconds % 60}秒"
        return f"{seconds}秒"

    def _update_status(self) -> None:
        if not self.runs:
            self.status_label.text = "空闲"
        else:
            parts = []
            for run in self.runs.values():
                rec = run["record"]
                parts.append(
                    f"{rec['task_name']}（{rec['accounts']}）已运行 "
                    f"{self._format_duration_ts(run['start_ts'])}"
                )
            self.status_label.text = (
                f"运行中 {len(self.runs)} 个任务: " + "; ".join(parts)
            )
        self.status_label.update()

    def _poll_output(self) -> None:
        for run in self.runs.values():
            while not run["queue"].empty():
                self.output_lines.append(run["queue"].get())
        self.log_list.clear()
        with self.log_list:
            for line in self.output_lines[-200:]:
                color = self._classify_line(line)
                ui.label(line).classes(f"w-full {color}").style(
                    "white-space: pre;"
                )
        self.log_list.update()

        finished_ids: list[str] = []
        for run_id, run in list(self.runs.items()):
            if all(i["proc"].poll() is not None for i in run["procs"]):
                if any(i["reader"].is_alive() for i in run["procs"]):
                    continue
                finished_ids.append(run_id)
            else:
                elapsed = f"已运行 {self._format_duration_ts(run['start_ts'])}"
                progress = self._format_progress(run)
                self._update_running_row(
                    run_id, f"{elapsed} · {progress}" if progress else elapsed
                )
        for run_id in finished_ids:
            self._finalize_run(run_id)
        self._update_status()
        if not self.runs:
            self._timer.deactivate()

    def clear_log(self) -> None:
        self.output_lines = []
        self.log_list.clear()
        self.log_list.update()

    @staticmethod
    def _classify_line(line: str) -> str:
        upper = line.upper()
        if "ERROR" in upper:
            return "text-red-700"
        if "WARN" in upper:
            return "text-amber-700"
        if "INFO" in upper:
            return "text-blue-700"
        return "text-gray-800"

    def __call__(self, *args, **kwargs):
        self._on_type_change()
        self._refresh_accounts()
        self._refresh_history_table()
        self._update_status()


class AccountLoginSession:
    """在页面上执行 tg-signer CLI 登录：连接 → 发送验证码 → 登录（可选两步验证密码）。

    使用文件模式 Client（与 CLI 一致），登录成功后自动落盘 <账户名>.session。
    """

    def __init__(self, name: str, phone: str) -> None:
        self.name = (name or "").strip()
        self.phone = (phone or "").strip()
        self.client = None
        self.phone_code_hash = None
        self.password_needed = False

    def _check_basic(self) -> None:
        if not self.name:
            raise ValueError("请填写账户名")
        if not self.phone:
            raise ValueError("请填写手机号（国际格式，如 +8613800138000）")

    @staticmethod
    def _save_me(me) -> None:
        data = {
            "id": me.id,
            "first_name": me.first_name,
            "last_name": me.last_name,
            "username": me.username,
            "phone_number": me.phone_number,
            "is_premium": getattr(me, "is_premium", None),
        }
        user_dir = get_workdir(state.workdir) / "users" / str(me.id)
        user_dir.mkdir(parents=True, exist_ok=True)
        with open(user_dir / "me.json", "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)

    async def start(self) -> str:
        """连接并发送验证码；若会话已授权则直接完成登录。"""
        self._check_basic()
        from pyrogram import Client

        api_id, api_hash = get_api_config()
        self.client = Client(
            self.name,
            api_id=api_id,
            api_hash=api_hash,
            proxy=get_proxy(),
            workdir=".",
        )
        authorized = await asyncio.wait_for(self.client.connect(), 60)
        if authorized:
            me = await asyncio.wait_for(self.client.get_me(), 30)
            self._save_me(me)
            await self.client.disconnect()
            self.client = None
            return f"该账户已登录：{me.first_name or me.id}"
        sent = await asyncio.wait_for(
            self.client.send_phone_number_code(self.phone), 60
        )
        self.phone_code_hash = sent.phone_code_hash
        return "验证码已发送，请查看 Telegram 官方消息"

    async def complete(self, code: str, password: str) -> str:
        """提交验证码（及可选的两步验证密码）完成登录。"""
        if self.client is None or self.phone_code_hash is None:
            raise ValueError("请先发送验证码")
        from pyrogram.errors import SessionPasswordNeeded

        try:
            if not self.password_needed:
                code = (code or "").strip()
                if not code:
                    raise ValueError("请填写验证码")
                await asyncio.wait_for(
                    self.client.sign_in(self.phone, self.phone_code_hash, code), 60
                )
            else:
                password = (password or "").strip()
                if not password:
                    raise ValueError("请填写两步验证密码")
                await asyncio.wait_for(self.client.check_password(password), 60)
        except SessionPasswordNeeded:
            self.password_needed = True
            raise ValueError("该账户开启了两步验证，请填写密码后再次点击登录")
        me = await asyncio.wait_for(self.client.get_me(), 30)
        self._save_me(me)
        await self.client.disconnect()
        self.client = None
        # 清掉该账户可能残留的旧客户端缓存，避免后续任务使用过期会话
        from tg_signer.core import _CLIENT_INSTANCES

        _CLIENT_INSTANCES.pop(str(Path(".").joinpath(self.name).resolve()), None)
        return str(me.first_name or me.id)

    async def cancel(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        self.phone_code_hash = None
        self.password_needed = False


# 账户卡片拖动排序初始化脚本：以 [data-account] 卡片的父元素（账户网格）为 Sortable 容器。
# 页面构建和每次打开用户信息弹窗时都会执行，_sortableInit 标记防止重复初始化。
ACCOUNT_SORTABLE_INIT_JS = """
(function() {
    function init() {
        if (!window.Sortable) { setTimeout(init, 400); return; }
        var card = document.querySelector('[data-account]');
        if (!card) return;
        var el = card.parentElement;
        if (!el || el._sortableInit) return;
        el._sortableInit = true;
        new Sortable(el, {
            animation: 150,
            draggable: '[data-account]',
            onEnd: function() {
                var order = Array.from(el.querySelectorAll('[data-account]'))
                    .map(function(c) { return c.getAttribute('data-account'); })
                    .filter(Boolean);
                emitEvent('account_reordered', { order: order });
            }
        });
        el.setAttribute('data-sortable-ready', '1');
    }
    init();
})();
"""


def user_info_block() -> Callable[[], None]:
    container = ui.column().classes("w-full gap-2")

    # 拖动排序依赖 SortableJS（CDN 加载失败时仅拖动不可用，其余功能不受影响）
    ui.add_head_html(
        '<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.6/Sortable.min.js">'
        "</script>"
    )

    # 增加账户弹窗（CLI 登录流程，只创建一次，refresh 清空 container 不影响它）
    login_session: dict = {"session": None}

    def _reset_login_dialog() -> None:
        async def _cancel() -> None:
            session = login_session.get("session")
            if session is not None:
                await session.cancel()
            login_session["session"] = None

        asyncio.ensure_future(_cancel())
        add_name.value = ""
        add_phone.value = ""
        add_code.value = ""
        add_password.value = ""
        add_status.text = ""
        add_status.classes(replace="text-sm text-gray-500")
        step2.visible = False
        add_password.visible = False
        send_btn.enable()
        login_btn.enable()

    with ui.dialog() as add_dlg, ui.card().classes("w-[520px] max-w-full"):
        ui.label("增加账户（TG 登录）").classes("text-lg font-semibold")
        add_name = ui.input("账户名", placeholder="例如 my_account").classes("w-full")
        add_phone = ui.input(
            "手机号", placeholder="国际格式，如 +8613800138000"
        ).classes("w-full")
        add_status = ui.label("").classes("text-sm text-gray-500")

        async def do_send_code() -> None:
            add_status.classes(replace="text-sm text-orange-600")
            add_status.text = "正在连接 Telegram 并发送验证码..."
            add_status.update()
            send_btn.disable()
            try:
                session = login_session.get("session")
                if session is None:
                    session = AccountLoginSession(add_name.value, add_phone.value)
                    login_session["session"] = session
                msg = await session.start()
                add_status.classes(replace="text-sm text-positive")
                add_status.text = msg
                step2.visible = True
            except Exception as exc:  # noqa: BLE001
                add_status.classes(replace="text-sm text-negative")
                add_status.text = f"发送失败：{exc}"
                send_btn.enable()
            add_status.update()

        async def do_login() -> None:
            session = login_session.get("session")
            if session is None:
                add_status.classes(replace="text-sm text-negative")
                add_status.text = "请先发送验证码"
                add_status.update()
                return
            login_btn.disable()
            add_status.classes(replace="text-sm text-orange-600")
            add_status.text = "正在登录..."
            add_status.update()
            try:
                who = await session.complete(add_code.value, add_password.value)
            except ValueError as exc:
                add_status.classes(replace="text-sm text-negative")
                add_status.text = str(exc)
                if "两步验证" in str(exc):
                    add_password.visible = True
                    add_password.update()
                login_btn.enable()
                add_status.update()
                return
            except Exception as exc:  # noqa: BLE001
                add_status.classes(replace="text-sm text-negative")
                add_status.text = f"登录失败：{exc}"
                login_btn.enable()
                add_status.update()
                return
            ui.notify(f"账户登录成功：{who}", type="positive")
            add_dlg.close()
            _reset_login_dialog()
            refresh()

        with ui.row().classes("w-full gap-2"):
            send_btn = ui.button("发送验证码", on_click=do_send_code).props("outline")
            login_btn = ui.button("登录", color="primary", on_click=do_login)

        with ui.column().classes("w-full") as step2:
            step2.visible = False
            add_code = ui.input("验证码", placeholder="Telegram 发送的验证码").classes(
                "w-full"
            )
            add_password = ui.input(
                "两步验证密码",
                placeholder="仅开启了两步验证的账户需要填写",
                password=True,
            ).props("password-icon-toggle-password-visibility").classes("w-full")
            add_password.visible = False

    def toggle_account(name: str, enabled: bool) -> None:
        set_account_enabled(name, enabled, state.workdir)
        ui.notify(f"账户 {name} 已{'启用' if enabled else '停用'}", type="positive")

    async def on_account_reordered(e) -> None:
        """拖动排序结束：校验并持久化新顺序。"""
        order = e.args.get("order") if isinstance(e.args, dict) else e.args
        if not isinstance(order, list):
            return
        current = [p.stem for p in Path(".").glob("*.session")]
        env_account = os.environ.get("TG_ACCOUNT")
        if env_account and env_account not in current:
            current.append(env_account)
        if set(order) != set(current):
            return
        set_account_order([str(n) for n in order], state.workdir)
        ui.notify("账户顺序已保存", type="positive")
        refresh()

    ui.on("account_reordered", on_account_reordered)

    def refresh() -> None:
        container.clear()
        disabled = load_disabled_accounts(state.workdir)
        with container:
            # —— 账户管理 ——
            with ui.card().classes("w-full shadow-sm"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("账户管理").classes("text-lg font-semibold")
                        ui.icon("drag_indicator").classes("text-gray-400").tooltip(
                            "拖动卡片可调整顺序，保存后各页面账号下拉也按此顺序显示"
                        )
                    ui.button(
                        "增加账户",
                        icon="person_add",
                        on_click=lambda: (_reset_login_dialog(), add_dlg.open()),
                    ).props("outline")
                names = sorted(p.stem for p in Path(".").glob("*.session"))
                env_account = os.environ.get("TG_ACCOUNT")
                if env_account and env_account not in names:
                    names.append(env_account)
                names = apply_account_order(names, state.workdir)
                if not names:
                    ui.label("未发现账户（当前目录无 *.session）").classes(
                        "text-sm text-gray-500"
                    )
                else:
                    identities = account_identities(names, workdir=state.workdir)
                    # 同一 Telegram 账号可能有多份 session，聚合用于重复标注
                    ids_by_uid: dict = {}
                    for n, info in identities.items():
                        ids_by_uid.setdefault(info["user_id"], []).append(n)

                    def delete_account(name: str) -> None:
                        with ui.dialog() as dlg, ui.card():
                            ui.label(f"确认删除账户 {name}？").classes(
                                "text-lg font-semibold"
                            )
                            ui.label(
                                "将删除该账户的 .session / .session_string 文件，不可恢复！"
                                "删除前请先停止使用该账户的任务。"
                            ).classes("text-sm text-red-600")
                            with ui.row().classes("w-full justify-end"):
                                ui.button("取消", on_click=dlg.close).props("flat")

                                def do_delete(n: str = name) -> None:
                                    errs = []
                                    for suffix in (".session", ".session_string"):
                                        try:
                                            Path(n + suffix).unlink(missing_ok=True)
                                        except Exception as exc:
                                            errs.append(str(exc))
                                    dlg.close()
                                    if errs:
                                        ui.notify(
                                            f"删除失败（文件可能被运行中的连接占用）: {errs[0]}",
                                            type="negative",
                                        )
                                        return
                                    from tg_signer.core import _CLIENT_INSTANCES

                                    _CLIENT_INSTANCES.pop(
                                        str(Path(".").joinpath(n).resolve()), None
                                    )
                                    ui.notify(f"账户 {n} 已删除", type="positive")
                                    refresh()

                                ui.button("确认删除", on_click=do_delete).props(
                                    "color=negative"
                                )
                        dlg.open()

                    def rename_account(name: str) -> None:
                        with ui.dialog() as dlg, ui.card().classes("w-[420px] max-w-full"):
                            ui.label(f"重命名账户 {name}").classes("text-lg font-semibold")
                            ui.label(
                                "将重命名 .session / .session_string 文件，"
                                "并同步更新顺序与停用记录。重命名前请先停止使用该账户的任务。"
                            ).classes("text-sm text-gray-500")
                            new_name = ui.input("新账户名", value=name).classes("w-full")
                            err = ui.label("").classes("text-sm text-negative")

                            def do_rename() -> None:
                                target = new_name.value.strip()
                                if not target or target == name:
                                    err.text = "请输入与当前不同的新账户名"
                                    err.update()
                                    return
                                if target in (".", "..") or any(
                                    ch in target for ch in '/\\:*?"<>|'
                                ):
                                    err.text = "账户名包含非法字符"
                                    err.update()
                                    return
                                if Path(target + ".session").exists() or Path(
                                    target + ".session_string"
                                ).exists():
                                    err.text = f"账户 {target} 已存在"
                                    err.update()
                                    return
                                errs = []
                                for suffix in (".session", ".session_string"):
                                    src = Path(name + suffix)
                                    if src.exists():
                                        try:
                                            src.rename(Path(target + suffix))
                                        except Exception as exc:
                                            errs.append(str(exc))
                                if errs:
                                    err.text = (
                                        f"重命名失败（文件可能被运行中的连接占用）: {errs[0]}"
                                    )
                                    err.update()
                                    return
                                rename_account_state(name, target, state.workdir)
                                from tg_signer.core import _CLIENT_INSTANCES

                                _CLIENT_INSTANCES.pop(
                                    str(Path(".").joinpath(name).resolve()), None
                                )
                                dlg.close()
                                ui.notify(
                                    f"账户 {name} 已重命名为 {target}", type="positive"
                                )
                                refresh()

                            with ui.row().classes("w-full justify-end"):
                                ui.button("取消", on_click=dlg.close).props("flat")
                                ui.button(
                                    "确认重命名", on_click=do_rename
                                ).props("color=primary")
                        dlg.open()

                    with ui.grid(columns=3).classes("w-full gap-2") as account_grid:
                        for name in names:
                            with ui.column().classes(
                                "border rounded p-2 gap-1 cursor-move"
                            ).props(f'data-account="{name}"'):
                                with ui.row().classes("w-full items-center justify-between"):
                                    ui.switch(
                                        name,
                                        value=name not in disabled,
                                        on_change=lambda e, n=name: toggle_account(
                                            n, e.value
                                        ),
                                    )
                                    ui.button(
                                        icon="edit",
                                        on_click=lambda n=name: rename_account(n),
                                    ).props("flat round dense").tooltip("重命名账户")
                                    ui.button(
                                        icon="delete",
                                        on_click=lambda n=name: delete_account(n),
                                    ).props("flat round dense color=negative").tooltip(
                                        "删除账户"
                                    )
                                info = identities.get(name)
                                if info:
                                    parts = []
                                    if info["name"]:
                                        parts.append(f"→ {info['name']}")
                                    if info["username"]:
                                        parts.append(f"@{info['username']}")
                                    parts.append(f"ID {info['user_id']}")
                                    text = " ".join(parts)
                                    dups = [
                                        n
                                        for n in ids_by_uid.get(info["user_id"], [])
                                        if n != name
                                    ]
                                    if dups:
                                        text += f"（与 {'、'.join(dups)} 为同一账号）"
                                    ui.label(text).classes("text-xs text-gray-500")
                                else:
                                    ui.label(
                                        "→ 未获取到 Telegram 身份（未登录或缺少用户信息）"
                                    ).classes("text-xs text-gray-400")
                ui.label(
                    "停用的账户不会出现在签到/随机发言/批量退频道的账号下拉列表中"
                ).classes("text-xs text-gray-500")

                # 初始化拖动排序（SortableJS），拖完把新顺序发回服务端持久化
                # 注意：不用 getElementById（NiceGUI 元素 id 不一定等于 DOM id），
                # 直接以 [data-account] 卡片的父元素（账户网格）作为 Sortable 容器
                ui.run_javascript(
                    """
                    (function() {
                        function init() {
                            if (!window.Sortable) { setTimeout(init, 400); return; }
                            var card = document.querySelector('[data-account]');
                            if (!card) return;
                            var el = card.parentElement;
                            if (!el || el._sortableInit) return;
                            el._sortableInit = true;
                            new Sortable(el, {
                                animation: 150,
                                draggable: '[data-account]',
                                onEnd: function() {
                                    var order = Array.from(el.querySelectorAll('[data-account]'))
                                        .map(function(c) { return c.getAttribute('data-account'); })
                                        .filter(Boolean);
                                    emitEvent('account_reordered', { order: order });
                                }
                            });
                            el.setAttribute('data-sortable-ready', '1');
                        }
                        init();
                    })();
                    """
                )

            # —— 用户信息 ——
            entries = load_user_infos(state.workdir)
            if not entries:
                ui.label("未找到用户信息").classes("text-gray-500")
                return
            for entry in entries:
                name = entry.data.get("first_name") or ""
                header = f"{entry.user_id} {name}".strip()
                with ui.expansion(header, icon="person"):
                    ui.label(f"文件: {entry.path}")
                    ui.code(pretty_json(entry.data), language="json").classes("w-full")

                    if entry.latest_chats:
                        ui.separator().classes("my-2")
                        ui.label(f"最近聊天 ({len(entry.latest_chats)})").classes(
                            "font-semibold"
                        )

                        chat_rows = []
                        for chat in entry.latest_chats:
                            chat_rows.append(
                                {
                                    "id": chat.get("id"),
                                    "title": chat.get("title")
                                    or chat.get("first_name")
                                    or "N/A",
                                    "type": chat.get("type"),
                                    "username": chat.get("username") or "",
                                }
                            )

                        ui.table(
                            columns=[
                                {
                                    "name": "id",
                                    "label": "ID",
                                    "field": "id",
                                    "align": "left",
                                },
                                {
                                    "name": "title",
                                    "label": "名称",
                                    "field": "title",
                                    "align": "left",
                                },
                                {
                                    "name": "type",
                                    "label": "类型",
                                    "field": "type",
                                    "align": "left",
                                },
                                {
                                    "name": "username",
                                    "label": "用户名",
                                    "field": "username",
                                    "align": "left",
                                },
                            ],
                            rows=chat_rows,
                            pagination=10,
                        ).classes("w-full").props("flat dense")
                    else:
                        ui.label("未找到最近聊天记录").classes(
                            "text-gray-500 text-sm mt-2"
                        )

    return refresh


class SignRecordBlock:
    def __init__(self):
        self.container = ui.column().classes("w-full gap-3")
        with ui.row().classes("items-end gap-3"):
            self.filter_input = ui.input(
                label="筛选任务/用户",
                placeholder="输入任务名或用户ID过滤",
                value=state.record_filter,
                on_change=lambda e: self._update_filter(e.value),
            ).classes("w-full")
            ui.button("清除筛选", on_click=lambda: self._update_filter("")).props(
                "outline"
            )
        self.status = ui.label("").classes("text-sm text-gray-500")

    def _update_filter(self, value: str) -> None:
        state.record_filter = value or ""
        self.refresh()

    def refresh(
        self,
    ) -> None:
        self.container.clear()
        records = load_sign_records(state.workdir)
        keyword = (state.record_filter or "").lower().strip()
        if keyword:
            records = [
                r
                for r in records
                if keyword in r.task.lower()
                or (r.user_id and keyword in str(r.user_id).lower())
            ]
        with self.container:
            if not records:
                self.status.text = "未找到匹配的签到记录" if keyword else "尚无签到记录"
                self.status.update()
                return
            self.status.text = f"共 {len(records)} 组记录"
            self.status.update()
            for record in records:
                user_text = record.user_id or "默认"
                header = f"{record.task} / {user_text}（{len(record.records)}条）"
                with ui.expansion(header, icon="event").classes("shadow-sm"):
                    ui.label(f"来源: {record.path}").classes("text-gray-500")
                    if not record.records:
                        ui.label("暂无记录").classes("text-gray-500")
                        continue
                    rows = [{"日期": k, "时间": v} for k, v in record.records]
                    ui.table(
                        columns=[
                            {"name": "日期", "label": "日期", "field": "日期"},
                            {"name": "时间", "label": "时间", "field": "时间"},
                        ],
                        rows=rows,
                    ).classes("w-full").props("flat dense")

    def __call__(self, *args, **kwargs):
        return self.refresh()


def log_block() -> Callable[[], None]:
    with ui.card().classes("w-full shadow-sm"):
        ui.label("日志查看").classes("text-md font-semibold")
        ui.label("查看最新日志行，可自定义文件路径和行数。").classes(
            "text-sm text-gray-500 mb-1"
        )

        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
            limit_input = ui.number(
                label="日志行数",
                value=state.log_limit,
                min=10,
                max=2000,
                format="%d",
            ).classes("w-32")
            log_select = ui.select(
                label=f"选择日志文件（{LOG_DIR}/）",
                options=[],
                on_change=lambda e: select_log_file(e.value),
            ).classes("min-w-[220px]")
            log_path_input = ui.input(
                label="日志路径（可自定义）", value=str(state.log_path)
            ).classes("w-full")
        log_area = ui.scroll_area().classes(
            "w-full bg-gray-50 rounded-lg border border-gray-200"
        )
        log_area.style("max-height: 420px")
        with log_area:
            log_list = (
                ui.column()
                .classes("w-full gap-0 p-3 font-mono text-sm")
                .style("white-space: pre;")
            )

        def classify_line(line: str) -> str:
            upper = line.upper()
            if "ERROR" in upper:
                return "text-red-700"
            if "WARN" in upper:
                return "text-amber-700"
            if "INFO" in upper:
                return "text-blue-700"
            return "text-gray-800"

        def refresh_log_options() -> None:
            options = [str(p) for p in list_log_files(LOG_DIR)]
            current_path = str(log_path_input.value or state.log_path)
            if current_path and current_path not in options:
                options.insert(0, current_path)
            log_select.options = options
            log_select.value = current_path
            log_select.update()

        def select_log_file(path_value: str | None) -> None:
            if not path_value:
                return
            log_path_input.value = path_value
            log_path_input.update()
            refresh()

        def refresh() -> None:
            refresh_log_options()
            try:
                state.log_limit = int(limit_input.value or state.log_limit)
            except ValueError:
                state.log_limit = 200
            state.set_log_path(log_path_input.value or str(DEFAULT_LOG_FILE))
            path, lines = load_logs(state.log_limit, log_path_input.value)
            log_list.clear()
            if not lines:
                with log_list:
                    ui.label(f"未找到日志文件: {path}").classes("text-gray-500 text-sm")
                log_list.update()
                refresh_status(f"未找到日志文件: {path}")
                return

            with log_list:
                for line in lines:
                    color = classify_line(line)
                    ui.label(line).classes(f"w-full {color}").style("white-space: pre;")
            log_list.update()
            refresh_status(f"文件: {path} | 显示最新 {len(lines)} 行")

        with ui.row().classes("gap-2 mt-2 items-center justify-between"):
            ui.button("刷新日志", on_click=refresh)
            log_status = ui.label("").classes("text-xs text-gray-500")

        def refresh_status(text: str) -> None:
            log_status.text = text
            log_status.update()

        refresh_log_options()

    return refresh


def _apply_paths(workdir_input, on_refresh: Callable[[], None]) -> None:
    try:
        state.set_workdir(workdir_input.value or str(DEFAULT_WORKDIR))
        ui.notify(f"已切换工作目录: {state.workdir}", type="positive")
    except Exception as exc:  # noqa: BLE001
        notify_error(exc)
        return
    on_refresh()


def _build_dashboard(container) -> None:
    with container:
        # 右上角菜单按钮，绝对定位不占布局空间（下拉项在对话框定义后填充）
        header_btn = ui.button(icon="more_vert").props("flat round dense").classes(
            "absolute top-1 right-3 z-10"
        )

        refreshers: list[Callable[[], None]] = []
        refresh_records: "SignRecordBlock"

        def refresh_all() -> None:
            for refresh in refreshers:
                refresh()

        # ---- 预构建"数据查看"对话框（用户信息 / 签到记录 / 日志） ----
        with ui.dialog() as users_dialog, ui.card().classes("w-full max-w-4xl"):
            ui.label("用户信息").classes("text-lg font-semibold")
            ui.label("查看当前已登录账户信息 (users/*/me.json)。").classes(
                "text-gray-600"
            )
            refreshers.append(user_info_block())
            with ui.row().classes("w-full justify-end"):
                ui.button("关闭", on_click=users_dialog.close).props("outline")

        with ui.dialog() as records_dialog, ui.card().classes("w-full max-w-4xl"):
            ui.label("签到记录").classes("text-lg font-semibold")
            ui.label("签到记录（优先读取 SQLite，兼容旧 sign_record.json）").classes(
                "text-gray-600"
            )
            refresh_records = SignRecordBlock()
            refreshers.append(refresh_records)
            with ui.row().classes("w-full justify-end"):
                ui.button("关闭", on_click=records_dialog.close).props("outline")

        with ui.dialog() as logs_dialog, ui.card().classes("w-full max-w-4xl"):
            ui.label("日志").classes("text-lg font-semibold")
            ui.label("查看日志文件的最新行。").classes("text-gray-600")
            refreshers.append(log_block())
            with ui.row().classes("w-full justify-end"):
                ui.button("关闭", on_click=logs_dialog.close).props("outline")

        def goto_records(task_name: str) -> None:
            refresh_records.filter_input.set_value(task_name)
            records_dialog.open()

        def open_settings() -> None:
            with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl"):
                ui.label("工作目录").classes("text-lg font-semibold")
                workdir_input = ui.input(
                    label="工作目录",
                    value=str(state.workdir),
                    placeholder=".signer",
                ).classes("w-full")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("取消", on_click=dialog.close).props("outline")
                    ui.button(
                        "应用并刷新",
                        color="primary",
                        on_click=lambda: (
                            _apply_paths(workdir_input, refresh_all),
                            dialog.close(),
                        ),
                    )
            dialog.open()

        # 「菜单」按钮的下拉项（点击按钮展开）
        with header_btn:
            with ui.menu():
                ui.menu_item("工作目录", on_click=open_settings)
                ui.menu_item("用户信息", on_click=users_dialog.open)
                ui.menu_item("签到记录", on_click=records_dialog.open)
                ui.menu_item("日志", on_click=logs_dialog.open)

        with ui.tabs().classes("w-full").props("align=left") as tabs:
            tab_run = ui.tab("run", "签到")
            tab_random = ui.tab("random", "随机发言")
            tab_leave = ui.tab("leave", "批量退频道")
            tab_configs = ui.tab("configs", "配置管理")

        valid_tabs = {"run", "random", "leave", "configs"}

        def _tab_name(value) -> str:
            if isinstance(value, str):
                return value
            return str(getattr(value, "name", "") or "")

        saved_tab = app.storage.user.get("tg_signer_tab")
        if saved_tab not in valid_tabs:
            saved_tab = "run"

        def _on_tab_change(e) -> None:
            name = _tab_name(e.value)
            if name in valid_tabs:
                try:
                    app.storage.user["tg_signer_tab"] = name
                except Exception:
                    pass

        tabs.on_value_change(_on_tab_change)

        with ui.tab_panels(tabs, value=saved_tab).classes("w-full"):
            with ui.tab_panel(tab_run):
                refreshers.append(TaskRunnerBlock())

            with ui.tab_panel(tab_random):
                ui.label(
                    "多账号随机发言：选择账号与目标群组，按固定间隔从内置语库随机发送消息。"
                ).classes("text-gray-600")
                refreshers.append(random_chat_block(state.workdir))

            with ui.tab_panel(tab_leave):
                ui.label(
                    "批量退出长期不更新的频道：扫描账号对话，预览确认后并发批量退出。"
                ).classes("text-gray-600")
                refreshers.append(auto_leave_block(state.workdir))

            with ui.tab_panel(tab_configs):
                ui.label(
                    "管理 signer 和 monitor 的配置文件，支持查看、编辑和删除。"
                ).classes("text-gray-600")
                with ui.tabs().classes("mt-2") as sub_tabs:
                    tab_signer = ui.tab("Signer")
                    tab_monitor = ui.tab("Monitor")
                with ui.tab_panels(sub_tabs, value=tab_signer).classes("w-full"):
                    with ui.tab_panel(tab_signer):
                        refreshers.append(
                            SignerBlock(SIGNER_TEMPLATE, goto_records=goto_records)
                        )
                    with ui.tab_panel(tab_monitor):
                        refreshers.append(MonitorBlock(MONITOR_TEMPLATE))

        refresh_all()


def _auth_gate(container, auth_code: str, on_success: Callable[[], None]) -> None:
    with container:
        ui.label("TG Signer Web 控制台").classes(
            "text-2xl font-semibold tracking-wide mb-2"
        )
        ui.label("已启用访问控制，请输入 Auth Code 继续使用 Web 控制台。").classes(
            "text-gray-600"
        )
        with ui.column().classes("w-full items-center"):
            with ui.card().classes("w-full max-w-xl shadow-md"):
                ui.label("Auth Code 验证").classes("text-lg font-semibold")
                ui.label("检测到auth_code环境变量已配置，首次访问需验证。").classes(
                    "text-sm text-gray-500"
                )
                code_input = ui.input(
                    label="Auth Code",
                    placeholder="请输入授权码",
                    password=True,
                    password_toggle_button=True,
                ).classes("w-full")
                status = ui.label("").classes("text-sm text-negative")

                def verify() -> None:
                    # TODO: Security improvements needed
                    # 1. Add rate limiting (e.g. max 5 attempts per minute) to prevent brute-force attacks.
                    # 2. Use secrets.compare_digest(code, auth_code) to prevent timing attacks.
                    code = (code_input.value or "").strip()
                    if not code:
                        ui.notify("请输入授权码", type="warning")
                        return
                    if code != auth_code:
                        status.text = "授权码错误，请重试"
                        status.update()
                        code_input.set_value("")
                        ui.notify("认证失败", type="negative")
                        return
                    app.storage.user[AUTH_STORAGE_KEY] = auth_code
                    ui.notify("认证成功", type="positive")
                    container.clear()
                    on_success()

                ui.button("验证并进入", color="primary", on_click=verify).classes(
                    "w-full mt-2"
                )


def build_ui(auth_code: str = None) -> None:
    ui.page_title("TG Signer Web 控制台")
    root = ui.column().classes("w-full gap-3")

    def render_dashboard() -> None:
        root.clear()
        _build_dashboard(root)

    auth_code = auth_code or (os.environ.get(AUTH_CODE_ENV) or "").strip()
    if not auth_code:
        render_dashboard()
        return

    if app.storage.user.get(AUTH_STORAGE_KEY) == auth_code:
        render_dashboard()
        return

    root.clear()
    _auth_gate(root, auth_code, render_dashboard)


def main(host: str = None, port: int = None, storage_secret: str = None) -> None:
    ui.run(
        build_ui,
        title="TG Signer WebUI",
        favicon="⚙️",
        reload=False,
        host=host,
        port=port,
        show=False,
        storage_secret=storage_secret or os.urandom(10).hex(),
    )
