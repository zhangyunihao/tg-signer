"""自动抽奖：bot 深度链接解析、消息链接提取、关键词匹配、去重存储与引擎决策。"""

import time
from types import SimpleNamespace

import pytest

from tg_signer.giveaway_core import GiveawayEngine
from tg_signer.giveaway_parsing import (
    extract_bot_links,
    extract_channel_targets,
    message_matches_keywords,
    message_region_allowed,
    message_requires_speech,
    parse_bot_start_link,
    parse_draw_date,
    parse_region_limits,
    strip_rules_section,
)
from tg_signer.giveaway_state import FLOOD_COOLDOWN, GiveawayState

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


def _msg(
    text=None, entities=None, caption=None, caption_entities=None, buttons=None, id=None
):
    reply_markup = SimpleNamespace(inline_keyboard=buttons) if buttons else None
    return SimpleNamespace(
        id=id,
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


def test_extract_links_glued_without_spaces():
    """多个链接紧挨着（仅中文标点分隔、无空格）也要逐个取出，不能只取第一个。"""
    message = _msg(
        text="抽奖1 t.me/AlphaBot?start=x1】抽奖2 https://t.me/BetaBotx?start=y2，"
        "抽奖3 tg://resolve?domain=GammaBot&start=z3 和 t.me/DeltaBot?start=w4抽奖"
    )
    assert extract_bot_links(message) == [
        ("AlphaBot", "x1"),
        ("BetaBotx", "y2"),
        ("GammaBot", "z3"),
        ("DeltaBot", "w4"),
    ]


def test_extract_links_glued_same_line_no_space():
    message = _msg(text="t.me/AlphaBot?start=a，https://t.me/BetaBotx?start=b")
    assert extract_bot_links(message) == [("AlphaBot", "a"), ("BetaBotx", "b")]


# ---------------- message_matches_keywords ----------------


def test_message_matches_keywords():
    message = _msg(text="今晚八点抽奖活动")
    assert message_matches_keywords(message, ["抽奖"])
    assert not message_matches_keywords(message, ["空投"])
    # 空关键词 = 全部放行
    assert message_matches_keywords(message, [])
    # caption 也算文本
    assert message_matches_keywords(_msg(caption="抽奖"), ["抽奖"])


# ---------------- 抽奖说明段落剔除（@LotteryBestBot 帖式） ----------------

LOTTERYBEST_POST = """🎰 XX福利半价抽奖

📮 参与条件：
🎫 加入-某测试群组A备用
🎫 加入-某测试群组A
└  🎟️ 发言数大于 10 条
🎫 加入-七七后花园
🎫 加入-某测试群组A报告录
🎫 加入-某测试群组A榜

🎁 奖品内容：
💰️ 半价p × 1

⚠️ 抽奖说明：
1.中奖请在24小时之内联系老师
2.七天内使用，写报告素质出击
3.退群及频道默认放弃抽奖资格 炸号者无效
4.禁止🚫刷屏乱水
5. 某测试群组A群内每日现金红包🧧 半价劵  等你来拿

🔋 人品加成：⚡️× 9  规则

💁 参与情况：4达标 / 6报名

📅 开奖日期：(北京时间)
2026年10月20日 17时22分00秒"""


def test_strip_rules_section():
    stripped = strip_rules_section(LOTTERYBEST_POST)
    # 说明段落（1.中奖请在24小时... 现金红包 ...）被整体剔除
    assert "现金红包" not in stripped
    assert "24小时之内联系老师" not in stripped
    assert "抽奖说明" not in stripped
    # 其余段落保留
    assert "参与条件" in stripped
    assert "半价p × 1" in stripped
    assert "人品加成" in stripped
    assert "开奖日期" in stripped
    assert "2026年10月20日" in stripped
    # 无说明段头的文本原样返回
    assert strip_rules_section("普通抽奖帖，无说明段") == "普通抽奖帖，无说明段"
    # 末尾说明段（无后续段头）也整体剔除
    assert strip_rules_section("奖品：现金\n抽奖说明：xxxx") == "奖品：现金"


def test_keywords_ignore_rules_section():
    # 说明段里的「现金」「红包」不应触发关键字（奖品内容是半价p，无关键字）
    assert not message_matches_keywords(
        _msg(text=LOTTERYBEST_POST), ["上海", "现金", "红包"]
    )
    # 关键字出现在标题/奖品内容等正常段落时仍然命中
    assert message_matches_keywords(_msg(text=LOTTERYBEST_POST), ["半价"])
    # 无说明段落时行为不变
    assert message_matches_keywords(_msg(text="奖品：现金红包"), ["现金"])


# ---------------- 归属地限制过滤 ----------------


def test_parse_region_limits():
    # 无「归属地」行 = 不限地区
    assert parse_region_limits("正常抽奖帖，无地区限制") is None
    # 常规格式
    assert parse_region_limits("奖品：现金\n归属地 陕西") == [["陕西"]]
    assert parse_region_limits("归属地 上海,江苏") == [["上海", "江苏"]]
    # 全角冒号与空格
    assert parse_region_limits("归属地：上海、江苏") == [["上海", "江苏"]]
    # 行首 emoji 装饰
    assert parse_region_limits("📍归属地：陕西") == [["陕西"]]
    # 句子中间提及「归属地」不误判
    assert parse_region_limits("请填写归属地信息后参与") is None


def test_message_region_allowed():
    # 无归属地行：放行
    assert message_region_allowed(_msg(text="https://t.me/LotteryBestBot?start=x"))
    # 限制地区不含上海：拦截
    assert not message_region_allowed(
        _msg(text="抽奖\n归属地 陕西\nhttps://t.me/LotteryBestBot?start=x")
    )
    # 限制地区含上海：放行
    assert message_region_allowed(
        _msg(text="抽奖\n归属地 上海,江苏\nhttps://t.me/LotteryBestBot?start=x")
    )
    # 全国/不限：放行
    assert message_region_allowed(_msg(text="归属地 全国"))
    assert message_region_allowed(_msg(text="归属地 不限"))
    # caption 同样生效
    assert not message_region_allowed(_msg(caption="归属地 北京"))
    # 多行归属地：任一行放行即可
    assert message_region_allowed(_msg(text="归属地 陕西\n归属地 上海"))


# ---------------- 发言数要求检测与双向账户跳过 ----------------


def test_message_requires_speech():
    # 用户给出的两种真实帖式样
    assert message_requires_speech(_msg(text="🎫 加入-群组\n└  🎟️ 发言数大于 10 条"))
    assert message_requires_speech(_msg(text="发言数大于10条"))
    assert message_requires_speech(_msg(text="发言数超过 5 条"))
    assert message_requires_speech(_msg(text="发言数不少于 3 条"))
    assert message_requires_speech(_msg(text="发言大于 8 条"))
    # 无发言要求 / 口令要求不算发言要求
    assert not message_requires_speech(_msg(text="正常抽奖，发送口令激活"))
    assert not message_requires_speech(_msg(text="发言数要求见群规"))


def test_engine_two_way_account_skips_speech_required(tmp_path):
    import asyncio

    engine, state = _engine(tmp_path)
    client = FakeClient(bot_users={"LotteryBot"})
    engine.two_way_accounts = {"acc_two_way"}
    engine.log = lambda _m: None

    text = (
        "🎰 某测试频道B抽奖 参与条件：加入群组\n"
        "└  🎟️ 发言数大于 10 条\n"
        "https://t.me/LotteryBot?start=x1"
    )
    message = _msg(text=text)

    # 双向账户：发言数要求抽奖自动跳过，不发 start
    asyncio.run(_run_process(engine, client, "acc_two_way", message))
    assert client.started == []
    # 非双向账户：正常参与
    asyncio.run(_run_process(engine, client, "acc_normal", message))
    assert client.started == [("LotteryBot", "x1")]

    # 同一抽奖无发言数要求时，双向账户也正常参与
    plain = _msg(text="抽奖啦 https://t.me/LotteryBot?start=x2")
    asyncio.run(_run_process(engine, client, "acc_two_way", plain))
    assert client.started == [("LotteryBot", "x1"), ("LotteryBot", "x2")]


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
    record = reloaded.record("acc1", "SomeBot", "p1")
    assert record["bot"] == "SomeBot"
    assert record["channel"] == "频道A"


def test_state_caps_per_account(tmp_path):
    state = GiveawayState(tmp_path)
    for i in range(10):
        state.add("acc", f"Bot{i:05d}", "p")
    import tg_signer.giveaway_state as state_mod

    old_cap = state_mod.MAX_STATE_PER_ACCOUNT
    state_mod.MAX_STATE_PER_ACCOUNT = 5
    try:
        state2 = GiveawayState(tmp_path)
        for i in range(10, 20):
            state2.add("acc", f"Bot{i:05d}", "p")
        assert state2.count("acc") == 5
    finally:
        state_mod.MAX_STATE_PER_ACCOUNT = old_cap


# ---------------- GiveawayEngine._process_message 决策 ----------------


class FakeClient:
    def __init__(self, bot_users=None):
        self.bot_users = bot_users or {"LotteryBot"}
        self.started = []
        self.handlers = []
        self.history = []
        self.is_connected = True
        self.chat_info = SimpleNamespace(
            pinned_message=None, type=SimpleNamespace(name="CHANNEL")
        )
        # search_messages(filter=PINNED) 返回的置顶消息列表（默认含最新置顶）
        self.pinned_list = []
        self.joined_targets = []
        self.sent = []
        self.forwarded = []

    async def join_chat(self, target):
        self.joined_targets.append(target)
        return SimpleNamespace(title=f"chat-{target}")

    async def forward_messages(self, chat_id, from_chat_id, message_ids):
        self.forwarded.append((chat_id, from_chat_id, tuple(message_ids)))

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=1)

    async def get_chat_member(self, target, user_id):
        raise RuntimeError(
            "Telegram says: [400 USER_NOT_PARTICIPANT] - User not participant"
        )

    async def get_chat(self, chat):
        return self.chat_info

    async def search_messages(self, chat, filter=None, limit=None):
        # filter=MessagesFilter.PINNED 时返回全部置顶消息
        name = str(getattr(filter, "name", filter) or "")
        if "PINNED" in name.upper():
            for m in self.pinned_list:
                yield m
        return

    async def get_users(self, username):
        if username not in self.bot_users:
            raise RuntimeError("user not found")
        return SimpleNamespace(is_bot=True)

    async def start_bot(self, username, param):
        self.started.append((username, param))
        return None

    def get_chat_history(self, chat, limit=None, max_id=0):
        return self._history(limit, max_id)

    async def _history(self, limit=None, max_id=0):
        items = self.history
        if max_id:
            items = [m for m in items if getattr(m, "id", 0) <= max_id]
        if limit is not None:
            items = items[:limit]
        for m in items:
            yield m

    def add_handler(self, handler):
        self.handlers.append(handler)


def _engine(tmp_path, keywords=("抽奖",), delay_max=0.0):
    state = GiveawayState(tmp_path)
    engine = GiveawayEngine(state=state)
    engine.keywords = tuple(keywords)
    engine.delay_range = (0.0, delay_max)
    # 测试里禁用加入/解析节奏与滚动配额等待，只验证行为
    engine.join_pace_range = (0.0, 0.0)
    engine.resolve_pace_range = (0.0, 0.0)
    engine.confirm_wait = 0
    return engine, state


def test_parse_draw_date_expired_and_future():
    from datetime import datetime

    now = datetime(2026, 10, 17, 12, 0, 0)
    # 已过期：明确年份的过去日期
    assert parse_draw_date("1月1日开奖", now=now) == datetime(2026, 1, 1, 23, 59, 59)
    assert parse_draw_date("2026-01-01 20:00 开奖", now=now) == datetime(
        2026, 1, 1, 20, 0
    )
    # 未来：明确的将来日期
    assert parse_draw_date("2026-12-01 开奖", now=now) == datetime(
        2026, 12, 1, 23, 59, 59
    )
    # 无提示词 / 无日期：返回 None
    assert parse_draw_date("1月1日发红包", now=now) is None
    assert parse_draw_date("明天开奖，速来", now=now) is None
    # 无年份按当年理解：10 月看到「1月1日开奖」视为今年已开奖（跳过旧帖）
    assert parse_draw_date("1月1日开奖", now=now) == datetime(2026, 1, 1, 23, 59, 59)
    assert parse_draw_date("2027年1月1日8点开奖", now=now) == datetime(2027, 1, 1, 8, 0)


def test_parse_draw_date_choujiangs_bot_sample():
    """@choujiangs_bot 样本：说明段「3日后开奖了」先于「开奖时间」行出现，
    第一个提示词窗口内无日期需继续向后尝试；奖品行「18.8」这类
    非日期数字不得中断解析。"""
    from datetime import datetime

    now = datetime(2026, 10, 19, 10, 0, 0)
    sample = (
        "🎟️ 抽奖标题：福田 浅浅 @qianqian150\n\n"
        "📪抽奖说明：中奖者机器人会发送中奖信息\n"
        "本次抽奖活动3日后开奖了，中奖者联系老师兑奖，"
        "有什么问题联系茶花汇管理员\n\n"
        "🎫 参与条件: \n"
        "🎫 加入-福田浅浅人工白虎天然🐻E（开课🀄️）✅\n"
        "🎫 加入-深圳茶花汇✅\n"
        "🎫 加入-深圳茶花汇 榜单✅\n"
        "🎫 加入-深圳茶花汇抽奖活动规则✅\n\n"
        "🎁 奖品内容: \n"
        "💰️ 奶茶现金红包18.8 × 1\n"
        "💰️ PP200优惠卷 × 10\n"
        "💰️ P100优惠卷 × 10\n\n"
        "📅 开奖时间：2026-09-30 12:00自动开奖"
    )
    assert parse_draw_date(sample, now=now) == datetime(2026, 9, 30, 12, 0)


def test_parse_draw_date_laomao_channel_sample():
    """老猫福利榜样本：「📅 开奖日期:(UTC+8)⏎2026年10月26日 17时00分00秒」，
    前缀「日期:(UTC+8)」+换行占掉十余字符，旧版 +20 窗口把「10月26日」
    截成「10月2」误判为 2026-10-02 已开奖；先出现的「开奖后请24小时内
    联系老师兑奖」窗口内无日期，需继续向后尝试。"""
    from datetime import datetime

    now = datetime(2026, 10, 20, 1, 30, 0)
    sample = (
        "🎰 浦东蜜桃老师优惠抽奖活动\n\n"
        "📮 抽奖条件:\n"
        "🎫 加入-蜜桃的秘密花园\n"
        "🎫 加入-上海老猫修车闲聊(免费上榜)\n"
        "🎫 加入-老猫福利榜【上海】\n\n"
        "🎁 奖品内容:\n"
        "单次半价券 × 1\n\n"
        "💡 活动说明:\n"
        "（1）中奖者7天内使用优惠卷，超时作废\n"
        "（4）开奖后请24小时内联系老师兑奖 @mitaomeimei02\n"
        "（5）提交评价报告联系老猫：@laomaoguanjia2_bot\n"
        "（6）提交报告会获得相应积分 积分用处可在群聊查看\n\n"
        "📅 开奖日期:(UTC+8)\n"
        "2026年10月26日 17时00分00秒"
    )
    assert parse_draw_date(sample, now=now) == datetime(2026, 10, 26, 17, 0)


def test_parse_draw_date_skips_window_edge_truncated_dates():
    """窗口因 +80 上限截断时，贴窗口边界的残缺日期（如被截成
    「2026年10月2」的「2026年10月26日」）不得参与判定。"""
    from datetime import datetime

    now = datetime(2026, 10, 20, 1, 30, 0)
    # 日期起点在窗口内、终点越过 +80 边界：旧逻辑会解析出 2026-10-02
    text = "开奖。" + "字" * 70 + "2026年10月26日 17时00分00秒，其余内容"
    assert parse_draw_date(text, now=now) is None
    # 提示词贴近文本末尾：窗口未被截断，贴边日期是完整的，正常解析
    assert parse_draw_date(
        "长文本" * 60 + "开奖 2026年10月26日 17时00分", now=now
    ) == datetime(2026, 10, 26, 17, 0)


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
    # 已开奖静默跳过，不打印逐条日志
    assert not any("已开奖" in line for line in logs)


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


def test_engine_ignore_history_allows_repeat(tmp_path, monkeypatch):
    """忽略历史成功记录：关闭时已参与过的链接去重跳过，开启后允许重新参与。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)

    engine, state = _engine(tmp_path)
    engine.ignore_history = False
    client = FakeClient(bot_users={"LotteryBot"})
    message = _msg(text="抽奖 https://t.me/LotteryBot?start=dup1")
    asyncio.run(_run_process(engine, client, "acc1", message))
    assert len(client.started) == 1
    # 开关关闭：同链接去重跳过
    asyncio.run(_run_process(engine, client, "acc1", message))
    assert len(client.started) == 1
    # 开启忽略历史后：重新参与
    engine.ignore_history = True
    asyncio.run(_run_process(engine, client, "acc1", message))
    assert len(client.started) == 2


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


def test_scan_now_stops_at_scan_cap_when_keywords_miss(tmp_path):
    """关键词全部未命中时按扫描上限截断，并发出 scan_cap 事件。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    engine.scan_cap = 3
    engine.chats = [123]
    client = FakeClient(bot_users={"LotteryBot", "OtherBot"})
    engine._clients = {"acc": client}
    logs: list = []
    engine.log = logs.append
    events: list = []
    engine.on_event = events.append
    # 均不含关键词「抽奖」：不会触发已开奖止损，只能靠扫描上限截断
    client.history = [
        _msg(text=f"推广 https://t.me/LotteryBot?start=k{i}", id=i) for i in range(1, 6)
    ]
    asyncio.run(engine.scan_now())
    assert client.started == []
    assert any("已扫描 3 条" in line for line in logs)
    caps = [e for e in events if e.get("type") == "scan_cap"]
    assert caps == [{"type": "scan_cap", "account": "acc", "chat": "123", "cap": 3}]


def test_scan_now_enumerates_all_pinned_messages(tmp_path):
    """群组可置顶多条消息：全部枚举处理并打印数量，get_chat 只给最新一条。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    engine.chats = [123]
    client = FakeClient(bot_users={"LotteryBot", "OtherBot"})
    engine._clients = {"acc": client}
    logs: list = []
    engine.log = logs.append
    latest = _msg(id=1002, text="新置顶抽奖 https://t.me/LotteryBot?start=pin2")
    older = _msg(id=1001, text="旧置顶抽奖 https://t.me/OtherBot?start=pin1")
    client.chat_info = SimpleNamespace(
        pinned_message=latest, type=SimpleNamespace(name="SUPERGROUP")
    )
    client.pinned_list = [older, latest]  # PINNED 过滤搜索返回全部置顶
    asyncio.run(engine.scan_now())
    # 最新置顶之外的其他置顶也被参与
    assert sorted(client.started) == [
        ("LotteryBot", "pin2"),
        ("OtherBot", "pin1"),
    ]
    # 打印置顶消息条数
    assert any("共有 2 条置顶消息" in line for line in logs)
    assert any("命中 1 个抽奖链接：@OtherBot" in line for line in logs)


def test_scan_now_participates_pinned_message(tmp_path):
    """群组仅扫置顶消息：置顶抽奖被参与，历史消息不再拉取。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    engine.chats = [123]
    client = FakeClient(bot_users={"LotteryBot"})
    engine._clients = {"acc": client}
    logs: list = []
    engine.log = logs.append
    client.chat_info = SimpleNamespace(
        pinned_message=_msg(text="置顶抽奖 https://t.me/LotteryBot?start=pinned1"),
        type=SimpleNamespace(name="SUPERGROUP"),
    )
    client.history = [
        # 群组不扫历史：即使历史里也有抽奖帖也不参与
        _msg(text="抽奖 https://t.me/LotteryBot?start=hist1"),
    ]
    asyncio.run(engine.scan_now())
    assert client.started == [("LotteryBot", "pinned1")]
    assert any("本次参与 1 个" in line for line in logs)
    # 已参与日志附带抽奖帖标题
    assert any("[已参与]" in line and "置顶抽奖" in line for line in logs)


def test_scan_now_pinned_expired_counts_toward_cap(tmp_path):
    """置顶消息也受参与条数上限约束，达到上限后跳过历史扫描。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    engine.max_joins = 1
    engine.chats = [123]
    client = FakeClient(bot_users={"LotteryBot", "OtherBot"})
    engine._clients = {"acc": client}
    logs: list = []
    engine.log = logs.append
    client.chat_info = SimpleNamespace(
        pinned_message=_msg(text="抽奖 https://t.me/OtherBot?start=pin1"),
        type=SimpleNamespace(name="SUPERGROUP"),
    )
    client.history = [
        _msg(text="抽奖 https://t.me/LotteryBot?start=h1"),
    ]
    asyncio.run(engine.scan_now())
    assert client.started == [("OtherBot", "pin1")]


def test_scan_now_survives_get_chat_failure(tmp_path):
    """get_chat 失败（如权限/网络）不影响后续历史消息扫描。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    engine.chats = [123]
    client = FakeClient(bot_users={"LotteryBot"})
    engine._clients = {"acc": client}
    logs: list = []
    engine.log = logs.append

    async def broken_get_chat(chat):
        raise RuntimeError("chat not found")

    client.get_chat = broken_get_chat
    client.history = [
        _msg(text="抽奖 https://t.me/LotteryBot?start=h1"),
    ]
    asyncio.run(engine.scan_now())
    assert client.started == [("LotteryBot", "h1")]
    assert any("跳过类型识别" in line for line in logs)


def test_scan_now_group_only_scans_pinned(tmp_path):
    """群组只扫指定（置顶）消息，不做历史回溯扫描。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    engine.chats = [123]
    client = FakeClient(bot_users={"LotteryBot"})
    engine._clients = {"acc": client}
    logs: list = []
    engine.log = logs.append
    client.chat_info = SimpleNamespace(
        type=SimpleNamespace(name="SUPERGROUP"),
        pinned_message=_msg(text="置顶抽奖 https://t.me/LotteryBot?start=pin1"),
    )
    client.history = [
        _msg(text="抽奖 https://t.me/LotteryBot?start=hist1"),
    ]
    asyncio.run(engine.scan_now())
    assert client.started == [("LotteryBot", "pin1")]

    # 同一聊天若是频道：忽略置顶，仅扫历史（用独立状态目录避免去重串扰）
    engine2, _state2 = _engine(tmp_path / "channel")
    engine2.chats = [123]
    client2 = FakeClient(bot_users={"LotteryBot"})
    engine2._clients = {"acc": client2}
    engine2.log = lambda _m: None
    client2.chat_info = SimpleNamespace(
        type=SimpleNamespace(name="CHANNEL"),
        pinned_message=_msg(text="置顶抽奖 https://t.me/LotteryBot?start=pin1"),
    )
    client2.history = [
        _msg(text="抽奖 https://t.me/LotteryBot?start=hist1"),
    ]
    asyncio.run(engine2.scan_now())
    assert client2.started == [("LotteryBot", "hist1")]


def test_scan_now_group_pinned_ignores_keywords(tmp_path):
    """群组置顶消息不受关键词影响：无关键词命中也参与；频道置顶仍受约束。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    engine.chats = [123]
    client = FakeClient(bot_users={"LotteryBot"})
    engine._clients = {"acc": client}
    client.chat_info = SimpleNamespace(
        type=SimpleNamespace(name="SUPERGROUP"),
        # 文本不含任何关键词
        pinned_message=_msg(text="福利活动 https://t.me/LotteryBot?start=pin1"),
    )
    client.history = []
    asyncio.run(engine.scan_now())
    assert client.started == [("LotteryBot", "pin1")]

    # 频道置顶：不含关键词则不参与
    engine2, _state2 = _engine(tmp_path / "channel")
    engine2.chats = [123]
    client2 = FakeClient(bot_users={"LotteryBot"})
    engine2._clients = {"acc": client2}
    client2.chat_info = SimpleNamespace(
        type=SimpleNamespace(name="CHANNEL"),
        pinned_message=_msg(text="福利活动 https://t.me/LotteryBot?start=pin1"),
    )
    client2.history = []
    asyncio.run(engine2.scan_now())
    assert client2.started == []


def test_rescue_group_rechecks_pinned_only(tmp_path, monkeypatch):
    """群组补漏只重查置顶消息（新置顶参与、历史不扫、旧置顶去重）。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)

    engine, _state = _engine(tmp_path)
    engine.ignore_history = False  # 显式关闭忽略历史，验证已参与链接去重
    engine.chats = [123]
    client = FakeClient(bot_users={"LotteryBot"})
    engine._clients = {"acc": client}
    logs: list = []
    engine.log = logs.append
    # 已通过扫描阶段缓存为群组
    engine._chat_types["123"] = True
    client.chat_info = SimpleNamespace(
        pinned_message=_msg(text="新置顶抽奖 https://t.me/LotteryBot?start=pin1")
    )
    client.history = [
        _msg(text="历史抽奖 https://t.me/LotteryBot?start=hist1"),
    ]
    asyncio.run(engine._rescue_channel("acc", client, 123))
    assert client.started == [("LotteryBot", "pin1")]

    # 同一置顶再次补漏：水位线去重，不重复参与
    asyncio.run(engine._rescue_channel("acc", client, 123))
    assert client.started == [("LotteryBot", "pin1")]

    # 群主换了新置顶：新链接参与
    client.chat_info = SimpleNamespace(
        pinned_message=_msg(text="换新置顶抽奖 https://t.me/LotteryBot?start=pin2")
    )
    asyncio.run(engine._rescue_channel("acc", client, 123))
    assert client.started == [
        ("LotteryBot", "pin1"),
        ("LotteryBot", "pin2"),
    ]


async def _run_process(engine, client, account, message):
    import asyncio

    await engine._process_message(account, client, message)
    # 参与过程持锁但不创建后台任务，执行完即完成
    await asyncio.sleep(0)


def test_engine_participates_and_dedups(tmp_path):
    import asyncio

    engine, state = _engine(tmp_path)
    engine.ignore_history = False  # 显式关闭忽略历史，验证已参与链接去重
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
    logs: list = []
    engine.log = logs.append
    client = FakeClient()

    # 关键词不命中：不参与、不 resolve，且静默跳过（不打印逐条日志）
    miss = _msg(text="今日闲聊 t.me/LotteryBot?start=act1")
    asyncio.run(_run_process(engine, client, "acc", miss))
    assert client.started == []
    assert not any("关键词未命中" in line for line in logs)

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


def test_engine_flood_wait_hard_block_on_long_wait(tmp_path):
    """限流需等待 ≥FLOOD_HARD_BLOCK_SECONDS 时立即熔断该 bot（不等 3 次），
    且暂停期内换 param 也不再发请求。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    engine, state = _engine(tmp_path)
    client = FloodBotClient(wait_seconds=gc.FLOOD_HARD_BLOCK_SECONDS + 1417)

    msg = _msg(text="抽奖 https://t.me/LotteryBot?start=act1")
    asyncio.run(_run_process(engine, client, "acc", msg))
    assert client.calls == 1
    assert "acc|lotterybot" in engine._flood_bots
    assert engine._flood_paused("acc", "LotteryBot")

    # 同一 bot 的其它 param 也不再调用 start_bot
    msg = _msg(text="抽奖 https://t.me/LotteryBot?start=act2")
    asyncio.run(_run_process(engine, client, "acc", msg))
    assert client.calls == 1

    # 失败已记录为 flood_wait：has() 不算已参与，但冷却期内 is_blocked
    assert not state.has("acc", "LotteryBot", "act1")
    assert state.is_blocked("acc", "LotteryBot", "act1")


def test_engine_flood_wait_breaker_after_three_short_floods(tmp_path, monkeypatch):
    """短限流（<FLOOD_HARD_BLOCK_SECONDS）按次数熔断：连续 3 次后跳过该 bot。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)

    engine, state = _engine(tmp_path)
    client = FloodBotClient(wait_seconds=30)

    for i in range(3):
        msg = _msg(text=f"抽奖 https://t.me/LotteryBot?start=act{i + 1}")
        asyncio.run(_run_process(engine, client, "acc", msg))
        # 模拟暂停期已过（真实运行中要等 Telegram 给的秒数）
        engine._flood_until.clear()
    assert client.calls == 3
    assert "acc|lotterybot" in engine._flood_bots

    # 熔断后新的 param 也不再调用 start_bot，不计入失败
    failed_before = engine.failed
    msg = _msg(text="抽奖 https://t.me/LotteryBot?start=act4")
    asyncio.run(_run_process(engine, client, "acc", msg))
    assert client.calls == 3
    assert engine.failed == failed_before


def test_flood_does_not_overwrite_joined_record(tmp_path):
    """已参与的记录不能被后来的限流记录覆盖（否则成功会翻成失败并被重复参与）。"""
    engine, state = _engine(tmp_path)
    state.add("acc", "LotteryBot", "act1", note="joined")
    exc = RuntimeError("Telegram says: [420 FLOOD_WAIT_X] - Please wait 1717 seconds")
    exc.value = 1717
    assert engine._record_flood("acc", "LotteryBot", "act1", "", exc)
    assert state.has("acc", "LotteryBot", "act1")
    assert not state.is_blocked("acc", "LotteryBot", "act1")


def test_record_flood_persists_account_limit(tmp_path):
    """限流要落盘账号级状态，账户页才能展示剩余时长。"""
    engine, state = _engine(tmp_path)
    exc = RuntimeError("Telegram says: [420 FLOOD_WAIT_X] - Please wait 90 seconds")
    exc.value = 90
    assert engine._record_flood("acc", "LotteryBot", "act1", "", exc)
    limits = state.active_account_limits()
    assert [item["kind"] for item in limits["acc"]] == ["flood_wait"]
    assert limits["acc"][0]["note"] == "@LotteryBot（act1）"
    assert 80 < limits["acc"][0]["until"] - time.time() <= 90


def test_transport_flood_probe_captures_pyrogram_429(monkeypatch):
    """Pyrogram 的协议层 429 只写日志，探针要能捕获其时间点。"""
    import logging

    import tg_signer.giveaway_core as gc

    monkeypatch.setattr(gc, "_LAST_TRANSPORT_FLOOD_AT", 0.0)
    gc._install_transport_flood_probe()
    logging.getLogger("pyrogram.session").warning(
        "Server sent transport error: 429 (Transport flood. Please slow down your requests.)"
    )
    assert gc._LAST_TRANSPORT_FLOOD_AT > 0
    # 普通日志不应被误判
    monkeypatch.setattr(gc, "_LAST_TRANSPORT_FLOOD_AT", 0.0)
    logging.getLogger("pyrogram.session").warning("Server sent transport error")
    assert gc._LAST_TRANSPORT_FLOOD_AT == 0


class _RestartSession:
    """模拟 Pyrogram 会话：429 后重启会替换 connection 对象。"""

    def __init__(self):
        self.connection = object()


class _TransportFloodClient:
    """调用期间发生 429 会话重连、调用方最终只看到超时的假客户端。"""

    def __init__(self):
        self.session = _RestartSession()

    async def start_bot(self, username, param):
        self.session.connection = object()  # 429 触发会话重启
        import asyncio

        raise asyncio.TimeoutError("Request timed out")


def test_transport_flood_syncs_account_limit_on_reconnect(tmp_path, monkeypatch):
    """协议层 429 导致会话重连+超时时，必须落盘账号限流，账户页才不会显示正常。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    engine, state = _engine(tmp_path)
    monkeypatch.setattr(gc, "_LAST_TRANSPORT_FLOOD_AT", time.monotonic())
    client = gc._TimeoutClient(
        _TransportFloodClient(),
        account="deux",
        on_transport_flood=engine._note_transport_flood,
    )

    with pytest.raises(RuntimeError):
        asyncio.run(client.start_bot("LotteryBot", "p1"))

    limits = state.active_account_limits()
    assert [item["kind"] for item in limits["deux"]] == ["flood_wait"]
    assert "Transport flood" in limits["deux"][0]["note"]
    assert limits["deux"][0]["until"] - time.time() > 0
    assert "deux" in engine._transport_flood_logged


def test_transport_flood_without_429_does_not_mark_limit(tmp_path, monkeypatch):
    """没有 429 日志时，普通网络超时不能误报成限流。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    engine, state = _engine(tmp_path)
    monkeypatch.setattr(gc, "_LAST_TRANSPORT_FLOOD_AT", 0.0)
    client = gc._TimeoutClient(
        _TransportFloodClient(),
        account="acc",
        on_transport_flood=engine._note_transport_flood,
    )

    with pytest.raises(RuntimeError):
        asyncio.run(client.start_bot("LotteryBot", "p1"))

    assert state.active_account_limits() == {}


def test_transport_flood_logs_account_and_time(tmp_path):
    """429 明细日志要带账号，并且触发一次主动提醒。"""
    engine, _state = _engine(tmp_path)
    logs: list = []
    alerts: list = []
    engine.log = logs.append
    engine._spawn_flood_alert = alerts.append
    engine._note_transport_flood("deux", 60)
    assert any("[429]" in line and "deux" in line for line in logs)
    assert any("单账号请求过密" in line for line in logs)
    assert alerts and "deux" in alerts[0] and "单账号" in alerts[0]


def test_transport_flood_alert_sent_via_healthy_account(tmp_path):
    """429 提醒要用未限流的账号发到通知目标（默认收藏夹）。"""
    import asyncio

    class _FakeClient:
        def __init__(self):
            self.sent: list = []

        async def send_message(self, target, text):
            self.sent.append((target, text))

    engine, _state = _engine(tmp_path)
    healthy, flooded = _FakeClient(), _FakeClient()
    engine._clients = {"deux": flooded, "3": healthy}
    engine.win_forward_targets = ["me"]
    engine._transport_flood_hits = {"deux": time.time()}

    asyncio.run(engine._send_flood_alert("[429] 测试"))

    assert healthy.sent == [("me", "[429] 测试")]
    assert flooded.sent == []


def test_transport_flood_alert_when_no_healthy_account(tmp_path):
    """所有账号都在限流时，提醒发送失败要记日志而不是静默。"""
    import asyncio

    class _FakeClient:
        async def send_message(self, target, text):
            raise RuntimeError("flood")

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    engine._clients = {"deux": _FakeClient()}
    engine.win_forward_targets = ["me"]
    engine._transport_flood_hits = {}

    asyncio.run(engine._send_flood_alert("[429] 测试"))

    assert any("429 提醒发送到 收藏夹 失败" in line for line in logs)


def test_transport_flood_logs_cluster_when_multi_accounts(tmp_path):
    """60 秒内 3 个账号都被 429：日志判定为共享出口/IP 层风控。"""
    engine, _state = _engine(tmp_path)
    logs: list = []
    alerts: list = []
    engine.log = logs.append
    engine._spawn_flood_alert = alerts.append
    for acc in ("deux", "3", "扣扣un"):
        engine._note_transport_flood(acc, 60)
    cluster = [line for line in logs if "疑似共享出口" in line]
    assert len(cluster) == 1
    assert "deux" in cluster[0] and "扣扣un" in cluster[0]
    assert any("疑似共享出口" in text for text in alerts)


def test_join_flood_paused_reflects_suspend_state(tmp_path):
    """加入被暂停的日志原因要能区分限流与网络异常。"""
    engine, _state = _engine(tmp_path)
    assert not engine._join_flood_paused("acc")
    engine._join_suspended["acc"] = time.monotonic() + 600
    assert engine._join_flood_paused("acc")
    engine._peer_flood_accounts.add("acc")
    assert engine._join_flood_paused("acc")


def test_state_flood_wait_cooldown_expiry(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    import tg_signer.giveaway_state as state_mod

    state = GiveawayState(tmp_path)
    state.add("acc", "Bot", "p1", note="flood_wait:2688")
    assert state.is_blocked("acc", "Bot", "p1")
    # 把「当前时间」推到冷却期之后：记录仍在，但不再视为阻塞
    real_now = datetime.now()

    class _AfterCooldown(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now + timedelta(seconds=FLOOD_COOLDOWN + 1)

    monkeypatch.setattr(state_mod, "datetime", _AfterCooldown)
    assert not state.is_blocked("acc", "Bot", "p1")
    # 已参与记录不受影响
    state.add("acc", "Bot", "p2", note="joined")
    assert state.has("acc", "Bot", "p2")
    assert state.is_blocked("acc", "Bot", "p2") is False


class JoinClient:
    def __init__(self):
        self.joined = []
        # 默认所有目标解析为频道且未加入；可按 target 覆盖类型，None 表示失效
        self.target_types = {}
        # 已加入的目标（小写），get_chat_member 对这些目标不抛异常
        self.member_targets = set()

    async def get_chat(self, target):
        type_name = self.target_types.get(target, "CHANNEL")
        if type_name is None:
            raise RuntimeError("PEER_ID_INVALID")
        return SimpleNamespace(type=SimpleNamespace(name=type_name))

    async def get_chat_member(self, target, user_id):
        if str(target).lower().lstrip("@") in self.member_targets:
            return SimpleNamespace(status="member")
        raise RuntimeError(
            "Telegram says: [400 USER_NOT_PARTICIPANT] - User not participant"
        )

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


def test_engine_join_precheck_filters_users_and_invalid(tmp_path):
    """加入前预检：个人用户/机器人静默跳过，失效目标跳过并提示，不发无效 JoinChannel。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = JoinClient()
    client.target_types = {
        "SomePerson": "USER",
        "HelperBot": "BOT",
        "DeadChannel": None,
    }
    msg = _msg(
        text="加 @SomePerson 和 @HelperBot，再关注 @GoodChannel 参与；"
        "坏目标 https://t.me/DeadChannel 已失效"
    )
    asyncio.run(engine._join_from_message("acc", client, "LotteryBot", msg))
    # 只有有效频道会真正调用 join_chat
    assert client.joined == ["GoodChannel"]
    # 失效目标有提示日志，个人用户/机器人静默
    assert any("跳过无效目标 DeadChannel" in line for line in logs)
    assert not any("SomePerson" in line or "HelperBot" in line for line in logs)


def test_join_precheck_logs_missing_membership(tmp_path):
    """加入前检查成员身份：能看到哪些目标未加入，已加入的跳过。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = JoinClient()
    client.member_targets = {"alreadychan"}  # 这个目标已在（按小写匹配）
    msg = _msg(text="加入 @AlreadyChan 和 @NeedJoinChan 后参与")
    asyncio.run(engine._join_from_message("acc", client, "LotteryBot", msg))
    # 只有未加入的目标会真正 join
    assert client.joined == ["NeedJoinChan"]
    assert any(
        "尚未加入" in line and "NeedJoinChan" in line and "加入后才能参与" in line
        for line in logs
    )
    assert any("已在" in line and "AlreadyChan" in line for line in logs)


def test_join_daily_cap_skips_without_join(tmp_path):
    """滚动 24 小时加入配额用尽后不再调用 join_chat（跨重启仍生效）。"""
    import asyncio
    import time

    engine, state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    engine.join_daily_cap = 20  # 默认不限，测试里显式开启
    # 1 小时以前、24 小时以内的加入尝试：只触发 24 小时配额
    old = time.time() - 2 * 3600
    for _ in range(engine.join_daily_cap):
        state.add_join_event("acc", old)
    client = JoinClient()
    msg = _msg(text="请先关注 @GiveawayChannel")
    asyncio.run(engine._join_from_message("acc", client, "LotteryBot", msg))
    assert client.joined == []
    assert any("今日不再自动加入" in line for line in logs)


def test_join_hourly_cap_skips_without_join(tmp_path):
    """滚动 1 小时加入配额用尽后暂停自动加入。"""
    import asyncio

    engine, state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    engine.join_hourly_cap = 8  # 默认不限，测试里显式开启
    for _ in range(engine.join_hourly_cap):
        state.add_join_event("acc")
    client = JoinClient()
    msg = _msg(text="请先关注 @GiveawayChannel")
    asyncio.run(engine._join_from_message("acc", client, "LotteryBot", msg))
    assert client.joined == []
    assert any("本轮暂停自动加入" in line for line in logs)


def test_join_caps_configurable_per_task(tmp_path):
    """任务级配额配置生效：0 表示不限制。"""
    import asyncio

    engine, state = _engine(tmp_path)
    engine.log = lambda _m: None
    state.add_join_event("acc")
    state.add_join_event("acc")
    engine.join_hourly_cap = 2
    engine.join_daily_cap = 0
    assert asyncio.run(engine._join_ready("acc")) is False  # 1 小时配额 2 已用完
    engine.join_hourly_cap = 0
    assert asyncio.run(engine._join_ready("acc")) is True  # 0 = 不限制


def test_join_daily_cap_excludes_events_older_than_24h(tmp_path):
    """24 小时配额只统计滚动 24 小时内的加入（48 小时日志中的旧记录不计）。"""
    import asyncio
    import time

    engine, state = _engine(tmp_path)
    engine.log = lambda _m: None
    engine.join_daily_cap = 2
    old = time.time() - 30 * 3600
    state.add_join_event("acc", old)
    state.add_join_event("acc", old)
    assert asyncio.run(engine._join_ready("acc")) is True


def test_join_peer_flood_blocks_account_only(tmp_path):
    """PEER_FLOOD 后本账号本轮停止加入，其它账号不受影响。"""
    import asyncio

    class PeerFloodClient(JoinClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def join_chat(self, target):
            self.calls += 1
            raise RuntimeError(
                "Telegram says: [420 PEER_FLOOD] - The method is too spammy"
            )

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = PeerFloodClient()
    msg = _msg(text="请先关注 @ChanA 和 @ChanB")
    asyncio.run(engine._join_from_message("acc", client, "LotteryBot", msg))
    # 首个目标触发 PEER_FLOOD 后不再尝试第二个
    assert client.calls == 1
    assert engine._join_paused("acc")
    assert not engine._join_paused("other")
    assert any("PEER_FLOOD" in line for line in logs)


def test_join_flood_wait_suspends_only_that_account(tmp_path):
    """FLOOD_WAIT 只暂停触发限流的账号，且不再继续打第二个目标。"""
    import asyncio

    class FloodWaitClient(JoinClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def join_chat(self, target):
            self.calls += 1
            raise RuntimeError(
                "Telegram says: [420 FLOOD_WAIT_X] - A wait of 120 seconds is required"
            )

    engine, _state = _engine(tmp_path)
    engine.log = lambda _m: None
    client = FloodWaitClient()
    msg = _msg(text="请先关注 @ChanA 和 @ChanB")
    asyncio.run(engine._join_from_message("acc", client, "LotteryBot", msg))
    assert client.calls == 1
    assert engine._join_paused("acc")
    assert not engine._join_paused("other")


def test_invite_flood_only_pauses_invite_links(tmp_path):
    """私有邀请链接触发限流时只跳过邀请链接，@username 加入照常。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    engine._suspend_joins("acc", 259, "https://t.me/+T8kdoFXKC5o2NjY5")
    assert not engine._join_paused("acc")
    assert engine._invite_blocked("acc", "https://t.me/+AbCdEf123")
    client = JoinClient()
    msg = _msg(text="请先加入 https://t.me/+AbCdEf123 和 @ChanA")
    joined_any, blocked = asyncio.run(
        engine._join_from_message("acc", client, "LotteryBot", msg)
    )
    assert client.joined == ["ChanA"]
    assert joined_any is True
    assert blocked is False
    assert any("邀请链接" in line and "259" in line for line in logs)
    assert any("邀请链接加入限流中" in line for line in logs)


def test_click_confirm_restarts_any_bot_with_join_targets(tmp_path, monkeypatch):
    """通用策略：任何 bot 消息含加入要求时，加入后自动重发一次 start。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"AnyLotteryBot"})
    msg = _msg(text="请先加入 https://t.me/NeedJoinCh 再点下方抽奖")
    asyncio.run(engine._click_confirm("acc", client, "AnyLotteryBot", msg, "r1"))
    # 加入完成后重新 start 了一次
    assert client.started == [("AnyLotteryBot", "r1")]
    assert any("再次发送 start" in line for line in logs)


def test_click_confirm_no_restart_without_join_targets(tmp_path, monkeypatch):
    """bot 消息没有加入要求且不在强制名单：不重发 start。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"OtherLotteryBot"})
    msg = _msg(text="点击下方按钮领取奖励")
    asyncio.run(engine._click_confirm("acc", client, "OtherLotteryBot", msg, "r1"))
    assert client.started == []  # 不重新 start
    assert not any("再次发送 start" in line for line in logs)


def test_click_confirm_prefers_join_retry_button(tmp_path, monkeypatch):
    """bot 提示「加入后需再点一次 我已加入 重试参与」且消息带该回调按钮：
    优先点按钮完成重试，不再重发 start（少一轮防脚本验证）。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = _CallbackClient(bot_users={"DbLotteryBot"})
    msg = _msg(
        text="请先加入 https://t.me/NeedJoinCh 后重试参与。"
        "加入要求参与的频道或者群组后 需要再点一次 我已加入 重试参与",
        buttons=[[_btn("✅ 我已加入 重试参与", callback_data="retry1")]],
    )
    asyncio.run(engine._click_confirm("acc", client, "DbLotteryBot", msg, "r1"))
    # 点了重试按钮，没有重发 start
    assert client.started == []
    assert client.callback_clicks
    assert any("重试按钮" in line and "我已加入" in line for line in logs)


def test_click_confirm_retry_button_failure_falls_back_to_start(tmp_path, monkeypatch):
    """重试按钮点击失败：兜底重发一次 start，不丢参与。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append

    class _BrokenCallbackClient(_CallbackClient):
        async def request_callback_answer(self, chat_id, message_id, callback_data):
            raise RuntimeError("button expired")

    client = _BrokenCallbackClient(bot_users={"DbLotteryBot"})
    msg = _msg(
        text="请先加入 https://t.me/NeedJoinCh 后重试参与。",
        buttons=[[_btn("✅ 我已加入 重试参与", callback_data="retry1")]],
    )
    asyncio.run(engine._click_confirm("acc", client, "DbLotteryBot", msg, "r1"))
    assert client.started == [("DbLotteryBot", "r1")]
    assert any("改用重发 start" in line for line in logs)


class _CallbackClient(FakeClient):
    """带 callback 点击记录的客户端；bot 用户名的 get_chat 返回 BOT 类型。"""

    def __init__(self, bot_users=None):
        super().__init__(bot_users)
        self.callback_clicks = []

    async def get_chat(self, chat):
        if str(chat).lower() in {u.lower() for u in self.bot_users}:
            return SimpleNamespace(
                pinned_message=None, type=SimpleNamespace(name="BOT")
            )
        return self.chat_info

    async def request_callback_answer(self, chat_id, message_id, callback_data):
        self.callback_clicks.append((chat_id, message_id, callback_data))


def test_click_confirm_clicks_same_bot_url_button(tmp_path, monkeypatch):
    """bot 回复「您已获得抽奖资格」类消息带 URL 按钮「点击参与抽奖」，
    链接指向同一 bot 的 start 深度链接时，按链接参数重发 start 完成参与。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = _CallbackClient(bot_users={"BaibaoxiangCjBot"})
    msg = _msg(
        text="您已获得抽奖资格（已达标）✅您已完成所有任务，请直接参与抽奖！",
        buttons=[
            [_btn("🎟️ 点击参与抽奖", url="https://t.me/BaibaoxiangCjBot?start=q1")]
        ],
    )
    asyncio.run(engine._click_confirm("acc", client, "BaibaoxiangCjBot", msg, "r1"))
    assert client.started == [("BaibaoxiangCjBot", "q1")]
    assert any("参与按钮" in line and "点击参与抽奖" in line for line in logs)


def test_click_confirm_clicks_plain_same_bot_url_button(tmp_path, monkeypatch):
    """URL 按钮指向同一 bot 但无 start 参数：重发 start（空参数）。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = _CallbackClient(bot_users={"SomeGiveBot"})
    msg = _msg(
        text="请点击下方按钮参与抽奖",
        buttons=[[_btn("点击参与抽奖", url="https://t.me/SomeGiveBot")]],
    )
    asyncio.run(engine._click_confirm("acc", client, "SomeGiveBot", msg, "r1"))
    assert client.started == [("SomeGiveBot", "")]


def test_click_confirm_resends_duplicate_same_param_start(tmp_path, monkeypatch):
    """URL 按钮的 start 链接与本次已发 start 一致也照样再发一次。

    百宝箱等 bot 要求「加入频道后重新参与」，加入生效后正是这次 start
    才真正报名成功；旧版跳过会导致显示已参与、实际没进抽奖。
    """
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = _CallbackClient(bot_users={"SameLinkBot"})
    msg = _msg(
        text="请点击下方按钮参与抽奖",
        buttons=[[_btn("🎟️ 点击参与抽奖", url="https://t.me/SameLinkBot?start=p1")]],
    )
    asyncio.run(engine._click_confirm("acc", client, "SameLinkBot", msg, "p1"))
    assert client.started == [("SameLinkBot", "p1")]
    assert not any("跳过重复 start" in line for line in logs)


def test_click_confirm_join_paused_reports_incomplete(tmp_path, monkeypatch):
    """账号加入被 FLOOD_WAIT 暂停期间，bot 要求加入频道才能参与：

    旧版仍会重发 start，随后因按钮参数相同「跳过重复 start」，却照样记成
    已参与（真实日志中的欢欢/小琳琳假成功）。现在应直接判定跟进未完成。
    """
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = _CallbackClient(bot_users={"BaibaoxiangCjBot"})
    engine._suspend_joins("acc", 259, "xbd5252a")
    msg = _msg(
        text="请先加入 https://t.me/NeedJoinCh 后重新参与",
        buttons=[
            [_btn("🎟️ 点击参与抽奖", url="https://t.me/BaibaoxiangCjBot?start=p1")]
        ],
    )
    completed = asyncio.run(
        engine._click_confirm("acc", client, "BaibaoxiangCjBot", msg, "p1")
    )
    assert completed is False
    assert client.started == []
    assert client.callback_clicks == []
    assert client.joined_targets == []
    assert any("自动加入已暂停" in line for line in logs)


class _JoinRequiredReplyClient(_CallbackClient):
    """start 后回复「请先加入频道」，并带同参数参与按钮。"""

    async def start_bot(self, username, param):
        self.started.append((username, param))
        return _msg(
            text="请先加入 https://t.me/NeedJoinCh 后重新参与",
            buttons=[
                [_btn("🎟️ 点击参与抽奖", url=f"https://t.me/{username}?start={param}")]
            ],
        )


def test_participate_join_paused_not_recorded_as_participated(tmp_path, monkeypatch):
    """加入暂停期间的参与不能记为成功，否则后续扫描直接跳过、用户永远漏参与。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, state = _engine(tmp_path)
    logs: list = []
    events: list = []
    engine.log = logs.append
    engine.on_event = events.append
    engine.max_joins = 5
    client = _JoinRequiredReplyClient(bot_users={"BaibaoxiangCjBot"})
    engine._suspend_joins("acc", 259, "xbd5252a")

    asyncio.run(
        engine._participate("acc", client, "BaibaoxiangCjBot", "hh1", "导航频道")
    )

    # 未记成功：状态库无记录、名额已归还、计入失败
    assert not state.has("acc", "BaibaoxiangCjBot", "hh1")
    assert engine.joined == 0
    assert engine.failed == 1
    assert any("[未参与]" in line for line in logs)
    assert not any("[已参与]" in line for line in logs)
    assert client.started == [("BaibaoxiangCjBot", "hh1")]  # 只发了原始 start
    assert events and events[-1]["ok"] is False


class _StillNeedsJoinClient(_CallbackClient):
    """加入成功，但重发 start 后 bot 仍回复「请先加入频道」。"""

    async def start_bot(self, username, param):
        self.started.append((username, param))
        msg = _msg(
            text="请先加入 https://t.me/NeedJoinCh 后重新参与",
            buttons=[
                [_btn("🎟️ 点击参与抽奖", url=f"https://t.me/{username}?start={param}")]
            ],
        )
        self.history = [msg]
        return msg


def test_participate_bot_still_requires_join_not_recorded(tmp_path, monkeypatch):
    """已加入、但点击后 bot 仍要求先加入频道：本次未生效，不能记「已参与」。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, state = _engine(tmp_path)
    logs: list = []
    events: list = []
    engine.log = logs.append
    engine.on_event = events.append
    engine.max_joins = 5
    client = _StillNeedsJoinClient(bot_users={"BaibaoxiangCjBot"})

    asyncio.run(
        engine._participate("acc", client, "BaibaoxiangCjBot", "hh2", "导航频道")
    )

    assert client.joined_targets == ["NeedJoinCh"]
    assert not state.has("acc", "BaibaoxiangCjBot", "hh2")
    assert engine.joined == 0
    assert any("[未参与]" in line for line in logs)
    assert not any("[已参与]" in line for line in logs)
    assert events and events[-1]["ok"] is False


def test_click_confirm_flood_records_strike_and_blocks_bot(tmp_path, monkeypatch):
    """确认按钮里的 StartBot 触发长限流时，同样登记熔断（旧版此处直接吞异常）。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append

    class _FloodConfirmClient(_CallbackClient):
        async def start_bot(self, username, param):
            self.started.append((username, param))
            exc = RuntimeError(
                "Telegram says: [420 FLOOD_WAIT_X] - Please wait 1717 seconds"
            )
            exc.value = 1717
            raise exc

    client = _FloodConfirmClient(bot_users={"FloodConfirmBot"})
    msg = _msg(
        text="请点击下方按钮参与抽奖",
        buttons=[[_btn("🎟️ 点击参与抽奖", url="https://t.me/FloodConfirmBot?start=q1")]],
    )
    asyncio.run(engine._click_confirm("acc", client, "FloodConfirmBot", msg, "p1"))
    assert "acc|floodconfirmbot" in engine._flood_bots
    assert engine._flood_paused("acc", "FloodConfirmBot")
    assert any("点击确认按钮" in line and "限流" in line for line in logs)


def test_click_confirm_ignores_url_button_other_target(tmp_path, monkeypatch):
    """URL 按钮指向其他频道/bot：不点击，也不重发 start。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = _CallbackClient(bot_users={"SomeGiveBot"})
    msg = _msg(
        text="抽奖活动",
        buttons=[[_btn("点击参与抽奖", url="https://t.me/OtherChannel?start=x")]],
    )
    asyncio.run(engine._click_confirm("acc", client, "SomeGiveBot", msg, "r1"))
    assert client.started == []
    assert client.callback_clicks == []
    assert not any("参与按钮" in line for line in logs)


def test_click_confirm_still_clicks_callback_button(tmp_path, monkeypatch):
    """callback 按钮行为不变：request_callback_answer 点击。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = _CallbackClient(bot_users={"ConfirmBot"})
    msg = _msg(
        text="请确认参与",
        buttons=[[_btn("✅ 确认参与抽奖", callback_data="confirm_1")]],
    )
    asyncio.run(engine._click_confirm("acc", client, "ConfirmBot", msg, "r1"))
    assert client.callback_clicks == [("ConfirmBot", msg.id, "confirm_1")]
    assert client.started == []
    assert any("确认按钮" in line for line in logs)


def test_click_confirm_callback_timeout_returns_incomplete(tmp_path, monkeypatch):
    """Bug 1 回归：request_callback_answer 超时（非 FLOOD_WAIT）时应返回
    completed=False，让走 _handle_join_blocked 归还名额、不写参与记录。
    之前错误地返回 completed=True，导致超时被记为「已参与」假成功。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)

    class _TimeoutCallbackClient(_CallbackClient):
        """request_callback_answer 始终抛网络超时（_TimeoutClient 层
        就是把 asyncio.TimeoutError 包成 RuntimeError）。"""

        async def request_callback_answer(self, chat_id, message_id, callback_data):
            raise RuntimeError(
                "网络请求 request_callback_answer 超时（30 秒，网络异常）"
            )

    engine, state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = _TimeoutCallbackClient(bot_users={"ConfirmBot"})
    msg = _msg(
        text="请确认参与",
        buttons=[[_btn("✅ 确认参与抽奖", callback_data="confirm_1")]],
    )
    # _click_confirm 直接调用：验证返回值语义
    completed = asyncio.run(
        engine._click_confirm("acc", client, "ConfirmBot", msg, "r1")
    )
    assert completed is False, (
        "callback 超时应返回 False 让上层走 _handle_join_blocked，"
        "不能返回 True 被误记为已参与"
    )
    # 日志应有失败记录
    assert any("确认按钮失败" in line for line in logs)
    # 不应写参与记录（completed=False 让入口走 _handle_join_blocked）
    assert not state.has("acc", "ConfirmBot", "r1")


def test_click_confirm_multi_round_join_restart_converges(tmp_path, monkeypatch):
    """Bug 2 回归：@lycj_bot 风格 bot 每轮 start 只露出一个前置频道，
    单次 _click_confirm 应在内部循环「加入→重发 start→再加→重发」直到
    bot 不再有加入要求，而不是靠多轮任务扫描才能加完所有频道。

    通过 patch _auto_join_and_restart 让它依次返回 3 轮收敛路径：
    第 1 轮 did_join=True（加 A），第 2 轮 did_join=True（加 B），
    第 3 轮 did_join=False（无新频道，退出循环）。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append

    calls: list = []

    async def _patched(
        self, account, client, username, param, source_message, candidates
    ):
        calls.append(1)
        # 第 1 轮：有加入目标，did_join=True
        # 第 2 轮：仍有加入目标，did_join=True
        # 第 3 轮：无新加入目标，did_join=False → while 应 break
        did_join = len(calls) <= 2
        had_join_targets = len(calls) <= 2
        return False, did_join, had_join_targets, candidates, False

    monkeypatch.setattr(gc.GiveawayEngine, "_auto_join_and_restart", _patched)

    # start_msg 随便传什么，patch 后不再关心候选里的内容
    client = _CallbackClient(bot_users={"LyCJBot"})
    msg = _msg(text="bot 初始回复")
    completed = asyncio.run(engine._click_confirm("acc", client, "LyCJBot", msg, "r1"))
    # 断言 while 循环至少跑了 3 轮（前 2 轮 did_join=True，第 3 轮 did_join=False break）
    assert len(calls) >= 3, (
        f"多轮 join→restart 应至少迭代 3 次才收敛，实际只调了 {len(calls)} 次"
    )
    # _auto_join_and_restart 都没返回 stop/join_blocked，流程应完成
    assert completed is True


def test_click_confirm_rejoin_button_after_join_restart(tmp_path, monkeypatch):
    """百宝箱式流程：bot 回复「必须先关注以下群组/频道…全部关注后请点击
    重新参与抽奖」，链接在警示文本之后。加入频道并重发 start 后 bot 未再
    回复带按钮的新消息时，原消息上的「重新参与抽奖」按钮仍要点到。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = _CallbackClient(bot_users={"BaibaoxiangCjBot"})
    msg = _msg(
        text=(
            "⚠️必须先关注以下群组/频道才能参加抽奖\n\n"
            "⚠️全部关注后请点击 重新参与抽奖\n\n"
            "📢 https://t.me/BBXNews\n📢 @BBXFun"
        ),
        buttons=[[_btn("🔄 重新参与抽奖", callback_data="rejoin")]],
    )
    asyncio.run(engine._click_confirm("acc", client, "BaibaoxiangCjBot", msg, "r1"))
    # 链接位于警示文本之后，仍全部提取并加入
    assert client.joined_targets == ["BBXNews", "BBXFun"]
    # 加入完成后重发 start 完成重新参与
    assert client.started == [("BaibaoxiangCjBot", "r1")]
    # 重发后 bot 无新回复：原消息上的「重新参与抽奖」按钮被点击
    assert client.callback_clicks == [("BaibaoxiangCjBot", msg.id, "rejoin")]
    assert any("要求加入频道后重新参与" in line for line in logs)


def test_click_confirm_popup_ended_skips_verify(tmp_path, monkeypatch):
    """@choujiangs_bot 类 bot：抽奖结束后点确认按钮只弹窗提示
    （callback answer 携带文案），不发新消息。弹窗文案命中终态时
    立即返回，不进入回复核对（避免误读旧消息）。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append

    class _PopupClient(_CallbackClient):
        async def request_callback_answer(self, chat_id, message_id, callback_data):
            self.callback_clicks.append((chat_id, message_id, callback_data))
            return SimpleNamespace(text="本期抽奖已开奖")

    client = _PopupClient(bot_users={"ChoujiangsBot"})
    msg = _msg(
        text="请点击按钮参与抽奖",
        buttons=[[_btn("立刻参加抽奖", callback_data="join_1")]],
    )
    verify_calls: list = []

    async def fake_verify(*a, **k):
        verify_calls.append(1)

    monkeypatch.setattr(engine, "_verify_after_click", fake_verify)
    asyncio.run(engine._click_confirm("acc", client, "ChoujiangsBot", msg, "r1"))
    assert client.callback_clicks == [("ChoujiangsBot", msg.id, "join_1")]
    assert any("弹窗提示" in line and "已结束" in line for line in logs)
    assert verify_calls == []


def test_participate_ended_reply_marks_state_and_refunds_quota(tmp_path):
    """@Lottery8Bot 类 bot：start 后直接回复 "The event has ended or does not
    exist" ＝终态：不记已参与、归还参与名额，状态标记 ended 防止重试
    （含 ignore_history 开启时）。"""
    import asyncio

    engine, state = _engine(tmp_path)
    engine.max_joins = 5
    logs: list = []
    engine.log = logs.append

    class EndedClient(FakeClient):
        async def start_bot(self, username, param):
            self.started.append((username, param))
            return _msg(text="The event has ended or does not exist")

    client = EndedClient(bot_users={"Lottery8Bot"})
    message = _msg(text="抽奖 https://t.me/Lottery8Bot?start=dead1")
    asyncio.run(_run_process(engine, client, "acc", message))

    assert client.started == [("Lottery8Bot", "dead1")]
    assert engine.joined == 0  # 名额已归还
    assert not any("[已参与]" in line for line in logs)
    assert any("[已结束]" in line and "Lottery8Bot" in line for line in logs)
    assert state.is_ended("acc", "Lottery8Bot", "dead1")

    # 同一链接即使开启忽略历史也不再重发 start
    engine.ignore_history = True
    asyncio.run(_run_process(engine, client, "acc", message))
    assert client.started == [("Lottery8Bot", "dead1")]


def test_ended_reply_breaks_bot_after_three_strikes(tmp_path, monkeypatch):
    """同一 bot 累计 3 次回复「已结束」：本轮熔断，后续新链接静默跳过。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append

    class EndedClient(FakeClient):
        async def start_bot(self, username, param):
            self.started.append((username, param))
            return _msg(text="The event has ended or does not exist")

    client = EndedClient(bot_users={"Lottery8Bot"})
    for i in range(1, 5):
        message = _msg(text=f"抽奖 https://t.me/Lottery8Bot?start=e{i}")
        asyncio.run(_run_process(engine, client, "acc", message))

    # 第 4 个链接被熔断跳过，不再发 start
    assert client.started == [
        ("Lottery8Bot", "e1"),
        ("Lottery8Bot", "e2"),
        ("Lottery8Bot", "e3"),
    ]
    assert any("本轮运行跳过该 bot" in line for line in logs)


def test_click_confirm_poll_ended_checks_newest_only(tmp_path, monkeypatch):
    """confirm_wait 轮询期间仅核对最新一条消息：会话中历史遗留的旧
    「已结束」回复不误杀本次参与；最新回复命中「已结束」时终止并计数。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    engine.confirm_wait = 1

    stale_ended = _msg(text="The event has ended or does not exist", id=1)
    normal = _msg(
        text="请点击按钮参与抽奖",
        buttons=[[_btn("立刻参加抽奖", callback_data="join_1")]],
        id=2,
    )

    # 旧「已结束」回复在历史里（非最新）：不终止，正常点击按钮
    client = _CallbackClient(bot_users={"Lottery8Bot"})
    client.history = [normal, stale_ended]
    logs: list = []
    engine.log = logs.append
    asyncio.run(
        engine._click_confirm(
            "acc", client, "Lottery8Bot", _msg(text="/start", id=3), "r1"
        )
    )
    assert client.callback_clicks == [("Lottery8Bot", normal.id, "join_1")]
    assert not any("回复抽奖已结束" in line for line in logs)
    assert engine._ended_counts == {}

    # 最新回复即「已结束」：终止跟进并累计熔断计数
    client2 = _CallbackClient(bot_users={"Lottery8Bot"})
    client2.history = [stale_ended]
    logs2: list = []
    engine.log = logs2.append
    asyncio.run(
        engine._click_confirm(
            "acc", client2, "Lottery8Bot", _msg(text="/start", id=3), "r1"
        )
    )
    assert client2.callback_clicks == []
    assert any("回复抽奖已结束" in line for line in logs2)
    assert engine._ended_counts == {"acc|lottery8bot": 1}


def test_engine_join_invite_request_sent_is_not_failure(tmp_path):
    """私有群审批制：INVITE_REQUEST_SENT 视为申请已提交，不算失败。"""
    import asyncio

    class RequestClient:
        def __init__(self):
            self.calls = []

        async def join_chat(self, target):
            self.calls.append(target)
            raise RuntimeError(
                "Telegram says: [400 INVITE_REQUEST_SENT] - You have "
                "successfully requested to join this chat or channel. "
                '(caused by "channels.JoinChannel")'
            )

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = RequestClient()
    msg = _msg(text="加入 https://t.me/+AbCdEfGh123 参与")
    joined_any, blocked = asyncio.run(
        engine._join_from_message("acc", client, "LotteryBot", msg)
    )
    assert joined_any is True
    assert blocked is False
    assert any("已提交加入" in line and "待管理员审批" in line for line in logs)
    assert not any("失败" in line for line in logs)


def test_ensure_monitor_membership_emits_join_events(tmp_path):
    """监控对象成员检查：加入失败发 join_failed 事件，已加入/加入成功发
    join_resolved 事件（WebUI 据此通知+持久化/消除提醒）。"""
    import asyncio

    events: list = []
    engine, _state = _engine(tmp_path)
    engine.on_event = events.append
    logs: list = []
    engine.log = logs.append

    # 已是成员 → join_resolved，无 join_chat 调用
    client = JoinClient()
    client.member_targets = {"chat1"}
    asyncio.run(engine._ensure_monitor_membership("acc", client, "Chat1"))
    assert client.joined == []
    assert events == [{"type": "join_resolved", "account": "acc", "chat": "Chat1"}]

    # 未加入且加入失败 → join_failed（带错误原因）
    events.clear()
    client2 = JoinClient()

    async def _boom(target):
        raise RuntimeError("PEER_ID_INVALID")

    client2.join_chat = _boom
    asyncio.run(engine._ensure_monitor_membership("acc", client2, "Chat2"))
    assert len(events) == 1
    assert events[0]["type"] == "join_failed"
    assert events[0]["account"] == "acc"
    assert events[0]["chat"] == "Chat2"
    assert "PEER_ID_INVALID" in events[0]["error"]
    assert any("自动加入监控对象 Chat2 失败" in line for line in logs)

    # 加入成功 → join_resolved
    events.clear()
    client3 = JoinClient()
    asyncio.run(engine._ensure_monitor_membership("acc", client3, "Chat3"))
    assert client3.joined == ["@Chat3", "Chat3"][:1] or client3.joined
    assert events and events[0]["type"] == "join_resolved"


def test_ensure_monitor_membership_network_timeout_not_join_failed(tmp_path):
    """监控对象加入遇网络超时：不发 join_failed、不占 _join_cache、允许下次重试。

    回归场景：网络请求 join_chat 超时（30 秒，网络异常）不应算作"加入失败"，
    因为账号/频道本身没有问题，只是瞬时网络故障。
    """
    import asyncio

    events: list = []
    engine, _state = _engine(tmp_path)
    engine.on_event = events.append
    logs: list = []
    engine.log = logs.append

    # join_chat 模拟网络超时
    client = JoinClient()

    async def _timeout(target):
        raise RuntimeError("网络请求 join_chat 超时（30 秒，网络异常）")

    client.join_chat = _timeout

    # 第一次尝试：不应发 join_failed，key 不应进 _join_cache
    asyncio.run(engine._ensure_monitor_membership("acc", client, "ChatX"))
    assert events == [], f"网络超时不应发任何事件，实际: {events}"
    key = "acc|monitor|chatx"
    assert key not in engine._join_cache, "网络超时不应消耗 _join_cache 尝试机会"

    # 第二次尝试（模拟下次引擎运行）：因为 key 不在 _join_cache，应该再次调用
    client2 = JoinClient()
    client2.join_chat = _timeout
    calls_before = len(client2.joined)
    asyncio.run(engine._ensure_monitor_membership("acc", client2, "ChatX"))
    assert client2.joined[calls_before:] == []  # 仍超时
    assert events == []


def test_join_single_target_network_timeout_does_not_block_retry(tmp_path):
    """消息链接加入遇网络超时：调用方 discard key，下次扫描可重试。"""
    import asyncio

    class TimeoutClient:
        def __init__(self):
            self.calls = []

        async def get_chat(self, target):
            return SimpleNamespace(type=SimpleNamespace(name="CHANNEL"))

        async def get_chat_member(self, target, user_id):
            raise RuntimeError("USER_NOT_PARTICIPANT")

        async def join_chat(self, target):
            self.calls.append(target)
            raise RuntimeError("网络请求 join_chat 超时（30 秒，网络异常）")

    engine, _state = _engine(tmp_path)
    client = TimeoutClient()
    msg = _msg(text="加入 @ChanA 和 @ChanB 领奖")

    joined_any, blocked = asyncio.run(
        engine._join_from_message("acc", client, "LotteryBot", msg)
    )
    assert joined_any is False
    assert blocked is True  # 网络超时被当作 blocked 处理
    # key 应从 _join_cache 中被 discard
    assert "acc|chana" not in engine._join_cache
    assert "acc|chanb" not in engine._join_cache


def test_engine_join_flood_wait_suspends(tmp_path):
    """JoinChannel 触发 FLOOD_WAIT 后暂停自动加入，恢复前跳过后续加入。"""
    import asyncio

    class FloodClient:
        def __init__(self):
            self.calls = []

        async def get_chat(self, target):
            return SimpleNamespace(type=SimpleNamespace(name="CHANNEL"))

        async def join_chat(self, target):
            self.calls.append(target)
            if len(self.calls) == 1:
                raise RuntimeError(
                    "Telegram says: [420 FLOOD_WAIT_X] - Please wait 163 "
                    "seconds before repeating the action. (caused by "
                    '"channels.JoinChannel")'
                )
            return SimpleNamespace(title=f"chat-{target}")

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FloodClient()
    msg = _msg(text="加入 @ChanA 和 @ChanB 领奖")
    asyncio.run(engine._join_from_message("acc", client, "LotteryBot", msg))
    assert client.calls == ["ChanA"]
    assert any("触发限流" in line and "163" in line for line in logs)
    # 暂停期间：后续消息的所有加入被跳过（只提示一次）
    msg2 = _msg(text="加入 @ChanC 和 @ChanD")
    joined_any, paused_blocked = asyncio.run(
        engine._join_from_message("acc", client, "LotteryBot", msg2)
    )
    assert client.calls == ["ChanA"]
    assert joined_any is False
    assert paused_blocked is True
    assert any("自动加入被限流暂停中" in line for line in logs)
    # 暂停恢复后正常加入
    engine._join_suspended.clear()
    asyncio.run(engine._join_from_message("acc", client, "LotteryBot", msg2))
    assert client.calls == ["ChanA", "ChanC", "ChanD"]


# ---------------- 历史补漏（rescue） ----------------


def _msgid(message, mid):
    message.id = mid
    return message


def test_rescue_round_dedups_seen_and_joins_new(tmp_path, monkeypatch):
    """补漏只处理水位线之外的新消息，重复轮次不重复参与。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)

    engine, _state = _engine(tmp_path)
    engine.chats = [123]
    client = FakeClient(bot_users={"LotteryBot"})
    engine._clients = {"acc": client}
    logs: list = []
    engine.log = logs.append

    seen_msg = _msgid(_msg(text="抽奖 https://t.me/LotteryBot?start=old1"), 1)
    client.history = [seen_msg]
    asyncio.run(engine.scan_now())
    assert client.started == [("LotteryBot", "old1")]

    # 新帖到达，旧帖仍在最近列表里：补漏只参与新帖
    new_msg = _msgid(_msg(text="抽奖 https://t.me/LotteryBot?start=new1"), 2)
    client.history = [new_msg, seen_msg]
    asyncio.run(engine._rescue_round())
    assert client.started == [("LotteryBot", "old1"), ("LotteryBot", "new1")]

    # 再补漏一轮：无新消息，不重复参与
    asyncio.run(engine._rescue_round())
    assert client.started == [("LotteryBot", "old1"), ("LotteryBot", "new1")]
    assert any("补漏轮次完成" in line for line in logs)


def test_rescue_respects_limit(tmp_path):
    """补漏只重拉最近 rescue_limit 条消息。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    engine.chats = [123]
    engine.rescue_limit = 2
    client = FakeClient(bot_users={"LotteryBot"})
    engine._clients = {"acc": client}
    # 历史消息最新在前：limit=2 只拉前 2 条，最旧一条（r2）在补漏窗口之外
    client.history = [
        _msgid(_msg(text="无关消息"), 1),
        _msgid(_msg(text="抽奖 https://t.me/LotteryBot?start=r1"), 2),
        _msgid(_msg(text="抽奖 https://t.me/LotteryBot?start=r2"), 3),
    ]
    asyncio.run(engine._rescue_round())
    assert client.started == [("LotteryBot", "r1")]


class FailingHistoryClient(FakeClient):
    """get_chat_history 始终抛异常的假客户端（模拟坏网络/RPC 故障）"""

    def get_chat_history(self, chat, limit=None):
        return self._boom()

    async def _boom(self):
        raise RuntimeError("RPC timeout")
        yield  # pragma: no cover  使其成为 async generator


def test_rescue_suspends_after_consecutive_failures(tmp_path):
    """连续 RESCUE_MAX_FAILS 轮失败后熔断暂停补漏。"""
    import asyncio
    from datetime import datetime

    engine, _state = _engine(tmp_path)
    engine.chats = [123]
    client = FailingHistoryClient()
    engine._clients = {"acc": client}
    logs: list = []
    engine.log = logs.append
    for _ in range(3):
        asyncio.run(engine._rescue_round())
    assert engine._rescue_fail_streak == 3
    assert engine._rescue_suspended_until is not None
    assert engine._rescue_suspended_until > datetime.now()
    assert any("暂停" in line and "自动恢复" in line for line in logs)

    # 成功后连续失败计数清零
    engine2, _s2 = _engine(tmp_path)
    engine2.chats = [123]
    ok_client = FakeClient()
    engine2._clients = {"acc": ok_client}
    asyncio.run(engine2._rescue_round())
    assert engine2._rescue_fail_streak == 0
    assert engine2._rescue_suspended_until is None


def test_rescue_tick_skips_while_suspended_then_recovers(tmp_path):
    """熔断暂停期间单轮决策不扫描，到期后恢复并清零失败计数。"""
    import asyncio
    from datetime import datetime, timedelta
    from unittest.mock import AsyncMock

    engine, _state = _engine(tmp_path)
    engine.chats = [123]
    engine._clients = {"acc": FakeClient()}
    engine._rescue_round = AsyncMock()
    engine._rescue_suspended_until = datetime.now() + timedelta(seconds=60)

    asyncio.run(engine._rescue_tick())
    engine._rescue_round.assert_not_awaited()  # 暂停期间不扫描

    engine._rescue_fail_streak = 3
    engine._rescue_suspended_until = datetime.now() - timedelta(seconds=1)
    asyncio.run(engine._rescue_tick())
    engine._rescue_round.assert_awaited_once()  # 到期后正常补漏
    assert engine._rescue_suspended_until is None
    assert engine._rescue_fail_streak == 0


def test_rescue_tick_skips_while_scanning(tmp_path):
    """完整/手动扫描进行中时补漏让路，不重复拉取。"""
    import asyncio
    from unittest.mock import AsyncMock

    engine, _state = _engine(tmp_path)
    engine._clients = {"acc": FakeClient()}
    engine.scanning = True
    engine._rescue_round = AsyncMock()

    asyncio.run(engine._rescue_tick())
    engine._rescue_round.assert_not_awaited()


# ---------------- LotteryBestBot 回复「加入-名称 ❌」链接解析 ----------------

STATUS_REPLY = """您需加入指定的群组/频道

🎰 某测试频道C
🎫 加入-某测试频道D ❌
🎫 加入-某测试频道E（免费上榜） ✅
🎫 加入-某测试频道E报告榜 ❌"""


def _u16_len(s: str) -> int:
    """字符串的 UTF-16 code unit 长度（astral emoji 占 2）。"""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _status_message(text, name_urls):
    """按 name->url 构造带 TEXT_URL 实体的消息，实体覆盖名称本身。

    offset 按 UTF-16 code units 计，与真实 Telegram 协议一致。
    """
    entities = []
    for name, url in name_urls.items():
        prefix = text[: text.index(name)]
        entities.append(
            SimpleNamespace(
                type="TEXT_URL",
                url=url,
                offset=_u16_len(prefix),
                length=_u16_len(name),
            )
        )
    return _msg(text=text, entities=entities)


def test_extract_join_status_targets():
    from tg_signer.giveaway_parsing import extract_join_status_targets

    msg = _status_message(
        STATUS_REPLY,
        {
            "某测试频道D": "https://t.me/test_channel_d",
            "某测试频道E（免费上榜）": "https://t.me/test_channel_e",
            "某测试频道E报告榜": "https://t.me/+abc12345678",
        },
    )
    # 只提取 ❌（未加入）行的链接，✅ 行跳过；私有邀请链接保留
    assert extract_join_status_targets(msg) == [
        "test_channel_d",
        "https://t.me/+abc12345678",
    ]
    # 无实体 / 无❌标记 / 普通消息均返回空
    assert extract_join_status_targets(_msg(text=STATUS_REPLY)) == []
    assert (
        extract_join_status_targets(
            _status_message("🎫 加入-某频道", {"某频道": "https://t.me/SomeChan"})
        )
        == []
    )
    assert extract_join_status_targets(_msg(text="开奖日期：2026年10月20日")) == []


def test_extract_join_status_targets_utf16_and_edge_urls():
    """真实场景回归：astral emoji 造成 UTF-16 偏移漂移、尾斜杠链接、
    tg://resolve 形态链接、帖子路径链接仍被排除。"""
    from tg_signer.giveaway_parsing import extract_join_status_targets

    text = (
        "您需加入指定的群组/频道\n"
        "\n"
        "🎰 10月福利第二弹！【出击大药房】 ❌【BBW群】\n"
        "🎫 加入-出击大药房 ❌\n"
        "🎫 加入-BBW成都群🧸[大胸大屁股万岁] ❌\n"
        "🎫 加入-印度药|伟哥|增硬延时💊 ❌"
    )
    msg = _status_message(
        text,
        {
            "加入-出击大药房": "https://t.me/chujiya/",
            "BBW成都群🧸[大胸大屁股万岁]": "tg://resolve?domain=bbw_cd_group",
            "印度药|伟哥|增硬延时💊": "https://t.me/yindianyao",
        },
    )
    assert extract_join_status_targets(msg) == [
        "chujiya",
        "bbw_cd_group",
        "yindianyao",
    ]
    # 帖子路径链接（t.me/x/123）仍不作为加入目标
    msg2 = _status_message(
        "🎫 加入-某测试频道F ❌",
        {"某测试频道F": "https://t.me/some_chan/123"},
    )
    assert extract_join_status_targets(msg2) == []


def test_extract_join_status_targets_caption_entities():
    """带图帖子的清单实体在 caption_entities 里，同样要能提取。"""
    from types import SimpleNamespace

    from tg_signer.giveaway_parsing import extract_join_status_targets

    text = "🎫 加入-某测试频道G ❌"
    msg = SimpleNamespace(
        text=None,
        caption=text,
        entities=[],
        caption_entities=[
            SimpleNamespace(
                type="TEXT_URL",
                url="https://t.me/some_channel_g",
                offset=5,
                length=6,
            )
        ],
    )
    assert extract_join_status_targets(msg) == ["some_channel_g"]


def test_extract_join_status_targets_pyrogram_text_link():
    """Pyrogram 的隐藏链接实体叫 TEXT_LINK（非 Telethon 的 TEXT_URL），
    之前被类型过滤整体丢弃，导致 ❌ 清单永远解析不到链接。"""
    from types import SimpleNamespace

    from tg_signer.giveaway_parsing import extract_join_status_targets

    msg = SimpleNamespace(
        text="🎫 加入-某测试频道H ❌",
        caption=None,
        entities=[
            SimpleNamespace(
                type="TEXT_LINK",
                url="https://t.me/some_channel_h",
                offset=5,
                length=6,
            )
        ],
        caption_entities=[],
    )
    assert extract_join_status_targets(msg) == ["some_channel_h"]


SPEECH_TASK_REPLY = (
    "您已成功报名抽奖（未达标β）\n"
    "\n"
    "您还需完成激活任务才能抽奖！\n"
    "\n"
    "🎰 某老师 半价票\n"
    "🎫 已加入-某修车补习班免费上榜\n"
    "└  🎟️ 发言数大于 30 条 ❌\n"
    "\n"
    "💁 参与情况：0达标 / 10报名\n"
    "\n"
    "📅 开奖日期：(北京时间)\n"
    "2026年10月19日 14时48分00秒"
)


def test_extract_join_status_targets_speech_task_rows():
    """发言数 ❌ 子行不是未加入频道行：已加入行 + 发言任务行的清单
    整体提取不到任何加入目标。"""
    from tg_signer.giveaway_parsing import extract_join_status_targets

    msg = _status_message(
        SPEECH_TASK_REPLY,
        {"已加入-某修车补习班免费上榜": "https://t.me/some_group"},
    )
    # 已加入行（无 ❌）与发言任务子行（无「加入」）均不算未加入频道行
    assert extract_join_status_targets(msg) == []


def test_reply_says_signup_success():
    """「已成功报名抽奖（未达标β）」「已获得抽奖资格」＝报名成功（已参与，
    未达标仅剩发言数这类激活任务，系统标记为完成）。"""
    from tg_signer.giveaway_parsing import reply_says_signup_success

    assert reply_says_signup_success(_msg(text=SPEECH_TASK_REPLY))
    assert reply_says_signup_success(
        _msg(text="您已获得抽奖资格（已达标） 请耐心等待，中奖通知机器人会私发给您！")
    )
    # 未报名 / 需先加入类回复不算成功
    assert not reply_says_signup_success(_msg(text="您未获得抽奖资格，请先加入频道"))
    assert not reply_says_signup_success(_msg(text="您需加入指定的群组/频道"))
    assert not reply_says_signup_success(_msg(text=STATUS_REPLY))


def test_reply_says_ended():
    """bot 回复「已结束/已经结束」类文案＝终态（如 @Lottery8Bot 的
    「该活动已经结束或不存在」与英文 "The event has ended or does not exist"）。"""
    from tg_signer.giveaway_parsing import reply_says_ended

    # 中文变体（含「已经结束」，2026-10-19 修复前仅匹配「已结束」漏掉该文案）
    assert reply_says_ended(_msg(text="该活动已经结束或不存在"))
    assert reply_says_ended(_msg(text="该活动已结束或不存在"))
    assert reply_says_ended(_msg(text="抽奖活动已结束"))
    assert reply_says_ended(_msg(text="抽奖已结束，请关注下期"))
    assert reply_says_ended(_msg(text="活动已过期"))
    # 英文变体
    assert reply_says_ended(_msg(text="The event has ended or does not exist"))
    # 正常回复不算已结束
    assert not reply_says_ended(_msg(text="您已成功报名抽奖（已达标）"))
    assert not reply_says_ended(_msg(text="您需加入指定的群组/频道"))


def test_engine_no_false_diagnostic_for_speech_task_rows(tmp_path):
    """清单仅含「已加入行 + 发言数 ❌ 子行」时不报「未解析到链接」诊断；
    存在真正的「加入-名称 ❌」行却解析不到链接时诊断仍输出。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = JoinClient()
    client.member_targets.add("some_group")  # 已加入行链接按已加入处理
    msg = _status_message(
        SPEECH_TASK_REPLY,
        {"已加入-某修车补习班免费上榜": "https://t.me/some_group"},
    )
    asyncio.run(engine._join_from_message("acc", client, "LotteryBestBot", msg))
    assert not any("清单含未加入" in line for line in logs)
    # 真正的未加入行（加入+❌）链接为帖子路径无法加入 → 诊断仍然输出
    bad = _status_message(
        "您需加入指定的群组/频道\n🎫 加入-某测试频道D ❌",
        {"某测试频道D": "https://t.me/some_chan/123"},
    )
    asyncio.run(engine._join_from_message("acc", client, "LotteryBestBot", bad))
    assert any("清单含未加入" in line for line in logs)


def test_engine_joins_only_unjoined_status_targets(tmp_path):
    """LotteryBestBot 回复：只加入 ❌ 行目标；其他 bot 不解析该格式。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = JoinClient()
    msg = _status_message(
        STATUS_REPLY,
        {
            "某测试频道D": "https://t.me/test_channel_d",
            "某测试频道E报告榜": "https://t.me/+abc12345678",
        },
    )
    asyncio.run(engine._join_from_message("acc", client, "LotteryBestBot", msg))
    assert client.joined == ["test_channel_d", "https://t.me/+abc12345678"]
    assert any("尚未加入 test_channel_d" in line for line in logs)
    # 非 LotteryBestBot 不解析❌/✅格式：纯名称行无链接实体，不应触发任何加入
    plain_msg = _msg(text="🎫 加入-某测试频道D ❌")
    asyncio.run(engine._join_from_message("acc", client, "OtherBot", plain_msg))
    assert client.joined == ["test_channel_d", "https://t.me/+abc12345678"]


def test_status_targets_trigger_restart(tmp_path, monkeypatch):
    """LotteryBestBot 回复只含 ❌ 清单时，加入后同样触发重发 start。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    client = FakeClient(bot_users={"LotteryBestBot"})
    msg = _status_message(STATUS_REPLY, {"某测试频道D": "https://t.me/test_channel_d"})
    asyncio.run(engine._click_confirm("acc", client, "LotteryBestBot", msg, "r1"))
    assert "test_channel_d" in client.joined_targets
    assert client.started == [("LotteryBestBot", "r1")]


def test_lotterybest_restart_reply_participate_button_clicked(tmp_path, monkeypatch):
    """端到端：LotteryBestBot ❌ 清单 → 加入 → 重发 start → 回复带
    「点击参与抽奖」URL 按钮（同 bot participate 链接）→ 应自动点击。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append

    class _RestartClient(_CallbackClient):
        async def start_bot(self, username, param):
            self.started.append((username, param))
            if param == "post1":
                # 重发 start 后 bot 回复达标消息 + 参与按钮
                return _msg(
                    text="您已获得抽奖资格，请点击按钮完成参与",
                    buttons=[
                        [
                            _btn(
                                "🎟️ 点击参与抽奖",
                                url=(
                                    "https://t.me/LotteryBestBot"
                                    "?start=participate_-100123_456_95"
                                ),
                            )
                        ]
                    ],
                )
            return None  # 参与按钮的 start 不再回复

    client = _RestartClient(bot_users={"LotteryBestBot"})
    checklist = _status_message(
        STATUS_REPLY, {"某测试频道D": "https://t.me/test_channel_d"}
    )
    asyncio.run(
        engine._click_confirm("acc", client, "LotteryBestBot", checklist, "post1")
    )
    assert "test_channel_d" in client.joined_targets
    # 第一次：原参数重发；第二次：点击参与按钮（链接参数）
    assert client.started == [
        ("LotteryBestBot", "post1"),
        ("LotteryBestBot", "participate_-100123_456_95"),
    ]
    assert any("参与按钮" in line for line in logs)


# ---------------- 口令要求 + 双向账号跳过 ----------------

PASSWORD_POST = """🎰 上海婷婷半价券

📮 参与条件：
🌏️ 归属地 上海,江苏
🪪 必须设置用户名
🎫 加入-婷宝大队💕
🎫 加入-野寻·上海学生交流群
🎫 加入-野寻·上海打卡通告群
└  🔑 发送口令﹝出击婷婷宝宝﹞

❗ 口令提示：
请先参与抽奖，再发送口令激活

🎁 奖品内容：
💰️ 婷婷半价券 × 1"""


def test_message_requires_password():
    from tg_signer.giveaway_parsing import message_requires_password

    assert message_requires_password(_msg(text=PASSWORD_POST))
    assert message_requires_password(_msg(text="请先参与抽奖，再发送口令激活"))
    # 普通帖不带口令要求
    assert not message_requires_password(
        _msg(text="🎫 加入-上海某测试频道B\n🎁 奖品：现金红包")
    )


def test_two_way_account_skips_password_post(tmp_path):
    """双向账号遇口令要求帖静默跳过；非双向账号正常参与。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    engine.two_way_accounts = {"双向号"}
    logs: list = []
    engine.log = logs.append
    msg = _msg(text=f"{PASSWORD_POST}\n\n抽奖 https://t.me/LotteryBot?start=pw1")

    result = asyncio.run(engine._process_message("双向号", None, msg))
    assert result is None
    assert not any("跳过" in line for line in logs)

    # 非双向账号不受影响，正常发 start
    logs.clear()
    client = FakeClient(bot_users={"LotteryBot"})
    engine._clients = {"普通号": client}
    asyncio.run(engine._process_message("普通号", client, msg))
    assert ("LotteryBot", "pw1") in client.started
    assert not any("口令要求" in line for line in logs)


def test_draw_announce_post_is_filtered(tmp_path):
    """开奖结果公告帖（含置顶路径）直接过滤：即使带 start 链接也不参与。"""
    import asyncio

    from tg_signer.giveaway_parsing import message_is_draw_announce

    announce = (
        "🎉🎉🎉 开奖通知 🎉🎉🎉\n\n"
        "🔖 防伪编号：H179229906522\n\n"
        "🎰 🎰 萝莉岛（@shlldq）  抽奖中奖联系 @shlldqbot\n\n"
        "⚠️ 抽奖说明：\n"
        "1️⃣本次抽奖由 上海萝莉岛赞助 \n"
        "2️⃣开奖后请24小时内联系 @shlldqbot\n\n"
        "💁 参与情况：1276达标 / 1349报名\n\n"
        "🎅 联系领奖：上海萝莉岛客服\n\n"
        "1. 富贵花开 奖品：口令🧧38\n"
        "2. 为何如 此霸气 奖品：口令🧧38\n\n"
        "祝大家心想事成好运连连！"
    )
    assert message_is_draw_announce(_msg(text=announce))

    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"LotteryBot"})
    # 公告帖尾部拼接 start 深度链接，模拟残留链接的最坏情况
    msg = _msg(text=announce + "\n\nhttps://t.me/LotteryBot?start=again", id=33)

    # 群置顶路径（绕过关键词过滤）：仍须过滤且不参与
    result = asyncio.run(
        engine._process_message("acc", client, msg, skip_keywords=True, log_skips=True)
    )
    assert result is None
    assert ("LotteryBot", "again") not in client.started
    assert any("开奖结果公告，跳过" in line for line in logs)

    # 历史扫描路径：静默过滤（不打跳过日志）
    logs.clear()
    result = asyncio.run(engine._process_message("acc", client, msg))
    assert result is None
    assert ("LotteryBot", "again") not in client.started
    assert not any("跳过" in line for line in logs)


# ---------------- 百宝箱机器人私聊任务类型 ----------------


BAIBAOXIANG_ELIGIBLE = """您已获得抽奖资格（已达标）

✅您已完成所有任务，请直接参与抽奖！

🎰 上海市民网 浦东 星星 优惠福利  @xing5517

🎫 已加入-星星的朋友圈 ✅
🎫 已加入-上海市民网 ✅
└ 🔑 发送口令﹝欢迎星星入驻上海市民网﹞ ✅

💁 参与情况：109达标 / 124报名"""


def _bot_engine(tmp_path):
    engine, state = _engine(
        tmp_path, keywords=("您已获得抽奖资格（已达标）",), delay_max=0.0
    )
    engine.source_type = "bot"
    engine.bot_username = "BaibaoxiangCjBot"
    return engine, state


def test_bot_source_participates_eligible_button_message(tmp_path):
    """机器人私聊来源：命中「已达标」关键词后按按钮 start 参数参与。"""
    import asyncio

    engine, state = _bot_engine(tmp_path)
    client = FakeClient(bot_users={"BaibaoxiangCjBot"})
    engine._clients = {"deux": client}
    param = "participate_-1003984379574_8480110324_137"
    msg = _msg(
        text=BAIBAOXIANG_ELIGIBLE,
        buttons=[
            [
                _btn(
                    "点击参与抽奖",
                    url=f"https://t.me/BaibaoxiangCjBot?start={param}",
                )
            ]
        ],
        id=11143,
    )

    result = asyncio.run(engine._process_message("deux", client, msg))

    assert result == "joined"
    assert client.started == [("BaibaoxiangCjBot", param)]
    assert state.has("deux", "baibaoxiangcjbot", param)


def test_bot_source_ignores_channel_post_conditions(tmp_path):
    """「已达标」消息虽含「发送口令」「归属地」字样，双向账号也照常参与。"""
    import asyncio

    engine, _state = _bot_engine(tmp_path)
    engine.two_way_accounts = {"deux"}
    client = FakeClient(bot_users={"BaibaoxiangCjBot"})
    param = "participate_-1003984379574_8480110324_137"
    msg = _msg(
        text=BAIBAOXIANG_ELIGIBLE + "\n🌏️ 归属地 上海\n📅 开奖时间：2099-12-31 23:59",
        buttons=[
            [
                _btn(
                    "点击参与抽奖",
                    url=f"https://t.me/BaibaoxiangCjBot?start={param}",
                )
            ]
        ],
        id=11143,
    )

    result = asyncio.run(engine._process_message("deux", client, msg))

    assert result == "joined"
    assert client.started == [("BaibaoxiangCjBot", param)]


def test_bot_source_skips_unqualified_message(tmp_path):
    """机器人私聊来源：未达标消息不含资格关键词，不得点击参与按钮。"""
    import asyncio

    engine, _state = _bot_engine(tmp_path)
    client = FakeClient(bot_users={"BaibaoxiangCjBot"})
    param = "participate_-1002839891304_7357117687_408"
    msg = _msg(
        text="您已成功报名抽奖（未达标）\n❌您还需要完成激活任务才能抽奖！",
        buttons=[
            [
                _btn(
                    "点击参与抽奖",
                    url=f"https://t.me/BaibaoxiangCjBot?start={param}",
                )
            ]
        ],
        id=11222,
    )

    result = asyncio.run(engine._process_message("deux", client, msg))

    assert result is None
    assert client.started == []


def test_bot_source_ignores_other_bot_links(tmp_path):
    """机器人私聊来源只认当前机器人自己的链接，避免误点推广 bot。"""
    import asyncio

    engine, _state = _bot_engine(tmp_path)
    client = FakeClient(bot_users={"OtherBot"})
    msg = _msg(
        text=BAIBAOXIANG_ELIGIBLE + "\nhttps://t.me/OtherBot?start=promo",
        id=11144,
    )

    result = asyncio.run(engine._process_message("deux", client, msg))

    assert result is None
    assert client.started == []


def test_bot_source_sweeps_only_target_bot(tmp_path):
    """机器人类型任务启动回扫只扫目标 bot，不扫账号历史参与过的其他 bot。"""
    import asyncio

    engine, state = _bot_engine(tmp_path)
    state.add("deux", "lotterybestbot", "p1")
    state.add("deux", "BaibaoxiangCjBot", "p2")

    class RecordingClient(FakeClient):
        def __init__(self):
            super().__init__(bot_users={"BaibaoxiangCjBot", "lotterybestbot"})
            self.history_chats = []

        def get_chat_history(self, chat, limit=None, max_id=0):
            self.history_chats.append(str(chat))
            return super().get_chat_history(chat, limit, max_id)

    client = RecordingClient()

    asyncio.run(engine._sweep_bot_wins("deux", client))

    assert client.history_chats == ["BaibaoxiangCjBot"]


def test_win_sweep_progress_kept_after_complete(tmp_path):
    """回扫结束后保留进度文案（完成/中断两种），补漏轮开始不重置。"""
    engine, _state = _bot_engine(tmp_path)

    progress = engine._begin_win_sweep(2)
    engine._advance_win_sweep(progress)
    engine._end_win_sweep()
    assert engine.win_sweep_text == "中奖回扫 (1/2)"

    progress = engine._begin_win_sweep(2)
    engine._advance_win_sweep(progress)
    engine._advance_win_sweep(progress)
    engine._end_win_sweep()
    assert engine.win_sweep_text == "中奖回扫完成 (2/2)"

    # 补漏轮（announce=False）开始时保留上一轮完成文案，直至本轮有进展
    engine._begin_win_sweep(2, announce=False)
    assert engine.win_sweep_text == "中奖回扫完成 (2/2)"


def test_bot_source_click_confirm_without_auto_join(tmp_path):
    """机器人任务强制关闭自动加群：跟进确认时未走加群分支也不应报未赋值。"""
    import asyncio

    engine, _state = _bot_engine(tmp_path)
    engine.auto_join = False
    client = FakeClient(bot_users={"BaibaoxiangCjBot"})
    msg = _msg(
        text=f"{BAIBAOXIANG_ELIGIBLE}\n📢 https://t.me/xing5517",
        buttons=[[_btn("点击参与抽奖", callback_data="join")]],
        id=11143,
    )

    asyncio.run(engine._click_confirm("deux", client, "BaibaoxiangCjBot", msg, "p1"))


def test_bot_source_scan_skips_membership_and_respects_limit(tmp_path):
    """机器人私聊扫描：不尝试加入监控对象，且只回扫指定条数。"""
    import asyncio

    engine, _state = _bot_engine(tmp_path)
    engine.scan_limit = 1
    engine.chats = ["BaibaoxiangCjBot"]
    client = FakeClient(bot_users={"BaibaoxiangCjBot"})
    client.history = [
        _msg(
            text=BAIBAOXIANG_ELIGIBLE,
            buttons=[
                [
                    _btn(
                        "点击参与抽奖",
                        url=(
                            "https://t.me/BaibaoxiangCjBot?start="
                            "participate_-1003984379574_8480110324_137"
                        ),
                    )
                ]
            ],
            id=11143,
        ),
        _msg(
            text=BAIBAOXIANG_ELIGIBLE,
            buttons=[
                [
                    _btn(
                        "点击参与抽奖",
                        url=(
                            "https://t.me/BaibaoxiangCjBot?start="
                            "participate_-1003984379574_8480110324_136"
                        ),
                    )
                ]
            ],
            id=11142,
        ),
    ]
    engine._clients = {"deux": client}

    asyncio.run(engine.scan_now())

    assert client.joined_targets == []  # 机器人私聊不做成员检查/加入
    assert client.started == [
        ("BaibaoxiangCjBot", "participate_-1003984379574_8480110324_137")
    ]


def test_active_post_mentioning_announce_not_filtered(tmp_path):
    """活跃帖仅在「抽奖说明」段提及「开奖通知」时不被误过滤。"""
    import asyncio

    from tg_signer.giveaway_parsing import message_is_draw_announce

    active = (
        "🎰 上海江晚晴福利抽奖\n\n"
        "🎁 奖品：现金红包\n\n"
        "⚠️ 抽奖说明：\n"
        "1️⃣开奖后本频道将发布开奖通知，请留意\n"
        "2️⃣中奖者请24小时内联系客服\n\n"
        "https://t.me/LotteryBot?start=active1"
    )
    assert not message_is_draw_announce(_msg(text=active))

    engine, _state = _engine(tmp_path, keywords=("上海",))
    client = FakeClient(bot_users={"LotteryBot"})
    asyncio.run(engine._process_message("acc", client, _msg(text=active)))
    assert ("LotteryBot", "active1") in client.started


# ---------------- 口令任务解析与自动发送 ----------------

PAOFU_POST = """🎰 上海泡芙半价券

📮 参与条件：
🌏️ 归属地 上海,江苏
🪪 必须设置用户名
🎫 加入-泡芙的频道
🎫 加入-野寻·上海学生交流群
└  🎟️ 发言数大于 6 条
🎫 加入-野寻·上海打卡通告群
└  🔑 发送口令﹝出击泡芙宝宝﹞
🎫 加入-野寻·上海活动公告
🎫 加入-野寻·上海修车档案

❗ 口令提示：
请先参与抽奖，再发送口令激活

🎁 奖品内容：
💰️ 泡芙P半价券 × 1

📅 开奖日期：(北京时间)
2026年10月21日 13时30分00秒"""


def test_extract_password_tasks():
    from tg_signer.giveaway_parsing import extract_password_tasks

    # 帖内口令子行归属上方最近的「加入-」行
    assert extract_password_tasks(_msg(text=PASSWORD_POST)) == [
        ("野寻·上海打卡通告群", "出击婷婷宝宝")
    ]
    assert extract_password_tasks(_msg(text=PAOFU_POST)) == [
        ("野寻·上海打卡通告群", "出击泡芙宝宝")
    ]
    # 多群组各带口令：逐个返回，支持「」/【】括号变体
    multi = (
        "🎫 加入-甲群\n"
        "└  🔑 发送口令﹝口令甲﹞\n"
        "🎫 加入-乙群\n"
        "└  🔑 发送口令【口令乙】\n"
    )
    assert extract_password_tasks(_msg(text=multi)) == [
        ("甲群", "口令甲"),
        ("乙群", "口令乙"),
    ]
    # 无括号的「发送口令激活」不视为口令文本；普通帖返回空
    assert extract_password_tasks(_msg(text="请先参与抽奖，再发送口令激活")) == []
    assert (
        extract_password_tasks(_msg(text="🎫 加入-上海某频道\n🎁 奖品：现金红包")) == []
    )


def test_extract_join_name_links():
    from tg_signer.giveaway_parsing import extract_join_name_links

    text = (
        "您需加入指定的群组/频道\n"
        "\n"
        "🎫 加入-野寻·上海打卡通告群 ✅\n"
        "🎫 加入-泡芙的频道 ❌\n"
        "🎫 加入-私有口令群 ❌\n"
    )
    msg = _status_message(
        text,
        {
            "野寻·上海打卡通告群": "https://t.me/daka_group",
            "泡芙的频道": "https://t.me/paofu_channel",
            "私有口令群": "https://t.me/+abc12345678",
        },
    )
    # ❌/✅ 行均收集（口令群即使已加入也需定位目标才能发送口令）；
    # 私有邀请链接整链保留
    assert extract_join_name_links(msg) == [
        ("野寻·上海打卡通告群", "daka_group"),
        ("泡芙的频道", "paofu_channel"),
        ("私有口令群", "https://t.me/+abc12345678"),
    ]
    # 无实体（原始帖只有群名没链接）/ 无「加入」行均返回空
    assert extract_join_name_links(_msg(text=text)) == []
    assert extract_join_name_links(_msg(text="普通消息")) == []


def test_click_confirm_sends_password(tmp_path, monkeypatch):
    """帖内口令任务：按 bot 清单定位口令群组 → 加入 → 发送口令 → 重发 start。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"LotteryBestBot"})
    checklist = _status_message(
        "您需加入指定的群组/频道\n\n"
        "🎫 加入-野寻·上海学生交流群 ❌\n"
        "🎫 加入-野寻·上海打卡通告群 ❌",
        {
            "野寻·上海学生交流群": "https://t.me/stu_group",
            "野寻·上海打卡通告群": "https://t.me/daka_group",
        },
    )
    asyncio.run(
        engine._click_confirm(
            "acc",
            client,
            "LotteryBestBot",
            checklist,
            "r1",
            source_message=_msg(text=PASSWORD_POST),
        )
    )
    # 口令群已加入并发送口令；随后重发 start 让 bot 核验
    assert "daka_group" in client.joined_targets
    assert ("daka_group", "出击婷婷宝宝") in client.sent
    assert any("发送口令" in line for line in logs)
    assert ("LotteryBestBot", "r1") in client.started


def test_click_confirm_password_group_missing(tmp_path, monkeypatch):
    """清单中无口令群链接：跳过发送并记日志，不中断参与流程。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"LotteryBestBot"})
    checklist = _status_message(
        STATUS_REPLY, {"某测试频道D": "https://t.me/test_channel_d"}
    )
    asyncio.run(
        engine._click_confirm(
            "acc",
            client,
            "LotteryBestBot",
            checklist,
            "r1",
            source_message=_msg(text=PASSWORD_POST),
        )
    )
    assert client.sent == []
    assert any("未在清单中定位到链接" in line for line in logs)
    # 参与流程不中断：重发 start 仍发生
    assert ("LotteryBestBot", "r1") in client.started


def test_send_password_tasks_dedup(tmp_path, monkeypatch):
    """同一账号×bot×群组只发送一次口令，重复参与不重复发言。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"LotteryBestBot"})
    checklist = _status_message(
        "您需加入指定的群组/频道\n\n🎫 加入-野寻·上海打卡通告群 ✅",
        {"野寻·上海打卡通告群": "https://t.me/daka_group"},
    )
    for _ in range(2):
        asyncio.run(
            engine._send_password_tasks(
                "acc", client, "LotteryBestBot", _msg(text=PASSWORD_POST), [checklist]
            )
        )
    # ✅ 行（已加入）也参与定位：无需 join 直接发送；第二次调用去重
    assert ("daka_group", "出击婷婷宝宝") in client.sent
    assert len(client.sent) == 1
    assert len([line for line in logs if "发送口令" in line]) == 1


class _PrivateGroupClient(FakeClient):
    """口令目标不可访问：成员检查与加入都返回 CHANNEL_PRIVATE。"""

    def __init__(self):
        super().__init__()
        self.join_attempts = 0

    async def get_chat_member(self, target, user_id):
        raise RuntimeError(
            "Telegram says: [400 CHANNEL_PRIVATE] - "
            "You haven't joined this channel/supergroup."
        )

    async def join_chat(self, target):
        self.join_attempts += 1
        raise RuntimeError(
            "Telegram says: [400 CHANNEL_PRIVATE] - "
            "You haven't joined this channel/supergroup."
        )


def test_private_password_group_skipped_within_ttl(tmp_path, monkeypatch):
    """CHANNEL_PRIVATE 口令群：加入失败后打标记，TTL 内不再重复尝试。

    旧逻辑：成员检查返回未知就跳过 join 直接发消息，每轮都拿
    CHANNEL_PRIVATE 失败并重复消耗请求额度（日志里的
    「发送口令失败（【上海】沪上嫩妹甄选府）」）。
    """
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = _PrivateGroupClient()
    private_post = (
        "🎰 测试抽奖\n"
        "📮 参与条件：\n"
        "🎫 加入-沪上嫩妹甄选府\n"
        "└  🔑 发送口令﹝出击婷婷宝宝﹞"
    )
    checklist = _status_message(
        "您需加入指定的群组/频道\n\n🎫 加入-沪上嫩妹甄选府 ❌",
        {"沪上嫩妹甄选府": "https://t.me/hsnmyxs_private"},
    )
    for _ in range(2):
        asyncio.run(
            engine._send_password_tasks(
                "acc",
                client,
                "LotteryBestBot",
                _msg(text=private_post),
                [checklist],
            )
        )
    # 成员未知时先尝试加入（而不是直接发消息撞 CHANNEL_PRIVATE）
    assert client.join_attempts == 1
    assert client.sent == []
    assert engine._target_unreachable("acc", "沪上嫩妹甄选府")
    assert sum("不可访问" in line for line in logs) == 1


def test_unreachable_join_target_not_retried(tmp_path, monkeypatch):
    """CHANNEL_PRIVATE 的加入目标（如 chasesh_jlq）在后续消息里直接跳过。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = _PrivateGroupClient()
    message = _status_message(
        "您需加入指定的群组/频道\n\n🎫 加入-私密群组 ❌",
        {"私密群组": "https://t.me/chasesh_private"},
    )
    # 直接走加入路径：第一次失败标记，第二次不再发起 join_chat
    for _ in range(2):
        asyncio.run(engine._join_from_message("acc", client, "LotteryBestBot", message))
    assert client.join_attempts == 1
    assert engine._target_unreachable("acc", "chasesh_private")


def test_unreachable_target_retried_after_ttl(tmp_path, monkeypatch):
    """不可访问标记 24 小时后过期：到期后允许再试一次加入。

    避免私密群恢复可加入（或被重新拉回）后，任务一直跳过直到重启。
    """
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    engine.log = lambda *a, **k: None
    monkeypatch.setattr(gc, "UNREACHABLE_TARGET_TTL", 60)
    client = _PrivateGroupClient()
    message = _status_message(
        "您需加入指定的群组/频道\n\n🎫 加入-私密群组 ❌",
        {"私密群组": "https://t.me/chasesh_private"},
    )
    asyncio.run(engine._join_from_message("acc", client, "LotteryBestBot", message))
    assert client.join_attempts == 1
    assert engine._target_unreachable("acc", "chasesh_private")

    # 手工把到期时间拨到过去，模拟 TTL 过期
    key = engine._target_key("acc", "chasesh_private")
    engine._unreachable_targets[key] = 0.0
    assert not engine._target_unreachable("acc", "chasesh_private")
    assert key not in engine._unreachable_targets

    asyncio.run(engine._join_from_message("acc", client, "LotteryBestBot", message))
    assert client.join_attempts == 2


def test_send_password_tasks_dedup_per_password(tmp_path, monkeypatch):
    """去重键含口令文本：同一群组被多个抽奖复用且各带不同口令时，
    第二个抽奖的口令不被首个口令的去重误拦（旧键 account|bot|群组
    导致整轮只有第一个抽奖的口令真正发出）。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    client = FakeClient(bot_users={"LotteryBestBot"})
    checklist = _status_message(
        "您需加入指定的群组/频道\n\n🎫 加入-野寻·上海打卡通告群 ✅",
        {"野寻·上海打卡通告群": "https://t.me/daka_group"},
    )
    asyncio.run(
        engine._send_password_tasks(
            "acc", client, "LotteryBestBot", _msg(text=PASSWORD_POST), [checklist]
        )
    )
    other_post = PASSWORD_POST.replace("婷婷", "雾织星")
    asyncio.run(
        engine._send_password_tasks(
            "acc", client, "LotteryBestBot", _msg(text=other_post), [checklist]
        )
    )
    assert ("daka_group", "出击婷婷宝宝") in client.sent
    assert ("daka_group", "出击雾织星宝宝") in client.sent


def test_password_send_slots_spaced_per_group(tmp_path, monkeypatch):
    """多账号命中同一群口令时按群组错峰排期：后到者在上一个名额之后发言。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    engine.password_send_gap = (10.0, 10.0)

    async def _run():
        await engine._wait_password_send_slot("acc1", "同一群", "same_group")
        first = engine._password_gate_until["target:same_group"]
        await engine._wait_password_send_slot("acc2", "同一群", "same_group")
        second = engine._password_gate_until["target:same_group"]
        return first, second

    first, second = asyncio.run(_run())
    assert second - first == pytest.approx(10.0, abs=0.5)


def test_send_password_tasks_private_invite_link(tmp_path, monkeypatch):
    """口令群为私有邀请链接：先解析成真实 chat id 再发送，不再把
    t.me/+hash 整串当用户名走 contacts.ResolveUsername（USERNAME_INVALID）。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append

    class InviteClient(FakeClient):
        def __init__(self):
            super().__init__(bot_users={"BaibaoxiangCjBot"})
            self.invocations = []

        async def invoke(self, query):
            self.invocations.append(query)
            # CheckChatInvite：已是成员，返回带 chat 的结果
            from pyrogram import raw

            return SimpleNamespace(
                chat=raw.types.Channel(
                    id=2651731839,
                    title="野寻·上海打卡通告群",
                    photo=raw.types.ChatPhotoEmpty(),
                    date=0,
                )
            )

    client = InviteClient()
    checklist = _status_message(
        "您需加入指定的群组/频道\n\n🎫 加入-野寻·上海打卡通告群 ❌",
        {"野寻·上海打卡通告群": "https://t.me/+abc12345678"},
    )
    password_post = PASSWORD_POST.replace("出击婷婷宝宝", "出击普陀嫩妹思思")
    asyncio.run(
        engine._send_password_tasks(
            "acc",
            client,
            "BaibaoxiangCjBot",
            _msg(text=password_post),
            [checklist],
        )
    )
    assert client.invocations, "应先经 CheckChatInvite 解析私有邀请链接"
    assert (-1002651731839, "出击普陀嫩妹思思") in client.sent
    assert any("已在「野寻·上海打卡通告群」发送口令" in line for line in logs)


def test_send_password_tasks_two_way_account_skips(tmp_path):
    """双向账号一律跳过口令任务（含 bot 清单 ❌ 补发场景）：
    不发送、不产生 speak_failed 事件、无日志，避免触发 Telegram 风控。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    engine.two_way_accounts = {"双向号"}
    logs: list = []
    engine.log = logs.append
    events: list = []
    engine.on_event = events.append
    client = FakeClient(bot_users={"LotteryBestBot"})
    checklist = _status_message(
        "您需加入指定的群组/频道\n\n🎫 加入-野寻·上海打卡通告群 ❌",
        {"野寻·上海打卡通告群": "https://t.me/daka_group"},
    )
    asyncio.run(
        engine._send_password_tasks(
            "双向号", client, "LotteryBestBot", _msg(text=PASSWORD_POST), [checklist]
        )
    )
    assert not client.sent
    assert not logs
    assert not events


def test_send_password_tasks_stale_username_dialog_fallback(tmp_path, monkeypatch):
    """清单链接的用户名失效（群改名/收回用户名）：按群名在已加入会话中
    精确匹配兜底发送；匹配不到时保留原失败路径。"""
    import asyncio
    from types import SimpleNamespace

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append

    class StaleClient(FakeClient):
        def __init__(self):
            super().__init__(bot_users={"LotteryBestBot"})

        async def send_message(self, chat_id, text):
            if str(chat_id) == "stale_group":
                raise RuntimeError(
                    "Telegram says: [400 USERNAME_INVALID] - "
                    "The provided username is not valid."
                )
            self.sent.append((chat_id, text))

        async def get_dialogs(self, limit=None):
            yield SimpleNamespace(
                chat=SimpleNamespace(id=-1002651731839, title="野寻·上海打卡通告群")
            )

    client = StaleClient()
    checklist = _status_message(
        "您需加入指定的群组/频道\n\n🎫 加入-野寻·上海打卡通告群 ❌",
        {"野寻·上海打卡通告群": "https://t.me/stale_group"},
    )
    asyncio.run(
        engine._send_password_tasks(
            "acc",
            client,
            "LotteryBestBot",
            _msg(text=PASSWORD_POST.replace("出击婷婷宝宝", "出击普陀嫩妹思思")),
            [checklist],
        )
    )
    assert (-1002651731839, "出击普陀嫩妹思思") in client.sent
    assert any("已按群名匹配已加入会话兜底发送" in line for line in logs)


ACTIVATION_REPLY = (
    "您已成功报名抽奖（未达标α）\n"
    "\n"
    "您还需完成激活任务才能抽奖！\n"
    "\n"
    "🎰 上海雾织星半价券\n"
    "🎫 已加入-野寻·上海学生交流群\n"
    "└  🎟️ 发言数大于 6 条 ❌\n"
    "🎫 已加入-野寻·上海打卡通告群\n"
    "└  🔑 发送口令﹝出击雾织星宝宝﹞ ❌\n"
    "\n"
    "💁 参与情况：69达标 / 90报名\n"
    "🔋 人品加成：6票\n"
    "\n"
    "📅 开奖日期：(北京时间)\n"
    "2026年10月21日 13时00分00秒"
)


def test_extract_password_tasks_activation_reply():
    """报名成功后的激活任务清单：❌ 口令子行按上方最近「已加入-名称」
    行归属群组；✅ 行跳过；发言数子行不视为口令任务。"""
    from tg_signer.giveaway_parsing import extract_password_tasks

    msg = _status_message(
        ACTIVATION_REPLY,
        {
            "已加入-野寻·上海学生交流群": "https://t.me/xuesheng_group",
            "已加入-野寻·上海打卡通告群": "https://t.me/daka_group2",
        },
    )
    assert extract_password_tasks(msg) == [("野寻·上海打卡通告群", "出击雾织星宝宝")]
    # ✅ 已完成行不产生口令任务
    done = ACTIVATION_REPLY.replace(
        "发送口令﹝出击雾织星宝宝﹞ ❌", "发送口令﹝出击雾织星宝宝﹞ ✅"
    )
    assert extract_password_tasks(_msg(text=done)) == []


def test_extract_speech_tasks_activation_reply():
    """报名成功清单：「发言数大于 N 条」子行按上方最近加入行归属群组，
    ✅ 已达标行跳过。"""
    from tg_signer.giveaway_parsing import extract_speech_tasks

    assert extract_speech_tasks(_msg(text=ACTIVATION_REPLY)) == [
        ("野寻·上海学生交流群", 6)
    ]
    done = ACTIVATION_REPLY.replace("发言数大于 6 条 ❌", "发言数大于 6 条 ✅")
    assert extract_speech_tasks(_msg(text=done)) == []
    # 原帖写法（无 ❌ 标记）同样可解析
    assert extract_speech_tasks(
        _msg(text="🎫 加入-某测试群组A\n└  🎟️ 发言数大于 10 条")
    ) == [("某测试群组A", 10)]


def test_verify_after_click_emits_speech_required(tmp_path, monkeypatch):
    """报名成功清单中 ❌ 发言数任务 → speech_required 事件（含标题/群组/条数）。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    events: list = []
    engine.on_event = events.append
    client = FakeClient(bot_users={"LotteryBestBot"})
    activation = _status_message(
        ACTIVATION_REPLY,
        {
            "已加入-野寻·上海学生交流群": "https://t.me/xuesheng_group",
            "已加入-野寻·上海打卡通告群": "https://t.me/daka_group2",
        },
    )
    client.history = [activation]
    asyncio.run(
        engine._verify_after_click("acc", client, "LotteryBestBot", [activation])
    )
    speech = [e for e in events if e.get("type") == "speech_required"]
    assert speech == [
        {
            "type": "speech_required",
            "account": "acc",
            "bot": "LotteryBestBot",
            "title": "上海雾织星半价券",
            "group": "野寻·上海学生交流群",
            "required": 6,
        }
    ]


def test_verify_after_click_resends_pending_password(tmp_path, monkeypatch):
    """报名成功回复的激活清单仍列 ❌ 口令任务＝bot 未计入此前发送：
    立即按清单补发口令（忽略预发送去重），发言数任务不处理。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"LotteryBestBot"})
    activation = _status_message(
        ACTIVATION_REPLY,
        {
            "已加入-野寻·上海学生交流群": "https://t.me/xuesheng_group",
            "已加入-野寻·上海打卡通告群": "https://t.me/daka_group2",
        },
    )
    client.history = [activation]
    # 预发送阶段已发过同一口令（模拟 bot 未计入的场景）
    engine._password_sent.add("acc|LotteryBestBot|野寻·上海打卡通告群|出击雾织星宝宝")
    asyncio.run(
        engine._verify_after_click("acc", client, "LotteryBestBot", [activation])
    )
    assert ("daka_group2", "出击雾织星宝宝") in client.sent
    assert ("xuesheng_group", "发言数大于 6 条") not in client.sent


def test_verify_after_click_done_password_not_resent(tmp_path, monkeypatch):
    """激活清单口令行标 ✅（bot 已计入）：不重发口令。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"LotteryBestBot"})
    done_reply = ACTIVATION_REPLY.replace(
        "发送口令﹝出击雾织星宝宝﹞ ❌", "发送口令﹝出击雾织星宝宝﹞ ✅"
    )
    activation = _status_message(
        done_reply,
        {
            "已加入-野寻·上海学生交流群": "https://t.me/xuesheng_group",
            "已加入-野寻·上海打卡通告群": "https://t.me/daka_group2",
        },
    )
    client.history = [activation]
    asyncio.run(
        engine._verify_after_click("acc", client, "LotteryBestBot", [activation])
    )
    assert client.sent == []


# ---------------- 中奖识别（win detect） ----------------


WIN_SAMPLE = (
    "💐💐💐 中奖提醒 💐💐💐\n\n"
    "🔖 防伪编号：H179190232717\n\n"
    "🎰 【上海栖心地】 徐汇奶霸 @fhurdvjj 福利\n\n"
    "🎊 江澈 恭喜您中奖了！\n\n"
    "💁 参与人数：79人\n\n"
    "💰️ 获得奖品：p-100\n🎅 联系领奖：@fhurdvjj\n\n"
    "幸运时刻已开启, 中奖只是开始！"
)


def test_win_regex_matches_real_bot_samples():
    """真实 bot 中奖私信样本均命中识别正则；参与类消息不误报。"""
    from tg_signer.giveaway_parsing import WIN_MESSAGE_RE

    # @LotteryBestBot：标题行 + 正文恭喜 + 防伪编号
    assert WIN_MESSAGE_RE.search(WIN_SAMPLE)
    # @BaibaoxiangCjBot：无「中奖提醒」标题、无防伪编号，仅正文恭喜
    baibao = (
        "🎉 恭喜您，您中奖啦！\n\n"
        "👥 中奖群组：上海艺术展览会备用群\n\n"
        "🎁 中得奖品：2次优惠300\n\n"
        "📜 抽奖说明：\n中奖者截图发给老师兑换 @mitai113"
    )
    assert WIN_MESSAGE_RE.search(baibao)
    # @RealSaberBot：仅正文「恭喜您中奖了」+ 校验码
    saber = (
        "🎉🎊 恭喜您中奖了 🎊🎉\n\n"
        "👑 中奖人：林叙\n🆔 ID：8216950628\n\n"
        "🔑 校验码：dXFKQFL07KsODbbzJDVQRWxN"
    )
    assert WIN_MESSAGE_RE.search(saber)
    # 参与清单/规则说明类消息不误报
    assert not WIN_MESSAGE_RE.search("🎫 加入-某频道 ❌\n└  🔑 发送口令﹝出击宝宝﹞")
    assert not WIN_MESSAGE_RE.search("📜 抽奖说明：中奖者联系老师兑奖")


def test_check_win_message_skips_draw_announcement(tmp_path):
    """开奖结果公告（防伪编号+他人中奖名单，无个人化标记）不按中奖处理。"""
    import asyncio

    from tg_signer.giveaway_parsing import (
        WIN_ANNOUNCE_RE,
        WIN_MESSAGE_RE,
        WIN_PERSONAL_RE,
    )

    # 真实样本：@shlldqbot 群发的开奖通知（曾误转进中奖群）
    announce = (
        "🎉🎉🎉 开奖通知 🎉🎉🎉\n\n"
        "🔖 防伪编号：H179229906522\n\n"
        "🎰 🎰 萝莉岛（@shlldq）  抽奖中奖联系 @shlldqbot\n\n"
        "⚠️ 抽奖说明：\n"
        "1️⃣本次抽奖由 上海萝莉岛赞助 \n"
        "2️⃣开奖后请24小时内联系 @shlldqbot\n"
        "3️⃣遇到问题请找唯一客服 @shlldqbot\n\n"
        "💁 参与情况：1276达标 / 1349报名\n\n"
        "🎅 联系领奖：上海萝莉岛客服\n\n"
        "1. 富贵花开 奖品：口令🧧38\n"
        "2. 为何如 此霸气 奖品：口令🧧38\n\n"
        "祝大家心想事成好运连连！"
    )
    assert WIN_MESSAGE_RE.search(announce)  # 命中防伪编号
    assert WIN_ANNOUNCE_RE.search(announce)
    assert not WIN_PERSONAL_RE.search(announce)

    engine, _state = _engine(tmp_path)
    client = FakeClient(bot_users={"shlldqbot"})
    events: list = []
    engine.on_event = events.append
    asyncio.run(
        engine._check_win_message(
            "acc", client, "shlldqbot", _msg(text=announce, id=11)
        )
    )
    assert client.forwarded == []
    assert events == []

    # 带个人化标记（恭喜的是「您」）的开奖消息仍照常识别为中奖
    personal = (
        "🎉 开奖通知 🎉\n\n恭喜您中奖了！\n\n"
        "👑 中奖人：林叙\n🔖 防伪编号：H179229906522"
    )
    assert WIN_PERSONAL_RE.search(personal)
    engine2, _ = _engine(tmp_path)
    client2 = FakeClient(bot_users={"shlldqbot"})
    events2: list = []
    engine2.on_event = events2.append
    asyncio.run(
        engine2._check_win_message(
            "acc", client2, "shlldqbot", _msg(text=personal, id=12)
        )
    )
    assert len(client2.forwarded) == 1
    assert len(events2) == 1


def test_check_win_message_ignores_nenmeibi_prize(tmp_path):
    """奖品为「嫩妹币」的中奖消息不转发、不推送；说明段提及不误伤。"""
    import asyncio

    from tg_signer.giveaway_parsing import WIN_IGNORE_PRIZE_RE

    # 真实样本：@RealSaberBot 私信，100 嫩妹币
    real = (
        "🎉🎊 恭喜您中奖了 🎊🎉\n\n"
        "👑 中奖人：用户\n🆔 ID：8416628750\n\n"
        "🎁 中奖奖品：100嫩妹币\n├─ 积分变化：+100\n"
        "🎯 抽奖活动：秘宝猎人集结令\n\n"
        "🔑 校验码：PxDEgwoxnouf3mT0VtdjGfWv"
    )
    assert WIN_IGNORE_PRIZE_RE.search(real)

    engine, _state = _engine(tmp_path)
    client = FakeClient(bot_users={"RealSaberBot"})
    events: list = []
    engine.on_event = events.append
    asyncio.run(
        engine._check_win_message("acc", client, "RealSaberBot", _msg(text=real, id=21))
    )
    assert client.forwarded == []
    assert events == []

    # 说明段提及「嫩妹币」但奖品是优惠券：仍按中奖处理
    others = (
        "💐💐💐 中奖提醒 💐💐💐\n\n"
        "🔖 防伪编号：H179190232717\n\n"
        "🎰 某抽奖活动\n\n"
        "🎊 用户 恭喜您中奖了！\n\n"
        "💰️ 获得奖品：p-100\n\n"
        "⚠️ 抽奖说明：嫩妹币不计入本次活动奖品范围"
    )
    assert not WIN_IGNORE_PRIZE_RE.search(others)
    engine2, _ = _engine(tmp_path)
    client2 = FakeClient(bot_users={"LotteryBestBot"})
    events2: list = []
    engine2.on_event = events2.append
    asyncio.run(
        engine2._check_win_message(
            "acc", client2, "LotteryBestBot", _msg(text=others, id=22)
        )
    )
    assert len(client2.forwarded) == 1
    assert len(events2) == 1


def test_state_bots_for_orders_by_recency(tmp_path):
    """bots_for：按最近交互时间倒序返回账号参与过的 bot，大小写去重。"""
    state = GiveawayState(tmp_path)
    # AAA 最后一条后写入（同一秒内按 rowid 决胜负），排在最前
    state.add("acc", "BotBBB", "p2")
    state.add("acc", "BotAAA", "p3")
    state.add("acc", "BotAAA", "p1")
    assert state.bots_for("acc") == ["BotAAA", "BotBBB"]
    assert state.bots_for("other") == []


def test_state_clear_keeps_win_records_and_watermark(tmp_path):
    """清除参与记录：中奖去重记录与回扫水位线保留，旧中奖不会重复推送。"""
    state = GiveawayState(tmp_path)
    state.add("acc", "BotAAA", "p1")
    state.add("acc", "BotAAA", "win:7", note="win")
    state.set_win_watermark("acc", "BotAAA", 7)
    state.save()

    assert state.count() == 1  # 不含 win: 去重记录
    assert state.clear() == 1
    assert state.count() == 0
    assert not state.has("acc", "BotAAA", "p1")
    assert state.has("acc", "BotAAA", "win:7")
    assert state.win_watermark("acc", "BotAAA") == 7

    # 落盘后重新读取：保留项仍在
    reloaded = GiveawayState(tmp_path)
    assert reloaded.count() == 0
    assert reloaded.has("acc", "BotAAA", "win:7")
    assert reloaded.win_watermark("acc", "BotAAA") == 7


def test_check_win_message_forwards_emits_and_dedups(tmp_path):
    """中奖消息：转发到收藏夹 + 发 win 事件 + 状态记录；同实例重复检测不重复转发。"""
    import asyncio

    engine, state = _engine(tmp_path)
    events: list = []
    engine.on_event = events.append
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"LotteryBestBot"})
    win = _msg(text=WIN_SAMPLE, id=7)
    asyncio.run(engine._check_win_message("acc", client, "LotteryBestBot", win))
    assert client.forwarded == [("me", -1001234567890, (7,))]
    assert len(events) == 1
    assert events[0]["type"] == "win"
    assert events[0]["account"] == "acc"
    assert events[0]["bot"] == "LotteryBestBot"
    assert state.has("acc", "LotteryBestBot", "win:7")
    assert any("检测到中奖消息" in line for line in logs)
    assert any("已转发到 收藏夹" in line for line in logs)

    # 同一引擎实例重复检测：不重复转发/发事件
    asyncio.run(engine._check_win_message("acc", client, "LotteryBestBot", win))
    assert client.forwarded == [("me", -1001234567890, (7,))]
    assert len(events) == 1


def test_check_win_message_dedups_across_restart(tmp_path):
    """跨重启去重：状态文件里已记录 win:<id> 的新引擎实例不再转发。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    client = FakeClient(bot_users={"LotteryBestBot"})
    win = _msg(text=WIN_SAMPLE, id=9)
    asyncio.run(engine._check_win_message("acc", client, "LotteryBestBot", win))
    assert len(client.forwarded) == 1

    engine2, _ = _engine(tmp_path)
    events2: list = []
    engine2.on_event = events2.append
    asyncio.run(engine2._check_win_message("acc", client, "LotteryBestBot", win))
    # 未新增转发，也未重复发事件
    assert client.forwarded == [("me", -1001234567890, (9,))]
    assert events2 == []


def test_check_win_message_ignores_non_win_and_disabled(tmp_path):
    """参与清单类消息不误报；开关关闭时不检测。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    client = FakeClient(bot_users={"LotteryBestBot"})
    asyncio.run(
        engine._check_win_message(
            "acc",
            client,
            "LotteryBestBot",
            _msg(text="🎫 加入-某频道 ❌\n└  🔑 发送口令﹝出击宝宝﹞", id=1),
        )
    )
    assert client.forwarded == []

    engine.win_detect = False
    asyncio.run(
        engine._check_win_message(
            "acc", client, "LotteryBestBot", _msg(text=WIN_SAMPLE, id=2)
        )
    )
    assert client.forwarded == []


def test_sweep_bot_wins_scans_known_bots_only(tmp_path):
    """启动回扫：只扫状态里交互过的 bot；扫到中奖消息即转发+发事件。"""
    import asyncio

    engine, state = _engine(tmp_path)
    state.add("acc", "LotteryBestBot", "p1")
    state.save()
    client = FakeClient(bot_users={"LotteryBestBot"})
    client.history = [
        _msg(text="请点击按钮参与抽奖", id=12),
        _msg(text="💐💐💐 中奖提醒 💐💐💐 恭喜您中奖了！", id=11),
    ]
    events: list = []
    engine.on_event = events.append
    asyncio.run(engine._sweep_bot_wins("acc", client))
    assert client.forwarded == [("me", -1001234567890, (11,))]
    assert len(events) == 1

    # 无交互记录的账号：不扫任何 bot
    client2 = FakeClient(bot_users={"LotteryBestBot"})
    client2.history = [_msg(text=WIN_SAMPLE, id=13)]
    asyncio.run(engine._sweep_bot_wins("other", client2))
    assert client2.forwarded == []


def test_sweep_bot_wins_incremental_watermark(tmp_path):
    """中奖回扫水位线：首次按回扫条数建基线，之后只扫新消息并跨重启持久化。"""
    import asyncio

    engine, state = _engine(tmp_path)
    state.add("acc", "LotteryBestBot", "p1")
    state.save()
    client = FakeClient(bot_users={"LotteryBestBot"})
    client.history = [
        _msg(text="普通消息", id=12),
        _msg(text="💐💐💐 中奖提醒 💐💐💐 恭喜您中奖了！", id=11),
    ]
    events: list = []
    engine.on_event = events.append
    asyncio.run(engine._sweep_bot_wins("acc", client))
    assert len(events) == 1
    assert state.win_watermark("acc", "LotteryBestBot") == 12

    # 新消息到达后再次回扫：只处理比水位线新的消息
    client.history.insert(0, _msg(text="💐💐💐 中奖提醒 💐💐💐 又中奖了！", id=14))
    asyncio.run(engine._sweep_bot_wins("acc", client))
    assert len(events) == 2
    assert state.win_watermark("acc", "LotteryBestBot") == 14

    # 水位线随状态文件跨引擎实例（重启）持久化，无新消息不重复推送
    state2 = GiveawayState(tmp_path)
    assert state2.win_watermark("acc", "LotteryBestBot") == 14
    engine2 = GiveawayEngine(state=state2)
    engine2.on_event = events.append
    client2 = FakeClient(bot_users={"LotteryBestBot"})
    client2.history = client.history
    asyncio.run(engine2._sweep_bot_wins("acc", client2))
    assert len(events) == 2


def test_sweep_bot_wins_min_count_extends_past_watermark(tmp_path):
    """回扫条数作为扫描量下限：水位线后新消息不足下限时向更旧消息补扫。"""
    import asyncio

    engine, state = _engine(tmp_path)
    state.add("acc", "LotteryBestBot", "p1")
    # 水位线=10，旧中奖消息 id=5 在基线之外（此前漏捡）
    state.set_win_watermark("acc", "LotteryBestBot", 10)
    state.save()
    client = FakeClient(bot_users={"LotteryBestBot"})
    client.history = [
        _msg(text="普通消息", id=11),
        _msg(text="普通消息", id=10),
        _msg(text="普通消息", id=9),
        _msg(text="普通消息", id=8),
        _msg(text="普通消息", id=7),
        _msg(text="普通消息", id=6),
        _msg(text="💐💐💐 中奖提醒 💐💐💐 恭喜您中奖了！", id=5),
    ]
    events: list = []
    engine.on_event = events.append
    asyncio.run(engine._sweep_bot_wins("acc", client))
    # 水位线后仅 1 条新消息，不足回扫条数下限 → 越过水位线补扫到旧中奖消息
    assert len(events) == 1
    assert state.win_watermark("acc", "LotteryBestBot") == 11

    # 再次回扫：已处理的旧消息被状态去重，不重复推送
    asyncio.run(engine._sweep_bot_wins("acc", client))
    assert len(events) == 1


def test_win_forward_target_numeric_string_becomes_int(tmp_path):
    """转发目标为数字（频道 id）时必须转为 int，避免被 Telethon 当手机号 resolve。"""
    import asyncio

    engine, state = _engine(tmp_path)
    asyncio.run(
        engine.start(
            accounts=[],
            chats=[],
            session_dir=tmp_path,
            log=lambda msg: None,
            win_detect=True,
            win_forward_target=["-1004324986564", "me", "@channel"],
            state=state,
        )
    )
    assert engine.win_forward_targets == [-1004324986564, "me", "@channel"]


def test_win_settings_roundtrip(tmp_path):
    """中奖消息转发目标（全局设置）：保存/读取往返；损坏文件回退收藏夹。"""
    from tg_signer.webui.giveaway import (
        load_win_forward_targets,
        save_win_forward_targets,
    )

    assert load_win_forward_targets(tmp_path) == ["me"]  # 无配置文件 → 默认收藏夹
    save_win_forward_targets(tmp_path, ["me", "-1001234567890", "me", " "])
    assert load_win_forward_targets(tmp_path) == ["me", "-1001234567890"]

    # 旧版单目标字符串配置：自动转为列表
    (tmp_path / "webui_win_settings.json").write_text(
        '{"win_forward_target": "-1004324986564"}', encoding="utf-8"
    )
    assert load_win_forward_targets(tmp_path) == ["-1004324986564"]

    # 清空保存 → 回退收藏夹；损坏文件同样回退
    save_win_forward_targets(tmp_path, [])
    assert load_win_forward_targets(tmp_path) == ["me"]
    (tmp_path / "webui_win_settings.json").write_text("{bad", encoding="utf-8")
    assert load_win_forward_targets(tmp_path) == ["me"]


def test_scan_cap_setting_roundtrip(tmp_path):
    """单频道扫描上限（全局设置）：默认 500，保存/读取往返，损坏文件回退默认。"""
    from tg_signer.giveaway_core import DEFAULT_SCAN_CAP
    from tg_signer.webui.giveaway import (
        load_giveaway_scan_cap,
        save_giveaway_scan_cap,
    )

    assert DEFAULT_SCAN_CAP == 500
    assert load_giveaway_scan_cap(tmp_path) == 500
    save_giveaway_scan_cap(tmp_path, 800)
    assert load_giveaway_scan_cap(tmp_path) == 800
    # 非法值（0/负数）回退为 1，避免上限失效
    save_giveaway_scan_cap(tmp_path, 0)
    assert load_giveaway_scan_cap(tmp_path) == 1
    (tmp_path / "webui_win_settings.json").write_text("{bad", encoding="utf-8")
    assert load_giveaway_scan_cap(tmp_path) == 500


def test_win_skip_keywords_setting_roundtrip(tmp_path):
    """中奖消息跳过关键字（全局设置）：逗号分隔读写、去重、损坏文件回退空。"""
    from tg_signer.webui.giveaway import (
        load_win_skip_keywords,
        save_win_skip_keywords,
    )

    assert load_win_skip_keywords(tmp_path) == []
    save_win_skip_keywords(tmp_path, "感谢参与，未中奖, 未中奖")
    assert load_win_skip_keywords(tmp_path) == ["感谢参与", "未中奖"]
    # 列表形式（兼容旧配置）同样可读
    save_win_skip_keywords(tmp_path, ["活动结束", " ", "未中奖"])
    assert load_win_skip_keywords(tmp_path) == ["活动结束", "未中奖"]
    (tmp_path / "webui_win_settings.json").write_text("{bad", encoding="utf-8")
    assert load_win_skip_keywords(tmp_path) == []


def test_win_skip_keywords_ignore_win_message(tmp_path):
    """中奖消息命中跳过关键字：不推送事件、不转发。"""
    import asyncio

    engine, state = _engine(tmp_path)
    asyncio.run(
        engine.start(
            accounts=[],
            chats=[],
            session_dir=tmp_path,
            log=lambda msg: None,
            win_detect=True,
            win_skip_keywords=["感谢参与"],
            state=state,
        )
    )
    assert engine.win_skip_keywords == ("感谢参与",)
    state.add("acc", "LotteryBestBot", "p1")
    state.save()
    client = FakeClient(bot_users={"LotteryBestBot"})
    client.history = [
        _msg(text="💐💐💐 中奖提醒 💐💐💐 恭喜您中奖了！感谢参与", id=11),
        _msg(text="💐💐💐 中奖提醒 💐💐💐 恭喜您中奖了！", id=10),
    ]
    events: list = []
    engine.on_event = events.append
    asyncio.run(engine._sweep_bot_wins("acc", client))
    # 只有不含跳过关键字的那条按中奖处理
    assert len(events) == 1
    assert len(client.forwarded) == 1
    assert client.forwarded[0][2] == (10,)


# ---------------- 防脚本验证（按顺序点击 emoji） ----------------

DBCHOUJIANG_VERIFY_TEXT = (
    "🤖 防脚本验证\n请按顺序点击：【🎯、🐻、🐴】\n点错会冷却 30 秒。"
)


def test_extract_emoji_order_samples():
    from tg_signer.giveaway_parsing import _extract_emoji_order

    # @dbchoujiang123_bot 样本：顿号分隔
    assert _extract_emoji_order(DBCHOUJIANG_VERIFY_TEXT) == ["🎯", "🐻", "🐴"]
    # 无分隔连写
    assert _extract_emoji_order("请按顺序点击：【🎯🐻🐴】") == ["🎯", "🐻", "🐴"]
    # 逗号/空格分隔
    assert _extract_emoji_order("【🎁, 🎈 🎉】") == ["🎁", "🎈", "🎉"]
    # 变体选择符并入同一 token
    assert _extract_emoji_order("【❤️、🎯】") == ["❤\ufe0f", "🎯"]
    # 无【】括号 / 空括号
    assert _extract_emoji_order("请按顺序点击 🎯 🐻") == []
    assert _extract_emoji_order("【】") == []
    assert _extract_emoji_order("") == []


def test_is_emoji_verify_message():
    from tg_signer.giveaway_parsing import is_emoji_verify_message

    assert is_emoji_verify_message(_msg(text=DBCHOUJIANG_VERIFY_TEXT))
    # 点错重试提示同样命中
    assert is_emoji_verify_message(
        _msg(text="❌ 点错了，冷却 30 秒\n请重新按顺序点击：【🎯、🐻、🐴】")
    )
    assert not is_emoji_verify_message(_msg(text="欢迎参加抽奖"))
    assert not is_emoji_verify_message(_msg(caption=None, text=""))


def _verify_msg(buttons=None):
    return _msg(
        id=10,
        text=DBCHOUJIANG_VERIFY_TEXT,
        buttons=buttons
        if buttons is not None
        else [
            [
                _btn("🎯", callback_data=b"v1"),
                _btn("🐻", callback_data=b"v2"),
                _btn("🐴", callback_data=b"v3"),
            ]
        ],
    )


def _click_recorder(client):
    clicked: list = []

    async def fake_answer(chat_id, message_id, callback_data):
        clicked.append(callback_data)

    client.request_callback_answer = fake_answer
    return clicked


def test_handle_emoji_verification_clicks_in_order(tmp_path, monkeypatch):
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"Dbchoujiang123Bot"})
    clicked = _click_recorder(client)
    handled = asyncio.run(
        engine._handle_emoji_verification(
            "acc", client, "Dbchoujiang123Bot", [_verify_msg()]
        )
    )
    assert handled is True
    # 按正文顺序点击（先全部校验按钮齐全，再逐个点击）
    assert clicked == [b"v1", b"v2", b"v3"]
    assert any("按顺序点击" in line for line in logs)


def test_handle_emoji_verification_missing_button_no_click(tmp_path, monkeypatch):
    """键盘缺目标按钮：不乱点（点错有冷却惩罚），只记日志。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"Dbchoujiang123Bot"})
    clicked = _click_recorder(client)
    message = _verify_msg(
        buttons=[[_btn("🎯", callback_data=b"v1"), _btn("🐻", callback_data=b"v2")]]
    )
    handled = asyncio.run(
        engine._handle_emoji_verification("acc", client, "Dbchoujiang123Bot", [message])
    )
    assert handled is True
    assert clicked == []
    assert any("不乱点" in line for line in logs)


def test_handle_emoji_verification_unparseable_order(tmp_path, monkeypatch):
    """验证消息解析不出【…】顺序：不点击，记日志。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"Dbchoujiang123Bot"})
    clicked = _click_recorder(client)
    message = _msg(
        id=11,
        text="🤖 防脚本验证\n请完成下方拼图后再试。",
        buttons=[[_btn("开始拼图", callback_data=b"pz")]],
    )
    handled = asyncio.run(
        engine._handle_emoji_verification("acc", client, "Dbchoujiang123Bot", [message])
    )
    assert handled is True
    assert clicked == []
    assert any("未能解析点击顺序" in line for line in logs)


def test_handle_emoji_verification_dedup_same_message(tmp_path, monkeypatch):
    """同一条验证消息进程内只点击一次（重发 start 后旧验证不重按）。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    client = FakeClient(bot_users={"Dbchoujiang123Bot"})
    clicked = _click_recorder(client)
    message = _verify_msg()
    for _ in range(2):
        asyncio.run(
            engine._handle_emoji_verification(
                "acc", client, "Dbchoujiang123Bot", [message]
            )
        )
    assert clicked == [b"v1", b"v2", b"v3"]


def test_handle_emoji_verification_ignores_plain_messages(tmp_path):
    import asyncio

    engine, _state = _engine(tmp_path)
    client = FakeClient(bot_users={"LotteryBot"})
    clicked = _click_recorder(client)
    handled = asyncio.run(
        engine._handle_emoji_verification(
            "acc", client, "LotteryBot", [_msg(text="点击按钮立即参与")]
        )
    )
    assert handled is False
    assert clicked == []


def test_click_confirm_solves_verification_then_confirms(tmp_path, monkeypatch):
    """start 回复防脚本验证：按序点完 emoji → 刷新候选 → 点击后续确认按钮。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"Dbchoujiang123Bot"})
    clicked = _click_recorder(client)
    # 验证通过后 bot 发出的参与消息（刷新候选时拉到）
    client.history = [
        _msg(
            id=11,
            text="验证通过，点击下方按钮立即参与抽奖",
            buttons=[[_btn("立即参加", callback_data=b"go")]],
        )
    ]
    asyncio.run(
        engine._click_confirm("acc", client, "Dbchoujiang123Bot", _verify_msg(), "x1")
    )
    # 先按顺序完成验证，再点击确认按钮
    assert clicked == [b"v1", b"v2", b"v3", b"go"]
    assert any("防脚本验证" in line for line in logs)
    assert any("已点击" in line for line in logs)


# ---------------- 算术验证（直接回复答案数字） ----------------

MATH_VERIFY_TEXT = "请计算：18 + 9 = ？直接回复答案数字。"


def test_is_math_verify_message():
    from tg_signer.giveaway_parsing import is_math_verify_message

    assert is_math_verify_message(_msg(text=MATH_VERIFY_TEXT))
    assert is_math_verify_message(_msg(text="请计算 19+6 = ?"))
    assert not is_math_verify_message(_msg(text="欢迎参加抽奖"))
    assert not is_math_verify_message(_msg(caption=None, text=""))


@pytest.mark.parametrize(
    "text,expected",
    [
        (MATH_VERIFY_TEXT, "18 + 9"),
        ("请计算: 19+6=?", "19+6"),
        ("请计算：１２ ＋ ３ ＝ ？直接回复答案数字。", "12 + 3"),
        ("请计算：6 × 7 = ？直接回复答案数字。", "6 * 7"),
        ("请计算：7 ÷ 2 = ？直接回复答案数字。", "7 / 2"),
    ],
)
def test_extract_math_expression(text, expected):
    from tg_signer.giveaway_parsing import extract_math_expression

    assert extract_math_expression(_msg(text=text)) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        (MATH_VERIFY_TEXT, "27"),
        ("请计算：19 + 6 = ？直接回复答案数字。", "25"),
        ("请计算：１２ ＋ ３ ＝ ？直接回复答案数字。", "15"),
        ("请计算：6 × 7 = ？直接回复答案数字。", "42"),
        ("请计算：7 ÷ 2 = ？直接回复答案数字。", "3.5"),
        ("请计算：3.5 + 1.5 = ？直接回复答案数字。", "5"),
        ("请计算：（2 + 3）× 4 = ？直接回复答案数字。", "20"),
    ],
)
def test_math_verify_answer(text, expected):
    from tg_signer.giveaway_parsing import math_verify_answer

    assert math_verify_answer(_msg(text=text)) == expected


def test_solve_math_expression_rejects_non_arithmetic():
    from tg_signer.giveaway_parsing import solve_math_expression

    assert solve_math_expression("18 + 9") == "27"
    assert solve_math_expression("-5 + 2") == "-3"
    # 只允许四则运算：函数调用/变量/幂运算一律拒绝
    assert solve_math_expression("__import__('os')") is None
    assert solve_math_expression("2 ** 8") is None
    assert solve_math_expression("abc") is None
    assert solve_math_expression("") is None


def test_handle_math_verification_sends_answer(tmp_path, monkeypatch):
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = FakeClient(bot_users={"MianfeichoujiangBot"})
    handled = asyncio.run(
        engine._handle_math_verification(
            "acc",
            client,
            "mianfeichoujiang_bot",
            [_msg(id=99, text=MATH_VERIFY_TEXT)],
        )
    )
    assert handled is True
    assert client.sent == [("mianfeichoujiang_bot", "27")]
    assert any("已回复答案 27" in line for line in logs)


def test_handle_math_verification_dedup_same_message(tmp_path, monkeypatch):
    """同一条验证消息进程内只回复一次答案（重发 start 后旧题不重复答）。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    engine.log = lambda *a, **k: None
    client = FakeClient(bot_users={"MianfeichoujiangBot"})
    message = _msg(id=99, text=MATH_VERIFY_TEXT)
    for _ in range(2):
        asyncio.run(
            engine._handle_math_verification(
                "acc", client, "mianfeichoujiang_bot", [message]
            )
        )
    assert client.sent == [("mianfeichoujiang_bot", "27")]


class _MathVerifyClient(FakeClient):
    """send_message/start_bot 后按脚本切换历史，模拟 bot 的验证回复。"""

    def __init__(self):
        super().__init__(bot_users={"MianfeichoujiangBot"})
        self.send_replies = []
        self.after_start = None

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        if self.send_replies:
            self.history = list(self.send_replies.pop(0))
        return SimpleNamespace(id=1)

    async def start_bot(self, username, param):
        self.started.append((username, param))
        if self.after_start is not None:
            self.history = [self.after_start]
        return None


def test_handle_math_verification_expired_restarts_and_answers_new(
    tmp_path, monkeypatch
):
    """答题后 bot 回「验证过期」：重发 start 换新题并再答一次。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _nosleep(*a, **k):
        pass

    monkeypatch.setattr(gc.asyncio, "sleep", _nosleep)
    engine, _state = _engine(tmp_path)
    logs: list = []
    engine.log = logs.append
    client = _MathVerifyClient()
    client.send_replies = [
        [_msg(id=101, text="验证过期，请重新参与")],
        [_msg(id=103, text="验证成功，点击下方按钮参与抽奖")],
    ]
    client.after_start = _msg(id=102, text="请计算：19 + 6 = ？直接回复答案数字。")
    handled = asyncio.run(
        engine._handle_math_verification(
            "acc",
            client,
            "mianfeichoujiang_bot",
            [_msg(id=99, text=MATH_VERIFY_TEXT)],
            "p1",
        )
    )
    assert handled is True
    assert client.started == [("mianfeichoujiang_bot", "p1")]
    assert client.sent == [
        ("mianfeichoujiang_bot", "27"),
        ("mianfeichoujiang_bot", "25"),
    ]
    assert any("验证已过期" in line for line in logs)


def test_timeout_client_history_iteration_allows_api_calls_inside_loop():
    """回归：历史迭代循环体内再发同账号 API 不能死锁。

    历史扫描在 async for 循环体里调用 get_users/start_bot 等 API。若迭代器
    整段持有账号锁，循环体内的请求会等待自己未释放的锁而永久挂起，表现为
    日志停在「命中」之后再无输出（等锁不触发网络超时）。
    """
    import asyncio

    from tg_signer.giveaway_core import _TimeoutClient

    class _HistoryClient:
        def get_chat_history(self, chat, limit=None, max_id=0):
            return self._history()

        async def _history(self):
            for i in range(3):
                yield SimpleNamespace(id=i + 1)

        async def get_users(self, username):
            return SimpleNamespace(is_bot=True)

    async def _main():
        client = _TimeoutClient(_HistoryClient(), account="test-lock-reentrant-history")
        seen = []
        async for message in client.get_chat_history("chat"):
            seen.append(message.id)
            # 修复前会在这里永久等待账号锁
            await client.get_users("SomeBot")
        return seen

    seen = asyncio.run(asyncio.wait_for(_main(), timeout=5))
    assert seen == [1, 2, 3]


def test_extract_giveaway_title_accepts_party_emoji():
    """天天免费抽奖 的帖子标题以 🎉 开头（不是 🎰），也应能取到标题。"""
    from tg_signer.giveaway_parsing import extract_giveaway_title

    msg = _msg(
        text="🎉 秋季校园招生福利抽奖大放送（L1792657025976c0958d1b）\n\n📝 抽奖说明："
    )
    assert extract_giveaway_title(msg).startswith("秋季校园招生福利抽奖大放送")
    assert extract_giveaway_title(_msg(text="🎰 周末福利")) == "周末福利"
    assert extract_giveaway_title(_msg(text="普通消息")) == ""


def test_win_sweep_paces_between_bots(tmp_path, monkeypatch):
    """回扫相邻 bot 之间要留间隔，避免单账号连发 GetHistory 触发 429。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    sleeps: list = []

    async def _fake_sleep(sec, *a, **k):
        sleeps.append(sec)

    monkeypatch.setattr(gc.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(gc.random, "uniform", lambda lo, hi: hi)
    engine, _state = _engine(tmp_path)
    calls: list = []

    async def _fake_sweep(account, client, bot):
        calls.append(bot)

    engine._sweep_bot_messages = _fake_sweep
    asyncio.run(engine._sweep_bot_wins("acc", object(), bots=["b1", "b2", "b3"]))
    assert calls == ["b1", "b2", "b3"]
    # 第一个 bot 前不等待，之后每个 bot 前各等一次
    assert sleeps == [gc.WIN_SWEEP_BOT_GAP_RANGE[1]] * 2


def test_timeout_client_backs_off_and_resets(monkeypatch):
    """连续超时按等级退避，成功一次后回退到 0。

    网络差时 Pyrogram 会把超时请求反复重试，退避是为了压住重试风暴。
    """
    import asyncio

    import tg_signer.giveaway_core as gc

    monkeypatch.setattr(gc, "_LAST_TRANSPORT_FLOOD_AT", 0.0)
    client = gc._TimeoutClient(_TransportFloodClient(), account="acc-timeout-backoff")
    for expected in (1, 2):
        with pytest.raises(RuntimeError):
            asyncio.run(client.start_bot("Bot", "p"))
        assert client._timeout_backoff_idx == expected

    sleeps: list = []

    async def _fake_sleep(sec, *a, **k):
        sleeps.append(sec)

    monkeypatch.setattr(gc.asyncio, "sleep", _fake_sleep)
    asyncio.run(client._wait_timeout_cooldown())
    assert sleeps and 0 < sleeps[0] <= gc._CLIENT_TIMEOUT_BACKOFF[1]

    class _OkClient:
        async def start_bot(self, username, param):
            return "ok"

    ok = gc._TimeoutClient(_OkClient(), account="acc-timeout-backoff")
    ok._timeout_backoff_idx = 2
    assert asyncio.run(ok.start_bot("Bot", "p")) == "ok"
    assert ok._timeout_backoff_idx == 0


def test_global_rate_limiter_interval():
    """0 或负值表示不限制；正值换算为相邻两次调用的最小间隔。"""
    import tg_signer.giveaway_core as gc

    assert gc._GlobalRateLimiter(0).interval == 0
    assert gc._GlobalRateLimiter(-5).interval == 0
    assert gc._GlobalRateLimiter(8).interval == pytest.approx(0.125)


def test_global_rate_limiter_queues_calls():
    """连续预约时按间隔排队：第 2、3 次调用需要等待，不会同时放行。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    async def _run():
        limiter = gc._GlobalRateLimiter(20)  # 间隔 50ms
        await limiter.wait()
        await limiter.wait()
        await limiter.wait()

    started = time.monotonic()
    asyncio.run(_run())
    # 首调用不等待，后两次各等一个间隔
    assert time.monotonic() - started >= 0.09


def test_timeout_client_waits_global_limiter():
    """账号代理的每次 API 调用都要先过全局令牌桶。"""
    import asyncio

    import tg_signer.giveaway_core as gc

    calls = []

    class _Limiter:
        async def wait(self):
            calls.append(1)

    class _Api:
        async def ping(self):
            return "pong"

    client = gc._TimeoutClient(_Api(), account="acc-global", global_limiter=_Limiter())
    assert asyncio.run(client.ping()) == "pong"
    assert len(calls) == 1


def test_engine_global_limiter_defaults(tmp_path):
    import tg_signer.giveaway_core as gc

    engine, _state = _engine(tmp_path)
    assert engine.global_qps == gc.DEFAULT_GLOBAL_QPS
    assert engine.max_concurrent_accounts == gc.DEFAULT_MAX_CONCURRENT_ACCOUNTS
    assert engine._api_limiter.interval == pytest.approx(1 / gc.DEFAULT_GLOBAL_QPS)


def test_engine_start_applies_global_limiter(tmp_path, monkeypatch):
    """任务参数里的全局 QPS / 并发账号数要落到引擎与客户端代理上。"""
    import asyncio

    async def _run():
        engine, _state = _engine(tmp_path)

        async def _fake_run(accounts, auto_scan):
            return None

        monkeypatch.setattr(engine, "_run", _fake_run)
        ok = await engine.start(
            ["acc"],
            [],
            tmp_path,
            lambda _m: None,
            global_qps=3,
            max_concurrent_accounts=2,
        )
        assert ok
        assert engine.global_qps == 3
        assert engine._api_limiter.interval == pytest.approx(1 / 3)
        assert engine._account_slots is not None
        await engine.stop()

    asyncio.run(_run())


def test_account_slots_limit_parallel_accounts(tmp_path):
    """全局账号窗口生效时，同时运行的账号数不超过上限。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    engine.max_concurrent_accounts = 2
    engine._account_slots = asyncio.Semaphore(2)
    active = {"now": 0, "peak": 0}

    async def _job():
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        await asyncio.sleep(0.01)
        active["now"] -= 1

    async def _run():
        await asyncio.gather(*[engine._with_account_slot(_job) for _ in range(6)])

    asyncio.run(_run())
    assert active["peak"] == 2


def test_account_slots_unlimited_when_zero(tmp_path):
    """0=不限制：没有信号量时直接执行，不做并发约束。"""
    import asyncio

    engine, _state = _engine(tmp_path)
    assert engine._account_slots is None
    ran = []

    async def _job(tag):
        ran.append(tag)

    async def _run():
        await asyncio.gather(*[engine._with_account_slot(_job, i) for i in range(3)])

    asyncio.run(_run())
    assert ran == [0, 1, 2]
