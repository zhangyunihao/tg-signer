"""右上角消息中心：汇总抽奖任务加入失败提醒与未完成的发言任务。

消息来源（均从工作目录持久化文件实时汇总）：
- 抽奖任务监控对象加入失败提醒：webui_join_alerts.json（加入成功后由任务自动消除）
- 抽奖过程中记录的未完成发言任务（口令发送失败）：webui_speak_alerts.json
  （口令发送成功后由任务自动消除）

已读状态按消息键持久化在工作目录 webui_message_read.json，
刷新页面、重启服务、更换浏览器后依然保留。
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable, List, Set

from nicegui import ui

from tg_signer.utils import atomic_write_json
from tg_signer.webui.common import add_dialog_close_button, state
from tg_signer.webui.giveaway import (
    channel_title_map,
    format_join_alert,
    format_speak_alert,
    load_join_alerts,
    load_speak_alerts,
)

logger = logging.getLogger(__name__)

# 已读状态文件与容量上限（防止长期运行膨胀）
READ_STATE_FILE = "webui_message_read.json"
READ_STATE_MAX = 1000


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


def collect_messages(workdir) -> List[dict]:
    """汇总全部消息：加入失败提醒在前（最新在前），未完成发言任务随后。"""
    wd = Path(workdir)
    messages: List[dict] = []
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
                "text": format_speak_alert(a),
            }
        )
    return messages


def message_center_button() -> Callable[[], None]:
    """创建右上角消息按钮（含未读数徽标）与消息中心弹窗，返回徽标刷新函数。

    需在页面上下文中调用（按钮挂入当前容器）；后台轮询每 10 秒刷新未读徽标。
    """
    client = ui.context.client
    read_keys: Set[str] = load_read_keys(state.workdir)

    def mark_one(key: str) -> None:
        if key in read_keys:
            return
        read_keys.add(key)
        save_read_keys(state.workdir, read_keys)
        refresh_badge()
        render_list()

    def mark_all_read() -> None:
        for m in collect_messages(state.workdir):
            read_keys.add(m["key"])
        save_read_keys(state.workdir, read_keys)
        refresh_badge()
        render_list()

    def render_list() -> None:
        list_container.clear()
        messages = collect_messages(state.workdir)
        # 未读排在前（稳定排序，组内保持原顺序）
        messages.sort(key=lambda m: m["key"] in read_keys)
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

    with ui.button(icon="notifications").props("flat round dense") as msg_btn:
        badge = ui.badge("0").props("floating color=red").classes("text-[10px]")

    with ui.dialog() as msg_dialog, ui.card().classes("w-full max-w-2xl"):
        add_dialog_close_button(msg_dialog)
        ui.label("消息中心").classes("text-lg font-semibold")
        ui.label(
            "汇总抽奖任务监控对象加入失败提醒与抽奖过程中未完成的发言任务"
            "（口令发送失败，发送成功后自动消除），点击消息标记为已读。"
        ).classes("text-xs text-gray-500")
        with ui.row().classes("w-full justify-end"):
            ui.button("全部已读", on_click=mark_all_read).props("outline dense")
        list_container = ui.column().classes("w-full gap-2 max-h-[60vh] overflow-y-auto")

    def refresh_badge() -> None:
        messages = collect_messages(state.workdir)
        count = sum(1 for m in messages if m["key"] not in read_keys)
        badge.set_text(str(count))
        badge.set_visibility(count > 0)

    def open_center() -> None:
        render_list()
        msg_dialog.open()

    msg_btn.on_click(open_center)
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
