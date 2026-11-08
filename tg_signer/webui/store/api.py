"""Store 对外门面：同步打开（启动窗口）+ 异步读写（事件循环安全）。

UI 侧只使用 async 方法；所有 SQLite 调用都经 ``asyncio.to_thread`` 落到工作
线程，事件循环上不会出现同步 DB 操作。P1 阶段尚未接入任何 UI 读写。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, TypeVar

from tg_signer.webui.store import legacy_import, repositories
from tg_signer.webui.store.db import (
    Database,
    StoreStatus,
    StoreUnavailableError,
)

T = TypeVar("T")


class Store:
    """webui_state.db 的唯一读写入口。"""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.RLock()

    @property
    def status(self) -> StoreStatus:
        return self._database.status

    @property
    def available(self) -> bool:
        return self._database.conn is not None

    @property
    def version(self) -> int:
        return self._database.version

    def close(self) -> None:
        self._database.close()

    # ---- 同步打开（只允许在 ui.run 之前调用） ----

    @classmethod
    def open(cls, workdir: Path | str, *, import_legacy: bool = True) -> "Store":
        database = Database(workdir).open()
        store = cls(database)
        if database.conn is not None and import_legacy:
            try:
                report = legacy_import.import_all(database.conn, workdir)
            except Exception:  # noqa: BLE001 - 导入层异常同样只降级不崩溃
                database.mark_partial_degrade(["legacy_import"])
            else:
                database.mark_partial_degrade(report.failed)
        return store

    # ---- 内部执行器 ----

    def _conn(self) -> sqlite3.Connection:
        conn = self._database.conn
        if conn is None:
            raise StoreUnavailableError(
                f"存储层不可用（{self.status.reason or 'unknown'}）："
                f"{self.status.detail}"
            )
        return conn

    def _run_sync(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        with self._lock:
            return func(self._conn(), *args, **kwargs)

    async def _call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(self._run_sync, func, *args, **kwargs)

    def _write_many(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = func(conn, *args, **kwargs)
                conn.execute("COMMIT")
                return result
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise

    async def _write(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(self._write_many, func, *args, **kwargs)

    # ---- 任务历史 / 运行态进度 ----

    async def list_task_runs(self, limit: int = 100) -> List[Dict[str, Any]]:
        return await self._call(repositories.list_task_runs, limit)

    async def get_task_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return await self._call(repositories.get_task_run, run_id)

    async def upsert_task_run(self, record: Dict[str, Any]) -> bool:
        return await self._call(repositories.upsert_task_run, record)

    async def list_combo_progress(self, limit: int = 30) -> Dict[str, Dict[str, Any]]:
        return await self._call(repositories.list_run_progress, limit)

    async def get_combo_progress(self, run_id: str) -> Optional[Dict[str, Any]]:
        return await self._call(repositories.get_run_progress, run_id)

    async def upsert_combo_progress(
        self,
        run_id: str,
        snapshot: Dict[str, Any],
        updated_at: Optional[float] = None,
    ) -> bool:
        return await self._call(
            repositories.upsert_run_progress, run_id, snapshot, updated_at
        )

    # ---- 提醒 ----

    async def list_alerts(self, domain: str) -> List[Dict[str, Any]]:
        return await self._call(repositories.list_alerts, domain)

    async def replace_alerts(
        self, domain: str, alerts: Iterable[Dict[str, Any]]
    ) -> int:
        return await self._write(repositories.replace_alerts, domain, list(alerts))

    # ---- 全局设置 ----

    async def get_setting(self, key: str, default: Any = None) -> Any:
        value = await self._call(repositories.get_setting, key)
        return default if value is None else value

    async def set_setting(self, key: str, value: Any) -> None:
        await self._call(repositories.set_setting, key, value)

    async def load_settings(self) -> Dict[str, Any]:
        return await self._call(repositories.load_settings)

    # ---- 账号状态 ----

    async def load_account_state(self) -> Dict[str, List[str]]:
        return await self._call(repositories.load_account_state)

    async def save_account_state(
        self,
        disabled: Iterable[str],
        two_way: Iterable[str],
        order: Iterable[str],
    ) -> int:
        return await self._write(
            repositories.save_account_state, list(disabled), list(two_way), list(order)
        )

    async def set_account_enabled(self, name: str, enabled: bool) -> None:
        await self._call(repositories.set_account_flag, name, "disabled", not enabled)

    async def set_account_two_way(self, name: str, two_way: bool) -> None:
        await self._call(repositories.set_account_flag, name, "two_way", two_way)

    async def save_account_order(self, order: Iterable[str]) -> None:
        await self._write(repositories.save_account_order, list(order))

    # ---- 任务绑定账号 ----

    async def load_task_bindings(self) -> Dict[str, List[str]]:
        return await self._call(repositories.load_task_bindings)

    async def save_task_binding(
        self, task_type: str, task_name: str, accounts: Iterable[str]
    ) -> None:
        await self._call(
            repositories.save_task_binding,
            task_type,
            task_name,
            list(accounts),
        )

    # ---- 消息已读/置顶 ----

    async def load_message_state(self) -> Dict[str, List[str]]:
        return await self._call(repositories.load_message_state)

    async def save_message_state(
        self, read: Iterable[str], pinned: Iterable[str]
    ) -> int:
        return await self._write(
            repositories.save_message_state, list(read), list(pinned)
        )

    async def set_message_read(self, key: str, is_read: bool) -> None:
        await self._call(repositories.set_message_flag, key, "is_read", is_read)

    async def set_message_pinned(self, key: str, pinned: bool) -> None:
        await self._call(repositories.set_message_flag, key, "pinned", pinned)

    # ---- 账号身份缓存 ----

    async def list_account_identities(self) -> Dict[str, Dict[str, Any]]:
        return await self._call(repositories.list_account_identities)

    async def upsert_account_identity(
        self,
        name: str,
        user_id: Optional[int],
        display_name: str = "",
        username: str = "",
    ) -> None:
        await self._call(
            repositories.upsert_account_identity,
            name,
            user_id,
            display_name,
            username,
        )

    # ---- 启动窗口 / 后台线程专用同步入口（禁止在事件循环上调用） ----

    def list_task_runs_blocking(self, limit: int = 100) -> List[Dict[str, Any]]:
        """仅限 ``ui.run`` 之前的启动窗口或专用后台写线程调用。"""
        return self._run_sync(repositories.list_task_runs, limit)

    def replace_task_runs_blocking(self, records: Iterable[Dict[str, Any]]) -> int:
        """仅限专用后台写线程调用（任务历史防抖落盘）。"""
        return self._write_many(repositories.replace_task_runs, list(records))

    def list_combo_progress_blocking(
        self, limit: int = 30
    ) -> Dict[str, Dict[str, Any]]:
        """仅限 ``ui.run`` 之前的启动窗口或专用后台写线程调用。"""
        return self._run_sync(repositories.list_run_progress, limit)

    def replace_combo_progress_blocking(
        self, snapshots: Dict[str, Dict[str, Any]]
    ) -> int:
        """仅限专用后台写线程调用（组合进度防抖落盘）。"""
        return self._write_many(repositories.replace_run_progress, dict(snapshots))

    def data_version_blocking(self) -> int:
        """跨进程变更检测：一次 PRAGMA 查询，仅供后台检测线程调用。"""
        return self._run_sync(
            lambda conn: int(conn.execute("PRAGMA data_version").fetchone()[0])
        )


def open_store(workdir: Path | str, *, import_legacy: bool = True) -> Store:
    """在 ``ui.run`` 之前打开存储层；失败只返回降级状态，不抛异常。"""
    return Store.open(workdir, import_legacy=import_legacy)
