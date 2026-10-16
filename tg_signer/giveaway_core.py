"""自动抽奖核心：识别频道消息中的 t.me/<bot>?start=<参数> 链接并自动参与。

典型链路：频道抽奖帖 -> 消息文本/隐藏超链接/内联按钮中的 bot 深度链接 ->
调用 messages.StartBot（等价于点开链接）-> 机器人若回复带「确认参与」类
回调按钮，再自动点击一次。

纯解析函数不依赖网络，便于单测；GiveawayEngine 负责连接、
历史扫描、去重与参与。

触发方式：启动时可选自动扫描最近消息 + 手动扫描；同一引擎常驻连接，
便于随时发起扫描与参与。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from tg_signer.core import get_client

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
# FLOOD_WAIT 失败记录的冷却时间（秒），冷却后允许重试
FLOOD_COOLDOWN = 3600
# 扫描停止条件：连续这么多条「已开奖」的抽奖帖后，认为再往前都是旧抽奖
EXPIRED_STOP_STREAK = 5
# 单频道单次扫描的硬上限（条），防止无开奖日期标注时无限回溯
SCAN_HARD_CAP = 1000

# t.me/<username> + 后续路径/查询（用户名 5~32 位，字母/数字/下划线）
_TME_RE = re.compile(
    r"(?:https?://)?t\.me/(?P<name>[A-Za-z0-9_]{5,32})(?P<rest>[/?#]\S*)?",
    re.IGNORECASE,
)
# tg://resolve?domain=<u>&start=<p> 形式
_TG_RESOLVE_RE = re.compile(r"tg://resolve\?", re.IGNORECASE)
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
_TIME_HM_RE = re.compile(r"(?P<h>\d{1,2})\s*[:点时]\s*(?P<mi>\d{1,2})\s*分?|(?P<h2>\d{1,2})点")


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
    tail = window[dm.end(): dm.end() + 12]
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
        getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    )


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
    """提取消息中全部去重后的 (用户名, start参数)，按出现顺序排列。"""
    found: List[Tuple[str, str]] = []
    seen: set = set()
    for candidate in _iter_candidate_urls(message):
        # 一个候选字符串里可能含多个链接（整段消息文本）
        for match in re.finditer(r"\S+", candidate):
            piece = match.group(0)
            parsed = parse_bot_start_link(piece)
            if parsed is None:
                continue
            key = (parsed[0].lower(), parsed[1])
            if key in seen:
                continue
            seen.add(key)
            found.append(parsed)
    return found


def message_matches_keywords(message: Any, keywords: Iterable[str]) -> bool:
    """消息文本是否包含任一关键词；关键词为空表示全部放行。"""
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not kws:
        return True
    text = _message_text(message)
    return any(kw in text for kw in kws)


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
                    if any(
                        k in query
                        for k in ("start", "startgroup", "startapp")
                    ):
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
            for old_key in sorted(
                bucket, key=lambda k: str(bucket[k].get("ts") or "")
            )[: len(bucket) - MAX_STATE_PER_ACCOUNT]:
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
        # FLOOD_WAIT 熔断：{account|bot_lower: 连续次数}；达到上限加入 _flood_bots
        self._flood_counts: Dict[str, int] = {}
        self._flood_bots: set = set()
        # 跳过提示去重（已开奖消息/参与上限），避免每轮扫描重复刷日志
        self._skip_logged: set = set()
        self.chats: List = []
        self.keywords: Tuple[str, ...] = DEFAULT_KEYWORDS
        self.max_joins: Optional[int] = None
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
        delay_range: Tuple[float, float] = (3.0, 10.0),
        confirm_wait: int = 8,
        confirm_keywords: Optional[Iterable[str]] = None,
        state: Optional[GiveawayState] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        on_finish: Optional[Callable[[], None]] = None,
    ) -> bool:
        if self.running:
            log("任务已在运行中")
            return False
        self._state = state or self._state
        self.chats = list(chats)
        self.keywords = tuple(str(k).strip() for k in (keywords or ()) if str(k).strip())
        self.confirm_keywords = tuple(confirm_keywords or DEFAULT_CONFIRM_KEYWORDS)
        self.confirm_wait = max(0, int(confirm_wait))
        self.auto_join = bool(auto_join)
        self.max_joins = max(0, int(max_joins)) if max_joins else None
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
            pass
        if self._task is task:
            self._task = None

    def _emit(self, payload: dict) -> None:
        if self.on_event is not None:
            try:
                self.on_event(payload)
            except Exception:
                pass

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
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
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
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            except Exception as exc:
                self.log(f"「{account}」连接失败: {exc}")
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

    async def _ensure_connected(self, client: Any) -> bool:
        """确保 client 可用；断线时尝试重连一次。"""
        if getattr(client, "is_connected", False):
            return True
        try:
            await asyncio.wait_for(
                client.connect(), timeout=self.CONNECT_TIMEOUT
            )
        except Exception:
            pass
        return bool(getattr(client, "is_connected", False))

    async def _run(self, accounts: List[str], auto_scan: bool) -> None:
        try:
            await self._connect_accounts(accounts)
            if not self._clients:
                self.log("没有可用账号，任务结束")
                return
            mode_kw = "，".join(self.keywords) if self.keywords else "全部 bot 链接"
            self.log(
                f"任务已启动：{len(self._clients)} 个账号、{len(self.chats)} 个频道，"
                f"关键词：{mode_kw}"
            )
            if auto_scan and not self._stop.is_set():
                await self.scan_now()
            await self._stop.wait()
        finally:
            for client in self._clients.values():
                try:
                    await client.disconnect()
                except Exception:
                    pass
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
                    try:
                        async for message in client.get_chat_history(chat):
                            scanned += 1
                            if scanned > SCAN_HARD_CAP:
                                self.log(
                                    f"[{account}] {chat} 已扫描 "
                                    f"{SCAN_HARD_CAP} 条，停止该频道本轮扫描"
                                )
                                break
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

    def _chat_title(self, message: Any) -> str:
        chat = getattr(message, "chat", None)
        return str(getattr(chat, "title", "") or getattr(chat, "id", "") or "")

    async def _process_message(
        self, account: str, client: Any, message: Any
    ) -> Optional[str]:
        """处理单条消息。返回 "joined"（本次有参与）/"expired"（已开奖跳过）/None。"""
        if self._stop is not None and self._stop.is_set():
            return None
        if not message_matches_keywords(message, self.keywords):
            return None
        links = extract_bot_links(message)
        if not links:
            return None
        channel = self._chat_title(message)
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
                self.log(f"[{account}] 已达参与上限 {self.max_joins} 个，后续命中不再参与")
            return None
        lock = self._locks.setdefault(account, asyncio.Lock())
        participated = 0
        for username, param in links:
            if self._stop is not None and self._stop.is_set():
                break
            if self.max_joins is not None and self.joined >= self.max_joins:
                break
            if self._state is not None and self._state.has(account, username, param):
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
                    account, client, username, param, channel
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
        self, account: str, client: Any, username: str, param: str, channel: str
    ) -> None:
        if self._state is not None and self._state.has(account, username, param):
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
        self.log(
            f"{datetime.now():%H:%M:%S} [已参与] {account} -> @{username} "
            f"（来自 {channel or '未知频道'}）"
        )
        self._emit(
            {
                "account": account,
                "bot": username,
                "channel": channel,
                "ok": True,
            }
        )
        await self._click_confirm(account, client, username, start_msg)

    async def _join_from_message(
        self, account: str, client: Any, bot_username: str, message: Any
    ) -> bool:
        """激进模式：把消息中出现的频道/群组链接逐个加入。返回是否发生加入。"""
        joined_any = False
        for target in extract_channel_targets(message):
            if target.lower().lstrip("@") == bot_username.lower():
                continue  # 跳过 bot 自己
            key = f"{account}|{target.lower()}"
            if key in self._join_cache:
                continue
            self._join_cache.add(key)
            try:
                chat = await client.join_chat(target)
                title = str(getattr(chat, "title", "") or target)
                self.log(
                    f"[{account}] 已加入 {title}（来自 @{bot_username} 的消息）"
                )
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
                else:
                    self.log(f"[{account}] 加入 {target} 失败: {msg[:120]}")
        return joined_any

    async def _click_confirm(
        self, account: str, client: Any, username: str, start_msg: Any
    ) -> None:
        """start 后跟进：自动加入 bot 消息中的频道（若开启），并点确认按钮。"""
        candidates: List[Any] = []
        if start_msg is not None:
            candidates.append(start_msg)
        if self.confirm_wait > 0:
            deadline = asyncio.get_event_loop().time() + self.confirm_wait
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(1.5)
                try:
                    async for recent in client.get_chat_history(username, limit=4):
                        candidates.append(recent)
                except Exception:
                    pass
                if any(
                    self._find_confirm_button(m) is not None for m in candidates
                ):
                    break
        if self.auto_join:
            did_join = False
            for message in candidates:
                if await self._join_from_message(account, client, username, message):
                    did_join = True
            if did_join:
                # 加入后稍等，再拉一轮消息（bot 可能更新了按钮/提示）
                await asyncio.sleep(3)
                try:
                    async for recent in client.get_chat_history(username, limit=4):
                        candidates.append(recent)
                except Exception:
                    pass
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
