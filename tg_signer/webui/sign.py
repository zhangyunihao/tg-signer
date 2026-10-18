import asyncio
import html as html_lib
import json
import logging
import os
import queue
import random
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from nicegui import ui
from pydantic import TypeAdapter

from tg_signer.core import get_api_config, get_proxy, safe_disconnect
from tg_signer.utils import atomic_write_json
from tg_signer.webui.auto_leave import UNSELECT_KEYWORDS as _LEAVE_EXCLUDE_KEYWORDS
from tg_signer.webui.auto_leave import load_config as _load_leave_config
from tg_signer.webui.auto_leave import load_leave_tasks as _load_leave_tasks
from tg_signer.webui.combo import load_composite_tasks
from tg_signer.webui.common import (
    SOURCE_ROOT,
    _assign_to_job,
    add_dialog_close_button,
    notify_error,
    pretty_json,
    state,
)
from tg_signer.webui.data import (
    CONFIG_META,
    ConfigKind,
    account_identities,
    apply_account_order,
    delete_config,
    get_workdir,
    list_task_names,
    load_config,
    load_disabled_accounts,
    load_sign_records,
    load_two_way_accounts,
    load_user_infos,
    rename_account_state,
    save_config,
    set_account_enabled,
    set_account_order,
    set_account_two_way,
)
from tg_signer.webui.giveaway import (
    channel_title_map as _chat_title_map,
)
from tg_signer.webui.giveaway import (
    load_task_store as _load_giveaway_store,
)
from tg_signer.webui.interactive import InteractiveSignerConfig
from tg_signer.webui.schema_utils import clean_schema

logger = logging.getLogger(__name__)

# 多账号错峰启动总跨度（秒）：N 个账号的签到启动时间在此跨度内按槽位递增错开，
# 避免同一群里连续出现多个账号的签到消息；单账号不生效
ACCOUNT_STAGGER_SPREAD = 300


def build_combo_sub_spec(
    account: str, sub: dict, workdir
) -> Optional[dict]:
    """把组合任务的一个子任务配置构建为可执行的子任务 spec。

    返回 {"label", "args"}（CLI 型）或 {"label", "module", "spec"}（内置
    runner 型：自动抽奖/批量退频道），无法构建时返回 None。
    """
    sub_type = str(sub.get("type") or "")
    task_name = str(sub.get("task") or "")
    label = f"{sub_type} · {task_name}" if task_name else sub_type
    if "退频道" in sub_type:
        # 按名称选择退频道任务（任务列表模式）；「默认配置」或找不到时
        # 回退旧版全局配置，兼容历史保存的组合
        leave_task = next(
            (
                t
                for t in _load_leave_tasks(workdir)
                if str(t.get("name")) == task_name
            ),
            None,
        )
        if leave_task is not None:
            return {
                "label": label,
                "module": "tg_signer.leave_runner",
                "spec": {
                    "account": account,
                    "session_dir": str(workdir),
                    "days": int(leave_task.get("days") or 7),
                    "include_groups": bool(leave_task.get("include_groups")),
                    "skip_pinned": bool(leave_task.get("skip_pinned", True)),
                    "exclude_keywords": list(
                        leave_task.get("keywords") or _LEAVE_EXCLUDE_KEYWORDS
                    ),
                    "auto_leave": bool(leave_task.get("auto_leave", False)),
                },
            }
        cfg = _load_leave_config(workdir)
        return {
            "label": label,
            "module": "tg_signer.leave_runner",
            "spec": {
                "account": account,
                "session_dir": str(workdir),
                "days": int(cfg.get("days") or 7),
                "include_groups": bool(cfg.get("include_groups")),
                "skip_pinned": bool(cfg.get("skip_pinned", True)),
                "exclude_keywords": list(_LEAVE_EXCLUDE_KEYWORDS),
            },
        }
    if "抽奖" in sub_type:
        store = _load_giveaway_store(workdir)
        gcfg = store["tasks"].get(task_name)
        if not isinstance(gcfg, dict):
            return None
        return {
            "label": label,
            "module": "tg_signer.giveaway_runner",
            "spec": {
                "account": account,
                "workdir": str(workdir),
                "task": task_name,
                "session_dir": str(gcfg.get("session_dir") or "."),
                "chats": [str(c) for c in (gcfg.get("chats") or [])],
                "keywords": list(gcfg.get("keywords") or []),
                "auto_scan": bool(gcfg.get("auto_scan", True)),
                "auto_join": bool(gcfg.get("auto_join", True)),
                "ignore_history": bool(gcfg.get("ignore_history", True)),
                "max_joins": gcfg.get("max_joins"),
                "delay_min": gcfg.get("delay_min") or 0,
                "delay_max": gcfg.get("delay_max") or 0,
                "confirm_wait": int(gcfg.get("confirm_wait") or 8),
                "rescue_interval_min": int(gcfg.get("rescue_interval_min") or 0),
                "rescue_limit": int(gcfg.get("rescue_limit") or 10),
            },
        }
    if "run-once" in sub_type:
        args = ["-w", str(workdir), "-a", account, "run-once", task_name]
    elif "run 定时" in sub_type:
        args = ["-w", str(workdir), "-a", account, "run", task_name]
    elif "自动化" in sub_type:
        args = ["-w", str(workdir), "-a", account, "automation", "run", task_name]
    elif "监控" in sub_type:
        args = ["-w", str(workdir), "-a", account, "monitor", "run", task_name]
    else:
        return None
    return {"label": label, "args": args}


def _combo_sub_accounts(sub: dict, workdir) -> list[str]:
    """获取组合子任务自身绑定的账号列表（组合执行不再手动选账号）。

    - 自动抽奖：抽奖任务配置里的 accounts；
    - 批量退频道：退频道任务的 accounts（找不到任务时回退旧版全局配置）；
    - 签到 (run-once)：任务绑定账号（webui_task_accounts.json），无绑定时
      回退最近一次执行记录中的账号。
    """
    sub_type = str(sub.get("type") or "")
    task_name = str(sub.get("task") or "")
    if "抽奖" in sub_type:
        store = _load_giveaway_store(workdir)
        gcfg = store["tasks"].get(task_name) or {}
        return [str(a) for a in (gcfg.get("accounts") or []) if a]
    if "退频道" in sub_type:
        leave_task = next(
            (
                t
                for t in _load_leave_tasks(workdir)
                if str(t.get("name")) == task_name
            ),
            None,
        )
        if leave_task is not None:
            return [str(a) for a in (leave_task.get("accounts") or []) if a]
        return [
            str(a)
            for a in (_load_leave_config(workdir).get("accounts") or [])
            if a
        ]
    if "run-once" in sub_type:
        bound = load_task_accounts().get(f"{sub_type}|{task_name}", [])
        if bound:
            return [str(a) for a in bound if a]
        for r in reversed(load_task_history()):
            if (
                "签到 (run-once)" in str(r.get("task_type") or "")
                and str(r.get("task_name") or "") == task_name
            ):
                parts = str(r.get("accounts") or "").split(",")
                return [a.strip() for a in parts if a.strip()]
    return []


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
        # 默认选中第一个配置（会触发 load_current 自动加载）
        if options and not self.select.value:
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
    atomic_write_json(_task_history_path(), items[-TASK_HISTORY_LIMIT:])


def append_task_history(record: dict) -> None:
    items = load_task_history()
    items.append(record)
    save_task_history(items)


# 任务绑定账号：任务首次执行后记住所用账号，后续执行无需再选
TASK_ACCOUNTS_FILE = "webui_task_accounts.json"


def _task_accounts_path() -> Path:
    return get_workdir(state.workdir) / TASK_ACCOUNTS_FILE


def load_task_accounts() -> dict[str, list[str]]:
    path = _task_accounts_path()
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_task_binding(task_type: str, task_name: str, accounts: list[str]) -> None:
    if not task_type or not task_name or not accounts:
        return
    data = load_task_accounts()
    data[f"{task_type}|{task_name}"] = list(accounts)
    atomic_write_json(_task_accounts_path(), data)


def update_task_history(record: dict) -> None:
    """按记录 id 更新已存在的历史条目并持久化。"""
    items = load_task_history()
    for i, item in enumerate(items):
        if item.get("id") == record.get("id"):
            items[i] = record
            break
    save_task_history(items)


class TaskRunnerBlock:
    """执行任务面板：从 WebUI 直接运行签到/自动化/监控/组合任务"""

    TASK_TYPES = [
        "签到 (run-once)",
        "签到 (run 定时)",
        "自动化 (automation run)",
        "监控 (monitor run)",
        "组合任务",
    ]

    # 服务进程级共享：所有浏览器标签页看到并管理同一批运行中的任务，
    # 服务重启后也据此把上次未结束的常驻任务自动恢复
    runs: dict[str, dict] = {}
    output_lines: list[str] = []
    _restore_done: bool = False

    def __init__(self) -> None:
        ui.label("签到").classes("text-lg font-semibold")
        ui.label(
            "点击「新增任务」在弹窗中选择任务类型、任务与账号；组合任务的创建与执行在独立的「组合任务」tab；历史列表可「复制」任意历史任务的参数快速新建，运行中的任务可停止。"
        ).classes("text-sm text-gray-500")

        with ui.row().classes("gap-2 items-center"):
            ui.button(
                "新增任务", icon="add", color="primary", on_click=self.open_dialog
            )
            ui.button("清空输出", on_click=self.clear_log).props("outline")

        self.status_label = ui.label("空闲").classes("text-sm text-gray-500")

        # ---- 任务历史 ----
        ui.separator().classes("my-2")
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("任务历史").classes("text-md font-semibold")
            ui.button("清空历史", on_click=self.clear_history).props("outline dense")
        ui.label(
            "记录通过本页面手动执行过的任务（含类型、账号、状态与结果），保存在工作目录下。"
        ).classes("text-sm text-gray-500")
        self.history_summary = ui.label("").classes("text-sm text-gray-600")
        self.history_table = (
            ui.table(
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
                        "style": "min-width: 340px;",
                        "classes": "result-col",
                    },
                    {
                        "name": "actions",
                        "label": "操作",
                        "field": "actions",
                        "align": "left",
                        "classes": "col-actions",
                        "style": "white-space: nowrap;",
                    },
                ],
                rows=[],
                pagination=5,
            )
            .classes("w-full tgs-desktop-table")
            .props("flat dense")
        )
        # 移动端（<=640px）用纵向卡片列表替代横滑宽表，桌面端隐藏
        self.history_rows: list[dict] = []
        self._cards_limit = 5
        self.history_cards = ui.column().classes("w-full gap-2 tgs-mobile-cards mt-1")
        # 状态列渲染为彩色徽章：已完成=绿、失败=红、运行中=蓝、其他=灰
        self.history_table.add_slot(
            "body-cell-status",
            '<q-td :props="props">'
            "<q-badge :color=\"props.row.status === '已完成' ? 'positive' : "
            "props.row.status === '失败' ? 'negative' : "
            "props.row.status === '运行中' ? 'info' : 'grey-6'\">"
            "{{ props.row.status }}</q-badge></q-td>",
        )
        # 结果列：给文字一个固定 data-runres=<任务id>（桌面表格单元格与移动端卡片
        # 各有一份），运行中进度用 run_javascript 直接改写两处文本，
        # 避免整表重渲染吞掉按钮点击
        self.history_table.add_slot(
            "body-cell-result",
            '<q-td :props="props">'
            '<span :data-runres="props.row.id" '
            'style="white-space: pre-line; overflow-wrap: break-word; '
            'display: block; width: 100%;">'
            "{{ props.row.result }}</span>"
            "</q-td>",
        )
        # 操作列：所有行显示「复制」「明细」（用该行参数打开新增弹窗/查看子任务明细），运行中的行额外显示「停止」
        self.history_table.add_slot(
            "body-cell-actions",
            '<q-td :props="props" class="col-actions">'
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
            "w-full bg-gray-50 rounded-lg border border-gray-200 sign-log-area"
        )
        self.log_area.style("max-height: 420px")
        with self.log_area:
            self.log_list = (
                ui.column()
                .classes("w-full gap-0 p-3 font-mono text-sm")
                .style("white-space: pre;")
            )

        # 轮询改用独立 asyncio 循环而非 ui.timer：页面重建（root.clear）会删除
        # timer 的父 slot，ui.timer 内部访问已删 slot 会抛 RuntimeError 并可能
        # 终止服务进程；asyncio 循环配合断连退出检查更健壮。
        self._poll_enabled = False
        self._poll_client = ui.context.client
        self._poll_task = asyncio.create_task(self._poll_forever())

        # ---- 新增任务弹窗 ----
        with ui.dialog() as self.dialog, ui.card().classes("w-full max-w-2xl"):
            add_dialog_close_button(self.dialog)
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
                    on_change=self._on_task_change,
                ).classes("min-w-[200px]")

                self.account_select = (
                    ui.select(
                        label="选择账号（可多选）",
                        options=[],
                        multiple=True,
                    )
                    .classes("min-w-[240px]")
                    .props("use-chips")
                )
            with ui.row().classes("items-center gap-2 w-full") as self.binding_row:
                self.binding_label = ui.label("").classes("text-sm text-primary")
                ui.button(
                    "更改账号", on_click=self._show_account_select
                ).props("flat dense")
            self.binding_row.set_visibility(False)
            ui.label("账号来自 *.session 文件；多选时每个账号各启动一个进程。首次执行后账号与任务绑定，后续执行无需再选。").classes(
                "text-xs text-gray-500"
            )
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("取消", on_click=self.dialog.close).props("outline")
                ui.button("执行", color="primary", on_click=self.start_task)

    def _get_task_names(self, task_type: str | None = None) -> list[str]:
        if task_type is None:
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
        elif "抽奖" in task_type:
            store = _load_giveaway_store(state.workdir)
            return sorted(
                store["tasks"].keys(), key=lambda k: int(k) if str(k).isdigit() else 0
            )
        elif "退频道" in task_type:
            return ["默认配置"]
        elif "组合" in task_type:
            return [
                str(c.get("name"))
                for c in load_composite_tasks(state.workdir)
                if c.get("name")
            ]
        return []

    def _get_combo_task_names(self, task_type: str) -> list[str] | dict[str, str]:
        """组合任务弹窗的任务下拉：签到 (run-once) 列出历史上执行过的任务名
        （而非 signs/ 下的任务配置），并附「账号｜频道」摘要；自动抽奖列出
        「编号｜账号｜监控对象」；其余类型沿用 _get_task_names。"""
        if "run-once" in task_type:
            names: dict[str, str] = {}
            for r in reversed(load_task_history()):
                if "签到 (run-once)" not in str(r.get("task_type") or ""):
                    continue
                name = str(r.get("task_name") or "")
                if not name or name in names:
                    continue
                accounts = str(r.get("accounts") or "").strip()
                chats_text = ""
                try:
                    entry = load_config("signer", name, state.workdir)
                    chats = entry.payload.get("chats") or []
                    labels = [
                        str(c.get("name") or c.get("chat_id") or "") for c in chats
                    ]
                    labels = [x for x in labels if x]
                    chats_text = "、".join(labels[:5])
                    if len(labels) > 5:
                        chats_text += f" 等{len(labels)}个"
                except (FileNotFoundError, ValueError, OSError, KeyError, TypeError):
                    logger.debug("读取签到任务配置失败：%s", name, exc_info=True)
                names[name] = f"{name}｜账号：{accounts or '未绑定'}｜频道：{chats_text or '未设置'}"
            return names
        if "抽奖" in task_type:
            store = _load_giveaway_store(state.workdir)
            title_map = _chat_title_map(state.workdir)
            opts: dict[str, str] = {}
            for tid in sorted(
                store["tasks"].keys(), key=lambda k: int(k) if str(k).isdigit() else 0
            ):
                cfg = store["tasks"][tid] or {}
                accounts = "、".join(str(a) for a in (cfg.get("accounts") or []))
                chats = [str(c) for c in (cfg.get("chats") or [])]
                # 监控对象优先显示频道/群组名称，未缓存的保留原 id
                labels = [title_map.get(c, c) for c in chats]
                chats_text = "、".join(labels[:5])
                if len(labels) > 5:
                    chats_text += f" 等{len(labels)}个"
                opts[str(tid)] = (
                    f"{tid}｜账号：{accounts or '未设置'}｜监控：{chats_text or '未设置'}"
                )
            return opts
        if "退频道" in task_type:
            # 批量退频道为任务列表模式：仅列出开启了「自动退出未命中关键字的
            # 频道」的任务（组合执行无人工确认环节，仅扫描任务在组合中无意义）
            opts = {}
            for t in _load_leave_tasks(state.workdir):
                name = str(t.get("name") or "")
                if not name or not t.get("auto_leave"):
                    continue
                accs = "、".join(str(a) for a in (t.get("accounts") or []))
                opts[name] = f"{name}（{t.get('days')}天｜自动退出｜账号：{accs or '未设置'}）"
            return opts
        return self._get_task_names(task_type)

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

    def _reconcile_history(self, items: list[dict]) -> bool:
        """校正历史中的孤儿「运行中」记录。

        服务重启后 self.runs 为空，重启前启动的子进程已脱离本服务管理，
        历史里残留的「运行中」永远无法停止。这里统一标记为「已停止」。
        返回是否发生过校正（调用方据此持久化）。
        """
        changed = False
        for r in items:
            if r.get("status") == "运行中" and str(r.get("id") or "") not in self.runs:
                r["status"] = "已停止"
                r["result"] = "服务重启，任务已中断"
                changed = True
        if changed:
            save_task_history(items)
        return changed

    def _refresh_history_table(self) -> None:
        items = load_task_history()
        self._reconcile_history(items)
        counts: dict[str, int] = dict.fromkeys(
            ("运行中", "已完成", "失败", "已停止"), 0
        )
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
        self.history_rows = rows
        self.history_table.rows = rows
        self.history_table.update()
        self._render_history_cards()

    @staticmethod
    def _status_color(status: str) -> str:
        if status == "已完成":
            return "positive"
        if status == "失败":
            return "negative"
        if status == "运行中":
            return "info"
        return "grey-6"

    def _render_history_cards(self) -> None:
        """移动端卡片列表：与桌面表格共用同一批行数据与操作。"""
        self.history_cards.clear()
        with self.history_cards:
            shown = self.history_rows[: self._cards_limit]
            if not shown:
                ui.label("暂无记录").classes("text-sm text-gray-400")
            for row in shown:
                with ui.card().classes("w-full mobile-list-card border"):
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        status = row.get("status") or "未知"
                        ui.badge(status).props(f"color={self._status_color(status)}")
                        ui.label(row.get("task") or "-").classes(
                            "text-base font-semibold"
                        )
                    ui.label(row.get("time") or "").classes("text-xs text-gray-400")
                    ui.label(
                        f"类型：{row.get('type') or '-'}　账号：{row.get('accounts') or '-'}"
                    ).classes("text-xs text-gray-600")
                    result = row.get("result") or ""
                    ui.html(
                        '<div class="text-xs text-gray-700" '
                        f'data-runres="{html_lib.escape(str(row.get("id") or ""), quote=True)}" '
                        f'style="white-space: pre-line; overflow-wrap: anywhere;">'
                        f"{html_lib.escape(result)}</div>"
                    )
                    with ui.row().classes("w-full mobile-card-actions"):
                        ui.button(
                            "复制",
                            on_click=lambda r=row: self._duplicate_row(r),
                        ).props("flat dense color=primary")
                        ui.button(
                            "明细",
                            on_click=lambda r=row: self._detail_row(r),
                        ).props("flat dense color=grey-8")
                        if row.get("status") == "运行中":
                            ui.button(
                                "停止",
                                on_click=lambda r=row: self._stop_run(
                                    str(r.get("id") or "")
                                ),
                            ).props("flat dense color=negative")
            if len(self.history_rows) > self._cards_limit:
                ui.button(
                    f"展开更多（共 {len(self.history_rows)} 条）",
                    on_click=self._show_more_cards,
                ).props("flat dense").classes("w-full")

    def _show_more_cards(self) -> None:
        self._cards_limit += 10
        self._render_history_cards()

    def clear_history(self) -> None:
        save_task_history([])
        self._refresh_history_table()
        ui.notify("已清空任务历史", type="positive")

    def open_dialog(self) -> None:
        self._refresh_accounts()
        self._on_type_change()
        # 任务选中后按绑定账号自动填充，无需手动选择
        self._on_task_change()
        self.dialog.open()

    def _on_type_change(self) -> None:
        self.task_select.options = self._get_task_names()
        # 只有一个任务时默认选中，否则清空选择
        if len(self.task_select.options) == 1:
            self.task_select.value = self.task_select.options[0]
        else:
            self.task_select.value = None
        self.task_select.update()

    def _get_bound_accounts(self, task_name: str) -> list[str]:
        """任务已绑定的账号（过滤掉当前已不存在的账号）。"""
        task_type = self.type_select.value or ""
        bound = load_task_accounts().get(f"{task_type}|{task_name}", [])
        options = self.account_select.options or []
        return [a for a in bound if a and a in options]

    def _on_task_change(self) -> None:
        """任务选择变化：有绑定时直接使用绑定账号并隐藏选择框，无绑定时默认全选。"""
        task_name = self.task_select.value
        bound = self._get_bound_accounts(task_name) if task_name else []
        if bound:
            self.account_select.value = list(bound)
            self.binding_label.text = (
                f"已绑定账号：{'、'.join(bound)}，执行时无需再选择"
            )
            self.account_select.set_visibility(False)
            self.binding_row.set_visibility(True)
        else:
            self.binding_row.set_visibility(False)
            self.account_select.set_visibility(True)
            if not self.account_select.value and self.account_select.options:
                self.account_select.value = list(self.account_select.options)
        self.account_select.update()
        self.binding_row.update()

    def _show_account_select(self) -> None:
        """点击「更改账号」：重新显示账号选择框，绑定账号已预填可直接调整。"""
        self.binding_row.set_visibility(False)
        self.account_select.set_visibility(True)
        self.account_select.update()

    def _build_args(self, account: str, task_type: str, task_name: str) -> list[str]:
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
        # 记住该任务使用的账号，下次执行无需再选
        save_task_binding(self.type_select.value or "", task_name, accounts)
        if self._launch(self.type_select.value or "", task_name, accounts):
            self.dialog.close()

    def _launch(
        self,
        task_type: str,
        task_name: str,
        accounts: list[str],
        auto: bool = False,
    ) -> bool:
        """启动一个任务（每账号一个子进程）。auto=True 表示服务重启后的自动恢复。"""
        is_combo = "组合" in task_type
        combo_subs: list[dict] = []
        sub_account_sets: list[list[str]] = []
        if is_combo:
            combo = next(
                (
                    c
                    for c in load_composite_tasks(state.workdir)
                    if c.get("name") == task_name
                ),
                None,
            )
            if not combo or not combo.get("subs"):
                ui.notify("组合任务不存在或没有子任务", type="warning")
                return False
            combo_subs = combo["subs"]
            if accounts:
                # 执行弹窗显式指定账号：所有子任务仅用所选账号（覆盖绑定账号）
                available = set(self._get_account_names())
                accounts = [a for a in accounts if a in available]
                if not accounts:
                    ui.notify("所选账号均不可用", type="warning")
                    return False
                sub_account_sets = [list(accounts) for _ in combo_subs]
            else:
                # 默认：各子任务使用其自身绑定的账号并集
                sub_account_sets = [
                    _combo_sub_accounts(sub, state.workdir) for sub in combo_subs
                ]
                accounts = []
                for accs in sub_account_sets:
                    for a in accs:
                        if a and a not in accounts:
                            accounts.append(a)
                available = set(self._get_account_names())
                accounts = [a for a in accounts if a in available]
                if not accounts:
                    ui.notify("组合任务的子任务都没有绑定可用账号", type="warning")
                    return False
        # 多账号时打乱启动顺序，避免每次都按相同顺序发言
        accounts = [a for a in accounts if a]
        random.shuffle(accounts)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SOURCE_ROOT)
        # 每个账号进程各自随机打乱频道执行顺序，避免所有账号按相同顺序发同一批频道
        env["TG_SIGNER_SHUFFLE_CHATS"] = "1"
        # 多账号错峰启动：打乱后的第 i 个账号在 ACCOUNT_STAGGER_SPREAD 跨度内
        # 按槽位递增分配随机延迟（相邻账号至少隔半个槽位），避免同一群里
        # 连续出现多个账号的签到消息
        stagger_delays: Dict[str, int] = {}
        if len(accounts) > 1:
            gap = ACCOUNT_STAGGER_SPREAD / len(accounts)
            for i, account in enumerate(accounts):
                stagger_delays[account] = int(i * gap + random.uniform(0, gap * 0.5))
        record = {
            "id": f"{time.time_ns()}",
            "start_time": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
            "task_type": task_type,
            "task_name": task_name,
            "accounts": ", ".join(accounts),
            "status": "运行中",
            "result": "服务重启后自动恢复" if auto else "",
        }
        procs: list[dict] = []
        run_queue: queue.Queue = queue.Queue()
        chats_labels = [] if is_combo else self._chat_labels(task_name)
        detail_rows: list[dict] = []
        for account in accounts:
            if is_combo:
                # 组合任务：每账号一个进程，进程内按顺序执行该账号参与的全部子任务
                spec_subs = []
                for sub, sub_accs in zip(
                    combo_subs, sub_account_sets, strict=True
                ):
                    if account not in sub_accs:
                        continue
                    built = build_combo_sub_spec(
                        account, sub, state.workdir
                    )
                    if built:
                        spec_subs.append(built)
                if not spec_subs:
                    ui.notify(f"无法构建命令: {account}", type="warning")
                    continue
                cmd = [
                    sys.executable,
                    "-m",
                    "tg_signer.combo_runner",
                    json.dumps({"subs": spec_subs}, ensure_ascii=False),
                ]
            else:
                args = self._build_args(account, task_type, task_name)
                if not args:
                    ui.notify(f"无法构建命令: {account}", type="warning")
                    continue
                cmd = [sys.executable, "-m", "tg_signer.task_runner"] + args
            # 每账号进程独立统计聊天级进度：started=已开始的聊天数，failed=签到失败数
            # last_sign/next_run 从子进程日志解析，用于结果列展示上次/预计下次执行时间
            stats = {
                "started": 0,
                "failed": 0,
                "current_pos": None,
                "last_sign": None,
                "next_run": None,
            }
            try:
                # 该账号分配到错峰延迟时，通过独立 env 下发给子进程
                start_delay = stagger_delays.get(account, 0)
                proc_env = env
                if start_delay > 0:
                    proc_env = env.copy()
                    proc_env["TG_SIGNER_START_DELAY"] = str(start_delay)
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=proc_env,
                )
                _assign_to_job(proc)
            except Exception as exc:
                notify_error(exc)
                continue
            reader = threading.Thread(
                target=self._reader,
                args=(
                    proc,
                    task_name,
                    account,
                    run_queue,
                    stats,
                    chats_labels,
                    detail_rows,
                ),
                kwargs={"combo": is_combo},
                daemon=True,
            )
            reader.start()
            procs.append(
                {"proc": proc, "reader": reader, "account": account, "stats": stats}
            )
        if not procs:
            ui.notify("未能启动任务", type="negative")
            return False
        record["accounts"] = ", ".join(i["account"] for i in procs)
        total_units = (len(combo_subs) if is_combo else len(chats_labels)) * len(procs)
        self.runs[record["id"]] = {
            "record": record,
            "procs": procs,
            "queue": run_queue,
            "start_ts": time.time(),
            "total_chats": total_units,
            "details": detail_rows,
        }
        append_task_history(record)
        self._refresh_history_table()
        self._poll_enabled = True
        self._update_status()
        tip = "服务重启后已自动恢复" if auto else "已启动"
        ui.notify(f"{tip}: {task_name}（{len(procs)} 个账号）", type="positive")
        return True

    def _reader(
        self,
        proc: subprocess.Popen,
        task_name: str,
        account: str,
        q: "queue.Queue",
        stats: dict,
        chats_labels: list,
        detail_rows: list,
        combo: bool = False,
    ) -> None:
        prefix = f"[{task_name}:{account}] "
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            stripped = line.lstrip()
            # 组合任务：combo_runner 输出的「>>> 子任务开始/结束」标记行，
            # 每个子任务对应一条明细（开始=进行中，结束按退出码定成败）
            if combo and stripped.startswith(">>> 子任务开始:"):
                pos = stats["current_pos"]
                if pos is not None and detail_rows[pos]["status"] == "进行中":
                    detail_rows[pos]["status"] = "成功"
                label = stripped.split(">>> 子任务开始:", 1)[1].strip()
                detail_rows.append(
                    {"account": account, "chat": label, "status": "进行中"}
                )
                stats["current_pos"] = len(detail_rows) - 1
                stats["started"] += 1
                q.put(prefix + line.rstrip("\r\n"))
                continue
            if combo and stripped.startswith(">>> 子任务结束"):
                tail = stripped.rsplit("退出码", 1)[-1].strip()
                rc_ok = tail.isdigit() and int(tail) == 0
                pos = stats["current_pos"]
                if (
                    pos is not None
                    and pos < len(detail_rows)
                    and detail_rows[pos]["status"] == "进行中"
                ):
                    detail_rows[pos]["status"] = "成功" if rc_ok else "失败"
                if not rc_ok:
                    stats["failed"] += 1
                q.put(prefix + line.rstrip("\r\n"))
                continue
            # CLI 日志带格式前缀（[INFO] [name] 时间 ... 账户「x」- 任务「y」: 开始执行: ...），用子串匹配
            if not combo and "开始执行" in stripped:
                # 上一轮聊天结束，视为成功
                pos = stats["current_pos"]
                if pos is not None and detail_rows[pos]["status"] == "进行中":
                    detail_rows[pos]["status"] = "成功"
                # 按配置中的聊天顺序轮转取标签
                if chats_labels:
                    label = chats_labels[stats["started"] % len(chats_labels)]
                else:
                    label = f"第{stats['started'] + 1}个聊天"
                detail_rows.append(
                    {"account": account, "chat": label, "status": "进行中"}
                )
                stats["current_pos"] = len(detail_rows) - 1
                stats["started"] += 1
            elif not combo and "签到失败" in stripped:
                stats["failed"] += 1
                pos = stats["current_pos"]
                if pos is not None and detail_rows[pos]["status"] == "进行中":
                    detail_rows[pos]["status"] = "失败"
            elif "上次执行时间:" in stripped:
                # 形如：上次执行时间: 2026-10-14 00:44:22.965904+08:00
                stats["last_sign"] = stripped.split("上次执行时间:", 1)[1].strip()
            elif "下次运行时间:" in stripped:
                # 形如：下次运行时间: 2026-10-15 00:43:08+08:00
                stats["next_run"] = stripped.split("下次运行时间:", 1)[1].strip()
            elif "计划时刻" in stripped:
                # 当天签到时刻未到时：等待定时执行（计划时刻 2026-10-15 00:43:00）
                seg = stripped.split("计划时刻", 1)[1]
                stats["next_run"] = seg.split("）", 1)[0].strip()
            q.put(prefix + line.rstrip("\r\n"))

    def _chat_labels(self, task_name: str) -> list[str]:
        """读取签到配置的聊天标签（名称+chat_id），读取失败返回空列表。"""
        try:
            entry = load_config("signer", task_name, workdir=state.workdir)
            labels = []
            for c in entry.payload.get("chats") or []:
                name = str(c.get("name") or "").strip()
                labels.append(
                    f"{name}({c.get('chat_id')})" if name else str(c.get("chat_id"))
                )
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
        if total > 0 and ("run-once" in task_type or "组合" in task_type):
            return f"进度 {ok}/{total} · 失败 {failed}"
        if started > 0:
            return f"已完成 {ok} 个聊天 · 失败 {failed}"
        return ""

    @staticmethod
    def _short_dt(raw: str) -> str:
        """把日志时间串精简为 MM-DD HH:MM:SS；解析失败时原样返回。"""
        try:
            return raw.strip()[:19][5:]
        except Exception:
            return raw

    def _format_schedule(self, run: dict) -> str:
        """聚合各账号子进程日志解析出的上次/预计下次执行时间（多账号去重）。"""
        lasts: list[str] = []
        nexts: list[str] = []
        for item in run["procs"]:
            s = item["stats"]
            last = (s.get("last_sign") or "").strip()
            nxt = (s.get("next_run") or "").strip()
            if last and last not in lasts:
                lasts.append(last)
            if nxt and nxt not in nexts:
                nexts.append(nxt)
        parts = []
        if lasts:
            parts.append("上次 " + " / ".join(self._short_dt(x) for x in lasts))
        if nexts:
            parts.append("预计下次 " + " / ".join(self._short_dt(x) for x in nexts))
        return " · ".join(parts)

    @staticmethod
    def _event_row(e) -> dict | None:
        """从 Quasar 表格事件对象中取出行 dict（事件参数可能被包成 list）。"""
        row = getattr(e, "args", None)
        if isinstance(row, list) and row and isinstance(row[0], dict):
            row = row[0]
        return row if isinstance(row, dict) else None

    def _on_stop_event(self, e) -> None:
        row = self._event_row(e)
        if row and row.get("id"):
            self._stop_run(str(row["id"]))

    def _on_detail_event(self, e) -> None:
        """桌面表格「明细」按钮事件入口。"""
        row = self._event_row(e)
        if row is not None:
            self._detail_row(row)

    def _detail_row(self, row: dict) -> None:
        """查看某条任务记录的子任务（账号 × 聊天）成功/失败明细"""
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
            add_dialog_close_button(dlg)
            ui.label(f"子任务明细 — {row.get('task', '')}").classes(
                "text-lg font-semibold"
            )
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
            account_sel = (
                ui.radio(
                    ["全部账号"] + accounts,
                    value="全部账号",
                    on_change=lambda: render(),
                )
                .props("inline dense")
                .classes("w-full flex-wrap gap-x-4 gap-y-1 text-sm")
            )
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
                        mark = {"成功": "✓", "失败": "✗", "进行中": "…"}.get(
                            status, "−"
                        )
                        ui.label(
                            f"{mark} {d.get('account', '')} → {d.get('chat', '')}：{status}"
                        ).classes(f"text-sm py-0.5 {color}").style("white-space: pre;")
                list_container.update()

            render()
            with ui.row().classes("w-full justify-end"):
                ui.button("关闭", on_click=dlg.close).props("flat")
        dlg.open()

    def _on_duplicate_event(self, e) -> None:
        """桌面表格「复制」按钮事件入口。"""
        row = self._event_row(e)
        if row is not None:
            self._duplicate_row(row)

    def _duplicate_row(self, row: dict) -> None:
        """用历史行的参数打开「新增任务」弹窗（卡片与表格共用）。"""
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
        accounts = [
            a.strip() for a in (row.get("accounts") or "").split(",") if a.strip()
        ]
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
            # 孤儿记录（如服务重启前启动的任务）：进程已脱离本服务管理，
            # 仅能把历史状态校正为已停止，避免按钮点了毫无反应
            items = load_task_history()
            for r in items:
                if str(r.get("id") or "") == record_id and r.get("status") == "运行中":
                    r["status"] = "已停止"
                    r["result"] = "服务重启，任务已脱离管理，状态已校正"
                    save_task_history(items)
                    self._refresh_history_table()
                    self._update_status()
                    ui.notify(
                        "该任务启动于上次服务运行期间，已无法远程停止；"
                        "如进程仍在后台运行，请在系统任务管理器中结束对应 Python 进程",
                        type="warning",
                    )
                    return
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
        record["result"] = f"手动停止，耗时 {self._format_duration_ts(run['start_ts'])}"
        update_task_history(record)
        self._refresh_history_table()
        self._update_status()
        ui.notify(f"已停止: {record['task_name']}", type="positive")

    def _update_running_row(self, record_id: str, result_text: str) -> None:
        # 只同步内存行数据；进度文本用 JS 直接改写表格单元格与移动端卡片（固定
        # data-runres 属性，页面上有两份），不调用整表 update()，
        # 避免每 0.3s 重渲染导致「明细/停止」点击丢失
        for row in self.history_table.rows:
            if row.get("id") == record_id:
                row["result"] = result_text
                break
        safe = (
            result_text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        )
        safe_id = str(record_id).replace("\\", "\\\\").replace('"', '\\"')
        ui.run_javascript(
            f"document.querySelectorAll('[data-runres=\"{safe_id}\"]').forEach("
            f"el => {{ el.textContent = '{safe}'; }});"
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
        run = self.runs.pop(record_id, None)
        if run is None:
            return  # 已被其他浏览器标签页的轮询收尾
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
            self.status_label.text = f"运行中 {len(self.runs)} 个任务: " + "; ".join(
                parts
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
                ui.label(line).classes(f"w-full {color}").style("white-space: pre;")
        self.log_list.update()

        finished_ids: list[str] = []
        for run_id, run in list(self.runs.items()):
            if all(i["proc"].poll() is not None for i in run["procs"]):
                if any(i["reader"].is_alive() for i in run["procs"]):
                    continue
                finished_ids.append(run_id)
            else:
                parts = [f"已运行 {self._format_duration_ts(run['start_ts'])}"]
                progress = self._format_progress(run)
                if progress:
                    parts.append(progress)
                schedule = self._format_schedule(run)
                if schedule:
                    parts.append(schedule)
                # 每项独占一行（结果列 span 为 white-space: pre-line）
                self._update_running_row(run_id, "\n".join(parts))
        for run_id in finished_ids:
            self._finalize_run(run_id)
        self._update_status()
        if not self.runs:
            self._poll_enabled = False

    async def _poll_forever(self) -> None:
        """替代 ui.timer 的轮询循环：断连自动退出，UI 更新包裹在 client 上下文。"""
        while True:
            await asyncio.sleep(0.3)
            if not self._poll_client.has_socket_connection:
                return
            if not self._poll_enabled:
                continue
            try:
                with self._poll_client:
                    self._poll_output()
            except Exception:
                logger.debug("轮询读取子任务输出失败", exc_info=True)

    def clear_log(self) -> None:
        TaskRunnerBlock.output_lines.clear()
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

    def _auto_restore(self) -> None:
        """服务重启后自动恢复上次未结束的「签到 (run 定时)」常驻任务。

        每个服务进程只执行一次（多个浏览器连接共享结果）。run-once 等一次性
        任务不恢复；配置或账号已被删除的任务跳过。
        """
        if TaskRunnerBlock._restore_done:
            return
        TaskRunnerBlock._restore_done = True
        try:
            items = load_task_history()
            # task_name -> 账号（保持顺序去重）；只恢复 run 定时类常驻任务
            pending: dict[str, list[str]] = {}
            stale_ids: list[str] = []
            for r in items:
                if r.get("status") != "运行中":
                    continue
                if "run 定时" not in str(r.get("task_type") or ""):
                    continue
                task_name = str(r.get("task_name") or "").strip()
                if not task_name:
                    continue
                accs = [
                    a.strip()
                    for a in str(r.get("accounts") or "").split(",")
                    if a.strip()
                ]
                bucket = pending.setdefault(task_name, [])
                for a in accs:
                    if a not in bucket:
                        bucket.append(a)
                stale_ids.append(str(r.get("id") or ""))

            if not pending:
                return

            valid_tasks = set(list_task_names("signer", state.workdir))
            valid_accounts = set(self._get_account_names())
            restored = 0
            for task_name, accounts in pending.items():
                if task_name not in valid_tasks:
                    continue
                alive_accounts = [a for a in accounts if a in valid_accounts]
                if not alive_accounts:
                    continue
                if self._launch(
                    "签到 (run 定时)", task_name, alive_accounts, auto=True
                ):
                    restored += 1

            # 旧的中断记录统一收尾（无论是否成功恢复，都不再显示为运行中）。
            # 注意：_launch 已把新记录追加写入文件，这里必须重新读取，
            # 不能用启动前的 items 快照覆盖，否则会把新「运行中」记录冲掉。
            if stale_ids:
                latest = load_task_history()
                for r in latest:
                    if str(r.get("id") or "") in stale_ids:
                        r["status"] = "已停止"
                        r["result"] = (
                            "服务重启，已自动恢复为新任务"
                            if restored
                            else "服务重启，任务已中断"
                        )
                save_task_history(latest)
        except Exception as exc:  # noqa: BLE001
            try:
                ui.notify(f"自动恢复签到任务失败: {exc}", type="negative")
            except Exception:
                logger.debug("发送自动恢复失败通知失败", exc_info=True)

    def __call__(self, *args, **kwargs):
        # 首次有浏览器接入时，恢复服务重启前在跑的定时任务（仅本进程一次）。
        # 必须在 _refresh_history_table 的孤儿校正之前执行，否则候选记录会先被改写。
        if not TaskRunnerBlock._restore_done:
            self._auto_restore()
        self._on_type_change()
        self._refresh_accounts()
        self._refresh_history_table()
        self._update_status()
        # 任务由其他浏览器标签页启动、或本页刷新后重新接入时，runs 里可能已有
        # 在跑的任务。必须激活本实例的轮询，否则表格行停留在文件中的初始结果
        # （如空文本），永远不会更新「预计下次」等实时信息。
        if TaskRunnerBlock.runs:
            self._poll_enabled = True
            self._poll_output()


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
        atomic_write_json(user_dir / "me.json", data)

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
            await safe_disconnect(self.client)
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
            hint = await self._fetch_password_hint()
            msg = "该账户开启了两步验证，请填写密码后再次点击登录"
            if hint:
                msg += f"（密码提示：{hint}）"
            else:
                msg += "（该账户未设置密码提示）"
            raise ValueError(msg)
        me = await asyncio.wait_for(self.client.get_me(), 30)
        self._save_me(me)
        await safe_disconnect(self.client)
        self.client = None
        # 清掉该账户可能残留的旧客户端缓存，避免后续任务使用过期会话
        from tg_signer.core import _CLIENT_INSTANCES

        _CLIENT_INSTANCES.pop(str(Path(".").joinpath(self.name).resolve()), None)
        return str(me.first_name or me.id)

    async def _fetch_password_hint(self) -> str:
        """获取账户的两步验证「密码提示」（创建云密码时可选设置的内容）。"""
        if self.client is None:
            return ""
        try:
            from pyrogram.raw import functions

            pwd = await asyncio.wait_for(
                self.client.invoke(functions.account.GetPassword()), 30
            )
            return (getattr(pwd, "hint", "") or "").strip()
        except Exception:
            logger.debug("获取两步验证密码提示失败", exc_info=True)
            return ""

    async def cancel(self) -> None:
        if self.client is not None:
            await safe_disconnect(self.client)
            self.client = None
        self.phone_code_hash = None
        self.password_needed = False


def user_info_block() -> Callable[[], None]:
    container = ui.column().classes("w-full gap-2")

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
        add_dialog_close_button(add_dlg)
        ui.label("增加账户（TG 登录）").classes("text-lg font-semibold")
        add_name = ui.input("账户名", placeholder="例如 my_account").classes("w-full")
        add_phone = ui.input(
            "手机号", placeholder="国际格式，如 +8613800138000"
        ).classes("w-full")
        add_status = ui.label("").classes("text-sm text-gray-500")
        # 密码提示常驻显示（放在 step2 外，打开弹窗即可见）
        ui.label(
            "提示：若账户开启了两步验证，登录时会提示需要密码，届时密码输入框才会出现"
        ).classes("text-xs text-gray-400")

        with ui.column().classes("w-full") as step2:
            step2.visible = False
            add_code = ui.input("验证码", placeholder="Telegram 发送的验证码").classes(
                "w-full"
            )
            add_password = ui.input(
                "两步验证密码",
                placeholder="仅开启了两步验证的账户需要填写",
                password=True,
                password_toggle_button=True,
            ).classes("w-full")
            add_password.visible = False

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
                # 密码框保持隐藏，仅当登录提示需要两步验证密码时才出现
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

        # 操作按钮固定在弹窗最底部（发送验证码/登录）
        with ui.row().classes("w-full gap-2"):
            send_btn = ui.button("发送验证码", on_click=do_send_code).props("outline")
            login_btn = ui.button("登录", color="primary", on_click=do_login)

    def toggle_account(name: str, enabled: bool) -> None:
        set_account_enabled(name, enabled, state.workdir)
        ui.notify(f"账户 {name} 已{'启用' if enabled else '停用'}", type="positive")

    def toggle_two_way(name: str, two_way: bool) -> None:
        set_account_two_way(name, two_way, state.workdir)
        ui.notify(
            f"账户 {name} 已{'标记为双向（发言数要求类抽奖自动跳过）' if two_way else '取消双向标记'}",
            type="positive",
        )

    def refresh() -> None:
        container.clear()
        disabled = load_disabled_accounts(state.workdir)
        two_ways = load_two_way_accounts(state.workdir)
        with container:
            # —— 账户管理 ——
            with ui.card().classes("w-full shadow-sm"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("账户管理").classes("text-lg font-semibold")
                        ui.icon("swap_vert").classes("text-gray-400").tooltip(
                            "用上下箭头调整顺序，保存后各页面账号下拉也按此顺序显示"
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
                            add_dialog_close_button(dlg)
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
                        with (
                            ui.dialog() as dlg,
                            ui.card().classes("w-[420px] max-w-full"),
                        ):
                            add_dialog_close_button(dlg)
                            ui.label(f"重命名账户 {name}").classes(
                                "text-lg font-semibold"
                            )
                            ui.label(
                                "将重命名 .session / .session_string 文件，"
                                "并同步更新顺序与停用记录。重命名前请先停止使用该账户的任务。"
                            ).classes("text-sm text-gray-500")
                            new_name = ui.input("新账户名", value=name).classes(
                                "w-full"
                            )
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
                                if (
                                    Path(target + ".session").exists()
                                    or Path(target + ".session_string").exists()
                                ):
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
                                    err.text = f"重命名失败（文件可能被运行中的连接占用）: {errs[0]}"
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
                                ui.button("确认重命名", on_click=do_rename).props(
                                    "color=primary"
                                )
                        dlg.open()

                    def move_account(name: str, delta: int) -> None:
                        """上下箭头调整账户顺序：与相邻账户交换位置后持久化。"""
                        idx = names.index(name)
                        new_idx = idx + delta
                        if new_idx < 0 or new_idx >= len(names):
                            return
                        lst = list(names)
                        lst[idx], lst[new_idx] = lst[new_idx], lst[idx]
                        set_account_order(lst, state.workdir)
                        refresh()

                    with ui.grid(columns=3).classes("w-full gap-2 accounts-grid"):
                        for pos, name in enumerate(names):
                            with ui.column().classes("border rounded p-2 gap-1"):
                                with ui.row().classes(
                                    "w-full items-center justify-between"
                                ):
                                    ui.switch(
                                        name,
                                        value=name not in disabled,
                                        on_change=lambda e, n=name: toggle_account(
                                            n, e.value
                                        ),
                                    )
                                    up_btn = ui.button(
                                        icon="keyboard_arrow_up",
                                        on_click=lambda n=name: move_account(n, -1),
                                    ).props("flat round dense")
                                    up_btn.tooltip("上移")
                                    if pos <= 0:
                                        up_btn.props("disable")
                                    down_btn = ui.button(
                                        icon="keyboard_arrow_down",
                                        on_click=lambda n=name: move_account(n, 1),
                                    ).props("flat round dense")
                                    down_btn.tooltip("下移")
                                    if pos >= len(names) - 1:
                                        down_btn.props("disable")
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
                                with ui.row().classes(
                                    "w-full items-center justify-between"
                                ):
                                    ui.switch(
                                        "双向",
                                        value=name in two_ways,
                                        on_change=lambda e, n=name: toggle_two_way(
                                            n, e.value
                                        ),
                                    ).props("dense").tooltip(
                                        "双向账号无法接收陌生人主动私聊，"
                                        "遇「发言数大于 N 条」类抽奖自动跳过"
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
