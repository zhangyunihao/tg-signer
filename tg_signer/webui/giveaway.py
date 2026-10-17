"""自动抽奖标签页：监控指定频道中的 t.me/<bot>?start=<参数> 抽奖链接并自动参与。

触发方式：任务启动时可自动扫描最近 N 条，也可随时手动扫描。
账号会话/授权检测/从 TG 更新对话列表等能力复用 random_chat 模块。
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from nicegui import ui

from tg_signer.giveaway_core import DEFAULT_KEYWORDS, GiveawayEngine, GiveawayState
from tg_signer.utils import atomic_write_json
from tg_signer.webui.data import load_two_way_accounts, load_user_infos
from tg_signer.webui.random_chat import (
    build_account_options,
    list_session_names,
    parse_chat_id,
    schedule_auth_checks,
    update_account_chats,
)

logger = logging.getLogger(__name__)

_GLOBAL_ENGINES: Dict[str, GiveawayEngine] = {}
_GLOBAL_LOGS: Dict[str, List[str]] = {}
_GLOBAL_STATUS_CBS: Dict[str, Callable[[bool], None]] = {}

_TASKS_FILE = "giveaway_tasks.json"
# 任务配置字段 -> 默认值，加载旧配置时补齐缺失字段
_CFG_FIELDS = {
    "accounts": list,
    "chats": list,
    "session_dir": str,
    "keywords": lambda: list(DEFAULT_KEYWORDS),
    "auto_scan": lambda: True,
    "delay_min": lambda: 3,
    "delay_max": lambda: 10,
    "confirm_wait": lambda: 8,
    "auto_join": lambda: True,
    "max_joins": lambda: None,
    "rescue_interval_min": lambda: 0,  # 补漏扫描间隔（分钟），0=关闭（默认关闭）
    "rescue_limit": lambda: 10,  # 每次补漏每频道重拉条数
}


def list_known_channels(workdir) -> Dict[str, str]:
    """汇总 users/*/latest_chats.json 中的频道与群组，返回 {chat_id: "标题 (id)"}"""
    options: Dict[str, str] = {}
    for info in load_user_infos(workdir):
        for chat in info.latest_chats or []:
            ctype = str(chat.get("type") or "").lower().replace("chattype.", "")
            if ctype != "channel" and not ctype.endswith("group"):
                continue
            cid = chat.get("id")
            if cid is None:
                continue
            title = chat.get("title") or chat.get("first_name") or "N/A"
            options[str(cid)] = f"{title} ({cid})"
    return options


def chat_kind_map(workdir) -> Dict[str, str]:
    """返回 {chat_id: "channel"|"group"}，供监控对象类型筛选"""
    mapping: Dict[str, str] = {}
    for info in load_user_infos(workdir):
        for chat in info.latest_chats or []:
            ctype = str(chat.get("type") or "").lower().replace("chattype.", "")
            if ctype != "channel" and not ctype.endswith("group"):
                continue
            cid = chat.get("id")
            if cid is None:
                continue
            kind = "channel" if ctype == "channel" else "group"
            mapping.setdefault(str(cid), kind)
    return mapping


def channel_title_map(workdir) -> Dict[str, str]:
    """返回 {chat_id: 频道名}，供任务表格展示"""
    mapping: Dict[str, str] = {}
    for info in load_user_infos(workdir):
        for chat in info.latest_chats or []:
            cid = chat.get("id")
            if cid is None:
                continue
            title = chat.get("title") or chat.get("first_name") or ""
            mapping.setdefault(str(cid), title)
    return mapping


def _split_items(raw: str) -> List[str]:
    """把逗号/空格/顿号/换行分隔的输入拆成列表"""
    return [x for x in re.split(r"[\s,，、;；]+", raw or "") if x.strip()]


def _store_path(workdir) -> Path:
    return Path(workdir) / _TASKS_FILE


def load_task_store(workdir) -> dict:
    """读取持久化任务配置：{"seq": N, "tasks": {id: cfg}}，并补齐缺失字段"""
    path = _store_path(workdir)
    if not path.is_file():
        return {"seq": 1, "tasks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"seq": 1, "tasks": {}}
    if not isinstance(data, dict):
        return {"seq": 1, "tasks": {}}
    data.setdefault(
        "seq",
        max((int(k) for k in data.get("tasks", {}) if str(k).isdigit()), default=0) + 1,
    )
    data.setdefault("tasks", {})
    for cfg in data["tasks"].values():
        if not isinstance(cfg, dict):
            continue
        for field, default in _CFG_FIELDS.items():
            if field not in cfg or cfg[field] is None:
                cfg[field] = default()
    return data


def save_task_store(workdir, store: dict) -> None:
    atomic_write_json(_store_path(workdir), store)


def giveaway_block(workdir, default_session_dir: str = ".") -> Callable[[], None]:
    """构建“自动抽奖”标签页内容，返回刷新函数以纳入全局刷新。"""
    wd_key = str(Path(workdir).resolve())
    store = load_task_store(workdir)
    tasks: Dict[str, dict] = {
        str(k): v for k, v in store["tasks"].items() if isinstance(v, dict)
    }
    giveaway_state = GiveawayState(workdir)
    chat_options_map: Dict[str, str] = {}
    chat_title_map: Dict[str, str] = {}
    kind_map: Dict[str, str] = {}
    table_ref: Dict[str, object] = {"table": None}

    def _persist() -> None:
        clean = {}
        for name, cfg in tasks.items():
            clean[name] = {
                "accounts": list(cfg.get("accounts") or []),
                "chats": [str(c) for c in (cfg.get("chats") or [])],
                "session_dir": str(cfg.get("session_dir") or "."),
                "keywords": list(cfg.get("keywords") or []),
                "auto_scan": bool(cfg.get("auto_scan", True)),
                "auto_join": bool(cfg.get("auto_join", True)),
                "delay_min": int(cfg.get("delay_min") or 0),
                "delay_max": int(cfg.get("delay_max") or 0),
                "confirm_wait": int(cfg.get("confirm_wait") or 0),
                "max_joins": (int(cfg["max_joins"]) if cfg.get("max_joins") else None),
                "rescue_interval_min": max(0, int(cfg.get("rescue_interval_min") or 0)),
                "rescue_limit": max(1, int(cfg.get("rescue_limit") or 10)),
            }
        save_task_store(workdir, {"seq": store["seq"], "tasks": clean})

    def engine_key(name: str) -> str:
        return f"{wd_key}|{name}"

    def get_engine(name: str) -> Optional[GiveawayEngine]:
        return _GLOBAL_ENGINES.get(engine_key(name))

    def ensure_engine(name: str) -> GiveawayEngine:
        engine = get_engine(name)
        if engine is None:
            engine = GiveawayEngine(state=giveaway_state)
            _GLOBAL_ENGINES[engine_key(name)] = engine
        return engine

    def chat_display(cid) -> str:
        title = chat_title_map.get(str(cid))
        return f"{title} ({cid})" if title else str(cid)

    def make_log(name: str) -> Callable[[str], None]:
        def _log(msg: str) -> None:
            buf = _GLOBAL_LOGS.setdefault(wd_key, [])
            # 消息已带时间戳（如「已参与」行）则不重复加
            ts = f"{datetime.now():%m-%d %H:%M:%S}"
            if not re.match(r"^\d{2}:\d{2}:\d{2}", msg):
                msg = f"{ts} {msg}"
            buf.append(f"[#{name}] {msg}")
            if len(buf) > 300:
                del buf[:-300]

        return _log

    def _set_account_options() -> None:
        session_dir = Path(session_dir_input.value or ".")
        names = list_session_names(session_dir, workdir)
        account_select.options = build_account_options(session_dir, names)
        account_select.update()

    def apply_channel_filter() -> None:
        """按类型筛选监控对象下拉选项（全部/仅频道/仅群组），不影响已选值"""
        try:
            mode = channel_type_toggle.value or "all"
        except NameError:
            return
        if mode == "all":
            filtered = dict(chat_options_map)
        else:
            filtered = {
                cid: label
                for cid, label in chat_options_map.items()
                if kind_map.get(cid) == mode
            }
        channel_select.options = filtered
        channel_select.update()

    def refresh_options() -> None:
        session_dir = Path(session_dir_input.value or ".")
        _set_account_options()
        names = list_session_names(session_dir, workdir)
        schedule_auth_checks(session_dir, names, on_done=_set_account_options)
        chat_options_map.clear()
        chat_options_map.update(list_known_channels(workdir))
        chat_title_map.clear()
        chat_title_map.update(channel_title_map(workdir))
        kind_map.clear()
        kind_map.update(chat_kind_map(workdir))
        apply_channel_filter()

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
        refresh_options()
        channel_select.value = [str(c) for c in cfg["chats"]]
        manual_chats_input.value = ""
        keywords_input.value = "，".join(cfg.get("keywords") or [])
        max_joins_input.value = cfg.get("max_joins") or None
        auto_scan_switch.value = cfg["auto_scan"]
        auto_join_switch.value = cfg.get("auto_join", True)
        ignore_hist_switch.value = bool(cfg.get("ignore_history", True))
        delay_min_input.value = cfg["delay_min"]
        delay_max_input.value = cfg["delay_max"]
        rescue_interval_input.value = cfg.get("rescue_interval_min") or 0
        rescue_limit_input.value = cfg.get("rescue_limit") or 10
        for el in (
            session_dir_input,
            account_select,
            channel_select,
            manual_chats_input,
            keywords_input,
            max_joins_input,
            auto_scan_switch,
            auto_join_switch,
            ignore_hist_switch,
            delay_min_input,
            delay_max_input,
            rescue_interval_input,
            rescue_limit_input,
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
                    f"账号已被运行中的任务 #{other} 占用（同一账号不能并行建立连接），"
                    "请先停止该任务",
                    type="warning",
                )
                return

        def _on_finish(_n: str = name) -> None:
            cb = _GLOBAL_STATUS_CBS.get(engine_key(_n))
            if cb is not None:
                _apply_status(cb, False)

        engine = ensure_engine(name)
        started = await engine.start(
            accounts=list(cfg["accounts"]),
            chats=[
                parse_chat_id(c) if parse_chat_id(c) is not None else str(c)
                for c in cfg["chats"]
            ],
            session_dir=Path(cfg["session_dir"]),
            log=make_log(name),
            keywords=list(cfg.get("keywords") or []),
            auto_scan=bool(cfg.get("auto_scan", True)),
            auto_join=bool(cfg.get("auto_join", True)),
            ignore_history=bool(cfg.get("ignore_history", True)),
            two_way_accounts=sorted(load_two_way_accounts(workdir)),
            max_joins=(int(cfg["max_joins"]) if cfg.get("max_joins") else None),
            delay_range=(
                float(cfg.get("delay_min") or 0),
                float(cfg.get("delay_max") or 0),
            ),
            confirm_wait=int(cfg.get("confirm_wait") or 8),
            rescue_interval=(int(cfg.get("rescue_interval_min") or 0) * 60),
            rescue_limit=int(cfg.get("rescue_limit") or 10),
            state=giveaway_state,
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
            await engine.stop()
        else:
            set_status(False)

    async def scan_task(name: str) -> None:
        engine = get_engine(name)
        if engine is None or not engine.running:
            ui.notify("任务未运行", type="warning")
            return
        if engine.scanning:
            ui.notify("正在扫描中，请稍候", type="warning")
            return
        ui.notify("开始扫描（至参与上限或连续 5 条已开奖为止）", type="positive")
        asyncio.create_task(engine.scan_now())

    def _apply_status(set_status: Callable[[bool], None], running: bool) -> None:
        try:
            set_status(running)
        except Exception:
            logger.debug("更新任务运行状态回调执行失败", exc_info=True)

    def on_add_task() -> None:
        accounts = list(account_select.value or [])
        # 清理历史标签后缀
        accounts = [str(a).replace(" ⚠️未授权", "") for a in accounts]
        if not accounts:
            ui.notify("请至少选择一个账号", type="warning")
            return
        chats: List[str] = []
        for cid in list(channel_select.value or []):
            chats.append(str(cid))
        for raw in _split_items(manual_chats_input.value or ""):
            parsed = parse_chat_id(raw)
            if parsed is not None and str(parsed) not in chats:
                chats.append(str(parsed))
        if not chats:
            ui.notify("请至少选择或手动填写一个频道", type="warning")
            return
        # 群组和频道扫描策略不同（群组只扫置顶、频道全量扫），不允许混选
        kinds = {kind_map.get(cid) for cid in chats if kind_map.get(cid)}
        if "group" in kinds and "channel" in kinds:
            ui.notify(
                "群组和频道不能同时选择：群组只扫置顶消息、频道扫描全部历史，"
                "请分开建任务",
                type="warning",
            )
            return
        keywords = _split_items(keywords_input.value or "")
        try:
            max_joins = (
                max(0, int(max_joins_input.value))
                if max_joins_input.value is not None
                else None
            )
            delay_min = max(0, int(delay_min_input.value or 0))
            delay_max = max(0, int(delay_max_input.value or 0))
            rescue_interval_min = max(0, int(rescue_interval_input.value or 0))
            rescue_limit = max(1, int(rescue_limit_input.value or 10))
        except (TypeError, ValueError):
            ui.notify("参与上限/延迟/补漏参数需为数字", type="warning")
            return
        if delay_min > delay_max:
            delay_min, delay_max = delay_max, delay_min
        name = str(store["seq"])
        store["seq"] += 1
        tasks[name] = {
            "accounts": accounts,
            "chats": chats,
            "session_dir": str(Path(session_dir_input.value or ".")),
            "keywords": keywords,
            "max_joins": max_joins,
            "auto_scan": bool(auto_scan_switch.value),
            "auto_join": bool(auto_join_switch.value),
            "ignore_history": bool(ignore_hist_switch.value),
            "delay_min": delay_min,
            "delay_max": delay_max,
            "confirm_wait": 8,
            "rescue_interval_min": rescue_interval_min,
            "rescue_limit": rescue_limit,
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
                {
                    "name": "chats",
                    "label": "监控频道",
                    "field": "chats",
                    "align": "left",
                },
                {
                    "name": "keywords",
                    "label": "关键词",
                    "field": "keywords",
                    "align": "left",
                },
                {
                    "name": "scan",
                    "label": "参与上限",
                    "field": "scan",
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
                    "classes": "col-actions",
                    "style": "white-space: nowrap;",
                },
            ]
            rows = []
            for name in sorted(
                tasks,
                key=lambda n: int(n) if str(n).isdigit() else 0,
                reverse=True,
            ):
                cfg = tasks[name]
                chat_names = [chat_display(c) for c in cfg["chats"]]
                rows.append(
                    {
                        "name": str(name),
                        "accounts": ", ".join(cfg["accounts"]),
                        "chats": f"{len(cfg['chats'])} 个："
                        + "、".join(chat_names[:3])
                        + ("…" if len(chat_names) > 3 else ""),
                        "keywords": "、".join(cfg.get("keywords") or []) or "全部链接",
                        "scan": "不限"
                        if not cfg.get("max_joins")
                        else str(cfg["max_joins"]),
                        "status": _status_text(name),
                    }
                )
            table = (
                ui.table(
                    columns=columns,
                    rows=rows,
                    row_key="name",
                    pagination=5,
                )
                .classes("w-full tgs-desktop-table")
                .props("flat dense")
            )
            table_ref["table"] = table
            card_badges: dict = {}
            # 「扫描中」也算运行态（info 蓝色）
            running_expr = (
                "props.row.status.startsWith('运行中')"
                " || props.row.status.startsWith('扫描中')"
                " || props.row.status.startsWith('停止中')"
            )
            table.add_slot(
                "body-cell-status",
                '<q-td :props="props">'
                f"<q-badge :color=\"({running_expr}) ? 'info' : 'grey-6'\">"
                "{{ props.row.status }}</q-badge></q-td>",
            )
            table.add_slot(
                "body-cell-actions",
                '<q-td :props="props" class="col-actions">'
                f'<q-btn v-if="!({running_expr})" flat dense color="primary" '
                'label="启动" @click="$parent.$emit(\'startTask\', props.row)" />'
                f'<q-btn v-if="({running_expr})" flat dense color="negative" '
                'label="停止" @click="$parent.$emit(\'stopTask\', props.row)" />'
                f'<q-btn v-if="({running_expr})" flat dense color="primary" '
                'label="扫描" @click="$parent.$emit(\'scanTask\', props.row)" />'
                '<q-btn flat dense color="primary" '
                'label="复制" @click="$parent.$emit(\'copyTask\', props.row)" />'
                '<q-btn flat dense color="negative" '
                'label="删除" @click="$parent.$emit(\'deleteTask\', props.row)" />'
                "</q-td>",
            )

            def make_set_status(n: str) -> Callable[[bool], None]:
                def _set(running: bool) -> None:
                    text = "运行中（已参与0）" if running else "未运行"
                    if not running:
                        text = "未运行"
                    else:
                        engine = get_engine(n)
                        text = engine.progress_text() if engine else "运行中"
                    for r in table.rows:
                        if r["name"] == str(n):
                            r["status"] = text
                            try:
                                table.update()
                            except Exception:
                                logger.debug("刷新任务表格 UI 失败", exc_info=True)
                            break
                    badge = card_badges.get(str(n))
                    if badge is not None:
                        try:
                            badge.props(f"color={'info' if running else 'grey-6'}")
                            badge.text = text
                            badge.update()
                        except Exception:
                            logger.debug("刷新任务状态徽标 UI 失败", exc_info=True)

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

            async def on_scan_event(e) -> None:
                row = e.args
                if isinstance(row, dict) and row.get("name"):
                    await scan_task(str(row["name"]))

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
            table.on("scanTask", on_scan_event)
            table.on("copyTask", on_copy_event)
            table.on("deleteTask", on_delete_event)

            # —— 移动端卡片列表 ——
            mobile_cards = ui.column().classes("w-full gap-2 tgs-mobile-cards")
            shown_rows = rows if mobile_state["show_all"] else rows[:5]

            async def _card_start(nn: str, cb) -> None:
                await start_task(nn, cb)
                render_tasks()

            async def _card_stop(nn: str, cb) -> None:
                await stop_task(nn, cb)
                render_tasks()

            with mobile_cards:
                for row in shown_rows:
                    n = str(row["name"])
                    running = row["status"] != "未运行"
                    set_status = make_set_status(n)
                    with ui.card().classes("w-full mobile-list-card border"):
                        with ui.row().classes(
                            "w-full items-center gap-2 no-wrap justify-between"
                        ):
                            ui.label(f"任务 #{n}").classes("text-base font-semibold")
                            badge = ui.badge(row["status"]).props(
                                f"color={'info' if running else 'grey-6'}"
                            )
                            card_badges[n] = badge
                        ui.label(f"账号：{row['accounts']}").classes(
                            "text-xs text-gray-600"
                        )
                        ui.label(f"频道：{row['chats']}").classes(
                            "text-xs text-gray-600"
                        )
                        ui.label(
                            f"关键词：{row['keywords']}　参与上限：{row['scan']}"
                        ).classes("text-xs text-gray-600")
                        with ui.row().classes("w-full mobile-card-actions"):
                            if not running:
                                ui.button(
                                    "启动",
                                    on_click=lambda nn=n,
                                    cb=set_status: asyncio.create_task(
                                        _card_start(nn, cb)
                                    ),
                                ).props("flat dense color=primary")
                            else:
                                ui.button(
                                    "停止",
                                    on_click=lambda nn=n,
                                    cb=set_status: asyncio.create_task(
                                        _card_stop(nn, cb)
                                    ),
                                ).props("flat dense color=negative")
                                ui.button(
                                    "扫描",
                                    on_click=lambda nn=n: asyncio.create_task(
                                        scan_task(nn)
                                    ),
                                ).props("flat dense color=primary")
                            ui.button(
                                "复制",
                                on_click=lambda nn=n: duplicate_task(nn),
                            ).props("flat dense color=primary")
                            ui.button(
                                "删除",
                                on_click=lambda nn=n: remove_task(nn),
                            ).props("flat dense color=negative")
                if len(rows) > 5:
                    if mobile_state["show_all"]:
                        ui.button(
                            "收起",
                            on_click=lambda: (
                                mobile_state.update(show_all=False),
                                render_tasks(),
                            ),
                        ).props("flat dense").classes("w-full")
                    else:
                        ui.button(
                            f"展开更多（共 {len(rows)} 条）",
                            on_click=lambda: (
                                mobile_state.update(show_all=True),
                                render_tasks(),
                            ),
                        ).props("flat dense").classes("w-full")

            for name in list(tasks):
                set_status = make_set_status(str(name))
                tasks[name]["_set_status"] = set_status
                _GLOBAL_STATUS_CBS[engine_key(name)] = set_status

    ui.label("自动抽奖").classes("text-lg font-semibold")
    ui.label(
        "监控指定频道消息中的 t.me 抽奖机器人链接（?start= 参数），自动点击参与；"
        "任务启动时可自动扫描历史消息（至参与上限或连续 5 条已开奖为止），"
        "也可随时手动扫描。同一抽奖链接每个账号只参与一次，"
        "记录持久化在工作目录，重启不重复参与。"
    ).classes("text-sm text-gray-500")

    with ui.row().classes("w-full items-center justify-between"):
        ui.label("任务列表").classes("font-semibold")
        ui.button("添加任务", icon="add", on_click=lambda: add_dlg.open()).props(
            "outline"
        )
    task_list_container = ui.column().classes("w-full gap-2")
    mobile_state = {"show_all": False}

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
            logger.debug("回放历史日志到界面日志区失败", exc_info=True)

    client = ui.context.client

    async def _poll_loop() -> None:
        while True:
            await asyncio.sleep(1.0)
            if not client.has_socket_connection:
                return
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
                    logger.debug("轮询刷新任务表格 UI 失败", exc_info=True)
            if _seen["n"] > len(log_buf):
                _seen["n"] = len(log_buf)
            new_lines = log_buf[_seen["n"] :]
            if not new_lines:
                continue
            _seen["n"] = len(log_buf)
            try:
                with client:
                    for line in new_lines:
                        try:
                            send_log.push(line)
                        except Exception:
                            logger.debug("推送新日志到界面日志区失败", exc_info=True)
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
            account_select = (
                ui.select(
                    label="选择账号（可多选）",
                    options=[],
                    multiple=True,
                )
                .classes("min-w-[260px]")
                .props("use-chips")
            )
            ui.button("刷新账号/频道", on_click=lambda: refresh_options()).props(
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
                    _log(f"开始从 Telegram 更新 {len(accounts)} 个账号的对话列表...")
                    for a in accounts:
                        await update_account_chats(
                            a, Path(session_dir_input.value or "."), workdir, _log
                        )
                    refresh_options()
                    ui.notify("频道列表已从 Telegram 更新", type="positive")
                finally:
                    update_btn.enable()

            update_btn = ui.button("从TG更新频道", on_click=update_chats_from_tg).props(
                "outline"
            )

        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
            channel_select = (
                ui.select(
                    label="选择监控频道（可多选，输入关键字过滤）",
                    options={},
                    multiple=True,
                )
                .classes("min-w-[320px] flex-grow")
                .props("use-chips use-input options-dense")
            )
            channel_type_toggle = ui.toggle(
                {"all": "全部", "channel": "仅频道", "group": "仅群组"},
                value="all",
                on_change=lambda _: apply_channel_filter(),
            ).props("dense")
            manual_chats_input = ui.input(
                label="手动添加频道（逗号分隔 chat_id 或 @username）",
                placeholder="-1001234567890, @somechannel",
            ).classes("min-w-[300px] flex-grow")

        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
            keywords_input = ui.input(
                label="消息关键词（逗号分隔，留空=含 bot 链接就参与）",
                value="，".join(DEFAULT_KEYWORDS),
            ).classes("min-w-[280px] flex-grow")
            max_joins_input = ui.number(
                label="参与条数上限（留空=不限）",
                value=None,
                min=0,
                max=10000,
                format="%d",
            ).classes("w-56")
            auto_scan_switch = ui.switch("启动时自动扫描一轮", value=True).classes(
                "pt-6"
            )
            auto_join_switch = ui.switch(
                "自动加入抽奖要求的频道/群组", value=True
            ).classes("pt-6")
            ignore_hist_switch = ui.switch(
                "忽略历史成功记录（允许重复参与）", value=True
            ).classes("pt-6")

        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
            delay_min_input = ui.number(
                label="参与前随机延迟下限（秒）", value=3, min=0, max=600, format="%d"
            ).classes("w-44")
            delay_max_input = ui.number(
                label="延迟上限（秒）", value=10, min=0, max=600, format="%d"
            ).classes("w-36")
            rescue_interval_input = ui.number(
                label="补漏扫描间隔（分钟，0=关闭）",
                value=0,
                min=0,
                max=1440,
                format="%d",
            ).classes("w-52")
            rescue_limit_input = ui.number(
                label="补漏重拉条数/频道",
                value=10,
                min=1,
                max=100,
                format="%d",
            ).classes("w-44")

        ui.label(
            "提示：参与 = 以账号身份向抽奖机器人发送 /start（等同手动点开链接）；"
            "机器人若弹出「确认参与」类按钮会自动点击；开启自动加入后，"
            "机器人回复消息中出现的频道/群组链接会被逐一加入（激进模式，可能误加）。"
            "补漏扫描会按设定间隔重拉各频道最近若干条消息，"
            "捞回此前因网络异常/中断漏掉的抽奖帖，连续多轮拉取失败会自动暂停一段时间。"
            "请确保账号已加入所监控的频道。"
        ).classes("text-xs text-amber-600")
        with ui.row().classes("w-full justify-end"):
            ui.button("取消", on_click=add_dlg.close).props("flat")
            ui.button("确定添加", color="primary", on_click=on_add_task)

    refresh_options()
    render_tasks()
    return refresh_options
