"""WebUI 轮询生命周期：断连等待重连、仅在删除时退出、异常日志与退避。"""

import asyncio
import logging
from typing import Optional

from tg_signer.webui import activity


class FakeClient:
    """只实现轮询判断所需属性的最小 client 替身。"""

    def __init__(
        self, client_id: str = "c1", *, deleted: bool = False, connected: bool = True
    ) -> None:
        self.id = client_id
        self.is_deleted = deleted
        self.has_socket_connection = connected


def _set_state(client_id: str, *, hidden: bool = False, tab: str = "") -> None:
    activity._STATE[client_id] = {"hidden": hidden, "tab": tab}


def test_is_client_alive_only_depends_on_deleted():
    assert activity.is_client_alive(FakeClient(connected=False)) is True
    assert activity.is_client_alive(FakeClient(deleted=True)) is False


def test_should_poll_ignores_socket_connection():
    # 短暂断连时仍应刷新（写入 outbox，重连后回放），不能返回 False
    assert activity.should_poll(FakeClient(connected=False)) is True


def test_should_poll_without_state_returns_true():
    # 独立进度页没有活跃度记录，行为保持不变
    assert activity.should_poll(FakeClient(client_id="no-state")) is True


def test_should_poll_respects_visibility_and_tab():
    client = FakeClient()
    _set_state(client.id, hidden=True, tab="run")
    assert activity.should_poll(client) is False
    _set_state(client.id, hidden=False, tab="run")
    assert activity.should_poll(client, "run") is True
    assert activity.should_poll(client, "giveaway") is False


def test_should_poll_deleted_client_is_false():
    assert activity.should_poll(FakeClient(deleted=True)) is False


def test_poll_delay_backoff():
    assert activity.poll_delay(2.0, 0) == 2.0
    assert activity.poll_delay(2.0, 1) == 4.0
    assert activity.poll_delay(2.0, 2) == 8.0
    assert activity.poll_delay(2.0, 10) == 30.0  # 上限


def test_run_poll_logs_unexpected_exit(caplog):
    async def boom() -> None:
        raise RuntimeError("boom")

    client = FakeClient()
    with caplog.at_level(logging.ERROR, logger=activity.__name__):
        asyncio.run(activity._run_poll(boom(), name="test-poll", client=client))

    assert "异常退出" in caplog.text
    assert "boom" in caplog.text


def test_run_poll_reraises_cancellation():
    async def wait_forever() -> None:
        await asyncio.Event().wait()

    async def main() -> Optional[BaseException]:
        task = asyncio.create_task(
            activity._run_poll(wait_forever(), name="cancel-poll", client=FakeClient())
        )
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError as exc:
            return exc
        return None

    assert isinstance(asyncio.run(main()), asyncio.CancelledError)
