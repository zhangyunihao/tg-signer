"""组合任务 tab：只展示历史任务列表；模板配置与新增任务均在弹窗中完成。

模板（命名组合）持久化在工作目录 ``webui_composite_tasks.json``；新增任务时
选择一个模板和账号（可空，留空则各子任务用其绑定账号）立即执行，执行复用
TaskRunnerBlock 的 _launch（task_type="组合任务"），历史统一记录在任务历史里。
"""

import functools
import json
import logging
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Callable

from nicegui import app, ui

from tg_signer.utils import atomic_write_json
from tg_signer.webui.activity import install_activity_tracking, should_poll
from tg_signer.webui.auto_leave import load_leave_tasks
from tg_signer.webui.common import (
    add_dialog_close_button,
    attach_select_all,
    copy_text_to_clipboard,
    format_duration,
    mobile_card_fields,
    mobile_card_result,
    state,
    with_select_all,
)
from tg_signer.webui.components import (
    LOG_AREA_CLASS,
    LOG_AREA_STYLE,
    progress_summary,
    stop_with_feedback,
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


# 组合任务进度快照：运行中每 2 秒落盘一次，任务停止/结束后仍可查看
# 子任务明细与运行日志（内存 runs 已清理、甚至服务重启后也不丢）
COMPOSITE_PROGRESS_FILE = "webui_combo_progress.json"
COMBO_PROGRESS_LIMIT = 30
COMBO_PROGRESS_LOG_LINES = 200


def _composite_progress_path(workdir: Path | str | None = None) -> Path:
    return get_workdir(workdir) / COMPOSITE_PROGRESS_FILE


def load_combo_progress(workdir: Path | str | None = None) -> dict[str, dict]:
    path = _composite_progress_path(workdir)
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_combo_progress(store: dict, workdir: Path | str | None = None) -> None:
    # 单写者假设：只由 WebUI 主进程的轮询逻辑写入（子进程不落盘快照），
    # 因此这里不加文件锁；若将来改由子进程写入，需要补 file_lock。
    atomic_write_json(_composite_progress_path(workdir), store)


# 组合任务进度独立页面路由（替代原「查看进度」弹窗），?record_id=任务记录ID
COMBO_PROGRESS_PAGE_PATH = "/combo-progress"


class _SharedRunner:
    """进度独立页面访问服务进程级运行状态（不依赖某个浏览器标签页的实例）。"""

    @property
    def runs(self) -> dict:
        # 延迟导入避免与 task_panel 循环依赖（task_panel 也会导入本模块的常量）
        from tg_signer.webui.task_panel import TaskRunnerBlock

        return TaskRunnerBlock.runs

    @property
    def output_lines(self):
        from tg_signer.webui.task_panel import TaskRunnerBlock

        return TaskRunnerBlock.output_lines

    @staticmethod
    def drain_output() -> None:
        """收集服务进程内各运行任务的输出（与主控台是否打开无关）。"""
        from tg_signer.webui.task_panel import TaskRunnerBlock

        TaskRunnerBlock.drain_output()

    @staticmethod
    def _stop_run(record_id: str):
        from tg_signer.webui.task_panel import stop_run_core

        return stop_run_core(record_id)


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
    is_bot = str(cfg.get("source_type") or "channel") == "bot"
    chats = [str(c) for c in (cfg.get("chats") or [])]
    chats_text = "\n".join(
        f"{titles.get(cid)} ({cid})" if titles.get(cid) else cid for cid in chats
    )
    max_joins = cfg.get("max_joins")
    rescue_min = int(cfg.get("rescue_interval_min") or 0)
    return [
        ("账号", _fmt_param_value(cfg.get("accounts"))),
        ("任务类型", "百宝箱机器人" if is_bot else "频道/群组"),
        ("监控机器人", f"@{cfg.get('bot_username')}" if is_bot else "—"),
        ("监控频道/群组", ("—" if is_bot else chats_text) or "—"),
        ("会话目录", str(cfg.get("session_dir") or ".")),
        ("消息关键词", _fmt_param_value(cfg.get("keywords"))),
        ("启动时自动扫描", _fmt_param_value(cfg.get("auto_scan"))),
        ("自动加入频道链接", _fmt_param_value(cfg.get("auto_join"))),
        (
            "参与前随机延迟（秒）",
            f"{cfg.get('delay_min') or 0}–{cfg.get('delay_max') or 0}",
        ),
        ("确认等待（秒）", str(cfg.get("confirm_wait") or 0)),
        ("参与条数上限", str(max_joins) if max_joins else "不限"),
        ("允许重复参与", _fmt_param_value(cfg.get("ignore_history", True))),
        ("补漏扫描间隔（分钟）", f"{rescue_min}（0=关闭）"),
        ("补漏重拉条数/频道", str(cfg.get("rescue_limit") or 0)),
        ("中奖识别", _fmt_param_value(cfg.get("win_detect", True))),
        (
            "加入间隔（秒）",
            f"{cfg.get('join_pace_min') or 0}–{cfg.get('join_pace_max') or 0}",
        ),
        (
            "每账号 1 小时/24 小时加入上限",
            (
                f"{cfg.get('join_hourly_cap') or 0} / {cfg.get('join_daily_cap') or 0}"
                "（0=不限）"
            ),
        ),
        ("限流额外缓冲（秒）", str(cfg.get("join_flood_buffer") or 0)),
        (
            "解析间隔（秒）",
            f"{cfg.get('resolve_pace_min') or 0}–{cfg.get('resolve_pace_max') or 0}",
        ),
        (
            "全局调用上限 / 同时扫描账号数",
            (
                f"{cfg.get('global_qps') or 0} 次/秒 / "
                f"{cfg.get('max_concurrent_accounts') or 0}（0=不限）"
            ),
        ),
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
    days_text = "—" if days is None else f"{float(days):g}"
    return [
        ("账号", _fmt_param_value(task.get("accounts"))),
        (
            "不更新天数阈值",
            f"{days_text}（全部）" if days == 0 else days_text,
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
        # 抽奖：只保留频道/群组（缩略前3个）、消息关键词、参与上限、允许重复参与、超时
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
            elif lbl in (
                "消息关键词",
                "参与条数上限",
                "允许重复参与",
                "超时（分钟，全局）",
            ):
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
    # 进度单元（一个账号的一个子任务）状态配色，进度页各处共用
    UNIT_STATUS_COLOR = {
        "成功": "positive",
        "失败": "negative",
        "进行中": "info",
        "中断": "orange",
        "等待": "grey-6",
    }

    def __init__(self, runner, workdir: Path | str | None = None) -> None:
        self.runner = runner  # TaskRunnerBlock 实例：复用 _launch/_stop_run/账号发现
        self.workdir = workdir
        self._editing_name: str | None = None  # 非 None 表示正在编辑该组合

        ui.label("组合任务").classes("text-lg font-semibold")
        ui.label(
            "点击「新增任务」选择组合任务模板和账号执行（账号可留空，"
            "留空时各子任务使用其绑定的账号；可设置开始时间延迟启动）；"
            "模板的创建与编辑在右上角「任务组合配置」。"
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
                        "name": "seq",
                        "label": "序号",
                        "field": "seq",
                        "align": "left",
                    },
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
        self._last_hist_sig: tuple | None = None
        # 进度快照：跟踪运行中的组合任务 id，运行结束/停止时补写最终快照
        self._live_ids: set[str] = set()
        self._live_names: dict[str, str] = {}
        self._snap_sigs: dict[str, tuple] = {}
        self._render_history()
        self._poll_client = ui.context.client
        # 轮询刷新：历史状态变化时刷新列表，同时落盘运行中任务的进度快照
        ui.timer(2.0, self._tick)

    @classmethod
    def headless(cls, workdir, rerender: Callable[[], None]) -> "ComboTaskBlock":
        """构造只用于进度渲染的实例（独立进度页面使用，不构建 tab 界面）。"""
        view = cls.__new__(cls)
        view.workdir = workdir
        view.runner = _SharedRunner()
        view._combo_log_full = False
        view._live_ids = set()
        view._live_names = {}
        view._snap_sigs = {}
        # _render_run_card 的日志全屏切换会调用该方法，由页面传入重绘函数
        view._render_progress_content = rerender
        return view

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

    def _hist_signature(self, records: list[dict] | None = None) -> tuple:
        """历史列表签名：包含状态与运行中任务的进度，驱动是否需要重渲染。"""
        if records is None:
            records = self._combo_history_records()
        sig = []
        for rec in records:
            rid = str(rec.get("id") or "")
            status = self._record_status(rec)
            entry: list = [rid, status]
            run = self.runner.runs.get(rid)
            if run is not None:
                combo_subs = self._run_subs(run)
                summary = self._progress_summary(run, combo_subs)
                entry.extend(
                    [
                        summary["done"],
                        summary["total"],
                        tuple(summary["counts"].items()),
                    ]
                )
            sig.append(tuple(entry))
        return tuple(sig)

    def _run_progress_text(self, rid: str) -> str | None:
        """运行中任务的进度分数文本（如 "3/10 步 · 30%"）；非运行中返回 None。"""
        run = self.runner.runs.get(rid)
        if run is None:
            return None
        combo_subs = self._run_subs(run)
        summary = self._progress_summary(run, combo_subs)
        done = summary["done"]
        total = summary["total"]
        ratio = done / total * 100 if total else 0
        return f"{done}/{total} 步 · {int(ratio)}%"

    def _render_history(self) -> None:
        self.history_container.clear()
        records = self._combo_history_records()
        self._last_hist_sig = self._hist_signature(records)
        # 桌面端表格同步行数据（表格自带分页，每页 10 条）；seq 为全量序号（跨页连续）
        table_rows = []
        for idx, rec in enumerate(records):
            rid = str(rec.get("id") or "")
            status = self._record_status(rec)
            result_text = str(rec.get("result") or "")
            # 运行中任务：在 result 列最前面附上进度分数
            progress_text = self._run_progress_text(rid)
            if progress_text is not None:
                result_text = (
                    f"进度 {progress_text}\n{result_text}"
                    if result_text
                    else f"进度 {progress_text}"
                )
            table_rows.append(
                {
                    "id": rid,
                    "seq": idx + 1,
                    "status": status,
                    "task_name": str(rec.get("task_name") or "-"),
                    "accounts": str(rec.get("accounts") or "—"),
                    "start_time": str(rec.get("start_time") or "—"),
                    "duration": str(rec.get("duration") or "—"),
                    "result": result_text,
                }
            )
        self.history_table.rows = table_rows
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
            for i, rec in enumerate(page_records):
                rid = str(rec.get("id") or "")
                status = self._record_status(rec)
                seq = page * page_size + i + 1  # 全量序号（跨页连续）
                with ui.card().classes("w-full mobile-list-card border"):
                    with ui.row().classes(
                        "w-full items-center gap-2 no-wrap justify-between"
                    ):
                        ui.label(
                            f"任务 #{seq} · {rec.get('task_name') or '-'}"
                        ).classes("text-base font-semibold truncate min-w-0")
                        ui.badge(status).props(
                            f"color={self.STATUS_COLOR.get(status, 'grey-6')}"
                        ).classes("shrink-0")
                    duration = str(rec.get("duration") or "")
                    start_text = str(rec.get("start_time") or "—")
                    time_text = (
                        f"{start_text}（耗时 {duration}）"
                        if duration and duration != "—"
                        else start_text
                    )
                    mobile_card_fields(
                        [
                            ("开始时间", time_text, True),
                            ("账号", rec.get("accounts"), True),
                        ]
                    )
                    # 运行中任务：卡片上直接显示进度条 + 状态徽章
                    run = self.runner.runs.get(rid)
                    if run is not None:
                        combo_subs = self._run_subs(run)
                        summary = self._progress_summary(run, combo_subs)
                        counts = summary["counts"]
                        progress_summary(
                            summary["done"],
                            summary["total"],
                            counts["失败"],
                            unit="步",
                        )
                        with ui.row().classes(
                            "w-full items-center gap-1 flex-wrap mt-1"
                        ):
                            for key in ("成功", "失败", "中断"):
                                cnt = counts.get(key, 0)
                                if cnt:
                                    color = self.UNIT_STATUS_COLOR.get(key, "grey-6")
                                    ui.badge(f"{key} {cnt}").props(
                                        f"color={color} outline"
                                    ).classes("text-xs")
                    elif rec.get("result"):
                        mobile_card_result("结果", str(rec["result"]))
                    with ui.row().classes("w-full mobile-card-actions"):
                        if run is not None:
                            ui.button(
                                "停止",
                                on_click=functools.partial(self._stop, rid),
                            ).props("flat dense color=negative")
                        ui.button(
                            "复制",
                            on_click=lambda r=rec: self._duplicate_from_rec(r),
                        ).props("flat dense color=primary")
                        ui.button(
                            "查看进度",
                            on_click=lambda r=rec: self.open_progress_page(r),
                        ).props("flat dense color=primary")
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
        """桌面表格「进度」按钮：打开进度独立页面。"""
        row = self._event_row(e)
        if row:
            self.open_progress_page(row)

    async def _on_table_stop(self, e) -> None:
        """桌面表格「停止」按钮。"""
        row = self._event_row(e)
        if row and row.get("id"):
            await self._stop(str(row["id"]))

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
        interrupted = sum(1 for d in rows if d.get("status") == "中断")
        if expected and ok + bad >= expected:
            return "失败" if bad else "成功"
        if expected and interrupted and ok + bad + interrupted >= expected:
            return "中断"
        # 任务已结束/已停止：未执行到的账号不会再继续，整体视为中断
        final = str((run.get("record") or {}).get("status") or "") != "运行中"
        if final and interrupted:
            return "中断"
        if final and (ok or bad):
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
    def _progress_summary(run: dict, combo_subs: list[dict]) -> dict:
        """整体进度：一步 = 一个账号执行一个子任务，统计完成数与各状态数量。"""
        labels = [f"{s.get('type')} · {s.get('task')}" for s in combo_subs]
        label_set = set(labels)
        rows = [
            d
            for d in (run.get("details") or [])
            if str(d.get("chat") or "") in label_set
        ]
        procs = run.get("procs") or []
        expected = 0
        for p in procs:
            sub_labels = [lb for lb in (p.get("sub_labels") or []) if lb in label_set]
            # 旧版启动/快照没有 sub_labels 时，按该账号执行全部子任务计
            expected += len(sub_labels) if sub_labels else len(labels)
        if not procs:
            accounts = {str(d.get("account") or "") for d in rows if d.get("account")}
            expected = len(labels) * max(1, len(accounts))
        counts = dict.fromkeys(("成功", "失败", "中断", "进行中"), 0)
        for d in rows:
            status = str(d.get("status") or "")
            if status in counts:
                counts[status] += 1
        done = counts["成功"] + counts["失败"] + counts["中断"]
        total = max(expected, done)
        counts["等待"] = max(0, total - sum(counts.values()))
        return {"total": total, "done": done, "counts": counts}

    @staticmethod
    def _account_progress(run: dict, combo_subs: list[dict]) -> list[dict]:
        """按账号汇总执行进度：子任务顺序、当前步骤、完成数与进度备注。

        组合任务按账号串行执行子任务，明细行的 chat 即子任务标签。
        """
        labels = [f"{s.get('type')} · {s.get('task')}" for s in combo_subs]
        label_set = set(labels)
        status_by_acc: dict[str, dict[str, str]] = {}
        notes: dict[tuple[str, str], str] = {}
        for d in run.get("details") or []:
            acc = str(d.get("account") or "")
            label = str(d.get("chat") or "")
            if not acc or label not in label_set:
                continue
            status_by_acc.setdefault(acc, {})[label] = str(d.get("status") or "")
            if d.get("note"):
                notes[(acc, label)] = str(d["note"])
        accounts: list[str] = []
        acc_labels: dict[str, list[str]] = {}
        for p in run.get("procs") or []:
            acc = str(p.get("account") or "")
            if acc and acc not in accounts:
                accounts.append(acc)
            sub_labels = [lb for lb in (p.get("sub_labels") or []) if lb in label_set]
            if sub_labels:
                acc_labels[acc] = sub_labels
        # 兼容旧版启动/快照：procs 无 sub_labels 时以明细里出现过的账号与子任务兜底
        for acc in status_by_acc:
            if acc not in accounts:
                accounts.append(acc)
            acc_labels.setdefault(
                acc, [lb for lb in labels if lb in status_by_acc[acc]]
            )
        entries = []
        for acc in accounts:
            planned = acc_labels.get(acc) or []
            if not planned:
                continue
            per = status_by_acc.get(acc) or {}
            statuses = [per.get(lb, "等待") for lb in planned]
            finished = [
                i for i, st in enumerate(statuses) if st in ("成功", "失败", "中断")
            ]
            done = len(finished)
            total = len(planned)
            current = next((i for i, st in enumerate(statuses) if st == "进行中"), None)
            if current is not None:
                status = "进行中"
                text = f"第 {current + 1}/{total} 步 · {planned[current]}"
                note = notes.get((acc, planned[current]), "")
            else:
                interrupted = sum(1 for st in statuses if st == "中断")
                failed = sum(1 for st in statuses if st == "失败")
                if interrupted:
                    status = "中断"
                    text = f"已停止（{interrupted} 个子任务被中断）"
                elif total and done >= total:
                    status = "失败" if failed else "成功"
                    text = "已完成" + (f"（{failed} 个子任务失败）" if failed else "")
                elif finished:
                    status = "等待"
                    text = f"第 {finished[-1] + 1}/{total} 步已结束，准备下一个"
                else:
                    status = "等待"
                    text = "等待开始"
                note = notes.get((acc, planned[finished[-1]]), "") if finished else ""
            entries.append(
                {
                    "account": acc,
                    "status": status,
                    "text": text,
                    "done": done,
                    "total": total,
                    "note": note,
                }
            )
        return entries

    def _render_run_card(
        self,
        run: dict,
        *,
        logs: list[str] | None = None,
        live: bool = True,
    ) -> None:
        """渲染任务进度卡片；live=False 时用进度快照展示已停止/已结束的任务。"""
        rec = run["record"]
        if live:
            elapsed = format_duration(max(0, time.time() - run["start_ts"]))
        else:
            elapsed = str(rec.get("duration") or "—")
        combo_subs = self._run_subs(run)
        with ui.card().classes("w-full border tgs-prog-outer"):
            with ui.row().classes(
                "w-full items-center gap-2 no-wrap tgs-prog-head-row"
            ):
                if live:
                    ui.badge("运行中").props("color=info")
                else:
                    status = str(rec.get("status") or "未知")
                    ui.badge(status).props(
                        f"color={self.STATUS_COLOR.get(status, 'grey-6')} outline"
                    )
                ui.label(rec.get("task_name") or "-").classes("text-base font-semibold")
                ui.label(f"账号: {rec.get('accounts', '')}").classes(
                    "text-xs text-gray-600"
                )
            time_text = f"执行时间：{rec.get('start_time') or '—'}　"
            time_text += f"已运行：{elapsed}" if live else f"时长：{elapsed}"
            ui.label(time_text).classes("text-xs text-gray-600")
            if not live and rec.get("result"):
                ui.label(f"结果：{rec['result']}").classes("text-xs text-gray-600")
            # 整体进度：一步 = 一个账号执行一个子任务，配进度条与状态徽章
            summary = self._progress_summary(run, combo_subs)
            counts = summary["counts"]
            progress_summary(
                summary["done"],
                summary["total"],
                counts["失败"],
                unit="步",
            )
            # 退频道/抽奖的累计成果（多账号求和，来自子进程日志解析计数）
            sub_types = " ".join(str(s.get("type") or "") for s in combo_subs)
            left_n = sum(
                int((p.get("stats") or {}).get("left_total") or 0)
                for p in run.get("procs") or []
            )
            joined_n = sum(
                int((p.get("stats") or {}).get("joined_total") or 0)
                for p in run.get("procs") or []
            )
            with ui.row().classes("w-full items-center gap-1 flex-wrap mt-1"):
                for key in ("进行中", "成功", "失败", "中断", "等待"):
                    if counts.get(key):
                        ui.badge(f"{key} {counts[key]}").props(
                            f"color={self.UNIT_STATUS_COLOR.get(key, 'grey-6')} outline"
                        )
                if left_n and "退频道" in sub_types:
                    ui.badge(f"已退频道 {left_n} 个").props("color=orange outline")
                if joined_n and "抽奖" in sub_types:
                    ui.badge(f"已参与抽奖 {joined_n} 个").props("color=orange outline")
            # 账号执行进度：每个账号一行，展示当前步骤、完成数与进度条
            ui.label("账号执行进度").classes("text-sm font-semibold text-gray-800 mt-2")
            entries = self._account_progress(run, combo_subs)
            if not entries:
                ui.label("暂无账号执行记录").classes("text-xs text-gray-400")
            else:
                with ui.column().classes("w-full gap-1"):
                    for entry in entries:
                        self._render_account_progress_row(entry)
            # 子任务卡片：按类型展示绑定任务的参数与各账号执行状态
            ui.label(f"子任务（{len(combo_subs)} 个）").classes(
                "text-sm font-semibold text-gray-800 mt-2"
            )
            with ui.element("div").classes("w-full tgs-combo-sub-grid"):
                for i, sub in enumerate(combo_subs, 1):
                    self._render_subtask_card(run, sub, i)
            # 运行日志区：只显示本组合任务的输出行；不再独立滚动，
            # 统一由进度页面的滚动承载（避免出现两条滚动条）
            if logs is None:
                prefix = f"[{rec.get('task_name') or ''}:"
                lines = [
                    line for line in self.runner.output_lines if line.startswith(prefix)
                ]
            else:
                lines = list(logs)
            if not live:
                lines = lines[-COMBO_PROGRESS_LOG_LINES:]

            def _copy_log() -> None:
                if not lines:
                    ui.notify("暂无可复制的日志", type="warning")
                    return
                copy_text_to_clipboard("\n".join(lines))
                ui.notify(f"已复制 {len(lines)} 行日志", type="positive")

            def _toggle_log_fullscreen() -> None:
                """切换组合任务日志全屏态：日志区 fixed 铺满视口，右上角显示退出按钮。"""
                self._combo_log_full = not getattr(self, "_combo_log_full", False)
                # 内容由页面轮询重建，这里立即按最新状态重绘
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
            log_classes = f"{LOG_AREA_CLASS} gap-0 p-2 font-mono text-sm"
            if getattr(self, "_combo_log_full", False):
                log_classes += " tgs-page-log-fullscreen"
            # pre-wrap：保留日志缩进但允许长行折行，避免移动端横向溢出
            with (
                ui.column()
                .classes(log_classes)
                .style(
                    f"white-space: pre-wrap; overflow-wrap: anywhere; "
                    f"{LOG_AREA_STYLE}; overflow-y: auto;"
                )
            ):
                if not lines:
                    ui.label("暂无日志输出").classes("text-xs text-gray-400")
                else:
                    # 整段日志用一个 <pre> 渲染：200 行时元素数从 200 降为 1，
                    # 且进度页每秒重绘时序列化体积与 DOM 压力都显著降低
                    body = "\n".join(escape(line) for line in lines[-200:])
                    # 字体/折行继承外层日志列样式，这里只去掉 pre 默认外边距
                    ui.html(f"<pre>{body}</pre>").classes("w-full").style("margin: 0")
            # 退出全屏悬浮按钮：仅全屏时 fixed 显示在视口右上角，平时不占布局
            if getattr(self, "_combo_log_full", False):
                ui.button(icon="close", on_click=_toggle_log_fullscreen).props(
                    "flat round dense unelevated"
                ).classes("tgs-log-exit-full tgs-log-exit-visible")

    def _acc_sub_status(self, run: dict, acc: str, label: str) -> str:
        """某账号在某个子任务上的状态；无明细但进程存在时视为等待。"""
        rows = [
            d
            for d in run["details"]
            if d.get("chat") == label and str(d.get("account") or "") == acc
        ]
        for st in ("进行中", "中断", "失败", "成功"):
            if any(d.get("status") == st for d in rows):
                return st
        return "等待"

    @staticmethod
    def _sub_expected_accounts(run: dict, label: str) -> list[str]:
        """某子任务的参与账号：优先进程的 sub_labels，旧数据回退明细里的账号。"""
        accounts = [
            str(p.get("account") or "")
            for p in run.get("procs") or []
            if label in (p.get("sub_labels") or [])
        ]
        if not accounts:
            accounts = [
                str(d.get("account") or "")
                for d in run.get("details") or []
                if str(d.get("chat") or "") == label
            ]
        return list(dict.fromkeys(a for a in accounts if a))

    def _render_account_progress_row(self, entry: dict) -> None:
        """账号进度行：账号、当前步骤、完成数与进度条。"""
        status = str(entry.get("status") or "等待")
        color = self.UNIT_STATUS_COLOR.get(status, "grey-6")
        total = int(entry.get("total") or 0)
        done = int(entry.get("done") or 0)
        ratio = done / total if total else 0.0
        with (
            ui.card()
            .classes("w-full border gap-1 tgs-combo-acc-card")
            .style("padding: 6px 10px")
        ):
            with ui.row().classes("w-full items-center gap-2 tgs-prog-acc-row"):
                ui.icon("person").classes("text-sm text-gray-500")
                ui.label(str(entry.get("account") or "")).classes("text-sm font-medium")
                ui.label(f"{done}/{total}").classes("text-xs text-gray-500")
                ui.space()
                ui.badge(status).props(f"color={color} outline")
            ui.label(str(entry.get("text") or "")).classes("text-xs text-gray-600")
            if entry.get("note"):
                ui.label(str(entry["note"])).classes(
                    "text-xs font-medium text-gray-500"
                )
            ui.linear_progress(value=ratio, show_value=False).props(
                f"rounded color={color}"
            ).classes("w-full").style("height: 5px")

    def _render_subtask_card(self, run: dict, sub: dict, idx: int) -> None:
        """子任务卡片：沿用其类型任务的展示字段，并附加各账号执行状态。"""
        label = f"{sub.get('type')} · {sub.get('task')}"
        status = self._sub_run_status(sub, run)
        color = self.UNIT_STATUS_COLOR.get(status, "grey-6")
        with ui.card().classes("w-full border gap-1 tgs-combo-sub-card"):
            with ui.row().classes("w-full items-center gap-2 tgs-prog-sub-row"):
                ui.badge(str(idx)).props(f"color={color}")
                ui.label(str(sub.get("type") or "子任务")).classes(
                    "text-sm font-semibold tgs-prog-sub-title"
                )
                ui.space()
                ui.badge(status).props(f"color={color} outline")
            ui.label(_sub_task_display(sub, self.workdir)).classes(
                "text-xs font-medium text-gray-700"
            )
            try:
                params = _sub_config_params_brief(sub, self.workdir) or []
            except Exception:
                params = []
            for lbl, short, full in params:
                with ui.row().classes("w-full items-start gap-2 tgs-prog-param-row"):
                    ui.label(lbl).classes(
                        "text-xs text-gray-500 shrink-0 tgs-prog-param-label"
                    )
                    value = ui.label(short).classes(
                        "text-xs text-gray-800 flex-grow tgs-prog-param-value"
                    )
                    if full != short:
                        value.tooltip(f"{lbl}（全部）：\n{full}")
            accounts = self._sub_expected_accounts(run, label)
            if not accounts:
                ui.label("未绑定账号").classes("text-xs text-gray-400")
                return
            with ui.column().classes("w-full gap-0 mt-1"):
                for acc in accounts:
                    acc_status = self._acc_sub_status(run, acc, label)
                    acc_color = self.UNIT_STATUS_COLOR.get(acc_status, "grey-6")
                    note = next(
                        (
                            str(d.get("note") or "")
                            for d in run.get("details") or []
                            if d.get("chat") == label
                            and str(d.get("account") or "") == acc
                            and d.get("note")
                        ),
                        "",
                    )
                    with ui.row().classes("w-full items-center gap-1 tgs-prog-acc-row"):
                        ui.icon("person").classes("text-xs text-gray-400")
                        ui.label(acc).classes("text-xs text-gray-800")
                        if note:
                            ui.label(note).classes("text-xs font-medium text-gray-500")
                        ui.space()
                        ui.badge(acc_status).props(f"color={acc_color} outline")

    def _tick(self) -> None:
        """每 2 秒轮询：落盘进度快照；历史状态或运行中任务进度变化时刷新列表。"""
        # 页面不可见或已切到其他 tab 时跳过，切回后自动恢复
        if not should_poll(self._poll_client, "combo"):
            return
        # 进度快照落盘：运行中每 2 秒更新，停止/结束后保留最终状态
        self._sync_progress_snapshots()
        # 历史列表：状态变化或运行中任务进度（done/total/计数）变化时重渲染
        hist_sig = self._hist_signature()
        if hist_sig != self._last_hist_sig:
            self._last_hist_sig = hist_sig
            self._render_history()

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

    async def _stop(self, run_id: str) -> None:
        await self._stop_shared(run_id)
        self._render_history()

    async def _stop_shared(self, run_id: str) -> tuple[str, dict | None]:
        """停止任务并落盘终态快照；返回 (结果, 记录)，供历史列表与进度页面共用。"""
        run = self.runner.runs.get(run_id)
        if run is not None:
            # 停止前先落盘一次，确保中断前的明细与日志被保留
            self._persist_snapshot(self._build_snapshot(run), force=True)
        outcome, record = await stop_with_feedback(
            lambda: self.runner._stop_run(run_id)
        )
        if outcome == "stopped":
            self._finalize_snapshot(run_id)
        self._live_ids.discard(run_id)
        return outcome, record

    # ---- 进度快照：任务停止/结束后仍可继续查看 ----

    @staticmethod
    def _record_brief(rec: dict) -> dict:
        """任务历史记录里用于展示/快照的字段（不含明细，避免重复存储）。"""
        return {
            "id": str(rec.get("id") or ""),
            "task_type": str(rec.get("task_type") or ""),
            "task_name": str(rec.get("task_name") or ""),
            "accounts": str(rec.get("accounts") or ""),
            "status": str(rec.get("status") or ""),
            "start_time": str(rec.get("start_time") or ""),
            "duration": str(rec.get("duration") or ""),
            "result": str(rec.get("result") or ""),
        }

    def _combo_log_lines(self, task_name: str) -> list[str]:
        """本组合任务的运行日志（共享输出缓冲按任务名前缀过滤）。"""
        if not task_name:
            return []
        prefix = f"[{task_name}:"
        lines = [line for line in self.runner.output_lines if line.startswith(prefix)]
        return lines[-COMBO_PROGRESS_LOG_LINES:]

    def _build_snapshot(self, run: dict) -> dict:
        rec = run.get("record") or {}
        return {
            "id": str(rec.get("id") or ""),
            "record": self._record_brief(rec),
            "details": [dict(d) for d in run.get("details") or []],
            "procs": [
                {
                    "account": str(p.get("account") or ""),
                    "sub_labels": [str(x) for x in (p.get("sub_labels") or [])],
                    # 保留退频道/抽奖累计计数，任务结束后进度页仍可展示
                    "stats": {
                        k: (p.get("stats") or {}).get(k)
                        for k in ("left_total", "joined_total")
                        if (p.get("stats") or {}).get(k)
                    },
                }
                for p in run.get("procs") or []
            ],
            "combo_subs": [dict(s) for s in run.get("combo_subs") or []],
            "start_ts": float(run.get("start_ts") or time.time()),
            "logs": self._combo_log_lines(str(rec.get("task_name") or "")),
        }

    @staticmethod
    def _snapshot_sig(snap: dict) -> tuple:
        """快照内容指纹，未变化时跳过落盘（日志每多一行会触发更新）。"""
        logs = snap.get("logs") or []
        return (
            str((snap.get("record") or {}).get("status") or ""),
            tuple(
                (
                    str(d.get("chat") or ""),
                    str(d.get("account") or ""),
                    str(d.get("status") or ""),
                    str(d.get("note") or ""),
                )
                for d in snap.get("details") or []
            ),
            len(logs),
            str(logs[-1])[:80] if logs else "",
        )

    def _persist_snapshot(self, snap: dict, *, force: bool = False) -> None:
        rid = str(snap.get("id") or "")
        if not rid:
            return
        sig = self._snapshot_sig(snap)
        if not force and self._snap_sigs.get(rid) == sig:
            return
        store = load_combo_progress(self.workdir)
        store[rid] = {**snap, "updated_at": time.time()}
        if len(store) > COMBO_PROGRESS_LIMIT:
            ordered = sorted(
                store.items(),
                key=lambda kv: float((kv[1] or {}).get("updated_at") or 0),
            )
            for key, _ in ordered[: len(store) - COMBO_PROGRESS_LIMIT]:
                store.pop(key, None)
        try:
            save_combo_progress(store, self.workdir)
        except OSError:
            logger.debug("写入组合任务进度快照失败", exc_info=True)
            return
        self._snap_sigs[rid] = sig

    def _sync_progress_snapshots(self) -> None:
        """每轮轮询：运行中的组合任务落盘快照，刚结束的运行补写终态快照。"""
        # 组合任务通常在「组合任务」tab 启动，签到页的日志轮询不会运行；
        # 这里主动收集子进程输出，进度页与快照才有日志可读
        self.runner.drain_output()
        live: dict[str, dict] = {}
        for rid, run in list(self.runner.runs.items()):
            rec = run.get("record") or {}
            if "组合" not in str(rec.get("task_type") or ""):
                continue
            live[rid] = run
            self._persist_snapshot(self._build_snapshot(run))
        for rid in list(self._live_ids):
            if rid not in live:
                self._finalize_snapshot(rid)
        self._live_ids = set(live)
        self._live_names = {
            rid: str((run.get("record") or {}).get("task_name") or "")
            for rid, run in live.items()
        }

    def _finalize_snapshot(self, rid: str) -> None:
        """运行结束/停止：用任务历史里的终态明细补写快照，日志沿用运行期间采集的。"""
        name = self._live_names.pop(rid, "")
        rec = next(
            (r for r in self._combo_history_records() if str(r.get("id") or "") == rid),
            None,
        )
        if rec is None:
            return
        store = load_combo_progress(self.workdir)
        snap = store.get(rid) or {}
        if not name:
            name = str(rec.get("task_name") or "")
        # 无历史快照且记录里也没有明细（如服务重启前的孤儿任务）：不造空快照
        if not snap and not rec.get("details"):
            return
        self._persist_snapshot(
            {
                "id": rid,
                "record": self._record_brief(rec),
                "details": [
                    dict(d) for d in (rec.get("details") or snap.get("details") or [])
                ],
                "procs": snap.get("procs") or [],
                "combo_subs": snap.get("combo_subs") or [],
                "start_ts": float(snap.get("start_ts") or time.time()),
                "logs": snap.get("logs") or self._combo_log_lines(name),
            },
            force=True,
        )
        # 组合任务包含退频道子任务时，运行结束后自动刷新涉及账号的频道统计
        combo_subs = snap.get("combo_subs") or []
        has_leave = any("退频道" in str(s.get("type") or "") for s in combo_subs)
        if has_leave:
            procs = snap.get("procs") or []
            if procs:
                accounts = list(
                    dict.fromkeys(
                        str(p.get("account") or "") for p in procs if p.get("account")
                    )
                )
            else:
                accounts = list(
                    dict.fromkeys(
                        str(d.get("account") or "")
                        for d in (rec.get("details") or [])
                        if d.get("account")
                    )
                )
            self._fire_combo_stats_refresh(accounts)

    def _fire_combo_stats_refresh(self, accounts: list[str]) -> None:
        """组合任务结束后异步刷新频道统计（fire-and-forget）。"""
        if not accounts:
            return
        try:
            import asyncio

            from tg_signer.webui.channel_stats import refresh_stats_for_accounts

            async def _runner() -> None:
                try:
                    await refresh_stats_for_accounts(accounts, self.workdir, ".")
                except Exception:
                    logger.debug("组合任务自动刷新频道统计失败", exc_info=True)

            asyncio.get_running_loop().create_task(_runner())
        except (ImportError, RuntimeError):
            logger.debug("跳过组合任务频道统计自动刷新")

    def _snapshot_run_view(self, rec: dict) -> dict | None:
        """把历史记录 + 进度快照拼成与运行中一致的展示结构；无快照时返回 None。"""
        rid = str(rec.get("id") or "")
        snap = load_combo_progress(self.workdir).get(rid)
        if not snap:
            return None
        details = rec.get("details") or snap.get("details") or []
        procs = snap.get("procs") or []
        if not procs:
            # 旧快照没有进程信息：按明细里的账号兜底
            procs = [
                {"account": acc, "sub_labels": []}
                for acc in dict.fromkeys(
                    str(d.get("account") or "") for d in details if d.get("account")
                )
            ]
        return {
            "record": {**(snap.get("record") or {}), **rec},
            "details": [dict(d) for d in details],
            "procs": procs,
            "combo_subs": snap.get("combo_subs") or [],
            "start_ts": float(snap.get("start_ts") or time.time()),
            "logs": snap.get("logs") or [],
        }

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
                {},
                label="组合任务模板",
                with_input=True,
                on_change=lambda: self._render_new_task_subs(),
            ).classes("w-full")
            ui.label("子任务（勾选本次要执行的，默认全部执行）").classes(
                "text-sm text-gray-600"
            )
            self.new_task_subs_container = ui.column().classes(
                "w-full gap-1 max-h-56 overflow-y-auto"
            )
            self._new_task_checks = {}
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
            ui.label("开始时间（可选）").classes("text-sm text-gray-600")
            self.new_task_start_time = (
                ui.time(value=None).classes("w-full").props("clearable")
            )
            ui.label(
                "留空立即执行；所选时间已过则从明天该时间开始。"
                "开始时间到达后先检测 TG 网络，不通则按下方间隔重试，"
                "次数用尽仍不通则放弃本次任务。"
            ).classes("text-xs text-gray-500")
            with ui.row().classes("w-full gap-2 no-wrap"):
                self.new_task_retry_interval = ui.number(
                    label="网络不通重试间隔（分钟）",
                    value=30,
                    min=1,
                    max=1440,
                    step=1,
                    precision=0,
                ).classes("flex-1")
                self.new_task_retry_times = ui.number(
                    label="最多重试次数",
                    value=3,
                    min=0,
                    max=20,
                    step=1,
                    precision=0,
                ).classes("flex-1")
            with ui.row().classes("gap-2 justify-end w-full"):
                ui.button("取消", on_click=self.new_task_dialog.close).props("outline")
                ui.button("启动", color="primary", on_click=self._start_new_task)

    def _render_new_task_subs(self) -> None:
        """按当前所选模板重建子任务勾选列表（默认全部勾选）。"""
        self.new_task_subs_container.clear()
        self._new_task_checks = {}
        name = str(self.new_task_template.value or "")
        combo = next((c for c in self._load() if str(c.get("name")) == name), None)
        subs = (combo or {}).get("subs") or []
        with self.new_task_subs_container:
            if not subs:
                ui.label("请选择组合任务模板").classes("text-xs text-gray-400")
                return
            for i, sub in enumerate(subs):
                label = f"{i + 1}. {sub.get('type') or '-'}"
                display = _sub_task_display(sub, self.workdir)
                if display:
                    label += f" · {display}"
                self._new_task_checks[i] = ui.checkbox(label, value=True).classes(
                    "text-sm"
                )

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
        self._render_new_task_subs()
        valid_accounts = list(self.runner._get_account_names())
        options = {a: a for a in valid_accounts}
        self.new_task_accounts.options = with_select_all(options)
        self.new_task_accounts.update()
        self.new_task_accounts.set_value(
            [a for a in (accounts or []) if a in valid_accounts]
        )
        self.new_task_start_time.set_value(None)
        self.new_task_retry_interval.set_value(30)
        self.new_task_retry_times.set_value(3)
        self.new_task_dialog.open()
        if template:
            ui.notify("已复制任务参数，可调整后点击「启动」", type="positive")

    def _start_new_task(self) -> None:
        name = self.new_task_template.value
        if not name:
            ui.notify("请选择组合任务模板", type="warning")
            return
        sub_indexes = None
        if self._new_task_checks:
            sub_indexes = [i for i, cb in self._new_task_checks.items() if cb.value]
            if not sub_indexes:
                ui.notify("请至少勾选一个子任务", type="warning")
                return
        # 开始时间（可选）：HH:MM；已过则顺延到明天，避免误以为会立即执行
        raw_time = str(self.new_task_start_time.value or "").strip()
        start_at = None
        if raw_time:
            try:
                hour, minute = (int(part) for part in raw_time.split(":")[:2])
                target = datetime.now().replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
            except ValueError:
                ui.notify("开始时间格式无效，请重新选择", type="warning")
                return
            if target <= datetime.now():
                target += timedelta(days=1)
            start_at = f"{target:%Y-%m-%d %H:%M:%S}"
        try:
            retry_minutes = int(self.new_task_retry_interval.value)
            retry_times = int(self.new_task_retry_times.value)
        except (TypeError, ValueError):
            ui.notify("重试间隔和次数需为整数", type="warning")
            return
        if retry_minutes < 1 or retry_times < 0:
            ui.notify("重试间隔需不小于 1 分钟，重试次数不能为负", type="warning")
            return
        accounts = [a for a in (self.new_task_accounts.value or []) if a]
        if self.runner._launch(
            "组合任务",
            str(name),
            accounts,
            sub_indexes=sub_indexes,
            start_at=start_at,
            retry_interval_seconds=retry_minutes * 60,
            max_retries=retry_times,
        ):
            self.new_task_dialog.close()
            self._render_history()

    # ---- 进度独立页面：展示运行中/已结束任务的内容 ----

    def open_progress_page(self, rec: dict) -> None:
        """点击历史任务的「查看进度」：跳转到进度独立页面（运行中实时刷新）。"""
        rid = str(rec.get("id") or "")
        if not rid:
            ui.notify("该任务缺少记录 ID，无法打开进度页面", type="warning")
            return
        ui.navigate.to(f"{COMBO_PROGRESS_PAGE_PATH}?record_id={rid}")

    def _render_progress_into(self, container, rec: dict) -> None:
        """把任务进度渲染进指定容器（独立页面复用，兼容运行中与快照两种来源）。"""
        rid = str(rec.get("id") or "")
        container.clear()
        run = self.runner.runs.get(rid)
        with container:
            if run is not None:
                # 运行中（或刚结束未清理）：展示实时的子任务进度与运行日志
                self._render_run_card(run)
                return
            # 已停止/已结束：优先用落盘的进度快照继续展示明细与日志
            fresh = next(
                (
                    r
                    for r in self._combo_history_records()
                    if str(r.get("id") or "") == rid
                ),
                None,
            )
            merged = {**rec, **self._record_brief(fresh)} if fresh else rec
            if fresh and fresh.get("details"):
                merged["details"] = fresh["details"]
            snapshot = self._snapshot_run_view(merged)
            if snapshot is not None and (
                snapshot.get("details")
                or snapshot.get("logs")
                or snapshot.get("combo_subs")
            ):
                self._render_run_card(
                    snapshot, logs=snapshot.get("logs") or [], live=False
                )
                ui.label("以上为该任务结束时的进度快照。").classes(
                    "text-xs text-gray-400"
                )
                return
            status = str(merged.get("status") or "未知")
            with ui.card().classes("w-full border gap-1"):
                with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                    ui.badge(status).props(
                        f"color={self.STATUS_COLOR.get(status, 'grey-6')} outline"
                    )
                    ui.label(str(merged.get("task_name") or "-")).classes(
                        "text-base font-semibold"
                    )
                ui.label(
                    f"开始：{merged.get('start_time') or '—'}　"
                    f"时长：{merged.get('duration') or '—'}　"
                    f"账号：{merged.get('accounts') or '—'}"
                ).classes("text-xs text-gray-600")
                if merged.get("result"):
                    ui.label(f"结果：{merged['result']}").classes(
                        "text-xs text-gray-600"
                    )
                if status == "运行中":
                    ui.label(
                        "未找到本服务的运行记录（可能启动于上次服务运行期间），"
                        "仅展示历史信息。"
                    ).classes("text-xs text-gray-400")
                else:
                    ui.label("任务已结束，无实时进度数据。").classes(
                        "text-xs text-gray-400"
                    )


# 进度独立页面：与主控台同源的浅色渐变背景 + 白色卡片，移动端单列布局
_PROGRESS_PAGE_STYLE = """
<style>
    body { background: linear-gradient(180deg, #eef1f5 0%, #e3e7ee 100%) fixed !important; }
    .q-layout, .q-page, .q-page-container, .nicegui-content { background: transparent !important; }
    .q-card { background: #ffffff; border-radius: 12px; box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08); }
    .q-notifications__list--bottom { top: 72px !important; bottom: auto !important; }
    /* 子任务卡片：桌面端自适应多列，参数值单行省略，悬停/长按看完整内容 */
    .tgs-combo-sub-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
        gap: 8px;
    }
    .tgs-combo-sub-card { gap: 4px !important; }
    .tgs-combo-sub-card .tgs-prog-param-value {
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .tgs-combo-acc-card { box-shadow: none !important; }
    /* 页面头部（返回 / 标题 / 停止）吸顶，长页面滚动时仍可操作 */
    .tgs-page-head {
        position: sticky;
        top: 0;
        z-index: 50;
        padding: 6px 4px;
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.94);
        backdrop-filter: blur(6px);
    }
    /* 日志本地全屏态（与主控台保持一致的铺满视口样式） */
    .tgs-page-log-fullscreen {
        position: fixed !important;
        inset: 12px !important;
        width: calc(100vw - 24px) !important;
        height: calc(100vh - 24px) !important;
        max-height: none !important;
        z-index: 9000;
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 12px;
        box-shadow: 0 4px 24px rgba(0, 0, 0, 0.18);
        /* 顶部留出退出按钮的位置，避免首行日志被遮挡 */
        padding: 44px 8px 8px;
        overflow-y: auto;
    }
    .tgs-log-exit-full { display: none !important; }
    .tgs-log-exit-full.tgs-log-exit-visible {
        display: inline-flex !important;
        position: fixed;
        top: 16px;
        right: 16px;
        z-index: 9001;
        background: #ffffff !important;
        border-radius: 50%;
        box-shadow: 0 1px 6px rgba(0, 0, 0, 0.2);
    }
    @media (max-width: 640px) {
        .nicegui-content { padding: 12px !important; }
        .tgs-page-log-fullscreen {
            width: calc(100vw - 24px) !important;
            height: calc(100vh - 24px) !important;
        }
        /* 去掉最外层任务卡片边框：内容直接落在页面背景上，减少一层套框 */
        .tgs-prog-outer {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            border-radius: 0 !important;
            padding: 0 !important;
        }
        /* 内层卡片收紧内边距与圆角，窄屏节省空间 */
        .q-card {
            padding: 10px 12px !important;
            border-radius: 10px !important;
        }
        /* 子任务卡片窄屏单列；参数改回换行展示，避免省略后看不到内容 */
        .tgs-combo-sub-grid { grid-template-columns: 1fr; }
        .tgs-combo-sub-card .tgs-prog-param-value {
            white-space: normal;
            overflow: visible;
            overflow-wrap: anywhere;
        }
        /* 进度内容行允许换行，长参数名/任务名不挤压值区（no-wrap 需 !important 覆盖） */
        .tgs-prog-head-row,
        .tgs-prog-sub-row { flex-wrap: wrap !important; row-gap: 2px; }
        .tgs-prog-sub-row .tgs-prog-sub-title {
            flex: 1 1 auto;
            min-width: 0;
            overflow-wrap: anywhere;
        }
        /* 子任务行里的弹性占位在窄屏去掉，把宽度让给标题，减少无谓折行 */
        .tgs-prog-sub-row .q-space { display: none !important; }
        /* 参数行：参数名限宽，值占剩余宽度并可换行（!important 覆盖 Tailwind 定宽类） */
        .tgs-prog-param-row { flex-wrap: wrap !important; row-gap: 2px; }
        .tgs-prog-param-row .tgs-prog-param-label {
            flex: 0 1 auto;
            min-width: 0 !important;
            max-width: 40% !important;
        }
        .tgs-prog-param-row .tgs-prog-param-value {
            flex: 1 1 40%;
            min-width: 0;
        }
        /* 日志区收紧内边距；标题行与按钮间距压缩 */
        .tgs-prog-outer > .font-mono { padding: 8px !important; }
        .nicegui-content .text-lg { font-size: 1.05rem !important; }
    }
</style>
"""


def _build_progress_page(record_id: str) -> None:
    """渲染任务进度页内容；容器每次轮询变化时清空重绘。"""
    install_activity_tracking()
    page_client = ui.context.client
    view = ComboTaskBlock.headless(state.workdir, rerender=lambda: render_content())
    record = next(
        (
            r
            for r in view._combo_history_records()
            if str(r.get("id") or "") == record_id
        ),
        None,
    )
    if record is None:
        # 历史文件尚未写入或已被清理，但服务进程里仍有这条运行记录
        live_run = view.runner.runs.get(record_id)
        if live_run is not None:
            record = live_run["record"]

    with ui.row().classes("w-full items-center gap-2 flex-wrap tgs-page-head"):
        ui.button(icon="arrow_back").props("flat round dense").tooltip(
            "返回主控台"
        ).on_click(lambda: ui.navigate.to("/"))
        title = ui.label(
            f"任务进度：{(record or {}).get('task_name') or record_id or '-'}"
        ).classes("text-lg font-semibold flex-grow")
        stop_btn = ui.button("停止").props("flat dense color=negative")
        stop_btn.set_visibility(False)

    content = ui.column().classes("w-full gap-2")

    if record is None:
        with content:
            ui.label("未找到该组合任务记录（可能已被清理）").classes(
                "text-sm text-gray-500"
            )
        return

    async def stop_task() -> None:
        outcome, stopped = await view._stop_shared(record_id)
        if outcome == "stopped" and stopped is not None:
            ui.notify(f"已停止: {stopped['task_name']}", type="positive")
        elif outcome == "orphan":
            ui.notify(
                "该任务启动于上次服务运行期间，已无法远程停止；"
                "如进程仍在后台运行，请在系统任务管理器中结束对应 Python 进程",
                type="warning",
            )
        render_content()

    stop_btn.on_click(stop_task)
    # 「停止」只在页面头部提供；headless 实例没有历史列表可刷新，这里无需额外处理

    last_sig: dict[str, tuple | None] = {"value": None}

    def live_sig() -> tuple:
        run = view.runner.runs.get(record_id)
        if run is not None:
            return (
                "live",
                tuple(
                    (
                        str(d.get("chat") or ""),
                        str(d.get("account") or ""),
                        str(d.get("status") or ""),
                        str(d.get("note") or ""),
                    )
                    for d in run["details"]
                ),
                tuple(view._sub_run_status(s, run) for s in view._run_subs(run)),
                sum(
                    int((p.get("stats") or {}).get("left_total") or 0)
                    for p in run.get("procs") or []
                ),
                sum(
                    int((p.get("stats") or {}).get("joined_total") or 0)
                    for p in run.get("procs") or []
                ),
                len(
                    [
                        line
                        for line in view.runner.output_lines
                        if line.startswith(f"[{run['record'].get('task_name') or ''}:")
                    ]
                ),
                int(time.time() - run["start_ts"]),
            )
        fresh = next(
            (
                r
                for r in view._combo_history_records()
                if str(r.get("id") or "") == record_id
            ),
            record,
        )
        snap = load_combo_progress(view.workdir).get(record_id) or {}
        return (
            "done",
            str((fresh or {}).get("status") or ""),
            len((fresh or {}).get("details") or []),
            bool(snap),
            len(snap.get("logs") or []),
        )

    def render_content() -> None:
        # 进度页常单独打开（主控台已关闭/手机切页），先收集子进程输出再渲染
        view.runner.drain_output()
        title.text = f"任务进度：{record.get('task_name') or record_id}"
        stop_btn.set_visibility(view.runner.runs.get(record_id) is not None)
        view._render_progress_into(content, record)

    def tick() -> None:
        try:
            # 页面独立打开时也持续落盘快照，不依赖主控台是否开着
            view._sync_progress_snapshots()
            # 页面隐藏（手机锁屏/切后台）时跳过重绘；快照落盘不受影响，切回后自动补上
            if not should_poll(page_client):
                return
            sig = live_sig()
            if sig != last_sig["value"]:
                last_sig["value"] = sig
                render_content()
        except Exception:
            logger.debug("刷新任务进度页面失败", exc_info=True)

    render_content()
    ui.timer(2.0, tick)


@ui.page(COMBO_PROGRESS_PAGE_PATH)
def combo_progress_page(record_id: str = "") -> None:
    """组合任务进度独立页面（替代原「查看进度」弹窗），与主控台共用访问授权。"""
    # 延迟导入避免与 app.py 循环依赖（app 模块加载时会导入本模块）
    from tg_signer.webui.app import AUTH_STORAGE_KEY, _auth_gate, _resolve_auth_code

    ui.dark_mode(False)
    ui.page_title("任务进度 - TG Signer")
    ui.add_head_html(_PROGRESS_PAGE_STYLE)
    root = ui.column().classes("w-full gap-3")

    def render_page() -> None:
        root.clear()
        with root:
            _build_progress_page(record_id)

    auth_code = _resolve_auth_code(None)
    if auth_code and app.storage.user.get(AUTH_STORAGE_KEY) != auth_code:
        # 未通过主控台授权：复用同一个授权验证门，验证通过后原地渲染
        _auth_gate(root, auth_code, render_page)
        return
    render_page()


def combo_block(runner, workdir: Path | str | None = None) -> ComboTaskBlock:
    """构建「组合任务」tab 面板；runner 为 TaskRunnerBlock 实例。"""
    return ComboTaskBlock(runner, workdir)
