"""自动抽奖：bot 深度链接解析、消息链接提取、关键词匹配、去重存储与引擎决策。"""

from types import SimpleNamespace

import pytest

from tg_signer.giveaway_core import (
    FLOOD_COOLDOWN,
    GiveawayEngine,
    GiveawayState,
    extract_bot_links,
    extract_channel_targets,
    message_matches_keywords,
    parse_bot_start_link,
    parse_draw_date,
)

# ---------------- parse_bot_start_link ----------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://t.me/SomeLotteryBot?start=abc123", ("SomeLotteryBot", "abc123")),
        ("http://t.me/SomeLotteryBot?start=abc123", ("SomeLotteryBot", "abc123")),
        ("t.me/SomeLotteryBot?start=abc123", ("SomeLotteryBot", "abc123")),
        ("HTTPS://T.ME/SomeBot?start=a_b-c", ("SomeBot", "a_b-c")),
        (
            "tg://resolve?domain=SomeBot&start=P1",
            ("SomeBot", "P1"),
        ),
        # URL 紧贴中文/标点：参数按合法字符截断
        (
            "快来抽奖https://t.me/SomeBot?start=abc_123-x（速来）",
            ("SomeBot", "abc_123-x"),
        ),
        ("https://t.me/SomeBot?start=Z9", ("SomeBot", "Z9")),
    ],
)
def test_parse_bot_start_link_valid(url, expected):
    assert parse_bot_start_link(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/SomeBot",  # 无 start 参数
        "https://t.me/SomeBot/123",  # 路径型（消息/邀请链接）
        "https://t.me/SomeBot?startgroup=true",  # 拉群，语义不同
        "https://t.me/SomeBot?startapp=abc",  # 小程序
        "https://t.me/joinchat/AAAxxxx",  # 邀请链接
        "https://t.me/durov",  # 普通用户/频道
        "t.me/ab?start=x",  # 用户名不足 5 位
        "https://t.me/SomeBot?start=",  # 空参数
        "not a link",
        "",
        None,
    ],
)
def test_parse_bot_start_link_invalid(url):
    assert parse_bot_start_link(url) is None


def _msg(text=None, entities=None, caption=None, caption_entities=None, buttons=None):
    reply_markup = SimpleNamespace(inline_keyboard=buttons) if buttons else None
    return SimpleNamespace(
        text=text,
        entities=entities,
        caption=caption,
        caption_entities=caption_entities,
        reply_markup=reply_markup,
        chat=SimpleNamespace(title="测试频道", id=-1001234567890),
    )


def _btn(text="参与", url=None, callback_data=None):
    return SimpleNamespace(text=text, url=url, callback_data=callback_data)


def _ent(entity_type, url=None):
    return SimpleNamespace(type=entity_type, url=url)


# ---------------- extract_bot_links ----------------

def test_extract_links_from_plain_text_and_entities_and_buttons():
    message = _msg(
        text="今日抽奖 https://t.me/TextBot?start=t1",
        entities=[
            _ent("bold"),
            _ent("MessageEntityType.TEXT_URL", "https://t.me/HiddenBot?start=h_1"),
            _ent("MessageEntityType.URL"),
        ],
        buttons=[
            [
                _btn("参与抽奖", url="https://t.me/BtnBot?start=b1"),
                _btn("官网", url="https://t.me/durov"),
            ]
        ],
    )
    assert extract_bot_links(message) == [
        ("TextBot", "t1"),
        ("HiddenBot", "h_1"),
        ("BtnBot", "b1"),
    ]


def test_extract_links_dedup_preserves_order():
    message = _msg(
        text="t.me/SameBot?start=AAA 和 "
        "https://t.me/SameBot?start=AAA 重复，"
        "https://t.me/OtherBot?start=BBB",
    )
    assert extract_bot_links(message) == [
        ("SameBot", "AAA"),
        ("OtherBot", "BBB"),
    ]


def test_extract_links_from_caption_entity():
    message = _msg(
        caption="看图抽奖",
        caption_entities=[_ent("text_url", url="t.me/CapBot?start=c1")],
    )
    assert extract_bot_links(message) == [("CapBot", "c1")]


def test_extract_links_empty_message():
    assert extract_bot_links(_msg(text="普通聊天，无链接")) == []


# ---------------- message_matches_keywords ----------------

def test_message_matches_keywords():
    message = _msg(text="今晚八点抽奖活动")
    assert message_matches_keywords(message, ["抽奖"])
    assert not message_matches_keywords(message, ["空投"])
    # 空关键词 = 全部放行
    assert message_matches_keywords(message, [])
    # caption 也算文本
    assert message_matches_keywords(_msg(caption="抽奖"), ["抽奖"])


# ---------------- GiveawayState 去重持久化 ----------------

def test_state_dedup_and_persistence(tmp_path):
    state = GiveawayState(tmp_path)
    assert not state.has("acc1", "SomeBot", "p1")
    state.add("acc1", "SomeBot", "p1", channel="频道A")
    assert state.has("acc1", "SomeBot", "p1")
    # 用户名大小写不敏感
    assert state.has("acc1", "somebot", "p1")
    # 参数不同 / 账号不同 视为不同活动
    assert not state.has("acc1", "SomeBot", "p2")
    assert not state.has("acc2", "SomeBot", "p1")
    state.save()

    reloaded = GiveawayState(tmp_path)
    assert reloaded.has("acc1", "SomeBot", "p1")
    assert reloaded.count() == 1
    record = reloaded._data["acc1"]["somebot|p1"]
    assert record["bot"] == "SomeBot"
    assert record["channel"] == "频道A"


def test_state_caps_per_account(tmp_path):
    state = GiveawayState(tmp_path)
    for i in range(10):
        state.add("acc", f"Bot{i:05d}", "p")
    import tg_signer.giveaway_core as core

    old_cap = core.MAX_STATE_PER_ACCOUNT
    core.MAX_STATE_PER_ACCOUNT = 5
    try:
        state2 = GiveawayState(tmp_path)
        for i in range(10, 20):
            state2.add("acc", f"Bot{i:05d}", "p")
        assert len(state2._data["acc"]) == 5
    finally:
        core.MAX_STATE_PER_ACCOUNT = old_cap


# ---------------- GiveawayEngine._process_message 决策 ----------------

class FakeClient:
    def __init__(self, bot_users=None):
        self.bot_users = bot_users or {"LotteryBot"}
        self.started = []
        self.handlers = []
        self.history = []
        self.is_connected = True

    async def get_users(self, username):
        if username not in self.bot_users:
            raise RuntimeError("user not found")
        return SimpleNamespace(is_bot=True)

    async def start_bot(self, username, param):
        self.started.append((username, param))
        return None

    def get_chat_history(self, chat):
        return self._history()

    async def _history(self):
        for m in self.history:
            yield m

    def add_handler(self, handler):
        self.handlers.append(handler)


def _engine(tmp_path, keywords=("抽奖",), delay_max=0.0):
    state = GiveawayState(tmp_path)
    engine = GiveawayEngine(state=state)
    engine.keywords = tuple(keywords)
    engine.delay_range = (0.0, delay_max)
    engine.confirm_wait = 0
    return engine, state


def test_parse_draw_date_expired_and_future():
    from datetime import datetime

    now = datetime(2026, 10, 17, 12, 0, 0)
    # 已过期：明确年份的过去日期
    assert parse_draw_date("1月1日开奖", now=now) == datetime(2026, 1, 1, 23, 59, 59)
    assert parse_draw_date(
        "2026-01-01 20:00 开奖", now=now
    ) == datetime(2026, 1, 1, 20, 0)
    # 未来：明确的将来日期
    assert parse_draw_date(
        "2026-12-01 开奖", now=now
    ) == datetime(2026, 12, 1, 23, 59, 59)
    # 无提示词 / 无日期：返回 None
    assert parse_draw_date("1月1日发红包", now=now) is None
    assert parse_draw_date("明天开奖，速来", now=now) is None
    # 无年份按当年理解：10 月看到「1月1日开奖」视为今年已开奖（跳过旧帖）
    assert parse_draw_date("1月1日开奖", now=now) == datetime(2026, 1, 1, 23, 59, 59)
    assert parse_draw_date(
        "2027年1月1日8点开奖", now=now
    ) == datetime(2027, 1, 1, 8, 0)


def test_engine_skips_expired_draw_message(tmp_path):
    import asyncio
    from datetime import datetime, timedelta

    engine, _state = _engine(tmp_path)
    client = FakeClient(bot_users={"LotteryBot"})
    logs: list = []
    engine.log = logs.append
    past = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    message = _msg(text=f"抽奖 {past} 开奖 https://t.me/LotteryBot?start=act1")
    asyncio.run(_run_process(engine, client, "acc1", message))
    assert client.started == []
    assert any("已开奖" in line for line in logs)


def test_engine_respects_max_joins(tmp_path):
    import asyncio

    engine, state = _engine(tmp_path)
    engine.max_joins = 1
    client = FakeClient(bot_users={"LotteryBot", "OtherBot"})
    logs: list = []
    engine.log = logs.append
    message = _msg(text="抽奖 https://t.me/LotteryBot?start=act1")
    asyncio.run(_run_process(engine, client, "acc1", message))
    assert len(client.started) == 1
    # 达到上限后不再参与
    message2 = _msg(text="抽奖 https://t.me/OtherBot?start=act2")
    asyncio.run(_run_process(engine, client, "acc1", message2))
    assert len(client.started) == 1
    assert any("参与上限" in line for line in logs)


def _expired_msg(days_ago: int, idx: int):
    from datetime import datetime, timedelta

    past = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    return _msg(text=f"抽奖 {past} 开奖 https://t.me/OtherBot?start=e{idx}")


def test_scan_now_stops_on_expired_streak(tmp_path):
    """连续 5 条已开奖抽奖帖后停止扫描该频道。"""
    import asyncio
    from datetime import datetime, timedelta

    engine, _state = _engine(tmp_path)
    engine.chats = [123]
    client = FakeClient(bot_users={"LotteryBot", "OtherBot"})
    engine._clients = {"acc": client}
    logs: list = []
    engine.log = logs.append
    future = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    client.history = [
        _expired_msg(9, 1),
        _expired_msg(8, 2),
        _msg(text="抽奖 https://t.me/LotteryBot?start=new1"),  # 参与，清零
        _expired_msg(7, 3),
        _expired_msg(6, 4),
        _expired_msg(5, 5),
        _expired_msg(4, 6),
        _expired_msg(3, 7),  # 连续第 5 条，停止
        _msg(text=f"抽奖 {future} 开奖 https://t.me/LotteryBot?start=new2"),
    ]
    asyncio.run(engine.scan_now())
    assert client.started == [("LotteryBot", "new1")]
    assert any("连续 5 条已开奖" in line for line in logs)
    assert any("本次参与 1 个" in line for line in logs)


def test_scan_now_stops_on_max_joins(tmp_path):
    """达到参与条数上限后停止扫描。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    engine.max_joins = 1
    engine.chats = [123]
    client = FakeClient(bot_users={"LotteryBot", "OtherBot"})
    engine._clients = {"acc": client}
    logs: list = []
    engine.log = logs.append
    client.history = [
        _msg(text="抽奖 https://t.me/LotteryBot?start=a1"),
        _msg(text="抽奖 https://t.me/OtherBot?start=a2"),
        _msg(text="抽奖 https://t.me/LotteryBot?start=a3"),
    ]
    asyncio.run(engine.scan_now())
    assert len(client.started) == 1
    # 达到上限直接停止该频道扫描（不会再拉后续消息）
    assert any("本次参与 1 个" in line for line in logs)


async def _run_process(engine, client, account, message):
    import asyncio

    await engine._process_message(account, client, message)
    # 参与过程持锁但不创建后台任务，执行完即完成
    await asyncio.sleep(0)


def test_engine_participates_and_dedups(tmp_path):
    import asyncio

    engine, state = _engine(tmp_path)
    client = FakeClient(bot_users={"LotteryBot", "OtherBot"})

    message = _msg(
        text="抽奖啦 https://t.me/LotteryBot?start=act1",
        buttons=[[_btn("按钮", url="t.me/OtherBot?start=act2")]],
    )
    asyncio.run(_run_process(engine, client, "acc1", message))
    assert sorted(client.started) == [
        ("LotteryBot", "act1"),
        ("OtherBot", "act2"),
    ]
    # 再次处理同一消息：去重不重复参与
    asyncio.run(_run_process(engine, client, "acc1", message))
    assert len(client.started) == 2
    # 另一账号各自独立参与
    asyncio.run(_run_process(engine, client, "acc2", message))
    assert len(client.started) == 4


def test_engine_skips_non_bot_and_keyword_miss(tmp_path):
    import asyncio

    engine, _state = _engine(tmp_path)
    client = FakeClient()

    # 关键词不命中：不参与、不 resolve
    miss = _msg(text="今日闲聊 t.me/LotteryBot?start=act1")
    asyncio.run(_run_process(engine, client, "acc", miss))
    assert client.started == []

    # 链接目标不是 bot：跳过
    notbot = _msg(text="抽奖 https://t.me/NotABot?start=act3")
    asyncio.run(_run_process(engine, client, "acc", notbot))
    assert client.started == []


# ---------------- extract_channel_targets（自动加入目标） ----------------

def test_extract_channel_targets_mixed():
    msg = _msg(
        text="请关注 @GiveawayChannel 和 https://t.me/NewsGroup",
        buttons=[
            [_btn("加入频道", url="https://t.me/+AbCdEf123456")],
            [_btn("老式邀请", url="t.me/joinchat/XYZ_98765")],
        ],
    )
    assert extract_channel_targets(msg) == [
        "GiveawayChannel",
        "NewsGroup",
        "https://t.me/+AbCdEf123456",
        "https://t.me/joinchat/XYZ_98765",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "抽奖 bot t.me/SomeBot?start=abc",  # start 深链
        "抽奖 t.me/SomeBot?startgroup=true",  # startgroup
        "看帖子 t.me/SomeChannel/123",  # 帖子路径链接
        "没有任何链接",
    ],
)
def test_extract_channel_targets_excludes(text):
    assert extract_channel_targets(_msg(text=text)) == []


def test_extract_channel_targets_chinese_at():
    # 中文后紧跟 @ 不应漏掉
    assert extract_channel_targets(_msg(text="关注@GiveawayChannel参与抽奖")) == [
        "GiveawayChannel"
    ]


class FloodBotClient(FakeClient):
    """start_bot 始终抛 FLOOD_WAIT（带 .value 等待秒数）的假客户端"""

    def __init__(self, wait_seconds=2688):
        super().__init__(bot_users={"LotteryBot"})
        self.wait_seconds = wait_seconds
        self.calls = 0

    async def start_bot(self, username, param):
        self.calls += 1
        exc = RuntimeError(
            f"Telegram says: [420 FLOOD_WAIT_X] - Please wait "
            f"{self.wait_seconds} seconds before repeating the action."
        )
        exc.value = self.wait_seconds
        raise exc


def test_engine_flood_wait_breaker_and_cooldown(tmp_path):
    import asyncio

    engine, state = _engine(tmp_path)
    client = FloodBotClient()

    # 连续 3 次限流（不同 param，模拟不同抽奖帖）：第 3 次后熔断该 bot
    for i in range(3):
        msg = _msg(text=f"抽奖 https://t.me/LotteryBot?start=act{i + 1}")
        asyncio.run(_run_process(engine, client, "acc", msg))
    assert client.calls == 3
    assert "acc|lotterybot" in engine._flood_bots

    # 熔断后新的 param 也不再调用 start_bot，不计入失败
    failed_before = engine.failed
    msg = _msg(text="抽奖 https://t.me/LotteryBot?start=act4")
    asyncio.run(_run_process(engine, client, "acc", msg))
    assert client.calls == 3
    assert engine.failed == failed_before

    # 失败已记录为 flood_wait：has() 不算已参与，但冷却期内 is_blocked
    assert not state.has("acc", "LotteryBot", "act1")
    assert state.is_blocked("acc", "LotteryBot", "act1")


def test_state_flood_wait_cooldown_expiry(tmp_path):
    from datetime import datetime, timedelta

    state = GiveawayState(tmp_path)
    state.add("acc", "Bot", "p1", note="flood_wait:2688")
    # 手动把时间戳改到冷却期之前
    old = (datetime.now() - timedelta(seconds=FLOOD_COOLDOWN + 1)).isoformat(
        timespec="seconds"
    )
    state._data["acc"][state._key("Bot", "p1")]["ts"] = old
    assert not state.is_blocked("acc", "Bot", "p1")
    # 已参与记录不受影响
    state.add("acc", "Bot", "p2", note="joined")
    assert state.has("acc", "Bot", "p2")
    assert state.is_blocked("acc", "Bot", "p2") is False


class JoinClient:
    def __init__(self):
        self.joined = []

    async def join_chat(self, target):
        self.joined.append(target)
        return SimpleNamespace(title=f"chat-{target}")


def test_engine_auto_join_dedups_and_skips_self(tmp_path):
    import asyncio

    engine, _state = _engine(tmp_path)
    client = JoinClient()
    msg = _msg(text="请先关注 @GiveawayChannel 和 https://t.me/NewsGroup")
    asyncio.run(engine._join_from_message("acc", client, "LotteryBot", msg))
    assert client.joined == ["GiveawayChannel", "NewsGroup"]
    # 同一目标不重复加入
    asyncio.run(engine._join_from_message("acc", client, "LotteryBot", msg))
    assert client.joined == ["GiveawayChannel", "NewsGroup"]
    # bot 自己不加入
    self_msg = _msg(text="已向 @LotteryBot 发送请求")
    asyncio.run(engine._join_from_message("acc", client, "LotteryBot", self_msg))
    assert client.joined == ["GiveawayChannel", "NewsGroup"]


def test_engine_join_invite_request_sent_is_not_failure(tmp_path):
    """私有群审批制：INVITE_REQUEST_SENT 视为申请已提交，不算失败。"""
    import asyncio

    class RequestClient:
        def __init__(self):
            self.calls = []

        async def join_chat(self, target):
            self.calls.append(target)
            raise RuntimeError(
                'Telegram says: [400 INVITE_REQUEST_SENT] - You have '
                'successfully requested to join this chat or channel. '
                '(caused by "channels.JoinChannel")'
            )

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = RequestClient()
    msg = _msg(text="加入 https://t.me/+AbCdEfGh123 参与")
    joined_any = asyncio.run(
        engine._join_from_message("acc", client, "LotteryBot", msg)
    )
    assert joined_any is True
    assert any("已提交加入" in line and "待管理员审批" in line for line in logs)
    assert not any("失败" in line for line in logs)
