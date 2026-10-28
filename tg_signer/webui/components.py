"""WebUI 共用交互组件：统一日志窗口、进度条与停止按钮反馈。

签到、自动抽奖、批量退频道、组合任务此前各自实现日志区容器、全屏切换与
停止按钮，外观和行为不一致。这里集中定义一套可复用实现：

- ``LOG_AREA_CLASS`` / ``LOG_AREA_STYLE``：统一日志区外观（等宽、浅底、
  圆角、边框、限高）
- ``classify_log_line``：按日志级别返回文字颜色（原先在 app.py 与
  task_panel.py 各复制了一份）
- ``apply_log_fullscreen`` / ``LogFullscreen``：统一的页面内嵌全屏切换
- ``progress_summary``：统一的进度行（标题 + 百分比 + 进度条）
- ``stop_with_feedback``：统一的「停止中…」等待反馈
"""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

from nicegui import ui

# 日志区统一外观与限高；页面可在其后追加自己的类（如 sign-log-area 的移动端限高）
LOG_AREA_CLASS = "w-full bg-gray-50 rounded-lg border border-gray-200 tgs-log-area"
LOG_AREA_STYLE = "max-height: 320px"

# 停止任务时先展示的过渡状态，避免用户重复点击
STOPPING_STATUS = "停止中"


def classify_log_line(line: str) -> str:
    """按日志级别返回文字颜色类（ERROR 红、WARN 黄、INFO 蓝、其余灰）。"""
    upper = line.upper()
    if "ERROR" in upper:
        return "text-red-700"
    if "WARN" in upper:
        return "text-amber-700"
    if "INFO" in upper:
        return "text-blue-700"
    return "text-gray-800"


def apply_log_fullscreen(target, active: bool, exit_button=None) -> None:
    """把日志容器切换为铺满视口的全屏态，并显隐右上角退出按钮。

    ``target`` 为日志区本身（ui.log / scroll_area / column 均可），
    ``exit_button`` 为退出全屏的悬浮按钮（可为 None）。
    """
    if active:
        target.classes(add="tgs-page-log-fullscreen")
        if exit_button is not None:
            exit_button.classes(add="tgs-log-exit-visible")
    else:
        target.classes(remove="tgs-page-log-fullscreen")
        if exit_button is not None:
            exit_button.classes(remove="tgs-log-exit-visible")
    target.update()
    if exit_button is not None:
        exit_button.update()


class LogFullscreen:
    """页面内嵌日志区的全屏切换状态机（各页面复用同一套 CSS 与行为）。"""

    def __init__(self, target, exit_button=None) -> None:
        self.target = target
        self.exit_button = exit_button
        self.active = False

    def toggle(self) -> bool:
        self.apply(not self.active)
        return self.active

    def apply(self, active: bool) -> None:
        self.active = bool(active)
        apply_log_fullscreen(self.target, self.active, self.exit_button)


def exit_fullscreen_button(on_click) -> ui.button:
    """右上角退出全屏悬浮按钮：默认隐藏，全屏时由 LogFullscreen 显示。"""
    return (
        ui.button(icon="close", on_click=on_click)
        .props("flat round dense unelevated")
        .classes("tgs-log-exit-full")
    )


def progress_summary(
    done: int,
    total: int,
    failed: int = 0,
    *,
    label: str = "整体进度",
    unit: str = "步",
) -> None:
    """统一进度行：标题 + 完成数/百分比 + 线性进度条。

    失败优先标红，全部完成标绿，其余为主题色。
    """
    ratio = (done / total) if total else 0.0
    if failed:
        color = "negative"
    elif total and done >= total:
        color = "positive"
    else:
        color = "primary"
    with ui.row().classes("w-full items-center gap-2 mt-1"):
        ui.label(label).classes("text-sm font-semibold text-gray-800")
        ui.space()
        ui.label(f"{done}/{total} {unit} · {int(ratio * 100)}%").classes(
            "text-xs text-gray-600"
        )
    ui.linear_progress(value=ratio, show_value=False).props(
        f"rounded color={color}"
    ).classes("w-full").style("height: 8px")


async def stop_with_feedback(
    stop_call: Optional[Callable[[], object]] = None,
    *,
    stop_async: Optional[Callable] = None,
    on_busy: Optional[Callable[[bool], None]] = None,
) -> object:
    """统一停止反馈：先通知界面进入「停止中」态，再执行停止逻辑。

    ``stop_call`` 为阻塞函数，放入线程执行以免卡住事件循环（原先同步
    terminate + wait 最多阻塞 5 秒/进程，多账号时页面会假死）；
    ``stop_async`` 为协程工厂，直接 await。
    """
    if on_busy is not None:
        on_busy(True)
    try:
        if stop_async is not None:
            return await stop_async()
        if stop_call is None:
            return None
        return await asyncio.to_thread(stop_call)
    finally:
        if on_busy is not None:
            on_busy(False)
