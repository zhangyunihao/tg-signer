import json
import logging
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict

from nicegui import ui

from tg_signer import __version__
from tg_signer.webui.data import DEFAULT_LOG_FILE, DEFAULT_WORKDIR, get_workdir

logger = logging.getLogger(__name__)

# Windows 下把任务子进程绑到 Job Object：WebUI 服务进程退出（含被杀）时
# 操作系统自动结束所有子进程，避免 run 定时等常驻任务变成无法停止的孤儿进程
_JOB_HANDLE = None


def _ensure_job_handle():
    global _JOB_HANDLE
    if _JOB_HANDLE is not None or not sys.platform.startswith("win"):
        return _JOB_HANDLE
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        # 9 = JobObjectExtendedLimitInformation
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(job)
            return None
        _JOB_HANDLE = job
    except Exception:
        return None
    return _JOB_HANDLE


def _assign_to_job(proc: "subprocess.Popen") -> None:
    job = _ensure_job_handle()
    if job is None:
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject(job, int(proc._handle))
    except Exception:
        logger.debug("将子进程加入 Windows Job Object 失败", exc_info=True)


SOURCE_ROOT = Path(__file__).resolve().parents[2]


class UIState:
    def __init__(self) -> None:
        self.workdir: Path = get_workdir(DEFAULT_WORKDIR)
        self.log_path: Path = DEFAULT_LOG_FILE
        self.log_limit: int = 200
        self.record_filter: str = ""

    def set_workdir(self, path_str: str) -> None:
        self.workdir = get_workdir(Path(path_str).expanduser())

    def set_log_path(self, path_str: str) -> None:
        self.log_path = Path(path_str).expanduser()


state = UIState()


def pretty_json(data: Dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def format_duration(seconds: float) -> str:
    """把秒数格式化为「X分Y秒 / Y秒」文本，供各任务列表展示执行时长。"""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}小时{m}分{s}秒" if s else f"{h}小时{m}分"
    if seconds >= 60:
        return f"{seconds // 60}分{seconds % 60}秒"
    return f"{seconds}秒"


def notify_error(exc: Exception) -> None:
    ui.notify(f"{exc}", type="negative")


def add_dialog_close_button(dlg: ui.dialog) -> None:
    """在弹窗卡片右上角添加悬浮关闭按钮（移动端点 X 即可关闭，不占布局空间）。

    需在弹窗卡片创建之后调用；样式 .dialog-close-fab 由 app.build_ui 注入。
    """
    for card in list(dlg.default_slot.children):
        if not isinstance(card, ui.card):
            continue
        card.classes("relative")
        with card:
            ui.button(icon="close", on_click=dlg.close).props(
                "flat round dense unelevated"
            ).classes("dialog-close-fab")


# 账号多选下拉的「全选」虚拟项：出现在选项最顶部，勾选后自动展开为全部账号
SELECT_ALL_KEY = "__select_all__"


def with_select_all(options):
    """在账号多选选项最前面插入「全选」虚拟项（dict/list 两种选项格式均支持）。"""
    if isinstance(options, dict):
        return {SELECT_ALL_KEY: "全选", **options}
    return [SELECT_ALL_KEY, *options]


def attach_select_all(select: ui.select) -> None:
    """给账号多选下拉附加「全选」展开逻辑（需在 select 创建后调用一次）。

    勾选「全选」虚拟项后立即替换为全部真实账号，虚拟值不会残留在
    select.value 中，任务提交侧拿到的永远是真实账号名。
    """

    def _handler(_e=None) -> None:
        if SELECT_ALL_KEY not in (select.value or []):
            return
        options = select.options or {}
        names = options.keys() if isinstance(options, dict) else options
        select.set_value([k for k in names if k != SELECT_ALL_KEY])

    select.on("update:model-value", _handler)


def copy_text_to_clipboard(text: str) -> None:
    """把文本复制到浏览器剪贴板（安全上下文用 Clipboard API，否则降级 execCommand）。"""
    if not text:
        return
    # json.dumps 产出合法的 JS 字符串字面量，换行/引号均已转义
    payload = json.dumps(text, ensure_ascii=False)
    ui.run_javascript(
        "(async () => {"
        f"const t = {payload};"
        "try {"
        "if (navigator.clipboard && window.isSecureContext) {"
        "await navigator.clipboard.writeText(t); return;"
        "}"
        "} catch (e) {}"
        "const ta = document.createElement('textarea');"
        "ta.value = t; ta.style.position = 'fixed'; ta.style.opacity = '0';"
        "document.body.appendChild(ta); ta.focus(); ta.select();"
        "document.execCommand('copy'); ta.remove();"
        "})()"
    )


def read_file_tail(path: Path | str, max_lines: int = 200, max_bytes: int = 512_000) -> str:
    """读取文本文件最后 max_lines 行（最多回读 max_bytes，避免大文件占用过多内存）。

    文件不存在或读取失败返回空字符串；被截断时丢弃可能不完整的首行。
    """
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]
        return "\n".join(lines[-max_lines:])
    except OSError:
        logger.debug("读取日志文件尾部失败: %s", p, exc_info=True)
        return ""


def build_debug_report(workdir, extra_sections: list[tuple[str, str]]) -> str:
    """组装「复制调试日志」文本：环境头（版本/Python/系统/工作目录）+ 各数据段。

    用于手机端一键复制后粘贴到电脑定位问题；空段自动跳过。
    """
    termux = bool(os.environ.get("TERMUX_VERSION")) or "com.termux" in (
        os.environ.get("PREFIX") or ""
    )
    env_desc = "Termux (Android)" if termux else platform.system()
    try:
        workdir_text = str(Path(workdir).resolve())
    except OSError:
        workdir_text = str(workdir)
    header = "\n".join(
        [
            "==================== tg-signer 调试信息 ====================",
            f"复制时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"版本: tg-signer-plus {__version__}",
            f"Python: {platform.python_version()}",
            f"系统: {platform.platform()}",
            f"运行环境: {env_desc}",
            f"工作目录: {workdir_text}",
            f"服务进程目录: {Path.cwd()}",
            "============================================================",
        ]
    )
    parts = [header]
    for title, content in extra_sections:
        content = (content or "").strip()
        if not content:
            continue
        parts.append(f"\n----- {title} -----\n{content}")
    return "\n".join(parts)
