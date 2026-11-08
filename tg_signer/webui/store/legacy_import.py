"""旧版 WebUI JSON 状态文件的一次性导入。

原则：
- 原文件只读，不删除、不改名（降级时 UI 仍走旧路径）；
- 每个来源独立事务，单个文件损坏不阻塞其余来源；
- ``migrated_sources`` 只有 status=ok 才跳过，失败的下次启动会重试；
- 文件当前不存在时不标记 ok，等它出现后下次启动再导入。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List

from tg_signer.webui.store import repositories

logger = logging.getLogger(__name__)

TASK_HISTORY_FILE = "webui_task_history.json"
COMBO_PROGRESS_FILE = "webui_combo_progress.json"
WIN_SETTINGS_FILE = "webui_win_settings.json"
ACCOUNTS_FILE = "webui_accounts.json"
TASK_ACCOUNTS_FILE = "webui_task_accounts.json"
READ_STATE_FILE = "webui_message_read.json"
PIN_STATE_FILE = "webui_message_pin.json"
GIVEAWAY_TASKS_FILE = "giveaway_tasks.json"

# 提醒文件 -> domain（按文件独立导入，互不影响）
ALERT_FILES: Dict[str, str] = {
    "webui_join_alerts.json": "join",
    "webui_speak_alerts.json": "speak",
    "webui_speech_alerts.json": "speech",
    "webui_speech_insufficient_alerts.json": "speech_insufficient",
    "webui_win_alerts.json": "win",
    "webui_channels_full_alerts.json": "channels_full",
    "webui_scan_cap_alerts.json": "scan_cap",
    "webui_checklist_unactionable_alerts.json": "checklist_unactionable",
}

# 抽奖任务定义留在 JSON；只迁以下运行态字段
GIVEAWAY_RUNTIME_FIELDS = (
    "last_start_time",
    "last_duration",
    "last_outcome",
    "last_joined",
    "last_joined_by_account",
    "last_joined_by_account_chat",
    "last_sweep",
)


class LegacyImportError(ValueError):
    """来源文件无法解析或结构非法。"""


@dataclass
class ImportReport:
    imported: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise LegacyImportError(f"{path.name} 解析失败: {exc}") from exc


def _mark(
    conn: sqlite3.Connection,
    source: str,
    status: str,
    row_count: int,
    error: str = "",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO migrated_sources"
        " (source, status, row_count, error, migrated_at) VALUES (?, ?, ?, ?, ?)",
        (
            source,
            status,
            int(row_count),
            error,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def _already_ok(conn: sqlite3.Connection, source: str) -> bool:
    row = conn.execute(
        "SELECT status FROM migrated_sources WHERE source = ?", (source,)
    ).fetchone()
    return row is not None and str(row["status"]) == "ok"


# ---- 各来源的导入实现 ----


def _import_task_history(conn: sqlite3.Connection, path: Path) -> int:
    data = _read_json(path)
    records = data if isinstance(data, list) else []
    count = 0
    for record in records:
        if isinstance(record, dict) and repositories.upsert_task_run(conn, record):
            count += 1
    return count


def _import_combo_progress(conn: sqlite3.Connection, path: Path) -> int:
    data = _read_json(path)
    if not isinstance(data, dict):
        return 0
    count = 0
    for run_id, snapshot in data.items():
        if not isinstance(snapshot, dict):
            continue
        updated_at = snapshot.get("updated_at")
        try:
            ts = float(updated_at)
        except (TypeError, ValueError):
            ts = time.time()
        if repositories.upsert_run_progress(conn, str(run_id), snapshot, ts):
            count += 1
    return count


def _make_alert_importer(domain: str) -> Callable[[sqlite3.Connection, Path], int]:
    def _import(conn: sqlite3.Connection, path: Path) -> int:
        data = _read_json(path)
        alerts = data.get("alerts") if isinstance(data, dict) else None
        if not isinstance(alerts, list):
            alerts = []
        return repositories.replace_alerts(conn, domain, alerts)

    return _import


def _import_win_settings(conn: sqlite3.Connection, path: Path) -> int:
    data = _read_json(path)
    if not isinstance(data, dict):
        return 0
    return repositories.set_settings(conn, data)


def _import_accounts(conn: sqlite3.Connection, path: Path) -> int:
    data = _read_json(path)
    if not isinstance(data, dict):
        return 0

    def _names(field: str) -> List[str]:
        values = data.get(field)
        return [str(item) for item in values] if isinstance(values, list) else []

    return repositories.save_account_state(
        conn, _names("disabled"), _names("two_way"), _names("order")
    )


def _import_task_accounts(conn: sqlite3.Connection, path: Path) -> int:
    data = _read_json(path)
    if not isinstance(data, dict):
        return 0
    count = 0
    for key, accounts in data.items():
        task_type, _, task_name = str(key).partition("|")
        if not task_type or not task_name or not isinstance(accounts, list):
            continue
        repositories.save_task_binding(
            conn, task_type, task_name, [str(name) for name in accounts]
        )
        count += 1
    return count


def _import_read_state(conn: sqlite3.Connection, path: Path) -> int:
    data = _read_json(path)
    keys = data.get("read") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        return 0
    read_keys = [str(key) for key in keys]
    pinned = repositories.load_message_state(conn).get("pinned") or []
    return repositories.save_message_state(conn, read_keys, pinned)


def _import_pin_state(conn: sqlite3.Connection, path: Path) -> int:
    data = _read_json(path)
    keys = data.get("pinned") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        return 0
    read_keys = repositories.load_message_state(conn).get("read") or []
    return repositories.save_message_state(conn, read_keys, [str(key) for key in keys])


def _import_giveaway_runtime(conn: sqlite3.Connection, path: Path) -> int:
    data = _read_json(path)
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, dict):
        return 0
    count = 0
    for name, cfg in tasks.items():
        if not isinstance(cfg, dict):
            continue
        if not any(field in cfg for field in GIVEAWAY_RUNTIME_FIELDS):
            continue
        record: Dict[str, Any] = {
            "id": f"giveaway-last:{name}",
            "task_type": "抽奖 (运行态)",
            "task_name": str(name),
            "accounts": ", ".join(str(item) for item in (cfg.get("accounts") or [])),
            "status": str(cfg.get("last_outcome") or ""),
            "duration": str(cfg.get("last_duration") or ""),
            "result": str(cfg.get("last_outcome") or ""),
            "start_time": str(cfg.get("last_start_time") or ""),
            "details": [],
            "source_file": GIVEAWAY_TASKS_FILE,
        }
        for key in GIVEAWAY_RUNTIME_FIELDS:
            if key in cfg:
                record[key] = cfg[key]
        if repositories.upsert_task_run(conn, record):
            count += 1
    return count


# 固定顺序：结构表与账号状态互不依赖，顺序只影响日志可读性
_IMPORTERS: List[tuple[str, Callable[[sqlite3.Connection, Path], int]]] = [
    (TASK_HISTORY_FILE, _import_task_history),
    (COMBO_PROGRESS_FILE, _import_combo_progress),
    *[
        (filename, _make_alert_importer(domain))
        for filename, domain in ALERT_FILES.items()
    ],
    (WIN_SETTINGS_FILE, _import_win_settings),
    (ACCOUNTS_FILE, _import_accounts),
    (TASK_ACCOUNTS_FILE, _import_task_accounts),
    (READ_STATE_FILE, _import_read_state),
    (PIN_STATE_FILE, _import_pin_state),
    (GIVEAWAY_TASKS_FILE, _import_giveaway_runtime),
]


def import_all(conn: sqlite3.Connection, workdir: Path | str) -> ImportReport:
    """依次导入所有旧 JSON 来源；返回成功/失败清单。"""
    base = Path(workdir)
    report = ImportReport()
    for source, importer in _IMPORTERS:
        if _already_ok(conn, source):
            continue
        path = base / source
        if not path.is_file():
            _mark(conn, source, "absent", 0)
            continue
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = importer(conn, path)
            _mark(conn, source, "ok", rows)
            conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 - 单来源失败不能阻塞其余来源
            try:
                conn.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                logger.debug("回滚失败来源事务失败: %s", source, exc_info=True)
            logger.warning("导入旧状态文件失败（%s）：%s", source, exc)
            try:
                _mark(conn, source, "failed", 0, str(exc))
            except sqlite3.DatabaseError:
                logger.debug("记录迁移失败状态失败: %s", source, exc_info=True)
            report.failed.append(source)
        else:
            report.imported.append(source)
    return report


def iter_sources() -> Iterable[str]:
    return (source for source, _ in _IMPORTERS)
