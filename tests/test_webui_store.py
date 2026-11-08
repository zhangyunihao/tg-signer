"""P1 存储层测试：schema 迁移、旧 JSON 导入、降级与幂等。"""

from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tg_signer.webui.store import StoreUnavailableError, open_store
from tg_signer.webui.store import db as db_module
from tg_signer.webui.store.db import DB_FILENAME, LATEST_VERSION, backup_path_for
from tg_signer.webui.store.migrations import Migration


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _seed_legacy(workdir: Path) -> None:
    """写入覆盖全部导入来源的最小旧数据集。"""
    _write_json(
        workdir / "webui_task_history.json",
        [
            {
                "id": "hist-1",
                "task_type": "签到",
                "task_name": "demo",
                "accounts": "acc_a, acc_b",
                "status": "成功",
                "duration": "3秒",
                "result": "完成",
                "start_time": "2026-01-01 12:00:00",
                "details": [{"account": "acc_a", "chat": "@demo", "status": "成功"}],
                "legacy_extra": {"source": "test"},
            },
            {
                "id": "hist-2",
                "task_type": "组合任务",
                "task_name": "combo",
                "accounts": "acc_a",
                "status": "失败",
                "duration": "5秒",
                "result": "部分失败",
                "start_time": "2026-01-02 12:00:00",
                "details": [],
            },
        ],
    )
    _write_json(
        workdir / "webui_combo_progress.json",
        {
            "run-1": {
                "id": "run-1",
                "record": {"id": "run-1", "status": "运行中"},
                "details": [{"account": "acc_a", "chat": "@demo", "status": "进行中"}],
                "updated_at": 100.0,
            },
            "run-2": {
                "id": "run-2",
                "record": {"id": "run-2", "status": "已结束"},
                "details": [],
                "updated_at": 90.0,
            },
        },
    )
    _write_json(
        workdir / "webui_join_alerts.json",
        {
            "alerts": [
                {"account": "acc_a", "chat": "@demo", "error": "join failed"},
                {"account": "acc_b", "chat": "@demo2", "error": "blocked"},
            ]
        },
    )
    _write_json(
        workdir / "webui_win_alerts.json",
        {"alerts": [{"account": "acc_a", "text": "win", "time": "2026-01-01"}]},
    )
    _write_json(
        workdir / "webui_win_settings.json",
        {
            "win_forward_target": ["me", "@ops"],
            "win_sweep_limit": 120,
            "giveaway_timeout_min": 15,
        },
    )
    _write_json(
        workdir / "webui_accounts.json",
        {
            "disabled": ["acc_b"],
            "two_way": ["acc_a"],
            "order": ["acc_b", "acc_a", "acc_c"],
        },
    )
    _write_json(
        workdir / "webui_task_accounts.json",
        {"签到|demo": ["acc_a", "acc_b"], "组合任务|combo": ["acc_a"]},
    )
    _write_json(workdir / "webui_message_read.json", {"read": ["win|1", "join|2"]})
    _write_json(workdir / "webui_message_pin.json", {"pinned": ["win|1"]})
    _write_json(
        workdir / "giveaway_tasks.json",
        {
            "seq": 3,
            "tasks": {
                "1": {
                    "accounts": ["acc_a"],
                    "last_start_time": "2026-01-03 12:00:00",
                    "last_duration": "10秒",
                    "last_outcome": "已完成",
                    "last_joined": 2,
                    "last_joined_by_account": {"acc_a": 2},
                },
                "2": {"accounts": ["acc_b"]},
            },
        },
    )


def test_schema_created_on_empty_workdir(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    try:
        assert store.available
        assert not store.status.degraded
        assert store.version == LATEST_VERSION
        conn = sqlite3.connect(tmp_path / DB_FILENAME)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()
        assert version == LATEST_VERSION
        assert {
            "meta",
            "schema_migrations",
            "task_runs",
            "run_progress",
            "alerts",
            "settings",
            "account_state",
            "task_bindings",
            "message_state",
            "account_identity",
            "migrated_sources",
        } <= tables
    finally:
        store.close()


@pytest.mark.asyncio
async def test_legacy_import_values(tmp_path: Path) -> None:
    _seed_legacy(tmp_path)
    originals = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    store = open_store(tmp_path)
    try:
        assert store.available
        assert not store.status.degraded

        runs = await store.list_task_runs()
        assert {run["id"] for run in runs} == {
            "hist-1",
            "hist-2",
            "giveaway-last:1",
        }
        hist = await store.get_task_run("hist-1")
        assert hist["legacy_extra"] == {"source": "test"}
        assert hist["details"][0]["account"] == "acc_a"

        progress = await store.list_combo_progress()
        assert set(progress) == {"run-1", "run-2"}
        assert progress["run-1"]["record"]["status"] == "运行中"

        join_alerts = await store.list_alerts("join")
        assert len(join_alerts) == 2
        assert join_alerts[0]["error"] == "join failed"
        assert len(await store.list_alerts("win")) == 1

        assert await store.get_setting("win_forward_target") == ["me", "@ops"]
        assert await store.get_setting("win_sweep_limit") == 120

        account_state = await store.load_account_state()
        assert account_state["disabled"] == ["acc_b"]
        assert account_state["two_way"] == ["acc_a"]
        assert account_state["order"] == ["acc_b", "acc_a", "acc_c"]

        bindings = await store.load_task_bindings()
        assert bindings["签到|demo"] == ["acc_a", "acc_b"]
        assert bindings["组合任务|combo"] == ["acc_a"]

        messages = await store.load_message_state()
        assert messages["read"] == ["join|2", "win|1"]
        assert messages["pinned"] == ["win|1"]

        giveaway = await store.get_task_run("giveaway-last:1")
        assert giveaway["status"] == "已完成"
        assert giveaway["last_joined"] == 2
        assert giveaway["last_joined_by_account"] == {"acc_a": 2}
        assert await store.get_task_run("giveaway-last:2") is None
    finally:
        store.close()

    # 原文件保持原样，降级时旧读取路径仍然可用
    assert {
        path.name: path.read_bytes() for path in tmp_path.glob("*.json")
    } == originals


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path: Path) -> None:
    _seed_legacy(tmp_path)
    first = open_store(tmp_path)
    try:
        runs = await first.list_task_runs()
        progress = await first.list_combo_progress(limit=100)
        join_alerts = await first.list_alerts("join")
    finally:
        first.close()

    second = open_store(tmp_path)
    try:
        assert not second.status.degraded
        assert len(await second.list_task_runs()) == len(runs)
        assert len(await second.list_combo_progress(limit=100)) == len(progress)
        assert len(await second.list_alerts("join")) == len(join_alerts)
        conn = sqlite3.connect(tmp_path / DB_FILENAME)
        try:
            source_rows = conn.execute(
                "SELECT COUNT(*) FROM migrated_sources"
            ).fetchone()[0]
            run_rows = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
        finally:
            conn.close()
        assert source_rows == 16
        assert run_rows == 3
    finally:
        second.close()


def test_corrupt_source_does_not_block_others(tmp_path: Path) -> None:
    _seed_legacy(tmp_path)
    (tmp_path / "webui_join_alerts.json").write_text("{not json", encoding="utf-8")
    store = open_store(tmp_path)
    try:
        assert store.available
        assert store.status.degraded
        assert store.status.reason == "legacy_import_failed"
        assert "webui_join_alerts.json" in store.status.failed_sources
        conn = sqlite3.connect(tmp_path / DB_FILENAME)
        try:
            assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 3
            status = conn.execute(
                "SELECT status FROM migrated_sources WHERE source = ?",
                ("webui_join_alerts.json",),
            ).fetchone()[0]
        finally:
            conn.close()
        assert status == "failed"
    finally:
        store.close()

    # 修好后再次打开应重试并恢复，不再降级
    _write_json(
        tmp_path / "webui_join_alerts.json",
        {"alerts": [{"account": "acc_a", "chat": "@demo", "error": "ok"}]},
    )
    repaired = open_store(tmp_path)
    try:
        assert not repaired.status.degraded
        conn = sqlite3.connect(tmp_path / DB_FILENAME)
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM alerts WHERE domain = 'join'"
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()
    finally:
        repaired.close()


@pytest.mark.asyncio
async def test_schema_too_new_degrades_without_touching_db(tmp_path: Path) -> None:
    path = tmp_path / DB_FILENAME
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA user_version = 99")
        conn.execute("CREATE TABLE keep_me (value TEXT)")
        conn.commit()
    finally:
        conn.close()

    store = open_store(tmp_path)
    try:
        assert not store.available
        assert store.status.degraded
        assert store.status.reason == "schema_too_new"
        with pytest.raises(StoreUnavailableError):
            await store.list_task_runs()
    finally:
        store.close()

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "keep_me" in tables
        assert "task_runs" not in tables
    finally:
        conn.close()


def test_corrupt_database_degrades(tmp_path: Path) -> None:
    (tmp_path / DB_FILENAME).write_bytes(b"not a sqlite database")
    store = open_store(tmp_path)
    try:
        assert not store.available
        assert store.status.degraded
        assert store.status.reason == "db_corrupt"
    finally:
        store.close()


def test_migration_failure_degrades(tmp_path: Path, monkeypatch) -> None:
    bad = (Migration(1, "坏迁移", ("CREATE TABLE broken (",)),)
    monkeypatch.setattr(db_module, "MIGRATIONS", bad)
    store = open_store(tmp_path)
    try:
        assert not store.available
        assert store.status.degraded
        assert store.status.reason == "migration_failed"
    finally:
        store.close()
    assert (tmp_path / DB_FILENAME).exists()


def test_migration_backup_before_upgrade(tmp_path: Path) -> None:
    path = tmp_path / DB_FILENAME
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE legacy_marker (value TEXT)")
        conn.execute("INSERT INTO legacy_marker VALUES ('kept')")
        conn.commit()
    finally:
        conn.close()

    store = open_store(tmp_path)
    try:
        assert store.available
        assert store.version == LATEST_VERSION
    finally:
        store.close()

    backup = backup_path_for(path, 0)
    assert backup.is_file()
    conn = sqlite3.connect(backup)
    try:
        assert conn.execute("SELECT value FROM legacy_marker").fetchone()[0] == "kept"
    finally:
        conn.close()


def test_concurrent_start_only_migrates_once(tmp_path: Path) -> None:
    _seed_legacy(tmp_path)

    def _open() -> tuple[bool, bool]:
        store = open_store(tmp_path)
        try:
            return store.available, store.status.degraded
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _open(), range(2)))
    assert all(available and not degraded for available, degraded in results)

    conn = sqlite3.connect(tmp_path / DB_FILENAME)
    try:
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 3
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE domain = 'join'"
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_real_scale_migration_duration(tmp_path: Path) -> None:
    """真实规模工作目录：账号 200、历史 500、提醒 8×200、组合快照 30。"""
    accounts = 200
    _write_json(
        tmp_path / "webui_accounts.json",
        {
            "disabled": [f"acc_{i}" for i in range(0, accounts, 10)],
            "two_way": [f"acc_{i}" for i in range(1, accounts, 10)],
            "order": [f"acc_{i}" for i in range(accounts)],
        },
    )
    _write_json(
        tmp_path / "webui_task_history.json",
        [
            {
                "id": f"run-{index}",
                "task_type": "组合任务",
                "task_name": f"task-{index}",
                "accounts": ", ".join(f"acc_{i}" for i in range(accounts)),
                "status": "成功" if index % 3 else "失败",
                "duration": "30秒",
                "result": "完成",
                "start_time": f"2026-01-{(index % 28) + 1:02d} 12:00:00",
                "details": [
                    {
                        "account": f"acc_{(index + slot) % accounts}",
                        "chat": f"@chat_{slot}",
                        "status": "成功",
                        "note": "备注" * 8,
                    }
                    for slot in range(10)
                ],
            }
            for index in range(500)
        ],
    )
    _write_json(
        tmp_path / "webui_combo_progress.json",
        {
            f"combo-{index}": {
                "id": f"combo-{index}",
                "record": {"id": f"combo-{index}", "status": "运行中"},
                "details": [
                    {
                        "account": f"acc_{slot}",
                        "chat": f"@chat_{slot}",
                        "status": "进行中",
                        "note": "日志" * 20,
                    }
                    for slot in range(30)
                ],
                "logs": [f"第 {line} 行日志" for line in range(200)],
                "updated_at": float(index),
            }
            for index in range(30)
        },
    )
    for filename in (
        "webui_join_alerts.json",
        "webui_speak_alerts.json",
        "webui_speech_alerts.json",
        "webui_speech_insufficient_alerts.json",
        "webui_win_alerts.json",
        "webui_channels_full_alerts.json",
        "webui_scan_cap_alerts.json",
        "webui_checklist_unactionable_alerts.json",
    ):
        _write_json(
            tmp_path / filename,
            {
                "alerts": [
                    {
                        "account": f"acc_{index % accounts}",
                        "chat": f"@chat_{index}",
                        "error": "错误" * 10,
                        "time": "2026-01-01 12:00:00",
                    }
                    for index in range(200)
                ]
            },
        )
    _write_json(
        tmp_path / "webui_task_accounts.json",
        {
            f"签到|task-{index}": [f"acc_{i}" for i in range(accounts)]
            for index in range(300)
        },
    )
    _write_json(
        tmp_path / "webui_message_read.json",
        {"read": [f"win|{index}" for index in range(1000)]},
    )
    _write_json(
        tmp_path / "webui_message_pin.json",
        {"pinned": [f"win|{index}" for index in range(200)]},
    )
    _write_json(
        tmp_path / "webui_win_settings.json",
        {"win_forward_target": ["me"], "win_sweep_limit": 100},
    )
    _write_json(
        tmp_path / "giveaway_tasks.json",
        {
            "seq": 50,
            "tasks": {
                str(index): {
                    "accounts": [f"acc_{index % accounts}"],
                    "last_start_time": "2026-01-01 12:00:00",
                    "last_duration": "10秒",
                    "last_outcome": "已完成",
                    "last_joined": index,
                }
                for index in range(50)
            },
        },
    )

    started = time.perf_counter()
    store = open_store(tmp_path)
    elapsed = time.perf_counter() - started
    try:
        assert store.available
        assert not store.status.degraded
        print(f"real-scale migration: {elapsed * 1000:.1f}ms")
        assert elapsed < 5.0
    finally:
        store.close()
