"""自动退群：任务列表模式。

新建任务（选择账号、不更新天数阈值、保护关键字、自动退出开关），
启动后扫描所选账号超过 N 天未更新的频道（可选含群组）：
- 未命中保护关键字的频道：开关打开时自动退出（无需确认）；
- 命中保护关键字的频道：始终展示在任务结果里，可手动勾选后退出。
自己是创建者的对话永远不会被退出。
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List

from nicegui import ui
from pyrogram.errors import FloodWait, RPCError

from tg_signer.core import get_client, safe_disconnect
from tg_signer.utils import atomic_write_json
from tg_signer.webui.random_chat import (
    build_account_options,
    list_session_names,
)

logger = logging.getLogger(__name__)

CONFIG_NAME = "auto_leave_config.json"
LEAVE_TASKS_FILE = "webui_leave_tasks.json"

# 保护关键字（频道名命中即不自动退出，仅在结果中展示供手动勾选）
UNSELECT_KEYWORDS = ("抽奖", "嫩妹社", "野寻", "榜")

DEFAULT_KEYWORDS = "抽奖,嫩妹社,野寻,榜"


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
        logger.debug("读取自动退群配置文件失败，使用默认配置", exc_info=True)
    return defaults


def save_config(workdir, cfg: Dict) -> None:
    try:
        atomic_write_json(_config_path(workdir), cfg)
    except Exception:
        logger.debug("保存自动退群配置文件失败", exc_info=True)


def _tasks_path(workdir) -> Path:
    return Path(workdir) / LEAVE_TASKS_FILE


def load_leave_tasks(workdir) -> List[Dict]:
    """读取退频道任务列表；缺失时尝试从旧版单配置迁移出一条默认任务"""
    try:
        data = json.loads(_tasks_path(workdir).read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [t for t in data if isinstance(t, dict) and t.get("id")]
    except Exception:
        logger.debug("读取退频道任务列表失败，视为空", exc_info=True)
    cfg = load_config(workdir)
    if cfg.get("accounts"):
        return [
            {
                "id": f"leave_{datetime.now():%Y%m%d%H%M%S}",
                "name": "默认退频道任务",
                "accounts": list(cfg.get("accounts") or []),
                "days": int(cfg.get("days") or 7),
                "include_groups": bool(cfg.get("include_groups")),
                "skip_pinned": bool(cfg.get("skip_pinned", True)),
                "keywords": list(UNSELECT_KEYWORDS),
                "auto_leave": False,
            }
        ]
    return []


def save_leave_tasks(workdir, tasks: List[Dict]) -> None:
    try:
        atomic_write_json(_tasks_path(workdir), tasks)
    except Exception:
        logger.debug("保存退频道任务列表失败", exc_info=True)


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
        log(
            f"[{account}] 扫描完成，共 {scanned} 个对话，{len(found)} 个超过 {days} 天未更新"
        )
    except asyncio.TimeoutError:
        log(f"[{account}] 连接超时（30s），跳过")
    except Exception as exc:
        log(f"[{account}] 扫描失败: {exc}")
    finally:
        if client is not None:
            await safe_disconnect(client)
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


async def _leave_rows(
    account: str,
    session_dir: Path,
    rows: List[Dict],
    log: Callable[[str], None],
    should_stop: Callable[[], bool] | None = None,
) -> set:
    """按账号并发退出给定对话（并发 8），返回成功退出的 key 集合"""
    done: set = set()
    if not rows:
        return done
    client = None
    try:
        client = get_client(account, workdir=str(session_dir))
        ok = await asyncio.wait_for(client.connect(), 30)
        if not ok:
            log(f"[{account}] 会话未授权，跳过 {len(rows)} 个")
            return done
        log(f"[{account}] 开始批量退出 {len(rows)} 个对话（并发 8）")
        done_lock = asyncio.Lock()
        sem = asyncio.Semaphore(8)

        async def _do_leave(row: Dict) -> None:  # noqa: B023
            if should_stop is not None and should_stop():
                return
            async with sem:  # noqa: B023
                success = await leave_one(
                    client,  # noqa: B023
                    row["chat_id"],
                    row["title"],
                    log,  # noqa: B023
                )
                if success:
                    async with done_lock:  # noqa: B023
                        done.add(row["key"])  # noqa: B023

        await asyncio.gather(*[_do_leave(row) for row in rows])
        log(f"[{account}] 批量退出完成：成功 {len(done)}/{len(rows)}")
    except asyncio.TimeoutError:
        log(f"[{account}] 连接超时（30s），跳过")
    except Exception as exc:
        log(f"[{account}] 退出失败: {exc}")
    finally:
        if client is not None:
            await safe_disconnect(client)
    return done


def _split_keywords(text: str) -> List[str]:
    return [
        kw.strip() for kw in (text or "").replace("，", ",").split(",") if kw.strip()
    ]


def auto_leave_block(workdir, default_session_dir: str = ".") -> Callable[[], None]:
    """构建「批量退频道」标签页内容，返回刷新函数以纳入全局刷新。"""
    session_dir = Path(default_session_dir or ".")
    tasks = load_leave_tasks(workdir)
    if tasks:
        save_leave_tasks(workdir, tasks)  # 旧配置迁移后立即落盘

    # 每个任务（执行记录）的运行时状态：running/stop/rows/selected/status/auto_left
    task_states: Dict[str, Dict] = {
        t["id"]: {
            "running": False,
            "stop": False,
            "rows": [],
            "selected": set(),
            "status": str(t.get("status") or "—"),
            "auto_left": 0,
        }
        for t in tasks
    }
    log_lines: List[str] = []
    toggle_counts: Dict[str, ui.label | None] = {}

    def log(msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        log_lines.append(line)
        if len(log_lines) > 300:
            del log_lines[:-300]
        try:
            log_area.push(line)
        except Exception:
            logger.debug("推送日志到界面日志区失败", exc_info=True)

    def find_task(task_id: str) -> Dict | None:
        return next((t for t in tasks if t["id"] == task_id), None)

    list_container: ui.column | None = None

    def _kw_hit(task: Dict, title: str) -> bool:
        return any(kw in str(title or "") for kw in (task.get("keywords") or []))

    async def run_task(task_id: str) -> None:
        task = find_task(task_id)
        st = task_states.get(task_id)
        if not task or not st or st["running"]:
            return
        accounts = list(task.get("accounts") or [])
        if not accounts:
            ui.notify("请先为该任务选择账号（删除后新建）", type="warning")
            return
        st["running"] = True
        st["stop"] = False
        st["rows"].clear()
        st["selected"].clear()
        st["auto_left"] = 0
        st["status"] = "扫描中"
        render_list()
        log(
            f"[{task['name']}] 启动：{len(accounts)} 个账号，阈值 {task.get('days')} 天"
        )
        auto_leave_on = bool(task.get("auto_leave"))
        try:
            for account in accounts:
                if st["stop"]:
                    log(f"[{task['name']}] 已停止，跳过账号 {account}")
                    break
                found = await scan_stale_chats(
                    account,
                    session_dir,
                    int(task.get("days") or 7),
                    bool(task.get("include_groups")),
                    bool(task.get("skip_pinned", True)),
                    log,
                )
                if auto_leave_on:
                    auto_rows = [r for r in found if not _kw_hit(task, r["title"])]
                    if auto_rows:
                        done = await _leave_rows(
                            account,
                            session_dir,
                            auto_rows,
                            log,
                            should_stop=lambda: st["stop"],
                        )
                        st["auto_left"] += len(done)
                    kept = [r for r in found if _kw_hit(task, r["title"])]
                    log(
                        f"[{account}] 命中保护关键字 {len(kept)} 个（待手动确认），"
                        f"未命中 {len(auto_rows)} 个自动退出"
                    )
                    st["rows"].extend(kept)
                else:
                    st["rows"].extend(found)
                render_list()
            if st["stop"]:
                st["status"] = "已停止"
            else:
                st["status"] = (
                    f"完成：自动退出 {st['auto_left']} 个，待确认 {len(st['rows'])} 个"
                )
            log(f"[{task['name']}] {st['status']}")
        except Exception as exc:
            st["status"] = f"异常：{exc}"
            log(f"[{task['name']}] 运行异常: {exc}")
            logger.debug("退频道任务运行异常", exc_info=True)
        finally:
            st["running"] = False
            # 终态写回记录并持久化，刷新页面后仍可见
            task["status"] = st["status"]
            save_leave_tasks(workdir, tasks)
            render_list()

    def stop_task(task_id: str) -> None:
        st = task_states.get(task_id)
        if st:
            st["stop"] = True

    async def leave_selected(task_id: str) -> None:
        task = find_task(task_id)
        st = task_states.get(task_id)
        if not task or not st or st["running"]:
            return
        selected = [r for r in st["rows"] if r["key"] in st["selected"]]
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
                    ui.label(f"… 等共 {len(selected)} 个").classes(
                        "text-sm text-gray-500"
                    )
            with ui.row().classes("w-full justify-end"):
                ui.button("取消", on_click=dialog.close).props("flat")

                async def do_leave() -> None:
                    dialog.close()
                    st["running"] = True
                    st["status"] = "退出中"
                    render_list()
                    try:
                        by_account: Dict[str, List[Dict]] = {}
                        for row in selected:
                            by_account.setdefault(row["account"], []).append(row)
                        done_keys: set = set()
                        for account, items in by_account.items():
                            done_keys |= await _leave_rows(
                                account, session_dir, items, log
                            )
                        if done_keys:
                            st["rows"] = [
                                r for r in st["rows"] if r["key"] not in done_keys
                            ]
                            st["selected"].difference_update(done_keys)
                        st["status"] = f"退出完成，剩余待确认 {len(st['rows'])} 个"
                    except Exception as exc:
                        st["status"] = f"异常：{exc}"
                        log(f"[{task['name']}] 退出异常: {exc}")
                        logger.debug("退频道手动退出异常", exc_info=True)
                    finally:
                        st["running"] = False
                        task["status"] = st["status"]
                        save_leave_tasks(workdir, tasks)
                        render_list()

                ui.button("确认退出", on_click=do_leave).props("color=negative")
        dialog.open()

    def delete_task(task_id: str) -> None:
        task = find_task(task_id)
        st = task_states.get(task_id)
        if not task:
            return
        if st and st["running"]:
            ui.notify("任务运行中，请先停止再删除", type="warning")
            return
        with ui.dialog() as dialog, ui.card():

            def do_delete() -> None:
                dialog.close()
                tasks.remove(task)
                task_states.pop(task_id, None)
                save_leave_tasks(workdir, tasks)
                log(f"已删除任务：{task['name']}")
                render_list()

            ui.label(f"确认删除任务「{task['name']}」？").classes(
                "text-lg font-semibold"
            )
            with ui.row().classes("w-full justify-end"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button("删除", on_click=do_delete).props("color=negative")
        dialog.open()

    def open_create_dialog(prefill: Dict | None = None) -> None:
        prefill = prefill or {}
        names = list_session_names(session_dir, workdir)
        with (
            ui.dialog() as dialog,
            ui.card().classes("w-[520px] max-w-[96vw] max-h-[88vh] overflow-auto"),
        ):
            ui.label("复制退频道任务" if prefill else "新建退频道任务").classes(
                "text-lg font-semibold"
            )
            default_name = (
                f"{prefill.get('name', '')} 副本"
                if prefill
                else f"退频道任务 {datetime.now():%m-%d %H:%M}"
            )
            name_input = ui.input("任务名称", value=default_name).classes("w-full")
            account_sel = ui.select(
                label="账号（可多选）",
                options=build_account_options(session_dir, names),
                multiple=True,
                with_input=True,
                value=[a for a in (prefill.get("accounts") or []) if a in names]
                or None,
            ).classes("w-full")
            with ui.row().classes("w-full items-end flex-wrap gap-2"):
                days_input = ui.number(
                    label="不更新天数阈值",
                    value=int(prefill.get("days") or 7),
                    min=1,
                    step=1,
                    format="%.0f",
                ).classes("w-36")
                ig = ui.checkbox("包含群组", value=bool(prefill.get("include_groups")))
                sp = ui.checkbox(
                    "跳过置顶", value=bool(prefill.get("skip_pinned", True))
                )
            kw_input = ui.input(
                "保护关键字（逗号分隔）",
                value=",".join(prefill.get("keywords") or []) or DEFAULT_KEYWORDS,
            ).classes("w-full")
            ui.label(
                "未命中关键字的频道将在启动后自动退出；命中关键字的频道仅展示，需手动勾选退出"
            ).classes("text-xs text-gray-500")
            auto_cb = ui.checkbox(
                "自动退出未命中关键字的频道",
                value=bool(prefill.get("auto_leave")),
            )
            with ui.row().classes("w-full justify-end"):

                def do_create() -> None:
                    accounts = list(account_sel.value or [])
                    if not accounts:
                        ui.notify("请至少选择一个账号", type="warning")
                        return
                    task = {
                        "id": f"leave_{datetime.now():%Y%m%d%H%M%S%f}",
                        "name": str(name_input.value or "").strip() or "退频道任务",
                        "accounts": accounts,
                        "days": max(1, int(days_input.value or 7)),
                        "include_groups": bool(ig.value),
                        "skip_pinned": bool(sp.value),
                        "keywords": _split_keywords(str(kw_input.value or "")),
                        "auto_leave": bool(auto_cb.value),
                        "status": "—",
                    }
                    tasks.append(task)
                    task_states[task["id"]] = {
                        "running": False,
                        "stop": False,
                        "rows": [],
                        "selected": set(),
                        "status": "—",
                        "auto_left": 0,
                    }
                    save_leave_tasks(workdir, tasks)
                    dialog.close()
                    render_list()
                    # 每次执行都是一条新记录，创建后立即启动
                    run_task(task["id"])

                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button("启动执行", on_click=do_create).props("color=primary")
        dialog.open()

    def render_task_card(container, task: Dict) -> None:
        st = task_states[task["id"]]
        with container:
            with ui.card().classes("w-full leave-task-card border"):
                with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                    ui.label(task["name"]).classes("text-base font-semibold")
                    status_text = str(st["status"] or "—")
                    (
                        ui.label(status_text).classes("text-xs text-blue-600")
                        if st["running"]
                        else ui.label(status_text).classes("text-xs text-gray-500")
                    )
                    with ui.row().classes("ml-auto gap-1"):
                        ui.button(
                            "停止", on_click=lambda t=task["id"]: stop_task(t)
                        ).props("outline dense color=warning").classes(
                            "min-w-0"
                        ).bind_visibility_from(st, "running")
                        ui.button(
                            "复制",
                            on_click=lambda t=task: open_create_dialog(
                                {k: v for k, v in t.items() if k != "status"}
                            ),
                        ).props("outline dense color=primary")
                        ui.button(
                            "删除", on_click=lambda t=task["id"]: delete_task(t)
                        ).props("outline dense color=negative")
                ui.label(
                    f"账号：{'、'.join(task.get('accounts') or []) or '未选择'}　"
                    f"阈值：{task.get('days')} 天　"
                    f"{'含群组，' if task.get('include_groups') else ''}"
                    f"{'跳过置顶，' if task.get('skip_pinned', True) else ''}"
                    f"{'自动退出未命中，' if task.get('auto_leave') else '仅展示，'}"
                    f"关键字：{'、'.join(task.get('keywords') or []) or '无'}"
                ).classes("text-xs text-gray-600")
                # 结果区：命中关键字的频道（或自动退出关闭时的全部候选）
                if st["rows"]:

                    def _on_toggle(e, k: str, tid=task["id"]) -> None:
                        sts = task_states[tid]
                        if e.value:
                            sts["selected"].add(k)
                        else:
                            sts["selected"].discard(k)
                        if toggle_counts.get(tid) is not None:
                            toggle_counts[
                                tid
                            ].text = f"已选 {len(sts['selected'])}/{len(sts['rows'])}"

                    with ui.row().classes("w-full items-center gap-2"):

                        def _select_all(t=task) -> None:
                            sts = task_states[t["id"]]
                            sts["selected"].update(r["key"] for r in sts["rows"])
                            render_list()

                        def _select_none(t=task) -> None:
                            sts = task_states[t["id"]]
                            sts["selected"].clear()
                            render_list()

                        ui.button("全选", on_click=_select_all).props("flat dense")
                        ui.button("全不选", on_click=_select_none).props("flat dense")
                        toggle_counts[task["id"]] = ui.label(
                            f"已选 {len(st['selected'])}/{len(st['rows'])}"
                        ).classes("text-xs text-gray-500 ml-auto")
                    for row in st["rows"]:
                        key = row["key"]
                        with ui.row().classes(
                            "w-full items-start gap-2 no-wrap p-2 rounded border"
                        ):
                            ui.checkbox(
                                value=key in st["selected"],
                                on_change=lambda e, k=key, t=task["id"]: _on_toggle(
                                    e, k, t
                                ),
                            ).props("dense").classes("mt-0.5")
                            with ui.column().classes("gap-0 min-w-0"):
                                ui.label(str(row["title"])).classes(
                                    "text-sm font-semibold"
                                ).style("overflow-wrap: anywhere;")
                                ui.label(
                                    f"账号：{row['account']}　类型：{row['type']}　"
                                    f"未更新 {row['age_days']} 天"
                                ).classes("text-xs text-gray-500")
                    with ui.row().classes("w-full items-center"):
                        ui.button(
                            "退出所选", on_click=lambda t=task["id"]: leave_selected(t)
                        ).props("color=negative outline dense")
                        ui.label("退出不可撤销，请仔细核对").classes(
                            "text-xs text-gray-500"
                        )

    def render_list() -> None:
        if list_container is None:
            return
        toggle_counts.clear()
        list_container.clear()
        with list_container:
            if not tasks:
                ui.label("暂无执行记录，点击「新建任务」开始").classes(
                    "text-sm text-gray-400"
                )
            # 最新执行的记录显示在最上方
            for task in reversed(tasks):
                if task["id"] not in task_states:
                    task_states[task["id"]] = {
                        "running": False,
                        "stop": False,
                        "rows": [],
                        "selected": set(),
                        "status": str(task.get("status") or "—"),
                        "auto_left": 0,
                    }
                render_task_card(list_container, task)

    ui.label("批量退频道").classes("text-lg font-semibold")
    ui.label(
        "扫描所选账号超过指定天数没有新消息的频道（可选包含群组）并批量退出。"
        "开启「自动退出未命中关键字」后，未命中保护关键字的频道自动退出；"
        "命中关键字的频道展示在任务结果中，需手动勾选退出。自己是创建者的对话永远不会被退出。"
    ).classes("text-sm text-gray-500")

    with ui.row().classes("w-full items-center"):
        ui.button("新建任务", on_click=lambda: open_create_dialog()).props(
            "color=primary"
        )
        ui.label("每次执行都会在列表新增一条记录，可复制配置再次执行").classes(
            "text-xs text-gray-500 ml-auto"
        )

    list_container = ui.column().classes("w-full gap-2")
    render_list()

    log_area = ui.log(max_lines=300).classes("w-full h-48 mt-2")
    for line in log_lines:
        log_area.push(line)

    def refresh() -> None:
        render_list()

    return refresh
