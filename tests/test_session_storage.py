import pytest

from tg_signer.core import Client, ResilientSQLiteStorage


@pytest.mark.asyncio
async def test_session_db_uses_wal_and_busy_timeout(tmp_path):
    storage = ResilientSQLiteStorage("acc", workdir=tmp_path)
    await storage.open()
    try:
        mode = storage.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        busy_timeout = storage.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy_timeout > 0
        await storage.dc_id(2)
        assert await storage.dc_id() == 2
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_second_open_while_first_still_connected(tmp_path):
    """两个任务同时打开同一 session：Pyrogram 原实现在 open() 里 VACUUM 会直接报锁库。"""
    first = ResilientSQLiteStorage("acc", workdir=tmp_path)
    await first.open()
    try:
        await first.dc_id(2)
        second = ResilientSQLiteStorage("acc", workdir=tmp_path)
        await second.open()
        try:
            assert await second.dc_id() == 2
        finally:
            await second.close()
        # 第二个连接关闭后，第一个仍可正常读写
        await first.dc_id(4)
        assert await first.dc_id() == 4
    finally:
        await first.close()


def test_client_uses_resilient_storage_for_file_session(tmp_path):
    client = Client("acc", api_id=1, api_hash="0" * 32, workdir=tmp_path)
    assert isinstance(client.storage, ResilientSQLiteStorage)


def test_client_keeps_default_storage_for_session_string(tmp_path):
    client = Client(
        "acc", api_id=1, api_hash="0" * 32, workdir=tmp_path, session_string="x"
    )
    assert not isinstance(client.storage, ResilientSQLiteStorage)
