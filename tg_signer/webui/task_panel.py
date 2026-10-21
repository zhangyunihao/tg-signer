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
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from nicegui import ui

from tg_signer.webui.auto_leave import UNSELECT_KEYWORDS as _LEAVE_EXCLUDE_KEYWORDS
from tg_signer.webui.auto_leave import _scan_types_of as _leave_scan_types
from tg_signer.webui.auto_leave import load_config as _load_leave_config
from tg_signer.webui.auto_leave import load_leave_tasks as _load_leave_tasks
from tg_signer.webui.combo import load_composite_tasks
from tg_signer.webui.common import (
    SELECT_ALL_KEY,
    SOURCE_ROOT,
    _assign_to_job,
    add_dialog_close_button,
    attach_select_all,
    build_debug_report,
    copy_text_to_clipboard,
    notify_error,
    pretty_json,
    read_file_tail,
    state,
    with_select_all,
)
from tg_signer.webui.data import (
    CONFIG_META,
    apply_account_order,
    get_workdir,
    list_task_names,
    load_config,
    load_disabled_accounts,
)
from tg_signer.webui.giveaway import (
    channel_title_map as _chat_title_map,
)
from tg_signer.webui.giveaway import (
    load_giveaway_timeout_min,
)
from tg_signer.webui.giveaway import (
    load_task_store as _load_giveaway_store,
)
from tg_signer.webui.task_history import (
    append_task_history,
    load_task_accounts,
    load_task_history,
    save_task_binding,
    save_task_history,
    update_task_history,
)

logger = logging.getLogger(__name__)


# 多账号错峰启动总跨度（秒）：N 个账号的签到启动时间在此跨度内按槽位递增错开，
# 避免同一群里连续出现多个账号的签到消息；单账号不生效
ACCOUNT_STAGGER_SPREAD = 300


def build_combo_sub_spec(account: str, sub: dict, workdir) -> Optional[dict]:
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
            (t for t in _load_leave_tasks(workdir) if str(t.get("name")) == task_name),
            None,
        )
        if leave_task is not None:
            return {
                "label": label,
                "module": "tg_signer.leave_runner",
                "spec": {
                    "account": account,
                    "session_dir": str(workdir),
                    "days": (
                        int(leave_task["days"])
                        if leave_task.get("days") is not None
                        else 7
                    ),
                    "scan_types": _leave_scan_types(leave_task),
                    "include_groups": bool(leave_task.get("include_groups")),
                    "skip_pinned": bool(leave_task.get("skip_pinned", True)),
                    "exclude_keywords": list(
                        leave_task.get("keywords") or _LEAVE_EXCLUDE_KEYWORDS
                    ),
                    "include_keywords": list(leave_task.get("include_keywords") or []),
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
                "days": int(cfg["days"]) if cfg.get("days") is not None else 7,
                "scan_types": _leave_scan_types(cfg),
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
                "win_detect": bool(gcfg.get("win_detect", True)),
                # 超时（分钟→秒，全局设置）：0=不限
                "timeout": load_giveaway_timeout_min(workdir) * 60,
            },
        }
    if "run-once" in sub_type:
        args = ["-w", str(workdir), "-a", account, "run-once", task_name]
    elif "run 定时" in sub_type:
        args = ["-w", str(workdir), "-a", account, "run", task_name]
    elif "自动化" in sub_type:
        args = ["-w", str(workdir), "-a", account, "automation", "run", task_name]
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
            (t for t in _load_leave_tasks(workdir) if str(t.get("name")) == task_name),
            None,
        )
        if leave_task is not None:
            return [str(a) for a in (leave_task.get("accounts") or []) if a]
        return [
            str(a) for a in (_load_leave_config(workdir).get("accounts") or []) if a
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


class TaskRunnerBlock:
    """执行任务面板：从 WebUI 直接运行签到/自动化/组合任务"""

    TASK_TYPES = [
        "签到 (run-once)",
        "签到 (run 定时)",
        "自动化 (automation run)",
        "组合任务",
    ]

    # 服务进程级共享：所有浏览器标签页看到并管理同一批运行中的任务，
    # 服务重启后也据此把上次未结束的常驻任务自动恢复
    runs: dict[str, dict] = {}
    MAX_OUTPUT_LINES = 500  # 共享输出缓冲上限，防止长时间运行内存无限增长
    MAX_RENDER_LINES = 200  # 日志区最多渲染的行数
    output_lines: deque[str] = deque(maxlen=MAX_OUTPUT_LINES)
    _line_seq: int = 0  # 累计追加行数（单调递增），用于各标签页增量渲染
    _clear_seq: int = 0  # 清空代次：清空输出时 +1，各标签页检测到变化后全量重绘
    _restore_done: bool = False

    @classmethod
    def _append_output_line(cls, line: str) -> None:
        cls.output_lines.append(line)
        cls._line_seq += 1

    @classmethod
    def _lines_since(cls, seq: int) -> list[str]:
        """返回 seq 之后追加的行（deque 淘汰旧行后起点自动截断）。"""
        lines = list(cls.output_lines)
        return lines[max(0, seq - (cls._line_seq - len(lines))) :]

    @classmethod
    def last_lines(cls, n: int) -> list[str]:
        return list(cls.output_lines)[-n:]

    def __init__(self) -> None:
        ui.label("签到").classes("text-lg font-semibold")
        ui.label(
            "点击「新增任务」在弹窗中选择任务类型、任务与账号；组合任务的创建与执行在独立的「组合任务」tab；历史列表可「复制」任意历史任务的参数快速新建，运行中的任务可停止。"
        ).classes("text-sm text-gray-500")

        with ui.row().classes("gap-2 items-center flex-wrap"):
            ui.button(
                "新增任务", icon="add", color="primary", on_click=self.open_dialog
            )
            ui.button("清空输出", on_click=self.clear_log).props("outline")
            ui.button(
                "复制调试日志",
                icon="bug_report",
                on_click=self.copy_debug_log,
            ).props("outline").tooltip(
                "复制环境信息+运行输出+任务历史+日志文件，粘贴到电脑排查复现"
            )

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
                        "label": "执行时间",
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
                        "name": "duration",
                        "label": "执行时长",
                        "field": "duration",
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
        # 执行时长列：运行中用 run_javascript 直接改写文本（data-rundur=<任务id>）
        self.history_table.add_slot(
            "body-cell-duration",
            '<q-td :props="props">'
            '<span :data-rundur="props.row.id">{{ props.row.duration }}</span></q-td>',
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

        with ui.row().classes("w-full items-center justify-between"):
            ui.label("实时输出").classes("text-md font-semibold")
            ui.button(icon="fullscreen", on_click=self._toggle_log_fullscreen).props(
                "flat round dense"
            ).tooltip("全屏显示")

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
        # 退出全屏悬浮按钮：仅全屏时 fixed 显示在视口右上角，平时不占布局
        self._log_exit_btn = (
            ui.button(icon="close", on_click=self._toggle_log_fullscreen)
            .props("flat round dense unelevated")
            .classes("tgs-log-exit-full")
        )
        # 增量渲染状态：记录 DOM 中已渲染的 label 与行序号/清空代次
        self._rendered_labels: deque[ui.label] = deque()
        self._last_render_seq: Optional[int] = None
        self._last_clear_seq: int = 0

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
                attach_select_all(self.account_select)
            with ui.row().classes("items-center gap-2 w-full") as self.binding_row:
                self.binding_label = ui.label("").classes("text-sm text-primary")
                ui.button("更改账号", on_click=self._show_account_select).props(
                    "flat dense"
                )
            self.binding_row.set_visibility(False)
            ui.label(
                "账号来自 *.session 文件；多选时每个账号各启动一个进程。首次执行后账号与任务绑定，后续执行无需再选。"
            ).classes("text-xs text-gray-500")
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
        """组合任务弹窗的任务下拉：签到 (run-once) 列出 signs/ 下所有签到任务
        （含从未执行过的），并合并历史上执行过的任务名，附「账号｜频道」摘要；
        自动抽奖列出「编号｜账号｜监控对象」；其余类型沿用 _get_task_names。
        各类型均按创建时间倒序（最新的在前）。"""
        if "run-once" in task_type:
            history_accounts: dict[str, str] = {}
            for r in reversed(load_task_history()):
                if "签到 (run-once)" not in str(r.get("task_type") or ""):
                    continue
                name = str(r.get("task_name") or "")
                if name and name not in history_accounts:
                    history_accounts[name] = str(r.get("accounts") or "").strip()
            bound = load_task_accounts()
            # 签到任务按创建时间倒序：signs/ 目录创建时间新的在前
            signs_root = get_workdir(state.workdir) / CONFIG_META["signer"][0]
            config_names = sorted(
                list_task_names("signer", state.workdir),
                key=lambda n: (
                    (signs_root / n).stat().st_ctime
                    if (signs_root / n).is_dir()
                    else 0.0
                ),
                reverse=True,
            )
            ordered = config_names + [
                n for n in history_accounts if n not in set(config_names)
            ]
            names: dict[str, str] = {}
            for name in ordered:
                bound_accs = [
                    str(a) for a in bound.get(f"签到 (run-once)|{name}", []) if a
                ]
                accounts = (
                    "、".join(bound_accs)
                    if bound_accs
                    else history_accounts.get(name, "")
                )
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
                names[name] = (
                    f"{name}｜账号：{accounts or '未绑定'}｜频道：{chats_text or '未设置'}"
                )
            return names
        if "抽奖" in task_type:
            store = _load_giveaway_store(state.workdir)
            title_map = _chat_title_map(state.workdir)
            opts: dict[str, str] = {}
            for tid in sorted(
                store["tasks"].keys(),
                key=lambda k: int(k) if str(k).isdigit() else 0,
                reverse=True,  # 创建时间倒序：编号大的（新建的）在前
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
            # 批量退频道为任务列表模式：仅列出「自动退出」任务（排除仅扫描任务），
            # 按创建时间倒序；序号与「批量退频道」tab 一致（按创建顺序，最早为 1）
            all_tasks = _load_leave_tasks(state.workdir)
            total = len(all_tasks)
            opts = {}
            for j, t in enumerate(reversed(all_tasks)):
                name = str(t.get("name") or "")
                if not name or not t.get("auto_leave"):
                    continue
                accs = "、".join(str(a) for a in (t.get("accounts") or []))
                opts[name] = (
                    f"{total - j}｜{name}"
                    f"（{t.get('days')}天｜自动退出｜账号：{accs or '未设置'}）"
                )
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
        self.account_select.options = with_select_all(options)
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
                "duration": r.get("duration") or "—",
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
                    ui.label(f"执行时长：{row.get('duration') or '—'}").classes(
                        "text-xs text-gray-600"
                    )
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
                self.account_select.value = [
                    v for v in self.account_select.options if v != SELECT_ALL_KEY
                ]
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
        sub_indexes: list[int] | None = None,
    ) -> bool:
        """启动一个任务（每账号一个子进程）。auto=True 表示服务重启后的自动恢复。

        组合任务可通过 ``sub_indexes``（子任务下标，0 起）只执行勾选的子任务，
        None 表示全部执行。
        """
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
            if sub_indexes is not None:
                combo_subs = [
                    sub for i, sub in enumerate(combo_subs) if i in set(sub_indexes)
                ]
                if not combo_subs:
                    ui.notify("请至少勾选一个子任务", type="warning")
                    return False
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
            "duration": "",
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
                for sub, sub_accs in zip(combo_subs, sub_account_sets, strict=True):
                    if account not in sub_accs:
                        continue
                    built = build_combo_sub_spec(account, sub, state.workdir)
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
                {
                    "proc": proc,
                    "reader": reader,
                    "account": account,
                    "stats": stats,
                    # 组合任务：该账号进程将执行的子任务标签（与明细行 chat 对应），
                    # 用于判断每个子任务涉及几个账号，从而聚合出整体状态
                    "sub_labels": (
                        [str(s.get("label") or "") for s in spec_subs]
                        if is_combo
                        else []
                    ),
                }
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
            # 组合任务：本次实际执行的子任务列表（已按勾选过滤），
            # 「组合任务」tab 据此渲染子任务进度卡片
            "combo_subs": [dict(s) for s in combo_subs] if is_combo else [],
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
            if combo and stripped.startswith(">>> 子任务开始"):
                pos = stats["current_pos"]
                if pos is not None and detail_rows[pos]["status"] == "进行中":
                    detail_rows[pos]["status"] = "成功"
                # 格式：>>> 子任务开始 [i/total]: 标签（兼容旧格式 开始: 标签）
                if "]:" in stripped:
                    label = stripped.split("]:", 1)[1].strip()
                else:
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
        record["duration"] = self._format_duration_ts(run["start_ts"])
        record["result"] = f"手动停止，耗时 {record['duration']}"
        update_task_history(record)
        self._refresh_history_table()
        self._update_status()
        ui.notify(f"已停止: {record['task_name']}", type="positive")

    def _update_running_row(self, record_id: str, result_text: str) -> None:
        # 只同步内存行数据；进度文本用 JS 直接改写表格单元格与移动端卡片（固定
        # data-runres 属性，页面上有两份），不调用整表 update()，
        # 避免每 0.3s 重渲染导致「明细/停止」点击丢失
        run = self.runs.get(record_id)
        dur_text = self._format_duration_ts(run["start_ts"]) if run is not None else ""
        for row in self.history_table.rows:
            if row.get("id") == record_id:
                row["result"] = result_text
                row["duration"] = dur_text
                break
        safe = (
            result_text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        )
        safe_dur = dur_text.replace("\\", "\\\\").replace("'", "\\'")
        safe_id = str(record_id).replace("\\", "\\\\").replace('"', '\\"')
        ui.run_javascript(
            f"document.querySelectorAll('[data-runres=\"{safe_id}\"]').forEach("
            f"el => {{ el.textContent = '{safe}'; }});"
        )
        ui.run_javascript(
            f"document.querySelectorAll('[data-rundur=\"{safe_id}\"]').forEach("
            f"el => {{ el.textContent = '{safe_dur}'; }});"
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
        record["duration"] = self._format_duration_ts(run["start_ts"])
        progress = self._format_progress(run)
        parts = []
        if progress:
            parts.append(progress)
        parts.append(f"返回码: {rcs}")
        parts.append(f"耗时 {record['duration']}")
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
                self._append_output_line(run["queue"].get())
        self._render_log_lines()
        self._check_finished_runs()

    def _render_log_lines(self) -> None:
        """增量渲染日志：只追加新行并裁剪超限旧行，避免每 0.3 秒全量重建 DOM。

        各标签页通过共享 _line_seq 计算各自缺的行（谁先轮询谁取队列，行只会进
        共享缓冲一次），清空输出通过 _clear_seq 代次通知所有标签页全量重绘。
        """
        if self._last_clear_seq != TaskRunnerBlock._clear_seq:
            self.log_list.clear()
            self._rendered_labels.clear()
            self._last_render_seq = None
            self._last_clear_seq = TaskRunnerBlock._clear_seq
        if self._last_render_seq is None:
            # 首次渲染（页面新建/任务启动/清空后）：只显示最近 MAX_RENDER_LINES 行
            pending = self._lines_since(
                max(TaskRunnerBlock._line_seq - TaskRunnerBlock.MAX_RENDER_LINES, 0)
            )
        else:
            pending = self._lines_since(self._last_render_seq)
        self._last_render_seq = TaskRunnerBlock._line_seq
        if pending:
            with self.log_list:
                for line in pending:
                    color = self._classify_line(line)
                    label = (
                        ui.label(line)
                        .classes(f"w-full {color}")
                        .style("white-space: pre;")
                    )
                    self._rendered_labels.append(label)
            while len(self._rendered_labels) > TaskRunnerBlock.MAX_RENDER_LINES:
                self._rendered_labels.popleft().delete()
            self.log_list.update()

    def _check_finished_runs(self) -> None:
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
        TaskRunnerBlock._clear_seq += 1
        self.log_list.clear()
        self._rendered_labels.clear()
        self._last_render_seq = None
        self.log_list.update()

    def _toggle_log_fullscreen(self) -> None:
        """切换实时输出全屏态：日志区 fixed 铺满视口，右上角显示退出按钮。"""
        self._log_full = not getattr(self, "_log_full", False)
        if self._log_full:
            self.log_area.classes(add="tgs-page-log-fullscreen")
            self._log_exit_btn.classes(add="tgs-log-exit-visible")
        else:
            self.log_area.classes(remove="tgs-page-log-fullscreen")
            self._log_exit_btn.classes(remove="tgs-log-exit-visible")
        self.log_area.update()
        self._log_exit_btn.update()

    def _load_task_history_tail(self, limit: int) -> str:
        """读取任务历史文件尾部记录（读取失败返回空串，不打扰用户）。"""
        path = Path(state.workdir) / "webui_task_history.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            logger.debug("读取任务历史失败: %s", path, exc_info=True)
            return ""
        if isinstance(data, list) and data:
            return pretty_json(data[-limit:])
        return ""

    def copy_debug_log(self) -> None:
        """复制调试日志：环境信息 + 实时输出 + 任务历史 + 子进程日志文件尾部。

        手机端报错后一键复制，粘贴到电脑即可拿到定位/复现所需的全部上下文。
        """
        wd = Path(state.workdir)
        sections: list[tuple[str, str]] = []
        if TaskRunnerBlock.output_lines:
            sections.append(
                (
                    "本次运行输出（最近300行）",
                    "\n".join(self.last_lines(300)),
                )
            )
        history_tail = self._load_task_history_tail(15)
        if history_tail:
            sections.append(("任务历史（最近15条）", history_tail))
        # 子进程日志相对服务进程 cwd 与工作目录都可能落盘，两处都尝试
        sections.append(
            (
                "logs/tg-signer.log（工作目录）最近200行",
                read_file_tail(wd / "logs" / "tg-signer.log"),
            )
        )
        sections.append(
            (
                "logs/tg-signer.log（服务进程目录）最近200行",
                read_file_tail(Path("logs") / "tg-signer.log"),
            )
        )
        copy_text_to_clipboard(build_debug_report(wd, sections))
        ui.notify(
            "已复制调试日志（含环境信息与任务历史），粘贴给电脑即可排查",
            type="positive",
        )

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
