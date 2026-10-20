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
    message_region_allowed,
    message_requires_speech,
    parse_bot_start_link,
    parse_draw_date,
    parse_region_limits,
    strip_rules_section,
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

    def get_chat_history(self, chat, limit=None, offset_id=0):
        return self._history(limit, offset_id)

    async def _history(self, limit=None, offset_id=0):
        items = self.history
        if offset_id:
            items = [m for m in items if getattr(m, "id", 0) < offset_id]
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


def test_engine_ignore_history_allows_repeat(tmp_path):
    """忽略历史成功记录：关闭时已参与过的链接去重跳过，开启后允许重新参与。"""
    import asyncio

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


def test_rescue_group_rechecks_pinned_only(tmp_path):
    """群组补漏只重查置顶消息（新置顶参与、历史不扫、旧置顶去重）。"""
    import asyncio

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


def test_ended_reply_breaks_bot_after_three_strikes(tmp_path):
    """同一 bot 累计 3 次回复「已结束」：本轮熔断，后续新链接静默跳过。"""
    import asyncio

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
    joined_any = asyncio.run(
        engine._join_from_message("acc", client, "LotteryBot", msg)
    )
    assert joined_any is True
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
    joined_any = asyncio.run(
        engine._join_from_message("acc", client, "LotteryBot", msg2)
    )
    assert client.calls == ["ChanA"]
    assert joined_any is False
    assert any("自动加入被限流暂停中" in line for line in logs)
    # 暂停恢复后正常加入
    engine._join_suspended_until = None
    asyncio.run(engine._join_from_message("acc", client, "LotteryBot", msg2))
    assert client.calls == ["ChanA", "ChanC", "ChanD"]


# ---------------- 历史补漏（rescue） ----------------


def _msgid(message, mid):
    message.id = mid
    return message


def test_rescue_round_dedups_seen_and_joins_new(tmp_path):
    """补漏只处理水位线之外的新消息，重复轮次不重复参与。"""
    import asyncio

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


def test_rescue_loop_skips_while_suspended_then_recovers(tmp_path):
    """熔断暂停期间补漏循环不扫描，到期后自动恢复。"""
    import asyncio
    from datetime import datetime, timedelta

    engine, _state = _engine(tmp_path)
    engine.chats = [123]
    client = FakeClient(bot_users={"LotteryBot"})
    client.history = [_msgid(_msg(text="抽奖 https://t.me/LotteryBot?start=r1"), 5)]
    engine._clients = {"acc": client}
    engine.rescue_interval = 0.01
    engine._stop = asyncio.Event()

    async def scenario():
        engine._rescue_suspended_until = datetime.now() + timedelta(seconds=1)
        task = asyncio.create_task(engine._rescue_loop())
        await asyncio.sleep(0.1)
        assert client.started == []  # 暂停期间不扫描
        engine._rescue_suspended_until = datetime.now() - timedelta(seconds=1)
        await asyncio.sleep(0.05)
        assert ("LotteryBot", "r1") in client.started  # 恢复后正常补漏
        engine._stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


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
    from tg_signer.giveaway_core import extract_join_status_targets

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
    from tg_signer.giveaway_core import extract_join_status_targets

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

    from tg_signer.giveaway_core import extract_join_status_targets

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

    from tg_signer.giveaway_core import extract_join_status_targets

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
    from tg_signer.giveaway_core import extract_join_status_targets

    msg = _status_message(
        SPEECH_TASK_REPLY,
        {"已加入-某修车补习班免费上榜": "https://t.me/some_group"},
    )
    # 已加入行（无 ❌）与发言任务子行（无「加入」）均不算未加入频道行
    assert extract_join_status_targets(msg) == []


def test_reply_says_signup_success():
    """「已成功报名抽奖（未达标β）」「已获得抽奖资格」＝报名成功（已参与，
    未达标仅剩发言数这类激活任务，系统标记为完成）。"""
    from tg_signer.giveaway_core import reply_says_signup_success

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
    from tg_signer.giveaway_core import reply_says_ended

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
    from tg_signer.giveaway_core import message_requires_password

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
    from tg_signer.giveaway_core import extract_password_tasks

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
    from tg_signer.giveaway_core import extract_join_name_links

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
    from tg_signer.giveaway_core import extract_password_tasks

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
    from tg_signer.giveaway_core import WIN_MESSAGE_RE

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


def test_state_bots_for_orders_by_recency(tmp_path):
    """bots_for：按最近交互时间倒序返回账号参与过的 bot，大小写去重。"""
    state = GiveawayState(tmp_path)
    state.add("acc", "BotAAA", "p1")
    state.add("acc", "BotBBB", "p2")
    state.add("acc", "BotAAA", "p3")
    # 直接改 ts 模拟时间先后：p3(AAA) 最旧，BBB 居中，p1(AAA) 最新
    state._data["acc"]["botaaa|p3"]["ts"] = "2026-10-01T10:00:00"
    state._data["acc"]["botbbb|p2"]["ts"] = "2026-10-03T10:00:00"
    state._data["acc"]["botaaa|p1"]["ts"] = "2026-10-05T10:00:00"
    assert state.bots_for("acc") == ["BotAAA", "BotBBB"]
    assert state.bots_for("other") == []


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


# ---------------- 防脚本验证（按顺序点击 emoji） ----------------

DBCHOUJIANG_VERIFY_TEXT = (
    "🤖 防脚本验证\n请按顺序点击：【🎯、🐻、🐴】\n点错会冷却 30 秒。"
)


def test_extract_emoji_order_samples():
    from tg_signer.giveaway_core import _extract_emoji_order

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
    from tg_signer.giveaway_core import is_emoji_verify_message

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
