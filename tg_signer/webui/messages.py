"""右上角消息中心：汇总中奖提醒、抽奖任务加入失败提醒与未完成的发言任务。

消息来源（均从工作目录持久化文件实时汇总）：
- 中奖提醒：webui_win_alerts.json（抽奖引擎识别到 bot 私聊中奖消息时写入）
- 抽奖任务监控对象加入失败提醒：webui_join_alerts.json（加入成功后由任务自动消除）
- 抽奖过程中记录的未完成发言任务（口令发送失败）：webui_speak_alerts.json
  （口令发送成功后由任务自动消除）
- 报名成功后仍未达标的发言数任务（还需发言 N 条）：webui_speech_alerts.json

主控台右上角铃铛只负责展示未读数徽标，点击在新标签页打开 /messages
消息中心页面（支持按用户过滤、全部已读、10 秒轮询自动刷新）。
已读状态按消息键持久化在工作目录 webui_message_read.json，
刷新页面、重启服务、更换浏览器后依然保留。
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Set

from nicegui import app, ui

from tg_signer.utils import atomic_write_json
from tg_signer.webui.common import state
from tg_signer.webui.giveaway import (
    channel_title_map,
    format_join_alert,
    format_speak_alert,
    format_speech_alert,
    format_win_alert,
    load_join_alerts,
    load_speak_alerts,
    load_speech_alerts,
    load_win_alerts,
)

logger = logging.getLogger(__name__)

# 已读状态文件与容量上限（防止长期运行膨胀）
READ_STATE_FILE = "webui_message_read.json"
READ_STATE_MAX = 1000

# 用户过滤下拉的「全部」哨兵值（clearable 清空后 value 为 None，同样视为全部）
ALL_ACCOUNTS = ""

# 消息中心独立页面路由（右上角铃铛在新标签页打开）
MESSAGES_PAGE_PATH = "/messages"


def _read_state_path(workdir) -> Path:
    return Path(workdir) / READ_STATE_FILE


def load_read_keys(workdir) -> Set[str]:
    path = _read_state_path(workdir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        keys = data.get("read") if isinstance(data, dict) else None
        return {str(k) for k in (keys or [])}
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        logger.debug("读取消息已读状态失败", exc_info=True)
        return set()


def save_read_keys(workdir, keys: Set[str]) -> None:
    try:
        atomic_write_json(
            _read_state_path(workdir), {"read": sorted(keys)[:READ_STATE_MAX]}
        )
    except OSError:
        logger.debug("写入消息已读状态失败", exc_info=True)


def _time_sort_value(m: dict) -> int:
    """把消息时间（%m-%d %H:%M:%S）转为可比较整数，便于跨分类按新旧排序。"""
    time_str = str(m.get("time") or "")
    try:
        dt = datetime.strptime(time_str, "%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return (
        ((dt.month * 100 + dt.day) * 100 + dt.hour) * 10000
        + dt.minute * 100
        + dt.second
    )


def message_sort_key(m: dict, read_keys: Set[str]) -> tuple:
    """排序规则：未读优先于已读；未读内部中奖消息最优先；同组内新消息在前。"""
    unread = m["key"] not in read_keys
    return (
        0 if unread else 1,
        (0 if m["tag"] == "中奖" else 1) if unread else 1,
        -_time_sort_value(m),
    )


def collect_messages(workdir) -> List[dict]:
    """汇总全部消息：中奖提醒、加入失败提醒与未完成发言任务（排序见 message_sort_key）。"""
    wd = Path(workdir)
    messages: List[dict] = []
    # 0) 中奖提醒（最重要，置顶展示）
    for a in load_win_alerts(wd):
        acct = str(a.get("account") or "")
        bot = str(a.get("bot") or "")
        time_str = str(a.get("time") or "")
        messages.append(
            {
                "key": f"win|{acct}|{bot}|{time_str}",
                "tag": "中奖",
                "color": "green",
                "account": acct,
                "time": time_str,
                "text": format_win_alert(a),
            }
        )
    # 1) 抽奖任务：监控对象加入失败（文件内已按最新在前排序）
    title_map = channel_title_map(wd)
    for a in load_join_alerts(wd):
        acct = str(a.get("account") or "")
        chat = str(a.get("chat") or "")
        time_str = str(a.get("time") or "")
        messages.append(
            {
                "key": f"join_failed|{acct}|{chat}|{time_str}",
                "tag": "加入失败",
                "color": "red",
                "account": acct,
                "time": time_str,
                "text": format_join_alert(a, title_map),
            }
        )
    # 2) 抽奖过程记录的未完成发言任务（口令发送失败，发送成功后自动消除）
    for a in load_speak_alerts(wd):
        acct = str(a.get("account") or "")
        group = str(a.get("group") or "")
        password = str(a.get("password") or "")
        time_str = str(a.get("time") or "")
        messages.append(
            {
                "key": f"speak_failed|{acct}|{group}|{password}|{time_str}",
                "tag": "发言任务",
                "color": "orange",
                "account": acct,
                "time": time_str,
                "text": format_speak_alert(a),
            }
        )
    # 3) 报名成功后仍未达标的发言数任务（还需发言 N 条）
    for a in load_speech_alerts(wd):
        acct = str(a.get("account") or "")
        bot = str(a.get("bot") or "")
        group = str(a.get("group") or "")
        time_str = str(a.get("time") or "")
        messages.append(
            {
                "key": (
                    f"speech_required|{acct}|{bot}|{group}|"
                    f"{a.get('required')}|{time_str}"
                ),
                "tag": "发言数未达标",
                "color": "blue",
                "account": acct,
                "time": time_str,
                "text": format_speech_alert(a),
            }
        )
    return messages


def message_center_button() -> Callable[[], None]:
    """创建右上角消息按钮（含未读数徽标），点击在新标签页打开消息中心。

    需在页面上下文中调用（按钮挂入当前容器）；后台轮询每 10 秒刷新未读徽标，
    每次刷新从磁盘重读已读状态，消息中心标签页里标记的已读能同步消除徽标。
    """
    client = ui.context.client

    def refresh_badge() -> None:
        read_keys = load_read_keys(state.workdir)
        messages = collect_messages(state.workdir)
        unread = [m for m in messages if m["key"] not in read_keys]
        count = len(unread)
        has_unread_win = any(m["tag"] == "中奖" for m in unread)
        badge.set_text(str(count))
        badge.set_visibility(count > 0)
        # 有未读中奖消息时，铃铛换成绿色奖杯以示区分
        # 注意：props() 按空格分词，不能用逗号分隔（逗号会并入图标名渲染成多余字符）
        msg_btn.props(
            f"icon={'emoji_events' if has_unread_win else 'notifications'} "
            f"color={'green' if has_unread_win else 'primary'}"
        )

    with ui.button(icon="notifications").props("flat round dense") as msg_btn:
        badge = ui.badge("0").props("floating color=red").classes("text-[10px]")

    msg_btn.on_click(lambda: ui.navigate.to(MESSAGES_PAGE_PATH, new_tab=True))
    refresh_badge()

    async def _poll() -> None:
        while True:
            await asyncio.sleep(10)
            if not client.has_socket_connection:
                return
            try:
                refresh_badge()
            except Exception:
                logger.debug("轮询刷新消息未读徽标失败", exc_info=True)

    asyncio.create_task(_poll())
    return refresh_badge


def _filter_options() -> dict:
    """构建用户过滤下拉选项：全部用户 + 消息中出现过的账号（去重排序）。"""
    accounts = sorted(
        {str(m.get("account") or "") for m in collect_messages(state.workdir)} - {""}
    )
    return {ALL_ACCOUNTS: "全部用户", **{acct: acct for acct in accounts}}


# 消息中心独立页面：与主控台同源的浅色渐变背景 + 白色卡片风格
_PAGE_HEAD_STYLE = """
<style>
    body { background: linear-gradient(180deg, #eef1f5 0%, #e3e7ee 100%) fixed !important; }
    .q-layout, .q-page, .q-page-container, .nicegui-content { background: transparent !important; }
    .q-card { background: #ffffff; border-radius: 12px; box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08); }
    .q-notifications__list--bottom { top: 72px !important; bottom: auto !important; }
</style>
"""


def _build_center_page() -> None:
    """在当前页面上下文渲染消息中心内容（过滤、已读标记、列表、轮询刷新）。"""
    read_keys: Set[str] = load_read_keys(state.workdir)
    last_fp = {"value": ""}

    def visible_messages() -> List[dict]:
        """按当前用户过滤条件取可见消息（未排序）。"""
        messages = collect_messages(state.workdir)
        sel = str(filter_sel.value or "")
        if sel:
            messages = [m for m in messages if m.get("account") == sel]
        return messages

    def mark_one(key: str) -> None:
        if key in read_keys:
            return
        read_keys.add(key)
        save_read_keys(state.workdir, read_keys)
        render_list()
        refresh_unread_badge()

    def mark_all_read() -> None:
        for m in visible_messages():
            read_keys.add(m["key"])
        save_read_keys(state.workdir, read_keys)
        render_list()
        refresh_unread_badge()

    def render_list() -> None:
        list_container.clear()
        messages = visible_messages()
        # 未读在前；未读内部中奖消息置顶；同组内按新消息优先（时间倒序）
        messages.sort(key=lambda m: message_sort_key(m, read_keys))
        with list_container:
            if not messages:
                ui.label("暂无消息").classes("text-sm text-gray-400")
                return
            for m in messages:
                unread = m["key"] not in read_keys

                def _on_row_click(*_args, _key: str = m["key"]) -> None:
                    mark_one(_key)

                with (
                    ui.row()
                    .classes(
                        "w-full items-start gap-2 p-2 rounded cursor-pointer "
                        + ("bg-blue-50" if unread else "")
                    )
                    .on("click", _on_row_click)
                ):
                    ui.icon(
                        "fiber_manual_record" if unread else "radio_button_unchecked"
                    ).classes(
                        ("text-blue-500" if unread else "text-gray-300")
                        + " text-[10px] mt-1.5 shrink-0"
                    )
                    ui.badge(m["tag"]).props(
                        f"outline dense color={m['color']}"
                    ).classes("shrink-0")
                    ui.label(m["text"]).classes(
                        "text-sm flex-1 min-w-0 "
                        + ("text-gray-900" if unread else "text-gray-500")
                    )

    def refresh_all() -> None:
        # 重建过滤选项（账号集合可能变化），当前选中项失效时回落到「全部用户」
        filter_sel.options = _filter_options()
        if str(filter_sel.value or "") not in filter_sel.options:
            filter_sel.value = ALL_ACCOUNTS
        filter_sel.update()
        render_list()
        refresh_unread_badge()

    def refresh_unread_badge() -> None:
        count = sum(
            1 for m in collect_messages(state.workdir) if m["key"] not in read_keys
        )
        unread_badge.set_text(str(count))
        unread_badge.set_visibility(count > 0)

    def _fingerprint() -> str:
        """列表内容指纹：消息集合 + 已读状态变化时才重渲染，避免轮询打断滚动位置。"""
        return json.dumps(
            [[m["key"], m["key"] in read_keys] for m in collect_messages(state.workdir)]
        )

    with ui.column().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            ui.button(icon="arrow_back").props("flat round dense").on_click(
                lambda: ui.navigate.to("/", new_tab=False)
            )
            ui.label("消息中心").classes("text-xl font-semibold")
            unread_badge = ui.badge("0").props("color=red").classes("text-[10px]")
            ui.space()
            ui.button("全部已读", on_click=mark_all_read).props("outline dense")
            ui.button(icon="refresh").props("flat round dense").on_click(refresh_all)
        ui.label(
            "汇总抽奖中奖提醒、监控对象加入失败提醒与抽奖过程中未完成的发言任务"
            "（口令发送失败/发言数未达标，显示还需发言条数），点击消息标记为已读。"
        ).classes("text-xs text-gray-500")
        filter_sel = (
            ui.select(
                _filter_options(),
                value=ALL_ACCOUNTS,
                label="按用户过滤",
                on_change=lambda _e: render_list(),
            )
            .props("dense outlined clearable options-dense")
            .classes("w-48")
        )
        list_container = ui.column().classes("w-full gap-2")

    refresh_all()

    client = ui.context.client

    async def _poll() -> None:
        while True:
            await asyncio.sleep(10)
            if not client.has_socket_connection:
                return
            try:
                fp = _fingerprint()
                if fp != last_fp["value"]:
                    last_fp["value"] = fp
                    refresh_all()
            except Exception:
                logger.debug("轮询刷新消息中心列表失败", exc_info=True)

    asyncio.create_task(_poll())


@ui.page(MESSAGES_PAGE_PATH)
def messages_page() -> None:
    """消息中心独立页面（新标签页打开），与主控台共用同一套访问授权。"""
    # 延迟导入避免与 app.py 循环依赖（app 模块加载时导入本模块）
    from tg_signer.webui.app import (
        AUTH_STORAGE_KEY,
        _auth_gate,
        _resolve_auth_code,
    )

    ui.dark_mode(False)
    ui.page_title("消息中心 - TG Signer")
    ui.add_head_html(_PAGE_HEAD_STYLE)

    root = ui.column().classes("w-full gap-3")

    def render_center() -> None:
        root.clear()
        with root:
            _build_center_page()

    auth_code = _resolve_auth_code(None)
    if auth_code and app.storage.user.get(AUTH_STORAGE_KEY) != auth_code:
        # 未通过主控台授权：复用同一个授权验证门，验证通过后原地渲染消息中心
        _auth_gate(root, auth_code, render_center)
        return
    render_center()
