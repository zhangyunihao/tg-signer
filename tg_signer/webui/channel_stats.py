"""账号频道统计：统计每个账号已加入的频道 / 超级群组数量与上限占用情况。

Telegram 没有提供「已加入数量」的接口，只能遍历账号对话逐个计数：
- 名额只统计频道 + 超级群组（含自己创建的），基础群与私聊不计入；
- 免费账号上限 500，Telegram Premium 上限 1000（以 me.is_premium 判断）；
- 统计结果写入工作目录 channel_stats.json，重新打开页面可直接查看上次结果。
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List

from nicegui import ui

from tg_signer.core import get_client, safe_disconnect, timeout_aiter
from tg_signer.utils import atomic_write_json
from tg_signer.webui.common import (
    attach_select_all,
    format_duration,
    with_select_all,
)
from tg_signer.webui.random_chat import build_account_options, list_session_names

logger = logging.getLogger(__name__)

STATS_FILE = "channel_stats.json"
# 单次网络调用超时（秒）：代理僵死时明确失败而不是永久挂起
_NET_CALL_TIMEOUT = 90
_FREE_LIMIT = 500
_PREMIUM_LIMIT = 1000


def _chat_type(chat) -> str:
    return str(getattr(chat, "type", "")).upper().replace("CHATTYPE.", "")


def load_stats(workdir) -> Dict:
    """读取上次统计结果；文件缺失或损坏时返回空结构。"""
    data: Dict = {"updated_at": "", "accounts": {}}
    try:
        raw = json.loads((Path(workdir) / STATS_FILE).read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict) and isinstance(raw.get("accounts"), dict):
            data["updated_at"] = str(raw.get("updated_at") or "")
            data["accounts"] = {
                str(k): v for k, v in raw["accounts"].items() if isinstance(v, dict)
            }
    except Exception:
        logger.debug("读取频道统计缓存失败，视为空", exc_info=True)
    return data


def save_stats(workdir, data: Dict) -> None:
    try:
        atomic_write_json(Path(workdir) / STATS_FILE, data)
    except Exception:
        logger.debug("保存频道统计缓存失败", exc_info=True)


async def scan_account(
    account: str,
    session_dir: Path,
    log: Callable[[str], None],
    progress: Callable[[str], None] | None = None,
) -> Dict:
    """遍历单个账号的对话列表并统计频道 / 超级群组数量。

    统计失败（未授权、网络超时等）直接抛异常，由调用方决定是否沿用旧数据。
    """
    client = None
    try:
        client = get_client(account, workdir=str(session_dir))
        ok = await asyncio.wait_for(client.connect(), 30)
        if not ok:
            raise RuntimeError("会话未授权")
        me = await asyncio.wait_for(client.get_me(), 30)
        premium = bool(getattr(me, "is_premium", False))
        channel = supergroup = basic = private = scanned = 0
        # 逐项拉取套超时：代理僵死时明确失败，不永久挂起
        async for dialog in timeout_aiter(
            client.get_dialogs(), _NET_CALL_TIMEOUT, "get_dialogs"
        ):
            scanned += 1
            if scanned % 200 == 0:
                log(f"[{account}] 已扫描 {scanned} 个对话...")
                if progress is not None:
                    progress(f"已扫描 {scanned} 个对话")
            ctype = _chat_type(dialog.chat)
            if ctype == "CHANNEL":
                channel += 1
            elif ctype == "SUPERGROUP":
                supergroup += 1
            elif ctype == "GROUP":
                basic += 1
            else:
                private += 1
        joined = channel + supergroup
        limit = _PREMIUM_LIMIT if premium else _FREE_LIMIT
        log(
            f"[{account}] 扫描完成：共 {scanned} 个对话，"
            f"频道 {channel} + 超级群组 {supergroup} = {joined}，"
            f"上限 {limit}{'（Premium）' if premium else ''}"
        )
        return {
            "account": account,
            "joined": joined,
            "limit": limit,
            "premium": premium,
            "channel": channel,
            "supergroup": supergroup,
            "basic_group": basic,
            "private": private,
            "scanned": scanned,
            "updated_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
            "error": "",
        }
    finally:
        if client is not None:
            await safe_disconnect(client)


async def refresh_stats_for_accounts(
    accounts: List[str],
    workdir,
    session_dir: Path | str,
    log: Callable[[str], None] | None = None,
) -> Dict:
    """只刷新指定账号的频道统计并落盘（不阻塞调用方，失败只标记不抛异常）。

    退频道任务完成后调用，避免全量扫描所有账号；传入的账号列表为空时直接返回。
    """
    accounts = [str(a) for a in (accounts or []) if str(a)]
    if not accounts:
        return load_stats(workdir)
    store = load_stats(workdir)
    _log = log or logger.debug
    _log(f"[频道统计] 自动刷新 {len(accounts)} 个账号：{'、'.join(accounts)}")

    for account in accounts:

        def _local_log(msg: str, _a=account) -> None:
            _log(f"[{_a}] {msg}")

        try:
            rec = await scan_account(account, Path(session_dir), _local_log)
            store["accounts"][account] = rec
        except Exception as exc:
            old = dict(store["accounts"].get(account) or {})
            old.update({"account": account, "error": str(exc)})
            store["accounts"][account] = old
            _log(f"[{account}] 自动刷新失败：{exc}")
    store["updated_at"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    save_stats(workdir, store)
    _log(f"[频道统计] 自动刷新完成：{store['updated_at']}")
    return store


def _ordered_records(store: Dict, names: List[str]) -> List[tuple]:
    """按账号管理中的顺序返回统计记录（未知账号排在最后）。"""
    order = {name: idx for idx, name in enumerate(names)}
    rows = [(name, rec) for name, rec in store.get("accounts", {}).items()]
    rows.sort(key=lambda item: (order.get(item[0], len(order)), item[0]))
    return rows


def _record_text(name: str, rec: Dict) -> str:
    """单个账号的统计摘要文本：有旧数据时失败也保留原计数。"""
    joined = rec.get("joined")
    limit = rec.get("limit")
    if isinstance(joined, int) and isinstance(limit, int):
        pct = (joined / limit * 100) if limit else 0
        premium = "（Premium）" if rec.get("premium") else ""
        text = (
            f"{name}：{joined} / {limit}（{pct:.1f}%）{premium}"
            f"｜频道 {rec.get('channel', 0)}，"
            f"超级群组 {rec.get('supergroup', 0)}，"
            f"基础群 {rec.get('basic_group', 0)}"
        )
        if rec.get("error"):
            text += f"｜本次更新失败：{rec['error']}"
        return text
    return f"{name}：更新失败 — {rec.get('error') or '无数据'}"


def _chart_options(rows: List[tuple]) -> Dict:
    names = [name for name, _ in rows]
    channel = [int(rec.get("channel") or 0) for _, rec in rows]
    supergroup = [int(rec.get("supergroup") or 0) for _, rec in rows]
    remain = [
        max(0, int(rec.get("limit") or _FREE_LIMIT) - int(rec.get("joined") or 0))
        for _, rec in rows
    ]
    return {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": ["频道", "超级群组", "剩余名额"], "top": 4},
        "grid": {"left": 56, "right": 18, "top": 40, "bottom": 84},
        "xAxis": {
            "type": "category",
            "data": names,
            "axisLabel": {"rotate": 30, "fontSize": 11},
        },
        "yAxis": {"type": "value", "minInterval": 1},
        "series": [
            {
                "name": "频道",
                "type": "bar",
                "stack": "limit",
                "barMaxWidth": 48,
                "data": channel,
                "itemStyle": {"color": "#3b82f6"},
                "emphasis": {"focus": "series"},
            },
            {
                "name": "超级群组",
                "type": "bar",
                "stack": "limit",
                "barMaxWidth": 48,
                "data": supergroup,
                "itemStyle": {"color": "#14b8a6"},
                "emphasis": {"focus": "series"},
            },
            {
                "name": "剩余名额",
                "type": "bar",
                "stack": "limit",
                "barMaxWidth": 48,
                "data": remain,
                "itemStyle": {"color": "#e5e7eb"},
                "emphasis": {"focus": "series"},
            },
        ],
    }


def channel_stats_block(workdir) -> None:
    """构建频道统计区块：柱形图 + 更新按钮 + 更新进度 + 更新日志。"""
    session_dir = Path(".")
    store = load_stats(workdir)
    names = list_session_names(session_dir, workdir)
    running = {"flag": False}
    _stats_path = Path(workdir) / STATS_FILE
    _last_mtime: int = 0

    def log(message: str) -> None:
        try:
            log_area.push(f"{datetime.now():%H:%M:%S} {message}")
        except Exception:
            logger.debug("推送频道统计日志失败", exc_info=True)

    def _current_mtime() -> int:
        try:
            return _stats_path.stat().st_mtime_ns
        except OSError:
            return 0

    def _poll_file() -> None:
        """每 3 秒检查 channel_stats.json 是否被其他进程更新（如自动退频道任务触发的刷新），
        如果变了就重新加载并重渲染图表与摘要。"""
        nonlocal store, _last_mtime
        mtime = _current_mtime()
        if mtime and mtime == _last_mtime:
            return
        new_store = load_stats(workdir)
        if new_store == store:
            _last_mtime = mtime
            return
        store = new_store
        _last_mtime = mtime
        update_time.text = f"上次更新：{store.get('updated_at') or '尚未统计'}"
        update_time.update()
        render_chart()
        render_summary()

    def render_chart() -> None:
        rows = [
            (name, rec)
            for name, rec in _ordered_records(store, names)
            if isinstance(rec.get("joined"), int)
        ]
        chart_box.clear()
        with chart_box:
            if not rows:
                ui.label("暂无可统计的数据，点击「更新统计」开始扫描").classes(
                    "text-sm text-gray-400"
                )
                return
            ui.echart(_chart_options(rows)).classes("w-full").style("height: 340px")

    def render_summary() -> None:
        summary_box.clear()
        with summary_box:
            for name, rec in _ordered_records(store, names):
                css = "text-xs text-red-600" if rec.get("error") else "text-xs"
                ui.label(_record_text(name, rec)).classes(css).style(
                    "overflow-wrap: anywhere;"
                )

    async def do_update() -> None:
        accounts = [str(a) for a in (account_sel.value or []) if str(a)]
        if not accounts:
            ui.notify("请先选择要统计的账号", type="warning")
            return
        if running["flag"]:
            return
        running["flag"] = True
        update_btn.disable()
        total = len(accounts)
        progress_bar.set_value(0)
        progress_status.text = f"准备统计 {total} 个账号..."
        progress_status.update()
        started = time.time()
        log(f"开始更新统计：{'、'.join(accounts)}")
        try:
            for idx, account in enumerate(accounts):

                def on_scan(message: str, i=idx, name=account) -> None:
                    progress_status.text = (
                        f"正在统计 {i + 1}/{total}：{name}（{message}）"
                    )
                    progress_status.update()

                try:
                    rec = await scan_account(account, session_dir, log, on_scan)
                    store["accounts"][account] = rec
                    log(f"[{account}] 完成：{rec['joined']} / {rec['limit']}")
                except Exception as exc:
                    # 本次失败时保留上次的计数，只标记错误，避免图表数据被清零
                    old = dict(store["accounts"].get(account) or {})
                    old.update({"account": account, "error": str(exc)})
                    store["accounts"][account] = old
                    log(f"[{account}] 统计失败：{exc}")
                    logger.debug("统计账号 %s 失败", account, exc_info=True)
                store["updated_at"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
                save_stats(workdir, store)
                update_time.text = f"上次更新：{store['updated_at']}"
                update_time.update()
                render_chart()
                render_summary()
                progress_bar.set_value((idx + 1) / total)
            progress_status.text = f"更新完成：{total} 个账号，耗时 {format_duration(time.time() - started)}"
            log(progress_status.text)
        finally:
            progress_status.update()
            running["flag"] = False
            update_btn.enable()

    ui.label(
        "统计每个账号已加入的频道与超级群组数量（计入 500/1000 上限，"
        "基础群与私聊不计入）。仅读取对话列表，不会加入或退出任何频道。"
    ).classes("text-xs text-gray-500")

    with ui.row().classes("w-full items-end gap-2 flex-wrap"):
        account_sel = (
            ui.select(
                with_select_all(build_account_options(session_dir, names)),
                value=names or None,
                label="账号（可多选）",
                multiple=True,
                with_input=True,
            )
            .classes("flex-1 min-w-[220px]")
            .props("use-chips clearable dense")
        )
        attach_select_all(account_sel)
        update_btn = ui.button("更新统计", icon="refresh", on_click=do_update).props(
            "color=primary"
        )

    update_time = ui.label(
        f"上次更新：{store.get('updated_at') or '尚未统计'}"
    ).classes("text-xs text-gray-500")
    progress_bar = ui.linear_progress(value=0, show_value=True).props("rounded")
    progress_status = ui.label("").classes("text-xs text-gray-600")

    chart_box = ui.column().classes("w-full")
    summary_box = ui.column().classes("w-full gap-0")
    render_chart()
    render_summary()

    # 初始化文件 mtime 并启动 3 秒轮询：后台自动刷新（如退频道任务触发）落盘后，
    # 弹窗打开时自动反映最新数据，无需手动点「更新统计」
    _last_mtime = _current_mtime()
    ui.timer(3.0, _poll_file)

    with ui.row().classes("w-full items-center justify-between mt-2"):
        ui.label("更新日志").classes("font-semibold text-sm")
    log_area = ui.log(max_lines=300).classes("w-full h-40")
