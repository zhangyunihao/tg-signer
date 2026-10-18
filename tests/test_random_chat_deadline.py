"""随机发言引擎 20 分钟运行硬上限测试。"""

import asyncio
import time

import pytest

from tg_signer.webui.random_chat import RandomChatEngine


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id, text):
        self.sent.append(text)


@pytest.mark.asyncio
async def test_account_loop_stops_at_deadline():
    """截止时间已过时，账号循环不发送任何消息并记录超时日志。"""
    engine = RandomChatEngine()
    engine._stop = asyncio.Event()
    engine._deadline = time.monotonic() - 1
    logs: list[str] = []
    client = FakeClient()

    await engine._account_loop(client, "acc", 123, 1, logs.append, 0, 10)

    assert client.sent == []
    assert engine._stop.is_set()
    assert any("20 分钟" in line for line in logs)


@pytest.mark.asyncio
async def test_account_loop_sends_before_deadline():
    """截止时间未到时正常发送，达到条数上限后自然结束。"""
    engine = RandomChatEngine()
    engine._stop = asyncio.Event()
    engine._deadline = time.monotonic() + 60
    engine.pool = ["hello"]
    logs: list[str] = []
    client = FakeClient()

    await engine._account_loop(client, "acc", 123, 0, logs.append, 0, 2)

    assert client.sent == ["hello", "hello"]
    assert not any("20 分钟" in line for line in logs)
