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
from typing import Any, Callable, Dict

from tg_signer.giveaway_core import GiveawayEngine
from tg_signer.giveaway_state import GiveawayState

# 已自带 HH:MM:SS 时间戳的消息不重复加前缀
_TS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}")


def engine_kwargs_from_spec(spec: dict) -> Dict[str, Any]:
    """把 JSON spec 映射为 GiveawayEngine.start 的关键字参数（纯函数，便于测试）。"""
    max_joins = spec.get("max_joins")
    return {
        "accounts": [str(spec.get("account") or "")],
        "chats": [str(c) for c in (spec.get("chats") or [])],
        "session_dir": Path(str(spec.get("session_dir") or ".")),
        "keywords": [str(k) for k in (spec.get("keywords") or [])],
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
            if not log_file.exists() or log_file.stat().st_size < 2_000_000:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass

    return _log


async def run_giveaway(spec: dict) -> int:
    account = str(spec.get("account") or "")
    workdir = str(spec.get("workdir") or ".")
    if not account:
        print("缺少账号", flush=True)
        return 2
    from tg_signer.webui.data import chat_username_map, load_two_way_accounts
    from tg_signer.webui.giveaway import (
        load_win_forward_targets,
        load_win_sweep_limit,
    )
    from tg_signer.webui.random_chat import parse_chat_id

    kwargs = engine_kwargs_from_spec(spec)
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
    engine = GiveawayEngine()
    finished = asyncio.Event()
    kwargs["state"] = GiveawayState(workdir)
    kwargs["log"] = _make_log(workdir, str(spec.get("task") or account))
    kwargs["on_finish"] = finished.set
    started = await engine.start(
        two_way_accounts=sorted(load_two_way_accounts(workdir)),
        chat_usernames=chat_username_map(workdir),
        **kwargs,
    )
    if not started:
        print(f"任务 #{spec.get('task')} 未能启动（可能配置为空）", flush=True)
        return 1
    await finished.wait()
    return 0


def main() -> None:
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
