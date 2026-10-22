"""网络容错（手机代理不稳定场景）测试。

覆盖：
- BaseUserWorker._call_telegram_api 网络类异常限时重试与超时兜底
- timeout_aiter 异步迭代逐项超时
- webui.auto_leave.leave_one 退出调用超时计为失败
"""

import asyncio

import pytest

from tg_signer.core import BaseUserWorker, timeout_aiter


class _FakeApp:
    key = "test-net-resilience"


class _StubWorker:
    """仅提供 _call_telegram_api 用到的属性（app.key / log）。"""

    def __init__(self) -> None:
        self.app = _FakeApp()
        self.logs: list[tuple[str, str]] = []

    def log(self, msg, level: str = "INFO", **kwargs) -> None:
        self.logs.append((level.upper(), str(msg)))


@pytest.fixture(autouse=True)
def _clean_api_state(monkeypatch):
    """隔离模块级调用状态，避免测试间相互影响。"""
    monkeypatch.setattr("tg_signer.core._API_LAST_CALL_AT", {})
    monkeypatch.setattr("tg_signer.core._API_ASYNC_LOCKS", {})


@pytest.mark.asyncio
async def test_call_telegram_api_retries_on_transient_os_error(monkeypatch):
    monkeypatch.setattr("tg_signer.core._API_NET_RETRY_DELAY_SECONDS", 0.01)
    worker = _StubWorker()
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("connection reset by peer")
        return "ok"

    result = await BaseUserWorker._call_telegram_api(worker, "测试操作", flaky)
    assert result == "ok"
    assert calls["n"] == 3
    warnings = [msg for level, msg in worker.logs if level == "WARNING"]
    assert any("网络异常" in msg for msg in warnings)


@pytest.mark.asyncio
async def test_call_telegram_api_raises_runtime_error_when_retries_exhausted(
    monkeypatch,
):
    monkeypatch.setattr("tg_signer.core._API_NET_RETRY_DELAY_SECONDS", 0.01)
    worker = _StubWorker()

    async def always_fail():
        raise OSError("proxy dead")

    with pytest.raises(RuntimeError, match="网络异常"):
        await BaseUserWorker._call_telegram_api(worker, "测试操作", always_fail)
    warnings = [msg for level, msg in worker.logs if level == "WARNING"]
    assert len(warnings) == 2  # 重试 2 次各记一条 WARNING


@pytest.mark.asyncio
async def test_call_telegram_api_timeout_raises_runtime_error(monkeypatch):
    monkeypatch.setattr("tg_signer.core._API_CALL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("tg_signer.core._API_NET_RETRY_DELAY_SECONDS", 0.01)

    async def hang():
        await asyncio.sleep(10)

    with pytest.raises(RuntimeError, match="网络异常"):
        await BaseUserWorker._call_telegram_api(_StubWorker(), "测试操作", hang)


@pytest.mark.asyncio
async def test_call_telegram_api_floodwait_still_raises_without_retry():
    from pyrogram import errors

    async def flood():
        raise errors.FloodWait(value=1)

    with pytest.raises(errors.FloodWait):
        await BaseUserWorker._call_telegram_api(
            _StubWorker(), "测试操作", flood, retry_on_floodwait=False
        )


@pytest.mark.asyncio
async def test_timeout_aiter_yields_all_items_then_stops():
    async def gen():
        yield 1
        yield 2

    got = [item async for item in timeout_aiter(gen(), 1, "测试拉取")]
    assert got == [1, 2]


@pytest.mark.asyncio
async def test_timeout_aiter_slow_item_raises_runtime_error():
    async def gen():
        yield 1
        await asyncio.sleep(10)
        yield 2

    got = []
    with pytest.raises(RuntimeError, match="超时"):
        async for item in timeout_aiter(gen(), 0.05, "测试拉取"):
            got.append(item)
    assert got == [1]


@pytest.mark.asyncio
async def test_leave_one_timeout_counts_as_failure(monkeypatch):
    from tg_signer.webui import auto_leave

    monkeypatch.setattr(auto_leave, "_NET_CALL_TIMEOUT", 0.05)

    class FakeLeaveClient:
        async def leave_chat(self, chat_id):
            await asyncio.sleep(10)

    logs: list[str] = []
    ok = await auto_leave.leave_one(FakeLeaveClient(), -100123, "测试频道", logs.append)
    assert ok is False
    assert any("网络超时" in msg for msg in logs)
