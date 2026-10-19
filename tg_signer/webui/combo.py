"""组合任务 tab：把已有任务按顺序组合为命名组合，各子任务用其绑定账号执行。

持久化在工作目录 ``webui_composite_tasks.json``；执行复用 TaskRunnerBlock
的 _launch（task_type="组合任务"），运行状态与历史统一记录在「签到」tab。
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from nicegui import ui

from tg_signer.utils import atomic_write_json
from tg_signer.webui.auto_leave import load_leave_tasks
from tg_signer.webui.common import (
    add_dialog_close_button,
    attach_select_all,
    format_duration,
    with_select_all,
)
from tg_signer.webui.data import get_workdir, load_config
from tg_signer.webui.giveaway import (
    channel_title_map,
    load_giveaway_timeout_min,
    load_task_store,
)

logger = logging.getLogger(__name__)

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


def _composite_tasks_path(workdir: Path | str | None = None) -> Path:
    return get_workdir(workdir) / COMPOSITE_TASKS_FILE


def load_composite_tasks(workdir: Path | str | None = None) -> list[dict]:
    path = _composite_tasks_path(workdir)
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_composite_tasks(items: list[dict], workdir: Path | str | None = None) -> None:
    atomic_write_json(_composite_tasks_path(workdir), items)


# 签到动作代码 -> 描述（见 tg_signer.config.SupportAction）
SIGN_ACTION_DESC = {
    1: "发送普通文本",
    2: "发送Dice emoji",
    3: "点击键盘按钮",
    4: "图片选择选项",
    5: "回复计算题",
}
SCAN_TYPE_DESC = {"channel": "仅频道", "group": "仅群组", "both": "频道+群组"}


def _fmt_param_value(value) -> str:
    """把配置值格式化为可读文本；None/空列表显示为 —，布尔显示为 是/否。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        if not value:
            return "—"
        if all(isinstance(x, (str, int, float)) for x in value):
            return "、".join(str(x) for x in value)
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _fmt_sign_action(action: dict) -> str:
    desc = SIGN_ACTION_DESC.get(action.get("action"), "未知动作")
    extra = ""
    if action.get("text") is not None:
        extra = f"「{action['text']}」"
    elif action.get("dice") is not None:
        extra = f"「{action['dice']}」"
    return f"{desc}{extra}"


def _sign_config_params(task_name: str, workdir) -> list[tuple[str, str]] | None:
    """签到 (run-once)：signs/<task>/config.json 全部参数。"""
    try:
        payload = load_config("signer", task_name, workdir).payload
    except (FileNotFoundError, ValueError):
        return None
    rows: list[tuple[str, str]] = [
        ("签到时间", str(payload.get("sign_at") or "—")),
        ("随机偏移（秒）", str(payload.get("random_seconds") or 0)),
        ("连续签到间隔（秒）", str(payload.get("sign_interval") or 1)),
    ]
    chats = payload.get("chats") or []
    rows.append(("签到频道数", str(len(chats))))
    titles = channel_title_map(workdir)
    for i, chat in enumerate(chats, 1):
        chat_id = chat.get("chat_id")
        title = titles.get(str(chat_id)) or str(chat.get("name") or "")
        actions = (
            " → ".join(_fmt_sign_action(a) for a in (chat.get("actions") or [])) or "—"
        )
        delete_after = chat.get("delete_after")
        lines = [
            f"chat_id: {chat_id}",
            f"动作: {actions}",
            f"动作间隔: {chat.get('action_interval')} 秒",
            f"话题 ID: {chat.get('message_thread_id') or '—'}",
            f"发送后删除: {f'{delete_after} 秒' if delete_after else '—'}",
        ]
        rows.append((f"频道{i}" + (f"·{title}" if title else ""), "\n".join(lines)))
    return rows


def _giveaway_config_params(task_name: str, workdir) -> list[tuple[str, str]] | None:
    """自动抽奖：giveaway_tasks.json 里对应任务的全部参数。"""
    store = load_task_store(workdir)
    cfg = (store.get("tasks") or {}).get(str(task_name))
    if not isinstance(cfg, dict):
        return None
    titles = channel_title_map(workdir)
    chats = [str(c) for c in (cfg.get("chats") or [])]
    chats_text = "\n".join(
        f"{titles.get(cid)} ({cid})" if titles.get(cid) else cid for cid in chats
    )
    max_joins = cfg.get("max_joins")
    rescue_min = int(cfg.get("rescue_interval_min") or 0)
    return [
        ("账号", _fmt_param_value(cfg.get("accounts"))),
        ("监控频道/群组", chats_text or "—"),
        ("会话目录", str(cfg.get("session_dir") or ".")),
        ("消息关键词", _fmt_param_value(cfg.get("keywords"))),
        ("启动时自动扫描", _fmt_param_value(cfg.get("auto_scan"))),
        ("自动加入频道链接", _fmt_param_value(cfg.get("auto_join"))),
        ("参与前随机延迟下限（秒）", str(cfg.get("delay_min") or 0)),
        ("参与前随机延迟上限（秒）", str(cfg.get("delay_max") or 0)),
        ("确认等待（秒）", str(cfg.get("confirm_wait") or 0)),
        ("参与条数上限", str(max_joins) if max_joins else "不限"),
        ("补漏扫描间隔（分钟）", f"{rescue_min}（0=关闭）"),
        ("补漏重拉条数/频道", str(cfg.get("rescue_limit") or 0)),
        ("中奖识别", _fmt_param_value(cfg.get("win_detect", True))),
        (
            "超时（分钟，全局）",
            f"{load_giveaway_timeout_min(workdir)}（0=不限）",
        ),
    ]


def _leave_config_params(task_name: str, workdir) -> list[tuple[str, str]] | None:
    """批量退频道：webui_leave_tasks.json 里对应任务的全部参数。"""
    task = next(
        (t for t in load_leave_tasks(workdir) if str(t.get("name")) == task_name),
        None,
    )
    if task is None:
        return None
    scan = str(task.get("scan_types") or "")
    return [
        ("账号", _fmt_param_value(task.get("accounts"))),
        ("不更新天数阈值", str(task.get("days") or "—")),
        ("扫描范围", SCAN_TYPE_DESC.get(scan, scan or "—")),
        ("跳过置顶对话", _fmt_param_value(task.get("skip_pinned", True))),
        ("包含关键字", _fmt_param_value(task.get("include_keywords"))),
        ("保护关键字", _fmt_param_value(task.get("keywords"))),
        ("自动退出未命中保护项", _fmt_param_value(task.get("auto_leave"))),
    ]


def _sub_config_params(sub: dict, workdir) -> list[tuple[str, str]] | None:
    """按子任务类型加载其绑定任务的完整配置参数；找不到配置时返回 None。"""
    sub_type = str(sub.get("type") or "")
    task_name = str(sub.get("task") or "")
    if "run-once" in sub_type:
        return _sign_config_params(task_name, workdir)
    if "抽奖" in sub_type:
        return _giveaway_config_params(task_name, workdir)
    if "退频道" in sub_type:
        return _leave_config_params(task_name, workdir)
    return None


def giveaway_chats_text(task_name, workdir: Path | str | None = None) -> str:
    """自动抽奖任务的监控对象文案：优先显示频道/群组名称，未缓存的保留原 id。

    最多展示 5 个，超出时以「等N个」收尾，与「自动抽奖」tab 的展示一致。
    """
    wd = get_workdir(workdir)
    store = load_task_store(wd)
    gcfg = store["tasks"].get(str(task_name)) or {}
    chats = [str(c) for c in (gcfg.get("chats") or []) if c]
    if not chats:
        return ""
    title_map = channel_title_map(wd)
    labels = [title_map.get(c, c) for c in chats]
    text = "、".join(labels[:5])
    if len(labels) > 5:
        text += f" 等{len(labels)}个"
    return text


class ComboTaskBlock:
    """组合任务 tab 面板：组合列表、创建/编辑弹窗、执行弹窗、运行中状态。"""

    def __init__(self, runner, workdir: Path | str | None = None) -> None:
        self.runner = runner  # TaskRunnerBlock 实例：复用 _launch/_stop_run/账号发现
        self.workdir = workdir
        self._editing_name: str | None = None  # 非 None 表示正在编辑该组合

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
        # 运行中组合任务的轮询刷新：每秒同步子任务进度与运行日志
        self._last_tick_sig: tuple | None = None
        ui.timer(1.0, self._tick)

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
            runs = self._combo_runs()
            if not runs:
                ui.label("当前没有运行中的组合任务").classes("text-sm text-gray-400")
            for rid, run in runs:
                self._render_running_card(rid, run)

    def _combo_runs(self) -> list[tuple[str, dict]]:
        return [
            (rid, run)
            for rid, run in self.runner.runs.items()
            if "组合" in str(run["record"].get("task_type") or "")
        ]

    def _run_subs(self, run: dict) -> list[dict]:
        """本次实际执行的子任务列表；旧启动（无 combo_subs）回退读已保存组合。"""
        subs = run.get("combo_subs")
        if subs is not None:
            return subs
        name = str(run["record"].get("task_name") or "")
        combo = next((c for c in self._load() if c.get("name") == name), None)
        return (combo or {}).get("subs") or []

    def _sub_run_status(self, sub: dict, run: dict) -> str:
        """聚合各账号进程的明细，得到一个子任务的整体状态。"""
        label = f"{sub.get('type')} · {sub.get('task')}"
        rows = [d for d in run["details"] if d.get("chat") == label]
        procs = run.get("procs") or []
        expected = sum(1 for p in procs if label in (p.get("sub_labels") or []))
        if not expected:
            # 旧版本启动的运行：按明细里出现过的账号数估计
            expected = len({d.get("account") for d in rows if d.get("account")})
        ok = sum(1 for d in rows if d.get("status") == "成功")
        bad = sum(1 for d in rows if d.get("status") == "失败")
        if expected and ok + bad >= expected:
            return "失败" if bad else "成功"
        if any(d.get("status") == "进行中" for d in rows) or ok or bad:
            return "进行中"
        return "等待"

    def _sub_accounts_text(self, sub: dict) -> str:
        from tg_signer.webui.data import apply_account_order
        from tg_signer.webui.sign import _combo_sub_accounts

        try:
            accs = apply_account_order(_combo_sub_accounts(sub, self.workdir), self.workdir)
        except Exception:
            accs = []
        return "、".join(accs) if accs else "未绑定"

    @staticmethod
    def _account_progress(
        run: dict, combo_subs: list[dict]
    ) -> list[tuple[str, str]]:
        """按账号汇总进度：(账号, 当前进行到哪步的描述)。

        组合任务按账号串行执行子任务，明细行的 chat 即子任务标签。
        """
        labels = [f"{s.get('type')} · {s.get('task')}" for s in combo_subs]
        idx = {label: i for i, label in enumerate(labels)}
        by_acc: dict[str, dict[int, str]] = {}
        for d in run["details"]:
            acc = str(d.get("account") or "")
            i = idx.get(str(d.get("chat") or ""))
            if acc and i is not None:
                by_acc.setdefault(acc, {})[i] = str(d.get("status") or "")
        accounts = [str(p.get("account") or "") for p in (run.get("procs") or [])]
        # 兼容旧版启动的运行（procs 无 sub_labels 时仍以明细里的账号兜底）
        for acc in by_acc:
            if acc not in accounts:
                accounts.append(acc)
        total = len(combo_subs)
        result = []
        for acc in accounts:
            per = by_acc.get(acc) or {}
            cur = next((i for i in sorted(per) if per[i] == "进行中"), None)
            if cur is not None:
                text = f"第 {cur + 1}/{total} 个 {labels[cur]}（进行中）"
            else:
                done = [i for i in sorted(per) if per[i] in ("成功", "失败")]
                if total and len(done) >= total:
                    bad = sum(1 for i in done if per[i] == "失败")
                    text = "已完成" + ("（有失败）" if bad else "")
                elif done:
                    last = done[-1]
                    text = f"第 {last + 1}/{total} 个 {labels[last]} 已结束，准备下一个"
                else:
                    text = "未开始"
            result.append((acc, text))
        return result

    def _render_running_card(self, rid: str, run: dict) -> None:
        rec = run["record"]
        elapsed = format_duration(max(0, time.time() - run["start_ts"]))
        combo_subs = self._run_subs(run)
        subs_status = [self._sub_run_status(s, run) for s in combo_subs]
        with ui.card().classes("w-full border"):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.badge("运行中").props("color=info")
                ui.label(rec.get("task_name") or "-").classes("text-base font-semibold")
                ui.label(f"账号: {rec.get('accounts', '')}").classes(
                    "text-xs text-gray-600"
                )
                ui.space()
                ui.button("停止", on_click=lambda r=rid: self._stop(r)).props(
                    "flat dense color=negative"
                )
            ui.label(
                f"执行时间：{rec.get('start_time') or '—'}　已运行：{elapsed}"
            ).classes("text-xs text-gray-600")
            # 进度行：带序号显示当前进行到哪个子任务
            counts = {
                k: subs_status.count(k) for k in ("成功", "失败", "进行中", "等待")
            }
            parts = [
                f"{k} {counts[k]}"
                for k in ("进行中", "成功", "失败", "等待")
                if counts[k]
            ]
            text = f"子任务进度（{len(combo_subs)} 个）：{' · '.join(parts) or '—'}"
            ui.label(text).classes("text-sm text-gray-800 font-medium")
            # 按账号展示各账号进行到哪一步（组合任务按账号串行执行子任务）
            for acc, txt in self._account_progress(run, combo_subs):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.badge(acc).props("outline color=primary dense")
                    ui.label(txt).classes("text-xs text-gray-700")
            # 每个子任务一张子卡片：状态 + 账号 + 与原类别一致的完整配置参数
            status_color = {
                "成功": "positive",
                "失败": "negative",
                "进行中": "info",
                "等待": "grey-6",
            }
            with ui.column().classes("w-full gap-2 mt-1"):
                for i, (sub, status) in enumerate(
                    zip(combo_subs, subs_status, strict=True), 1
                ):
                    with (
                        ui.card()
                        .classes("w-full border gap-1")
                        .style("background: #fafafa")
                    ):
                        with ui.row().classes("w-full items-center gap-2 no-wrap"):
                            ui.badge(str(i)).props(
                                "color=positive"
                                if status == "成功"
                                else (
                                    "color=negative"
                                    if status == "失败"
                                    else "color=primary"
                                )
                            )
                            ui.label(f"{sub.get('type')} · {sub.get('task')}").classes(
                                "text-sm font-medium"
                            )
                            ui.space()
                            ui.badge(status).props(
                                f"color={status_color.get(status, 'grey-6')} outline"
                            )
                        ui.label(f"账号：{self._sub_accounts_text(sub)}").classes(
                            "text-xs text-gray-600"
                        )
                        try:
                            params = _sub_config_params(sub, self.workdir)
                        except Exception:
                            params = []
                        if params:
                            with ui.column().classes("w-full gap-0 mt-1"):
                                for lbl, val in params:
                                    with ui.row().classes(
                                        "w-full items-start gap-3 no-wrap"
                                    ):
                                        ui.label(lbl).classes(
                                            "text-xs text-gray-500 shrink-0 min-w-[130px] max-w-[200px]"
                                        )
                                        ui.label(val).classes(
                                            "text-xs text-gray-800 whitespace-pre-line flex-grow"
                                        )
                        else:
                            ui.label(
                                "未找到该任务的配置参数（任务可能已被删除）"
                            ).classes("text-xs text-gray-500")
            # 运行日志区：只显示本组合任务的输出行
            with ui.row().classes("w-full items-center justify-between mt-1"):
                ui.label("运行日志").classes("text-xs font-semibold text-gray-600")
            with (
                ui.scroll_area()
                .classes("w-full bg-gray-50 rounded-lg border border-gray-200")
                .style("max-height: 220px")
            ):
                with (
                    ui.column()
                    .classes("w-full gap-0 p-2 font-mono text-xs")
                    .style("white-space: pre;")
                ):
                    prefix = f"[{rec.get('task_name') or ''}:"
                    lines = [
                        line
                        for line in self.runner.output_lines
                        if line.startswith(prefix)
                    ]
                    if not lines:
                        ui.label("暂无日志输出").classes("text-xs text-gray-400")
                    for line in lines[-200:]:
                        ui.label(line).classes("w-full").style("white-space: pre;")

    def _tick(self) -> None:
        """每秒轮询：有运行中的组合任务时同步子任务进度与日志。"""
        if not self._combo_runs():
            if self._last_tick_sig is not None:
                # 最后一个组合已结束，刷新一次清掉运行中卡片
                self._last_tick_sig = None
                self._render_running()
            return
        sig = tuple(
            (
                rid,
                len(run["details"]),
                tuple(self._sub_run_status(s, run) for s in self._run_subs(run)),
                len(
                    [
                        line
                        for line in self.runner.output_lines
                        if line.startswith(f"[{run['record'].get('task_name') or ''}:")
                    ]
                ),
                int(time.time() - run["start_ts"]),
            )
            for rid, run in self._combo_runs()
        )
        if sig != self._last_tick_sig:
            self._last_tick_sig = sig
            self._render_running()

    def _render_list(self) -> None:
        self.list_container.clear()
        items = self._load()
        # 从任务历史中找每个组合最近一次执行记录（避免与 sign.py 循环导入，延迟导入）
        last_runs: dict[str, dict] = {}
        try:
            from tg_signer.webui.sign import load_task_history

            for rec in reversed(load_task_history()):
                if "组合" not in str(rec.get("task_type") or ""):
                    continue
                name = str(rec.get("task_name") or "")
                if name and name not in last_runs:
                    last_runs[name] = rec
        except Exception:
            logger.debug("读取任务历史失败", exc_info=True)
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
                            text = f"{i}. {s.get('type', '')} · {s.get('task', '')}"
                            if "抽奖" in str(s.get("type") or ""):
                                chats_text = giveaway_chats_text(
                                    s.get("task"), self.workdir
                                )
                                if chats_text:
                                    text += f"｜监控对象：{chats_text}"
                            ui.label(text).classes("text-sm text-gray-700")
                    last = last_runs.get(name)
                    if last is not None:
                        ui.label(
                            f"执行时间：{last.get('start_time') or '—'}　"
                            f"执行时长：{last.get('duration') or '—'}"
                            f"　（{last.get('status') or '未知'}）"
                        ).classes("text-xs text-gray-600")
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

    def open_combo_dialog(self, combo: dict | None = None) -> None:
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
        self._run_sub_checks: list = []
        with ui.dialog() as self.run_dialog, ui.card().classes("w-full max-w-xl"):
            add_dialog_close_button(self.run_dialog)
            self.run_title = ui.label("执行组合任务").classes("text-lg font-semibold")
            ui.label(
                "勾选要执行的子任务（默认全选）；点击子任务可查看其全部配置参数"
            ).classes("text-xs text-gray-500")
            self.run_subs_container = ui.column().classes("w-full gap-1")
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
        self._build_sub_detail_dialog()

    def _render_run_subs(self) -> None:
        """执行弹窗的子任务列表；每行带勾选框（默认勾选），点击可查看配置参数。"""
        from tg_signer.webui.data import apply_account_order
        from tg_signer.webui.sign import _combo_sub_accounts

        self.run_subs_container.clear()
        self._run_sub_checks = []
        with self.run_subs_container:
            for i, sub in enumerate(self._run_combo["subs"], 1):
                accs = apply_account_order(
                    _combo_sub_accounts(sub, self.workdir), self.workdir
                )
                acc_text = "、".join(accs) if accs else "未绑定"
                chats_part = ""
                if "抽奖" in str(sub.get("type") or ""):
                    chats_text = giveaway_chats_text(sub.get("task"), self.workdir)
                    if chats_text:
                        chats_part = f"｜监控对象：{chats_text}"
                with ui.row().classes("w-full items-center gap-1 no-wrap"):
                    check = (
                        ui.checkbox(value=True)
                        .props("dense")
                        .tooltip("取消勾选则本次执行跳过该子任务")
                    )
                    self._run_sub_checks.append(check)
                    with (
                        ui.button(
                            on_click=lambda s=sub, a=acc_text: self._open_sub_detail(
                                s, a
                            )
                        )
                        .props("flat no-caps color=grey-9")
                        .classes("flex-grow border rounded-lg")
                        .tooltip("点击查看全部配置参数"),
                        ui.row().classes(
                            "tgs-run-sub-inner w-full items-center gap-2 no-wrap"
                        ),
                    ):
                        ui.badge(str(i)).props("color=primary")
                        ui.label(
                            f"{sub.get('type')} · {sub.get('task')}{chats_part}"
                        ).classes("tgs-run-sub-title text-sm")
                        ui.space().classes("tgs-run-sub-space")
                        ui.label(f"账号：{acc_text}").classes(
                            "tgs-run-sub-accs text-xs text-gray-500 whitespace-nowrap"
                        )
                        ui.icon("chevron_right").classes(
                            "tgs-run-sub-chev text-gray-400"
                        )

    def _build_sub_detail_dialog(self) -> None:
        with (
            ui.dialog() as self.sub_detail_dialog,
            ui.card().classes("w-full max-w-2xl"),
        ):
            add_dialog_close_button(self.sub_detail_dialog)
            self.sub_detail_title = ui.label("子任务配置参数").classes(
                "text-lg font-semibold"
            )
            self.sub_detail_summary = ui.label("").classes(
                "text-xs text-gray-600 whitespace-pre-line"
            )
            with ui.scroll_area().classes("w-full max-h-[60vh] border-t pt-2"):
                self.sub_detail_container = ui.column().classes("w-full gap-1")

    def _open_sub_detail(self, sub: dict, accounts_text: str) -> None:
        """弹窗展示子任务绑定任务的全部配置参数。"""
        sub_type = str(sub.get("type") or "")
        task_name = str(sub.get("task") or "")
        self.sub_detail_title.text = f"子任务配置参数：{sub_type} · {task_name}"
        self.sub_detail_summary.text = f"绑定账号：{accounts_text}"
        self.sub_detail_container.clear()
        with self.sub_detail_container:
            params = _sub_config_params(sub, self.workdir)
            if not params:
                ui.label("未找到该任务的配置参数（任务可能已被删除）").classes(
                    "text-sm text-gray-500"
                )
            for label, value in params or []:
                with ui.row().classes(
                    "tgs-sub-detail-row w-full items-start gap-3 no-wrap"
                ):
                    ui.label(label).classes(
                        "tgs-sub-detail-label text-xs text-gray-500 shrink-0 min-w-[150px] max-w-[240px]"
                    )
                    ui.label(value).classes(
                        "tgs-sub-detail-value text-sm whitespace-pre-line flex-grow"
                    )
        self.sub_detail_dialog.open()

    def open_run_dialog(self, name: str) -> None:
        combo = next((c for c in self._load() if c.get("name") == name), None)
        if not combo or not combo.get("subs"):
            ui.notify("组合任务不存在或没有子任务", type="warning")
            return
        self.run_title.text = f"执行组合任务: {name}"
        self._run_combo = combo
        self._render_run_subs()
        # 刷新可选账号（仅活跃账号，按账户管理排序），并清空上次的选择
        options = {name: name for name in self.runner._get_account_names()}
        self.run_accounts_select.options = with_select_all(options)
        self.run_accounts_select.update()
        self.run_accounts_select.set_value([])
        self._run_name = name
        self.run_dialog.open()

    def _start_run(self) -> None:
        selected = list(self.run_accounts_select.value or [])
        sub_indexes = [i for i, check in enumerate(self._run_sub_checks) if check.value]
        if self.runner._launch(
            "组合任务", self._run_name, selected, sub_indexes=sub_indexes
        ):
            self.run_dialog.close()
            self._render_running()


def combo_block(runner, workdir: Path | str | None = None) -> ComboTaskBlock:
    """构建「组合任务」tab 面板；runner 为 TaskRunnerBlock 实例。"""
    return ComboTaskBlock(runner, workdir)
