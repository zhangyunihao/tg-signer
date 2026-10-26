import json
import logging
import threading
from pathlib import Path
from typing import Optional

from tg_signer.locks import file_lock
from tg_signer.utils import atomic_write_json
from tg_signer.webui.common import state
from tg_signer.webui.data import get_workdir

logger = logging.getLogger(__name__)


TASK_HISTORY_FILE = "webui_task_history.json"
TASK_HISTORY_LIMIT = 100
# 落盘防抖：组合任务结束时可能连续追加多条记录，先在内存合并再写盘
TASK_HISTORY_DEBOUNCE_SEC = 0.5

# 缓存结构：(文件路径, 文件指纹(mtime_ns, size), 记录列表)。
# 指纹用于发现其他 WebUI 实例（如 8080/8081 共用工作目录）对同一文件的写入。
_task_history_cache: Optional[tuple[Path, Optional[tuple[int, int]], list[dict]]] = None
_task_history_timer: Optional[threading.Timer] = None
_task_history_lock = threading.Lock()


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


def load_task_history() -> list[dict]:
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
    """立即把内存中的任务历史落盘（退出前调用，避免防抖窗口内丢数据）。"""
    global _task_history_timer
    with _task_history_lock:
        timer = _task_history_timer
        _task_history_timer = None
        cache = _task_history_cache
    if timer is not None:
        timer.cancel()
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
    global _task_history_cache, _task_history_timer
    path = _task_history_path()
    snapshot = list(items[-TASK_HISTORY_LIMIT:])
    with _task_history_lock:
        old_timer = _task_history_timer
        _task_history_cache = (path, _task_history_fingerprint(path), snapshot)
        timer = threading.Timer(
            TASK_HISTORY_DEBOUNCE_SEC, _persist_task_history, args=(path, snapshot)
        )
        timer.daemon = True
        _task_history_timer = timer
    if old_timer is not None:
        old_timer.cancel()
    timer.start()


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
