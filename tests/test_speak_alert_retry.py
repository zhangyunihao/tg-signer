"""消息中心「发言任务」重试按钮：提醒落盘字段、重发口令与成功后消除。"""

import asyncio
from types import SimpleNamespace

import pytest

from tg_signer.webui import giveaway
from tg_signer.webui.messages import collect_messages


class _FakeClient:
    """最小客户端：按目标发送；目标失败时可按群名在会话里兜底匹配。"""

    def __init__(self, title_chat_id=None, fail_target=False):
        self.title_chat_id = title_chat_id
        self.fail_target = fail_target
        self.sent = []

    async def connect(self):
        return True

    async def get_dialogs(self, limit=300):
        if self.title_chat_id is None:
            return
        yield SimpleNamespace(
            chat=SimpleNamespace(id=self.title_chat_id, title="上海艺术展览会交流群")
        )

    async def send_message(self, target, text):
        if self.fail_target and target != self.title_chat_id:
            raise RuntimeError("403 CHAT_WRITE_FORBIDDEN")
        self.sent.append((target, text))


async def _noop_disconnect(_client):
    return None


def _speak_alert(tmp_path):
    return {
        "account": "知遥quatre",
        "group": "上海艺术展览会交流群",
        "password": "欢迎浦东老师时雨",
        "error": "403 CHAT_WRITE_FORBIDDEN",
        "target": "-1001234567890",
        "session_dir": str(tmp_path),
        "time": "11-02 03:03:28",
    }


def test_speak_alert_carries_retry_payload(tmp_path):
    giveaway._handle_speak_event(
        None, tmp_path, {"type": "speak_failed", **_speak_alert(tmp_path)}
    )

    alerts = giveaway.load_speak_alerts(tmp_path)
    assert alerts[0]["target"] == "-1001234567890"
    assert alerts[0]["session_dir"] == str(tmp_path)

    msgs = [m for m in collect_messages(tmp_path) if m["tag"] == "发言任务"]
    assert len(msgs) == 1
    assert msgs[0]["retry"]["password"] == "欢迎浦东老师时雨"


@pytest.mark.asyncio
async def test_retry_speak_alert_sends_and_removes(tmp_path, monkeypatch):
    alert = _speak_alert(tmp_path)
    giveaway._handle_speak_event(None, tmp_path, {"type": "speak_failed", **alert})

    client = _FakeClient()
    monkeypatch.setattr(giveaway, "get_client", lambda *a, **k: client)
    monkeypatch.setattr(giveaway, "safe_disconnect", _noop_disconnect)

    ok, detail = await giveaway.retry_speak_alert(tmp_path, alert)
    assert ok is True
    assert "欢迎浦东老师时雨" in detail
    assert client.sent == [(-1001234567890, "欢迎浦东老师时雨")]

    giveaway.remove_speak_alert(tmp_path, alert)
    assert giveaway.load_speak_alerts(tmp_path) == []


@pytest.mark.asyncio
async def test_retry_speak_alert_falls_back_to_group_title(tmp_path, monkeypatch):
    alert = _speak_alert(tmp_path)
    alert["target"] = ""
    client = _FakeClient(title_chat_id=-1009999, fail_target=True)
    monkeypatch.setattr(giveaway, "get_client", lambda *a, **k: client)
    monkeypatch.setattr(giveaway, "safe_disconnect", _noop_disconnect)

    ok, _detail = await giveaway.retry_speak_alert(tmp_path, alert)
    assert ok is True
    assert client.sent == [(-1009999, "欢迎浦东老师时雨")]


@pytest.mark.asyncio
async def test_retry_speak_alert_keeps_alert_on_failure(tmp_path, monkeypatch):
    alert = _speak_alert(tmp_path)
    giveaway._handle_speak_event(None, tmp_path, {"type": "speak_failed", **alert})

    client = _FakeClient(title_chat_id=None, fail_target=True)
    monkeypatch.setattr(giveaway, "get_client", lambda *a, **k: client)
    monkeypatch.setattr(giveaway, "safe_disconnect", _noop_disconnect)

    ok, detail = await giveaway.retry_speak_alert(tmp_path, alert)
    assert ok is False
    assert "403 CHAT_WRITE_FORBIDDEN" in detail
    assert len(giveaway.load_speak_alerts(tmp_path)) == 1


class _BlockingClient(_FakeClient):
    """connect 卡住直到测试放行，用于模拟「第一次重试进行中」。"""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def connect(self):
        self.started.set()
        await self.release.wait()
        return True


@pytest.mark.asyncio
async def test_retry_speak_alert_rejects_concurrent_retry_for_same_account(
    tmp_path, monkeypatch
):
    alert = _speak_alert(tmp_path)
    client = _BlockingClient()
    monkeypatch.setattr(giveaway, "get_client", lambda *a, **k: client)
    monkeypatch.setattr(giveaway, "safe_disconnect", _noop_disconnect)

    first = asyncio.create_task(giveaway.retry_speak_alert(tmp_path, alert))
    await client.started.wait()

    ok, detail = await giveaway.retry_speak_alert(tmp_path, alert)
    assert ok is False
    assert "正在重试中" in detail

    client.release.set()
    ok, _detail = await first
    assert ok is True
    assert giveaway._SPEAK_RETRY_IN_FLIGHT == set()


@pytest.mark.asyncio
async def test_retry_all_speak_alerts_serial_and_summarize(tmp_path, monkeypatch):
    alerts = [
        {**_speak_alert(tmp_path), "group": "群A", "password": "口令A"},
        {**_speak_alert(tmp_path), "group": "群B", "password": "口令B"},
    ]
    for a in alerts:
        giveaway._handle_speak_event(None, tmp_path, {"type": "speak_failed", **a})

    client = _FakeClient()
    monkeypatch.setattr(giveaway, "get_client", lambda *args, **kwargs: client)
    monkeypatch.setattr(giveaway, "safe_disconnect", _noop_disconnect)

    progress = []
    ok_count, failures = await giveaway.retry_all_speak_alerts(
        tmp_path, on_progress=lambda i, n: progress.append((i, n))
    )
    assert ok_count == 2
    assert failures == []
    assert progress == [(1, 2), (2, 2)]
    assert giveaway.load_speak_alerts(tmp_path) == []
    assert len(client.sent) == 2


@pytest.mark.asyncio
async def test_retry_all_speak_alerts_keeps_failed_alerts(tmp_path, monkeypatch):
    alerts = [
        {**_speak_alert(tmp_path), "group": "群A", "password": "口令A"},
        {**_speak_alert(tmp_path), "group": "群B", "password": "口令B"},
    ]
    for a in alerts:
        giveaway._handle_speak_event(None, tmp_path, {"type": "speak_failed", **a})

    # 目标发送失败且按群名匹配不到会话：两条都失败
    client = _FakeClient(title_chat_id=None, fail_target=True)
    monkeypatch.setattr(giveaway, "get_client", lambda *args, **kwargs: client)
    monkeypatch.setattr(giveaway, "safe_disconnect", _noop_disconnect)

    ok_count, failures = await giveaway.retry_all_speak_alerts(tmp_path)
    assert ok_count == 0
    assert len(failures) == 2
    # 提醒按写入倒序存储，这里只校验两条失败都带上了各自群名
    assert any(f.startswith("群A：") for f in failures)
    assert any(f.startswith("群B：") for f in failures)
    # 失败的提醒保留，便于再次一键重发
    assert len(giveaway.load_speak_alerts(tmp_path)) == 2
