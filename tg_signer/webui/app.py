import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Callable, Optional

from nicegui import app, run, ui

from tg_signer import __version__
from tg_signer.core import get_proxy
from tg_signer.webui.activity import install_activity_tracking, set_active_tab
from tg_signer.webui.auto_leave import auto_leave_block
from tg_signer.webui.channel_stats import channel_stats_block
from tg_signer.webui.combo import combo_block
from tg_signer.webui.common import (
    add_dialog_close_button,
    build_debug_report,
    copy_text_to_clipboard,
    notify_error,
    pretty_json,
    state,
)
from tg_signer.webui.data import (
    DEFAULT_LOG_FILE,
    DEFAULT_WORKDIR,
    LOG_DIR,
    export_backup,
    import_backup,
    list_log_files,
    load_logs,
)
from tg_signer.webui.giveaway import (
    giveaway_block,
    list_known_channels,
    load_giveaway_scan_cap,
    load_giveaway_timeout_min,
    load_win_forward_targets,
    load_win_sweep_limit,
    save_giveaway_scan_cap,
    save_giveaway_timeout_min,
    save_win_forward_targets,
    save_win_sweep_limit,
)
from tg_signer.webui.messages import message_center_button
from tg_signer.webui.random_chat import random_chat_block
from tg_signer.webui.sign import (
    SIGNER_TEMPLATE,
    SignerBlock,
    TaskRunnerBlock,
    flush_task_history,
    user_info_block,
)
from tg_signer.webui.stop_all import stop_all_tasks

logger = logging.getLogger(__name__)

AUTH_CODE_ENV = "TG_SIGNER_GUI_AUTHCODE"
AUTH_STORAGE_KEY = "tg_signer_gui_auth_code"


def log_block(card: Optional[ui.card] = None) -> Callable[[], None]:
    """日志查看卡片；传入外部弹窗卡片（card）时提供全屏显示切换。"""
    with ui.card().classes("w-full shadow-sm tgs-log-card"):
        ui.label("日志查看").classes("text-md font-semibold")
        ui.label("查看最新日志行，可自定义文件路径和行数。").classes(
            "text-sm text-gray-500 mb-1"
        )

        with ui.row().classes("tgs-log-controls items-end w-full gap-3 flex-wrap"):
            limit_input = ui.number(
                label="日志行数",
                value=state.log_limit,
                min=10,
                max=2000,
                format="%d",
            ).classes("w-32")
            level_toggle = ui.toggle(
                {"all": "全部", "warn": "警告+", "error": "仅错误"},
                value="all",
                on_change=lambda _: refresh(),
            ).props("dense")
            keyword_input = (
                ui.input(label="关键字过滤", placeholder="回车应用")
                .props("clearable dense")
                .classes("min-w-[180px]")
            )
            keyword_input.on("keydown.enter", lambda _: refresh())
            keyword_input.on_value_change(lambda e: refresh() if not e.value else None)
            log_select = ui.select(
                label=f"选择日志文件（{LOG_DIR}/）",
                options=[],
                on_change=lambda e: select_log_file(e.value),
            ).classes("min-w-[220px]")
            log_path_input = ui.input(
                label="日志路径（可自定义）", value=str(state.log_path)
            ).classes("w-full")
        log_area = ui.scroll_area().classes(
            "w-full bg-gray-50 rounded-lg border border-gray-200 tgs-log-area"
        )
        log_area.style("max-height: 420px")
        with log_area:
            log_list = (
                ui.column()
                .classes("w-full gap-0 p-3 font-mono text-sm")
                .style("white-space: pre;")
            )

        def classify_line(line: str) -> str:
            upper = line.upper()
            if "ERROR" in upper:
                return "text-red-700"
            if "WARN" in upper:
                return "text-amber-700"
            if "INFO" in upper:
                return "text-blue-700"
            return "text-gray-800"

        current_lines: list[str] = []

        def refresh_log_options() -> None:
            options = [str(p) for p in list_log_files(LOG_DIR)]
            current_path = str(log_path_input.value or state.log_path)
            if current_path and current_path not in options:
                options.insert(0, current_path)
            log_select.options = options
            log_select.value = current_path
            log_select.update()

        def select_log_file(path_value: str | None) -> None:
            if not path_value:
                return
            log_path_input.value = path_value
            log_path_input.update()
            refresh()

        def refresh() -> None:
            nonlocal current_lines
            refresh_log_options()
            try:
                state.log_limit = int(limit_input.value or state.log_limit)
            except ValueError:
                state.log_limit = 200
            state.set_log_path(log_path_input.value or str(DEFAULT_LOG_FILE))

            level = level_toggle.value or "all"
            keyword = (keyword_input.value or "").lower().strip()
            filtering = level != "all" or bool(keyword)
            # 过滤命中会减少可见行数，多读几倍原始行再取最新 limit 行
            fetch_limit = state.log_limit * 4 if filtering else state.log_limit
            path, lines = load_logs(fetch_limit, log_path_input.value)
            if filtering and lines:

                def _match(line: str) -> bool:
                    upper = line.upper()
                    if level == "error" and "ERROR" not in upper:
                        return False
                    if level == "warn" and "ERROR" not in upper and "WARN" not in upper:
                        return False
                    if keyword and keyword not in line.lower():
                        return False
                    return True

                lines = [line for line in lines if _match(line)][-state.log_limit :]

            log_list.clear()
            if not lines:
                current_lines = []
                with log_list:
                    if filtering:
                        ui.label("没有匹配过滤条件的日志行").classes(
                            "text-gray-500 text-sm"
                        )
                    else:
                        ui.label(f"未找到日志文件: {path}").classes(
                            "text-gray-500 text-sm"
                        )
                log_list.update()
                if filtering:
                    refresh_status("没有匹配过滤条件的日志行")
                else:
                    refresh_status(f"未找到日志文件: {path}")
                return

            # 整段日志用一个 <pre> 渲染：几千行时元素数从「每行一个」降为 1，
            # 显著减少 websocket 序列化与浏览器 DOM 压力
            body = "\n".join(
                f'<span class="{classify_line(line)}">{escape(line)}</span>'
                for line in lines
            )
            with log_list:
                ui.html(f'<pre class="tgs-log-pre">{body}</pre>').classes("w-full")
            current_lines = lines
            log_list.update()
            suffix = "（已过滤）" if filtering else ""
            refresh_status(f"文件: {path} | 显示最新 {len(lines)} 行{suffix}")

        def copy_logs() -> None:
            """把当前显示的全部日志行复制到剪贴板（含安全上下文降级方案）。"""
            if not current_lines:
                ui.notify("暂无可复制的日志", type="warning")
                return
            copy_text_to_clipboard("\n".join(current_lines))
            ui.notify(f"已复制 {len(current_lines)} 行日志", type="positive")

        def copy_debug_info() -> None:
            """复制调试信息：环境头 + 当前日志文件与显示行 + 任务历史。

            手机端报错后一键复制，粘贴到电脑即可拿到定位/复现所需的上下文。
            """
            sections: list[tuple[str, str]] = [
                ("当前日志文件", str(log_path_input.value or state.log_path))
            ]
            if current_lines:
                sections.append(("当前显示日志行", "\n".join(current_lines)))
            try:
                hist = json.loads(
                    (Path(state.workdir) / "webui_task_history.json").read_text(
                        encoding="utf-8-sig"
                    )
                )
                if isinstance(hist, list) and hist:
                    sections.append(("任务历史（最近10条）", pretty_json(hist[-10:])))
            except (OSError, json.JSONDecodeError):
                logger.debug("读取任务历史失败", exc_info=True)
            copy_text_to_clipboard(build_debug_report(state.workdir, sections))
            ui.notify(
                "已复制调试信息（含环境信息），粘贴给电脑即可排查", type="positive"
            )

        fullscreen_on = False

        def toggle_fullscreen() -> None:
            """切换日志弹窗全屏态：卡片撑满视口，日志区占满剩余高度。"""
            nonlocal fullscreen_on
            if card is None:
                return
            fullscreen_on = not fullscreen_on
            if fullscreen_on:
                card.classes(add="tgs-log-fullscreen")
                fullscreen_btn.text = "退出全屏"
            else:
                card.classes(remove="tgs-log-fullscreen")
                fullscreen_btn.text = "全屏显示"
            fullscreen_btn.update()
            log_area.update()

        with ui.row().classes("gap-2 mt-2 items-center justify-between"):
            with ui.row().classes("gap-2 items-center flex-wrap"):
                ui.button("刷新日志", on_click=refresh)
                ui.button("一键复制日志", on_click=copy_logs).props("outline")
                ui.button("复制调试信息", on_click=copy_debug_info).props(
                    "outline"
                ).tooltip("复制环境信息+当前日志+任务历史，粘贴到电脑排查复现")
                if card is not None:
                    fullscreen_btn = ui.button(
                        "全屏显示", on_click=toggle_fullscreen
                    ).props("outline")
            log_status = ui.label("").classes("text-xs text-gray-500")

        def refresh_status(text: str) -> None:
            log_status.text = text
            log_status.update()

        refresh_log_options()

    return refresh


def _apply_paths(workdir_input, on_refresh: Callable[[], None]) -> None:
    try:
        state.set_workdir(workdir_input.value or str(DEFAULT_WORKDIR))
        ui.notify(f"已切换工作目录: {state.workdir}", type="positive")
    except Exception as exc:  # noqa: BLE001
        notify_error(exc)
        return
    on_refresh()


def _build_dashboard(container) -> None:
    # 跟踪浏览器会话的可见性与当前 tab，供各面板轮询门控使用
    install_activity_tracking()
    with container:
        refreshers: list[Callable[[], None]] = []

        def refresh_all() -> None:
            for refresh in refreshers:
                refresh()

        # ---- 预创建"数据查看"对话框（内容首次打开时才构建，减少首屏负载） ----
        with (
            ui.dialog() as users_dialog,
            ui.card().classes("w-full max-w-4xl") as users_card,
        ):
            pass
        add_dialog_close_button(users_dialog)

        with (
            ui.dialog() as logs_dialog,
            ui.card().classes("w-full max-w-4xl") as logs_card,
        ):
            pass
        add_dialog_close_button(logs_dialog)

        with (
            ui.dialog() as configs_dialog,
            ui.card().classes("w-full max-w-4xl tgs-dialog-flex") as configs_card,
        ):
            pass
        add_dialog_close_button(configs_dialog)

        with (
            ui.dialog() as stats_dialog,
            ui.card().classes("w-full max-w-4xl") as stats_card,
        ):
            pass
        add_dialog_close_button(stats_dialog)

        _users_built = False
        _logs_built = False
        _configs_built = False
        _stats_built = False

        def _ensure_users() -> None:
            """用户信息对话框内容懒构建（仅首次）。"""
            nonlocal _users_built
            if _users_built:
                return
            _users_built = True
            with users_card:
                ui.label("用户信息").classes("text-lg font-semibold")
                ui.label("查看当前已登录账户信息 (users/*/me.json)。").classes(
                    "text-gray-600"
                )
                users_refresh = user_info_block()
                refreshers.append(users_refresh)
                # 块本身只创建空容器并返回 refresh，需立即调用一次填充内容
                users_refresh()
                with ui.row().classes("w-full justify-end"):
                    ui.button("关闭", on_click=users_dialog.close).props("outline")

        def _ensure_logs() -> None:
            """日志对话框内容懒构建（仅首次）。"""
            nonlocal _logs_built
            if _logs_built:
                return
            _logs_built = True
            with logs_card:
                ui.label("日志").classes("text-lg font-semibold")
                logs_refresh = log_block(logs_card)
                refreshers.append(logs_refresh)
                logs_refresh()
                with ui.row().classes("w-full justify-end"):
                    ui.button("关闭", on_click=logs_dialog.close).props("outline")

        def _ensure_stats() -> None:
            """频道统计对话框内容懒构建（仅首次）。"""
            nonlocal _stats_built
            if _stats_built:
                return
            _stats_built = True
            with stats_card:
                ui.label("频道 / 群组统计").classes("text-lg font-semibold")
                channel_stats_block(state.workdir)
                with ui.row().classes("w-full justify-end"):
                    ui.button("关闭", on_click=stats_dialog.close).props("outline")

        async def open_settings() -> None:
            # 扫描各账号最近会话（list_known_channels）与读取多个状态文件都是磁盘
            # 密集操作，放到线程池执行，避免卡住事件循环里的其他轮询/请求
            target_options = {"me": "收藏夹 (me)"}
            target_options.update(
                await run.io_bound(list_known_channels, state.workdir)
            )
            current_targets = await run.io_bound(
                load_win_forward_targets, state.workdir
            )
            sweep_limit = await run.io_bound(load_win_sweep_limit, state.workdir)
            timeout_min = await run.io_bound(load_giveaway_timeout_min, state.workdir)
            scan_cap = await run.io_bound(load_giveaway_scan_cap, state.workdir)
            with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl"):
                add_dialog_close_button(dialog)
                ui.label("设置").classes("text-lg font-semibold")
                workdir_input = ui.input(
                    label="工作目录",
                    value=str(state.workdir),
                    placeholder=".signer",
                ).classes("w-full")
                # 中奖消息转发目标（全局设置，对所有抽奖任务生效）：
                # 多选，选项 = 收藏夹 + 各账号最近会话中的群组/频道
                ui.label("中奖消息转发目标").classes("text-lg font-semibold mt-2")
                ui.label(
                    "抽奖引擎识别到中奖消息后，用各账号把中奖原消息转发到这里"
                    "（可多选，需为该账号已加入的群组/频道），修改后新启动的任务生效。"
                ).classes("text-xs text-gray-500")
                for tid in current_targets:
                    if tid not in target_options:
                        target_options[tid] = tid
                target_select = (
                    ui.select(
                        target_options,
                        value=current_targets,
                        label="转发目标（可多选）",
                        multiple=True,
                        with_input=True,
                    )
                    .classes("w-full")
                    .props("use-chips clearable")
                )
                # 中奖回扫条数：首次记录某 bot 时的初始基线，之后增量扫描
                ui.label("中奖回扫条数").classes("text-lg font-semibold mt-2")
                ui.label(
                    "首次记录某个 bot 时回扫其私聊最近多少条消息找中奖通知"
                    "（默认 50）；之后按水位线增量扫描，只处理新消息，"
                    "该值不再参与日常回扫。"
                ).classes("text-xs text-gray-500")
                sweep_input = ui.number(
                    label="回扫条数",
                    value=sweep_limit,
                    min=1,
                    max=1000,
                    precision=0,
                ).classes("w-full")
                # 抽奖任务超时（全局）：单独运行与组合子任务共用
                ui.label("抽奖任务超时（分钟）").classes("text-lg font-semibold mt-2")
                ui.label(
                    "抽奖任务运行达到该时长后自动停止"
                    "（单独运行的抽奖任务与组合任务中的抽奖子任务共用，"
                    "0=不限，默认 10）。"
                ).classes("text-xs text-gray-500")
                timeout_input = ui.number(
                    label="超时（分钟，0=不限）",
                    value=timeout_min,
                    min=0,
                    max=10080,
                    precision=0,
                ).classes("w-full")
                # 单频道扫描上限（全局）：关键词未命中时也按该条数截断
                ui.label("单频道扫描上限（条）").classes("text-lg font-semibold mt-2")
                ui.label(
                    "每个频道/群组每轮最多回溯多少条历史消息"
                    "（关键词未命中时同样生效，达到上限会写入消息中心提醒，"
                    "默认 500）。"
                ).classes("text-xs text-gray-500")
                scan_cap_input = ui.number(
                    label="扫描上限（条）",
                    value=scan_cap,
                    min=1,
                    max=10000,
                    precision=0,
                ).classes("w-full")
                # 频道统计：独立页面展示各账号已加入频道/超级群组数量
                ui.label("账号频道统计").classes("text-lg font-semibold mt-2")
                ui.label(
                    "统计每个账号已加入的频道与超级群组数量（计入 500/1000 上限），"
                    "以柱形图展示各账号占用情况。"
                ).classes("text-xs text-gray-500")
                ui.button(
                    "打开频道统计",
                    icon="bar_chart",
                    on_click=lambda: (_ensure_stats(), stats_dialog.open()),
                ).props("outline")

                async def apply_settings() -> None:
                    # 先切换工作目录并刷新界面，再在后台线程落盘各项设置
                    _apply_paths(workdir_input, refresh_all)
                    try:
                        await run.io_bound(
                            save_win_forward_targets,
                            state.workdir,
                            list(target_select.value or []),
                        )
                        await run.io_bound(
                            save_win_sweep_limit, state.workdir, sweep_input.value
                        )
                        await run.io_bound(
                            save_giveaway_timeout_min,
                            state.workdir,
                            timeout_input.value,
                        )
                        await run.io_bound(
                            save_giveaway_scan_cap, state.workdir, scan_cap_input.value
                        )
                    except Exception as exc:  # noqa: BLE001
                        notify_error(exc)
                        return
                    ui.notify("设置已保存，新启动的抽奖任务生效", type="positive")
                    dialog.close()

                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("取消", on_click=dialog.close).props("outline")
                    ui.button("应用并刷新", color="primary", on_click=apply_settings)
            dialog.open()

        async def _stop_all_tasks() -> None:
            """一键停止所有面板运行中的任务，释放会话/数据库占用。"""
            counts = await stop_all_tasks(state.workdir)
            total = sum(counts.values())
            if total <= 0:
                ui.notify("当前没有运行中的任务", type="info")
                return
            detail = "，".join(f"{name} {n} 个" for name, n in counts.items() if n)
            ui.notify(
                f"已停止 {total} 个任务（{detail}），会话/数据库占用已释放",
                type="positive",
            )

        with ui.row().classes("w-full items-center justify-between gap-2 no-wrap"):
            with (
                ui.tabs()
                .classes("flex-1 min-w-0 main-tabs")
                .props("align=left") as tabs
            ):
                tab_run = ui.tab("run", "签到")
                tab_combo = ui.tab("combo", "组合任务")
                tab_random = ui.tab("random", "随机发言")
                tab_leave = ui.tab("leave", "批量退频道")
                tab_giveaway = ui.tab("giveaway", "自动抽奖")

            # 版本号 + 消息中心 + 菜单按钮，与主标签栏同行（下拉项在下方绑定）
            with ui.row().classes("items-center gap-0 no-wrap tgs-header-actions"):
                ui.label(f"v{__version__}").classes(
                    "text-xs text-gray-500 select-none mr-1 tgs-header-version"
                )
                # 消息中心：铃铛按钮 + 未读数徽标 + 消息弹窗（加入失败提醒/待完成发言任务）
                refreshers.append(message_center_button())
                header_btn = ui.button(icon="more_vert").props("flat round dense")

        # 默认选中「签到」：未选中任何 tab（首次访问、localStorage 无记录或值失效）时，
        # 面板区域不显示任何内容，这里显式设为 run 保证默认面板有内容
        tabs.value = "run"
        set_active_tab("run", tabs.client)

        # 「菜单」按钮的下拉项（点击按钮展开）
        with header_btn:
            with ui.menu():
                # 版本号：移动端右上角空间不足已隐藏，统一在菜单顶部展示
                with (
                    ui.row()
                    .classes("w-full items-center justify-center no-wrap")
                    .style("padding: 4px 12px 2px")
                ):
                    ui.label(f"v{__version__}").classes(
                        "text-xs text-gray-400 select-none"
                    )
                ui.separator()
                ui.menu_item(
                    "签到配置管理",
                    on_click=lambda: (_ensure_configs(), configs_dialog.open()),
                )
                ui.menu_item("设置", on_click=open_settings)
                ui.menu_item(
                    "用户信息", on_click=lambda: (_ensure_users(), users_dialog.open())
                )
                ui.menu_item(
                    "日志", on_click=lambda: (_ensure_logs(), logs_dialog.open())
                )
                ui.separator()
                ui.menu_item("停止所有任务并解锁数据库", on_click=_stop_all_tasks)

        # 移动端 tab 短标签：按 tab 顺序给每个 label 写入 data-short 短名，
        # 宽度<=640px 时由媒体查询用短名替换全名（桌面端不受影响）。
        # 注意：q-tab 的 name 属性不会反映到 DOM（getAttribute('name') 为 null），
        # 因此按索引匹配而非按 name
        _short_tab_names = ["签到", "组合", "发言", "退频道", "抽奖"]
        ui.run_javascript(
            "const shortNames = " + __import__("json").dumps(_short_tab_names) + ";"
            # 带重试：Quasar 渲染 q-tab 标签可能晚于脚本执行，未全部写入则 200ms 后重试，
            # 否则窄屏下 font-size:0 生效而 data-short 缺失，标签会全部空白
            "(function(){let tries=0;const apply=()=>{"
            "  const labels=document.querySelectorAll('.main-tabs .q-tab .q-tab__label');"
            "  let done=0;"
            "  labels.forEach((label,i)=>{"
            "    if(shortNames[i]){label.dataset.short=shortNames[i];done++;}"
            "  });"
            "  if(done<labels.length&&tries++<30)setTimeout(apply,200);"
            "};apply();})();"
        )

        valid_tabs = {"run", "combo", "random", "leave", "giveaway"}

        def _tab_name(value) -> str:
            if isinstance(value, str):
                return value
            return str(getattr(value, "name", "") or "")

        # 上次选中的 tab 保存在浏览器 localStorage（key: tg_signer_tab），
        # 不依赖 cookie / 服务端存储，刷新页面、重启服务、重开浏览器都能恢复；
        # 切换 tab 时同时懒构建对应面板内容
        def _on_tab_change(e) -> None:
            name = _tab_name(e.value)
            if name in valid_tabs:
                # 记录当前激活 tab：非激活 tab 的轮询会跳过，切回时自动恢复
                set_active_tab(name, tabs.client)
                _ensure_panel(name)
                ui.run_javascript(
                    f'localStorage.setItem("tg_signer_tab", {json.dumps(name)})'
                )

        tabs.on_value_change(_on_tab_change)

        # 页面加载后从 localStorage 恢复上次选中的 tab（带重试，等待 Quasar 渲染完成）
        # 注意 NiceGUI 元素 .id 是纯数字，DOM 中的 id 带 "c" 前缀
        _tab_ids = {
            "run": f"c{tab_run.id}",
            "combo": f"c{tab_combo.id}",
            "random": f"c{tab_random.id}",
            "leave": f"c{tab_leave.id}",
            "giveaway": f"c{tab_giveaway.id}",
        }
        ui.run_javascript(
            f"""
            (function() {{
                const map = {json.dumps(_tab_ids)};
                const stored = localStorage.getItem("tg_signer_tab");
                // 无记录/记录失效时兜底到「签到」；服务端 tabs.value 设值在部分
                // Quasar 版本下不生效，这里统一用点击保证选中态与面板一致
                const name = stored && Object.prototype.hasOwnProperty.call(map, stored) ? stored : "run";
                let tries = 0;
                const restore = () => {{
                    const active = document.querySelector('.main-tabs .q-tab--active');
                    if (name === "run" && active) return;  // 已有选中项则不打扰
                    const el = document.getElementById(map[name]);
                    if (el) el.click();
                    else if (tries++ < 30) setTimeout(restore, 100);
                }};
                restore();
            }})();
            """
        )

        # 除默认「签到」tab 外全部懒构建：首次切到对应 tab 才创建内容元素，
        # 大幅减少手机端首屏的元素数量与序列化传输量
        with ui.tab_panels(tabs).classes("w-full"):
            panel_run = ui.tab_panel(tab_run)
            panel_combo = ui.tab_panel(tab_combo)
            panel_random = ui.tab_panel(tab_random)
            panel_leave = ui.tab_panel(tab_leave)
            panel_giveaway = ui.tab_panel(tab_giveaway)

        runner: Optional[TaskRunnerBlock] = None

        def _build_run_panel() -> None:
            nonlocal runner
            with panel_run:
                runner = TaskRunnerBlock()
                refreshers.append(runner)

        def _build_combo_panel() -> None:
            with panel_combo:
                # 组合任务区块自带标题与说明，这里不再重复一段提示文案
                refreshers.append(combo_block(runner, state.workdir))

        def _build_random_panel() -> None:
            with panel_random:
                ui.label(
                    "多账号随机发言：选择账号与目标群组，按固定间隔从内置语库随机发送消息。"
                ).classes("text-gray-600 tgs-tab-intro")
                refreshers.append(random_chat_block(state.workdir))

        def _build_leave_panel() -> None:
            with panel_leave:
                ui.label(
                    "批量退出长期不更新的频道：扫描账号对话，预览确认后并发批量退出。"
                ).classes("text-gray-600 tgs-tab-intro")
                refreshers.append(auto_leave_block(state.workdir))

        def _build_giveaway_panel() -> None:
            with panel_giveaway:
                ui.label(
                    "自动参与频道抽奖：监听 t.me 抽奖机器人链接（?start= 参数），"
                    "实时新帖与手动扫描两种方式触发，支持多账号与指定频道列表。"
                ).classes("text-gray-600 tgs-tab-intro")
                refreshers.append(giveaway_block(state.workdir))

        def _ensure_configs() -> None:
            """配置管理对话框内容懒构建（仅首次）。"""
            nonlocal _configs_built
            if _configs_built:
                return
            _configs_built = True
            with configs_card:
                ui.label("签到配置管理").classes("text-lg font-semibold")
                ui.label("管理 signer 的配置文件，支持查看、编辑和删除。").classes(
                    "text-gray-600"
                )

                # ---- 配置导入导出（用于备份或迁移到其他设备） ----
                async def export_configs() -> None:
                    try:
                        # 打包 zip 是同步磁盘操作，放入线程池避免阻塞事件循环
                        data = await run.io_bound(export_backup, state.workdir)
                        ui.download(
                            data,
                            f"tg-signer-backup-{datetime.now():%Y%m%d-%H%M%S}.zip",
                            "application/zip",
                        )
                        ui.notify(
                            "备份已导出（不含 session 文件），浏览器已开始下载",
                            type="positive",
                        )
                    except Exception as exc:  # noqa: BLE001
                        notify_error(exc)

                async def on_import_file(e) -> None:
                    try:
                        # NiceGUI 3.13：上传内容在 e.file（FileUpload），read() 为异步
                        raw = await e.file.read()
                        # 解包 zip 并逐个写配置同样是同步磁盘操作
                        counts = await run.io_bound(import_backup, raw, state.workdir)
                    except Exception as exc:  # noqa: BLE001
                        notify_error(exc)
                        return
                    ui.notify(
                        "导入完成：签到配置 {signer} 个、数据文件 {signer_data_files} 个".format(
                            **counts
                        ),
                        type="positive",
                    )
                    try:
                        refresh_all()
                    except Exception as exc:  # noqa: BLE001
                        # 导入已成功，仅界面刷新失败时单独提示，避免误报导入失败
                        notify_error(exc)

                with ui.row().classes("gap-2 items-center flex-wrap"):
                    ui.button("导出全部配置", icon="download", on_click=export_configs)
                    ui.button(
                        "导入配置", icon="upload", on_click=lambda: import_dlg.open()
                    )

                with (
                    ui.dialog() as import_dlg,
                    ui.card().classes("w-[420px] max-w-full"),
                ):
                    add_dialog_close_button(import_dlg)
                    ui.label("导入备份").classes("text-lg font-semibold")
                    ui.label(
                        "选择此前「导出全部配置」生成的备份文件（.zip 或 .json）。"
                        "同名配置会被覆盖，账户状态同步恢复；"
                        "ZIP 还会还原最近聊天缓存和批量退频道排除关键字配置。"
                        "任务历史不随备份迁移；备份不含 session 登录凭据，"
                        "迁移账号需另行复制 .session 文件。"
                    ).classes("text-sm text-gray-600")
                    ui.upload(
                        label="选择备份文件",
                        on_upload=on_import_file,
                        auto_upload=True,
                    ).props("accept=.json,.zip").classes("w-full")
                    with ui.row().classes("w-full justify-end"):
                        ui.button("关闭", on_click=import_dlg.close).props("outline")

                signer_block = SignerBlock(SIGNER_TEMPLATE)
                refreshers.append(signer_block)
                # 块本身只创建空容器并返回 refresh，需立即调用一次填充配置列表
                signer_block()

                with ui.row().classes("w-full justify-end"):
                    ui.button("关闭", on_click=configs_dialog.close).props("outline")

        _panel_builders = {
            "run": _build_run_panel,
            "combo": _build_combo_panel,
            "random": _build_random_panel,
            "leave": _build_leave_panel,
            "giveaway": _build_giveaway_panel,
        }
        _panel_built: set = set()

        def _ensure_panel(name: str) -> None:
            """首次激活 tab 时构建对应面板（幂等）。"""
            if name in _panel_built:
                return
            builder = _panel_builders.get(name)
            if builder is None:
                return
            _panel_built.add(name)
            builder()

        # 默认 tab（run/签到）立即构建，保证首屏可用
        _ensure_panel("run")

        refresh_all()


def _resolve_auth_code(explicit: Optional[str]) -> str:
    """解析 WebUI 访问授权码：显式参数优先，其次 TG_SIGNER_GUI_AUTHCODE 环境变量，空白视为未配置。"""
    return (explicit or (os.environ.get(AUTH_CODE_ENV) or "")).strip()


def _check_auth_code(input_code: str, auth_code: str) -> tuple[bool, str]:
    """校验验证页输入的授权码，返回 (是否通过, 提示消息)；通过时消息为空串。"""
    code = (input_code or "").strip()
    if not code:
        return False, "请输入授权码"
    if code != auth_code:
        return False, "授权码错误，请重试"
    return True, ""


# —— 页面初始化科学上网检查 ——
# 所有任务功能（签到/自动化/监控/抽奖等）都依赖能连上 Telegram，
# 页面初始化时通过 TG_PROXY（未配置则直连）对 Telegram DC 做一次 TCP 握手验证。
_TELEGRAM_DC_HOST = "149.154.167.51"
_TELEGRAM_DC_PORT = 443
_NETWORK_CHECK_TIMEOUT = 8.0


async def _probe_telegram_connectivity() -> None:
    """通过当前代理配置连一次 Telegram DC，失败时在页面顶部弹出持久警告。"""
    proxy = get_proxy()
    dest = f"{_TELEGRAM_DC_HOST}:{_TELEGRAM_DC_PORT}"
    logger.info(
        "网络自检开始（目标 %s，代理 %s）",
        dest,
        f"{proxy.get('scheme')}://{proxy.get('hostname')}:{proxy.get('port')}"
        if proxy
        else "未配置（直连）",
    )
    try:
        if proxy and proxy.get("scheme") in ("socks5", "socks4", "http"):
            from urllib.parse import quote

            from python_socks.async_.asyncio import Proxy as SocksProxy

            auth = ""
            if proxy.get("username"):
                auth = f"{quote(str(proxy['username']))}:{quote(str(proxy['password'] or ''))}@"
            proxy_url = f"{proxy['scheme']}://{auth}{proxy['hostname']}:{proxy['port']}"
            sock = await asyncio.wait_for(
                SocksProxy.from_url(proxy_url).connect(
                    dest_host=_TELEGRAM_DC_HOST, dest_port=_TELEGRAM_DC_PORT
                ),
                timeout=_NETWORK_CHECK_TIMEOUT,
            )
            sock.close()
        else:
            writer = None
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(_TELEGRAM_DC_HOST, _TELEGRAM_DC_PORT),
                    timeout=_NETWORK_CHECK_TIMEOUT,
                )
            finally:
                if writer is not None:
                    writer.close()
    except Exception as exc:
        reason = f"通过代理 {proxy['hostname']}:{proxy['port']}" if proxy else "直连"
        logger.warning(
            "网络自检失败（%s 连 Telegram）：(%s) %s", reason, type(exc).__name__, exc
        )
        ui.notify(
            f"科学上网不可用：{reason}连接 Telegram 失败（{_NETWORK_CHECK_TIMEOUT:.0f} 秒内未握手成功）。"
            "所有任务功能都无法正常使用，请检查代理/VPN 后刷新页面重试。",
            type="warning",
            position="top",
            timeout=0,
            close_button="知道了",
        )
        return
    logger.info("网络自检通过：可连上 Telegram（%s）", dest)


def _auth_gate(container, auth_code: str, on_success: Callable[[], None]) -> None:
    with container:
        ui.label("TG Signer Web 控制台").classes(
            "text-2xl font-semibold tracking-wide mb-2"
        )
        ui.label("已启用访问控制，请输入 Auth Code 继续使用 Web 控制台。").classes(
            "text-gray-600"
        )
        with ui.column().classes("w-full items-center"):
            with ui.card().classes("w-full max-w-xl shadow-md"):
                ui.label("Auth Code 验证").classes("text-lg font-semibold")
                ui.label("检测到auth_code环境变量已配置，首次访问需验证。").classes(
                    "text-sm text-gray-500"
                )
                code_input = ui.input(
                    label="Auth Code",
                    placeholder="请输入授权码",
                    password=True,
                    password_toggle_button=True,
                ).classes("w-full")
                status = ui.label("").classes("text-sm text-negative")

                def verify() -> None:
                    code = (code_input.value or "").strip()
                    ok, message = _check_auth_code(code, auth_code)
                    if not ok:
                        if code:
                            status.text = message
                            status.update()
                            code_input.set_value("")
                        ui.notify(message, type="warning" if not code else "negative")
                        return
                    app.storage.user[AUTH_STORAGE_KEY] = auth_code
                    ui.notify("认证成功", type="positive")
                    container.clear()
                    on_success()

                ui.button("验证并进入", color="primary", on_click=verify).classes(
                    "w-full mt-2"
                )


def build_ui(auth_code: str = None) -> None:
    # 固定浅色模式：NiceGUI 默认跟随系统主题，系统深色时页面会整体变黑
    ui.dark_mode(False)
    # 整页灰底渐变，卡片区域白色
    ui.add_head_html(
        """
        <style>
            body {
                background: linear-gradient(180deg, #eef1f5 0%, #e3e7ee 100%) fixed !important;
            }
            .q-layout, .q-page, .q-page-container, .nicegui-content {
                background: transparent !important;
            }
            .q-card {
                background: #ffffff;
                border-radius: 12px;
                box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
            }
            /* 弹窗右上角悬浮关闭按钮（移动端友好），不占布局空间 */
            .dialog-close-fab {
                position: absolute !important;
                top: 6px !important;
                right: 6px !important;
                z-index: 30;
                min-width: 40px !important;
                min-height: 40px !important;
                color: #666 !important;
                background: rgba(0, 0, 0, 0.05) !important;
                border-radius: 50% !important;
            }
            /* 日志弹窗全屏态：卡片撑满视口，日志区占满剩余高度（标题/过滤/按钮行保持原样） */
            .tgs-log-fullscreen {
                width: 96vw !important;
                max-width: 96vw !important;
                height: 92vh !important;
                max-height: 92vh !important;
                display: flex !important;
                flex-direction: column;
            }
            .tgs-log-fullscreen > * { flex-shrink: 0; }
            /* 日志整段渲染：保留换行与等宽字体，去掉 pre 默认外边距 */
            .tgs-log-pre {
                margin: 0;
                font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
                font-size: 13px;
                line-height: 1.5;
                /* 保留缩进的换行与空格，超长行自动折行，避免手机端横向滚动 */
                white-space: pre-wrap;
                overflow-wrap: anywhere;
            }
            .tgs-log-fullscreen .tgs-log-area {
                flex: 1 1 auto !important;
                min-height: 0;
                height: auto !important;
                max-height: none !important;
            }
            /* 页面内嵌日志区（自动抽奖共用日志）全屏态：fixed 铺满视口 */
            .tgs-page-log-fullscreen {
                position: fixed !important;
                inset: 12px !important;
                width: calc(100vw - 24px) !important;
                height: calc(100vh - 24px) !important;
                max-height: none !important;
                z-index: 9000;
                background: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 12px;
                box-shadow: 0 4px 24px rgba(0, 0, 0, 0.18);
                padding: 8px;
            }
            /* 退出全屏悬浮按钮：默认隐藏，全屏时显示在视口右上角 */
            .tgs-log-exit-full { display: none !important; }
            .tgs-log-exit-full.tgs-log-exit-visible {
                display: inline-flex !important;
                position: fixed;
                top: 16px;
                right: 16px;
                z-index: 9001;
            }
            /* 全局通知消息固定在顶部 tab 栏下方：默认底部弹出会遮挡弹窗底部的「确定」按钮；
               tab 栏底部位置 = 页面内容内边距（桌面 16px / 移动端 8px）+ tab 高度 48px，
               取 72px 保证两端都在 tab 下方且留出间距，不再遮挡 tab */
            .q-notifications__list--bottom {
                top: 72px !important;
                bottom: auto !important;
            }
            /* 弹窗整卡限高可滚动：桌面端内容超高的弹窗（如签到配置管理里的
               JSON 编辑器）会溢出视口且无法滚动，统一限高在视口内并允许滚动；
               移动端媒体查询里有更细的规则（宽度/内边距/安全区），此处不重复 */
            .q-dialog__inner {
                max-height: 100dvh;
            }
            .q-dialog .q-card {
                max-height: calc(100dvh - 32px);
                overflow-y: auto;
                -webkit-overflow-scrolling: touch;
            }
            /* 内含 tab 面板的弹窗（如签到配置管理）：卡片 flex 列布局并让
               tab 面板占据剩余高度，内部 .q-panel.scroll 才能在限高内滚动。
               否则面板高度会被内容撑开（数千 px），外层卡片滚不动 */
            .tgs-dialog-flex {
                display: flex;
                flex-direction: column;
            }
            .tgs-dialog-flex .q-tab-panels {
                flex: 1 1 auto;
                min-height: 0;
            }
            /* 桌面端（>640px）：所有列表最右的「操作」列固定在右侧（粘性列），
               横向滚动时始终可见。移动端表格整体隐藏改用卡片列表，无需生效；
               th/td 均需不透明底色，否则横向滚动时下层单元格内容会透出来，
               左侧投影用于提示该列右侧还有被遮挡的内容 */
            @media (min-width: 641px) {
                .tgs-desktop-table th.col-actions,
                .tgs-desktop-table td.col-actions {
                    position: sticky;
                    right: 0;
                    z-index: 1;
                    background: #ffffff;
                    box-shadow: -6px 0 6px -6px rgba(0, 0, 0, 0.25);
                }
            }
        </style>
        """
    )
    # 移动端竖屏适配（红米 K80 等宽度 <=640px 的设备）；所有规则限定在媒体查询内，
    # 桌面端（>640px）布局与间距完全不变。
    ui.add_head_html(
        """
        <style>
            /* 桌面端：隐藏移动端卡片列表（<=640px 时才显示）。
               注意类名不可用 mobile-only/desktop-only——那是 Quasar 框架自带、
               按 body.mobile/body.desktop 设备类（而非视口宽度）生效的可见性类 */
            .tgs-mobile-cards { display: none !important; }
            @media (max-width: 640px) {
                .nicegui-content { padding: 8px !important; }
                /* 移动端排版压缩：说明文字与页面标题各缩一档，区块间距收紧 */
                .nicegui-content .text-sm {
                    font-size: 12.5px;
                    line-height: 1.5;
                }
                .nicegui-content .text-lg { font-size: 17px; line-height: 1.4; }
                .q-dialog .text-lg { font-size: 17px; }
                .nicegui-content .gap-3 { gap: 8px !important; }
                /* 弹窗内各区块间距由 16px 收到 10px，一屏能看到更多设置项 */
                .q-dialog .q-card { gap: 10px; }
                /* 弹窗里嵌套的卡片去掉阴影与内边距，直接铺在弹窗白底上 */
                .q-dialog .tgs-log-card,
                .q-dialog .tgs-account-card {
                    box-shadow: none !important;
                    background: transparent !important;
                    border: 0 !important;
                    padding: 0 !important;
                }
                /* 顶部与区块内说明重复，移动端隐藏 tab 级提示，省出首屏空间 */
                .tgs-tab-intro { display: none !important; }
                /* 子任务明细行：移动端允许换行，长群名/ID 不再横向截断 */
                .tgs-detail-line {
                    white-space: pre-wrap !important;
                    overflow-wrap: anywhere;
                    line-height: 1.4;
                }
                /* 日志控件全部整行铺开，输入框与级别按钮不再挤在一起 */
                .tgs-log-controls {
                    display: grid !important;
                    grid-template-columns: minmax(0, 1fr);
                    gap: 6px !important;
                    align-items: stretch;
                }
                .tgs-log-controls > * { width: 100% !important; min-width: 0 !important; }
                .tgs-log-controls .q-btn-group { width: 100%; }
                .tgs-log-controls .q-btn-group > .q-btn {
                    flex: 1 1 0;
                    padding: 0 2px;
                    font-size: 12px;
                }
                /* 日志弹窗内日志区随视口收缩，让操作按钮尽量留在首屏 */
                .q-dialog .tgs-log-area { max-height: 42dvh !important; }
                /* 账户卡片首行不换行：名称可省略，图标按钮固定在右侧 */
                .account-card-head { gap: 2px; }
                .account-card-head .q-toggle { flex: 1 1 auto; min-width: 0; }
                .account-card-head .q-toggle__label {
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }
                .account-card-head > .q-btn { flex: 0 0 auto; }
                .accounts-grid .q-btn {
                    min-width: 30px;
                    min-height: 30px;
                    padding: 3px;
                }
                .accounts-grid > div { padding: 6px 8px !important; gap: 2px !important; }
                /* 移动端：隐藏横滑宽表，改显纵向卡片列表 */
                .tgs-desktop-table { display: none !important; }
                .tgs-mobile-cards { display: flex !important; }
                /* 卡片式列表统一样式：信息竖排、按钮大而易点 */
                .mobile-list-card { gap: 3px; padding: 10px 12px; }
                .mobile-list-card .q-card__section { padding: 0; }
                .mobile-card-actions { gap: 6px; }
                .mobile-card-actions .q-btn { flex: 1 1 0; min-width: 0; }
                /* 卡片字段网格：短字段两列并排、长字段整行，标签定宽供上下对齐 */
                .tgs-fields {
                    display: grid;
                    width: 100%;
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                    column-gap: 12px;
                    row-gap: 1px;
                }
                .tgs-field {
                    display: flex;
                    align-items: flex-start;
                    gap: 6px;
                    min-width: 0;
                    font-size: 12px;
                    line-height: 1.45;
                }
                .tgs-field-full { grid-column: 1 / -1; }
                .tgs-field-k { flex: 0 0 52px; color: #6b7280; }
                .tgs-field-v {
                    flex: 1 1 auto;
                    min-width: 0;
                    color: #374151;
                    overflow-wrap: anywhere;
                }
                .tgs-field-pre { white-space: pre-line; }
                .mobile-card-actions { margin-top: 2px; }
                /* 顶部主标签：tab 与右上三点按钮同行（flex 布局自动分配宽度），
                   tab 栏占满剩余空间，隐藏版本号让出空间 */
                .tgs-header-version { display: none; }
                .tgs-header-actions .q-btn { padding: 4px; }
                .main-tabs { overflow: hidden; }
                .main-tabs .q-tab { padding-left: 9px; padding-right: 9px; }
                /* tab 文字用两字短名（data-short 由页面脚本写入 label 上）：
                   原全名字号置 0 隐藏，::before 显示短名，五个 tab 一屏放下 */
                .main-tabs .q-tab .q-tab__label { font-size: 0; }
                .main-tabs .q-tab .q-tab__label::before {
                    content: attr(data-short);
                    font-size: 15px;
                    letter-spacing: normal;
                }
                /* 弹窗：利用几乎全部宽度，高度限制在可视区内（dvh 适配地址栏伸缩），
                   整卡可滚动，并预留底部手势条安全区 */
                .q-dialog__inner { padding: 8px; }
                .q-dialog .q-card {
                    width: 100%;
                    max-width: 100% !important;
                    max-height: calc(100dvh - 16px);
                    overflow-y: auto;
                    -webkit-overflow-scrolling: touch;
                    border-radius: 10px;
                    padding-bottom: calc(12px + env(safe-area-inset-bottom));
                }
                /* 用户信息弹窗：账户卡片由 3 列改为单列，避免窄列里文字竖排 */
                .accounts-grid {
                    grid-template-columns: minmax(0, 1fr) !important;
                }
                /* 签到实时日志区压缩高度，把屏幕优先让给任务历史表格 */
                .sign-log-area { max-height: 220px !important; }
                /* 页面内嵌日志区全屏态在移动端解除 sign-log-area 等限高（本查询内规则
                   源码顺序在后，可覆盖第一段样式块中的同权重规则） */
                .tgs-page-log-fullscreen {
                    width: calc(100vw - 24px) !important;
                    height: calc(100vh - 24px) !important;
                    max-height: none !important;
                }
                /* 批量退频道工具栏：天数框收窄，争取与两个复选项排在同一行 */
                .leave-days { width: 104px; min-width: 104px; }
                /* 表格底部分页栏字号略缩，避免拥挤 */
                .q-table__bottom { font-size: 13px; }
                /* 任务进度弹窗（tgs-log-fullscreen）：移动端铺满几乎整个视口 */
                .tgs-log-fullscreen {
                    width: calc(100vw - 16px) !important;
                    height: calc(100dvh - 16px) !important;
                }
                /* 进度内容行移动端允许换行，长参数名/任务名不挤压值区 */
                .tgs-prog-head-row,
                .tgs-prog-sub-row { flex-wrap: wrap; row-gap: 2px; }
                .tgs-prog-sub-row .tgs-prog-sub-title {
                    flex: 1 1 0;
                    min-width: 0;
                    overflow-wrap: anywhere;
                }
                /* 参数行改为上下两行：参数名占整行，值换行展示 */
                .tgs-prog-param-row { flex-wrap: wrap; }
                .tgs-prog-param-row .tgs-prog-param-label {
                    flex: 0 0 100%;
                    min-width: 0;
                    max-width: none;
                }
                .tgs-prog-param-row .tgs-prog-param-value { flex: 1 1 100%; }
            }
        </style>
        """
    )
    ui.page_title("TG Signer Web 控制台")
    root = ui.column().classes("w-full gap-3")
    # 页面初始化即检测科学上网连通性（后台执行不阻塞首屏，失败弹持久报错）
    ui.timer(0.5, _probe_telegram_connectivity, once=True)

    def render_dashboard() -> None:
        root.clear()
        _build_dashboard(root)

    auth_code = _resolve_auth_code(auth_code)
    if not auth_code:
        render_dashboard()
        return

    if app.storage.user.get(AUTH_STORAGE_KEY) == auth_code:
        render_dashboard()
        return

    root.clear()
    _auth_gate(root, auth_code, render_dashboard)


# NiceGUI 每个浏览器会话会在 .nicegui/ 下生成一个 storage-user-*.json，
# 长期运行会无限累积，启动时清理超过保留期的旧会话文件。
_NICEGUI_STORAGE_DIR = Path(".nicegui")
_SESSION_CLEANUP_DAYS = 30


def cleanup_stale_sessions(max_age_days: int = _SESSION_CLEANUP_DAYS) -> int:
    """删除超过 max_age_days 未更新的 NiceGUI 会话缓存文件，返回删除数量。"""
    if not _NICEGUI_STORAGE_DIR.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for f in _NICEGUI_STORAGE_DIR.glob("storage-user-*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _load_or_create_storage_secret() -> str:
    """读取或生成固定的 storage secret，保证服务重启后浏览器会话存储（记住的 tab、访问码）不失效。"""
    secret_file = _NICEGUI_STORAGE_DIR / "storage_secret.txt"
    try:
        if secret_file.is_file():
            secret = secret_file.read_text(encoding="utf-8").strip()
            if secret:
                return secret
        _NICEGUI_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        secret = os.urandom(16).hex()
        secret_file.write_text(secret, encoding="utf-8")
        return secret
    except OSError:
        return os.urandom(16).hex()


def _install_loop_exception_handler() -> None:
    """给运行中的事件循环安装兜底异常处理器。

    pyrogram 的 Session.restart() 在会话断线重连时若撞上已关闭的 sqlite 存储，
    会产生「Task exception was never retrieved」级噪音堆栈
    （Cannot operate on a closed database / Client has not been started yet 等），
    这类异常无法在调用侧捕获，属于关闭/重连竞态的良性残留，降级为 debug 日志。
    """
    loop = asyncio.get_running_loop()
    default_handler = loop.get_exception_handler()

    def _handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        if isinstance(exc, sqlite3.ProgrammingError) and "closed database" in str(exc):
            logger.debug("忽略会话重连竞态异常（已关闭数据库）: %s", exc)
            return
        # 未检索任务异常的上下文键是 future（部分场景为 task），取任务对象的字符串做来源判断
        task_obj = context.get("future") or context.get("task")
        if (
            isinstance(exc, (ConnectionError, TimeoutError))
            and "pyrogram" in str(task_obj).lower()
        ):
            logger.debug("忽略 pyrogram 会话重连网络异常: %s", exc)
            return
        if default_handler is not None:
            default_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


def main(host: str = None, port: int = None, storage_secret: str = None) -> None:
    removed = cleanup_stale_sessions()
    if removed:
        print(
            f"已清理 {removed} 个过期会话缓存文件（保留 {_SESSION_CLEANUP_DAYS} 天内）"
        )

    @app.on_startup
    async def _setup_loop_handler() -> None:
        _install_loop_exception_handler()

    @app.on_shutdown
    async def _flush_pending_writes() -> None:
        # 任务历史为防抖写盘，退出前强制落盘，避免最后一条记录丢失
        flush_task_history()

    # GZip 压缩：NiceGUI 静态资源约 6MB（Vue/Quasar/字体等），gzip 后约 1.5MB，
    # 显著降低手机等弱网环境的首次加载耗时
    from starlette.middleware.gzip import GZipMiddleware

    app.add_middleware(GZipMiddleware, minimum_size=1024)

    ui.run(
        build_ui,
        title="TG Signer WebUI",
        favicon="⚙️",
        reload=False,
        host=host,
        port=port,
        show=False,
        storage_secret=storage_secret or _load_or_create_storage_secret(),
        # 会话 cookie 持久化 30 天（与过期会话清理策略一致），浏览器重开/服务重启后仍能记住上次选中的 tab
        session_middleware_kwargs={"max_age": 30 * 86400},
    )
