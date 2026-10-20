"""自动抽奖核心：识别频道消息中的 t.me/<bot>?start=<参数> 链接并自动参与。

典型链路：频道抽奖帖 -> 消息文本/隐藏超链接/内联按钮中的 bot 深度链接 ->
调用 messages.StartBot（等价于点开链接）-> 机器人若回复带「确认参与」类
回调按钮，再自动点击一次。

纯解析函数不依赖网络，便于单测；GiveawayEngine 负责连接、
历史扫描、去重与参与。

触发方式：启动时可选自动扫描最近消息 + 手动扫描 + 周期性历史补漏
（重拉最近消息捞回漏网之鱼，含连续失败熔断暂停）；同一引擎常驻连接，
便于随时发起扫描与参与。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from tg_signer.core import get_client, safe_disconnect

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
# 「加入频道后重试参与」类按钮关键词：bot 明确提示「加入后需再点一次
# 我已加入 重试参与」时，优先点该按钮而不是重发 start——少触发一轮
# 防脚本验证、少消耗一条 start 消息（降低 FLOOD_WAIT 概率）
JOIN_RETRY_BUTTON_KEYWORDS = ("我已加入", "重试参与")
STATE_FILE = "giveaway_state.json"
# 每个账号最多保留的去重记录数，超出丢弃最旧的
MAX_STATE_PER_ACCOUNT = 1000
# 同一 bot 连续 FLOOD_WAIT 达到该次数后，本轮运行熔断（不再对其 start_bot）
MAX_FLOOD_STRIKES = 3
# 同一 bot 累计收到该次数「抽奖已结束」类回复后，本轮运行熔断该 bot
# （如 @Lottery8Bot 对所有 start 一律回复 "The event has ended or does not exist"）
MAX_ENDED_STRIKES = 3

# 抽奖 bot 黑名单（小写用户名）：消息中的这些 bot 链接不作为抽奖链接参与，
# 整条消息直接跳过（不 start、不加频道、不留日志）。
# 如御姐群抽奖 bot：正文链接是推广位、真实参与靠其站内按钮，start 流程无意义且耗时
BLOCKED_BOTS = frozenset({"yujie_feedback_search_bot"})
# 强制重参与名单：即使消息里没有加频道要求也重发 start 的 bot（小写用户名）
BOTS_RESTART_AFTER_JOIN = frozenset({"realsaberbot", "lotterybestbot"})
# FLOOD_WAIT 失败记录的冷却时间（秒），冷却后允许重试
FLOOD_COOLDOWN = 3600
# 扫描停止条件：连续这么多条「已开奖」的抽奖帖后，认为再往前都是旧抽奖
EXPIRED_STOP_STREAK = 5
# 单频道单次扫描的硬上限（条），防止无开奖日期标注时无限回溯
SCAN_HARD_CAP = 1000

# —— 历史补漏 ——
# 周期性重拉各频道最近若干条消息，捞回此前扫描失败/中断漏掉的抽奖帖
RESCUE_INTERVAL = 600  # 补漏扫描间隔（秒），0 = 关闭
RESCUE_LIMIT = 10  # 每次补漏每频道重拉的消息条数
RESCUE_MAX_FAILS = 3  # 连续失败达该轮数后熔断暂停补漏
RESCUE_RESUME_COOLDOWN = 1800  # 熔断后的自动恢复等待（秒）
RESCUE_FETCH_TIMEOUT = 60  # 单频道补漏拉取消息的超时（秒）
SEEN_CAP_PER_CHAT = 500  # 每频道保留的已处理消息 id 上限

# —— 中奖识别 ——
# bot 私聊中奖消息的默认识别正则（样本来自 @LotteryBestBot：标题行为
# 「💐💐💐 中奖提醒 💐💐💐」，正文有「恭喜您中奖了！」与「🔖 防伪编号：H…」；
# 三个特征均只出现在中奖消息里，参与清单/抽奖说明类回复不会误命中）
WIN_MESSAGE_RE = re.compile(r"中奖提醒|恭喜.{0,8}中奖|防伪编号")
# 首次记录某 bot 时的回看消息条数（初始基线）；之后按水位线增量扫描，
# 只处理比上次扫到的消息 id 更新的消息，该值不再参与日常回扫
WIN_SWEEP_LIMIT = 50
WIN_SWEEP_PAGE = 100  # 增量回扫单页拉取的消息条数
WIN_SWEEP_PAGE_MAX = 20  # 增量回扫最多翻页数（防水位线异常时无限翻页）

# —— 网络调用统一超时 ——
# 半开连接（手机切换网络/代理僵死）下 Pyrogram 请求可能永久挂起，导致引擎
# 任务协程卡死、WebUI 状态永不收敛。所有经引擎客户端发出的请求统一加超时：
# 单次请求超过 NET_CALL_TIMEOUT 秒按网络异常抛错，由各调用点的既有异常处理兜底。
NET_CALL_TIMEOUT = 90


async def _timeout_aiter(agen: Any, timeout: float, name: str = "") -> Any:
    """包装异步迭代器（如 get_chat_history）：每一批拉取限时，防遍历永久卡住。"""
    while True:
        try:
            item = await asyncio.wait_for(agen.__anext__(), timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"网络请求 {name or '消息拉取'} 超时（{timeout:.0f} 秒，网络异常）"
            ) from None
        yield item


class _TimeoutClient:
    """Pyrogram 客户端代理：协程调用与异步迭代统一套 NET_CALL_TIMEOUT 超时。"""

    def __init__(self, client: Any, timeout: float = NET_CALL_TIMEOUT) -> None:
        self._raw = client
        self._timeout = timeout

    def __getattr__(self, item: str) -> Any:
        attr = getattr(self._raw, item)
        if not callable(attr):
            return attr

        def method(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            if hasattr(result, "__anext__"):
                return _timeout_aiter(result, self._timeout, item)
            if inspect.isawaitable(result):
                return self._bounded(result, item)
            return result

        return method

    async def _bounded(self, coro: Any, name: str) -> Any:
        try:
            return await asyncio.wait_for(coro, self._timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"网络请求 {name} 超时（{self._timeout:.0f} 秒，网络异常）"
            ) from None


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
LOTTERYBEST_BOT = "lotterybestbot"


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


class GiveawayState:
    """按账号持久化已参与记录：{accounts: {account: {"bot|param": record}}}。

    另持久化中奖回扫水位线 {win_watermarks: {account: {bot_lower: msg_id}}}。
    """

    def __init__(self, workdir) -> None:
        self.path = Path(workdir) / STATE_FILE
        self._data: Dict[str, Dict[str, dict]] = {}
        self._win_wm: Dict[str, Dict[str, int]] = {}
        self._dirty = False
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            accounts = data.get("accounts")
            if isinstance(accounts, dict):
                self._data = {
                    str(k): dict(v) for k, v in accounts.items() if isinstance(v, dict)
                }
            wm = data.get("win_watermarks")
            if isinstance(wm, dict):
                parsed: Dict[str, Dict[str, int]] = {}
                for acc, sub in wm.items():
                    if not isinstance(sub, dict):
                        continue
                    bot_map: Dict[str, int] = {}
                    for bot, mid in sub.items():
                        try:
                            bot_map[str(bot)] = int(mid)
                        except (TypeError, ValueError):
                            continue
                    if bot_map:
                        parsed[str(acc)] = bot_map
                self._win_wm = parsed
        except (OSError, json.JSONDecodeError):
            self._data = {}
            self._win_wm = {}

    def _merge_from_disk(self) -> None:
        """落盘前重读磁盘并合并，防止并发覆盖（多任务并发运行时各自持有
        独立的 GiveawayState 实例，save 整文件写入会让旧快照冲掉他处
        新写入的参与/去重记录）：

        - 本地没有的账号/键 → 从磁盘补进来
        - 同键都存在 → 保留 ts 较新的一条
        """
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            return
        for account, bucket in accounts.items():
            if not isinstance(bucket, dict):
                continue
            local = self._data.setdefault(str(account), {})
            for key, rec in bucket.items():
                if not isinstance(rec, dict):
                    continue
                cur = local.get(key)
                if cur is None or str(rec.get("ts") or "") > str(cur.get("ts") or ""):
                    local[key] = rec

    def save(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        self._merge_from_disk()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"accounts": self._data, "win_watermarks": self._win_wm},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        self._dirty = False

    @staticmethod
    def _key(username: str, param: str) -> str:
        return f"{username.lower()}|{param}"

    def has(self, account: str, username: str, param: str) -> bool:
        """已真正参与（非 flood_wait 失败记录）才视为重复"""
        rec = self._data.get(account, {}).get(self._key(username, param))
        if rec is None:
            return False
        return not str(rec.get("note") or "").startswith("flood_wait")

    def is_ended(self, account: str, username: str, param: str) -> bool:
        """bot 曾回复「已结束」＝该链接终态，无论是否忽略历史都不再重试"""
        rec = self._data.get(account, {}).get(self._key(username, param))
        return rec is not None and str(rec.get("note") or "") == "ended"

    def is_blocked(
        self, account: str, username: str, param: str, cooldown: int = FLOOD_COOLDOWN
    ) -> bool:
        """flood_wait 失败记录是否仍在冷却期内（冷却后允许重试）"""
        rec = self._data.get(account, {}).get(self._key(username, param))
        if rec is None or not str(rec.get("note") or "").startswith("flood_wait"):
            return False
        try:
            ts = datetime.fromisoformat(str(rec.get("ts") or ""))
        except ValueError:
            return False
        return (datetime.now() - ts).total_seconds() < cooldown

    def add(
        self,
        account: str,
        username: str,
        param: str,
        *,
        channel: str = "",
        note: str = "joined",
    ) -> None:
        bucket = self._data.setdefault(account, {})
        bucket[self._key(username, param)] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "bot": username,
            "param": param,
            "channel": channel,
            "note": note,
        }
        if len(bucket) > MAX_STATE_PER_ACCOUNT:
            # 按时间戳丢弃最旧的若干条
            for old_key in sorted(bucket, key=lambda k: str(bucket[k].get("ts") or ""))[
                : len(bucket) - MAX_STATE_PER_ACCOUNT
            ]:
                bucket.pop(old_key, None)
        self._dirty = True

    def count(self, account: Optional[str] = None) -> int:
        if account is not None:
            return len(self._data.get(account, {}))
        return sum(len(v) for v in self._data.values())

    def clear(self) -> int:
        """清空所有账号的已参与记录（含中奖消息去重与回扫水位线）并立即落盘。

        返回清除的参与记录条数。win: 去重记录存于 _data 一并清除；
        回扫水位线（每账号 × bot 扫到的位置）同步重置，下轮回扫从
        新基线（回扫条数）重建。
        """
        total = self.count()
        self._data = {}
        self._win_wm = {}
        self._dirty = True
        # save 会从磁盘合并回来，先删文件确保清空真正生效
        try:
            self.path.unlink()
        except OSError:
            pass
        self.save(force=True)
        return total

    def bots_for(self, account: str) -> List[str]:
        """该账号有过交互记录的全部 bot 用户名（按最近交互时间倒序）。

        中奖回扫用：账号参与过的 bot 才可能发来中奖私信。
        """
        bucket = self._data.get(account, {})
        latest: Dict[str, tuple] = {}  # lower -> (ts, 原始用户名)
        for key, rec in bucket.items():
            bot = str((rec or {}).get("bot") or key.split("|", 1)[0]).strip()
            if not bot:
                continue
            lower = bot.lower()
            ts = str((rec or {}).get("ts") or "")
            if lower not in latest or ts > latest[lower][0]:
                latest[lower] = (ts, bot)
        return [
            bot
            for _, bot in sorted(
                latest.values(), key=lambda pair: pair[0], reverse=True
            )
        ]

    def win_watermark(self, account: str, bot: str) -> Optional[int]:
        """该账号 × bot 上次中奖回扫扫到的消息 id，从未扫过返回 None。"""
        return self._win_wm.get(account, {}).get(bot.lower())

    def set_win_watermark(self, account: str, bot: str, msg_id: int) -> None:
        """推进中奖回扫水位线到 msg_id（仅前进不后退）。"""
        bot_map = self._win_wm.setdefault(account, {})
        old = bot_map.get(bot.lower())
        if old is not None and int(msg_id) <= old:
            return
        bot_map[bot.lower()] = int(msg_id)
        self._dirty = True


class GiveawayEngine:
    """自动抽奖后台引擎：每账号一个常驻连接，手动/启动时历史扫描并自动参与。"""

    CONNECT_TIMEOUT = 30

    def __init__(self, state: Optional[GiveawayState] = None) -> None:
        self._state = state
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._stopping = False
        self._clients: Dict[str, Any] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        # bot 身份内存缓存：{account|username_lower: True/False}，避免反复 resolve
        self._bot_cache: Dict[str, bool] = {}
        # 本次运行已处理过的加入目标：{account|target_lower}，避免重复 join
        self._join_cache: set = set()
        # 已发送过口令的群组：{account|bot|群名_lower}，重复参与不重复发言
        self._password_sent: set = set()
        # 已处理过点击的防脚本验证消息：{account|bot|msg_id}，进程内去重
        # （重发 start 后旧验证消息仍在候选里，避免误点过期验证按钮）
        self._emoji_verify_handled: set = set()
        # 自动加入频道的限流暂停（JoinChannel FLOOD_WAIT 后暂停一段时间再恢复）
        self._join_suspended_until: Optional[datetime] = None
        self._join_suspend_logged = False
        # FLOOD_WAIT 熔断：{account|bot_lower: 连续次数}；达到上限加入 _flood_bots
        self._flood_counts: Dict[str, int] = {}
        self._flood_bots: set = set()
        # 「已结束」回复熔断：{account|bot_lower: 次数}；达到上限加入 _ended_bots
        self._ended_counts: Dict[str, int] = {}
        self._ended_bots: set = set()
        # 跳过提示去重（已开奖消息/参与上限），避免每轮扫描重复刷日志
        self._skip_logged: set = set()
        # 历史补漏：已处理消息 id 水位线 {account|chat_key: {msg_id: None}（保序）}
        self._seen_msgs: Dict[str, Dict[int, None]] = {}
        # 补漏连续失败轮数与熔断恢复时间
        self._rescue_fail_streak = 0
        self._rescue_suspended_until: Optional[datetime] = None
        # 聊天类型缓存 {str(chat): 是否群组}：群组只扫置顶，频道才全量扫描
        self._chat_types: Dict[str, bool] = {}
        # 监控对象 chat_id -> @username 映射（来自用户缓存）：数字 id 无法加入时兜底
        self.chat_usernames: Dict[str, str] = {}
        # 加入目标的类型缓存 {target_lower: 类型名/None}：过滤个人用户与失效目标
        self._join_target_types: Dict[str, Optional[str]] = {}
        # 忽略历史成功记录（start 时可覆盖）：默认 True＝已参与过的链接允许重新参与
        self.ignore_history = True
        # 中奖识别：bot 私聊消息命中关键词时转发提醒（默认开启，转发到收藏夹）
        self.win_detect = True
        self.win_forward_targets: List[Any] = ["me"]
        # 中奖回扫条数：每次回扫各 bot 私聊最近多少条消息找中奖通知
        self.win_sweep_limit = WIN_SWEEP_LIMIT
        # 已处理的中奖消息：{account|bot|msg_id}，进程内去重（跨重启靠状态文件）
        self._win_handled: set = set()
        # 标记为「双向」的账户集合：发言数要求类抽奖自动跳过
        self.two_way_accounts: set = set()
        self.chats: List = []
        self.keywords: Tuple[str, ...] = DEFAULT_KEYWORDS
        self.max_joins: Optional[int] = None
        self.rescue_interval = RESCUE_INTERVAL
        self.rescue_limit = RESCUE_LIMIT
        self.confirm_keywords: Tuple[str, ...] = DEFAULT_CONFIRM_KEYWORDS
        self.confirm_wait = 8
        self.delay_range = (3.0, 10.0)
        self.auto_join = True
        self.session_dir: Optional[Path] = None
        self.log: Callable[[str], None] = lambda _m: None
        self.on_event: Optional[Callable[[dict], None]] = None
        # 本轮运行统计
        self.joined = 0
        # 本轮各账号新参与数：{account: count}，供明细展示
        self.joined_by_account: Dict[str, int] = {}
        # 本轮各账号按来源频道/群组的新参与数：{account: {chat标题: count}}
        self.joined_by_account_chat: Dict[str, Dict[str, int]] = {}
        self.failed = 0
        self.scanning = False
        self.scan_text = ""
        # 多账号并行：参与条数上限用同步名额预留保证不超额（_participate）；
        # 不对 bot 请求加串行锁——各账号与 bot 独立私聊、Telegram 限流按账号独立，
        # 请求错峰靠随机延迟实现

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def progress_text(self) -> Optional[str]:
        if not self.running:
            return None
        if self._stopping:
            return "停止中"
        if self.scanning:
            return f"扫描中 {self.scan_text}".strip()
        if self.rescue_interval > 0:
            # 补漏开启时任务会常驻待命，明确展示等待语义，避免被误读为卡死
            mins = max(1, round(self.rescue_interval / 60))
            return f"常驻待命中（每{mins}分钟补漏扫描，已参与{self.joined}）"
        return f"运行中（已参与{self.joined}）"

    async def start(
        self,
        accounts: List[str],
        chats: List,
        session_dir: Path,
        log: Callable[[str], None],
        *,
        keywords: Optional[Iterable[str]] = None,
        auto_scan: bool = True,
        auto_join: bool = True,
        max_joins: Optional[int] = None,
        rescue_interval: Optional[int] = None,
        rescue_limit: Optional[int] = None,
        delay_range: Tuple[float, float] = (3.0, 10.0),
        confirm_wait: int = 8,
        confirm_keywords: Optional[Iterable[str]] = None,
        ignore_history: bool = True,
        win_detect: bool = True,
        win_forward_target=None,
        win_sweep_limit: Optional[int] = None,
        two_way_accounts: Optional[Iterable[str]] = None,
        chat_usernames: Optional[Dict[str, str]] = None,
        state: Optional[GiveawayState] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        on_finish: Optional[Callable[[], None]] = None,
    ) -> bool:
        if self.running:
            log("任务已在运行中")
            return False
        self._state = state or self._state
        self.chats = list(chats)
        self.keywords = tuple(
            str(k).strip() for k in (keywords or ()) if str(k).strip()
        )
        self.confirm_keywords = tuple(confirm_keywords or DEFAULT_CONFIRM_KEYWORDS)
        self.confirm_wait = max(0, int(confirm_wait))
        self.auto_join = bool(auto_join)
        # 忽略历史成功记录：开启后已参与过的抽奖链接允许重新参与
        self.ignore_history = bool(ignore_history)
        # 中奖识别配置：转发目标列表，"me" = 收藏夹（Saved Messages），
        # 兼容旧的单目标字符串配置
        self.win_detect = bool(win_detect)
        raw_targets = win_forward_target or "me"
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        targets = []
        for item in raw_targets:
            # 数字目标（如 -100xxx 频道 id）必须保留为 int：字符串形式的
            # 数字会被 Telethon 当作手机号 resolve 而报 PHONE_NOT_OCCUPIED
            if isinstance(item, int):
                target: Any = item
            else:
                target = str(item).strip()
                if target and re.fullmatch(r"-?\d+", target):
                    target = int(target)
            if target and target not in targets:
                targets.append(target)
        self.win_forward_targets = targets or ["me"]
        # 中奖回扫条数：0/None/非法值回退默认
        self.win_sweep_limit = (
            max(1, int(win_sweep_limit)) if win_sweep_limit else WIN_SWEEP_LIMIT
        )
        self._win_handled.clear()
        # 双向账户集合：发言数要求类抽奖自动跳过
        self.two_way_accounts = {str(a) for a in (two_way_accounts or ())}
        self.max_joins = max(0, int(max_joins)) if max_joins else None
        if rescue_interval is not None:
            self.rescue_interval = max(0, int(rescue_interval))
        if rescue_limit is not None:
            self.rescue_limit = max(1, int(rescue_limit))
        lo, hi = delay_range
        self.delay_range = (min(lo, hi), max(lo, hi))
        self.session_dir = Path(session_dir)
        self.log = log
        self.on_event = on_event
        self.joined = 0
        self.joined_by_account = {}
        self.failed = 0
        self.scan_text = ""
        self._stopping = False
        self._flood_counts.clear()
        self._flood_bots.clear()
        self._ended_counts.clear()
        self._ended_bots.clear()
        self._seen_msgs.clear()
        # 每次启动重新尝试自动加入监控对象（账号可能已手动加入/限流已解除）
        self._join_cache.clear()
        self._join_suspended_until = None
        self._join_suspend_logged = False
        # 监控对象 chat_id -> @username：数字 id 无法直接加入时兜底
        self.chat_usernames = {
            str(k).strip(): str(v).lstrip("@").strip()
            for k, v in (chat_usernames or {}).items()
            if str(v).strip().lstrip("@").strip()
        }
        self._rescue_fail_streak = 0
        self._rescue_suspended_until = None
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(accounts, bool(auto_scan)))
        if on_finish is not None:
            self._task.add_done_callback(lambda _t: on_finish())
        return True

    async def stop(self) -> None:
        self._stopping = True
        if self._stop is not None:
            self._stop.set()
        task = self._task
        if task is None:
            return
        asyncio.create_task(self._finalize(task))

    async def _finalize(self, task: asyncio.Task) -> None:
        try:
            await task
        except Exception:
            logger.debug("等待抽奖任务收尾时任务异常退出", exc_info=True)
        if self._task is task:
            self._task = None

    def _emit(self, payload: dict) -> None:
        if self.on_event is not None:
            try:
                self.on_event(payload)
            except Exception:
                logger.debug("事件回调 on_event 执行失败", exc_info=True)

    async def _connect_accounts(self, accounts: List[str]) -> None:
        """多账号并行连接（各账号客户端相互独立，连接超时互不拖累）。"""

        async def _connect_one(account: str) -> None:
            client = None
            try:
                client = get_client(account, workdir=str(self.session_dir))
                ok = await asyncio.wait_for(
                    client.connect(), timeout=self.CONNECT_TIMEOUT
                )
                if not ok:
                    self.log(f"「{account}」会话未授权，已跳过")
                    await safe_disconnect(client)
                    return
                # 连接成功即纳入账号表；昵称仅用于展示，获取失败不断开。
                # 统一包一层网络超时代理，防半开连接永久挂起任务协程
                self._clients[account] = _TimeoutClient(client)
                self._locks[account] = asyncio.Lock()
                try:
                    me = await asyncio.wait_for(
                        client.get_me(), timeout=self.CONNECT_TIMEOUT
                    )
                    nickname = getattr(me, "first_name", "") or ""
                    self.log(f"「{account}」已连接（{nickname}）")
                except Exception:
                    self.log(f"「{account}」已连接（昵称获取超时，已忽略）")
            except asyncio.TimeoutError:
                self.log(f"「{account}」连接超时（网络或代理异常），已跳过")
                if client is not None:
                    await safe_disconnect(client)
            except Exception as exc:
                self.log(f"「{account}」连接失败: {exc}")
                if client is not None:
                    await safe_disconnect(client)

        await asyncio.gather(*[_connect_one(account) for account in accounts])

    async def _ensure_connected(self, client: Any) -> bool:
        """确保 client 可用；断线时尝试重连一次。"""
        if getattr(client, "is_connected", False):
            return True
        try:
            await asyncio.wait_for(client.connect(), timeout=self.CONNECT_TIMEOUT)
        except Exception:
            logger.debug("重连客户端连接失败", exc_info=True)
        return bool(getattr(client, "is_connected", False))

    async def _run(self, accounts: List[str], auto_scan: bool) -> None:
        try:
            await self._connect_accounts(accounts)
            if not self._clients:
                self.log("没有可用账号，任务结束")
                return
            mode_kw = "，".join(self.keywords) if self.keywords else "全部 bot 链接"
            self.log(
                f"任务已启动：{len(self._clients)} 个账号、{len(self.chats)} 个监控对象，"
                f"关键词：{mode_kw}"
            )
            # 中奖回扫：开奖常在参与后数日，先补捡停机期间 bot 私聊的中奖通知
            if not self._stop.is_set():
                await asyncio.gather(
                    *[
                        self._sweep_bot_wins(account, client)
                        for account, client in list(self._clients.items())
                    ]
                )
            if auto_scan and not self._stop.is_set():
                await self.scan_now()
                # 首扫后新增参与的 bot 需建回扫基线（清除记录后启动回扫无目标）
                if not self._stop.is_set():
                    await asyncio.gather(
                        *[
                            self._sweep_bot_wins(account, client)
                            for account, client in list(self._clients.items())
                        ]
                    )
            if self.rescue_interval > 0:
                await self._rescue_loop()
            elif not self._stop.is_set():
                # 补漏未开启：首轮扫描完成后直接结束任务，不常驻待命
                self.log("补漏扫描未开启，本轮扫描完成，任务结束")
        finally:
            for client in self._clients.values():
                await safe_disconnect(client)
            self._clients.clear()
            self.log("自动抽奖已停止")

    async def scan_now(self) -> None:
        """对所有账号 × 频道回溯扫描历史消息并自动参与（多账号并行扫描）。

        每个频道持续向前扫描，直到满足任一停止条件：
          - 参与数达到参与条数上限（若设置）
          - 连续 EXPIRED_STOP_STREAK 条已开奖的抽奖帖（再往前都是旧抽奖）
          - 达到单频道硬上限 SCAN_HARD_CAP 条（无开奖日期标注时兜底）
        """
        if self.scanning or not self._clients:
            return
        self.scanning = True
        try:
            total = len(self._clients) * max(1, len(self.chats))
            done = {"n": 0}
            joined_before = self.joined

            async def _scan_one(account: str, client: Any) -> None:
                if not await self._ensure_connected(client):
                    self.log(f"[{account}] 连接不可用，跳过该账号本轮扫描")
                    return
                for chat in self.chats:
                    if self._stop is not None and self._stop.is_set():
                        return
                    # 监控对象成员检查：账号未加入该频道/群组时自动尝试加入
                    await self._ensure_monitor_membership(account, client, chat)
                    done["n"] += 1
                    self.scan_text = f"({done['n']}/{total})"
                    expired_streak = 0
                    scanned = 0
                    # 识别聊天类型：群组仍只扫置顶；频道忽略置顶（多为长期
                    # 挂着的旧抽奖帖），直接从最新历史消息开始扫
                    is_group, chat_info = await self._detect_chat_type(
                        account, client, chat
                    )
                    if is_group:
                        await self._scan_pinned(
                            account, client, chat, chat_info=chat_info
                        )
                    if is_group or (
                        self.max_joins is not None and self.joined >= self.max_joins
                    ):
                        # 群组只扫指定（置顶）消息；达到上限也不再扫历史
                        continue
                    try:
                        async for message in client.get_chat_history(chat):
                            scanned += 1
                            if scanned > SCAN_HARD_CAP:
                                self.log(
                                    f"[{account}] {chat} 已扫描 "
                                    f"{SCAN_HARD_CAP} 条，停止该频道本轮扫描"
                                )
                                break
                            mid = getattr(message, "id", None)
                            if mid is not None:
                                self._mark_seen(account, str(chat), mid)
                            outcome = await self._process_message(
                                account, client, message
                            )
                            if outcome == "joined":
                                expired_streak = 0
                            elif outcome == "expired":
                                expired_streak += 1
                                if expired_streak >= EXPIRED_STOP_STREAK:
                                    self.log(
                                        f"[{account}] 连续 {expired_streak} 条已开奖，"
                                        f"停止扫描 {chat}"
                                    )
                                    break
                            if (
                                self.max_joins is not None
                                and self.joined >= self.max_joins
                            ):
                                break
                    except Exception as exc:
                        self.log(f"[{account}] 扫描 {chat} 失败: {exc}")

            await asyncio.gather(
                *[
                    _scan_one(account, client)
                    for account, client in list(self._clients.items())
                ]
            )
            self.scan_text = ""
            gained = self.joined - joined_before
            self.log(
                f"扫描完成：本次参与 {gained} 个"
                + (f"，失败 {self.failed} 个" if self.failed else "")
            )
        finally:
            self.scanning = False
            self.scan_text = ""
            if self._state is not None:
                self._state.save()

    async def _detect_chat_type(
        self, account: str, client: Any, chat: Any
    ) -> tuple[Optional[bool], Any]:
        """get_chat 识别聊天类型并缓存。

        群组返回 (True, chat_info)、频道返回 (False, chat_info)、
        获取失败返回 (None, None)（沿用缓存的旧类型，无法识别时为 None）。
        """
        try:
            chat_info = await client.get_chat(chat)
        except Exception as exc:
            self.log(f"[{account}] 获取 {chat} 信息失败，跳过类型识别: {exc}")
            return self._chat_types.get(str(chat)), None
        type_name = getattr(getattr(chat_info, "type", None), "name", "")
        is_group = type_name in ("GROUP", "SUPERGROUP")
        first_detect = str(chat) not in self._chat_types
        self._chat_types[str(chat)] = is_group
        if first_detect:
            kind = "群组（仅扫置顶）" if is_group else "频道（仅历史，忽略置顶）"
            title = getattr(chat_info, "title", "") or str(chat)
            self.log(f"[{account}] 识别 {chat} 类型：{kind}「{title}」")
        return is_group, chat_info

    async def _scan_pinned(
        self, account: str, client: Any, chat: Any, chat_info: Any = None
    ) -> None:
        """扫描群组的置顶消息并参与（群组只扫指定/置顶消息）。

        置顶消息与历史消息走同一套处理流程（关键词过滤、开奖日期跳过、
        去重、自动加入）。聊天类型识别由 _detect_chat_type 完成。
        """
        if chat_info is None:
            try:
                chat_info = await client.get_chat(chat)
            except Exception as exc:
                self.log(f"[{account}] 获取 {chat} 信息失败，跳过置顶扫描: {exc}")
                return
        pinned = getattr(chat_info, "pinned_message", None)
        # 枚举全部置顶消息：get_chat 只返回最新一条置顶，而群/频道可置顶多条，
        # 其余置顶只有通过 PINNED 过滤搜索才能拿到
        pinned_list: List[Any] = []
        try:
            from pyrogram.enums import MessagesFilter

            async for m in client.search_messages(chat, filter=MessagesFilter.PINNED):
                pinned_list.append(m)
        except Exception as exc:
            self.log(f"[{account}] 枚举置顶消息失败，仅处理最新置顶: {exc}")
            pinned_list = []
        latest_id = getattr(pinned, "id", None)
        if pinned is not None and all(
            getattr(m, "id", None) != latest_id for m in pinned_list
        ):
            pinned_list.append(pinned)  # 兜底：确保最新置顶一定被处理
        if pinned_list:
            self.log(
                f"[{account}] 「{getattr(chat_info, 'title', '') or chat}」"
                f"共有 {len(pinned_list)} 条置顶消息"
            )
        for msg in pinned_list:
            if self._stop is not None and self._stop.is_set():
                break
            if not self._mark_seen(account, str(chat), getattr(msg, "id", None)):
                self.log(
                    f"[{account}] 置顶消息 {getattr(msg, 'id', '?')} 本轮已处理过，跳过"
                )
                continue  # 本轮会话中已处理过
            try:
                await self._process_message(
                    account,
                    client,
                    msg,
                    skip_keywords=True,
                    log_skips=True,
                )  # 仅群组走此路径：置顶不受关键词过滤，且逐条打印跳过原因
            except Exception as exc:
                self.log(f"[{account}] 处理 {chat} 置顶消息失败: {exc}")

    async def _participate_via_source_button(
        self, account: str, client: Any, message: Any
    ) -> None:
        """群组置顶的「按钮型」抽奖：帖上「参与抽奖」按钮就在群聊消息里，
        直接点击源帖按钮完成参与（无需向任何 bot 发 start）。

        callback 按钮发送 callback 并读取弹窗文案（终态弹窗＝抽奖已结束）；
        URL 按钮若解析为 bot start 深度链接（非黑名单 bot）则走常规
        _participate 流程。以 source_btn|chat|msg_id 持久化去重，
        避免每轮扫描重复点击同一帖。
        """
        if self._stop is not None and self._stop.is_set():
            return
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        msg_id = getattr(message, "id", None)
        if chat_id is None or msg_id is None:
            return
        state_key = f"{chat_id}|{msg_id}"
        if self._state is not None and (
            self._state.has(account, "source_btn", state_key)
            or self._state.is_ended(account, "source_btn", state_key)
        ):
            return
        if self.max_joins is not None and self.joined >= self.max_joins:
            return
        btn = self._find_confirm_button(message)
        if btn is None:
            return
        channel = self._chat_title(message)
        title = _giveaway_title(message)
        # URL 按钮：解析为 bot start 链接则等同普通链接参与
        url = str(getattr(btn, "url", "") or "").strip()
        if url and not getattr(btn, "callback_data", None):
            link = parse_bot_start_link(url)
            if link is not None and link[0].lower() not in BLOCKED_BOTS:
                await self._participate(
                    account,
                    client,
                    link[0],
                    link[1],
                    channel,
                    title,
                    source_message=message,
                )
            return
        # 模拟人工点击节奏，避免连点触发风控
        lo, hi = self.delay_range
        if hi > 0:
            await asyncio.sleep(random.uniform(lo, max(lo, hi)))
        if self._stop is not None and self._stop.is_set():
            return
        # 同步预留参与名额（与 _participate 同口径），失败时归还
        reserved = False
        if self.max_joins is not None:
            self.joined += 1
            reserved = True
        try:
            answer = await client.request_callback_answer(
                chat_id=chat_id,
                message_id=msg_id,
                callback_data=btn.callback_data,
            )
        except Exception as exc:
            if reserved:
                self.joined -= 1
            self.failed += 1
            self.log(f"[{account}] 点击置顶抽奖帖「{btn.text}」按钮失败: {exc}")
            return
        popup = str(getattr(answer, "text", "") or "").strip()
        if popup and _POPUP_ENDED_RE.search(popup):
            if reserved:
                self.joined -= 1
            if self._state is not None:
                self._state.add(
                    account, "source_btn", state_key, channel=channel, note="ended"
                )
                self._state.save()
            self.log(
                f"[{account}] 置顶抽奖帖「{btn.text}」弹窗提示「{popup[:40]}」，"
                "抽奖已结束，跳过"
            )
            return
        if self._state is not None:
            self._state.add(
                account,
                "source_btn",
                state_key,
                channel=channel,
                note="btn_joined",
            )
            self._state.save()
        if not reserved:  # 已设上限时名额在预留时计入
            self.joined += 1
        self.joined_by_account[account] = self.joined_by_account.get(account, 0) + 1
        chat_counts = self.joined_by_account_chat.setdefault(account, {})
        chat_key = channel or "未知频道"
        chat_counts[chat_key] = chat_counts.get(chat_key, 0) + 1
        popup_part = f"，弹窗：{popup[:40]}" if popup else ""
        self.log(
            f"{datetime.now():%H:%M:%S} [已参与] {account} -> 置顶抽奖帖"
            f"「{btn.text}」按钮（来自 {channel or '未知频道'}{popup_part}）"
        )
        self._emit(
            {
                "account": account,
                "bot": "source_button",
                "channel": channel,
                "title": title,
                "ok": True,
            }
        )

    def _mark_seen(self, account: str, chat_key: str, message_id: int) -> bool:
        """记录消息已处理（水位线）。返回 False 表示此前已见过，补漏时应跳过。"""
        if message_id is None:
            return True
        key = f"{account}|{chat_key}"
        bucket = self._seen_msgs.setdefault(key, {})
        if message_id in bucket:
            return False
        bucket[message_id] = None
        if len(bucket) > SEEN_CAP_PER_CHAT:
            # dict 保序：最先插入的是最旧的消息，超出容量丢弃最旧的
            for old in list(bucket)[: len(bucket) - SEEN_CAP_PER_CHAT]:
                bucket.pop(old, None)
        return True

    async def _rescue_loop(self) -> None:
        """历史补漏主循环：周期性重拉各频道最近消息，捞回漏掉的抽奖帖。

        网络异常/超时连续 RESCUE_MAX_FAILS 轮失败后熔断暂停
        RESCUE_RESUME_COOLDOWN，再自动恢复，避免坏网络下无意义刷请求。
        """
        while not (self._stop is not None and self._stop.is_set()):
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.rescue_interval)
                return  # 收到停止信号
            except asyncio.TimeoutError:
                logger.debug("历史补漏轮询等待超时，继续下一周期", exc_info=True)
            if self.scanning:
                continue  # 完整/手动扫描进行中，让路并重等一个周期
            if self._rescue_suspended_until is not None:
                if datetime.now() < self._rescue_suspended_until:
                    continue
                self._rescue_suspended_until = None
                self._rescue_fail_streak = 0
                self.log("历史补漏已从熔断暂停中恢复")
            await self._rescue_round()

    async def _rescue_round(self) -> None:
        """执行一轮补漏：每账号 × 每频道重拉最近 rescue_limit 条消息（多账号并行）。"""
        failures = {"n": 0}
        joined_before = self.joined
        total = len(self._clients) * max(1, len(self.chats))
        done = {"n": 0}
        self.scanning = True

        async def _rescue_one(account: str, client: Any) -> None:
            if not await self._ensure_connected(client):
                self.log(f"[{account}] 补漏：连接不可用，跳过该账号")
                failures["n"] += 1
                return
            for chat in self.chats:
                if self._stop is not None and self._stop.is_set():
                    return
                done["n"] += 1
                self.scan_text = f"补漏 ({done['n']}/{total})"
                try:
                    await self._rescue_channel(account, client, chat)
                except asyncio.TimeoutError:
                    self.log(
                        f"[{account}] 补漏 {chat} 拉取超时（{RESCUE_FETCH_TIMEOUT}s）"
                    )
                    failures["n"] += 1
                except Exception as exc:
                    self.log(f"[{account}] 补漏 {chat} 失败: {exc}")
                    failures["n"] += 1
            # 补漏轮顺带回扫 bot 私聊中奖消息（不依赖监控对象配置）
            await self._sweep_bot_wins(account, client)

        try:
            await asyncio.gather(
                *[
                    _rescue_one(account, client)
                    for account, client in list(self._clients.items())
                ]
            )
            gained = self.joined - joined_before
            # 每轮必记一行，便于确认补漏循环在按间隔运行
            self.log(
                f"补漏轮次完成：参与 {gained} 个"
                + (f"，{failures['n']} 个频道拉取失败" if failures["n"] else "")
            )
        finally:
            self.scanning = False
            self.scan_text = ""
            if self._state is not None:
                self._state.save()
        if failures["n"] == 0:
            self._rescue_fail_streak = 0
            return
        self._rescue_fail_streak += 1
        if self._rescue_fail_streak >= RESCUE_MAX_FAILS:
            self._rescue_suspended_until = datetime.now() + timedelta(
                seconds=RESCUE_RESUME_COOLDOWN
            )
            self.log(
                f"补漏连续 {self._rescue_fail_streak} 轮失败，暂停 "
                f"{RESCUE_RESUME_COOLDOWN // 60} 分钟后自动恢复"
            )

    async def _rescue_channel(self, account: str, client: Any, chat: Any) -> None:
        """补漏单个频道：限时拉取最近 N 条，未处理过的消息走常规参与流程。"""
        if self._chat_types.get(str(chat)):
            # 群组不扫历史补漏，只重新检查置顶消息（群主可能换了新置顶抽奖帖）
            await self._rescue_group_pinned(account, client, chat)
            return
        fetched: List[Any] = []

        async def _fetch() -> None:
            async for message in client.get_chat_history(chat, limit=self.rescue_limit):
                fetched.append(message)

        # 超时仅约束网络拉取；参与流程含预期内的随机延迟，不做强杀
        await asyncio.wait_for(_fetch(), timeout=RESCUE_FETCH_TIMEOUT)
        for message in fetched:
            if self._stop is not None and self._stop.is_set():
                return
            mid = getattr(message, "id", None)
            if not self._mark_seen(account, str(chat), mid):
                continue  # 已处理过（水位线去重）
            await self._process_message(account, client, message)

    async def _rescue_group_pinned(self, account: str, client: Any, chat: Any) -> None:
        """群组补漏：只重拉置顶消息并按水位线去重处理，不扫历史。"""
        chat_info = await client.get_chat(chat)
        pinned = getattr(chat_info, "pinned_message", None)
        if pinned is None:
            return
        mid = getattr(pinned, "id", None)
        if not self._mark_seen(account, str(chat), mid):
            return  # 置顶未变化，此前已处理过
        # 群组置顶不受关键词影响
        await self._process_message(account, client, pinned, skip_keywords=True)

    # —— 中奖识别 ——

    async def _check_win_message(
        self, account: str, client: Any, bot_username: str, message: Any
    ) -> None:
        """中奖识别：bot 私聊消息命中关键词时转发提醒并推送到消息中心。

        去重两级：进程内 _win_handled（同一消息重复轮询只处理一次）+
        状态文件 win:<msg_id> 记录（跨重启不重复转发/推送）。
        """
        if not self.win_detect:
            return
        text = _message_text(message)
        if not text or not WIN_MESSAGE_RE.search(text):
            return
        mid = getattr(message, "id", None)
        dedup_key = (
            f"{account}|{bot_username.lower()}|{mid if mid is not None else hash(text)}"
        )
        if dedup_key in self._win_handled:
            return
        if (
            mid is not None
            and self._state is not None
            and self._state.has(account, bot_username, f"win:{mid}")
        ):
            self._win_handled.add(dedup_key)
            return
        self._win_handled.add(dedup_key)
        if mid is not None and self._state is not None:
            self._state.add(account, bot_username, f"win:{mid}", note="win")
            self._state.save()
        summary = " ".join(text.split())
        if len(summary) > 80:
            summary = summary[:80] + "…"
        self.log(f"[{account}] 检测到中奖消息（@{bot_username}）：{summary}")
        # 转发原消息到各转发目标（默认收藏夹），单个失败不阻断其余目标与提醒
        chat_id = getattr(getattr(message, "chat", None), "id", None) or bot_username
        if mid is not None:
            for target in self.win_forward_targets:
                try:
                    await client.forward_messages(
                        chat_id=target,
                        from_chat_id=chat_id,
                        message_ids=[mid],
                    )
                    self.log(
                        f"[{account}] 中奖消息已转发到 "
                        f"{'收藏夹' if target == 'me' else target}"
                    )
                except Exception as exc:
                    self.log(
                        f"[{account}] 中奖消息转发到 "
                        f"{'收藏夹' if target == 'me' else target} 失败: {exc}"
                    )
                    logger.debug("中奖消息转发失败", exc_info=True)
        self._emit(
            {
                "type": "win",
                "account": account,
                "bot": bot_username,
                "text": text[:300],
            }
        )

    async def _sweep_bot_wins(self, account: str, client: Any) -> None:
        """增量回扫该账号全部已知 bot 的私聊消息，补捡错过的中奖通知。

        在任务启动时与每个补漏轮执行：开奖常在参与后数日，中奖私信到达时
        任务可能早已结束，靠回扫兜底。每账号 × bot 持久化水位线（上次扫到
        的消息 id），本轮只处理比水位线新的消息并推进水位线；首次见到某
        bot（无水位线）时才用回扫条数 win_sweep_limit 做初始基线。回扫条
        数同时作为每轮扫描量下限：水位线后新消息不足下限时继续向更旧消息
        补扫（已处理过的中奖消息靠状态去重）。bot 从未开过会话/已失效等
        拉取失败静默跳过（调试日志），不影响其他 bot。
        """
        if not self.win_detect or self._state is None:
            return
        bots = self._state.bots_for(account)
        if not bots:
            # 清除参与记录后 bot 列表为空，等账号重新参与后重建回扫目标
            self.log(f"[{account}] 回扫：暂无 bot 记录（等待账号参与后重建）")
            return
        for bot in bots:
            if self._stop is not None and self._stop.is_set():
                return
            watermark = self._state.win_watermark(account, bot)
            try:
                if watermark is None:
                    # 首次见到该 bot：按回扫条数建初始基线
                    batch: List[Any] = []
                    async for message in client.get_chat_history(
                        bot, limit=self.win_sweep_limit
                    ):
                        batch.append(message)
                else:
                    batch = await self._fetch_newer_bot_messages(
                        client, bot, watermark, min_count=self.win_sweep_limit
                    )
                # batch 从新到旧，按时间正序处理
                for message in reversed(batch):
                    await self._check_win_message(account, client, bot, message)
                # 回扫本身静默，记一行结果便于确认扫描在按配置执行
                new_n = (
                    len(batch)
                    if watermark is None
                    else sum(1 for m in batch if (getattr(m, "id", 0) or 0) > watermark)
                )
                self.log(
                    f"[{account}] 回扫 @{bot} 私聊：扫描 {len(batch)} 条"
                    + (
                        f"（新 {new_n} 条）"
                        if watermark is not None
                        else "（首次建基线）"
                    )
                )
                newest = getattr(batch[0], "id", None) if batch else None
                if newest is not None:
                    self._state.set_win_watermark(account, bot, newest)
                    # 立即落盘：处理中奖消息时会顺带 save 旧水位线
                    self._state.save()
            except Exception:
                logger.debug(f"回扫 @{bot} 私聊中奖消息失败", exc_info=True)

    async def _fetch_newer_bot_messages(
        self, client: Any, bot: str, watermark: int, min_count: int = 0
    ) -> List[Any]:
        """拉取 bot 私聊中比水位线新的消息（返回结果从新到旧）。

        get_chat_history 只能从新到旧翻页：逐页向后翻，遇到不新于水位线
        的消息且累计扫描量已达 min_count 时停。min_count 为本轮扫描量
        下限（即回扫条数 win_sweep_limit）：水位线后新消息不足下限时
        越过水位线继续向更旧消息补扫，旧消息靠状态去重不会重复提醒；
        最多翻 WIN_SWEEP_PAGE_MAX 页兜底（防水位线异常时无限翻页）。
        """
        batch: List[Any] = []
        offset_id = 0
        reached = False
        for _ in range(WIN_SWEEP_PAGE_MAX):
            page: List[Any] = []
            async for message in client.get_chat_history(
                bot, offset_id=offset_id, limit=WIN_SWEEP_PAGE
            ):
                page.append(message)
            if not page:
                break
            for message in page:
                mid = getattr(message, "id", None)
                if not reached and mid is not None and mid <= watermark:
                    reached = True
                    if len(batch) >= min_count:
                        break
                batch.append(message)
            if reached and len(batch) >= min_count:
                break
            offset_id = getattr(page[-1], "id", 0) or 0
            if offset_id <= 0:
                break
        return batch

    async def _resolve_join_target_type(
        self, client: Any, target: str
    ) -> Optional[str]:
        """识别加入目标的聊天类型名（GROUP/SUPERGROUP/CHANNEL/USER/BOT）。

        无法解析（用户名不存在/已失效）返回 None。结果按目标缓存。
        """
        key = target.lower()
        if key in self._join_target_types:
            return self._join_target_types[key]
        try:
            chat_info = await client.get_chat(target)
            type_name = getattr(getattr(chat_info, "type", None), "name", "") or None
        except Exception:
            type_name = None
        self._join_target_types[key] = type_name
        return type_name

    def _chat_title(self, message: Any) -> str:
        chat = getattr(message, "chat", None)
        return str(getattr(chat, "title", "") or getattr(chat, "id", "") or "")

    async def _process_message(
        self,
        account: str,
        client: Any,
        message: Any,
        skip_keywords: bool = False,
        log_skips: bool = False,
    ) -> Optional[str]:
        """处理单条消息。返回 "joined"（本次有参与）/"expired"（已开奖跳过）/None。

        skip_keywords=True 时跳过关键词过滤（用于群组置顶消息：不受关键词影响）。
        log_skips=True 时逐条打印跳过原因（用于置顶消息排查，历史扫描仍静默）；
        例外：已开奖跳过始终记录诊断日志（带解析出的开奖时间），用于排查
        「连续已开奖止损」是否漏扫未开奖旧帖。
        """
        if self._stop is not None and self._stop.is_set():
            return None
        text = " ".join((_message_text(message) or "").split())
        mid = getattr(message, "id", None)
        # 置顶路径标「置顶消息」，历史扫描标「消息」，便于区分日志来源
        tag = (
            ("置顶消息 " if skip_keywords else "消息 ")
            + (str(mid) if mid is not None else "?")
            + (
                f"「{text[:30]}…」"
                if len(text) > 30
                else (f"「{text}」" if text else "")
            )
        )
        links = extract_bot_links(message)
        # 黑名单 bot 的链接不作为抽奖链接（整条消息无可用链接时直接跳过）
        links = [ln for ln in links if ln[0].lower() not in BLOCKED_BOTS]
        if not links:
            if log_skips:
                self.log(f"[{account}] {tag} 无可用抽奖 bot 链接，跳过")
            if skip_keywords:
                # 群组置顶的「按钮型」抽奖：帖内无可 start 的 bot 链接
                # （或仅剩黑名单推广链接），但帖上带「参与抽奖」按钮，
                # 直接点击源帖按钮完成参与
                await self._participate_via_source_button(account, client, message)
            return None
        if not skip_keywords and not message_matches_keywords(message, self.keywords):
            # 消息含 bot 链接但关键词未命中：静默跳过（日志逐条刷屏，不再打印）
            return None
        # 归属地限制过滤：仅参与无地区限制或含白名单地区的抽奖（对群组置顶同样生效）
        if not message_region_allowed(message):
            if log_skips:
                self.log(f"[{account}] {tag} 含地区限制，跳过")
            return None
        # 发言数要求/口令要求 + 双向账户：无法达标，自动跳过（对群组置顶同样生效）
        if account in self.two_way_accounts and (
            message_requires_speech(message) or message_requires_password(message)
        ):
            if log_skips:
                self.log(f"[{account}] {tag} 需发言数/口令，双向账户无法达标，跳过")
            return None
        channel = self._chat_title(message)
        title = _giveaway_title(message)
        # 开奖日期早于当前时间：自动跳过（始终带诊断日志，见下方注释）
        draw_dt = parse_draw_date(_message_text(message))
        if draw_dt is not None and draw_dt < datetime.now():
            # 已开奖跳过始终记录（不收 log_skips 门控）：排查止损漏扫问题
            self.log(
                f"[{account}] {tag} 开奖时间已过"
                f"（{draw_dt.strftime('%Y-%m-%d %H:%M')}），跳过"
            )
            return "expired"
        # 参与条数上限（None = 不限），达到后本轮不再参与
        if self.max_joins is not None and self.joined >= self.max_joins:
            cap_key = ("cap", account)
            if cap_key not in self._skip_logged:
                self._skip_logged.add(cap_key)
                self.log(
                    f"[{account}] 已达参与上限 {self.max_joins} 个，后续命中不再参与"
                )
            return None
        participated = 0
        # 先过滤出可参与的链接（未参与过/未冷却/未熔断），全部不可参与时
        # 连「命中」日志也静默，避免熔断或历史去重期间刷屏
        actionable: list = []
        for username, param in links:
            if self._stop is not None and self._stop.is_set():
                break
            if self.max_joins is not None and self.joined >= self.max_joins:
                break
            if self._state is not None and self._state.is_ended(
                account, username, param
            ):
                continue  # bot 曾回复「已结束」＝终态，静默跳过
            if self._participated_before(account, username, param):
                continue  # 已参与过且未忽略历史，静默跳过
            if self._state is not None and self._state.is_blocked(
                account, username, param
            ):
                continue
            if self._flood_key(account, username) in self._flood_bots:
                continue
            if self._flood_key(account, username) in self._ended_bots:
                continue  # 已结束回复熔断的 bot，静默跳过
            actionable.append((username, param))
        if not actionable:
            if log_skips:
                self.log(
                    f"[{account}] {tag} 命中 {len(links)} 个链接但均不可参与"
                    "（已参与过/已结束/限流熔断），跳过"
                )
            return "joined" if participated else None
        # 命中多少个抽奖入口一目了然
        self.log(
            f"[{account}] 命中 {len(actionable)} 个抽奖链接："
            + "、".join(f"@{u}" for u, _ in actionable)
        )
        for username, param in actionable:
            # 名额上限在此快速检查；同步预留与失败归还在 _participate 内完成
            if self.max_joins is not None and self.joined >= self.max_joins:
                break
            before = self.joined
            await self._participate(
                account,
                client,
                username,
                param,
                channel,
                title,
                source_message=message,
            )
            if self.joined > before:
                participated += 1
        return "joined" if participated else None

    async def _is_bot(self, account: str, client: Any, username: str) -> bool:
        cache_key = f"{account}|{username.lower()}"
        cached = self._bot_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            # 通过 get_users 拿到 is_bot 标记（不同层的 InputPeer 标记不统一）
            users = await client.get_users(username)
            is_bot = bool(getattr(users, "is_bot", False))
        except Exception:
            is_bot = False
        self._bot_cache[cache_key] = is_bot
        return is_bot

    @staticmethod
    def _flood_key(account: str, username: str) -> str:
        return f"{account}|{username.lower()}"

    def _participated_before(self, account: str, username: str, param: str) -> bool:
        """该链接是否视为「已参与过」需要跳过。

        历史成功记录默认生效（参与过即跳过）；开启 ignore_history
        （忽略历史成功记录）后不再视为已参与，允许重复参与。
        bot 回复「已结束」的终态记录不受该开关影响，由 is_ended 单独判断。
        """
        if self._state is None or self.ignore_history:
            return False
        return self._state.has(account, username, param)

    def _note_ended(self, account: str, username: str) -> None:
        """记录 bot 一次「已结束」回复；累计达上限后本轮运行熔断该 bot。

        与 FLOOD_WAIT 熔断同口径：计数不因参与成功重置，跨轮扫描累计，
        达到 MAX_ENDED_STRIKES 后本运行内不再向该 bot 发 start。
        """
        fkey = self._flood_key(account, username)
        if fkey in self._ended_bots:
            return
        strikes = self._ended_counts.get(fkey, 0) + 1
        self._ended_counts[fkey] = strikes
        if strikes >= MAX_ENDED_STRIKES:
            self._ended_bots.add(fkey)
            self.log(
                f"[{account}] @{username} 已 {strikes} 次回复抽奖已结束，"
                "本轮运行跳过该 bot"
            )

    @staticmethod
    def _flood_seconds(exc: Exception) -> int:
        """从 FLOOD_WAIT 异常中提取需等待秒数；不是限流异常返回 0"""
        value = getattr(exc, "value", None)
        try:
            return int(value)
        except (TypeError, ValueError):
            m = re.search(r"wait (\d+) seconds", str(exc))
            return int(m.group(1)) if m else 0

    async def _participate(
        self,
        account: str,
        client: Any,
        username: str,
        param: str,
        channel: str,
        title: str = "",
        source_message: Any = None,
    ) -> None:
        if self._participated_before(account, username, param):
            return
        if self._state is not None and self._state.is_ended(account, username, param):
            # bot 曾回复「已结束」＝终态，无论是否忽略历史都不再发 start
            return
        if not await self._is_bot(account, client, username):
            self.log(f"[{account}] 跳过非机器人链接 @{username}")
            return
        lo, hi = self.delay_range
        if hi > 0:
            await asyncio.sleep(random.uniform(lo, max(lo, hi)))
        if self._stop is not None and self._stop.is_set():
            return
        # 同步预留参与名额：检查与自增之间无 await，事件循环内天然原子，
        # 多账号并发扫描也不会突破「参与条数上限」；start 失败时归还名额
        reserved = False
        if self.max_joins is not None:
            if self.joined >= self.max_joins:
                return
            self.joined += 1
            reserved = True
        try:
            start_msg = await client.start_bot(username, param)
        except Exception as exc:
            if reserved:
                self.joined -= 1
            self.failed += 1
            wait = self._flood_seconds(exc)
            if wait > 0:
                # Telegram 限流：记录失败（冷却期内不重试），连续超限则熔断该 bot
                fkey = self._flood_key(account, username)
                strikes = self._flood_counts.get(fkey, 0) + 1
                self._flood_counts[fkey] = strikes
                if self._state is not None:
                    self._state.add(
                        account,
                        username,
                        param,
                        channel=channel,
                        note=f"flood_wait:{wait}",
                    )
                    self._state.save()
                self.log(
                    f"[{account}] 参与失败 @{username}（{param}）: "
                    f"Telegram 限流，需等待 {wait} 秒"
                    f"（连续第 {strikes} 次）"
                )
                if strikes >= MAX_FLOOD_STRIKES:
                    self._flood_bots.add(fkey)
                    self.log(
                        f"[{account}] @{username} 连续限流 {strikes} 次，"
                        "本轮运行跳过该 bot"
                    )
            else:
                self.log(f"[{account}] 参与失败 @{username}（{param}）: {exc}")
            self._emit(
                {
                    "account": account,
                    "bot": username,
                    "channel": channel,
                    "ok": False,
                    "error": str(exc),
                }
            )
            return
        # bot 对本次 start 直接回复「已结束」（如 @Lottery8Bot 的
        # "The event has ended or does not exist"）＝终态：归还名额、
        # 不记已参与，持久化标记防止今后重试，并累计 bot 熔断计数
        if reply_says_ended(start_msg):
            if reserved:
                self.joined -= 1
            if self._state is not None:
                self._state.add(account, username, param, channel=channel, note="ended")
                self._state.save()
            self._note_ended(account, username)
            from_part = f"来自 {channel or '未知频道'}"
            if title:
                from_part += f"：{title}"
            self.log(
                f"{datetime.now():%H:%M:%S} [已结束] {account} -> @{username} "
                f"回复抽奖已结束，跳过（{from_part}）"
            )
            self._emit(
                {
                    "account": account,
                    "bot": username,
                    "channel": channel,
                    "ok": False,
                    "error": "抽奖已结束",
                }
            )
            return
        if self._state is not None:
            self._state.add(account, username, param, channel=channel)
            self._state.save()
        if not reserved:  # 已设上限时名额在预留时计入
            self.joined += 1
        self.joined_by_account[account] = self.joined_by_account.get(account, 0) + 1
        chat_counts = self.joined_by_account_chat.setdefault(account, {})
        chat_key = channel or "未知频道"
        chat_counts[chat_key] = chat_counts.get(chat_key, 0) + 1
        from_part = f"来自 {channel or '未知频道'}"
        if title:
            from_part += f"：{title}"
        self.log(
            f"{datetime.now():%H:%M:%S} [已参与] {account} -> @{username} "
            f"（{from_part}）"
        )
        self._emit(
            {
                "account": account,
                "bot": username,
                "channel": channel,
                "title": title,
                "ok": True,
            }
        )
        await self._click_confirm(
            account, client, username, start_msg, param, source_message=source_message
        )

    async def _join_from_message(
        self, account: str, client: Any, bot_username: str, message: Any
    ) -> bool:
        """激进模式：把消息中出现的频道/群组链接逐个加入。返回是否发生加入。"""
        joined_any = False
        if (
            self._join_suspended_until is not None
            and datetime.now() < self._join_suspended_until
        ):
            if not self._join_suspend_logged:
                self._join_suspend_logged = True
                self.log(
                    "自动加入被限流暂停中，恢复时间 "
                    f"{self._join_suspended_until:%H:%M}（暂停期间跳过所有加入）"
                )
            return False
        targets = extract_channel_targets(message)
        if bot_username.lower() == LOTTERYBEST_BOT:
            # LotteryBestBot 的回复把「加入-名称」做成带隐藏链接的❌/✅清单，
            # 补充提取其中 ❌（未加入）行的链接目标
            known = {t.lower() for t in targets}
            status_targets = [
                t
                for t in extract_join_status_targets(message)
                if t.lower() not in known
            ]
            if not status_targets:
                raw = _message_text(message)
                # 「└ 🎟️ 发言数大于 N 条 ❌」等激活任务子行也带 ❌，但不含
                # 「加入」，属于正常状态不是未加入频道行；仅当存在真正的
                # 「加入-名称 ❌」行却提取不到链接时才报诊断，避免发言任务
                # 行触发解析失败误报
                has_unjoined_row = bool(raw) and any(
                    "加入" in ln and "❌" in ln and "✅" not in ln
                    for ln in raw.split("\n")
                )
                if raw and has_unjoined_row:
                    # 逐行明细：每行文本 + 挂在该行上的链接实体，
                    # 用于定位 ❌ 行是链接缺失还是行匹配失败
                    ents = list(getattr(message, "entities", None) or []) + list(
                        getattr(message, "caption_entities", None) or []
                    )
                    line_urls: Dict[int, List[str]] = {}
                    bounds: List[Tuple[int, int]] = []
                    pos = 0
                    for ln in raw.split("\n"):
                        bounds.append((pos, pos + len(ln)))
                        pos += len(ln) + 1
                    for ent in ents:
                        url = getattr(ent, "url", None)
                        if not url:
                            continue
                        off = _utf16_to_index(raw, int(getattr(ent, "offset", 0) or 0))
                        for idx, (ls, le) in enumerate(bounds):
                            if ls <= off <= le:
                                line_urls.setdefault(idx, []).append(str(url))
                                break
                    detail = "；".join(
                        "L{}`{}`{}`".format(
                            i + 1,
                            raw[ls:le].strip()[:36],
                            ("→" + "、".join(line_urls[i])) if i in line_urls else "",
                        )
                        for i, (ls, le) in enumerate(bounds)
                        if "❌" in raw[ls:le] or "✅" in raw[ls:le] or i in line_urls
                    )
                    self.log(
                        f"[{account}] @LotteryBestBot 清单含未加入(❌)行但未解析到"
                        f"链接（逐行明细：{detail}）"
                    )
            targets = targets + status_targets
        for target in targets:
            if target.lower().lstrip("@") == bot_username.lower():
                continue  # 跳过 bot 自己
            key = f"{account}|{target.lower()}"
            if key in self._join_cache:
                continue
            self._join_cache.add(key)
            # 用户名目标先识别类型：个人用户/机器人/失效目标直接跳过，
            # 避免发出注定失败的 JoinChannel（CHANNEL_INVALID 噪音）。
            # 私有邀请链接（+hash/joinchat）无法预解析，仍直接尝试加入。
            if not target.lower().startswith(("http://", "https://")):
                type_name = await self._resolve_join_target_type(client, target)
                if type_name is None:
                    self.log(f"[{account}] 跳过无效目标 {target}（无法解析或已失效）")
                    continue
                if type_name not in ("GROUP", "SUPERGROUP", "CHANNEL"):
                    continue  # 个人用户/机器人，静默跳过
                # 加入前检查成员身份：能看到哪些目标还没加入（bot 常要求
                # 加入频道后才给抽奖资格）
                already_member = await self._is_member(client, target)
                if already_member:
                    self.log(f"[{account}] 已在 {target} 中，跳过加入")
                    continue
                self.log(
                    f"[{account}] 尚未加入 {target}"
                    f"（@{bot_username} 要求加入后才能参与，自动加入中）"
                )
            try:
                chat = await client.join_chat(target)
                title = str(getattr(chat, "title", "") or target)
                self.log(f"[{account}] 已加入 {title}（来自 @{bot_username} 的消息）")
                joined_any = True
            except Exception as exc:
                msg = str(exc)
                if "ALREADY_PARTICIPANT" in msg or "already" in msg.lower():
                    self.log(f"[{account}] 已在 {target} 中，跳过加入")
                elif "INVITE_REQUEST_SENT" in msg:
                    self.log(
                        f"[{account}] 已提交加入 {target} 的申请"
                        "（私有群/频道，待管理员审批）"
                    )
                    joined_any = True
                elif "FLOOD_WAIT" in msg:
                    wait = self._flood_seconds(exc) or 60
                    self._join_suspended_until = datetime.now() + timedelta(
                        seconds=wait + 10
                    )
                    self._join_suspend_logged = False
                    self.log(
                        f"[{account}] 加入频道触发限流（需等待 {wait} 秒），"
                        f"自动加入暂停至 {self._join_suspended_until:%H:%M}"
                    )
                    break  # 已被限流，本条消息剩余目标一并跳过
                else:
                    self.log(f"[{account}] 加入 {target} 失败: {msg[:120]}")
        return joined_any

    async def _send_password_tasks(
        self,
        account: str,
        client: Any,
        username: str,
        source_message: Any,
        bot_messages: List[Any],
        ignore_sent: bool = False,
        link_messages: Optional[List[Any]] = None,
    ) -> None:
        """回复清单/帖内口令任务：定位口令群组并发送口令文本。

        时序遵循「请先参与抽奖，再发送口令激活」：start 已发、加入完成
        后在此发送口令，随后由重发 start 让 bot 核验。每账号×bot×群组×
        口令文本只发送一次（进程内去重），避免重复参与时反复发言——
        同一群组常被多个抽奖复用且各带不同口令，去重键必须含口令文本。
        报名成功后的激活清单补发场景传 ignore_sent=True 忽略去重
        （清单仍标 ❌ 即代表 bot 未计入此前发送，需要重发）。
        """
        # 双向账户一律不执行口令任务：无法主动发言/私聊，避免反复触发
        # Telegram 风控（USER_BANNED_IN_CHANNEL），跳过保持静默
        if account in self.two_way_accounts:
            return
        # 口令任务以 bot 回复清单为准（原帖与回复都可能出现「发送口令﹝xx﹞」行）：
        # bot 返回了口令任务（❌ 未完成行）才发送，✅ 行已发送过自动跳过。
        # 仅当 bot 回复完全不带口令要求时才回退解析原始抽奖帖，兼容帖内
        # 直接带口令任务的 bot；bot 回复出现口令字样但解析不到文本时不回退
        # （如 ✅ 已完成行被跳过，避免借原帖重发）。
        tasks: List[Tuple[str, str]] = []
        bot_mentions_password = False
        for message in bot_messages or []:
            text = _message_text(message) or ""
            if not bot_mentions_password and PASSWORD_REQUIRE_RE.search(text):
                bot_mentions_password = True
            for task in extract_password_tasks(message):
                if task not in tasks:
                    tasks.append(task)
        if not tasks and not bot_mentions_password and source_message is not None:
            tasks = extract_password_tasks(source_message)
        if not tasks:
            return
        # bot 回复清单「加入-名称」行的（名称 → 目标）映射，❌/✅ 行均收集：
        # 口令群即使已加入（✅）也需拿到目标才能发言；补发场景可传入
        # 更早的回复一并定位（link_messages 优先级高于 bot_messages）
        if link_messages is None:
            link_sources = list(bot_messages)
        else:
            link_sources = list(link_messages)
        name_links: Dict[str, str] = {}
        for message in link_sources:
            for name, target in extract_join_name_links(message):
                name_links.setdefault(name.lower(), target)
        for group_name, password in tasks:
            sent_key = f"{account}|{username}|{group_name.lower()}|{password}"
            if not ignore_sent and sent_key in self._password_sent:
                continue
            target = name_links.get(group_name.lower())
            if target is None:
                # 名称不完全一致时按包含关系模糊匹配（帖子群名 vs 清单行名）
                target = next(
                    (
                        t
                        for n, t in name_links.items()
                        if group_name in n or n in group_name
                    ),
                    None,
                )
            if target is None:
                self.log(
                    f"[{account}] 口令群「{group_name}」未在清单中定位到链接，"
                    "跳过发送口令"
                )
                # 记录未完成发言任务（消息中心展示，发送成功后自动消除）
                self._emit(
                    {
                        "type": "speak_failed",
                        "account": account,
                        "group": group_name,
                        "password": password,
                        "error": "未在清单中定位到口令群组链接",
                    }
                )
                continue
            # 私有邀请链接目标：先解析成真实 chat id（未加入则顺带加入），
            # 直接拿链接字符串 send_message 会误走用户名解析必失败
            send_target: Any = target
            invite_match = _INVITE_LINK_RE.match(str(target).strip())
            if invite_match:
                send_target = await self._resolve_invite_target(
                    client, invite_match.group(1), str(target).strip()
                )
                if send_target is None:
                    self.log(
                        f"[{account}] 加入口令群「{group_name}」失败，"
                        "无法发送口令: 邀请链接解析失败（私有群可能需管理员审批）"
                    )
                    self._emit(
                        {
                            "type": "speak_failed",
                            "account": account,
                            "group": group_name,
                            "password": password,
                            "error": "加入口令群失败: 邀请链接解析失败（私有群可能需管理员审批）",
                        }
                    )
                    continue
                # 解析即加入：无需再做成员检查/二次 join
                already = True
            else:
                already = await self._is_member(client, send_target)
            if already is False:
                try:
                    await client.join_chat(send_target)
                    self.log(f"[{account}] 已加入口令群「{group_name}」")
                    # 成员状态同步后再发言，避免 bot 侧校验不到
                    await asyncio.sleep(3)
                except Exception as exc:
                    self.log(
                        f"[{account}] 加入口令群「{group_name}」失败，"
                        f"无法发送口令: {str(exc)[:120]}"
                    )
                    self._emit(
                        {
                            "type": "speak_failed",
                            "account": account,
                            "group": group_name,
                            "password": password,
                            "error": f"加入口令群失败: {str(exc)[:120]}",
                        }
                    )
                    continue
            try:
                # 随机延迟错峰：多账号不在同一时刻向同群发言
                await asyncio.sleep(random.uniform(2.0, 6.0))
                try:
                    await client.send_message(send_target, password)
                except Exception as resolve_exc:
                    # 公开用户名失效（群改名/收回用户名）：按群名在账号已
                    # 加入会话中精确匹配兜底；其余异常原样抛出
                    if not _is_username_resolve_error(resolve_exc):
                        raise
                    fallback = await self._find_dialog_by_title(client, group_name)
                    if fallback is None:
                        raise
                    self.log(
                        f"[{account}] 口令群「{group_name}」链接解析失败，"
                        "已按群名匹配已加入会话兜底发送"
                    )
                    await client.send_message(fallback, password)
                self._password_sent.add(sent_key)
                self.log(
                    f"[{account}] 已在「{group_name}」发送口令"
                    f"（@{username} 要求：{password}）"
                )
                self._emit(
                    {
                        "type": "speak_done",
                        "account": account,
                        "group": group_name,
                        "password": password,
                    }
                )
            except Exception as exc:
                self.log(f"[{account}] 发送口令失败（{group_name}）: {str(exc)[:120]}")
                self._emit(
                    {
                        "type": "speak_failed",
                        "account": account,
                        "group": group_name,
                        "password": password,
                        "error": str(exc)[:120],
                    }
                )

    async def _resolve_invite_target(
        self, client: Any, invite_hash: str, invite_link: str
    ) -> Optional[int]:
        """私有邀请链接 → 真实 chat id（未加入则先加入）。

        CheckChatInvite 已加入时直接返回群组；未加入走 join_chat 后再查。
        审批类链接（JoinRequest）拿不到 chat 时返回 None。
        """
        try:
            from pyrogram import raw as _raw
            from pyrogram import utils as _utils

            r = await client.invoke(
                _raw.functions.messages.CheckChatInvite(hash=invite_hash)
            )
            chat = getattr(r, "chat", None)
            if chat is None:
                await client.join_chat(invite_link)
                await asyncio.sleep(3)
                r = await client.invoke(
                    _raw.functions.messages.CheckChatInvite(hash=invite_hash)
                )
                chat = getattr(r, "chat", None)
            if chat is None:
                return None
            # CheckChatInvite 的 chat 是 raw Chat/Channel（非 Peer），
            # 需按类型换算成带 -100 前缀的标记 id
            if isinstance(chat, _raw.types.Channel):
                return _utils.get_channel_id(chat.id)
            if isinstance(chat, _raw.types.Chat):
                return -chat.id
            return None
        except Exception:
            logger.debug("私有邀请链接解析口令群失败", exc_info=True)
            return None

    async def _find_dialog_by_title(self, client: Any, title: str) -> Optional[int]:
        """按群名在账号已加入会话中精确匹配，返回 chat id（找不到返回 None）。"""
        target_title = (title or "").strip()
        if not target_title:
            return None
        try:
            async for dlg in client.get_dialogs(limit=300):
                chat = getattr(dlg, "chat", None)
                chat_title = (getattr(chat, "title", "") or "").strip()
                chat_id = getattr(chat, "id", None)
                if chat_title == target_title and chat_id is not None:
                    return chat_id
        except Exception:
            logger.debug("按群名遍历会话匹配口令群失败", exc_info=True)
        return None

    async def _is_member(self, client: Any, target: str) -> Optional[bool]:
        """检查当前账号是否已是 target 的成员。

        返回 True（已加入）/ False（未加入）/ None（无法判断，按未处理继续）。
        """
        try:
            await client.get_chat_member(target, "me")
            return True
        except Exception as exc:
            msg = str(exc).upper()
            if "NOT_PARTICIPANT" in msg or "USER_NOT_PARTICIPANT" in msg:
                return False
            return None  # 权限不足/无法解析等，交给 join 环节处理

    async def _ensure_monitor_membership(
        self, account: str, client: Any, chat: Any
    ) -> None:
        """任务监控对象成员检查：账号未加入该频道/群组时自动尝试加入。

        每个账号 × 监控对象每次启动只尝试一次（_join_cache 去重，限流除外）；
        数字 chat_id 在账号未加入的情况下无法解析，优先用缓存的 @username
        兜底加入（公开频道/群组），两者都失败才记失败日志。
        """
        key = f"{account}|monitor|{str(chat).lower()}"
        if key in self._join_cache:
            return
        # 自动加入被限流暂停中：不消耗本次尝试机会，恢复后重试
        if (
            self._join_suspended_until is not None
            and datetime.now() < self._join_suspended_until
        ):
            return
        already = await self._is_member(client, chat)
        if already is True:
            self._join_cache.add(key)
            self._emit({"type": "join_resolved", "account": account, "chat": str(chat)})
            return
        # 数字 chat_id 无法解析时无法直接 join，优先尝试 @username（公开频道）
        uname = self.chat_usernames.get(str(chat))
        candidates: List[Any] = []
        if uname:
            candidates.append(f"@{uname}")
        candidates.append(chat)
        last_exc: Optional[Exception] = None
        for cand in candidates:
            try:
                joined = await client.join_chat(cand)
                title = str(getattr(joined, "title", "") or chat)
                self._join_cache.add(key)
                self.log(f"[{account}] 未加入监控对象「{title}」，已自动加入")
                self._emit(
                    {"type": "join_resolved", "account": account, "chat": str(chat)}
                )
                return
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if "ALREADY_PARTICIPANT" in msg or "already" in msg.lower():
                    self._join_cache.add(key)
                    self._emit(
                        {"type": "join_resolved", "account": account, "chat": str(chat)}
                    )
                    return
                if "INVITE_REQUEST_SENT" in msg:
                    self._join_cache.add(key)
                    self.log(
                        f"[{account}] 已提交加入 {chat} 的申请（私有群/频道，待管理员审批）"
                    )
                    self._emit(
                        {"type": "join_resolved", "account": account, "chat": str(chat)}
                    )
                    return
                if "FLOOD_WAIT" in msg:
                    wait = self._flood_seconds(exc) or 60
                    self._join_suspended_until = datetime.now() + timedelta(
                        seconds=wait + 10
                    )
                    self._join_suspend_logged = False
                    self.log(
                        f"[{account}] 加入监控对象 {chat} 触发限流（需等待 {wait} 秒），"
                        f"自动加入暂停至 {self._join_suspended_until:%H:%M}"
                    )
                    self._emit(
                        {
                            "type": "join_failed",
                            "account": account,
                            "chat": str(chat),
                            "error": f"触发限流，需等待 {wait} 秒后重试",
                        }
                    )
                    self._join_cache.discard(key)  # 限流不消耗尝试机会，恢复后重试
                    return
                # 其他失败（如数字 id 无法解析）：还有候选目标则继续尝试
        self._join_cache.add(key)
        self.log(
            f"[{account}] 自动加入监控对象 {chat} 失败: {str(last_exc)[:120]}"
            "（私有频道仅凭数字 chat_id 无法加入，请账号先加入一次或手动填写 @username）"
        )
        self._emit(
            {
                "type": "join_failed",
                "account": account,
                "chat": str(chat),
                "error": str(last_exc)[:120],
            }
        )

    async def _append_recent_bot_messages(
        self, account: str, client: Any, username: str, candidates: List[Any]
    ) -> None:
        """拉取 bot 最新 4 条消息并入候选尾部（失败仅 debug 日志）。"""
        try:
            async for recent in client.get_chat_history(username, limit=4):
                candidates.append(recent)
                await self._check_win_message(account, client, username, recent)
        except Exception:
            logger.debug("拉取 bot 最新消息失败", exc_info=True)

    async def _handle_emoji_verification(
        self,
        account: str,
        client: Any,
        username: str,
        messages: List[Any],
    ) -> bool:
        """处理 bot 的「防脚本验证」（按正文顺序点击 emoji 回调按钮）。

        返回是否命中验证消息。按候选中最新一条验证消息的按钮顺序点击；
        解析不出顺序或缺按钮时不乱点（点错有冷却惩罚），只记日志。
        同一条验证消息进程内只点击一次，防止重发 start 后误点旧验证。
        """
        verify_msgs = [m for m in messages if is_emoji_verify_message(m)]
        if not verify_msgs:
            return False
        # 防脚本验证响应的是本次 start：取候选里最新一条，避免点到旧验证
        message = max(verify_msgs, key=lambda m: getattr(m, "id", 0) or 0)
        msg_key = f"{account}|{username}|{getattr(message, 'id', 0)}"
        if msg_key in self._emoji_verify_handled:
            return True
        self._emoji_verify_handled.add(msg_key)
        order = _extract_emoji_order(_message_text(message) or "")
        if not order:
            self.log(f"[{account}] @{username} 防脚本验证：未能解析点击顺序，跳过")
            return True
        all_btns: List[Any] = []
        markup = getattr(message, "reply_markup", None)
        for row in getattr(markup, "inline_keyboard", None) or []:
            all_btns.extend(row or [])
        targets: List[Tuple[str, Any]] = []
        for emoji in order:
            match = next(
                (
                    btn
                    for btn in all_btns
                    if emoji in str(getattr(btn, "text", "") or "")
                    and getattr(btn, "callback_data", None)
                ),
                None,
            )
            if match is None:
                self.log(
                    f"[{account}] @{username} 防脚本验证：键盘上没有「{emoji}」按钮，"
                    "不乱点（点错会冷却），跳过"
                )
                return True
            targets.append((emoji, match))
        self.log(f"[{account}] @{username} 防脚本验证：按顺序点击 {' → '.join(order)}")
        for emoji, btn in targets:
            if self._stop is not None and self._stop.is_set():
                return True
            try:
                await client.request_callback_answer(
                    chat_id=username,
                    message_id=getattr(message, "id", None),
                    callback_data=btn.callback_data,
                )
            except Exception as exc:
                self.log(f"[{account}] @{username} 点击验证按钮「{emoji}」失败: {exc}")
                return True
            # 模拟人工点击节奏，避免过快触发风控
            await asyncio.sleep(random.uniform(0.8, 1.6))
        return True

    async def _click_join_retry_button(
        self,
        account: str,
        client: Any,
        username: str,
        candidates: List[Any],
    ) -> bool:
        """优先点击「我已加入/重试参与」按钮完成重试参与。

        部分抽奖 bot（如 @dbchoujiang123_bot）在要求加入频道后提示
        「需要再点一次 我已加入 重试参与」：这是 bot 设计的重试入口，
        点击比重发 start 少触发一轮防脚本验证。取候选中最新一条带该
        按钮的消息点击（callback 按钮），点击后刷新候选并核对回复。

        返回是否已点击（True＝无需再重发 start；点击失败返回 False，
        由调用方兜底重发 start）。
        """
        for message in reversed(candidates):
            markup = getattr(message, "reply_markup", None)
            for row in getattr(markup, "inline_keyboard", None) or []:
                for btn in row or []:
                    text = str(getattr(btn, "text", "") or "")
                    if not any(kw in text for kw in JOIN_RETRY_BUTTON_KEYWORDS):
                        continue
                    if not getattr(btn, "callback_data", None):
                        continue
                    try:
                        answer = await client.request_callback_answer(
                            chat_id=username,
                            message_id=getattr(message, "id", None),
                            callback_data=btn.callback_data,
                        )
                        # 弹窗提示终态（抽奖已结束）＝不再跟进
                        popup = str(getattr(answer, "text", "") or "").strip()
                        if popup and _POPUP_ENDED_RE.search(popup):
                            self._note_ended(account, username)
                            self.log(
                                f"[{account}] @{username} 弹窗提示「{popup[:40]}」，"
                                "抽奖已结束，跳过后续跟进"
                            )
                            return True
                        self.log(
                            f"[{account}] 已点击 @{username} 的重试按钮「{text}」"
                            "（我已加入，重试参与）"
                        )
                        await asyncio.sleep(2)
                        await self._append_recent_bot_messages(
                            account, client, username, candidates
                        )
                        await self._verify_after_click(
                            account, client, username, candidates
                        )
                        return True
                    except Exception as exc:
                        self.log(
                            f"点击 @{username} 重试按钮失败，改用重发 start: {exc}"
                        )
                        return False
        return False

    async def _click_confirm(
        self,
        account: str,
        client: Any,
        username: str,
        start_msg: Any,
        param: str = "",
        source_message: Any = None,
    ) -> None:
        """start 后跟进：自动加入 bot 消息中的频道（若开启），并点确认按钮。"""
        candidates: List[Any] = []
        # 原始抽奖帖参与条件中的频道（如「加入：xx 频道」）与 bot 回复一并进行提取，
        # 否则只覆盖 bot 回复会漏加原始帖列出的频道
        if source_message is not None:
            candidates.append(source_message)
        if start_msg is not None:
            candidates.append(start_msg)
            # bot 直接回复「已结束」（活动提前结束）＝终态，立即返回，
            # 不轮询、不加入、不重发 start
            if reply_says_ended(start_msg):
                self._note_ended(account, username)
                self.log(f"[{account}] @{username} 回复抽奖已结束，跳过后续跟进")
                return
        if self.confirm_wait > 0:
            deadline = asyncio.get_event_loop().time() + self.confirm_wait
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(1.5)
                fresh: List[Any] = []
                try:
                    async for recent in client.get_chat_history(username, limit=4):
                        fresh.append(recent)
                        await self._check_win_message(account, client, username, recent)
                except Exception:
                    logger.debug("轮询拉取 bot 最新消息失败", exc_info=True)
                candidates.extend(fresh)
                # 仅核对最新一条（get_chat_history 按新到旧返回）：
                # 会话里历史遗留的旧「已结束」回复不应误杀本次参与
                if fresh and reply_says_ended(fresh[0]):
                    self._note_ended(account, username)
                    self.log(f"[{account}] @{username} 回复抽奖已结束，跳过后续跟进")
                    return
                if any(self._find_confirm_button(m) is not None for m in candidates):
                    break
                # 防脚本验证消息没有确认按钮：提前结束轮询交给验证处理
                if any(is_emoji_verify_message(m) for m in candidates):
                    break
        # 「防脚本验证」（如 @dbchoujiang123_bot）：按正文顺序点击 emoji 按钮
        # 完成人机校验，再刷新候选，让后续加入/确认流程看到验证后的消息
        if await self._handle_emoji_verification(account, client, username, candidates):
            await asyncio.sleep(2)
            await self._append_recent_bot_messages(
                account, client, username, candidates
            )
        if self.auto_join:
            did_join = False
            had_join_targets = False
            for message in candidates:
                # 排除指向当前 bot 自身的 t.me 链接（按钮「点击参与抽奖」等）：
                # 那是参与入口不是加入目标，交给下方 URL 按钮点击逻辑处理
                join_targets = [
                    t
                    for t in extract_channel_targets(message)
                    if str(t).lower().lstrip("@") != username.lower()
                ]
                if join_targets or (
                    username.lower() == LOTTERYBEST_BOT
                    and extract_join_status_targets(message)
                ):
                    had_join_targets = True
                if await self._join_from_message(account, client, username, message):
                    did_join = True
            if did_join:
                # 加入后稍等，再拉一轮消息（bot 可能更新了按钮/提示）
                await asyncio.sleep(3)
                try:
                    async for recent in client.get_chat_history(username, limit=4):
                        candidates.append(recent)
                        await self._check_win_message(account, client, username, recent)
                except Exception:
                    logger.debug("加入频道后拉取 bot 最新消息失败", exc_info=True)
            # 帖内口令任务：加入完成后按 bot 清单定位口令群组并发送口令，
            # 发送后的重发 start（下方）让 bot 核验口令达标
            await self._send_password_tasks(
                account, client, username, source_message, candidates
            )
            # 通用策略：多数抽奖 bot 要求「先加入频道再点一次抽奖」——
            # bot 消息里出现过加入要求时，加入校验完成后优先点
            # 「我已加入/重试参与」按钮完成重试（bot 设计的重试入口）；
            # 没有该按钮时兜底重发一次 start 完成参与（此前可能已全部
            # 加入过，不必本次有新加入）
            if had_join_targets:
                if await self._click_join_retry_button(
                    account, client, username, candidates
                ):
                    return
                self.log(
                    f"[{account}] @{username} 要求加入频道后重新参与，再次发送 start"
                )
                try:
                    restart_msg = await client.start_bot(username, param)
                    if restart_msg is not None and reply_says_ended(restart_msg):
                        # 加入期间活动提前结束，bot 终态回复，不再点按钮
                        self._note_ended(account, username)
                        self.log(
                            f"[{account}] @{username} 回复抽奖已结束，跳过后续跟进"
                        )
                        return
                    # 重发 start 后：新回复与最新历史优先；重发前的消息保留在
                    # 候选末尾兜底——部分 bot（如百宝箱抽奖「全部关注后请点击
                    # 重新参与抽奖」）对重复 start 不再回复带按钮的新消息，
                    # 只有原消息上的「重新参与抽奖」按钮可点
                    pre_restart = candidates
                    candidates = [restart_msg] if restart_msg is not None else []
                    await asyncio.sleep(2)
                    async for recent in client.get_chat_history(username, limit=4):
                        candidates.append(recent)
                        await self._check_win_message(account, client, username, recent)
                    candidates.extend(pre_restart)
                    # 重发 start 可能再次触发防脚本验证：完成后刷新候选
                    if await self._handle_emoji_verification(
                        account, client, username, candidates
                    ):
                        await asyncio.sleep(2)
                        await self._append_recent_bot_messages(
                            account, client, username, candidates
                        )
                except Exception as exc:
                    self.log(f"重新参与 @{username} 失败: {exc}")
            elif username.lower() in BOTS_RESTART_AFTER_JOIN:
                # 强制名单：无加入要求也重发一次
                self.log(f"[{account}] @{username} 加入校验完成，重新发送 start")
                try:
                    restart_msg = await client.start_bot(username, param)
                    if restart_msg is not None and reply_says_ended(restart_msg):
                        self._note_ended(account, username)
                        self.log(
                            f"[{account}] @{username} 回复抽奖已结束，跳过后续跟进"
                        )
                        return
                    pre_restart = candidates
                    candidates = [restart_msg] if restart_msg is not None else []
                    await asyncio.sleep(2)
                    async for recent in client.get_chat_history(username, limit=4):
                        candidates.append(recent)
                        await self._check_win_message(account, client, username, recent)
                    candidates.extend(pre_restart)
                    # 重发 start 可能再次触发防脚本验证：完成后刷新候选
                    if await self._handle_emoji_verification(
                        account, client, username, candidates
                    ):
                        await asyncio.sleep(2)
                        await self._append_recent_bot_messages(
                            account, client, username, candidates
                        )
                except Exception as exc:
                    self.log(f"重新参与 @{username} 失败: {exc}")

        def _confirm_log(text: str) -> str:
            """按钮文字含「已参加」= bot 已确认参与成功，日志按成功口径输出。"""
            if "已参加" in text:
                return f"@{username} 参与成功（{text}）"
            return f"已点击 @{username} 的确认按钮「{text}」"

        stop_set = self._stop is not None and self._stop.is_set()
        if had_join_targets and not stop_set:
            # 加入频道后稍等再点确认按钮，给 bot 侧成员状态同步留时间，
            # 避免「立刻参加抽奖」点了却因身份未同步而不生效
            await asyncio.sleep(5)
        for message in candidates:
            btn = self._find_confirm_button(message)
            if btn is None:
                continue
            try:
                if getattr(btn, "callback_data", None):
                    answer = await client.request_callback_answer(
                        chat_id=username,
                        message_id=message.id,
                        callback_data=btn.callback_data,
                    )
                    # 抽奖结束后部分 bot 只弹窗提示、不发新消息：读弹窗文案，
                    # 命中终态即返回，不进入回复核对（避免误读旧消息）
                    popup = str(getattr(answer, "text", "") or "").strip()
                    if popup and _POPUP_ENDED_RE.search(popup):
                        self._note_ended(account, username)
                        self.log(
                            f"[{account}] @{username} 弹窗提示「{popup[:40]}」，"
                            "抽奖已结束，跳过后续跟进"
                        )
                        return
                    self.log(_confirm_log(btn.text))
                    await self._verify_after_click(
                        account, client, username, candidates
                    )
                    return
                # URL 按钮：仅当链接指向同一 bot（含/不含 start 参数）时才「点击」
                # （等同按链接参数重发 start 完成参与），其他链接一律不碰
                url_btn = str(getattr(btn, "url", "") or "").strip()
                link = parse_bot_start_link(url_btn)
                plain_same_bot = False
                if link is None:
                    m = _TME_RE.search(url_btn)
                    if m and not (m.group("rest") or ""):
                        plain_same_bot = m.group("name").lower() == username.lower()
                if link is not None and link[0].lower() == username.lower():
                    await client.start_bot(username, link[1])
                    self.log(
                        f"已点击 @{username} 的参与按钮「{btn.text}」"
                        f"（按链接参数重新参与）"
                    )
                    await self._verify_after_click(
                        account, client, username, candidates
                    )
                    return
                if plain_same_bot:
                    await client.start_bot(username, "")
                    self.log(
                        f"已点击 @{username} 的参与按钮「{btn.text}」（重发 start）"
                    )
                    await self._verify_after_click(
                        account, client, username, candidates
                    )
                    return
            except Exception as exc:
                self.log(f"点击 @{username} 确认按钮失败: {exc}")
                return

    def _emit_speech_tasks(self, account: str, bot_username: str, message: Any) -> None:
        """报名成功回复中的发言数任务 → 消息中心提醒（还需发言条数）。"""
        tasks = extract_speech_tasks(message)
        if not tasks:
            return
        title = extract_giveaway_title(message)
        for group, required in tasks:
            self._emit(
                {
                    "type": "speech_required",
                    "account": account,
                    "bot": bot_username,
                    "title": title,
                    "group": group,
                    "required": required,
                }
            )

    async def _verify_after_click(
        self,
        account: str,
        client: Any,
        username: str,
        candidates: Optional[List[Any]] = None,
    ) -> None:
        """点击确认/参与按钮后核对 bot 回复，把结果写进日志（不再盲点）。"""
        try:
            await asyncio.sleep(3)
            if self._stop is not None and self._stop.is_set():
                return
            async for msg in client.get_chat_history(username, limit=1):
                await self._check_win_message(account, client, username, msg)
                text = (_message_text(msg) or "").strip()
                if not text:
                    return
                flat = " ".join(str(text).split())[:40]
                if "已参加" in text or reply_says_signup_success(msg):
                    # 「已参加…」或「已成功报名抽奖（未达标β）/已获得抽奖资格」
                    # 均为参与成功（未达标仅剩发言数等激活任务，标记为完成）
                    self.log(f"@{username} 参与成功（{flat}）")
                    # 报名成功后的激活任务清单若仍列出 ❌ 口令任务
                    # （发送口令﹝xx﹞），说明 bot 未计入此前发送的口令
                    # （部分 bot 只核验报名之后在群里的发言），立即按清单
                    # 补发一次；✅ 已完成行由解析自动跳过，发言数等其余
                    # 激活任务不处理
                    try:
                        await self._send_password_tasks(
                            account,
                            client,
                            username,
                            None,
                            [msg],
                            ignore_sent=True,
                            link_messages=[msg, *(candidates or [])],
                        )
                    except Exception:
                        logger.debug("报名成功后补发口令任务失败", exc_info=True)
                    # 清单中未达标（❌）的发言数任务：推送「还需发言 N 条」提醒
                    self._emit_speech_tasks(account, username, msg)
                elif not reply_says_ended(msg):
                    self.log(f"@{username} 点击后回复：{flat}")
        except Exception:
            logger.debug("核对确认按钮回复失败", exc_info=True)

    def _find_confirm_button(self, message: Any) -> Optional[Any]:
        reply_markup = getattr(message, "reply_markup", None)
        for row in getattr(reply_markup, "inline_keyboard", None) or []:
            for btn in row or []:
                text = str(getattr(btn, "text", "") or "")
                if not (
                    getattr(btn, "callback_data", None) or getattr(btn, "url", None)
                ):
                    continue
                if any(kw in text for kw in self.confirm_keywords):
                    return btn
        return None
