"""自动退群：任务列表模式。

新建任务（选择账号、不更新天数阈值、扫描范围、包含/保护关键字、自动退出开关），
启动后按扫描范围（仅频道/仅群组/频道+群组）扫描所选账号超过 N 天未更新的对话：
- 填写包含关键字时，标题命中任一关键字（或关系）的对话才进入扫描结果（留空不限制）；
- 未命中保护关键字的对话：开关打开时自动退出（无需确认，仅扫描范围为「仅频道」时可选）；
- 命中保护关键字的对话：始终展示在任务结果里，可手动勾选后退出。
自己是创建者的对话永远不会被退出。
"""

import asyncio
import html as html_lib
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List

from nicegui import ui
from pyrogram.errors import FloodWait, RPCError

from tg_signer.core import get_client, safe_disconnect
from tg_signer.utils import atomic_write_json
from tg_signer.webui.common import (
    add_dialog_close_button,
    attach_select_all,
    copy_text_to_clipboard,
    format_duration,
    with_select_all,
)
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


def _scan_types_of(obj: Dict) -> str:
    """读取扫描范围；旧配置只有 include_groups 布尔时按其推导（True→both）"""
    st = str(obj.get("scan_types") or "")
    if st in ("channel", "group", "both"):
        return st
    return "both" if obj.get("include_groups") else "channel"


def load_leave_tasks(workdir) -> List[Dict]:
    """读取退频道任务列表；缺失时尝试从旧版单配置迁移出一条默认任务"""
    try:
        data = json.loads(_tasks_path(workdir).read_text(encoding="utf-8"))
        if isinstance(data, list):
            tasks = [t for t in data if isinstance(t, dict) and t.get("id")]
            for t in tasks:
                t.setdefault("scan_types", _scan_types_of(t))
                t.setdefault("include_keywords", [])
            return tasks
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
                "scan_types": _scan_types_of(cfg),
                "skip_pinned": bool(cfg.get("skip_pinned", True)),
                "keywords": list(UNSELECT_KEYWORDS),
                "include_keywords": [],
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
    scan_types: str = "both",
    skip_pinned: bool = True,
    log: Callable[[str], None] = None,
) -> List[Dict]:
    """扫描单个账号，返回超过 days 天不更新的对话列表（只读，不做任何退出）

    scan_types: 扫描范围——"channel" 仅频道 / "group" 仅群组 / "both" 频道+群组
    """
    scan_types = str(scan_types or "both")
    want_channel = scan_types in ("channel", "both")
    want_group = scan_types in ("group", "both")
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
            if not ((is_channel and want_channel) or (is_group and want_group)):
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
    # rows（命中/待确认频道）从任务记录恢复，任务完成后重新打开仍可见
    def _default_state(task: Dict) -> Dict:
        return {
            "running": False,
            "stop": False,
            "rows": [dict(r) for r in (task.get("rows") or []) if isinstance(r, dict)],
            "selected": set(),
            "status": str(task.get("status") or "—"),
            "auto_left": 0,
        }

    task_states: Dict[str, Dict] = {t["id"]: _default_state(t) for t in tasks}
    log_lines: List[str] = []
    # 页面级持久弹窗的引用与状态（弹窗不在事件槽位里动态创建）
    detail_refs: Dict = {"task_id": None, "body": None, "count": None}
    leave_confirm: Dict = {"task_id": None, "selected": []}
    delete_state: Dict = {"task_id": None}
    cards_limit = {"n": 5}

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

    def _seq_of(task_id: str) -> int:
        # 序号按创建顺序编号（最早的任务为 1，与发言任务一致）
        return next(
            (idx for idx, t in enumerate(tasks, start=1) if t["id"] == task_id), 0
        )

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
        st["start_ts"] = time.time()
        task["start_time"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        task["duration"] = ""
        render_list()
        log(
            f"[{task['name']}] 启动：{len(accounts)} 个账号，阈值 {task.get('days')} 天"
        )
        auto_leave_on = bool(task.get("auto_leave"))
        if auto_leave_on and _scan_types_of(task) != "channel":
            # 扫描范围包含群组时不允许自动退出（仅频道可自动退出）
            log(f"[{task['name']}] 扫描范围包含群组，自动退出不可用，仅扫描记录")
            auto_leave_on = False
        try:
            for account in accounts:
                if st["stop"]:
                    log(f"[{task['name']}] 已停止，跳过账号 {account}")
                    break
                found = await scan_stale_chats(
                    account,
                    session_dir,
                    int(task.get("days") or 7),
                    _scan_types_of(task),
                    bool(task.get("skip_pinned", True)),
                    log,
                )
                inc_kws = [str(k) for k in (task.get("include_keywords") or []) if k]
                if inc_kws:
                    before = len(found)
                    found = [
                        r for r in found if any(k in str(r["title"]) for k in inc_kws)
                    ]
                    log(f"[{account}] 包含关键字过滤：{before} -> {len(found)} 个")
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
            # 终态与命中频道写回记录并持久化，刷新页面后仍可见
            task["status"] = st["status"]
            task["rows"] = [dict(r) for r in st["rows"]]
            if st.get("start_ts"):
                task["duration"] = format_duration(time.time() - st["start_ts"])
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
        # 填充页面级持久确认弹窗（不在事件槽位里动态创建弹窗）
        leave_confirm["task_id"] = task_id
        leave_confirm["selected"] = selected
        confirm_title.text = f"确认退出以下 {len(selected)} 个对话？"
        confirm_list.clear()
        with confirm_list:
            for row in selected[:50]:
                ui.label(
                    f"[{row['account']}] {row['title']}（{row['age_days']}天未更新）"
                ).classes("text-sm")
            if len(selected) > 50:
                ui.label(f"… 等共 {len(selected)} 个").classes("text-sm text-gray-500")
        confirm_dialog.open()

    async def _do_leave_selected() -> None:
        task_id = leave_confirm.get("task_id")
        task = find_task(task_id) if task_id else None
        st = task_states.get(task_id) if task_id else None
        confirm_dialog.close()
        if not task or not st:
            return
        selected = list(leave_confirm.get("selected") or [])
        if not selected:
            return
        st["running"] = True
        st["status"] = "退出中"
        render_list()
        try:
            by_account: Dict[str, List[Dict]] = {}
            for row in selected:
                by_account.setdefault(row["account"], []).append(row)
            done_keys: set = set()
            for account, items in by_account.items():
                done_keys |= await _leave_rows(account, session_dir, items, log)
            if done_keys:
                st["rows"] = [r for r in st["rows"] if r["key"] not in done_keys]
                st["selected"].difference_update(done_keys)
            st["status"] = f"退出完成，剩余待确认 {len(st['rows'])} 个"
        except Exception as exc:
            st["status"] = f"异常：{exc}"
            log(f"[{task['name']}] 退出异常: {exc}")
            logger.debug("退频道手动退出异常", exc_info=True)
        finally:
            st["running"] = False
            task["status"] = st["status"]
            task["rows"] = [dict(r) for r in st["rows"]]
            save_leave_tasks(workdir, tasks)
            _refresh_detail(task_id)
            render_list()

    def delete_task(task_id: str) -> None:
        task = find_task(task_id)
        st = task_states.get(task_id)
        if not task:
            return
        if st and st["running"]:
            ui.notify("任务运行中，请先停止再删除", type="warning")
            return
        # 填充页面级持久确认弹窗（不在事件槽位里动态创建弹窗）
        delete_state["task_id"] = task_id
        delete_label.text = f"确认删除任务 #{_seq_of(task['id'])}？"
        delete_dialog.open()

    def _do_delete() -> None:
        tid = delete_state.get("task_id")
        task = find_task(tid) if tid else None
        delete_dialog.close()
        if not task:
            return
        seq = _seq_of(task["id"])
        tasks.remove(task)
        task_states.pop(task["id"], None)
        if detail_refs.get("task_id") == task["id"]:
            detail_refs.update({"task_id": None, "body": None, "count": None})
        save_leave_tasks(workdir, tasks)
        log(f"已删除任务 #{seq}")
        render_list()

    def open_create_dialog(prefill: Dict | None = None) -> None:
        prefill = prefill or {}
        names = list_session_names(session_dir, workdir)
        # 填充页面级持久新建/复制弹窗（不在事件槽位里动态创建弹窗）
        create_title.text = "复制退频道任务" if prefill else "新建退频道任务"
        account_sel.options = with_select_all(build_account_options(session_dir, names))
        account_sel.value = [
            a for a in (prefill.get("accounts") or []) if a in names
        ] or None
        account_sel.update()
        days_input.value = int(prefill.get("days") or 7)
        scope_sel.value = _scan_types_of(prefill)
        sp.value = bool(prefill.get("skip_pinned", True))
        kw_input.value = ",".join(prefill.get("keywords") or []) or DEFAULT_KEYWORDS
        ik_input.value = ",".join(prefill.get("include_keywords") or [])
        auto_cb.value = bool(prefill.get("auto_leave"))
        _sync_auto_cb()
        create_dialog.open()

    async def _do_create() -> None:
        accounts = list(account_sel.value or [])
        if not accounts:
            ui.notify("请至少选择一个账号", type="warning")
            return
        scope = str(scope_sel.value or "both")
        task = {
            "id": f"leave_{datetime.now():%Y%m%d%H%M%S%f}",
            "name": f"退频道任务 {datetime.now():%m-%d %H:%M}",
            "accounts": accounts,
            "days": max(1, int(days_input.value or 7)),
            "scan_types": scope,
            "skip_pinned": bool(sp.value),
            "keywords": _split_keywords(str(kw_input.value or "")),
            "include_keywords": _split_keywords(str(ik_input.value or "")),
            # 扫描范围包含群组时不允许自动退出
            "auto_leave": bool(auto_cb.value) and scope == "channel",
            "status": "—",
        }
        tasks.append(task)
        task_states[task["id"]] = _default_state(task)
        save_leave_tasks(workdir, tasks)
        create_dialog.close()
        render_list()
        # 每次执行都是一条新记录，创建后立即启动
        await run_task(task["id"])

    def _short_status(st: Dict) -> str:
        full = str(st.get("status") or "—")
        if st.get("running"):
            return "运行中"
        if full.startswith("完成") or full.startswith("退出完成"):
            return "已完成"
        if full == "已停止":
            return "已停止"
        if full.startswith("异常"):
            return "失败"
        return "—"

    def _status_color(status: str) -> str:
        if status == "已完成":
            return "positive"
        if status == "失败":
            return "negative"
        if status == "运行中":
            return "info"
        return "grey-6"

    def _event_row(e) -> Dict | None:
        """从 Quasar 表格事件对象中取出行 dict（事件参数可能被包成 list）。"""
        row = getattr(e, "args", None)
        if isinstance(row, list) and row and isinstance(row[0], dict):
            row = row[0]
        return row if isinstance(row, dict) else None

    def _prefill_of(task: Dict) -> Dict:
        # 复制仅取配置，不带上次状态与命中频道
        return {k: v for k, v in task.items() if k not in ("status", "rows")}

    def _dup_by_id(task_id: str) -> None:
        task = find_task(task_id)
        if task:
            open_create_dialog(_prefill_of(task))

    # ---- 任务明细弹窗：展示命中/待确认频道，可勾选手动退出 ----

    def _detail_on_toggle(e, key: str, task_id: str) -> None:
        sts = task_states.get(task_id)
        if not sts:
            return
        if e.value:
            sts["selected"].add(key)
        else:
            sts["selected"].discard(key)
        cnt = detail_refs.get("count")
        if cnt is not None:
            cnt.text = f"已选 {len(sts['selected'])}/{len(sts['rows'])}"

    def _detail_select(task_id: str, all_on: bool) -> None:
        sts = task_states.get(task_id)
        if not sts:
            return
        if all_on:
            sts["selected"].update(r["key"] for r in sts["rows"])
        else:
            sts["selected"].clear()
        render_detail_rows(task_id)

    def render_detail_rows(task_id: str) -> None:
        body = detail_refs.get("body")
        if body is None:
            return
        st = task_states.get(task_id) or {"rows": [], "selected": set()}
        body.clear()
        with body:
            rows = st["rows"]
            if not rows:
                ui.label("暂无待确认的频道").classes("text-sm text-gray-400")
                return
            with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                ui.button(
                    "全选", on_click=lambda t=task_id: _detail_select(t, True)
                ).props("flat dense")
                ui.button(
                    "全不选", on_click=lambda t=task_id: _detail_select(t, False)
                ).props("flat dense")
                ui.button(
                    "退出所选", on_click=lambda t=task_id: leave_selected(t)
                ).props("outline dense color=negative")
                detail_refs["count"] = ui.label(
                    f"已选 {len(st['selected'])}/{len(rows)}"
                ).classes("text-xs text-gray-500 ml-auto")
            # 按频道聚合（chat_id 跨账号相同），组间按未更新天数由大到小排序
            groups: Dict = {}
            for row in rows:
                groups.setdefault(row.get("chat_id", row["key"]), []).append(row)
            ordered = sorted(
                groups.values(),
                key=lambda g: max(r["age_days"] for r in g),
                reverse=True,
            )
            for group in ordered:
                group.sort(
                    key=lambda r: (-r["age_days"], str(r["account"]))
                )
                head = group[0]
                group_keys = {r["key"] for r in group}
                group_days = max(r["age_days"] for r in group)
                all_on = group_keys <= st["selected"]

                def _toggle_group(e, keys=group_keys, t=task_id) -> None:
                    sts = task_states.get(t)
                    if not sts:
                        return
                    if e.value:
                        sts["selected"].update(keys)
                    else:
                        sts["selected"].difference_update(keys)
                    render_detail_rows(t)

                with ui.column().classes("w-full gap-1 p-2 rounded border"):
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        ui.checkbox(
                            value=all_on, on_change=_toggle_group
                        ).props("dense")
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label(str(head["title"])).classes(
                                "text-sm font-semibold"
                            ).style("overflow-wrap: anywhere;")
                            ui.label(
                                f"{head['type']}　{len(group)} 个账号"
                                + ("" if len(group) == 1 else f"　最多 {group_days} 天未更新")
                            ).classes("text-xs text-gray-500")
                    for row in group:
                        key = row["key"]
                        with ui.row().classes(
                            "w-full items-center gap-2 no-wrap pl-6"
                        ):
                            ui.checkbox(
                                value=key in st["selected"],
                                on_change=lambda e, k=key, t=task_id: _detail_on_toggle(
                                    e, k, t
                                ),
                            ).props("dense")
                            ui.label(
                                f"{row['account']}（{row['age_days']}天未更新）"
                            ).classes("text-xs").style("overflow-wrap: anywhere;")
            ui.label("退出不可撤销，请仔细核对").classes("text-xs text-gray-500")

    def open_detail_dialog(task_id: str) -> None:
        task = find_task(task_id)
        if not task:
            return
        if task_id not in task_states:
            task_states[task_id] = _default_state(task)
        # 填充页面级持久明细弹窗（不在事件槽位里动态创建弹窗）
        detail_refs["task_id"] = task_id
        detail_refs["body"] = detail_body
        detail_title.text = f"任务明细 — #{_seq_of(task['id'])}"
        render_detail_rows(task_id)
        detail_dialog.open()

    def _refresh_detail(task_id: str) -> None:
        if (
            detail_refs.get("task_id") == task_id
            and detail_refs.get("body") is not None
        ):
            try:
                render_detail_rows(task_id)
            except Exception:
                logger.debug("刷新任务明细弹窗失败", exc_info=True)

    # ---- 任务列表：桌面表格 + 移动端卡片双渲染 ----

    def render_mobile_cards(rows: List[Dict]) -> None:
        cards_container.clear()
        with cards_container:
            shown = rows[: cards_limit["n"]]
            if not shown:
                ui.label("暂无执行记录，点击「新建任务」开始").classes(
                    "text-sm text-gray-400"
                )
            for row in shown:
                with ui.card().classes("w-full mobile-list-card border"):
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        ui.badge(row["status"]).props(
                            f"color={_status_color(row['status'])}"
                        )
                        ui.label(f"任务 #{row['seq']}").classes(
                            "text-base font-semibold"
                        )
                    ui.label(f"账号：{row['accounts']}").classes(
                        "text-xs text-gray-600"
                    )
                    ui.label(
                        f"执行时间：{row['start_time']}　执行时长：{row['duration']}"
                    ).classes("text-xs text-gray-600")
                    ui.html(
                        '<div class="text-xs text-gray-700" '
                        f'data-runres="{html_lib.escape(str(row["id"] or ""), quote=True)}" '
                        'style="white-space: pre-line; overflow-wrap: anywhere;">'
                        f'{html_lib.escape(str(row["result"] or ""))}</div>'
                    )
                    with ui.row().classes("w-full mobile-card-actions"):
                        ui.button(
                            "复制", on_click=lambda r=row: _dup_by_id(str(r["id"]))
                        ).props("flat dense color=primary")
                        ui.button(
                            "明细",
                            on_click=lambda r=row: open_detail_dialog(str(r["id"])),
                        ).props("flat dense color=grey-8")
                        if row["running"]:
                            ui.button(
                                "停止",
                                on_click=lambda r=row: stop_task(str(r["id"])),
                            ).props("flat dense color=negative")
                        ui.button(
                            "删除", on_click=lambda r=row: delete_task(str(r["id"]))
                        ).props("flat dense color=negative")
            if len(rows) > cards_limit["n"]:

                def _more() -> None:
                    cards_limit["n"] += 10
                    render_list()

                ui.button(f"展开更多（共 {len(rows)} 条）", on_click=_more).props(
                    "flat dense"
                ).classes("w-full")

    def render_list() -> None:
        # 最新执行的记录显示在最上方；序号按创建顺序编号（最早的任务为 1，与发言任务一致）
        rows = []
        for task in reversed(tasks):
            if task["id"] not in task_states:
                task_states[task["id"]] = _default_state(task)
            st = task_states[task["id"]]
            rows.append(
                {
                    "id": task["id"],
                    "seq": _seq_of(task["id"]),
                    "start_time": str(task.get("start_time") or "") or "—",
                    "duration": (
                        format_duration(time.time() - st["start_ts"])
                        if st.get("running") and st.get("start_ts")
                        else (str(task.get("duration") or "") or "—")
                    ),
                    "accounts": "、".join(str(a) for a in (task.get("accounts") or []))
                    or "未选择",
                    "status": _short_status(st),
                    "result": str(st.get("status") or "—"),
                    "running": bool(st.get("running")),
                }
            )
        leave_table.rows = rows
        leave_table.update()
        render_mobile_cards(rows)

    def _on_leave_dup(e) -> None:
        row = _event_row(e)
        if row and row.get("id"):
            _dup_by_id(str(row["id"]))

    def _on_leave_detail(e) -> None:
        row = _event_row(e)
        if row and row.get("id"):
            open_detail_dialog(str(row["id"]))

    def _on_leave_stop(e) -> None:
        row = _event_row(e)
        if row and row.get("id"):
            stop_task(str(row["id"]))

    def _on_leave_del(e) -> None:
        row = _event_row(e)
        if row and row.get("id"):
            delete_task(str(row["id"]))

    ui.label("批量退频道").classes("text-lg font-semibold")
    ui.label(
        "扫描所选账号超过指定天数没有新消息的频道（可选包含群组）并批量退出。"
        "开启「自动退出未命中关键字」后，未命中保护关键字的频道自动退出；"
        "命中关键字的频道在「明细」中展示，需手动勾选退出。自己是创建者的对话永远不会被退出。"
    ).classes("text-sm text-gray-500")

    with ui.row().classes("w-full items-center"):
        ui.button("新建任务", on_click=lambda: open_create_dialog()).props(
            "color=primary"
        )
        ui.label(
            "每次执行都会在列表新增一条记录，可复制配置再次执行；点击「明细」查看命中的频道"
        ).classes("text-xs text-gray-500 ml-auto")

    leave_table = (
        ui.table(
            columns=[
                {
                    "name": "seq",
                    "label": "序号",
                    "field": "seq",
                    "align": "left",
                },
                {
                    "name": "start_time",
                    "label": "执行时间",
                    "field": "start_time",
                    "align": "left",
                },
                {
                    "name": "duration",
                    "label": "执行时长",
                    "field": "duration",
                    "align": "left",
                },
                {
                    "name": "accounts",
                    "label": "账号",
                    "field": "accounts",
                    "align": "left",
                },
                {
                    "name": "status",
                    "label": "状态",
                    "field": "status",
                    "align": "left",
                },
                {
                    "name": "result",
                    "label": "结果",
                    "field": "result",
                    "align": "left",
                    "style": "min-width: 280px;",
                    "classes": "result-col",
                },
                {
                    "name": "actions",
                    "label": "操作",
                    "field": "actions",
                    "align": "left",
                    "classes": "col-actions",
                    "style": "white-space: nowrap;",
                },
            ],
            rows=[],
            pagination=5,
        )
        .classes("w-full tgs-desktop-table")
        .props("flat dense")
    )
    # 状态列渲染为彩色徽章：已完成=绿、失败=红、运行中=蓝、其他=灰
    leave_table.add_slot(
        "body-cell-status",
        '<q-td :props="props">'
        "<q-badge :color=\"props.row.status === '已完成' ? 'positive' : "
        "props.row.status === '失败' ? 'negative' : "
        "props.row.status === '运行中' ? 'info' : 'grey-6'\">"
        "{{ props.row.status }}</q-badge></q-td>",
    )
    # 操作列：复制 / 明细（命中频道）/ 停止（运行中）/ 删除
    leave_table.add_slot(
        "body-cell-actions",
        '<q-td :props="props" class="col-actions">'
        '<q-btn flat dense color="primary" '
        'label="复制" @click="$parent.$emit(\'leaveDup\', props.row)" />'
        '<q-btn flat dense color="grey-8" '
        'label="明细" @click="$parent.$emit(\'leaveDetail\', props.row)" />'
        '<q-btn v-if="props.row.running" flat dense color="negative" '
        'label="停止" @click="$parent.$emit(\'leaveStop\', props.row)" />'
        '<q-btn flat dense color="negative" '
        'label="删除" @click="$parent.$emit(\'leaveDel\', props.row)" />'
        "</q-td>",
    )
    leave_table.on("leaveDup", _on_leave_dup)
    leave_table.on("leaveDetail", _on_leave_detail)
    leave_table.on("leaveStop", _on_leave_stop)
    leave_table.on("leaveDel", _on_leave_del)
    # 移动端（<=640px）用纵向卡片列表替代横滑宽表，桌面端隐藏
    cards_container = ui.column().classes("w-full gap-2 tgs-mobile-cards mt-1")
    render_list()

    # ---- 页面级持久弹窗（统一在页面构建时创建，事件里只填充并打开，
    # 避免在表格单元格等事件槽位里动态创建弹窗导致 Quasar 渲染异常）----

    with (
        ui.dialog() as create_dialog,
        ui.card().classes("w-[520px] max-w-[96vw] max-h-[88vh] overflow-auto"),
    ):
        add_dialog_close_button(create_dialog)
        create_title = ui.label("新建退频道任务").classes("text-lg font-semibold")
        account_sel = ui.select(
            label="账号（可多选）", options=[], multiple=True, with_input=True
        ).classes("w-full")
        attach_select_all(account_sel)
        with ui.row().classes("w-full items-end flex-wrap gap-2"):
            days_input = ui.number(
                label="不更新天数阈值", value=7, min=1, step=1, format="%.0f"
            ).classes("w-36")
            scope_sel = ui.select(
                {"channel": "仅频道", "group": "仅群组", "both": "频道+群组"},
                value="both",
                label="扫描范围",
            ).classes("w-40")
            sp = ui.checkbox("跳过置顶", value=True)
        kw_input = ui.input("保护关键字（逗号分隔）", value=DEFAULT_KEYWORDS).classes(
            "w-full"
        )
        ik_input = ui.input(
            "包含关键字（逗号分隔，任一命中即算，留空不限制）", value=""
        ).classes("w-full")
        ui.label(
            "多个关键字为或关系：标题命中任一关键字即算；"
            "填写后仅命中的频道/群组会进入扫描结果（再按保护关键字区分自动退出与手动确认）"
        ).classes("text-xs text-gray-500")
        ui.label(
            "未命中关键字的频道将在启动后自动退出；命中关键字的频道仅展示，需手动勾选退出"
        ).classes("text-xs text-gray-500")
        auto_cb = ui.checkbox("自动退出未命中关键字的频道")

        def _sync_auto_cb() -> None:
            # 扫描范围包含群组时自动退出不可选（仅频道可自动退出）
            if str(scope_sel.value or "both") == "channel":
                auto_cb.enable()
            else:
                auto_cb.value = False
                auto_cb.disable()
                auto_cb.tooltip("扫描范围包含群组时不允许自动退出")

        scope_sel.on_value_change(lambda _e: _sync_auto_cb())
        _sync_auto_cb()
        with ui.row().classes("w-full justify-end"):
            ui.button("取消", on_click=create_dialog.close).props("flat")
            ui.button("启动执行", on_click=_do_create).props("color=primary")

    with (
        ui.dialog() as detail_dialog,
        ui.card().classes("w-[560px] max-w-[96vw] max-h-[88vh] overflow-auto"),
    ):
        add_dialog_close_button(detail_dialog)
        detail_title = ui.label("任务明细").classes("text-lg font-semibold")
        ui.label(
            "以下频道不会被自动退出（命中保护关键字或未开启自动退出），"
            "可勾选后手动退出。明细已持久化，任务完成后重新打开仍可见。"
        ).classes("text-xs text-gray-500")
        detail_body = ui.column().classes("w-full gap-2")

    with ui.dialog() as confirm_dialog, ui.card():
        add_dialog_close_button(confirm_dialog)
        confirm_title = ui.label("确认退出？").classes("text-lg font-semibold")
        ui.label("退出后需要重新搜索/加群才能回来，请仔细核对！").classes(
            "text-sm text-orange-600"
        )
        confirm_list = ui.column().classes("max-h-60 overflow-auto w-full")
        with ui.row().classes("w-full justify-end"):
            ui.button("取消", on_click=confirm_dialog.close).props("flat")
            ui.button("确认退出", on_click=_do_leave_selected).props("color=negative")

    with ui.dialog() as delete_dialog, ui.card():
        add_dialog_close_button(delete_dialog)
        delete_label = ui.label("确认删除任务？").classes("text-lg font-semibold")
        with ui.row().classes("w-full justify-end"):
            ui.button("取消", on_click=delete_dialog.close).props("flat")
            ui.button("删除", on_click=_do_delete).props("color=negative")

    def copy_log() -> None:
        if not log_lines:
            ui.notify("暂无可复制的日志", type="warning")
            return
        copy_text_to_clipboard("\n".join(log_lines))
        ui.notify(f"已复制 {len(log_lines)} 行日志", type="positive")

    fullscreen_on = False

    def toggle_fullscreen() -> None:
        """切换运行日志全屏态：日志区 fixed 铺满视口，右上角显示退出按钮。"""
        nonlocal fullscreen_on
        fullscreen_on = not fullscreen_on
        if fullscreen_on:
            log_area.classes(add="tgs-page-log-fullscreen")
            exit_full_btn.classes(add="tgs-log-exit-visible")
        else:
            log_area.classes(remove="tgs-page-log-fullscreen")
            exit_full_btn.classes(remove="tgs-log-exit-visible")
        log_area.update()
        exit_full_btn.update()

    with ui.row().classes("w-full items-center justify-between mt-2"):
        ui.label("运行日志").classes("font-semibold")
        with ui.row().classes("gap-1 items-center"):
            ui.button(icon="content_copy", on_click=copy_log).props(
                "flat round dense"
            ).tooltip("一键复制日志")
            ui.button(icon="fullscreen", on_click=toggle_fullscreen).props(
                "flat round dense"
            ).tooltip("全屏显示")
    log_area = ui.log(max_lines=300).classes("w-full h-48")
    for line in log_lines:
        log_area.push(line)
    # 退出全屏悬浮按钮：仅全屏时 fixed 显示在视口右上角，平时不占布局
    exit_full_btn = (
        ui.button(icon="close", on_click=toggle_fullscreen)
        .props("flat round dense unelevated")
        .classes("tgs-log-exit-full")
    )

    def refresh() -> None:
        render_list()

    return refresh
