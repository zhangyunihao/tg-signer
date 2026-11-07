"""异常可见化：页面回调异常提示、页面构建失败错误页、面板构建失败占位。

app.py 只负责注册与调用；弹窗结构、5 秒去重与错误页 UI 集中在这里，
避免把异常分支散落在页面构建代码中。
"""

import logging
import time
from collections.abc import Callable
from typing import Optional

from nicegui import ui

logger = logging.getLogger(__name__)

# 同一条异常在该窗口内只弹一次，避免重复点击/轮询把弹窗刷屏
DEDUP_WINDOW_SECONDS = 5.0
# 弹窗里的异常文本上限，避免超长堆栈消息撑爆卡片
_MAX_DETAIL_CHARS = 2000

# 已注册异常处理器的 client.id；登录页与主控制台可能先后调用，只注册一次
_INSTALLED: set[str] = set()


def reload_page() -> None:
    """重新加载当前页面（浏览器侧整页刷新）。"""
    ui.run_javascript("window.location.reload()")


def _exception_detail(exc: Exception) -> str:
    text = str(exc).strip() or repr(exc)
    if len(text) > _MAX_DETAIL_CHARS:
        text = text[:_MAX_DETAIL_CHARS] + "…"
    return f"{type(exc).__name__}: {text}"


def _ensure_dialog(state: dict) -> tuple[ui.dialog, ui.label]:
    """首次弹窗时创建对话框（挂在 client 内容根上，容器清理不会带走它）。"""
    dialog = state.get("dialog")
    if dialog is not None:
        return dialog, state["detail"]
    with (
        ui.dialog().props("transition-show=fade transition-hide=fade") as dialog,
        ui.card().classes("w-full max-w-md"),
    ):
        ui.label("页面出现异常").classes("text-base font-semibold text-negative")
        ui.label("这次操作没有完成，可重新加载页面后再试。").classes(
            "text-xs text-gray-500"
        )
        detail = (
            ui.label("")
            .classes("text-xs text-gray-700")
            .style(
                "white-space: pre-wrap; word-break: break-word;"
                " max-height: 30vh; overflow-y: auto;"
            )
        )
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("关闭", on_click=dialog.close).props("outline")
            ui.button("重新加载页面", on_click=reload_page, color="primary")
    state["dialog"] = dialog
    state["detail"] = detail
    return dialog, detail


def install_exception_notifier() -> None:
    """在当前页面注册 ui.on_exception：异常弹窗 + 5 秒去重 + 自行记录日志。

    注册页面级处理器后异常不再输出 NiceGUI 的默认日志，因此 handler 内
    显式使用 logger.exception 保留完整堆栈；弹窗提供「重新加载页面」「关闭」。
    每个 client 只注册一次；对话框在首次异常时才创建。
    """
    client = ui.context.client
    if client.id in _INSTALLED:
        return
    # ui.on_exception 在部分 NiceGUI 版本不存在：此时保持框架默认日志，
    # 仅跳过自定义弹窗，不能让页面构建失败。
    if not hasattr(ui, "on_exception"):
        logger.warning("当前 NiceGUI 不支持 ui.on_exception，异常仅记录日志")
        return
    _INSTALLED.add(client.id)
    client.on_delete(lambda: _INSTALLED.discard(client.id))

    state: dict = {"dialog": None, "detail": None, "last": {}}

    def handler(exc: Exception) -> None:
        logger.exception("页面回调异常: %s", exc)
        key = f"{type(exc).__name__}: {exc}"
        now = time.monotonic()
        last = state["last"].get(key)
        if last is not None and now - last < DEDUP_WINDOW_SECONDS:
            return
        state["last"][key] = now
        # 清掉已过窗口的记录，避免长期运行时字典无限增长
        state["last"] = {
            k: t for k, t in state["last"].items() if now - t < DEDUP_WINDOW_SECONDS
        }
        dialog, detail = _ensure_dialog(state)
        detail.text = _exception_detail(exc)
        detail.update()
        dialog.open()

    ui.on_exception(handler)


def page_error_page(exc: Optional[Exception] = None) -> None:
    """页面构建失败时的可读错误页（替换 NiceGUI 默认 500 页面）。"""
    if exc is None:
        logger.error("页面构建失败（未提供异常信息）")
    else:
        logger.error("页面构建失败: %s", exc, exc_info=exc)
    with ui.column().classes("w-full items-start gap-2 p-4"):
        ui.label("页面加载失败").classes("text-xl font-semibold")
        ui.label(
            "页面构建时出现异常，重新加载后再试；若持续失败，请查看服务端日志。"
        ).classes("text-sm text-gray-600")
        if exc is not None:
            with ui.expansion("错误详情", icon="error_outline").classes("w-full"):
                ui.label(_exception_detail(exc)).classes("text-xs text-gray-600").style(
                    "white-space: pre-wrap; word-break: break-word;"
                )
        ui.button("重新加载", icon="refresh", on_click=reload_page, color="primary")


def show_panel_error(panel, title: str, retry: Callable[[], None]) -> None:
    """在指定 tab 面板内显示「加载失败 / 重试」，不影响其它面板。"""

    def _on_retry() -> None:
        # 清掉失败占位再重建，避免重试后占位与面板内容同时存在
        panel.clear()
        retry()

    with panel:
        with ui.column().classes("w-full items-center gap-2 py-10"):
            ui.label(f"{title}加载失败").classes("text-base font-medium text-negative")
            ui.label("该面板暂时无法显示，其他面板不受影响。").classes(
                "text-xs text-gray-500"
            )
            ui.button("重试", icon="refresh", on_click=_on_retry).props("outline")
