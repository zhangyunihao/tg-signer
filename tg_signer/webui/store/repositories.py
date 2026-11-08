"""webui_state.db 表的读写函数（同步实现，由 :mod:`store.api` 放进线程池调用）。

每个函数只负责单表逻辑，不管理事务边界：调用方（迁移导入或 async 门面）
决定何时提交/回滚。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional

# 任务历史里除以下字段外的键都原样保存在 extra 列，保证迁移不丢字段
_TASK_RUN_KNOWN = {
    "id",
    "task_type",
    "task_name",
    "task",
    "accounts",
    "status",
    "duration",
    "result",
    "start_time",
    "started_at",
    "details",
}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def upsert_task_run(
    conn: sqlite3.Connection, record: Dict[str, Any], now: Optional[float] = None
) -> bool:
    run_id = str(record.get("id") or "").strip()
    if not run_id:
        return False
    details = record.get("details")
    if not isinstance(details, list):
        details = []
    extra = {key: value for key, value in record.items() if key not in _TASK_RUN_KNOWN}
    conn.execute(
        "INSERT OR REPLACE INTO task_runs"
        " (id, task_type, task_name, accounts, status, duration, result,"
        "  started_at, details, extra, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            str(record.get("task_type") or ""),
            str(record.get("task_name") or record.get("task") or ""),
            str(record.get("accounts") or ""),
            str(record.get("status") or ""),
            str(record.get("duration") or ""),
            str(record.get("result") or ""),
            str(record.get("start_time") or record.get("started_at") or ""),
            _dump(details),
            _dump(extra),
            float(now if now is not None else time.time()),
        ),
    )
    return True


def replace_task_runs(
    conn: sqlite3.Connection, records: Iterable[Dict[str, Any]]
) -> int:
    """整表替换任务历史（内存快照落盘的唯一入口）。"""
    conn.execute("DELETE FROM task_runs")
    count = 0
    for record in records:
        if upsert_task_run(conn, record):
            count += 1
    return count


def list_task_runs(conn: sqlite3.Connection, limit: int = 100) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM task_runs ORDER BY started_at DESC, id DESC LIMIT ?",
        (max(0, int(limit)),),
    ).fetchall()
    return [_task_run_payload(row) for row in rows]


def get_task_run(conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    return _task_run_payload(row) if row is not None else None


def _task_run_payload(row: sqlite3.Row) -> Dict[str, Any]:
    payload = {
        "id": row["id"],
        "task_type": row["task_type"],
        "task_name": row["task_name"],
        "accounts": row["accounts"],
        "status": row["status"],
        "duration": row["duration"],
        "result": row["result"],
        "start_time": row["started_at"],
        "details": _load(row["details"], []),
        "updated_at": row["updated_at"],
    }
    extra = _load(row["extra"], {})
    if isinstance(extra, dict):
        payload.update(extra)
    return payload


def upsert_run_progress(
    conn: sqlite3.Connection,
    run_id: str,
    snapshot: Dict[str, Any],
    updated_at: Optional[float] = None,
) -> bool:
    if not run_id:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO run_progress (run_id, snapshot, updated_at)"
        " VALUES (?, ?, ?)",
        (
            run_id,
            _dump(snapshot),
            float(updated_at if updated_at is not None else time.time()),
        ),
    )
    return True


def replace_run_progress(
    conn: sqlite3.Connection, snapshots: Dict[str, Dict[str, Any]]
) -> int:
    """整表替换组合进度快照（条数上限由调用方控制，通常 ≤30）。"""
    conn.execute("DELETE FROM run_progress")
    count = 0
    for run_id, snapshot in snapshots.items():
        if not run_id or not isinstance(snapshot, dict):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO run_progress (run_id, snapshot, updated_at)"
            " VALUES (?, ?, ?)",
            (
                str(run_id),
                _dump(snapshot),
                float(snapshot.get("updated_at") or time.time()),
            ),
        )
        count += 1
    return count


def list_run_progress(
    conn: sqlite3.Connection, limit: int = 30
) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute(
        "SELECT run_id, snapshot FROM run_progress"
        " ORDER BY updated_at DESC, run_id DESC LIMIT ?",
        (max(0, int(limit)),),
    ).fetchall()
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        snapshot = _load(row["snapshot"], {})
        if isinstance(snapshot, dict):
            result[str(row["run_id"])] = snapshot
    return result


def get_run_progress(conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT snapshot FROM run_progress WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    snapshot = _load(row["snapshot"], {})
    return snapshot if isinstance(snapshot, dict) else None


def replace_alerts(
    conn: sqlite3.Connection, domain: str, alerts: Iterable[Dict[str, Any]]
) -> int:
    conn.execute("DELETE FROM alerts WHERE domain = ?", (domain,))
    count = 0
    for seq, alert in enumerate(alerts):
        if not isinstance(alert, dict):
            continue
        conn.execute(
            "INSERT INTO alerts (domain, seq, payload) VALUES (?, ?, ?)",
            (domain, seq, _dump(alert)),
        )
        count += 1
    return count


def list_alerts(conn: sqlite3.Connection, domain: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM alerts WHERE domain = ? ORDER BY seq ASC", (domain,)
    ).fetchall()
    alerts = []
    for row in rows:
        payload = _load(row["payload"], None)
        if isinstance(payload, dict):
            alerts.append(payload)
    return alerts


def get_setting(conn: sqlite3.Connection, key: str) -> Optional[Any]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return _load(row["value"], None)


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, _dump(value)),
    )


def set_settings(conn: sqlite3.Connection, values: Dict[str, Any]) -> int:
    count = 0
    for key, value in values.items():
        set_setting(conn, str(key), value)
        count += 1
    return count


def load_settings(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {str(row["key"]): _load(row["value"], None) for row in rows}


def load_account_state(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    """还原成与 webui_accounts.json 相同的三个列表结构。"""
    rows = conn.execute(
        "SELECT name FROM account_state WHERE disabled = 1 ORDER BY name"
    ).fetchall()
    disabled = [str(row["name"]) for row in rows]
    rows = conn.execute(
        "SELECT name FROM account_state WHERE two_way = 1 ORDER BY name"
    ).fetchall()
    two_way = [str(row["name"]) for row in rows]
    rows = conn.execute(
        "SELECT name FROM account_state WHERE position IS NOT NULL"
        " ORDER BY position ASC, name"
    ).fetchall()
    order = [str(row["name"]) for row in rows]
    return {"disabled": disabled, "two_way": two_way, "order": order}


def save_account_state(
    conn: sqlite3.Connection,
    disabled: Iterable[str],
    two_way: Iterable[str],
    order: Iterable[str],
) -> int:
    order_list = [str(name) for name in order]
    positions = {name: index for index, name in enumerate(order_list)}
    names = set(order_list)
    names.update(str(name) for name in disabled)
    names.update(str(name) for name in two_way)
    disabled_set = {str(name) for name in disabled}
    two_way_set = {str(name) for name in two_way}
    # 整体替换语义：旧状态里被移出所有集合/排序的账号必须删除，
    # 否则重新启用后下次启动又会从库里读回 disabled=1
    for row in conn.execute("SELECT name FROM account_state").fetchall():
        existing = str(row["name"])
        if existing not in names:
            conn.execute("DELETE FROM account_state WHERE name = ?", (existing,))
    for name in names:
        conn.execute(
            "INSERT OR REPLACE INTO account_state (name, disabled, two_way, position)"
            " VALUES (?, ?, ?, ?)",
            (
                name,
                int(name in disabled_set),
                int(name in two_way_set),
                positions.get(name),
            ),
        )
    return len(names)


def set_account_flag(
    conn: sqlite3.Connection, name: str, field: str, value: bool
) -> None:
    if field not in ("disabled", "two_way"):
        raise ValueError(f"不支持的账号状态字段: {field}")
    column = "disabled" if field == "disabled" else "two_way"
    conn.execute(
        "INSERT INTO account_state (name, disabled, two_way)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(name) DO UPDATE SET"
        f" {column} = excluded.{column}",
        (
            str(name),
            int(value) if column == "disabled" else 0,
            int(value) if column == "two_way" else 0,
        ),
    )


def save_account_order(conn: sqlite3.Connection, order: Iterable[str]) -> None:
    names = [str(name) for name in order]
    conn.execute("UPDATE account_state SET position = NULL WHERE position IS NOT NULL")
    for index, name in enumerate(names):
        conn.execute(
            "INSERT INTO account_state (name, position) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET position = excluded.position",
            (name, index),
        )


def load_task_bindings(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    rows = conn.execute(
        "SELECT task_type, task_name, accounts FROM task_bindings"
    ).fetchall()
    result: Dict[str, List[str]] = {}
    for row in rows:
        accounts = _load(row["accounts"], [])
        if isinstance(accounts, list):
            result[f"{row['task_type']}|{row['task_name']}"] = [
                str(name) for name in accounts
            ]
    return result


def save_task_binding(
    conn: sqlite3.Connection, task_type: str, task_name: str, accounts: Iterable[str]
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO task_bindings (task_type, task_name, accounts)"
        " VALUES (?, ?, ?)",
        (str(task_type), str(task_name), _dump([str(name) for name in accounts])),
    )


def load_message_state(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    read_rows = conn.execute(
        "SELECT key FROM message_state WHERE is_read = 1 ORDER BY key"
    ).fetchall()
    pin_rows = conn.execute(
        "SELECT key FROM message_state WHERE pinned = 1 ORDER BY key"
    ).fetchall()
    return {
        "read": [str(row["key"]) for row in read_rows],
        "pinned": [str(row["key"]) for row in pin_rows],
    }


def save_message_state(
    conn: sqlite3.Connection,
    read: Iterable[str],
    pinned: Iterable[str],
) -> int:
    read_set = {str(key) for key in read}
    pin_set = {str(key) for key in pinned}
    names = read_set | pin_set
    for key in names:
        conn.execute(
            "INSERT OR REPLACE INTO message_state (key, is_read, pinned)"
            " VALUES (?, ?, ?)",
            (key, int(key in read_set), int(key in pin_set)),
        )
    return len(names)


def set_message_flag(
    conn: sqlite3.Connection, key: str, field: str, value: bool
) -> None:
    if field not in ("is_read", "pinned"):
        raise ValueError(f"不支持的消息状态字段: {field}")
    # 冲突分支只更新目标字段，另一列保持原值
    conn.execute(
        "INSERT INTO message_state (key, is_read, pinned) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET"
        f" {field} = excluded.{field}",
        (
            str(key),
            int(value) if field == "is_read" else 0,
            int(value) if field == "pinned" else 0,
        ),
    )


def list_account_identities(
    conn: sqlite3.Connection,
) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute(
        "SELECT name, user_id, display_name, username FROM account_identity"
    ).fetchall()
    return {
        str(row["name"]): {
            "user_id": row["user_id"],
            "name": row["display_name"],
            "username": row["username"],
        }
        for row in rows
    }


def upsert_account_identity(
    conn: sqlite3.Connection,
    name: str,
    user_id: Optional[int],
    display_name: str = "",
    username: str = "",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO account_identity"
        " (name, user_id, display_name, username) VALUES (?, ?, ?, ?)",
        (str(name), user_id, display_name, username),
    )
