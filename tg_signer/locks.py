"""跨进程互斥：共享 JSON 的文件锁与「账号 + 任务」单实例锁。

WebUI 主进程与任务子进程（``task_runner`` / ``combo_runner`` /
``giveaway_runner``）共用同一个 workdir，两类并发必须显式处理：

- **读-改-写**：``webui_accounts.json`` 这类整文件覆盖写，两次并发保存
  会互相覆盖。用 :func:`file_lock` 包住「读 → 改 → 写」临界区。
- **重复启动**：同一账号的同一任务被启动两次会抢同一个 ``.session`` 文件。
  用 :func:`acquire_task_locks` 在任务进程内占位，进程退出（含崩溃）
  时由操作系统自动释放。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, List, Optional, Union

logger = logging.getLogger(__name__)

if os.name == "nt":
    import msvcrt

    def _try_lock(fp) -> bool:
        try:
            msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fp) -> None:
        try:
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _try_lock(fp) -> bool:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fp) -> None:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


# 锁文件轮询间隔（秒）
POLL_INTERVAL = 0.05
# 读-改-写锁的默认等待上限（秒）：临界区只有几毫秒，超时说明有异常占用
DEFAULT_TIMEOUT = 5.0


def _open_lock_file(path: Path):
    """打开（必要时创建）锁文件，并把读写位置复位到 0。

    Windows 的 ``msvcrt.locking`` 按文件位置加锁，空文件先补一个字节，
    保证每次都能锁到同一区间。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fp = open(path, "a+b")
    if fp.seek(0, os.SEEK_END) == 0:
        fp.write(b"\0")
        fp.flush()
    fp.seek(0)
    return fp


def lock_path_for(target: Path | str) -> Path:
    """读写锁文件路径：目标文件旁边的 ``<name>.lock``。"""
    return Path(str(target) + ".lock")


@contextmanager
def file_lock(target: Path | str, timeout: float = DEFAULT_TIMEOUT):
    """跨进程互斥地执行一段「读-改-写」，超时抛 :class:`TimeoutError`。

    注意：同一线程内不要嵌套对同一目标加锁（非重入）。
    """
    path = lock_path_for(target)
    fp = _open_lock_file(path)
    deadline = time.monotonic() + timeout
    try:
        while not _try_lock(fp):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待文件锁超时: {path}")
            time.sleep(POLL_INTERVAL)
        try:
            yield
        finally:
            _unlock(fp)
    finally:
        fp.close()


def _lock_filename(account: str, kind: str, name: str) -> str:
    """可读前缀 + 哈希后缀，避免任务名里的中文/空格/分隔符造成路径问题。"""
    raw = f"{account}|{kind}|{name}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^\w.-]+", "_", raw)[:48].strip("_") or "task"
    return f"{slug}-{digest}.lock"


def task_lock_path(workdir: Path | str, account: str, kind: str, name: str) -> Path:
    """「账号 + 任务类型 + 任务名」单实例锁的路径（位于 workdir/locks）。"""
    return Path(workdir) / "locks" / _lock_filename(account, kind, name)


class InstanceLock:
    """任务生命周期锁：持有期间文件句柄一直打开，进程退出即释放。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fp = None

    def acquire(self) -> bool:
        if self._fp is not None:
            return True
        fp = _open_lock_file(self.path)
        if not _try_lock(fp):
            fp.close()
            return False
        self._fp = fp
        return True

    def release(self) -> None:
        fp = self._fp
        self._fp = None
        if fp is None:
            return
        try:
            _unlock(fp)
        finally:
            fp.close()

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def acquire_task_locks(
    workdir: Path | str,
    accounts: Iterable[str],
    kind: str,
    names: Union[str, Iterable[str]],
) -> Optional[List[InstanceLock]]:
    """按「账号 × 任务名」抢占单实例锁。

    全部抢占成功返回锁列表；任一被占用则释放已占用的锁并返回 ``None``。
    """
    if isinstance(names, str):
        names = [names]
    targets = [(a, n) for a in accounts for n in names if a and n]
    locks: List[InstanceLock] = []
    for account, name in targets:
        lock = InstanceLock(task_lock_path(workdir, account, kind, name))
        if not lock.acquire():
            logger.info("任务已在运行，拒绝重复启动: %s / %s (%s)", account, name, kind)
            release_task_locks(locks)
            return None
        locks.append(lock)
    return locks


def release_task_locks(locks: Optional[Iterable[InstanceLock]]) -> None:
    for lock in locks or ():
        lock.release()
