import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from nicegui import app, ui

from tg_signer.webui.auto_leave import auto_leave_block
from tg_signer.webui.combo import combo_block
from tg_signer.webui.common import notify_error, state
from tg_signer.webui.data import (
    DEFAULT_LOG_FILE,
    DEFAULT_WORKDIR,
    LOG_DIR,
    export_backup,
    import_backup,
    list_log_files,
    load_logs,
)
from tg_signer.webui.giveaway import giveaway_block
from tg_signer.webui.random_chat import random_chat_block
from tg_signer.webui.sign import (
    MONITOR_TEMPLATE,
    SIGNER_TEMPLATE,
    MonitorBlock,
    SignerBlock,
    SignRecordBlock,
    TaskRunnerBlock,
    user_info_block,
)

logger = logging.getLogger(__name__)

AUTH_CODE_ENV = "TG_SIGNER_GUI_AUTHCODE"
AUTH_STORAGE_KEY = "tg_signer_gui_auth_code"


def log_block() -> Callable[[], None]:
    with ui.card().classes("w-full shadow-sm"):
        ui.label("日志查看").classes("text-md font-semibold")
        ui.label("查看最新日志行，可自定义文件路径和行数。").classes(
            "text-sm text-gray-500 mb-1"
        )

        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
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
            "w-full bg-gray-50 rounded-lg border border-gray-200"
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

            with log_list:
                for line in lines:
                    color = classify_line(line)
                    ui.label(line).classes(f"w-full {color}").style("white-space: pre;")
            log_list.update()
            suffix = "（已过滤）" if filtering else ""
            refresh_status(f"文件: {path} | 显示最新 {len(lines)} 行{suffix}")

        with ui.row().classes("gap-2 mt-2 items-center justify-between"):
            ui.button("刷新日志", on_click=refresh)
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
    with container:
        # 右上角菜单按钮，绝对定位不占布局空间（下拉项在对话框定义后填充）
        header_btn = (
            ui.button(icon="more_vert")
            .props("flat round dense")
            .classes("absolute top-1 right-3 z-10")
        )

        refreshers: list[Callable[[], None]] = []
        refresh_records: "SignRecordBlock"

        def refresh_all() -> None:
            for refresh in refreshers:
                refresh()

        # ---- 预构建"数据查看"对话框（用户信息 / 签到记录 / 日志） ----
        with ui.dialog() as users_dialog, ui.card().classes("w-full max-w-4xl"):
            ui.label("用户信息").classes("text-lg font-semibold")
            ui.label("查看当前已登录账户信息 (users/*/me.json)。").classes(
                "text-gray-600"
            )
            refreshers.append(user_info_block())
            with ui.row().classes("w-full justify-end"):
                ui.button("关闭", on_click=users_dialog.close).props("outline")

        with ui.dialog() as records_dialog, ui.card().classes("w-full max-w-4xl"):
            ui.label("签到记录").classes("text-lg font-semibold")
            ui.label("签到记录（优先读取 SQLite，兼容旧 sign_record.json）").classes(
                "text-gray-600"
            )
            refresh_records = SignRecordBlock()
            refreshers.append(refresh_records)
            with ui.row().classes("w-full justify-end"):
                ui.button("关闭", on_click=records_dialog.close).props("outline")

        with ui.dialog() as logs_dialog, ui.card().classes("w-full max-w-4xl"):
            ui.label("日志").classes("text-lg font-semibold")
            ui.label("查看日志文件的最新行。").classes("text-gray-600")
            refreshers.append(log_block())
            with ui.row().classes("w-full justify-end"):
                ui.button("关闭", on_click=logs_dialog.close).props("outline")

        def goto_records(task_name: str) -> None:
            refresh_records.filter_input.set_value(task_name)
            records_dialog.open()

        def open_settings() -> None:
            with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl"):
                ui.label("工作目录").classes("text-lg font-semibold")
                workdir_input = ui.input(
                    label="工作目录",
                    value=str(state.workdir),
                    placeholder=".signer",
                ).classes("w-full")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("取消", on_click=dialog.close).props("outline")
                    ui.button(
                        "应用并刷新",
                        color="primary",
                        on_click=lambda: (
                            _apply_paths(workdir_input, refresh_all),
                            dialog.close(),
                        ),
                    )
            dialog.open()

        # 「菜单」按钮的下拉项（点击按钮展开）
        with header_btn:
            with ui.menu():
                ui.menu_item("工作目录", on_click=open_settings)
                ui.menu_item("用户信息", on_click=users_dialog.open)
                ui.menu_item("签到记录", on_click=records_dialog.open)
                ui.menu_item("日志", on_click=logs_dialog.open)

        with ui.tabs().classes("w-full main-tabs").props("align=left") as tabs:
            tab_run = ui.tab("run", "签到")
            tab_combo = ui.tab("combo", "组合任务")
            tab_random = ui.tab("random", "随机发言")
            tab_leave = ui.tab("leave", "批量退频道")
            tab_giveaway = ui.tab("giveaway", "自动抽奖")
            tab_configs = ui.tab("configs", "配置管理")

        valid_tabs = {"run", "combo", "random", "leave", "giveaway", "configs"}

        def _tab_name(value) -> str:
            if isinstance(value, str):
                return value
            return str(getattr(value, "name", "") or "")

        # 上次选中的 tab 保存在浏览器 localStorage（key: tg_signer_tab），
        # 不依赖 cookie / 服务端存储，刷新页面、重启服务、重开浏览器都能恢复
        def _on_tab_change(e) -> None:
            name = _tab_name(e.value)
            if name in valid_tabs:
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
            "configs": f"c{tab_configs.id}",
        }
        ui.run_javascript(
            f"""
            (function() {{
                const map = {json.dumps(_tab_ids)};
                const name = localStorage.getItem("tg_signer_tab");
                if (!name || !Object.prototype.hasOwnProperty.call(map, name) || name === "run") return;
                let tries = 0;
                const restore = () => {{
                    const el = document.getElementById(map[name]);
                    if (el) el.click();
                    else if (tries++ < 30) setTimeout(restore, 100);
                }};
                restore();
            }})();
            """
        )

        with ui.tab_panels(tabs).classes("w-full"):
            with ui.tab_panel(tab_run):
                runner = TaskRunnerBlock()
                refreshers.append(runner)

            with ui.tab_panel(tab_combo):
                ui.label(
                    "组合任务：把多个已有任务按顺序组合执行，每个账号依次跑完全部子任务。"
                ).classes("text-gray-600")
                refreshers.append(combo_block(runner, state.workdir))

            with ui.tab_panel(tab_random):
                ui.label(
                    "多账号随机发言：选择账号与目标群组，按固定间隔从内置语库随机发送消息。"
                ).classes("text-gray-600")
                refreshers.append(random_chat_block(state.workdir))

            with ui.tab_panel(tab_leave):
                ui.label(
                    "批量退出长期不更新的频道：扫描账号对话，预览确认后并发批量退出。"
                ).classes("text-gray-600")
                refreshers.append(auto_leave_block(state.workdir))

            with ui.tab_panel(tab_giveaway):
                ui.label(
                    "自动参与频道抽奖：监听 t.me 抽奖机器人链接（?start= 参数），"
                    "实时新帖与手动扫描两种方式触发，支持多账号与指定频道列表。"
                ).classes("text-gray-600")
                refreshers.append(giveaway_block(state.workdir))

            with ui.tab_panel(tab_configs):
                ui.label(
                    "管理 signer 和 monitor 的配置文件，支持查看、编辑和删除。"
                ).classes("text-gray-600")

                # ---- 配置导入导出（用于备份或迁移到其他设备） ----
                def export_configs() -> None:
                    try:
                        data = export_backup(state.workdir)
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
                        counts = import_backup(raw, state.workdir)
                    except Exception as exc:  # noqa: BLE001
                        notify_error(exc)
                        return
                    ui.notify(
                        "导入完成：签到配置 {signer} 个、监控配置 {monitor} 个、"
                        "数据文件 {signer_data_files} 个".format(**counts),
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

                with ui.tabs().classes("mt-2") as sub_tabs:
                    tab_signer = ui.tab("Signer")
                    tab_monitor = ui.tab("Monitor")
                with ui.tab_panels(sub_tabs, value=tab_signer).classes("w-full"):
                    with ui.tab_panel(tab_signer):
                        refreshers.append(
                            SignerBlock(SIGNER_TEMPLATE, goto_records=goto_records)
                        )
                    with ui.tab_panel(tab_monitor):
                        refreshers.append(MonitorBlock(MONITOR_TEMPLATE))

        refresh_all()


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
                    if not code:
                        ui.notify("请输入授权码", type="warning")
                        return
                    if code != auth_code:
                        status.text = "授权码错误，请重试"
                        status.update()
                        code_input.set_value("")
                        ui.notify("认证失败", type="negative")
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
                /* 移动端：隐藏横滑宽表，改显纵向卡片列表 */
                .tgs-desktop-table { display: none !important; }
                .tgs-mobile-cards { display: flex !important; }
                /* 卡片式列表统一样式：信息竖排、按钮大而易点 */
                .mobile-list-card { gap: 3px; padding: 10px 12px; }
                .mobile-list-card .q-card__section { padding: 0; }
                .mobile-card-actions { gap: 6px; }
                .mobile-card-actions .q-btn { flex: 1 1 0; min-width: 0; }
                /* 顶部主标签：缩小横向间距、给右上菜单按钮留位，空间不足时标签栏可横向滑动 */
                .main-tabs { padding-right: 40px; }
                .main-tabs .q-tab { padding-left: 9px; padding-right: 9px; }
                .main-tabs .q-tab .q-tab__label { font-size: 15px; }
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
                /* 批量退频道工具栏：天数框收窄，争取与两个复选项排在同一行 */
                .leave-days { width: 104px; min-width: 104px; }
                /* 表格底部分页栏字号略缩，避免拥挤 */
                .q-table__bottom { font-size: 13px; }
            }
        </style>
        """
    )
    ui.page_title("TG Signer Web 控制台")
    root = ui.column().classes("w-full gap-3")

    def render_dashboard() -> None:
        root.clear()
        _build_dashboard(root)

    auth_code = auth_code or (os.environ.get(AUTH_CODE_ENV) or "").strip()
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
