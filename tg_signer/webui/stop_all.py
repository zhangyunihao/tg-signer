"""一键停止所有任务：结束各面板运行中的任务，释放会话与数据库占用。

各面板分别持有自己的运行状态（子进程句柄、协程引擎、停止开关），这里统一
编排。任务进程退出/客户端断开后，Telegram 会话 SQLite 与 giveaway_state.db
上的占用随之释放，之前出现的「database is locked」即可消除。
"""

from __future__ import annotations

import asyncio
from typing import Dict


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
