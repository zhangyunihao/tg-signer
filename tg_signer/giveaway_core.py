"""自动抽奖核心：识别频道消息中的 t.me/<bot>?start=<参数> 链接并自动参与。

典型链路：频道抽奖帖 -> 消息文本/隐藏超链接/内联按钮中的 bot 深度链接 ->
调用 messages.StartBot（等价于点开链接）-> 机器人若回复带「确认参与」类
回调按钮，再自动点击一次。

纯解析函数不依赖网络，便于单测；GiveawayEngine 负责连接、实时监听、
历史扫描、去重与参与。
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

from pyrogram import filters
from pyrogram.handlers import EditedMessageHandler, MessageHandler

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
DEFAULT_KEYWORDS = ("抽奖",)
STATE_FILE = "giveaway_state.json"
# 每个账号最多保留的去重记录数，超出丢弃最旧的
MAX_STATE_PER_ACCOUNT = 1000

# t.me/<username> + 后续路径/查询（用户名 5~32 位，字母/数字/下划线）
_TME_RE = re.compile(
    r"(?:https?://)?t\.me/(?P<name>[A-Za-z0-9_]{5,32})(?P<rest>[/?#]\S*)?",
    re.IGNORECASE,
)
# tg://resolve?domain=<u>&start=<p> 形式
_TG_RESOLVE_RE = re.compile(r"tg://resolve\?", re.IGNORECASE)
# start 参数合法字符：字母数字下划线短横；URL 紧贴中文时取首个非法字符之前部分
_START_PARAM_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


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
        return self._key(username, param) in self._data.get(account, {})

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
    """自动抽奖后台引擎：每账号一个常驻连接，实时监听 + 手动/启动时历史扫描。"""

    CONNECT_TIMEOUT = 30

    def __init__(self, state: Optional[GiveawayState] = None) -> None:
        self._state = state
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._stopping = False
        self._clients: Dict[str, Any] = {}
        self._handlers: List[Tuple[Any, Any]] = []
        self._locks: Dict[str, asyncio.Lock] = {}
        # bot 身份内存缓存：{account|username_lower: True/False}，避免反复 resolve
        self._bot_cache: Dict[str, bool] = {}
        self.chats: List = []
        self.keywords: Tuple[str, ...] = DEFAULT_KEYWORDS
        self.confirm_keywords: Tuple[str, ...] = DEFAULT_CONFIRM_KEYWORDS
        self.confirm_wait = 8
        self.delay_range = (3.0, 10.0)
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
        scan_limit: int = 10,
        auto_scan: bool = True,
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
        lo, hi = delay_range
        self.delay_range = (min(lo, hi), max(lo, hi))
        self.session_dir = Path(session_dir)
        self.log = log
        self.on_event = on_event
        self.joined = 0
        self.failed = 0
        self.scan_text = ""
        self._stopping = False
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(accounts, int(scan_limit), bool(auto_scan))
        )
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
                self._clients[account] = client
                self._locks[account] = asyncio.Lock()
                me = await asyncio.wait_for(
                    client.get_me(), timeout=self.CONNECT_TIMEOUT
                )
                nickname = getattr(me, "first_name", "") or ""
                self.log(f"「{account}」已连接（{nickname}）")
            except asyncio.TimeoutError:
                self.log(f"「{account}」连接超时（网络或代理异常），已跳过")
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            except Exception as exc:
                self.log(f"「{account}」连接失败: {exc}")

    async def _run(self, accounts: List[str], scan_limit: int, auto_scan: bool) -> None:
        try:
            await self._connect_accounts(accounts)
            if not self._clients:
                self.log("没有可用账号，任务结束")
                return
            self._register_handlers()
            mode_kw = "，".join(self.keywords) if self.keywords else "全部 bot 链接"
            self.log(
                f"开始监听：{len(self._clients)} 个账号、{len(self.chats)} 个频道，"
                f"关键词：{mode_kw}"
            )
            if auto_scan and scan_limit > 0 and not self._stop.is_set():
                await self.scan_now(scan_limit)
            await self._stop.wait()
        finally:
            for client, handler in list(self._handlers):
                try:
                    client.remove_handler(handler)
                except Exception:
                    pass
            self._handlers.clear()
            for client in self._clients.values():
                try:
                    await client.disconnect()
                except Exception:
                    pass
            self._clients.clear()
            self.log("自动抽奖已停止")

    def _register_handlers(self) -> None:
        if not self.chats:
            return
        chat_filter = filters.chat(list(self.chats))
        for account, client in self._clients.items():
            async def _on_new(_client, message, acc: str = account) -> None:
                await self._process_message(acc, _client, message)

            async def _on_edited(_client, message, acc: str = account) -> None:
                # 编辑消息（有时抽奖链接是后加的）稍作延迟，等待实体信息完整
                await asyncio.sleep(0.3)
                await self._process_message(acc, _client, message)

            h_new = MessageHandler(_on_new, chat_filter)
            h_edit = EditedMessageHandler(_on_edited, chat_filter)
            client.add_handler(h_new)
            client.add_handler(h_edit)
            self._handlers.append((client, h_new))
            self._handlers.append((client, h_edit))

    async def scan_now(self, limit: int) -> None:
        """对所有账号 × 频道扫描最近 limit 条消息；未运行时直接返回。"""
        if self.scanning or not self._clients:
            return
        self.scanning = True
        try:
            total = len(self._clients) * max(1, len(self.chats))
            done = 0
            for account, client in list(self._clients.items()):
                for chat in self.chats:
                    if self._stop is not None and self._stop.is_set():
                        return
                    done += 1
                    self.scan_text = f"({done}/{total})"
                    try:
                        async for message in client.get_chat_history(
                            chat, limit=max(1, int(limit))
                        ):
                            await self._process_message(account, client, message)
                    except Exception as exc:
                        self.log(f"[{account}] 扫描 {chat} 失败: {exc}")
            self.scan_text = ""
            self.log(
                f"扫描完成：新参与 {self.joined} 个"
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

    async def _process_message(self, account: str, client: Any, message: Any) -> None:
        if self._stop is not None and self._stop.is_set():
            return
        if not message_matches_keywords(message, self.keywords):
            return
        links = extract_bot_links(message)
        if not links:
            return
        channel = self._chat_title(message)
        lock = self._locks.setdefault(account, asyncio.Lock())
        for username, param in links:
            if self._stop is not None and self._stop.is_set():
                return
            if self._state is not None and self._state.has(account, username, param):
                continue
            async with lock:
                await self._participate(
                    account, client, username, param, channel
                )

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
        await self._click_confirm(client, username, start_msg)

    async def _click_confirm(self, client: Any, username: str, start_msg: Any) -> None:
        """机器人回复带确认按钮时自动点击；只点 callback 按钮，避免误跳 URL。"""
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
