"""浏览器会话活跃状态：页面可见性与当前激活的 tab。

各面板的轮询循环据此在页面不可见（手机锁屏/切到后台）或所在 tab
未激活时跳过刷新，减少空转唤醒、websocket 流量与服务端负载。
"""

from typing import Optional

from nicegui import ui

# key: client.id, value: {"hidden": bool, "tab": str}
_STATE: dict[str, dict] = {}
# 已安装可见性监听的 client.id：主控台与页内对话框可能重复调用，只安装一次
_INSTALLED: set[str] = set()


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


def should_poll(client, tab: Optional[str] = None) -> bool:
    """轮询循环是否应继续刷新：页面可见，且（可选）所在 tab 处于激活状态。

    未安装活跃度跟踪的页面（如独立进度页）没有状态记录，返回 True 保持原行为。
    """
    entry = _STATE.get(client.id)
    if entry is None:
        return True
    if entry["hidden"]:
        return False
    return tab is None or entry["tab"] == tab
