"""一键停止所有任务：结束各面板运行中的任务，释放会话与数据库占用。

各面板分别持有自己的运行状态（子进程句柄、协程引擎、停止开关），这里统一
编排。任务进程退出/客户端断开后，Telegram 会话 SQLite 与 giveaway_state.db
上的占用随之释放，之前出现的「database is locked」即可消除。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict, Set


async def stop_all_tasks(workdir) -> Dict[str, int]:
    """停止当前工作目录下所有运行中的任务，返回各类型实际停止的数量。"""
    from tg_signer.webui.auto_leave import stop_all_leave_tasks
    from tg_signer.webui.giveaway import stop_all_giveaway_tasks
    from tg_signer.webui.random_chat import stop_all_random_chat_tasks
    from tg_signer.webui.task_panel import stop_all_runs

    counts: Dict[str, int] = {}
    # 子进程类任务需要等进程真正退出（每个最长 5 秒），放线程里避免阻塞界面
    counts["签到/组合/自动化"] = await asyncio.to_thread(stop_all_runs)
    counts["自动抽奖"] = await asyncio.to_thread(stop_all_giveaway_tasks, workdir)
    counts["随机发言"] = await stop_all_random_chat_tasks(workdir)
    counts["批量退频道"] = stop_all_leave_tasks(workdir)
    return counts


def running_tabs(workdir) -> Set[str]:
    """当前有任务正在执行的主 tab 名集合，供标签栏高亮使用。

    与 stop_all_tasks 同源：各面板的运行状态只登记在各自模块里，这里只读汇总
    （子进程存活 / 引擎 running / 停止开关在册），不改变任何任务状态。
    运行态不依赖面板是否构建、是否停留在该 tab，服务重启后残留的记录也只
    按真实运行情况统计。
    """
    from tg_signer.webui import auto_leave, giveaway, random_chat, task_panel

    prefix = f"{Path(workdir).resolve()}|"
    running: set[str] = set()
    # 签到与组合任务共用 TaskRunnerBlock.runs，按任务类型区分归属；
    # 只看子进程存活：记录状态只在签到 tab 轮询到时才结算，跨 tab 不可靠
    for run in task_panel.TaskRunnerBlock.runs.values():
        procs = run.get("procs") or []
        if not any(
            item.get("proc") is not None and item["proc"].poll() is None
            for item in procs
        ):
            continue
        task_type = str((run.get("record") or {}).get("task_type") or "")
        running.add("combo" if "组合" in task_type else "run")
    for key, engine in giveaway._GLOBAL_ENGINES.items():
        if str(key).startswith(prefix) and engine is not None and engine.running:
            running.add("giveaway")
    for key, engine in random_chat._GLOBAL_ENGINES.items():
        if str(key).startswith(prefix) and engine is not None and engine.running:
            running.add("random")
    if any(str(key).startswith(prefix) for key in auto_leave._ACTIVE_STOPS):
        running.add("leave")
    return running
