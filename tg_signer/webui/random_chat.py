"""随机发言：选择多个账号，按固定间隔向指定群组随机发送内置语库中的消息。"""

import asyncio
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from nicegui import ui

from tg_signer.core import get_client
from tg_signer.webui.data import load_disabled_accounts, load_user_infos
from tg_signer.webui.phrases import PHRASES

# 跨浏览器刷新仍可控制的运行中引擎：{workdir|task_id -> engine}
_GLOBAL_ENGINES: Dict[str, "RandomChatEngine"] = {}
# 跨浏览器刷新保留的共用日志缓冲：{workdir -> [line, ...]}，页面通过定时器增量拉取
_GLOBAL_LOGS: Dict[str, List[str]] = {}
# 跨浏览器刷新可用的任务状态回调：{workdir|task_id -> set_status(bool)}
_GLOBAL_STATUS_CBS: Dict[str, Callable[[bool], None]] = {}

_TASKS_FILE = "random_chat_tasks.json"


def list_session_names(
    session_dir: Union[Path, str], workdir=None
) -> List[str]:
    """列出会话目录下所有 .session 文件对应的账号名（排除停用账户）"""
    base = Path(session_dir)
    if not base.is_dir():
        return []
    disabled = load_disabled_accounts(workdir)
    return sorted(p.stem for p in base.glob("*.session") if p.stem not in disabled)


def list_known_chats(workdir) -> Dict[str, str]:
    """汇总 users/*/latest_chats.json 中的群组聊天，返回 {显示名: chat_id}"""
    options: Dict[str, str] = {}
    for info in load_user_infos(workdir):
        for chat in info.latest_chats or []:
            # type 可能是 "group"/"supergroup" 或枚举形式 "ChatType.GROUP" 等
            chat_type = str(chat.get("type") or "").lower()
            if not chat_type.endswith("group"):
                continue
            cid = chat.get("id")
            if cid is None:
                continue
            title = chat.get("title") or chat.get("first_name") or "N/A"
            options[f"{title} ({cid})"] = str(cid)
    return options


def chat_titles(workdir) -> Dict[str, str]:
    """汇总 users/*/latest_chats.json 中的群组聊天，返回 {chat_id: 群名}"""
    mapping: Dict[str, str] = {}
    for info in load_user_infos(workdir):
        for chat in info.latest_chats or []:
            chat_type = str(chat.get("type") or "").lower()
            if not chat_type.endswith("group"):
                continue
            cid = chat.get("id")
            if cid is None:
                continue
            title = chat.get("title") or chat.get("first_name") or "N/A"
            mapping[str(cid)] = title
    return mapping


async def update_account_chats(
    account: str,
    session_dir: Union[Path, str],
    workdir,
    log: Callable[[str], None],
) -> int:
    """连接 Telegram 拉取该账号最近对话并写入 users/<id>/latest_chats.json，返回对话数（失败返回 -1）"""
    client = None
    try:
        client = get_client(account, workdir=str(session_dir))
        ok = await asyncio.wait_for(client.connect(), 30)
        if not ok:
            log(f"[{account}] 会话未授权，跳过")
            return -1
        me = await asyncio.wait_for(client.get_me(), 30)
        chats = []
        async for dialog in client.get_dialogs(limit=200):
            chats.append(dialog.chat)
        latest = [
            {
                "id": c.id,
                "title": c.title,
                "type": str(c.type).replace("ChatType.", ""),
                "username": c.username,
                "first_name": c.first_name,
                "last_name": c.last_name,
            }
            for c in chats
        ]
        user_dir = Path(workdir) / "users" / str(me.id)
        user_dir.mkdir(parents=True, exist_ok=True)
        with open(user_dir / "latest_chats.json", "w", encoding="utf-8") as fp:
            json.dump(latest, fp, indent=4, ensure_ascii=False)
        me_file = user_dir / "me.json"
        if not me_file.is_file():
            me_file.write_text(str(me), encoding="utf-8")
        log(f"[{account}] 已更新 {len(latest)} 个对话")
        return len(latest)
    except asyncio.TimeoutError:
        log(f"[{account}] 连接超时（30s），跳过")
        return -1
    except Exception as exc:
        log(f"[{account}] 更新失败: {exc}")
        return -1
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


# 账户授权状态懒检测缓存：{"<session_dir>|<account>": bool}
_AUTHZ_CACHE: Dict[str, bool] = {}
_AUTHZ_PENDING: set = set()
_AUTHZ_TASK: Dict[str, Optional[asyncio.Task]] = {"task": None}


async def _check_account_authorized(account: str, session_dir: Path) -> Optional[bool]:
    """连接一次判断会话授权状态。

    返回 True（已授权）/ False（连接成功但确认无 auth_key）；
    网络异常、超时、数据库占用等一律返回 None（不缓存，下次刷新重试）。
    """
    client = None
    try:
        client = get_client(account, workdir=str(session_dir))
        ok = await asyncio.wait_for(client.connect(), 15)
        return bool(ok)
    except Exception:
        return None
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
            from tg_signer.core import _CLIENT_INSTANCES

            _CLIENT_INSTANCES.pop(
                str(Path(session_dir).joinpath(account).resolve()), None
            )


def authz_cached(session_dir: Path, account: str) -> Optional[bool]:
    """返回缓存的授权状态：True/False 已知，None 未检测"""
    return _AUTHZ_CACHE.get(f"{session_dir}|{account}")


def build_account_options(session_dir: Path, names: List[str]) -> Dict[str, str]:
    """把账号列表转成下拉选项。NiceGUI 字典选项格式为 {值: 显示名}，未授权的加 ⚠️ 标注"""
    opts: Dict[str, str] = {}
    for n in names:
        cached = authz_cached(session_dir, n)
        opts[n] = f"{n} ⚠️未授权" if cached is False else n
    return opts


def schedule_auth_checks(
    session_dir: Path, names: List[str], on_done: Optional[Callable[[], None]] = None
) -> None:
    """后台逐个检测未缓存账户的授权状态，完成后回调 on_done 刷新标注"""
    session_key = str(session_dir)
    todo = [
        n
        for n in names
        if f"{session_key}|{n}" not in _AUTHZ_CACHE
        and f"{session_key}|{n}" not in _AUTHZ_PENDING
    ]

    async def _run() -> None:
        try:
            for n in todo:
                key = f"{session_key}|{n}"
                _AUTHZ_PENDING.add(key)
                try:
                    result = await _check_account_authorized(n, session_dir)
                    # None = 检测失败（网络/占用等），不缓存，下次刷新会重试
                    if result is not None:
                        _AUTHZ_CACHE[key] = result
                finally:
                    _AUTHZ_PENDING.discard(key)
        except Exception:
            pass
        finally:
            if on_done is not None:
                try:
                    on_done()
                except Exception:
                    pass

    if not todo:
        if on_done is not None:
            _AUTHZ_TASK["task"] = asyncio.create_task(_run())
        return
    _AUTHZ_TASK["task"] = asyncio.create_task(_run())


def _store_path(workdir) -> Path:
    return Path(workdir) / _TASKS_FILE


def load_task_store(workdir) -> dict:
    """读取持久化任务配置：{"seq": N, "tasks": {id: cfg}}"""
    path = _store_path(workdir)
    if not path.is_file():
        return {"seq": 1, "tasks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"seq": 1, "tasks": {}}
    if not isinstance(data, dict):
        return {"seq": 1, "tasks": {}}
    data.setdefault("seq", max((int(k) for k in data.get("tasks", {})), default=0) + 1)
    data.setdefault("tasks", {})
    return data


def save_task_store(workdir, store: dict) -> None:
    path = _store_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_chat_id(raw) -> Optional[Union[int, str]]:
    text = (raw or "").strip()
    if not text:
        return None
    # 兼容最近聊天标签格式「名称 (id)」，直接提取 id
    m = re.search(r"\((-?\d+)\)\s*$", text)
    if m:
        return int(m.group(1))
    try:
        return int(text)
    except ValueError:
        return text


class RandomChatEngine:
    """后台随机发言任务：每个账号各自按间隔发送随机语料"""

    # 连接阶段的超时秒数，避免代理异常时卡住无法停止
    CONNECT_TIMEOUT = 30

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._stopping: bool = False
        # 运行进度：{account: 已发送条数}，供任务表格状态列展示
        self.progress: Dict[str, int] = {}
        self._total_accounts: int = 0
        self._total_count: int = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def progress_text(self) -> Optional[str]:
        """运行中返回带进度的状态文本（如 "运行中 (18/100)"），未运行返回 None"""
        if not self.running:
            return None
        if self._stopping:
            return "停止中"
        sent = sum(self.progress.values())
        if self._total_count and self._total_accounts:
            return f"运行中 ({sent}/{self._total_count * self._total_accounts})"
        return f"运行中 (已发{sent}条)"

    async def start(
        self,
        accounts: List[str],
        chat_id: Union[int, str],
        interval: int,
        session_dir: Path,
        log: Callable[[str], None],
        delete_after: int = 20,
        total_count: int = 50,
        on_finish: Optional[Callable[[], None]] = None,
    ) -> bool:
        if self.running:
            log("任务已在运行中")
            return False
        self.progress = {}
        self._stopping = False
        self._total_accounts = len(accounts)
        self._total_count = total_count
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self._loop(
                accounts, chat_id, interval, session_dir, log, delete_after, total_count
            )
        )
        if on_finish is not None:
            self._task.add_done_callback(lambda _t: on_finish())
        return True

    async def stop(self) -> None:
        """立即返回，不阻塞界面；发送循环可能在网络请求中卡住，由后台任务收尾"""
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

    async def _delete_later(
        self, message, delay, log, account, text, chat_label, undelivered
    ) -> None:
        """消息发出 delay 秒后撤回"""
        try:
            await asyncio.sleep(delay)
            await message.delete()
            undelivered.discard(message.id)
            log(f"{datetime.now():%H:%M:%S} [已删除] {account} -> {chat_label}: {text}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"{datetime.now():%H:%M:%S} [删除失败] {account}: {exc}")

    async def _account_loop(
        self, client, account, chat_id, interval, log, delete_after, total_count
    ) -> None:
        """单个账号的发送循环：每条间隔 interval + 随机 0~3 秒"""
        pending_deletes: set = set()
        undelivered: set = set()
        sent = 0
        try:
            while not self._stop.is_set():
                if total_count and sent >= total_count:
                    break
                text = random.choice(PHRASES)
                try:
                    message = await client.send_message(chat_id, text)
                    sent += 1
                    self.progress[account] = sent
                    undelivered.add(message.id)
                    done = f"{sent}/{total_count}" if total_count else str(sent)
                    chat_title = getattr(getattr(message, "chat", None), "title", None)
                    target = chat_title or str(chat_id)
                    log(
                        f"{datetime.now():%H:%M:%S} [成功] {account}({done}) -> {target}: {text}"
                    )
                    if delete_after:
                        task = asyncio.create_task(
                            self._delete_later(
                                message,
                                delete_after,
                                log,
                                account,
                                text,
                                target,
                                undelivered,
                            )
                        )
                        pending_deletes.add(task)
                        task.add_done_callback(pending_deletes.discard)
                except Exception as exc:
                    log(f"{datetime.now():%H:%M:%S} [失败] {account}: {exc}")
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=interval + random.uniform(0, 3),
                    )
                except TimeoutError:
                    pass
        finally:
            # 停止发送后，已发出的消息不立即撤回，仍按设定的删除延迟依次自动删除
            if self._stop.is_set() and undelivered:
                if delete_after:
                    log(
                        f"{datetime.now():%H:%M:%S} [停止] {account} 停止发送，"
                        f"剩余 {len(undelivered)} 条将按 {delete_after}s 延迟自动删除"
                    )
                else:
                    log(
                        f"{datetime.now():%H:%M:%S} [停止] {account} 停止发送，"
                        f"已发送的 {len(undelivered)} 条不删除"
                    )
            if pending_deletes:
                # 等待延迟删除收尾，设上限避免网络异常时无限挂起
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending_deletes, return_exceptions=True),
                        timeout=delete_after + 60,
                    )
                except asyncio.TimeoutError:
                    log(
                        f"{datetime.now():%H:%M:%S} [停止] {account} "
                        f"删除收尾超时，未确认删除的消息保留在群里可手动处理"
                    )

    async def _loop(
        self, accounts, chat_id, interval, session_dir, log, delete_after, total_count
    ) -> None:
        clients = {}
        for account in accounts:
            if self._stop.is_set():
                break
            client = None
            try:
                client = get_client(account, workdir=str(session_dir))
                is_authorized = await asyncio.wait_for(
                    client.connect(), timeout=self.CONNECT_TIMEOUT
                )
                if not is_authorized:
                    log(f"「{account}」会话未授权，已跳过（请先用 CLI 登录该账号）")
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    continue
                if self._stop.is_set():
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    break
                me = await asyncio.wait_for(
                    client.get_me(), timeout=self.CONNECT_TIMEOUT
                )
                nickname = getattr(me, "first_name", "") or ""
                clients[account] = client
                log(f"「{account}」已连接（{nickname}）")
            except asyncio.TimeoutError:
                log(f"「{account}」连接超时（网络或代理异常），已跳过")
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            except Exception as exc:
                log(f"「{account}」连接失败: {exc}")
        if not clients:
            log("没有可用账号，任务结束")
            return
        count_label = f"每账号 {total_count} 条" if total_count else "不限条数"
        log(
            f"开始随机发言：{len(clients)} 个账号 -> {chat_id}，"
            f"每账号独立计时，间隔 {interval}~{interval + 3} 秒，{count_label}"
        )
        try:
            await asyncio.gather(
                *[
                    self._account_loop(
                        client, account, chat_id, interval, log, delete_after, total_count
                    )
                    for account, client in clients.items()
                ]
            )
            if total_count:
                summary = ", ".join(f"{a}:{total_count}" for a in sorted(clients))
                log(f"每个账号均已发满 {total_count} 条（{summary}），任务完成")
        finally:
            for client in clients.values():
                try:
                    await client.disconnect()
                except Exception:
                    pass
            log("随机发言已停止")


def random_chat_block(workdir, default_session_dir: str = ".") -> Callable[[], None]:
    """构建“随机发言”标签页内容，返回刷新函数以纳入全局刷新。"""
    wd_key = str(Path(workdir).resolve())
    store = load_task_store(workdir)
    tasks: Dict[str, dict] = dict(store["tasks"])
    # 修复历史 bug：账号值曾被写入「⚠️未授权」标签后缀，加载时统一清理
    for cfg in tasks.values():
        if isinstance(cfg.get("accounts"), list):
            cleaned = [str(a).replace(" ⚠️未授权", "") for a in cfg["accounts"]]
            if cleaned != cfg["accounts"]:
                cfg["accounts"] = cleaned
    store["tasks"] = {
        name: {k: v for k, v in cfg.items() if not k.startswith("_")}
        for name, cfg in tasks.items()
    }
    save_task_store(workdir, store)
    chat_options_map: Dict[str, str] = {}
    chat_title_map: Dict[str, str] = {}
    # 当前渲染的任务表格引用，供轮询循环更新进度状态列
    table_ref: Dict[str, object] = {"table": None}

    def _persist() -> None:
        clean = {}
        for name, cfg in tasks.items():
            clean[name] = {
                "accounts": list(cfg["accounts"]),
                "chat_id": cfg["chat_id"],
                "interval": cfg["interval"],
                "delete_after": cfg["delete_after"],
                "total_count": cfg["total_count"],
                "session_dir": str(cfg["session_dir"]),
            }
        store["tasks"] = clean
        save_task_store(workdir, store)

    def engine_key(name: str) -> str:
        return f"{wd_key}|{name}"

    def get_engine(name: str) -> Optional[RandomChatEngine]:
        return _GLOBAL_ENGINES.get(engine_key(name))

    def ensure_engine(name: str) -> RandomChatEngine:
        engine = get_engine(name)
        if engine is None:
            engine = RandomChatEngine()
            _GLOBAL_ENGINES[engine_key(name)] = engine
        return engine

    def chat_display(cid) -> str:
        return chat_title_map.get(str(cid), str(cid))

    def make_log(name: str) -> Callable[[str], None]:
        def _log(msg: str) -> None:
            buf = _GLOBAL_LOGS.setdefault(wd_key, [])
            buf.append(f"[#{name}] {msg}")
            if len(buf) > 300:
                del buf[:-300]

        return _log

    def _set_account_options() -> None:
        session_dir = Path(session_dir_input.value or ".")
        names = list_session_names(session_dir, workdir)
        account_select.options = build_account_options(session_dir, names)
        account_select.update()

    def refresh_options() -> None:
        session_dir = Path(session_dir_input.value or ".")
        _set_account_options()
        names = list_session_names(session_dir, workdir)
        # 后台懒检测未授权账户，完成后刷新 ⚠️ 标注
        schedule_auth_checks(session_dir, names, on_done=_set_account_options)
        chat_options_map.clear()
        chat_options_map.update(list_known_chats(workdir))
        chat_select.options = list(chat_options_map.keys())
        chat_select.update()
        chat_title_map.clear()
        chat_title_map.update(chat_titles(workdir))

    def _on_chat_pick(e) -> None:
        value = e.value or ""
        resolved = chat_options_map.get(value, value)
        chat_input.value = resolved
        chat_input.update()
        # 调试：确认最近聊天选择事件是否触发及解析结果
        try:
            log_buf.append(
                f"{datetime.now():%H:%M:%S} [调试] 最近聊天选择 value={value!r} "
                f"解析={resolved!r} map大小={len(chat_options_map)}"
            )
        except Exception:
            pass

    def task_running(name: str) -> bool:
        engine = get_engine(name)
        return engine is not None and engine.running

    def _status_text(name: str) -> str:
        engine = get_engine(name)
        if engine is None or not engine.running:
            return "未运行"
        return engine.progress_text() or "运行中"

    def remove_task(name: str) -> None:
        if task_running(name):
            ui.notify(f"任务 #{name} 正在运行，请先停止再删除", type="warning")
            return
        tasks.pop(name, None)
        _GLOBAL_ENGINES.pop(engine_key(name), None)
        _GLOBAL_STATUS_CBS.pop(engine_key(name), None)
        _persist()
        render_tasks()

    def duplicate_task(name: str) -> None:
        cfg = tasks[name]
        session_dir_input.value = str(cfg["session_dir"])
        account_select.value = list(cfg["accounts"])
        chat_input.value = str(cfg["chat_id"])
        interval_input.value = cfg["interval"]
        delete_input.value = cfg["delete_after"]
        total_input.value = cfg["total_count"]
        for el in (
            session_dir_input,
            account_select,
            chat_input,
            interval_input,
            delete_input,
            total_input,
        ):
            el.update()
        ui.notify("已复制任务参数，可修改后点击「确定添加」", type="positive")
        add_dlg.open()

    async def start_task(name: str, set_status: Callable[[bool], None]) -> None:
        cfg = tasks[name]
        for other, ocfg in tasks.items():
            if (
                other != name
                and task_running(other)
                and set(cfg["accounts"]) & set(ocfg["accounts"])
            ):
                ui.notify(
                    f"账号已被运行中的任务 #{other} 占用（同一账号不能并行发消息），请先停止该任务",
                    type="warning",
                )
                return
        def _on_finish(_n: str = name) -> None:
            cb = _GLOBAL_STATUS_CBS.get(engine_key(_n)) or tasks.get(_n, {}).get(
                "_set_status"
            )
            if cb is not None:
                _apply_status(cb, False)

        engine = ensure_engine(name)
        started = await engine.start(
            cfg["accounts"],
            cfg["chat_id"],
            cfg["interval"],
            Path(cfg["session_dir"]),
            make_log(name),
            delete_after=cfg["delete_after"],
            total_count=cfg["total_count"],
            on_finish=_on_finish,
        )
        if started:
            set_status(True)
            ui.notify(f"任务 #{name} 已启动", type="positive")
        else:
            ui.notify(f"任务 #{name} 正在运行或收尾中，请稍后再试", type="warning")

    async def stop_task(name: str, set_status: Callable[[bool], None]) -> None:
        engine = get_engine(name)
        if engine is not None and engine.running:
            # 停止后由状态定时器显示「停止中」，收尾完成经 on_finish 回调置为未运行
            await engine.stop()
        else:
            set_status(False)

    def _apply_status(set_status: Callable[[bool], None], running: bool) -> None:
        try:
            set_status(running)
        except Exception:
            pass

    def on_add_task() -> None:
        accounts = list(account_select.value or [])
        if not accounts:
            ui.notify("请至少选择一个账号", type="warning")
            return
        chat_raw = (chat_input.value or "").strip()
        if not chat_raw and chat_select.value:
            # 目标群组框为空但最近聊天已选中：直接用下拉选中值（容错回填）
            chat_raw = str(chat_select.value)
            chat_input.value = chat_raw
            chat_input.update()
        chat_id = parse_chat_id(chat_raw)
        if chat_id is None:
            ui.notify("请填写目标群组 chat_id 或 @username", type="warning")
            return
        name = str(store["seq"])
        store["seq"] += 1
        tasks[name] = {
            "accounts": accounts,
            "chat_id": chat_id,
            "interval": max(1, int(interval_input.value or 5)),
            # 0 或留空 = 不删除
            "delete_after": max(0, int(delete_input.value or 0)),
            "total_count": max(
                0, int(total_input.value if total_input.value is not None else 50)
            ),
            "session_dir": str(Path(session_dir_input.value or ".")),
        }
        ensure_engine(name)
        _persist()
        render_tasks()
        add_dlg.close()
        ui.notify(f"任务 #{name} 已添加，正在启动...", type="positive")
        asyncio.create_task(start_task(name, tasks[name]["_set_status"]))

    def render_tasks() -> None:
        task_list_container.clear()
        table_ref["table"] = None
        with task_list_container:
            if not tasks:
                ui.label("暂无任务，请点击「添加任务」创建。").classes(
                    "text-sm text-gray-400"
                )
                return
            columns = [
                {"name": "name", "label": "任务", "field": "name", "align": "left"},
                {
                    "name": "accounts",
                    "label": "账号",
                    "field": "accounts",
                    "align": "left",
                },
                {"name": "chat", "label": "群组", "field": "chat", "align": "left"},
                {
                    "name": "interval",
                    "label": "间隔",
                    "field": "interval",
                    "align": "left",
                },
                {
                    "name": "delete_after",
                    "label": "删除延迟",
                    "field": "delete_after",
                    "align": "left",
                },
                {
                    "name": "total",
                    "label": "每账号条数",
                    "field": "total",
                    "align": "left",
                },
                {
                    "name": "status",
                    "label": "状态",
                    "field": "status",
                    "align": "left",
                },
                {
                    "name": "actions",
                    "label": "操作",
                    "field": "actions",
                    "align": "left",
                },
            ]
            rows = []
            for name in sorted(
                tasks,
                key=lambda n: int(n) if str(n).isdigit() else 0,
                reverse=True,
            ):
                cfg = tasks[name]
                rows.append(
                    {
                        "name": str(name),
                        "accounts": ", ".join(cfg["accounts"]),
                        "chat": chat_display(cfg["chat_id"]),
                        "interval": f"{cfg['interval']}s",
                        "delete_after": (
                            f"{cfg['delete_after']}s" if cfg["delete_after"] else "不删除"
                        ),
                        "total": str(cfg["total_count"]) if cfg["total_count"] else "不限",
                        "status": _status_text(name),
                    }
                )
            table = ui.table(
                columns=columns,
                rows=rows,
                row_key="name",
                pagination=5,
            ).classes("w-full").props("flat dense")
            table_ref["table"] = table
            # 状态列渲染为彩色徽章：运行中=蓝、未运行=灰（状态文本可能带进度，用 startsWith 匹配）
            table.add_slot(
                "body-cell-status",
                '<q-td :props="props">'
                '<q-badge :color="props.row.status.startsWith(\'运行中\') ? \'info\' : \'grey-6\'">'
                "{{ props.row.status }}</q-badge></q-td>",
            )
            # 操作列：未运行显示「启动」，运行中显示「停止」，另附「复制」「删除」
            table.add_slot(
                "body-cell-actions",
                '<q-td :props="props">'
                '<q-btn v-if="!props.row.status.startsWith(\'运行中\')" flat dense color="primary" '
                'label="启动" @click="$parent.$emit(\'startTask\', props.row)" />'
                '<q-btn v-if="props.row.status.startsWith(\'运行中\')" flat dense color="negative" '
                'label="停止" @click="$parent.$emit(\'stopTask\', props.row)" />'
                '<q-btn flat dense color="primary" '
                'label="复制" @click="$parent.$emit(\'copyTask\', props.row)" />'
                '<q-btn flat dense color="negative" '
                'label="删除" @click="$parent.$emit(\'deleteTask\', props.row)" />'
                "</q-td>",
            )

            def make_set_status(n: str) -> Callable[[bool], None]:
                def _set(running: bool) -> None:
                    for r in table.rows:
                        if r["name"] == str(n):
                            r["status"] = "运行中" if running else "未运行"
                            try:
                                table.update()
                            except Exception:
                                pass
                            break

                return _set

            async def on_start_event(e) -> None:
                row = e.args
                if isinstance(row, dict):
                    n = str(row.get("name") or "")
                    if n in tasks:
                        await start_task(n, make_set_status(n))

            async def on_stop_event(e) -> None:
                row = e.args
                if isinstance(row, dict):
                    n = str(row.get("name") or "")
                    if n in tasks:
                        await stop_task(n, make_set_status(n))

            def on_copy_event(e) -> None:
                row = e.args
                if isinstance(row, dict) and row.get("name"):
                    duplicate_task(str(row["name"]))

            def on_delete_event(e) -> None:
                row = e.args
                if isinstance(row, dict) and row.get("name"):
                    remove_task(str(row["name"]))

            table.on("startTask", on_start_event)
            table.on("stopTask", on_stop_event)
            table.on("copyTask", on_copy_event)
            table.on("deleteTask", on_delete_event)
            for name in list(tasks):
                set_status = make_set_status(str(name))
                tasks[name]["_set_status"] = set_status
                _GLOBAL_STATUS_CBS[engine_key(name)] = set_status

    with ui.card().classes("w-full shadow-md"):
        ui.label("随机发言").classes("text-lg font-semibold")
        ui.label(
            f"从内置 {len(PHRASES)} 条语库中随机选一句，由所选账号发送到目标群组，发送后可在指定秒数自动删除（删除延迟填 0 或留空则不删除）。支持添加多个任务并行运行。"
        ).classes("text-sm text-gray-500")

        # —— 任务列表（置顶） ——
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("任务列表").classes("font-semibold")
            ui.button("添加任务", icon="add", on_click=lambda: add_dlg.open()).props(
                "outline"
            )
        task_list_container = ui.column().classes("w-full gap-2")

        # —— 共用日志 ——
        ui.separator()

        def clear_log() -> None:
            log_buf.clear()
            _seen["n"] = len(log_buf)
            send_log.clear()

        with ui.row().classes("w-full items-center justify-between"):
            ui.label("共用日志（所有任务）").classes("font-semibold")
            ui.button(icon="delete_outline", on_click=clear_log).props(
                "flat round dense"
            ).tooltip("清空日志")
        send_log = ui.log(max_lines=300).classes("w-full h-40")
        log_buf = _GLOBAL_LOGS.setdefault(wd_key, [])
        _seen = {"n": len(log_buf)}

        for line in log_buf:
            try:
                send_log.push(line)
            except Exception:
                pass

        client = ui.context.client

        async def _poll_loop() -> None:
            while True:
                await asyncio.sleep(1.0)
                if not client.has_socket_connection:
                    return
                # 每秒刷新运行中任务的进度状态列（如 "运行中 (18/100)"）
                table = table_ref["table"]
                if table is not None:
                    try:
                        changed = False
                        for r in table.rows:
                            n = str(r.get("name") or "")
                            if n in tasks:
                                new_status = _status_text(n)
                                if r.get("status") != new_status:
                                    r["status"] = new_status
                                    changed = True
                        if changed:
                            table.update()
                    except Exception:
                        pass
                if _seen["n"] > len(log_buf):
                    _seen["n"] = len(log_buf)
                new_lines = log_buf[_seen["n"]:]
                if not new_lines:
                    continue
                _seen["n"] = len(log_buf)
                try:
                    with client:
                        for line in new_lines:
                            try:
                                send_log.push(line)
                            except Exception:
                                pass
                except Exception:
                    return

        asyncio.create_task(_poll_loop())

        # —— 添加任务弹窗 ——
        with ui.dialog() as add_dlg, ui.card().classes("w-full max-w-3xl"):
            ui.label("添加任务").classes("text-lg font-semibold")
            with ui.row().classes("items-end w-full gap-3 flex-wrap"):
                session_dir_input = ui.input(
                    label="会话目录（.session 所在目录）", value=default_session_dir
                ).classes("w-56")
                account_select = ui.select(
                    label="选择账号（可多选）",
                    options=[],
                    multiple=True,
                ).classes("min-w-[260px]").props("use-chips")
                ui.button("刷新账号/群组", on_click=lambda: refresh_options()).props(
                    "outline"
                )

                async def update_chats_from_tg() -> None:
                    accounts = list(account_select.value or [])
                    if not accounts:
                        accounts = list_session_names(
                            Path(session_dir_input.value or "."), workdir
                        )
                    if not accounts:
                        ui.notify("没有可用账号", type="warning")
                        return
                    update_btn.disable()

                    def _log(msg: str) -> None:
                        log_buf.append(f"{datetime.now():%H:%M:%S} {msg}")

                    try:
                        _log(f"开始从 Telegram 更新 {len(accounts)} 个账号的群组列表...")
                        for a in accounts:
                            await update_account_chats(
                                a, Path(session_dir_input.value or "."), workdir, _log
                            )
                        refresh_options()
                        ui.notify("群组列表已从 Telegram 更新", type="positive")
                    finally:
                        update_btn.enable()

                update_btn = ui.button(
                    "从TG更新群组", on_click=update_chats_from_tg
                ).props("outline")

            with ui.row().classes("items-end w-full gap-3 flex-wrap"):
                chat_input = ui.input(
                    label="目标群组（chat_id 或 @username）",
                    placeholder="-1001234567890 或 @groupname",
                ).classes("min-w-[320px]")
                chat_select = ui.select(
                    label="或从最近聊天选择",
                    options=[],
                    with_input=True,
                    on_change=_on_chat_pick,
                ).classes("min-w-[280px]")
                interval_input = ui.number(
                    label="发送间隔（秒）", value=5, min=1, max=3600, format="%d"
                ).classes("w-36")
                delete_input = ui.number(
                    label="删除延迟（秒，0或空=不删除）", value=20, min=0, max=86400, format="%d"
                ).classes("w-36")
                total_input = ui.number(
                    label="每账号发送条数（0=不限）", value=50, min=0, max=1000000, format="%d"
                ).classes("w-48")

            ui.label(
                "提示：发送过于频繁可能触发 Telegram 风控，建议间隔不小于 5 秒；同一账号不能同时用于多个运行中的任务。"
            ).classes("text-xs text-amber-600")
            with ui.row().classes("w-full justify-end"):
                ui.button("取消", on_click=add_dlg.close).props("flat")
                ui.button("确定添加", color="primary", on_click=on_add_task)

    refresh_options()
    render_tasks()
    return refresh_options
