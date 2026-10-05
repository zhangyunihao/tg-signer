"""自动退群：扫描所选账号的对话，找出超过 N 天不更新的频道（可选含群组），预览确认后批量退出。"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List

from nicegui import ui
from pyrogram.errors import FloodWait, RPCError

from tg_signer.core import get_client
from tg_signer.webui.random_chat import (
    build_account_options,
    list_session_names,
    schedule_auth_checks,
)

CONFIG_NAME = "auto_leave_config.json"

# 扫描结果默认不勾选的关键字（频道名命中即排除，防止误退）
UNSELECT_KEYWORDS = ("抽奖", "嫩妹社", "野寻")


def _config_path(workdir) -> Path:
    return Path(workdir) / CONFIG_NAME


def load_config(workdir) -> Dict:
    """读取自动退群配置，文件缺失或损坏时返回默认值"""
    defaults = {
        "session_dir": ".",
        "days": 7,
        "accounts": [],
        "include_groups": False,
        "skip_pinned": True,
    }
    try:
        data = json.loads(_config_path(workdir).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            defaults.update({k: v for k, v in data.items() if k in defaults})
    except Exception:
        pass
    return defaults


def save_config(workdir, cfg: Dict) -> None:
    try:
        _config_path(workdir).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _chat_type(chat) -> str:
    return str(getattr(chat, "type", "")).upper().replace("CHATTYPE.", "")


async def scan_stale_chats(
    account: str,
    session_dir: Path,
    days: int,
    include_groups: bool,
    skip_pinned: bool,
    log: Callable[[str], None],
) -> List[Dict]:
    """扫描单个账号，返回超过 days 天不更新的对话列表（只读，不做任何退出）"""
    found: List[Dict] = []
    client = None
    try:
        client = get_client(account, workdir=str(session_dir))
        ok = await asyncio.wait_for(client.connect(), 30)
        if not ok:
            log(f"[{account}] 会话未授权，跳过")
            return found
        cutoff = datetime.utcnow() - timedelta(days=days)
        scanned = 0
        async for dialog in client.get_dialogs():  # 不设上限，遍历全部对话
            scanned += 1
            if scanned % 200 == 0:
                log(f"[{account}] 已扫描 {scanned} 个对话...")
            chat = dialog.chat
            ctype = _chat_type(chat)
            is_channel = ctype == "CHANNEL"
            is_group = ctype in ("GROUP", "SUPERGROUP")
            if not (is_channel or (include_groups and is_group)):
                continue
            title = getattr(chat, "title", None) or "N/A"
            if getattr(chat, "is_creator", False):
                continue  # 自己是创建者，绝不退出
            if skip_pinned and getattr(dialog, "pinned_message", None) is not None:
                log(f"[{account}] 跳过置顶对话：{title}")
                continue
            top = dialog.top_message
            if top is None or getattr(top, "date", None) is None:
                log(f"[{account}] 无最新消息信息，跳过：{title}")
                continue
            if top.date < cutoff:
                age_days = max(0, (datetime.utcnow() - top.date).days)
                found.append(
                    {
                        "key": f"{account}:{chat.id}",
                        "account": account,
                        "chat_id": chat.id,
                        "title": title,
                        "type": ctype,
                        "age_days": age_days,
                    }
                )
        log(f"[{account}] 扫描完成，共 {scanned} 个对话，{len(found)} 个超过 {days} 天未更新")
    except asyncio.TimeoutError:
        log(f"[{account}] 连接超时（30s），跳过")
    except Exception as exc:
        log(f"[{account}] 扫描失败: {exc}")
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
    return found


async def leave_one(client, chat_id, title: str, log: Callable[[str], None]) -> bool:
    """退出单个对话，处理 FloodWait（等待后重试一次）"""
    for attempt in (1, 2):
        try:
            await client.leave_chat(chat_id)
            log(f"[已退出] {title} ({chat_id})")
            return True
        except FloodWait as exc:
            wait = min(int(getattr(exc, "value", 0) or 60), 120)
            if attempt == 2:
                log(f"[失败] {title}: FloodWait 超时放弃")
                return False
            log(f"[限流] 等待 {wait}s 后重试：{title}")
            await asyncio.sleep(wait + 1)
        except RPCError as exc:
            log(f"[失败] {title}: {exc}")
            return False
    return False


def auto_leave_block(workdir, default_session_dir: str = ".") -> Callable[[], None]:
    """构建「自动退群」标签页内容，返回刷新函数以纳入全局刷新。"""
    cfg = load_config(workdir)
    running = {"flag": False}
    rows_all: List[Dict] = []
    log_lines: List[str] = []

    def log(msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        log_lines.append(line)
        if len(log_lines) > 300:
            del log_lines[:-300]
        try:
            log_area.push(line)
        except Exception:
            pass

    def persist() -> None:
        save_config(
            workdir,
            {
                "session_dir": session_dir_input.value or ".",
                "days": int(days_input.value or 7),
                "accounts": list(account_select.value or []),
                "include_groups": bool(include_groups.value),
                "skip_pinned": bool(skip_pinned.value),
            },
        )

    def refresh_options() -> None:
        session_dir = Path(session_dir_input.value or ".")
        names = list_session_names(session_dir, workdir)
        account_select.options = build_account_options(session_dir, names)
        if not account_select.value:
            account_select.value = [a for a in (cfg.get("accounts") or []) if a in names]
        account_select.update()
        # 后台懒检测未授权账户，完成后刷新 ⚠️ 标注
        schedule_auth_checks(session_dir, names, on_done=refresh_marks)

    def refresh_marks() -> None:
        session_dir = Path(session_dir_input.value or ".")
        names = list_session_names(session_dir, workdir)
        account_select.options = build_account_options(session_dir, names)
        account_select.update()

    async def set_busy(busy: bool) -> None:
        running["flag"] = busy
        for btn in (scan_btn, leave_btn):
            btn.enable() if not busy else btn.disable()

    async def on_scan() -> None:
        if running["flag"]:
            return
        accounts = list(account_select.value or [])
        if not accounts:
            ui.notify("请至少选择一个账号", type="warning")
            return
        persist()
        days = max(1, int(days_input.value or 7))
        session_dir = Path(session_dir_input.value or ".")
        await set_busy(True)
        try:
            rows_all.clear()
            result_table.rows = []
            log(f"开始扫描：{len(accounts)} 个账号，阈值 {days} 天")
            for account in accounts:
                found = await scan_stale_chats(
                    account,
                    session_dir,
                    days,
                    bool(include_groups.value),
                    bool(skip_pinned.value),
                    log,
                )
                rows_all.extend(found)
                result_table.rows = list(rows_all)
                result_table.update()
            if not rows_all:
                log("未发现符合条件的对话")
            else:
                # 默认全选，但名字含敏感关键字的频道不勾选（防误退抽奖/特定频道）
                default_selected = [
                    r
                    for r in rows_all
                    if not any(kw in str(r.get("title") or "") for kw in UNSELECT_KEYWORDS)
                ]
                result_table.selected = default_selected
                result_table.update()
                skipped = len(rows_all) - len(default_selected)
                if skipped:
                    log(
                        f"共 {len(rows_all)} 个候选，含「抽奖」的 {skipped} 个已默认不勾选，"
                        f"点击「退出所选」执行"
                    )
                else:
                    log(f"共 {len(rows_all)} 个候选，已默认全选，点击「退出所选」执行")
        finally:
            await set_busy(False)

    def confirm_leave() -> None:
        selected = list(result_table.selected or [])
        if not selected:
            ui.notify("请先勾选要退出的对话", type="warning")
            return
        with ui.dialog() as dialog, ui.card():
            ui.label(f"确认退出以下 {len(selected)} 个对话？").classes(
                "text-lg font-semibold"
            )
            ui.label("退出后需要重新搜索/加群才能回来，请仔细核对！").classes(
                "text-sm text-orange-600"
            )
            with ui.column().classes("max-h-60 overflow-auto w-full"):
                for row in selected[:50]:
                    ui.label(
                        f"[{row['account']}] {row['title']}（{row['age_days']}天未更新）"
                    ).classes("text-sm")
                if len(selected) > 50:
                    ui.label(f"… 等共 {len(selected)} 个").classes("text-sm text-gray-500")
            with ui.row().classes("w-full justify-end"):
                ui.button("取消", on_click=dialog.close).props("flat")

                async def do_leave() -> None:
                    dialog.close()
                    await on_leave(selected)

                ui.button("确认退出", on_click=do_leave).props("color=negative")
        dialog.open()

    async def on_leave(selected: List[Dict]) -> None:
        if running["flag"]:
            return
        await set_busy(True)
        persist()
        session_dir = Path(session_dir_input.value or ".")
        by_account: Dict[str, List[Dict]] = {}
        for row in selected:
            by_account.setdefault(row["account"], []).append(row)
        try:
            for account, items in by_account.items():
                client = None
                try:
                    client = get_client(account, workdir=str(session_dir))
                    ok = await asyncio.wait_for(client.connect(), 30)
                    if not ok:
                        log(f"[{account}] 会话未授权，跳过 {len(items)} 个")
                        continue
                    log(f"[{account}] 开始批量退出 {len(items)} 个对话（并发 8）")
                    done_keys: set = set()
                    done_lock = asyncio.Lock()
                    sem = asyncio.Semaphore(8)

                    async def _do_leave(row: Dict) -> None:
                        async with sem:
                            success = await leave_one(
                                client, row["chat_id"], row["title"], log
                            )
                            if success:
                                async with done_lock:
                                    done_keys.add(row["key"])

                    await asyncio.gather(*[_do_leave(row) for row in items])
                    log(
                        f"[{account}] 批量退出完成：成功 {len(done_keys)}/{len(items)}"
                    )
                    if done_keys:
                        remain = [r for r in rows_all if r["key"] not in done_keys]
                        rows_all.clear()
                        rows_all.extend(remain)
                        result_table.rows = list(rows_all)
                        result_table.update()
                except asyncio.TimeoutError:
                    log(f"[{account}] 连接超时（30s），跳过")
                except Exception as exc:
                    log(f"[{account}] 退出失败: {exc}")
                finally:
                    if client is not None:
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
            log("退出流程结束")
        finally:
            await set_busy(False)

    with ui.card().classes("w-full shadow-md"):
        ui.label("批量退频道").classes("text-lg font-semibold")
        ui.label(
            "扫描所选账号的对话，列出超过指定天数没有新消息的频道（可选包含群组），"
            "默认全选后并发批量退出。自己是创建者的对话永远不会被退出。"
        ).classes("text-sm text-gray-500")

        with ui.row().classes("w-full items-end flex-nowrap"):
            session_dir_input = ui.input(
                label="会话目录", value=cfg.get("session_dir") or default_session_dir
            ).classes("w-40")
            days_input = ui.number(
                label="不更新天数阈值", value=cfg.get("days") or 7, min=1, step=1,
                format="%.0f",
            ).classes("w-36")
            include_groups = ui.checkbox(
                "包含群组", value=bool(cfg.get("include_groups"))
            )
            skip_pinned = ui.checkbox(
                "跳过置顶", value=bool(cfg.get("skip_pinned", True))
            )
            account_select = ui.select(
                label="账号（可多选）",
                options=[],
                multiple=True,
                with_input=True,
            ).classes("min-w-64 flex-1")
            scan_btn = ui.button("扫描预览", on_click=on_scan).props(
                "outline"
            ).classes("ml-auto")

        result_table = ui.table(
            columns=[
                {"name": "account", "label": "账号", "field": "account", "align": "left"},
                {"name": "title", "label": "名称", "field": "title", "align": "left"},
                {"name": "type", "label": "类型", "field": "type", "align": "left"},
                {
                    "name": "age_days",
                    "label": "未更新(天)",
                    "field": "age_days",
                    "align": "left",
                },
                {"name": "chat_id", "label": "chat_id", "field": "chat_id", "align": "left"},
            ],
            rows=[],
            row_key="key",
            selection="multiple",
        ).classes("w-full")

        with ui.row().classes("w-full items-center"):
            leave_btn = ui.button(
                "退出所选", on_click=confirm_leave
            ).props("color=negative outline")
            ui.label("勾选表格左侧复选框后操作；扫描结果默认不执行任何退出").classes(
                "text-xs text-gray-500"
            )

        log_area = ui.log(max_lines=300).classes("w-full h-48 mt-2")
        for line in log_lines:
            log_area.push(line)

        account_select.on("update:model-value", lambda: persist())
        refresh_options()

    return refresh_options
