"""批量退频道子任务入口（组合任务用，每账号一个进程）。

用法（由 WebUI 的组合任务启动）::

    python -m tg_signer.leave_runner '<json spec>'

spec 格式::

    {"account": "账号名", "session_dir": "工作目录",
     "days": 7, "include_groups": false, "skip_pinned": true,
     "exclude_keywords": ["抽奖", ...]}

行为与「批量退频道」tab 一致但非交互：扫描超过 N 天未更新的频道/群组
（始终跳过自己是创建者、置顶对话），排除命中关键字的对话后全部自动退出
（并发 8）。日志输出到 stdout 由 WebUI 组合任务采集。
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Dict, List

from tg_signer.core import get_client, safe_disconnect
from tg_signer.webui.auto_leave import leave_one, scan_stale_chats


def filter_candidates(
    rows: List[Dict], exclude_keywords: List[str]
) -> List[Dict]:
    """按标题关键字过滤扫描结果（命中即排除，防止误退）。"""
    kws = [str(k) for k in (exclude_keywords or []) if k]
    if not kws:
        return list(rows)
    return [
        r for r in rows if not any(k in str(r.get("title") or "") for k in kws)
    ]


async def run_leave(spec: dict) -> int:
    account = str(spec.get("account") or "")
    if not account:
        print("缺少账号", flush=True)
        return 2

    def log(msg: str) -> None:
        print(msg, flush=True)

    rows = await scan_stale_chats(
        account,
        Path(str(spec.get("session_dir") or ".")),
        max(1, int(spec.get("days") or 7)),
        bool(spec.get("include_groups")),
        bool(spec.get("skip_pinned", True)),
        log,
    )
    kws = list(spec.get("exclude_keywords") or [])
    if kws:
        before = len(rows)
        rows = filter_candidates(rows, kws)
        log(f"[{account}] 排除关键字过滤：{before} -> {len(rows)} 个候选")
    if not rows:
        log(f"[{account}] 没有需要退出的对话")
        return 0

    client = None
    try:
        client = get_client(account, workdir=str(spec.get("session_dir") or "."))
        ok = await asyncio.wait_for(client.connect(), 30)
        if not ok:
            log(f"[{account}] 会话未授权，跳过 {len(rows)} 个")
            return 1
        log(f"[{account}] 开始批量退出 {len(rows)} 个对话（并发 8）")
        done_keys: set = set()
        done_lock = asyncio.Lock()
        sem = asyncio.Semaphore(8)

        async def _do_leave(row: Dict) -> None:
            async with sem:
                success = await leave_one(
                    client, row["chat_id"], row["title"], log
                )
                if success:
                    async with done_lock:
                        done_keys.add(row["key"])

        await asyncio.gather(*[_do_leave(row) for row in rows])
        log(f"[{account}] 批量退出完成：成功 {len(done_keys)}/{len(rows)}")
        return 0 if len(done_keys) == len(rows) else 1
    except asyncio.TimeoutError:
        log(f"[{account}] 连接超时（30s）")
        return 1
    except Exception as exc:
        log(f"[{account}] 退出失败: {exc}")
        return 1
    finally:
        if client is not None:
            await safe_disconnect(client)


def main() -> None:
    if len(sys.argv) < 2:
        print("缺少退频道任务参数", flush=True)
        raise SystemExit(2)
    try:
        spec = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(f"退频道任务参数解析失败: {exc}", flush=True)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(run_leave(spec)))


if __name__ == "__main__":
    main()
