"""自动抽奖引擎：识别频道消息中的 t.me/<bot>?start=<参数> 链接并自动参与。

典型链路：频道抽奖帖 -> 消息文本/隐藏超链接/内联按钮中的 bot 深度链接 ->
调用 messages.StartBot（等价于点开链接）-> 机器人若回复带「确认参与」类
回调按钮，再自动点击一次。

本模块只保留引擎（连接、扫描、去重、参与）与运行期调优常量；
纯文本/链接解析在 giveaway_parsing，参与记录持久化在 giveaway_state。

触发方式：启动时可选自动扫描最近消息 + 手动扫描 + 周期性历史补漏
（重拉最近消息捞回漏网之鱼，含连续失败熔断暂停）；同一引擎常驻连接，
便于随时发起扫描与参与。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from tg_signer.core import get_client, safe_disconnect
from tg_signer.giveaway_parsing import (
    _INVITE_LINK_RE,
    _POPUP_ENDED_RE,
    _TME_RE,
    DEFAULT_CONFIRM_KEYWORDS,
    DEFAULT_KEYWORDS,
    JOIN_REQUIRED_POPUP_RE,
    JOIN_RETRY_BUTTON_KEYWORDS,
    PASSWORD_REQUIRE_RE,
    SCORE_BUTTON_RE,
    SCORE_SPEND_RE,
    WIN_ANNOUNCE_RE,
    WIN_IGNORE_PRIZE_RE,
    WIN_MESSAGE_RE,
    WIN_PERSONAL_RE,
    _extract_emoji_order,
    _giveaway_title,
    _is_username_resolve_error,
    _message_text,
    _utf16_to_index,
    bot_is_blocked,
    bot_policy,
    callback_answer_text,
    extract_bot_links,
    extract_channel_targets,
    extract_giveaway_title,
    extract_join_name_links,
    extract_join_status_targets,
    extract_password_tasks,
    extract_speech_tasks,
    is_emoji_verify_message,
    is_math_verify_message,
    math_verify_answer,
    message_is_draw_announce,
    message_matches_keywords,
    message_region_allowed,
    message_requires_password,
    message_requires_speech,
    parse_bot_start_link,
    parse_draw_date,
    reply_says_ended,
    reply_says_signup_success,
)
from tg_signer.giveaway_state import (
    JOIN_LOG_WINDOW,
    GiveawayState,
)

logger = logging.getLogger(__name__)


# 同一 bot 连续 FLOOD_WAIT 达到该次数后，本轮运行熔断（不再对其 start_bot）
MAX_FLOOD_STRIKES = 3


# 单次限流要求等待超过该秒数时立即熔断该 bot（不必等满 MAX_FLOOD_STRIKES 次）
FLOOD_HARD_BLOCK_SECONDS = 300


# 同一 bot 相邻两次 StartBot 的最小间隔（秒）：一次参与流程会连发
# 1~3 次 start（初次 / 加入后重发 / 点参与按钮），间隔过密会触发账号级限流
START_MIN_INTERVAL = 8.0


# —— 加入频道限流保护 ——
# 抽奖参与通常要先加入条件频道；短时间大量 JoinChannel 会触发 FLOOD_WAIT，
# 更严重时触发不可重试的 PEER_FLOOD（该账号数小时到数天不能加群/参与）。
# 默认开启节流（见下），配额默认 0=不限制。
# 相邻两次 JoinChannel 的最小间隔（秒，区间内随机取值，模拟人工节奏）
JOIN_PACE_RANGE = (2.0, 5.0)


# 单账号滚动 1 小时内最多加入的频道/群组数（突发保护），0=不限制
JOIN_HOURLY_CAP = 0


# 单账号滚动 24 小时内最多加入的频道/群组数（跨进程重启同样生效），0=不限制
JOIN_DAILY_CAP = 0


# 加入类调用触发 FLOOD_WAIT 后，在 Telegram 要求的等待时间之外再缓冲的秒数
# （与加限流保护前一致：等待 wait+10 秒）
JOIN_FLOOD_BUFFER = 10


# 解析类调用（get_users/get_chat 等）的账号级最小间隔（秒），
# 避免一条消息里十几条链接连续 resolve 触发限流；默认开启
RESOLVE_PACE_RANGE = (1.0, 2.0)


# 多账号向同一个群发送口令的最小间隔（秒，区间内随机）：各账号相互独立、
# 并行扫描时会同时命中同一条抽奖，几秒内扎堆发言容易被当成刷屏/机器人
PASSWORD_SEND_GAP_RANGE = (20.0, 45.0)


# 同一 bot 累计收到该次数「抽奖已结束」类回复后，本轮运行熔断该 bot
# （如 @Lottery8Bot 对所有 start 一律回复 "The event has ended or does not exist"）
MAX_ENDED_STRIKES = 3


# 扫描停止条件：连续这么多条「已开奖」的抽奖帖后，认为再往前都是旧抽奖
EXPIRED_STOP_STREAK = 5


# 单频道单次扫描的硬上限（条），防止无开奖日期标注时无限回溯
SCAN_HARD_CAP = 1000

# 频道/群组单轮每频道的默认扫描上限（条）：全局设置可覆盖
# （关键词未命中时不会触发「连续已开奖」止损，靠该上限兜底）
DEFAULT_SCAN_CAP = 500


# —— 历史补漏 ——
# 周期性重拉各频道最近若干条消息，捞回此前扫描失败/中断漏掉的抽奖帖
RESCUE_INTERVAL = 600  # 补漏扫描间隔（秒），0 = 关闭


RESCUE_LIMIT = 10  # 每次补漏每频道重拉的消息条数


RESCUE_MAX_FAILS = 3  # 连续失败达该轮数后熔断暂停补漏


RESCUE_RESUME_COOLDOWN = 1800  # 熔断后的自动恢复等待（秒）


RESCUE_FETCH_TIMEOUT = 60  # 单频道补漏拉取消息的超时（秒）


SEEN_CAP_PER_CHAT = 500  # 每频道保留的已处理消息 id 上限


# 首次记录某 bot 时的回看消息条数（初始基线）；之后按水位线增量扫描，
# 只处理比上次扫到的消息 id 更新的消息，该值不再参与日常回扫
WIN_SWEEP_LIMIT = 50


WIN_SWEEP_PAGE = 100  # 增量回扫单页拉取的消息条数


WIN_SWEEP_PAGE_MAX = 20  # 增量回扫最多翻页数（防水位线异常时无限翻页）

# 回扫相邻 bot 之间的随机间隔（秒）：避免单账号短时间内连发多次
# GetHistory，把连接打到 Transport flood 429
WIN_SWEEP_BOT_GAP_RANGE = (1.0, 2.0)

# 429 明细日志：同账号 60 秒内只记一条；60 秒窗口内 ≥3 个账号被 429 视为
# 「共享出口/IP 层风控」，否则视为单账号请求过密
TRANSPORT_FLOOD_LOG_INTERVAL = 60
TRANSPORT_FLOOD_CLUSTER_WINDOW = 60
TRANSPORT_FLOOD_CLUSTER_SIZE = 3
# 429 主动提醒节流（秒）：单账号 15 分钟一次，多账号同批 10 分钟一次，
# 避免限流期间又用有限配额刷自己的通知
TRANSPORT_FLOOD_ALERT_INTERVAL_SINGLE = 900
TRANSPORT_FLOOD_ALERT_INTERVAL_CLUSTER = 600


# —— 全局调用节奏（同进程内跨账号共享）——
# 所有账号合计每秒最多发起的 API 调用数；0=不限制。
# Transport flood 429 按出口 IP 计，账号一多单账号节流就管不住聚合速率，
# 默认 8 QPS 约为「4 个账号同时满速」的水平（单账号自然约 2 QPS）。
DEFAULT_GLOBAL_QPS = 8.0


# 同时进行扫描/补漏/回扫的账号数上限；0=不限制。
# 与 DEFAULT_GLOBAL_QPS 匹配：4 个账号并行即可打满令牌桶，再开更多只会排队。
DEFAULT_MAX_CONCURRENT_ACCOUNTS = 4


# —— 网络调用统一超时 ——
# 半开连接（手机切换网络/代理僵死）下 Pyrogram 请求可能永久挂起，导致引擎
# 任务协程卡死、WebUI 状态永不收敛。所有经引擎客户端发出的请求统一加超时：
# 单次请求超过 NET_CALL_TIMEOUT 秒按网络异常抛错，由各调用点的既有异常处理兜底。
# 默认 30 秒（原 90 秒）：限流/网络故障时快速失败，避免单请求卡 90 秒
# 导致连接堆积、触发更多超时请求形成雪崩。
NET_CALL_TIMEOUT = 30

# —— 账号级 API 统一节流（_TimeoutClient 层）——
# GiveawayEngine 自建的 _TimeoutClient 直接包 Pyrogram，不走 core.py
# BaseUserWorker._call_telegram_api 的统一限流。这里在 _TimeoutClient 层
# 补齐：per-account 串行锁 + 最小调用间隔，以及 Transport flood 429
# 识别与指数退避。
_CLIENT_API_LOCKS: Dict[str, asyncio.Lock] = {}
_CLIENT_API_LAST_CALL: Dict[str, float] = {}
_CLIENT_API_MIN_INTERVAL = 0.35  # 相邻 API 调用最小间隔（秒）
_CLIENT_TRANSPORT_FLOOD_BACKOFF = [5, 10, 20, 30]  # Transport 429 指数退避（秒）
# 网络超时后的重试退避（秒）：连续超时逐级加大，成功一次即清零。
# 网络差时 Pyrogram 会反复重试同一请求，不加退避会形成重试风暴。
_CLIENT_TIMEOUT_BACKOFF = [2, 5, 10, 20]


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


def _is_transport_flood(exc: BaseException) -> bool:
    """判断异常是否为 Telegram MTProto Transport flood 429 限流。

    Transport flood 发生在协议层而非 API 层，Pyrogram 不会把它映射成
    errors.FloodWait，错误消息里通常含 "429"、"Transport flood" 或
    "Please slow down"。识别后可触发指数退避而非落入通用重试逻辑。
    """
    msg = str(exc).lower()
    return any(k in msg for k in ("transport flood", "429", "please slow down"))


# Transport flood 429 出现后多久内发生的会话重连/请求失败，视为该账号被限流
_TRANSPORT_FLOOD_ATTRIBUTION_WINDOW = 15.0

# 只有「请求超时」类失败才允许在窗口内直接归属，避免把别的账号的 429 算错
_TRANSPORT_FLOOD_TIMEOUT_WINDOW = 5.0

# 账户页展示的 Transport flood 限流时长（秒）：每次 429 重新计时
_TRANSPORT_FLOOD_LIMIT_SECONDS = 60

# 不可访问目标（CHANNEL_PRIVATE/CHANNEL_INVALID）的跳过有效期（秒）：
# 到期后允许再试一次加入，失败则重新计时，避免永久放弃或每轮撞墙
UNREACHABLE_TARGET_TTL = 24 * 3600

# 最近一次 Transport flood 429 的时刻（monotonic 秒），由日志探针写入
_LAST_TRANSPORT_FLOOD_AT = 0.0

_PROBE_INSTALLED = False


class _TransportFloodProbe(logging.Handler):
    """监听 Pyrogram 会话日志中的 429 Transport flood。

    协议层的 429 不会作为异常抛给调用方：Pyrogram 只打一条
    `Server sent transport error: 429 (Transport flood...)` 并重启会话，
    调用方最终拿到的是「Request timed out」。因此只能从日志里捕获时间点，
    再由 _TimeoutClient 结合「会话连接对象是否被替换」判断是哪个账号被限流。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        global _LAST_TRANSPORT_FLOOD_AT
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 日志格式化失败不影响业务
            return
        if "429" in message and "flood" in message.lower():
            _LAST_TRANSPORT_FLOOD_AT = time.monotonic()


def _install_transport_flood_probe() -> None:
    """安装一次日志探针（幂等），用于把协议层 429 同步到账号限流状态。"""
    global _PROBE_INSTALLED
    if _PROBE_INSTALLED:
        return
    _PROBE_INSTALLED = True
    logging.getLogger("pyrogram.session").addHandler(_TransportFloodProbe())


class _GlobalRateLimiter:
    """进程内跨账号的令牌桶：限制所有账号合计的 API 发起速率。

    在锁内预约「下次可用时刻」并把游标推进一个间隔，睡眠放在锁外，
    因此多个账号是排队等待而非串行执行；rate<=0 表示不限制。
    """

    def __init__(self, qps: float) -> None:
        self.qps = max(0.0, float(qps))
        self.interval = 1.0 / self.qps if self.qps > 0 else 0.0
        self._next_at = 0.0
        self._lock: Optional[asyncio.Lock] = None

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        loop = asyncio.get_running_loop()
        async with self._lock:
            start = max(loop.time(), self._next_at)
            self._next_at = start + self.interval
        delay = start - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)


class _TimeoutClient:
    """Pyrogram 客户端代理：超时 + 账号级统一节流 + Transport flood 退避。

    内置 per-account asyncio.Lock + 最小调用间隔，让 GiveawayEngine
    的所有 API 调用都受统一节流（否则会绕过 core.py 的 _call_telegram_api）。
    Transport flood 429 被识别后执行指数退避，避免重试风暴。
    """

    def __init__(
        self,
        client: Any,
        timeout: float = NET_CALL_TIMEOUT,
        account: str = "",
        on_transport_flood: Optional[Callable[[str, float], None]] = None,
        global_limiter: Optional[_GlobalRateLimiter] = None,
    ) -> None:
        self._raw = client
        self._timeout = timeout
        self._account = account or "default"
        # 跨账号共享的全局令牌桶：限制同进程所有账号的合计调用速率
        self._global_limiter = global_limiter
        # 每个 _TimeoutClient 实例共享 per-account 锁（GiveawayEngine
        # 里通常一账号一 client，所以 key 就是 account）
        self._lock = _CLIENT_API_LOCKS.setdefault(self._account, asyncio.Lock())
        self._backoff_idx = 0  # 当前账号 Transport flood 退避等级
        self._last_transport_flood_at = 0.0
        self._timeout_backoff_idx = 0  # 当前账号网络超时退避等级
        self._last_timeout_at = 0.0
        # 协议层 429 被识别后回调，用于把限流状态同步到账户页
        self._on_transport_flood = on_transport_flood

    def __getattr__(self, item: str) -> Any:
        attr = getattr(self._raw, item)
        if not callable(attr):
            return attr

        def method(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            if hasattr(result, "__anext__"):
                return self._throttled_aiter(result, item)
            if inspect.isawaitable(result):
                return self._throttled_bounded(result, item)
            return result

        return method

    async def _throttled_bounded(self, coro: Any, name: str) -> Any:
        """带统一节流的单次 API 调用。"""
        async with self._lock:
            # 1) 最小间隔补齐
            await self._wait_min_interval()
            # 2) Transport flood 冷却期：如果刚被限流过，先 sleep
            await self._wait_transport_cooldown()
            # 2.5) 网络超时冷却期：连续超时时按等级退避，压住重试风暴
            await self._wait_timeout_cooldown()
            # 2.6) 全局令牌桶：跨账号共享，压住同 IP 的聚合速率
            await self._wait_global()
            # 3) 执行带超时的调用；记录调用前的会话连接对象，
            #    调用期间被 429 重启会话时据此判定是本账号被限流
            await self._throttled_enter()
            conn_before = self._connection_ref()
            try:
                result = await asyncio.wait_for(coro, self._timeout)
            except asyncio.TimeoutError:
                self._on_timeout()
                self._note_transport_flood(conn_before, timeout_failure=True)
                raise RuntimeError(
                    f"网络请求 {name} 超时（{self._timeout:.0f} 秒，网络异常）"
                ) from None
            except BaseException as exc:
                self._on_call_exception(exc)
                self._note_transport_flood(conn_before, timeout_failure=True)
                raise
            else:
                self._note_transport_flood(conn_before, timeout_failure=False)
                self._backoff_idx = 0
                self._timeout_backoff_idx = 0
                _CLIENT_API_LAST_CALL[self._account] = self._loop_time()
                return result

    async def _throttled_aiter(self, agen: Any, name: str) -> Any:
        """带统一节流的异步迭代器（get_chat_history 等）。

        锁只覆盖单次拉取，yield 放在锁外：调用方会在遍历循环体内发起
        同账号的其它 API 请求（如 get_users/start_bot），若整段迭代都持锁，
        循环体内的请求会等待自己未释放的锁而永久挂起（asyncio.Lock 不可重入，
        且等锁不触发网络超时）。
        """
        while True:
            async with self._lock:
                await self._wait_min_interval()
                await self._wait_transport_cooldown()
                await self._wait_timeout_cooldown()
                await self._wait_global()
                await self._throttled_enter()
                conn_before = self._connection_ref()
                try:
                    item = await asyncio.wait_for(agen.__anext__(), self._timeout)
                except StopAsyncIteration:
                    self._backoff_idx = 0
                    self._timeout_backoff_idx = 0
                    return
                except asyncio.TimeoutError:
                    self._on_timeout()
                    self._note_transport_flood(conn_before, timeout_failure=True)
                    raise RuntimeError(
                        f"网络请求 {name or '数据拉取'} 超时"
                        f"（{self._timeout:.0f} 秒，网络异常）"
                    ) from None
                except BaseException as exc:
                    self._on_call_exception(exc)
                    self._note_transport_flood(conn_before, timeout_failure=True)
                    raise
                self._note_transport_flood(conn_before, timeout_failure=False)
                self._timeout_backoff_idx = 0
                _CLIENT_API_LAST_CALL[self._account] = self._loop_time()
            yield item

    # —— 节流内部辅助 ——

    async def _wait_global(self) -> None:
        """全局令牌桶等待：所有账号共享同一速率上限。"""
        if self._global_limiter is not None:
            await self._global_limiter.wait()

    async def _wait_min_interval(self) -> None:
        loop = asyncio.get_running_loop()
        last = _CLIENT_API_LAST_CALL.get(self._account)
        if last is not None:
            gap = _CLIENT_API_MIN_INTERVAL - (loop.time() - last)
            if gap > 0:
                await asyncio.sleep(gap)

    async def _wait_transport_cooldown(self) -> None:
        """刚被 Transport flood 限流后，按指数退避等待。"""
        now = time.monotonic()
        idx = self._backoff_idx
        if idx > 0 and idx - 1 < len(_CLIENT_TRANSPORT_FLOOD_BACKOFF):
            # 距离上次 flood 不久才需要退避；过了就自然过期
            since = now - self._last_transport_flood_at
            need = _CLIENT_TRANSPORT_FLOOD_BACKOFF[
                min(idx - 1, len(_CLIENT_TRANSPORT_FLOOD_BACKOFF) - 1)
            ]
            if since < need:
                await asyncio.sleep(need - since)

    async def _wait_timeout_cooldown(self) -> None:
        """网络超时后按等级退避：避免网络差时连续重试形成请求风暴。"""
        idx = self._timeout_backoff_idx
        if idx <= 0:
            return
        need = _CLIENT_TIMEOUT_BACKOFF[min(idx - 1, len(_CLIENT_TIMEOUT_BACKOFF) - 1)]
        since = time.monotonic() - self._last_timeout_at
        if since < need:
            await asyncio.sleep(need - since)

    def _on_timeout(self) -> None:
        """请求超时：升级网络超时退避等级（成功一次即由调用方清零）。"""
        self._timeout_backoff_idx = min(
            self._timeout_backoff_idx + 1, len(_CLIENT_TIMEOUT_BACKOFF)
        )
        self._last_timeout_at = time.monotonic()

    def _on_call_exception(self, exc: BaseException) -> None:
        """调用异常后更新状态：Transport flood 升级退避等级。"""
        if _is_transport_flood(exc):
            self._backoff_idx = min(
                self._backoff_idx + 1, len(_CLIENT_TRANSPORT_FLOOD_BACKOFF)
            )
            self._last_transport_flood_at = time.monotonic()
            level = min(self._backoff_idx, len(_CLIENT_TRANSPORT_FLOOD_BACKOFF))
            wait = _CLIENT_TRANSPORT_FLOOD_BACKOFF[level - 1]
            logger.warning(
                "[%s] Transport flood 429 限流（第 %d 级退避 %d 秒）：%s",
                self._account,
                self._backoff_idx,
                wait,
                str(exc)[:80],
            )
            # 异常本身带 429 信息时直接同步（多数情况是日志里的 transport 429，
            # 见 _note_transport_flood）
            self._emit_transport_flood()

    def _connection_ref(self) -> Any:
        """当前会话的底层连接对象；429 后 Pyrogram 会重启会话并替换它。"""
        session = getattr(self._raw, "session", None)
        return getattr(session, "connection", None)

    def _note_transport_flood(self, conn_before: Any, timeout_failure: bool) -> None:
        """把「协议层 429 导致本账号会话重连/请求超时」同步为账号限流。

        Pyrogram 收到 `429 Transport flood` 时不会抛给调用方，只会重启会话，
        于是账号页看不到任何限流。这里用两个信号定位到具体账号：
        1) 调用期间会话连接对象被替换（说明 429 触发了本账号重连）；
        2) 紧接着出现请求超时（429 重连途中在途请求必然超时）。
        """
        now = time.monotonic()
        since = now - _LAST_TRANSPORT_FLOOD_AT
        if not _LAST_TRANSPORT_FLOOD_AT or since > _TRANSPORT_FLOOD_ATTRIBUTION_WINDOW:
            return
        conn_after = self._connection_ref()
        restarted = (
            conn_before is not None
            and conn_after is not None
            and conn_after is not conn_before
        )
        if restarted or (timeout_failure and since <= _TRANSPORT_FLOOD_TIMEOUT_WINDOW):
            self._last_transport_flood_at = now
            self._emit_transport_flood()

    def _emit_transport_flood(self) -> None:
        """通知引擎把本账号标记为限流（账户页展示剩余时长）。"""
        if self._on_transport_flood is None:
            return
        try:
            self._on_transport_flood(self._account, _TRANSPORT_FLOOD_LIMIT_SECONDS)
        except Exception:  # noqa: BLE001 - 状态同步失败不能影响参与流程
            logger.debug("同步 Transport flood 限流状态失败", exc_info=True)

    async def _throttled_enter(self) -> None:
        """调用前的统一钩子（给子类/测试扩展用）。"""
        pass

    @staticmethod
    def _loop_time() -> float:
        loop = asyncio.get_running_loop()
        return loop.time()


class GiveawayEngine:
    """自动抽奖后台引擎：每账号一个常驻连接，手动/启动时历史扫描并自动参与。"""

    CONNECT_TIMEOUT = 30

    def __init__(self, state: Optional[GiveawayState] = None) -> None:
        self._state = state
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._stopping = False
        self._clients: Dict[str, Any] = {}
        # 429 提醒发送任务（fire-and-forget，持有引用防被 GC）
        self._flood_alert_tasks: set = set()
        self._locks: Dict[str, asyncio.Lock] = {}
        # bot 身份内存缓存：{account|username_lower: True/False}，避免反复 resolve
        self._bot_cache: Dict[str, bool] = {}
        # 本次运行已处理过的加入目标：{account|target_lower}，避免重复 join
        self._join_cache: set = set()
        # 已发送过口令的群组：{account|bot|群名_lower}，重复参与不重复发言
        self._password_sent: set = set()
        # 同群口令发言排期：{群名/目标: 下一个可发言时刻（monotonic 秒）}，
        # 多账号并行命中同一条抽奖时按群组错峰，避免几个账号扎堆发口令
        self._password_gate_until: Dict[str, float] = {}
        self.password_send_gap: Tuple[float, float] = PASSWORD_SEND_GAP_RANGE
        # 已处理过点击的防脚本验证消息：{account|bot|msg_id}，进程内去重
        # （重发 start 后旧验证消息仍在候选里，避免误点过期验证按钮）
        self._emoji_verify_handled: set = set()
        # 已回复过答案的算术验证消息：{account|bot|msg_id}，进程内去重
        # （重发 start 后旧验证题仍在候选里，避免重复回复过期题目）
        self._math_verify_handled: set = set()
        # 自动加入频道的限流暂停：JoinChannel FLOOD_WAIT 后暂停该账号的加入
        # {account: 恢复时刻（monotonic 秒）}，只影响触发限流的账号
        self._join_suspended: Dict[str, float] = {}
        self._join_suspend_logged: set = set()
        # 私有邀请链接（+hash）单独计数：这类加入被限时不暂停 @username 加入
        self._invite_suspended: Dict[str, float] = {}
        self._invite_suspend_logged: set = set()
        # PEER_FLOOD 强制限流账号：本轮运行停止该账号的一切加入与参与
        self._peer_flood_accounts: set = set()
        # 频道/群组已满提醒去重：本轮运行每账号只回报一次（WebUI 据此自动退频道）
        self._channels_full_notified: set = set()
        # 加入频道时间日志（state 不可用时的内存兜底）{account: [epoch 秒...]}
        self._join_events: Dict[str, List[float]] = {}
        # 协议层 Transport flood 429 提示节流：{account: 上次提示的 monotonic 秒}
        self._transport_flood_logged: Dict[str, float] = {}
        # 429 明细日志节流：{account: 上次记录的 monotonic 秒}
        self._transport_flood_429_logged: Dict[str, float] = {}
        # 最近 429 的账号与时刻（epoch 秒），用于判断是单账号过密还是多账号同时被风控
        self._transport_flood_hits: Dict[str, float] = {}
        # 429 主动提醒节流：{"cluster" 或 "single:账号": 上次提醒的 monotonic 秒}
        self._transport_flood_alerted: Dict[str, float] = {}
        # 不可访问目标（CHANNEL_PRIVATE/CHANNEL_INVALID）：{账号|目标_lower: 到期时间戳}，
        # 未加入/被踢/链接失效，冷却期内直接跳过，到期（24 小时）后再试一次
        self._unreachable_targets: Dict[str, float] = {}
        # 加入节奏与配额（任务级可配置，默认取模块常量）
        self.join_pace_range: Tuple[float, float] = JOIN_PACE_RANGE
        self.resolve_pace_range: Tuple[float, float] = RESOLVE_PACE_RANGE
        self.join_hourly_cap: int = JOIN_HOURLY_CAP
        self.join_daily_cap: int = JOIN_DAILY_CAP
        self.join_flood_buffer: int = JOIN_FLOOD_BUFFER
        # 全局调用节奏（任务级可配置）：跨账号共享的 API QPS 上限与并扫账号数
        self.global_qps: float = DEFAULT_GLOBAL_QPS
        self.max_concurrent_accounts: int = DEFAULT_MAX_CONCURRENT_ACCOUNTS
        self._api_limiter = _GlobalRateLimiter(self.global_qps)
        self._account_slots: Optional[asyncio.Semaphore] = None
        # 解析类调用上次发起时刻（monotonic 秒），用于账号级最小间隔
        self._last_resolve_at: Dict[str, float] = {}
        # FLOOD_WAIT 熔断：{account|bot_lower: 连续次数}；达到上限加入 _flood_bots
        self._flood_counts: Dict[str, int] = {}
        self._flood_bots: set = set()
        # 限流暂停：{account|bot_lower: 恢复时刻（monotonic 秒）}，到期自动解除；
        # 按 bot 级别而非单条链接，避免刚被罚就换 param 继续打同一个 bot
        self._flood_until: Dict[str, float] = {}
        # 同一 bot 上次 StartBot 时刻（monotonic 秒），用于强制最小间隔
        self._last_start_at: Dict[str, float] = {}
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
        # 中奖消息跳过关键字：文本命中任一关键字即不提醒、不转发（全局设置）
        self.win_skip_keywords: Tuple[str, ...] = ()
        # 已处理的中奖消息：{account|bot|msg_id}，进程内去重（跨重启靠状态文件）
        self._win_handled: set = set()
        # 标记为「双向」的账户集合：发言数要求类抽奖自动跳过
        self.two_way_accounts: set = set()
        self.chats: List = []
        # 监控来源类型：channel=频道/群组（默认），bot=抽奖机器人私聊
        self.source_type = "channel"
        # 机器人私聊来源时的目标用户名（不含 @），只参与该 bot 的抽奖链接
        self.bot_username = ""
        # 机器人私聊来源时的历史回扫条数上限（None=沿用 SCAN_HARD_CAP）
        self.scan_limit: Optional[int] = None
        # 频道/群组每轮每频道的扫描上限（全局设置，默认 DEFAULT_SCAN_CAP）
        self.scan_cap: int = DEFAULT_SCAN_CAP
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
        # 中奖回扫进度（与频道扫描分开显示）：分母=本轮待回扫的账号×bot 数，
        # 分子=已回扫完成的 bot 数；本轮结束后保留完成进度继续展示
        self.win_sweep_text = ""
        self.win_sweep_done = 0
        self.win_sweep_total = 0
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
        source_type: str = "channel",
        bot_username: Optional[str] = None,
        scan_limit: Optional[int] = None,
        scan_cap: Optional[int] = None,
        auto_scan: bool = True,
        auto_join: bool = True,
        max_joins: Optional[int] = None,
        rescue_interval: Optional[int] = None,
        rescue_limit: Optional[int] = None,
        delay_range: Tuple[float, float] = (3.0, 10.0),
        join_pace_range: Optional[Tuple[float, float]] = None,
        resolve_pace_range: Optional[Tuple[float, float]] = None,
        join_hourly_cap: Optional[int] = None,
        join_daily_cap: Optional[int] = None,
        join_flood_buffer: Optional[int] = None,
        global_qps: Optional[float] = None,
        max_concurrent_accounts: Optional[int] = None,
        confirm_wait: int = 8,
        confirm_keywords: Optional[Iterable[str]] = None,
        ignore_history: bool = True,
        win_detect: bool = True,
        win_forward_target=None,
        win_sweep_limit: Optional[int] = None,
        win_skip_keywords: Optional[Iterable[str]] = None,
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
        self.source_type = str(source_type or "channel").strip().lower()
        self.bot_username = str(bot_username or "").lstrip("@").strip()
        self.scan_limit = max(1, int(scan_limit)) if scan_limit else None
        # 频道/群组单轮扫描上限：全局设置为空时沿用默认值
        self.scan_cap = max(1, int(scan_cap)) if scan_cap else DEFAULT_SCAN_CAP
        self.chats = list(chats)
        self.keywords = tuple(
            str(k).strip() for k in (keywords or ()) if str(k).strip()
        )
        self.confirm_keywords = tuple(confirm_keywords or DEFAULT_CONFIRM_KEYWORDS)
        self.confirm_wait = max(0, int(confirm_wait))
        # 机器人私聊来源不做自动加入：消息里的 ✅ 清单与推广链接都不是
        # 本次任务的加入目标，避免误加频道/群组
        self.auto_join = bool(auto_join) and self.source_type != "bot"
        # 忽略历史成功记录：开启后已参与过的抽奖链接允许重新参与
        self.ignore_history = bool(ignore_history)
        # 中奖识别配置：转发目标列表，"me" = 收藏夹（Saved Messages），
        # 兼容旧的单目标字符串配置
        self.win_detect = bool(win_detect)
        self.win_forward_targets = self._normalize_win_forward_targets(
            win_forward_target
        )
        # 中奖回扫条数：0/None/非法值回退默认
        self.win_sweep_limit = (
            max(1, int(win_sweep_limit)) if win_sweep_limit else WIN_SWEEP_LIMIT
        )
        # 中奖消息跳过关键字：命中即视为非中奖（空配置＝不过滤）
        self.win_skip_keywords = tuple(
            str(k).strip() for k in (win_skip_keywords or ()) if str(k).strip()
        )
        self._win_handled.clear()
        # 口令错峰排期按运行周期重置，避免上一轮残留的等待时刻拖慢本次启动
        self._password_gate_until.clear()
        # 双向账户集合：发言数要求类抽奖自动跳过
        self.two_way_accounts = {str(a) for a in (two_way_accounts or ())}
        self.max_joins = max(0, int(max_joins)) if max_joins else None
        if rescue_interval is not None:
            self.rescue_interval = max(0, int(rescue_interval))
        if rescue_limit is not None:
            self.rescue_limit = max(1, int(rescue_limit))
        lo, hi = delay_range
        self.delay_range = (min(lo, hi), max(lo, hi))
        # 限流保护参数：None 表示沿用模块默认值（UI 未传时保持原行为）
        if join_pace_range is not None:
            lo, hi = join_pace_range
            self.join_pace_range = (max(0.0, min(lo, hi)), max(0.0, lo, hi))
        if resolve_pace_range is not None:
            lo, hi = resolve_pace_range
            self.resolve_pace_range = (max(0.0, min(lo, hi)), max(0.0, lo, hi))
        if join_hourly_cap is not None:
            self.join_hourly_cap = max(0, int(join_hourly_cap))
        if join_daily_cap is not None:
            self.join_daily_cap = max(0, int(join_daily_cap))
        if join_flood_buffer is not None:
            self.join_flood_buffer = max(0, int(join_flood_buffer))
        # 全局限速：None 沿用默认值；0 表示不限制
        self.global_qps = (
            max(0.0, float(global_qps))
            if global_qps is not None
            else DEFAULT_GLOBAL_QPS
        )
        self._api_limiter = _GlobalRateLimiter(self.global_qps)
        self.max_concurrent_accounts = (
            max(0, int(max_concurrent_accounts))
            if max_concurrent_accounts is not None
            else DEFAULT_MAX_CONCURRENT_ACCOUNTS
        )
        self._account_slots = (
            asyncio.Semaphore(self.max_concurrent_accounts)
            if self.max_concurrent_accounts > 0
            else None
        )
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
        self._flood_until.clear()
        self._last_start_at.clear()
        self._ended_counts.clear()
        self._ended_bots.clear()
        self._seen_msgs.clear()
        # 每次启动重新尝试自动加入监控对象（账号可能已手动加入/限流已解除）
        self._join_cache.clear()
        self._join_suspended.clear()
        self._join_suspend_logged.clear()
        self._invite_suspended.clear()
        self._invite_suspend_logged.clear()
        self._peer_flood_accounts.clear()
        self._join_events.clear()
        self._last_resolve_at.clear()
        # 监控对象 chat_id -> @username：数字 id 无法直接加入时兜底
        self.chat_usernames = self._normalize_chat_usernames(chat_usernames)
        self._rescue_fail_streak = 0
        self._rescue_suspended_until = None
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(accounts, bool(auto_scan)))
        if on_finish is not None:
            self._task.add_done_callback(lambda _t: on_finish())
        return True

    @staticmethod
    def _normalize_chat_usernames(
        chat_usernames: Optional[Dict[str, str]],
    ) -> Dict[str, str]:
        """归一化监控对象 chat_id -> @username 映射（去掉 @ 前缀与空白）。"""
        return {
            str(k).strip(): str(v).lstrip("@").strip()
            for k, v in (chat_usernames or {}).items()
            if str(v).strip().lstrip("@").strip()
        }

    @staticmethod
    def _normalize_win_forward_targets(win_forward_target: Any) -> List[Any]:
        """归一化中奖转发目标为列表，"me" = 收藏夹（Saved Messages）。

        兼容旧的单目标字符串配置；空列表回退为 ["me"]。
        """
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
        return targets or ["me"]

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
        # 协议层 429 只写日志不抛异常，先装上探针才能同步到账号限流状态
        _install_transport_flood_probe()

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
                # 统一包一层网络超时代理，防半开连接永久挂起任务协程；
                # 传入 account 让 _TimeoutClient 复用账号级节流锁与最小调用间隔。
                # on_transport_flood：协议层 429 不抛异常，需由日志探针+会话
                # 重连判定后回写账号限流，否则账户页会显示「正常」。
                self._clients[account] = _TimeoutClient(
                    client,
                    account=account,
                    on_transport_flood=self._note_transport_flood,
                    global_limiter=self._api_limiter,
                )
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
            source_desc = (
                f"机器人 @{self.bot_username}"
                if self.source_type == "bot"
                else f"{len(self.chats)} 个监控对象"
            )
            self.log(
                f"任务已启动：{len(self._clients)} 个账号、{source_desc}，"
                f"关键词：{mode_kw}"
            )
            # 中奖回扫：开奖常在参与后数日，先补捡停机期间 bot 私聊的中奖通知
            if not self._stop.is_set():
                await self._sweep_all_accounts()
            if auto_scan and not self._stop.is_set():
                await self.scan_now()
                # 首扫后新增参与的 bot 需建回扫基线（此前无水位线会漏扫）
                if not self._stop.is_set():
                    await self._sweep_all_accounts()
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

    async def _with_account_slot(
        self, fn: Callable[..., Awaitable[None]], *args: Any, **kwargs: Any
    ) -> None:
        """在全局账号窗口内执行单账号任务；窗口满时排队，避免账号数即并发数。"""
        slots = self._account_slots
        if slots is None:
            await fn(*args, **kwargs)
            return
        async with slots:
            await fn(*args, **kwargs)

    async def scan_now(self) -> None:
        """对所有账号 × 频道回溯扫描历史消息并自动参与（多账号并行扫描）。

        每个频道持续向前扫描，直到满足任一停止条件：
          - 参与数达到参与条数上限（若设置）
          - 连续 EXPIRED_STOP_STREAK 条已开奖的抽奖帖（再往前都是旧抽奖）
          - 达到单频道扫描上限（全局设置，默认 DEFAULT_SCAN_CAP 条；
            关键词未命中时同样生效，防止翻完整个频道历史）
        """
        if self.scanning or not self._clients:
            return
        self.scanning = True
        try:
            total = len(self._clients) * max(1, len(self.chats))
            done: Dict[str, int] = {"n": 0}
            joined_before = self.joined
            account_items = list(self._clients.items())
            # 多账号错峰启动：各账号协程自己先 sleep idx*5 秒，
            # 避免 asyncio.gather 把所有账号的 API 调用同时打出去触发限流；
            # 真正并发数再由全局账号窗口（max_concurrent_accounts）限制
            tasks = []
            for idx, (account, client) in enumerate(account_items):
                if idx == 0:
                    tasks.append(
                        self._with_account_slot(
                            self._scan_one, account, client, done, total
                        )
                    )
                else:
                    tasks.append(
                        self._scan_one_delayed(idx, account, client, done, total)
                    )
            await asyncio.gather(*tasks)
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

    async def _scan_one(
        self, account: str, client: Any, done: Dict[str, int], total: int
    ) -> None:
        """单账号扫描全部监控对象（scan_now 的并行子任务）。"""
        if not await self._ensure_connected(client):
            self.log(f"[{account}] 连接不可用，跳过该账号本轮扫描")
            return
        for chat in self.chats:
            if self._stop is not None and self._stop.is_set():
                return
            if self.source_type == "bot":
                # 机器人私聊：无需加入，也不走群组置顶逻辑，直接回扫历史消息
                done["n"] += 1
                self.scan_text = f"({done['n']}/{total})"
                await self._scan_chat_history(account, client, chat)
                continue
            # 监控对象成员检查：账号未加入该频道/群组时自动尝试加入
            await self._ensure_monitor_membership(account, client, chat)
            done["n"] += 1
            self.scan_text = f"({done['n']}/{total})"
            # 识别聊天类型：群组仍只扫置顶；频道忽略置顶（多为长期
            # 挂着的旧抽奖帖），直接从最新历史消息开始扫
            is_group, chat_info = await self._detect_chat_type(account, client, chat)
            if is_group:
                await self._scan_pinned(account, client, chat, chat_info=chat_info)
            if is_group or (
                self.max_joins is not None and self.joined >= self.max_joins
            ):
                # 群组只扫指定（置顶）消息；达到上限也不再扫历史
                continue
            await self._scan_chat_history(account, client, chat)

    async def _scan_one_delayed(
        self, idx: int, account: str, client: Any, done: Dict[str, int], total: int
    ) -> None:
        """错峰启动：idx > 0 的账号先 sleep idx*5 秒再进入全局账号窗口扫描。"""
        await asyncio.sleep(idx * 5)
        await self._with_account_slot(self._scan_one, account, client, done, total)

    async def _scan_chat_history(self, account: str, client: Any, chat: Any) -> None:
        """单频道回溯扫描历史消息：水位线去重 + 处理，命中停止条件即止。"""
        expired_streak = 0
        scanned = 0
        # 机器人私聊来源可单独限制回扫条数；频道/群组按全局扫描上限兜底，
        # 关键词未命中时不会触发「连续已开奖」止损，仍需按条数截断
        if self.source_type == "bot":
            hard_cap = (
                min(SCAN_HARD_CAP, self.scan_limit)
                if self.scan_limit is not None
                else SCAN_HARD_CAP
            )
        else:
            hard_cap = self.scan_cap
        try:
            async for message in client.get_chat_history(chat):
                scanned += 1
                if scanned > hard_cap:
                    self.log(
                        f"[{account}] {chat} 已扫描 {hard_cap} 条，停止该频道本轮扫描"
                    )
                    if self.source_type != "bot":
                        # 达到全局扫描上限：通知消息中心，避免任务静默截断
                        self._emit(
                            {
                                "type": "scan_cap",
                                "account": account,
                                "chat": str(chat),
                                "cap": hard_cap,
                            }
                        )
                    break
                mid = getattr(message, "id", None)
                if mid is not None:
                    self._mark_seen(account, str(chat), mid)
                outcome = await self._process_message(account, client, message)
                expired_streak, stop = self._advance_expired_streak(
                    account, chat, outcome, expired_streak
                )
                if stop:
                    break
                if self.max_joins is not None and self.joined >= self.max_joins:
                    break
        except Exception as exc:
            self.log(f"[{account}] 扫描 {chat} 失败: {exc}")

    def _advance_expired_streak(
        self, account: str, chat: Any, outcome: Optional[str], expired_streak: int
    ) -> Tuple[int, bool]:
        """按消息处理结果推进「连续已开奖」计数。返回 (新计数, 是否停止扫描)。

        连续 EXPIRED_STOP_STREAK 条已开奖的抽奖帖后认为再往前都是旧抽奖。
        """
        if outcome == "joined":
            return 0, False
        if outcome != "expired":
            return expired_streak, False
        expired_streak += 1
        if expired_streak >= EXPIRED_STOP_STREAK:
            self.log(f"[{account}] 连续 {expired_streak} 条已开奖，停止扫描 {chat}")
            return expired_streak, True
        return expired_streak, False

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
        pinned_list = await self._collect_pinned_messages(
            account, client, chat, chat_info
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

    async def _collect_pinned_messages(
        self, account: str, client: Any, chat: Any, chat_info: Any
    ) -> List[Any]:
        """收集群组全部置顶消息；聊天信息获取失败返回空列表（跳过扫描）。"""
        if chat_info is None:
            try:
                chat_info = await client.get_chat(chat)
            except Exception as exc:
                self.log(f"[{account}] 获取 {chat} 信息失败，跳过置顶扫描: {exc}")
                return []
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
        return pinned_list

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
        ctx = await self._source_button_ctx(account, message)
        if ctx is None:
            return
        chat_id, msg_id, btn = ctx
        channel = self._chat_title(message)
        title = _giveaway_title(message)
        # URL 按钮：解析为 bot start 链接则等同普通链接参与
        url = str(getattr(btn, "url", "") or "").strip()
        if url and not getattr(btn, "callback_data", None):
            await self._join_via_source_url(
                account, client, url, channel, title, message
            )
            return
        await self._click_source_button(
            account, client, message, btn, chat_id, msg_id, channel, title
        )

    async def _source_button_ctx(
        self, account: str, message: Any
    ) -> Optional[Tuple[int, int, Any]]:
        """按钮型抽奖参与前的守卫检查；返回 (chat_id, msg_id, 按钮)，跳过时 None。"""
        if self._stop is not None and self._stop.is_set():
            return None
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        msg_id = getattr(message, "id", None)
        if chat_id is None or msg_id is None:
            return None
        state_key = f"{chat_id}|{msg_id}"
        if self._source_btn_seen(account, state_key):
            return None
        if self.max_joins is not None and self.joined >= self.max_joins:
            return None
        btn = self._find_confirm_button(message)
        if btn is None:
            return None
        return chat_id, msg_id, btn

    def _source_btn_seen(self, account: str, state_key: str) -> bool:
        """该置顶帖是否已处理过/已终态（source_btn 持久化去重）。"""
        return self._state is not None and (
            self._state.has(account, "source_btn", state_key)
            or self._state.is_ended(account, "source_btn", state_key)
        )

    async def _join_via_source_url(
        self,
        account: str,
        client: Any,
        url: str,
        channel: str,
        title: str,
        message: Any,
    ) -> None:
        """URL 按钮指向 bot start 深度链接（非黑名单 bot）时走常规参与流程。"""
        link = parse_bot_start_link(url)
        if link is not None and not bot_is_blocked(link[0]):
            await self._participate(
                account,
                client,
                link[0],
                link[1],
                channel,
                title,
                source_message=message,
            )

    async def _click_source_button(
        self,
        account: str,
        client: Any,
        message: Any,
        btn: Any,
        chat_id: int,
        msg_id: int,
        channel: str,
        title: str,
    ) -> None:
        """callback 按钮参与：模拟节奏点击并按弹窗文案判定结果。"""
        if account in self._peer_flood_accounts:
            return  # 账号已被 PEER_FLOOD 熔断
        state_key = f"{chat_id}|{msg_id}"
        # 先按帖内频道/群组链接自动加入：按钮型抽奖点击后 bot 会校验
        # 「您未加入 XXX」，不先加入则点击无效
        if self.auto_join:
            joined, _blocked = await self._join_from_message(
                account, client, "", message
            )
            if joined:
                await asyncio.sleep(2)
        # 模拟人工点击节奏，避免连点触发风控
        lo, hi = self.delay_range
        if hi > 0:
            await asyncio.sleep(random.uniform(lo, max(lo, hi)))
        if self._stop is not None and self._stop.is_set():
            return
        # 同步预留参与名额（与 _participate 同口径），失败时归还
        reserved = self.max_joins is not None
        if reserved:
            self.joined += 1
        popup = await self._request_source_btn_answer(
            account, client, btn, chat_id, msg_id
        )
        if popup is None:
            return
        if self._source_btn_popup_blocked(
            account, btn, popup, state_key, channel, reserved
        ):
            return
        self._record_source_btn_join(
            account, btn, state_key, channel, title, popup, reserved
        )

    async def _request_source_btn_answer(
        self, account: str, client: Any, btn: Any, chat_id: int, msg_id: int
    ) -> Optional[str]:
        """点击 callback 按钮并读取弹窗文案；失败时归还预留名额并计数。

        返回弹窗文案，点击失败返回 None。
        """
        try:
            answer = await client.request_callback_answer(
                chat_id=chat_id,
                message_id=msg_id,
                callback_data=btn.callback_data,
            )
        except Exception as exc:
            if self.max_joins is not None:
                self.joined -= 1
            self.failed += 1
            if self._is_peer_flood(exc):
                self._block_peer_flood(account, f"点击按钮「{btn.text}」")
                return None
            self.log(f"[{account}] 点击置顶抽奖帖「{btn.text}」按钮失败: {exc}")
            return None
        return callback_answer_text(answer)

    def _source_btn_popup_blocked(
        self,
        account: str,
        btn: Any,
        popup: str,
        state_key: str,
        channel: str,
        reserved: bool,
    ) -> bool:
        """终态/条件未满足弹窗处理：归还名额并记录，返回 True 表示终止参与。"""
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
            return True
        # 「您未加入 XXX」类弹窗：参与条件未满足，点击未生效，不能记为成功
        if popup and JOIN_REQUIRED_POPUP_RE.search(popup):
            if reserved:
                self.joined -= 1
            self.failed += 1
            self.log(
                f"[{account}] 置顶抽奖帖「{btn.text}」弹窗提示「{popup[:60]}」，"
                "参与条件未满足（未加入指定对象），未参与成功"
            )
            return True
        return False

    def _record_source_btn_join(
        self,
        account: str,
        btn: Any,
        state_key: str,
        channel: str,
        title: str,
        popup: str,
        reserved: bool,
    ) -> None:
        """记录按钮型参与成功：持久化去重、计数与事件推送。"""
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
            await self._rescue_tick()

    async def _rescue_tick(self) -> None:
        """补漏循环的单轮决策（不含等待）：扫描中让路、熔断中跳过、否则执行一轮。"""
        if self.scanning:
            return  # 完整/手动扫描进行中，让路并重等一个周期
        if self._rescue_suspended_until is not None:
            if datetime.now() < self._rescue_suspended_until:
                return
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
        # 中奖回扫与频道补漏分开计进度：分母为本轮全部账号待回扫的 bot 数
        sweep = self._begin_win_sweep(
            sum(len(self._win_sweep_bots(a)) for a in self._clients),
            announce=False,
        )
        self.scanning = True
        try:
            account_items = list(self._clients.items())
            tasks = []
            for idx, (account, client) in enumerate(account_items):
                if idx == 0:
                    tasks.append(
                        self._with_account_slot(
                            self._rescue_one_account,
                            account,
                            client,
                            failures,
                            done,
                            total,
                            sweep,
                        )
                    )
                else:
                    tasks.append(
                        self._rescue_one_account_delayed(
                            idx, account, client, failures, done, total, sweep
                        )
                    )
            await asyncio.gather(*tasks)
            gained = self.joined - joined_before
            # 每轮必记一行，便于确认补漏循环在按间隔运行
            self.log(
                f"补漏轮次完成：参与 {gained} 个"
                + (f"，{failures['n']} 个频道拉取失败" if failures["n"] else "")
            )
        finally:
            self.scanning = False
            self.scan_text = ""
            self._end_win_sweep()
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

    async def _rescue_one_account(
        self,
        account: str,
        client: Any,
        failures: Dict[str, int],
        done: Dict[str, int],
        total: int,
        sweep: Dict[str, int],
    ) -> None:
        """补漏单账号子任务：逐频道重拉最近消息，顺带回扫 bot 私聊中奖消息。"""
        if not await self._ensure_connected(client):
            self.log(f"[{account}] 补漏：连接不可用，跳过该账号")
            failures["n"] += 1
            return
        for chat in self.chats:
            if self._stop is not None and self._stop.is_set():
                return
            done["n"] += 1
            self.scan_text = f"补漏 ({done['n']}/{total})"
            await self._rescue_chat_safe(account, client, chat, failures)
        # 补漏轮顺带回扫 bot 私聊中奖消息（不依赖监控对象配置）
        await self._sweep_bot_wins(
            account, client, bots=self._win_sweep_bots(account), progress=sweep
        )

    async def _rescue_one_account_delayed(
        self,
        idx: int,
        account: str,
        client: Any,
        failures: Dict[str, int],
        done: Dict[str, int],
        total: int,
        sweep: Dict[str, int],
    ) -> None:
        """错峰启动：idx > 0 的账号先 sleep idx*5 秒再进入全局账号窗口补漏。"""
        await asyncio.sleep(idx * 5)
        await self._with_account_slot(
            self._rescue_one_account, account, client, failures, done, total, sweep
        )

    async def _rescue_chat_safe(
        self, account: str, client: Any, chat: Any, failures: Dict[str, int]
    ) -> None:
        """补漏单频道并兜底记录失败（超时与其他异常分别计数）。"""
        try:
            await self._rescue_channel(account, client, chat)
        except asyncio.TimeoutError:
            self.log(f"[{account}] 补漏 {chat} 拉取超时（{RESCUE_FETCH_TIMEOUT}s）")
            failures["n"] += 1
        except Exception as exc:
            self.log(f"[{account}] 补漏 {chat} 失败: {exc}")
            failures["n"] += 1

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
        # 开奖结果公告（含防伪编号但恭喜的是别人）：跳过，不按中奖处理
        if WIN_ANNOUNCE_RE.search(text) and not WIN_PERSONAL_RE.search(text):
            return
        # 奖品为站内虚拟积分「嫩妹币」：不转发、不推送
        if WIN_IGNORE_PRIZE_RE.search(text):
            return
        # 用户配置的中奖消息跳过关键字：命中即不提醒、不转发
        if any(kw in text for kw in self.win_skip_keywords):
            logger.debug("中奖消息命中跳过关键字，忽略：%s", text[:80])
            return
        mid = getattr(message, "id", None)
        if not self._mark_win_handled(account, bot_username, mid, text):
            return
        summary = " ".join(text.split())
        if len(summary) > 80:
            summary = summary[:80] + "…"
        self.log(f"[{account}] 检测到中奖消息（@{bot_username}）：{summary}")
        # 转发原消息到各转发目标（默认收藏夹），单个失败不阻断其余目标与提醒
        chat_id = getattr(getattr(message, "chat", None), "id", None) or bot_username
        if mid is not None:
            await self._forward_win_message(account, client, chat_id, mid)
        self._emit(
            {
                "type": "win",
                "account": account,
                "bot": bot_username,
                "text": text[:300],
            }
        )

    def _mark_win_handled(
        self, account: str, bot_username: str, mid: Optional[int], text: str
    ) -> bool:
        """中奖消息去重并落盘：进程内 _win_handled + 状态文件 win:<msg_id>。

        返回 False 表示该消息已处理过，应跳过转发/推送。
        """
        dedup_key = (
            f"{account}|{bot_username.lower()}|{mid if mid is not None else hash(text)}"
        )
        if dedup_key in self._win_handled:
            return False
        if (
            mid is not None
            and self._state is not None
            and self._state.has(account, bot_username, f"win:{mid}")
        ):
            self._win_handled.add(dedup_key)
            return False
        self._win_handled.add(dedup_key)
        if mid is not None and self._state is not None:
            self._state.add(account, bot_username, f"win:{mid}", note="win")
            self._state.save()
        return True

    async def _forward_win_message(
        self, account: str, client: Any, chat_id: Any, mid: int
    ) -> None:
        """转发中奖原消息到各转发目标（默认收藏夹），单个失败不阻断其余。"""
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

    def _win_sweep_bots(self, account: str) -> List[str]:
        """该账号本轮待回扫的 bot 列表（机器人任务只回扫目标 bot）。"""
        if self._state is None:
            return []
        bots = self._state.bots_for(account)
        if self.source_type == "bot" and self.bot_username:
            # 机器人类型任务只关心目标 bot 的中奖通知：不回扫账号历史上
            # 参与过的其他 bot，避免启动阶段被十几个 bot 拖住几分钟
            target = self.bot_username.lower()
            bots = [b for b in bots if str(b).lower() == target]
        return bots

    def _begin_win_sweep(self, total: int, *, announce: bool = True) -> Dict[str, int]:
        """开始一轮中奖回扫：记录分母，返回本轮已完成计数。

        announce=False（补漏轮）不重置展示文案，让上一轮的完成进度继续
        显示，直到本轮有 bot 回扫完成再更新为本轮进度。
        """
        self.win_sweep_total = total
        self.win_sweep_done = 0
        if total and announce:
            self.win_sweep_text = f"中奖回扫 (0/{total})"
        return {"n": 0}

    def _advance_win_sweep(self, progress: Dict[str, int]) -> None:
        """完成一个 bot 的回扫后推进中奖回扫进度。"""
        progress["n"] += 1
        self.win_sweep_done = progress["n"]
        self.win_sweep_text = f"中奖回扫 ({progress['n']}/{self.win_sweep_total})"

    def _end_win_sweep(self) -> None:
        """本轮中奖回扫结束：保留进度文案，界面在回扫结束后继续展示。"""
        if self.win_sweep_total > 0:
            if self.win_sweep_done >= self.win_sweep_total:
                self.win_sweep_text = (
                    f"中奖回扫完成 ({self.win_sweep_done}/{self.win_sweep_total})"
                )
            else:
                # 中途停止/异常退出：保留已扫进度，不标「完成」
                self.win_sweep_text = (
                    f"中奖回扫 ({self.win_sweep_done}/{self.win_sweep_total})"
                )

    async def _sweep_all_accounts(self) -> None:
        """全部已连接账号并行回扫中奖消息，并单独维护该轮进度。"""
        if not self.win_detect or self._state is None:
            return
        pairs = [
            (account, client, self._win_sweep_bots(account))
            for account, client in list(self._clients.items())
        ]
        total = sum(len(bots) for _, _, bots in pairs)
        if total == 0:
            # 保留原有「暂无 bot 记录」日志
            for account, client, _ in pairs:
                await self._sweep_bot_wins(account, client, bots=[])
            return
        progress = self._begin_win_sweep(total)
        try:
            await asyncio.gather(
                *[
                    self._with_account_slot(
                        self._sweep_bot_wins,
                        account,
                        client,
                        bots=bots,
                        progress=progress,
                    )
                    for account, client, bots in pairs
                ]
            )
        finally:
            self._end_win_sweep()

    async def _sweep_bot_wins(
        self,
        account: str,
        client: Any,
        *,
        bots: Optional[List[str]] = None,
        progress: Optional[Dict[str, int]] = None,
    ) -> None:
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
        if bots is None:
            bots = self._win_sweep_bots(account)
        if not bots:
            # 无任何交互记录（含保留的 win: 去重）时没有回扫目标，等账号参与后重建
            self.log(f"[{account}] 回扫：暂无 bot 记录（等待账号参与后重建）")
            return
        for idx, bot in enumerate(bots):
            if self._stop is not None and self._stop.is_set():
                return
            # bot 之间随机间隔：一个账号 10+ 个 bot 连发 GetHistory 会在很短的
            # 时间内把连接打到 Transport flood 429（尤其多个账号同时回扫时）
            if idx > 0:
                await asyncio.sleep(random.uniform(*WIN_SWEEP_BOT_GAP_RANGE))
            await self._sweep_bot_messages(account, client, bot)
            if progress is not None:
                self._advance_win_sweep(progress)

    async def _sweep_bot_messages(self, account: str, client: Any, bot: str) -> None:
        """回扫单个 bot 的私聊消息：拉取批次并按时间正序处理、推进水位线。

        bot 从未开过会话/已失效等拉取失败静默跳过（调试日志），不影响其他 bot。
        """
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
            await self._process_sweep_batch(account, client, bot, watermark, batch)
        except Exception:
            logger.debug(f"回扫 @{bot} 私聊中奖消息失败", exc_info=True)

    async def _process_sweep_batch(
        self,
        account: str,
        client: Any,
        bot: str,
        watermark: Optional[int],
        batch: List[Any],
    ) -> None:
        """按时间正序处理回扫批次，记录结果并推进水位线。"""
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
            + (f"（新 {new_n} 条）" if watermark is not None else "（首次建基线）")
        )
        newest = getattr(batch[0], "id", None) if batch else None
        if newest is not None:
            self._state.set_win_watermark(account, bot, newest)
            # 立即落盘：处理中奖消息时会顺带 save 旧水位线
            self._state.save()

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
        max_id = 0
        reached = False
        for _ in range(WIN_SWEEP_PAGE_MAX):
            page: List[Any] = []
            async for message in client.get_chat_history(
                bot, max_id=max_id, limit=WIN_SWEEP_PAGE
            ):
                page.append(message)
            if not page:
                break
            reached, stop = self._absorb_history_page(
                batch, page, watermark, min_count, reached
            )
            if stop:
                break
            # max_id 为闭区间（含 max_id 本身）；下一页取 id 严格小于本页最旧消息。
            oldest_id = getattr(page[-1], "id", 0) or 0
            if oldest_id <= 1:
                break
            max_id = oldest_id - 1
        return batch

    @staticmethod
    def _absorb_history_page(
        batch: List[Any],
        page: List[Any],
        watermark: int,
        min_count: int,
        reached: bool,
    ) -> Tuple[bool, bool]:
        """吸收一页历史消息进 batch。返回 (reached, stop)。

        reached：本页是否碰到不新于水位线的消息；stop：累计扫描量已达
        min_count 下限，应停止翻页。
        """
        for message in page:
            mid = getattr(message, "id", None)
            if not reached and mid is not None and mid <= watermark:
                reached = True
                if len(batch) >= min_count:
                    break
            batch.append(message)
        return reached, reached and len(batch) >= min_count

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
        if self.source_type == "bot":
            username = getattr(chat, "username", None) or self.bot_username
            if username:
                return f"@{username}"
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
        tag = self._message_log_tag(message, skip_keywords)
        # 开奖结果公告（抽奖已结束）：链接/按钮提取前直接过滤，置顶路径同样生效
        if message_is_draw_announce(message):
            if log_skips:
                self.log(f"[{account}] {tag} 开奖结果公告，跳过")
            return None
        links = self._candidate_bot_links(message)
        if not links:
            await self._handle_missing_bot_links(
                account, client, message, tag, skip_keywords, log_skips
            )
            return None
        return await self._process_giveaway_links(
            account, client, message, tag, links, skip_keywords, log_skips
        )

    async def _process_giveaway_links(
        self,
        account: str,
        client: Any,
        message: Any,
        tag: str,
        links: list,
        skip_keywords: bool,
        log_skips: bool,
    ) -> Optional[str]:
        """消息含抽奖链接时的过滤与参与流程。返回 "joined"/"expired"/None。"""
        if self.source_type == "bot":
            # 机器人私聊的「已达标」消息已完成口令/发言/地区任务，
            # 只按关键词与开奖时间过滤，不再套用频道帖的参与条件检查
            if not skip_keywords and not message_matches_keywords(
                message, self.keywords
            ):
                return None
        elif self._message_filtered_out(
            account, message, tag, skip_keywords, log_skips
        ):
            return None
        channel = self._chat_title(message)
        title = _giveaway_title(message)
        expired = self._expired_draw_status(account, message, tag)
        if expired is not None:
            return expired
        if self._join_cap_reached(account):
            return None
        # 先过滤出可参与的链接（未参与过/未冷却/未熔断），全部不可参与时
        # 连「命中」日志也静默，避免熔断或历史去重期间刷屏
        actionable = self._filter_actionable_links(account, links)
        if not actionable:
            if log_skips:
                self.log(
                    f"[{account}] {tag} 命中 {len(links)} 个链接但均不可参与"
                    "（已参与过/已结束/限流熔断），跳过"
                )
            return None
        participated = await self._participate_actionable_links(
            account, client, message, channel, title, actionable
        )
        return "joined" if participated else None

    def _message_log_tag(self, message: Any, skip_keywords: bool) -> str:
        text = " ".join((_message_text(message) or "").split())
        mid = getattr(message, "id", None)
        # 置顶路径标「置顶消息」，历史扫描标「消息」，便于区分日志来源
        return (
            ("置顶消息 " if skip_keywords else "消息 ")
            + (str(mid) if mid is not None else "?")
            + (
                f"「{text[:30]}…」"
                if len(text) > 30
                else (f"「{text}」" if text else "")
            )
        )

    def _candidate_bot_links(self, message: Any) -> list:
        links = extract_bot_links(message)
        # 黑名单 bot 的链接不作为抽奖链接（整条消息无可用链接时直接跳过）
        links = [ln for ln in links if not bot_is_blocked(ln[0])]
        if self.source_type == "bot":
            # 机器人私聊来源：只参与当前机器人自己的抽奖入口，
            # 避免误点消息里夹带的其他推广 bot 链接
            target = self.bot_username.lower()
            links = [ln for ln in links if ln[0].lower() == target]
        return links

    async def _handle_missing_bot_links(
        self,
        account: str,
        client: Any,
        message: Any,
        tag: str,
        skip_keywords: bool,
        log_skips: bool,
    ) -> None:
        if log_skips:
            self.log(f"[{account}] {tag} 无可用抽奖 bot 链接，跳过")
        if skip_keywords:
            # 群组置顶的「按钮型」抽奖：帖内无可 start 的 bot 链接
            # （或仅剩黑名单推广链接），但帖上带「参与抽奖」按钮，
            # 直接点击源帖按钮完成参与
            await self._participate_via_source_button(account, client, message)
            return
        if self.source_type == "bot":
            # 机器人私聊的「按钮型」抽奖（按钮为 callback，无 start 链接）：
            # 与历史帖同一口径，先过关键词与开奖时间，再点击参与按钮
            if not message_matches_keywords(message, self.keywords):
                return
            if self._expired_draw_status(account, message, tag) is not None:
                return
            await self._participate_via_source_button(account, client, message)

    def _message_filtered_out(
        self,
        account: str,
        message: Any,
        tag: str,
        skip_keywords: bool,
        log_skips: bool,
    ) -> bool:
        if not skip_keywords and not message_matches_keywords(message, self.keywords):
            # 消息含 bot 链接但关键词未命中：静默跳过（日志逐条刷屏，不再打印）
            return True
        # 归属地限制过滤：仅参与无地区限制或含白名单地区的抽奖（对群组置顶同样生效）
        if not message_region_allowed(message):
            if log_skips:
                self.log(f"[{account}] {tag} 含地区限制，跳过")
            return True
        # 发言数要求/口令要求 + 双向账户：无法达标，自动跳过（对群组置顶同样生效）
        if account in self.two_way_accounts and (
            message_requires_speech(message) or message_requires_password(message)
        ):
            if log_skips:
                self.log(f"[{account}] {tag} 需发言数/口令，双向账户无法达标，跳过")
            return True
        return False

    def _expired_draw_status(
        self, account: str, message: Any, tag: str
    ) -> Optional[str]:
        # 开奖日期早于当前时间：自动跳过（始终带诊断日志，见下方注释）
        draw_dt = parse_draw_date(_message_text(message))
        if draw_dt is not None and draw_dt < datetime.now():
            # 已开奖跳过始终记录（不收 log_skips 门控）：排查止损漏扫问题
            self.log(
                f"[{account}] {tag} 开奖时间已过"
                f"（{draw_dt.strftime('%Y-%m-%d %H:%M')}），跳过"
            )
            return "expired"
        return None

    def _join_cap_reached(self, account: str) -> bool:
        # 参与条数上限（None = 不限），达到后本轮不再参与
        if self.max_joins is not None and self.joined >= self.max_joins:
            cap_key = ("cap", account)
            if cap_key not in self._skip_logged:
                self._skip_logged.add(cap_key)
                self.log(
                    f"[{account}] 已达参与上限 {self.max_joins} 个，后续命中不再参与"
                )
            return True
        return False

    def _filter_actionable_links(self, account: str, links: list) -> list:
        actionable: list = []
        for username, param in links:
            if self._stop is not None and self._stop.is_set():
                break
            if self.max_joins is not None and self.joined >= self.max_joins:
                break
            if not self._link_is_actionable(account, username, param):
                continue
            actionable.append((username, param))
        return actionable

    def _link_is_actionable(self, account: str, username: str, param: str) -> bool:
        if self._state is not None and self._state.is_ended(account, username, param):
            return False  # bot 曾回复「已结束」＝终态，静默跳过
        if self._participated_before(account, username, param):
            return False  # 已参与过且未忽略历史，静默跳过
        if self._state is not None and self._state.is_blocked(account, username, param):
            return False
        if self._flood_key(account, username) in self._flood_bots:
            return False
        if account in self._peer_flood_accounts:
            return False  # 账号已被 PEER_FLOOD 熔断，本轮不再参与任何抽奖
        if self._flood_paused(account, username):
            return False  # 该 bot 仍在限流暂停期内，等恢复后下一轮再试
        if self._flood_key(account, username) in self._ended_bots:
            return False  # 已结束回复熔断的 bot，静默跳过
        return True

    async def _participate_actionable_links(
        self,
        account: str,
        client: Any,
        message: Any,
        channel: str,
        title: str,
        actionable: list,
    ) -> int:
        # 命中多少个抽奖入口一目了然
        self.log(
            f"[{account}] 命中 {len(actionable)} 个抽奖链接："
            + "、".join(f"@{u}" for u, _ in actionable)
        )
        participated = 0
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
                # 机器人私聊消息本身就是参与入口（按钮即本次 start 参数），
                # 不把它当作「参与条件帖」再做加入/重发跟进
                source_message=None if self.source_type == "bot" else message,
            )
            if self.joined > before:
                participated += 1
        return participated

    async def _is_bot(self, account: str, client: Any, username: str) -> bool:
        cache_key = f"{account}|{username.lower()}"
        cached = self._bot_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            # 通过 get_users 拿到 is_bot 标记（不同层的 InputPeer 标记不统一）
            await self._pace_resolve(account)
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
    def _is_peer_flood(exc: Exception) -> bool:
        """PEER_FLOOD 为不可重试的账号级强制限流（没有 wait 秒数）。"""
        return "PEER_FLOOD" in str(exc).upper()

    @staticmethod
    def _is_channels_too_much(exc: Exception) -> bool:
        """账号已加入的频道/群组数达到 Telegram 上限（CHANNELS_TOO_MUCH）。"""
        return "CHANNELS_TOO_MUCH" in str(exc).upper()

    @staticmethod
    def _is_unreachable_target_error(exc: Exception) -> bool:
        """目标群组不可访问且本任务内重试无意义。

        CHANNEL_PRIVATE 表示账号未加入/已被踢出/链接已失效，
        CHANNEL_INVALID 表示目标已不存在；两者重试都不会成功，
        继续每轮尝试只会白白消耗请求额度并触发限流，直接跳过。
        """
        msg = str(exc).upper()
        return "CHANNEL_PRIVATE" in msg or "CHANNEL_INVALID" in msg

    @staticmethod
    def _is_network_timeout(exc: Exception) -> bool:
        """判断异常是否为网络临时异常（超时/连接断开/代理僵死等）。

        网络异常是瞬时的，账号/频道本身没有问题，应该允许下次重试，
        不应记入持久化的「加入失败」提醒或消耗 _join_cache 尝试机会。
        目前 GiveawayEngine 的 _TimeoutClient 会把 asyncio.TimeoutError
        包装成 RuntimeError，消息含「网络请求 … 超时」「网络异常」关键字；
        同时识别原生 TimeoutError / ConnectionError 作为兜底。
        """
        msg = str(exc)
        if "网络请求" in msg and "超时" in msg:
            return True
        if "网络异常" in msg:
            return True
        if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
            return True
        return False

    def _note_channels_full(
        self, account: str, exc: Exception, where: str = ""
    ) -> bool:
        """识别账号频道/群组已满：每账号本轮只回报一次，交由 WebUI 自动退频道。"""
        if not self._is_channels_too_much(exc):
            return False
        if account not in self._channels_full_notified:
            self._channels_full_notified.add(account)
            detail = f"（{where}）" if where else ""
            self.log(
                f"[{account}] 已加入的频道/群组达到 Telegram 上限，无法继续加入{detail}"
            )
            self._emit(
                {
                    "type": "channels_full",
                    "account": account,
                    "detail": where,
                    "error": str(exc)[:120],
                    "session_dir": str(self.session_dir or "."),
                }
            )
        return True

    def _block_peer_flood(self, account: str, detail: str = "") -> None:
        """PEER_FLOOD 熔断：本轮运行停止该账号的一切加入与参与。"""
        if account in self._peer_flood_accounts:
            return
        self._peer_flood_accounts.add(account)
        if self._state is not None:
            # PEER_FLOOD 没有官方解封时间，按建议的 24 小时记录，供账户页展示
            self._state.set_account_limit(
                account,
                "peer_flood",
                time.time() + 24 * 3600,
                "强制限流，建议停用 24 小时以上",
            )
        where = f"（{detail}）" if detail else ""
        self.log(
            f"[{account}] 触发 Telegram 强制限流 PEER_FLOOD{where}："
            "本账号本轮运行停止加入与参与，建议至少停用 24 小时后再试"
        )

    def _log_once(self, key: str, message: str) -> None:
        """同一原因只提示一次，避免每轮扫描重复刷日志。"""
        if key in self._skip_logged:
            return
        self._skip_logged.add(key)
        self.log(message)

    async def _pace_resolve(self, account: str) -> None:
        """解析类调用（get_users 等）的账号级最小间隔。

        一条消息里可能挂十几条抽奖链接，连续 resolve 同样会触发限流。
        """
        lo, hi = self.resolve_pace_range
        if hi <= 0:
            return
        now = time.monotonic()
        last = self._last_resolve_at.get(account)
        if last is not None:
            gap = random.uniform(lo, max(lo, hi)) - (now - last)
            if gap > 0:
                await asyncio.sleep(gap)
        self._last_resolve_at[account] = time.monotonic()

    def _account_join_events(self, account: str) -> List[float]:
        """该账号滚动窗口内的加入时间，优先读持久化状态（跨重启生效）。"""
        if self._state is not None:
            return self._state.join_events(account)
        cutoff = time.time() - JOIN_LOG_WINDOW
        kept = [t for t in self._join_events.get(account, []) if t >= cutoff]
        self._join_events[account] = kept
        return kept

    def _note_join_attempt(self, account: str) -> None:
        """记录一次加入尝试（失败也计入配额：Telegram 按请求次数限流）。"""
        if self._state is not None:
            self._state.add_join_event(account)
            self._state.save()
        else:
            self._join_events.setdefault(account, []).append(time.time())

    @staticmethod
    def _is_invite_target(target: Any) -> bool:
        """私有邀请链接（t.me/+hash、joinchat、http(s)://）目标。"""
        text = str(target or "").strip().lower()
        return text.startswith(("http://", "https://")) or "joinchat" in text

    def _suspend_joins(self, account: str, wait: int, target: Any = "") -> None:
        """加入触发 FLOOD_WAIT：只暂停该账号的加入，其它账号照常。

        私有邀请链接的限流通常只作用于 ImportChatInvite，不波及 @username
        的 JoinChannel，因此单独暂停：期间邀请链接跳过，用户名加入继续。
        """
        pause = max(1, int(wait)) + self.join_flood_buffer
        until = time.monotonic() + pause
        resume_at = datetime.now() + timedelta(seconds=pause)
        if self._state is not None:
            self._state.set_account_limit(
                account,
                "join_flood",
                time.time() + pause,
                f"邀请链接 {target}（@username 加入不受影响）"
                if self._is_invite_target(target)
                else f"加入 {target}",
            )
        if self._is_invite_target(target):
            self._invite_suspended[account] = max(
                self._invite_suspended.get(account, 0.0), until
            )
            self._invite_suspend_logged.discard(account)
            self.log(
                f"[{account}] 邀请链接加入 {target} 触发限流（需等待 {wait} 秒），"
                f"邀请链接暂停至 {resume_at:%H:%M}（@username 加入照常）"
            )
            return
        self._join_suspended[account] = max(
            self._join_suspended.get(account, 0.0), until
        )
        self._join_suspend_logged.discard(account)
        self.log(
            f"[{account}] 加入 {target} 触发限流（需等待 {wait} 秒），"
            f"本账号自动加入暂停至 {resume_at:%H:%M}"
        )

    def _invite_blocked(self, account: str, target: Any) -> bool:
        """该目标是否因邀请链接限流而被挡住（仅影响邀请链接目标）。"""
        if not self._is_invite_target(target):
            return False
        until = self._invite_suspended.get(account)
        if until is None:
            return False
        left = until - time.monotonic()
        if left <= 0:
            self._invite_suspended.pop(account, None)
            return False
        if account not in self._invite_suspend_logged:
            self._invite_suspend_logged.add(account)
            resume_at = datetime.now() + timedelta(seconds=left)
            self.log(
                f"[{account}] 邀请链接加入限流暂停中，恢复时间 "
                f"{resume_at:%H:%M}（期间只跳过邀请链接，@username 照常）"
            )
        return True

    async def _join_ready(self, account: str, target: Any = "") -> bool:
        """加入前统一闸门，返回是否允许本次加入（需要时已按节奏等待）。

        - PEER_FLOOD 账号：直接拒绝
        - FLOOD_WAIT 暂停期内：直接拒绝
        - 滚动 1 小时 / 24 小时配额（0=不限制）：达上限后拒绝
        - 相邻两次加入的最小随机间隔：不足则先 sleep 补齐
        """
        if account in self._peer_flood_accounts:
            return False
        if self._invite_blocked(account, target):
            return False
        until = self._join_suspended.get(account)
        if until is not None:
            if time.monotonic() < until:
                return False
            self._join_suspended.pop(account, None)
        events = self._account_join_events(account)
        wall = time.time()
        hourly = sum(1 for t in events if t > wall - 3600)
        daily = sum(1 for t in events if t > wall - 86400)
        if self.join_hourly_cap > 0 and hourly >= self.join_hourly_cap:
            self._log_once(
                f"join_hourly_cap|{account}",
                f"[{account}] 最近 1 小时已加入 {hourly} 个频道/群组"
                f"（上限 {self.join_hourly_cap}），本轮暂停自动加入",
            )
            return False
        if self.join_daily_cap > 0 and daily >= self.join_daily_cap:
            self._log_once(
                f"join_daily_cap|{account}",
                f"[{account}] 最近 24 小时已加入 {daily} 个频道/群组"
                f"（上限 {self.join_daily_cap}），为避免被限流今日不再自动加入",
            )
            return False
        lo, hi = self.join_pace_range
        if hi > 0 and events:
            gap = random.uniform(lo, max(lo, hi)) - (wall - events[-1])
            if gap > 0:
                await asyncio.sleep(gap)
        return True

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

    def _flood_paused(self, account: str, username: str) -> bool:
        """该账号对该 bot 是否仍在限流暂停期内（到期自动解除）。"""
        fkey = self._flood_key(account, username)
        until = self._flood_until.get(fkey)
        if until is None:
            return False
        if time.monotonic() >= until:
            self._flood_until.pop(fkey, None)
            return False
        return True

    def _log_transport_flood_429(self, account: str) -> None:
        """记录每次 429 的账号与时刻，并区分「单账号过密」和「多账号同时被风控」。

        Pyrogram 的 429 只打一行无账号前缀的会话日志，事后无法判断是哪个账号、
        也无法判断是否集中在同一时段，因此这里补一条带账号和时刻的明细。
        """
        now = time.monotonic()
        wall = time.time()
        self._transport_flood_hits[account] = wall
        window = TRANSPORT_FLOOD_CLUSTER_WINDOW
        recent = {
            acc: at
            for acc, at in self._transport_flood_hits.items()
            if wall - at <= window
        }
        self._transport_flood_hits = recent
        if (
            now - self._transport_flood_429_logged.get(account, 0.0)
            < TRANSPORT_FLOOD_LOG_INTERVAL
        ):
            return
        self._transport_flood_429_logged[account] = now
        if len(recent) >= TRANSPORT_FLOOD_CLUSTER_SIZE:
            accounts = "、".join(sorted(recent))
            text = (
                f"[429] {window:.0f} 秒内 {len(recent)} 个账号触发 Transport flood"
                f"（{accounts}）：疑似共享出口/IP 层风控，建议减少同时在线账号数或换出口"
            )
            alert_key = "cluster"
            alert_interval = TRANSPORT_FLOOD_ALERT_INTERVAL_CLUSTER
        else:
            text = (
                f"[429] [{account}] 触发 Transport flood"
                f"（{datetime.fromtimestamp(wall):%H:%M:%S}），单账号请求过密，"
                "已按退避放缓"
            )
            alert_key = f"single:{account}"
            alert_interval = TRANSPORT_FLOOD_ALERT_INTERVAL_SINGLE
        self.log(text)
        if now - self._transport_flood_alerted.get(alert_key, 0.0) < alert_interval:
            return
        self._transport_flood_alerted[alert_key] = now
        self._spawn_flood_alert(text)

    def _spawn_flood_alert(self, text: str) -> None:
        """把 429 判定发到自己的通知目标（默认收藏夹）。

        限流期间发送本身也消耗配额，因此只在判定为「单账号/多账号」的那一刻
        发一条，并交给独立任务执行，不阻塞请求链路。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._send_flood_alert(text))
        self._flood_alert_tasks.add(task)
        task.add_done_callback(self._flood_alert_tasks.discard)

    async def _send_flood_alert(self, text: str) -> None:
        """用未处于限流状态的账号把 429 提醒发到通知目标（默认收藏夹）。"""
        candidates = [
            (acc, client)
            for acc, client in list(self._clients.items())
            if acc not in self._transport_flood_hits
        ]
        for target in self.win_forward_targets:
            name = "收藏夹" if target == "me" else target
            for account, client in candidates:
                try:
                    await client.send_message(target, text)
                    self.log(f"[{account}] 429 提醒已发送到 {name}")
                    break
                except Exception:  # noqa: BLE001 - 提醒失败不影响业务
                    logger.debug("429 提醒发送失败", exc_info=True)
                    continue
            else:
                self.log(f"429 提醒发送到 {name} 失败：没有可用账号（均在限流或断连）")

    def _note_transport_flood(self, account: str, seconds: float) -> None:
        """协议层 Transport flood 429：落盘账号限流，账户页才能显示「限流中」。

        Pyrogram 收到 429 只重启会话、不抛异常，调用方拿到的是「请求超时」，
        因此这条链路必须单独回写，否则账户页始终显示「限流：正常」。
        """
        resume_at = datetime.now() + timedelta(seconds=seconds)
        if self._state is not None:
            self._state.set_account_limit(
                account,
                "flood_wait",
                time.time() + seconds,
                "Transport flood 429（协议层限流）",
            )
        now = time.monotonic()
        self._log_transport_flood_429(account)
        if now - self._transport_flood_logged.get(account, 0.0) < 300:
            return
        self._transport_flood_logged[account] = now
        self.log(
            f"[{account}] Telegram 协议层限流（Transport flood 429），"
            f"已同步到账户页（约至 {resume_at:%H:%M}）"
        )

    def _record_flood(
        self,
        account: str,
        username: str,
        param: str,
        channel: str,
        exc: Exception,
        label: str = "参与失败",
    ) -> bool:
        """统一登记一次 FLOOD_WAIT：计入熔断次数、按 bot 级别暂停、持久化冷却。

        返回 True 表示确是限流异常（调用方据此跳过后续跟进而不要当普通错误）。
        Transport flood 429 也按 FLOOD_WAIT 处理：Pyrogram 不会把它映射成
        errors.FloodWait，需要显式识别并给默认等待秒数。
        """
        # PEER_FLOOD 没有 wait 秒数、重试也不会恢复：按账号整体熔断
        if self._is_peer_flood(exc):
            self._block_peer_flood(account, f"{label} @{username}")
            return True
        wait = self._flood_seconds(exc)
        if wait <= 0 and _is_transport_flood(exc):
            # Transport flood 429：协议层限流，默认等 30 秒（指数退避已由
            # _TimeoutClient 在上游执行，这里只需要登记熔断计数避免继续打）
            wait = 30
        if wait <= 0:
            return False
        fkey = self._flood_key(account, username)
        strikes = self._flood_counts.get(fkey, 0) + 1
        self._flood_counts[fkey] = strikes
        # 按 bot 级别暂停到限流解除：暂停期间同账号不再对该 bot 发 start
        resume_at = time.monotonic() + wait
        self._flood_until[fkey] = max(self._flood_until.get(fkey, 0.0), resume_at)
        if self._state is not None:
            # 落盘账号级限流，供账户页展示剩余时长（落盘用 wall clock）
            self._state.set_account_limit(
                account, "flood_wait", time.time() + wait, f"@{username}（{param}）"
            )
        # 已参与（joined）或已结束（ended）的记录不能被限流记录覆盖，
        # 否则会把成功状态翻成失败并在冷却后重复参与
        if self._state is not None and not self._state.has(account, username, param):
            self._state.add(
                account,
                username,
                param,
                channel=channel,
                note=f"flood_wait:{wait}",
            )
            self._state.save()
        self.log(
            f"[{account}] {label} @{username}（{param}）: "
            f"Telegram 限流，需等待 {wait} 秒"
            f"（连续第 {strikes} 次）"
        )
        if wait >= FLOOD_HARD_BLOCK_SECONDS:
            if fkey not in self._flood_bots:
                self._flood_bots.add(fkey)
                self.log(
                    f"[{account}] @{username} 限流需等待 {wait} 秒"
                    f"（≥{FLOOD_HARD_BLOCK_SECONDS} 秒），本轮运行直接跳过该 bot"
                )
        elif strikes >= MAX_FLOOD_STRIKES:
            if fkey not in self._flood_bots:
                self._flood_bots.add(fkey)
                self.log(
                    f"[{account}] @{username} 连续限流 {strikes} 次，本轮运行跳过该 bot"
                )
        return True

    async def _start_bot(
        self, client: Any, account: str, username: str, param: str
    ) -> Any:
        """带最小间隔的 StartBot：同一 bot 相邻两次调用至少间隔 START_MIN_INTERVAL。"""
        fkey = self._flood_key(account, username)
        last = self._last_start_at.get(fkey)
        if last is not None:
            gap = START_MIN_INTERVAL - (time.monotonic() - last)
            if gap > 0:
                await asyncio.sleep(gap)
        self._last_start_at[fkey] = time.monotonic()
        return await client.start_bot(username, param)

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
        if not self._can_participate_link(account, username, param):
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
            start_msg = await self._start_bot(client, account, username, param)
        except Exception as exc:
            self._participate_start_failed(
                account, username, param, channel, exc, reserved
            )
            return
        await self._handle_start_reply(
            account,
            client,
            username,
            param,
            channel,
            title,
            reserved,
            start_msg,
            source_message,
        )

    def _can_participate_link(self, account: str, username: str, param: str) -> bool:
        """发 start 前的本地守卫：已参与过（未忽略历史）或 bot 终态「已结束」时跳过。"""
        if account in self._peer_flood_accounts:
            return False  # 账号已被 PEER_FLOOD 熔断，本轮不再发任何 start
        if self._participated_before(account, username, param):
            return False
        if self._state is not None and self._state.is_ended(account, username, param):
            # bot 曾回复「已结束」＝终态，无论是否忽略历史都不再发 start
            return False
        if self._flood_paused(account, username):
            return False
        return True

    async def _handle_start_reply(
        self,
        account: str,
        client: Any,
        username: str,
        param: str,
        channel: str,
        title: str,
        reserved: bool,
        start_msg: Any,
        source_message: Any,
    ) -> None:
        """start 发出后的回复分流：已结束终态 / 参与成功记录 / 点确认按钮。"""
        # bot 对本次 start 直接回复「已结束」（如 @Lottery8Bot 的
        # "The event has ended or does not exist"）＝终态：归还名额、
        # 不记已参与，持久化标记防止今后重试，并累计 bot 熔断计数
        if reply_says_ended(start_msg):
            self._handle_start_ended(account, username, param, channel, title, reserved)
            return
        completed = await self._click_confirm(
            account, client, username, start_msg, param, source_message=source_message
        )
        if not completed:
            # bot 要求加入频道，但加入没生效（限流暂停，或点击后 bot 仍要求
            # 先加入）：不能记「已参与」，否则后续扫描会当成功跳过、用户看到假成功
            self._handle_join_blocked(account, username, channel, title, reserved)
            return
        self._record_participate_success(
            account, username, param, channel, title, reserved
        )

    def _participate_start_failed(
        self,
        account: str,
        username: str,
        param: str,
        channel: str,
        exc: Exception,
        reserved: bool,
    ) -> None:
        """start 失败处理：归还名额、失败计数、FLOOD_WAIT 熔断判定与事件推送。"""
        if reserved:
            self.joined -= 1
        self.failed += 1
        if not self._record_flood(account, username, param, channel, exc):
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

    def _handle_start_ended(
        self,
        account: str,
        username: str,
        param: str,
        channel: str,
        title: str,
        reserved: bool,
    ) -> None:
        """start 回复「已结束」终态处理：归还名额、持久化标记并累计熔断计数。"""
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

    def _record_participate_success(
        self,
        account: str,
        username: str,
        param: str,
        channel: str,
        title: str,
        reserved: bool,
    ) -> None:
        """记录参与成功：持久化记录、计数与事件推送。"""
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

    def _handle_join_blocked(
        self,
        account: str,
        username: str,
        channel: str,
        title: str,
        reserved: bool,
    ) -> None:
        """参与跟进未完成（需加入频道但未生效）：归还名额、不记成功，保留重试。

        两种情形走这里：本账号加入被限流/配额暂停；或已发 start/点按钮后
        bot 仍回复要求先加入频道。两者都不能写参与记录，否则后续扫描会
        把它当成功跳过，用户看到假成功。
        """
        if reserved:
            self.joined -= 1
        self.failed += 1
        from_part = f"来自 {channel or '未知频道'}"
        if title:
            from_part += f"：{title}"
        self.log(
            f"{datetime.now():%H:%M:%S} [未参与] {account} -> @{username} "
            f"（{from_part}）: bot 要求加入频道但本次未生效，稍后重试"
        )
        self._emit(
            {
                "account": account,
                "bot": username,
                "channel": channel,
                "title": title,
                "ok": False,
                "error": "需加入频道但参与未生效，稍后重试",
            }
        )

    async def _join_from_message(
        self, account: str, client: Any, bot_username: str, message: Any
    ) -> Tuple[bool, bool]:
        """激进模式：把消息中出现的频道/群组链接逐个加入。

        返回（是否发生加入，是否因限流/配额拦截而未能完成加入）。
        """
        joined_any = False
        blocked = False
        label = f"@{bot_username}" if bot_username else "置顶抽奖帖"
        if self._join_paused(account):
            return False, True
        targets = self._join_targets(account, bot_username, message)
        for target in targets:
            if target.lower().lstrip("@") == bot_username.lower():
                continue  # 跳过 bot 自己
            key = f"{account}|{target.lower()}"
            if key in self._join_cache:
                continue
            # 已判定不可访问（私密/失效）：冷却期内不再重复尝试，也不消耗加入配额
            if self._target_unreachable(account, target):
                continue
            self._join_cache.add(key)
            if not await self._should_join_target(account, client, target, label):
                continue
            joined, flood_paused = await self._join_single_target(
                account, client, target, label
            )
            if joined:
                joined_any = True
            if flood_paused:
                # 限流/配额拦截不消耗尝试机会：恢复后（下一轮扫描）重试
                self._join_cache.discard(key)
                blocked = True
                break
        return joined_any, blocked

    @staticmethod
    def _target_key(account: str, target: Any) -> str:
        return f"{account}|{str(target).strip().lower()}"

    def _mark_unreachable(self, account: str, *targets: Any) -> bool:
        """标记目标不可访问（TTL 内跳过），返回是否首次标记。"""
        first = False
        until = time.time() + UNREACHABLE_TARGET_TTL
        for target in targets:
            key = self._target_key(account, target)
            # 释放加入缓存：否则 TTL 到期后仍被 _join_cache 永久拦下
            self._join_cache.discard(f"{account}|{str(target).lower()}")
            if key in self._unreachable_targets:
                continue
            self._unreachable_targets[key] = until
            first = True
        return first

    def _target_unreachable(self, account: str, *targets: Any) -> bool:
        """目标是否仍在不可访问冷却期内；过期条目顺手清理，到期后可再试。"""
        now = time.time()
        for target in targets:
            key = self._target_key(account, target)
            until = self._unreachable_targets.get(key)
            if until is None:
                continue
            if until > now:
                return True
            self._unreachable_targets.pop(key, None)
        return False

    def _join_flood_paused(self, account: str) -> bool:
        """是否因限流（PEER_FLOOD / JoinChannel FLOOD_WAIT）暂停加入，不写日志。

        仅用于区分日志原因：网络异常也会阻塞加入，但不应报成「限流」。
        """
        if account in self._peer_flood_accounts:
            return True
        until = self._join_suspended.get(account)
        return until is not None and until > time.monotonic()

    def _join_paused(self, account: str) -> bool:
        if account in self._peer_flood_accounts:
            return True
        until = self._join_suspended.get(account)
        if until is None:
            return False
        left = until - time.monotonic()
        if left <= 0:
            self._join_suspended.pop(account, None)
            return False
        if account not in self._join_suspend_logged:
            self._join_suspend_logged.add(account)
            resume_at = datetime.now() + timedelta(seconds=left)
            self.log(
                f"[{account}] 自动加入被限流暂停中，恢复时间 "
                f"{resume_at:%H:%M}（暂停期间跳过该账号的所有加入）"
            )
        return True

    def _join_targets(self, account: str, bot_username: str, message: Any) -> List[str]:
        targets = extract_channel_targets(message)
        if not bot_policy(bot_username).join_status_checklist:
            return targets
        # LotteryBestBot 的回复把「加入-名称」做成带隐藏链接的❌/✅清单，
        # 补充提取其中 ❌（未加入）行的链接目标
        known = {t.lower() for t in targets}
        status_targets = [
            t for t in extract_join_status_targets(message) if t.lower() not in known
        ]
        if not status_targets:
            self._log_join_status_diagnostic(account, message)
        return targets + status_targets

    def _log_join_status_diagnostic(self, account: str, message: Any) -> None:
        raw = _message_text(message)
        # 「└ 🎟️ 发言数大于 N 条 ❌」等激活任务子行也带 ❌，但不含
        # 「加入」，属于正常状态不是未加入频道行；仅当存在真正的
        # 「加入-名称 ❌」行却提取不到链接时才报诊断，避免发言任务
        # 行触发解析失败误报
        has_unjoined_row = bool(raw) and any(
            "加入" in ln and "❌" in ln and "✅" not in ln for ln in raw.split("\n")
        )
        if not (raw and has_unjoined_row):
            return
        detail = self._describe_unjoined_rows(raw, message)
        self.log(
            f"[{account}] @LotteryBestBot 清单含未加入(❌)行但未解析到"
            f"链接（逐行明细：{detail}）"
        )

    def _describe_unjoined_rows(self, raw: str, message: Any) -> str:
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
        return "；".join(
            "L{}`{}`{}`".format(
                i + 1,
                raw[ls:le].strip()[:36],
                ("→" + "、".join(line_urls[i])) if i in line_urls else "",
            )
            for i, (ls, le) in enumerate(bounds)
            if "❌" in raw[ls:le] or "✅" in raw[ls:le] or i in line_urls
        )

    async def _should_join_target(
        self, account: str, client: Any, target: str, label: str
    ) -> bool:
        # 用户名目标先识别类型：个人用户/机器人/失效目标直接跳过，
        # 避免发出注定失败的 JoinChannel（CHANNEL_INVALID 噪音）。
        # 私有邀请链接（+hash/joinchat）无法预解析，仍直接尝试加入。
        if not target.lower().startswith(("http://", "https://")):
            type_name = await self._resolve_join_target_type(client, target)
            if type_name is None:
                self.log(f"[{account}] 跳过无效目标 {target}（无法解析或已失效）")
                return False
            if type_name not in ("GROUP", "SUPERGROUP", "CHANNEL"):
                return False  # 个人用户/机器人，静默跳过
            # 加入前检查成员身份：能看到哪些目标还没加入（bot 常要求
            # 加入频道后才给抽奖资格）
            already_member = await self._is_member(client, target)
            if already_member:
                self.log(f"[{account}] 已在 {target} 中，跳过加入")
                return False
            self.log(
                f"[{account}] 尚未加入 {target}"
                f"（{label} 要求加入后才能参与，自动加入中）"
            )
        return True

    async def _join_single_target(
        self, account: str, client: Any, target: str, label: str
    ) -> Tuple[bool, bool]:
        """加入单个目标，返回（是否发生加入，是否因限流/配额中断后续目标）。"""
        if not await self._join_ready(account, target):
            if self._invite_blocked(account, target):
                # 邀请链接限流只挡这一条，继续处理后面的 @username 目标
                self.log(
                    f"[{account}] 邀请链接加入限流中，跳过 {target}"
                    "（@username 加入照常）"
                )
                return False, False
            return False, True
        # 闸门放行后先记账：Telegram 按请求次数限流，失败请求同样消耗额度
        self._note_join_attempt(account)
        try:
            chat = await client.join_chat(target)
            title = str(getattr(chat, "title", "") or target)
            self.log(f"[{account}] 已加入 {title}（来自 {label}）")
            return True, False
        except Exception as exc:
            msg = str(exc)
            if self._is_peer_flood(exc):
                self._block_peer_flood(account, f"加入 {target}")
                return False, True
            if self._note_channels_full(account, exc, f"加入 {target}"):
                return False, False
            if "ALREADY_PARTICIPANT" in msg or "already" in msg.lower():
                self.log(f"[{account}] 已在 {target} 中，跳过加入")
            elif "INVITE_REQUEST_SENT" in msg:
                self.log(
                    f"[{account}] 已提交加入 {target} 的申请"
                    "（私有群/频道，待管理员审批）"
                )
                return True, False
            elif "FLOOD_WAIT" in msg or _is_transport_flood(exc):
                wait = self._flood_seconds(exc) or 60
                self._suspend_joins(account, wait, target)
                return False, True
            elif self._is_network_timeout(exc):
                # 网络临时异常：类似限流，不消耗 _join_cache 尝试机会
                logger.debug(
                    "消息链接加入触发网络异常，跳过本轮：account=%s target=%s err=%s",
                    account,
                    target,
                    str(exc)[:120],
                )
                return False, True
            elif self._is_unreachable_target_error(exc):
                # 私密/已失效群组：加入永远失败，本任务内直接跳过
                self._mark_unreachable(account, target)
                self.log(f"[{account}] {target} 不可访问（私密/已失效，本任务内跳过）")
                return False, False
            else:
                self.log(f"[{account}] 加入 {target} 失败: {msg[:120]}")
            return False, False

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
        tasks = self._collect_password_tasks(bot_messages, source_message)
        if not tasks:
            return
        name_links = self._collect_join_name_links(bot_messages, link_messages)
        for group_name, password in tasks:
            sent_key = f"{account}|{username}|{group_name.lower()}|{password}"
            if not ignore_sent and sent_key in self._password_sent:
                continue
            # 该群已判定不可访问（被踢/链接失效）：冷却期内不再尝试
            if self._target_unreachable(account, group_name):
                continue
            target = self._match_password_target(group_name, name_links)
            if target is None:
                self._log_missing_password_target(account, group_name, password)
                continue
            send_target = await self._prepare_password_send_target(
                account, client, group_name, password, target
            )
            if send_target is None:
                continue
            await self._send_password_to_target(
                account, client, username, group_name, password, sent_key, send_target
            )

    def _collect_password_tasks(
        self, bot_messages: List[Any], source_message: Any
    ) -> List[Tuple[str, str]]:
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
        return tasks

    def _collect_join_name_links(
        self, bot_messages: List[Any], link_messages: Optional[List[Any]]
    ) -> Dict[str, str]:
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
        return name_links

    @staticmethod
    def _match_password_target(
        group_name: str, name_links: Dict[str, str]
    ) -> Optional[str]:
        target = name_links.get(group_name.lower())
        if target is not None:
            return target
        # 名称不完全一致时按包含关系模糊匹配（帖子群名 vs 清单行名）
        return next(
            (t for n, t in name_links.items() if group_name in n or n in group_name),
            None,
        )

    def _log_missing_password_target(
        self, account: str, group_name: str, password: str
    ) -> None:
        self.log(
            f"[{account}] 口令群「{group_name}」未在清单中定位到链接，跳过发送口令"
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

    def _log_password_group_join_failure(
        self, account: str, group_name: str, password: str, detail: str
    ) -> None:
        self.log(f"[{account}] 加入口令群「{group_name}」失败，无法发送口令: {detail}")
        self._emit(
            {
                "type": "speak_failed",
                "account": account,
                "group": group_name,
                "password": password,
                "error": f"加入口令群失败: {detail}",
            }
        )

    async def _prepare_password_send_target(
        self, account: str, client: Any, group_name: str, password: str, target: str
    ) -> Any:
        # 私有邀请链接目标：先解析成真实 chat id（未加入则顺带加入），
        # 直接拿链接字符串 send_message 会误走用户名解析必失败
        send_target: Any = target
        if self._target_unreachable(account, group_name, target):
            return None
        invite_match = _INVITE_LINK_RE.match(str(target).strip())
        if invite_match:
            send_target = await self._resolve_invite_target(
                account, client, invite_match.group(1), str(target).strip()
            )
            if send_target is None:
                self._log_password_group_join_failure(
                    account,
                    group_name,
                    password,
                    "邀请链接解析失败（私有群可能需管理员审批）",
                )
                return None
            # 解析即加入：无需再做成员检查/二次 join
            already = True
        else:
            already = await self._is_member(client, send_target)
        # 成员检查返回 None（无法判断）时也要尝试加入：直接发消息会拿到
        # CHANNEL_PRIVATE 并每轮重复失败，先 join 一次更可靠
        if already is not True:
            try:
                await client.join_chat(send_target)
                self.log(f"[{account}] 已加入口令群「{group_name}」")
                # 成员状态同步后再发言，避免 bot 侧校验不到
                await asyncio.sleep(3)
            except Exception as exc:
                if self._is_unreachable_target_error(exc):
                    self._mark_unreachable(account, group_name, target, send_target)
                    self.log(
                        f"[{account}] 口令群「{group_name}」不可访问"
                        "（私密/已失效，本任务内跳过）"
                    )
                    return None
                self._note_channels_full(account, exc, f"加入口令群「{group_name}」")
                self._log_password_group_join_failure(
                    account, group_name, password, str(exc)[:120]
                )
                return None
        return send_target

    @staticmethod
    def _password_gate_keys(group_name: str, send_target: Any) -> Tuple[str, str]:
        """口令发言排期的两个键：群名 + 目标，覆盖同一群组的不同解析路径。"""
        return (
            f"name:{group_name.strip().lower()}",
            f"target:{str(send_target).strip().lower()}",
        )

    async def _wait_password_send_slot(
        self, account: str, group_name: str, send_target: Any
    ) -> bool:
        """同群口令发言错峰排期；返回 False 表示任务已停止、放弃本次发送。

        多账号并行扫描时会几乎同时命中同一条抽奖，若都在几秒内往同一个群
        发口令，看起来就是几个账号扎堆刷屏。这里把群组当作共享资源：每个
        账号先同步预约自己的发言时段（检查与写入之间无 await，单事件循环内
        天然原子），再按排期等待，后到者在上一个账号的随机间隔走完后再发。
        等待只阻塞当前账号，其他群组的参与不受影响。
        """
        keys = sorted(self._password_gate_keys(group_name, send_target))
        now = time.monotonic()
        due = max(self._password_gate_until.get(key, 0.0) for key in keys)
        slot_at = max(now, due)
        lo, hi = self.password_send_gap
        gap = random.uniform(lo, max(lo, hi))
        for key in keys:
            self._password_gate_until[key] = slot_at + gap
        wait = slot_at - now + random.uniform(2.0, 6.0)
        if wait >= 5:
            self.log(f"[{account}] 口令群「{group_name}」错峰等待 {wait:.0f} 秒")
        if self._stop is not None:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            else:
                return False
        else:
            await asyncio.sleep(wait)
        return True

    async def _send_password_to_target(
        self,
        account: str,
        client: Any,
        username: str,
        group_name: str,
        password: str,
        sent_key: str,
        send_target: Any,
    ) -> None:
        try:
            # 同群错峰：多账号不在同一时刻向同群发言，也不扎堆连续发言
            if not await self._wait_password_send_slot(
                account, group_name, send_target
            ):
                return
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
            if self._is_unreachable_target_error(exc):
                # 群组已不可访问（被踢/链接失效）：标记后本任务内不再尝试
                fresh = self._mark_unreachable(account, group_name, send_target)
                if fresh:
                    self.log(
                        f"[{account}] 口令群「{group_name}」不可访问"
                        "（私密/已失效，本任务内跳过）"
                    )
                return
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
        self, account: str, client: Any, invite_hash: str, invite_link: str
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
        except Exception as exc:
            if self._is_unreachable_target_error(exc):
                # 私密/失效邀请链接：重试不会成功，本任务内不再尝试
                self._mark_unreachable(account, invite_link)
            self._note_channels_full(account, exc, "加入私有邀请链接")
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
        # 该账号自动加入被限流暂停中：不消耗本次尝试机会，恢复后重试
        if self._join_paused(account):
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
        await self._join_monitor_candidates(account, client, chat, key, candidates)

    async def _join_monitor_candidates(
        self,
        account: str,
        client: Any,
        chat: Any,
        key: str,
        candidates: List[Any],
    ) -> None:
        """逐个候选目标尝试加入监控对象；全部失败才记失败日志。"""
        last_exc: Optional[Exception] = None
        for cand in candidates:
            # 与消息链接加入共用同一闸门：配额/限流期间整体暂停，恢复后重试
            if not await self._join_ready(account, cand):
                if self._invite_blocked(account, cand):
                    continue  # 仅邀请链接被限，继续试下一个候选（如 @username）
                return
            self._note_join_attempt(account)
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
                if self._monitor_join_error_handled(account, chat, key, exc):
                    return
                # 其他失败（如数字 id 无法解析）：还有候选目标则继续尝试
        # 全部候选失败：区分网络异常 vs 真实失败
        if last_exc is not None and self._is_network_timeout(last_exc):
            # 网络临时异常：不记入持久化的「加入失败」提醒，允许下次重试
            logger.debug(
                "监控对象加入全部候选遇网络异常，跳过本轮：account=%s chat=%s err=%s",
                account,
                chat,
                str(last_exc)[:120],
            )
            return
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

    def _monitor_join_error_handled(
        self, account: str, chat: Any, key: str, exc: Exception
    ) -> bool:
        """监控对象加入失败的分类处理。返回 True 表示已终态、停止尝试更多候选。"""
        msg = str(exc)
        if self._is_peer_flood(exc):
            self._block_peer_flood(account, f"加入监控对象 {chat}")
            self._join_cache.discard(key)  # 熔断不消耗尝试机会
            return True
        if self._note_channels_full(account, exc, f"加入监控对象 {chat}"):
            self._join_cache.add(key)  # 已满：本轮不再对该对象重试
            return True
        if "ALREADY_PARTICIPANT" in msg or "already" in msg.lower():
            self._join_cache.add(key)
            self._emit({"type": "join_resolved", "account": account, "chat": str(chat)})
            return True
        if "INVITE_REQUEST_SENT" in msg:
            self._join_cache.add(key)
            self.log(
                f"[{account}] 已提交加入 {chat} 的申请（私有群/频道，待管理员审批）"
            )
            self._emit({"type": "join_resolved", "account": account, "chat": str(chat)})
            return True
        if "FLOOD_WAIT" in msg or _is_transport_flood(exc):
            wait = self._flood_seconds(exc) or 60
            self._suspend_joins(account, wait, f"监控对象 {chat}")
            self._emit(
                {
                    "type": "join_failed",
                    "account": account,
                    "chat": str(chat),
                    "error": f"触发限流，需等待 {wait} 秒后重试",
                }
            )
            self._join_cache.discard(key)  # 限流不消耗尝试机会，恢复后重试
            return True
        if self._is_network_timeout(exc):
            # 网络临时异常：账号/频道本身没问题，允许下次重试，
            # 不消耗 _join_cache 尝试机会，不写入持久化的「加入失败」提醒。
            logger.debug(
                "监控对象加入触发网络异常，跳过本轮：account=%s chat=%s err=%s",
                account,
                chat,
                str(exc)[:120],
            )
            self._join_cache.discard(key)
            return True
        return False

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
        targets = self._collect_verify_targets(account, username, message, order)
        if targets is None:
            return True
        self.log(f"[{account}] @{username} 防脚本验证：按顺序点击 {' → '.join(order)}")
        await self._click_verify_buttons(account, client, username, message, targets)
        return True

    async def _handle_math_verification(
        self,
        account: str,
        client: Any,
        username: str,
        messages: List[Any],
        param: str = "",
        attempt: int = 1,
    ) -> bool:
        """处理 bot 的「算术验证」（如 @mianfeichoujiang_bot）。

        start 后 bot 回复「请计算：18 + 9 = ？直接回复答案数字。」，需在私聊
        直接发送计算结果；题目随机变化，本地安全求值后回复。同一条验证消息
        进程内只回复一次，避免重发 start 后重复回复过期题目。
        bot 回复「验证过期」时重发一次 start 换取新题再答（attempt 限 2 次）。
        """
        verify_msgs = [m for m in messages if is_math_verify_message(m)]
        if not verify_msgs:
            return False
        # 取最新一条，避免候选中残留的上一次题目被当成本次验证
        message = max(verify_msgs, key=lambda m: getattr(m, "id", 0) or 0)
        msg_key = f"{account}|{username}|{getattr(message, 'id', 0)}"
        if msg_key in self._math_verify_handled:
            return True
        answer = math_verify_answer(message)
        if answer is None:
            self.log(f"[{account}] @{username} 算术验证：未能解析题目，跳过")
            return True
        self._math_verify_handled.add(msg_key)
        try:
            await client.send_message(username, answer)
        except Exception as exc:
            self.log(f"[{account}] @{username} 算术验证：回复 {answer} 失败: {exc}")
            return True
        self.log(f"[{account}] @{username} 算术验证：已回复答案 {answer}")
        await asyncio.sleep(2)
        fresh = await self._refresh_after_verify_reply(
            account, client, username, messages
        )
        latest = next((m for m in fresh if not getattr(m, "outgoing", False)), None)
        if attempt <= 1 and "验证过期" in (_message_text(latest) or ""):
            self.log(f"[{account}] @{username} 算术验证已过期，重发 start 换取新题")
            try:
                await self._start_bot(client, account, username, param)
            except Exception as exc:
                self.log(f"重新参与 @{username} 失败: {exc}")
                return True
            await asyncio.sleep(2)
            await self._refresh_after_verify_reply(account, client, username, messages)
            return await self._handle_math_verification(
                account, client, username, messages, param, attempt + 1
            )
        return True

    async def _refresh_after_verify_reply(
        self,
        account: str,
        client: Any,
        username: str,
        messages: List[Any],
    ) -> List[Any]:
        """验证回复后拉取 bot 最新消息并入候选（失败仅 debug 日志）。"""
        fresh: List[Any] = []
        try:
            async for recent in client.get_chat_history(username, limit=2):
                fresh.append(recent)
                messages.append(recent)
                await self._check_win_message(account, client, username, recent)
        except Exception:
            logger.debug("拉取验证回复后的 bot 消息失败", exc_info=True)
        return fresh

    def _collect_verify_targets(
        self, account: str, username: str, message: Any, order: List[str]
    ) -> Optional[List[Tuple[str, Any]]]:
        """按正文 emoji 顺序在键盘上匹配回调按钮；缺任一按钮返回 None（不乱点）。"""
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
                return None
            targets.append((emoji, match))
        return targets

    async def _click_verify_buttons(
        self,
        account: str,
        client: Any,
        username: str,
        message: Any,
        targets: List[Tuple[str, Any]],
    ) -> None:
        """按匹配好的顺序逐个点击验证按钮；单个失败即止，模拟人工点击节奏。"""
        for emoji, btn in targets:
            if self._stop is not None and self._stop.is_set():
                return
            try:
                await client.request_callback_answer(
                    chat_id=username,
                    message_id=getattr(message, "id", None),
                    callback_data=btn.callback_data,
                )
            except Exception as exc:
                self.log(f"[{account}] @{username} 点击验证按钮「{emoji}」失败: {exc}")
                return
            # 模拟人工点击节奏，避免过快触发风控
            await asyncio.sleep(random.uniform(0.8, 1.6))

    async def _click_join_retry_button(
        self,
        account: str,
        client: Any,
        username: str,
        candidates: List[Any],
    ) -> Tuple[bool, bool]:
        """优先点击「我已加入/重试参与」按钮完成重试参与。

        部分抽奖 bot（如 @dbchoujiang123_bot）在要求加入频道后提示
        「需要再点一次 我已加入 重试参与」：这是 bot 设计的重试入口，
        点击比重发 start 少触发一轮防脚本验证。取候选中最新一条带该
        按钮的消息点击（callback 按钮），点击后刷新候选并核对回复。

        返回（是否已点击，参与是否完成）。已点击时无需再重发 start；
        点击失败返回（False, True），由调用方兜底重发 start。
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
                        popup = callback_answer_text(answer)
                        if popup and _POPUP_ENDED_RE.search(popup):
                            self._note_ended(account, username)
                            self.log(
                                f"[{account}] @{username} 弹窗提示「{popup[:40]}」，"
                                "抽奖已结束，跳过后续跟进"
                            )
                            return True, True
                        self.log(
                            f"[{account}] 已点击 @{username} 的重试按钮「{text}」"
                            "（我已加入，重试参与）"
                        )
                        await asyncio.sleep(2)
                        await self._append_recent_bot_messages(
                            account, client, username, candidates
                        )
                        return True, await self._verify_after_click(
                            account, client, username, candidates
                        )
                    except Exception as exc:
                        self.log(
                            f"点击 @{username} 重试按钮失败，改用重发 start: {exc}"
                        )
                        return False, True
        return False, True

    async def _click_confirm(
        self,
        account: str,
        client: Any,
        username: str,
        start_msg: Any,
        param: str = "",
        source_message: Any = None,
    ) -> bool:
        """start 后跟进：自动加入 bot 消息中的频道（若开启），并点确认按钮。

        返回 False 表示 bot 要求加入频道但本次未生效（加入被暂停，或点击后
        bot 仍要求先加入），跟进未完成；其余情况返回 True（含已结束终态）。
        """
        ended, candidates = self._initial_confirm_candidates(
            account, username, start_msg, source_message
        )
        if ended:
            return True
        if self.confirm_wait > 0:
            if await self._poll_confirm_candidates(
                account, client, username, candidates
            ):
                return True
        # 「防脚本验证」（如 @dbchoujiang123_bot）：按正文顺序点击 emoji 按钮
        # 完成人机校验，再刷新候选，让后续加入/确认流程看到验证后的消息
        await self._refresh_candidates_after_verification(
            account, client, username, candidates, param
        )
        # 关闭自动加群时不会走 _auto_join_and_restart，这里必须先有默认值，
        # 否则后面读取会触发 UnboundLocalError（机器人类型任务即此路径）
        had_join_targets = False
        if self.auto_join:
            # 多轮循环：一次 start 后 bot 可能要求加入多个频道（如 @lycj_bot
            # 每轮 start 只露出一个前置频道），必须在单次 _click_confirm 内
            # 循环「扫目标→加入→重发 start」直到 bot 不再有新加入要求，否则
            # 要靠多轮任务扫描才能跑完所有前置频道，效率极低且易错过开奖。
            # _join_cache 保证同一 target 不会重复尝试加入；did_join 在本轮
            # 没真加入任何新频道时为 False（哪怕 had_join_targets 因旧消息
            # 残留仍为 True），配合硬上限防止 bot 无限循环或 FakeClient 类
            # 测试场景死循环。
            _MAX_AUTO_JOIN_ROUNDS = 5
            for _round in range(_MAX_AUTO_JOIN_ROUNDS):
                (
                    stop,
                    did_join,
                    had_join_targets,
                    candidates,
                    join_blocked,
                ) = await self._auto_join_and_restart(
                    account, client, username, param, source_message, candidates
                )
                if stop:
                    return True
                if join_blocked:
                    return False
                # 本轮没有真的加入任何新频道：要么 bot 不再出新需求，
                # 要么候选都是旧消息残留但已全被 join_cache 挡住，两种
                # 情况都应该退出循环，交给后续点确认按钮流程兜底。
                if not did_join:
                    break
            else:
                # 达到硬上限仍在加新频道（罕见）：记录熔断日志但不崩，
                # 继续尝试点按钮（可能已满足，也可能失败——由后续链路兜底）
                self.log(
                    f"[{account}] @{username} 连续 {_MAX_AUTO_JOIN_ROUNDS} 轮仍在"
                    f"加入新频道，强制结束跟进"
                )
        stop_set = self._stop is not None and self._stop.is_set()
        if had_join_targets and not stop_set:
            # 加入频道后稍等再点确认按钮，给 bot 侧成员状态同步留时间，
            # 避免「立刻参加抽奖」点了却因身份未同步而不生效
            await asyncio.sleep(5)
        return await self._click_first_confirm_button(
            account, client, username, candidates, param
        )

    def _initial_confirm_candidates(
        self,
        account: str,
        username: str,
        start_msg: Any,
        source_message: Any,
    ) -> Tuple[bool, List[Any]]:
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
                return True, candidates
        return False, candidates

    async def _poll_confirm_candidates(
        self, account: str, client: Any, username: str, candidates: List[Any]
    ) -> bool:
        """轮询 bot 最新消息直到出现确认按钮/验证；返回是否因已结束而中止。"""
        # 按 confirm_wait 换算固定轮询次数（而非读墙钟）：总等待时长不变，
        # 但不再依赖真实时间流逝，单测注入 no-op sleep 时行为完全确定
        poll_times = max(1, int(round(self.confirm_wait / 1.5)))
        for _ in range(poll_times):
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
                return True
            if any(self._find_confirm_button(m) is not None for m in candidates):
                break
            # 防脚本验证/算术验证消息没有确认按钮：提前结束轮询交给验证处理
            if any(is_emoji_verify_message(m) for m in candidates) or any(
                is_math_verify_message(m) for m in candidates
            ):
                break
        return False

    async def _refresh_candidates_after_verification(
        self,
        account: str,
        client: Any,
        username: str,
        candidates: List[Any],
        param: str = "",
    ) -> None:
        handled = await self._handle_math_verification(
            account, client, username, candidates, param
        )
        if not handled:
            handled = await self._handle_emoji_verification(
                account, client, username, candidates
            )
        if handled:
            await asyncio.sleep(2)
            await self._append_recent_bot_messages(
                account, client, username, candidates
            )

    async def _auto_join_and_restart(
        self,
        account: str,
        client: Any,
        username: str,
        param: str,
        source_message: Any,
        candidates: List[Any],
    ) -> Tuple[bool, bool, bool, List[Any], bool]:
        """自动加入候选消息中的目标，必要时重发 start。

        返回（是否应直接结束跟进，本轮是否实际加入了新频道，
        当前是否仍存在加入目标，更新后的候选，
        加入是否被限流/配额阻塞导致跟进未完成）。
        did_join 与 had_join_targets 分离：前者仅在本轮真的执行了
        新加入时为 True（join_cache 挡住的不计），后者反映 candidates
        中是否还存在未处理的加入要求（可能只是旧消息残留）。
        """
        did_join, had_join_targets, blocked = await self._auto_join_candidates(
            account, client, username, candidates
        )
        if had_join_targets and blocked:
            # 加入被暂停时重发 start/点按钮都不会生效（bot 仍判定未加入），
            # 还会白白消耗一次 start 并触发重复按钮跳过；直接按未完成返回，
            # 由上层归还名额且不记「已参与」，等恢复后重试
            paused = self._join_flood_paused(account)
            self.log(
                f"[{account}] @{username} 需要加入频道才能参与，但"
                + (
                    "本账号自动加入已暂停（限流），本次跳过，稍后重试"
                    if paused
                    else "自动加入未完成（网络异常或已达加入配额），本次跳过，稍后重试"
                )
            )
            return False, did_join, had_join_targets, candidates, True
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
        # 加入过，不必本次有新加入）。注意：只有本轮真加入了新频道
        # （did_join=True）才重发 start——bot 判定身份需要新的 start
        # 触发；旧消息残留被 join_cache 挡住（did_join=False）不算。
        if had_join_targets and did_join:
            retried, retry_completed = await self._click_join_retry_button(
                account, client, username, candidates
            )
            if retried:
                return True, did_join, had_join_targets, candidates, not retry_completed
            self.log(f"[{account}] @{username} 要求加入频道后重新参与，再次发送 start")
            stop, had_join_targets, candidates = await self._restart_after_join(
                account, client, username, param, candidates, had_join_targets
            )
            return stop, did_join, had_join_targets, candidates, False
        if bot_policy(username).restart_after_join and did_join:
            # 强制名单：本轮真加入了频道时（did_join=True），
            # 额外重发一次 start 让 bot 校验参与资格（即使 bot 消息里没有
            # 显式加入要求）。LotteryBestBot 之类有清单的 bot 会因上面
            # had_join_targets 分支已触发过 start，这里天然跳过；
            # realsaberbot 这类完全不发加入要求的 bot，did_join=False 也
            # 跳过（它通常也没什么好加的）。
            self.log(f"[{account}] @{username} 加入校验完成，重新发送 start")
            stop, had_join_targets, candidates = await self._restart_after_join(
                account, client, username, param, candidates, had_join_targets
            )
            return stop, did_join, had_join_targets, candidates, False
        return False, did_join, had_join_targets, candidates, False

    async def _auto_join_candidates(
        self, account: str, client: Any, username: str, candidates: List[Any]
    ) -> Tuple[bool, bool, bool]:
        did_join = False
        had_join_targets = False
        blocked = False
        for message in candidates:
            # 排除指向当前 bot 自身的 t.me 链接（按钮「点击参与抽奖」等）：
            # 那是参与入口不是加入目标，交给下方 URL 按钮点击逻辑处理
            join_targets = [
                t
                for t in extract_channel_targets(message)
                if str(t).lower().lstrip("@") != username.lower()
            ]
            if join_targets or (
                bot_policy(username).join_status_checklist
                and extract_join_status_targets(message)
            ):
                had_join_targets = True
            joined, message_blocked = await self._join_from_message(
                account, client, username, message
            )
            if joined:
                did_join = True
            if message_blocked:
                blocked = True
        return did_join, had_join_targets, blocked

    async def _restart_after_join(
        self,
        account: str,
        client: Any,
        username: str,
        param: str,
        candidates: List[Any],
        had_join_targets: bool,
    ) -> Tuple[bool, bool, List[Any]]:
        stop, candidates = await self._restart_start_and_refresh(
            account, client, username, param, candidates
        )
        return stop, had_join_targets, candidates

    async def _restart_start_and_refresh(
        self,
        account: str,
        client: Any,
        username: str,
        param: str,
        candidates: List[Any],
    ) -> Tuple[bool, List[Any]]:
        """重发 start 并刷新候选；返回（是否中止后续跟进，更新后的候选）。

        bot 终态回复或触发 FLOOD_WAIT 时返回 True 中止，避免继续打同一个 bot。
        """
        try:
            restart_msg = await self._start_bot(client, account, username, param)
            if restart_msg is not None and reply_says_ended(restart_msg):
                # 加入期间活动提前结束，bot 终态回复，不再点按钮
                self._note_ended(account, username)
                self.log(f"[{account}] @{username} 回复抽奖已结束，跳过后续跟进")
                return True, candidates
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
            # 重发 start 可能再次触发防脚本/算术验证：完成后刷新候选
            handled = await self._handle_math_verification(
                account, client, username, candidates, param
            )
            if not handled:
                handled = await self._handle_emoji_verification(
                    account, client, username, candidates
                )
            if handled:
                await asyncio.sleep(2)
                await self._append_recent_bot_messages(
                    account, client, username, candidates
                )
        except Exception as exc:
            # 限流也走统一登记（计数 + bot 级暂停 + 冷却落盘），并中止后续跟进
            if self._record_flood(account, username, param, "", exc, label="重新参与"):
                return True, candidates
            self.log(f"重新参与 @{username} 失败: {exc}")
        return False, candidates

    async def _click_first_confirm_button(
        self,
        account: str,
        client: Any,
        username: str,
        candidates: List[Any],
        param: str = "",
    ) -> bool:
        """找到并处理第一个确认按钮；返回参与是否真正完成。"""
        if self._flood_paused(account, username):
            return True  # 该 bot 已被限流，暂停期内不再发起任何 start/点击
        for message in candidates:
            btn = self._find_confirm_button(message)
            if btn is None:
                continue
            stop, completed = await self._try_confirm_button(
                account, client, username, message, btn, candidates, param
            )
            if stop:
                return completed
        return True

    async def _try_confirm_button(
        self,
        account: str,
        client: Any,
        username: str,
        message: Any,
        btn: Any,
        candidates: List[Any],
        param: str = "",
    ) -> Tuple[bool, bool]:
        """处理单个确认按钮；返回（是否结束整个跟进流程，参与是否完成）。"""
        try:
            if getattr(btn, "callback_data", None):
                answer = await client.request_callback_answer(
                    chat_id=username,
                    message_id=message.id,
                    callback_data=btn.callback_data,
                )
                # 抽奖结束后部分 bot 只弹窗提示、不发新消息：读弹窗文案，
                # 命中终态即返回，不进入回复核对（避免误读旧消息）
                popup = callback_answer_text(answer)
                if popup and _POPUP_ENDED_RE.search(popup):
                    self._note_ended(account, username)
                    self.log(
                        f"[{account}] @{username} 弹窗提示「{popup[:40]}」，"
                        "抽奖已结束，跳过后续跟进"
                    )
                    return True, True
                self.log(self._format_confirm_button_log(username, btn.text))
                completed = await self._verify_after_click(
                    account, client, username, candidates
                )
                return True, completed
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
                # 参数与本次已发的 start 相同也照样再发一次：百宝箱这类 bot
                # 要求「加入频道后重新参与」，加入生效后正是这次 start 才真正
                # 报名成功，跳过会导致显示已参与、实际没进抽奖
                await self._start_bot(client, account, username, link[1])
                self.log(
                    f"已点击 @{username} 的参与按钮「{btn.text}」（按链接参数重新参与）"
                )
                completed = await self._verify_after_click(
                    account, client, username, candidates
                )
                return True, completed
            if plain_same_bot:
                await self._start_bot(client, account, username, "")
                self.log(f"已点击 @{username} 的参与按钮「{btn.text}」（重发 start）")
                completed = await self._verify_after_click(
                    account, client, username, candidates
                )
                return True, completed
        except Exception as exc:
            # 限流同样登记（计数 + bot 级暂停 + 冷却落盘），避免异常被吞后
            # 继续对同一个 bot 发请求把短限流拖成小时级。
            # 限流返回 (True, True)：completed=True 让上层跳过 join_blocked
            # 分支（bot 级熔断已登记，重试需等冷却，不该再无意义地记成功）。
            if self._record_flood(
                account, username, param, "", exc, label="点击确认按钮"
            ):
                return True, True
            # 非限流异常（超时/网络抖动/RPC 偶发故障）：按钮点击实际没成功，
            # 必须返回 completed=False，让上层走 _handle_join_blocked 归还
            # 名额并保留重试可能。绝不能记「已参与」持久化记录，否则下一轮
            # 扫描会把它当真成功跳过，参与请求彻底丢失。
            self.log(f"点击 @{username} 确认按钮失败: {exc}")
            return True, False
        return False, True

    @staticmethod
    def _format_confirm_button_log(username: str, text: str) -> str:
        """按钮文字含「已参加」= bot 已确认参与成功，日志按成功口径输出。"""
        if "已参加" in text:
            return f"@{username} 参与成功（{text}）"
        return f"已点击 @{username} 的确认按钮「{text}」"

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
    ) -> bool:
        """点击确认/参与按钮后核对 bot 回复，把结果写进日志（不再盲点）。

        返回 False 表示 bot 仍要求先加入频道（本次点击未生效）；其余情况返回
        True（成功、终态或无法判定，沿用原来的完成口径）。
        """
        try:
            await asyncio.sleep(3)
            if self._stop is not None and self._stop.is_set():
                return True
            async for msg in client.get_chat_history(username, limit=1):
                await self._check_win_message(account, client, username, msg)
                text = (_message_text(msg) or "").strip()
                if not text:
                    return True
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
                    return True
                if JOIN_REQUIRED_POPUP_RE.search(text):
                    # bot 仍在要求加入频道：这次 start/点击没有真正报名成功，
                    # 不能记「已参与」，留给下一轮扫描重试
                    self.log(f"@{username} 仍要求加入频道，本次参与未生效：{flat}")
                    return False
                if not reply_says_ended(msg):
                    self.log(f"@{username} 点击后回复：{flat}")
                return True
        except Exception:
            logger.debug("核对确认按钮回复失败", exc_info=True)
        return True

    def _find_confirm_button(self, message: Any) -> Optional[Any]:
        # 「花费 N 积分」确认提示（如积分兑换）：点「确认」会消耗积分，
        # 不属于参与动作，返回 None 跳过（覆盖确认按钮的全部调用点）
        if SCORE_SPEND_RE.search(_message_text(message) or ""):
            return None
        reply_markup = getattr(message, "reply_markup", None)
        for row in getattr(reply_markup, "inline_keyboard", None) or []:
            for btn in row or []:
                text = str(getattr(btn, "text", "") or "")
                if not (
                    getattr(btn, "callback_data", None) or getattr(btn, "url", None)
                ):
                    continue
                # 「参与（100 分）」类扣分按钮：不自动点击，避免消耗账号积分
                if SCORE_BUTTON_RE.search(text):
                    continue
                if any(kw in text for kw in self.confirm_keywords):
                    return btn
        return None
