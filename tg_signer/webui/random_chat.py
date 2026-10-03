"""随机发言：选择多个账号，按固定间隔向指定群组随机发送内置语库中的消息。"""

import asyncio
import random
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from nicegui import ui

from tg_signer.core import get_client
from tg_signer.webui.data import load_user_infos
from tg_signer.webui.phrases import PHRASES


def list_session_names(session_dir: Union[Path, str]) -> List[str]:
    """列出会话目录下所有 .session 文件对应的账号名"""
    base = Path(session_dir)
    if not base.is_dir():
        return []
    return sorted(p.stem for p in base.glob("*.session"))


def list_known_chats(workdir) -> Dict[str, str]:
    """汇总 users/*/latest_chats.json 中的最近聊天，返回 {显示名: chat_id}"""
    options: Dict[str, str] = {}
    for info in load_user_infos(workdir):
        for chat in info.latest_chats or []:
            cid = chat.get("id")
            if cid is None:
                continue
            title = chat.get("title") or chat.get("first_name") or "N/A"
            options[f"{title} ({cid})"] = str(cid)
    return options


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
            await message.delete()
            log(f"{datetime.now():%H:%M:%S} [已删除] {account}: {text}")
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
                    log(f"{datetime.now():%H:%M:%S} [成功] {account}({done}): {text}")
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
    engine = RandomChatEngine()
    chat_options_map: Dict[str, str] = {}

    def log(msg: str) -> None:
        send_log.push(str(msg))

    def refresh_options() -> None:
        session_dir = Path(session_dir_input.value or ".")
        account_select.options = list_session_names(session_dir)
        account_select.update()
        chat_options_map.clear()
        chat_options_map.update(list_known_chats(workdir))
        chat_select.options = list(chat_options_map.keys())
        chat_select.update()

    def _on_chat_pick(e) -> None:
        value = e.value or ""
        chat_input.value = chat_options_map.get(value, value)
        chat_input.update()

    def reset_ui() -> None:
        status_label.text = "状态：未运行"
        status_label.update()
        start_btn.enable()
        stop_btn.disable()

    async def on_start() -> None:
        accounts = list(account_select.value or [])
        chat_id = parse_chat_id(chat_input.value)
        if not accounts:
            ui.notify("请至少选择一个账号", type="warning")
            return
        if chat_id is None:
            ui.notify("请填写目标群组 chat_id 或 @username", type="warning")
            return
        interval = max(1, int(interval_input.value or 5))
        delete_after = max(1, int(delete_input.value or 20))
        total_count = max(0, int(total_input.value if total_input.value is not None else 50))
        session_dir = Path(session_dir_input.value or ".")
        started = await engine.start(
            accounts,
            chat_id,
            interval,
            session_dir,
            log,
            delete_after=delete_after,
            total_count=total_count,
            on_finish=reset_ui,
        )
        if started:
            status_label.text = "状态：运行中"
            status_label.update()
            start_btn.disable()
            stop_btn.enable()
            ui.notify("随机发言已启动", type="positive")

    async def on_stop() -> None:
        await engine.stop()
        reset_ui()

    with ui.card().classes("w-full shadow-md"):
        ui.label("随机发言").classes("text-lg font-semibold")
        ui.label(
            f"从内置 {len(PHRASES)} 条语库中随机选一句，每隔固定秒数由所选账号之一发送到目标群组，发送的消息会在指定秒数后自动删除。"
        ).classes("text-sm text-gray-500")

        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
            session_dir_input = ui.input(
                label="会话目录（.session 所在目录）", value=default_session_dir
            ).classes("w-56")
            account_select = ui.select(
                label="选择账号（可多选）",
                options=[],
                multiple=True,
            ).classes("min-w-[260px]").props("use-chips")
            ui.button("刷新账号/群组", on_click=lambda: refresh_options()).props("outline")

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

        ui.label("提示：发送过于频繁可能触发 Telegram 风控，建议间隔不小于 5 秒。").classes(
            "text-xs text-amber-600"
        )

        with ui.row().classes("gap-2 items-center"):
            start_btn = ui.button("开始", color="primary", on_click=on_start)
            stop_btn = ui.button("停止", color="negative", on_click=on_stop)
            stop_btn.disable()
            status_label = ui.label("状态：未运行").classes("text-sm text-gray-600")

        send_log = ui.log(max_lines=300).classes("w-full h-64")

    refresh_options()
    return refresh_options
