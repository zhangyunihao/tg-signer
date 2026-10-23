"""随机发言重复执行测试：每轮结束断开连接，等待间隔期间不占用 session 数据库。"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from tg_signer.webui import random_chat


class FakeClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.connected = False
        self.sent: list[str] = []

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def get_me(self):
        return type("Me", (), {"first_name": "nick"})()


@pytest.mark.asyncio
async def test_repeat_rounds_disconnect_between_rounds(monkeypatch):
    """重复 2 次 = 共 3 轮；每轮结束断开连接，下一轮重新连接。"""
    events: list[str] = []
    clients: list[FakeClient] = []

    def fake_get_client(name, workdir=None):
        events.append("connect")
        client = FakeClient(name)
        clients.append(client)
        return client

    async def fake_safe_disconnect(client):
        client.connected = False
        events.append("disconnect")

    monkeypatch.setattr(random_chat, "get_client", fake_get_client)
    monkeypatch.setattr(random_chat, "safe_disconnect", fake_safe_disconnect)

    engine = random_chat.RandomChatEngine()
    engine._stop = asyncio.Event()
    engine.pool = ["hi"]

    async def fake_account_loop(
        client, account, chat_id, interval, log, delete_after, total_count
    ):
        client.sent.append("hi")
        engine.progress[account] = len(client.sent)

    monkeypatch.setattr(engine, "_account_loop", fake_account_loop)
    logs: list[str] = []

    await engine._loop(
        ["acc"],
        123,
        1,
        Path("."),
        logs.append,
        0,
        1,
        repeat_count=2,
        repeat_interval=1,
    )

    assert events == ["connect", "disconnect"] * 3
    assert all(c.connected is False for c in clients)
    assert sum(len(c.sent) for c in clients) == 3
    assert engine._round == 3
    assert engine._total_sent == 3
    assert any("不占用 session 数据库" in line for line in logs)
    assert any("随机发言任务完成" in line for line in logs)


@pytest.mark.asyncio
async def test_stop_during_wait_ends_task(monkeypatch):
    """等待下一轮期间停止任务：不再连接下一轮。"""
    events: list[str] = []

    def fake_get_client(name, workdir=None):
        events.append("connect")
        return FakeClient(name)

    async def fake_safe_disconnect(client):
        events.append("disconnect")

    async def fake_account_loop(
        client, account, chat_id, interval, log, delete_after, total_count
    ):
        return None

    monkeypatch.setattr(random_chat, "get_client", fake_get_client)
    monkeypatch.setattr(random_chat, "safe_disconnect", fake_safe_disconnect)

    engine = random_chat.RandomChatEngine()
    engine._stop = asyncio.Event()
    monkeypatch.setattr(engine, "_account_loop", fake_account_loop)
    logs: list[str] = []

    task = asyncio.create_task(
        engine._loop(
            ["acc"],
            123,
            1,
            Path("."),
            logs.append,
            0,
            1,
            repeat_count=5,
            repeat_interval=30,
        )
    )
    # 等首轮结束进入等待状态后停止
    for _ in range(100):
        if engine._next_round_at is not None:
            break
        await asyncio.sleep(0.01)
    engine._stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert events == ["connect", "disconnect"]
    assert any("随机发言已停止" in line for line in logs)


def test_format_wait():
    assert random_chat.format_wait(0) == "0秒"
    assert random_chat.format_wait(30) == "30秒"
    assert random_chat.format_wait(600) == "10分钟"
    assert random_chat.format_wait(7200) == "2小时"


@pytest.mark.asyncio
async def test_db_locked_postpones_and_retries(monkeypatch):
    """到点执行时数据库被占用：不结束任务，延后后重试同一轮。"""
    attempts: list[str] = []

    def fake_get_client(name, workdir=None):
        attempts.append(name)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return FakeClient(name)

    async def fake_safe_disconnect(client):
        return None

    monkeypatch.setattr(random_chat, "get_client", fake_get_client)
    monkeypatch.setattr(random_chat, "safe_disconnect", fake_safe_disconnect)

    engine = random_chat.RandomChatEngine()
    engine._stop = asyncio.Event()
    engine.pool = ["hi"]
    monkeypatch.setattr(engine, "LOCK_RETRY_SECONDS", 0.01)

    async def fake_account_loop(
        client, account, chat_id, interval, log, delete_after, total_count
    ):
        client.sent.append("hi")
        engine.progress[account] = len(client.sent)

    monkeypatch.setattr(engine, "_account_loop", fake_account_loop)
    logs: list[str] = []

    await engine._loop(
        ["acc"], 123, 1, Path("."), logs.append, 0, 1, repeat_count=0, repeat_interval=0
    )

    assert attempts == ["acc", "acc"]
    assert any("数据库被其他任务占用" in line and "重试" in line for line in logs)
    assert engine._total_sent == 1
    assert any("随机发言任务完成" in line for line in logs)
    assert engine._lock_wait_until is None


@pytest.mark.asyncio
async def test_db_locked_gives_up_after_max_retries(monkeypatch):
    """数据库一直被占用：达到最大重试次数后结束任务，不无限等待。"""
    attempts: list[str] = []

    def fake_get_client(name, workdir=None):
        attempts.append(name)
        raise sqlite3.OperationalError("database is locked")

    async def fake_safe_disconnect(client):
        return None

    monkeypatch.setattr(random_chat, "get_client", fake_get_client)
    monkeypatch.setattr(random_chat, "safe_disconnect", fake_safe_disconnect)

    engine = random_chat.RandomChatEngine()
    engine._stop = asyncio.Event()
    monkeypatch.setattr(engine, "LOCK_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(engine, "LOCK_MAX_RETRIES", 2)
    logs: list[str] = []

    await engine._loop(["acc"], 123, 1, Path("."), logs.append, 0, 1)

    # 首次连接 + 2 次重试后放弃
    assert len(attempts) == 3
    assert any("数据库持续被占用" in line and "已重试 2 次" in line for line in logs)
    assert not any("随机发言任务完成" in line for line in logs)


@pytest.mark.asyncio
async def test_db_locked_retry_can_be_stopped(monkeypatch):
    """数据库占用等待期间可立即停止任务，不再重连。"""
    attempts: list[str] = []

    def fake_get_client(name, workdir=None):
        attempts.append(name)
        raise sqlite3.OperationalError("database is locked")

    async def fake_safe_disconnect(client):
        return None

    monkeypatch.setattr(random_chat, "get_client", fake_get_client)
    monkeypatch.setattr(random_chat, "safe_disconnect", fake_safe_disconnect)

    engine = random_chat.RandomChatEngine()
    engine._stop = asyncio.Event()
    monkeypatch.setattr(engine, "LOCK_RETRY_SECONDS", 60)
    logs: list[str] = []

    task = asyncio.create_task(
        engine._loop(["acc"], 123, 1, Path("."), logs.append, 0, 1)
    )
    for _ in range(100):
        if engine._lock_wait_until is not None:
            break
        await asyncio.sleep(0.01)
    engine._stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert attempts == ["acc"]
    assert any("随机发言已停止" in line for line in logs)
