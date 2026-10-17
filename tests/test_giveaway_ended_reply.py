"""bot 回复「抽奖已结束」终态识别测试：跳过加入、重发与按钮点击。"""

import asyncio
import types

from tg_signer.giveaway_core import GiveawayEngine


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
        return False

    async def fake_start(*a, **k):
        calls.append("start")
        return _Msg("ok")

    monkeypatch.setattr(engine, "_join_from_message", fake_join)

    async def fake_start_bot(username, param):
        calls.append("start_bot")
        return _Msg("ok")

    monkeypatch.setattr(
        types.SimpleNamespace(), "x", 1, raising=False
    )  # 占位，无需
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
        return False

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
