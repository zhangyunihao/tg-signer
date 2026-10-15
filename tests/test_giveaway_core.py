"""自动抽奖：bot 深度链接解析、消息链接提取、关键词匹配、去重存储与引擎决策。"""

from types import SimpleNamespace

import pytest

from tg_signer.giveaway_core import (
    GiveawayEngine,
    GiveawayState,
    extract_bot_links,
    message_matches_keywords,
    parse_bot_start_link,
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

    async def get_users(self, username):
        if username not in self.bot_users:
            raise RuntimeError("user not found")
        return SimpleNamespace(is_bot=True)

    async def start_bot(self, username, param):
        self.started.append((username, param))
        return None

    def add_handler(self, handler):
        self.handlers.append(handler)


def _engine(tmp_path, keywords=("抽奖",), delay_max=0.0):
    state = GiveawayState(tmp_path)
    engine = GiveawayEngine(state=state)
    engine.keywords = tuple(keywords)
    engine.delay_range = (0.0, delay_max)
    engine.confirm_wait = 0
    return engine, state


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
