"""组合任务 tab：把已有任务按顺序组合为命名组合，各子任务用其绑定账号执行。

持久化在工作目录 ``webui_composite_tasks.json``；执行复用 TaskRunnerBlock
的 _launch（task_type="组合任务"），运行状态与历史统一记录在「签到」tab。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from nicegui import ui

from tg_signer.utils import atomic_write_json
from tg_signer.webui.common import (
    add_dialog_close_button,
    attach_select_all,
    with_select_all,
)
from tg_signer.webui.data import get_workdir

COMPOSITE_TASKS_FILE = "webui_composite_tasks.json"

# 组合任务的子任务类型（组合任务不可嵌套组合）
# 已移除的类型及原因：自动化/监控（常驻型任务，放入组合会永久阻塞后续子任务）、
# 签到 run 定时（同样是常驻等待定时的任务，会阻塞组合）；批量退频道使用
# 「批量退频道」tab 的任务配置；自动抽奖：选择「自动抽奖」tab 里已保存的任务
COMBO_SUB_TYPES = [
    "签到 (run-once)",
    "自动抽奖 (giveaway run)",
    "批量退频道 (auto leave)",
]


def _composite_tasks_path(workdir: Optional[Path | str] = None) -> Path:
    return get_workdir(workdir) / COMPOSITE_TASKS_FILE


def load_composite_tasks(workdir: Optional[Path | str] = None) -> list[dict]:
    path = _composite_tasks_path(workdir)
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_composite_tasks(
    items: list[dict], workdir: Optional[Path | str] = None
) -> None:
    atomic_write_json(_composite_tasks_path(workdir), items)


class ComboTaskBlock:
    """组合任务 tab 面板：组合列表、创建/编辑弹窗、执行弹窗、运行中状态。"""

    def __init__(self, runner, workdir: Optional[Path | str] = None) -> None:
        self.runner = runner  # TaskRunnerBlock 实例：复用 _launch/_stop_run/账号发现
        self.workdir = workdir
        self._editing_name: Optional[str] = None  # 非 None 表示正在编辑该组合

        ui.label("组合任务").classes("text-lg font-semibold")
        ui.label(
            "把多个已有任务按顺序组合成一个组合任务；执行时各子任务使用其绑定的账号，"
            "账号之间并行。运行状态与历史记录在「签到」tab 查看。"
        ).classes("text-sm text-gray-500")

        with ui.row().classes("gap-2 items-center"):
            ui.button(
                "新增组合任务",
                icon="playlist_add",
                color="primary",
                on_click=self.open_combo_dialog,
            )
            ui.button("刷新", icon="refresh", on_click=self.refresh).props("outline")

        # 运行中的组合任务
        self.running_container = ui.column().classes("w-full gap-2 mt-1")
        ui.separator().classes("my-2")
        ui.label("已保存的组合").classes("text-md font-semibold")
        self.list_container = ui.column().classes("w-full gap-2")

        self._build_combo_dialog()
        self._build_run_dialog()
        self.refresh()

    # ---- 数据 ----

    def _load(self) -> list[dict]:
        return load_composite_tasks(self.workdir)

    def _save(self, items: list[dict]) -> None:
        save_composite_tasks(items, self.workdir)

    # ---- 页面渲染 ----

    def refresh(self) -> None:
        self._render_running()
        self._render_list()

    def __call__(self) -> None:
        self.refresh()

    def _render_running(self) -> None:
        self.running_container.clear()
        with self.running_container:
            runs = [
                (rid, run)
                for rid, run in self.runner.runs.items()
                if "组合" in str(run["record"].get("task_type") or "")
            ]
            if not runs:
                ui.label("当前没有运行中的组合任务").classes("text-sm text-gray-400")
            for rid, run in runs:
                rec = run["record"]
                with ui.card().classes("w-full border"):
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        ui.badge("运行中").props("color=info")
                        ui.label(rec.get("task_name") or "-").classes(
                            "text-base font-semibold"
                        )
                        ui.label(f"账号: {rec.get('accounts', '')}").classes(
                            "text-xs text-gray-600"
                        )
                        ui.space()
                        ui.button(
                            "停止",
                            on_click=lambda r=rid: self._stop(r),
                        ).props("flat dense color=negative")

    def _render_list(self) -> None:
        self.list_container.clear()
        items = self._load()
        with self.list_container:
            if not items:
                ui.label("还没有组合任务，点击「新增组合任务」创建").classes(
                    "text-sm text-gray-400"
                )
            for c in items:
                name = str(c.get("name") or "")
                subs = c.get("subs") or []
                with ui.card().classes("w-full border"):
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        ui.label(name).classes("text-base font-semibold")
                        ui.badge(f"{len(subs)} 个子任务").props("color=grey-6 outline")
                        ui.space()
                        ui.button(
                            "执行", on_click=lambda n=name: self.open_run_dialog(n)
                        ).props("flat dense color=primary")
                        ui.button(
                            "编辑", on_click=lambda cc=c: self.open_combo_dialog(cc)
                        ).props("flat dense color=grey-8")
                        ui.button(
                            "删除", on_click=lambda n=name: self._confirm_delete(n)
                        ).props("flat dense color=negative")
                    with ui.column().classes("w-full gap-0 mt-1"):
                        for i, s in enumerate(subs, 1):
                            ui.label(
                                f"{i}. {s.get('type', '')} · {s.get('task', '')}"
                            ).classes("text-sm text-gray-700")
                    created = str(c.get("created_at") or "")
                    if created:
                        ui.label(f"创建于 {created}").classes("text-xs text-gray-400")

    def _stop(self, run_id: str) -> None:
        self.runner._stop_run(run_id)
        self._render_running()

    def _confirm_delete(self, name: str) -> None:
        with ui.dialog() as dlg, ui.card():
            add_dialog_close_button(dlg)
            ui.label(f"确定删除组合任务「{name}」？").classes("text-base")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=dlg.close).props("outline")
                ui.button(
                    "删除",
                    color="negative",
                    on_click=lambda: self._delete(name, dlg),
                )
        dlg.open()

    def _delete(self, name: str, dlg) -> None:
        items = [c for c in self._load() if c.get("name") != name]
        self._save(items)
        dlg.close()
        self._render_list()
        ui.notify(f"已删除组合任务: {name}", type="positive")

    # ---- 创建/编辑弹窗 ----

    def _build_combo_dialog(self) -> None:
        with ui.dialog() as self.combo_dialog, ui.card().classes("w-full max-w-2xl"):
            add_dialog_close_button(self.combo_dialog)
            self.combo_title = ui.label("新增组合任务").classes("text-lg font-semibold")
            ui.label(
                "把多个已有任务按顺序组合；执行时各子任务使用其绑定的账号，账号之间并行。"
            ).classes("text-xs text-gray-500")
            self.combo_name_input = ui.input(label="组合名称").classes("w-full")
            ui.label("子任务（按添加顺序执行，可用箭头调整）").classes(
                "text-sm text-gray-600"
            )
            self.combo_subs_container = ui.column().classes("w-full gap-2")
            self._combo_subs: list[dict] = []
            with ui.row().classes("w-full gap-2"):
                ui.button(
                    "添加子任务",
                    icon="add",
                    on_click=self._add_combo_sub_row,
                ).props("outline dense")
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("取消", on_click=self.combo_dialog.close).props("outline")
                ui.button("保存", color="primary", on_click=self._save_combo)

    def open_combo_dialog(self, combo: Optional[dict] = None) -> None:
        """打开创建（combo=None）或编辑（传入已有组合）弹窗。"""
        self._editing_name = str(combo.get("name")) if combo else None
        self.combo_title.text = (
            f"编辑组合任务: {self._editing_name}" if combo else "新增组合任务"
        )
        self.combo_name_input.value = self._editing_name or ""
        if combo:
            self._combo_subs = [
                {
                    "type": str(s.get("type") or COMBO_SUB_TYPES[0]),
                    "task": s.get("task"),
                }
                for s in (combo.get("subs") or [])
            ] or [self._new_combo_sub()]
        else:
            self._combo_subs = [self._new_combo_sub()]
        self._render_combo_sub_rows()
        self.combo_dialog.open()

    @staticmethod
    def _new_combo_sub() -> dict:
        return {"type": COMBO_SUB_TYPES[0], "task": None}

    def _render_combo_sub_rows(self) -> None:
        self.combo_subs_container.clear()
        with self.combo_subs_container:
            if not self._combo_subs:
                ui.label("还没有子任务，点击「添加子任务」").classes(
                    "text-sm text-gray-400"
                )
            for i, item in enumerate(self._combo_subs):
                with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                    ui.badge(str(i + 1)).props("color=primary")
                    ui.select(
                        COMBO_SUB_TYPES,
                        value=item["type"],
                        label="子任务类型",
                        on_change=lambda e, it=item: self._combo_sub_type_changed(
                            it, e.value
                        ),
                    ).classes("min-w-[200px]")
                    task_sel = ui.select(
                        self.runner._get_combo_task_names(item["type"]),
                        value=item.get("task"),
                        label="选择任务",
                        with_input=True,
                        on_change=lambda e, it=item: it.__setitem__("task", e.value),
                    ).classes("min-w-[180px] flex-grow")
                    item["task_select"] = task_sel
                    ui.button(
                        icon="keyboard_arrow_up",
                        on_click=lambda idx=i: self._move_combo_sub_row(idx, -1),
                    ).props("flat dense").tooltip("上移")
                    ui.button(
                        icon="keyboard_arrow_down",
                        on_click=lambda idx=i: self._move_combo_sub_row(idx, 1),
                    ).props("flat dense").tooltip("下移")
                    ui.button(
                        icon="delete",
                        on_click=lambda idx=i: self._remove_combo_sub_row(idx),
                    ).props("flat dense color=negative").tooltip("删除")
        self.combo_subs_container.update()

    def _combo_sub_type_changed(self, item: dict, value: str) -> None:
        item["type"] = value
        item["task"] = None
        sel = item.get("task_select")
        if sel is not None:
            sel.options = self.runner._get_combo_task_names(value)
            sel.value = None
            sel.update()

    def _add_combo_sub_row(self) -> None:
        self._combo_subs.append(self._new_combo_sub())
        self._render_combo_sub_rows()

    def _remove_combo_sub_row(self, idx: int) -> None:
        if 0 <= idx < len(self._combo_subs):
            self._combo_subs.pop(idx)
            self._render_combo_sub_rows()

    def _move_combo_sub_row(self, idx: int, delta: int) -> None:
        new_idx = idx + delta
        if 0 <= idx < len(self._combo_subs) and 0 <= new_idx < len(self._combo_subs):
            subs = self._combo_subs
            subs[idx], subs[new_idx] = subs[new_idx], subs[idx]
            self._render_combo_sub_rows()

    def _save_combo(self) -> None:
        name = (self.combo_name_input.value or "").strip()
        if not name:
            ui.notify("请填写组合名称", type="warning")
            return
        if not self._combo_subs:
            ui.notify("请至少添加一个子任务", type="warning")
            return
        subs = [{"type": it["type"], "task": it.get("task")} for it in self._combo_subs]
        missing = [i + 1 for i, s in enumerate(subs) if not s["task"]]
        if missing:
            ui.notify(
                f"第 {', '.join(map(str, missing))} 个子任务尚未选择任务",
                type="warning",
            )
            return
        items = self._load()
        updated = any(c.get("name") == name for c in items)
        items = [c for c in items if c.get("name") != name]
        items.append(
            {
                "name": name,
                "created_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                "subs": subs,
            }
        )
        self._save(items)
        self.combo_dialog.close()
        self._render_list()
        ui.notify(
            f"{'已更新' if updated else '已保存'}组合任务: {name}（{len(subs)} 个子任务）",
            type="positive",
        )

    # ---- 执行弹窗 ----

    def _build_run_dialog(self) -> None:
        with ui.dialog() as self.run_dialog, ui.card().classes("w-full max-w-xl"):
            add_dialog_close_button(self.run_dialog)
            self.run_title = ui.label("执行组合任务").classes("text-lg font-semibold")
            self.run_subs_label = ui.label("").classes(
                "text-xs text-gray-600 whitespace-pre-line"
            )
            # 可选账号覆盖：留空走各子任务绑定账号，选了则全部子任务仅用所选账号
            self.run_accounts_select = ui.select(
                label="指定账号（可选，选了则全部子任务仅用所选账号）",
                options={},
                multiple=True,
                on_change=None,
            ).classes("w-full")
            attach_select_all(self.run_accounts_select)
            ui.label(
                "留空：各子任务使用其绑定的账号执行（账号之间并行启动）。"
                "选择账号后：所有子任务仅用所选账号执行。"
            ).classes("text-xs text-gray-500")
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("取消", on_click=self.run_dialog.close).props("outline")
                ui.button("执行", color="primary", on_click=self._start_run)

    def open_run_dialog(self, name: str) -> None:
        combo = next((c for c in self._load() if c.get("name") == name), None)
        if not combo or not combo.get("subs"):
            ui.notify("组合任务不存在或没有子任务", type="warning")
            return
        # 延迟导入避免循环依赖（sign.py 在模块级导入 combo.py）
        from tg_signer.webui.data import apply_account_order
        from tg_signer.webui.sign import _combo_sub_accounts

        self.run_title.text = f"执行组合任务: {name}"
        lines = []
        for i, sub in enumerate(combo["subs"], 1):
            accs = apply_account_order(
                _combo_sub_accounts(sub, self.workdir), self.workdir
            )
            lines.append(
                f"{i}. {sub.get('type')} · {sub.get('task')}"
                f"｜账号：{'、'.join(accs) if accs else '未绑定'}"
            )
        self.run_subs_label.text = "\n".join(lines)
        # 刷新可选账号（仅活跃账号，按账户管理排序），并清空上次的选择
        options = {name: name for name in self.runner._get_account_names()}
        self.run_accounts_select.options = with_select_all(options)
        self.run_accounts_select.update()
        self.run_accounts_select.set_value([])
        self._run_name = name
        self.run_dialog.open()

    def _start_run(self) -> None:
        selected = list(self.run_accounts_select.value or [])
        if self.runner._launch("组合任务", self._run_name, selected):
            self.run_dialog.close()
            self._render_running()


def combo_block(runner, workdir: Optional[Path | str] = None) -> ComboTaskBlock:
    """构建「组合任务」tab 面板；runner 为 TaskRunnerBlock 实例。"""
    return ComboTaskBlock(runner, workdir)
