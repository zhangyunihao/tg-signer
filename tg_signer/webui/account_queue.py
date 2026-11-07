"""跨任务的账号租约与排队队列（WebUI 进程内共享）。

同一账号在同一时间只允许一个任务使用：任务启动账号前先申请租约，
账号被其它任务持有时登记等待条目（同一账号按登记顺序 FIFO 放行）。
等待条目总数超过全局设置「最大排队账号条目」时拒绝登记，由调用方提示。

设计要点：
- 租约与等待队列都只在内存中维护：WebUI 重启后旧进程已脱离管理，
  队列随进程清空，避免残留「幽灵占用」。
- 放行由等待方主动轮询（``poll``），不跨线程回调，异步任务与子进程任务
  可以共用同一套状态。
- 抽奖/随机发言这类整轮持有全部账号的任务，在引擎启动前申请全部账号，
  任一账号忙则整个任务显示「排队中」。
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

# 等待条目上限默认值（账号条目数），0 表示不限
DEFAULT_MAX_QUEUE_ENTRIES = 20


class QueueLimitError(RuntimeError):
    """等待条目超过全局上限。"""

    def __init__(self, limit: int, waiting: int) -> None:
        self.limit = limit
        self.waiting = waiting
        super().__init__(f"排队条目已达上限（{waiting}/{limit}），请稍后再启动任务")


@dataclass
class _Lease:
    task_id: str
    task_name: str
    task_type: str
    acquired_at: float = field(default_factory=time.monotonic)


@dataclass
class _Waiter:
    task_id: str
    task_name: str
    task_type: str
    created_at: float = field(default_factory=time.monotonic)


_LOCK = threading.RLock()
_LEASES: Dict[str, _Lease] = {}
_WAITERS: Dict[str, List[_Waiter]] = {}


def _normalize(accounts: Iterable[str]) -> List[str]:
    """去重并保持传入顺序，忽略空账号。"""
    seen = set()
    result = []
    for raw in accounts or []:
        name = str(raw or "").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def waiting_count() -> int:
    """当前等待条目总数（按账号条目计）。"""
    with _LOCK:
        return sum(len(items) for items in _WAITERS.values())


def reset() -> None:
    """清空全部租约与等待条目（测试/切换工作目录时使用）。"""
    with _LOCK:
        _LEASES.clear()
        _WAITERS.clear()


def queue_cap(workdir) -> int:
    """读取全局「最大排队账号条目」，读取失败按默认值处理。"""
    try:
        from tg_signer.webui.giveaway import load_max_queue_entries

        return max(0, int(load_max_queue_entries(workdir)))
    except Exception:
        return DEFAULT_MAX_QUEUE_ENTRIES


def request(
    task_id: str,
    task_name: str,
    task_type: str,
    accounts: Iterable[str],
    *,
    max_entries: int = 0,
) -> Tuple[List[str], List[str]]:
    """批量申请账号租约。

    空闲账号立即授予；被其它任务持有的账号登记为等待条目（FIFO）。
    返回 ``(已授予账号, 已排队账号)``。等待条目超过 ``max_entries``
    （0=不限）时抛 :class:`QueueLimitError`，且不会登记任何账号。
    """
    names = _normalize(accounts)
    if not names:
        return [], []
    with _LOCK:

        def _will_wait(account: str) -> bool:
            """该账号本次是否需要登记新的等待条目（按当前队列状态推算）。"""
            lease = _LEASES.get(account)
            if lease is not None and lease.task_id == task_id:
                return False
            waiters = _WAITERS.get(account, [])
            if lease is None:
                # 账号空闲：队首或无等待者时直接放行，不需要新条目
                if not waiters or waiters[0].task_id == task_id:
                    return False
            return not any(w.task_id == task_id for w in waiters)

        new_waiters = [a for a in names if _will_wait(a)]
        cap = max(0, int(max_entries))
        if cap > 0 and waiting_count() + len(new_waiters) > cap:
            raise QueueLimitError(cap, waiting_count())

        granted: List[str] = []
        queued: List[str] = []
        for account in names:
            lease = _LEASES.get(account)
            waiters = _WAITERS.get(account)
            if lease is not None and lease.task_id == task_id:
                # 已持有：幂等返回，并清掉本任务残留的等待条目
                if waiters:
                    remain = [w for w in waiters if w.task_id != task_id]
                    if remain:
                        _WAITERS[account] = remain
                    else:
                        _WAITERS.pop(account, None)
                granted.append(account)
                continue
            if lease is not None:
                # 被其它任务持有：登记等待（不重复登记）
                queued.append(account)
                waiters = _WAITERS.setdefault(account, [])
                if not any(w.task_id == task_id for w in waiters):
                    waiters.append(_Waiter(task_id, task_name, task_type))
                continue
            if waiters:
                head = waiters[0]
                if head.task_id == task_id:
                    # 账号空闲且排到队首：按 FIFO 放行并移除等待条目
                    waiters.pop(0)
                    if not waiters:
                        _WAITERS.pop(account, None)
                    _LEASES[account] = _Lease(task_id, task_name, task_type)
                    granted.append(account)
                    continue
                # 前面还有别人在等，不能插队
                queued.append(account)
                if not any(w.task_id == task_id for w in waiters):
                    waiters.append(_Waiter(task_id, task_name, task_type))
                continue
            _LEASES[account] = _Lease(task_id, task_name, task_type)
            granted.append(account)
        return granted, queued


def poll(task_id: str, account: str) -> bool:
    """等待方轮询：排在该账号队首且账号空闲时授予租约并返回 True。"""
    with _LOCK:
        waiters = _WAITERS.get(account)
        if not waiters or account in _LEASES:
            return False
        head = waiters[0]
        if head.task_id != task_id:
            return False
        waiters.pop(0)
        if not waiters:
            _WAITERS.pop(account, None)
        _LEASES[account] = _Lease(head.task_id, head.task_name, head.task_type)
        return True


def release(task_id: str, accounts: Optional[Iterable[str]] = None) -> List[str]:
    """释放该任务持有的账号租约，返回实际释放的账号。"""
    with _LOCK:
        names = _normalize(accounts) if accounts is not None else list(_LEASES.keys())
        released = [a for a in names if a in _LEASES and _LEASES[a].task_id == task_id]
        for account in released:
            _LEASES.pop(account, None)
        return released


def cancel(task_id: str, accounts: Optional[Iterable[str]] = None) -> None:
    """取消该任务的等待条目（不释放已持有的租约）。"""
    with _LOCK:
        targets = (
            _normalize(accounts) if accounts is not None else list(_WAITERS.keys())
        )
        for account in targets:
            waiters = _WAITERS.get(account)
            if not waiters:
                continue
            remain = [w for w in waiters if w.task_id != task_id]
            if remain:
                _WAITERS[account] = remain
            else:
                _WAITERS.pop(account, None)


def release_all(task_id: str) -> None:
    """任务结束/停止：释放租约并取消全部等待条目。"""
    with _LOCK:
        release(task_id)
        cancel(task_id)


def blocked_by(account: str) -> Optional[str]:
    """返回占用该账号的任务展示文本（无占用返回 None）。"""
    with _LOCK:
        lease = _LEASES.get(account)
        if lease is None:
            return None
        return f"{lease.task_name}（{lease.task_type}）"


def task_state(task_id: str) -> Dict[str, list]:
    """该任务当前的租约与排队明细，供任务列表/明细展示。"""
    snap = snapshot()
    return {
        "running": [
            e
            for e in snap["entries"]
            if e["task_id"] == task_id and e["kind"] == "running"
        ],
        "queued": [
            e
            for e in snap["entries"]
            if e["task_id"] == task_id and e["kind"] == "queued"
        ],
    }


def snapshot() -> Dict[str, object]:
    """队列全量快照：租约、等待条目与汇总，供顶部「运行/排队」面板刷新。"""
    now = time.monotonic()
    with _LOCK:
        leases = [
            {
                "kind": "running",
                "account": account,
                "task_id": lease.task_id,
                "task_name": lease.task_name,
                "task_type": lease.task_type,
                "holder": "",
                "seconds": int(max(0, now - lease.acquired_at)),
                "position": 0,
            }
            for account, lease in sorted(_LEASES.items())
        ]
        waiters = []
        for account, items in sorted(_WAITERS.items()):
            holder = _LEASES.get(account)
            for idx, waiter in enumerate(items, start=1):
                waiters.append(
                    {
                        "kind": "queued",
                        "account": account,
                        "task_id": waiter.task_id,
                        "task_name": waiter.task_name,
                        "task_type": waiter.task_type,
                        "holder": (
                            f"{holder.task_name}（{holder.task_type}）"
                            if holder
                            else ""
                        ),
                        "seconds": int(max(0, now - waiter.created_at)),
                        "position": idx,
                    }
                )
        return {
            "entries": leases + waiters,
            "running": len(leases),
            "waiting": len(waiters),
            "tasks": len({e["task_id"] for e in leases + waiters}),
        }


async def acquire_wait(
    task_id: str,
    task_name: str,
    task_type: str,
    account: str,
    *,
    max_entries: int = 0,
    should_stop: Optional[Callable[[], bool]] = None,
    on_wait: Optional[Callable[[str], None]] = None,
    poll_interval: float = 1.0,
) -> bool:
    """异步等待单个账号租约。

    成功返回 True；任务被停止时取消等待并返回 False。
    ``on_wait`` 在首次排队时回调一次，参数为「等待任务X」文本。
    """
    import asyncio

    announced = False
    while True:
        if should_stop is not None and should_stop():
            cancel(task_id, [account])
            return False
        try:
            granted, _queued = request(
                task_id, task_name, task_type, [account], max_entries=max_entries
            )
        except QueueLimitError:
            # 队列已满：不登记条目，稍后重试，避免任务直接失败
            if not announced and on_wait is not None:
                announced = True
                on_wait("排队等待账号空闲（队列已满）")
            await asyncio.sleep(poll_interval)
            continue
        if granted:
            return True
        if not announced and on_wait is not None:
            announced = True
            holder = blocked_by(account)
            on_wait(f"排队中：等待 {holder}" if holder else "排队中")
        if poll(task_id, account):
            return True
        await asyncio.sleep(poll_interval)
