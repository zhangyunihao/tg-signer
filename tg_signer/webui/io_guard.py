"""事件循环同步 IO 观测器。

默认完全不生效；只有显式设置环境变量后才安装包装器：

- ``TG_SIGNER_WEBUI_IO_GUARD=1``   启用手表
- ``TG_SIGNER_WEBUI_IO_LOG_MS=10`` 同步 IO 超过该阈值时记录告警（默认 10ms）
- ``TG_SIGNER_WEBUI_IO_STRICT=1``  超阈值直接抛 ``BlockingIOError``（仅测试用）
- ``TG_SIGNER_WEBUI_IO_STATS_PATH`` 退出时把统计写入该 JSON 文件
- ``TG_SIGNER_WEBUI_LAG_LOG``      事件循环延迟采样输出（JSONL）

只统计事件循环线程上的调用。``asyncio.to_thread`` / ``run.io_bound`` 的工作线程
没有运行中的事件循环，不会被计入，因此这两类正确写法不会产生噪声。
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import logging
import os
import sqlite3
import threading
import time
import traceback
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_THRESHOLD_MS = 10.0
_DEDUPE_SECONDS = 60.0
_TRUTHY = {"1", "true", "yes", "on"}

_installed = False
_originals: dict[str, Any] = {}
_local = threading.local()
_stats_lock = threading.Lock()
_stats: dict[str, dict[str, float]] = {}
_last_warn: dict[str, float] = {}
_lag_task: Optional[asyncio.Task] = None
_lag_buffer: list[dict[str, float]] = []
_stats_thread: Optional[threading.Thread] = None
_stats_stop = threading.Event()


def _enabled() -> bool:
    return os.environ.get("TG_SIGNER_WEBUI_IO_GUARD", "").strip().lower() in _TRUTHY


def _strict() -> bool:
    return os.environ.get("TG_SIGNER_WEBUI_IO_STRICT", "").strip().lower() in _TRUTHY


def _threshold_ms() -> float:
    raw = os.environ.get("TG_SIGNER_WEBUI_IO_LOG_MS", "").strip()
    if not raw:
        return _DEFAULT_THRESHOLD_MS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_THRESHOLD_MS


def _on_loop_thread() -> bool:
    """当前线程是否有正在运行的事件循环。

    同步回调由事件循环线程执行时能取到 running loop；``asyncio.to_thread``
    的工作线程取不到，用来区分「同一份同步代码是否落在事件循环上」。
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _caller_site() -> str:
    stack = traceback.extract_stack()
    for frame in reversed(stack):
        filename = frame.filename
        if filename == __file__:
            continue
        if filename.endswith(("pathlib.py", "pathlib\\__init__.py")):
            continue
        if filename.endswith(("frozen", "<frozen importlib._bootstrap>")):
            continue
        return f"{filename}:{frame.lineno} in {frame.name}"
    return "unknown"


def _record(operation: str, site: str, elapsed_ms: float) -> None:
    key = f"{operation} @ {site}"
    with _stats_lock:
        item = _stats.setdefault(key, {"count": 0.0, "max_ms": 0.0, "total_ms": 0.0})
        item["count"] += 1
        item["max_ms"] = max(item["max_ms"], elapsed_ms)
        item["total_ms"] += elapsed_ms

    if elapsed_ms < _threshold_ms():
        return
    now = time.monotonic()
    if now - _last_warn.get(key, 0.0) < _DEDUPE_SECONDS:
        return
    _last_warn[key] = now
    logger.warning(
        "事件循环同步 IO 阻塞 %.1fms：%s @ %s",
        elapsed_ms,
        operation,
        site,
    )


def _wrap(operation: str, func: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if getattr(_local, "active", False):
            return func(*args, **kwargs)
        if not _enabled() or not _on_loop_thread():
            return func(*args, **kwargs)
        _local.active = True
        try:
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except BaseException:
                elapsed = (time.perf_counter() - start) * 1000
                _record(operation, _caller_site(), elapsed)
                raise
            elapsed = (time.perf_counter() - start) * 1000
            _record(operation, _caller_site(), elapsed)
            if _strict() and elapsed >= _threshold_ms():
                raise BlockingIOError(
                    f"事件循环同步 IO {elapsed:.1f}ms 超过阈值 "
                    f"{_threshold_ms():.1f}ms：{operation}"
                )
            return result
        finally:
            _local.active = False

    return wrapper


def install() -> None:
    """安装文件/数据库包装器；重复调用安全。"""

    global _installed
    if _installed:
        return
    _originals["builtins.open"] = builtins.open
    _originals["io.open"] = io.open
    _originals["sqlite3.connect"] = sqlite3.connect
    _originals["os.stat"] = os.stat
    _originals["os.scandir"] = os.scandir
    _originals["os.listdir"] = os.listdir

    builtins.open = _wrap("open", _originals["builtins.open"])
    io.open = _wrap("io.open", _originals["io.open"])
    sqlite3.connect = _wrap("sqlite3.connect", _originals["sqlite3.connect"])
    os.stat = _wrap("os.stat", _originals["os.stat"])
    os.scandir = _wrap("os.scandir", _originals["os.scandir"])
    os.listdir = _wrap("os.listdir", _originals["os.listdir"])
    _installed = True
    logger.info("事件循环同步 IO 观测器已启用（阈值 %.1fms）", _threshold_ms())
    _start_stats_writer()


def uninstall() -> None:
    """恢复原始函数，主要供测试使用。"""

    global _installed
    if not _installed:
        return
    builtins.open = _originals["builtins.open"]
    io.open = _originals["io.open"]
    sqlite3.connect = _originals["sqlite3.connect"]
    os.stat = _originals["os.stat"]
    os.scandir = _originals["os.scandir"]
    os.listdir = _originals["os.listdir"]
    _installed = False


def install_from_env() -> None:
    if _enabled():
        install()


def snapshot() -> dict[str, dict[str, float]]:
    """返回当前统计快照（调用点 → 次数 / 最大耗时 / 总耗时）。"""

    with _stats_lock:
        return {key: dict(value) for key, value in _stats.items()}


def reset_stats() -> None:
    with _stats_lock:
        _stats.clear()
        _last_warn.clear()


def dump_stats_from_env() -> None:
    """退出时写统计文件；由调用方放到工作线程执行。"""

    path = os.environ.get("TG_SIGNER_WEBUI_IO_STATS_PATH", "").strip()
    if path:
        _write_stats(path)


def _write_stats(path: str) -> None:
    payload = {
        "threshold_ms": _threshold_ms(),
        "stats": snapshot(),
    }
    temp = f"{path}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp, path)


def _stats_writer(path: str, interval: float = 5.0) -> None:
    while not _stats_stop.wait(interval):
        try:
            _write_stats(path)
        except OSError:
            logger.debug("写入同步 IO 统计失败", exc_info=True)


def _start_stats_writer() -> None:
    global _stats_thread
    path = os.environ.get("TG_SIGNER_WEBUI_IO_STATS_PATH", "").strip()
    if not path or _stats_thread is not None:
        return
    _stats_thread = threading.Thread(
        target=_stats_writer,
        args=(path,),
        name="tgs-io-stats",
        daemon=True,
    )
    _stats_thread.start()


async def _lag_loop(path: str, interval: float) -> None:
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await asyncio.sleep(interval)
        # 一次卡顿只记一个样本：若按固定节拍追赶，长阻塞会被拆成上百条
        # 递减的积压样本，把 p95/p99 严重放大
        lag_ms = max(0.0, (loop.time() - started - interval) * 1000)
        _lag_buffer.append({"t": time.time(), "lag_ms": round(lag_ms, 3)})
        if len(_lag_buffer) >= 10:
            await asyncio.to_thread(_flush_lag, path)


def _flush_lag(path: str) -> None:
    if not _lag_buffer:
        return
    chunk = _lag_buffer[:]
    _lag_buffer.clear()
    with open(path, "a", encoding="utf-8") as handle:
        for item in chunk:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def start_lag_monitor(path: str, interval: float = 0.02) -> Optional[asyncio.Task]:
    """在运行中的事件循环里启动延迟采样，返回任务引用。"""

    global _lag_task
    if _lag_task is not None and not _lag_task.done():
        return _lag_task
    _lag_task = asyncio.create_task(_lag_loop(path, interval), name="tgs-loop-lag")
    return _lag_task


def start_lag_monitor_from_env() -> None:
    path = os.environ.get("TG_SIGNER_WEBUI_LAG_LOG", "").strip()
    if not path:
        return
    start_lag_monitor(path)
