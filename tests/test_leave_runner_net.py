"""批量退频道子任务：扫描网络失败按失败退出码返回的回归测试。"""

import asyncio

import pytest

from tg_signer import leave_runner
from tg_signer.webui import auto_leave


class _NetFailDialogsClient:
    """get_dialogs 一律超时，模拟代理僵死时的扫描阶段。"""

    async def connect(self):
        return True

    async def get_dialogs(self):
        raise TimeoutError("proxy dead")
        yield  # pragma: no cover


def _patch_net_client(monkeypatch):
    monkeypatch.setattr(
        auto_leave, "get_client", lambda *a, **k: _NetFailDialogsClient()
    )

    async def _noop_disconnect(*_a, **_k):
        return None

    monkeypatch.setattr(auto_leave, "safe_disconnect", _noop_disconnect)


def test_scan_stale_chats_default_keeps_old_behavior(tmp_path, monkeypatch):
    """页面路径默认行为不变：网络失败仍返回空列表，不抛异常。"""
    _patch_net_client(monkeypatch)
    logs = []
    rows = asyncio.run(
        auto_leave.scan_stale_chats("alice", tmp_path, 7, "channel", True, logs.append)
    )
    assert rows == []
    assert any("扫描失败" in line for line in logs)


def test_scan_stale_chats_raise_on_net_error(tmp_path, monkeypatch):
    """组合任务路径：扫描因网络异常中断时抛 ScanNetworkError。"""
    _patch_net_client(monkeypatch)
    with pytest.raises(auto_leave.ScanNetworkError, match="网络异常导致扫描未完成"):
        asyncio.run(
            auto_leave.scan_stale_chats(
                "alice",
                tmp_path,
                7,
                "channel",
                True,
                lambda *_a: None,
                raise_on_net_error=True,
            )
        )


def test_scan_stale_chats_floodwait_is_not_network_error(tmp_path, monkeypatch):
    """限流不是网络失败：即使开启抛错也不应抛 ScanNetworkError。"""
    from pyrogram.errors import FloodWait

    class FloodClient:
        async def connect(self):
            return True

        async def get_dialogs(self):
            raise FloodWait(value=5)
            yield  # pragma: no cover

    monkeypatch.setattr(auto_leave, "get_client", lambda *a, **k: FloodClient())

    async def _noop_disconnect(*_a, **_k):
        return None

    monkeypatch.setattr(auto_leave, "safe_disconnect", _noop_disconnect)
    rows = asyncio.run(
        auto_leave.scan_stale_chats(
            "alice",
            tmp_path,
            7,
            "channel",
            True,
            lambda *_a: None,
            raise_on_net_error=True,
        )
    )
    assert rows == []


def test_run_leave_scan_network_failure_returns_net_exit_code(tmp_path, monkeypatch):
    """扫描网络失败时子任务按网络失败退出码（4）结束，不再显示成功。"""

    async def fake_scan(*args, **kwargs):
        # leave_runner 必须把「网络失败要抛错」的开关打开
        assert kwargs.get("raise_on_net_error") is True
        raise auto_leave.ScanNetworkError("网络异常导致扫描未完成（账号：alice）")

    monkeypatch.setattr(leave_runner, "scan_stale_chats", fake_scan)
    code = asyncio.run(
        leave_runner.run_leave(
            {"account": "alice", "session_dir": str(tmp_path), "workdir": str(tmp_path)}
        )
    )
    assert code == 4 == leave_runner.NET_UNFINISHED_EXIT_CODE
