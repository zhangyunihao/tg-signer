"""自动抽奖子任务入口（组合任务用，每账号一个进程）。

用法（由 WebUI 的组合任务启动）::

    python -m tg_signer.giveaway_runner '<json spec>'

spec 为所选抽奖任务配置加上账号与工作目录::

    {"account": "账号名", "workdir": "工作目录", "task": "任务ID",
     "session_dir": ".", "chats": [...], "keywords": [...],
     "auto_scan": true, "auto_join": true, "ignore_history": true,
     "max_joins": null, "delay_min": 3, "delay_max": 10,
     "confirm_wait": 8, "rescue_interval_min": 0, "rescue_limit": 10,
     "win_detect": true}

中奖消息转发目标为全局设置（工作目录 webui_win_settings.json，右上角
「设置」弹窗配置，多目标列表），启动时统一读取，不随任务配置传递。

加载任务配置后运行 GiveawayEngine 至自然结束（补漏关闭时首轮扫描完成
即结束），日志输出到 stdout 并同步落盘到工作目录 logs/giveaway.log。
"""

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from tg_signer.giveaway_core import GiveawayEngine
from tg_signer.giveaway_state import GiveawayState

# 已自带 HH:MM:SS 时间戳的消息不重复加前缀
_TS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}")

# 日志文件达到该大小后滚动为 *.1，避免超限后静默停止落盘
LOG_FILE_MAX_BYTES = 2_000_000


def engine_kwargs_from_spec(spec: dict) -> Dict[str, Any]:
    """把 JSON spec 映射为 GiveawayEngine.start 的关键字参数（纯函数，便于测试）。"""
    max_joins = spec.get("max_joins")

    def _optional_range(prefix: str) -> Optional[Tuple[float, float]]:
        """限流节奏区间：两项都缺省时返回 None（沿用引擎默认值）。"""
        lo = spec.get(f"{prefix}_min")
        hi = spec.get(f"{prefix}_max")
        if lo is None and hi is None:
            return None
        return (float(lo or 0), float(hi or 0))

    def _optional_int(key: str) -> Optional[int]:
        val = spec.get(key)
        if val is None or val == "":
            return None
        return int(val)

    # 兼容单账号（组合任务）与多账号（WebUI 抽奖任务）两种 spec
    if spec.get("accounts") is not None:
        accounts = [
            str(a).strip() for a in spec.get("accounts") or [] if str(a).strip()
        ]
    else:
        accounts = [str(spec.get("account") or "")]
    return {
        "accounts": accounts,
        "chats": [str(c) for c in (spec.get("chats") or [])],
        "session_dir": Path(str(spec.get("session_dir") or ".")),
        "keywords": [str(k) for k in (spec.get("keywords") or [])],
        # 任务类型：channel=频道/群组，bot=抽奖机器人私聊
        "source_type": str(spec.get("source_type") or "channel"),
        "bot_username": str(spec.get("bot_username") or ""),
        "scan_limit": spec.get("scan_limit"),
        "auto_scan": bool(spec.get("auto_scan", True)),
        "auto_join": bool(spec.get("auto_join", True)),
        "ignore_history": bool(spec.get("ignore_history", True)),
        "max_joins": (int(max_joins) if max_joins else None),
        "delay_range": tuple(
            sorted(
                (
                    float(spec.get("delay_min") or 0),
                    float(spec.get("delay_max") or 0),
                )
            )
        ),
        # 加入/解析限流保护（任务级配置，缺省沿用引擎默认值）
        "join_pace_range": _optional_range("join_pace"),
        "resolve_pace_range": _optional_range("resolve_pace"),
        "join_hourly_cap": _optional_int("join_hourly_cap"),
        "join_daily_cap": _optional_int("join_daily_cap"),
        "join_flood_buffer": _optional_int("join_flood_buffer"),
        "confirm_wait": (
            int(spec["confirm_wait"]) if spec.get("confirm_wait") is not None else 8
        ),
        "rescue_interval": max(0, int(spec.get("rescue_interval_min") or 0)) * 60,
        "rescue_limit": (
            max(1, int(spec["rescue_limit"]))
            if spec.get("rescue_limit") is not None
            else 10
        ),
        "win_detect": bool(spec.get("win_detect", True)),
    }


def _make_log(workdir: str, task: str) -> Callable[[str], None]:
    log_file = Path(workdir) / "logs" / "giveaway.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    def _log(msg: str) -> None:
        ts = f"{datetime.now():%H:%M:%S}"
        if not _TS_RE.match(msg):  # 已自带 HH:MM:SS 的消息不重复加前缀
            msg = f"{ts} {msg}"
        line = f"[#{task}] {msg}"
        print(line, flush=True)
        try:
            if log_file.exists() and log_file.stat().st_size >= LOG_FILE_MAX_BYTES:
                backup = log_file.with_name(log_file.name + ".1")
                try:
                    if backup.exists():
                        backup.unlink()
                    log_file.replace(backup)
                except OSError:
                    pass
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    return _log


def _event_writer(path: Optional[str]) -> Callable[[dict], None]:
    """事件通道：向 JSONL 文件追加一条记录（WebUI 父进程轮询读取）。

    未指定 events_file 时（组合任务）返回空实现，不产生额外 IO。
    """
    if not path:
        return lambda payload: None

    def _emit(payload: dict) -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

    return _emit


def _stats_payload(engine: GiveawayEngine) -> Dict[str, Any]:
    return {
        "joined": int(engine.joined),
        "by_account": dict(engine.joined_by_account),
        "by_chat": {k: dict(v) for k, v in engine.joined_by_account_chat.items()},
        "progress": engine.progress_text() or "",
    }


async def _stats_loop(engine: GiveawayEngine, emit: Callable[[dict], None]) -> None:
    """每秒回报一次运行统计，供 WebUI 实时展示进度与参与数。"""
    while engine.running:
        emit({"kind": "stats", **_stats_payload(engine)})
        await asyncio.sleep(1)


async def run_giveaway(spec: dict) -> int:
    account = str(spec.get("account") or "")
    accounts = spec.get("accounts")
    if accounts is not None:
        accounts = [str(a).strip() for a in accounts if str(a).strip()]
    else:
        accounts = [account] if account else []
    workdir = str(spec.get("workdir") or ".")
    if not accounts:
        print("缺少账号", flush=True)
        return 2
    from tg_signer.webui.data import chat_username_map, load_two_way_accounts
    from tg_signer.webui.giveaway import (
        load_giveaway_scan_cap,
        load_win_forward_targets,
        load_win_sweep_limit,
    )
    from tg_signer.webui.random_chat import parse_chat_id

    emit = _event_writer(spec.get("events_file"))
    kwargs = engine_kwargs_from_spec(spec)
    if kwargs["source_type"] == "bot" and kwargs["bot_username"]:
        # 机器人私聊来源：监控对象固定为该机器人本身
        kwargs["chats"] = [kwargs["bot_username"]]
    kwargs["chats"] = [
        parse_chat_id(c) if parse_chat_id(c) is not None else c for c in kwargs["chats"]
    ]
    # 中奖消息转发目标：全局设置（右上角「设置」弹窗配置），多目标列表
    # 数字目标（如 -100xxx 频道 id）需转为 int，否则 Telethon 会当作手机号 resolve
    kwargs["win_forward_target"] = [
        parse_chat_id(t) if parse_chat_id(t) is not None else t
        for t in load_win_forward_targets(workdir)
    ]
    # 中奖回扫条数：全局设置，回扫各 bot 私聊最近 N 条消息找中奖通知
    kwargs["win_sweep_limit"] = load_win_sweep_limit(workdir)
    # 频道/群组单轮扫描上限：全局设置，关键词未命中时也按此条数截断
    kwargs["scan_cap"] = load_giveaway_scan_cap(workdir)
    engine = GiveawayEngine()
    finished = asyncio.Event()

    def _on_finish() -> None:
        emit({"kind": "stats", **_stats_payload(engine)})
        emit({"kind": "finish"})
        finished.set()

    kwargs["state"] = GiveawayState(workdir)
    kwargs["log"] = _make_log(workdir, str(spec.get("task") or accounts[0]))
    kwargs["on_finish"] = _on_finish
    kwargs["on_event"] = lambda ev: emit({"kind": "event", "event": ev})
    started = await engine.start(
        two_way_accounts=sorted(load_two_way_accounts(workdir)),
        chat_usernames=chat_username_map(workdir),
        **kwargs,
    )
    if not started:
        print(f"任务 #{spec.get('task')} 未能启动（可能配置为空）", flush=True)
        return 1
    stats_task = asyncio.create_task(_stats_loop(engine, emit))
    try:
        await finished.wait()
    finally:
        stats_task.cancel()
    return 0


def main() -> None:
    # Windows 下 stdout 为管道时默认按 GBK 编码，WebUI 侧按 UTF-8 解码会导致中文日志乱码
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if len(sys.argv) < 2:
        print("缺少自动抽奖任务参数", flush=True)
        raise SystemExit(2)
    try:
        spec = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(f"自动抽奖任务参数解析失败: {exc}", flush=True)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(run_giveaway(spec)))


if __name__ == "__main__":
    main()
