"""bot 回复/弹窗判定测试：已结束与条件未满足弹窗都不得记「已参与」。"""

import asyncio
import types

import tg_signer.giveaway_core as gc
from tg_signer.giveaway_core import GiveawayEngine
from tg_signer.giveaway_state import GiveawayState


class _Msg:
    def __init__(self, text: str):
        self.text = text
        self.id = 1
        self.entities = []
        self.reply_markup = None
        self.chat = None


def _engine() -> GiveawayEngine:
    engine = GiveawayEngine(state=None)
    engine.confirm_wait = 0
    engine.auto_join = True
    return engine


def test_click_confirm_returns_on_ended_start_reply(monkeypatch):
    """start 后 bot 直接回复「该抽奖活动已结束」→ 立即返回，不加入不重发。"""
    engine = _engine()
    start_reply = _Msg("该抽奖活动已结束")
    calls: list[str] = []

    async def fake_join(*a, **k):
        calls.append("join")
        return False, False

    async def fake_start(*a, **k):
        calls.append("start")
        return _Msg("ok")

    monkeypatch.setattr(engine, "_join_from_message", fake_join)

    async def fake_start_bot(username, param):
        calls.append("start_bot")
        return _Msg("ok")

    monkeypatch.setattr(types.SimpleNamespace(), "x", 1, raising=False)  # 占位，无需
    asyncio.run(
        engine._click_confirm("u1", types.SimpleNamespace(), "somebot", start_reply, "")
    )
    assert calls == []


def test_click_confirm_stops_on_ended_restart_reply(monkeypatch):
    """重发 start 后 bot 回复「已结束」→ 不再点确认按钮。"""
    engine = _engine()
    # start 回复带加入要求 → 走重发分支
    start_reply = _Msg("请加入频道 https://t.me/some_channel 后重试")
    clicked: list = []

    async def fake_join(*a, **k):
        return False, False

    async def fake_start_bot(username, param):
        return _Msg("该抽奖活动已结束")

    async def fake_history(username, limit):
        return
        yield  # 无更多消息

    async def fake_callback(**kwargs):
        clicked.append(kwargs)

    monkeypatch.setattr(engine, "_join_from_message", fake_join)
    monkeypatch.setattr(engine, "_find_confirm_button", lambda m: None)

    client = types.SimpleNamespace(
        start_bot=fake_start_bot,
        get_chat_history=fake_history,
        request_callback_answer=fake_callback,
    )
    asyncio.run(engine._click_confirm("u1", client, "somebot", start_reply, ""))
    assert clicked == []


def test_try_confirm_button_ended_popup_not_completed():
    """点击确认按钮后弹窗「抽奖不存在或已结束」：按未完成返回 ended 原因。

    此前的返回值是 (True, True, "")，上层会把它当参与成功记成 [已参与]。
    """
    engine = _engine()
    logs: list[str] = []
    engine.log = logs.append

    async def fake_callback(**kwargs):
        return types.SimpleNamespace(message="抽奖不存在或已结束")

    client = types.SimpleNamespace(request_callback_answer=fake_callback)
    msg = _Msg("参与抽奖")
    btn = types.SimpleNamespace(text="参与抽奖", callback_data="/check?id=1", url=None)
    stop, completed, reason = asyncio.run(
        engine._try_confirm_button("u1", client, "dbchoujiang123_bot", msg, btn, [])
    )
    assert (stop, completed) == (True, False)
    assert reason == gc.CONFIRM_REASON_ENDED
    assert not any("[已参与]" in line for line in logs)


def test_handle_start_reply_ended_popup_records_ended_note(tmp_path, monkeypatch):
    """确认按钮弹窗报已结束：写入 note=ended、归还名额，不计 [已参与]。"""
    state = GiveawayState(tmp_path)
    engine = GiveawayEngine(state=state)
    engine.confirm_wait = 0
    logs: list[str] = []
    engine.log = logs.append
    events: list[dict] = []
    engine.on_event = events.append

    async def fake_click_confirm(*a, **k):
        return False, gc.CONFIRM_REASON_ENDED

    monkeypatch.setattr(engine, "_click_confirm", fake_click_confirm)
    engine.joined = 1  # 参与前已预留的名额
    asyncio.run(
        engine._handle_start_reply(
            "u1",
            types.SimpleNamespace(),
            "dbchoujiang123_bot",
            "P1",
            "频道A",
            "标题",
            True,
            _Msg("参与抽奖"),
            None,
        )
    )
    record = state.record("u1", "dbchoujiang123_bot", "P1")
    assert record is not None and record["note"] == "ended"
    assert engine.joined == 0
    assert not any("[已参与]" in line for line in logs)
    assert any("[已结束]" in line for line in logs)
    assert events and all(e["ok"] is False for e in events)


def test_source_btn_popup_participate_failed_not_recorded():
    """「您的发言数:0 反馈数:0 参与失败」弹窗：不记成功，计失败并归还名额。"""
    engine = _engine()
    logs: list[str] = []
    engine.log = logs.append
    events: list[dict] = []
    engine.on_event = events.append
    engine.failed = 0
    engine.joined = 1  # 预留名额
    btn = types.SimpleNamespace(text="参与抽奖", callback_data="/check?id=1")
    blocked = engine._source_btn_popup_blocked(
        "acc3",
        btn,
        "您的发言数:0 反馈数:0 参与失败",
        "1|1",
        "频道A",
        True,
    )
    assert blocked is True
    assert engine.failed == 1
    assert engine.joined == 0
    assert not any("[已参与]" in line for line in logs)
    assert events and events[0]["ok"] is False
    assert events[0]["bot"] == "source_button"
    # 事件带 type/detail，WebUI 消息中心据此生成「发言数不够」类别提醒
    assert events[0]["type"] == "speech_insufficient"
    assert events[0]["detail"] == "您的发言数:0 反馈数:0 参与失败"


def test_speech_insufficient_event_goes_to_message_center(tmp_path):
    """speech_insufficient 事件 → 消息中心新增「发言数不够」类别（同键去重）。"""
    from tg_signer.webui import giveaway
    from tg_signer.webui.messages import collect_messages

    event = {
        "type": "speech_insufficient",
        "account": "acc3",
        "bot": "source_button",
        "button": "参与抽奖",
        "channel": "频道A",
        "title": "",
        "detail": "您的发言数:0 反馈数:0 参与失败",
    }
    # client=None：顶部通知在无浏览器上下文时静默失败，不影响持久化
    giveaway.handle_giveaway_event(None, tmp_path, event)
    giveaway.handle_giveaway_event(None, tmp_path, event)
    alerts = giveaway.load_speech_insufficient_alerts(tmp_path)
    assert len(alerts) == 1
    msgs = [m for m in collect_messages(tmp_path) if m["tag"] == "发言数不够"]
    assert len(msgs) == 1
    text = msgs[0]["text"]
    assert "发言数不够" in text
    assert "置顶抽奖帖「参与抽奖」" in text
    assert "您的发言数:0 反馈数:0 参与失败" in text
