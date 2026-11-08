import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from tg_signer.locks import file_lock
from tg_signer.utils import atomic_write_json
from tg_signer.webui.common import state
from tg_signer.webui.data import get_workdir
from tg_signer.webui.store import runtime as store_runtime

logger = logging.getLogger(__name__)


TASK_HISTORY_FILE = "webui_task_history.json"
TASK_HISTORY_LIMIT = 100
# 落盘防抖：组合任务结束时可能连续追加多条记录，先在内存合并再写盘
TASK_HISTORY_DEBOUNCE_SEC = 0.5
TASK_HISTORY_DOMAIN = "task_history"

# 缓存结构：(文件路径, 文件指纹(mtime_ns, size), 记录列表)。
# 指纹用于发现其他 WebUI 实例（如 8080/8081 共用工作目录）对同一文件的写入。
_task_history_cache: Optional[tuple[Path, Optional[tuple[int, int]], list[dict]]] = None
_task_history_timer: Optional[threading.Timer] = None
_task_history_lock = threading.Lock()

# Store 模式：内存快照即唯一事实来源；文件路径只在降级时使用
_store = None
_memory: Optional[list[dict]] = None


def _task_history_path() -> Path:
    return get_workdir(state.workdir) / TASK_HISTORY_FILE


def _task_history_fingerprint(path: Path) -> Optional[tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_task_history(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_task_history(path: Path, items: list[dict]) -> None:
    global _task_history_cache
    try:
        atomic_write_json(path, items[-TASK_HISTORY_LIMIT:])
    except OSError:
        logger.debug("写入任务历史失败", exc_info=True)
        return
    # 写盘后同步缓存指纹，避免下一次读取重复解析
    with _task_history_lock:
        cache = _task_history_cache
        if cache is not None and cache[0] == path and cache[2] is items:
            _task_history_cache = (path, _task_history_fingerprint(path), items)


# ---- Store 接入 / 订阅 ----


def attach_store(store) -> None:
    """启动窗口（``ui.run`` 之前）调用：把任务历史读入内存。

    之后 ``load_task_history`` 不再碰文件；持久化由防抖写线程走 Store。
    读取失败或该来源导入失败时保持旧文件路径，避免丢数据。
    """
    global _store, _memory
    if store is None or not store.available:
        detach_store()
        return
    if TASK_HISTORY_FILE in (store.status.failed_sources or []):
        logger.warning("任务历史导入失败，继续使用旧文件路径")
        detach_store()
        return
    try:
        records = store.list_task_runs_blocking(TASK_HISTORY_LIMIT)
    except Exception:  # noqa: BLE001 - 读取失败只降级
        logger.warning("读取任务历史失败，继续使用旧文件路径", exc_info=True)
        detach_store()
        return
    with _task_history_lock:
        _store = store
        _memory = list(reversed(records))[-TASK_HISTORY_LIMIT:]


async def hydrate_from_store(store) -> None:
    """运行期切换工作目录：在线程里读库后替换内存快照。"""
    await asyncio.to_thread(attach_store, store)


def detach_store() -> None:
    """回到旧文件路径（切目录失败 / Store 降级时）。"""
    global _store, _memory
    with _task_history_lock:
        _store = None
        _memory = None


def subscribe_task_history(callback: Callable[[], None]) -> Callable[[], None]:
    """订阅任务历史变更（回调在事件循环线程执行）。"""
    return store_runtime.subscribe(TASK_HISTORY_DOMAIN, callback)


def _notify_task_history() -> None:
    store_runtime.notify(TASK_HISTORY_DOMAIN)


def _persist_store(store, snapshot: list[dict]) -> None:
    """防抖定时器回调：把内存快照整表写回 Store（在独立线程执行）。"""
    try:
        store.replace_task_runs_blocking(snapshot)
    except Exception:  # noqa: BLE001 - 写失败不能影响 UI
        logger.warning("写入任务历史失败", exc_info=True)


def load_task_history() -> list[dict]:
    with _task_history_lock:
        if _store is not None and _memory is not None:
            return list(_memory)
    global _task_history_cache
    path = _task_history_path()
    fingerprint = _task_history_fingerprint(path)
    with _task_history_lock:
        cache = _task_history_cache
        if cache is not None and cache[0] == path and cache[1] == fingerprint:
            return list(cache[2])
        items = _read_task_history(path)
        _task_history_cache = (path, fingerprint, items)
        return list(items)


def flush_task_history() -> None:
    """立即把内存中的任务历史落盘（退出前在线程里调用，避免防抖窗口丢数据）。"""
    global _task_history_timer
    with _task_history_lock:
        timer = _task_history_timer
        _task_history_timer = None
        store = _store
        snapshot = _memory
        cache = _task_history_cache
    if timer is not None:
        timer.cancel()
    if store is not None and snapshot is not None:
        _persist_store(store, snapshot)
        return
    if cache is not None:
        _write_task_history(cache[0], cache[2])


def _persist_task_history(path: Path, snapshot: list[dict]) -> None:
    """防抖定时器回调：仅当缓存仍指向同一份快照时写盘，避免旧写覆盖新写。"""
    global _task_history_timer
    with _task_history_lock:
        cache = _task_history_cache
        if cache is None or cache[2] is not snapshot:
            return
        _task_history_timer = None
    _write_task_history(path, snapshot)


def save_task_history(items: list[dict]) -> None:
    global _task_history_cache, _task_history_timer, _memory
    snapshot = list(items[-TASK_HISTORY_LIMIT:])
    path = _task_history_path()
    with _task_history_lock:
        store = _store
        if store is not None:
            _memory = snapshot
        else:
            _task_history_cache = (
                path,
                _task_history_fingerprint(path),
                snapshot,
            )
        old_timer = _task_history_timer
        if store is not None:
            timer = threading.Timer(
                TASK_HISTORY_DEBOUNCE_SEC, _persist_store, args=(store, snapshot)
            )
        else:
            timer = threading.Timer(
                TASK_HISTORY_DEBOUNCE_SEC,
                _persist_task_history,
                args=(path, snapshot),
            )
        timer.daemon = True
        _task_history_timer = timer
    if old_timer is not None:
        old_timer.cancel()
    timer.start()
    _notify_task_history()


def append_task_history(record: dict) -> None:
    items = load_task_history()
    items.append(record)
    save_task_history(items)


# 任务绑定账号：任务首次执行后记住所用账号，后续执行无需再选
TASK_ACCOUNTS_FILE = "webui_task_accounts.json"


def _task_accounts_path() -> Path:
    return get_workdir(state.workdir) / TASK_ACCOUNTS_FILE


def load_task_accounts() -> dict[str, list[str]]:
    path = _task_accounts_path()
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_task_binding(task_type: str, task_name: str, accounts: list[str]) -> None:
    if not task_type or not task_name or not accounts:
        return
    path = _task_accounts_path()
    # 一个文件存所有任务的绑定关系，读改写需互斥，否则并发保存会丢别的任务的绑定
    try:
        with file_lock(path):
            data = load_task_accounts()
            data[f"{task_type}|{task_name}"] = list(accounts)
            atomic_write_json(path, data)
    except TimeoutError:
        logger.warning("更新任务绑定账号超时，本次修改未保存: %s", path)


def update_task_history(record: dict) -> None:
    """按记录 id 更新已存在的历史条目并持久化。"""
    items = load_task_history()
    for i, item in enumerate(items):
        if item.get("id") == record.get("id"):
            items[i] = record
            break
    save_task_history(items)
