"""积分兑换确认拦截测试：「花费 N 积分」提示的确认按钮不得被点击。"""

from tg_signer.giveaway_core import GiveawayEngine


class _Btn:
    def __init__(self, text: str, callback_data: str = None, url: str = None):
        self.text = text
        self.callback_data = callback_data
        self.url = url


class _Markup:
    def __init__(self, rows):
        self.inline_keyboard = rows


class _Msg:
    def __init__(self, text: str, rows):
        self.text = text
        self.id = 1
        self.entities = []
        self.caption_entities = []
        self.reply_markup = _Markup(rows)
        self.chat = None


def _engine() -> GiveawayEngine:
    engine = GiveawayEngine(state=None)
    engine.confirm_wait = 0
    return engine


def test_score_exchange_confirm_blocked():
    """lotteryBa_bot 积分兑换确认提示（花费 20 积分）：不返回任何可点按钮。"""
    engine = _engine()
    msg = _Msg(
        "✅ 检测到您已满足条件！\n\n确定要花费 20 积分兑换该优惠券吗？",
        [
            [_Btn("❌ 取消", callback_data="/score_exchange?id=918&t=0")],
            [_Btn("✅ 确认", callback_data="/score_exchange?id=918&t=1")],
        ],
    )
    assert engine._find_confirm_button(msg) is None


def test_score_exchange_variant_no_space():
    """无空格变体「花费20积分」同样拦截。"""
    engine = _engine()
    msg = _Msg(
        "确定要花费20积分兑换该优惠券吗？",
        [[_Btn("✅ 确认", callback_data="/score_exchange?id=1&t=1")]],
    )
    assert engine._find_confirm_button(msg) is None


def test_signup_confirm_still_clickable():
    """普通报名按钮（满足所有条件，点此报名）不受守卫影响。"""
    engine = _engine()
    msg = _Msg(
        "🎁抽奖获取喜茶 - 红包券 | 福利频道\n\n📮 参与条件:\n🎫 加入- 上海【喜茶】茶友群",
        [[_Btn("✅ 满足所有条件，👉 点此报名", callback_data="/check?id=924")]],
    )
    btn = engine._find_confirm_button(msg)
    assert btn is not None and btn.callback_data == "/check?id=924"


def test_activity_post_with_score_mention_still_clickable():
    """活动帖含「需要积分 - 20」但无「花费 N 积分」确认语：按钮仍可点。"""
    engine = _engine()
    msg = _Msg(
        "🎁喜茶积分兑换 | 福利频道\n\n🎫 需要积分 - 20",
        [[_Btn("✅ 满足所有条件，👉 点此报名", callback_data="/check?id=918")]],
    )
    assert engine._find_confirm_button(msg) is not None
