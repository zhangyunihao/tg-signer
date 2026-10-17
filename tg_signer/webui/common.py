import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Dict

from nicegui import ui

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


def notify_error(exc: Exception) -> None:
    ui.notify(f"{exc}", type="negative")
