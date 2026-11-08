"""全局设置与账号状态的进程内缓存。

Store 可用时：启动窗口（``ui.run`` 之前）把设置与账号状态读入内存，之后
所有 UI 读取都是纯内存操作；写入先改内存，再由防抖线程整批落库，事件循环
上不再出现同步文件/DB 操作。

Store 不可用或对应来源导入失败时退回旧 JSON 文件路径：文件按 mtime+size
指纹缓存，同一文件内容未变化时不会重复读盘解析。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from tg_signer.locks import file_lock
from tg_signer.utils import atomic_write_json
from tg_signer.webui.store import runtime as store_runtime

logger = logging.getLogger(__name__)

SETTINGS_FILE = "webui_win_settings.json"
ACCOUNTS_FILE = "webui_accounts.json"
# 写盘防抖：设置/账号状态是低频用户操作，合并短时间内的连续修改
WRITE_DEBOUNCE_SEC = 0.5

_lock = threading.RLock()
_store = None
_settings: Optional[Dict[str, Any]] = None
_accounts: Optional[Dict[str, List[str]]] = None
# 旧文件路径缓存：filename -> (path, (mtime_ns, size), parsed dict)
_file_caches: Dict[str, Tuple[Path, Optional[Tuple[int, int]], Dict[str, Any]]] = {}
_settings_timer: Optional[threading.Timer] = None
_accounts_timer: Optional[threading.Timer] = None
_settings_pending: Dict[str, Any] = {}
_accounts_dirty = False


def _base(workdir) -> Path:
    return Path(workdir) if workdir else Path(".")


def _fingerprint(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_json_cached(workdir, filename: str) -> Dict[str, Any]:
    """旧文件路径读取：同一 (path, mtime, size) 只解析一次。"""
    path = _base(workdir) / filename
    fingerprint = _fingerprint(path)
    with _lock:
        cached = _file_caches.get(filename)
        if cached is not None and cached[0] == path and cached[1] == fingerprint:
            return cached[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    with _lock:
        _file_caches[filename] = (path, fingerprint, data)
    return data


def _update_json_file(workdir, filename: str, mutate) -> None:
    """旧文件路径写入：文件锁内读改写，并同步缓存指纹。"""
    path = _base(workdir) / filename
    try:
        with file_lock(path):
            # 锁内必须拿最新内容，不能用锁外的缓存快照覆盖并发写入
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            mutate(data)
            atomic_write_json(path, data)
            with _lock:
                _file_caches[filename] = (path, _fingerprint(path), data)
    except TimeoutError:
        logger.warning("更新状态文件超时，本次修改未保存: %s", path)


# ---- 启动接入 / 切目录 / 关停 ----


def attach_store(store) -> None:
    """启动窗口或专用线程调用：把设置与账号状态读入内存。"""
    global _store, _settings, _accounts
    flush_state()
    use_store = store is not None and getattr(store, "available", False)
    settings_data: Optional[Dict[str, Any]] = None
    accounts_data: Optional[Dict[str, List[str]]] = None
    if use_store:
        status = store.status
        if SETTINGS_FILE not in status.failed_sources:
            try:
                settings_data = dict(store.load_settings_blocking())
            except Exception:  # noqa: BLE001 - 读取失败退回旧文件路径
                logger.warning("读取全局设置失败，继续使用旧文件路径", exc_info=True)
        if ACCOUNTS_FILE not in status.failed_sources:
            try:
                raw = store.load_account_state_blocking()
                accounts_data = _normalize_accounts(raw)
            except Exception:  # noqa: BLE001
                logger.warning("读取账号状态失败，继续使用旧文件路径", exc_info=True)
    with _lock:
        _store = store if use_store else None
        _settings = settings_data
        _accounts = accounts_data
        _file_caches.pop(SETTINGS_FILE, None)
        _file_caches.pop(ACCOUNTS_FILE, None)


async def hydrate_from_store(store) -> None:
    """运行期切换工作目录：在线程里读库后替换内存快照。"""
    import asyncio

    await asyncio.to_thread(attach_store, store)


def detach_store() -> None:
    """回到旧文件路径（Store 降级/关闭时）。"""
    global _store, _settings, _accounts
    flush_state()
    with _lock:
        _store = None
        _settings = None
        _accounts = None


def flush_state() -> None:
    """退出/切目录前把防抖中的设置与账号状态立即落库。"""
    global _settings_timer, _accounts_timer
    with _lock:
        settings_timer = _settings_timer
        accounts_timer = _accounts_timer
        _settings_timer = None
        _accounts_timer = None
    if settings_timer is not None:
        settings_timer.cancel()
    if accounts_timer is not None:
        accounts_timer.cancel()
    _persist_settings()
    _persist_accounts()


# ---- 全局设置 ----


def subscribe_settings(callback: Callable[[], None]) -> Callable[[], None]:
    return store_runtime.subscribe(store_runtime.DOMAIN_SETTINGS, callback)


def _settings_data(workdir) -> Dict[str, Any]:
    with _lock:
        if _settings is not None:
            return _settings
    return _read_json_cached(workdir, SETTINGS_FILE)


def get_setting(workdir, key: str, default: Any = None) -> Any:
    data = _settings_data(workdir)
    return data[key] if key in data else default


def _schedule_settings_persist(fields: Dict[str, Any]) -> None:
    global _settings_timer
    with _lock:
        _settings_pending.update(fields)
        old_timer = _settings_timer
        timer = threading.Timer(WRITE_DEBOUNCE_SEC, _persist_settings)
        timer.daemon = True
        _settings_timer = timer
    if old_timer is not None:
        old_timer.cancel()
    timer.start()


def _persist_settings() -> None:
    global _settings_timer
    with _lock:
        store = _store
        fields = dict(_settings_pending)
        _settings_pending.clear()
        _settings_timer = None
    if store is None or not store.available or not fields:
        return
    try:
        store.set_settings_blocking(fields)
    except Exception:  # noqa: BLE001 - 写失败不影响内存中的当前设置
        logger.warning("写入全局设置失败", exc_info=True)


def update_settings(workdir, **fields) -> None:
    """更新若干设置键：Store 模式先改内存再防抖落库，降级模式立即写文件。"""
    with _lock:
        if _settings is not None:
            _settings.update(fields)
            schedule = True
        else:
            schedule = False
    if schedule:
        _schedule_settings_persist(fields)
    else:
        _update_json_file(workdir, SETTINGS_FILE, lambda data: data.update(fields))
    store_runtime.notify(store_runtime.DOMAIN_SETTINGS)


# ---- 账号状态 ----


def subscribe_accounts(callback: Callable[[], None]) -> Callable[[], None]:
    return store_runtime.subscribe(store_runtime.DOMAIN_ACCOUNTS, callback)


def _normalize_accounts(raw) -> Dict[str, List[str]]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        field: [str(item) for item in (raw.get(field) or [])]
        for field in ("disabled", "two_way", "order")
    }


def _accounts_data(workdir) -> Dict[str, List[str]]:
    with _lock:
        if _accounts is not None:
            return _accounts
    return _normalize_accounts(_read_json_cached(workdir, ACCOUNTS_FILE))


def load_disabled_accounts(workdir) -> Set[str]:
    return set(_accounts_data(workdir).get("disabled") or [])


def load_two_way_accounts(workdir) -> Set[str]:
    return set(_accounts_data(workdir).get("two_way") or [])


def load_account_order(workdir) -> List[str]:
    return list(_accounts_data(workdir).get("order") or [])


def _mutate_accounts(workdir, mutate) -> None:
    with _lock:
        if _accounts is not None:
            mutate(_accounts)
            writable = True
        else:
            writable = False
    if writable:
        _schedule_accounts_persist()
    else:
        _update_json_file(workdir, ACCOUNTS_FILE, mutate)
    store_runtime.notify(store_runtime.DOMAIN_ACCOUNTS)


def _schedule_accounts_persist() -> None:
    global _accounts_timer, _accounts_dirty
    with _lock:
        _accounts_dirty = True
        old_timer = _accounts_timer
        timer = threading.Timer(WRITE_DEBOUNCE_SEC, _persist_accounts)
        timer.daemon = True
        _accounts_timer = timer
    if old_timer is not None:
        old_timer.cancel()
    timer.start()


def _persist_accounts() -> None:
    global _accounts_timer, _accounts_dirty
    with _lock:
        store = _store
        _accounts_timer = None
        if not _accounts_dirty:
            return
        snapshot = _normalize_accounts(_accounts) if _accounts is not None else None
        _accounts_dirty = False
    if store is None or snapshot is None or not store.available:
        return
    try:
        store.save_account_state_blocking(
            snapshot["disabled"], snapshot["two_way"], snapshot["order"]
        )
    except Exception:  # noqa: BLE001 - 写失败不影响内存中的当前状态
        logger.warning("写入账号状态失败", exc_info=True)


def set_account_enabled(workdir, name: str, enabled: bool) -> None:
    def _mutate(data: Dict[str, Any]) -> None:
        disabled = set(data.get("disabled") or [])
        if enabled:
            disabled.discard(name)
        else:
            disabled.add(name)
        data["disabled"] = sorted(disabled)

    _mutate_accounts(workdir, _mutate)


def set_account_two_way(workdir, name: str, two_way: bool) -> None:
    def _mutate(data: Dict[str, Any]) -> None:
        values = set(data.get("two_way") or [])
        if two_way:
            values.add(name)
        else:
            values.discard(name)
        data["two_way"] = sorted(values)

    _mutate_accounts(workdir, _mutate)


def set_account_order(workdir, order: List[str]) -> None:
    def _mutate(data: Dict[str, Any]) -> None:
        data["order"] = list(order)

    _mutate_accounts(workdir, _mutate)


def replace_accounts(workdir, values: Dict[str, Any]) -> None:
    """备份导入：用备份中的账号状态整体替换当前状态（仅覆盖存在的字段）。"""

    def _mutate(data: Dict[str, Any]) -> None:
        for field in ("disabled", "two_way", "order"):
            if isinstance(values.get(field), list):
                data[field] = [str(item) for item in values[field]]

    _mutate_accounts(workdir, _mutate)


def rename_account_state(workdir, old: str, new: str) -> None:
    def _mutate(data: Dict[str, Any]) -> None:
        for field in ("disabled", "two_way", "order"):
            values = data.get(field)
            if isinstance(values, list) and old in values:
                values[values.index(old)] = new
                data[field] = values

    _mutate_accounts(workdir, _mutate)
