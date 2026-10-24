import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Dict

from nicegui import ui

from tg_signer.core import get_api_config, get_proxy, safe_disconnect
from tg_signer.utils import atomic_write_json
from tg_signer.webui.common import add_dialog_close_button, pretty_json, state
from tg_signer.webui.data import (
    account_identities,
    apply_account_order,
    get_workdir,
    load_disabled_accounts,
    load_two_way_accounts,
    load_user_infos,
    rename_account_state,
    set_account_enabled,
    set_account_order,
    set_account_two_way,
)

logger = logging.getLogger(__name__)


class AccountLoginSession:
    """在页面上执行 tg-signer CLI 登录：连接 → 发送验证码 → 登录（可选两步验证密码）。

    使用文件模式 Client（与 CLI 一致），登录成功后自动落盘 <账户名>.session。
    """

    def __init__(self, name: str, phone: str) -> None:
        self.name = (name or "").strip()
        self.phone = (phone or "").strip()
        self.client = None
        self.phone_code_hash = None
        self.password_needed = False

    def _check_basic(self) -> None:
        if not self.name:
            raise ValueError("请填写账户名")
        if not self.phone:
            raise ValueError("请填写手机号（国际格式，如 +8613800138000）")

    @staticmethod
    def _save_me(me) -> None:
        data = {
            "id": me.id,
            "first_name": me.first_name,
            "last_name": me.last_name,
            "username": me.username,
            "phone_number": me.phone_number,
            "is_premium": getattr(me, "is_premium", None),
        }
        user_dir = get_workdir(state.workdir) / "users" / str(me.id)
        user_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(user_dir / "me.json", data)

    async def start(self) -> str:
        """连接并发送验证码；若会话已授权则直接完成登录。"""
        self._check_basic()
        from pyrogram import Client

        api_id, api_hash = get_api_config()
        self.client = Client(
            self.name,
            api_id=api_id,
            api_hash=api_hash,
            proxy=get_proxy(),
            workdir=".",
        )
        authorized = await asyncio.wait_for(self.client.connect(), 60)
        if authorized:
            me = await asyncio.wait_for(self.client.get_me(), 30)
            self._save_me(me)
            await safe_disconnect(self.client)
            self.client = None
            return f"该账户已登录：{me.first_name or me.id}"
        sent = await asyncio.wait_for(
            self.client.send_phone_number_code(self.phone), 60
        )
        self.phone_code_hash = sent.phone_code_hash
        return "验证码已发送，请查看 Telegram 官方消息"

    async def complete(self, code: str, password: str) -> str:
        """提交验证码（及可选的两步验证密码）完成登录。"""
        if self.client is None or self.phone_code_hash is None:
            raise ValueError("请先发送验证码")
        from pyrogram.errors import SessionPasswordNeeded

        try:
            if not self.password_needed:
                code = (code or "").strip()
                if not code:
                    raise ValueError("请填写验证码")
                await asyncio.wait_for(
                    self.client.sign_in(self.phone, self.phone_code_hash, code), 60
                )
            else:
                password = (password or "").strip()
                if not password:
                    raise ValueError("请填写两步验证密码")
                await asyncio.wait_for(self.client.check_password(password), 60)
        except SessionPasswordNeeded:
            self.password_needed = True
            hint = await self._fetch_password_hint()
            msg = "该账户开启了两步验证，请填写密码后再次点击登录"
            if hint:
                msg += f"（密码提示：{hint}）"
            else:
                msg += "（该账户未设置密码提示）"
            raise ValueError(msg)
        me = await asyncio.wait_for(self.client.get_me(), 30)
        self._save_me(me)
        await safe_disconnect(self.client)
        self.client = None
        # 清掉该账户可能残留的旧客户端缓存，避免后续任务使用过期会话
        from tg_signer.core import _CLIENT_INSTANCES

        _CLIENT_INSTANCES.pop(str(Path(".").joinpath(self.name).resolve()), None)
        return str(me.first_name or me.id)

    async def _fetch_password_hint(self) -> str:
        """获取账户的两步验证「密码提示」（创建云密码时可选设置的内容）。"""
        if self.client is None:
            return ""
        try:
            from pyrogram.raw import functions

            pwd = await asyncio.wait_for(
                self.client.invoke(functions.account.GetPassword()), 30
            )
            return (getattr(pwd, "hint", "") or "").strip()
        except Exception:
            logger.debug("获取两步验证密码提示失败", exc_info=True)
            return ""

    async def cancel(self) -> None:
        if self.client is not None:
            await safe_disconnect(self.client)
            self.client = None
        self.phone_code_hash = None
        self.password_needed = False


def user_info_block() -> Callable[[], None]:
    container = ui.column().classes("w-full gap-2")

    # 增加账户弹窗（CLI 登录流程，只创建一次，refresh 清空 container 不影响它）
    login_session: dict = {"session": None}

    def _reset_login_dialog() -> None:
        async def _cancel() -> None:
            session = login_session.get("session")
            if session is not None:
                await session.cancel()
            login_session["session"] = None

        asyncio.ensure_future(_cancel())
        add_name.value = ""
        add_phone.value = ""
        add_code.value = ""
        add_password.value = ""
        add_status.text = ""
        add_status.classes(replace="text-sm text-gray-500")
        step2.visible = False
        add_password.visible = False
        send_btn.enable()
        login_btn.enable()

    with ui.dialog() as add_dlg, ui.card().classes("w-[520px] max-w-full"):
        add_dialog_close_button(add_dlg)
        ui.label("增加账户（TG 登录）").classes("text-lg font-semibold")
        add_name = ui.input("账户名", placeholder="例如 my_account").classes("w-full")
        add_phone = ui.input(
            "手机号", placeholder="国际格式，如 +8613800138000"
        ).classes("w-full")
        add_status = ui.label("").classes("text-sm text-gray-500")
        # 密码提示常驻显示（放在 step2 外，打开弹窗即可见）
        ui.label(
            "提示：若账户开启了两步验证，登录时会提示需要密码，届时密码输入框才会出现"
        ).classes("text-xs text-gray-400")

        with ui.column().classes("w-full") as step2:
            step2.visible = False
            add_code = ui.input("验证码", placeholder="Telegram 发送的验证码").classes(
                "w-full"
            )
            add_password = ui.input(
                "两步验证密码",
                placeholder="仅开启了两步验证的账户需要填写",
                password=True,
                password_toggle_button=True,
            ).classes("w-full")
            add_password.visible = False

        async def do_send_code() -> None:
            add_status.classes(replace="text-sm text-orange-600")
            add_status.text = "正在连接 Telegram 并发送验证码..."
            add_status.update()
            send_btn.disable()
            try:
                session = login_session.get("session")
                if session is None:
                    session = AccountLoginSession(add_name.value, add_phone.value)
                    login_session["session"] = session
                msg = await session.start()
                add_status.classes(replace="text-sm text-positive")
                add_status.text = msg
                step2.visible = True
                # 密码框保持隐藏，仅当登录提示需要两步验证密码时才出现
            except Exception as exc:  # noqa: BLE001
                add_status.classes(replace="text-sm text-negative")
                add_status.text = f"发送失败：{exc}"
                send_btn.enable()
            add_status.update()

        async def do_login() -> None:
            session = login_session.get("session")
            if session is None:
                add_status.classes(replace="text-sm text-negative")
                add_status.text = "请先发送验证码"
                add_status.update()
                return
            login_btn.disable()
            add_status.classes(replace="text-sm text-orange-600")
            add_status.text = "正在登录..."
            add_status.update()
            try:
                who = await session.complete(add_code.value, add_password.value)
            except ValueError as exc:
                add_status.classes(replace="text-sm text-negative")
                add_status.text = str(exc)
                if "两步验证" in str(exc):
                    add_password.visible = True
                    add_password.update()
                login_btn.enable()
                add_status.update()
                return
            except Exception as exc:  # noqa: BLE001
                add_status.classes(replace="text-sm text-negative")
                add_status.text = f"登录失败：{exc}"
                login_btn.enable()
                add_status.update()
                return
            ui.notify(f"账户登录成功：{who}", type="positive")
            add_dlg.close()
            _reset_login_dialog()
            refresh()

        # 操作按钮固定在弹窗最底部（发送验证码/登录）
        with ui.row().classes("w-full gap-2"):
            send_btn = ui.button("发送验证码", on_click=do_send_code).props("outline")
            login_btn = ui.button("登录", color="primary", on_click=do_login)

    def toggle_account(name: str, enabled: bool) -> None:
        set_account_enabled(name, enabled, state.workdir)
        ui.notify(f"账户 {name} 已{'启用' if enabled else '停用'}", type="positive")

    def toggle_two_way(name: str, two_way: bool) -> None:
        set_account_two_way(name, two_way, state.workdir)
        ui.notify(
            f"账户 {name} 已{'标记为双向（发言数要求类抽奖自动跳过）' if two_way else '取消双向标记'}",
            type="positive",
        )

    # @spambot 批量状态查询：进行中标记（防并发重复触发）
    _spamb_checking = [False]

    def _spamb_set(lbl, text: str, cls: str = "text-xs text-gray-500") -> None:
        try:
            lbl.classes(replace=cls)
            lbl.text = text
            lbl.update()
        except Exception:  # noqa: BLE001 - 元素可能已被 refresh 清理
            logger.debug("更新状态查询标签失败", exc_info=True)

    async def _query_spamb(name: str) -> str:
        """连接 @spambot 查询单个账户状态，返回 bot 回复文本（短暂连接即断）。"""
        from tg_signer.core import get_client

        client = get_client(name, reuse=False)
        try:
            await client.start()
            await client.send_message("spambot", "/start")
            await asyncio.sleep(6)
            async for m in client.get_chat_history("spambot", limit=3):
                if m.from_user and m.from_user.is_bot:
                    return m.text or ""
            return ""
        finally:
            await safe_disconnect(client)

    def check_all_status() -> None:
        """查询所有账户在 @spambot 的限制状态：弹窗内并发 3 逐个展示。"""
        if _spamb_checking[0]:
            ui.notify("状态查询进行中，请稍候", type="warning")
            return
        names = apply_account_order(
            sorted(p.stem for p in Path(".").glob("*.session")), state.workdir
        )
        if not names:
            ui.notify("未发现账户（当前目录无 *.session）", type="warning")
            return
        _spamb_checking[0] = True
        with ui.dialog() as dlg, ui.card().classes("w-[560px] max-w-full"):
            add_dialog_close_button(dlg)
            ui.label("@spambot 账户限制状态").classes("text-lg font-semibold")
            ui.label("逐个连接 Telegram 查询，结果实时刷新").classes(
                "text-xs text-gray-500"
            )
            with ui.column().classes("w-full gap-1"):
                labels: Dict[str, object] = {}
                for n in names:
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(n).classes("text-sm font-medium w-28 truncate")
                        labels[n] = ui.label("等待查询…").classes("text-xs")
        dlg.open()

        async def _run() -> None:
            semaphore = asyncio.Semaphore(3)

            async def _one(n: str) -> None:
                async with semaphore:
                    lbl = labels[n]
                    _spamb_set(lbl, "查询中…", "text-xs text-gray-500")
                    try:
                        text = await _query_spamb(n)
                    except Exception as exc:
                        msg = str(exc)[:120]
                        if "locked" in msg.lower():
                            msg = "session 被占用（任务运行中），请停止相关任务后重试"
                        _spamb_set(lbl, f"查询失败: {msg}", "text-xs text-negative")
                        return
                    low = text.lower()
                    if not text:
                        _spamb_set(
                            lbl,
                            "未收到回复，请在 TG 内手动查看",
                            "text-xs text-orange-600",
                        )
                    elif "not limited" in low or "good news" in low:
                        _spamb_set(lbl, "正常：未被限制", "text-xs text-green-600")
                    elif "limited" in low:
                        _spamb_set(
                            lbl,
                            "受限：可在 TG 内打开 @spambot 申诉；"
                            "建议标记「双向」跳过发言类抽奖任务",
                            "text-xs text-negative",
                        )
                    else:
                        _spamb_set(
                            lbl, f"回复：{text.strip()[:120]}", "text-xs text-gray-600"
                        )

            try:
                await asyncio.gather(*(_one(n) for n in names))
            finally:
                _spamb_checking[0] = False

        asyncio.ensure_future(_run())

    def refresh() -> None:
        container.clear()
        disabled = load_disabled_accounts(state.workdir)
        two_ways = load_two_way_accounts(state.workdir)
        with container:
            # —— 账户管理 ——
            with ui.card().classes("w-full shadow-sm tgs-account-card"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("账户管理").classes("text-lg font-semibold")
                        ui.icon("swap_vert").classes("text-gray-400").tooltip(
                            "用上下箭头调整顺序，保存后各页面账号下拉也按此顺序显示"
                        )
                    with ui.row().classes("items-center gap-2"):
                        ui.button(
                            "查询状态",
                            icon="fact_check",
                            on_click=check_all_status,
                        ).props("outline").tooltip("查询所有账户在 @spambot 的限制状态")
                        ui.button(
                            "增加账户",
                            icon="person_add",
                            on_click=lambda: (_reset_login_dialog(), add_dlg.open()),
                        ).props("outline")
                names = sorted(p.stem for p in Path(".").glob("*.session"))
                env_account = os.environ.get("TG_ACCOUNT")
                if env_account and env_account not in names:
                    names.append(env_account)
                names = apply_account_order(names, state.workdir)
                if not names:
                    ui.label("未发现账户（当前目录无 *.session）").classes(
                        "text-sm text-gray-500"
                    )
                else:
                    identities = account_identities(names, workdir=state.workdir)
                    # 同一 Telegram 账号可能有多份 session，聚合用于重复标注
                    ids_by_uid: dict = {}
                    for n, info in identities.items():
                        ids_by_uid.setdefault(info["user_id"], []).append(n)

                    def delete_account(name: str) -> None:
                        with ui.dialog() as dlg, ui.card():
                            add_dialog_close_button(dlg)
                            ui.label(f"确认删除账户 {name}？").classes(
                                "text-lg font-semibold"
                            )
                            ui.label(
                                "将删除该账户的 .session / .session_string 文件，不可恢复！"
                                "删除前请先停止使用该账户的任务。"
                            ).classes("text-sm text-red-600")
                            with ui.row().classes("w-full justify-end"):
                                ui.button("取消", on_click=dlg.close).props("flat")

                                def do_delete(n: str = name) -> None:
                                    errs = []
                                    for suffix in (".session", ".session_string"):
                                        try:
                                            Path(n + suffix).unlink(missing_ok=True)
                                        except Exception as exc:
                                            errs.append(str(exc))
                                    dlg.close()
                                    if errs:
                                        ui.notify(
                                            f"删除失败（文件可能被运行中的连接占用）: {errs[0]}",
                                            type="negative",
                                        )
                                        return
                                    from tg_signer.core import _CLIENT_INSTANCES

                                    _CLIENT_INSTANCES.pop(
                                        str(Path(".").joinpath(n).resolve()), None
                                    )
                                    ui.notify(f"账户 {n} 已删除", type="positive")
                                    refresh()

                                ui.button("确认删除", on_click=do_delete).props(
                                    "color=negative"
                                )
                        dlg.open()

                    def rename_account(name: str) -> None:
                        with (
                            ui.dialog() as dlg,
                            ui.card().classes("w-[420px] max-w-full"),
                        ):
                            add_dialog_close_button(dlg)
                            ui.label(f"重命名账户 {name}").classes(
                                "text-lg font-semibold"
                            )
                            ui.label(
                                "将重命名 .session / .session_string 文件，"
                                "并同步更新顺序与停用记录。重命名前请先停止使用该账户的任务。"
                            ).classes("text-sm text-gray-500")
                            new_name = ui.input("新账户名", value=name).classes(
                                "w-full"
                            )
                            err = ui.label("").classes("text-sm text-negative")

                            def do_rename() -> None:
                                target = new_name.value.strip()
                                if not target or target == name:
                                    err.text = "请输入与当前不同的新账户名"
                                    err.update()
                                    return
                                if target in (".", "..") or any(
                                    ch in target for ch in '/\\:*?"<>|'
                                ):
                                    err.text = "账户名包含非法字符"
                                    err.update()
                                    return
                                if (
                                    Path(target + ".session").exists()
                                    or Path(target + ".session_string").exists()
                                ):
                                    err.text = f"账户 {target} 已存在"
                                    err.update()
                                    return
                                errs = []
                                for suffix in (".session", ".session_string"):
                                    src = Path(name + suffix)
                                    if src.exists():
                                        try:
                                            src.rename(Path(target + suffix))
                                        except Exception as exc:
                                            errs.append(str(exc))
                                if errs:
                                    err.text = f"重命名失败（文件可能被运行中的连接占用）: {errs[0]}"
                                    err.update()
                                    return
                                rename_account_state(name, target, state.workdir)
                                from tg_signer.core import _CLIENT_INSTANCES

                                _CLIENT_INSTANCES.pop(
                                    str(Path(".").joinpath(name).resolve()), None
                                )
                                dlg.close()
                                ui.notify(
                                    f"账户 {name} 已重命名为 {target}", type="positive"
                                )
                                refresh()

                            with ui.row().classes("w-full justify-end"):
                                ui.button("取消", on_click=dlg.close).props("flat")
                                ui.button("确认重命名", on_click=do_rename).props(
                                    "color=primary"
                                )
                        dlg.open()

                    def move_account(name: str, delta: int) -> None:
                        """上下箭头调整账户顺序：与相邻账户交换位置后持久化。"""
                        idx = names.index(name)
                        new_idx = idx + delta
                        if new_idx < 0 or new_idx >= len(names):
                            return
                        lst = list(names)
                        lst[idx], lst[new_idx] = lst[new_idx], lst[idx]
                        set_account_order(lst, state.workdir)
                        refresh()

                    with ui.grid(columns=3).classes("w-full gap-2 accounts-grid"):
                        for pos, name in enumerate(names):
                            with ui.column().classes("border rounded p-2 gap-1"):
                                with ui.row().classes(
                                    "w-full items-center justify-between no-wrap "
                                    "account-card-head"
                                ):
                                    ui.switch(
                                        name,
                                        value=name not in disabled,
                                        on_change=lambda e, n=name: toggle_account(
                                            n, e.value
                                        ),
                                    )
                                    up_btn = ui.button(
                                        icon="keyboard_arrow_up",
                                        on_click=lambda n=name: move_account(n, -1),
                                    ).props("flat round dense")
                                    up_btn.tooltip("上移")
                                    if pos <= 0:
                                        up_btn.props("disable")
                                    down_btn = ui.button(
                                        icon="keyboard_arrow_down",
                                        on_click=lambda n=name: move_account(n, 1),
                                    ).props("flat round dense")
                                    down_btn.tooltip("下移")
                                    if pos >= len(names) - 1:
                                        down_btn.props("disable")
                                    ui.button(
                                        icon="edit",
                                        on_click=lambda n=name: rename_account(n),
                                    ).props("flat round dense").tooltip("重命名账户")
                                    ui.button(
                                        icon="delete",
                                        on_click=lambda n=name: delete_account(n),
                                    ).props("flat round dense color=negative").tooltip(
                                        "删除账户"
                                    )
                                with ui.row().classes(
                                    "w-full items-center justify-between"
                                ):
                                    ui.switch(
                                        "双向",
                                        value=name in two_ways,
                                        on_change=lambda e, n=name: toggle_two_way(
                                            n, e.value
                                        ),
                                    ).props("dense").tooltip(
                                        "双向账号无法接收陌生人主动私聊，"
                                        "遇「发言数大于 N 条」类抽奖自动跳过"
                                    )
                                info = identities.get(name)
                                if info:
                                    parts = []
                                    if info["name"]:
                                        parts.append(f"→ {info['name']}")
                                    if info["username"]:
                                        parts.append(f"@{info['username']}")
                                    parts.append(f"ID {info['user_id']}")
                                    text = " ".join(parts)
                                    dups = [
                                        n
                                        for n in ids_by_uid.get(info["user_id"], [])
                                        if n != name
                                    ]
                                    if dups:
                                        text += f"（与 {'、'.join(dups)} 为同一账号）"
                                    ui.label(text).classes("text-xs text-gray-500")
                                else:
                                    ui.label(
                                        "→ 未获取到 Telegram 身份（未登录或缺少用户信息）"
                                    ).classes("text-xs text-gray-400")
                ui.label(
                    "停用的账户不会出现在签到/随机发言/批量退频道的账号下拉列表中"
                ).classes("text-xs text-gray-500")

            # —— 用户信息 ——
            entries = load_user_infos(state.workdir)
            if not entries:
                ui.label("未找到用户信息").classes("text-gray-500")
                return
            for entry in entries:
                name = entry.data.get("first_name") or ""
                header = f"{entry.user_id} {name}".strip()
                with ui.expansion(header, icon="person"):
                    ui.label(f"文件: {entry.path}")
                    ui.code(pretty_json(entry.data), language="json").classes("w-full")

                    if entry.latest_chats:
                        ui.separator().classes("my-2")
                        ui.label(f"最近聊天 ({len(entry.latest_chats)})").classes(
                            "font-semibold"
                        )

                        chat_rows = []
                        for chat in entry.latest_chats:
                            chat_rows.append(
                                {
                                    "id": chat.get("id"),
                                    "title": chat.get("title")
                                    or chat.get("first_name")
                                    or "N/A",
                                    "type": chat.get("type"),
                                    "username": chat.get("username") or "",
                                }
                            )

                        ui.table(
                            columns=[
                                {
                                    "name": "id",
                                    "label": "ID",
                                    "field": "id",
                                    "align": "left",
                                },
                                {
                                    "name": "title",
                                    "label": "名称",
                                    "field": "title",
                                    "align": "left",
                                },
                                {
                                    "name": "type",
                                    "label": "类型",
                                    "field": "type",
                                    "align": "left",
                                },
                                {
                                    "name": "username",
                                    "label": "用户名",
                                    "field": "username",
                                    "align": "left",
                                },
                            ],
                            rows=chat_rows,
                            pagination=10,
                        ).classes("w-full").props("flat dense")
                    else:
                        ui.label("未找到最近聊天记录").classes(
                            "text-gray-500 text-sm mt-2"
                        )

    return refresh
