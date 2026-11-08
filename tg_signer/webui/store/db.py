"""webui_state.db 连接、WAL 配置与 schema 版本迁移。

迁移只在 ``ui.run`` 之前执行（事件循环尚未启动），是唯一允许同步磁盘 IO 的
窗口。并发/重复启动由跨进程文件锁串行化；迁移前用 SQLite backup API 生成
``webui_state.db.bak-v{n}``，任何一步失败都保留原库并进入降级状态。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from tg_signer.locks import file_lock
from tg_signer.webui.store.migrations import LATEST_VERSION, MIGRATIONS

logger = logging.getLogger(__name__)

DB_FILENAME = "webui_state.db"
MIGRATION_LOCK_TIMEOUT = 30.0


class StoreUnavailableError(RuntimeError):
    """存储层不可用（库损坏 / schema 版本过高 / 迁移失败）。"""


@dataclass
class StoreStatus:
    """存储层健康状态；UI 顶部横幅与日志据此提示降级原因。"""

    available: bool = True
    degraded: bool = False
    reason: str = ""
    detail: str = ""
    failed_sources: list[str] = field(default_factory=list)


def backup_path_for(path: Path, version: int) -> Path:
    return path.with_name(f"{path.name}.bak-v{version}")


def _read_user_version(path: Path) -> Optional[int]:
    """只读探测现有库的 user_version；不可读时返回 None。"""
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except (sqlite3.DatabaseError, OSError):
        logger.warning("读取数据库 schema 版本失败: %s", path, exc_info=True)
        return None


def _backup_database(src: Path, dest: Path) -> None:
    """用 SQLite backup API 生成快照（兼容 WAL，不会漏掉未 checkpoint 的数据）。

    已存在的备份不会被覆盖，保留最早的一份原始快照。
    """
    if dest.exists():
        return
    source_conn = sqlite3.connect(
        f"file:{src.as_posix()}?mode=ro", uri=True, timeout=5.0
    )
    target_conn = sqlite3.connect(str(dest))
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


def _apply_migrations(conn: sqlite3.Connection, current: int) -> int:
    """在调用方开启的事务内执行未应用的迁移，返回最终版本。"""
    for migration in MIGRATIONS:
        if migration.version <= current:
            continue
        for statement in migration.statements:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {migration.version}")
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations"
            " (version, description, applied_at) VALUES (?, ?, ?)",
            (
                migration.version,
                migration.description,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        current = migration.version
    return current


class Database:
    """webui_state.db 的生命周期与迁移。"""

    def __init__(self, workdir: Path | str) -> None:
        self.workdir = Path(workdir)
        self.path = self.workdir / DB_FILENAME
        self.conn: Optional[sqlite3.Connection] = None
        self.version = 0
        self.status = StoreStatus()

    def open(self) -> "Database":
        """打开并迁移数据库；失败不抛异常，只把状态标记为降级。"""
        self.workdir.mkdir(parents=True, exist_ok=True)
        try:
            # 锁文件为 webui_state.db.lock，只保护迁移窗口
            with file_lock(self.path, timeout=MIGRATION_LOCK_TIMEOUT):
                self._open_locked()
        except TimeoutError:
            self._degrade("migration_locked", "等待迁移锁超时")
        return self

    def _open_locked(self) -> None:
        exists = self.path.is_file()
        version = 0
        if exists:
            probed = _read_user_version(self.path)
            if probed is None:
                self._degrade("db_corrupt", "数据库文件无法读取，保留旧文件路径")
                return
            version = probed
        if version > LATEST_VERSION:
            self._degrade(
                "schema_too_new",
                f"数据库版本 {version} 高于代码支持的最高版本 {LATEST_VERSION}",
            )
            return

        if exists and version < LATEST_VERSION:
            try:
                _backup_database(self.path, backup_path_for(self.path, version))
            except (sqlite3.DatabaseError, OSError) as exc:
                self._degrade("backup_failed", f"迁移前备份失败: {exc}")
                return

        try:
            conn = self._connect()
        except (sqlite3.DatabaseError, OSError) as exc:
            self._degrade("db_unavailable", str(exc))
            return

        if version < LATEST_VERSION:
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if current > LATEST_VERSION:
                    conn.execute("ROLLBACK")
                    conn.close()
                    self._degrade(
                        "schema_too_new",
                        f"数据库版本 {current} 高于代码支持的最高版本 {LATEST_VERSION}",
                    )
                    return
                self.version = _apply_migrations(conn, current)
                conn.execute("COMMIT")
            except Exception as exc:  # noqa: BLE001 - 迁移失败必须降级而不是崩
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                conn.close()
                logger.exception("webui_state.db schema 迁移失败")
                self._degrade("migration_failed", str(exc))
                return
        else:
            self.version = version
        self.conn = conn

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        conn.isolation_level = None  # 自动提交；多语句用显式事务
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            logger.debug("WAL 不可用，回退默认日志模式", exc_info=True)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _degrade(self, reason: str, detail: str) -> None:
        self.status.available = False
        self.status.degraded = True
        self.status.reason = reason
        self.status.detail = detail
        logger.warning("WebUI 存储层降级（%s）：%s", reason, detail)

    def mark_partial_degrade(self, failed: list[str]) -> None:
        """数据库可用但部分旧 JSON 导入失败时的部分降级。"""
        if not failed:
            return
        self.status.degraded = True
        self.status.reason = "legacy_import_failed"
        self.status.detail = "、".join(failed)
        self.status.failed_sources = list(failed)

    def close(self) -> None:
        conn = self.conn
        self.conn = None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.DatabaseError:
                logger.debug("关闭 webui_state.db 失败", exc_info=True)
