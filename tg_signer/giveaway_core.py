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

# 命中「确认参与」类按钮的默认关键词（仅点击带 callback_data 的按钮，不碰 URL 按钮）
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
STATE_FILE = "giveaway_state.json"
# 每个账号最多保留的去重记录数，超出丢弃最旧的
MAX_STATE_PER_ACCOUNT = 1000
# 同一 bot 连续 FLOOD_WAIT 达到该次数后，本轮运行熔断（不再对其 start_bot）
MAX_FLOOD_STRIKES = 3

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
    """
    if not text or not _DRAW_HINT_RE.search(text):
        return None
    now = now or datetime.now()
    hint = _DRAW_HINT_RE.search(text)
    start = max(0, hint.start() - 20)
    end = min(len(text), hint.end() + 20)
    window = text[start:end]
    dm = _DATE_YMD_RE.search(window)
    if dm is None:
        return None
    year = int(dm.group("y")) if dm.group("y") else now.year
    try:
        dt = datetime(year, int(dm.group("m")), int(dm.group("d")))
    except ValueError:
        return None
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


def _iter_candidate_urls(message: Any) -> Iterable[str]:
    """从消息中枚举可能含 bot 链接的 URL：明文文本 + 隐藏超链接实体 + 内联按钮。"""
    text = _message_text(message)
    if text:
        yield text
    for attr in ("entities", "caption_entities"):
        for ent in getattr(message, attr, None) or []:
            ent_type = str(getattr(ent, "type", ""))
            url = getattr(ent, "url", None)
            # 文本超链接实体（pyrogram: MessageEntityType.TEXT_URL）
            if "TEXT_URL" in ent_type.upper() and url:
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


def message_requires_speech(message: Any) -> bool:
    """消息是否带「发言数」类参与条件。"""
    return bool(SPEECH_REQUIRE_RE.search(_message_text(message) or ""))


# 口令参与条件：「发送口令﹝xx﹞」「发送口令激活」及「口令提示」段落头，
# 出现即代表参与需先发送口令（双向账号无法完成该私聊互动）。
PASSWORD_REQUIRE_RE = re.compile(r"发送口令|口令提示")


def message_requires_password(message: Any) -> bool:
    """消息是否带「发送口令」类参与条件。"""
    return bool(PASSWORD_REQUIRE_RE.search(_message_text(message) or ""))


# bot 回复「抽奖活动已结束」类文案（活动可能提前结束）＝终态：
# 跳过加入、重发、点按钮等一切后续跟进，避免对已结束的抽奖反复发 start
ENDED_REPLY_RE = re.compile(r"抽奖活动已结束|活动已结束|抽奖已结束")


def reply_says_ended(message: Any) -> bool:
    """bot 回复是否表明该抽奖已结束（终态）。"""
    return bool(ENDED_REPLY_RE.search(_message_text(message) or ""))


# —— LotteryBestBot 式回复解析 ——
# start 后不满足条件时 bot 回复「您需加入指定的群组/频道」，逐行列出
# 「🎫 加入-名称 ❌/✅」，名称是带 TEXT_URL 隐藏链接的文本。只提取
# ❌（未加入）行的链接目标，✅ 已加入行跳过；无链接实体的行不处理。
LOTTERYBEST_BOT = "lotterybestbot"


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

    for ent in getattr(message, "entities", None) or []:
        if "TEXT_URL" not in str(getattr(ent, "type", "")).upper():
            continue
        url = getattr(ent, "url", None)
        if not url:
            continue
        offset = int(getattr(ent, "offset", 0) or 0)
        line = next((lt for ls, le, lt in bounds if ls <= offset < le), "")
        if "加入" not in line or "❌" not in line or "✅" in line:
            continue
        m = _TME_CHANNEL_RE.search(str(url))
        if m is None:
            continue
        target = m.group("target")
        rest = m.group("rest") or ""
        if target.lower().startswith(("+", "joinchat/")):
            _add(f"https://t.me/{target}")
        elif rest.startswith("/") or rest.startswith("?"):
            continue  # 帖子路径/bot 深度链接，不属于加入目标
        else:
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
                if rest.startswith("/"):
                    continue  # 帖子/消息路径链接，不能 join
                if rest.startswith("?"):
                    query = parse_qs(urlparse(rest).query)
                    if any(k in query for k in ("start", "startgroup", "startapp")):
                        continue  # bot 深度链接，不属于加入目标
                _add(target)
            for am in _AT_CHANNEL_RE.finditer(piece):
                _add(am.group(1))
    return targets


class GiveawayState:
    """按账号持久化已参与记录：{accounts: {account: {"bot|param": record}}}"""

    def __init__(self, workdir) -> None:
        self.path = Path(workdir) / STATE_FILE
        self._data: Dict[str, Dict[str, dict]] = {}
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
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def save(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"accounts": self._data}, ensure_ascii=False, indent=2),
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
        # 自动加入频道的限流暂停（JoinChannel FLOOD_WAIT 后暂停一段时间再恢复）
        self._join_suspended_until: Optional[datetime] = None
        self._join_suspend_logged = False
        # FLOOD_WAIT 熔断：{account|bot_lower: 连续次数}；达到上限加入 _flood_bots
        self._flood_counts: Dict[str, int] = {}
        self._flood_bots: set = set()
        # 跳过提示去重（已开奖消息/参与上限），避免每轮扫描重复刷日志
        self._skip_logged: set = set()
        # 历史补漏：已处理消息 id 水位线 {account|chat_key: {msg_id: None}（保序）}
        self._seen_msgs: Dict[str, Dict[int, None]] = {}
        # 补漏连续失败轮数与熔断恢复时间
        self._rescue_fail_streak = 0
        self._rescue_suspended_until: Optional[datetime] = None
        # 聊天类型缓存 {str(chat): 是否群组}：群组只扫置顶，频道才全量扫描
        self._chat_types: Dict[str, bool] = {}
        # 加入目标的类型缓存 {target_lower: 类型名/None}：过滤个人用户与失效目标
        self._join_target_types: Dict[str, Optional[str]] = {}
        # 忽略历史成功记录（start 时可覆盖）：默认遵循去重记录
        self.ignore_history = False
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
        self.failed = 0
        self.scanning = False
        self.scan_text = ""

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
        ignore_history: bool = False,
        two_way_accounts: Optional[Iterable[str]] = None,
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
        self.failed = 0
        self.scan_text = ""
        self._stopping = False
        self._flood_counts.clear()
        self._flood_bots.clear()
        self._seen_msgs.clear()
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
        for account in accounts:
            if self._stop.is_set():
                break
            client = None
            try:
                client = get_client(account, workdir=str(self.session_dir))
                ok = await asyncio.wait_for(
                    client.connect(), timeout=self.CONNECT_TIMEOUT
                )
                if not ok:
                    self.log(f"「{account}」会话未授权，已跳过")
                    await safe_disconnect(client)
                    continue
                # 连接成功即纳入账号表；昵称仅用于展示，获取失败不断开
                self._clients[account] = client
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
            if auto_scan and not self._stop.is_set():
                await self.scan_now()
            if self.rescue_interval > 0:
                await self._rescue_loop()
            else:
                await self._stop.wait()
        finally:
            for client in self._clients.values():
                await safe_disconnect(client)
            self._clients.clear()
            self.log("自动抽奖已停止")

    async def scan_now(self) -> None:
        """对所有账号 × 频道回溯扫描历史消息并自动参与。

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
            done = 0
            joined_before = self.joined
            for account, client in list(self._clients.items()):
                if self._stop is not None and self._stop.is_set():
                    return
                if not await self._ensure_connected(client):
                    self.log(f"[{account}] 连接不可用，跳过该账号本轮扫描")
                    continue
                for chat in self.chats:
                    if self._stop is not None and self._stop.is_set():
                        return
                    done += 1
                    self.scan_text = f"({done}/{total})"
                    expired_streak = 0
                    scanned = 0
                    # 先扫置顶消息：很多群的抽奖帖长期置顶；同时识别聊天类型
                    is_group = await self._scan_pinned(account, client, chat)
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

    async def _scan_pinned(
        self, account: str, client: Any, chat: Any
    ) -> Optional[bool]:
        """扫描频道/群组的置顶消息并参与（抽奖帖常被置顶）。

        通过 get_chat 拿到 pinned_message；置顶消息与历史消息走同一套
        处理流程（关键词过滤、开奖日期跳过、去重、自动加入）。
        同时缓存聊天类型：群组返回 True、频道返回 False、获取失败返回 None。
        """
        try:
            chat_info = await client.get_chat(chat)
        except Exception as exc:
            self.log(f"[{account}] 获取 {chat} 信息失败，跳过置顶扫描: {exc}")
            return self._chat_types.get(str(chat))
        type_name = getattr(getattr(chat_info, "type", None), "name", "")
        is_group = type_name in ("GROUP", "SUPERGROUP")
        first_detect = str(chat) not in self._chat_types
        self._chat_types[str(chat)] = is_group
        if first_detect:
            kind = "群组（仅扫置顶）" if is_group else "频道（置顶+历史）"
            title = getattr(chat_info, "title", "") or str(chat)
            self.log(f"[{account}] 识别 {chat} 类型：{kind}「{title}」")
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
                continue  # 本轮会话中已处理过
            try:
                await self._process_message(
                    account, client, msg, skip_keywords=is_group
                )
            except Exception as exc:
                self.log(f"[{account}] 处理 {chat} 置顶消息失败: {exc}")
        return is_group

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
        """执行一轮补漏：每账号 × 每频道重拉最近 rescue_limit 条消息。"""
        failures = 0
        joined_before = self.joined
        total = len(self._clients) * max(1, len(self.chats))
        done = 0
        self.scanning = True
        try:
            for account, client in list(self._clients.items()):
                if self._stop is not None and self._stop.is_set():
                    return
                if not await self._ensure_connected(client):
                    self.log(f"[{account}] 补漏：连接不可用，跳过该账号")
                    failures += 1
                    continue
                for chat in self.chats:
                    if self._stop is not None and self._stop.is_set():
                        return
                    done += 1
                    self.scan_text = f"补漏 ({done}/{total})"
                    try:
                        await self._rescue_channel(account, client, chat)
                    except asyncio.TimeoutError:
                        self.log(
                            f"[{account}] 补漏 {chat} 拉取超时"
                            f"（{RESCUE_FETCH_TIMEOUT}s）"
                        )
                        failures += 1
                    except Exception as exc:
                        self.log(f"[{account}] 补漏 {chat} 失败: {exc}")
                        failures += 1
            gained = self.joined - joined_before
            # 每轮必记一行，便于确认补漏循环在按间隔运行
            self.log(
                f"补漏轮次完成：参与 {gained} 个"
                + (f"，{failures} 个频道拉取失败" if failures else "")
            )
        finally:
            self.scanning = False
            self.scan_text = ""
            if self._state is not None:
                self._state.save()
        if failures == 0:
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
        self, account: str, client: Any, message: Any, skip_keywords: bool = False
    ) -> Optional[str]:
        """处理单条消息。返回 "joined"（本次有参与）/"expired"（已开奖跳过）/None。

        skip_keywords=True 时跳过关键词过滤（用于群组置顶消息：不受关键词影响）。
        """
        if self._stop is not None and self._stop.is_set():
            return None
        links = extract_bot_links(message)
        if not links:
            return None
        if not skip_keywords and not message_matches_keywords(message, self.keywords):
            # 消息含 bot 链接但关键词未命中：记录一条日志（同一条消息只提示一次）
            msg_key = (
                account,
                str(getattr(getattr(message, "chat", None), "id", "") or ""),
                str(getattr(message, "id", "") or ""),
            )
            if msg_key not in self._skip_logged:
                self._skip_logged.add(msg_key)
                self.log(
                    f"[{account}] 关键词未命中"
                    f"（{'，'.join(self.keywords) or '无'}），跳过"
                    f"：{_giveaway_title(message) or '(无文本)'}"
                )
            return None
        # 归属地限制过滤：仅参与无地区限制或含白名单地区的抽奖（对群组置顶同样生效）
        if not message_region_allowed(message):
            limits = parse_region_limits(_message_text(message)) or []
            first = limits[0] if limits else []
            msg_key = (
                account,
                str(getattr(getattr(message, "chat", None), "id", "") or ""),
                str(getattr(message, "id", "") or ""),
            )
            if msg_key not in self._skip_logged:
                self._skip_logged.add(msg_key)
                self.log(
                    f"[{account}] 归属地限制（{'、'.join(first) or '未知'}），跳过"
                    f"：{_giveaway_title(message) or '(无文本)'}"
                )
            return None
        # 发言数要求/口令要求 + 双向账户：无法达标，自动跳过（对群组置顶同样生效）
        two_way_reason = ""
        if account in self.two_way_accounts:
            if message_requires_speech(message):
                two_way_reason = "发言数要求"
            elif message_requires_password(message):
                two_way_reason = "口令要求"
        if two_way_reason:
            msg_key = (
                account,
                str(getattr(getattr(message, "chat", None), "id", "") or ""),
                str(getattr(message, "id", "") or ""),
            )
            if msg_key not in self._skip_logged:
                self._skip_logged.add(msg_key)
                self.log(
                    f"[{account}] 账号为双向且抽奖有{two_way_reason}，跳过"
                    f"：{_giveaway_title(message) or '(无文本)'}"
                )
            return None
        channel = self._chat_title(message)
        title = _giveaway_title(message)
        # 开奖日期早于当前时间：自动跳过（同一条消息只提示一次）
        draw_dt = parse_draw_date(_message_text(message))
        if draw_dt is not None and draw_dt < datetime.now():
            msg_key = (
                account,
                str(getattr(getattr(message, "chat", None), "id", "") or ""),
                str(getattr(message, "id", "") or ""),
            )
            if msg_key not in self._skip_logged:
                self._skip_logged.add(msg_key)
                self.log(
                    f"[{account}] 已开奖（{draw_dt:%m-%d %H:%M}），跳过"
                    + (f"：{channel}" if channel else "")
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
        lock = self._locks.setdefault(account, asyncio.Lock())
        participated = 0
        # 命中多少个抽奖入口一目了然（去重/失败仍会逐个处理）
        self.log(
            f"[{account}] 命中 {len(links)} 个抽奖链接："
            + "、".join(f"@{u}" for u, _ in links)
        )
        for username, param in links:
            if self._stop is not None and self._stop.is_set():
                break
            if self.max_joins is not None and self.joined >= self.max_joins:
                break
            if (
                self._state is not None
                and not self.ignore_history
                and self._state.has(account, username, param)
            ):
                continue
            if self._state is not None and self._state.is_blocked(
                account, username, param
            ):
                continue
            if self._flood_key(account, username) in self._flood_bots:
                continue
            async with lock:
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
        if (
            self._state is not None
            and not self.ignore_history
            and self._state.has(account, username, param)
        ):
            return
        if not await self._is_bot(account, client, username):
            self.log(f"[{account}] 跳过非机器人链接 @{username}")
            return
        lo, hi = self.delay_range
        if hi > 0:
            await asyncio.sleep(random.uniform(lo, max(lo, hi)))
        if self._stop is not None and self._stop.is_set():
            return
        try:
            start_msg = await client.start_bot(username, param)
        except Exception as exc:
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
        if self._state is not None:
            self._state.add(account, username, param, channel=channel)
            self._state.save()
        self.joined += 1
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
            targets = targets + [
                t
                for t in extract_join_status_targets(message)
                if t.lower() not in known
            ]
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
                except Exception:
                    logger.debug("轮询拉取 bot 最新消息失败", exc_info=True)
                candidates.extend(fresh)
                if any(reply_says_ended(m) for m in fresh):
                    self.log(f"[{account}] @{username} 回复抽奖已结束，跳过后续跟进")
                    return
                if any(self._find_confirm_button(m) is not None for m in candidates):
                    break
        if self.auto_join:
            did_join = False
            had_join_targets = False
            for message in candidates:
                if extract_channel_targets(message) or (
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
                except Exception:
                    logger.debug("加入频道后拉取 bot 最新消息失败", exc_info=True)
            # 通用策略：多数抽奖 bot 要求「先加入频道再点一次抽奖」——
            # bot 消息里出现过加入要求时，加入校验完成后重发一次 start
            # 完成参与（此前可能已全部加入过，不必本次有新加入）
            if had_join_targets:
                self.log(
                    f"[{account}] @{username} 要求加入频道后重新参与，再次发送 start"
                )
                try:
                    restart_msg = await client.start_bot(username, param)
                    if restart_msg is not None and reply_says_ended(restart_msg):
                        # 加入期间活动提前结束，bot 终态回复，不再点按钮
                        self.log(f"[{account}] @{username} 回复抽奖已结束，跳过后续跟进")
                        return
                    candidates = [restart_msg] if restart_msg is not None else []
                    await asyncio.sleep(2)
                    async for recent in client.get_chat_history(username, limit=4):
                        candidates.append(recent)
                except Exception as exc:
                    self.log(f"重新参与 @{username} 失败: {exc}")
            elif username.lower() in BOTS_RESTART_AFTER_JOIN:
                # 强制名单：无加入要求也重发一次
                self.log(f"[{account}] @{username} 加入校验完成，重新发送 start")
                try:
                    restart_msg = await client.start_bot(username, param)
                    if restart_msg is not None and reply_says_ended(restart_msg):
                        self.log(f"[{account}] @{username} 回复抽奖已结束，跳过后续跟进")
                        return
                    candidates = [restart_msg] if restart_msg is not None else []
                    await asyncio.sleep(2)
                    async for recent in client.get_chat_history(username, limit=4):
                        candidates.append(recent)
                except Exception as exc:
                    self.log(f"重新参与 @{username} 失败: {exc}")
        for message in candidates:
            btn = self._find_confirm_button(message)
            if btn is None:
                continue
            try:
                await client.request_callback_answer(
                    chat_id=username,
                    message_id=message.id,
                    callback_data=btn.callback_data,
                )
                self.log(f"已点击 @{username} 的确认按钮「{btn.text}」")
                return
            except Exception as exc:
                self.log(f"点击 @{username} 确认按钮失败: {exc}")
                return

    def _find_confirm_button(self, message: Any) -> Optional[Any]:
        reply_markup = getattr(message, "reply_markup", None)
        for row in getattr(reply_markup, "inline_keyboard", None) or []:
            for btn in row or []:
                text = str(getattr(btn, "text", "") or "")
                if not getattr(btn, "callback_data", None):
                    continue
                if any(kw in text for kw in self.confirm_keywords):
                    return btn
        return None
