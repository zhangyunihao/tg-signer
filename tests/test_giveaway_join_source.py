"""自动加入提取范围测试：原始抽奖帖的参与条件频道应纳入自动加入候选。"""

import asyncio

import tg_signer.giveaway_core as gc
from tg_signer.giveaway_core import GiveawayEngine


async def _nosleep(*a, **k):
    pass


class _Msg:
    def __init__(self, text: str):
        self.text = text
        self.id = 1
        self.reply_markup = None
        self.chat = None


def _engine() -> GiveawayEngine:
    engine = GiveawayEngine(state=None)
    engine.confirm_wait = 0  # 不轮询 bot 后续消息
    engine.auto_join = True
    return engine


def test_click_confirm_joins_from_source_message(monkeypatch):
    """原始抽奖帖中的频道目标应被提取并加入（不只 bot 回复）。"""
    engine = _engine()
    # 加入频道路径含真实 sleep(5)：注入 no-op 保持用例聚焦行为本身
    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    source = _Msg("参与条件：加入 上海修车社区【公开榜】 https://t.me/shanghai_board")
    seen_messages: list = []

    async def fake_join(account, client, bot_username, message):
        seen_messages.append(message)
        return False

    monkeypatch.setattr(engine, "_join_from_message", fake_join)

    client = type("C", (), {})()
    asyncio.run(
        engine._click_confirm("u1", client, "some_bot", None, "", source_message=source)
    )
    assert source in seen_messages


def test_click_confirm_without_source_keeps_old_behavior(monkeypatch):
    """未传原始帖时行为不变（仅 bot 回复）。"""
    engine = _engine()
    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    start_reply = _Msg("加入频道后参与 https://t.me/bot_reply_ch")
    seen_messages: list = []

    async def fake_join(account, client, bot_username, message):
        seen_messages.append(message)
        return False

    monkeypatch.setattr(engine, "_join_from_message", fake_join)

    client = type("C", (), {})()
    asyncio.run(engine._click_confirm("u1", client, "some_bot", start_reply, ""))
    assert seen_messages == [start_reply]
