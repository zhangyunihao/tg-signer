"""随机发言：选择多个账号，按固定间隔向指定群组随机发送内置语库中的消息。"""

import asyncio
import json
import random
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from nicegui import ui

from tg_signer.core import get_client
from tg_signer.webui.data import load_user_infos
from tg_signer.webui.phrases import PHRASES

# 跨浏览器刷新仍可控制的运行中引擎：{workdir:task_id -> engine}
_GLOBAL_ENGINES: Dict[str, "RandomChatEngine"] = {}
# 跨浏览器刷新保留的共用日志缓冲：{workdir -> [line, ...]}
_GLOBAL_LOGS: Dict[str, List[str]] = {}

_TASKS_FILE = "random_chat_tasks.json"


def list_session_names(session_dir: Union[Path, str]) -> List[str]:
    """列出会话目录下所有 .session 文件对应的账号名"""
    base = Path(session_dir)
    if not base.is_dir():
        return []
    return sorted(p.stem for p in base.glob("*.session"))


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
    try:
        return int(text)
    except ValueError:
        return text


class RandomChatEngine:
    """后台随机发言任务：每 interval 秒随机挑选一个账号发送一条随机语料"""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

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
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass
            self._task = None

    async def _delete_later(self, message, delay: int, log, account, text) -> None:
        """消息发出 delay 秒后撤回"""
        try:
            await asyncio.sleep(delay)
            chat = getattr(message, "chat", None)
            title = getattr(chat, "title", None)
            cid = getattr(chat, "id", "?")
            target = f"{title} ({cid})" if title else str(cid)
            await message.delete()
            log(f"{datetime.now():%H:%M:%S} [已删除] {account} -> {target}: {text}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"{datetime.now():%H:%M:%S} [删除失败] {account}: {exc}")

    async def _loop(
        self, accounts, chat_id, interval, session_dir, log, delete_after, total_count
    ) -> None:
        clients = {}
        pending_deletes: set = set()
        sent_by_account: Dict[str, int] = {}
        for account in accounts:
            try:
                client = get_client(account, workdir=str(session_dir))
                is_authorized = await client.connect()
                if not is_authorized:
                    log(f"「{account}」会话未授权，已跳过（请先用 CLI 登录该账号）")
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    continue
                me = await client.get_me()
                nickname = getattr(me, "first_name", "") or ""
                clients[account] = client
                sent_by_account[account] = 0
                log(f"「{account}」已连接（{nickname}）")
            except Exception as exc:
                log(f"「{account}」连接失败: {exc}")
        if not clients:
            log("没有可用账号，任务结束")
            return
        log(f"开始随机发言：{len(clients)} 个账号 -> {chat_id}，每 {interval} 秒一条")
        try:
            while not self._stop.is_set():
                if total_count:
                    remaining = [
                        a for a, c in sent_by_account.items() if c < total_count
                    ]
                else:
                    remaining = list(clients)
                if not remaining:
                    break
                account = random.choice(remaining)
                text = random.choice(PHRASES)
                try:
                    message = await clients[account].send_message(chat_id, text)
                    sent_by_account[account] += 1
                    done = (
                        f"{sent_by_account[account]}/{total_count}"
                        if total_count
                        else str(sent_by_account[account])
                    )
                    chat_title = getattr(getattr(message, "chat", None), "title", None)
                    target = f"{chat_title} ({chat_id})" if chat_title else str(chat_id)
                    log(
                        f"{datetime.now():%H:%M:%S} [成功] {account}({done}) -> {target}: {text}"
                    )
                    if delete_after:
                        task = asyncio.create_task(
                            self._delete_later(
                                message, delete_after, log, account, text
                            )
                        )
                        pending_deletes.add(task)
                        task.add_done_callback(pending_deletes.discard)
                    if total_count and all(
                        c >= total_count for c in sent_by_account.values()
                    ):
                        summary = ", ".join(
                            f"{a}:{c}" for a, c in sorted(sent_by_account.items())
                        )
                        log(f"每个账号均已发满 {total_count} 条（{summary}），任务完成")
                        break
                except Exception as exc:
                    log(f"{datetime.now():%H:%M:%S} [失败] {account}: {exc}")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=interval)
                except TimeoutError:
                    pass
        finally:
            for task in pending_deletes:
                task.cancel()
            if pending_deletes:
                await asyncio.gather(*pending_deletes, return_exceptions=True)
            for account, client in clients.items():
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
    chat_options_map: Dict[str, str] = {}
    chat_title_map: Dict[str, str] = {}

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
            line = f"[#{name}] {msg}"
            buf = _GLOBAL_LOGS.setdefault(wd_key, [])
            buf.append(line)
            if len(buf) > 300:
                del buf[:-300]
            try:
                send_log.push(line)
            except Exception:
                pass

        return _log

    def refresh_options() -> None:
        session_dir = Path(session_dir_input.value or ".")
        account_select.options = list_session_names(session_dir)
        account_select.update()
        chat_options_map.clear()
        chat_options_map.update(list_known_chats(workdir))
        chat_select.options = list(chat_options_map.keys())
        chat_select.update()
        chat_title_map.clear()
        chat_title_map.update(chat_titles(workdir))

    def _on_chat_pick(e) -> None:
        value = e.value or ""
        chat_input.value = chat_options_map.get(value, value)
        chat_input.update()

    def task_running(name: str) -> bool:
        engine = get_engine(name)
        return engine is not None and engine.running

    def remove_task(name: str) -> None:
        if task_running(name):
            ui.notify(f"任务 #{name} 正在运行，请先停止再删除", type="warning")
            return
        tasks.pop(name, None)
        _GLOBAL_ENGINES.pop(engine_key(name), None)
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
        ui.notify("已复制任务参数到新增表单，可修改后点击「添加任务」", type="positive")

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
            cb = tasks.get(_n, {}).get("_set_status")
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

    async def stop_task(name: str, set_status: Callable[[bool], None]) -> None:
        engine = get_engine(name)
        if engine is not None:
            await engine.stop()
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
        chat_id = parse_chat_id(chat_input.value)
        if chat_id is None:
            ui.notify("请填写目标群组 chat_id 或 @username", type="warning")
            return
        name = str(store["seq"])
        store["seq"] += 1
        tasks[name] = {
            "accounts": accounts,
            "chat_id": chat_id,
            "interval": max(1, int(interval_input.value or 5)),
            "delete_after": max(1, int(delete_input.value or 20)),
            "total_count": max(
                0, int(total_input.value if total_input.value is not None else 50)
            ),
            "session_dir": str(Path(session_dir_input.value or ".")),
        }
        ensure_engine(name)
        _persist()
        render_tasks()
        ui.notify(f"任务 #{name} 已添加，正在启动...", type="positive")
        asyncio.create_task(start_task(name, tasks[name]["_set_status"]))

    def render_tasks() -> None:
        task_list_container.clear()
        with task_list_container:
            if not tasks:
                ui.label("暂无任务，请在上方添加。").classes("text-sm text-gray-400")
                return
            for name in list(tasks):
                cfg = tasks[name]
                is_running = task_running(name)
                with ui.card().classes("w-full").props("flat bordered"):
                    with ui.row().classes("w-full items-center gap-3 flex-wrap"):
                        status_label = ui.label(
                            "状态：运行中" if is_running else "状态：未运行"
                        ).classes("text-sm text-gray-600")
                        start_btn = ui.button("启动", color="primary")
                        stop_btn = ui.button("停止", color="negative")
                        if is_running:
                            start_btn.disable()
                        else:
                            stop_btn.disable()
                        ui.button(
                            "复制",
                            color="primary",
                            on_click=lambda n=name: duplicate_task(n),
                        ).props("outline")
                        ui.button(
                            "删除",
                            color="negative",
                            on_click=lambda n=name: remove_task(n),
                        ).props("outline")

                        def set_status(
                            running: bool,
                            _s=status_label,
                            _start=start_btn,
                            _stop=stop_btn,
                        ) -> None:
                            _s.text = "状态：运行中" if running else "状态：未运行"
                            try:
                                _s.update()
                                if running:
                                    _start.disable()
                                    _stop.enable()
                                else:
                                    _start.enable()
                                    _stop.disable()
                            except Exception:
                                pass

                        ui.label(
                            f"#{name} | {len(cfg['accounts'])} 账号 → {chat_display(cfg['chat_id'])} | 间隔 "
                            f"{cfg['interval']}s | 删除延迟 {cfg['delete_after']}s | "
                            f"每账号 {cfg['total_count'] or '不限'} 条"
                        ).classes("text-sm text-gray-500 flex-1 min-w-[280px]")

                        start_btn.on_click(
                            lambda n=name, s=set_status: start_task(n, s)
                        )
                        stop_btn.on_click(lambda n=name, s=set_status: stop_task(n, s))
                        tasks[name]["_set_status"] = set_status

    with ui.card().classes("w-full shadow-md"):
        ui.label("随机发言").classes("text-lg font-semibold")
        ui.label(
            f"从内置 {len(PHRASES)} 条语库中随机选一句，由所选账号发送到目标群组，发送后在指定秒数自动删除。支持添加多个任务并行运行。"
        ).classes("text-sm text-gray-500")

        ui.label("新增任务").classes("font-semibold mt-1")
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
                label="删除延迟（秒）", value=20, min=1, max=86400, format="%d"
            ).classes("w-36")
            total_input = ui.number(
                label="每账号发送条数（0=不限）", value=50, min=0, max=1000000, format="%d"
            ).classes("w-48")
            ui.button("添加任务", color="primary", on_click=on_add_task)

        ui.label(
            "提示：发送过于频繁可能触发 Telegram 风控，建议间隔不小于 5 秒；同一账号不能同时用于多个运行中的任务。"
        ).classes("text-xs text-amber-600")

        ui.separator()
        ui.label("任务列表").classes("font-semibold")
        task_list_container = ui.column().classes("w-full gap-2")

        ui.separator()
        ui.label("共用日志（所有任务）").classes("font-semibold")
        send_log = ui.log(max_lines=300).classes("w-full h-40")
        for line in _GLOBAL_LOGS.get(wd_key, []):
            try:
                send_log.push(line)
            except Exception:
                pass

    refresh_options()
    render_tasks()
    return refresh_options
