"""组合任务 tab：只展示历史任务列表；模板配置与新增任务均在弹窗中完成。

模板（命名组合）持久化在工作目录 ``webui_composite_tasks.json``；新增任务时
选择一个模板和账号（可空，留空则各子任务用其绑定账号）立即执行，执行复用
TaskRunnerBlock 的 _launch（task_type="组合任务"），历史统一记录在任务历史里。
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
    copy_text_to_clipboard,
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
    days = task.get("days")
    return [
        ("账号", _fmt_param_value(task.get("accounts"))),
        (
            "不更新天数阈值",
            f"{days}（全部）"
            if days == 0
            else (str(days) if days is not None else "—"),
        ),
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


def _sub_config_params_brief(sub: dict, workdir) -> list[tuple[str, str, str]] | None:
    """进度弹窗用的简略参数：跳过逐账号/逐频道明细，只保留关键项。

    返回 (参数名, 缩略展示, 悬停完整内容) 三元组；无截断时两者相同。
    """
    params = _sub_config_params(sub, workdir)
    if not params:
        return params
    sub_type = str(sub.get("type") or "")
    rows: list[tuple[str, str, str]] = []
    for lbl, val in params:
        # 账号已在子任务卡片上单独展示，不重复
        if lbl == "账号":
            continue
        # 签到：跳过逐频道明细（频道1、频道2…）
        if "run-once" in sub_type and str(lbl).startswith("频道"):
            continue
        # 抽奖：只保留频道/群组（缩略前3个）、消息关键词、超时
        if "抽奖" in sub_type:
            if lbl == "监控频道/群组":
                lines = [ln for ln in str(val).splitlines() if ln.strip()]
                names = [
                    ln.rsplit(" (", 1)[0] if ln.endswith(")") else ln for ln in lines
                ]
                total = len(names)
                if total > 3:
                    short = f"{'、'.join(names[:3])} 等{total}个"
                else:
                    short = f"{'、'.join(names)}（共 {total} 个）"
                rows.append((lbl, short, str(val)))
            elif lbl in ("消息关键词", "超时（分钟，全局）"):
                rows.append((lbl, str(val), str(val)))
            continue
        rows.append((lbl, str(val), str(val)))
    return rows


def _leave_task_display(task_name: str, workdir) -> str:
    """退频道任务显示名：带与「批量退频道」tab 一致的序号（N，按创建顺序，最早为 1）。

    仅用于界面展示；执行匹配的 label 保持「类型 · 任务名」不变。
    """
    if not task_name:
        return task_name
    for i, t in enumerate(load_leave_tasks(workdir), 1):
        if str(t.get("name")) == task_name:
            return f"{i}｜{task_name}"
    return task_name


def _sub_task_display(sub: dict, workdir) -> str:
    """子任务绑定任务的显示名：退频道任务带序号（不带井号），其余原样。"""
    task_name = str(sub.get("task") or "")
    if "退频道" in str(sub.get("type") or ""):
        return _leave_task_display(task_name, workdir)
    return task_name


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
    """组合任务 tab 面板：历史任务列表、新增任务弹窗、模板配置弹窗、进度弹窗。"""

    STATUS_COLOR = {
        "运行中": "info",
        "已完成": "positive",
        "失败": "negative",
        "已停止": "grey-6",
    }

    def __init__(self, runner, workdir: Path | str | None = None) -> None:
        self.runner = runner  # TaskRunnerBlock 实例：复用 _launch/_stop_run/账号发现
        self.workdir = workdir
        self._editing_name: str | None = None  # 非 None 表示正在编辑该组合

        ui.label("组合任务").classes("text-lg font-semibold")
        ui.label(
            "点击「新增任务」选择组合任务模板和账号立即执行（账号可留空，"
            "留空时各子任务使用其绑定的账号）；模板的创建与编辑在右上角"
            "「任务组合配置」。"
        ).classes("text-sm text-gray-500")

        with ui.row().classes("w-full gap-2 items-center no-wrap"):
            ui.button(
                "新增任务",
                icon="add",
                color="primary",
                on_click=lambda: self.open_new_task_dialog(),
            )
            ui.space()
            ui.button(
                "任务组合配置",
                icon="settings",
                on_click=lambda: self.open_config_dialog(),
            ).props("outline")

        ui.label("历史任务").classes("text-md font-semibold")
        # 桌面端表格（<=640px 隐藏，改用下方卡片列表），操作列：进度/停止/复制
        self.history_table = (
            ui.table(
                columns=[
                    {
                        "name": "status",
                        "label": "状态",
                        "field": "status",
                        "align": "left",
                    },
                    {
                        "name": "task_name",
                        "label": "任务名",
                        "field": "task_name",
                        "align": "left",
                    },
                    {
                        "name": "accounts",
                        "label": "账号",
                        "field": "accounts",
                        "align": "left",
                    },
                    {
                        "name": "start_time",
                        "label": "开始时间",
                        "field": "start_time",
                        "align": "left",
                    },
                    {
                        "name": "duration",
                        "label": "时长",
                        "field": "duration",
                        "align": "left",
                    },
                    {
                        "name": "result",
                        "label": "结果",
                        "field": "result",
                        "align": "left",
                        "style": "min-width: 220px;",
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
                pagination=10,
            )
            .classes("w-full tgs-desktop-table")
            .props("flat dense")
        )
        # 状态列渲染为彩色徽章
        self.history_table.add_slot(
            "body-cell-status",
            '<q-td :props="props">'
            "<q-badge :color=\"props.row.status === '已完成' ? 'positive' : "
            "props.row.status === '失败' ? 'negative' : "
            "props.row.status === '运行中' ? 'info' : 'grey-6'\">"
            "{{ props.row.status }}</q-badge></q-td>",
        )
        # 操作列：进度（所有行）、停止（仅运行中）、复制（用该行参数打开新增弹窗）
        self.history_table.add_slot(
            "body-cell-actions",
            '<q-td :props="props" class="col-actions">'
            '<q-btn flat dense color="primary" '
            'label="进度" @click="$parent.$emit(\'progressTask\', props.row)" />'
            '<q-btn v-if="props.row.status === \'运行中\'" flat dense color="negative" '
            'label="停止" @click="$parent.$emit(\'stopTask\', props.row)" />'
            '<q-btn flat dense color="grey-8" '
            'label="复制" @click="$parent.$emit(\'duplicateTask\', props.row)" />'
            "</q-td>",
        )
        self.history_table.on("progressTask", self._on_table_progress)
        self.history_table.on("stopTask", self._on_table_stop)
        self.history_table.on("duplicateTask", self._on_table_duplicate)
        # 移动端（<=640px）卡片列表，桌面端隐藏
        self.history_container = ui.column().classes(
            "w-full gap-2 tgs-mobile-cards mt-1"
        )

        self._build_new_task_dialog()
        self._build_config_dialog()
        self._build_progress_dialog()
        self._progress_record: dict | None = None
        self._last_hist_sig: tuple | None = None
        self._last_prog_sig: tuple | None = None
        self._render_history()
        # 轮询刷新：历史状态变化时刷新列表；进度弹窗打开时每 2 秒同步进度与日志
        ui.timer(2.0, self._tick)

    # ---- 数据 ----

    def _load(self) -> list[dict]:
        return load_composite_tasks(self.workdir)

    def _save(self, items: list[dict]) -> None:
        save_composite_tasks(items, self.workdir)

    def _combo_history_records(self) -> list[dict]:
        """任务历史里的组合任务记录（最新在前）。"""
        try:
            from tg_signer.webui.sign import load_task_history

            items = load_task_history()
        except Exception:
            logger.debug("读取任务历史失败", exc_info=True)
            return []
        records = [r for r in items if "组合" in str(r.get("task_type") or "")]
        return list(reversed(records))

    # ---- 页面渲染 ----

    def refresh(self) -> None:
        self._render_history()
        if getattr(self.config_dialog, "visible", False):
            self._render_template_list()

    def __call__(self) -> None:
        self.refresh()

    def _record_status(self, rec: dict) -> str:
        """历史记录的状态；运行中任务以内存里的实时状态为准。"""
        run = self.runner.runs.get(str(rec.get("id") or ""))
        if run is not None:
            return str(run["record"].get("status") or rec.get("status") or "未知")
        return str(rec.get("status") or "未知")

    def _render_history(self) -> None:
        self.history_container.clear()
        records = self._combo_history_records()
        self._last_hist_sig = tuple(
            (str(rec.get("id") or ""), self._record_status(rec)) for rec in records
        )
        # 桌面端表格同步行数据（表格自带分页，每页 10 条）
        self.history_table.rows = [
            {
                "id": str(rec.get("id") or ""),
                "status": self._record_status(rec),
                "task_name": str(rec.get("task_name") or "-"),
                "accounts": str(rec.get("accounts") or "—"),
                "start_time": str(rec.get("start_time") or "—"),
                "duration": str(rec.get("duration") or "—"),
                "result": str(rec.get("result") or ""),
            }
            for rec in records
        ]
        self.history_table.update()
        # 分页：每页 10 条；列表刷新时夹取页码到有效范围，避免越界空白
        page_size = 10
        total = len(records)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(getattr(self, "_hist_page", 0), total_pages - 1)
        self._hist_page = page
        page_records = records[page * page_size : (page + 1) * page_size]

        def _goto(p: int) -> None:
            self._hist_page = min(max(p, 0), total_pages - 1)
            self._render_history()

        with self.history_container:
            if not records:
                ui.label("暂无历史任务，点击「新增任务」启动组合任务").classes(
                    "text-sm text-gray-400"
                )
            for rec in page_records:
                rid = str(rec.get("id") or "")
                status = self._record_status(rec)
                with ui.card().classes("w-full border"):
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        ui.badge(status).props(
                            f"color={self.STATUS_COLOR.get(status, 'grey-6')} outline"
                        )
                        ui.label(str(rec.get("task_name") or "-")).classes(
                            "text-sm font-medium"
                        )
                        ui.space()
                        if self.runner.runs.get(rid) is not None:
                            ui.button(
                                "停止", on_click=lambda r=rid: self._stop(r)
                            ).props("flat dense color=negative")
                        ui.button(
                            "复制",
                            on_click=lambda r=rec: self._duplicate_from_rec(r),
                        ).props("flat dense color=primary")
                        ui.button(
                            "查看进度",
                            on_click=lambda r=rec: self.open_progress_dialog(r),
                        ).props("flat dense color=primary")
                    ui.label(
                        f"开始：{rec.get('start_time') or '—'}　"
                        f"时长：{rec.get('duration') or '—'}　"
                        f"账号：{rec.get('accounts') or '—'}"
                    ).classes("text-xs text-gray-600")
                    if rec.get("result"):
                        ui.label(f"结果：{rec['result']}").classes(
                            "text-xs text-gray-600"
                        )
            # 分页控件：仅超过一页时显示
            if total_pages > 1:
                with ui.row().classes("w-full items-center justify-center gap-1 mt-1"):
                    ui.button(
                        icon="chevron_left", on_click=lambda: _goto(page - 1)
                    ).props("flat round dense").props(
                        "disable" if page <= 0 else ""
                    ).tooltip("上一页")
                    ui.label(
                        f"第 {page + 1} / {total_pages} 页 · 共 {total} 条"
                    ).classes("text-xs text-gray-500")
                    ui.button(
                        icon="chevron_right", on_click=lambda: _goto(page + 1)
                    ).props("flat round dense").props(
                        "disable" if page >= total_pages - 1 else ""
                    ).tooltip("下一页")

    @staticmethod
    def _event_row(e) -> dict | None:
        """从 Quasar 表格事件对象中取出行 dict（事件参数可能被包成 list）。"""
        row = getattr(e, "args", None)
        if isinstance(row, list) and row and isinstance(row[0], dict):
            row = row[0]
        return row if isinstance(row, dict) else None

    def _on_table_progress(self, e) -> None:
        """桌面表格「进度」按钮：打开进度弹窗。"""
        row = self._event_row(e)
        if row:
            self.open_progress_dialog(row)

    def _on_table_stop(self, e) -> None:
        """桌面表格「停止」按钮。"""
        row = self._event_row(e)
        if row and row.get("id"):
            self._stop(str(row["id"]))

    def _on_table_duplicate(self, e) -> None:
        """桌面表格「复制」按钮：用该行参数打开新增任务弹窗。"""
        row = self._event_row(e)
        if row:
            self._duplicate_from_rec(row)

    def _duplicate_from_rec(self, rec: dict) -> None:
        """「复制」：按历史记录参数预填并打开新增组合任务弹窗。"""
        accounts = [
            a.strip()
            for a in str(rec.get("accounts") or "").replace("，", ",").split(",")
            if a.strip()
        ]
        self.open_new_task_dialog(
            template=str(rec.get("task_name") or "") or None,
            accounts=accounts,
        )

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
            accs = apply_account_order(
                _combo_sub_accounts(sub, self.workdir), self.workdir
            )
        except Exception:
            accs = []
        return "、".join(accs) if accs else "未绑定"

    @staticmethod
    def _account_progress(run: dict, combo_subs: list[dict]) -> list[tuple[str, str]]:
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

    def _render_running_card(self, run: dict) -> None:
        rec = run["record"]
        rid = str(rec.get("id") or "")
        elapsed = format_duration(max(0, time.time() - run["start_ts"]))
        combo_subs = self._run_subs(run)
        subs_status = [self._sub_run_status(s, run) for s in combo_subs]
        with ui.card().classes("w-full border"):
            with ui.row().classes(
                "w-full items-center gap-2 no-wrap tgs-prog-head-row"
            ):
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
            # 按账号展开：每个账号一个折叠面板，展示该账号执行了哪些子任务及状态
            self._render_account_sections(run, combo_subs)
            # 运行日志区：只显示本组合任务的输出行；不再独立滚动，
            # 统一由进度弹窗的滚动区承载（避免出现两条滚动条）
            prefix = f"[{rec.get('task_name') or ''}:"
            lines = [
                line for line in self.runner.output_lines if line.startswith(prefix)
            ]

            def _copy_log() -> None:
                if not lines:
                    ui.notify("暂无可复制的日志", type="warning")
                    return
                copy_text_to_clipboard("\n".join(lines))
                ui.notify(f"已复制 {len(lines)} 行日志", type="positive")

            def _toggle_log_fullscreen() -> None:
                """切换组合任务日志全屏态：日志区 fixed 铺满视口，右上角显示退出按钮。"""
                self._combo_log_full = not getattr(self, "_combo_log_full", False)
                # 内容由轮询重建，这里立即按最新状态重渲染弹窗内容
                self._render_progress_content()

            with ui.row().classes("w-full items-center justify-between mt-1"):
                ui.label("运行日志").classes("text-xs font-semibold text-gray-600")
                with ui.row().classes("gap-1 items-center"):
                    ui.button(icon="content_copy", on_click=_copy_log).props(
                        "flat round dense"
                    ).tooltip("一键复制日志")
                    ui.button(icon="fullscreen", on_click=_toggle_log_fullscreen).props(
                        "flat round dense"
                    ).tooltip("全屏显示")
            log_classes = (
                "w-full bg-gray-50 rounded-lg border border-gray-200 gap-0 p-2 "
                "font-mono text-xs"
            )
            if getattr(self, "_combo_log_full", False):
                log_classes += " tgs-page-log-fullscreen"
            # pre-wrap：保留日志缩进但允许长行折行，避免移动端横向溢出
            with (
                ui.column()
                .classes(log_classes)
                .style("white-space: pre-wrap; overflow-wrap: anywhere;")
            ):
                if not lines:
                    ui.label("暂无日志输出").classes("text-xs text-gray-400")
                for line in lines[-200:]:
                    ui.label(line).classes("w-full").style(
                        "white-space: pre-wrap; overflow-wrap: anywhere;"
                    )
            # 退出全屏悬浮按钮：仅全屏时 fixed 显示在视口右上角，平时不占布局
            if getattr(self, "_combo_log_full", False):
                ui.button(icon="close", on_click=_toggle_log_fullscreen).props(
                    "flat round dense unelevated"
                ).classes("tgs-log-exit-full tgs-log-exit-visible")

    def _acc_sub_labels(self, run: dict, acc: str, labels: list[str]) -> list[str]:
        """某账号实际参与的子任务标签：优先读进程的 sub_labels，旧运行回退明细。"""
        for p in run.get("procs") or []:
            if str(p.get("account") or "") == acc and p.get("sub_labels"):
                sub = [lb for lb in p["sub_labels"] if lb in labels]
                if sub:
                    return sub
        return [
            lb
            for lb in labels
            if any(
                d.get("chat") == lb and str(d.get("account") or "") == acc
                for d in run["details"]
            )
        ]

    def _acc_sub_status(self, run: dict, acc: str, label: str) -> str:
        """某账号在某个子任务上的状态；无明细但进程存在时视为等待。"""
        rows = [
            d
            for d in run["details"]
            if d.get("chat") == label and str(d.get("account") or "") == acc
        ]
        for st in ("进行中", "失败", "成功"):
            if any(d.get("status") == st for d in rows):
                return st
        return "等待"

    def _render_account_sections(self, run: dict, combo_subs: list[dict]) -> None:
        """按账号折叠展开：面板头为账号+进度摘要，展开可见该账号的各子任务状态与参数。"""
        labels = [f"{s.get('type')} · {s.get('task')}" for s in combo_subs]
        sub_by_label = dict(zip(labels, combo_subs, strict=False))
        status_color = {
            "成功": "positive",
            "失败": "negative",
            "进行中": "info",
            "等待": "grey-6",
        }
        # 展开状态需跨轮询重建保留；默认仅展开当前「进行中」的账号
        if not hasattr(self, "_acc_expand"):
            self._acc_expand = {}
        for acc, txt in self._account_progress(run, combo_subs):
            acc_labels = self._acc_sub_labels(run, acc, labels)
            default_open = "进行中" in txt
            open_state = self._acc_expand.setdefault(acc, default_open)
            with ui.expansion(acc, caption=txt, icon="person").classes(
                "w-full border"
            ) as exp:
                exp.value = open_state
                exp.on_value_change(
                    lambda e, a=acc: self._acc_expand.__setitem__(a, e.value)
                )
                with ui.column().classes("w-full gap-1"):
                    if not acc_labels:
                        ui.label("暂无该账号的执行明细").classes(
                            "text-xs text-gray-400"
                        )
                        continue
                    for lb in acc_labels:
                        sub = sub_by_label.get(lb) or {}
                        st = self._acc_sub_status(run, acc, lb)
                        with (
                            ui.card()
                            .classes("w-full border gap-1")
                            .style("background: #fafafa")
                        ):
                            with ui.row().classes(
                                "w-full items-center gap-2 no-wrap tgs-prog-sub-row"
                            ):
                                idx = labels.index(lb) + 1
                                ui.badge(str(idx)).props(
                                    "color=positive"
                                    if st == "成功"
                                    else (
                                        "color=negative"
                                        if st == "失败"
                                        else "color=primary"
                                    )
                                )
                                ui.label(
                                    f"{lb.split(' · ')[0]} · "
                                    f"{_sub_task_display(sub, self.workdir)}"
                                ).classes("text-sm font-medium tgs-prog-sub-title")
                                ui.space()
                                ui.badge(st).props(
                                    f"color={status_color.get(st, 'grey-6')} outline"
                                )
                            try:
                                params = _sub_config_params_brief(sub, self.workdir)
                            except Exception:
                                params = []
                            if params:
                                with ui.column().classes("w-full gap-0 mt-1"):
                                    for lbl, short, full in params:
                                        with ui.row().classes(
                                            "w-full items-start gap-3 no-wrap tgs-prog-param-row"
                                        ):
                                            ui.label(lbl).classes(
                                                "text-xs text-gray-500 shrink-0 min-w-[130px] max-w-[200px] tgs-prog-param-label"
                                            )
                                            value = ui.label(short).classes(
                                                "text-xs text-gray-800 whitespace-pre-line flex-grow tgs-prog-param-value"
                                            )
                                            if full != short:
                                                value.tooltip(
                                                    f"{lbl}（全部）：\n{full}"
                                                )
                            else:
                                ui.label(
                                    "未找到该任务的配置参数（任务可能已被删除）"
                                ).classes("text-xs text-gray-500")

    def _tick(self) -> None:
        """每 2 秒轮询：历史状态变化时刷新列表；进度弹窗打开时同步进度与日志。"""
        # 历史列表：仅状态/记录数变化时重渲染，避免每秒重建
        hist_sig = tuple(
            (str(rec.get("id") or ""), self._record_status(rec))
            for rec in self._combo_history_records()
        )
        if hist_sig != self._last_hist_sig:
            self._last_hist_sig = hist_sig
            self._render_history()

        # 进度弹窗：打开且对应任务仍在运行时，每秒同步子任务进度与日志
        if not getattr(self.progress_dialog, "visible", False):
            return
        run = self.runner.runs.get(str((self._progress_record or {}).get("id") or ""))
        if run is not None:
            rec = run["record"]
            prog_sig: tuple = (
                "live",
                len(run["details"]),
                tuple(self._sub_run_status(s, run) for s in self._run_subs(run)),
                len(
                    [
                        line
                        for line in self.runner.output_lines
                        if line.startswith(f"[{rec.get('task_name') or ''}:")
                    ]
                ),
                int(time.time() - run["start_ts"]),
            )
        else:
            prog_sig = ("done", str((self._progress_record or {}).get("status") or ""))
        if prog_sig != self._last_prog_sig:
            self._last_prog_sig = prog_sig
            self._render_progress_content()

    def _render_template_list(self) -> None:
        self.template_container.clear()
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
        with self.template_container:
            if not items:
                ui.label("还没有组合任务模板，点击「新增组合任务模板」创建").classes(
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
                            "编辑", on_click=lambda cc=c: self.open_combo_dialog(cc)
                        ).props("flat dense color=grey-8")
                        ui.button(
                            "删除", on_click=lambda n=name: self._confirm_delete(n)
                        ).props("flat dense color=negative")
                    with ui.column().classes("w-full gap-0 mt-1"):
                        for i, s in enumerate(subs, 1):
                            text = (
                                f"{i}. {s.get('type', '')} · "
                                f"{_sub_task_display(s, self.workdir)}"
                            )
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
        self._render_history()

    def _confirm_delete(self, name: str) -> None:
        with ui.dialog() as dlg, ui.card():
            add_dialog_close_button(dlg)
            ui.label(f"确定删除组合任务模板「{name}」？").classes("text-base")
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
        self._render_template_list()
        ui.notify(f"已删除组合任务模板: {name}", type="positive")

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
        self._render_template_list()
        ui.notify(
            f"{'已更新' if updated else '已保存'}组合任务模板: {name}"
            f"（{len(subs)} 个子任务）",
            type="positive",
        )

    # ---- 模板配置弹窗（右上角入口） ----

    def _build_config_dialog(self) -> None:
        with (
            ui.dialog() as self.config_dialog,
            ui.card().classes("w-full max-w-2xl"),
        ):
            add_dialog_close_button(self.config_dialog)
            ui.label("任务组合配置").classes("text-lg font-semibold")
            ui.label(
                "把多个已有任务按顺序组合成模板；在「新增任务」中选择模板执行，"
                "执行时各子任务使用其绑定的账号，账号之间并行。"
            ).classes("text-xs text-gray-500")
            with ui.row().classes("w-full gap-2"):
                ui.button(
                    "新增组合任务模板",
                    icon="playlist_add",
                    color="primary",
                    on_click=lambda: self.open_combo_dialog(),
                )
            self.template_container = ui.column().classes("w-full gap-2")
        self._build_combo_dialog()

    def open_config_dialog(self) -> None:
        """打开模板配置弹窗：创建/编辑/删除组合任务模板。"""
        self._render_template_list()
        self.config_dialog.open()

    # ---- 新增任务弹窗：选择模板 + 账号（可空）立即执行 ----

    def _build_new_task_dialog(self) -> None:
        with (
            ui.dialog() as self.new_task_dialog,
            ui.card().classes("w-full max-w-lg"),
        ):
            add_dialog_close_button(self.new_task_dialog)
            ui.label("新增组合任务").classes("text-lg font-semibold")
            ui.label("选择一个组合任务模板和账号后立即执行。").classes(
                "text-xs text-gray-500"
            )
            self.new_task_template = ui.select(
                {}, label="组合任务模板", with_input=True
            ).classes("w-full")
            self.new_task_accounts = ui.select(
                label="账号（可选，选了则所有子任务仅用所选账号）",
                options={},
                multiple=True,
            ).classes("w-full")
            attach_select_all(self.new_task_accounts)
            ui.label(
                "留空：各子任务使用其绑定的账号执行（账号之间并行启动）。"
                "选择账号后：所有子任务仅用所选账号执行。"
            ).classes("text-xs text-gray-500")
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("取消", on_click=self.new_task_dialog.close).props("outline")
                ui.button("启动", color="primary", on_click=self._start_new_task)

    def open_new_task_dialog(
        self, template: str | None = None, accounts: list[str] | None = None
    ) -> None:
        """打开新增组合任务弹窗；传入 template/accounts 时按历史行参数预填（复制）。"""
        items = self._load()
        if not items:
            ui.notify(
                "还没有组合任务模板，请先在右上角「任务组合配置」中创建",
                type="warning",
            )
            return
        name_map = {str(c.get("name")): str(c.get("name")) for c in items}
        self.new_task_template.options = name_map
        self.new_task_template.value = template if template in name_map else None
        self.new_task_template.update()
        valid_accounts = list(self.runner._get_account_names())
        options = {a: a for a in valid_accounts}
        self.new_task_accounts.options = with_select_all(options)
        self.new_task_accounts.update()
        self.new_task_accounts.set_value(
            [a for a in (accounts or []) if a in valid_accounts]
        )
        self.new_task_dialog.open()
        if template:
            ui.notify("已复制任务参数，可调整后点击「启动」", type="positive")

    def _start_new_task(self) -> None:
        name = self.new_task_template.value
        if not name:
            ui.notify("请选择组合任务模板", type="warning")
            return
        accounts = [a for a in (self.new_task_accounts.value or []) if a]
        if self.runner._launch("组合任务", str(name), accounts):
            self.new_task_dialog.close()
            self._render_history()

    # ---- 进度弹窗：展示运行中/已结束任务的内容 ----

    def _build_progress_dialog(self) -> None:
        with ui.dialog() as self.progress_dialog:
            # 默认全屏展示（96vw×92vh，复用日志弹窗全局样式），无需手动切换
            self._progress_card = ui.card().classes("tgs-log-fullscreen")
            with self._progress_card:
                with ui.row().classes(
                    "w-full items-center gap-2 no-wrap shrink-0 pr-10"
                ):
                    self.progress_title = ui.label("任务进度").classes(
                        "text-lg font-semibold flex-grow"
                    )
                # 内容超出时只在弹窗内滚动；tgs-log-area 让全屏态样式
                # 的 .tgs-log-area 规则作用于本滚动区（撑满剩余高度）
                with ui.scroll_area().classes("w-full min-h-0 flex-grow tgs-log-area"):
                    self.progress_container = ui.column().classes("w-full gap-2")
            # 关闭按钮需在卡片创建之后调用（helper 会遍历 dialog 下的卡片）
            add_dialog_close_button(self.progress_dialog)

    def open_progress_dialog(self, rec: dict) -> None:
        """点击历史任务的「查看进度」：弹窗展示运行内容（运行中实时刷新）。"""
        self._progress_record = rec
        self._combo_log_full = False
        self._acc_expand = {}
        self.progress_title.text = f"任务进度：{rec.get('task_name') or '-'}"
        self._render_progress_content()
        self.progress_dialog.open()

    def _render_progress_content(self) -> None:
        rec = self._progress_record or {}
        self.progress_container.clear()
        run = self.runner.runs.get(str(rec.get("id") or ""))
        with self.progress_container:
            if run is not None:
                # 运行中（或刚结束未清理）：展示实时的子任务进度与运行日志
                self._render_running_card(run)
                return
            status = str(rec.get("status") or "未知")
            with ui.card().classes("w-full border gap-1"):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.badge(status).props(
                        f"color={self.STATUS_COLOR.get(status, 'grey-6')} outline"
                    )
                    ui.label(str(rec.get("task_name") or "-")).classes(
                        "text-base font-semibold"
                    )
                ui.label(
                    f"开始：{rec.get('start_time') or '—'}　"
                    f"时长：{rec.get('duration') or '—'}　"
                    f"账号：{rec.get('accounts') or '—'}"
                ).classes("text-xs text-gray-600")
                if rec.get("result"):
                    ui.label(f"结果：{rec['result']}").classes("text-xs text-gray-600")
                if status == "运行中":
                    ui.label(
                        "未找到本服务的运行记录（可能启动于上次服务运行期间），"
                        "仅展示历史信息。"
                    ).classes("text-xs text-gray-400")
                else:
                    ui.label("任务已结束，无实时进度数据。").classes(
                        "text-xs text-gray-400"
                    )


def combo_block(runner, workdir: Path | str | None = None) -> ComboTaskBlock:
    """构建「组合任务」tab 面板；runner 为 TaskRunnerBlock 实例。"""
    return ComboTaskBlock(runner, workdir)
