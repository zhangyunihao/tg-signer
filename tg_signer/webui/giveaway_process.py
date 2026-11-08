"""抽奖任务子进程句柄：引擎跑在独立进程，WebUI 只负责控制与展示。

这样长时间运行/多账号并发的 GiveawayEngine 不会与 NiceGUI 事件循环抢 CPU、
不会因引擎内部阻塞拖慢页面；进程崩溃也只影响该任务。
子进程通过 giveaway_runner 的 events 文件回报事件、统计与结束信号。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, Optional

from tg_signer.webui import account_queue
from tg_signer.webui.common import _assign_to_job, load_account_parallel_limit

logger = logging.getLogger(__name__)

# 停止任务时先 terminate，超过该秒数仍未退出则 kill
STOP_TIMEOUT = 5.0

# 读取 events 文件的轮询间隔（秒）
EVENTS_POLL_INTERVAL = 0.2


class GiveawayProcess:
    """管理一个抽奖任务子进程，接口与 GiveawayEngine 的展示部分保持一致。

    每一轮运行（start → 子进程退出）的读取线程、事件文件与结束回调都绑定本轮
    传入的参数，收尾时先核对「还是不是当前这一轮」：否则上一轮的收尾线程迟到时，
    会把新一轮的事件文件删掉、把新一轮的结束回调提前触发，导致任务反复启动后
    状态卡在「运行中」、统计与结束信号丢失。
    """

    def __init__(self, name: str, workdir, task_name: Optional[str] = None) -> None:
        self.name = name
        # 事件文件用的展示名（可能含账号名），任务名保持原任务 ID 供状态库使用
        self.task_name = task_name or name
        self.workdir = Path(workdir)
        self.proc: Optional[subprocess.Popen] = None
        # 运行统计：由子进程每秒回报一次，结束后保留最后值供「上次运行」展示
        self.joined = 0
        self.joined_by_account: Dict[str, int] = {}
        self.joined_by_account_chat: Dict[str, Dict[str, int]] = {}
        self._progress = ""
        # 中奖消息回扫进度（与频道扫描分开显示）
        self.win_sweep_text = ""
        self._events_file: Optional[Path] = None
        self._log: Optional[Callable[[str], None]] = None
        self._on_event: Optional[Callable[[dict], None]] = None
        self._on_finish: Optional[Callable[[], None]] = None
        # 统计回报：便于上层把参与明细边跑边落盘，停止/重启后不丢
        self._on_stats: Optional[Callable[[dict], None]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._finished_cb_called = False

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def progress_text(self) -> str:
        return self._progress

    def start(
        self,
        cfg: dict,
        *,
        log: Callable[[str], None],
        on_event: Callable[[dict], None],
        on_finish: Callable[[], None],
        on_stats: Optional[Callable[[dict], None]] = None,
    ) -> bool:
        """启动子进程；已在运行返回 False。"""
        if self.running:
            return False
        self._log = log
        self._on_event = on_event
        self._on_finish = on_finish
        self._on_stats = on_stats
        self._loop = asyncio.get_running_loop()
        self._finished_cb_called = False
        self.joined = 0
        self.joined_by_account = {}
        self.joined_by_account_chat = {}
        self._progress = ""
        self.win_sweep_text = ""

        log_dir = self.workdir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^0-9A-Za-z_.-]+", "_", self.name)[:40]
        events_file = log_dir / f"giveaway-{safe_name}-{uuid.uuid4().hex}.jsonl"
        self._events_file = events_file

        spec = self._build_spec(cfg)
        spec["events_file"] = str(events_file)
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tg_signer.giveaway_runner",
                    json.dumps(spec, ensure_ascii=False),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
            _assign_to_job(proc)
        except Exception:
            logger.debug("抽奖任务子进程启动失败", exc_info=True)
            self.proc = None
            self._cleanup_events_file()
            return False

        self.proc = proc
        # 线程持有本轮自己的参数：下一轮 start 覆盖实例属性后，旧线程仍按本轮收尾
        threading.Thread(
            target=self._read_stdout, args=(proc, log), daemon=True
        ).start()
        threading.Thread(
            target=self._read_events,
            args=(proc, events_file, on_event, on_finish, self._loop),
            daemon=True,
        ).start()
        return True

    def stop(self) -> None:
        """终止子进程；结束回调由读取线程统一触发。"""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                # 等它真正退出，停止后紧接着点「启动」才不会提示「收尾中」
                proc.wait(timeout=STOP_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            logger.debug("停止抽奖任务子进程失败", exc_info=True)

    def _build_spec(self, cfg: dict) -> dict:
        return {
            "accounts": [str(a) for a in (cfg.get("accounts") or [])],
            "task": self.task_name,
            "workdir": str(self.workdir),
            "session_dir": str(cfg.get("session_dir") or "."),
            "chats": [str(c) for c in (cfg.get("chats") or [])],
            "keywords": [str(k) for k in (cfg.get("keywords") or [])],
            # 任务类型：channel=频道/群组，bot=抽奖机器人私聊
            "source_type": str(cfg.get("source_type") or "channel"),
            "bot_username": str(cfg.get("bot_username") or ""),
            "scan_limit": cfg.get("scan_limit"),
            "auto_scan": bool(cfg.get("auto_scan", True)),
            "auto_join": bool(cfg.get("auto_join", True)),
            "ignore_history": bool(cfg.get("ignore_history", True)),
            "max_joins": cfg.get("max_joins"),
            "delay_min": cfg.get("delay_min") or 0,
            "delay_max": cfg.get("delay_max") or 0,
            "confirm_wait": cfg.get("confirm_wait") or 8,
            "rescue_interval_min": cfg.get("rescue_interval_min") or 0,
            "rescue_limit": cfg.get("rescue_limit") or 10,
            "win_detect": bool(cfg.get("win_detect", True)),
            # 限流保护：加入/解析节奏与配额（None 表示沿用引擎默认值）
            "join_pace_min": cfg.get("join_pace_min"),
            "join_pace_max": cfg.get("join_pace_max"),
            "resolve_pace_min": cfg.get("resolve_pace_min"),
            "resolve_pace_max": cfg.get("resolve_pace_max"),
            "join_hourly_cap": cfg.get("join_hourly_cap"),
            "join_daily_cap": cfg.get("join_daily_cap"),
            "join_flood_buffer": cfg.get("join_flood_buffer"),
        }

    def _read_stdout(self, proc: Optional[subprocess.Popen] = None, log=None) -> None:
        proc = self.proc if proc is None else proc
        if log is None:
            log = self._log
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line and log is not None:
                try:
                    log(line)
                except Exception:
                    logger.debug("写入抽奖任务日志失败", exc_info=True)
        try:
            proc.stdout.close()
        except OSError:
            pass

    def _read_events(
        self,
        proc: Optional[subprocess.Popen] = None,
        path: Optional[Path] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        on_finish: Optional[Callable[[], None]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        proc = self.proc if proc is None else proc
        path = self._events_file if path is None else path
        on_event = self._on_event if on_event is None else on_event
        on_finish = self._on_finish if on_finish is None else on_finish
        loop = self._loop if loop is None else loop
        if path is None or proc is None:
            return
        try:
            # 事件文件由子进程首次回报时创建，先等它出现
            while not path.exists():
                if proc.poll() is not None:
                    # 子进程没来得及回报就退出：仍需走结束回调，
                    # 否则任务状态与统计不会落盘
                    self._finalize(proc, path, on_finish, loop)
                    return
                time.sleep(EVENTS_POLL_INTERVAL)
            with open(path, "rb") as fp:
                buf = b""
                while True:
                    chunk = fp.read(65536)
                    if chunk:
                        buf += chunk
                        *lines, buf = buf.split(b"\n")
                        for raw in lines:
                            self._handle_event_line(
                                raw.decode("utf-8", "replace"), proc
                            )
                        continue
                    if proc.poll() is not None:
                        break
                    time.sleep(EVENTS_POLL_INTERVAL)
                # 进程已退出：写出时通常带换行，残留半行也尝试解析
                if buf:
                    self._handle_event_line(buf.decode("utf-8", "replace"), proc)
        except OSError:
            logger.debug("读取抽奖任务事件文件失败", exc_info=True)
        finally:
            self._finalize(proc, path, on_finish, loop)

    def _handle_event_line(self, line: str, proc: Optional[object] = None) -> None:
        proc = self.proc if proc is None else proc
        line = line.strip()
        if not line:
            return
        # 上一轮子进程迟到的回报：直接忽略，避免污染新一轮的状态与回调
        if proc is not None and proc is not self.proc:
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        kind = payload.get("kind")
        if kind == "stats":
            self._apply_stats(payload, proc)
        elif kind == "event":
            ev = payload.get("event")
            if isinstance(ev, dict) and self._on_event is not None and self._loop:
                self._loop.call_soon_threadsafe(self._on_event, ev)

    def _apply_stats(self, payload: dict, proc: Optional[object] = None) -> None:
        if proc is not None and proc is not self.proc:
            return
        try:
            self.joined = int(payload.get("joined") or 0)
        except (TypeError, ValueError):
            self.joined = 0
        by_account = payload.get("by_account")
        if isinstance(by_account, dict):
            self.joined_by_account = {str(k): int(v) for k, v in by_account.items()}
        by_chat = payload.get("by_chat")
        if isinstance(by_chat, dict):
            self.joined_by_account_chat = {
                str(k): {str(c): int(n) for c, n in (v or {}).items()}
                for k, v in by_chat.items()
            }
        self._progress = str(payload.get("progress") or "")
        self.win_sweep_text = str(payload.get("sweep") or "")
        cb = self._on_stats
        if cb is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(cb, payload)
            except RuntimeError:
                logger.debug("回报抽奖统计失败", exc_info=True)

    def _finalize(
        self,
        proc: Optional[object] = None,
        path: Optional[Path] = None,
        on_finish: Optional[Callable[[], None]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        proc = self.proc if proc is None else proc
        path = self._events_file if path is None else path
        on_finish = self._on_finish if on_finish is None else on_finish
        loop = self._loop if loop is None else loop
        # 旧一轮的收尾线程迟到：只清理它自己的事件文件，
        # 不得重置新一轮的完成标记、也不得触发新一轮的结束回调
        if proc is not None and proc is not self.proc:
            if path is not None and path != self._events_file:
                self._unlink_events(path)
            return
        if self._finished_cb_called:
            return
        self._finished_cb_called = True
        if path is not None and path == self._events_file:
            self._events_file = None
        self._unlink_events(path)
        if on_finish is None:
            return
        if loop is not None:
            loop.call_soon_threadsafe(on_finish)
        else:
            on_finish()

    @staticmethod
    def _unlink_events(path: Optional[Path]) -> None:
        if path is None:
            return
        try:
            path.unlink()
        except OSError:
            pass

    def _cleanup_events_file(self) -> None:
        path = self._events_file
        self._events_file = None
        self._unlink_events(path)


class GiveawayTaskRunner:
    """一个抽奖任务按账号拆分的子进程组，对外接口与单个 GiveawayProcess 一致。

    每个账号独立启动一个 ``giveaway_runner`` 子进程，因此可以做到账号级排队：
    账号被其它任务占用时只有该账号等待，空闲账号立即开始。
    状态库（giveaway_state）使用 WAL + busy_timeout，支持多进程并发读写，
    统计、日志、进度在这里聚合后交给界面，界面看到的仍是一个任务。
    """

    def __init__(self, name: str, workdir) -> None:
        self.name = name
        self.workdir = Path(workdir)
        self.queue_id = f"giveaway|{self.workdir.resolve()}|{name}"
        self._accounts: list[str] = []
        self._cfg: dict = {}
        self._engines: Dict[str, GiveawayProcess] = {}
        self._pending: Dict[str, asyncio.Task] = {}
        self._max_concurrent = 0
        self._queue_cap = 0
        self._reject_overlap = False
        self._last_error = ""
        self._stop_requested = False
        self._finished = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._log: Optional[Callable[[str], None]] = None
        self._on_event: Optional[Callable[[dict], None]] = None
        self._on_finish: Optional[Callable[[], None]] = None
        self._on_stats: Optional[Callable[[dict], None]] = None

    @property
    def running(self) -> bool:
        if self._pending:
            return True
        return any(engine.running for engine in self._engines.values())

    @property
    def last_error(self) -> str:
        """最近一次启动被拒绝的原因（为空表示未被拒绝）。"""
        return self._last_error

    @property
    def joined(self) -> int:
        return sum(int(engine.joined) for engine in self._engines.values())

    @property
    def joined_by_account(self) -> Dict[str, int]:
        merged: Dict[str, int] = {}
        for engine in self._engines.values():
            for account, count in engine.joined_by_account.items():
                merged[str(account)] = merged.get(str(account), 0) + int(count)
        return merged

    @property
    def joined_by_account_chat(self) -> Dict[str, Dict[str, int]]:
        merged: Dict[str, Dict[str, int]] = {}
        for engine in self._engines.values():
            for account, chats in engine.joined_by_account_chat.items():
                target = merged.setdefault(str(account), {})
                for chat, count in chats.items():
                    target[str(chat)] = target.get(str(chat), 0) + int(count)
        return merged

    @property
    def win_sweep_text(self) -> str:
        texts: list[str] = []
        for engine in self._engines.values():
            text = engine.win_sweep_text
            if text and text not in texts:
                texts.append(text)
        return "；".join(texts[:3])

    def progress_text(self) -> str:
        active = [engine for engine in self._engines.values() if engine.running]
        if not active:
            if self._pending:
                return f"排队中（{len(self._pending)} 个账号等待）"
            return ""
        text = f"运行中（{len(active)}/{len(self._accounts)} 个账号）"
        detail = next(
            (engine.progress_text() for engine in active if engine.progress_text()),
            "",
        )
        return f"{text} {detail}" if detail else text

    def _account_cfg(self, account: str) -> dict:
        cfg = dict(self._cfg)
        cfg["accounts"] = [account]
        return cfg

    def _emit_log(self, account: str, line: str) -> None:
        if self._log is None:
            return
        # 子进程日志自带「[时间] [级别] [#任务 账号]」前缀，原样上送；
        # 其它行（如 Python 异常栈）补上账号，便于多子进程时区分来源
        if re.match(r"^\[[0-9:]{8}\] \[[A-Z]+\] ", line) or re.match(
            r"^\[#[^\]]+\] \d{2}:\d{2}:\d{2} ", line
        ):
            self._log(line)
        else:
            self._log(f"[{account}] {line}")

    def _aggregate_stats_payload(self) -> dict:
        return {
            "joined": self.joined,
            "by_account": self.joined_by_account,
            "by_chat": {
                account: dict(chats)
                for account, chats in self.joined_by_account_chat.items()
            },
            "progress": self.progress_text(),
            "sweep": self.win_sweep_text,
        }

    def _account_stats(self, account: str, _payload: dict) -> None:
        # 每个账号的统计先落到各自的 GiveawayProcess，这里统一聚合后回传界面
        if self._on_stats is None:
            return
        try:
            self._on_stats(self._aggregate_stats_payload())
        except Exception:
            logger.debug("聚合抽奖统计失败", exc_info=True)

    def _launch_account(self, account: str) -> bool:
        engine = GiveawayProcess(
            f"{self.name}#{account}", self.workdir, task_name=self.name
        )
        # 先登记再启动：启动失败/立即结束时 running 判断与结束回调都只看 _engines
        self._engines[account] = engine
        started = engine.start(
            self._account_cfg(account),
            log=lambda line, a=account: self._emit_log(a, line),
            on_event=self._on_event or (lambda _ev: None),
            on_finish=lambda a=account: self._account_finished(a),
            on_stats=lambda payload, a=account: self._account_stats(a, payload),
        )
        if not started:
            self._engines.pop(account, None)
            account_queue.release(self.queue_id, [account])
            self._emit_log(account, "子进程启动失败，跳过该账号")
            self._notify_finish_if_idle()
            return False
        if self._stop_requested:
            # 停止请求恰好发生在启动前：立即停掉刚拉起的子进程
            engine.stop()
        return True

    async def _wait_account(self, account: str) -> None:
        """等本地并发槽位与跨任务账号租约，条件满足后启动该账号的子进程。"""
        try:
            while True:
                if self._stop_requested:
                    return
                if self._reject_overlap:
                    # 启动时检查后账号才被别的任务抢到：直接跳过该账号，不排队
                    holder = account_queue.blocked_by(account)
                    if holder is not None:
                        self._pending.pop(account, None)
                        self._emit_log(
                            account, f"账号被 {holder} 占用，已跳过（未排队）"
                        )
                        self._notify_finish_if_idle()
                        return
                if (
                    self._max_concurrent > 0
                    and sum(1 for e in self._engines.values() if e.running)
                    >= self._max_concurrent
                ):
                    await asyncio.sleep(1.0)
                    continue
                try:
                    granted, _queued = account_queue.request(
                        self.queue_id,
                        self.name,
                        "自动抽奖",
                        [account],
                        max_entries=self._queue_cap,
                    )
                except account_queue.QueueLimitError:
                    await asyncio.sleep(1.0)
                    continue
                if not granted and not account_queue.poll(self.queue_id, account):
                    await asyncio.sleep(1.0)
                    continue
                self._pending.pop(account, None)
                if self._stop_requested:
                    account_queue.release(self.queue_id, [account])
                    return
                self._launch_account(account)
                return
        except asyncio.CancelledError:
            account_queue.release(self.queue_id, [account])
            raise

    def _account_finished(self, account: str) -> None:
        account_queue.release(self.queue_id, [account])
        if self._on_stats is not None:
            try:
                self._on_stats(self._aggregate_stats_payload())
            except Exception:
                logger.debug("聚合抽奖统计失败", exc_info=True)
        self._notify_finish_if_idle()

    def _notify_finish_if_idle(self) -> None:
        if self._finished or self.running:
            return
        self._finished = True
        if self._on_finish is not None:
            self._on_finish()

    def start(
        self,
        cfg: dict,
        *,
        log: Callable[[str], None],
        on_event: Callable[[dict], None],
        on_finish: Callable[[], None],
        on_stats: Optional[Callable[[dict], None]] = None,
    ) -> bool:
        """启动任务：空闲账号立即开始，被占用账号进入跨任务队列等待。"""
        if self.running:
            return False
        accounts = [str(a) for a in (cfg.get("accounts") or []) if str(a).strip()]
        if not accounts:
            return False
        self._cfg = dict(cfg)
        self._accounts = accounts
        self._engines = {}
        self._pending = {}
        self._stop_requested = False
        self._finished = False
        self._log = log
        self._on_event = on_event
        self._on_finish = on_finish
        self._on_stats = on_stats
        self._loop = asyncio.get_running_loop()
        self._max_concurrent = load_account_parallel_limit(self.workdir)
        self._queue_cap = account_queue.queue_cap(self.workdir)
        self._reject_overlap = account_queue.reject_on_overlap(self.workdir)
        self._last_error = ""
        # 全局设置「账号被占用时直接报错」：任一账号与运行中任务重叠即拒绝启动
        try:
            account_queue.ensure_available(self.workdir, accounts)
        except account_queue.AccountBusyError as exc:
            self._last_error = str(exc)
            log(self._last_error)
            return False

        # 启动前检查排队上限：首批要启动的账号已把队列占满时直接拒绝
        probe = accounts[: self._max_concurrent] if self._max_concurrent else accounts
        busy_now = sum(
            1 for account in probe if account_queue.blocked_by(account) is not None
        )
        if (
            self._queue_cap > 0
            and busy_now
            and account_queue.waiting_count() + busy_now > self._queue_cap
        ):
            log("排队任务已满，请等待运行中的任务结束后再启动")
            return False

        for account in accounts:
            self._pending[account] = self._loop.create_task(self._wait_account(account))
        return True

    def _cancel_pending(self) -> None:
        """取消仍在排队/等槽位的账号（只能在事件循环线程调用）。"""
        for task in list(self._pending.values()):
            task.cancel()
        self._pending.clear()

    def stop(self) -> None:
        """停止全部子进程并取消仍在排队的账号（可由任意线程调用）。"""
        self._stop_requested = True
        loop = self._loop
        if loop is not None:
            try:
                # 取消 asyncio.Task 只能在事件循环线程执行
                loop.call_soon_threadsafe(self._cancel_pending)
            except RuntimeError:
                logger.debug("提交抽奖排队取消失败", exc_info=True)
        else:
            self._cancel_pending()
        account_queue.release_all(self.queue_id)
        for engine in list(self._engines.values()):
            if engine.running:
                engine.stop()
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._notify_finish_if_idle)
            except RuntimeError:
                logger.debug("提交抽奖任务结束回调失败", exc_info=True)
        else:
            self._notify_finish_if_idle()
