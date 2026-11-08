"""WebUI 存储运行时：启动时打开 Store、工作目录切换时重绑、变更订阅分发。

约束：
- ``open_at_startup`` 只能在 ``ui.run`` 之前调用（迁移是唯一允许同步 IO 的窗口）；
- 运行期重绑 Online 用 ``rebind``（在线程里打开），避免阻塞事件循环；
- 订阅回调一律在事件循环线程上调用，UI 可以直接更新控件；
- 订阅表只保存弱引用，页面关闭（``client.on_delete`` 退订）后不会泄漏。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from tg_signer.webui.store.api import Store
from tg_signer.webui.store.db import StoreStatus

logger = logging.getLogger(__name__)

# 跨进程变更检测间隔：只读一次 PRAGMA data_version，不做任何解析
WATCH_INTERVAL_SEC = 1.5
# 回调域：所有域
ALL_DOMAINS = "*"
# 提醒列表（消息中心/未读徽标）变更域
DOMAIN_ALERTS = "alerts"
# 消息已读/置顶状态变更域
DOMAIN_MESSAGES = "messages"

_lock = threading.Lock()
_store: Optional[Store] = None
_store_workdir: Optional[Path] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_listeners: List[Tuple[str, weakref.ReferenceType]] = []
_watch_thread: Optional[threading.Thread] = None
_watch_stop = threading.Event()


# ---- 打开 / 重绑 / 关闭 ----


def open_at_startup(workdir: Path | str) -> Optional[Store]:
    """在 ``ui.run`` 之前打开并迁移 Store；失败只降级，不抛异常。"""
    global _store, _store_workdir
    path = Path(workdir)
    with _lock:
        if _store is not None:
            return _store
        try:
            _store = Store.open(path)
        except Exception:  # noqa: BLE001 - 打开失败必须继续可用旧文件路径
            logger.warning("打开 WebUI 存储失败，降级为旧文件路径", exc_info=True)
            _store = None
            return None
        _store_workdir = path
        return _store


def current() -> Optional[Store]:
    """当前工作目录可用的 Store；降级或未打开时返回 None（调用方走旧路径）。"""
    store = _store
    return store if store is not None and store.available else None


def status() -> Optional[StoreStatus]:
    """当前 Store 的健康状态；未打开时返回 None。"""
    store = _store
    return store.status if store is not None else None


async def rebind(workdir: Path | str) -> Optional[Store]:
    """切换工作目录：在线程里打开新库，旧库关闭后释放。"""
    global _store, _store_workdir
    path = Path(workdir)
    if _store is not None and _store_workdir == path:
        return _store
    try:
        new_store: Optional[Store] = await asyncio.to_thread(Store.open, path)
    except Exception:  # noqa: BLE001
        logger.warning("切换工作目录时打开存储失败: %s", path, exc_info=True)
        new_store = None
    old_store = _store
    _store = new_store
    _store_workdir = path
    if old_store is not None:
        try:
            await asyncio.to_thread(old_store.close)
        except Exception:  # noqa: BLE001
            logger.debug("关闭旧存储失败", exc_info=True)
    notify()
    return new_store


def shutdown() -> None:
    """停止跨进程变更检测并关闭当前库（由关停钩子放到线程里调用）。"""
    global _store, _watch_thread
    _watch_stop.set()
    thread = _watch_thread
    _watch_thread = None
    if thread is not None:
        thread.join(timeout=2.0)
    store = _store
    _store = None
    if store is not None:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            logger.debug("关闭存储失败", exc_info=True)


# ---- 变更订阅 ----


def _make_ref(callback: Callable[[], None]) -> weakref.ReferenceType:
    if getattr(callback, "__self__", None) is not None:
        return weakref.WeakMethod(callback)
    return weakref.ref(callback)


def subscribe(domain: str, callback: Callable[[], None]) -> Callable[[], None]:
    """订阅某域的变更；返回退订函数。

    ``callback`` 必须是可弱引用的对象，且调用方需自行保持其生命周期
    （页面内可用 ``client.on_delete(unsubscribe)``）。
    """
    ref = _make_ref(callback)
    with _lock:
        _listeners.append((domain, ref))

    def _unsubscribe() -> None:
        with _lock:
            _listeners[:] = [
                (item_domain, item_ref)
                for item_domain, item_ref in _listeners
                if not (item_domain == domain and item_ref is ref)
            ]

    return _unsubscribe


def notify(domain: str = ALL_DOMAINS) -> None:
    """广播某域变更；跨线程调用会自动切回事件循环线程。"""
    loop = _loop
    if loop is not None and loop.is_running():
        try:
            loop.call_soon_threadsafe(_dispatch, domain)
            return
        except RuntimeError:
            logger.debug("事件循环已关闭，直接同步分发变更", exc_info=True)
    _dispatch(domain)


def _dispatch(domain: str) -> None:
    with _lock:
        targets = [
            (item_domain, item_ref)
            for item_domain, item_ref in _listeners
            if domain == ALL_DOMAINS or item_domain == domain
        ]
    dead: List[Tuple[str, weakref.ReferenceType]] = []
    for item_domain, item_ref in targets:
        callback = item_ref()
        if callback is None:
            dead.append((item_domain, item_ref))
            continue
        try:
            callback()
        except Exception:  # noqa: BLE001 - 单个订阅者异常不影响其余刷新
            logger.debug("变更订阅回调失败: %s", item_domain, exc_info=True)
    if dead:
        with _lock:
            _listeners[:] = [item for item in _listeners if item not in dead]


# ---- 跨进程变更检测 ----


def bind_loop() -> None:
    """在事件循环启动后登记循环并开启跨进程变更检测。"""
    global _loop
    _loop = asyncio.get_running_loop()
    _start_watcher()


def _start_watcher() -> None:
    global _watch_thread
    if _watch_thread is not None:
        return

    def _watch() -> None:
        last_version: Optional[int] = None
        while not _watch_stop.wait(WATCH_INTERVAL_SEC):
            store = _store
            if store is None or not store.available:
                last_version = None
                continue
            try:
                version = store.data_version_blocking()
            except Exception:  # noqa: BLE001 - 库被关闭/切换属正常竞态
                last_version = None
                continue
            if last_version is None:
                last_version = version
                continue
            if version != last_version:
                last_version = version
                notify()

    thread = threading.Thread(target=_watch, name="tgs-store-watch", daemon=True)
    _watch_thread = thread
    thread.start()
