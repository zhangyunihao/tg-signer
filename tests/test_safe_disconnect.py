"""safe_disconnect 兜底断连逻辑测试。"""

import asyncio

import pytest

from tg_signer.core import safe_disconnect


class FakeStorage:
    def __init__(self) -> None:
        self.conn = object()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, fail_graceful: bool = False, fail_all: bool = False) -> None:
        self.disconnect_calls = 0
        self.terminate_calls = 0
        self.storage = FakeStorage()
        self._fail_graceful = fail_graceful
        self._fail_all = fail_all

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self._fail_all or (self._fail_graceful and self.terminate_calls == 0):
            raise RuntimeError("disconnect failed")
        self.storage.conn = None

    async def terminate(self) -> None:
        # pyrogram 的 Client.terminate() 是异步协程，Fake 需与真实 API 一致
        self.terminate_calls += 1


def test_safe_disconnect_graceful_success():
    client = FakeClient()
    asyncio.run(safe_disconnect(client))
    assert client.disconnect_calls == 1
    assert client.terminate_calls == 0
    assert client.storage.closed is False


def test_safe_disconnect_retries_after_terminate():
    client = FakeClient(fail_graceful=True)
    asyncio.run(safe_disconnect(client))
    assert client.terminate_calls == 1
    assert client.disconnect_calls == 2
    assert client.storage.closed is False


def test_safe_disconnect_force_closes_storage_when_all_fail():
    client = FakeClient(fail_all=True)
    asyncio.run(safe_disconnect(client))
    assert client.disconnect_calls == 2
    assert client.terminate_calls == 1
    assert client.storage.closed is True


def test_safe_disconnect_none_client_is_noop():
    asyncio.run(safe_disconnect(None))


def test_safe_disconnect_timeout_falls_back_to_storage_close():
    client = FakeClient()

    async def hang_disconnect() -> None:
        await asyncio.sleep(10)

    client.disconnect = hang_disconnect  # type: ignore[method-assign]
    asyncio.run(safe_disconnect(client, timeout=0.05))
    assert client.terminate_calls == 1
    assert client.storage.closed is True


@pytest.mark.asyncio
async def test_safe_disconnect_within_running_loop():
    client = FakeClient(fail_graceful=True)
    await safe_disconnect(client)
    assert client.terminate_calls == 1
