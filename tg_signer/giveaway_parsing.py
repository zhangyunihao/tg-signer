"""自动抽奖纯解析层：抽奖帖文本/链接/口令/归属地/中奖消息的识别与提取。

所有函数不依赖网络与引擎状态，便于单测；引擎 giveaway_core 从这里引用。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)



# 命中「确认参与」类按钮的默认关键词（callback 按钮直接点击；URL 按钮仅当
# 链接指向同一 bot 的 start 深度链接时才点击，即重发 start 完成参与）
DEFAULT_CONFIRM_KEYWORDS = (
    "确认",
    "参与",
    "参加",
    "立即",
    "领取",
    "报名",
    "提交",
    "抽奖",
    "签到",
)


DEFAULT_KEYWORDS = ("上海", "现金", "红包")


# 「花费 N 积分兑换」确认提示（如 lotteryBa_bot 积分兑换）：按钮「✅ 确认」
# 虽命中确认关键词，但点击会消耗积分，不属于参与动作——整条消息不作为
# 可确认按钮来源（见 _find_confirm_button 守卫）
SCORE_SPEND_RE = re.compile(r"花费\s*\d+\s*积分")


# 「参与（100 分）」/「消耗 100 积分参与」类按钮文案：点击会扣账号积分，
# 不属于免费参与动作，按钮命中即跳过（见 _find_confirm_button 守卫）
SCORE_BUTTON_RE = re.compile(
    r"(?:花费|消耗|扣|需)\s*\d+\s*(?:积)?分|参与\s*[（(][^)）]*\d+\s*(?:积)?分"
)


# 「加入频道后重试参与」类按钮关键词：bot 明确提示「加入后需再点一次
# 我已加入 重试参与」时，优先点该按钮而不是重发 start——少触发一轮
# 防脚本验证、少消耗一条 start 消息（降低 FLOOD_WAIT 概率）
JOIN_RETRY_BUTTON_KEYWORDS = ("我已加入", "重试参与")




class BotPolicy:
    """单个抽奖 bot 的专属行为开关；未登记的用户名一律使用默认通用策略。"""

    __slots__ = ("blocked", "join_status_checklist", "restart_after_join")

    def __init__(
        self,
        *,
        blocked: bool = False,
        join_status_checklist: bool = False,
        restart_after_join: bool = False,
    ) -> None:
        # 黑名单：链接不作为抽奖链接参与，整条消息跳过（不 start、不加频道）
        self.blocked = blocked
        # 回复把「加入-名称」做成带隐藏链接的 ❌/✅ 清单，需额外解析未加入行
        self.join_status_checklist = join_status_checklist
        # 即使消息里没有加频道要求，也重发一次 start 让 bot 校验参与资格
        self.restart_after_join = restart_after_join




DEFAULT_BOT_POLICY = BotPolicy()


# 按小写用户名登记的 bot 专属策略（新增特判请集中加在这里，勿散落各处）
BOT_POLICIES: Dict[str, BotPolicy] = {
    # 御姐群抽奖 bot：正文链接是推广位、真实参与靠其站内按钮，start 无意义且耗时
    "yujie_feedback_search_bot": BotPolicy(blocked=True),
    # 无加入要求时也需重发 start 才会校验参与资格
    "realsaberbot": BotPolicy(restart_after_join=True),
    "lotterybestbot": BotPolicy(join_status_checklist=True, restart_after_join=True),
}




def bot_policy(username: Any) -> BotPolicy:
    """按 bot 用户名（忽略大小写与 @ 前缀）取专属策略，未登记返回默认策略。"""
    key = str(username or "").strip().lstrip("@").lower()
    return BOT_POLICIES.get(key, DEFAULT_BOT_POLICY)




def bot_is_blocked(username: Any) -> bool:
    return bot_policy(username).blocked



# —— 中奖识别 ——
# bot 私聊中奖消息的默认识别正则（样本来自 @LotteryBestBot：标题行为
# 「💐💐💐 中奖提醒 💐💐💐」，正文有「恭喜您中奖了！」与「🔖 防伪编号：H…」；
# 注意「防伪编号」也会出现在开奖结果公告里，公告由 WIN_ANNOUNCE_RE 排除）
WIN_MESSAGE_RE = re.compile(r"中奖提醒|恭喜.{0,8}中奖|防伪编号")


# 开奖结果公告（bot 群发给全体参与者的「🎉 开奖通知 🎉」，列出的是他人
# 中奖名单与防伪编号）不是个人中奖：含公告特征但无「恭喜…中奖/中奖提醒/
# 中奖人：」等个人化标记时，不按中奖处理（曾把 @shlldqbot 的开奖通知
# 误判为中奖转发进中奖群）
WIN_ANNOUNCE_RE = re.compile(r"开奖通知|开奖结果|中奖名单")


WIN_PERSONAL_RE = re.compile(r"恭喜.{0,8}中奖|中奖提醒|中奖人[：:]")




# URL 主体合法字符（不含空白与非西文字符）：链接紧贴中文/全角标点时在此截断
_URL_BODY = r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*"


# t.me/<username> + 后续路径/查询（用户名 5~32 位，字母/数字/下划线）
_TME_RE = re.compile(
    r"(?:https?://)?t\.me/(?P<name>[A-Za-z0-9_]{5,32})(?P<rest>[/?#]"
    + _URL_BODY
    + r")?",
    re.IGNORECASE,
)


# tg://resolve?domain=<u>&start=<p> 形式
_TG_RESOLVE_RE = re.compile(r"tg://resolve\?", re.IGNORECASE)


_TG_RESOLVE_FULL_RE = re.compile(r"tg://resolve\?" + _URL_BODY, re.IGNORECASE)


# start 参数合法字符：字母数字下划线短横；URL 紧贴中文时取首个非法字符之前部分
_START_PARAM_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


# t.me 频道/群组目标：邀请链接（+hash / joinchat/hash）或公开用户名
_TME_CHANNEL_RE = re.compile(
    r"(?:https?://)?t\.me/(?P<target>\+[A-Za-z0-9_-]{8,64}|joinchat/[A-Za-z0-9_-]{8,64}|[A-Za-z0-9_]{5,32})"
    r"(?P<rest>[/?#]\S*)?",
    re.IGNORECASE,
)


# 文本中的 @username（前后排除仅限 ASCII 词符与 @，避免紧贴中文时被漏掉）
_AT_CHANNEL_RE = re.compile(r"(?<![A-Za-z0-9_@])@([A-Za-z0-9_]{5,32})(?![A-Za-z0-9_])")


# 开奖日期解析：仅在文本含「开奖/公布/抽出/揭晓」提示词时尝试
_DRAW_HINT_RE = re.compile(r"开奖|公布|抽出|揭晓")


# 日期：2026-01-01 / 2026年1月1日 / 1月1日（年份可选）
_DATE_YMD_RE = re.compile(
    r"(?:(?P<y>20\d{2})\s*[-/.年]\s*)?(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*[日号]?"
)


# 时间：20:00 / 20点30分 / 8点
_TIME_HM_RE = re.compile(
    r"(?P<h>\d{1,2})\s*[:点时]\s*(?P<mi>\d{1,2})\s*分?|(?P<h2>\d{1,2})点"
)




def parse_draw_date(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """解析消息文本中的开奖日期；无提示词或解析不出返回 None。

    支持 2026-01-01、2026年1月1日、1月1日（默认当年），可带 20:00 / 20点30分；
    无时间的日期按当天 23:59:59 处理（保守：当天内不视为已开奖）。
    无年份的日期一律按当年理解：过期即视为旧帖跳过（宁可漏不可错参与）。
    提示词可能有多个（如 @choujiangs_bot 说明段「3日后开奖了」在前、
    「开奖时间：2026-09-30 12:00」在后）：逐个提示词位置尝试解析，
    取第一个能解析出的日期；窗口内无法构成合法日期的数字
    （如奖品行「18.8」）跳过继续，不中断整体解析。

    窗口向后取 80 字符（老猫福利榜帖「📅 开奖日期:(UTC+8)⏎2026年10月26日…」
    的前缀「日期:(UTC+8)」+换行恰好吃掉 13 字符，旧版 +20 窗口把
    「10月26日」截成「10月2」误判为已开奖）；窗口因长度上限被截断时，
    贴边（起止顶到窗口边界）的匹配视为残缺日期跳过，不参与判定。
    """
    if not text or not _DRAW_HINT_RE.search(text):
        return None
    now = now or datetime.now()
    for hint in _DRAW_HINT_RE.finditer(text):
        start = max(0, hint.start() - 20)
        end = min(len(text), hint.end() + 80)
        # 窗口是否因 +80 上限被截断（截断时贴边匹配不可信）
        truncated = end < len(text)
        window = text[start:end]
        for dm in _DATE_YMD_RE.finditer(window):
            if truncated and (
                dm.end() == len(window) or (dm.start() == 0 and start > 0)
            ):
                continue  # 贴窗口边界的日期可能被截断，宁可不判不误判
            year = int(dm.group("y")) if dm.group("y") else now.year
            try:
                dt = datetime(year, int(dm.group("m")), int(dm.group("d")))
            except ValueError:
                continue
            tail = window[dm.end() : dm.end() + 12]
            tm = _TIME_HM_RE.search(tail)
            if tm is not None:
                hour = int(tm.group("h") or tm.group("h2") or 0)
                minute = int(tm.group("mi") or 0)
                try:
                    dt = dt.replace(hour=hour, minute=minute)
                except ValueError:
                    dt = dt.replace(hour=23, minute=59, second=59)
            else:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt
    return None




def parse_bot_start_link(url: str) -> Optional[Tuple[str, str]]:
    """解析 bot 深度链接，返回 (用户名(不带@), start参数)；不是则返回 None。

    支持：
      https://t.me/SomeBot?start=abc
      http://t.me/SomeBot?start=abc
      t.me/SomeBot?start=abc
      tg://resolve?domain=SomeBot&start=abc
    不支持 startgroup/startapp（拉群/小程序，语义不同）与路径型链接
    （如 t.me/SomeBot/123 是消息/邀请类链接）。
    """
    if not url or not isinstance(url, str):
        return None
    raw = url.strip()
    if _TG_RESOLVE_RE.search(raw):
        query = parse_qs(urlparse(raw).query)
        domain = (query.get("domain") or [""])[0]
        param = (query.get("start") or [""])[0]
    else:
        m = _TME_RE.search(raw)
        if not m:
            return None
        domain = m.group("name")
        rest = m.group("rest") or ""
        # 路径型（t.me/Bot/xxx、t.me/joinchat/...）不是 start 深度链接
        if rest.startswith("/"):
            return None
        query = parse_qs(urlparse(rest).query) if rest.startswith("?") else {}
        param = (query.get("start") or [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", domain or ""):
        return None
    if not param:
        return None
    mparam = _START_PARAM_RE.match(param)
    if mparam is None:
        return None
    return domain, mparam.group(0)




def _message_text(message: Any) -> str:
    return str(
        getattr(message, "text", None) or getattr(message, "caption", None) or ""
    )




def _giveaway_title(message: Any, limit: int = 30) -> str:
    """截取抽奖帖标题：消息文本压成单行取前 limit 字，空文本返回空串。"""
    text = _message_text(message).strip()
    if not text:
        return ""
    snippet = re.sub(r"\s+", " ", text)
    return snippet if len(snippet) <= limit else snippet[:limit] + "…"




def extract_giveaway_title(message: Any, limit: int = 40) -> str:
    """提取 bot 清单回复中的抽奖标题行（以 🎰 开头），取不到返回空串。"""
    text = _message_text(message)
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line.startswith("🎰"):
            continue
        title = line.lstrip("🎰").strip()
        if title:
            return title if len(title) <= limit else title[:limit] + "…"
    return ""




def _iter_candidate_urls(message: Any) -> Iterable[str]:
    """从消息中枚举可能含 bot 链接的 URL：明文文本 + 隐藏超链接实体 + 内联按钮。"""
    text = _message_text(message)
    if text:
        yield text
    for attr in ("entities", "caption_entities"):
        for ent in getattr(message, attr, None) or []:
            ent_type = str(getattr(ent, "type", ""))
            url = getattr(ent, "url", None)
            # 文本超链接实体（pyrogram: TEXT_LINK；telethon: TEXT_URL）
            if (
                "TEXT_URL" in ent_type.upper() or "TEXT_LINK" in ent_type.upper()
            ) and url:
                yield str(url)
    reply_markup = getattr(message, "reply_markup", None)
    for row in getattr(reply_markup, "inline_keyboard", None) or []:
        for btn in row or []:
            url = getattr(btn, "url", None)
            if url:
                yield str(url)




def extract_bot_links(message: Any) -> List[Tuple[str, str]]:
    """提取消息中全部去重后的 (用户名, start参数)，按出现顺序排列。

    对整段候选文本做全局正则扫描：多个链接紧挨着（仅以中文标点等
    非 URL 字符分隔、无空格）也能逐个取出，不漏掉后面的链接。
    """
    found: List[Tuple[str, str]] = []
    seen: set = set()

    def _push(parsed: Optional[Tuple[str, str]]) -> None:
        if parsed is None:
            return
        key = (parsed[0].lower(), parsed[1])
        if key not in seen:
            seen.add(key)
            found.append(parsed)

    for candidate in _iter_candidate_urls(message):
        # 两种链接一起按出现位置排序，保持消息中的先后顺序
        hits: List[Tuple[int, Tuple[str, str]]] = []
        for m in _TME_RE.finditer(candidate):
            parsed = parse_bot_start_link(m.group(0))
            if parsed is not None:
                hits.append((m.start(), parsed))
        for m in _TG_RESOLVE_FULL_RE.finditer(candidate):
            parsed = parse_bot_start_link(m.group(0))
            if parsed is not None:
                hits.append((m.start(), parsed))
        for _, parsed in sorted(hits, key=lambda x: x[0]):
            _push(parsed)
    return found




def message_matches_keywords(message: Any, keywords: Iterable[str]) -> bool:
    """消息文本是否包含任一关键词；关键词为空表示全部放行。

    匹配时剔除「抽奖说明」段落：该段落是规则描述，常出现与奖品无关的
    关键字（如说明里提到群内每日现金红包），不应据此判定抽奖命中关键词。
    """
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kws:
        return True
    text = strip_rules_section(_message_text(message))
    return any(kw in text for kw in kws)




# 「抽奖说明」段落边界：段头行含「抽奖说明」，段落延续到下一个
# 「行首 emoji + 文字 + 冒号」的段头（如 🔋人品加成 / 💁参与情况 / 📅开奖日期），
# 无后续段头则到文本末尾。
_RULES_HEADER_RE = re.compile(r"抽奖说明")


_SECTION_HEADER_RE = re.compile(r"^[^\w\s\u4e00-\u9fff]+\s*\S*[：:]", re.MULTILINE)




def strip_rules_section(text: str) -> str:
    """剔除抽奖帖「抽奖说明」段落（含段头行），只保留其余内容。"""
    if not text:
        return text
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if not _RULES_HEADER_RE.search(line):
            continue
        end = len(lines)
        for j in range(idx + 1, len(lines)):
            if _SECTION_HEADER_RE.match(lines[j]):
                end = j
                break
        return "\n".join(lines[:idx] + lines[end:])
    return text




# —— 开奖结果公告过滤 ——
# bot 群发给全体参与者的开奖结果公告（如「🎉🎉🎉 开奖通知 🎉🎉🎉」+ 防伪编号 +
# 他人中奖名单）：抽奖已结束，参与无意义。置顶路径不受关键词过滤，且公告帖
# 可能残留参与按钮或客服 start 链接，须在链接提取前显式拦截。仅按剔除
# 「抽奖说明」段后的文本判断，避免活跃帖说明段里提及「开奖通知」被误过滤。
DRAW_ANNOUNCE_RE = re.compile(r"开奖通知|开奖结果|中奖名单")




def message_is_draw_announce(message: Any) -> bool:
    """消息是否为开奖结果公告（非活跃抽奖帖）。"""
    text = strip_rules_section(_message_text(message))
    return bool(text and DRAW_ANNOUNCE_RE.search(text))




# —— 归属地限制过滤 ——
# 消息中出现「归属地 xxx」行（如「归属地 陕西」「归属地 上海,江苏」）视为地区限制抽奖：
# 无该行 = 不限地区；有该行时仅当受限地区包含白名单地区（或写明全国/不限）才参与。
# 允许行首有至多 4 个非中文字符的装饰（emoji、符号等），「归属地」必须位于行首附近，
# 避免句子中间提及「归属地」被误判为限制声明。
REGION_LIMIT_RE = re.compile(
    r"^[^\u4e00-\u9fff\n]{0,4}归属地\s*[:：]?\s*(\S.*)$", re.MULTILINE
)


REGION_SPLIT_RE = re.compile(r"[，,、/|\s]+")


ALLOWED_REGIONS = ("上海",)


REGION_UNLIMITED_WORDS = ("全国", "不限", "所有", "全部")




def parse_region_limits(text: str) -> Optional[List[List[str]]]:
    """提取文本中全部「归属地」行的地区列表；无该行返回 None（不限地区）。"""
    limits: List[List[str]] = []
    for m in REGION_LIMIT_RE.finditer(text or ""):
        regions = [r for r in REGION_SPLIT_RE.split(m.group(1).strip()) if r]
        if regions:
            limits.append(regions)
    return limits or None




def message_region_allowed(message: Any) -> bool:
    """归属地限制过滤：无「归属地」行，或任一行的地区含白名单地区/全国不限时放行。"""
    limits = parse_region_limits(_message_text(message))
    if limits is None:
        return True
    for regions in limits:
        if any(any(w in r for w in REGION_UNLIMITED_WORDS) for r in regions):
            return True
        if any(any(want in r for r in regions) for want in ALLOWED_REGIONS):
            return True
    return False




# —— 发言数要求检测 ——
# 抽奖帖含「发言数大于 N 条」「发言数超过 N 条」等参与条件时，双向账号
# （隐私设置导致无法被 bot/群成员主动私聊核实）无法达标，需自动跳过。
SPEECH_REQUIRE_RE = re.compile(r"发言数?\s*(?:大于等于|不少于|大于|超过|>=|>)\s*\d+")


# 清单/原帖中的发言数任务行，捕获要求条数（「发言数大于 6 条」→ 6）
SPEECH_TASK_RE = re.compile(
    r"发言数?\s*(?:大于等于|不少于|大于|超过|>=|>)\s*(\d+)\s*条?"
)




def message_requires_speech(message: Any) -> bool:
    """消息是否带「发言数」类参与条件。"""
    return bool(SPEECH_REQUIRE_RE.search(_message_text(message) or ""))




# 口令参与条件：「发送口令﹝xx﹞」「发送口令激活」及「口令提示」段落头，
# 出现即代表参与需先发送口令（双向账号无法完成该私聊互动）。
PASSWORD_REQUIRE_RE = re.compile(r"发送口令|口令提示")




def message_requires_password(message: Any) -> bool:
    """消息是否带「发送口令」类参与条件。"""
    return bool(PASSWORD_REQUIRE_RE.search(_message_text(message) or ""))




# —— 口令任务解析 ——
# 「└  🔑 发送口令﹝出击泡芙宝宝﹞」子行归属上方最近的「🎫 加入-名称」行，
# 得到（群名 → 口令文本）对应关系，加入该群组后发送口令完成激活。
# 口令文本支持 ﹝﹞「」『』【】（）() 等括号变体；「发送口令激活」类
# 无括号写法不视为携带口令文本。
PASSWORD_TEXT_RE = re.compile(r"发送口令\s*[﹝「『【（(]\s*(.+?)\s*[﹞」』】）)]")


JOIN_ROW_RE = re.compile(r"加入\s*[-–—:：]\s*(.+?)\s*[❌✅]?\s*$")


# 私有邀请链接（t.me/+hash、t.me/joinchat/hash）：send_message 无法用链接
# 字符串解析 peer（kurigram 会剥掉 + 误走 contacts.ResolveUsername 抛
# USERNAME_INVALID），必须先解析成真实 chat id 再发送
_INVITE_LINK_RE = re.compile(
    r"^(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)$", re.IGNORECASE
)


# send_message 对字符串 peer 的解析类错误：触发「按群名匹配已加入会话」
# 兜底（群改名/收回公开用户名时清单链接已失效，但账号已在真实群组中）
_USERNAME_RESOLVE_ERRORS = (
    "USERNAME_INVALID",
    "USERNAME_NOT_OCCUPIED",
    "CHANNEL_INVALID",
    "PEER_ID_INVALID",
)




def _is_username_resolve_error(exc: BaseException) -> bool:
    msg = str(exc).upper()
    return any(code in msg for code in _USERNAME_RESOLVE_ERRORS)




def extract_password_tasks(message: Any) -> List[Tuple[str, str]]:
    """解析抽奖帖中的口令任务，返回 [(群组名称, 口令文本)]。

    「发送口令﹝xx﹞」子行归属上方最近的「加入-名称」行；多个群组
    各带口令时逐个返回。无口令任务时返回 []。
    """
    text = _message_text(message)
    if not text or "发送口令" not in text:
        return []
    tasks: List[Tuple[str, str]] = []
    seen: set = set()
    current_group = ""
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        jm = JOIN_ROW_RE.search(line)
        if jm and jm.group(1).strip():
            current_group = jm.group(1).strip()
        pm = PASSWORD_TEXT_RE.search(line)
        if pm:
            if "✅" in line:
                # bot 回复清单中口令行标 ✅＝口令已发送过，不重复发
                continue
            password = pm.group(1).strip()
            if current_group and password:
                key = (current_group.lower(), password)
                if key not in seen:
                    seen.add(key)
                    tasks.append((current_group, password))
    return tasks




def extract_speech_tasks(message: Any) -> List[Tuple[str, int]]:
    """解析清单/原帖中的发言数任务，返回 [(群组名称, 要求条数)]。

    「发言数大于 N 条」子行归属上方最近的「(已)加入-名称」行；
    ✅ 行代表已达标跳过。无群组归属的孤立要求不处理。
    """
    text = _message_text(message)
    if not text or "发言" not in text:
        return []
    tasks: List[Tuple[str, int]] = []
    seen: set = set()
    current_group = ""
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        jm = JOIN_ROW_RE.search(line)
        if jm and jm.group(1).strip():
            current_group = jm.group(1).strip()
        sm = SPEECH_TASK_RE.search(line)
        if sm is None or "✅" in line or not current_group:
            continue
        try:
            required = int(sm.group(1))
        except ValueError:
            continue
        key = (current_group.lower(), required)
        if key not in seen:
            seen.add(key)
            tasks.append((current_group, required))
    return tasks




def _tme_join_target(url: str) -> Optional[str]:
    """t.me / tg://resolve 链接 → 可 join 的目标（用户名或私有邀请链接）。

    帖子路径（t.me/x/123）、bot 深度链接、无效域名返回 None。
    """
    url_s = str(url)
    if url_s.lower().startswith("tg://resolve"):
        query = parse_qs(urlparse(url_s).query)
        domain = (query.get("domain") or [""])[0]
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", domain or ""):
            return domain
        return None
    m = _TME_CHANNEL_RE.search(url_s)
    if m is None:
        return None
    target = m.group("target")
    rest = m.group("rest") or ""
    if target.lower().startswith(("+", "joinchat/")):
        return f"https://t.me/{target}"
    if rest not in ("", "/"):
        return None
    return target




def extract_join_name_links(message: Any) -> List[Tuple[str, str]]:
    """解析「加入-名称」隐藏链接行的（名称, 加入目标）对应关系。

    ❌（未加入）/✅（已加入）行均收集——口令群即使已加入也需定位目标
    才能发送口令。仅处理带 TEXT_URL/TEXT_LINK 实体的行；无实体的普通
    文本行（原始帖只有群名没链接）忽略。
    """
    text = _message_text(message)
    if not text or "加入" not in text:
        return []
    bounds: List[Tuple[int, int, str]] = []
    start = 0
    for i, ch in enumerate(text):
        if ch == "\n":
            bounds.append((start, i, text[start:i]))
            start = i + 1
    bounds.append((start, len(text), text[start:]))
    results: List[Tuple[str, str]] = []
    seen: set = set()
    for attr in ("entities", "caption_entities"):
        for ent in getattr(message, attr, None) or []:
            # 隐藏链接实体：Pyrogram 叫 TEXT_LINK，Telethon 叫 TEXT_URL
            ent_type = str(getattr(ent, "type", "")).upper()
            if "TEXT_URL" not in ent_type and "TEXT_LINK" not in ent_type:
                continue
            url = getattr(ent, "url", None)
            if not url:
                continue
            offset = _utf16_to_index(text, int(getattr(ent, "offset", 0) or 0))
            line = next((lt for ls, le, lt in bounds if ls <= offset < le), "")
            if "加入" not in line:
                continue
            jm = JOIN_ROW_RE.search(line.strip())
            if jm is None:
                continue
            name = jm.group(1).strip()
            target = _tme_join_target(str(url))
            if name and target:
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    results.append((name, target))
    return results




# bot 回复「抽奖活动已结束」类文案（活动可能提前结束）＝终态：
# 跳过加入、重发、点按钮等一切后续跟进，避免对已结束的抽奖反复发 start
# 中文与英文文案均覆盖（如 @Lottery8Bot 的 "The event has ended or does not exist"
# 与中文「该活动已经结束或不存在」，「已结束/已经结束」两种变体均命中）
ENDED_REPLY_RE = re.compile(
    r"抽奖活动已(?:经)?结束|活动已(?:经)?结束|抽奖已(?:经)?结束"
    r"|event\s+has\s+ended|giveaway\s+has\s+ended|lottery\s+has\s+ended"
    r"|已过期",
    re.IGNORECASE,
)




def reply_says_ended(message: Any) -> bool:
    """bot 回复是否表明该抽奖已结束（终态）。"""
    return bool(ENDED_REPLY_RE.search(_message_text(message) or ""))




# 点击确认按钮后的弹窗提示（callback answer alert，非新消息）中的终态文案。
# 部分抽奖 bot（如 @choujiangs_bot）抽奖结束后不回复新消息，仅弹窗提示；
# 弹窗为即时短文本，匹配可比消息回复更宽（含「已开奖」），误判风险极低
_POPUP_ENDED_RE = re.compile(
    r"已结束|已开奖|已过期|has\s+ended",
    re.IGNORECASE,
)




# bot 回复「您已成功报名抽奖（未达标β）您还需完成激活任务才能抽奖！」
# 「您已获得抽奖资格（已达标）」等 ＝ 报名已成功：未达标仅剩发言数这类
# 激活任务（❌ 标在「└ 🎟️ 发言数大于 N 条」子行上，非未加入频道），
# 系统按参与成功标记，不再以中性行「点击后回复」呈现
SIGNUP_SUCCESS_RE = re.compile(r"已成功报名|报名成功|已获得抽奖资格")




def reply_says_signup_success(message: Any) -> bool:
    """bot 回复是否表明报名成功（未达标仅剩激活任务时同样算已参与）。"""
    return bool(SIGNUP_SUCCESS_RE.search(_message_text(message) or ""))




# —— 防脚本验证（按顺序点击 emoji，如 @dbchoujiang123_bot）——
# start 后 bot 回复「🤖 防脚本验证\n请按顺序点击：【🎯、🐻、🐴】\n
# 点错会冷却 30 秒。」，需按正文给出的顺序依次点击该消息内联键盘上的
# 对应 emoji 回调按钮；点错有冷却惩罚，解析不出顺序或缺按钮时不乱点。
EMOJI_VERIFY_RE = re.compile(r"防脚本验证|按顺序点击")


# 点击顺序写在【…】内
EMOJI_ORDER_RE = re.compile(r"【([^】]+)】")


# emoji 主体字符（符号区 + 补充符号区）与修饰符（变体选择符/ZWJ/肤色/键帽）
_EMOJI_BASE_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]")


_EMOJI_MOD_RE = re.compile(r"[\uFE0F\u200D\U0001F3FB-\U0001F3FF\u20E3]")




def _extract_emoji_order(text: str) -> List[str]:
    """从「请按顺序点击：【🎯、🐻、🐴】」提取需按序点击的 emoji 列表。

    支持 、，, 空格等分隔，也支持无分隔连写；无【】括号返回空列表。
    """
    if not text:
        return []
    m = EMOJI_ORDER_RE.search(text)
    if not m:
        return []
    order: List[str] = []
    for part in re.split(r"[、，,\s|·•/]+", m.group(1)):
        cur = ""
        for ch in part:
            if _EMOJI_BASE_RE.match(ch):
                # ZWJ 连接的复合 emoji（当前 token 以 ZWJ 结尾）并入 token
                if cur and not cur.endswith("\u200d"):
                    order.append(cur)
                    cur = ""
                cur += ch
            elif cur and _EMOJI_MOD_RE.match(ch):
                cur += ch
        if cur:
            order.append(cur)
    return order




def is_emoji_verify_message(message: Any) -> bool:
    """bot 回复是否为「防脚本验证」（要求按顺序点击 emoji 按钮）。"""
    return bool(EMOJI_VERIFY_RE.search(_message_text(message) or ""))




# —— LotteryBestBot 式回复解析 ——
# start 后不满足条件时 bot 回复「您需加入指定的群组/频道」，逐行列出
# 「🎫 加入-名称 ❌/✅」，名称是带 TEXT_URL 隐藏链接的文本。只提取
# ❌（未加入）行的链接目标，✅ 已加入行跳过；无链接实体的行不处理。
# 哪些 bot 使用该格式由 BOT_POLICIES 的 join_status_checklist 决定。


def _utf16_to_index(text: str, u16_offset: int) -> int:
    """Telegram 实体 offset 按 UTF-16 code units 计，转成 Python 字符索引。

    消息里含 🎫🎰🧸 等 emoji（UTF-16 占 2 位）时，直接用实体 offset 对齐
    Python 行区间会逐行漂移，导致实体被错分到相邻行而丢失加入目标。
    """
    if u16_offset <= 0:
        return 0
    units = 0
    for i, ch in enumerate(text):
        if units >= u16_offset:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)




def extract_join_status_targets(message: Any) -> List[str]:
    """从「加入-名称 ❌」式回复提取隐藏链接加入目标（仅未加入行）。"""
    text = _message_text(message)
    if not text or "加入" not in text:
        return []
    # 行区间表：实体 offset 定位所在行，据此判断该行是否为 ❌ 未加入
    bounds: List[Tuple[int, int, str]] = []
    start = 0
    for i, ch in enumerate(text):
        if ch == "\n":
            bounds.append((start, i, text[start:i]))
            start = i + 1
    bounds.append((start, len(text), text[start:]))
    targets: List[str] = []
    seen: set = set()

    def _add(target: str) -> None:
        key = target.lower()
        if key not in seen:
            seen.add(key)
            targets.append(target)

    for attr in ("entities", "caption_entities"):
        for ent in getattr(message, attr, None) or []:
            # 隐藏链接实体：Pyrogram 叫 TEXT_LINK，Telethon 叫 TEXT_URL
            ent_type = str(getattr(ent, "type", "")).upper()
            if "TEXT_URL" not in ent_type and "TEXT_LINK" not in ent_type:
                continue
            url = getattr(ent, "url", None)
            if not url:
                continue
            url_s = str(url)
            offset = _utf16_to_index(text, int(getattr(ent, "offset", 0) or 0))
            line = next((lt for ls, le, lt in bounds if ls <= offset < le), "")
            if "加入" not in line or "❌" not in line or "✅" in line:
                continue
            target = _tme_join_target(url_s)
            if target is not None:
                _add(target)
    return targets




def extract_channel_targets(message: Any) -> List[str]:
    """从消息文本/实体/按钮中提取频道与群组加入目标（激进模式）。

    返回可供 join_chat 的目标列表：
      - 公开频道/群组用户名（"SomeChannel"，不带 @）
      - 私有邀请链接（"https://t.me/+hash" 或 "https://t.me/joinchat/hash"）
    排除：start/startgroup/startapp 深度链接、帖子路径链接（t.me/x/123）。
    """
    targets: List[str] = []
    seen: set = set()

    def _add(item: str) -> None:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            targets.append(item)

    for candidate in _iter_candidate_urls(message):
        if candidate.lower().startswith("tg://resolve"):
            # tg://resolve?domain=xxx 形态的隐藏链接
            query = parse_qs(urlparse(candidate).query)
            domain = (query.get("domain") or [""])[0]
            if re.fullmatch(r"[A-Za-z0-9_]{5,32}", domain or ""):
                _add(domain)
            continue
        for match in re.finditer(r"\S+", candidate):
            piece = match.group(0)
            m = _TME_CHANNEL_RE.search(piece)
            if m is not None:
                target = m.group("target")
                rest = m.group("rest") or ""
                if target.lower().startswith(("+", "joinchat/")):
                    # 私有邀请链接
                    _add(f"https://t.me/{target}")
                    continue
                if rest not in ("", "/") and not rest.startswith("?"):
                    continue  # 帖子/消息路径链接（t.me/x/123），不能 join
                if rest.startswith("?"):
                    query = parse_qs(urlparse(rest).query)
                    if any(k in query for k in ("start", "startgroup", "startapp")):
                        continue  # bot 深度链接，不属于加入目标
                _add(target)
            for am in _AT_CHANNEL_RE.finditer(piece):
                _add(am.group(1))
    return targets
