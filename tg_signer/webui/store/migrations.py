"""webui_state.db 的 schema 迁移定义。

新增版本时只在 ``MIGRATIONS`` 末尾追加，不修改已发布版本的语句：
每个语句逐条执行（而不是 ``executescript``），保证迁移整体在一个事务里，
失败可以完整回滚。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    statements: tuple[str, ...]


_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version     INTEGER PRIMARY KEY,
        description TEXT NOT NULL,
        applied_at  TEXT NOT NULL
    )
    """,
    # 任务历史：一条记录一行，details/extra 存 JSON 文本
    """
    CREATE TABLE IF NOT EXISTS task_runs (
        id         TEXT PRIMARY KEY,
        task_type  TEXT NOT NULL DEFAULT '',
        task_name  TEXT NOT NULL DEFAULT '',
        accounts   TEXT NOT NULL DEFAULT '',
        status     TEXT NOT NULL DEFAULT '',
        duration   TEXT NOT NULL DEFAULT '',
        result     TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL DEFAULT '',
        details    TEXT NOT NULL DEFAULT '[]',
        extra      TEXT NOT NULL DEFAULT '{}',
        updated_at REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_task_runs_started
        ON task_runs (started_at, id)
    """,
    # 运行中进度快照：按 run_id 单行 upsert
    """
    CREATE TABLE IF NOT EXISTS run_progress (
        run_id     TEXT PRIMARY KEY,
        snapshot   TEXT NOT NULL,
        updated_at REAL NOT NULL DEFAULT 0
    )
    """,
    # 提醒：按 domain 隔离不同来源的提醒文件
    """
    CREATE TABLE IF NOT EXISTS alerts (
        domain  TEXT NOT NULL,
        seq     INTEGER NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (domain, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # 账号状态：停用/双向/展示顺序
    """
    CREATE TABLE IF NOT EXISTS account_state (
        name     TEXT PRIMARY KEY,
        disabled INTEGER NOT NULL DEFAULT 0,
        two_way  INTEGER NOT NULL DEFAULT 0,
        position INTEGER
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_account_state_position
        ON account_state (position)
    """,
    # 任务 -> 绑定账号
    """
    CREATE TABLE IF NOT EXISTS task_bindings (
        task_type TEXT NOT NULL,
        task_name TEXT NOT NULL,
        accounts  TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (task_type, task_name)
    )
    """,
    # 消息已读/置顶
    """
    CREATE TABLE IF NOT EXISTS message_state (
        key     TEXT PRIMARY KEY,
        is_read INTEGER NOT NULL DEFAULT 0,
        pinned  INTEGER NOT NULL DEFAULT 0
    )
    """,
    # 账号身份缓存（session -> user_id -> me.json）
    """
    CREATE TABLE IF NOT EXISTS account_identity (
        name         TEXT PRIMARY KEY,
        user_id      INTEGER,
        display_name TEXT NOT NULL DEFAULT '',
        username     TEXT NOT NULL DEFAULT ''
    )
    """,
    # 旧 JSON 导入幂等标记：只有 status=ok 才跳过
    """
    CREATE TABLE IF NOT EXISTS migrated_sources (
        source      TEXT PRIMARY KEY,
        status      TEXT NOT NULL,
        row_count   INTEGER NOT NULL DEFAULT 0,
        error       TEXT NOT NULL DEFAULT '',
        migrated_at TEXT NOT NULL
    )
    """,
)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "初始 WebUI 运行态 schema", _V1_STATEMENTS),
)

LATEST_VERSION = max(migration.version for migration in MIGRATIONS)
