"""浏览器会话活跃状态：页面可见性与当前激活的 tab。

各面板的轮询循环据此在页面不可见（手机锁屏/切到后台）或所在 tab
未激活时跳过刷新，减少空转唤醒、websocket 流量与服务端负载。
"""

import asyncio
import inspect
import logging
from collections.abc import Awaitable
from typing import Optional

from nicegui import background_tasks, ui

logger = logging.getLogger(__name__)

# background_tasks.create 的 context 参数是 nicegui 3.18 才新增的；
# 3.13 没有该参数，传了会直接 TypeError 导致页面构建失败。
_CREATE_SUPPORTS_CONTEXT = (
    "context" in inspect.signature(background_tasks.create).parameters
)

# key: client.id, value: {"hidden": bool, "tab": str}
_STATE: dict[str, dict] = {}
# 已安装可见性监听的 client.id：主控台与页内对话框可能重复调用，只安装一次
_INSTALLED: set[str] = set()
# 后台轮询任务引用（key: f"{client.id}:{name}"），done 时清理
_POLL_TASKS: dict[str, asyncio.Task] = {}


def _entry(client) -> dict:
    return _STATE.setdefault(client.id, {"hidden": False, "tab": ""})


def install_activity_tracking(client=None) -> None:
    """在当前页面安装可见性监听（每个页面会话调用一次）。

    前端通过 emitEvent 把 visibilitychange 上报到页面根元素（client.layout，
    id 为 0），这里在其上注册事件处理器更新内存状态。
    """
    client = client or ui.context.client
    if client.id in _INSTALLED:
        return
    _INSTALLED.add(client.id)
    entry = _entry(client)

    def _set_hidden(hidden: bool) -> None:
        entry["hidden"] = hidden

    client.layout.on("tgsPageHidden", lambda _e=None: _set_hidden(True))
    client.layout.on("tgsPageVisible", lambda _e=None: _set_hidden(False))

    def _cleanup(*_args) -> None:
        _STATE.pop(client.id, None)
        _INSTALLED.discard(client.id)

    client.on_delete(_cleanup)
    ui.run_javascript(
        "(function () {"
        "  const report = () => emitEvent("
        "document.hidden ? 'tgsPageHidden' : 'tgsPageVisible');"
        "  document.addEventListener('visibilitychange', report);"
        "  report();"
        "})();"
    )


def set_active_tab(name: str, client=None) -> None:
    """记录该会话当前激活的 tab（主标签栏切换时调用）。"""
    client = client or ui.context.client
    _entry(client)["tab"] = str(name or "")


def is_client_alive(client) -> bool:
    """会话是否仍然存在：只有被 NiceGUI 删除后才应停止轮询。

    短暂的 socket 断连（has_socket_connection=False）期间会话仍然存在，
    服务端保留 client 的重连窗口内页面和元素树都会保留，轮询应继续等待
    重连而非永久退出。
    """
    return not getattr(client, "is_deleted", False)


def should_poll(client, tab: Optional[str] = None) -> bool:
    """轮询是否应当刷新 UI：页面可见，且（可选）所在 tab 处于激活状态。

    只描述可见性/激活状态，不承载连接状态：短暂断连时仍返回 True，
    轮询可继续推进游标、同步后台数据，重连后直接补渲染。
    没有状态记录的页面（如独立进度页）返回 True 保持原行为；
    会话已被删除时返回 False。
    """
    if getattr(client, "is_deleted", False):
        return False
    entry = _STATE.get(client.id)
    if entry is None:
        return True
    if entry["hidden"]:
        return False
    return tab is None or entry["tab"] == tab


def poll_delay(base: float, failures: int, max_delay: float = 30.0) -> float:
    """连续失败时的指数退避秒数；failures 为 0 时返回 base。"""
    if failures <= 0:
        return base
    return min(base * (2 ** min(failures, 6)), max_delay)


def create_poll_task(awaitable: Awaitable, *, name: str, client) -> asyncio.Task:
    """创建带异常兜底的轮询后台任务。

    用 NiceGUI 的 background_tasks.create（保留强引用），同时在模块内按
    client+name 再持有一次引用；任务因非取消异常结束时用 logger.exception
    记录，避免"轮询静默死亡"而日志里查不到原因。

    nicegui 3.13 的 create 没有 context 参数：此时靠 asyncio 在创建任务时
    复制当前上下文（调用点本身处于页面/事件上下文）实现等价效果。
    """
    key = f"{getattr(client, 'id', '')}:{name}"
    create_kwargs: dict = {"name": name, "handle_exceptions": False}
    if _CREATE_SUPPORTS_CONTEXT:
        create_kwargs["context"] = client
    task = background_tasks.create(
        _run_poll(awaitable, name=name, client=client), **create_kwargs
    )
    _POLL_TASKS[key] = task

    def _on_done(finished: asyncio.Task) -> None:
        _POLL_TASKS.pop(key, None)
        if not finished.cancelled():
            finished.exception()  # 取回异常；具体日志已在 _run_poll 中输出

    task.add_done_callback(_on_done)
    return task


async def _run_poll(awaitable: Awaitable, *, name: str, client) -> None:
    try:
        await awaitable
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "后台轮询任务 %s 异常退出（client=%s）", name, getattr(client, "id", "?")
        )
