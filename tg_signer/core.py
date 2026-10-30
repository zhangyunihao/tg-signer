import asyncio
import json
import logging
import os
import pathlib
import random
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import (
    Annotated,
    Any,
    AsyncIterator,
    Awaitable,
    BinaryIO,
    Callable,
    Generic,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
)
from urllib import parse

from croniter import CroniterBadCronError, croniter
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pyrogram import Client as BaseClient
from pyrogram import errors, filters
from pyrogram.enums import ChatType
from pyrogram.handlers import EditedMessageHandler, MessageHandler
from pyrogram.session import Session
from pyrogram.storage import SQLiteStorage
from pyrogram.types import (
    Chat,
    Folder,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Object,
    User,
)

from tg_signer.config import (
    ActionT,
    BaseJSONConfig,
    ChatId,
    ChooseOptionByImageAction,
    ClickKeyboardByTextAction,
    ReplyByCalculationProblemAction,
    SendDiceAction,
    SendTextAction,
    SignChatV3,
    SignConfigV3,
    SupportAction,
    parse_chat_id_or_username,
)

from ._kurigram import SafeGetForumTopics
from .ai_tools import AITools, OpenAIConfigManager
from .utils import (
    UserInput,
    atomic_write_json,
    atomic_write_text,
    get_now,
    print_to_user,
)
from .utils import get_timezone as _get_timezone

logger = logging.getLogger("tg-signer")

DICE_EMOJIS = ("🎲", "🎯", "🏀", "⚽", "🎳", "🎰")

Session.START_TIMEOUT = 5  # 原始超时时间为2秒，但一些代理访问会超时，所以这里调大一点

OPENAI_USE_PROMPT = "当前任务需要配置大模型，请确保运行前正确设置`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`等环境变量，或在`<workdir>/.openai_config.json`中持久化配置。"

CHAT_TYPE_LABELS = {
    ChatType.BOT: "BOT",
    ChatType.GROUP: "群组",
    ChatType.SUPERGROUP: "超级群组",
    ChatType.CHANNEL: "频道",
    ChatType.FORUM: "论坛群组",
    ChatType.DIRECT: "频道私信",
}


class ChatFolderError(RuntimeError):
    """Raised when an explicit Telegram chat folder cannot be used."""


def _get_message_text(message: Message) -> str:
    return getattr(message, "text", None) or getattr(message, "caption", None) or ""


def _normalize_option_text(text: str) -> str:
    return "".join(text.split())


def _get_inline_keyboard_buttons(message: Message) -> list[InlineKeyboardButton]:
    if not isinstance(getattr(message, "reply_markup", None), InlineKeyboardMarkup):
        return []
    return [
        button
        for row in message.reply_markup.inline_keyboard
        for button in row
        if button.text
    ]


def readable_chat(chat: Chat):
    type_ = CHAT_TYPE_LABELS.get(chat.type, "个人")

    none_or_dash = lambda x: x or "-"  # noqa: E731

    return f"id: {chat.id}, username: {none_or_dash(chat.username)}, title: {none_or_dash(chat.title)}, type: {type_}, name: {none_or_dash(chat.first_name)}"


def _folder_dynamic_rules(folder: Folder) -> list[str]:
    return [
        label
        for enabled, label in (
            (folder.include_contacts, "联系人"),
            (folder.include_non_contacts, "非联系人"),
            (folder.include_bots, "机器人"),
            (folder.include_groups, "群组"),
            (folder.include_channels, "频道"),
        )
        if enabled
    ]


def _explicit_folder_chats(folder: Folder) -> list[Chat]:
    dynamic_rules = _folder_dynamic_rules(folder)
    if dynamic_rules:
        rules = "、".join(dynamic_rules)
        raise ChatFolderError(
            f"Folder「{folder.name}」包含动态规则（{rules}），当前仅支持手动添加对话的普通 Folder。"
        )

    seen_chat_ids = set()
    chats = []
    for chat in [
        *(folder.pinned_chats or []),
        *(folder.included_chats or []),
    ]:
        if chat is None or chat.id in seen_chat_ids:
            continue
        seen_chat_ids.add(chat.id)
        chats.append(chat)
    return chats


def _select_chat_folder(folders: list[Folder], selector: str) -> Folder:
    selector = selector.strip()
    if selector.isdecimal():
        folder_id = int(selector)
        for folder in folders:
            if folder.id == folder_id:
                return folder

    matches = [folder for folder in folders if folder.name == selector]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = "、".join(str(folder.id) for folder in matches)
        raise ChatFolderError(
            f"存在多个名为「{selector}」的 Folder，请改用 ID（{ids}）。"
        )

    available = "、".join(f"{folder.id}:{folder.name}" for folder in folders)
    if not available:
        available = "无"
    raise ChatFolderError(
        f"未找到 Folder「{selector}」。可用 Folder（ID:名称）：{available}"
    )


def chat_has_forum_topics(chat: Chat) -> bool:
    return chat.type == ChatType.FORUM or (
        chat.type == ChatType.SUPERGROUP and chat.is_forum
    )


def readable_topic(topic) -> str:
    none_or_dash = lambda x: x or "-"  # noqa: E731
    return (
        f"message_thread_id: {topic.id}, title: {none_or_dash(topic.title)}, "
        f"closed: {bool(getattr(topic, 'is_closed', False))}, "
        f"pinned: {bool(getattr(topic, 'is_pinned', False))}"
    )


_CLIENT_INSTANCES: dict[str, "Client"] = {}

# reference counts and async locks for shared client lifecycle management
# Keyed by account name. Use asyncio locks to serialize start/stop operations
# so multiple coroutines in the same process can safely share one Client.
_CLIENT_REFS: defaultdict[str, int] = defaultdict(int)
_CLIENT_ASYNC_LOCKS: dict[str, asyncio.Lock] = {}

# login bootstrap state keyed by account key. This prevents concurrent tasks
# from repeatedly calling get_me/get_dialogs for the same account.
_LOGIN_ASYNC_LOCKS: dict[str, asyncio.Lock] = {}
_LOGIN_USERS: dict[str, User] = {}

_API_ASYNC_LOCKS: dict[str, asyncio.Lock] = {}
_API_LAST_CALL_AT: dict[str, float] = {}
_API_MIN_INTERVAL_SECONDS = 0.35
_API_FLOODWAIT_PADDING_SECONDS = 0.5
_API_MAX_FLOODWAIT_RETRIES = 2
# 手机代理不稳定场景：单次 API 调用超过该时长按网络异常处理，限时重试
_API_CALL_TIMEOUT_SECONDS = 90.0
_API_MAX_NET_RETRIES = 2
_API_NET_RETRY_DELAY_SECONDS = 3.0
# 客户端启动（connect+authorize）重试参数
_CONNECT_ATTEMPTS = 3
_CONNECT_ATTEMPT_TIMEOUT_SECONDS = 60.0
_CONNECT_RETRY_DELAY_SECONDS = 5.0
# 网络类异常（代理瞬断/半开连接）；asyncio.TimeoutError 与内置 TimeoutError 在 3.10/3.11 归属不同，都捕
_NET_ERRORS = (asyncio.TimeoutError, TimeoutError, OSError)

RouteKey = tuple[ChatId, Optional[int]]
get_timezone = _get_timezone

# session 数据库等待锁的最长时间（秒）：并发任务短暂冲突时排队等待而不是直接报错
_SESSION_DB_TIMEOUT_SECONDS = 30.0


class ResilientSQLiteStorage(SQLiteStorage):
    """session 数据库：WAL + 长等待，避免并发任务互相报 "database is locked"。

    Pyrogram 默认 ``timeout=1`` 秒、DELETE 日志模式，而且每次打开数据库都会
    执行一次 ``VACUUM``（需要独占锁）。只要另一个任务或另一个 WebUI 实例
    正持有同一个 session，启动就会立刻抛 "database is locked"。
    这里改为 WAL（读写不互斥）+ busy_timeout，并跳过每次打开的 VACUUM。
    """

    async def open(self) -> None:
        if self.in_memory or self.session_string:
            return await super().open()
        path = self.database
        file_exists = isinstance(path, pathlib.Path) and path.is_file()
        self.conn = sqlite3.connect(
            str(path), timeout=_SESSION_DB_TIMEOUT_SECONDS, check_same_thread=False
        )
        self.conn.execute(
            f"PRAGMA busy_timeout={int(_SESSION_DB_TIMEOUT_SECONDS * 1000)}"
        )
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # 只读介质等场景无法切换：保持原有日志模式即可
            logger.debug("session 数据库切换到 WAL 失败", exc_info=True)
        if file_exists:
            await self.update()
        else:
            await self.create()


class Client(SafeGetForumTopics, BaseClient):
    def __init__(self, name: str, *args, **kwargs):
        key = kwargs.pop("key", None)
        # session 数据库改用 WAL + 长等待（见 ResilientSQLiteStorage）；
        # 内存 session / session_string 场景仍走 Pyrogram 默认实现
        if (
            not kwargs.get("in_memory")
            and not kwargs.get("session_string")
            and not kwargs.get("storage_engine")
            and "workdir" in kwargs
        ):
            kwargs["storage_engine"] = ResilientSQLiteStorage(
                name=name, workdir=pathlib.Path(kwargs["workdir"])
            )
        super().__init__(name, *args, **kwargs)
        self.key = key or str(pathlib.Path(self.workdir).joinpath(self.name).resolve())
        if self.in_memory and not self.session_string:
            self.load_session_string()
            self.storage = SQLiteStorage(
                name=self.name,
                workdir=self.workdir,
                session_string=self.session_string,
                in_memory=True,
            )

    async def _start_with_retry(self) -> None:
        """启动客户端；网络类失败自动重试（手机代理不稳定场景）。"""
        last_exc: Optional[BaseException] = None
        for attempt in range(1, _CONNECT_ATTEMPTS + 1):
            try:
                await asyncio.wait_for(
                    self.start(), timeout=_CONNECT_ATTEMPT_TIMEOUT_SECONDS
                )
                return
            except _NET_ERRORS as exc:
                last_exc = exc
                logger.debug(
                    f"启动客户端连接失败（第 {attempt}/{_CONNECT_ATTEMPTS} 次）",
                    exc_info=True,
                )
                if attempt < _CONNECT_ATTEMPTS:
                    await asyncio.sleep(_CONNECT_RETRY_DELAY_SECONDS)
                # 超时被取消的 start 可能残留半开连接，先兜底断开再重试
                try:
                    await asyncio.wait_for(self.disconnect(), timeout=5)
                except Exception:
                    logger.debug("重试前兜底断开客户端失败", exc_info=True)
        raise ConnectionError(
            f"连接 Telegram 失败（已重试 {_CONNECT_ATTEMPTS} 次，请检查代理/网络）：{last_exc}"
        ) from last_exc

    async def __aenter__(self):
        lock = _CLIENT_ASYNC_LOCKS.get(self.key)
        if lock is None:
            lock = asyncio.Lock()
            _CLIENT_ASYNC_LOCKS[self.key] = lock
        async with lock:
            _CLIENT_REFS[self.key] += 1
            if _CLIENT_REFS[self.key] == 1:
                try:
                    await self._start_with_retry()
                except BaseException:
                    # async with 失败不会走 __aexit__，回滚引用计数防泄漏
                    _CLIENT_REFS[self.key] -= 1
                    raise
            return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        lock = _CLIENT_ASYNC_LOCKS.get(self.key)
        if lock is None:
            return
        async with lock:
            _CLIENT_REFS[self.key] -= 1
            if _CLIENT_REFS[self.key] == 0:
                try:
                    await self.stop()
                except ConnectionError:
                    logger.debug("断开客户端连接失败", exc_info=True)
                _CLIENT_INSTANCES.pop(self.key, None)

    @property
    def session_string_file(self):
        return self.workdir / (self.name + ".session_string")

    async def save_session_string(self):
        # 原子写：会话串写到一半崩溃会得到不可用的残片
        atomic_write_text(self.session_string_file, await self.export_session_string())

    def load_session_string(self):
        logger.info("Loading session_string from local file.")
        if self.session_string_file.is_file():
            with open(self.session_string_file, "r") as fp:
                self.session_string = fp.read()
                logger.info("The session_string has been loaded.")
        return self.session_string

    async def log_out(self):
        await super().log_out()
        if self.session_string_file.is_file():
            os.remove(self.session_string_file)


def get_api_config():
    api_id = int(os.environ.get("TG_API_ID", 611335))
    api_hash = os.environ.get("TG_API_HASH", "d524b414d21f4d37f08684c1df41ac9c")
    return api_id, api_hash


def get_proxy(proxy: str = None):
    proxy = proxy or os.environ.get("TG_PROXY")
    if proxy:
        r = parse.urlparse(proxy)
        return {
            "scheme": r.scheme,
            "hostname": r.hostname,
            "port": r.port,
            "username": r.username,
            "password": r.password,
        }
    return None


def get_client(
    name: str = "my_account",
    proxy: dict = None,
    workdir: Union[str, pathlib.Path] = ".",
    session_string: str = None,
    in_memory: bool = False,
    reuse: bool = True,
    **kwargs,
) -> Client:
    """获取（或创建）Pyrogram 客户端。

    reuse=True 时复用 _CLIENT_INSTANCES 里的同名客户端；后台探测类调用
    （授权检测等短暂连接即断开）必须传 reuse=False 使用私有客户端，
    避免与任务流程共用同一个还在连接中的客户端产生并发 connect 竞态
    （sqlite 报 "Cannot operate on a closed database"）。
    """
    proxy = proxy or get_proxy()
    api_id, api_hash = get_api_config()
    key = str(pathlib.Path(workdir).joinpath(name).resolve())
    if reuse and key in _CLIENT_INSTANCES:
        return _CLIENT_INSTANCES[key]
    client = Client(
        name,
        api_id=api_id,
        api_hash=api_hash,
        proxy=proxy,
        workdir=workdir,
        session_string=session_string,
        in_memory=in_memory,
        key=key,
        **kwargs,
    )
    if reuse:
        _CLIENT_INSTANCES[key] = client
    return client


async def safe_disconnect(client: Optional[Client], timeout: float = 15.0) -> None:
    """兜底断连：优雅 disconnect 失败/超时后 terminate 重试，最后强制关闭 sqlite 存储。

    用于覆盖所有可能持有 Pyrogram 连接的场景，避免半连接残留导致
    session 文件锁（Windows WinError 32）或后续操作抛
    "Cannot operate on a closed database" / "Client has not been started yet"。

    断开完成后会把客户端从 _CLIENT_INSTANCES 缓存移除：pyrogram 的
    disconnect/terminate 会永久关闭 sqlite 存储（connect 不会重新打开），
    缓存里的断开客户端再次 connect 必报 "Cannot operate on a closed
    database"，因此断开后的客户端一律不可复用，下次 get_client 重建。
    """
    if client is None:
        return
    closed = False
    for stage in ("graceful", "terminate"):
        try:
            if stage == "terminate":
                await asyncio.wait_for(client.terminate(), timeout)
            await asyncio.wait_for(client.disconnect(), timeout)
            closed = True
            break
        except Exception:
            logger.debug("safe_disconnect %s 阶段失败", stage, exc_info=True)
    if not closed:
        storage = getattr(client, "storage", None)
        if getattr(storage, "conn", None) is not None:
            try:
                await storage.close()
            except Exception:
                logger.debug("强制关闭 session 存储失败", exc_info=True)
    for key, inst in list(_CLIENT_INSTANCES.items()):
        if inst is client:
            _CLIENT_INSTANCES.pop(key, None)


async def timeout_aiter(
    agen: AsyncIterator[Any], timeout: float, name: str = ""
) -> AsyncIterator[Any]:
    """给异步迭代器逐项套超时（手机代理不稳定场景）。

    任一单项拉取超过 timeout 秒抛 RuntimeError（带说明），由调用点
    既有异常处理兜底；正常结束时静默返回。
    """
    while True:
        try:
            item = await asyncio.wait_for(agen.__anext__(), timeout)
        except StopAsyncIteration:
            return
        except _NET_ERRORS:
            raise RuntimeError(
                f"网络请求 {name or '数据拉取'} 超时（{timeout:.0f} 秒，网络异常）"
            )
        yield item


def make_dirs(path: pathlib.Path, exist_ok=True):
    path = pathlib.Path(path)
    if not path.is_dir():
        os.makedirs(path, exist_ok=exist_ok)
    return path


ConfigT = TypeVar("ConfigT", bound=BaseJSONConfig)
ApiCallResultT = TypeVar("ApiCallResultT")


class BaseUserWorker(Generic[ConfigT]):
    _workdir = "."
    _tasks_dir = "tasks"
    cfg_cls: Type["ConfigT"] = BaseJSONConfig

    def __init__(
        self,
        task_name: str = None,
        session_dir: str = ".",
        account: str = "my_account",
        proxy=None,
        workdir=None,
        session_string: str = None,
        in_memory: bool = False,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self.task_name = task_name or "my_task"
        self._session_dir = pathlib.Path(session_dir)
        self._account = account
        if workdir:
            self._workdir = pathlib.Path(workdir)
        self.app = get_client(
            account,
            proxy,
            workdir=self._session_dir,
            session_string=session_string,
            in_memory=in_memory,
            loop=loop,
        )
        self.loop = self.app.loop
        self.user: Optional[User] = None
        self._config = None
        self.context = self.ensure_ctx()

    def ensure_ctx(self):
        return {}

    def app_run(self, coroutine=None):
        if coroutine is not None:
            run = self.loop.run_until_complete
            run(coroutine)
        else:
            self.app.run()

    @property
    def workdir(self) -> pathlib.Path:
        workdir = self._workdir
        make_dirs(workdir)
        return pathlib.Path(workdir)

    @property
    def tasks_dir(self):
        tasks_dir = self.workdir / self._tasks_dir
        make_dirs(tasks_dir)
        return pathlib.Path(tasks_dir)

    @property
    def task_dir(self):
        task_dir = self.tasks_dir / self.task_name
        make_dirs(task_dir)
        return task_dir

    def get_user_dir(self, user: User):
        user_dir = self.workdir / "users" / str(user.id)
        make_dirs(user_dir)
        return user_dir

    @property
    def config_file(self):
        return self.task_dir.joinpath("config.json")

    @property
    def config(self) -> ConfigT:
        return self._config or self.load_config()

    @config.setter
    def config(self, value):
        self._config = value

    def log(self, msg, level: str = "INFO", **kwargs):
        msg = f"账户「{self._account}」- 任务「{self.task_name}」: {msg}"
        if level.upper() == "INFO":
            logger.info(msg, **kwargs)
        elif level.upper() == "WARNING":
            logger.warning(msg, **kwargs)
        elif level.upper() == "ERROR":
            logger.error(msg, **kwargs)
        elif level.upper() == "CRITICAL":
            logger.critical(msg, **kwargs)
        else:
            logger.debug(msg, **kwargs)

    async def _call_telegram_api(
        self,
        operation: str,
        call: Callable[[], Awaitable[ApiCallResultT]],
        *,
        retry_on_floodwait: bool = True,
    ) -> ApiCallResultT:
        key = self.app.key
        lock = _API_ASYNC_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _API_ASYNC_LOCKS[key] = lock

        retries_left = _API_MAX_FLOODWAIT_RETRIES
        net_retries_left = _API_MAX_NET_RETRIES
        while True:
            async with lock:
                loop = asyncio.get_running_loop()
                last_called_at = _API_LAST_CALL_AT.get(key)
                if last_called_at is not None:
                    wait_for = _API_MIN_INTERVAL_SECONDS - (
                        loop.time() - last_called_at
                    )
                    if wait_for > 0:
                        await asyncio.sleep(wait_for)
                try:
                    result = await asyncio.wait_for(
                        call(), timeout=_API_CALL_TIMEOUT_SECONDS
                    )
                    _API_LAST_CALL_AT[key] = loop.time()
                    return result
                except errors.FloodWait as e:
                    _API_LAST_CALL_AT[key] = loop.time()
                    if not retry_on_floodwait or retries_left <= 0:
                        raise
                    retries_left -= 1
                    wait_seconds = (
                        max(float(getattr(e, "value", 0) or 0), 0)
                        + _API_FLOODWAIT_PADDING_SECONDS
                    )
                    self.log(
                        f"{operation} 触发 FloodWait，等待 {wait_seconds:.1f}s 后重试（剩余重试 {retries_left} 次）",
                        level="WARNING",
                    )
                    await asyncio.sleep(wait_seconds)
                except _NET_ERRORS as e:
                    # 代理不稳/半开连接等网络异常：限时重试，耗尽后抛 RuntimeError
                    # 由各调用点既有异常处理兜底；超时被取消的请求可能已到达服务端，
                    # 重试极小概率重复发送，与手动重跑任务的语义一致
                    if net_retries_left <= 0:
                        raise RuntimeError(
                            f"{operation} 网络异常（重试 {_API_MAX_NET_RETRIES} 次后放弃，请检查代理/网络）：{e}"
                        ) from e
                    net_retries_left -= 1
                    self.log(
                        f"{operation} 网络异常（可能代理不稳），"
                        f"{_API_NET_RETRY_DELAY_SECONDS:.0f}s 后重试（剩余 {net_retries_left} 次）",
                        level="WARNING",
                    )
                    await asyncio.sleep(_API_NET_RETRY_DELAY_SECONDS)

    def ask_for_config(self):
        raise NotImplementedError

    def write_config(self, config: BaseJSONConfig):
        atomic_write_json(self.config_file, config.to_jsonable(), indent=None)

    def reconfig(self):
        config = self.ask_for_config()
        self.write_config(config)
        return config

    def load_config(self, cfg_cls: Type[ConfigT] = None) -> ConfigT:
        cfg_cls = cfg_cls or self.cfg_cls
        if not self.config_file.exists():
            config = self.reconfig()
        else:
            with open(self.config_file, "r", encoding="utf-8") as fp:
                config, from_old = cfg_cls.load(json.load(fp))
                if from_old:
                    self.write_config(config)
        self.config = config
        return config

    def get_task_list(self):
        signs = []
        for d in os.listdir(self.tasks_dir):
            if self.tasks_dir.joinpath(d).is_dir():
                signs.append(d)
        return signs

    def set_me(self, user: User):
        self.user = user
        # 原子写：WebUI 轮询会并发读取 me.json，半截内容会解析失败
        atomic_write_text(self.get_user_dir(user).joinpath("me.json"), str(user))

    async def login(
        self,
        num_of_dialogs=20,
        print_chat=True,
        folder: Optional[str] = None,
    ):
        self.log("开始登录...")
        app = self.app
        key = app.key
        lock = _LOGIN_ASYNC_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOGIN_ASYNC_LOCKS[key] = lock

        async with lock:
            me = _LOGIN_USERS.get(key)
            if me is None:
                async with app:
                    me = await self._call_telegram_api("users.GetFullUser", app.get_me)

                    async def load_latest_chats():
                        selected_folder = None
                        if folder is None:
                            chats = []
                            async for dialog in app.get_dialogs(limit=num_of_dialogs):
                                chats.append(dialog.chat)
                        else:
                            folders = await app.get_folders()
                            selected_folder = _select_chat_folder(folders, folder)
                            chats = _explicit_folder_chats(selected_folder)

                        latest_chats = [
                            {
                                "id": chat.id,
                                "title": chat.title,
                                "type": chat.type,
                                "username": chat.username,
                                "first_name": chat.first_name,
                                "last_name": chat.last_name,
                            }
                            for chat in chats
                        ]
                        return chats, latest_chats, selected_folder

                    (
                        chats,
                        latest_chats,
                        selected_folder,
                    ) = await self._call_telegram_api(
                        "messages.GetDialogFilters"
                        if folder is not None
                        else "messages.GetDialogs",
                        load_latest_chats,
                    )

                    if print_chat:
                        if selected_folder is not None:
                            print_to_user(
                                f"Folder: id: {selected_folder.id}, "
                                f"name: {selected_folder.name}"
                            )
                        for chat in chats:
                            print_to_user(readable_chat(chat))
                            if chat_has_forum_topics(chat):
                                try:
                                    topics = await asyncio.wait_for(
                                        self.get_forum_topics(chat.id, limit=20),
                                        timeout=5,
                                    )
                                    for topic in topics:
                                        print_to_user(f"  {readable_topic(topic)}")
                                except (asyncio.TimeoutError, errors.RPCError):
                                    # Keep login robust: many chats don't support
                                    # forum topics or the current account may not
                                    # have permissions to read them.
                                    pass

                    atomic_write_json(
                        self.get_user_dir(me).joinpath("latest_chats.json"),
                        latest_chats,
                        indent=4,
                        default=Object.default,
                    )
                    await self._call_telegram_api(
                        "auth.ExportAuthorization", self.app.save_session_string
                    )
                _LOGIN_USERS[key] = me
            else:
                self.log("检测到同账号已完成登录初始化，复用已有会话信息")
            self.set_me(me)

    async def send_message(
        self,
        chat_id: Union[int, str],
        text: str,
        delete_after: int = None,
        message_thread_id: Optional[int] = None,
        **kwargs,
    ):
        """
        发送文本消息
        :param chat_id:
        :param text:
        :param delete_after: 秒, 发送消息后进行删除，``None`` 表示不删除, ``0`` 表示立即删除.
        :param message_thread_id: 群组内话题ID
        :param kwargs:
        :return:
        """
        send_kwargs = dict(kwargs)
        if message_thread_id is not None:
            send_kwargs["message_thread_id"] = message_thread_id
        message = await self._call_telegram_api(
            "messages.SendMessage",
            lambda: self.app.send_message(chat_id, text, **send_kwargs),
        )
        if delete_after is not None:
            self.log(
                f"Message「{text}」 to {chat_id} will be deleted after {delete_after} seconds."
            )
            self.log("Waiting...")
            await asyncio.sleep(delete_after)
            await self._call_telegram_api("messages.DeleteMessages", message.delete)
            self.log(f"Message「{text}」 to {chat_id} deleted!")
        return message

    async def send_dice(
        self,
        chat_id: Union[int, str],
        emoji: str = "🎲",
        delete_after: int = None,
        message_thread_id: Optional[int] = None,
        **kwargs,
    ):
        """
        发送DICE类型消息
        :param chat_id:
        :param emoji: Should be one of "🎲", "🎯", "🏀", "⚽", "🎳", or "🎰".
        :param delete_after:
        :param kwargs:
        :return:
        """
        emoji = emoji.strip()
        if emoji not in DICE_EMOJIS:
            self.log(
                f"Warning, emoji should be one of {', '.join(DICE_EMOJIS)}",
                level="WARNING",
            )
        send_kwargs = dict(kwargs)
        if message_thread_id is not None:
            send_kwargs["message_thread_id"] = message_thread_id
        message = await self._call_telegram_api(
            "messages.SendMedia",
            lambda: self.app.send_dice(chat_id, emoji, **send_kwargs),
        )
        if message and delete_after is not None:
            self.log(
                f"Dice「{emoji}」 to {chat_id} will be deleted after {delete_after} seconds."
            )
            self.log("Waiting...")
            await asyncio.sleep(delete_after)
            await self._call_telegram_api("messages.DeleteMessages", message.delete)
            self.log(f"Dice「{emoji}」 to {chat_id} deleted!")
        return message

    async def get_forum_topics(self, chat_id: Union[int, str], limit: int = 20):
        topics = []

        async def _collect_topics():
            async for topic in self.app.get_forum_topics(chat_id, limit=limit):
                topics.append(topic)
            return topics

        return await self._call_telegram_api("channels.GetForumTopics", _collect_topics)

    def ask_one(self):
        raise NotImplementedError

    def ensure_ai_cfg(self):
        cfg_manager = OpenAIConfigManager(self.workdir)
        cfg = cfg_manager.load_config()
        if not cfg:
            cfg = cfg_manager.ask_for_config()
        return cfg

    def get_ai_tools(self):
        return AITools(self.ensure_ai_cfg())


class Waiter:
    def __init__(self):
        self.waiting_ids = set()
        self.waiting_counter = Counter()

    def add(self, elm):
        self.waiting_ids.add(elm)
        self.waiting_counter[elm] += 1

    def discard(self, elm):
        self.waiting_ids.discard(elm)
        self.waiting_counter.pop(elm, None)

    def sub(self, elm):
        self.waiting_counter[elm] -= 1
        if self.waiting_counter[elm] <= 0:
            self.discard(elm)

    def clear(self):
        self.waiting_ids.clear()
        self.waiting_counter.clear()

    def __bool__(self):
        return bool(self.waiting_ids)

    def __repr__(self):
        return f"<{self.__class__.__name__}: {self.waiting_counter}>"


class UserSignerWorkerContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    waiter: Waiter
    sign_chats: defaultdict[RouteKey, list[SignChatV3]]  # 签到配置列表
    resolved_route_keys: dict[RouteKey, RouteKey]
    chat_messages: defaultdict[
        RouteKey,
        Annotated[
            dict[int, Optional[Message]],
            Field(default_factory=dict),
        ],
    ]  # 收到的消息，key为(chat id, message_thread_id)
    waiting_message: Optional[Message]  # 正在处理的消息


class UserSigner(BaseUserWorker[SignConfigV3]):
    _workdir = ".signer"
    _tasks_dir = "signs"
    cfg_cls = SignConfigV3
    context: UserSignerWorkerContext

    def ensure_ctx(self) -> UserSignerWorkerContext:
        return UserSignerWorkerContext(
            waiter=Waiter(),
            sign_chats=defaultdict(list),
            resolved_route_keys={},
            chat_messages=defaultdict(dict),
            waiting_message=None,
        )

    @staticmethod
    def get_route_key(
        chat_id: ChatId, message_thread_id: Optional[int] = None
    ) -> RouteKey:
        return chat_id, message_thread_id

    async def resolve_chat_route_key(self, chat: SignChatV3) -> RouteKey:
        route_key = self.get_route_key(chat.chat_id, chat.message_thread_id)
        resolved_route_key = self.context.resolved_route_keys.get(route_key)
        if resolved_route_key is not None:
            return resolved_route_key
        if isinstance(chat.chat_id, int):
            resolved_route_key = route_key
        else:
            resolved_chat = await self._call_telegram_api(
                "contacts.ResolveUsername",
                lambda: self.app.get_chat(chat.chat_id),
            )
            resolved_route_key = self.get_route_key(
                resolved_chat.id, chat.message_thread_id
            )
        self.context.resolved_route_keys[route_key] = resolved_route_key
        return resolved_route_key

    def get_runtime_route_key(self, chat: SignChatV3) -> RouteKey:
        route_key = self.get_route_key(chat.chat_id, chat.message_thread_id)
        return self.context.resolved_route_keys.get(route_key, route_key)

    def _ask_actions(
        self, input_: UserInput, available_actions: List[SupportAction] = None
    ) -> List[ActionT]:
        print_to_user(f"{input_.index_str}开始配置<动作>，请按照实际签到顺序配置。")
        available_actions = available_actions or list(SupportAction)
        actions = []
        while True:
            try:
                local_input_ = UserInput()
                print_to_user(f"第{len(actions) + 1}个动作: ")
                for action in available_actions:
                    print_to_user(f"  {action.value}: {action.desc}")
                print_to_user()
                action_str = local_input_("输入对应的数字选择动作: ").strip()
                action = SupportAction(int(action_str))
                if action not in available_actions:
                    raise ValueError(f"不支持的动作: {action}")
                if len(actions) == 0 and action not in [
                    SupportAction.SEND_TEXT,
                    SupportAction.SEND_DICE,
                ]:
                    raise ValueError(
                        f"第一个动作必须为「{SupportAction.SEND_TEXT.desc}」或「{SupportAction.SEND_DICE.desc}」"
                    )
                if action == SupportAction.SEND_TEXT:
                    text = local_input_("输入要发送的文本: ")
                    actions.append(SendTextAction(text=text))
                elif action == SupportAction.SEND_DICE:
                    dice = local_input_("输入要发送的骰子（如 🎲, 🎯）: ")
                    actions.append(SendDiceAction(dice=dice))
                elif action == SupportAction.CLICK_KEYBOARD_BY_TEXT:
                    text_of_btn_to_click = local_input_("键盘中需要点击的按钮文本: ")
                    actions.append(ClickKeyboardByTextAction(text=text_of_btn_to_click))
                elif action == SupportAction.CHOOSE_OPTION_BY_IMAGE:
                    print_to_user(
                        "图片识别将使用大模型回答，请确保大模型支持图片识别。"
                    )
                    actions.append(ChooseOptionByImageAction())
                elif action == SupportAction.REPLY_BY_CALCULATION_PROBLEM:
                    print_to_user("计算题将使用大模型回答。")
                    actions.append(ReplyByCalculationProblemAction())
                else:
                    raise ValueError(f"不支持的动作: {action}")
                if local_input_("是否继续添加动作？(y/N)：").strip().lower() != "y":
                    break
            except (ValueError, ValidationError) as e:
                print_to_user("错误: ")
                print_to_user(e)
        input_.incr()
        return actions

    def ask_one(self) -> SignChatV3:
        input_ = UserInput(numbering_lang="chinese_simple")
        chat_id = parse_chat_id_or_username(
            input_("Chat ID（登录时最近对话输出中的ID或@username）: ")
        )
        name = input_("Chat名称（可选）: ")
        use_message_thread = (
            input_("是否发送到话题（message_thread_id）？(y/N)：").strip().lower()
            == "y"
        )
        message_thread_id = None
        if use_message_thread:
            message_thread_id = int(input_("message_thread_id: "))
        actions = self._ask_actions(input_)
        delete_after = (
            input_(
                "等待N秒后删除消息（发送消息后等待进行删除, '0'表示立即删除, 不需要删除直接回车）, N: "
            )
            or None
        )
        if delete_after:
            delete_after = int(delete_after)
        cfgs = {
            "chat_id": chat_id,
            "message_thread_id": message_thread_id,
            "name": name,
            "delete_after": delete_after,
            "actions": actions,
        }
        return SignChatV3.model_validate(cfgs)

    def ask_for_config(self) -> "SignConfigV3":
        chats = []
        i = 1
        print_to_user(f"开始配置任务<{self.task_name}>\n")
        while True:
            print_to_user(f"第{i}个任务: ")
            try:
                chat = self.ask_one()
                print_to_user(chat)
                print_to_user(f"第{i}个任务配置成功\n")
                chats.append(chat)
            except Exception as e:
                print_to_user(e)
                print_to_user("配置失败")
                i -= 1
            continue_ = input("继续配置任务？(y/N)：")
            if continue_.strip().lower() != "y":
                break
            i += 1
        sign_at_prompt = "签到时间（time或crontab表达式，如'06:00:00'或'0 6 * * *'）: "
        sign_at_str = input(sign_at_prompt) or "06:00:00"
        while not (sign_at := self._validate_sign_at(sign_at_str)):
            print_to_user("请输入正确的时间格式")
            sign_at_str = input(sign_at_prompt) or "06:00:00"

        random_seconds_str = input("签到时间误差随机秒数（默认为0）: ") or "0"
        random_seconds = int(float(random_seconds_str))
        config = SignConfigV3.model_validate(
            {
                "chats": chats,
                "sign_at": sign_at,
                "random_seconds": random_seconds,
            }
        )
        if config.requires_ai:
            print_to_user(OPENAI_USE_PROMPT)
        return config

    def _validate_sign_at(
        self,
        sign_at_str: str,
    ) -> Optional[str]:
        sign_at_str = sign_at_str.replace("：", ":").strip()

        try:
            sign_at = dt_time.fromisoformat(sign_at_str)
            crontab_expr = self._time_to_crontab(sign_at)
        except ValueError:
            try:
                croniter(sign_at_str)
                crontab_expr = sign_at_str
            except CroniterBadCronError:
                self.log(f"时间格式错误: {sign_at_str}", level="error")
                return None
        return crontab_expr

    @staticmethod
    def _time_to_crontab(sign_at: dt_time) -> str:
        return f"{sign_at.minute} {sign_at.hour} * * *"

    @staticmethod
    def _sign_chat_token(chat: SignChatV3) -> str:
        """频道定位串：chat_id，带话题时追加 ``#message_thread_id``。"""
        token = str(chat.chat_id)
        if chat.message_thread_id is not None:
            token = f"{token}#{chat.message_thread_id}"
        return token

    async def sign_a_chat(
        self,
        chat: SignChatV3,
    ):
        for action in chat.actions:
            self.log(f"等待处理动作: {action}")
            await self.wait_for(chat, action)
            self.log(f"处理完成: {action}")
            self.context.waiting_message = None
            await asyncio.sleep(chat.action_interval)

    async def run(
        self,
        num_of_dialogs=20,
        only_once: bool = False,
        force_rerun: bool = False,
        folder: Optional[str] = None,
    ):
        if self.app.in_memory or self.app.session_string:
            return await self.in_memory_run(
                num_of_dialogs,
                only_once=only_once,
                force_rerun=force_rerun,
                folder=folder,
            )
        return await self.normal_run(
            num_of_dialogs,
            only_once=only_once,
            force_rerun=force_rerun,
            folder=folder,
        )

    async def in_memory_run(
        self,
        num_of_dialogs=20,
        only_once: bool = False,
        force_rerun: bool = False,
        folder: Optional[str] = None,
    ):
        async with self.app:
            await self.normal_run(
                num_of_dialogs,
                only_once=only_once,
                force_rerun=force_rerun,
                folder=folder,
            )

    async def normal_run(
        self,
        num_of_dialogs=20,
        only_once: bool = False,
        force_rerun: bool = False,
        folder: Optional[str] = None,
    ):
        if self.user is None:
            await self.login(num_of_dialogs, print_chat=True, folder=folder)

        config = self.load_config(self.cfg_cls)
        if config.requires_ai:
            self.ensure_ai_cfg()

        # 签到状态仅保存在当前进程内：进程重启后若当天签到时刻已过，会补签一次
        sign_record: dict[str, str] = {}
        chat_ids = [c.chat_id for c in config.chats]

        async def sign_once():
            # 多账号错峰启动：TG_SIGNER_START_DELAY（秒）由 WebUI 按账号槽位下发，
            # 签到动作前先等待，避免同一群里连续出现多个账号的签到消息
            start_delay = float(os.environ.get("TG_SIGNER_START_DELAY", "0") or 0)
            if start_delay > 0:
                self.log(f"多账号错峰启动：等待 {int(start_delay)} 秒后开始签到")
                await asyncio.sleep(start_delay)
            # TG_SIGNER_ONLY_CHATS（JSON 数组）非空时只执行指定频道，
            # 由 WebUI「重试签到」下发，用于只重跑之前失败的频道
            chats = list(config.chats)
            only_chats_raw = os.environ.get("TG_SIGNER_ONLY_CHATS", "").strip()
            if only_chats_raw:
                try:
                    only_chats = {str(x) for x in json.loads(only_chats_raw)}
                except (ValueError, TypeError):
                    only_chats = set()
                    self.log(
                        "TG_SIGNER_ONLY_CHATS 解析失败，按全部频道执行",
                        level="WARNING",
                    )
                if only_chats:
                    chats = [c for c in chats if self._sign_chat_token(c) in only_chats]
                    self.log(f"仅重试指定频道: 共 {len(chats)} 个")
            # TG_SIGNER_SHUFFLE_CHATS=1 时随机打乱频道执行顺序（每个账号进程各自独立打乱）
            if os.environ.get("TG_SIGNER_SHUFFLE_CHATS") == "1":
                random.shuffle(chats)
                self.log("已随机打乱频道执行顺序")
            for chat in chats:
                route_key = None
                try:
                    # 频道解析前先输出频道信息：解析失败时明细里也能定位到该频道
                    self.log(f"开始执行: \n{chat}")
                    route_key = await self.resolve_chat_route_key(chat)
                    self.context.sign_chats[route_key].append(chat)
                    await self.sign_a_chat(chat)
                except errors.RPCError as _e:
                    self.log(f"签到失败: {_e} \nchat: \n{chat}")
                    logger.warning(_e, exc_info=True)
                    continue
                except _NET_ERRORS as _e:
                    # 代理不稳场景：网络类异常只跳过当前频道，不中断整个签到流程
                    self.log(
                        f"签到失败（网络异常，跳过该频道继续）: {_e} \nchat: \n{chat}"
                    )
                    logger.warning(_e, exc_info=True)
                    continue
                except RuntimeError as _e:
                    # _call_telegram_api 重试耗尽抛出的网络异常
                    self.log(
                        f"签到失败（网络异常，跳过该频道继续）: {_e} \nchat: \n{chat}"
                    )
                    logger.warning(_e, exc_info=True)
                    continue

                if route_key is not None:
                    self.context.chat_messages[route_key].clear()
                await asyncio.sleep(config.sign_interval)
            sign_record[str(now.date())] = now.isoformat()

        def need_sign(last_date_str):
            if force_rerun:
                return True
            cron_expr = self._validate_sign_at(config.sign_at)
            if last_date_str not in sign_record:
                # 当天尚无签到记录：只有今天的签到时刻已到（或已过）才补签；
                # 若启动早于当天签到时刻，则继续等待，避免提前执行。
                # 基准时间加 1 微秒，保证恰好整点启动时也视为时刻已到。
                prev_run = croniter(
                    cron_expr, now + timedelta(microseconds=1)
                ).get_prev(datetime)
                if prev_run.date() == now.date():
                    return True
                next_run_today: datetime = croniter(cron_expr, now).next(datetime)
                self.log(
                    f"当天签到时刻未到（计划时刻 {next_run_today:%Y-%m-%d %H:%M:%S}），等待定时执行"
                )
                return False
            _last_sign_at = datetime.fromisoformat(sign_record[last_date_str])
            self.log(f"上次执行时间: {_last_sign_at}")
            _cron_it = croniter(cron_expr, _last_sign_at)
            _next_run: datetime = _cron_it.next(datetime)
            if _next_run > now:
                self.log("当前未到下次执行时间，无需执行")
                return False
            return True

        connect_failures = 0
        while True:
            self.log(f"为以下Chat添加消息回调处理函数：{chat_ids}")
            self.app.add_handler(
                MessageHandler(self.on_message, filters.chat(chat_ids))
            )
            self.app.add_handler(
                EditedMessageHandler(self.on_edited_message, filters.chat(chat_ids))
            )
            try:
                async with self.app:
                    connect_failures = 0
                    now = get_now()
                    self.log(f"当前时间: {now}")
                    now_date_str = str(now.date())
                    self.context = self.ensure_ctx()
                    if need_sign(now_date_str):
                        await sign_once()

            except (OSError, errors.Unauthorized) as e:
                logger.exception(e)
                await asyncio.sleep(30)
                if only_once:
                    # run-once 必须能结束：连接类失败最多重试 3 轮，之后明确失败退出
                    connect_failures += 1
                    if connect_failures >= 3:
                        raise RuntimeError(
                            "多次尝试后仍无法连接 Telegram，run-once 任务失败"
                            "（请检查代理/网络后重试）"
                        ) from e
                continue

            if only_once:
                break
            cron_it = croniter(self._validate_sign_at(config.sign_at), now)
            next_run: datetime = cron_it.next(datetime) + timedelta(
                seconds=random.randint(0, int(config.random_seconds))
            )
            self.log(f"下次运行时间: {next_run}")
            await asyncio.sleep((next_run - now).total_seconds())

    async def run_once(self, num_of_dialogs, folder: Optional[str] = None):
        return await self.run(
            num_of_dialogs,
            only_once=True,
            force_rerun=True,
            folder=folder,
        )

    async def send_text(
        self,
        chat_id: Union[int, str],
        text: str,
        delete_after: int = None,
        message_thread_id: Optional[int] = None,
        **kwargs,
    ):
        if self.user is None:
            await self.login(print_chat=False)
        async with self.app:
            await self.send_message(
                chat_id,
                text,
                delete_after,
                message_thread_id=message_thread_id,
                **kwargs,
            )

    async def _on_message(self, client: Client, message: Message):
        message_thread_id = getattr(message, "message_thread_id", None)
        route_key = self.get_route_key(message.chat.id, message_thread_id)
        chats = self.context.sign_chats.get(route_key)
        if not chats and message_thread_id is not None:
            route_key = self.get_route_key(message.chat.id, None)
            chats = self.context.sign_chats.get(route_key)
        if not chats:
            self.log("忽略意料之外的聊天", level="WARNING")
            return
        self.context.chat_messages[route_key][message.id] = message

    async def on_message(self, client: Client, message: Message):
        # 群里的消息仅用于驱动签到动作，不写入日志
        await self._on_message(client, message)

    async def on_edited_message(self, client, message: Message):
        # 避免更新正在处理的消息，等待处理完成
        while (
            self.context.waiting_message
            and self.context.waiting_message.id == message.id
        ):
            await asyncio.sleep(0.3)
        await self._on_message(client, message)

    async def _click_keyboard_by_text(
        self, action: ClickKeyboardByTextAction, message: Message
    ):
        if reply_markup := message.reply_markup:
            if isinstance(reply_markup, InlineKeyboardMarkup):
                flat_buttons = (b for row in reply_markup.inline_keyboard for b in row)
                option_to_btn: dict[str, InlineKeyboardButton] = {}
                for btn in flat_buttons:
                    option_to_btn[btn.text] = btn
                    if action.text in btn.text:
                        self.log(f"点击按钮: {btn.text}")
                        await self.request_callback_answer(
                            self.app,
                            message.chat.id,
                            message.id,
                            btn.callback_data,
                        )
                        return True
        return False

    async def _reply_by_calculation_problem(
        self, action: ReplyByCalculationProblemAction, message
    ):
        text = _get_message_text(message)
        if text:
            self.log("检测到文本回复，尝试调用大模型进行计算题回答")
            self.log(f"问题: \n{text}")
            option_to_btn = {}
            buttons = _get_inline_keyboard_buttons(message)
            if buttons:
                option_to_btn = {
                    _normalize_option_text(btn.text): btn for btn in buttons
                }
            query = text
            if option_to_btn:
                options = [btn.text for btn in option_to_btn.values()]
                query = (
                    f"{text}\n\n"
                    f"可选答案：{json.dumps(options, ensure_ascii=False)}\n"
                    "请只从可选答案中选择最匹配的一项，并原样回复该选项文本。"
                )
            answer = await self.get_ai_tools().calculate_problem(query)
            answer = answer.strip()
            self.log(f"回答为: {answer}")
            if option_to_btn:
                target_btn = option_to_btn.get(_normalize_option_text(answer))
                if not target_btn:
                    self.log("未找到匹配的按钮", level="WARNING")
                    return False
                if not target_btn.callback_data:
                    self.log("匹配的按钮没有 callback_data，无法点击", level="WARNING")
                    return False
                self.log(f"点击按钮: {target_btn.text}")
                await self.request_callback_answer(
                    self.app,
                    message.chat.id,
                    message.id,
                    target_btn.callback_data,
                )
                return True
            await self.send_message(
                message.chat.id,
                answer,
                message_thread_id=getattr(message, "message_thread_id", None),
            )
            return True
        return False

    def _find_previous_photo_message(self, messages: list[Message], message: Message):
        try:
            message_index = next(
                index for index, item in enumerate(messages) if item is message
            )
        except StopIteration:
            return None
        for previous_message in reversed(messages[:message_index]):
            if previous_message and previous_message.photo:
                return previous_message
        return None

    async def _choose_option_by_image(
        self,
        action: ChooseOptionByImageAction,
        message,
        previous_messages: list[Message] = None,
    ):
        buttons = _get_inline_keyboard_buttons(message)
        photo_message = message
        if buttons and not message.photo and previous_messages:
            photo_message = self._find_previous_photo_message(
                previous_messages, message
            )
        if buttons and photo_message and photo_message.photo:
            options = [btn.text for btn in buttons]
            self.log("检测到图片，尝试调用大模型进行图片识别并选择选项")
            image_buffer: BinaryIO = await self.app.download_media(
                photo_message.photo.file_id, in_memory=True
            )
            image_buffer.seek(0)
            image_bytes = image_buffer.read()
            query = _get_message_text(message) or "选择正确的选项"
            result_index = await self.get_ai_tools().choose_option_by_image(
                image_bytes,
                query,
                list(enumerate(options)),
            )
            if result_index < 0 or result_index >= len(buttons):
                self.log("图片识别返回的选项序号无效", level="WARNING")
                return False
            target_btn = buttons[result_index]
            self.log(f"选择结果为: {target_btn.text}")
            if not target_btn.callback_data:
                self.log("匹配的按钮没有 callback_data，无法点击", level="WARNING")
                return False
            await self.request_callback_answer(
                self.app,
                message.chat.id,
                message.id,
                target_btn.callback_data,
            )
            return True
        return False

    async def wait_for(self, chat: SignChatV3, action: ActionT, timeout=10):
        if isinstance(action, SendTextAction):
            return await self.send_message(
                chat.chat_id,
                action.text,
                chat.delete_after,
                message_thread_id=chat.message_thread_id,
            )
        elif isinstance(action, SendDiceAction):
            return await self.send_dice(
                chat.chat_id,
                action.dice,
                chat.delete_after,
                message_thread_id=chat.message_thread_id,
            )
        route_key = self.get_runtime_route_key(chat)
        self.context.waiter.add(route_key)
        start = time.perf_counter()
        last_message = None
        while time.perf_counter() - start < timeout:
            await asyncio.sleep(0.3)
            messages_dict = self.context.chat_messages.get(route_key)
            if not messages_dict:
                continue
            messages = list(messages_dict.values())
            # 暂无新消息
            if messages[-1] == last_message:
                continue
            last_message = messages[-1]
            for message in messages:
                if message is None:
                    continue
                self.context.waiting_message = message
                ok = False
                if isinstance(action, ClickKeyboardByTextAction):
                    ok = await self._click_keyboard_by_text(action, message)
                elif isinstance(action, ReplyByCalculationProblemAction):
                    ok = await self._reply_by_calculation_problem(action, message)
                elif isinstance(action, ChooseOptionByImageAction):
                    ok = await self._choose_option_by_image(action, message, messages)
                if ok:
                    self.context.waiter.sub(route_key)
                    # 将消息ID对应value置为None，保证收到消息的编辑时消息所处的顺序
                    self.context.chat_messages[route_key][message.id] = None
                    return None
        self.log(f"等待超时: \nchat: \n{chat} \naction: {action}", level="WARNING")
        return None

    async def request_callback_answer(
        self,
        client: Client,
        chat_id: Union[int, str],
        message_id: int,
        callback_data: Union[str, bytes],
        **kwargs,
    ):
        try:
            await self._call_telegram_api(
                "messages.GetBotCallbackAnswer",
                lambda: client.request_callback_answer(
                    chat_id,
                    message_id,
                    callback_data=callback_data,
                    **kwargs,
                ),
            )
            self.log("点击完成")
        except (errors.BadRequest, TimeoutError) as e:
            self.log(e, level="ERROR")

    async def schedule_messages(
        self,
        chat_id: Union[int, str],
        text: str,
        crontab: str = None,
        next_times: int = 1,
        random_seconds: int = 0,
        message_thread_id: Optional[int] = None,
    ):
        now = get_now()
        it = croniter(crontab, start_time=now)
        if self.user is None:
            await self.login(print_chat=False)
        results = []
        async with self.app:
            for n in range(next_times):
                next_dt: datetime = it.next(ret_type=datetime) + timedelta(
                    seconds=random.randint(0, random_seconds)
                )
                results.append({"at": next_dt.isoformat(), "text": text})
                await self._call_telegram_api(
                    "messages.SendScheduledMessage",
                    lambda schedule_date=next_dt: self.app.send_message(
                        chat_id,
                        text,
                        schedule_date=schedule_date,
                        message_thread_id=message_thread_id,
                    ),
                )
                await asyncio.sleep(0.1)
                print_to_user(f"已配置次数：{n + 1}")
        self.log(f"已配置定时发送消息，次数{next_times}")
        return results

    async def get_schedule_messages(self, chat_id: Union[int, str]):
        if self.user is None:
            await self.login(print_chat=False)
        async with self.app:
            messages = await self._call_telegram_api(
                "messages.GetScheduledHistory",
                lambda: self.app.get_scheduled_messages(chat_id),
            )
            for message in messages:
                print_to_user(f"{message.date}: {message.text}")
