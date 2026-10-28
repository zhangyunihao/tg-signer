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
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, Optional

from tg_signer.webui.common import _assign_to_job

logger = logging.getLogger(__name__)

# 停止任务时先 terminate，超过该秒数仍未退出则 kill
STOP_TIMEOUT = 5.0

# 读取 events 文件的轮询间隔（秒）
EVENTS_POLL_INTERVAL = 0.2


class GiveawayProcess:
    """管理一个抽奖任务子进程，接口与 GiveawayEngine 的展示部分保持一致。"""

    def __init__(self, name: str, workdir) -> None:
        self.name = name
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
        self._events_file = log_dir / f"giveaway-{self.name}-{uuid.uuid4().hex}.jsonl"

        spec = self._build_spec(cfg)
        spec["events_file"] = str(self._events_file)
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self.proc = subprocess.Popen(
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
            _assign_to_job(self.proc)
        except Exception:
            logger.debug("抽奖任务子进程启动失败", exc_info=True)
            self.proc = None
            self._cleanup_events_file()
            return False

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_events, daemon=True).start()
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
            except OSError:
                pass
        except OSError:
            logger.debug("停止抽奖任务子进程失败", exc_info=True)

    def _build_spec(self, cfg: dict) -> dict:
        return {
            "accounts": [str(a) for a in (cfg.get("accounts") or [])],
            "task": self.name,
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

    def _read_stdout(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        log = self._log
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

    def _read_events(self) -> None:
        path = self._events_file
        proc = self.proc
        if path is None or proc is None:
            return
        try:
            # 事件文件由子进程首次回报时创建，先等它出现
            while not path.exists():
                if proc.poll() is not None:
                    # 子进程没来得及回报就退出：仍需走结束回调，
                    # 否则任务状态与统计不会落盘
                    self._finalize()
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
                            self._handle_event_line(raw.decode("utf-8", "replace"))
                        continue
                    if proc.poll() is not None:
                        break
                    time.sleep(EVENTS_POLL_INTERVAL)
                # 进程已退出：写出时通常带换行，残留半行也尝试解析
                if buf:
                    self._handle_event_line(buf.decode("utf-8", "replace"))
        except OSError:
            logger.debug("读取抽奖任务事件文件失败", exc_info=True)
        finally:
            self._finalize()

    def _handle_event_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        kind = payload.get("kind")
        if kind == "stats":
            self._apply_stats(payload)
        elif kind == "event":
            ev = payload.get("event")
            if isinstance(ev, dict) and self._on_event is not None and self._loop:
                self._loop.call_soon_threadsafe(self._on_event, ev)

    def _apply_stats(self, payload: dict) -> None:
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

    def _finalize(self) -> None:
        if self._finished_cb_called:
            return
        self._finished_cb_called = True
        self._cleanup_events_file()
        if self._on_finish is None:
            return
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._on_finish)
        else:
            self._on_finish()

    def _cleanup_events_file(self) -> None:
        path = self._events_file
        self._events_file = None
        if path is None:
            return
        try:
            path.unlink()
        except OSError:
            pass
